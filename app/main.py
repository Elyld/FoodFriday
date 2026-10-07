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
from app.database import get_session, init_db
from app.models import Deal, Restaurant, Visit
from app.version import APP_NAME, VERSION

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


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
        "visit_count": visit_count,
        "last_visit": last_visit.isoformat() if last_visit else None,
    }


def visit_stats(session: Session) -> tuple[dict[int, int], dict[int, date]]:
    counts = {
        row[0]: row[1]
        for row in session.query(Visit.restaurant_id, func.count(Visit.id))
        .group_by(Visit.restaurant_id)
        .all()
    }
    lasts = {
        row[0]: row[1]
        for row in session.query(Visit.restaurant_id, func.max(Visit.visited_at))
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
        }
        for v in visits
    ]
    return pages.history_page(rows)


@app.get("/import", response_class=HTMLResponse)
def import_page():
    return pages.import_page()


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


class PickIn(BaseModel):
    keep_ids: list[int] = []
    veto_ids: list[int] = []


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


@app.post("/api/pick")
def pick(payload: PickIn, session: Session = Depends(get_session)):
    counts, lasts = visit_stats(session)
    restaurants = session.query(Restaurant).all()
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
    last_by_id = {rid: d for rid, d in lasts.items()}

    today = date.today()
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
    for v in session.query(Visit).filter(Visit.items.isnot(None)).all():
        items_by_id.setdefault(v.restaurant_id, []).extend(
            [i.strip() for i in (v.items or "").splitlines() if i.strip()]
        )

    stats = picker.candidate_stats(dicts, last_by_id, today=today,
                                   deals_by_id=deals_by_id, items_by_id=items_by_id)
    picks = picker.pick_three(stats, tuple(payload.keep_ids), tuple(payload.veto_ids))
    return {
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
        ]
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
