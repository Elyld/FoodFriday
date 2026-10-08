"""Crave: "what am I feeling?" — LLM-as-interpreter, app-as-decider.

The model turns free text ("I'm feeling soul food") into structured
filters (JSON). The APP then matches those filters against his restaurant
DB + nearby spots and ranks with the existing picker weights — so a
suggestion can never be hallucinated. No OpenRouter key? The keyword
fallback keeps the feature working, just dumber.

Intent schema:
    {"cuisines": [...], "keywords": [...], "max_price_tier": 1|2|3|null,
     "include_new": bool, "avoid_cuisines": [...]}
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import httpx

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
HTTP_TIMEOUT = 30.0

STOPWORDS = {
    "i", "im", "i'm", "a", "an", "the", "and", "or", "for", "to", "of",
    "in", "on", "is", "it", "its", "that", "this", "with", "want", "like",
    "something", "somewhere", "feel", "feeling", "feelin", "kinda", "kind",
    "really", "tonight", "friday", "us", "we", "me", "my", "our", "dont",
    "don't", "wanna", "what", "whats", "what's", "any", "good", "great",
    "mood", "craving", "crave", "maybe", "just", "get", "getting",
}
CHEAP_WORDS = {"cheap", "cheapest", "inexpensive", "budget", "affordable", "cheap!"}
MODERATE_PHRASES = (
    "not too expensive", "nothing too expensive", "moderate",
    "reasonably priced", "mid-range", "midrange", "not fancy",
)
PRICEY_WORDS = {"expensive", "fancy", "splurge", "upscale", "pricey", "fine dining"}
NEW_PHRASES = (
    "somewhere new", "something new", "never been", "havent tried",
    "haven't tried", "havent been", "haven't been", "new spot",
    "new place", "adventurous", "different",
)
AVOID_RE = re.compile(
    r"\b(?:not|no|anything but|except|don't want|dont want|skip)\s+([a-z& ]+?)(?:\s+(?:tonight|please|for us|for dinner))?$"
)


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def empty_intent() -> dict:
    return {
        "cuisines": [],
        "keywords": [],
        "max_price_tier": None,
        "include_new": False,
        "avoid_cuisines": [],
    }


def keyword_intent(text: str, cuisines: list[str]) -> dict:
    """No-key fallback: substring rules over the craving text."""
    intent = empty_intent()
    low = _norm(text)

    for c in cuisines:
        if c and _norm(c) in low:
            intent["cuisines"].append(c)

    m = AVOID_RE.search(low)
    if m:
        for c in cuisines:
            if c and _norm(c) in m.group(1):
                intent["avoid_cuisines"].append(c)

    if any(w in low for w in CHEAP_WORDS):
        intent["max_price_tier"] = 1
    elif any(p in low for p in MODERATE_PHRASES):
        intent["max_price_tier"] = 2
    # pricey words / no mention -> no cap

    if any(p in low for p in NEW_PHRASES):
        intent["include_new"] = True

    words = re.findall(r"[a-z']+", low)
    taken = {_norm(c) for c in intent["cuisines"]}
    for w in words:
        w = w.strip("'")
        if len(w) > 2 and w not in STOPWORDS and w not in taken:
            # don't let price/new phrasing leak into keywords
            if w not in CHEAP_WORDS and w not in PRICEY_WORDS:
                intent["keywords"].append(w)
    # de-dupe, keep order
    intent["keywords"] = list(dict.fromkeys(intent["keywords"]))
    return intent


def parse_intent_json(raw: str) -> dict | None:
    """Defensively parse the model's JSON (strips ``` fences, finds the
    object). Returns a validated intent dict or None."""
    try:
        s = (raw or "").strip()
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
        start, end = s.find("{"), s.rfind("}")
        if start < 0 or end <= start:
            return None
        d = json.loads(s[start : end + 1])
        if not isinstance(d, dict):
            return None
        tier = d.get("max_price_tier")
        return {
            "cuisines": [str(x) for x in (d.get("cuisines") or []) if str(x).strip()],
            "keywords": [str(x) for x in (d.get("keywords") or []) if str(x).strip()],
            "max_price_tier": tier if tier in (1, 2, 3) else None,
            "include_new": bool(d.get("include_new")),
            "avoid_cuisines": [
                str(x) for x in (d.get("avoid_cuisines") or []) if str(x).strip()
            ],
        }
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return None


def _system_prompt(cuisines: list[str]) -> str:
    vocab = ", ".join(cuisines) if cuisines else "(none yet)"
    return (
        "You turn a dinner craving into structured filters for a restaurant picker. "
        f"The user's known cuisines: {vocab}.\n"
        "Reply with ONLY a JSON object, no markdown, no other text:\n"
        '{"cuisines": [...], "keywords": [...], "max_price_tier": 1|2|3|null, '
        '"include_new": true|false, "avoid_cuisines": [...]}\n'
        "Rules:\n"
        "- cuisines: known cuisines the craving names, using the exact names from the list above. "
        'If the craving names a cuisine NOT in the list (e.g. "soul food"), put it in keywords instead.\n'
        '- keywords: other food words — dishes, vibes ("bar food", "tacos", "wings", "soul food").\n'
        "- max_price_tier: 1 for cheap/budget/inexpensive; 2 for \"not too expensive\"/moderate; "
        "3 or null for fancy/splurge/no mention.\n"
        "- include_new: true if they want somewhere new/untried.\n"
        "- avoid_cuisines: known cuisines they explicitly rule out."
    )


def parse_intent_llm(
    text: str,
    api_key: str,
    model: str,
    cuisines: list[str],
    http_post=None,
) -> dict | None:
    """Ask OpenRouter to turn the craving into an intent dict.

    Returns None on any failure (network, bad key, malformed JSON) — the
    caller falls back to keyword_intent.
    """
    if not api_key or not model:
        return None
    post = http_post or httpx.post
    try:
        resp = post(
            OPENROUTER_CHAT_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/Elyld/FoodFriday",
                "X-Title": "FoodFriday Crave",
                "User-Agent": "FoodFriday/1.0",
            },
            json={
                "model": model,
                "temperature": 0.1,
                "max_tokens": 300,
                "messages": [
                    {"role": "system", "content": _system_prompt(cuisines)},
                    {"role": "user", "content": text},
                ],
            },
            timeout=HTTP_TIMEOUT,
        )
        data = resp.json() if hasattr(resp, "json") else {}
        choices = data.get("choices") or []
        content = ((choices[0].get("message") or {}).get("content")) if choices else ""
        return parse_intent_json(content)
    except Exception:
        return None


# ---------- matching ----------


def match_candidate(cand: dict[str, Any], intent: dict) -> tuple[bool, list[str]]:
    """Does this restaurant/discover-card match the intent? Never raises.

    cand needs: name, cuisine, price_tier, notes (optional), include_in_picks
    (optional — discover cards are always includable).
    Returns (matched, reason_lines).
    """
    if cand.get("include_in_picks") is False:
        return False, []
    cap = intent.get("max_price_tier")
    if cap is not None and (cand.get("price_tier") or 2) > cap:
        return False, []
    avoid = {_norm(c) for c in intent.get("avoid_cuisines") or []}
    if _norm(cand.get("cuisine")) in avoid:
        return False, []

    cuisines = intent.get("cuisines") or []
    keywords = [k.lower() for k in intent.get("keywords") or []]
    if not cuisines and not keywords:
        return True, []  # no flavor constraints — rank by picker weights

    hay = f"{cand.get('name', '')} {cand.get('cuisine', '')} {cand.get('notes', '')}".lower()
    reasons: list[str] = []
    for c in cuisines:
        term = _norm(c)
        if term and (term == _norm(cand.get("cuisine")) or term in hay):
            reasons.append(f"Matches '{c}'")
    for kw in keywords:
        if kw and kw in hay:
            reasons.append(f"Matches '{kw}'")
    return (True if reasons else False), reasons


# ---------- suggestion engine ----------

K_CRAVE_KEY = "openrouter_api_key"
K_CRAVE_MODEL = "crave_model"
K_CRAVE_FREE_ONLY = "crave_model_free_only"
DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"


def _get_setting(session, key: str, default: str | None = None):
    from app.models import Setting

    row = session.get(Setting, key)
    return row.value if row is not None else default


def suggest(
    session,
    text: str,
    *,
    today: date | None = None,
    http_post=None,
    discover_fn=None,
) -> dict:
    """Build 3 grounded suggestions for a craving.

    LLM (or the keyword fallback) produces an intent; the app matches it
    against his restaurants + nearby spots and ranks with the picker
    weights. Suggestions can only ever come from real data — the model
    never names a restaurant.
    """
    from app import picker as _picker
    from app.main import _deal_and_item_inputs, visit_stats
    from app.models import Restaurant

    today = today or date.today()
    api_key = _get_setting(session, K_CRAVE_KEY)
    model = _get_setting(session, K_CRAVE_MODEL) or DEFAULT_MODEL

    restaurants = session.query(Restaurant).all()
    # display-case cuisine vocabulary for the LLM prompt / keyword matcher
    vocab = sorted({r.cuisine.strip() for r in restaurants if (r.cuisine or "").strip()})

    intent = None
    via = "keyword"
    if api_key:
        intent = parse_intent_llm(text, api_key, model, vocab, http_post=http_post)
        if intent is not None:
            via = "ai"
    if intent is None:
        intent = keyword_intent(text, vocab)

    counts, lasts = visit_stats(session, counted_only=True)
    deals_by_id, items_by_id = _deal_and_item_inputs(session, today)

    # --- his places ---
    his: list[tuple[float, dict]] = []
    matched_dicts = []
    for r in restaurants:
        cand = {
            "id": r.id,
            "name": r.name,
            "cuisine": r.cuisine,
            "price_tier": r.price_tier or 2,
            "notes": r.notes,
            "include_in_picks": r.include_in_picks,
            "favorite": bool(r.favorite),
        }
        ok, match_reasons = match_candidate(cand, intent)
        if ok:
            matched_dicts.append((r, cand, match_reasons))
    if matched_dicts:
        stats = _picker.candidate_stats(
            [c for _, c, _ in matched_dicts],
            {rid: d for rid, d in lasts.items()},
            today=today,
            deals_by_id=deals_by_id,
            items_by_id=items_by_id,
        )
        for (r, cand, match_reasons), st in zip(matched_dicts, stats):
            reasons = list(match_reasons)
            reasons.append(_picker.reason_for(st))
            for t in st.get("deal_titles", []):
                reasons.append(f"🏷️ {t}")
            his.append(
                (
                    st["weight"],
                    {
                        "kind": "tried",
                        "id": r.id,
                        "name": r.name,
                        "cuisine": r.cuisine,
                        "price_tier": r.price_tier or 2,
                        "favorite": bool(r.favorite),
                        "last_visit": st["last_visit"].isoformat() if st["last_visit"] else None,
                        "reasons": reasons,
                        "deal_titles": st.get("deal_titles", []),
                    },
                )
            )

    # --- new places (nearby search, filtered by the same intent) ---
    new: list[tuple[float, dict]] = []
    note = None
    lat = _get_setting(session, "home_lat")
    lon = _get_setting(session, "home_lon")
    try:
        flat, flon = float(lat), float(lon)
    except (TypeError, ValueError):
        flat = flon = None
    if flat is not None and flon is not None:
        from app import discover as _discover

        yelp_key = _get_setting(session, "yelp_api_key")
        dfn = discover_fn or _discover.discover
        try:
            res = dfn(session, flat, flon, 10.0, yelp_key, refresh=False)
            cards = res.get("businesses", []) if isinstance(res, dict) else []
            scored_new = []
            for b in cards:
                cand = {
                    "name": b.get("name"),
                    "cuisine": b.get("cuisine"),
                    "price_tier": b.get("price_tier") or 2,
                    "notes": b.get("address"),
                }
                ok, match_reasons = match_candidate(cand, intent)
                if not ok:
                    continue
                rating = b.get("rating")
                weight = 1.0 + (float(rating) / 10.0) if rating else 1.0
                reasons = ["✨ New spot near you", *match_reasons]
                scored_new.append(
                    (
                        weight,
                        {
                            "kind": "new",
                            "name": b.get("name"),
                            "cuisine": b.get("cuisine"),
                            "price_tier": b.get("price_tier") or 2,
                            "address": b.get("address"),
                            "distance_mi": b.get("distance_mi"),
                            "rating": rating,
                            "reasons": reasons,
                            "add_payload": {
                                "name": b.get("name"),
                                "cuisine": b.get("cuisine"),
                                "price_tier": b.get("price_tier") or 2,
                                "address": b.get("address"),
                                "rating": rating,
                                "yelp_id": b.get("yelp_id"),
                            },
                        },
                    )
                )
            new = scored_new
        except Exception:
            note = "Nearby search was unavailable — showing your places only."
    else:
        note = "Set your home location in Settings to include nearby new spots."

    # --- mix: normalize each group 0..1 by group max, take top 3 ---
    def _normed(pairs: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
        if not pairs:
            return []
        mx = max(w for w, _ in pairs) or 1.0
        return [(w / mx, card) for w, card in pairs]

    merged = sorted(_normed(his) + _normed(new), key=lambda p: p[0], reverse=True)
    suggestions = [card for _, card in merged[:3]]

    out: dict[str, Any] = {
        "intent": {**intent, "via": via},
        "suggestions": suggestions,
    }
    if note:
        out["note"] = note
    return out
