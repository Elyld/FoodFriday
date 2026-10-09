"""Tests for the national promo watcher (Reddit RSS).

Parsing logic is tested directly; the RSS fetch is tested against a fake
urlopen serving in-memory Atom XML; store_deals integration confirms the
national source survives the write path.

Run:  cd ~/workspace/foodfriday && PYTHONPATH=. .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ff-national-"))
os.environ.setdefault("FOODFRIDAY_DATA_DIR", str(TMP / "data"))

from app.database import init_db, SessionLocal  # noqa: E402
from app.deal_scan import store_deals  # noqa: E402
from app.models import Deal, Restaurant  # noqa: E402
from app.national_deals import (  # noqa: E402
    clean_title,
    extract_deal,
    fetch_national_deals,
    looks_like_deal,
    match_chain,
    parse_valid_until,
)

TODAY = date(2026, 10, 9)

init_db()


# ---------- looks_like_deal ----------

def test_looks_like_deal_positives():
    assert looks_like_deal("Domino's 50% off all menu-priced pizzas (Oct 5-11)")
    assert looks_like_deal("[Deal] Free McChicken with $1 purchase")
    assert looks_like_deal("Wendy's: BOGO Dave's Single this weekend")
    assert looks_like_deal("Taco Bell $5 off $20+")
    assert looks_like_deal("Papa Johns half price pizzas thru Sunday")


def test_looks_like_deal_negatives():
    assert not looks_like_deal("Crumbl Cookies closes dozens of stores amid declining sales")
    assert not looks_like_deal("Why does McDonald's still use tomato sauce sachets?")
    assert not looks_like_deal("CurderBurger vs BK Pretzel Whopper")
    assert not looks_like_deal("New Menu Item Charleys")


# ---------- match_chain ----------

def test_match_chain_builtin():
    assert match_chain("Domino's 50% off all menu-priced pizzas") == "Domino's"
    assert match_chain("FREE Big Mac with purchase at Mcdonalds") == "McDonald's"
    assert match_chain("Church's Chicken: 8pc deal") == "Church's Chicken"


def test_match_chain_own_names_win():
    # His spelling/casing is preserved.
    assert match_chain("schlotzsky's free chips deal", own_names=["Schlotzsky's"]) == "Schlotzsky's"


def test_match_chain_unknown():
    assert match_chain("50% off at Bob's Burger Barn") is None


def test_match_chain_curly_quotes():
    # Reddit titles use U+2019 — must still match.
    assert match_chain("Domino\u2019s 50% Off All Menu-Priced Pizzas") == "Domino's"
    assert match_chain("Dave\u2019s Hot Chicken: Free Slider 10/13") == "Dave's Hot Chicken"


# ---------- parse_valid_until ----------

def test_parse_valid_until_oct_range():
    assert parse_valid_until("Domino's 50% off (Oct 5-11)", TODAY) == date(2026, 10, 11)


def test_parse_valid_until_slash():
    assert parse_valid_until("Deal ends 10/11", TODAY) == date(2026, 10, 11)


def test_parse_valid_until_weekday():
    # TODAY is a Friday; "through Sunday" -> Oct 11.
    assert parse_valid_until("half price thru Sunday", TODAY) == date(2026, 10, 11)


def test_parse_valid_until_default():
    assert parse_valid_until("Free fries Friday", TODAY) == date(2026, 10, 23)


# ---------- extract_deal ----------

def test_extract_deal_dominos():
    deal = extract_deal("Domino's 50% off all menu-priced pizzas (Oct 5-11)", TODAY)
    assert deal is not None
    assert deal["restaurant"] == "Domino's"
    assert deal["valid_until"] == "2026-10-11"
    assert deal["source"] == "national"


def test_extract_deal_rejects_non_deal():
    assert extract_deal("Crumbl Cookies closes dozens of stores", TODAY) is None
    assert extract_deal("50% off at Bob's Burger Barn", TODAY) is None


def test_extract_deal_cleans_brackets():
    deal = extract_deal("[Deal] Wendy's: BOGO Dave's Single", TODAY)
    assert deal is not None
    assert deal["title"] == "Wendy's: BOGO Dave's Single"


# ---------- fetch_national_deals with fake RSS ----------

_FAKE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<updated>2026-10-09T16:52:15+00:00</updated>
<entry>
  <title>Domino's 50% off all menu-priced pizzas (Oct 5-11)</title>
  <link href="https://www.reddit.com/r/fastfood/comments/abc/deal/"/>
  <updated>2026-10-05T12:00:00+00:00</updated>
</entry>
<entry>
  <title>Crumbl Cookies closes dozens of stores amid declining sales</title>
  <link href="https://www.reddit.com/r/fastfood/comments/def/news/"/>
  <updated>2026-10-09T10:00:00+00:00</updated>
</entry>
<entry>
  <title>[US] Taco Bell $5 off $20+</title>
  <link href="https://www.reddit.com/r/fastfood/comments/ghi/deal2/"/>
  <updated>2026-10-09T11:00:00+00:00</updated>
</entry>
<entry>
  <title>Domino's 50% off all menu-priced pizzas (Oct 5-11)</title>
  <link href="https://www.reddit.com/r/fastfood/comments/abc/deal/"/>
  <updated>2026-10-05T12:00:00+00:00</updated>
</entry>
</feed>"""


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=None):
    assert "reddit.com" in req.full_url
    return _FakeResp(_FAKE_ATOM.encode())


