"""Tests for the Friday Discord nudge.

Run:  cd ~/workspace/foodfriday && PYTHONPATH=. .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ff-nudge-"))
os.environ.setdefault("FOODFRIDAY_DATA_DIR", str(TMP / "data"))

from fastapi.testclient import TestClient  # noqa: E402

from app import scheduler  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.deal_scan import get_setting, set_setting  # noqa: E402
from app.main import app  # noqa: E402
from app.nudge import (  # noqa: E402
    K_NUDGE_ENABLED,
    K_NUDGE_LAST_RESULT,
    K_NUDGE_TIME,
    K_WEBHOOK,
    build_message,
    nudge_enabled,
    send_nudge,
)

init_db()
client = TestClient(app)

POSTED: list[dict] = []


def fake_post(url, payload):
    POSTED.append({"url": url, "payload": payload})


def _webhook(on=True):
    s = SessionLocal()
    try:
        set_setting(s, K_WEBHOOK, "https://discord.test/webhook" if on else None)
        set_setting(s, K_NUDGE_ENABLED, "1" if on else "0")
        set_setting(s, K_NUDGE_TIME, "10:00")
        s.commit()
    finally:
        s.close()


def test_build_message():
    picks = [
        {"name": "Taco Town", "cuisine": "Mexican", "reason": "A favorite",
         "deal_titles": ["$2 tacos"]},
        {"name": "Noodle House", "cuisine": "Ramen", "reason": "Never tried",
         "deal_titles": []},
    ]
    msg = build_message(picks)
    assert "Friday dinner" in msg
    assert "Taco Town" in msg and "(Mexican)" in msg
    assert "🏷️ $2 tacos" in msg
    assert "Noodle House" in msg


def test_send_nudge_posts_picks():
    POSTED.clear()
    _webhook(True)
    client.post("/api/restaurants", json={"name": "Nudge Noodles", "cuisine": "Ramen"})
    s = SessionLocal()
    try:
        result = send_nudge(s, http_post=fake_post)
    finally:
        s.close()
    assert result["ok"] is True
    assert len(POSTED) == 1
    assert POSTED[0]["url"] == "https://discord.test/webhook"
    # the test DB is shared across test files, so the 3 random picks aren't
    # deterministic — assert the message shape, not the specific restaurant
    content = POSTED[0]["payload"]["content"]
    assert "Friday dinner" in content
    assert content.count("•") >= 1
    s = SessionLocal()
    try:
        assert get_setting(s, K_NUDGE_LAST_RESULT, "").startswith("sent")
    finally:
        s.close()


def test_send_nudge_test_mode():
    POSTED.clear()
    _webhook(True)
    s = SessionLocal()
    try:
        result = send_nudge(s, http_post=fake_post, test=True)
    finally:
        s.close()
    assert result["ok"] is True
    assert "test" in POSTED[0]["payload"]["content"].lower()


def test_send_nudge_no_webhook():
    _webhook(False)
    s = SessionLocal()
    try:
        assert nudge_enabled(s) is False
        result = send_nudge(s, http_post=fake_post)
    finally:
        s.close()
    assert result["ok"] is False
    assert "not configured" in result["error"]


def test_send_nudge_failure_recorded_not_raised():
    _webhook(True)

    def boom(url, payload):
        raise RuntimeError("connection refused")

    s = SessionLocal()
    try:
        result = send_nudge(s, http_post=boom)
    finally:
        s.close()
    assert result["ok"] is False
    s = SessionLocal()
    try:
        assert "connection refused" in get_setting(s, K_NUDGE_LAST_RESULT, "")
    finally:
        s.close()


def test_test_nudge_endpoint(monkeypatch):
    POSTED.clear()
    _webhook(True)
    monkeypatch.setattr("app.nudge._http_post", fake_post)
    r = client.post("/api/settings/test-nudge")
    assert r.status_code == 200, r.text
    assert len(POSTED) == 1
    assert "test" in POSTED[0]["payload"]["content"].lower()


def test_webhook_masked_in_settings():
    _webhook(True)
    s = client.get("/api/settings").json()
    assert s["discord_webhook_set"] is True
    assert s["discord_webhook_url"] == "********"
    assert "discord.test" not in str(s)


def test_nudge_scheduling():
    _webhook(True)
    try:
        scheduler.schedule_nudge_from_settings()
        job = scheduler.scheduler.get_job(scheduler.NUDGE_JOB_ID)
        assert job is not None
        trigger = str(job.trigger)
        assert "day_of_week='fri'" in trigger
        assert "hour='10'" in trigger
    finally:
        for job in list(scheduler.scheduler.get_jobs()):
            if job.id == scheduler.NUDGE_JOB_ID:
                scheduler.scheduler.remove_job(job.id)


def test_nudge_unscheduled_when_disabled():
    _webhook(False)
    try:
        scheduler.schedule_nudge_from_settings()
        assert scheduler.scheduler.get_job(scheduler.NUDGE_JOB_ID) is None
    finally:
        for job in list(scheduler.scheduler.get_jobs()):
            if job.id == scheduler.NUDGE_JOB_ID:
                scheduler.scheduler.remove_job(job.id)
