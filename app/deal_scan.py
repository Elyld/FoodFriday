"""In-app deal scanner: polls Gmail over IMAP and writes deals to the DB.

Runs on a schedule (APScheduler, see app/scheduler.py) and on demand via
POST /api/deals/scan. Read-only against the mailbox: it SELECTs INBOX and
fetches with BODY.PEEK[] so nothing is marked read, and never deletes.

Dedupes against existing deals on (restaurant_id, title, valid_until).
If a deal comes from a chain that isn't in the restaurant list, the
restaurant is auto-created (cuisine blank, $ tier, included in picks) so
the picker boost works — Josh can toggle it off or delete it.
"""

from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import logging
import re
import threading
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.deal_parse import chain_domains, parse_promo

from app.models import Deal, Restaurant, Setting

logger = logging.getLogger(__name__)

SCAN_WINDOW_DAYS = 14

# settings keys
K_ADDRESS = "gmail_address"
K_PASSWORD = "gmail_app_password"
K_ENABLED = "deal_scan_enabled"
K_TIME = "deal_scan_time"          # "HH:MM", 24h, local container time
K_LAST_RUN = "deal_scan_last_run"      # ISO datetime
K_LAST_RESULT = "deal_scan_last_result"  # human-readable summary
K_STATUS = "deal_scan_status"            # idle | running | done | error

# A stalled Gmail connection must never hang a request/scan forever.
IMAP_TIMEOUT = 30  # seconds

_scan_lock = threading.Lock()


def get_setting(session: Session, key: str, default: str | None = None) -> str | None:
    row = session.get(Setting, key)
    return row.value if row is not None else default


def set_setting(session: Session, key: str, value: str | None) -> None:
    row = session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value)
        session.add(row)
    else:
        row.value = value
        row.updated_at = datetime.utcnow()


def scan_configured(session: Session) -> bool:
    return bool(get_setting(session, K_ADDRESS)) and bool(get_setting(session, K_PASSWORD))


def scan_enabled(session: Session) -> bool:
    if get_setting(session, K_ENABLED, "1") != "1":
        return False
    return scan_configured(session)


# ---------- IMAP ----------

def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _message_date(msg: email.message.Message) -> date:
    parsed = email.utils.parsedate_to_datetime(msg.get("Date", "") or "")
    if parsed is None:
        return date.today()
    return parsed.date()


def _message_body(msg: email.message.Message) -> str:
    """Plain-text body (first 20k chars), skipping attachments."""
    chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in (
                part.get("Content-Disposition") or ""
            ):
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                chunks.append(payload.decode(charset, errors="replace"))
                break
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        chunks.append(payload.decode(charset, errors="replace"))
    return "\n".join(chunks)[:20000]


def fetch_promos(
    address: str,
    app_password: str,
    imap_class=imaplib.IMAP4_SSL,
    since: date | None = None,
    today: date | None = None,
) -> list[dict]:
    """Search Gmail for promo emails from known chains; return parsed deal dicts.

    `imap_class` is injectable for tests (fake IMAP server fixture).
    """
    today = today or date.today()
    since = since or (today - timedelta(days=SCAN_WINDOW_DAYS))
    since_str = since.strftime("%d-%b-%Y")  # IMAP date format: 01-Oct-2026

    conn = None
    try:
        # timeout= keeps a stalled Gmail connection from hanging forever
        # (imaplib.IMAP4_SSL supports it on 3.9+; test fakes must tolerate the kwarg).
        conn = imap_class("imap.gmail.com", timeout=IMAP_TIMEOUT)
        conn.login(address, app_password)
        conn.select("INBOX", readonly=True)
        seen_uids: set[bytes] = set()
        deals: list[dict] = []
        for domain in chain_domains():
            # FROM with a bare domain fragment also matches noreply@email.sonicdrivein.com
            status, data = conn.search(None, "FROM", domain, "SINCE", since_str)
            if status != "OK":
                continue
            for uid in (data[0] or b"").split():
                if uid in seen_uids:
                    continue
                seen_uids.add(uid)
                status, fetched = conn.fetch(uid, "(BODY.PEEK[])")
                if status != "OK" or not fetched:
                    continue
                raw = fetched[0][1] if isinstance(fetched[0], tuple) else None
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                sender = _decode_header(msg.get("From"))
                subject = _decode_header(msg.get("Subject"))
                body = _message_body(msg)
                sent = _message_date(msg)
                if sent < since:  # belt & suspenders vs the SINCE search
                    continue
                deal = parse_promo(sender, subject, body, sent, today=today)
                if deal:
                    deals.append(deal)
        # dedupe within this scan on (restaurant, title)
        uniq: dict[tuple[str, str], dict] = {}
        for d in deals:
            uniq.setdefault((d["restaurant"], d["title"].lower()), d)
        return list(uniq.values())
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn.logout()
            except Exception:
                pass


# ---------- write to DB ----------

def _norm(s: str | None) -> str:
    return " ".join((s or "").strip().lower().split())


