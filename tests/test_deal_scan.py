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
    K_STATUS,
    K_TIME,
    fetch_promos,
    list_accounts,
    migrate_legacy_gmail_settings,
    reset_stale_running,
    run_scan,
    scan_enabled,
    set_setting,
    try_start_scan,
)
from app.models import Deal, EmailAccount, Restaurant  # noqa: E402
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

    def __init__(self, host, **kwargs):
        self.host = host
        self.kwargs = kwargs
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
    client.post("/api/settings/clear-gmail")
    r = client.post("/api/settings/email-accounts", json={
        "label": "mine", "address": "sekret@gmail.com", "app_password": "sekret-123",
    })
    assert r.status_code == 201
    aid = r.json()["id"]
    try:
        body = client.get("/api/settings").json()
        accts = [a for a in body["email_accounts"] if a["address"] == "sekret@gmail.com"]
        assert len(accts) == 1
        assert accts[0]["password_set"] is True
        assert "sekret-123" not in client.get("/api/settings").text
        # the page HTML must not contain it either
        page = client.get("/settings")
        assert page.status_code == 200
        assert "sekret-123" not in page.text
        assert "app password" in page.text.lower()
    finally:
        client.delete(f"/api/settings/email-accounts/{aid}")


def test_settings_put_does_not_touch_accounts():
    _enable()
    before = client.get("/api/settings").json()["email_accounts"]
    assert len(before) == 1
    r = client.put("/api/settings", json={"deal_scan_time": "08:30"})
    assert r.status_code == 200
    assert r.json()["email_accounts"] == before
    assert r.json()["deal_scan_time"] == "08:30"


def test_settings_time_validation():
    r = client.put("/api/settings", json={"deal_scan_time": "nope"})
    assert r.status_code == 400
    r = client.put("/api/settings", json={"deal_scan_time": "25:00"})
    assert r.status_code == 400


def test_clear_gmail_forgets_creds():
    _enable()
    r = client.post("/api/settings/clear-gmail")
    assert r.status_code == 200
    body = r.json()
    assert body["email_accounts"] == []
    assert body["configured"] is False


# ---------- scan: dedupe + auto-create ----------

def _enable():
    client.post("/api/settings/clear-gmail")
    r = client.post("/api/settings/email-accounts", json={
        "label": "mine", "address": "u@gmail.com", "app_password": "pw",
    })
    assert r.status_code == 201
    # National promos hit the live web — keep this file's scans hermetic.
    client.put("/api/settings", json={"deal_scan_enabled": True,
                                      "national_deals_enabled": False})


def _reset_deals():
    s = SessionLocal()
    try:
        s.query(Deal).delete()
        s.query(Restaurant).delete()
        s.commit()
    finally:
        s.close()


def _patch_run_scan(monkeypatch):
    """Route the background worker's run_scan through the fake mailbox."""
    import app.deal_scan as ds

    real = ds.run_scan
    monkeypatch.setattr(
        ds, "run_scan",
        lambda session, imap_class=None: real(session, imap_class=_mailbox(), today=TODAY),
    )


def _wait_status(want=("done", "error"), timeout=20):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        s = SessionLocal()
        try:
            from app.deal_scan import get_setting

            st = get_setting(s, K_STATUS, "idle")
        finally:
            s.close()
        if st in want:
            return st
        time.sleep(0.1)
    raise AssertionError(f"scan status never reached {want}")


def _deal_count():
    s = SessionLocal()
    try:
        return s.query(Deal).count()
    finally:
        s.close()


def test_scan_now_starts_background_scan(monkeypatch):
    _reset_deals()
    _enable()
    _patch_run_scan(monkeypatch)
    r = client.post("/api/deals/scan")
    assert r.status_code == 202
    assert r.json()["status"] == "started"
    assert _wait_status() == "done"

    s = SessionLocal()
    try:
        freddys = s.query(Restaurant).filter(Restaurant.name == "Freddy's").one()
        assert freddys.price_tier == 1
        assert freddys.include_in_picks is True
        assert "auto-added" in (freddys.notes or "")
        deals = s.query(Deal).all()
        assert len(deals) == 3
        assert all(d.source == "email" for d in deals)
        assert all(d.restaurant_id is not None for d in deals)
    finally:
        s.close()


