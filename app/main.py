"""FoodFriday — Friday dinner decider (working name; will change)."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import pages, picker
from app import crave as crave_mod
from app import openrouter_models
from app.crave import K_CRAVE_FREE_ONLY, K_CRAVE_KEY, K_CRAVE_MODEL
from app.database import get_session, init_db
from app.discover import (
    K_AVOID_REPEAT_CUISINE,
    K_HOME_LAT,
    K_HOME_LON,
    K_YELP_KEY,
    discover as discover_nearby,
    norm_name as discover_norm,
)
from app.models import Deal, EmailAccount, Restaurant, Setting, Visit
from app.nudge import (
    K_NUDGE_ENABLED,
    K_NUDGE_LAST_RESULT,
    K_NUDGE_TIME,
    K_WEBHOOK,
    send_nudge as send_discord_nudge,
)
from app.version import APP_NAME, VERSION
from app import scheduler as deal_scheduler
from app.deal_scan import (
    K_ENABLED,
    K_LAST_RESULT,
    K_LAST_RUN,
    K_R_ENABLED,
    K_R_LAST_RESULT,
    K_R_LAST_RUN,
    K_STATUS,
    K_TIME,
    get_setting,
    list_accounts,
    scan_enabled,
    set_setting,
    try_start_scan,
)

logger = logging.getLogger(__name__)

MASKED = "********"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    deal_scheduler.start()
    yield
    deal_scheduler.shutdown()


app = FastAPI(title=APP_NAME, version=VERSION, lifespan=lifespan)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- helpers ----------

def _norm_name(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def restaurant_dict(r: Restaurant, visit_count: int, last_visit: date | None) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "cuisine": r.cuisine,
        "price_tier": r.price_tier or 2,
        "notes": r.notes,
        "address": r.address,
        "favorite": bool(r.favorite),
        "include_in_picks": r.include_in_picks is not False,
        "track_visits": r.track_visits is not False,
        "visit_count": visit_count,
        "last_visit": last_visit.isoformat() if last_visit else None,
    }


def visit_stats(session: Session, counted_only: bool = False) -> tuple[dict[int, int], dict[int, date]]:
    """Per-restaurant visit counts and last-visit dates.

    counted_only=True restricts to visits that count toward the Friday picks
    (Visit.exclude_from_picks is not True) — the picker uses this; the
    restaurant list and spending dashboard use all visits.
    """
    q = session.query(Visit)
    if counted_only:
        q = q.filter(Visit.exclude_from_picks.isnot(True))
    counts = {
        row[0]: row[1]
        for row in q.with_entities(Visit.restaurant_id, func.count(Visit.id))
        .group_by(Visit.restaurant_id)
        .all()
    }
    lasts = {
        row[0]: row[1]
        for row in q.with_entities(Visit.restaurant_id, func.max(Visit.visited_at))
        .group_by(Visit.restaurant_id)
        .all()
    }
    return counts, lasts


# ---------- pages ----------

@app.get("/", response_class=HTMLResponse)
def home():
    return pages.home_page()


@app.get("/restaurants", response_class=HTMLResponse)
def restaurants_page(session: Session = Depends(get_session)):
    counts, lasts = visit_stats(session)
    rows = [
        restaurant_dict(r, counts.get(r.id, 0), lasts.get(r.id))
        for r in session.query(Restaurant).order_by(Restaurant.name).all()
    ]
    deals = [deal_dict(d) for d in session.query(Deal).all()]
    deals.sort(key=lambda d: (not d["active"], d["valid_until"] or "9999", d["title"]))
    return pages.restaurants_page(rows, deals)


@app.get("/history", response_class=HTMLResponse)
def history_page(session: Session = Depends(get_session)):
    visits = session.query(Visit).order_by(Visit.visited_at.desc(), Visit.id.desc()).all()
    rows = [
        {
            "id": v.id,
            "visited_at": v.visited_at.isoformat(),
            "restaurant_name": v.restaurant.name if v.restaurant else "?",
            "total": v.total,
            "source": v.source,
            "exclude_from_picks": v.exclude_from_picks is True,
        }
        for v in visits
    ]
    return pages.history_page(rows)


@app.get("/import", response_class=HTMLResponse)
def import_page():
    return pages.import_page()


@app.get("/discover", response_class=HTMLResponse)
def discover_page():
    return pages.discover_page()


@app.get("/spending", response_class=HTMLResponse)
def spending_page(session: Session = Depends(get_session)):
    return pages.spending_page(spending_aggregates(session))


@app.get("/settings", response_class=HTMLResponse)
def settings_page(session: Session = Depends(get_session)):
    return pages.settings_page(settings_view(session))


# ---------- API ----------

@app.get("/api/health")
def health():
    return {"ok": True, "app": APP_NAME, "version": VERSION}


class RestaurantIn(BaseModel):
    name: str
    cuisine: str | None = None
    price_tier: int | None = 2
    notes: str | None = None
    address: str | None = None
    favorite: bool | None = False
    include_in_picks: bool | None = True
    track_visits: bool | None = True


@app.get("/api/restaurants")
def list_restaurants(session: Session = Depends(get_session)):
    counts, lasts = visit_stats(session)
    return [
        restaurant_dict(r, counts.get(r.id, 0), lasts.get(r.id))
        for r in session.query(Restaurant).order_by(Restaurant.name).all()
    ]


@app.post("/api/restaurants", status_code=201)
def create_restaurant(payload: RestaurantIn, session: Session = Depends(get_session)):
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(400, "Name is required")
    tier = payload.price_tier if payload.price_tier in (1, 2, 3) else 2
    r = Restaurant(
        name=name,
        cuisine=(payload.cuisine or "").strip() or None,
        price_tier=tier,
        notes=(payload.notes or "").strip() or None,
        address=(payload.address or "").strip() or None,
        favorite=bool(payload.favorite),
        include_in_picks=False if payload.include_in_picks is False else True,
    )
    session.add(r)
    session.commit()
    session.refresh(r)
    return restaurant_dict(r, 0, None)


@app.put("/api/restaurants/{rid}")
def update_restaurant(rid: int, payload: RestaurantIn, session: Session = Depends(get_session)):
    r = session.get(Restaurant, rid)
    if not r:
        raise HTTPException(404, "Restaurant not found")
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(400, "Name is required")
    r.name = name
    r.cuisine = (payload.cuisine or "").strip() or None
    r.price_tier = payload.price_tier if payload.price_tier in (1, 2, 3) else 2
    r.notes = (payload.notes or "").strip() or None
    r.address = (payload.address or "").strip() or None
    if payload.favorite is not None:
        r.favorite = bool(payload.favorite)
    if payload.include_in_picks is not None:
        r.include_in_picks = bool(payload.include_in_picks)
    if payload.track_visits is not None:
        r.track_visits = bool(payload.track_visits)
    session.commit()
    return {"ok": True}


@app.delete("/api/restaurants/{rid}", status_code=204)
def delete_restaurant(rid: int, session: Session = Depends(get_session)):
    r = session.get(Restaurant, rid)
    if not r:
        raise HTTPException(404, "Restaurant not found")
    session.delete(r)
    session.commit()
    return None


@app.post("/api/restaurants/{rid}/favorite")
def toggle_favorite(rid: int, session: Session = Depends(get_session)):
    r = session.get(Restaurant, rid)
    if not r:
        raise HTTPException(404, "Restaurant not found")
    r.favorite = not r.favorite
    session.commit()
    return {"favorite": bool(r.favorite)}


@app.post("/api/restaurants/{rid}/in-picks")
def toggle_in_picks(rid: int, session: Session = Depends(get_session)):
    r = session.get(Restaurant, rid)
    if not r:
        raise HTTPException(404, "Restaurant not found")
    r.include_in_picks = not (r.include_in_picks is not False)
    session.commit()
    return {"include_in_picks": r.include_in_picks is not False}


@app.post("/api/restaurants/{rid}/track-visits")
def toggle_track_visits(rid: int, session: Session = Depends(get_session)):
    """Skip-list for the receipt scanner: when off, no visits are auto-logged
    for this restaurant (e.g. the kid's McDonald's runs)."""
    r = session.get(Restaurant, rid)
    if not r:
        raise HTTPException(404, "Restaurant not found")
    r.track_visits = not (r.track_visits is not False)
    session.commit()
    return {"track_visits": r.track_visits is not False}


class VisitIn(BaseModel):
    restaurant_id: int
    visited_at: date | None = None
    total: float | None = None


@app.get("/api/visits")
def list_visits(session: Session = Depends(get_session)):
    visits = session.query(Visit).order_by(Visit.visited_at.desc(), Visit.id.desc()).all()
    return [
        {
            "id": v.id,
            "restaurant_id": v.restaurant_id,
            "restaurant_name": v.restaurant.name if v.restaurant else "?",
            "visited_at": v.visited_at.isoformat(),
            "total": v.total,
            "source": v.source,
            "exclude_from_picks": v.exclude_from_picks is True,
        }
        for v in visits
    ]


@app.post("/api/visits", status_code=201)
def log_visit(payload: VisitIn, session: Session = Depends(get_session)):
    r = session.get(Restaurant, payload.restaurant_id)
    if not r:
        raise HTTPException(404, "Restaurant not found")
    v = Visit(
        restaurant_id=r.id,
        visited_at=payload.visited_at or date.today(),
        total=payload.total,
        source="manual",
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    return {"id": v.id, "visited_at": v.visited_at.isoformat()}


@app.delete("/api/visits/{vid}", status_code=204)
def delete_visit(vid: int, session: Session = Depends(get_session)):
    v = session.get(Visit, vid)
    if not v:
        raise HTTPException(404, "Visit not found")
    session.delete(v)
    session.commit()
    return None


class VisitPatchIn(BaseModel):
    # None = toggle the current value
    exclude_from_picks: bool | None = None


@app.patch("/api/visits/{vid}")
def patch_visit(vid: int, payload: VisitPatchIn, session: Session = Depends(get_session)):
    """Toggle (or set) whether a visit counts toward the Friday picks.

    Picker-only: the visit stays in History and Spending either way.
    """
    v = session.get(Visit, vid)
    if not v:
        raise HTTPException(404, "Visit not found")
    if payload.exclude_from_picks is None:
        v.exclude_from_picks = not (v.exclude_from_picks is True)
    else:
        v.exclude_from_picks = payload.exclude_from_picks
    session.commit()
    return {"id": v.id, "exclude_from_picks": v.exclude_from_picks is True}


class PickIn(BaseModel):
    keep_ids: list[int] = []
    veto_ids: list[int] = []
    mode: str = "friday"          # "friday" | "new"
    include_cuisine: bool = False  # override the avoid-repeat-cuisine exclusion


def _deal_and_item_inputs(session: Session, today: date):
    """Active deals and counted visit items, keyed by restaurant id.

    Shared by the Friday picker and Crave suggestions so both rank with
    the same boosts (active deal x1.5, item-keyword match x1.25).
    """
    deals_by_id: dict[int, list[dict]] = {}
    for d in session.query(Deal).all():
        dd = {
            "title": d.title,
            "valid_from": d.valid_from,
            "valid_until": d.valid_until,
            "created_at": d.created_at,
            "item_keywords": d.item_keywords,
        }
        if d.restaurant_id is not None and picker.is_deal_active(dd, today):
            deals_by_id.setdefault(d.restaurant_id, []).append(dd)

    items_by_id: dict[int, list[str]] = {}
    for v in (
        session.query(Visit)
        .filter(Visit.items.isnot(None), Visit.exclude_from_picks.isnot(True))
        .all()
    ):
        items_by_id.setdefault(v.restaurant_id, []).extend(
            [i.strip() for i in (v.items or "").splitlines() if i.strip()]
        )
    return deals_by_id, items_by_id


def build_pick_response(session: Session, mode: str = "friday",
                        keep_ids: tuple[int, ...] = (),
                        veto_ids: tuple[int, ...] = (),
                        include_cuisine: bool = False) -> dict:
    """Shared pick builder for /api/pick and the Friday Discord nudge.

    mode="new": only restaurants with zero visits ("somewhere new").
    Otherwise the normal weighted Friday draw, optionally excluding the
    cuisine of the most recent logged visit (avoid_repeat_cuisine setting).

    All picker inputs (visit counts, last-visit dates, deal item matching,
    cuisine rotation) consider only visits that count toward picks —
    Visit.exclude_from_picks rows are ignored here (they still show in
    History and Spending).
    """
    counts, lasts = visit_stats(session, counted_only=True)
    restaurants = session.query(Restaurant).all()
    today = date.today()
    deals_by_id, items_by_id = _deal_and_item_inputs(session, today)

    if mode == "new":
        dicts = [
            {
                "id": r.id,
                "name": r.name,
                "cuisine": r.cuisine,
                "price_tier": r.price_tier or 2,
                "favorite": bool(r.favorite),
                "yelp_rating": r.yelp_rating,
            }
            for r in restaurants
            if r.include_in_picks is not False and counts.get(r.id, 0) == 0
        ]
        stats = picker.newcomer_stats(dicts)
        picks = picker.pick_three(stats, keep_ids, veto_ids)
        return {
            "mode": "new",
            "picks": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "cuisine": p["cuisine"],
                    "price_tier": p["price_tier"],
                    "favorite": p["favorite"],
                    "last_visit": None,
                    "reason": "Never tried",
                    "deal_titles": [],
                }
                for p in picks
            ],
        }

    dicts = [
        {
            "id": r.id,
            "name": r.name,
            "cuisine": r.cuisine,
            "price_tier": r.price_tier or 2,
            "favorite": bool(r.favorite),
        }
        for r in restaurants
        if r.include_in_picks is not False
    ]

    # Cuisine rotation: skip the cuisine of the most recent COUNTED visit
    # (excluded trips — kid's runs, breakfast pitstops — don't set the rotation).
    skipped_cuisine: str | None = None
    if get_setting(session, K_AVOID_REPEAT_CUISINE, "1") == "1" and not include_cuisine:
        latest = (
            session.query(Visit)
            .filter(Visit.exclude_from_picks.isnot(True))
            .order_by(Visit.visited_at.desc(), Visit.id.desc())
            .first()
        )
        if latest and latest.restaurant and (latest.restaurant.cuisine or "").strip():
            skipped_cuisine = latest.restaurant.cuisine.strip()
            skip_norm = discover_norm(skipped_cuisine)
            dicts = [d for d in dicts if discover_norm(d["cuisine"]) != skip_norm]

    last_by_id = {rid: d for rid, d in lasts.items()}

    stats = picker.candidate_stats(dicts, last_by_id, today=today,
                                   deals_by_id=deals_by_id, items_by_id=items_by_id)
    picks = picker.pick_three(stats, keep_ids, veto_ids)
    out = {
        "mode": "friday",
        "picks": [
            {
                "id": p["id"],
                "name": p["name"],
                "cuisine": p["cuisine"],
                "price_tier": p["price_tier"],
                "favorite": p["favorite"],
                "last_visit": p["last_visit"].isoformat() if p["last_visit"] else None,
                "reason": p["reason"],
                "deal_titles": p.get("deal_titles", []),
            }
            for p in picks
        ],
    }
    if skipped_cuisine:
        out["skipped_cuisine"] = skipped_cuisine
    return out


@app.post("/api/pick")
def pick(payload: PickIn, session: Session = Depends(get_session)):
    if payload.mode not in ("friday", "new"):
        raise HTTPException(400, 'mode must be "friday" or "new"')
    return build_pick_response(
        session,
        mode=payload.mode,
        keep_ids=tuple(payload.keep_ids),
        veto_ids=tuple(payload.veto_ids),
        include_cuisine=payload.include_cuisine,
    )


# ---------- crave (AI "what am I feeling?") ----------


@app.get("/api/openrouter-models")
def api_openrouter_models(free_only: bool = True):
    """Text/chat models from OpenRouter's public catalog (no auth needed).

    Mirrors Fauna's vision-model endpoint. Never fails hard — returns an
    empty list when the catalog is unreachable and the Settings UI falls
    back to a plain text field.
    """
    models = openrouter_models.get_chat_models()
    if free_only:
        models = [m for m in models if m["free"]]
    return {"models": models}


class CraveIn(BaseModel):
    text: str


@app.post("/api/crave")
def api_crave(payload: CraveIn, session: Session = Depends(get_session)):
    """3 grounded suggestions for a craving.

    An LLM (or the keyword fallback without a key) turns the text into
    structured filters; the app matches those against his restaurants +
    nearby spots. Suggestions only ever come from real data.
    """
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(400, "Tell me what you're feeling first.")
    if len(text) > 300:
        raise HTTPException(400, "Keep it under 300 characters.")
    try:
        return crave_mod.suggest(session, text)
    except Exception as exc:
        logger.warning("crave failed: %s", exc)
        raise HTTPException(502, f"Couldn't come up with suggestions: {exc}")


# ---------- deals ----------

def deal_dict(d: Deal) -> dict:
    active = picker.is_deal_active(
        {
            "valid_from": d.valid_from,
            "valid_until": d.valid_until,
            "created_at": d.created_at,
        },
        date.today(),
    )
    return {
        "id": d.id,
        "restaurant_id": d.restaurant_id,
        "restaurant_name": d.restaurant.name if d.restaurant else None,
        "title": d.title,
        "description": d.description,
        "valid_from": d.valid_from.isoformat() if d.valid_from else None,
        "valid_until": d.valid_until.isoformat() if d.valid_until else None,
        "item_keywords": d.item_keywords,
        "source": d.source,
        "active": active,
    }


def _norm_keywords(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if str(v).strip()]
    else:
        parts = [p.strip() for p in str(value).split(",") if p.strip()]
    return ", ".join(parts) if parts else None


class DealIn(BaseModel):
    restaurant_id: int | None = None
    title: str
    description: str | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    item_keywords: str | list[str] | None = None
    source: str | None = "manual"


@app.get("/api/deals")
def list_deals(restaurant_id: int | None = None, session: Session = Depends(get_session)):
    q = session.query(Deal)
    if restaurant_id is not None:
        q = q.filter(Deal.restaurant_id == restaurant_id)
    deals = [deal_dict(d) for d in q.all()]
    deals.sort(key=lambda d: (not d["active"], d["valid_until"] or "9999", d["title"]))
    return deals


@app.post("/api/deals", status_code=201)
def create_deal(payload: DealIn, session: Session = Depends(get_session)):
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(400, "Title is required")
    if payload.restaurant_id is not None and not session.get(Restaurant, payload.restaurant_id):
        raise HTTPException(404, "Restaurant not found")
    d = Deal(
        restaurant_id=payload.restaurant_id,
        title=title,
        description=(payload.description or "").strip() or None,
        valid_from=payload.valid_from,
        valid_until=payload.valid_until,
        item_keywords=_norm_keywords(payload.item_keywords),
        source=(payload.source or "manual").strip() or "manual",
    )
    session.add(d)
    session.commit()
    session.refresh(d)
    return deal_dict(d)


@app.put("/api/deals/{did}")
def update_deal(did: int, payload: DealIn, session: Session = Depends(get_session)):
    d = session.get(Deal, did)
    if not d:
        raise HTTPException(404, "Deal not found")
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(400, "Title is required")
    if payload.restaurant_id is not None and not session.get(Restaurant, payload.restaurant_id):
        raise HTTPException(404, "Restaurant not found")
    d.restaurant_id = payload.restaurant_id
    d.title = title
    d.description = (payload.description or "").strip() or None
    d.valid_from = payload.valid_from
    d.valid_until = payload.valid_until
    d.item_keywords = _norm_keywords(payload.item_keywords)
    if payload.source:
        d.source = payload.source.strip() or "manual"
    session.commit()
    return deal_dict(d)


@app.delete("/api/deals/{did}", status_code=204)
def delete_deal(did: int, session: Session = Depends(get_session)):
    d = session.get(Deal, did)
    if not d:
        raise HTTPException(404, "Deal not found")
    session.delete(d)
    session.commit()
    return None



# ---------- settings (deal scanner) ----------

def settings_view(session: Session) -> dict:
    """Settings for display/API — secrets are never returned."""
    yelp_key = get_setting(session, K_YELP_KEY)
    webhook = get_setting(session, K_WEBHOOK)
    accounts = [
        {
            "id": a.id,
            "label": a.label or "",
            "address": a.address,
            "password_set": True,
        }
        for a in list_accounts(session)
    ]
    return {
        "email_accounts": accounts,
        "deal_scan_enabled": get_setting(session, K_ENABLED, "1") == "1",
        "deal_scan_time": get_setting(session, K_TIME, "07:00") or "07:00",
        "deal_scan_last_run": get_setting(session, K_LAST_RUN),
        "deal_scan_last_result": get_setting(session, K_LAST_RESULT),
        "deal_scan_status": get_setting(session, K_STATUS, "idle") or "idle",
        "receipt_scan_enabled": get_setting(session, K_R_ENABLED, "1") == "1",
        "receipt_scan_last_run": get_setting(session, K_R_LAST_RUN),
        "receipt_scan_last_result": get_setting(session, K_R_LAST_RESULT),
        "configured": scan_enabled(session),
        # discover
        "yelp_api_key_set": bool(yelp_key),
        "yelp_api_key": MASKED if yelp_key else "",
        "home_lat": get_setting(session, K_HOME_LAT) or "",
        "home_lon": get_setting(session, K_HOME_LON) or "",
        # friday nudge
        "discord_webhook_set": bool(webhook),
        "discord_webhook_url": MASKED if webhook else "",
        "friday_nudge_enabled": get_setting(session, K_NUDGE_ENABLED, "0") == "1",
        "friday_nudge_time": get_setting(session, K_NUDGE_TIME, "10:00") or "10:00",
        "friday_nudge_last_result": get_setting(session, K_NUDGE_LAST_RESULT),
        # picker
        "avoid_repeat_cuisine": get_setting(session, K_AVOID_REPEAT_CUISINE, "1") == "1",
        # crave (AI suggestions)
        "openrouter_api_key_set": bool(get_setting(session, K_CRAVE_KEY)),
        "crave_model": get_setting(session, K_CRAVE_MODEL) or "",
        "crave_model_free_only": get_setting(session, K_CRAVE_FREE_ONLY, "1") == "1",
    }


class SettingsIn(BaseModel):
    deal_scan_enabled: bool | None = None
    deal_scan_time: str | None = None  # "HH:MM"
    receipt_scan_enabled: bool | None = None
    yelp_api_key: str | None = None  # write-only; ignored when empty/masked
    home_lat: str | None = None
    home_lon: str | None = None
    discord_webhook_url: str | None = None  # write-only; ignored when empty/masked
    friday_nudge_enabled: bool | None = None
    friday_nudge_time: str | None = None  # "HH:MM"
    avoid_repeat_cuisine: bool | None = None
    # crave (AI suggestions)
    openrouter_api_key: str | None = None  # write-only; empty/masked value keeps the saved key
    crave_model: str | None = None
    crave_model_free_only: bool | None = None


def _validate_hhmm(value: str, label: str) -> str:
    import re

    t = value.strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", t):
        raise HTTPException(400, f"{label} must be HH:MM (24h)")
    h, m = int(t.split(":")[0]), int(t.split(":")[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise HTTPException(400, f"{label} must be a real time of day")
    return f"{h:02d}:{m:02d}"


@app.get("/api/settings")
def get_settings(session: Session = Depends(get_session)):
    return settings_view(session)


@app.put("/api/settings")
def update_settings(payload: SettingsIn, session: Session = Depends(get_session)):
    if payload.deal_scan_enabled is not None:
        set_setting(session, K_ENABLED, "1" if payload.deal_scan_enabled else "0")
    if payload.deal_scan_time is not None:
        set_setting(session, K_TIME, _validate_hhmm(payload.deal_scan_time, "Scan time"))
    if payload.receipt_scan_enabled is not None:
        set_setting(session, K_R_ENABLED, "1" if payload.receipt_scan_enabled else "0")
    # discover
    if payload.yelp_api_key and payload.yelp_api_key != MASKED:
        set_setting(session, K_YELP_KEY, payload.yelp_api_key.strip() or None)
    if payload.home_lat is not None:
        lat = _parse_float(payload.home_lat)
        set_setting(session, K_HOME_LAT, None if lat is None else str(lat))
    if payload.home_lon is not None:
        lon = _parse_float(payload.home_lon)
        set_setting(session, K_HOME_LON, None if lon is None else str(lon))
    # friday nudge
    if payload.discord_webhook_url and payload.discord_webhook_url != MASKED:
        set_setting(session, K_WEBHOOK, payload.discord_webhook_url.strip() or None)
    if payload.friday_nudge_enabled is not None:
        set_setting(session, K_NUDGE_ENABLED, "1" if payload.friday_nudge_enabled else "0")
    if payload.friday_nudge_time is not None:
        set_setting(session, K_NUDGE_TIME, _validate_hhmm(payload.friday_nudge_time, "Nudge time"))
    # picker
    if payload.avoid_repeat_cuisine is not None:
        set_setting(session, K_AVOID_REPEAT_CUISINE, "1" if payload.avoid_repeat_cuisine else "0")
    # crave (AI suggestions)
    if payload.openrouter_api_key and payload.openrouter_api_key != MASKED:
        set_setting(session, K_CRAVE_KEY, payload.openrouter_api_key.strip() or None)
    if payload.crave_model is not None:
        set_setting(session, K_CRAVE_MODEL, payload.crave_model.strip() or None)
    if payload.crave_model_free_only is not None:
        set_setting(session, K_CRAVE_FREE_ONLY, "1" if payload.crave_model_free_only else "0")
    session.commit()
    deal_scheduler.schedule_from_settings()
    deal_scheduler.schedule_nudge_from_settings()
    return settings_view(session)


@app.post("/api/settings/clear-gmail")
def clear_gmail(session: Session = Depends(get_session)):
    """Forget all stored email accounts. The scanner stops until one is added."""
    session.query(EmailAccount).delete()
    session.commit()
    deal_scheduler.schedule_from_settings()
    return settings_view(session)


class EmailAccountIn(BaseModel):
    label: str | None = None
    address: str
    app_password: str  # write-only


@app.post("/api/settings/email-accounts", status_code=201)
def add_email_account(payload: EmailAccountIn, session: Session = Depends(get_session)):
    """Add a Gmail account for the deal/receipt scanner (app password per account)."""
    address = (payload.address or "").strip()
    password = (payload.app_password or "").strip()
    if not address or not password:
        raise HTTPException(400, "address and app password are required")
    if session.query(EmailAccount).filter(EmailAccount.address == address).first():
        raise HTTPException(409, "that address is already added")
    acct = EmailAccount(
        label=(payload.label or "").strip() or None,
        address=address,
        app_password=password,
    )
    session.add(acct)
    session.commit()
    session.refresh(acct)
    deal_scheduler.schedule_from_settings()
    return {"id": acct.id, "label": acct.label or "", "address": acct.address,
            "password_set": True}


@app.delete("/api/settings/email-accounts/{aid}", status_code=204)
def delete_email_account(aid: int, session: Session = Depends(get_session)):
    acct = session.get(EmailAccount, aid)
    if not acct:
        raise HTTPException(404, "Email account not found")
    session.delete(acct)
    session.commit()
    deal_scheduler.schedule_from_settings()
    return None


@app.post("/api/settings/clear-integrations")
def clear_integrations(session: Session = Depends(get_session)):
    """Forget the stored Yelp key and Discord webhook entirely."""
    set_setting(session, K_YELP_KEY, None)
    set_setting(session, K_WEBHOOK, None)
    session.commit()
    deal_scheduler.schedule_nudge_from_settings()
    return settings_view(session)


@app.post("/api/settings/test-nudge")
def test_nudge(session: Session = Depends(get_session)):
    """Send a test Friday-nudge message to the Discord webhook."""
    result = send_discord_nudge(session, test=True)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "nudge failed"))
    return {"ok": True, "result": result}


@app.post("/api/deals/scan", status_code=202)
def scan_deals_now():
    """Start a background Gmail scan and return immediately.

    Runs both phases: deal scanning + receipt scanning (if enabled).
    202 {"status": "started"} — the scan runs in a daemon thread; check the
    Settings page (or GET /api/settings) for the result lines.
    409 if a scan is already running; 400 if Gmail isn't configured.
    """
    from app.database import SessionLocal

    try:
        started = try_start_scan(SessionLocal)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    if not started:
        raise HTTPException(409, "a scan is already running")
    return {"status": "started"}


# ---------- discover (nearby: Yelp if keyed, else OpenStreetMap) ----------

def _parse_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@app.get("/api/discover")
def api_discover(radius_km: float = 10.0, refresh: bool = False,
                session: Session = Depends(get_session)):
    """Nearby restaurants, hiding ones already in the list.

    Uses Yelp when an API key is configured in Settings, otherwise
    OpenStreetMap/Overpass (no key needed). Results are cached 24h per
    provider+location+radius. `refresh=true` forces a live call.
    """
    api_key = get_setting(session, K_YELP_KEY)
    lat = _parse_float(get_setting(session, K_HOME_LAT))
    lon = _parse_float(get_setting(session, K_HOME_LON))
    if lat is None or lon is None:
        raise HTTPException(400, "Home location not set — add it in Settings.")
    radius_km = min(max(radius_km or 10.0, 1.0), 40.0)
    try:
        return discover_nearby(session, lat, lon, radius_km, api_key, refresh=refresh)
    except Exception as exc:
        logger.warning("discover failed: %s", exc)
        raise HTTPException(502, f"Nearby search failed: {exc}")


class DiscoverAddIn(BaseModel):
    yelp_id: str | None = None
    name: str
    cuisine: str | None = None
    price_tier: int | None = 2
    address: str | None = None
    rating: float | None = None


@app.post("/api/discover/add", status_code=201)
def api_discover_add(payload: DiscoverAddIn, session: Session = Depends(get_session)):
    """Add a Yelp business to the restaurant list. Idempotent by name."""
    result = _add_discovered(session, payload)
    return result


class DiscoverAddAllIn(BaseModel):
    businesses: list[DiscoverAddIn] = []


@app.post("/api/discover/add-all", status_code=201)
def api_discover_add_all(payload: DiscoverAddAllIn, session: Session = Depends(get_session)):
    """Add every discovered business to the restaurant list. Idempotent by name."""
    added = 0
    skipped = 0
    for b in payload.businesses:
        result = _add_discovered(session, b)
        if result.get("already"):
            skipped += 1
        else:
            added += 1
    return {"added": added, "skipped": skipped}


def _add_discovered(session: Session, payload: DiscoverAddIn) -> dict:
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(400, "Name is required")
    key = discover_norm(name)
    for r in session.query(Restaurant).all():
        if discover_norm(r.name) == key:
            counts, lasts = visit_stats(session)
            return {"already": True, **restaurant_dict(r, counts.get(r.id, 0), lasts.get(r.id))}
    tier = payload.price_tier if payload.price_tier in (1, 2, 3) else 2
    rating = None
    try:
        rating = float(payload.rating) if payload.rating is not None else None
        if rating is not None and not (0 <= rating <= 5):
            rating = None
    except (TypeError, ValueError):
        rating = None
    r = Restaurant(
        name=name,
        cuisine=(payload.cuisine or "").strip() or None,
        price_tier=tier,
        notes=None,
        address=(payload.address or "").strip() or None,
        favorite=False,
        include_in_picks=True,
        yelp_id=(payload.yelp_id or "").strip() or None,
        yelp_rating=rating,
    )
    session.add(r)
    session.commit()
    session.refresh(r)
    return {"already": False, **restaurant_dict(r, 0, None)}


# ---------- spending ----------

def spending_aggregates(session: Session) -> dict:
    """Spend stats for the /spending page. Visits without totals are excluded
    from the math (and counted separately)."""
    visits = (
        session.query(Visit)
        .order_by(Visit.visited_at.desc())
        .all()
    )
    with_totals = [v for v in visits if v.total is not None]
    without = len(visits) - len(with_totals)

    total = round(sum(v.total for v in with_totals), 2)
    avg = round(total / len(with_totals), 2) if with_totals else 0.0

    # monthly buckets, last 6 full-ish months (by calendar month)
    months: dict[str, float] = {}
    for v in with_totals:
        key = v.visited_at.strftime("%Y-%m")
        months[key] = months.get(key, 0.0) + v.total
    month_keys = sorted(months.keys())[-6:]
    monthly = [
        {
            "label": date.fromisoformat(k + "-01").strftime("%b %Y"),
            "total": round(months[k], 2),
        }
        for k in month_keys
    ]
    max_month = max((m["total"] for m in monthly), default=0.0)

    # per-restaurant top 10
    by_rest: dict[str, float] = {}
    by_rest_count: dict[str, int] = {}
    for v in with_totals:
        name = v.restaurant.name if v.restaurant else "?"
        by_rest[name] = by_rest.get(name, 0.0) + v.total
        by_rest_count[name] = by_rest_count.get(name, 0) + 1
    top_restaurants = sorted(by_rest.items(), key=lambda kv: -kv[1])[:10]

    # by cuisine
    by_cuisine: dict[str, float] = {}
    for v in with_totals:
        c = (v.restaurant.cuisine if v.restaurant and v.restaurant.cuisine else "Other").strip()
        by_cuisine[c] = by_cuisine.get(c, 0.0) + v.total
    cuisines = sorted(by_cuisine.items(), key=lambda kv: -kv[1])
    max_cuisine = max((t for _, t in cuisines), default=0.0)

    return {
        "total": total,
        "average": avg,
        "visit_count": len(with_totals),
        "without_totals": without,
        "monthly": monthly,
        "max_month": max_month,
        "top_restaurants": [
            {"name": n, "total": round(t, 2), "visits": by_rest_count[n]}
            for n, t in top_restaurants
        ],
        "cuisines": [{"name": n, "total": round(t, 2)} for n, t in cuisines],
        "max_cuisine": max_cuisine,
    }

# ---------- import ----------

def _parse_seed(data: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Validate the seed JSON shape. Returns (restaurants, visits, deals)."""
    if not isinstance(data, dict):
        raise HTTPException(400, "Seed file must be a JSON object")
    restaurants = data.get("restaurants", [])
    visits = data.get("visits", [])
    deals = data.get("deals", [])
    if not isinstance(restaurants, list) or not isinstance(visits, list) or not isinstance(deals, list):
        raise HTTPException(400, '"restaurants", "visits" and "deals" must be lists')
    clean_r, clean_v, clean_d = [], [], []
    for r in restaurants:
        name = (r.get("name") or "").strip() if isinstance(r, dict) else ""
        if not name:
            continue
        tier = r.get("price_tier")
        clean_r.append(
            {
                "name": name,
                "cuisine": (r.get("cuisine") or "").strip() or None,
                "price_tier": tier if tier in (1, 2, 3) else 2,
                "notes": (r.get("notes") or "").strip() or None,
                "address": (r.get("address") or "").strip() or None,
                "favorite": bool(r.get("favorite")),
                "include_in_picks": False if r.get("include_in_picks") is False else True,
            }
        )
    for v in visits:
        if not isinstance(v, dict):
            continue
        try:
            visited_at = date.fromisoformat(str(v.get("visited_at", "")))
        except ValueError:
            continue
        items = v.get("items")
        if isinstance(items, (list, tuple)):
            items = "\n".join(str(i).strip() for i in items if str(i).strip()) or None
        elif isinstance(items, str):
            items = items.strip() or None
        else:
            items = None
        clean_v.append(
            {
                "restaurant": (v.get("restaurant") or "").strip(),
                "visited_at": visited_at,
                "total": v.get("total"),
                "source": (v.get("source") or "import"),
                "external_id": (v.get("external_id") or "").strip() or None,
                "items": items,
            }
        )
    for d in deals:
        if not isinstance(d, dict):
            continue
        title = (d.get("title") or "").strip()
        if not title:
            continue
        try:
            valid_from = date.fromisoformat(str(d["valid_from"])) if d.get("valid_from") else None
        except ValueError:
            valid_from = None
        try:
            valid_until = date.fromisoformat(str(d["valid_until"])) if d.get("valid_until") else None
        except ValueError:
            valid_until = None
        clean_d.append(
            {
                "restaurant": (d.get("restaurant") or "").strip() or None,
                "title": title,
                "description": (d.get("description") or "").strip() or None,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "item_keywords": _norm_keywords(d.get("item_keywords")),
                "source": (d.get("source") or "import").strip() or "import",
            }
        )
    return clean_r, clean_v, clean_d


