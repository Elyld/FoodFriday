"""The Friday picker: weighted-random shortlist of 3 restaurants.

Weighting rules (Josh-approved):
- Days since last visit, weighted: longer absence = likelier pick.
- Never-visited restaurants get a strong boost (treated as 365 days).
- Favorites get x2.
- Places visited in the last 7 days are hard-excluded — unless that would
  leave fewer than 3 candidates, in which case the exclusion is relaxed.
- Active deals boost weight x1.5; a deal whose item keywords match something
  he actually orders gets another x1.25 (multiplicative).
- Weighted random sample, no repeats within the 3.

Pure functions over plain dicts so the logic is trivially testable.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import Any

DEAL_BOOST = 1.5        # active deal on the restaurant
DEAL_ITEM_BOOST = 1.25  # deal's item keywords match something he actually orders
OPEN_ENDED_DEAL_DAYS = 30  # a deal with no valid_until stays active this long


def is_deal_active(deal: dict[str, Any], today: date | None = None) -> bool:
    """A deal is active when today falls in [valid_from, valid_until].

    valid_from defaults to the day the deal was recorded; a missing
    valid_until means open-ended, capped at OPEN_ENDED_DEAL_DAYS from creation.
    """
    today = today or date.today()
    created = deal.get("created_at")
    if isinstance(created, datetime):
        created = created.date()
    start = deal.get("valid_from") or created or date.min
    end = deal.get("valid_until")
    if end is None:
        end = (created + timedelta(days=OPEN_ENDED_DEAL_DAYS)) if created else date.max
    return start <= today <= end


def _deal_item_match(deal: dict[str, Any], past_items: list[str]) -> bool:
    """True when any of the deal's item keywords substring-matches a past item."""
    keywords = [k.strip().lower() for k in (deal.get("item_keywords") or "").split(",") if k.strip()]
    if not keywords or not past_items:
        return False
    items = [i.lower() for i in past_items]
    return any(kw in item for kw in keywords for item in items)


def candidate_stats(
    restaurants: list[dict[str, Any]],
    last_visit_by_id: dict[int, date | None],
    today: date | None = None,
    deals_by_id: dict[int, list[dict[str, Any]]] | None = None,
    items_by_id: dict[int, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Attach days-since-last-visit and a weight to each restaurant dict.

    Each restaurant dict needs: id, name, favorite (bool).
    Returns the same dicts with added keys: days_since (int|None),
    weight (float), last_visit (date|None), deal_titles (list[str]).
    """
    today = today or date.today()
    deals_by_id = deals_by_id or {}
    items_by_id = items_by_id or {}
    out = []
    for r in restaurants:
        last = last_visit_by_id.get(r["id"])
        days = (today - last).days if last else None
        base = float(days) if days is not None else 365.0
        weight = base * (2.0 if r.get("favorite") else 1.0)
        deal_titles: list[str] = []
        for deal in deals_by_id.get(r["id"], []):
            if is_deal_active(deal, today):
                deal_titles.append(deal["title"])
                weight *= DEAL_BOOST
                if _deal_item_match(deal, items_by_id.get(r["id"], [])):
                    weight *= DEAL_ITEM_BOOST
        out.append(
            {**r, "last_visit": last, "days_since": days, "weight": weight,
             "deal_titles": deal_titles}
        )
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


def newcomer_stats(restaurants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stats for "somewhere new" mode: only restaurants with zero visits.

    Weight is uniform-ish, with a gentle tiebreak from the stored Yelp rating
    (a 5-star spot gets 1.5x a no-rating one — discovery, not dominance).
    Each restaurant dict needs: id, name, cuisine, price_tier, favorite,
    yelp_rating (optional).
    """
    out = []
    for r in restaurants:
        rating = r.get("yelp_rating")
        weight = 1.0 + (float(rating) / 10.0) if rating else 1.0
        out.append(
            {**r, "last_visit": None, "days_since": None, "weight": weight,
             "deal_titles": [], "reason": "Never tried"}
        )
    return out


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
