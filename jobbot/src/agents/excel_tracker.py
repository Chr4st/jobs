"""ExcelTracker agent: syncs SQLite events to Excel, Google Sheets, and generates summaries."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.storage.db import (
    get_all_applications_with_jobs,
    get_connection,
    get_job_by_dedup,
    get_unprocessed_events,
)
from src.storage.excel import get_daily_summary, upsert_application
from src.utils.logging import setup_logging

logger = setup_logging("jobbot.agent.excel")

# Track last processed event timestamp
_LAST_PROCESSED_FILE = Path(__file__).resolve().parents[2] / "data" / ".last_event_ts"


def _get_last_processed_ts() -> Optional[str]:
    """Get the timestamp of the last processed event."""
    if _LAST_PROCESSED_FILE.exists():
        return _LAST_PROCESSED_FILE.read_text().strip()
    return None


def _set_last_processed_ts(ts: str):
    """Save the timestamp of the last processed event."""
    _LAST_PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LAST_PROCESSED_FILE.write_text(ts)


def run_excel_tracker(
    db_path: Optional[Path] = None,
    excel_path: Optional[Path] = None,
) -> dict:
    """Process new events and update the Excel tracker.

    Returns summary of actions taken.
    """
    conn = get_connection(db_path)
    last_ts = _get_last_processed_ts()

    logger.info(f"Processing events since: {last_ts or 'beginning'}")

    events = get_unprocessed_events(conn, last_ts)
    logger.info(f"Found {len(events)} new events to process")

    summary = {
        "events_processed": 0,
        "rows_inserted": 0,
        "rows_updated": 0,
    }

    # Also do a full sync of all applications (handles any missed events)
    all_apps = get_all_applications_with_jobs(conn)

    for app in all_apps:
        job = get_job_by_dedup(conn, app["dedup_key"])
        raw_json = {}
        if job and job.get("raw_json"):
            try:
                raw_json = json.loads(job["raw_json"])
            except (json.JSONDecodeError, TypeError):
                pass

        app_data = {
            "app_id": app["app_id"],
            "source": "YC",
            "company": app.get("company", ""),
            "role_title": app.get("role_title", ""),
            "role_family": app.get("role_family", ""),
            "job_url": app.get("job_url", ""),
            "location": app.get("location", ""),
            "match_score": app.get("match_score", 0),
            "resume_version": app.get("resume_version", ""),
            "status": app.get("status", "DISCOVERED"),
            "date_discovered": app.get("created_at", "")[:10] if app.get("created_at") else "",
            "submission_proof": app.get("submission_proof", ""),
            "notes": "",
        }

        # Extract date_submitted from events
        if app["status"] == "SUBMITTED":
            app_data["date_submitted"] = app.get("updated_at", "")[:10]

        result = upsert_application(app_data, excel_path)
        if result == "inserted":
            summary["rows_inserted"] += 1
        else:
            summary["rows_updated"] += 1

    # Sync to Google Sheets if configured
    gsheet_enabled = os.environ.get("GOOGLE_SHEETS_ID", "")
    if gsheet_enabled:
        try:
            from src.storage.gsheet import sync_all_to_gsheet
            all_app_data = []
            for app in all_apps:
                job = get_job_by_dedup(conn, app["dedup_key"])
                app_data = {
                    "app_id": app["app_id"],
                    "source": "YC",
                    "company": app.get("company", ""),
                    "role_title": app.get("role_title", ""),
                    "role_family": app.get("role_family", ""),
                    "job_url": app.get("job_url", ""),
                    "location": app.get("location", ""),
                    "match_score": app.get("match_score", 0),
                    "resume_version": app.get("resume_version", ""),
                    "status": app.get("status", "DISCOVERED"),
                    "date_discovered": app.get("created_at", "")[:10] if app.get("created_at") else "",
                    "date_submitted": app.get("updated_at", "")[:10] if app["status"] == "SUBMITTED" else "",
                    "submission_proof": app.get("submission_proof", ""),
                    "notes": "",
                }
                all_app_data.append(app_data)
            gs_summary = sync_all_to_gsheet(all_app_data)
            summary["gsheet_inserted"] = gs_summary.get("inserted", 0)
            summary["gsheet_updated"] = gs_summary.get("updated", 0)
            logger.info(f"Google Sheets sync complete: {gs_summary}")
        except Exception as e:
            logger.warning(f"Google Sheets sync failed (non-fatal): {e}")

    # Process events for logging
    latest_ts = last_ts
    for event in events:
        summary["events_processed"] += 1
        event_ts = event.get("ts", "")
        if not latest_ts or event_ts > latest_ts:
            latest_ts = event_ts

        event_type = event.get("type", "")
        app_id = event.get("app_id", "")
        logger.info(f"Event: {event_type} for app {app_id}")

    if latest_ts:
        _set_last_processed_ts(latest_ts)

    conn.close()

    logger.info(f"Excel tracker summary: {summary}")
    return summary


def print_daily_summary(excel_path: Optional[Path] = None):
    """Print a formatted daily summary."""
    summary = get_daily_summary(excel_path)

    print("\n" + "=" * 60)
    print(f"  JOBBOT DAILY SUMMARY — {datetime.now().strftime('%Y-%m-%d')}")
    print("=" * 60)
    print(f"\n  Total applications tracked: {summary['total']}")

    if summary["by_status"]:
        print("\n  Status breakdown:")
        for status, count in sorted(summary["by_status"].items()):
            print(f"    {status:20s} {count}")

    if summary["needs_human"]:
        print(f"\n  Needs human attention ({len(summary['needs_human'])}):")
        for item in summary["needs_human"]:
            print(f"    [{item['app_id']}] {item['company']} — {item['role']}")

    if summary["upcoming_follow_ups"]:
        print(f"\n  Follow-ups due ({len(summary['upcoming_follow_ups'])}):")
        for item in summary["upcoming_follow_ups"]:
            print(f"    [{item['app_id']}] {item['company']} — {item['role']} (due: {item['due']})")

    print("\n" + "=" * 60 + "\n")
