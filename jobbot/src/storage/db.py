"""SQLite storage layer for jobs, applications, and events."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


DB_PATH = Path(__file__).resolve().parents[2] / "data" / "jobs.db"

# Valid pipeline stages (in order)
PIPELINE_STAGES = [
    "DISCOVERED",
    "MAPPABLE",
    "NEEDS_USER_DATA",
    "FILLING",
    "FILLED_AWAITING_SUBMIT",
    "APPLYING",
    "APPLIED",
    "OA_RECEIVED",
    "OA_COMPLETED",
    "INTERVIEW_SCHEDULED",
    "INTERVIEW_DONE",
    "OFFER",
    "REJECTED",
    "WITHDRAWN",
    "NEEDS_HUMAN",
    "ERROR",
]

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
    stage TEXT NOT NULL DEFAULT 'DISCOVERED',
    resume_version TEXT,
    submission_proof TEXT,
    applied_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    notes TEXT DEFAULT '',
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

CREATE TABLE IF NOT EXISTS email_threads (
    thread_id TEXT PRIMARY KEY,
    app_id TEXT,
    gmail_thread_id TEXT UNIQUE,
    subject TEXT,
    from_addr TEXT,
    last_message_at TEXT,
    classification TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (app_id) REFERENCES applications(app_id)
);

CREATE TABLE IF NOT EXISTS form_schemas (
    schema_id TEXT PRIMARY KEY,
    schema_hash TEXT NOT NULL,
    job_url TEXT,
    company TEXT,
    fields_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS field_mappings (
    mapping_id TEXT PRIMARY KEY,
    field_key TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    scope_value TEXT NOT NULL DEFAULT '',
    answer_value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_app_id ON events(app_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_form_schemas_hash ON form_schemas(schema_hash);
CREATE INDEX IF NOT EXISTS idx_field_mappings_key ON field_mappings(field_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_field_mappings_unique ON field_mappings(field_key, scope, scope_value);
"""

# Indexes that depend on columns added by migrations – run AFTER migrations
POST_MIGRATION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_applications_stage ON applications(stage)",
    "CREATE INDEX IF NOT EXISTS idx_email_threads_app_id ON email_threads(app_id)",
]

