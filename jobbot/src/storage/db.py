"""SQLite storage layer for jobs, applications, and events."""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


DB_PATH = Path(__file__).resolve().parents[2] / "data" / "jobs.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs_seen (
    dedup_key TEXT PRIMARY KEY,
    job_url TEXT NOT NULL,
    company TEXT,
    role_title TEXT,
    role_family TEXT,
    location TEXT,
    match_score REAL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS applications (
    app_id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'DISCOVERED',
    resume_version TEXT,
    submission_proof TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (dedup_key) REFERENCES jobs_seen(dedup_key)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    app_id TEXT,
    data_json TEXT,
    FOREIGN KEY (app_id) REFERENCES applications(app_id)
);

CREATE INDEX IF NOT EXISTS idx_events_app_id ON events(app_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
"""


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Get a SQLite connection, creating schema if needed."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def upsert_job(conn: sqlite3.Connection, dedup_key: str, job_url: str,
               company: str, role_title: str, role_family: str,
               location: str, match_score: float, raw_json: dict) -> bool:
    """Insert or update a job. Returns True if newly inserted."""
    now = now_iso()
    existing = conn.execute(
        "SELECT dedup_key FROM jobs_seen WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE jobs_seen SET last_seen = ?, raw_json = ? WHERE dedup_key = ?",
            (now, json.dumps(raw_json), dedup_key)
        )
        conn.commit()
        return False
    else:
        conn.execute(
            """INSERT INTO jobs_seen
               (dedup_key, job_url, company, role_title, role_family, location,
                match_score, first_seen, last_seen, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (dedup_key, job_url, company, role_title, role_family, location,
             match_score, now, now, json.dumps(raw_json))
        )
        conn.commit()
        return True


def create_application(conn: sqlite3.Connection, dedup_key: str,
                       resume_version: str = "") -> str:
    """Create an application record. Returns app_id."""
    app_id = new_id()
    now = now_iso()
    conn.execute(
        """INSERT OR IGNORE INTO applications
           (app_id, dedup_key, status, resume_version, created_at, updated_at)
           VALUES (?, ?, 'DISCOVERED', ?, ?, ?)""",
        (app_id, dedup_key, resume_version, now, now)
    )
    conn.commit()
    emit_event(conn, "DISCOVERED", app_id, {"dedup_key": dedup_key})
    return app_id


def update_application_status(conn: sqlite3.Connection, app_id: str,
                              status: str, extra_data: Optional[dict] = None):
    """Update application status and emit an event."""
    now = now_iso()
    updates = {"status": status, "updated_at": now}
    if extra_data and "submission_proof" in extra_data:
        updates["submission_proof"] = extra_data["submission_proof"]

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [app_id]
    conn.execute(f"UPDATE applications SET {set_clause} WHERE app_id = ?", values)
    conn.commit()
    emit_event(conn, status, app_id, extra_data or {})


def emit_event(conn: sqlite3.Connection, event_type: str,
               app_id: str, data: dict):
    """Write an event to the events table."""
    conn.execute(
        "INSERT INTO events (event_id, ts, type, app_id, data_json) VALUES (?, ?, ?, ?, ?)",
        (new_id(), now_iso(), event_type, app_id, json.dumps(data))
    )
    conn.commit()


def get_application_by_id(conn: sqlite3.Connection, app_id: str) -> Optional[dict]:
    """Fetch an application by ID."""
    row = conn.execute(
        "SELECT * FROM applications WHERE app_id = ?", (app_id,)
    ).fetchone()
    return dict(row) if row else None


def get_job_by_dedup(conn: sqlite3.Connection, dedup_key: str) -> Optional[dict]:
    """Fetch a job by dedup key."""
    row = conn.execute(
        "SELECT * FROM jobs_seen WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    return dict(row) if row else None


def get_applications_by_status(conn: sqlite3.Connection, status: str) -> list[dict]:
    """Fetch all applications with a given status."""
    rows = conn.execute(
        "SELECT * FROM applications WHERE status = ?", (status,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_unprocessed_events(conn: sqlite3.Connection,
                           since: Optional[str] = None) -> list[dict]:
    """Fetch events since a timestamp (or all if None)."""
    if since:
        rows = conn.execute(
            "SELECT * FROM events WHERE ts > ? ORDER BY ts", (since,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY ts"
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_applications_with_jobs(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all applications joined with job data."""
    rows = conn.execute(
        """SELECT a.*, j.job_url, j.company, j.role_title, j.role_family,
                  j.location, j.match_score
           FROM applications a
           JOIN jobs_seen j ON a.dedup_key = j.dedup_key
           ORDER BY a.created_at DESC"""
    ).fetchall()
    return [dict(r) for r in rows]
