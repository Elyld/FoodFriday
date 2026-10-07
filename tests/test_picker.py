"""Tests for the Friday picker's weighting rules.

Run:  cd ~/workspace/foodfriday && PYTHONPATH=. .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from app.picker import candidate_stats, pick_three, reason_for

TODAY = date(2026, 10, 7)


def make_restaurants(n=6):
    return [
        {"id": i, "name": f"Place {i}", "favorite": False}
        for i in range(1, n + 1)
    ]


def stats_for(last_by_id, favorites=(), n=6):
    rs = make_restaurants(n)
    for r in rs:
        if r["id"] in favorites:
            r["favorite"] = True
    return candidate_stats(rs, last_by_id, today=TODAY)


def test_seven_day_exclusion():
    recent = {1: TODAY - timedelta(days=3)}
    stats = stats_for(recent)
    rng = random.Random(42)
    for _ in range(50):
        picks = pick_three(stats, rng=rng)
        ids = [p["id"] for p in picks]
        assert 1 not in ids, "visited 3 days ago must be excluded"


def test_exclusion_relaxes_when_too_few_candidates():
    # Only 2 restaurants, both visited recently -> relax, still return both.
    rs = [{"id": 1, "name": "A", "favorite": False}, {"id": 2, "name": "B", "favorite": False}]
    stats = candidate_stats(rs, {1: TODAY - timedelta(days=1), 2: TODAY - timedelta(days=2)}, today=TODAY)
    picks = pick_three(stats, rng=random.Random(1))
    assert len(picks) == 2


def test_no_duplicate_cards():
    stats = stats_for({})
    for seed in range(20):
        picks = pick_three(stats, rng=random.Random(seed))
        ids = [p["id"] for p in picks]
        assert len(ids) == len(set(ids)) == 3


def test_favorite_boost():
    # Same recency for all: favorite must carry exactly 2x weight...
    last = {i: TODAY - timedelta(days=40) for i in range(1, 7)}
    stats = stats_for(last, favorites={2})
    fav_w = next(s["weight"] for s in stats if s["id"] == 2)
    others = [s["weight"] for s in stats if s["id"] != 2]
    assert all(fav_w == 2 * w for w in others)
    # ...and get picked noticeably more often.
    rng = random.Random(7)
    counts = {i: 0 for i in range(1, 7)}
    for _ in range(300):
        for p in pick_three(stats, rng=rng):
            counts[p["id"]] += 1
    avg_other = sum(counts[i] for i in range(1, 7) if i != 2) / 5
    assert counts[2] > avg_other * 1.3, counts


def test_stale_beats_fresh():
    stats = stats_for(
        {1: TODAY - timedelta(days=200), 2: TODAY - timedelta(days=10)},
    )
    rng = random.Random(11)
    picks_ids = [p["id"] for _ in range(200) for p in pick_three(stats, rng=rng)[:1]]
    assert picks_ids.count(1) > picks_ids.count(2) * 3


def test_never_visited_gets_reason():
    stats = stats_for({})
    p = next(s for s in stats if s["id"] == 1)
    assert reason_for(p) == "New — never logged"


def test_long_absence_reason():
    stats = stats_for({1: TODAY - timedelta(days=120)})
    p = next(s for s in stats if s["id"] == 1)
    assert reason_for(p) == "Haven't been since Jun 2026"


def test_favorite_reason():
    stats = stats_for({1: TODAY - timedelta(days=45)}, favorites={1})
    p = next(s for s in stats if s["id"] == 1)
    assert reason_for(p) == "A favorite"


def test_keep_and_veto():
    stats = stats_for({})
    rng = random.Random(3)
    first = pick_three(stats, rng=rng)
    keep_id = first[0]["id"]
    veto_id = first[1]["id"]
    second = pick_three(stats, keep_ids=(keep_id,), veto_ids=(veto_id,), rng=rng)
    ids = [p["id"] for p in second]
    assert keep_id in ids
    assert veto_id not in ids
    assert len(ids) == 3


def test_weight_zero_or_missing_still_picks():
    stats = stats_for({1: TODAY, 2: TODAY - timedelta(days=1)})
    # both excluded by 7-day rule with 6 candidates; force relaxation path
    picks = pick_three(stats, rng=random.Random(9))
    assert len(picks) == 3