def test_scan_twice_is_idempotent(monkeypatch):
    _patch_run_scan(monkeypatch)
    before = _deal_count()
    r = client.post("/api/deals/scan")
    assert r.status_code == 202
    assert _wait_status() == "done"
    assert _deal_count() == before  # same promos → no dupes
    s = SessionLocal()
    try:
        from app.deal_scan import get_setting

        assert get_setting(s, "deal_scan_last_result") == "no new deals (mine)"
    finally:
        s.close()


def test_scan_already_running_409():
    _enable()
    s = SessionLocal()
    try:
        set_setting(s, K_STATUS, "running")
        s.commit()
    finally:
        s.close()
    try:
        r = client.post("/api/deals/scan")
        assert r.status_code == 409
        assert "already running" in r.json()["detail"].lower()
    finally:
        s = SessionLocal()
        try:
            set_setting(s, K_STATUS, "idle")
            s.commit()
        finally:
            s.close()


def test_scan_without_creds_fails():
    client.post("/api/settings/clear-gmail")
    r = client.post("/api/deals/scan")
    assert r.status_code == 400
    assert "no email accounts" in r.json()["detail"].lower()


def test_scan_status_visible_in_settings(monkeypatch):
    _enable()
    _patch_run_scan(monkeypatch)
    r = client.post("/api/deals/scan")
    assert r.status_code == 202
    assert _wait_status() == "done"
    body = client.get("/api/settings").json()
    assert body["deal_scan_status"] == "done"
    assert body["deal_scan_last_result"]
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Scan running" not in page.text  # finished → shows last-scan line instead


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


# ---------- timeout + overlap ----------

def test_imap_timeout_passed():
    fetch_promos("u@gmail.com", "pw", imap_class=_mailbox(), today=TODAY)
    assert FakeIMAP.last_instance.kwargs.get("timeout") == 30


def test_stalled_connection_records_error_without_hanging():
    """A connection that stalls (socket.timeout on connect) must fail fast
    and record the error — never hang the scan thread."""
    import socket
    import time

    class StalledIMAP:
        def __init__(self, host, **kwargs):
            assert kwargs.get("timeout") == 30
            raise socket.timeout("timed out")  # what imaplib raises on a stalled connect

    _enable()
    s = SessionLocal()
    start = time.time()
    result = run_scan(s, imap_class=StalledIMAP)
    elapsed = time.time() - start
    s.close()
    assert elapsed < 10  # failed immediately, no 30s+ hang
    assert result["ok"] is False
    assert "timed out" in result["error"]
    s2 = SessionLocal()
    try:
        from app.deal_scan import get_setting

        assert get_setting(s2, K_STATUS) == "error"
        assert get_setting(s2, "deal_scan_last_result", "").startswith("error:")
        assert get_setting(s2, "deal_scan_last_run")
    finally:
        s2.close()


def test_try_start_scan_rejects_overlap():
    _enable()
    s = SessionLocal()
    try:
        set_setting(s, K_STATUS, "running")
        s.commit()
    finally:
        s.close()
    try:
        assert try_start_scan(SessionLocal) is False
    finally:
        s = SessionLocal()
        try:
            set_setting(s, K_STATUS, "idle")
            s.commit()
        finally:
            s.close()


def test_scheduler_job_skips_when_scan_running(monkeypatch):
    import app.deal_scan as ds

    _enable()
    calls = []
    monkeypatch.setattr(ds, "run_scan", lambda *a, **k: calls.append(1) or {"ok": True})
    s = SessionLocal()
    try:
        set_setting(s, K_STATUS, "running")
        s.commit()
    finally:
        s.close()
    try:
        deal_scheduler._job()
        assert calls == []  # manual scan in progress → scheduled run stands down
    finally:
        s = SessionLocal()
        try:
            set_setting(s, K_STATUS, "idle")
            s.commit()
        finally:
            s.close()


