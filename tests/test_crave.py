"""Tests for Crave ("what am I feeling?").

LLM-as-interpreter, app-as-decider: the model only produces structured
filters; all matching/ranking runs against real data. OpenRouter is fully
mocked — no live calls.

Run:  cd ~/workspace/foodfriday && PYTHONPATH=. .venv/bin/python -m pytest -q
"""
from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

TMP = Path(tempfile.mkdtemp(prefix="ff-crave-"))
os.environ.setdefault("FOODFRIDAY_DATA_DIR", str(TMP / "data"))

from fastapi.testclient import TestClient  # noqa: E402

from app import crave as crave_mod  # noqa: E402
from app import openrouter_models  # noqa: E402
from app.crave import (  # noqa: E402
    keyword_intent,
    match_candidate,
    parse_intent_json,
    parse_intent_llm,
)
from app.database import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Restaurant  # noqa: E402

init_db()
client = TestClient(app)

VOCAB = ["Mexican", "Italian", "Burgers", "Pizza"]


# ---------- intent JSON parsing ----------


def test_parse_intent_json_clean():
    d = parse_intent_json(
        '{"cuisines": ["Mexican"], "keywords": ["tacos"], '
        '"max_price_tier": 2, "include_new": false, "avoid_cuisines": []}'
    )
    assert d == {
        "cuisines": ["Mexican"],
        "keywords": ["tacos"],
        "max_price_tier": 2,
        "include_new": False,
        "avoid_cuisines": [],
    }


def test_parse_intent_json_fenced():
    d = parse_intent_json(
        '```json\n{"cuisines": [], "keywords": ["soul food"], '
        '"max_price_tier": null, "include_new": true, "avoid_cuisines": []}\n```'
    )
    assert d is not None
    assert d["keywords"] == ["soul food"]
    assert d["include_new"] is True
    assert d["max_price_tier"] is None


def test_parse_intent_json_malformed():
    assert parse_intent_json("sorry, I can't do that") is None
    assert parse_intent_json("") is None
    assert parse_intent_json("[1, 2, 3]") is None  # not a dict


def test_parse_intent_json_validates_tier():
    d = parse_intent_json(
        '{"cuisines": [], "keywords": [], "max_price_tier": 9, '
        '"include_new": false, "avoid_cuisines": []}'
    )
    assert d is not None and d["max_price_tier"] is None


# ---------- keyword fallback ----------


def test_keyword_intent_cuisine_and_unknown():
    d = keyword_intent("I'm feeling mexican", VOCAB)
    assert d["cuisines"] == ["Mexican"]
    d2 = keyword_intent("I'm feeling soul food", VOCAB)
    assert d2["cuisines"] == []  # not in vocab -> keywords
    assert "soul" in d2["keywords"] and "food" in d2["keywords"]


def test_keyword_intent_price():
    assert keyword_intent("something cheap", VOCAB)["max_price_tier"] == 1
    assert (
        keyword_intent("bar food, not too expensive", VOCAB)["max_price_tier"] == 2
    )
    assert keyword_intent("fancy italian", VOCAB)["max_price_tier"] is None
    assert keyword_intent("italian", VOCAB)["max_price_tier"] is None


def test_keyword_intent_new():
    assert keyword_intent("somewhere new and cheap", VOCAB)["include_new"] is True
    assert keyword_intent("never been somewhere", VOCAB)["include_new"] is True
    assert keyword_intent("mexican", VOCAB)["include_new"] is False


def test_keyword_intent_avoid():
    d = keyword_intent("anything but mexican tonight", VOCAB)
    assert d["avoid_cuisines"] == ["Mexican"]
    # "not too expensive" must NOT be read as avoiding a cuisine
    d2 = keyword_intent("bar food, not too expensive", VOCAB)
    assert d2["avoid_cuisines"] == []


# ---------- LLM intent (mocked) ----------


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _llm_response(content: str):
    return _FakeResp({"choices": [{"message": {"content": content}}]})


def test_parse_intent_llm_mock():
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["model"] = json["model"]
        # system prompt should carry the cuisine vocabulary
        assert "Mexican" in json["messages"][0]["content"]
        assert "api/v1/chat/completions" in url
        assert headers["Authorization"] == "Bearer sk-or-test"
        return _llm_response('{"cuisines": ["Italian"], "keywords": [], '
                             '"max_price_tier": null, "include_new": false, '
                             '"avoid_cuisines": []}')

    d = parse_intent_llm("pasta", "sk-or-test", "some-model", VOCAB,
                         http_post=fake_post)
    assert d is not None and d["cuisines"] == ["Italian"]
    assert seen["model"] == "some-model"


