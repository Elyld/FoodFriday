"""Nearby discovery via the Yelp Fusion API.

Searches for restaurants around a location, caches results for 24h so the
free Starter plan (~150 calls/day) isn't burned by repeat views. Businesses
already in the restaurant list are hidden by normalized name match.

Yelp setup for Josh: developer.yelp.com → Create App → Starter plan (free,
no credit card) → paste the API key in Settings.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import DiscoverCache, Restaurant

logger = logging.getLogger(__name__)

YELP_SEARCH_URL = "https://api.yelp.com/v3/businesses/search"
CACHE_TTL_HOURS = 24
HTTP_TIMEOUT = 20  # seconds

# settings keys
K_YELP_KEY = "yelp_api_key"
K_HOME_LAT = "home_lat"
K_HOME_LON = "home_lon"
K_AVOID_REPEAT_CUISINE = "avoid_repeat_cuisine"  # "1"/"0", default "1"


def norm_name(name: str | None) -> str:
    """Fuzzy name key: lowercase, punctuation/whitespace collapsed."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def price_str_to_tier(price: str | None) -> int:
    """Yelp '$'..'$$$$' -> our 1-3 tier."""
    if not price:
        return 2
    n = price.count("$")
    return 1 if n <= 1 else 3 if n >= 3 else 2


def _http_get(url: str, api_key: str) -> dict:
    """Real Yelp call. `http_get` is injectable in search_nearby for tests."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _business_to_card(b: dict) -> dict:
    cats = [c.get("title") for c in (b.get("categories") or []) if c.get("title")]
    addr = ", ".join(b.get("location", {}).get("display_address") or [])
    dist_m = b.get("distance")
    return {
        "yelp_id": b.get("id"),
        "name": b.get("name"),
        "rating": b.get("rating"),
        "price_tier": price_str_to_tier(b.get("price")),
        "price_label": b.get("price") or "",
        "cuisine": ", ".join(cats[:3]),
        "address": addr,
        "distance_mi": round(dist_m / 1609.344, 1) if isinstance(dist_m, (int, float)) else None,
    }


def search_yelp(lat: float, lon: float, radius_km: float, api_key: str,
                http_get=None) -> list[dict]:
    """One live Yelp Fusion search. Raises on HTTP/API errors."""
    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "radius": min(int(radius_km * 1000), 40000),  # Yelp caps at 40km
        "categories": "restaurants",
        "limit": 50,
        "sort_by": "rating",
    })
    data = (http_get or _http_get)(f"{YELP_SEARCH_URL}?{params}", api_key)
    if "error" in data:
        raise RuntimeError(f"Yelp API error: {data['error'].get('description', data['error'])}")
    return [_business_to_card(b) for b in data.get("businesses", [])]


def _cache_key(lat: float, lon: float, radius_km: float) -> tuple[float, float, float]:
    return round(lat, 3), round(lon, 3), round(radius_km, 1)


def get_cached(session: Session, lat: float, lon: float,
               radius_km: float) -> tuple[list[dict] | None, datetime | None]:
    """Return (businesses, cached_at) if a fresh cache row exists, else (None, None)."""
    clat, clon, crad = _cache_key(lat, lon, radius_km)
    row = (
        session.query(DiscoverCache)
        .filter(
            DiscoverCache.lat == clat,
            DiscoverCache.lon == clon,
            DiscoverCache.radius_km == crad,
        )
        .order_by(DiscoverCache.created_at.desc())
        .first()
    )
    if row is None:
        return None, None
    age = datetime.utcnow() - (row.created_at or datetime.min)
    if age > timedelta(hours=CACHE_TTL_HOURS):
        return None, None
    return json.loads(row.payload), row.created_at


def store_cache(session: Session, lat: float, lon: float, radius_km: float,
                businesses: list[dict]) -> None:
    clat, clon, crad = _cache_key(lat, lon, radius_km)
    # replace any older row for this key
    session.query(DiscoverCache).filter(
        DiscoverCache.lat == clat,
        DiscoverCache.lon == clon,
        DiscoverCache.radius_km == crad,
    ).delete()
    session.add(
        DiscoverCache(
            lat=clat, lon=clon, radius_km=crad,
            payload=json.dumps(businesses),
        )
    )
    session.commit()


def discover(session: Session, lat: float, lon: float, radius_km: float,
             api_key: str, refresh: bool = False,
             http_get=None) -> dict:
    """Nearby businesses, hiding ones already in the restaurant list.

    Returns {"businesses": [...], "cached": bool, "cached_at": iso|None}.
    Each business gets "already_added": True when a normalized-name match
    exists in the restaurant list.
    """
    cached_at = None
    if not refresh:
        businesses, cached_at = get_cached(session, lat, lon, radius_km)
        if businesses is not None:
            cached = True
        else:
            businesses = search_yelp(lat, lon, radius_km, api_key, http_get=http_get)
            store_cache(session, lat, lon, radius_km, businesses)
            cached = False
    else:
        businesses = search_yelp(lat, lon, radius_km, api_key, http_get=http_get)
        store_cache(session, lat, lon, radius_km, businesses)
        cached = False

    known = {norm_name(r.name) for r in session.query(Restaurant).all()}
    out = []
    for b in businesses:
        if norm_name(b.get("name")) in known:
            continue  # already in his list — hide it
        out.append(b)
    return {
        "businesses": out,
        "cached": cached,
        "cached_at": cached_at.isoformat(timespec="minutes") if cached_at else None,
    }
