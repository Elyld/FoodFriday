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
from app.models import Restaurant, Visit
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
    return pages.restaurants_page(rows)


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
    ]
    last_by_id = {rid: d for rid, d in lasts.items()}
    stats = picker.candidate_stats(dicts, last_by_id)
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
            }
            for p in picks
        ]
    }


# ---------- import ----------

def _parse_seed(data: dict) -> tuple[list[dict], list[dict]]:
    """Validate the seed JSON shape. Returns (restaurants, visits)."""
    if not isinstance(data, dict):
        raise HTTPException(400, "Seed file must be a JSON object")
    restaurants = data.get("restaurants", [])
    visits = data.get("visits", [])
    if not isinstance(restaurants, list) or not isinstance(visits, list):
        raise HTTPException(400, '"restaurants" and "visits" must be lists')
    clean_r, clean_v = [], []
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
            }
        )
    for v in visits:
        if not isinstance(v, dict):
            continue
        try:
            visited_at = date.fromisoformat(str(v.get("visited_at", "")))
        except ValueError:
            continue
        clean_v.append(
            {
                "restaurant": (v.get("restaurant") or "").strip(),
                "visited_at": visited_at,
                "total": v.get("total"),
                "source": (v.get("source") or "import"),
                "external_id": (v.get("external_id") or "").strip() or None,
            }
        )
    return clean_r, clean_v


def _import_preview(session: Session, restaurants: list[dict], visits: list[dict]) -> dict:
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

    sample = [f'{v["restaurant"]} — {v["visited_at"].isoformat()}' for v in new_visits[:5]]
    return {
        "new_restaurants": len(new_restaurants),
        "new_visits": len(new_visits),
        "skipped": f"{skipped} visits skipped (unknown restaurant)" if skipped else "",
        "sample": sample,
    }


@app.post("/api/import/preview")
async def import_preview(file: UploadFile = File(...), session: Session = Depends(get_session)):
    try:
        data = json.loads((await file.read()).decode("utf-8"))
    except Exception:
        raise HTTPException(400, "Couldn't read that file as JSON")
    restaurants, visits = _parse_seed(data)
    return _import_preview(session, restaurants, visits)


@app.post("/api/import/confirm")
async def import_confirm(payload: dict, session: Session = Depends(get_session)):
    restaurants, visits = _parse_seed(payload)

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
    for v in visits:
        r = existing.get(_norm_name(v["restaurant"]))
        if r is None:
            continue
        if v["external_id"] and v["external_id"] in existing_ext:
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
            )
        )
        if v["external_id"]:
            existing_ext.add(v["external_id"])
        existing_pairs.add((r.id, v["visited_at"].isoformat()))
        added_v += 1

    session.commit()
    return {"restaurants_added": added_r, "visits_added": added_v}
