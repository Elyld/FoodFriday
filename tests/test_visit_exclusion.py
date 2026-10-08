"""Tests for per-visit "counts toward picks" exclusion.

A visit can be unchecked from Friday-pick consideration (kid's solo runs,
breakfast pitstops) while staying in History and Spending. The picker must
ignore excluded visits for weighting, the 7-day rule, cuisine rotation, deal
item matching, and "last visit" lines; spending must still count them.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ff-excl-"))
os.environ.setdefault("FOODFRIDAY_DATA_DIR", str(TMP / "data"))

from fastapi.testclient import TestClient  # noqa: E402

import app.picker as picker  # noqa: E402
from app.database import init_db, SessionLocal  # noqa: E402
from app.main import build_pick_response, spending_aggregates, visit_stats  # noqa: E402
from app.models import Restaurant, Visit  # noqa: E402
from app.main import app  # noqa: E402

TODAY = date(2026, 10, 7)

init_db()
client = TestClient(app)


def _mk_restaurant(name: str, cuisine: str = "Mexican") -> int:
    s = SessionLocal()
    try:
        r = Restaurant(name=name, cuisine=cuisine, price_tier=1, favorite=False,
                       include_in_picks=True, track_visits=True)
        s.add(r)
        s.commit()
        return r.id
    finally:
        s.close()


def _mk_visit(rid: int, when: date, total: float = 20.0, excluded: bool = False) -> int:
    s = SessionLocal()
    try:
        v = Visit(restaurant_id=rid, visited_at=when, total=total,
                  source="manual", exclude_from_picks=excluded)
        s.add(v)
        s.commit()
        return v.id
    finally:
        s.close()


def _cleanup_restaurant(rid: int):
    s = SessionLocal()
    try:
        s.query(Visit).filter(Visit.restaurant_id == rid).delete()
        r = s.get(Restaurant, rid)
        if r:
            s.delete(r)
        s.commit()
    finally:
        s.close()


# ---------- PATCH API ----------

def test_patch_visit_toggle_roundtrip():
    rid = _mk_restaurant("ExclToggle")
    vid = _mk_visit(rid, TODAY - timedelta(days=10))
    try:
        r = client.patch(f"/api/visits/{vid}", json={})
        assert r.status_code == 200
        assert r.json()["exclude_from_picks"] is True

        r = client.patch(f"/api/visits/{vid}", json={})
        assert r.json()["exclude_from_picks"] is False

        r = client.patch(f"/api/visits/{vid}", json={"exclude_from_picks": True})
        assert r.json()["exclude_from_picks"] is True

        # reflected in the visit list
        lst = client.get("/api/visits").json()
        row = next(x for x in lst if x["id"] == vid)
        assert row["exclude_from_picks"] is True

        assert client.patch("/api/visits/999999", json={}).status_code == 404
    finally:
        _cleanup_restaurant(rid)


def test_history_page_shows_checkboxes():
    rid = _mk_restaurant("ExclHist")
    vid = _mk_visit(rid, TODAY - timedelta(days=3), excluded=True)
    try:
        page = client.get("/history")
        assert page.status_code == 200
        assert "In picks" in page.text
        assert f"toggleVisitPicks({vid}" in page.text
    finally:
        _cleanup_restaurant(rid)


# ---------- picker wiring ----------

def test_weight_ignores_excluded_visits():
    rid = _mk_restaurant("ExclWeight", "Burgers")
    _mk_visit(rid, TODAY - timedelta(days=60))
    recent_vid = _mk_visit(rid, TODAY - timedelta(days=2))
    try:
        s = SessionLocal()
        try:
            _, lasts_all = visit_stats(s)
            _, lasts_c = visit_stats(s, counted_only=True)
            assert lasts_all[rid] == TODAY - timedelta(days=2)
            assert lasts_c[rid] == TODAY - timedelta(days=2)
            stats = picker.candidate_stats(
                [{"id": rid, "name": "x", "favorite": False}], lasts_c, today=TODAY)
            assert stats[0]["weight"] == 2.0  # recent visit drags it down
        finally:
            s.close()

        # exclude the recent trip → weight jumps back to the 60-day visit
        client.patch(f"/api/visits/{recent_vid}", json={"exclude_from_picks": True})
        s = SessionLocal()
        try:
            _, lasts_c = visit_stats(s, counted_only=True)
            assert lasts_c[rid] == TODAY - timedelta(days=60)
            stats = picker.candidate_stats(
                [{"id": rid, "name": "x", "favorite": False}], lasts_c, today=TODAY)
            assert stats[0]["weight"] == 60.0
            assert stats[0]["last_visit"] == TODAY - timedelta(days=60)
        finally:
            s.close()
    finally:
        _cleanup_restaurant(rid)


def test_all_excluded_treated_as_never_visited():
    rid = _mk_restaurant("ExclNever", "Burgers")
    _mk_visit(rid, TODAY - timedelta(days=5), excluded=True)
    try:
        s = SessionLocal()
        try:
            counts_c, lasts_c = visit_stats(s, counted_only=True)
            assert rid not in lasts_c
            assert counts_c.get(rid, 0) == 0
            stats = picker.candidate_stats(
                [{"id": rid, "name": "x", "favorite": False}], lasts_c, today=TODAY)
            assert stats[0]["days_since"] is None
            assert stats[0]["weight"] == 365.0  # never-visited boost
        finally:
            s.close()
    finally:
        _cleanup_restaurant(rid)


def test_cuisine_rotation_ignores_excluded_visits():
    # excluded Sushi trip is dated in the future so it would DEFINITELY be
    # the latest visit without the exclusion filter
    r1 = _mk_restaurant("ExclSushi", "Sushi")
    _mk_visit(r1, TODAY + timedelta(days=30), excluded=True)
    r2 = _mk_restaurant("ExclRamen", "Ramen")
    _mk_visit(r2, TODAY - timedelta(days=400), excluded=False)
    try:
        s = SessionLocal()
        try:
            out = build_pick_response(s)
        finally:
            s.close()
        assert out.get("skipped_cuisine") != "Sushi"

        # flip it back to counted → Sushi becomes the skipped cuisine
        s = SessionLocal()
        try:
            v = s.query(Visit).filter(Visit.restaurant_id == r1).one()
            vid = v.id
        finally:
            s.close()
        client.patch(f"/api/visits/{vid}", json={"exclude_from_picks": False})
        s = SessionLocal()
        try:
            out = build_pick_response(s)
        finally:
            s.close()
        assert out.get("skipped_cuisine") == "Sushi"
    finally:
        _cleanup_restaurant(r1)
        _cleanup_restaurant(r2)


def test_seven_day_rule_ignores_excluded_visits():
    # only a 2-day-old visit, but it's excluded → treated as 60 days → fresh
    rid = _mk_restaurant("ExclFresh", "Burgers")
    _mk_visit(rid, TODAY - timedelta(days=60), excluded=False)
    recent_vid = _mk_visit(rid, TODAY - timedelta(days=2), excluded=False)
    try:
        client.patch(f"/api/visits/{recent_vid}", json={"exclude_from_picks": True})
        s = SessionLocal()
        try:
            _, lasts_c = visit_stats(s, counted_only=True)
            stats = picker.candidate_stats(
                [{"id": rid, "name": "x", "favorite": False}], lasts_c, today=TODAY)
            # days_since=60 → passes the 7-day freshness filter in pick_three
            assert stats[0]["days_since"] == 60
        finally:
            s.close()
    finally:
        _cleanup_restaurant(rid)


def test_deal_item_matching_ignores_excluded_visits():
    rid = _mk_restaurant("ExclItems", "Burgers")
    _mk_visit(rid, TODAY - timedelta(days=60))
    s = SessionLocal()
    try:
        v = Visit(restaurant_id=rid, visited_at=TODAY - timedelta(days=2),
                  total=15.0, source="manual", items="Kid's Happy Meal",
                  exclude_from_picks=True)
        s.add(v)
        s.commit()
    finally:
        s.close()
    try:
        s = SessionLocal()
        try:
            # replicate build_pick_response's items wiring
            items_by_id: dict[int, list[str]] = {}
            for vv in (
                s.query(Visit)
                .filter(Visit.items.isnot(None), Visit.exclude_from_picks.isnot(True))
                .all()
            ):
                if vv.restaurant_id == rid:
                    items_by_id.setdefault(rid, []).extend(
                        [i.strip() for i in (vv.items or "").splitlines() if i.strip()])
            assert rid not in items_by_id  # excluded visit's items invisible
        finally:
            s.close()
    finally:
        _cleanup_restaurant(rid)


# ---------- spending untouched ----------

def test_spending_still_counts_excluded_visits():
    rid = _mk_restaurant("ExclSpend", "Burgers")
    try:
        s = SessionLocal()
        before = spending_aggregates(s)["total"]
        s.close()
        _mk_visit(rid, TODAY - timedelta(days=10), total=42.50, excluded=True)
        s = SessionLocal()
        try:
            after = spending_aggregates(s)
        finally:
            s.close()
        assert round(after["total"] - before, 2) == 42.50
    finally:
        _cleanup_restaurant(rid)