MIGRATIONS = [
    "ALTER TABLE applications ADD COLUMN stage TEXT NOT NULL DEFAULT 'DISCOVERED'",
    "ALTER TABLE applications ADD COLUMN applied_at TEXT",
    "ALTER TABLE applications ADD COLUMN notes TEXT DEFAULT ''",
    """CREATE TABLE IF NOT EXISTS email_threads (
        thread_id TEXT PRIMARY KEY, app_id TEXT, gmail_thread_id TEXT UNIQUE,
        subject TEXT, from_addr TEXT, last_message_at TEXT, classification TEXT,
        raw_json TEXT, created_at TEXT NOT NULL,
        FOREIGN KEY (app_id) REFERENCES applications(app_id))""",
    "CREATE INDEX IF NOT EXISTS idx_applications_stage ON applications(stage)",
    "CREATE INDEX IF NOT EXISTS idx_email_threads_app_id ON email_threads(app_id)",
    # Phase 0 upgrades
    "ALTER TABLE applications ADD COLUMN policy TEXT DEFAULT 'pause_at_submit'",
    "ALTER TABLE applications ADD COLUMN proof_json TEXT",
    "ALTER TABLE applications ADD COLUMN missing_fields TEXT",
    "ALTER TABLE form_schemas ADD COLUMN job_id INTEGER",
    "ALTER TABLE field_mappings ADD COLUMN scope_type TEXT",
    # Copy existing scope → scope_type for backward compat
    "UPDATE field_mappings SET scope_type = scope WHERE scope_type IS NULL",
]


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Get a SQLite connection, creating schema if needed."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    # Run migrations for existing DBs (add columns/tables that didn't exist)
    for migration in MIGRATIONS:
        try:
            conn.execute(migration)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column/table already exists
    # Create indexes that depend on migrated columns
    for idx_sql in POST_MIGRATION_INDEXES:
        try:
            conn.execute(idx_sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass
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
    extra = extra_data or {}
    if "submission_proof" in extra:
        updates["submission_proof"] = extra["submission_proof"]
    if "proof_json" in extra:
        updates["proof_json"] = extra["proof_json"] if isinstance(extra["proof_json"], str) else json.dumps(extra["proof_json"])
    if "missing_fields" in extra:
        mf = extra["missing_fields"]
        updates["missing_fields"] = json.dumps(mf) if isinstance(mf, list) else mf
    if "policy" in extra:
        updates["policy"] = extra["policy"]

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [app_id]
    conn.execute(f"UPDATE applications SET {set_clause} WHERE app_id = ?", values)
    conn.commit()
    emit_event(conn, status, app_id, extra)


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
                  j.location, j.match_score, j.raw_json
           FROM applications a
           JOIN jobs_seen j ON a.dedup_key = j.dedup_key
           ORDER BY a.created_at DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def update_application_stage(conn: sqlite3.Connection, app_id: str,
                             stage: str, notes: str = ""):
    """Update the pipeline stage of an application."""
    now = now_iso()
    extra = {"notes": notes} if notes else {}
    conn.execute(
        "UPDATE applications SET stage = ?, status = ?, updated_at = ? WHERE app_id = ?",
        (stage, stage, now, app_id)
    )
    conn.commit()
    emit_event(conn, f"STAGE_{stage}", app_id, {"stage": stage, **extra})


def get_pipeline_stats(conn: sqlite3.Connection) -> dict:
    """Get counts of applications per pipeline stage."""
    rows = conn.execute(
        "SELECT stage, COUNT(*) as cnt FROM applications GROUP BY stage"
    ).fetchall()
    return {r["stage"]: r["cnt"] for r in rows}


def upsert_email_thread(conn: sqlite3.Connection, gmail_thread_id: str,
                        app_id: Optional[str], subject: str, from_addr: str,
                        classification: str, raw_json: dict) -> str:
    """Insert or update an email thread. Returns thread_id."""
    now = now_iso()
    existing = conn.execute(
        "SELECT thread_id FROM email_threads WHERE gmail_thread_id = ?",
        (gmail_thread_id,)
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE email_threads SET last_message_at = ?, classification = ?,
               raw_json = ?, app_id = ? WHERE gmail_thread_id = ?""",
            (now, classification, json.dumps(raw_json), app_id, gmail_thread_id)
        )
        conn.commit()
        return existing["thread_id"]
    else:
        thread_id = new_id()
        conn.execute(
            """INSERT INTO email_threads
               (thread_id, app_id, gmail_thread_id, subject, from_addr,
                last_message_at, classification, raw_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (thread_id, app_id, gmail_thread_id, subject, from_addr,
             now, classification, json.dumps(raw_json), now)
        )
        conn.commit()
        return thread_id


def get_email_threads_for_app(conn: sqlite3.Connection, app_id: str) -> list[dict]:
    """Fetch all email threads linked to an application."""
    rows = conn.execute(
        "SELECT * FROM email_threads WHERE app_id = ? ORDER BY last_message_at DESC",
        (app_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- Form schemas ----------

def save_form_schema(conn: sqlite3.Connection, schema_hash: str,
                     job_url: str, company: str, fields: list[dict]) -> str:
    """Save a form schema. Returns schema_id."""
    # Check if this exact hash already exists
    existing = conn.execute(
        "SELECT schema_id FROM form_schemas WHERE schema_hash = ?", (schema_hash,)
    ).fetchone()
    if existing:
        return existing["schema_id"]

    schema_id = new_id()
    now = now_iso()
    conn.execute(
        """INSERT INTO form_schemas (schema_id, schema_hash, job_url, company, fields_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (schema_id, schema_hash, job_url, company, json.dumps(fields), now)
    )
    conn.commit()
    return schema_id


def get_form_schema(conn: sqlite3.Connection, schema_hash: str) -> Optional[dict]:
    """Fetch a form schema by hash."""
    row = conn.execute(
        "SELECT * FROM form_schemas WHERE schema_hash = ?", (schema_hash,)
    ).fetchone()
    if row:
        d = dict(row)
        d["fields"] = json.loads(d["fields_json"])
        return d
    return None


def get_all_schemas(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all form schemas."""
    rows = conn.execute("SELECT * FROM form_schemas ORDER BY created_at DESC").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["fields"] = json.loads(d["fields_json"])
        result.append(d)
    return result


# ---------- Field mappings ----------

def save_field_mapping(conn: sqlite3.Connection, field_key: str,
                       answer_value: str, scope: str = "global",
                       scope_value: str = "") -> str:
    """Upsert a field mapping. Returns mapping_id."""
    now = now_iso()
    existing = conn.execute(
        "SELECT mapping_id FROM field_mappings WHERE field_key = ? AND scope = ? AND scope_value = ?",
        (field_key, scope, scope_value)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE field_mappings SET answer_value = ?, updated_at = ? WHERE mapping_id = ?",
            (answer_value, now, existing["mapping_id"])
        )
        conn.commit()
        return existing["mapping_id"]

    mapping_id = new_id()
    conn.execute(
        """INSERT INTO field_mappings (mapping_id, field_key, scope, scope_type, scope_value, answer_value, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (mapping_id, field_key, scope, scope, scope_value, answer_value, now, now)
    )
    conn.commit()
    return mapping_id


def get_field_mappings(conn: sqlite3.Connection, field_key: Optional[str] = None,
                       scope: Optional[str] = None) -> list[dict]:
    """Fetch field mappings, optionally filtered."""
    query = "SELECT * FROM field_mappings WHERE 1=1"
    params: list = []
    if field_key:
        query += " AND field_key = ?"
        params.append(field_key)
    if scope:
        query += " AND scope = ?"
        params.append(scope)
    query += " ORDER BY field_key, scope"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def resolve_field_value(conn: sqlite3.Connection, field_key: str,
                        company: str = "", schema_hash: str = "") -> Optional[str]:
    """Resolve field value using hierarchy: schema_hash → company → global."""
    # 1. Schema-specific
    if schema_hash:
        row = conn.execute(
            "SELECT answer_value FROM field_mappings WHERE field_key = ? AND scope = 'schema' AND scope_value = ?",
            (field_key, schema_hash)
        ).fetchone()
        if row:
            return row["answer_value"]
    # 2. Company-specific
    if company:
        row = conn.execute(
            "SELECT answer_value FROM field_mappings WHERE field_key = ? AND scope = 'company' AND scope_value = ?",
            (field_key, company)
        ).fetchone()
        if row:
            return row["answer_value"]
    # 3. Global
    row = conn.execute(
        "SELECT answer_value FROM field_mappings WHERE field_key = ? AND scope = 'global'",
        (field_key,)
    ).fetchone()
    if row:
        return row["answer_value"]
    return None


def delete_field_mapping(conn: sqlite3.Connection, mapping_id: str) -> bool:
    """Delete a field mapping. Returns True if deleted."""
    cursor = conn.execute("DELETE FROM field_mappings WHERE mapping_id = ?", (mapping_id,))
    conn.commit()
    return cursor.rowcount > 0