def _import_preview(session: Session, restaurants: list[dict], visits: list[dict],
                    deals: list[dict] | None = None) -> dict:
    existing_names = {_norm_name(r.name) for r in session.query(Restaurant).all()}
    existing_ext = {
        v.external_id for v in session.query(Visit).filter(Visit.external_id.isnot(None)).all()
    }
    existing_pairs = {
        (v.restaurant_id, v.visited_at.isoformat())
        for v in session.query(Visit).all()
    }

    new_restaurants = [r for r in restaurants if _norm_name(r["name"]) not in existing_names]
    # resolve restaurant ids for visit matching (including ones we'd just add)
    name_to_id: dict[str, int] = {}
    for r in session.query(Restaurant).all():
        name_to_id[_norm_name(r.name)] = r.id

    new_visits = []
    skipped = 0
    for v in visits:
        key = _norm_name(v["restaurant"])
        rid = name_to_id.get(key)
        if rid is None:
            # restaurant is new in this same import — match after insert; count it
            match = next((nr for nr in new_restaurants if _norm_name(nr["name"]) == key), None)
            if match is None:
                skipped += 1
                continue
            rid = -1  # placeholder: will exist after restaurant insert
        if v["external_id"] and v["external_id"] in existing_ext:
            continue
        if rid != -1 and (rid, v["visited_at"].isoformat()) in existing_pairs:
            continue
        new_visits.append(v)

    new_deals = []
    skipped_deals = 0
    existing_deal_keys = {
        (_norm_name(d.restaurant.name) if d.restaurant else "", _norm_name(d.title),
         d.valid_from.isoformat() if d.valid_from else "")
        for d in session.query(Deal).all()
    }
    for d in deals or []:
        key = _norm_name(d["restaurant"]) if d["restaurant"] else ""
        rid = name_to_id.get(key) if key else None
        if d["restaurant"] and rid is None:
            # restaurant new in this same import
            if not any(_norm_name(nr["name"]) == key for nr in new_restaurants):
                skipped_deals += 1
                continue
        deal_key = (key, _norm_name(d["title"]), d["valid_from"].isoformat() if d["valid_from"] else "")
        if deal_key in existing_deal_keys:
            continue
        new_deals.append(d)

    sample = [f'{v["restaurant"]} — {v["visited_at"].isoformat()}' for v in new_visits[:5]]
    deal_sample = [f'{d["restaurant"] or "anywhere"}: {d["title"]}' for d in new_deals[:5]]
    out = {
        "new_restaurants": len(new_restaurants),
        "new_visits": len(new_visits),
        "skipped": f"{skipped} visits skipped (unknown restaurant)" if skipped else "",
        "sample": sample,
        "new_deals": len(new_deals),
        "deal_sample": deal_sample,
    }
    if skipped_deals:
        out["skipped_deals"] = f"{skipped_deals} deals skipped (unknown restaurant)"
    return out


