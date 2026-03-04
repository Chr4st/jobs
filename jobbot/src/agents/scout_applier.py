"""ScoutApplier agent: discovers jobs from YC, parses Greenhouse, classifies, scores, and applies."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from src.greenhouse.apply_playwright import apply_to_greenhouse
from src.greenhouse.parse import parse_greenhouse_job
from src.storage.db import (
    create_application,
    get_connection,
    get_job_by_dedup,
    update_application_status,
    upsert_job,
)
from src.utils.dedupe import make_dedup_key
from src.utils.logging import setup_logging
from src.utils.role_classify import classify_role, get_resume_path
from src.utils.scoring import compute_match_score
from src.yc.discover import discover_greenhouse_jobs

logger = setup_logging("jobbot.agent.scout")

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_profile() -> dict:
    """Load user profile from profile.yaml."""
    profile_path = PROJECT_ROOT / "profile.yaml"
    if not profile_path.exists():
        logger.error("profile.yaml not found. Copy profile.yaml.example to profile.yaml and fill in your details.")
        raise FileNotFoundError("profile.yaml not found")
    with open(profile_path) as f:
        return yaml.safe_load(f)


def run_scout_applier(
    dry_run: bool = False,
    threshold: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Run the full scout+apply pipeline.

    Returns summary dict with counts.
    """
    if threshold is None:
        threshold = float(os.environ.get("MATCH_SCORE_THRESHOLD", "0.6"))

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
