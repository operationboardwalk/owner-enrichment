"""
scheduler.py — Background health check scheduler.

Runs diagnostics on boardwalk every N minutes and auto-fixes critical issues.
"""

import json
import logging
import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

import config as cfg_module
import diagnostics
import fixes

logger = logging.getLogger("boardwalk.scheduler")

# Global scheduler instance
_scheduler = BackgroundScheduler()
_last_results: list = []
_last_run: str = ""

STATUS_PATH = Path(__file__).parent / "last_status.json"


def _save_status(results: list) -> None:
    global _last_results, _last_run
    _last_results = results
    _last_run = datetime.datetime.utcnow().isoformat() + "Z"
    try:
        with open(STATUS_PATH, "w") as f:
            json.dump({"last_run": _last_run, "results": results}, f, indent=2)
    except Exception:
        pass


def load_last_status() -> dict:
    if STATUS_PATH.exists():
        try:
            with open(STATUS_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_run": None, "results": []}


def run_health_check() -> list:
    """Run all diagnostics and auto-fix critical issues if configured."""
    cfg = cfg_module.load()

    if not cfg_module.is_setup_complete(cfg):
        logger.debug("Setup not complete — skipping health check")
        return []

    logger.info("Running scheduled health check...")
    results = diagnostics.run_all(cfg)
    _save_status(results)

    auto_fix = cfg.get("auto_fix_critical", True)
    if auto_fix:
        for r in results:
            if r["status"] == "critical":
                issue_id = r["id"]
                if issue_id in fixes.ISSUE_FIX_MAP:
                    logger.warning(f"Auto-fixing critical issue: {issue_id}")
                    fix_result = fixes.apply_fix(issue_id, cfg)
                    logger.info(f"Fix result for {issue_id}: {fix_result['message']}")

    return results


def start(interval_minutes: int = None) -> None:
    """Start the background scheduler."""
    cfg = cfg_module.load()
    if interval_minutes is None:
        interval_minutes = int(cfg.get("health_check_interval_minutes", 15))

    if _scheduler.running:
        _scheduler.remove_all_jobs()
    else:
        _scheduler.start()

    _scheduler.add_job(
        run_health_check,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="health_check",
        replace_existing=True,
        next_run_time=datetime.datetime.now(),  # run immediately on start
    )
    logger.info(f"Scheduler started: health check every {interval_minutes} minutes")


def stop() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def get_last_results() -> list:
    return _last_results


def get_last_run() -> str:
    return _last_run
