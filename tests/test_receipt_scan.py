"""Tests for the automated in-container receipt scanner.

Per-chain total parsing, confirmation-without-total (NULL) visits, dedupe,
unknown-chain auto-create, the track_visits skip-list, item backfill, and
the Settings toggle — all against a fake imaplib server fixture (no live calls).

Run:  cd ~/workspace/foodfriday && PYTHONPATH=. .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import email.message
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ff-receipt-"))
os.environ.setdefault("FOODFRIDAY_DATA_DIR", str(TMP / "data"))

from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db, SessionLocal  # noqa: E402
from app.deal_scan import (  # noqa: E402
    K_R_ENABLED,
    K_R_LAST_RESULT,
    fetch_receipts,
    get_setting,
    list_accounts,
    receipt_scan_enabled,
    run_scan,
    set_setting,
    store_receipts,
)
from app.models import EmailAccount, Restaurant, Visit  # noqa: E402
from app.receipt_parse import detect_receipt, parse_receipt  # noqa: E402
from app.main import app  # noqa: E402

TODAY = date(2026, 10, 7)
RECENT = "Mon, 05 Oct 2026 12:00:00 -0500"

init_db()
client = TestClient(app)

CHIPOTLE_BODY = """| Chicken Bowl | |
| Chips | |
| Subtotal |
| $12.00 |
| Tax |
| $1.12 |
| Total |
| $13.12 |"""


def _raw(sender, subject, body, sent=RECENT, msgid=None):
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = sent
    if msgid:
        msg["Message-ID"] = msgid
    msg.set_content(body)
    return msg.as_bytes()


RECEIPT_MAIL = [
    _raw(
        "Chipotle <chipotle@chipotle.com>",
        "Thanks for your order, Joshua.",
        CHIPOTLE_BODY,
        msgid="<chip1@mail>",
    ),
    _raw(
        "Taco Bell <tacobell@tacobell.com>",
        "Success, we got your order.",
        "Order total\n$25.58\nThanks for choosing Taco Bell!",
        msgid="<tb1@mail>",
    ),
    _raw(
        "Sonic <no-reply@sonicdrivein.com>",
        "Your order is confirmed",
        "Thanks for ordering! Your food is on its way. No total in this one.",
        msgid="<sonic1@mail>",
    ),
    _raw(  # unknown chain -> auto-created restaurant
        "Grubhub <orders@grubhub.com>",
        "Your Grubhub receipt",
        "Order total: $42.10\nThanks!",
        msgid="<gh1@mail>",
    ),
    _raw(  # promo from a known sender — not a receipt, must be skipped
        "Chipotle <chipotle@chipotle.com>",
        "Free chips this week only",
        "Claim your free chips today only.",
        msgid="<chip-promo@mail>",
    ),
]


class FakeReceiptIMAP:
    """Minimal imaplib.IMAP4_SSL stand-in serving RECEIPT_MAIL."""

    last_instance = None

    def __init__(self, host, **kwargs):
        self.host = host
        self.kwargs = kwargs
        self.peek_used = False
        FakeReceiptIMAP.last_instance = self

    def login(self, user, password):
        if password == "bad-password":
            raise Exception("AUTHENTICATIONFAILED")

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return "OK", [b"5"]

    def search(self, charset, *criteria):
        frag = None
        crit = list(criteria)
        if "FROM" in crit:
            frag = str(crit[crit.index("FROM") + 1]).lower()
        hits = []
        for i, raw in enumerate(RECEIPT_MAIL, start=1):
            if frag and frag in raw.decode("utf-8", "replace").lower():
                hits.append(str(i).encode())
        return "OK", [b" ".join(hits)]

    def fetch(self, uid, spec):
        if "BODY.PEEK" in spec:
            self.peek_used = True
        i = int(uid) - 1
        return "OK", [(b"%d (BODY[] {%d}" % (int(uid), len(RECEIPT_MAIL[i])), RECEIPT_MAIL[i])]

    def close(self):
        return "OK", []

    def logout(self):
        return "OK", []


def _imap():
    return FakeReceiptIMAP


def _session_with_creds():
    s = SessionLocal()
    s.query(EmailAccount).delete()
    s.add(EmailAccount(label="mine", address="u@gmail.com", app_password="pw"))
    set_setting(s, K_R_ENABLED, "1")
    s.commit()
    return s


# ---------- pure parser tests ----------

def test_detect_receipt_known_chains():
    assert detect_receipt("Chipotle <chipotle@chipotle.com>", "Thanks for your order, Joshua.")["name"] == "Chipotle"
    assert detect_receipt("x@tacobell.com", "Success, we got your order.")["name"] == "Taco Bell"
    assert detect_receipt("x@dominos.com", "Your Domino's Order")["name"] == "Domino's"
    assert detect_receipt("x@caseys.com", "thanks for your order #1")["name"] == "Casey's"
    assert detect_receipt("x@sonicdrivein.com", "Your order is confirmed")["name"] == "Sonic"
    assert detect_receipt("x@order.online", "Your order from Spangles (order #a)")["name"] == "Spangles"
    assert detect_receipt("x@chilis.com", "Order confirmed, Josh")["name"] == "Chili's"
    assert detect_receipt("x@panerabread.com", "Your Rapid Pick-Up order is READY!")["name"] == "Panera"
    assert detect_receipt("x@buffalowildwings.com", "Buffalo Wild Wings Order Receipt")["name"] == "Buffalo Wild Wings"
    assert detect_receipt("x@whataburger.com", "Order confirmed")["name"] == "Whataburger"
    assert detect_receipt("x@littlecaesars.com", "Little Caesars Pizza Receipt")["name"] == "Little Caesars"
    assert detect_receipt("x@arbys.com", "Your order is confirmed")["name"] == "Arby's"
    assert detect_receipt("x@kfc.com", "We've received your KFC order")["name"] == "KFC"
    assert detect_receipt("x@wendys.com", "Your Wendy's Digital Order Receipt")["name"] == "Wendy's"
    assert detect_receipt("x@dairyqueen.com", "DQ Order Confirmation")["name"] == "Dairy Queen"
    assert detect_receipt("x@raisingcanes.com", "Raising Cane's Order Received")["name"] == "Raising Cane's"
    assert detect_receipt("x@pizzahut.com", "Thank you for your Pizza Hut order")["name"] == "Pizza Hut"
    assert detect_receipt("McDonald's <donotreply@mcdonalds.com>", "Your McDonald's receipt")["name"] == "McDonald's"
    assert detect_receipt("x@emails.mcdonalds.com", "Thanks for your order!")["name"] == "McDonald's"
    assert detect_receipt("Schlotzsky's <orders@schlotzskys.com>", "Your Schlotzsky's order confirmation")["name"] == "Schlotzsky's"
    assert detect_receipt("Church's Chicken <noreply@churchschicken.com>", "Your Church's order receipt")["name"] == "Church's Chicken"


def test_detect_receipt_rejects_promos_and_unrelated():
    assert detect_receipt("Sonic <noreply@email.sonicdrivein.com>", "Just for you–new offer drop") is None
    assert detect_receipt("Uber <noreply@uber.com>", "Your Tuesday trip receipt") is None  # ride, not Eats
    assert detect_receipt("Mom <mom@example.com>", "dinner receipt") is None


def test_parse_receipt_totals():
    def total(sender, subject, body):
        chain = detect_receipt(sender, subject)
        assert chain is not None, sender
        return parse_receipt(chain, body)["total"]

    assert total("c@chipotle.com", "Thanks for your order", "| Total |\n| $13.12 |") == 13.12
    assert total("t@tacobell.com", "Success, we got your order.", "Order total\n$25.58") == 25.58
    assert total("d@dominos.com", "Your Domino's Order", "| Total: $31.62 |") == 31.62
    assert total("c@caseys.com", "thanks for your order", "| Total |\n| $19.64 |") == 19.64
    assert total("s@sonicdrivein.com", "Your order is confirmed", "| Total |\n| $15.17 |") == 15.17
    assert total("s@order.online", "Your order from Spangles", "Total Charged Visa $35.18") == 35.18
    assert total("c@chilis.com", "Order confirmed", "**Total** **$39.34**") == 39.34
    assert total("p@panerabread.com", "Rapid Pick-Up", "Order Total $19.57") == 19.57
    assert total("b@buffalowildwings.com", "Order Receipt", "| Total: | $101.08 | |") == 101.08
    assert total("w@whataburger.com", "Order confirmed", "Burger $14.99 Total $30.21") == 30.21
    assert total("l@littlecaesars.com", "Pizza Receipt", "| Order Total | $30.60 |") == 30.60
    assert total("a@arbys.com", "confirmed", "| ORDER TOTAL: | | $22.25 |") == 22.25
    assert total("k@kfc.com", "We've received your KFC order", "| Total | $28.20 |") == 28.20
    assert total("w@wendys.com", "Digital Order Receipt", "| Total | $31.13 |") == 31.13
    assert total("d@dairyqueen.com", "Order Confirmation", "| TOTAL | $17.14 |") == 17.14
    assert total("r@raisingcanes.com", "Order Received", "| TOTAL | $26.83 |") == 26.83
    assert total("p@pizzahut.com", "Thank you for your Pizza Hut order", "Order Total: $65.33") == 65.33
    assert total("m@mcdonalds.com", "Your McDonald's receipt", "Subtotal $10.99\nTotal: $11.83") == 11.83
    assert total("m@mcdonalds.com", "Thanks for your order!", "Total $8.49") == 8.49
    assert total("s@schlotzskys.com", "Your Schlotzsky's order confirmation", "Subtotal $24.50\nTotal $26.10") == 26.10
    assert total("c@churchschicken.com", "Your Church's order receipt", "Total: $18.75") == 18.75


def test_parse_receipt_no_total_gives_none():
    chain = detect_receipt("s@sonicdrivein.com", "Your order is confirmed")
    assert parse_receipt(chain, "Thanks for ordering, no total here.")["total"] is None


def test_parse_receipt_items_chipotle():
    chain = detect_receipt("c@chipotle.com", "Thanks for your order")
    items = parse_receipt(chain, CHIPOTLE_BODY)["items"]
    assert "Chicken Bowl" in items and "Chips" in items


# ---------- IMAP fetch ----------

def test_fetch_receipts_parses_and_skips_promos():
    receipts = fetch_receipts("u@gmail.com", "pw", imap_class=_imap(), today=TODAY)
    by_chain = {r["chain"]: r for r in receipts}
    assert by_chain["Chipotle"]["total"] == 13.12
    assert by_chain["Chipotle"]["msgid"] == "chip1@mail"
    assert by_chain["Chipotle"]["sent"] == date(2026, 10, 5)
    assert "Chicken Bowl" in by_chain["Chipotle"]["items"]
    assert by_chain["Taco Bell"]["total"] == 25.58
    assert by_chain["Sonic"]["total"] is None  # confirmation-only → NULL visit
    assert by_chain["Grubhub"]["total"] == 42.10
    assert len(receipts) == 4  # the Chipotle promo is not a receipt
    assert FakeReceiptIMAP.last_instance.peek_used


# ---------- store + run_scan ----------

def _visits_for(name):
    s = SessionLocal()
    try:
        r = s.query(Restaurant).filter(Restaurant.name == name).first()
        return r.id if r else None, (
            s.query(Visit).filter(Visit.restaurant_id == r.id).all() if r else []
        )
    finally:
        s.close()


def test_run_scan_creates_visits_with_items():
    s = _session_with_creds()
    before = s.query(Visit).count()
    try:
        result = run_scan(s, imap_class=_imap(), today=TODAY)
    finally:
        s.close()
    assert result["ok"] is True
    assert result["visits_added"] == 4, result
    assert "4 new visits" in result["receipt_summary"]
    assert result["receipt_restaurants_added"] >= 1  # Grubhub auto-created
    s = SessionLocal()
    try:
        assert s.query(Visit).count() == before + 4
        chip = s.query(Restaurant).filter(Restaurant.name == "Chipotle").one()
        v = s.query(Visit).filter(Visit.external_id == "gmail:chip1@mail").one()
        assert v.restaurant_id == chip.id
        assert v.total == 13.12
        assert v.visited_at == date(2026, 10, 5)
        assert v.source == "email"
        assert "Chicken Bowl" in (v.items or "")
        sonic_v = s.query(Visit).filter(Visit.external_id == "gmail:sonic1@mail").one()
        assert sonic_v.total is None  # confirmation-only still logged
        gh = s.query(Restaurant).filter(Restaurant.name == "Grubhub").one()
        assert gh.include_in_picks is True
        # settings result lines recorded
        assert get_setting(s, K_R_LAST_RESULT) is not None
    finally:
        s.close()


def test_run_scan_dedupe_second_run_is_noop():
    s = _session_with_creds()
    try:
        first = run_scan(s, imap_class=_imap(), today=TODAY)
        second = run_scan(s, imap_class=_imap(), today=TODAY)
    finally:
        s.close()
    assert first["visits_added"] >= 0
    assert second["visits_added"] == 0
    assert second["receipt_summary"] == "no new receipts (mine)"


def test_track_visits_false_skips_restaurant():
    s = _session_with_creds()
    # hermetic regardless of test order: drop any earlier tb1 visit first
    s.query(Visit).filter(Visit.external_id == "gmail:tb1@mail").delete()
    s.commit()
    tb = s.query(Restaurant).filter(Restaurant.name == "Taco Bell").first()
    if tb is None:
        tb = Restaurant(name="Taco Bell", track_visits=True)
        s.add(tb)
        s.commit()
    tb_id = tb.id
    tb.track_visits = False
    s.commit()
    before = s.query(Visit).filter(Visit.restaurant_id == tb_id).count()
    try:
        result = run_scan(s, imap_class=_imap(), today=TODAY)
    finally:
        # restore for other tests sharing this DB
        tb.track_visits = True
        s.commit()
        s.close()
    assert result["ok"] is True
    s = SessionLocal()
    try:
        assert s.query(Visit).filter(Visit.restaurant_id == tb_id).count() == before
        assert s.query(Visit).filter(Visit.external_id == "gmail:tb1@mail").count() == 0
    finally:
        s.close()


def test_receipt_scan_disabled_skips_phase():
    s = _session_with_creds()
    set_setting(s, K_R_ENABLED, "0")
    s.commit()
    try:
        result = run_scan(s, imap_class=_imap(), today=TODAY)
    finally:
        set_setting(s, K_R_ENABLED, "1")
        s.commit()
        s.close()
    assert result["ok"] is True
    assert result["visits_added"] == 0
    assert result["receipt_summary"] == "receipt scanning disabled (mine)"


def test_receipt_scan_enabled_flag():
    s = SessionLocal()
    try:
        s.query(EmailAccount).delete()
        s.add(EmailAccount(label="mine", address="u@gmail.com", app_password="pw"))
        set_setting(s, K_R_ENABLED, "1")
        s.commit()
        assert receipt_scan_enabled(s) is True
        set_setting(s, K_R_ENABLED, "0")
        s.commit()
        assert receipt_scan_enabled(s) is False
    finally:
        set_setting(s, K_R_ENABLED, "1")
        s.commit()
        s.close()


# ---------- API ----------

def test_toggle_track_visits_api():
    s = SessionLocal()
    r = Restaurant(name="ZZ Track Test")
    s.add(r)
    s.commit()
    rid = r.id
    s.close()
    try:
        r1 = client.post(f"/api/restaurants/{rid}/track-visits")
        assert r1.status_code == 200
        assert r1.json()["track_visits"] is False
        r2 = client.post(f"/api/restaurants/{rid}/track-visits")
        assert r2.json()["track_visits"] is True
        # reflected in the restaurant list
        lst = client.get("/api/restaurants").json()
        row = next(x for x in lst if x["id"] == rid)
        assert row["track_visits"] is True
        assert client.post("/api/restaurants/999999/track-visits").status_code == 404
    finally:
        s = SessionLocal()
        try:
            s.delete(s.get(Restaurant, rid))
            s.commit()
        finally:
            s.close()


def test_settings_receipt_fields_roundtrip():
    r = client.put("/api/settings", json={"receipt_scan_enabled": False})
    assert r.status_code == 200
    body = r.json()
    assert body["receipt_scan_enabled"] is False
    assert "receipt_scan_last_result" in body
    r = client.put("/api/settings", json={"receipt_scan_enabled": True})
    assert r.json()["receipt_scan_enabled"] is True


def test_store_receipts_skips_seed_imported_pair():
    """A receipt whose (restaurant, date) was already logged via seed import
    (non-gmail external_id) must not be duplicated by the auto-scan."""
    from datetime import date as _date

    s = _session_with_creds()
    try:
        r = Restaurant(name="SeedDup Test", cuisine="Mexican", price_tier=2,
                       favorite=False, include_in_picks=True, track_visits=True)
        s.add(r)
        s.flush()
        s.add(Visit(restaurant_id=r.id, visited_at=_date(2026, 10, 5), total=13.12,
                    source="import", external_id="chipotle:2026-10-05:0"))
        s.commit()
        receipts = [{
            "chain": "SeedDup Test",
            "msgid": "seeddup1@mail",
            "sent": _date(2026, 10, 5),
            "total": 13.12,
            "items": [],
        }]
        added, _ = store_receipts(s, receipts)
        s.commit()
        assert added == 0
        assert s.query(Visit).filter(Visit.restaurant_id == r.id).count() == 1
    finally:
        s.close()
