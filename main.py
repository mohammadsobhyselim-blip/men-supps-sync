"""
men-supps sync engine
---------------------
Usage:
  python main.py                  → start web server + scheduler (runs every N hours)
  python main.py --now            → run full cycle once (import + sync)
  python main.py --dry-run        → simulate without writing
  python main.py --sync-only      → price/stock only, skip new-product import
  python main.py --import-only    → discover & add new MFsupps products, skip sync
"""

import os
import sys
import logging
import threading
import colorlog
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import settings
from core.sync_engine import run_sync
from core.importer import import_new_mfsupps_products


def setup_logging():
    os.makedirs("logs", exist_ok=True)
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "white",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        }
    ))

    file_handler = logging.FileHandler("logs/sync.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))
    root.addHandler(handler)
    root.addHandler(file_handler)


def start_oauth_server():
    """Run the Flask OAuth server in a background thread."""
    from oauth_server import app
    port = int(os.environ.get("PORT", 8080))
    log = logging.getLogger("oauth_server")
    log.info(f"OAuth server listening on port {port}")
    app.run(host="0.0.0.0", port=port, use_reloader=False)


def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    args = sys.argv[1:]
    dry_run        = "--dry-run"        in args
    import_only    = "--import-only"    in args
    sync_only      = "--sync-only"      in args
    tag_fitnessbag = "--tag-fitnessbag" in args
    run_now        = "--now" in args or dry_run or import_only or sync_only or tag_fitnessbag

    if run_now:
        tag = " (DRY RUN)" if dry_run else ""
        if tag_fitnessbag:
            from core import shopify_client as shopify
            from config import settings
            logger.info(f"Backfilling supplier=MFB metafield on last 270 products{tag}")
            stats = shopify.bulk_set_supplier_on_last_n(
                n=270,
                supplier_value=settings.SUPPLIER_FITNESSBAG,
                dry_run=dry_run,
            )
            logger.info(f"Done: {stats}")
        elif import_only:
            logger.info(f"Manual: import only{tag}")
            import_new_mfsupps_products(dry_run=dry_run)
        elif sync_only:
            logger.info(f"Manual: sync only{tag}")
            run_sync(dry_run=dry_run, skip_import=True)
        else:
            logger.info(f"Manual: full cycle{tag}")
            run_sync(dry_run=dry_run)
        return

    # ── Scheduled mode: Flask (OAuth) + APScheduler run together ──
    logger.info(f"Starting OAuth server + scheduler (every {settings.SYNC_INTERVAL_HOURS}h)")

    flask_thread = threading.Thread(target=start_oauth_server, daemon=True)
    flask_thread.start()

    from oauth_server import load_token
    scheduler = BlockingScheduler(timezone="Africa/Cairo")
    scheduler.add_job(
        run_sync,
        trigger=IntervalTrigger(hours=settings.SYNC_INTERVAL_HOURS),
        kwargs={"dry_run": False},
        id="sync_job",
        name="men-supps full sync",
        misfire_grace_time=300,
    )

    # Only run initial cycle if token is already available
    if load_token():
        logger.info("Token found — running initial sync on startup...")
        run_sync(dry_run=False)
    else:
        logger.warning(
            "No Shopify token yet. "
            f"Visit {os.environ.get('APP_URL', 'http://localhost:8080')}/install to authorize."
        )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
