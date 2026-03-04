"""Google Sheets integration — syncs application data to a shared Google Sheet.

Requires a service account JSON key file. To set up:
1. Go to https://console.cloud.google.com/
2. Create a project (or use existing)
3. Enable the Google Sheets API
4. Create a Service Account under IAM & Admin > Service Accounts
5. Download the JSON key file
6. Share your Google Sheet with the service account email (as Editor)
7. Set GOOGLE_SHEETS_CREDS_FILE=path/to/key.json in .env
8. Set GOOGLE_SHEETS_ID=<spreadsheet_id> in .env
"""

import os
from datetime import datetime, timezone
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from src.utils.logging import setup_logging

logger = setup_logging("jobbot.storage.gsheet")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

COLUMNS = [
    "app_id", "source", "company", "role_title", "role_family", "job_url",
    "location", "match_score", "resume_version", "status", "date_discovered",
    "date_submitted", "submission_proof", "next_action_due", "notes",
]

STATUS_COLORS = {
    "DISCOVERED": {"red": 0.886, "green": 0.937, "blue": 0.855},
    "READY_TO_APPLY": {"red": 0.741, "green": 0.843, "blue": 0.933},
    "APPLYING": {"red": 1.0, "green": 0.949, "blue": 0.8},
    "SUBMITTED": {"red": 0.776, "green": 0.937, "blue": 0.808},
    "NEEDS_HUMAN": {"red": 1.0, "green": 0.78, "blue": 0.808},
    "REJECTED": {"red": 0.851, "green": 0.851, "blue": 0.851},
    "INTERVIEW": {"red": 0.706, "green": 0.776, "blue": 0.906},
}


def _get_client() -> gspread.Client:
    """Authenticate and return a gspread client."""
    creds_file = os.environ.get("GOOGLE_SHEETS_CREDS_FILE", "credentials.json")
    credentials = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    return gspread.authorize(credentials)


def _get_worksheet() -> gspread.Worksheet:
    """Get the applications worksheet, creating headers if needed."""
    client = _get_client()
    sheet_id = os.environ.get("GOOGLE_SHEETS_ID", "")
    if not sheet_id:
        raise ValueError("GOOGLE_SHEETS_ID not set in environment")

    spreadsheet = client.open_by_key(sheet_id)

    # Try to get existing "Applications" sheet, or use first sheet
    try:
        ws = spreadsheet.worksheet("Applications")
    except gspread.WorksheetNotFound:
        ws = spreadsheet.sheet1
        ws.update_title("Applications")

    # Ensure headers exist
    existing = ws.row_values(1)
    if existing != COLUMNS:
        ws.update("A1", [COLUMNS])
        # Format header row
        ws.format("A1:O1", {
            "backgroundColor": {"red": 0.267, "green": 0.447, "blue": 0.769},
            "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1},
                           "bold": True, "fontSize": 11},
            "horizontalAlignment": "CENTER",
        })

    return ws


def _find_row_by_app_id(ws: gspread.Worksheet, app_id: str) -> Optional[int]:
    """Find row number (1-indexed) for an app_id. Returns None if not found."""
    try:
        cell = ws.find(app_id, in_column=1)
        return cell.row if cell else None
    except gspread.CellNotFound:
        return None


def upsert_to_gsheet(app_data: dict) -> str:
    """Insert or update a row in the Google Sheet. Returns 'inserted' or 'updated'."""
    try:
        ws = _get_worksheet()
    except Exception as e:
        logger.error(f"Failed to connect to Google Sheet: {e}")
        raise

    app_id = app_data.get("app_id", "")
    if not app_id:
        logger.warning("No app_id provided, skipping Google Sheet upsert")
        return "skipped"

    row_data = [
        app_data.get("app_id", ""),
        app_data.get("source", "YC"),
        app_data.get("company", ""),
        app_data.get("role_title", ""),
        app_data.get("role_family", ""),
        app_data.get("job_url", ""),
        app_data.get("location", ""),
        str(app_data.get("match_score", 0)),
        app_data.get("resume_version", ""),
        app_data.get("status", "DISCOVERED"),
        app_data.get("date_discovered", datetime.now(timezone.utc).strftime("%Y-%m-%d")),
        app_data.get("date_submitted", ""),
        app_data.get("submission_proof", ""),
        app_data.get("next_action_due", ""),
        app_data.get("notes", ""),
    ]

    existing_row = _find_row_by_app_id(ws, app_id)

    if existing_row:
        # Update the existing row
        cell_range = f"A{existing_row}:O{existing_row}"
        ws.update(cell_range, [row_data])
        _apply_status_color(ws, existing_row, app_data.get("status", ""))
        logger.info(f"Updated row {existing_row} for {app_id}")
        return "updated"
    else:
        # Append new row
        ws.append_row(row_data, value_input_option="USER_ENTERED")
        new_row = len(ws.get_all_values())
        _apply_status_color(ws, new_row, app_data.get("status", ""))
        logger.info(f"Inserted row {new_row} for {app_id}")
        return "inserted"


def _apply_status_color(ws: gspread.Worksheet, row: int, status: str):
    """Apply background color to the status cell."""
    color = STATUS_COLORS.get(status)
    if color:
        status_col_letter = "J"  # Column J = status
        ws.format(f"{status_col_letter}{row}", {
            "backgroundColor": color,
        })


def sync_all_to_gsheet(applications: list[dict]) -> dict:
    """Sync a list of application dicts to the Google Sheet.

    Returns summary dict with counts.
    """
    summary = {"inserted": 0, "updated": 0, "errors": 0}

    for app_data in applications:
        try:
            result = upsert_to_gsheet(app_data)
            if result == "inserted":
                summary["inserted"] += 1
            elif result == "updated":
                summary["updated"] += 1
        except Exception as e:
            summary["errors"] += 1
            logger.error(f"Error syncing {app_data.get('app_id', '?')}: {e}")

    logger.info(f"Google Sheets sync: {summary}")
    return summary