def test_reset_stale_running():
    s = SessionLocal()
    try:
        set_setting(s, K_STATUS, "running")
        s.commit()
    finally:
        s.close()
    reset_stale_running(SessionLocal)
    s = SessionLocal()
    try:
        from app.deal_scan import get_setting

        assert get_setting(s, K_STATUS) == "idle"
    finally:
        s.close()


def _raw_html(sender: str, subject: str, html: str, sent: str) -> bytes:
    """HTML-only promo (no text/plain part) — the common real-world case."""
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = sent
    msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


def test_message_body_falls_back_to_html():
    from app.deal_scan import _message_body

    raw = _raw_html(
        "Arby's <arbys@emails.arbys.com>",
        "Steak Bowl now, FREE Sandwich later",
        "<html><body><p>Valid thru 11/1/2026.</p>"
        "<p>Order the steak bowl today and get a <b>free sandwich</b> next visit.</p></body></html>",
        "Tue, 06 Oct 2026 09:00:00 -0500",
    )
    msg = email.message_from_bytes(raw)
    body = _message_body(msg)
    assert "Valid thru 11/1/2026" in body
    assert "free sandwich" in body
    assert "<p>" not in body  # tags stripped


def test_message_body_prefers_plain_text():
    from app.deal_scan import _message_body

    msg = email.message.EmailMessage()
    msg["From"] = "x@y.com"
    msg.set_content("plain version here")
    msg.add_alternative("<p>html version here</p>", subtype="html")
    assert _message_body(msg).strip() == "plain version here"


def test_html_only_promo_parses_to_deal():
    from app.deal_scan import _message_body
    from app.deal_parse import parse_promo

    raw = _raw_html(
        "Arby's <arbys@emails.arbys.com>",
        "Steak Bowl now, FREE Sandwich later",
        "<html><body><p>Valid thru 11/1/2026.</p>"
        "<p>Order the steak bowl today and get a <b>free sandwich</b> next visit.</p></body></html>",
        "Tue, 06 Oct 2026 09:00:00 -0500",
    )
    msg = email.message_from_bytes(raw)
    body = _message_body(msg)
    deal = parse_promo(
        "Arby's <arbys@emails.arbys.com>",
        "Steak Bowl now, FREE Sandwich later",
        body,
        date(2026, 10, 6),
        today=TODAY,
    )
    assert deal is not None
    assert deal["valid_until"] == "2026-11-01"
    assert deal["description"] is not None and "free sandwich" in deal["description"].lower()


# ---------- multiple email accounts ----------

def test_add_and_delete_email_account_api():
    client.post("/api/settings/clear-gmail")
    try:
        r = client.post("/api/settings/email-accounts", json={
            "address": "a@x.com", "app_password": "pw"})
        assert r.status_code == 201
        aid = r.json()["id"]
        assert r.json()["label"] == ""
        assert r.json()["password_set"] is True

        # duplicate address rejected
        r2 = client.post("/api/settings/email-accounts", json={
            "address": "a@x.com", "app_password": "pw"})
        assert r2.status_code == 409

        # missing password rejected
        r3 = client.post("/api/settings/email-accounts", json={"address": "b@x.com"})
        assert r3.status_code in (400, 422)

        body = client.get("/api/settings").json()
        assert any(a["address"] == "a@x.com" for a in body["email_accounts"])
        assert body["configured"] is True

        d = client.delete(f"/api/settings/email-accounts/{aid}")
        assert d.status_code == 204
        d2 = client.delete(f"/api/settings/email-accounts/{aid}")
        assert d2.status_code == 404
        assert client.get("/api/settings").json()["email_accounts"] == []
    finally:
        client.post("/api/settings/clear-gmail")


