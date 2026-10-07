"""Tests for the "include in Friday picks" toggle.

Run:  cd ~/workspace/foodfriday && PYTHONPATH=. .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ff-picks-"))
os.environ.setdefault("FOODFRIDAY_DATA_DIR", str(TMP / "data"))

from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)


def _make(name: str) -> dict:
    r = client.post("/api/restaurants", json={"name": name})
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_toggle_round_trip():
    r = _make("Toggle Diner")
    assert r["include_in_picks"] is True

    t = client.post(f"/api/restaurants/{r['id']}/in-picks")
    assert t.status_code == 200
    assert t.json()["include_in_picks"] is False

    t = client.post(f"/api/restaurants/{r['id']}/in-picks")
    assert t.json()["include_in_picks"] is True


def test_create_with_toggle_false():
    r = client.post("/api/restaurants", json={"name": "Skip Me", "include_in_picks": False})
    assert r.json()["include_in_picks"] is False
    # excluded by default in list payload
    lst = client.get("/api/restaurants").json()
    row = next(x for x in lst if x["name"] == "Skip Me")
    assert row["include_in_picks"] is False


def test_excluded_restaurant_never_picked():
    a = _make("Always In")
    b = _make("Never Picked")
    client.post(f"/api/restaurants/{b['id']}/in-picks")  # exclude b

    seen = set()
    for _ in range(30):
        r = client.post("/api/pick", json={})
        assert r.status_code == 200
        for p in r.json()["picks"]:
            seen.add(p["name"])
    assert "Never Picked" not in seen
    assert "Always In" in seen or "Skip Me" in seen  # others still eligible


def test_restaurants_page_shows_skip_marker():
    r = client.get("/restaurants")
    assert r.status_code == 200
    assert "skipped in picks" in r.text
