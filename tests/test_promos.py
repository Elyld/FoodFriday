"""Tests for the Promos page (/promos) and deal source links.

Covers: source_url threading (email Gmail link, Reddit post link),
deal_dict exposure, page rendering with source badges, and the
active/expired filter.

Run:  cd ~/workspace/foodfriday && PYTHONPATH=. .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ff-promos-"))
os.environ.setdefault("FOODFRIDAY_DATA_DIR", str(TMP / "data"))

from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db, SessionLocal  # noqa: E402
from app.deal_parse import gmail_search_url, parse_promo  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Deal, Restaurant  # noqa: E402

TODAY = date.today()

init_db()
client = TestClient(app)


def _make_deal(session, restaurant_name, title, source, source_url, days_left):
    r = session.query(Restaurant).filter(Restaurant.name == restaurant_name).first()
    if r is None:
        r = Restaurant(name=restaurant_name, price_tier=1)
        session.add(r)
        session.flush()
    valid_until = TODAY + timedelta(days=days_left) if days_left is not None else None
    d = Deal(
        restaurant_id=r.id,
        title=title,
        valid_from=TODAY - timedelta(days=1),
        valid_until=valid_until,
        source=source,
        source_url=source_url,
    )
    session.add(d)
    session.flush()
    return d.id, r.name


def _cleanup(*names):
    session = SessionLocal()
    try:
        for name in names:
            r = session.query(Restaurant).filter(Restaurant.name == name).first()
            if r:
                session.delete(r)  # cascade takes the deals
        session.commit()
    finally:
        session.close()


# ---------- gmail_search_url ----------

def test_gmail_search_url():
    url = gmail_search_url("abc123@mail.example.com")
    assert url.startswith("https://mail.google.com/mail/u/0/#search/rfc822msgid%3A")
    assert "abc123" in url
    assert gmail_search_url(None) is None
    assert gmail_search_url("  ") is None


def test_gmail_search_url_encodes_specials():
    url = gmail_search_url("<a+b@c.com>")
    assert "<" not in url and ">" not in url
    assert "a%2Bb" in url


# ---------- parse_promo threads message_id ----------

def test_parse_promo_message_id_becomes_source_url():
    d = parse_promo(
        "Domino's <noreply@dominos.com>",
        "50% off all menu-priced pizzas this week",
        "Get 50% off all menu-priced pizzas when you order online.",
        TODAY,
        message_id="xyz@mail.dominos.com",
    )
    assert d is not None
    assert d["source"] == "email"
    assert d["source_url"].startswith("https://mail.google.com/mail/u/0/#search/")
    assert "xyz" in d["source_url"]


def test_parse_promo_no_message_id_no_url():
    d = parse_promo(
        "Domino's <noreply@dominos.com>",
        "50% off all menu-priced pizzas this week",
        "Get 50% off all menu-priced pizzas when you order online.",
        TODAY,
    )
    assert d is not None
    assert d["source_url"] is None


# ---------- promos page ----------

def test_promos_page_active_only_by_default():
    s = SessionLocal()
    try:
        _make_deal(s, "Domino's", "50% off pizzas", "email",
                   "https://mail.google.com/mail/u/0/#search/x", 3)
        _make_deal(s, "Dave's Hot Chicken", "Free slider", "national",
                   "https://www.reddit.com/r/fastfood/comments/abc/", 4)
        _make_deal(s, "Old Chain", "Dead deal", "manual", None, -5)
        s.commit()
    finally:
        s.close()
    try:
        html = client.get("/promos").text
        assert "50% off pizzas" in html
        assert "Free slider" in html
        assert "Dead deal" not in html  # expired hidden by default
        # source badges + links
        assert "📧 email" in html
        assert "🌐 web" in html
        assert "open in Gmail ↗" in html
        assert "view post ↗" in html
        assert "https://www.reddit.com/r/fastfood/comments/abc/" in html
        # expired toggle offered
        assert "/promos?all=1" in html

        html_all = client.get("/promos?all=1").text
        assert "Dead deal" in html_all
        assert "✏️ manual" in html_all
    finally:
        _cleanup("Domino's", "Dave's Hot Chicken", "Old Chain")


def test_promos_page_empty():
    html = client.get("/promos").text
    assert "Promos" in html


def test_api_deals_exposes_source_url():
    s = SessionLocal()
    try:
        _make_deal(s, "Taco Bell", "$5 off $20", "national",
                   "https://www.reddit.com/r/fastfood/comments/xyz/", 7)
        s.commit()
    finally:
        s.close()
    try:
        deals = client.get("/api/deals").json()
        match = [d for d in deals if d["title"] == "$5 off $20"]
        assert len(match) == 1
        assert match[0]["source"] == "national"
        assert match[0]["source_url"] == "https://www.reddit.com/r/fastfood/comments/xyz/"
    finally:
        _cleanup("Taco Bell")


def test_nav_has_promos_tab():
    html = client.get("/").text
    assert 'href="/promos"' in html
