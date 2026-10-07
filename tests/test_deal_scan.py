"""Tests for the automated in-container deal scanner.

Settings CRUD + password masking, dedupe, auto-create restaurants, scan-disabled
paths, and IMAP fetching against a fake imaplib server fixture.

Run:  cd ~/workspace/foodfriday && PYTHONPATH=. .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import email.message
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ff-scan-"))
os.environ.setdefault("FOODFRIDAY_DATA_DIR", str(TMP / "data"))

from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db, SessionLocal  # noqa: E402
from app.deal_scan import (  # noqa: E402
    K_ENABLED,
    K_PASSWORD,
    K_TIME,
    fetch_promos,
    run_scan,
    scan_enabled,
    set_setting,
)
from app.models import Deal, Restaurant  # noqa: E402
from app.main import app  # noqa: E402
from app import scheduler as deal_scheduler  # noqa: E402

TODAY = date(2026, 10, 7)
SINCE = TODAY - timedelta(days=14)

init_db()
client = TestClient(app)


# ---------- fake IMAP server ----------

def _raw(sender: str, subject: str, body: str, sent: str) -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = sent
    msg.set_content(body)
    return msg.as_bytes()


PROMO_MAIL = [
    _raw(
        "SONIC <noreply@email.sonicdrivein.com>",
        "TODAY ONLY! $0.99 Corn Dogs",
        "Get $0.99 corn dogs today only at participating locations. Unsubscribe here.",
        "Wed, 07 Oct 2026 09:00:00 -0500",
    ),
    _raw(
        "Arby's <arbys@emails.arbys.com>",
        "Steak Bowl now, FREE Sandwich later",
        "Valid thru 11/1/2026. Order the steak bowl today and get a free sandwich next visit.",
        "Tue, 06 Oct 2026 09:00:00 -0500",
    ),
    _raw(  # brand fluff — no concrete offer
        "Sonic <noreply@email.sonicdrivein.com>",
        "New offers are here!",
        "Check out what's new this month at Sonic.",
        "Tue, 06 Oct 2026 09:00:00 -0500",
    ),
    _raw(  # expired — 'today only' two days ago
        "Chipotle <chipotle@chipotle.com>",
        "Free chips today only",
        "Claim your free chips today only.",
        "Mon, 05 Oct 2026 09:00:00 -0500",
    ),
    _raw(  # unknown chain -> auto-created restaurant
        "Freddy's <freddys@example.com>",
        "BOGO steakburgers this week",
        "Buy one get one steakburgers, this week only. Valid thru 10/14/2026.",
        "Wed, 07 Oct 2026 09:00:00 -0500",
    ),
]


class FakeIMAP:
    """Minimal imaplib.IMAP4_SSL stand-in serving PROMO_MAIL."""

    last_instance = None

    def __init__(self, host):
        self.host = host
        self.logged_in = None
        self.peek_used = False
        FakeIMAP.last_instance = self

    def login(self, user, password):
        self.logged_in = (user, password)
        if password == "bad-password":
            raise Exception("AUTHENTICATIONFAILED")

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return "OK", [b"5"]

    def search(self, charset, *criteria):
        # FROM value is the arg right after the FROM keyword
        frag = None
        crit = list(criteria)
        if "FROM" in crit:
            frag = str(crit[crit.index("FROM") + 1]).lower()
        hits = []
        for i, raw in enumerate(PROMO_MAIL, start=1):
            if frag and frag in raw.decode("utf-8", "replace").lower():
                hits.append(str(i).encode())
        return "OK", [b" ".join(hits)]

    def fetch(self, uid, spec):
        if "BODY.PEEK" in spec:
            self.peek_used = True
        i = int(uid) - 1
        return "OK", [(b"%d (BODY[] {%d}" % (int(uid), len(PROMO_MAIL[i])), PROMO_MAIL[i])]

    def close(self):
        return "OK", []

    def logout(self):
        return "OK", []


def _mailbox():
    return FakeIMAP


# ---------- fetch_promos ----------

def test_fetch_promos_parses_and_filters():
    deals = fetch_promos("u@gmail.com", "pw", imap_class=_mailbox(), today=TODAY)
    by_rest = {d["restaurant"]: d for d in deals}
    # concrete offers kept
    assert "Sonic" in by_rest
    assert by_rest["Sonic"]["title"].startswith("TODAY ONLY")
    assert by_rest["Sonic"]["valid_until"] == "2026-10-07"
    assert "Arby's" in by_rest
    assert by_rest["Arby's"]["valid_until"] == "2026-11-01"
    assert "Freddy's" in by_rest
    # fluff and expired dropped
    assert len(deals) == 3
    # BODY.PEEK used (read-only scan, nothing marked read)
    assert FakeIMAP.last_instance.peek_used


def test_fetch_promos_auth_failure_raises():
    import pytest

    with pytest.raises(Exception):
        fetch_promos("u@gmail.com", "bad-password", imap_class=_mailbox(), today=TODAY)


# ---------- settings API + masking ----------

def test_settings_password_never_returned():
    r = client.put("/api/settings", json={
        "gmail_address": "u@gmail.com",
        "gmail_app_password": "sekret-123",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["gmail_address"] == "u@gmail.com"
    assert body["gmail_app_password_set"] is True
    assert body["gmail_app_password"] == "********"
    assert "sekret-123" not in r.text

    # the page HTML must not contain it either
    page = client.get("/settings")
    assert page.status_code == 200
    assert "sekret-123" not in page.text
    assert "app password" in page.text.lower()


def test_settings_password_blank_keeps_old():
    # blank password on update must not wipe the stored one
    r = client.put("/api/settings", json={"deal_scan_time": "08:30"})
    assert r.status_code == 200
    assert r.json()["gmail_app_password_set"] is True
    assert r.json()["deal_scan_time"] == "08:30"


def test_settings_time_validation():
    r = client.put("/api/settings", json={"deal_scan_time": "nope"})
    assert r.status_code == 400
    r = client.put("/api/settings", json={"deal_scan_time": "25:00"})
    assert r.status_code == 400


def test_clear_gmail_forgets_creds():
    r = client.post("/api/settings/clear-gmail")
    assert r.status_code == 200
    body = r.json()
    assert body["gmail_app_password_set"] is False
    assert body["gmail_address"] == ""
    assert body["configured"] is False


# ---------- scan: dedupe + auto-create ----------

def _enable():
    client.put("/api/settings", json={
        "gmail_address": "u@gmail.com",
        "gmail_app_password": "pw",
        "deal_scan_enabled": True,
    })


def _reset_deals():
    s = SessionLocal()
    try:
        s.query(Deal).delete()
        s.query(Restaurant).delete()
        s.commit()
    finally:
        s.close()


def test_scan_now_adds_deals_and_autocreates_restaurant(monkeypatch):
    _reset_deals()
    _enable()
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "run_scan",
                        lambda session: __import__("app.deal_scan", fromlist=["run_scan"])
                        .run_scan(session, imap_class=_mailbox()))
    r = client.post("/api/deals/scan")
    assert r.status_code == 200
    data = r.json()
    assert data["deals_added"] == 3
    assert data["restaurants_added"] == 3  # Sonic, Arby's, Freddy's (auto-created)

    s = SessionLocal()
    try:
        freddys = s.query(Restaurant).filter(Restaurant.name == "Freddy's").one()
        assert freddys.price_tier == 1
        assert freddys.include_in_picks is True
        assert "auto-added" in (freddys.notes or "")
        deals = s.query(Deal).all()
        assert all(d.source == "email" for d in deals)
        assert all(d.restaurant_id is not None for d in deals)
    finally:
        s.close()


def test_scan_twice_is_idempotent(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "run_scan",
                        lambda session: __import__("app.deal_scan", fromlist=["run_scan"])
                        .run_scan(session, imap_class=_mailbox()))
    r = client.post("/api/deals/scan")
    assert r.status_code == 200
    assert r.json()["deals_added"] == 0
    assert r.json()["summary"] == "no new deals"


def test_scan_without_creds_fails():
    client.post("/api/settings/clear-gmail")
    r = client.post("/api/deals/scan")
    assert r.status_code == 502
    assert "not configured" in r.json()["detail"].lower()


# ---------- scheduler ----------

def test_scheduler_registers_and_reschedules():
    _enable()
    client.put("/api/settings", json={"deal_scan_time": "06:15"})
    jobs = {j.id: j for j in deal_scheduler.scheduler.get_jobs()}
    assert "deal-scan" in jobs
    trig = jobs["deal-scan"].trigger
    # cron fields: year, month, day, week, day_of_week, hour, minute, second
    assert (trig.fields[5].expressions[0].first, trig.fields[6].expressions[0].first) == (6, 15)

    # disabling removes the job
    client.put("/api/settings", json={"deal_scan_enabled": False})
    assert "deal-scan" not in {j.id for j in deal_scheduler.scheduler.get_jobs()}

    # re-enable restores it
    client.put("/api/settings", json={"deal_scan_enabled": True})
    assert "deal-scan" in {j.id for j in deal_scheduler.scheduler.get_jobs()}


def test_scheduler_skips_without_creds():
    client.post("/api/settings/clear-gmail")
    deal_scheduler.schedule_from_settings()
    assert "deal-scan" not in {j.id for j in deal_scheduler.scheduler.get_jobs()}


def test_scheduler_invalid_time_skips():
    _enable()
    s = SessionLocal()
    try:
        set_setting(s, K_TIME, "99:99")  # bypass API validation on purpose
        s.commit()
    finally:
        s.close()
    deal_scheduler.schedule_from_settings()
    assert "deal-scan" not in {j.id for j in deal_scheduler.scheduler.get_jobs()}


def test_scan_enabled_helper():
    s = SessionLocal()
    try:
        set_setting(s, K_ENABLED, "0")
        s.commit()
        assert scan_enabled(s) is False
        set_setting(s, K_ENABLED, "1")
        s.commit()
        assert scan_enabled(s) is True  # creds set by _enable()
    finally:
        s.close()
