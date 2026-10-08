"""Nearby discovery: Yelp Fusion when a key is configured, else OpenStreetMap.

Yelp's free self-serve app creation went away (greyed-out create button, docs
now push paid plans), so OpenStreetMap/Overpass is the default provider — zero
setup, no key, no account. A Yelp key in Settings still switches to Yelp for
ratings.

Results are cached 24h per provider+location+radius. Businesses already in the
restaurant list are hidden by normalized name match.
"""

from __future__ import annotations

import json
import logging
import math
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import DiscoverCache, Restaurant

logger = logging.getLogger(__name__)

YELP_SEARCH_URL = "https://api.yelp.com/v3/businesses/search"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.nchc.org.tw/api/interpreter",
]
OVERPASS_URL = OVERPASS_URLS[0]  # primary; search_overpass falls back through the rest
CACHE_TTL_HOURS = 24
HTTP_TIMEOUT = 20  # seconds (Yelp)
OVERPASS_TIMEOUT = 25  # seconds — Overpass can be slow on big areas
OVERPASS_LIMIT = 60  # elements requested; we display up to DISPLAY_LIMIT
DISPLAY_LIMIT = 50

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


# ---------- Yelp ----------

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


# ---------- OpenStreetMap / Overpass ----------

CUISINE_MAP = {
    "pizza": "Pizza", "burger": "Burgers", "chicken": "Chicken",
    "mexican": "Mexican", "chinese": "Chinese", "italian": "Italian",
    "japanese": "Japanese", "sushi": "Sushi", "indian": "Indian",
    "thai": "Thai", "bbq": "BBQ", "sandwich": "Sandwiches",
    "breakfast": "Breakfast", "ice_cream": "Dessert", "coffee_shop": "Coffee",
    "seafood": "Seafood", "steak_house": "Steakhouse", "vietnamese": "Vietnamese",
    "korean": "Korean", "kebab": "Kebab", "bakery": "Bakery", "diner": "Diner",
    "noodle": "Noodles", "ramen": "Ramen", "wings": "Wings",
}