def store_deals(session: Session, deals: list[dict]) -> tuple[int, int]:
    """Insert parsed deals with dedupe + auto-create restaurants.

    Returns (deals_added, restaurants_added).
    """
    restaurants_added = 0
    deals_added = 0
    name_to_r: dict[str, Restaurant] = {_norm(r.name): r for r in session.query(Restaurant).all()}
    existing_keys = {
        (d.restaurant_id, _norm(d.title), d.valid_until.isoformat() if d.valid_until else "")
        for d in session.query(Deal).all()
    }

    for d in deals:
        rname = _norm(d["restaurant"])
        r = name_to_r.get(rname)
        if r is None:
            r = Restaurant(
                name=d["restaurant"],
                cuisine=None,
                price_tier=1,
                notes="auto-added by the deal scanner",
                favorite=False,
                include_in_picks=True,
            )
            session.add(r)
            session.flush()
            name_to_r[rname] = r
            restaurants_added += 1
        valid_from = date.fromisoformat(d["valid_from"]) if d.get("valid_from") else None
        valid_until = date.fromisoformat(d["valid_until"]) if d.get("valid_until") else None
        key = (r.id, _norm(d["title"]), valid_until.isoformat() if valid_until else "")
        if key in existing_keys:
            continue
        session.add(
            Deal(
                restaurant_id=r.id,
                title=d["title"],
                description=d.get("description"),
                valid_from=valid_from,
                valid_until=valid_until,
                item_keywords=d.get("item_keywords"),
                source="email",
            )
        )
        existing_keys.add(key)
        deals_added += 1
    return deals_added, restaurants_added


def run_scan(session: Session, imap_class=imaplib.IMAP4_SSL) -> dict:
    """Full scan: fetch promos over IMAP, store new deals, record the result.

    Never raises for scan-time failures (bad creds, stalled connection, parse
    errors): those are recorded into the last-result setting and returned as
    ``{"ok": False, "error": ...}``. Only a missing Gmail configuration raises.
    """
    address = get_setting(session, K_ADDRESS)
    password = get_setting(session, K_PASSWORD)
    if not address or not password:
        raise RuntimeError("Gmail not configured — add your address and app password in Settings.")
    try:
        deals = fetch_promos(address, password, imap_class=imap_class)
        added, new_restaurants = store_deals(session, deals)
    except Exception as exc:
        now = datetime.now().isoformat(timespec="minutes")
        set_setting(session, K_STATUS, "error")
        set_setting(session, K_LAST_RUN, now)
        set_setting(session, K_LAST_RESULT, f"error: {exc}")
        session.commit()
        logger.warning("deal scan failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    now = datetime.now().isoformat(timespec="minutes")
    if added:
        summary = f"{added} new deal{'s' if added != 1 else ''}"
        if new_restaurants:
            summary += f" ({new_restaurants} new restaurant{'s' if new_restaurants != 1 else ''} added)"
    else:
        summary = "no new deals"
    set_setting(session, K_STATUS, "done")
    set_setting(session, K_LAST_RUN, now)
    set_setting(session, K_LAST_RESULT, summary)
    session.commit()
    return {"ok": True, "deals_found": len(deals), "deals_added": added,
            "restaurants_added": new_restaurants, "summary": summary}


# ---------- background execution ----------

def _scan_worker(session_factory, imap_class) -> None:
    """Run a scan in a daemon thread with its own session.

    Resolves run_scan via the module so tests can monkeypatch it.
    """
    import app.deal_scan as _self

    session = session_factory()
    try:
        _self.run_scan(session, imap_class=imap_class)
    except Exception as exc:  # belt & suspenders — run_scan already records failures
        logger.warning("background deal scan crashed: %s", exc)
        try:
            set_setting(session, K_STATUS, "error")
            set_setting(session, K_LAST_RUN, datetime.now().isoformat(timespec="minutes"))
            set_setting(session, K_LAST_RESULT, f"error: {exc}")
            session.commit()
        except Exception:
            pass
    finally:
        session.close()


def try_start_scan(session_factory, imap_class=None) -> bool:
    """Start a background scan unless one is already running.

    Returns True if a scan was started, False if one is already running.
    Raises RuntimeError if Gmail isn't configured.
    """
    if imap_class is None:
        imap_class = imaplib.IMAP4_SSL
    with _scan_lock:
        session = session_factory()
        try:
            if not scan_configured(session):
                raise RuntimeError(
                    "Gmail not configured — add your address and app password in Settings."
                )
            if get_setting(session, K_STATUS) == "running":
                return False
            set_setting(session, K_STATUS, "running")
            session.commit()
        finally:
            session.close()
    thread = threading.Thread(
        target=_scan_worker, args=(session_factory, imap_class), daemon=True,
        name="foodfriday-deal-scan",
    )
    thread.start()
    return True


def reset_stale_running(session_factory) -> None:
    """Clear a leftover 'running' status (e.g. container restarted mid-scan)."""
    session = session_factory()
    try:
        if get_setting(session, K_STATUS) == "running":
            set_setting(session, K_STATUS, "idle")
            session.commit()
    finally:
        session.close()
