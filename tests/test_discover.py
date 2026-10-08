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
    haversine_mi,
    humanize_cuisine,
    price_str_to_tier,
    search_overpass,
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


def test_discover_requires_location_not_key():
    # Yelp key is optional now — but home location is still required
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
    assert "Home location" in r.json()["detail"]


def test_discover_without_key_uses_osm(monkeypatch):
    # no Yelp key -> provider falls back to Overpass, no 400
    import app.discover as disc
    from app.deal_scan import set_setting

    s = SessionLocal()
    try:
        set_setting(s, K_YELP_KEY, None)  # hermetic: key must be absent
        s.commit()
    finally:
        s.close()
    _settings(home_lat="39.05", home_lon="-95.68")
    monkeypatch.setattr(disc, "search_overpass",
                        lambda lat, lon, r, http_post=None: [{"name": "OSM Diner"}])
    r = client.get("/api/discover")
    assert r.status_code == 200
    assert r.json()["provider"] == "osm"


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


# ---------- OpenStreetMap / Overpass provider ----------

def _fake_overpass_post_factory(elements):
    def _post(url, data):
        assert "overpass" in url
        return {"elements": elements}
    return _post


OSM_NODE = {
    "type": "node", "id": 1,
    "lat": 39.05, "lon": -95.68,
    "tags": {"amenity": "restaurant", "name": "Test Bistro",
             "cuisine": "italian;pizza",
             "addr:housenumber": "123", "addr:street": "Main St"},
}
OSM_WAY = {
    "type": "way", "id": 2,
    "center": {"lat": 39.06, "lon": -95.67},
    "tags": {"amenity": "fast_food", "name": "Burger Joint", "cuisine": "burger"},
}
OSM_NONAME = {
    "type": "node", "id": 3, "lat": 39.05, "lon": -95.68,
    "tags": {"amenity": "restaurant", "cuisine": "mexican"},
}


def test_humanize_cuisine():
    assert humanize_cuisine("pizza") == "Pizza"
    assert humanize_cuisine("italian;pizza") == "Italian, Pizza"
    assert humanize_cuisine("ice_cream") == "Dessert"
    assert humanize_cuisine("some_new_thing") == "Some New Thing"
    assert humanize_cuisine("") == ""
    assert humanize_cuisine(None) == ""


def test_haversine_mi_sanity():
    # ~1 degree of latitude ≈ 69 miles
    d = haversine_mi(39.0, -95.0, 40.0, -95.0)
    assert 68 < d < 70
    assert haversine_mi(39.0, -95.0, 39.0, -95.0) == 0


def test_search_overpass_maps_nodes_and_ways():
    cards = search_overpass(39.05, -95.68, 10,
                            http_post=_fake_overpass_post_factory([OSM_NODE, OSM_WAY, OSM_NONAME]))
    assert len(cards) == 2  # unnamed element skipped
    bistro = cards[0]
    assert bistro["name"] == "Test Bistro"
    assert bistro["cuisine"] == "Italian, Pizza"
    assert bistro["address"] == "123 Main St"
    assert bistro["distance_mi"] == 0.0
    assert bistro["rating"] is None
    assert bistro["price_tier"] == 2
    assert bistro["osm_id"] == "node/1"
    joint = cards[1]
    assert joint["cuisine"] == "Burgers"
    assert joint["distance_mi"] > 0  # way uses center coords


def test_search_overpass_fast_food_fallback_cuisine():
    el = {"type": "node", "id": 9, "lat": 39.0, "lon": -95.0,
          "tags": {"amenity": "fast_food", "name": "Quick Stop"}}
    cards = search_overpass(39.0, -95.0, 5, http_post=_fake_overpass_post_factory([el]))
    assert cards[0]["cuisine"] == "Fast Food"


def test_search_overpass_bad_response_raises():
    def _bad(url, data):
        return {"remark": "runtime error"}
    try:
        search_overpass(39.0, -95.0, 5, http_post=_bad)
    except RuntimeError as e:
        assert "unexpected response" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_search_overpass_timeout_message():
    def _slow(url, data):
        raise TimeoutError("timed out")
    try:
        search_overpass(39.0, -95.0, 5, http_post=_slow)
    except RuntimeError as e:
        assert "timed out" in str(e).lower()
    else:
        raise AssertionError("expected RuntimeError")


def test_discover_osm_hides_added_and_caches_per_provider():
    from app.models import Restaurant
    s = SessionLocal()
    try:
        r = Restaurant(name="Test Bistro", cuisine="Italian", price_tier=2)
        s.add(r)
        s.commit()
        els = [OSM_NODE, OSM_WAY]
        out = discover(s, 39.05, -95.68, 10, api_key=None, refresh=True,
                       http_post=_fake_overpass_post_factory(els))
        assert out["provider"] == "osm"
        names = [b["name"] for b in out["businesses"]]
        assert "Test Bistro" not in names  # already in list -> hidden
        assert "Burger Joint" in names
        # second call hits cache, no HTTP
        def _boom(url, data):
            raise AssertionError("should not be called")
        out2 = discover(s, 39.05, -95.68, 10, api_key=None, http_post=_boom)
        assert out2["cached"] is True
        # yelp cache for the same location is a separate key
        out3 = discover(s, 39.05, -95.68, 10, api_key="k", refresh=True,
                        http_get=lambda url, key: {"businesses": []})
        assert out3["provider"] == "yelp"
        assert out3["cached"] is False
    finally:
        s.close()


def test_discover_yelp_key_still_wins():
    import app.discover as disc
    s = SessionLocal()
    try:
        def _nope(*a, **k):
            raise AssertionError("overpass must not be called with a key")
        out = disc.discover(s, 39.0, -95.6, 10, api_key="k", refresh=True,
                            http_get=lambda url, key: {"businesses": []},
                            http_post=_nope)
        assert out["provider"] == "yelp"
    finally:
        s.close()


def test_search_overpass_falls_back_to_next_mirror():
    """overpass-api.de 504s under load — the search must try the next mirror."""
    from urllib.error import HTTPError

    calls = []

    def _flaky(url, data):
        calls.append(url)
        if "overpass-api.de" in url:
            raise HTTPError(url, 504, "Gateway Timeout", {}, None)
        return {"elements": [OSM_NODE]}

    cards = search_overpass(39.05, -95.68, 5, http_post=_flaky)
    assert len(calls) == 2
    assert "kumi" in calls[1]
    assert cards[0]["name"] == "Test Bistro"


def test_search_overpass_all_mirrors_down_raises():
    def _down(url, data):
        raise TimeoutError("timed out")

    import pytest
    with pytest.raises(RuntimeError, match="timed out"):
        search_overpass(39.05, -95.68, 5, http_post=_down)


def test_search_overpass_dns_error_message_is_human_readable():
    def _dns_fail(url, data):
        raise OSError("[Errno -2] Name or service not known")

    import pytest
    with pytest.raises(RuntimeError, match="DNS lookup failed"):
        search_overpass(39.05, -95.68, 5, http_post=_dns_fail)
