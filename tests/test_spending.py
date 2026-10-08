"""Tests for the spending dashboard aggregates.

Run:  cd ~/workspace/foodfriday && PYTHONPATH=. .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ff-spending-"))
os.environ.setdefault("FOODFRIDAY_DATA_DIR", str(TMP / "data"))

from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db  # noqa: E402
from app.main import app, spending_aggregates  # noqa: E402
from app.database import SessionLocal  # noqa: E402

init_db()
client = TestClient(app)


def _make(name, cuisine=None):
    r = client.post("/api/restaurants", json={"name": name, "cuisine": cuisine})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _visit(rid, on: date, total=None):
    r = client.post("/api/visits", json={
        "restaurant_id": rid,
        "visited_at": on.isoformat(),
        "total": total,
    })
    assert r.status_code == 201, r.text


def test_spending_aggregates():
    a = _make("Spend Tacos", cuisine="Mexican")
    b = _make("Spend Sushi", cuisine="Sushi")
    c = _make("Spend Burgers", cuisine="Burgers")
    today = date.today()
    last_month = (today.replace(day=1) - timedelta(days=1)).replace(day=15)

    s = SessionLocal()
    try:
        before = spending_aggregates(s)
    finally:
        s.close()

    _visit(a, today, 30.00)
    _visit(a, today, 20.00)
    _visit(b, last_month, 50.00)
    _visit(c, today, None)  # no total — excluded from math

    s = SessionLocal()
    try:
        agg = spending_aggregates(s)
    finally:
        s.close()

    # delta assertions — the test DB is shared across test files
    assert agg["total"] - before["total"] == 100.00
    assert agg["visit_count"] - before["visit_count"] == 3
    assert agg["without_totals"] - before["without_totals"] == 1
    assert agg["average"] == round(agg["total"] / agg["visit_count"], 2)

    # monthly buckets include this month and last month
    by_label = {m["label"]: m["total"] for m in agg["monthly"]}
    assert by_label[today.strftime("%b %Y")] - \
        {m["label"]: m["total"] for m in before["monthly"]}.get(today.strftime("%b %Y"), 0) == 50.00

    # per-name values are pollution-proof
    top = {r["name"]: r["total"] for r in agg["top_restaurants"]}
    assert top["Spend Tacos"] == 50.00
    assert top["Spend Sushi"] == 50.00
    assert "Spend Burgers" not in top  # no totals -> excluded

    cuis = {c["name"]: c["total"] for c in agg["cuisines"]}
    assert cuis["Mexican"] >= 50.00
    assert cuis["Sushi"] == 50.00


def test_spending_page_renders():
    r = client.get("/spending")
    assert r.status_code == 200
    html = r.text
    assert "💰 Spending" in html
    assert "all-time" in html
    assert "visit(s) without a total were excluded" in html


def test_spending_empty_db():
    # fresh session with no visits at all
    s = SessionLocal()
    try:
        for v in s.query(__import__("app.models", fromlist=["Visit"]).Visit).all():
            s.delete(v)
        s.commit()
        agg = spending_aggregates(s)
    finally:
        s.close()
    assert agg["total"] == 0
    assert agg["average"] == 0.0
    assert agg["monthly"] == []
    assert agg["top_restaurants"] == []
