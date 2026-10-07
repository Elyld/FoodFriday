"""Import tests: preview counts, idempotent confirm, dedupe.

Run:  cd ~/workspace/foodfriday && PYTHONPATH=. .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="foodfriday-import-test-"))
os.environ.setdefault("FOODFRIDAY_DATA_DIR", str(TMP / "data"))

from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)

SEED = {
    "restaurants": [
        {"name": "Taco Township", "cuisine": "Mexican", "price_tier": 1, "favorite": True},
        {"name": "Noodle Emporium", "cuisine": "Ramen", "price_tier": 2},
    ],
    "visits": [
        {
            "restaurant": "Taco Township",
            "visited_at": "2026-06-05",
            "total": 32.50,
            "source": "import",
            "external_id": "gmail:aaa",
        },
        {
            "restaurant": "Noodle Emporium",
            "visited_at": "2026-06-12",
            "total": 41.00,
            "source": "import",
            "external_id": "gmail:bbb",
        },
    ],
}


def _upload(payload: dict):
    raw = json.dumps(payload).encode()
    return client.post(
        "/api/import/preview", files={"file": ("seed.json", io.BytesIO(raw), "application/json")}
    )


def test_preview_counts():
    r = _upload(SEED)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["new_restaurants"] == 2
    assert body["new_visits"] == 2


def test_confirm_then_idempotent():
    _upload(SEED)
    c1 = client.post("/api/import/confirm", json=SEED)
    assert c1.status_code == 200
    assert c1.json() == {"restaurants_added": 2, "visits_added": 2}

    # Preview now shows nothing new...
    p = _upload(SEED).json()
    assert p["new_restaurants"] == 0
    assert p["new_visits"] == 0

    # ...and a second confirm is a safe no-op.
    c2 = client.post("/api/import/confirm", json=SEED)
    assert c2.json() == {"restaurants_added": 0, "visits_added": 0}

    visits = client.get("/api/visits").json()
    imported = [v for v in visits if v["restaurant_name"] in ("Taco Township", "Noodle Emporium")]
    assert len(imported) == 2


def test_dedupe_by_restaurant_and_date_without_external_id():
    dup = {
        "restaurants": [{"name": "Taco Township"}],
        "visits": [{"restaurant": "Taco Township", "visited_at": "2026-06-05", "total": 99.99}],
    }
    c = client.post("/api/import/confirm", json=dup)
    assert c.json()["visits_added"] == 0, "same restaurant+date must not duplicate"


def test_visit_to_unknown_restaurant_skipped():
    weird = {
        "restaurants": [],
        "visits": [{"restaurant": "Place That Does Not Exist", "visited_at": "2026-01-01"}],
    }
    p = _upload(weird).json()
    assert p["new_visits"] == 0
    assert "skipped" in p["skipped"]


def test_bad_json_rejected():
    r = client.post(
        "/api/import/preview",
        files={"file": ("seed.json", io.BytesIO(b"not json{{{"), "application/json")},
    )
    assert r.status_code == 400


def test_name_matching_case_insensitive():
    again = {
        "restaurants": [{"name": "taco township"}],
        "visits": [],
    }
    p = _upload(again).json()
    assert p["new_restaurants"] == 0
