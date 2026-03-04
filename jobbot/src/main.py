"""CLI entry point for jobbot.

Usage:
    python -m src.main run-once          # Single discovery + apply + track cycle
    python -m src.main run-daemon        # Loop every N minutes
    python -m src.main resume --app-id X # Resume a NEEDS_HUMAN application
    python -m src.main summary           # Print daily summary
    python -m src.main status            # Show all tracked applications
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
from src.agents.scout_applier import resume_application, run_scout_applier
from src.utils.logging import setup_logging

logger = setup_logging("jobbot.main")


@click.group()
def cli():
    """Jobbot — Local job application automation."""
    pass


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
