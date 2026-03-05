"""ScoutApplier agent: discovers jobs from Greenhouse boards, classifies, scores, and applies."""

import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml

from src.greenhouse.apply_playwright import apply_to_greenhouse
from src.greenhouse.parse import parse_greenhouse_job
from src.greenhouse.schema_extract import extract_form_schema, extract_schema_from_api
from src.mapping.auto_map import get_missing_required, resolve_all_fields
from src.storage.db import (
    create_application,
    get_connection,
    get_job_by_dedup,
    save_form_schema,
    save_field_mapping,
    update_application_status,
    update_application_stage,
    upsert_job,
)
from src.utils.dedupe import make_dedup_key
from src.utils.logging import setup_logging
from src.utils.role_classify import classify_role, get_resume_path
from src.utils.scoring import compute_match_score
from src.yc.discover import discover_greenhouse_direct, discover_greenhouse_jobs

logger = setup_logging("jobbot.agent.scout")

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_profile() -> dict:
    """Load user profile from jarvis_profile.yaml (preferred) or profile.yaml (legacy).

    Search order:
        1. jarvis_profile.yaml
        2. profile.yaml
    """
    for name in ("jarvis_profile.yaml", "profile.yaml"):
        p = PROJECT_ROOT / name
        if p.exists():
            with open(p) as f:
                data = yaml.safe_load(f)
            logger.debug(f"Loaded profile from {name}")
            return data
    logger.error(
        "No profile found. Create jarvis_profile.yaml (or profile.yaml) "
        "with your personal details."
    )
    raise FileNotFoundError("jarvis_profile.yaml not found")


