"""JarvisForJobs — 100% local, API-first job application automation.

Usage (preferred):
    python -m src.main gmi               # Start the engine (scan → map → fill)
    python -m src.main ngmi              # Stop the engine
    python -m src.main scan              # Scan Greenhouse boards only
    python -m src.main status            # Show all tracked applications

Legacy aliases still work:
    python -m src.main run-once          # Single discovery + apply + track cycle
    python -m src.main run-daemon        # Loop every N minutes
    python -m src.main scan-gh           # Alias for scan
"""

import os
import signal
import sys
import time
from pathlib import Path

import click
from dotenv import load_dotenv

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Load .env from project root
load_dotenv(PROJECT_ROOT / ".env")

from src.agents.excel_tracker import print_daily_summary, run_excel_tracker
from src.agents.scout_applier import resume_application, run_greenhouse_direct, run_scout_applier
from src.greenhouse.scanner import scan_greenhouse_boards
from src.utils.logging import setup_logging

logger = setup_logging("jobbot.main")


@click.group()
def cli():
    """JarvisForJobs — 100% local, API-first job application automation."""
    pass


# ── Jarvis-branded commands ────────────────────────────────────────────────

@cli.command("gmi")
@click.option("--dry-run", is_flag=True, default=False,
              help="Fill forms but do not submit")
@click.option("--max", "max_jobs", type=int, default=40,
              help="Maximum number of jobs to process per cycle")
@click.option("--policy", type=click.Choice(["pause_at_submit", "auto_if_safe"]),
              default="pause_at_submit", show_default=True,
              help="Submit policy")
@click.option("--daemon", is_flag=True, default=False,
              help="Run continuously (loop every N minutes)")
@click.option("--interval", type=int, default=None,
              help="Minutes between daemon cycles (default: from .env or 30)")
def gmi(dry_run: bool, max_jobs: int, policy: str, daemon: bool, interval: int):
    """Start the Jarvis engine: scan → map → fill.

    Default behaviour: run one cycle with pause_at_submit policy.
    Use --daemon to loop continuously.  Use --policy auto_if_safe to submit.
    """
    if dry_run:
        os.environ["DRY_RUN"] = "true"
        logger.info("🏜  DRY RUN MODE: forms will be filled but NOT submitted")

    click.echo("🚀 jarvis gmi — we're so back")

    if daemon:
        if interval is None:
            interval = int(os.environ.get("RUN_INTERVAL_MINUTES", "30"))
        _run_daemon_loop(max_jobs=max_jobs, dry_run=dry_run, policy=policy,
                         interval=interval)
    else:
        summary = run_greenhouse_direct(max_jobs=max_jobs, dry_run=dry_run,
                                         policy=policy)
        _print_gmi_summary(summary)
        run_excel_tracker()


