"""In-app deal/receipt scanner: polls Gmail accounts over IMAP, writes to the DB.

Runs on a schedule (APScheduler, see app/scheduler.py) and on demand via
POST /api/deals/scan. Read-only against the mailbox: it SELECTs INBOX and
fetches with BODY.PEEK[] so nothing is marked read, and never deletes.

Scans every configured email account (Settings → Email accounts), one IMAP
session per account; a failing account is recorded and skipped without
blocking the others.

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
from app.receipt_parse import (
    detect_receipt,
    parse_receipt,
    receipt_sender_domains,
)

from app.models import Deal, EmailAccount, Restaurant, Setting, Visit

logger = logging.getLogger(__name__)

SCAN_WINDOW_DAYS = 14

# settings keys
K_ADDRESS = "gmail_address"            # LEGACY — migrated to email_accounts on startup
K_PASSWORD = "gmail_app_password"     # LEGACY — migrated to email_accounts on startup
K_ENABLED = "deal_scan_enabled"
K_TIME = "deal_scan_time"          # "HH:MM", 24h, local container time
K_LAST_RUN = "deal_scan_last_run"      # ISO datetime
K_LAST_RESULT = "deal_scan_last_result"  # human-readable summary (deals)
K_STATUS = "deal_scan_status"            # idle | running | done | error

# receipt phase (same scan, same Gmail creds)
K_R_ENABLED = "receipt_scan_enabled"         # "1"/"0", default on
K_R_DAYS = "receipt_scan_days"              # how far back to look, default 365
K_R_LAST_RUN = "receipt_scan_last_run"      # ISO datetime
K_R_LAST_RESULT = "receipt_scan_last_result"  # human-readable summary (receipts)

DEFAULT_RECEIPT_SCAN_DAYS = 365
MAX_RECEIPT_SCAN_DAYS = 3650  # 10 years — sanity cap for the number field

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


def list_accounts(session: Session) -> list[EmailAccount]:
    """All configured scan email accounts, oldest first."""
    return session.query(EmailAccount).order_by(EmailAccount.id).all()


def _account_label(acct: EmailAccount) -> str:
    return (acct.label or "").strip() or acct.address


def migrate_legacy_gmail_settings() -> None:
    """One-time move of the old single-account settings keys into email_accounts.

    Idempotent: only runs when the legacy keys still exist. Safe to call on
    every startup.
    """
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        address = get_setting(session, K_ADDRESS)
        password = get_setting(session, K_PASSWORD)
        if not address or not password:
            return  # nothing to migrate (or already migrated)
        exists = session.query(EmailAccount).filter(
            EmailAccount.address == address.strip()
        ).first()
        if not exists:
            session.add(EmailAccount(
                label="primary",
                address=address.strip(),
                app_password=password,
            ))
        session.query(Setting).filter(
            Setting.key.in_([K_ADDRESS, K_PASSWORD])
        ).delete(synchronize_session=False)
        session.commit()
        logger.info("migrated legacy Gmail settings to email_accounts")
    finally:
        session.close()


def scan_configured(session: Session) -> bool:
    return session.query(EmailAccount).count() > 0


def scan_enabled(session: Session) -> bool:
    if get_setting(session, K_ENABLED, "1") != "1":
        return False
    return scan_configured(session)


def receipt_scan_enabled(session: Session) -> bool:
    if get_setting(session, K_R_ENABLED, "1") != "1":
        return False
    return scan_configured(session)


def receipt_scan_days(session: Session) -> int:
    """How far back the receipt phase looks. Defaults to a year; the user
    can raise it in Settings for a deeper backfill."""
    try:
        days = int(get_setting(session, K_R_DAYS, "") or DEFAULT_RECEIPT_SCAN_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_RECEIPT_SCAN_DAYS
    return min(max(days, 1), MAX_RECEIPT_SCAN_DAYS)


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
    """Readable body text (first 20k chars), skipping attachments.

    Prefers text/plain; falls back to text/html stripped of tags, since most
    promo emails are HTML-only and otherwise the parser sees just the subject.
    """
    if msg.is_multipart():
        for part in msg.walk():
            if "attachment" in (part.get("Content-Disposition") or ""):
                continue
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ctype == "text/html":
                text = _html_to_text(text)
            if text.strip():
                return text[:20000]
        return ""
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    text = payload.decode(charset, errors="replace")
    if msg.get_content_type() == "text/html":
        text = _html_to_text(text)
    return text[:20000]


def _html_to_text(html: str) -> str:
    """Crude HTML → text for receipt scanning (tables become spaced text)."""
    import html as _html

    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|div|tr|table|li|h\d)>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    return _html.unescape(text)


def _receipt_text(msg: email.message.Message) -> str:
    """Searchable text for receipt parsing: plain parts + stripped HTML parts."""
    parts: list[str] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if "attachment" in (part.get("Content-Disposition") or ""):
            continue
        payload = part.get_payload(decode=True) or b""
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        if ctype == "text/html":
            text = _html_to_text(text)
        elif ctype != "text/plain":
            continue
        parts.append(text)
    return "\n".join(parts)[:40000]


def _message_id(msg: email.message.Message) -> str | None:
    mid = (msg.get("Message-ID") or "").strip().strip("<>")
    return mid or None


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


def fetch_receipts(
    address: str,
    app_password: str,
    imap_class=imaplib.IMAP4_SSL,
    since: date | None = None,
    today: date | None = None,
) -> list[dict]:
    """Search Gmail for order receipts from known chains.

    Returns dicts: chain, sent (date), total (float|None), items (list),
    msgid, subject. `imap_class` is injectable for tests.
    """
    today = today or date.today()
    since = since or (today - timedelta(days=SCAN_WINDOW_DAYS))
    since_str = since.strftime("%d-%b-%Y")

    conn = None
    try:
        conn = imap_class("imap.gmail.com", timeout=IMAP_TIMEOUT)
        conn.login(address, app_password)
        conn.select("INBOX", readonly=True)
        seen_uids: set[bytes] = set()
        receipts: list[dict] = []
        for domain in receipt_sender_domains():
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
                chain = detect_receipt(sender, subject)
                if not chain:
                    continue  # promo or unrelated mail from a known sender
                sent = _message_date(msg)
                if sent < since:
                    continue
                parsed = parse_receipt(chain, _receipt_text(msg))
                msgid = _message_id(msg)
                receipts.append(
                    {
                        "chain": chain["name"],
                        "sent": sent,
                        "total": parsed["total"],
                        "items": parsed["items"],
                        "msgid": msgid,
                        "subject": subject,
                    }
                )
        return receipts
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


def store_receipts(session: Session, receipts: list[dict]) -> tuple[int, int, int]:
    """Insert parsed receipts as visits with dedupe + auto-create restaurants.

    Restaurants with track_visits=False are skipped entirely (the McDonald's /
    kid-orders skip list). Dedupes on external_id ("gmail:<message-id>") AND on
    (restaurant, date) — the seed import uses non-gmail external_ids, so without
    the pair guard recent seed visits would be duplicated by the first scan.
    Confirmation-only emails (no parseable total) are logged with total=None.

    Backfill: when a receipt's external_id is already logged but that visit
    has total=None (its chain's parser couldn't read the total at the time)
    and the receipt now parses a total, the visit is updated in place.

    Returns (visits_added, restaurants_added, totals_backfilled).
    """
    restaurants_added = 0
    visits_added = 0
    totals_backfilled = 0
    name_to_r: dict[str, Restaurant] = {_norm(r.name): r for r in session.query(Restaurant).all()}
    existing_ext = {
        v.external_id
        for v in session.query(Visit).filter(Visit.external_id.isnot(None)).all()
    }
    # Pair guard: the seed import uses non-gmail external_ids ("chipotle:2026-10-03:0"),
    # so a scan must also skip a receipt whose restaurant+date is already logged —
    # otherwise recent seed visits get duplicated on the first auto-scan.
    existing_pairs = {
        (v.restaurant_id, v.visited_at.isoformat()) for v in session.query(Visit).all()
    }

    for rc in receipts:
        if not rc.get("msgid"):
            continue  # can't dedupe without a message id — skip rather than risk dupes
        ext_id = f"gmail:{rc['msgid']}"
        if ext_id in existing_ext:
            if rc.get("total") is not None:
                v = session.query(Visit).filter(Visit.external_id == ext_id).first()
                if v is not None and v.total is None:
                    v.total = rc["total"]
                    totals_backfilled += 1
            continue
        rname = _norm(rc["chain"])
        r = name_to_r.get(rname)
        if r is None:
            r = Restaurant(
                name=rc["chain"],
                cuisine=None,
                price_tier=1,
                notes="auto-added by the receipt scanner",
                favorite=False,
                include_in_picks=True,
                track_visits=True,
            )
            session.add(r)
            session.flush()
            name_to_r[rname] = r
            restaurants_added += 1
        if r.track_visits is False:
            continue  # skip-listed (e.g. kid's McDonald's runs) — no visit, no noise
        if (r.id, rc["sent"].isoformat()) in existing_pairs:
            continue  # already logged (e.g. via seed import with a non-gmail external_id)
        items = rc.get("items") or []
        session.add(
            Visit(
                restaurant_id=r.id,
                visited_at=rc["sent"],
                total=rc["total"],
                source="email",
                external_id=ext_id,
                items="\n".join(items) if items else None,
            )
        )
        existing_ext.add(ext_id)
        existing_pairs.add((r.id, rc["sent"].isoformat()))
        visits_added += 1
    return visits_added, restaurants_added, totals_backfilled


def _scan_one_account(session: Session, address: str, password: str,
                      imap_class, today: date | None) -> dict:
    """Deal + receipt phases for a single account. Never raises for scan-time
    failures — those come back as error strings in the result dict."""
    errors: list[str] = []
    deals: list[dict] = []

    # --- deal phase ---
    try:
        deals = fetch_promos(address, password, imap_class=imap_class, today=today)
        added, new_restaurants = store_deals(session, deals)
        if added:
            deal_summary = f"{added} new deal{'s' if added != 1 else ''}"
            if new_restaurants:
                deal_summary += f" ({new_restaurants} new restaurant{'s' if new_restaurants != 1 else ''} added)"
        else:
            deal_summary = "no new deals"
    except Exception as exc:
        logger.warning("deal scan failed for %s: %s", address, exc)
        deal_summary = f"error: {exc}"
        errors.append(f"deals: {exc}")
        added, new_restaurants = 0, 0

    # --- receipt phase ---
    if receipt_scan_enabled(session):
        try:
            since = (today or date.today()) - timedelta(days=receipt_scan_days(session))
            receipts = fetch_receipts(address, password, imap_class=imap_class,
                                      since=since, today=today)
            v_added, r_added, t_backfilled = store_receipts(session, receipts)
            if v_added:
                receipt_summary = f"{v_added} new visit{'s' if v_added != 1 else ''}"
                if r_added:
                    receipt_summary += f" ({r_added} new restaurant{'s' if r_added != 1 else ''} added)"
            else:
                receipt_summary = "no new receipts"
            if t_backfilled:
                receipt_summary += f" ({t_backfilled} total{'s' if t_backfilled != 1 else ''} filled in)"
        except Exception as exc:
            logger.warning("receipt scan failed for %s: %s", address, exc)
            receipt_summary = f"error: {exc}"
            errors.append(f"receipts: {exc}")
            v_added, r_added, t_backfilled = 0, 0, 0
    else:
        receipt_summary = "receipt scanning disabled"
        v_added, r_added, t_backfilled = 0, 0, 0

    return {
        "ok": not errors,
        "error": "; ".join(errors) if errors else None,
        "deals_found": len(deals),
        "deals_added": added,
        "restaurants_added": new_restaurants,
        "deal_summary": deal_summary,
        "visits_added": v_added,
        "receipt_restaurants_added": r_added,
        "receipt_summary": receipt_summary,
    }


def run_scan(session: Session, imap_class=imaplib.IMAP4_SSL,
             today: date | None = None) -> dict:
    """Full scan across ALL configured email accounts.

    One IMAP session per account; a failing account is recorded and skipped —
    it never blocks the others. Never raises for scan-time failures (bad
    creds, stalled connection, parse errors): those are recorded into the
    last-result settings and returned as ``{"ok": False, ...}``. Only a missing
    email configuration raises.

    `today` is injectable for tests (defaults to the real current date).
    """
    accounts = list_accounts(session)
    if not accounts:
        raise RuntimeError("No email accounts configured — add one in Settings.")

    deal_parts: list[str] = []
    receipt_parts: list[str] = []
    totals = {
        "deals_found": 0, "deals_added": 0, "restaurants_added": 0,
        "visits_added": 0, "receipt_restaurants_added": 0,
    }
    errors: list[str] = []

    for acct in accounts:
        label = _account_label(acct)
        res = _scan_one_account(session, acct.address, acct.app_password,
                                imap_class, today)
        deal_parts.append(f"{res['deal_summary']} ({label})")
        receipt_parts.append(f"{res['receipt_summary']} ({label})")
        for key in totals:
            totals[key] += res[key]
        if res["error"]:
            errors.append(f"{label}: {res['error']}")

    deal_summary = "; ".join(deal_parts)
    receipt_summary = "; ".join(receipt_parts)
    now = datetime.now().isoformat(timespec="minutes")
    set_setting(session, K_STATUS, "error" if errors else "done")
    set_setting(session, K_LAST_RUN, now)
    set_setting(session, K_LAST_RESULT, deal_summary)
    set_setting(session, K_R_LAST_RUN, now)
    set_setting(session, K_R_LAST_RESULT, receipt_summary)
    session.commit()
    return {
        "ok": not errors,
        "error": "; ".join(errors) if errors else None,
        "deals_found": totals["deals_found"],
        "deals_added": totals["deals_added"],
        "restaurants_added": totals["restaurants_added"],
        "summary": deal_summary,
        "visits_added": totals["visits_added"],
        "receipt_restaurants_added": totals["receipt_restaurants_added"],
        "receipt_summary": receipt_summary,
        "accounts_scanned": len(accounts),
    }


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
                    "No email accounts configured — add one in Settings."
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
