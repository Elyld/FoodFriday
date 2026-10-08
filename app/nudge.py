"""Friday Discord nudge: post the week's 3 picks to a webhook.

Runs as a weekly APScheduler job (Friday at the configured time, container-local).
Failures are recorded in the settings result line and never crash the scheduler.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime

from sqlalchemy.orm import Session

from app.deal_scan import get_setting, set_setting

logger = logging.getLogger(__name__)

K_WEBHOOK = "discord_webhook_url"
K_NUDGE_ENABLED = "friday_nudge_enabled"   # "1"/"0"
K_NUDGE_TIME = "friday_nudge_time"         # "HH:MM", 24h, container-local time
K_NUDGE_LAST_RESULT = "friday_nudge_last_result"

HTTP_TIMEOUT = 20  # seconds


def nudge_configured(session: Session) -> bool:
    return bool(get_setting(session, K_WEBHOOK))


def nudge_enabled(session: Session) -> bool:
    return get_setting(session, K_NUDGE_ENABLED, "0") == "1" and nudge_configured(session)


def _http_post(url: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"Discord webhook returned HTTP {resp.status}")


def build_message(picks: list[dict]) -> str:
    """Short plain-text Friday message from pick dicts (name/cuisine/reason/deal_titles)."""
    lines = ["🍽️ **Friday dinner — 3 contenders**"]
    for p in picks:
        deal = f" 🏷️ {p['deal_titles'][0]}" if p.get("deal_titles") else ""
        cuisine = f" ({p['cuisine']})" if p.get("cuisine") else ""
        lines.append(f"• **{p['name']}**{cuisine} — {p.get('reason', '')}{deal}")
    return "\n".join(lines)


def send_nudge(session: Session, http_post=None, test: bool = False) -> dict:
    """Post picks to the Discord webhook. Never raises for send failures."""
    from app.main import build_pick_response  # deferred: main imports this module

    webhook = get_setting(session, K_WEBHOOK)
    if not webhook:
        return {"ok": False, "error": "Discord webhook not configured"}
    try:
        picks = build_pick_response(session, mode="friday")["picks"]
        if test:
            content = "🔔 FoodFriday test — the Friday nudge is wired up. See you Friday!"
        elif not picks:
            content = "🍽️ FoodFriday: no restaurants in the rotation yet — add some and I'll pick 3 on Fridays."
        else:
            content = build_message(picks)
        (http_post or _http_post)(webhook, {"content": content})
    except Exception as exc:
        now = datetime.now().isoformat(timespec="minutes")
        set_setting(session, K_NUDGE_LAST_RESULT, f"error: {exc} ({now})")
        session.commit()
        logger.warning("friday nudge failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    now = datetime.now().isoformat(timespec="minutes")
    set_setting(session, K_NUDGE_LAST_RESULT, f"sent ({now})")
    session.commit()
    return {"ok": True}
