"""FastAPI backend for JobBot dashboard."""

import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.storage.db import (
    delete_field_mapping,
    get_all_applications_with_jobs,
    get_all_schemas,
    get_application_by_id,
    get_applications_by_status,
    get_connection,
    get_email_threads_for_app,
    get_field_mappings,
    get_form_schema,
    get_job_by_dedup,
    get_pipeline_stats,
    get_unprocessed_events,
    save_field_mapping,
    save_form_schema,
    update_application_stage,
    update_application_status,
    PIPELINE_STAGES,
)
from src.utils.logging import setup_logging

logger = setup_logging("jobbot.server")

app = FastAPI(title="JobBot API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- State ----------
engine_thread: Optional[threading.Thread] = None
engine_running = threading.Event()
ws_clients: list[WebSocket] = []


# ---------- WebSocket broadcast ----------
async def broadcast(msg: dict):
    """Send a message to all connected WebSocket clients."""
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)


def sync_broadcast(msg: dict):
    """Call broadcast from a sync context (engine thread)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(broadcast(msg))
        else:
            loop.run_until_complete(broadcast(msg))
    except RuntimeError:
        pass  # No event loop available


# ---------- Models ----------
class StageUpdate(BaseModel):
    stage: str
    notes: str = ""


class EngineConfig(BaseModel):
    max_jobs: int = 40
    dry_run: bool = False


class MappingCreate(BaseModel):
    field_key: str
    answer_value: str
    scope: str = "global"
    scope_value: str = ""


# ---------- REST Endpoints ----------
@app.get("/api/applications")
def list_applications():
    conn = get_connection()
    apps = get_all_applications_with_jobs(conn)
    conn.close()
    return {"applications": apps, "count": len(apps)}


@app.get("/api/applications/{app_id}")
def get_application(app_id: str):
    conn = get_connection()
    app_data = get_application_by_id(conn, app_id)
    if not app_data:
        conn.close()
        return {"error": "Not found"}, 404
    emails = get_email_threads_for_app(conn, app_id)
    events = conn.execute(
        "SELECT * FROM events WHERE app_id = ? ORDER BY ts DESC", (app_id,)
    ).fetchall()
    conn.close()
    return {
        "application": dict(app_data),
        "emails": emails,
        "events": [dict(e) for e in events],
    }


@app.patch("/api/applications/{app_id}/stage")
def update_stage(app_id: str, body: StageUpdate):
    if body.stage not in PIPELINE_STAGES:
        return {"error": f"Invalid stage. Valid: {PIPELINE_STAGES}"}, 400
    conn = get_connection()
    update_application_stage(conn, app_id, body.stage, body.notes)
    conn.close()
    return {"success": True, "stage": body.stage}


@app.get("/api/stats")
def get_stats():
    conn = get_connection()
    stats = get_pipeline_stats(conn)
    total = sum(stats.values())
    conn.close()
    return {"stages": stats, "total": total}


@app.get("/api/stages")
def get_stages():
    return {"stages": PIPELINE_STAGES}


@app.get("/api/engine/status")
def engine_status():
    return {"running": engine_running.is_set()}


@app.post("/api/engine/start")
def engine_start(config: EngineConfig = EngineConfig()):
    global engine_thread
    if engine_running.is_set():
        return {"error": "Engine already running"}

    engine_running.set()

    def _run_engine():
        try:
            from src.agents.scout_applier import run_greenhouse_direct, set_engine_stop_event
            set_engine_stop_event(engine_running)
            logger.info(f"Engine started (max_jobs={config.max_jobs}, dry_run={config.dry_run})")
            sync_broadcast({"type": "engine", "status": "started"})
            summary = run_greenhouse_direct(
                max_jobs=config.max_jobs,
                dry_run=config.dry_run,
                event_callback=sync_broadcast,
            )
            sync_broadcast({"type": "engine", "status": "completed", "summary": summary})
        except Exception as e:
            logger.error(f"Engine error: {e}", exc_info=True)
            sync_broadcast({"type": "engine", "status": "error", "error": str(e)})
        finally:
            engine_running.clear()
            logger.info("Engine stopped")

    engine_thread = threading.Thread(target=_run_engine, daemon=True)
    engine_thread.start()
    return {"success": True, "message": "Engine started"}


@app.post("/api/engine/stop")
def engine_stop():
    if not engine_running.is_set():
        return {"error": "Engine not running"}
    engine_running.clear()
    return {"success": True, "message": "Engine stopping..."}


# ---------- Schema endpoints ----------
@app.get("/api/schemas")
def list_schemas():
    conn = get_connection()
    schemas = get_all_schemas(conn)
    conn.close()
    return {"schemas": schemas, "count": len(schemas)}


@app.get("/api/schemas/{schema_hash}")
def get_schema(schema_hash: str):
    conn = get_connection()
    schema = get_form_schema(conn, schema_hash)
    conn.close()
    if not schema:
        return {"error": "Schema not found"}
    return {"schema": schema}


# ---------- Mapping endpoints ----------
@app.get("/api/mappings")
def list_mappings(field_key: Optional[str] = None, scope: Optional[str] = None):
    conn = get_connection()
    mappings = get_field_mappings(conn, field_key=field_key, scope=scope)
    conn.close()
    return {"mappings": mappings, "count": len(mappings)}


@app.post("/api/mappings")
def create_mapping(body: MappingCreate):
    conn = get_connection()
    mapping_id = save_field_mapping(
        conn, body.field_key, body.answer_value,
        scope=body.scope, scope_value=body.scope_value
    )
    conn.close()
    return {"success": True, "mapping_id": mapping_id}


@app.delete("/api/mappings/{mapping_id}")
def remove_mapping(mapping_id: str):
    conn = get_connection()
    deleted = delete_field_mapping(conn, mapping_id)
    conn.close()
    if not deleted:
        return {"error": "Mapping not found"}
    return {"success": True}


# ---------- Fill / Apply endpoints ----------

class FillRequest(BaseModel):
    policy: str = "pause_at_submit"


@app.post("/api/jobs/{job_id}/fill")
def fill_job(job_id: str, body: FillRequest = FillRequest()):
    """Fill the application form for a job (dedup_key or app_id).

    Runs apply_to_greenhouse with the given policy (default: pause_at_submit).
    """
    import json as _json

    from src.agents.scout_applier import load_profile
    from src.greenhouse.apply_playwright import apply_to_greenhouse
    from src.utils.role_classify import get_resume_path

    conn = get_connection()

    # Try app_id first, then dedup_key lookup via job_id
    app_data = get_application_by_id(conn, job_id)
    if not app_data:
        # Treat job_id as dedup_key
        apps = conn.execute(
            "SELECT * FROM applications WHERE dedup_key = ?", (job_id,)
        ).fetchone()
        app_data = dict(apps) if apps else None

    if not app_data:
        conn.close()
        return {"error": "Application not found"}, 404

    job = get_job_by_dedup(conn, app_data["dedup_key"])
    if not job:
        conn.close()
        return {"error": "Job record not found"}, 404

    raw = _json.loads(job["raw_json"]) if job.get("raw_json") else {}
    job_url = raw.get("apply_url", job["job_url"])
    role_family = job.get("role_family", "fullstack")

    try:
        profile = load_profile()
    except FileNotFoundError:
        conn.close()
        return {"error": "profile.yaml not found"}, 500

    from src.utils.role_classify import get_resume_path
    resume_path = get_resume_path(role_family)

    app_id = app_data["app_id"]
    update_application_stage(conn, app_id, "FILLING")

    result = apply_to_greenhouse(
        job_url=job_url, profile=profile, resume_path=resume_path,
        company=job.get("company", ""), role_title=job.get("role_title", ""),
        policy=body.policy,
    )

    if result["status"] == "FILLED_AWAITING_SUBMIT":
        update_application_status(conn, app_id, "FILLED_AWAITING_SUBMIT", {
            "proof_json": {"proof_path": result["proof_path"]},
            "policy": body.policy,
        })
        update_application_stage(conn, app_id, "FILLED_AWAITING_SUBMIT")
    elif result["status"] == "SUBMITTED":
        update_application_status(conn, app_id, "APPLIED", {
            "submission_proof": result["proof_path"],
        })
        update_application_stage(conn, app_id, "APPLIED")
    else:
        update_application_status(conn, app_id, "NEEDS_HUMAN", {
            "blocked_reason": result.get("blocked_reason", result.get("error", "")),
        })

    conn.close()
    sync_broadcast({"type": "fill_result", "app_id": app_id, "status": result["status"],
                    "proof_path": result.get("proof_path", "")})
    return {"success": True, "status": result["status"],
            "proof_path": result.get("proof_path", ""),
            "error": result.get("error", "")}


@app.post("/api/jobs/{job_id}/apply")
def apply_job(job_id: str):
    """Submit an already-filled application (policy: auto_if_safe)."""
    return fill_job(job_id, FillRequest(policy="auto_if_safe"))


@app.get("/api/jobs/{job_id}/missing-fields")
def get_missing_fields(job_id: str):
    """Return missing required field_keys for an application."""
    import json as _json

    conn = get_connection()
    app_data = get_application_by_id(conn, job_id)
    if not app_data:
        apps = conn.execute(
            "SELECT * FROM applications WHERE dedup_key = ?", (job_id,)
        ).fetchone()
        app_data = dict(apps) if apps else None

    if not app_data:
        conn.close()
        return {"error": "Application not found"}, 404

    missing_raw = app_data.get("missing_fields", "")
    conn.close()
    if not missing_raw:
        return {"missing_fields": [], "count": 0}
    try:
        missing = _json.loads(missing_raw) if isinstance(missing_raw, str) else missing_raw
    except (ValueError, TypeError):
        missing = []
    return {"missing_fields": missing, "count": len(missing)}


# ---------- WebSocket ----------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.append(ws)
    logger.info(f"WebSocket client connected ({len(ws_clients)} total)")
    try:
        while True:
            # Keep alive — client can send pings
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        ws_clients.remove(ws)
        logger.info(f"WebSocket client disconnected ({len(ws_clients)} total)")


# ---------- Run ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

