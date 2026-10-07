"""Tests for deals: CRUD, active detection, picker boost math, badge rendering.

Run:  cd ~/workspace/foodfriday && PYTHONPATH=. .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ff-deals-"))
os.environ.setdefault("FOODFRIDAY_DATA_DIR", str(TMP / "data"))

from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.picker import (  # noqa: E402
    DEAL_BOOST,
    DEAL_ITEM_BOOST,
    candidate_stats,
    is_deal_active,
)

TODAY = date(2026, 10, 7)

init_db()
client = TestClient(app)


def _deal(**kw):
    base = {"title": "Test deal", "source": "manual"}
    base.update(kw)
    for k in ("valid_from", "valid_until"):
        if isinstance(base.get(k), str):
            base[k] = date.fromisoformat(base[k])
    if isinstance(base.get("created_at"), str):
        base["created_at"] = datetime.fromisoformat(base["created_at"])
    return base


# ---------- is_deal_active edge cases ----------

def test_active_within_range():
    assert is_deal_active(_deal(valid_from="2026-10-01", valid_until="2026-10-31"), TODAY)


def test_valid_until_today_is_active():
    assert is_deal_active(_deal(valid_from="2026-10-01", valid_until="2026-10-07"), TODAY)


def test_valid_until_yesterday_is_inactive():
    assert not is_deal_active(_deal(valid_from="2026-10-01", valid_until="2026-10-06"), TODAY)


def test_future_valid_from_is_inactive():
    assert not is_deal_active(_deal(valid_from="2026-10-08", valid_until="2026-10-31"), TODAY)


def test_open_ended_recent_is_active():
    assert is_deal_active(_deal(created_at="2026-09-28T00:00:00"), TODAY)


def test_open_ended_old_is_inactive():
    assert not is_deal_active(_deal(created_at="2026-08-28T00:00:00"), TODAY)


def test_no_dates_but_created_is_open_ended():
    # open-ended deal: active for 30 days from creation
    assert is_deal_active(_deal(created_at="2026-10-01T00:00:00"), TODAY)
    assert not is_deal_active(_deal(created_at="2026-08-01T00:00:00"), TODAY)


# ---------- picker boost math ----------

def _stats(deals_by_id=None, items_by_id=None):
    rs = [
        {"id": 1, "name": "Deal Place", "favorite": False},
        {"id": 2, "name": "Plain Place", "favorite": False},
    ]
    return candidate_stats(rs, {}, today=TODAY,
                           deals_by_id=deals_by_id, items_by_id=items_by_id)


def test_deal_boosts_weight():
    deal = _deal(id=1, valid_from="2026-10-01", valid_until="2026-10-31")
    s = _stats(deals_by_id={1: [deal]})
    w_deal = next(x for x in s if x["id"] == 1)["weight"]
    w_plain = next(x for x in s if x["id"] == 2)["weight"]
    assert w_deal == w_plain * DEAL_BOOST
    assert next(x for x in s if x["id"] == 1)["deal_titles"] == ["Test deal"]
    assert next(x for x in s if x["id"] == 2)["deal_titles"] == []


def test_item_match_adds_extra_multiplier():
    deal = _deal(id=1, valid_from="2026-10-01", valid_until="2026-10-31",
                 item_keywords="mozz sticks, corn dog")
    items = {1: ["Mozz Sticks", "Chips"]}
    s = _stats(deals_by_id={1: [deal]}, items_by_id=items)
    w_both = next(x for x in s if x["id"] == 1)["weight"]

    s2 = _stats(deals_by_id={1: [deal]}, items_by_id={1: ["Salad"]})
    w_deal_only = next(x for x in s2 if x["id"] == 1)["weight"]

    assert w_both == w_deal_only * DEAL_ITEM_BOOST


def test_expired_deal_gives_no_boost():
    deal = _deal(id=1, valid_from="2026-09-01", valid_until="2026-09-30")
    s = _stats(deals_by_id={1: [deal]})
    assert next(x for x in s if x["id"] == 1)["weight"] == next(x for x in s if x["id"] == 2)["weight"]
    assert next(x for x in s if x["id"] == 1)["deal_titles"] == []


# ---------- API: CRUD + pick badge ----------

def _restaurant(name):
    r = client.post("/api/restaurants", json={"name": name})
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_deal_crud_round_trip():
    r = _restaurant("Deal Diner")
    d = client.post("/api/deals", json={
        "restaurant_id": r["id"], "title": "$1 Tacos",
        "valid_from": "2026-10-07", "valid_until": "2026-10-14",
        "item_keywords": "taco", "source": "email",
    })
    assert d.status_code in (200, 201), d.text
    did = d.json()["id"]
    assert d.json()["active"] is True

    lst = client.get("/api/deals").json()
    assert any(x["id"] == did for x in lst)

    u = client.put(f"/api/deals/{did}", json={"title": "$2 Tacos"})
    assert u.json()["title"] == "$2 Tacos"

    assert client.delete(f"/api/deals/{did}").status_code in (200, 204)
    assert not any(x["id"] == did for x in client.get("/api/deals").json())


def test_deal_validation():
    assert client.post("/api/deals", json={"title": ""}).status_code == 400
    assert client.post("/api/deals", json={"title": "x", "restaurant_id": 999999}).status_code == 404


def test_pick_badge_shows_deal_title():
    r = _restaurant("Badge Bistro")
    client.post("/api/deals", json={
        "restaurant_id": r["id"], "title": "BOGO Burgers",
        "valid_from": "2026-10-01", "valid_until": "2026-10-31",
    })
    picks = client.post("/api/pick", json={}).json()["picks"]
    # with few restaurants, Badge Bistro should appear in 30 tries
    titles = []
    for _ in range(30):
        for p in client.post("/api/pick", json={}).json()["picks"]:
            if p["name"] == "Badge Bistro":
                titles.extend(p["deal_titles"])
    assert picks  # sanity
    assert "BOGO Burgers" in titles


def test_chain_wide_deal_does_not_boost():
    r = _restaurant("No Chain Boost")
    client.post("/api/deals", json={"restaurant_id": None, "title": "Chain-wide 20% off"})
    picks = client.post("/api/pick", json={}).json()["picks"]
    for p in picks:
        if p["name"] == "No Chain Boost":
            assert p["deal_titles"] == []
