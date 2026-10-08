"""OpenRouter's public model catalog, filtered to text/chat models.

Crave's intent parser needs a model that can *think* in JSON — any chat
model works, no vision needed. This mirrors Fauna's openrouter_models
module (which filters for vision). The public catalog needs no auth.

The list is cached in-process for 24 hours (the catalog changes slowly).
Every public function is defensive: failures return an empty list, never
raise — the Settings UI falls back to a plain text field when the catalog
is unreachable.
"""
from __future__ import annotations

import time

import httpx

MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_TTL = 24 * 3600

_cache = {"at": 0.0, "models": []}


def clear_cache() -> None:
    """Empty the model-list cache (used by tests)."""
    _cache.update(at=0.0, models=[])


def is_free(entry: dict) -> bool:
    """A model is free when OpenRouter prices both directions at zero
    (the `:free` suffix is the usual marker, but the pricing fields are
    the ground truth)."""
    mid = entry.get("id") or ""
    if mid.endswith(":free"):
        return True
    pricing = entry.get("pricing") or {}
    try:
        return float(pricing.get("prompt") or 1) == 0 and float(
            pricing.get("completion") or 1
        ) == 0
    except (TypeError, ValueError):
        return False


def is_chat(entry: dict) -> bool:
    """True for text-in/text-out chat models — skips embeddings, image
    generators, and other non-chat endpoints."""
    arch = entry.get("architecture") or {}
    in_mod = arch.get("input_modalities") or []
    out_mod = arch.get("output_modalities") or []
    # Be permissive when the catalog omits modalities; exclude anything
    # that clearly isn't text chat.
    if in_mod and "text" not in in_mod:
        return False
    if out_mod and "text" not in out_mod:
        return False
    mid = (entry.get("id") or "").lower()
    if any(
        junk in mid
        for junk in ("embedding", "rerank", "whisper", "tts", "dall-e", "imagen")
    ):
        return False
    return True


def get_chat_models() -> list[dict]:
    """Text/chat OpenRouter models: id, name, free flag. Cached 24h.

    Free models sort first, then alphabetically. Never raises — returns
    an empty list when the catalog can't be fetched.
    """
    now = time.time()
    if _cache["models"] and now - _cache["at"] < CACHE_TTL:
        return _cache["models"]
    models: list[dict] = []
    try:
        resp = httpx.get(
            MODELS_URL,
            headers={"User-Agent": "FoodFriday/1.0"},
            timeout=15.0,
        )
        resp.raise_for_status()
        for entry in resp.json().get("data", []):
            mid = entry.get("id") or ""
            if not mid or not is_chat(entry):
                continue
            models.append(
                {
                    "id": mid,
                    "name": entry.get("name") or mid,
                    "free": is_free(entry),
                }
            )
        models.sort(key=lambda m: (not m["free"], m["name"].lower()))
        _cache.update(at=now, models=models)
    except Exception:
        # Catalog unreachable — the UI falls back to a text field.
        return _cache["models"] if _cache["models"] else []
    return models
