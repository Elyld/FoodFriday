"""API tests: restaurants CRUD, visits, pick endpoint.

Run:  cd ~/workspace/foodfriday && PYTHONPATH=. .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="foodfriday-test-"))
os.environ.setdefault("FOODFRIDAY_DATA_DIR", str(TMP / "data"))

from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def _make(name="Taco Town", **kw):
    payload = {"name": name, "cuisine": "Mexican", "price_tier": 1, **kw}
    r = client.post("/api/restaurants", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_restaurant_crud():
    r = _make("Noodle Palace", cuisine="Ramen", price_tier=2)
    rid = r["id"]

    lst = client.get("/api/restaurants").json()
    assert any(x["name"] == "Noodle Palace" for x in lst)

    u = client.put(f"/api/restaurants/{rid}", json={"name": "Noodle Palace", "cuisine": "Japanese", "price_tier": 2})
    assert u.status_code == 200

    lst = client.get("/api/restaurants").json()
    row = next(x for x in lst if x["id"] == rid)
    assert row["cuisine"] == "Japanese"
    assert row["visit_count"] == 0
    assert row["last_visit"] is None


def test_restaurant_requires_name():
    r = client.post("/api/restaurants", json={"name": "   "})
    assert r.status_code == 400


def test_favorite_toggle():
    r = _make("Fav Place")
    rid = r["id"]
    t1 = client.post(f"/api/restaurants/{rid}/favorite").json()
    assert t1["favorite"] is True
    t2 = client.post(f"/api/restaurants/{rid}/favorite").json()
    assert t2["favorite"] is False


def test_visit_logging_and_history():
    r = _make("History Diner")
    rid = r["id"]

    v = client.post("/api/visits", json={"restaurant_id": rid, "total": 42.5})
    assert v.status_code == 201
    assert v.json()["visited_at"] == date.today().isoformat()

    lst = client.get("/api/restaurants").json()
    row = next(x for x in lst if x["id"] == rid)
    assert row["visit_count"] == 1
    assert row["last_visit"] == date.today().isoformat()

    hist = client.get("/api/visits").json()
    assert any(h["restaurant_name"] == "History Diner" and h["total"] == 42.5 for h in hist)

    vid = next(h["id"] for h in hist if h["restaurant_name"] == "History Diner")
    d = client.delete(f"/api/visits/{vid}")
    assert d.status_code == 204
    hist = client.get("/api/visits").json()
    assert not any(h["id"] == vid for h in hist)


def test_visit_404():
    assert client.post("/api/visits", json={"restaurant_id": 99999}).status_code == 404
    assert client.delete("/api/visits/99999").status_code == 404
    assert client.delete("/api/restaurants/99999").status_code == 404


def test_delete_restaurant_cascades_visits():
    r = _make("Doomed Cafe")
    rid = r["id"]
    client.post("/api/visits", json={"restaurant_id": rid})
    assert client.delete(f"/api/restaurants/{rid}").status_code == 204
    hist = client.get("/api/visits").json()
    assert not any(h["restaurant_name"] == "Doomed Cafe" for h in hist)


def test_pick_returns_three_with_reasons():
    for i in range(5):
        _make(f"Pick Place {i}")
    r = client.post("/api/pick", json={"keep_ids": [], "veto_ids": []})
    assert r.status_code == 200
    picks = r.json()["picks"]
    assert len(picks) == 3
    ids = [p["id"] for p in picks]
    assert len(set(ids)) == 3
    for p in picks:
        assert p["reason"], "each pick needs a reason"


def test_pick_veto_replaces_one_card():
    r = client.post("/api/pick", json={})
    first = r.json()["picks"]
    assert len(first) == 3
    veto_id = first[0]["id"]
    keep = [p["id"] for p in first[1:]]
    r2 = client.post("/api/pick", json={"keep_ids": keep, "veto_ids": [veto_id]})
    second = r2.json()["picks"]
    ids = [p["id"] for p in second]
    assert veto_id not in ids
    assert all(k in ids for k in keep)


def test_pick_excludes_recent_visit():
    r = _make("Ate Yesterday")
    rid = r["id"]
    client.post("/api/visits", json={"restaurant_id": rid})
    for _ in range(10):
        picks = client.post("/api/pick", json={}).json()["picks"]
        assert all(p["id"] != rid for p in picks)


def test_pages_render():
    for path in ["/", "/restaurants", "/history", "/import"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "FoodFriday" in r.text