@app.post("/api/import/preview")
async def import_preview(file: UploadFile = File(...), session: Session = Depends(get_session)):
    try:
        data = json.loads((await file.read()).decode("utf-8"))
    except Exception:
        raise HTTPException(400, "Couldn't read that file as JSON")
    restaurants, visits, deals = _parse_seed(data)
    return _import_preview(session, restaurants, visits, deals)


@app.post("/api/import/confirm")
async def import_confirm(payload: dict, session: Session = Depends(get_session)):
    restaurants, visits, deals = _parse_seed(payload)

    existing = {_norm_name(r.name): r for r in session.query(Restaurant).all()}
    added_r = 0
    for r in restaurants:
        if _norm_name(r["name"]) not in existing:
            obj = Restaurant(**r)
            session.add(obj)
            session.flush()
            existing[_norm_name(r["name"])] = obj
            added_r += 1

    existing_ext = {
        v.external_id for v in session.query(Visit).filter(Visit.external_id.isnot(None)).all()
    }
    existing_pairs = {
        (v.restaurant_id, v.visited_at.isoformat()) for v in session.query(Visit).all()
    }
    added_v = 0
    items_backfilled = 0
    for v in visits:
        r = existing.get(_norm_name(v["restaurant"]))
        if r is None:
            continue
        if v["external_id"] and v["external_id"] in existing_ext:
            # duplicate — but backfill item names if we now have them and the
            # stored visit doesn't
            if v["items"]:
                stored = (
                    session.query(Visit)
                    .filter(Visit.external_id == v["external_id"], Visit.items.is_(None))
                    .first()
                )
                if stored:
                    stored.items = v["items"]
                    items_backfilled += 1
            continue
        if (r.id, v["visited_at"].isoformat()) in existing_pairs:
            continue
        total = v["total"]
        try:
            total = float(total) if total is not None else None
        except (TypeError, ValueError):
            total = None
        session.add(
            Visit(
                restaurant_id=r.id,
                visited_at=v["visited_at"],
                total=total,
                source=v["source"],
                external_id=v["external_id"],
                items=v["items"],
            )
        )
        if v["external_id"]:
            existing_ext.add(v["external_id"])
        existing_pairs.add((r.id, v["visited_at"].isoformat()))
        added_v += 1

    existing_deal_keys = {
        (_norm_name(d.restaurant.name) if d.restaurant else "", _norm_name(d.title),
         d.valid_from.isoformat() if d.valid_from else "")
        for d in session.query(Deal).all()
    }
    added_d = 0
    for d in deals:
        rid = None
        if d["restaurant"]:
            r = existing.get(_norm_name(d["restaurant"]))
            if r is None:
                continue
            rid = r.id
        key = (
            _norm_name(d["restaurant"]) if d["restaurant"] else "",
            _norm_name(d["title"]),
            d["valid_from"].isoformat() if d["valid_from"] else "",
        )
        if key in existing_deal_keys:
            continue
        session.add(
            Deal(
                restaurant_id=rid,
                title=d["title"],
                description=d["description"],
                valid_from=d["valid_from"],
                valid_until=d["valid_until"],
                item_keywords=d["item_keywords"],
                source=d["source"],
            )
        )
        existing_deal_keys.add(key)
        added_d += 1

    session.commit()
    return {
        "restaurants_added": added_r,
        "visits_added": added_v,
        "deals_added": added_d,
        "items_backfilled": items_backfilled,
    }