@cli.command("ngmi")
def ngmi():
    """Stop the Jarvis engine (sends SIGTERM to the daemon).

    If no daemon is running this is a no-op.
    """
    import subprocess

    click.echo("🛑 jarvis ngmi — shutting down")
    # Find running daemon by process name
    try:
        result = subprocess.run(
            ["pkill", "-f", "src.main gmi.*--daemon"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            click.echo("  Daemon process terminated.")
        else:
            click.echo("  No running daemon found.")
    except Exception as e:
        click.echo(f"  Could not stop daemon: {e}")


@cli.command("scan")
@click.option("--max", "max_jobs", type=int, default=200,
              help="Maximum number of jobs to return")
def scan(max_jobs: int):
    """Scan Greenhouse boards for matching roles (no apply)."""
    click.echo("🔍 jarvis scan — scanning boards...")
    summary = scan_greenhouse_boards(max_jobs=max_jobs)
    _print_scan_summary(summary)


def _run_daemon_loop(*, max_jobs: int, dry_run: bool, policy: str,
                     interval: int):
    """Continuous scan→fill loop with graceful shutdown."""
    running = True

    def signal_handler(sig, frame):
        nonlocal running
        logger.info("Received shutdown signal. Finishing current cycle...")
        running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    cycle = 0
    while running:
        cycle += 1
        click.echo(f"\n=== Cycle {cycle} ===")
        try:
            summary = run_greenhouse_direct(max_jobs=max_jobs, dry_run=dry_run,
                                             policy=policy)
            _print_gmi_summary(summary)
            run_excel_tracker()
        except Exception as e:
            logger.error(f"Error in cycle {cycle}: {e}", exc_info=True)

        if running:
            logger.info(f"Sleeping {interval} minutes until next cycle...")
            for _ in range(interval * 60):
                if not running:
                    break
                time.sleep(1)

    click.echo("Daemon stopped. ngmi achieved. ✌️")


def _print_gmi_summary(summary: dict):
    click.echo(f"\n  Discovered:      {summary['discovered']}")
    click.echo(f"  New jobs:        {summary['new_jobs']}")
    click.echo(f"  Filled (wait):   {summary.get('filled_awaiting', 0)}")
    click.echo(f"  Applied:         {summary['applied']}")
    click.echo(f"  Needs user data: {summary['needs_user_data']}")
    click.echo(f"  Needs human:     {summary['needs_human']}")
    click.echo(f"  Errors:          {summary['errors']}")
    if summary.get("details"):
        click.echo(f"\n  {'Company':<20} {'Role':<40} {'Status'}")
        click.echo("  " + "-" * 70)
        for d in summary["details"]:
            click.echo(
                f"  {d.get('company', '?')[:20]:<20} "
                f"{d.get('title', '?')[:40]:<40} "
                f"{d.get('status', '?')}"
            )


def _print_scan_summary(summary: dict):
    click.echo(f"\n{'='*60}")
    click.echo(f"  GREENHOUSE SCAN RESULTS")
    click.echo(f"{'='*60}")
    click.echo(f"  Companies scanned:    {summary['companies_scanned']}")
    click.echo(f"  Companies with hits:  {summary['companies_with_hits']}")
    click.echo(f"  Total intern roles:   {summary['discovered']}")
    click.echo(f"  New (not seen before):{summary['new_jobs']}")
    click.echo(f"  Errors:               {summary['errors']}")
    click.echo(f"{'='*60}")
    if summary["jobs"]:
        click.echo(f"\n  {'Company':<20} {'Role Family':<12} {'Title':<45} {'New?'}")
        click.echo("  " + "-" * 85)
        for j in summary["jobs"]:
            marker = "✓ NEW" if j["is_new"] else "  seen"
            click.echo(
                f"  {j['company'][:20]:<20} {j['role_family']:<12} "
                f"{j['title'][:45]:<45} {marker}"
            )
    click.echo()


@cli.command("run-once")
@click.option("--dry-run", is_flag=True, default=False,
              help="Fill forms but do not submit")
@click.option("--threshold", type=float, default=None,
              help="Match score threshold (default: from .env or 0.6)")
def run_once(dry_run: bool, threshold: float):
    """Run one cycle: discover → classify → apply → track."""
    if dry_run:
        os.environ["DRY_RUN"] = "true"
        logger.info("DRY RUN MODE: forms will be filled but not submitted")

    logger.info("=== Starting run-once cycle ===")

    # Phase 1: Scout and apply
    scout_summary = run_scout_applier(dry_run=dry_run, threshold=threshold)

    # Phase 2: Update Excel tracker
    tracker_summary = run_excel_tracker()

    # Phase 3: Print summary
    print_daily_summary()

    logger.info("=== Run-once cycle complete ===")
    click.echo(f"\nDiscovered: {scout_summary['discovered']}")
    click.echo(f"New jobs: {scout_summary['new_jobs']}")
    click.echo(f"Classified: {scout_summary['classified']}")
    click.echo(f"Applied: {scout_summary['applied']}")
    click.echo(f"Needs human: {scout_summary['needs_human']}")
    click.echo(f"Errors: {scout_summary['errors']}")


@cli.command("run-daemon")
@click.option("--dry-run", is_flag=True, default=False,
              help="Fill forms but do not submit")
@click.option("--interval", type=int, default=None,
              help="Minutes between cycles (default: from .env or 30)")
@click.option("--threshold", type=float, default=None,
              help="Match score threshold")
def run_daemon(dry_run: bool, interval: int, threshold: float):
    """Run in a loop, discovering and applying every N minutes."""
    if interval is None:
        interval = int(os.environ.get("RUN_INTERVAL_MINUTES", "30"))

    if dry_run:
        os.environ["DRY_RUN"] = "true"

    logger.info(f"Starting daemon mode (interval: {interval}m, dry_run: {dry_run})")

    # Handle graceful shutdown
    running = True

    def signal_handler(sig, frame):
        nonlocal running
        logger.info("Received shutdown signal. Finishing current cycle...")
        running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    cycle = 0
    while running:
        cycle += 1
        logger.info(f"=== Daemon cycle {cycle} ===")

        try:
            scout_summary = run_scout_applier(dry_run=dry_run, threshold=threshold)
            run_excel_tracker()
            print_daily_summary()

            click.echo(f"\nCycle {cycle} complete. Applied: {scout_summary['applied']}, "
                       f"Needs human: {scout_summary['needs_human']}")
        except Exception as e:
            logger.error(f"Error in cycle {cycle}: {e}", exc_info=True)

        if running:
            logger.info(f"Sleeping {interval} minutes until next cycle...")
            # Sleep in small increments to allow graceful shutdown
            for _ in range(interval * 60):
                if not running:
                    break
                time.sleep(1)

    logger.info("Daemon stopped.")


@cli.command("resume")
@click.option("--app-id", required=True, help="Application ID to resume")
@click.option("--dry-run", is_flag=True, default=False)
def resume(app_id: str, dry_run: bool):
    """Resume a NEEDS_HUMAN application after manual intervention."""
    logger.info(f"Resuming application: {app_id}")

    result = resume_application(app_id, dry_run=dry_run)

    if result.get("error"):
        click.echo(f"Error: {result['error']}")
        sys.exit(1)

    click.echo(f"Result: {result['status']}")
    if result.get("proof_path"):
        click.echo(f"Proof: {result['proof_path']}")

    # Update Excel
    run_excel_tracker()


@cli.command("apply-waas")
@click.option("--max", "max_apps", type=int, default=20,
              help="Maximum number of applications to submit")
@click.option("--dry-run", is_flag=True, default=False)
def apply_waas(max_apps: int, dry_run: bool):
    """Log into WAAS and apply to jobs (opens visible browser for CAPTCHAs)."""
    from src.agents.scout_applier import load_profile
    profile = load_profile()

    from src.yc.apply_waas import apply_to_waas_jobs

    logger.info(f"Starting WAAS apply flow (max: {max_apps}, dry_run: {dry_run})")
    result = apply_to_waas_jobs(profile, max_applications=max_apps, dry_run=dry_run)

    click.echo(f"\nApplied: {result['applied']}")
    click.echo(f"Already applied: {result.get('already_applied', 0)}")
    click.echo(f"Needs human: {result['needs_human']}")
    click.echo(f"Errors: {result['errors']}")

    if result.get("details"):
        click.echo("\nDetails:")
        for d in result["details"]:
            click.echo(f"  {d.get('company', '?'):20s} | {d.get('title', '?'):30s} | {d.get('status', '?')}")

    # Update Excel tracker
    run_excel_tracker()


@cli.command("apply-greenhouse")
@click.option("--max", "max_jobs", type=int, default=40,
              help="Maximum number of jobs to process")
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--policy", type=click.Choice(["pause_at_submit", "auto_if_safe"]),
              default="pause_at_submit", show_default=True,
              help="Submit policy: pause_at_submit (default) or auto_if_safe")
def apply_greenhouse(max_jobs: int, dry_run: bool, policy: str):
    """Scan Greenhouse boards and fill applications (default: stop before submit)."""
    logger.info(f"Starting Greenhouse apply (max: {max_jobs}, dry_run: {dry_run}, policy: {policy})")
    summary = run_greenhouse_direct(max_jobs=max_jobs, dry_run=dry_run, policy=policy)

    click.echo(f"\nDiscovered: {summary['discovered']}")
    click.echo(f"New jobs: {summary['new_jobs']}")
    click.echo(f"Filled (awaiting submit): {summary.get('filled_awaiting', 0)}")
    click.echo(f"Applied: {summary['applied']}")
    click.echo(f"Needs user data: {summary['needs_user_data']}")
    click.echo(f"Needs human: {summary['needs_human']}")
    click.echo(f"Errors: {summary['errors']}")

    if summary.get("details"):
        click.echo("\nDetails:")
        for d in summary["details"]:
            click.echo(f"  {d.get('company', '?'):20s} | {d.get('title', '?'):40s} | {d.get('status', '?')}")

    # Update Excel tracker
    run_excel_tracker()


@cli.command("gh-fill")
@click.option("--app-id", "app_id", default=None,
              help="Fill a specific application by app_id")
@click.option("--app-ids", "app_ids_str", default=None,
              help="Comma-separated list of app_ids to fill")
@click.option("--all-ready", is_flag=True, default=False,
              help="Fill all MAPPABLE or DISCOVERED applications")
@click.option("--policy", type=click.Choice(["pause_at_submit", "auto_if_safe"]),
              default="pause_at_submit", show_default=True)
def gh_fill(app_id: str, app_ids_str: str, all_ready: bool, policy: str):
    """Fill one or more applications (DISCOVERED or MAPPABLE) via Playwright.

    Runs schema extraction + field mapping + form fill for each target.
    Default policy stops before submit (pause_at_submit); use auto_if_safe to submit.
    """
    import json as _json

    from src.storage.db import (
        get_application_by_id, get_applications_by_status,
        get_connection, get_job_by_dedup,
        update_application_status, update_application_stage,
    )
    from src.greenhouse.apply_playwright import apply_to_greenhouse
    from src.greenhouse.schema_extract import extract_form_schema
    from src.mapping.auto_map import get_missing_required, resolve_all_fields
    from src.storage.db import save_form_schema
    from src.utils.role_classify import get_resume_path
    from src.agents.scout_applier import load_profile

    profile = load_profile()
    conn = get_connection()

    if app_id:
        raw_targets = [get_application_by_id(conn, app_id)]
    elif app_ids_str:
        ids = [i.strip() for i in app_ids_str.split(",") if i.strip()]
        raw_targets = [get_application_by_id(conn, i) for i in ids]
    elif all_ready:
        raw_targets = (
            get_applications_by_status(conn, "MAPPABLE") +
            get_applications_by_status(conn, "DISCOVERED")
        )
    else:
        click.echo("Provide --app-id, --app-ids, or --all-ready")
        conn.close()
        return

    targets = [t for t in raw_targets if t]
    if not targets:
        click.echo("No target applications found.")
        conn.close()
        return

    click.echo(f"Filling {len(targets)} application(s) with policy={policy}\n")
    for app in targets:
        a_id = app["app_id"]
        job = get_job_by_dedup(conn, app["dedup_key"])
        if not job:
            click.echo(f"  [{a_id}] job record not found, skipping")
            continue

        raw = _json.loads(job["raw_json"]) if job.get("raw_json") else {}
        job_url = raw.get("apply_url", job["job_url"])
        company = job.get("company", "")
        role_title = job.get("role_title", "")
        role_family = job.get("role_family", "fullstack")
        resume_path = get_resume_path(role_family)

        click.echo(f"  [{company}] {role_title}")

        # Schema extraction + field mapping
        resolved = None
        schema = extract_form_schema(job_url, headless=True)
        if schema and schema.get("fields"):
            schema_hash = schema["schema_hash"]
            save_form_schema(conn, schema_hash, job_url, company, schema["fields"])
            resolved = resolve_all_fields(
                schema["fields"], profile,
                company=company, schema_hash=schema_hash,
                role_family=role_family, role_title=role_title,
            )
            missing = get_missing_required(resolved)
            if missing:
                missing_keys = [m["field_key"] for m in missing]
                click.echo(f"    ⚠ Missing required fields: {missing_keys[:5]}")
                update_application_status(conn, a_id, "NEEDS_USER_DATA", {
                    "missing_fields": missing_keys, "policy": policy,
                })
                update_application_stage(conn, a_id, "NEEDS_USER_DATA",
                                         notes=f"Missing: {', '.join(missing_keys[:5])}")
                continue
            update_application_stage(conn, a_id, "MAPPABLE")

        # Fill
        update_application_stage(conn, a_id, "FILLING")
        result = apply_to_greenhouse(
            job_url=job_url, profile=profile, resume_path=resume_path,
            company=company, role_title=role_title, policy=policy,
            resolved_fields=resolved,
        )

        if result["status"] == "FILLED_AWAITING_SUBMIT":
            update_application_status(conn, a_id, "FILLED_AWAITING_SUBMIT", {
                "proof_json": {"proof_path": result["proof_path"]}, "policy": policy,
            })
            update_application_stage(conn, a_id, "FILLED_AWAITING_SUBMIT")
            click.echo(f"    ✓ FILLED — screenshot: {result['proof_path']}")
        elif result["status"] == "SUBMITTED":
            update_application_status(conn, a_id, "APPLIED", {
                "submission_proof": result["proof_path"],
            })
            update_application_stage(conn, a_id, "APPLIED")
            click.echo(f"    ✓ APPLIED")
        elif result["status"] == "NEEDS_HUMAN":
            update_application_status(conn, a_id, "NEEDS_HUMAN", {
                "blocked_reason": result.get("blocked_reason", ""),
            })
            update_application_stage(conn, a_id, "NEEDS_HUMAN",
                                     notes=result.get("blocked_reason", ""))
            click.echo(f"    ⚠ NEEDS_HUMAN: {result.get('blocked_reason', '')}")
        else:
            click.echo(f"    ✗ {result['status']}: {result.get('error', '')}")

    conn.close()
    run_excel_tracker()


@cli.command("rebuild-excel")
def rebuild_excel_cmd():
    """Drop and rebuild applications.xlsx from the database."""
    from src.storage.excel import rebuild_excel
    from src.storage.db import DB_PATH

    click.echo("Rebuilding applications.xlsx from DB...")
    count = rebuild_excel(DB_PATH)
    click.echo(f"Done — {count} applications written.")


@cli.command("scan-gh")
@click.option("--max", "max_jobs", type=int, default=200,
              help="Maximum number of jobs to return")
def scan_gh(max_jobs: int):
    """Scan Greenhouse boards (legacy alias for 'scan')."""
    summary = scan_greenhouse_boards(max_jobs=max_jobs)
    _print_scan_summary(summary)


@cli.command("summary")
def summary():
    """Print daily summary of all applications."""
    print_daily_summary()


@cli.command("status")
def status():
    """Show all tracked applications."""
    from src.storage.db import get_all_applications_with_jobs, get_connection

    conn = get_connection()
    apps = get_all_applications_with_jobs(conn)
    conn.close()

    if not apps:
        click.echo("No applications tracked yet.")
        return

    click.echo(f"\n{'ID':>12} {'Status':>14} {'Score':>6} {'Company':<20} {'Role':<30}")
    click.echo("-" * 85)
    for app in apps:
        click.echo(
            f"{app['app_id']:>12} {app['status']:>14} "
            f"{app.get('match_score', 0):>6.2f} "
            f"{(app.get('company', '') or '')[:20]:<20} "
            f"{(app.get('role_title', '') or '')[:30]:<30}"
        )
    click.echo(f"\nTotal: {len(apps)} applications\n")


if __name__ == "__main__":
    cli()