def humanize_cuisine(tag: str | None) -> str:
    """OSM cuisine tag ('pizza;italian') -> 'Pizza, Italian'."""
    if not tag:
        return ""
    out = []
    for part in tag.split(";"):
        part = part.strip().lower()
        if not part:
            continue
        out.append(CUISINE_MAP.get(part, part.replace("_", " ").title()))
    return ", ".join(out[:3])


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles."""
    r = 3958.8  # earth radius, miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _http_post(url: str, data: bytes) -> dict:
    """Real Overpass call. `http_post` is injectable for tests."""
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "FoodFriday/1.0 (personal use)",
        },
    )
    with urllib.request.urlopen(req, timeout=OVERPASS_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _osm_to_card(el: dict, lat: float, lon: float) -> dict | None:
    """One Overpass element -> discover card shape. None when unusable."""
    tags = el.get("tags") or {}
    name = (tags.get("name") or "").strip()
    if not name:
        return None  # unnamed node — not useful as a discover result
    if el.get("type") == "node":
        elat, elon = el.get("lat"), el.get("lon")
    else:
        center = el.get("center") or {}
        elat, elon = center.get("lat"), center.get("lon")
    dist = (
        round(haversine_mi(lat, lon, elat, elon), 1)
        if isinstance(elat, (int, float)) and isinstance(elon, (int, float))
        else None
    )
    addr = " ".join(
        p for p in (tags.get("addr:housenumber"), tags.get("addr:street")) if p
    ) or None
    cuisine = humanize_cuisine(tags.get("cuisine"))
    if not cuisine and tags.get("amenity") == "fast_food":
        cuisine = "Fast Food"
    return {
        "osm_id": f"{el.get('type')}/{el.get('id')}",
        "name": name,
        "rating": None,  # OSM has no ratings
        "price_tier": 2,
        "price_label": "",
        "cuisine": cuisine or None,
        "address": addr,
        "distance_mi": dist,
    }


def search_overpass(lat: float, lon: float, radius_km: float,
                    http_post=None) -> list[dict]:
    """One live Overpass query for restaurants/fast_food. Raises on errors."""
    radius_m = min(int(radius_km * 1000), 50000)
    ql = (
        "[out:json][timeout:25];"
        f'nwr["amenity"~"restaurant|fast_food"]'
        f"(around:{radius_m},{lat},{lon});"
        f"out center {OVERPASS_LIMIT};"
    )
    body = urllib.parse.urlencode({"data": ql}).encode("utf-8")
    last_exc: Exception | None = None
    # The public Overpass instances are flaky (504s under load) — try each in
    # order and only fail if all of them are down.
    for url in OVERPASS_URLS:
        try:
            data = (http_post or _http_post)(url, body)
            break
        except Exception as exc:  # noqa: BLE001 — any failure moves to the next mirror
            last_exc = exc
            continue
    else:
        exc = last_exc
        # urllib raises URLError (a OSError) on timeouts; normalize the message
        if exc is not None and "timed out" in str(exc).lower():
            raise RuntimeError("OpenStreetMap search timed out — try again.") from exc
        raise RuntimeError(f"OpenStreetMap search failed: {exc}") from exc
    elements = data.get("elements")
    if not isinstance(elements, list):
        raise RuntimeError("OpenStreetMap returned an unexpected response.")
    cards = []
    for el in elements:
        card = _osm_to_card(el, lat, lon)
        if card is not None:
            cards.append(card)
    return cards[:DISPLAY_LIMIT]


# ---------- cache (provider-aware) ----------

def _cache_key(lat: float, lon: float, radius_km: float,
               provider: str) -> tuple[str, float, float, float]:
    return provider, round(lat, 3), round(lon, 3), round(radius_km, 1)


def _provider_clause(provider: str):
    # legacy rows predate the provider column (NULL) — those were Yelp searches
    if provider == "yelp":
        return func.coalesce(DiscoverCache.provider, "yelp") == "yelp"
    return DiscoverCache.provider == provider


def get_cached(session: Session, lat: float, lon: float, radius_km: float,
               provider: str = "yelp") -> tuple[list[dict] | None, datetime | None]:
    """Return (businesses, cached_at) if a fresh cache row exists, else (None, None)."""
    prov, clat, clon, crad = _cache_key(lat, lon, radius_km, provider)
    row = (
        session.query(DiscoverCache)
        .filter(
            _provider_clause(prov),
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
                businesses: list[dict], provider: str = "yelp") -> None:
    prov, clat, clon, crad = _cache_key(lat, lon, radius_km, provider)
    # replace any older row for this key
    session.query(DiscoverCache).filter(
        _provider_clause(prov),
        DiscoverCache.lat == clat,
        DiscoverCache.lon == clon,
        DiscoverCache.radius_km == crad,
    ).delete()
    session.add(
        DiscoverCache(
            provider=prov,
            lat=clat, lon=clon, radius_km=crad,
            payload=json.dumps(businesses),
        )
    )
    session.commit()


# ---------- entry point ----------

def discover(session: Session, lat: float, lon: float, radius_km: float,
             api_key: str | None = None, refresh: bool = False,
             http_get=None, http_post=None) -> dict:
    """Nearby businesses, hiding ones already in the restaurant list.

    Provider is Yelp when an API key is configured, else OpenStreetMap
    (no key needed). Returns {"businesses": [...], "cached": bool,
    "cached_at": iso|None, "provider": "yelp"|"osm"}.
    """
    provider = "yelp" if api_key else "osm"
    cached_at = None
    if not refresh:
        businesses, cached_at = get_cached(session, lat, lon, radius_km, provider)
        if businesses is not None:
            cached = True
        else:
            businesses = _live_search(lat, lon, radius_km, api_key, provider,
                                      http_get=http_get, http_post=http_post)
            store_cache(session, lat, lon, radius_km, businesses, provider=provider)
            cached = False
    else:
        businesses = _live_search(lat, lon, radius_km, api_key, provider,
                                  http_get=http_get, http_post=http_post)
        store_cache(session, lat, lon, radius_km, businesses, provider=provider)
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
        "provider": provider,
    }


def _live_search(lat: float, lon: float, radius_km: float, api_key: str | None,
                 provider: str, http_get=None, http_post=None) -> list[dict]:
    if provider == "yelp":
        return search_yelp(lat, lon, radius_km, api_key, http_get=http_get)
    return search_overpass(lat, lon, radius_km, http_post=http_post)