def test_fetch_national_deals_filters_and_dedupes():
    deals = fetch_national_deals(urlopen=_fake_urlopen)
    titles = [d["title"] for d in deals]
    # Duplicate Domino's post appears once; news post skipped.
    assert titles.count("Domino's 50% off all menu-priced pizzas (Oct 5-11)") == 1
    assert any("Taco Bell" in d["restaurant"] for d in deals)
    assert not any("Crumbl" in t for t in titles)
    assert all(d["source"] == "national" for d in deals)


# ---------- run_scan integration ----------

def test_run_scan_national_phase(monkeypatch):
    """run_scan picks up national deals once per scan (not per account)."""
    import app.deal_scan as ds
    from app.models import EmailAccount

    class EmptyIMAP:
        def __init__(self, *a, **k):
            pass

        def login(self, *a):
            return ("OK", [b""])

        def select(self, *a, **k):
            return ("OK", [b"0"])

        def search(self, *a):
            return ("OK", [b""])

        def logout(self):
            return ("OK", [b""])

    fake_deal = {
        "restaurant": "Domino's",
        "title": "50% off all menu-priced pizzas",
        "description": None,
        "valid_from": "2026-10-05",
        "valid_until": "2026-10-11",
        "item_keywords": "",
        "source": "national",
    }
    monkeypatch.setattr(ds, "fetch_national_deals", lambda own_names: [fake_deal])

    # Test files share one DB (first-imported module's DATA_DIR wins), so every
    # setting this test touches is restored afterwards — a leaked K_R_ENABLED
    # or K_N_DEALS would silently change test_deal_scan's behavior.
    s = SessionLocal()
    saved: dict[str, tuple[bool, str | None]] = {}
    for key in (ds.K_ENABLED, ds.K_N_DEALS, ds.K_R_ENABLED):
        row = s.get(ds.Setting, key)
        saved[key] = (row is not None, row.value if row else None)
    s.add(EmailAccount(label="t", address="t@gmail.com", app_password="x"))
    ds.set_setting(s, ds.K_ENABLED, "1")
    ds.set_setting(s, ds.K_N_DEALS, "1")
    ds.set_setting(s, ds.K_R_ENABLED, "0")  # receipts off — not under test
    s.commit()
    try:
        res = ds.run_scan(s, imap_class=EmptyIMAP, today=TODAY)
        assert res["deals_added"] == 1
        assert "1 new national promo (web)" in res["summary"]
        deal = (
            s.query(Deal)
            .join(Restaurant, Deal.restaurant_id == Restaurant.id)
            .filter(Restaurant.name == "Domino's")
            .one()
        )
        assert deal.source == "national"
        # second run dedupes — no duplicate deal
        res2 = ds.run_scan(s, imap_class=EmptyIMAP, today=TODAY)
        assert res2["deals_added"] == 0
        assert "no new national promos (web)" in res2["summary"]
    finally:
        # Commit and close before _cleanup_restaurant — it opens its own
        # session, and SQLite won't let two connections write at once.
        s.query(EmailAccount).filter(EmailAccount.address == "t@gmail.com").delete()
        for key, (existed, old_value) in saved.items():
            if existed:
                ds.set_setting(s, key, old_value)
            else:
                s.query(ds.Setting).filter(ds.Setting.key == key).delete()
        s.commit()
        s.close()
        _cleanup_restaurant("Domino's")


# ---------- store_deals keeps the national source ----------

def _cleanup_restaurant(name: str):
    session = SessionLocal()
    try:
        r = session.query(Restaurant).filter(Restaurant.name == name).first()
        if r:
            # Restaurant.deals has cascade="all, delete-orphan" — a plain
            # ORM delete takes the deals with it. (A bulk Query.delete()
            # first leaves the selectin-loaded Deal objects orphaned in the
            # identity map and SQLAlchemy warns on the redundant flush.)
            session.delete(r)
            session.commit()
    finally:
        session.close()


def test_store_deals_national_source():
    session = SessionLocal()
    try:
        added, _ = store_deals(session, [
            {
                "restaurant": "Domino's",
                "title": "50% off all menu-priced pizzas",
                "description": None,
                "valid_from": "2026-10-05",
                "valid_until": "2026-10-11",
                "item_keywords": "",
                "source": "national",
            }
        ])
        session.commit()
        assert added == 1
        r = session.query(Restaurant).filter(Restaurant.name == "Domino's").one()
        d = session.query(Deal).filter(Deal.restaurant_id == r.id).one()
        assert d.source == "national"
    finally:
        session.close()
        _cleanup_restaurant("Domino's")