def test_parse_intent_llm_failure_falls_back():
    def boom(*a, **k):
        raise RuntimeError("network down")

    assert parse_intent_llm("pasta", "k", "m", VOCAB, http_post=boom) is None
    assert parse_intent_llm("pasta", "", "m", VOCAB) is None  # no key
    # malformed model output -> None
    d = parse_intent_llm("pasta", "k", "m", VOCAB,
                         http_post=lambda *a, **k: _llm_response("nope"))
    assert d is None


# ---------- matching ----------


def _cand(**kw):
    base = {"name": "X", "cuisine": "Mexican", "price_tier": 2, "notes": "",
            "include_in_picks": True}
    base.update(kw)
    return base


def test_match_cuisine_hit():
    ok, reasons = match_candidate(_cand(), {"cuisines": ["Mexican"], "keywords": [],
                                            "max_price_tier": None, "include_new": False,
                                            "avoid_cuisines": []})
    assert ok and reasons == ["Matches 'Mexican'"]


def test_match_price_filter():
    ok, _ = match_candidate(_cand(price_tier=3),
                            {"cuisines": [], "keywords": [], "max_price_tier": 2,
                             "include_new": False, "avoid_cuisines": []})
    assert not ok
    ok2, _ = match_candidate(_cand(price_tier=2),
                             {"cuisines": [], "keywords": [], "max_price_tier": 2,
                              "include_new": False, "avoid_cuisines": []})
    assert ok2


def test_match_keyword_on_name():
    ok, reasons = match_candidate(_cand(name="Smoky Wings Co", cuisine="American"),
                                  {"cuisines": [], "keywords": ["wings"],
                                   "max_price_tier": None, "include_new": False,
                                   "avoid_cuisines": []})
    assert ok and reasons == ["Matches 'wings'"]


def test_match_avoid_and_excluded_restaurant():
    ok, _ = match_candidate(_cand(),
                            {"cuisines": ["Mexican"], "keywords": [],
                             "max_price_tier": None, "include_new": False,
                             "avoid_cuisines": ["Mexican"]})
    assert not ok
    ok2, _ = match_candidate(_cand(include_in_picks=False),
                             {"cuisines": [], "keywords": [],
                              "max_price_tier": None, "include_new": False,
                              "avoid_cuisines": []})
    assert not ok2


def test_match_no_constraints_matches_all():
    ok, reasons = match_candidate(_cand(), crave_mod.empty_intent())
    assert ok and reasons == []


# ---------- /api/crave end to end (keyword path) ----------


def _seed_restaurant(name, cuisine, tier=2, favorite=False):
    r = client.post("/api/restaurants", json={
        "name": name, "cuisine": cuisine, "price_tier": tier, "favorite": favorite,
    })
    assert r.status_code == 201, r.text
    return r.json()


