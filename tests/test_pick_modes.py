"""Tests for "somewhere new" pick mode and cuisine rotation.

Run:  cd ~/workspace/foodfriday && PYTHONPATH=. .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ff-pickmodes-"))
os.environ.setdefault("FOODFRIDAY_DATA_DIR", str(TMP / "data"))

from fastapi.testclient import TestClient  # noqa: E402

from app import picker  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.deal_scan import set_setting  # noqa: E402
from app.discover import K_AVOID_REPEAT_CUISINE  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)


def _make(name, cuisine=None, **kw):
    r = client.post("/api/restaurants",
                    json={"name": name, "cuisine": cuisine, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _visit(rid, days_ago=0):
    r = client.post("/api/visits", json={
        "restaurant_id": rid,
        "visited_at": (date.today() - timedelta(days=days_ago)).isoformat(),
    })
    assert r.status_code == 201, r.text


# ---------- somewhere new ----------

def _all_ids():
    return [r["id"] for r in client.get("/api/restaurants").json()]


def test_new_mode_only_zero_visit_restaurants():
    tried = _make("Been There Burgers", cuisine="Burgers")
    fresh = _make("Never Tried Noodles", cuisine="Ramen")
    _visit(tried["id"], days_ago=30)
    # veto everything else in the (shared) test DB so the result is deterministic:
    # the visited restaurant must still be filtered by mode="new" itself
    veto = [i for i in _all_ids() if i not in (tried["id"], fresh["id"])]

    seen = set()
    for _ in range(5):
        data = client.post(
            "/api/pick", json={"mode": "new", "veto_ids": veto}
        ).json()
        assert data["mode"] == "new"
        for p in data["picks"]:
            seen.add(p["name"])
            assert p["reason"] == "Never tried"
            assert p["last_visit"] is None
    assert seen == {"Never Tried Noodles"}
    assert "Been There Burgers" not in seen


def test_new_mode_empty_state():
    # every restaurant has a visit now
    data = client.get("/api/restaurants").json()
    for r in data:
        client.post("/api/visits", json={"restaurant_id": r["id"]})
    data = client.post("/api/pick", json={"mode": "new"}).json()
    assert data["picks"] == []


def test_new_mode_excludes_not_in_picks():
    r = _make("Skipped Sushi", cuisine="Sushi", include_in_picks=False)
    data = client.post("/api/pick", json={"mode": "new"}).json()
    assert all(p["id"] != r["id"] for p in data["picks"])


def test_newcomer_stats_yelp_rating_tiebreak():
    stats = picker.newcomer_stats([
        {"id": 1, "name": "A", "cuisine": "x", "price_tier": 2, "favorite": False, "yelp_rating": 5.0},
        {"id": 2, "name": "B", "cuisine": "x", "price_tier": 2, "favorite": False, "yelp_rating": None},
        {"id": 3, "name": "C", "cuisine": "x", "price_tier": 2, "favorite": False, "yelp_rating": 2.0},
    ])
    weights = {s["id"]: s["weight"] for s in stats}
    assert weights[1] > weights[3] > weights[2]
    assert weights[2] == 1.0
    assert all(s["days_since"] is None for s in stats)


def test_invalid_mode_rejected():
    r = client.post("/api/pick", json={"mode": "bogus"})
    assert r.status_code == 400


# ---------- cuisine rotation ----------

def test_cuisine_rotation_skips_latest_cuisine():
    s = SessionLocal()
    try:
        set_setting(s, K_AVOID_REPEAT_CUISINE, "1")
        s.commit()
    finally:
        s.close()
    mex = _make("Rotation Tacos", cuisine="Mexican")
    bbq = _make("Rotation BBQ", cuisine="BBQ")
    _visit(mex["id"], days_ago=0)  # most recent visit overall: Mexican, today
    # veto everything else in the (shared) test DB for a deterministic pool
    veto = [i for i in _all_ids() if i not in (mex["id"], bbq["id"])]

    data = client.post("/api/pick", json={"veto_ids": veto}).json()
    assert data.get("skipped_cuisine") == "Mexican"
    assert [p["name"] for p in data["picks"]] == ["Rotation BBQ"]


def test_cuisine_override_include_anyway():
    # veto everyone but the Mexican spot: with the override it must be pickable
    others = [
        r["id"] for r in client.get("/api/restaurants").json()
        if r["name"] != "Rotation Tacos"
    ]
    data = client.post(
        "/api/pick", json={"include_cuisine": True, "veto_ids": others}
    ).json()
    assert "skipped_cuisine" not in data
    assert [p["name"] for p in data["picks"]] == ["Rotation Tacos"]


def test_cuisine_rotation_disabled_by_setting():
    s = SessionLocal()
    try:
        set_setting(s, K_AVOID_REPEAT_CUISINE, "0")
        s.commit()
    finally:
        s.close()
    data = client.post("/api/pick", json={}).json()
    assert "skipped_cuisine" not in data
    # restore default for other tests
    s = SessionLocal()
    try:
        set_setting(s, K_AVOID_REPEAT_CUISINE, "1")
        s.commit()
    finally:
        s.close()
