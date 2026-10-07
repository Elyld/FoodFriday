"""Daily deal-scan scheduling (APScheduler, in-process).

The scanner job runs once a day at the `deal_scan_time` setting (HH:MM,
container-local time). It only fires when scanning is enabled and Gmail
creds are present. Settings changes re-arm the schedule.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.database import SessionLocal
from app.deal_scan import K_TIME, get_setting, reset_stale_running, try_start_scan

logger = logging.getLogger(__name__)

JOB_ID = "deal-scan"

scheduler = BackgroundScheduler()


def _job() -> None:
    """The scheduled scan — starts a background scan, never overlapping a
    manual one (try_start_scan returns False while one is running)."""
    try:
        started = try_start_scan(SessionLocal)
    except RuntimeError:
        return  # creds removed since scheduling; nothing to do
    if not started:
        logger.info("scheduled deal scan skipped — a scan is already running")


def schedule_from_settings() -> None:
    """(Re)arm the daily scan from the current settings. Call on startup and
    whenever the schedule-related settings change."""
    for job in list(scheduler.get_jobs()):
        if job.id == JOB_ID:
            scheduler.remove_job(JOB_ID)
    session = SessionLocal()
    try:
        time_raw = (get_setting(session, K_TIME, "07:00") or "07:00").strip()
        enabled = get_setting(session, "deal_scan_enabled", "1") == "1"
        has_creds = bool(get_setting(session, "gmail_address")) and bool(
            get_setting(session, "gmail_app_password")
        )
    finally:
        session.close()
    if not (enabled and has_creds):
        return  # nothing to schedule
    try:
        hour_s, minute_s = time_raw.split(":")
        hour, minute = int(hour_s), int(minute_s)
        assert 0 <= hour <= 23 and 0 <= minute <= 59
    except (ValueError, AssertionError):
        logger.warning("invalid deal_scan_time %r — scan not scheduled", time_raw)
        return
    scheduler.add_job(
        _job, CronTrigger(hour=hour, minute=minute), id=JOB_ID,
        misfire_grace_time=3600, coalesce=True,
    )
    logger.info("deal scan scheduled daily at %02d:%02d", hour, minute)


def start() -> None:
    if not scheduler.running:
        scheduler.start()
    reset_stale_running(SessionLocal)  # a restart must not wedge scans as "running"
    schedule_from_settings()


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