def test_suggest_keyword_flow():
    r1 = _seed_restaurant("Crave Taco Barn", "CraveMex", 1)
    _seed_restaurant("Crave Pasta Palace", "CraveItalian", 3)
    resp = client.post("/api/crave", json={"text": "cravemex cheap"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["intent"]["via"] == "keyword"
    names = [s["name"] for s in data["suggestions"]]
    assert "Crave Taco Barn" in names
    assert "Crave Pasta Palace" not in names  # tier 3 over the cheap cap
    card = next(s for s in data["suggestions"] if s["name"] == "Crave Taco Barn")
    assert card["kind"] == "tried"
    assert any("CraveMex" in r for r in card["reasons"])
    # log a visit straight from the suggestion, then clean it up — the
    # shared test DB is wiped of restaurants (but not visits) by later
    # test files, so a leftover visit would become an orphan and break
    # their exact-count assertions after id reuse.
    v = client.post("/api/visits", json={"restaurant_id": r1["id"]})
    assert v.status_code == 201
    assert client.delete(f"/api/visits/{v.json()['id']}").status_code == 204


def test_suggest_ai_flow_mock():
    _seed_restaurant("Crave Ramen Spot", "CraveRamen", 2)
    fake = _llm_response('{"cuisines": ["CraveRamen"], "keywords": [], '
                         '"max_price_tier": null, "include_new": false, '
                         '"avoid_cuisines": []}')
    with patch.object(crave_mod.httpx, "post", return_value=fake):
        s = SessionLocal()
        try:
            from app.deal_scan import set_setting
            set_setting(s, "openrouter_api_key", "sk-or-test")
            set_setting(s, "crave_model", "test-model")
            s.commit()
        finally:
            s.close()
        try:
            resp = client.post("/api/crave", json={"text": "ramen please"})
            data = resp.json()
            assert data["intent"]["via"] == "ai", data["intent"]
            assert any(s["name"] == "Crave Ramen Spot" for s in data["suggestions"])
        finally:
            s = SessionLocal()
            try:
                from app.deal_scan import set_setting
                set_setting(s, "openrouter_api_key", None)
                set_setting(s, "crave_model", None)
                s.commit()
            finally:
                s.close()


def test_suggest_mixes_new_spots():
    _seed_restaurant("Crave Burger Joint", "CraveBurgers", 1)

    def fake_discover(session, lat, lon, radius_km, api_key, refresh=False):
        return {"businesses": [
            {"name": "Crave Smash Burgers", "cuisine": "CraveBurgers",
             "price_tier": 1, "address": "1 Main St", "distance_mi": 1.2,
             "rating": 4.5, "yelp_id": None},
            {"name": "Crave Sushi Bar", "cuisine": "CraveSushi",
             "price_tier": 2, "address": "2 Main St", "distance_mi": 2.0,
             "rating": None, "yelp_id": None},
        ], "cached": False, "cached_at": None, "provider": "osm"}

    s = SessionLocal()
    try:
        from app.deal_scan import set_setting
        set_setting(s, "home_lat", "39.05")
        set_setting(s, "home_lon", "-95.68")
        s.commit()
        from app.crave import suggest
        out = suggest(s, "craveburgers", discover_fn=fake_discover,
                      today=date(2026, 10, 8))
    finally:
        s2 = SessionLocal()
        try:
            from app.deal_scan import set_setting as _ss
            _ss(s2, "home_lat", None)
            _ss(s2, "home_lon", None)
            s2.commit()
        finally:
            s2.close()
        s.close()
    kinds = {c["kind"] for c in out["suggestions"]}
    assert "tried" in kinds and "new" in kinds
    new_card = next(c for c in out["suggestions"] if c["kind"] == "new")
    assert new_card["name"] == "Crave Smash Burgers"
    assert "✨ New spot near you" in new_card["reasons"]
    assert new_card["add_payload"]["name"] == "Crave Smash Burgers"
    # the sushi place doesn't match the craving
    assert not any(c["name"] == "Crave Sushi Bar" for c in out["suggestions"])


def test_suggest_deal_reasons():
    r = _seed_restaurant("Crave Wing Shack", "CraveWings", 1)
    d = client.post("/api/deals", json={
        "restaurant_id": r["id"], "title": "Crave 20% off wings",
        "valid_from": "2026-10-01", "valid_until": "2026-12-31",
    })
    assert d.status_code == 201
    resp = client.post("/api/crave", json={"text": "cravewings"})
    card = next(s for s in resp.json()["suggestions"] if s["name"] == "Crave Wing Shack")
    assert any("🏷️" in reason for reason in card["reasons"])
    assert card["deal_titles"] == ["Crave 20% off wings"]


# ---------- openrouter models endpoint ----------


def test_openrouter_models_endpoint_mock():
    openrouter_models.clear_cache()
    payload = {"data": [
        {"id": "big-paid/model", "name": "Big Paid",
         "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
         "pricing": {"prompt": "0.001", "completion": "0.002"}},
        {"id": "tiny/model:free", "name": "Tiny Free",
         "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
         "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "embed/model", "name": "Embedder",
         "architecture": {"input_modalities": ["text"], "output_modalities": ["embedding"]},
         "pricing": {"prompt": "0", "completion": "0"}},
    ]}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    with patch.object(openrouter_models.httpx, "get", return_value=Resp()):
        r = client.get("/api/openrouter-models")
        assert r.status_code == 200
        models = r.json()["models"]
        ids = [m["id"] for m in models]
        # default free_only=true: only the free chat model listed
        assert ids == ["tiny/model:free"]
        assert models[0]["free"] is True
        # free_only=false keeps the paid one; embedder never qualifies
        r2 = client.get("/api/openrouter-models", params={"free_only": "false"})
        ids2 = [m["id"] for m in r2.json()["models"]]
        assert "tiny/model:free" in ids2 and "big-paid/model" in ids2
        assert "embed/model" not in ids2  # not a chat model
        # free first
        assert ids2[0] == "tiny/model:free"
    openrouter_models.clear_cache()


def test_openrouter_models_endpoint_offline_graceful():
    openrouter_models.clear_cache()
    with patch.object(openrouter_models.httpx, "get", side_effect=RuntimeError("down")):
        r = client.get("/api/openrouter-models")
        assert r.status_code == 200
        assert r.json()["models"] == []
    openrouter_models.clear_cache()


# ---------- settings ----------


def test_crave_settings_roundtrip():
    r = client.put("/api/settings", json={
        "openrouter_api_key": "sk-or-secret",
        "crave_model": "tiny/model:free",
        "crave_model_free_only": False,
    })
    assert r.status_code == 200
    s = r.json()
    assert s["openrouter_api_key_set"] is True
    assert "sk-or-secret" not in str(s)  # never echoed back
    assert s["crave_model"] == "tiny/model:free"
    assert s["crave_model_free_only"] is False
    # cleanup so other tests see the no-key path
    s2 = SessionLocal()
    try:
        from app.deal_scan import set_setting
        set_setting(s2, "openrouter_api_key", None)
        set_setting(s2, "crave_model", None)
        s2.commit()
    finally:
        s2.close()
