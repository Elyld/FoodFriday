"""Daily deal-scan scheduling (APScheduler, in-process).

The scanner job runs once a day at the `deal_scan_time` setting (HH:MM,
container-local time). It only fires when scanning is enabled and Gmail
creds are present. Settings changes re-arm the schedule.

The Friday nudge job posts the week's 3 picks to Discord every Friday at the
`friday_nudge_time` setting (container-local time), when enabled and a
webhook URL is set.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.database import SessionLocal
from app.deal_scan import K_TIME, get_setting, reset_stale_running, try_start_scan

logger = logging.getLogger(__name__)

JOB_ID = "deal-scan"
NUDGE_JOB_ID = "friday-nudge"

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
    schedule_nudge_from_settings()


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


# ---------- friday nudge ----------

def _nudge_job() -> None:
    """The scheduled Friday nudge — post picks to Discord, never crash."""
    from app.nudge import K_NUDGE_LAST_RESULT, send_nudge
    from app.deal_scan import set_setting

    session = SessionLocal()
    try:
        send_nudge(session)
    except Exception as exc:  # belt & suspenders — send_nudge already records failures
        logger.warning("scheduled friday nudge crashed: %s", exc)
        try:
            set_setting(session, K_NUDGE_LAST_RESULT, f"error: {exc}")
            session.commit()
        except Exception:
            pass
    finally:
        session.close()


def _valid_hhmm(raw: str | None, default: str) -> tuple[int, int] | None:
    try:
        h_s, m_s = (raw or default).strip().split(":")
        h, m = int(h_s), int(m_s)
        assert 0 <= h <= 23 and 0 <= m <= 59
        return h, m
    except (ValueError, AssertionError):
        return None


def schedule_nudge_from_settings() -> None:
    """(Re)arm the Friday nudge from the current settings."""
    for job in list(scheduler.get_jobs()):
        if job.id == NUDGE_JOB_ID:
            scheduler.remove_job(NUDGE_JOB_ID)
    session = SessionLocal()
    try:
        from app.nudge import K_NUDGE_ENABLED, K_NUDGE_TIME, K_WEBHOOK

        time_raw = (get_setting(session, K_NUDGE_TIME, "10:00") or "10:00").strip()
        enabled = get_setting(session, K_NUDGE_ENABLED, "0") == "1"
        has_webhook = bool(get_setting(session, K_WEBHOOK))
    finally:
        session.close()
    if not (enabled and has_webhook):
        return  # nothing to schedule
    hm = _valid_hhmm(time_raw, "10:00")
    if hm is None:
        logger.warning("invalid friday_nudge_time %r — nudge not scheduled", time_raw)
        return
    hour, minute = hm
    scheduler.add_job(
        _nudge_job, CronTrigger(day_of_week="fri", hour=hour, minute=minute),
        id=NUDGE_JOB_ID, misfire_grace_time=3600, coalesce=True,
    )
    logger.info("friday nudge scheduled for Fridays at %02d:%02d", hour, minute)