def test_migrate_legacy_gmail_settings():
    from app.deal_scan import get_setting as gs

    s = SessionLocal()
    try:
        s.query(EmailAccount).delete()
        set_setting(s, "gmail_address", "legacy@gmail.com")
        set_setting(s, "gmail_app_password", "legacy-pw")
        s.commit()
    finally:
        s.close()
    try:
        migrate_legacy_gmail_settings()
        migrate_legacy_gmail_settings()  # second run must be a no-op
        s = SessionLocal()
        try:
            assert gs(s, "gmail_address") is None
            assert gs(s, "gmail_app_password") is None
            accts = [a for a in list_accounts(s) if a.address == "legacy@gmail.com"]
            assert len(accts) == 1
            assert accts[0].label == "primary"
        finally:
            s.close()
    finally:
        s = SessionLocal()
        try:
            s.query(EmailAccount).delete()
            s.commit()
        finally:
            s.close()


def test_scan_two_accounts_bad_one_does_not_block():
    _reset_deals()
    client.post("/api/settings/clear-gmail")
    client.post("/api/settings/email-accounts", json={
        "label": "mine", "address": "u@gmail.com", "app_password": "pw"})
    client.post("/api/settings/email-accounts", json={
        "label": "family", "address": "f@gmail.com", "app_password": "bad-password"})
    try:
        logins = []

        class RecordingIMAP(FakeIMAP):
            def login(self, user, password):
                logins.append(user)
                return super().login(user, password)

        s = SessionLocal()
        try:
            res = run_scan(s, imap_class=RecordingIMAP, today=TODAY)
        finally:
            s.close()
        assert res["ok"] is False  # one account errored
        assert "family" in (res["error"] or "")
        assert res["deals_added"] >= 1  # the good account still delivered
        assert set(logins) == {"u@gmail.com", "f@gmail.com"}  # both attempted
        assert res["accounts_scanned"] == 2
        s = SessionLocal()
        try:
            from app.deal_scan import get_setting as gs

            last = gs(s, "deal_scan_last_result") or ""
            assert "(mine)" in last and "(family)" in last
        finally:
            s.close()
    finally:
        client.post("/api/settings/clear-gmail")


def test_scan_no_accounts_clean_state():
    client.post("/api/settings/clear-gmail")
    s = SessionLocal()
    try:
        import pytest

        with pytest.raises(RuntimeError, match="No email accounts"):
            run_scan(s)
        assert list_accounts(s) == []
    finally:
        s.close()


def test_receipt_scan_days_setting_round_trip():
    _enable()
    # default is a year
    assert client.get("/api/settings").json()["receipt_scan_days"] == 365
    # set a custom value
    r = client.put("/api/settings", json={"receipt_scan_days": 730})
    assert r.status_code == 200, r.text
    assert client.get("/api/settings").json()["receipt_scan_days"] == 730
    # validation
    assert client.put("/api/settings", json={"receipt_scan_days": 0}).status_code == 400
    assert client.put("/api/settings", json={"receipt_scan_days": 99999}).status_code == 400


def test_receipt_scan_days_clamps_garbage():
    from app.deal_scan import K_R_DAYS, receipt_scan_days, set_setting
    s = SessionLocal()
    try:
        set_setting(s, K_R_DAYS, "not-a-number")
        s.commit()
        assert receipt_scan_days(s) == 365
        set_setting(s, K_R_DAYS, "5")
        s.commit()
        assert receipt_scan_days(s) == 5
    finally:
        s.close()


def test_scan_one_account_uses_configured_lookback():
    """The receipt phase must search back receipt_scan_days, not 14."""
    from datetime import date
    from app import deal_scan as ds
    from app.deal_scan import K_R_DAYS, set_setting

    captured = {}

    def fake_fetch(address, password, imap_class=None, since=None, today=None):
        captured["since"] = since
        return []

    s = SessionLocal()
    try:
        set_setting(s, K_R_DAYS, "100")
        s.commit()
        orig = ds.fetch_receipts
        ds.fetch_receipts = fake_fetch
        try:
            ds._scan_one_account(s, "u@gmail.com", "pw", imap_class=None,
                                 today=date(2026, 10, 8))
        finally:
            ds.fetch_receipts = orig
        assert captured["since"] == date(2026, 10, 8) - timedelta(days=100)
    finally:
        s.close()