def run_scout_applier(
    dry_run: bool = False,
    threshold: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Run the full scout+apply pipeline.

    Returns summary dict with counts.
    """
    if threshold is None:
        threshold = float(os.environ.get("MATCH_SCORE_THRESHOLD", "0.15"))

    profile = load_profile()
    preferred_locations = [profile.get("location", "")]

    conn = get_connection(db_path)

    summary = {
        "discovered": 0,
        "new_jobs": 0,
        "classified": 0,
        "above_threshold": 0,
        "applied": 0,
        "needs_human": 0,
        "errors": 0,
    }

    # Step 1: Discover Greenhouse URLs from YC
    logger.info("=== Step 1: Discovering jobs from YC ===")
    raw_jobs = discover_greenhouse_jobs()
    summary["discovered"] = len(raw_jobs)
    logger.info(f"Found {len(raw_jobs)} Greenhouse job URLs")

    for raw_job in raw_jobs:
        job_url = raw_job["job_url"]

        try:
            # Step 2: Parse Greenhouse job page
            logger.info(f"--- Processing: {job_url} ---")
            parsed = parse_greenhouse_job(job_url)
            if not parsed:
                logger.warning(f"Could not parse job page: {job_url}")
                continue

            title = parsed["title"]
            company = parsed.get("company", "") or raw_job.get("company", "")
            location = parsed.get("location", "")
            description = parsed.get("description", "")

            # Step 3: Classify role
            role_family = classify_role(title, description)
            if not role_family:
                logger.info(f"Skipping (no matching role family): {title} at {company}")
                continue
            summary["classified"] += 1
            logger.info(f"Classified as: {role_family}")

            # Step 4: Compute dedup key and check if seen
            dedup_key = make_dedup_key(company, title, job_url)

            # Step 5: Score the match
            score = compute_match_score(
                title=title,
                description=description,
                role_family=role_family,
                location=location,
                preferred_locations=preferred_locations,
            )
            logger.info(f"Match score: {score:.3f} (threshold: {threshold})")

            # Step 6: Upsert to DB
            raw_json = {
                "title": title,
                "company": company,
                "location": location,
                "description": description[:2000],
                "department": parsed.get("department", ""),
                "apply_url": parsed.get("apply_url", ""),
                "source_url": raw_job.get("source_url", ""),
            }

            is_new = upsert_job(
                conn, dedup_key, job_url, company, title,
                role_family, location, score, raw_json,
            )

            if is_new:
                summary["new_jobs"] += 1
                app_id = create_application(conn, dedup_key, get_resume_path(role_family))
            else:
                # Already seen, skip applying
                logger.info(f"Already seen: {title} at {company}")
                continue

            # Step 7: Check threshold
            if score < threshold:
                logger.info(f"Below threshold ({score:.3f} < {threshold}), skipping apply")
                update_application_status(conn, app_id, "DISCOVERED", {
                    "reason": f"Below threshold: {score:.3f}"
                })
                continue

            summary["above_threshold"] += 1

            # Step 8: Apply
            update_application_status(conn, app_id, "READY_TO_APPLY")
            update_application_status(conn, app_id, "APPLYING")

            resume_path = get_resume_path(role_family)
            apply_url = parsed.get("apply_url", job_url)

            logger.info(f"Applying to {title} at {company} (score: {score:.3f})")
            result = apply_to_greenhouse(
                job_url=apply_url,
                profile=profile,
                resume_path=resume_path,
                company=company,
                role_title=title,
                dry_run=dry_run,
            )

            if result["status"] == "SUBMITTED":
                summary["applied"] += 1
                update_application_status(conn, app_id, "SUBMITTED", {
                    "submission_proof": result["proof_path"],
                    "confirmation_text": result["confirmation_text"],
                    "date_submitted": datetime.now(timezone.utc).isoformat(),
                })
                logger.info(f"Successfully applied to {title} at {company}")

            elif result["status"] == "NEEDS_HUMAN":
                summary["needs_human"] += 1
                update_application_status(conn, app_id, "NEEDS_HUMAN", {
                    "blocked_reason": result["blocked_reason"],
                    "proof_path": result["proof_path"],
                    "job_url": job_url,
                })
                logger.warning(
                    f"Needs human intervention for {title} at {company}: "
                    f"{result['blocked_reason']}"
                )

            else:
                summary["errors"] += 1
                update_application_status(conn, app_id, "NEEDS_HUMAN", {
                    "error": result["error"],
                    "proof_path": result.get("proof_path", ""),
                })
                logger.error(f"Error applying to {title} at {company}: {result['error']}")

        except Exception as e:
            summary["errors"] += 1
            logger.error(f"Error processing {job_url}: {e}", exc_info=True)
            continue

    conn.close()

    logger.info(f"=== Scout Summary: {json.dumps(summary)} ===")
    return summary


def resume_application(app_id: str, dry_run: bool = False) -> dict:
    """Resume a NEEDS_HUMAN application after user completes the blocking step."""
    profile = load_profile()
    conn = get_connection()

    from src.storage.db import get_application_by_id, get_job_by_dedup

    app = get_application_by_id(conn, app_id)
    if not app:
        logger.error(f"Application {app_id} not found")
        return {"error": f"Application {app_id} not found"}

    if app["status"] != "NEEDS_HUMAN":
        logger.warning(f"Application {app_id} is not NEEDS_HUMAN (status: {app['status']})")
        return {"error": f"Application status is {app['status']}, expected NEEDS_HUMAN"}

    job = get_job_by_dedup(conn, app["dedup_key"])
    if not job:
        logger.error(f"Job not found for dedup key: {app['dedup_key']}")
        return {"error": "Job record not found"}

    job_data = json.loads(job["raw_json"]) if job["raw_json"] else {}
    apply_url = job_data.get("apply_url", job["job_url"])
    resume_path = app.get("resume_version", "") or get_resume_path(job["role_family"])

    update_application_status(conn, app_id, "APPLYING")

    result = apply_to_greenhouse(
        job_url=apply_url,
        profile=profile,
        resume_path=resume_path,
        company=job.get("company", ""),
        role_title=job.get("role_title", ""),
        dry_run=dry_run,
    )

    if result["status"] == "SUBMITTED":
        update_application_status(conn, app_id, "SUBMITTED", {
            "submission_proof": result["proof_path"],
            "confirmation_text": result["confirmation_text"],
            "date_submitted": datetime.now(timezone.utc).isoformat(),
        })
    elif result["status"] == "NEEDS_HUMAN":
        update_application_status(conn, app_id, "NEEDS_HUMAN", {
            "blocked_reason": result["blocked_reason"],
            "proof_path": result["proof_path"],
        })
    else:
        update_application_status(conn, app_id, "NEEDS_HUMAN", {
            "error": result["error"],
        })

    conn.close()
    return result


# Global stop signal for engine control from the server
_engine_stop_event = None


def set_engine_stop_event(event):
    """Set the threading.Event used to signal engine stop."""
    global _engine_stop_event
    _engine_stop_event = event


def run_greenhouse_direct(
    max_jobs: int = 40,
    dry_run: bool = False,
    db_path: Optional[Path] = None,
    event_callback=None,
    policy: str = "pause_at_submit",
) -> dict:
    """Scan Greenhouse boards, extract schemas, map fields, and apply.

    Integrates schema extraction + auto_map for intelligent form filling.
    Adds rate limiting (5-10 min jitter) and closes browser after each app.
    """
    profile = load_profile()
    conn = get_connection(db_path)

    def _emit(msg: dict):
        if event_callback:
            try:
                event_callback(msg)
            except Exception:
                pass

    # Read automation config from profile
    auto_cfg = profile.get("automation", {})
    no_approval = auto_cfg.get("no_approval", False)
    daily_cap = auto_cfg.get("max_applications_per_day", 0)
    min_wait = auto_cfg.get("min_minutes_between_applications", 5)
    max_wait = auto_cfg.get("max_minutes_between_applications", 10)

    summary = {
        "discovered": 0, "new_jobs": 0, "classified": 0,
        "applied": 0, "needs_human": 0, "needs_user_data": 0,
        "skipped_unanswerable": 0,
        "errors": 0, "details": [],
    }

    # Phase 1: Scan
    logger.info("=== Phase 1: Scanning Greenhouse boards ===")
    _emit({"type": "engine", "status": "scanning"})

    from src.greenhouse.scanner import scan_greenhouse_boards
    scan_result = scan_greenhouse_boards(max_jobs=max_jobs, db_path=db_path, event_callback=_emit)
    summary["discovered"] = scan_result["discovered"]
    summary["new_jobs"] = scan_result["new_jobs"]
    logger.info(f"Scan: {scan_result['discovered']} roles, {scan_result['new_jobs']} new")
    _emit({"type": "engine", "status": "discovered", "count": scan_result["discovered"]})

    # Phase 2: For each new DISCOVERED application, extract schema + resolve fields
    from src.storage.db import get_all_applications_with_jobs
    apps = get_all_applications_with_jobs(conn)
    discovered_apps = [a for a in apps if a.get("stage") == "DISCOVERED"]
    logger.info(f"=== Phase 2: Processing {len(discovered_apps)} DISCOVERED applications ===")

    apply_count = 0
    for app in discovered_apps:
        if _engine_stop_event and not _engine_stop_event.is_set():
            logger.info("Engine stopped by user")
            _emit({"type": "engine", "status": "stopped_by_user"})
            break

        # Daily cap enforcement
        if daily_cap > 0 and apply_count >= daily_cap:
            logger.info(f"Daily cap reached ({daily_cap} applications). Stopping.")
            _emit({"type": "engine", "status": "daily_cap_reached", "count": apply_count})
            break

        app_id = app["app_id"]
        company = app.get("company", "")
        title = app.get("role_title", "")
        job_url = app.get("job_url", "")
        role_family = app.get("role_family", "fullstack")
        detail = {"company": company, "title": title, "url": job_url, "status": ""}

        try:
            logger.info(f"[{company}] {title}")

            # Phase 2a: Extract form schema
            _emit({"type": "applying", "company": company, "title": title,
                    "app_id": app_id, "role_family": role_family})

            resolved = None
            # Try API-first schema extraction (no browser needed)
            raw_data = json.loads(app.get("raw_json", "{}")) if app.get("raw_json") else {}
            gh_slug = raw_data.get("gh_slug", "")
            gh_job_id = raw_data.get("gh_job_id", "")
            schema = None
            if gh_slug and gh_job_id:
                schema = extract_schema_from_api(gh_slug, gh_job_id)
                if schema:
                    logger.info(f"  Schema via API: {len(schema['fields'])} fields")
            if not schema:
                schema = extract_form_schema(job_url, headless=True)
            if schema and schema.get("fields"):
                schema_hash = schema["schema_hash"]
                save_form_schema(conn, schema_hash, job_url, company, schema["fields"])
                logger.info(f"  Schema: {len(schema['fields'])} fields, hash={schema_hash}")

                # Phase 2b: Resolve all fields
                resolved = resolve_all_fields(
                    schema["fields"], profile,
                    company=company, schema_hash=schema_hash, db_path=db_path,
                    role_family=role_family, role_title=title,
                )
                missing_required = get_missing_required(resolved)

                if missing_required:
                    missing_keys = [m["field_key"] for m in missing_required]
                    # Use descriptive reasons from get_missing_required
                    reasons = [m.get("reason", m.get("label", m["field_key"])[:60])
                               for m in missing_required]
                    skip_reason = (
                        f"Missing {len(missing_required)} required field(s): "
                        + "; ".join(reasons[:5])
                    )
                    logger.warning(f"  {skip_reason}")

                    if no_approval:
                        # No-approval mode: auto-skip unresolvable jobs
                        update_application_stage(conn, app_id, "SKIPPED_UNANSWERABLE",
                                                 notes=skip_reason)
                        update_application_status(conn, app_id, "SKIPPED_UNANSWERABLE", {
                            "missing_fields": missing_keys,
                            "skip_reason": skip_reason,
                        })
                        summary["skipped_unanswerable"] += 1
                        detail["status"] = "SKIPPED_UNANSWERABLE"
                        summary["details"].append(detail)
                        _emit({"type": "skipped", "company": company, "title": title,
                                "app_id": app_id, "reason": skip_reason})
                        continue
                    else:
                        # Legacy approval mode: pause for user data
                        update_application_stage(conn, app_id, "NEEDS_USER_DATA",
                                                 notes=skip_reason)
                        update_application_status(conn, app_id, "NEEDS_USER_DATA", {
                            "missing_fields": missing_keys,
                            "policy": policy,
                        })
                        summary["needs_user_data"] += 1
                        detail["status"] = "NEEDS_USER_DATA"
                        summary["details"].append(detail)
                        _emit({"type": "needs_data", "company": company, "title": title,
                                "app_id": app_id, "missing": missing_keys[:5]})
                        continue

                # All required fields resolved → MAPPABLE
                update_application_stage(conn, app_id, "MAPPABLE")
                _emit({"type": "mappable", "company": company, "title": title, "app_id": app_id})
            else:
                # No schema extracted — attempt apply with profile only
                update_application_stage(conn, app_id, "MAPPABLE")

            # Phase 3: Fill (and optionally submit)
            resume_path = get_resume_path(role_family)
            update_application_stage(conn, app_id, "FILLING")
            logger.info(f"  Filling with policy={policy}, resume={resume_path}...")

            result = apply_to_greenhouse(
                job_url=job_url, profile=profile, resume_path=resume_path,
                company=company, role_title=title, dry_run=dry_run,
                resolved_fields=resolved, policy=policy,
            )

            if result["status"] == "FILLED_AWAITING_SUBMIT":
                summary.setdefault("filled_awaiting", 0)
                summary["filled_awaiting"] = summary.get("filled_awaiting", 0) + 1
                detail["status"] = "FILLED_AWAITING_SUBMIT"
                update_application_status(conn, app_id, "FILLED_AWAITING_SUBMIT", {
                    "proof_json": {"proof_path": result["proof_path"]},
                    "policy": policy,
                })
                update_application_stage(conn, app_id, "FILLED_AWAITING_SUBMIT",
                                         notes=result.get("confirmation_text", ""))
                logger.info(f"  ✓ Filled (awaiting submit): {title} at {company}")
                _emit({"type": "filled", "company": company, "title": title,
                        "app_id": app_id, "proof_path": result["proof_path"]})

            elif result["status"] == "SUBMITTED":
                summary["applied"] += 1
                detail["status"] = "APPLIED"
                update_application_status(conn, app_id, "APPLIED", {
                    "submission_proof": result["proof_path"],
                    "confirmation_text": result["confirmation_text"],
                    "date_submitted": datetime.now(timezone.utc).isoformat(),
                })
                update_application_stage(conn, app_id, "APPLIED")
                logger.info(f"  ✓ Applied to {title} at {company}")
                _emit({"type": "applied", "company": company, "title": title, "app_id": app_id})
                apply_count += 1

            elif result["status"] == "NEEDS_HUMAN":
                summary["needs_human"] += 1
                detail["status"] = "NEEDS_HUMAN"
                update_application_status(conn, app_id, "NEEDS_HUMAN", {
                    "blocked_reason": result["blocked_reason"],
                    "proof_path": result["proof_path"],
                })
                update_application_stage(conn, app_id, "NEEDS_HUMAN",
                                         notes=result.get("blocked_reason", ""))
                logger.warning(f"  ⚠ Needs human: {result['blocked_reason']}")
                _emit({"type": "captcha", "company": company, "title": title,
                        "app_id": app_id, "reason": result["blocked_reason"]})
            else:
                summary["errors"] += 1
                detail["status"] = "ERROR"
                update_application_status(conn, app_id, "ERROR", {"error": result["error"]})
                update_application_stage(conn, app_id, "ERROR", notes=result.get("error", ""))
                logger.error(f"  ✗ Error: {result['error']}")
                _emit({"type": "error", "company": company, "title": title, "error": result["error"]})

            # Rate limiting: configurable wait between applications
            if apply_count > 0 and apply_count < len(discovered_apps):
                jitter = random.uniform(min_wait * 60, max_wait * 60)
                logger.info(f"  💤 Rate limit: waiting {jitter:.0f}s before next application")
                _emit({"type": "engine", "status": "rate_limit", "wait_seconds": int(jitter)})
                # Sleep in small increments so we can check for stop signal
                waited = 0
                while waited < jitter:
                    if _engine_stop_event and not _engine_stop_event.is_set():
                        break
                    time.sleep(min(5, jitter - waited))
                    waited += 5

        except Exception as e:
            summary["errors"] += 1
            detail["status"] = "ERROR"
            logger.error(f"  ✗ Exception: {e}", exc_info=True)

        summary["details"].append(detail)

    conn.close()
    logger.info(f"=== Summary: {json.dumps({k: v for k, v in summary.items() if k != 'details'})} ===")
    return summary