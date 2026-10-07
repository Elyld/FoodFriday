"""The Friday picker: weighted-random shortlist of 3 restaurants.

Weighting rules (Josh-approved):
- Days since last visit, weighted: longer absence = likelier pick.
- Never-visited restaurants get a strong boost (treated as 365 days).
- Favorites get x2.
- Places visited in the last 7 days are hard-excluded — unless that would
  leave fewer than 3 candidates, in which case the exclusion is relaxed.
- Weighted random sample, no repeats within the 3.

Pure functions over plain dicts so the logic is trivially testable.
"""

from __future__ import annotations

import random
from datetime import date
from typing import Any


def candidate_stats(
    restaurants: list[dict[str, Any]],
    last_visit_by_id: dict[int, date | None],
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Attach days-since-last-visit and a weight to each restaurant dict.

    Each restaurant dict needs: id, name, favorite (bool).
    Returns the same dicts with added keys: days_since (int|None),
    weight (float), last_visit (date|None).
    """
    today = today or date.today()
    out = []
    for r in restaurants:
        last = last_visit_by_id.get(r["id"])
        days = (today - last).days if last else None
        base = float(days) if days is not None else 365.0
        weight = base * (2.0 if r.get("favorite") else 1.0)
        out.append({**r, "last_visit": last, "days_since": days, "weight": weight})
    return out


def reason_for(pick: dict[str, Any]) -> str:
    """One-line human reason attached to each pick card."""
    if pick["days_since"] is None:
        return "New — never logged"
    if pick["days_since"] >= 90:
        return f"Haven't been since {pick['last_visit'].strftime('%b %Y')}"
    if pick.get("favorite"):
        return "A favorite"
    if pick["days_since"] >= 30:
        return f"Been {pick['days_since']} days"
    return "In the rotation"


def pick_three(
    stats: list[dict[str, Any]],
    keep_ids: tuple[int, ...] = (),
    veto_ids: tuple[int, ...] = (),
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    """Return exactly 3 picks (or fewer if fewer restaurants exist).

    keep_ids: restaurant ids pinned from a previous round (returned as-is).
    veto_ids: restaurant ids the veto button ruled out this round.
    """
    rng = rng or random.Random()
    by_id = {s["id"]: s for s in stats}
    kept = [by_id[i] for i in keep_ids if i in by_id]
    need = max(0, 3 - len(kept))

    pool = [s for s in stats if s["id"] not in veto_ids and s["id"] not in keep_ids]

    # Hard-exclude places visited in the last 7 days — unless relaxing is
    # the only way to fill the shortlist.
    fresh = [s for s in pool if s["days_since"] is None or s["days_since"] >= 7]
    if len(fresh) >= need:
        pool = fresh

    picks = list(kept)
    remaining = list(pool)
    for _ in range(need):
        if not remaining:
            break
        total = sum(s["weight"] for s in remaining)
        if total <= 0:
            chosen = rng.choice(remaining)
        else:
            roll = rng.uniform(0, total)
            chosen = remaining[-1]
            for s in remaining:
                roll -= s["weight"]
                if roll <= 0:
                    chosen = s
                    break
        picks.append(chosen)
        remaining.remove(chosen)

    for p in picks:
        p["reason"] = reason_for(p)
    return picks
