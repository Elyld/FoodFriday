"""Tests for Yelp-powered nearby discovery.

Run:  cd ~/workspace/foodfriday && PYTHONPATH=. .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ff-discover-"))
os.environ.setdefault("FOODFRIDAY_DATA_DIR", str(TMP / "data"))

from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.discover import (  # noqa: E402
    K_HOME_LAT,
    K_HOME_LON,
    K_YELP_KEY,
    discover,
    get_cached,
    price_str_to_tier,
    search_yelp,
    store_cache,
)
from app.main import app  # noqa: E402
from app.models import DiscoverCache  # noqa: E402

init_db()
client = TestClient(app)

FAKE_YELP = {
    "businesses": [
        {
            "id": "abc123",
            "name": "The Good Fork",
            "rating": 4.5,
            "price": "$$",
            "categories": [{"title": "American"}, {"title": "Burgers"}],
            "location": {"display_address": ["123 Main St", "Topeka, KS"]},
            "distance": 1609.344,
        },
        {
            "id": "def456",
            "name": "Taco Town",
            "rating": 3.5,
            "price": "$",
            "categories": [{"title": "Mexican"}],
            "location": {"display_address": ["9 Elm St"]},
            "distance": 3218.688,
        },
    ]
}


def fake_http_get(url, api_key):
    assert api_key == "test-key"
    assert "latitude=39.0" in url
    return FAKE_YELP


def _settings(**kw):
    r = client.put("/api/settings", json=kw)
    assert r.status_code == 200, r.text
    return r.json()


def test_search_yelp_parses():
    cards = search_yelp(39.0, -95.6, 10, "test-key", http_get=fake_http_get)
    assert len(cards) == 2
    first = cards[0]
    assert first["name"] == "The Good Fork"
    assert first["rating"] == 4.5
    assert first["price_tier"] == 2
    assert first["price_label"] == "$$"
    assert first["cuisine"] == "American, Burgers"
    assert first["address"] == "123 Main St, Topeka, KS"
    assert first["distance_mi"] == 1.0


def test_price_str_to_tier():
    assert price_str_to_tier("$") == 1
    assert price_str_to_tier("$$") == 2
    assert price_str_to_tier("$$$$") == 3
    assert price_str_to_tier(None) == 2


def test_discover_hides_already_added():
    client.post("/api/restaurants", json={"name": "taco town!", "cuisine": "Mexican"})
    _settings(yelp_api_key="test-key", home_lat="39.0", home_lon="-95.6")
    # prime the cache through discover() with a fake HTTP client, so the
    # endpoint below serves from cache (no live Yelp call in tests)
    s = SessionLocal()
    try:
        out = discover(s, 39.0, -95.6, 10, "test-key", refresh=True, http_get=fake_http_get)
        assert out["cached"] is False
    finally:
        s.close()
    r = client.get("/api/discover?radius_km=10")
    assert r.status_code == 200, r.text
    data = r.json()
    names = [b["name"] for b in data["businesses"]]
    assert "Taco Town" not in names  # fuzzy match hid it
    assert "The Good Fork" in names
    assert data["cached"] is True


def test_discover_cache_hit_and_refresh():
    calls = {"n": 0}

    def counting(url, api_key):
        calls["n"] += 1
        return FAKE_YELP

    s = SessionLocal()
    try:
        # prime cache directly
        store_cache(s, 39.0, -95.6, 10, [{"name": "Cached Spot"}])
        biz, _ = get_cached(s, 39.0, -95.6, 10)
        assert biz == [{"name": "Cached Spot"}]

        # discover() uses cache, no HTTP
        out = discover(s, 39.0, -95.6, 10, "k", http_get=counting)
        assert out["cached"] is True
        assert calls["n"] == 0

        # refresh forces a live call
        out = discover(s, 39.0, -95.6, 10, "k", refresh=True, http_get=counting)
        assert out["cached"] is False
        assert calls["n"] == 1
    finally:
        s.close()


def test_discover_cache_expires_after_24h():
    calls = {"n": 0}

    def counting(url, api_key):
        calls["n"] += 1
        return FAKE_YELP

    s = SessionLocal()
    try:
        store_cache(s, 39.0, -95.6, 5, [{"name": "Old Spot"}])
        row = (
            s.query(DiscoverCache)
            .filter(
                DiscoverCache.lat == 39.0,
                DiscoverCache.lon == -95.6,
                DiscoverCache.radius_km == 5,
            )
            .first()
        )
        assert row is not None
        row.created_at = datetime.utcnow() - timedelta(hours=25)
        s.commit()
        out = discover(s, 39.0, -95.6, 5, "k", http_get=counting)
        assert out["cached"] is False
        assert calls["n"] == 1  # stale cache -> live call
    finally:
        s.close()


def test_discover_requires_key_and_location():
    _settings(yelp_api_key=None, home_lat=None, home_lon=None)
    # clear via direct settings (None clears)
    from app.deal_scan import set_setting

    s = SessionLocal()
    try:
        set_setting(s, K_YELP_KEY, None)
        set_setting(s, K_HOME_LAT, None)
        set_setting(s, K_HOME_LON, None)
        s.commit()
    finally:
        s.close()
    r = client.get("/api/discover")
    assert r.status_code == 400
    assert "Yelp" in r.json()["detail"]


def test_discover_add_creates_restaurant():
    _settings(yelp_api_key="test-key", home_lat="39.0", home_lon="-95.6")
    payload = {
        "yelp_id": "abc123",
        "name": "The Good Fork",
        "cuisine": "American, Burgers",
        "price_tier": 2,
        "address": "123 Main St, Topeka, KS",
        "rating": 4.5,
    }
    r = client.post("/api/discover/add", json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["already"] is False

    # idempotent — same name (fuzzy) returns the existing one
    r2 = client.post("/api/discover/add", json={**payload, "name": "the good fork"})
    assert r2.status_code == 201
    assert r2.json()["already"] is True
    assert r2.json()["id"] == r.json()["id"]

    s = SessionLocal()
    try:
        from app.models import Restaurant

        row = s.get(Restaurant, r.json()["id"])
        assert row.yelp_id == "abc123"
        assert row.yelp_rating == 4.5
        assert row.include_in_picks is not False
    finally:
        s.close()


def test_yelp_key_masked_in_settings():
    _settings(yelp_api_key="super-secret-key")
    s = client.get("/api/settings").json()
    assert s["yelp_api_key_set"] is True
    assert s["yelp_api_key"] == "********"
    assert "super-secret-key" not in str(s)
