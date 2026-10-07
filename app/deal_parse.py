"""Pure deal-parsing helpers — no I/O.

Shared by scripts/scan_deals.py (Gmail CLI) and app/deal_scan.py (in-app IMAP).
Turns a promo email (sender, subject, body, sent date) into a deal dict, or
None when the email is brand fluff, merch, expired, or from an unknown chain.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

# domain fragment -> display name
CHAINS = {
    "chipotle": "Chipotle",
    "sonicdrivein": "Sonic",
    "sonic": "Sonic",
    "tacobell": "Taco Bell",
    "dominos": "Domino's",
    "arbys": "Arby's",
    "schlotzskys": "Schlotzsky's",
    "panera": "Panera",
    "chilis": "Chili's",
    "applebees": "Applebee's",
    "wendys": "Wendy's",
    "kfc": "KFC",
    "whataburger": "Whataburger",
    "littlecaesars": "Little Caesars",
    "pizzahut": "Pizza Hut",
    "dairyqueen": "Dairy Queen",
    "raisingcanes": "Raising Cane's",
    "culvers": "Culver's",
    "buffalowildwings": "Buffalo Wild Wings",
    "jimmyjohns": "Jimmy John's",
    "chick-fil-a": "Chick-fil-A",
    "freddys": "Freddy's",
}

STOPWORDS = {
    "free", "bogo", "today", "only", "deal", "deals", "off", "claim", "redeem",
    "your", "now", "ends", "tonight", "new", "get", "buy", "enjoy", "try",
    "psa", "alert", "offer", "offers", "exclusive", "reward", "rewards",
    "app", "online", "visit", "later", "back", "score", "just", "for", "you",
    "the", "and", "with", "from", "this", "joshua", "almost", "gone", "clock",
    "running", "officially", "weekend", "waiting", "day", "before", "line",
    "have", "claimed", "wednesday", "monday", "tuesday", "thursday", "friday",
    "saturday", "sunday", "way", "make", "lineup",
}


def detect_chain(sender: str, subject: str) -> str | None:
    text = f"{sender} {subject}".lower()
    for key, name in CHAINS.items():
        if key in text:
            return name
    return None


def chain_domains() -> list[str]:
    """Domain fragments to search for in IMAP FROM clauses (unique, longest first)."""
    keys = sorted({k for k in CHAINS}, key=len, reverse=True)
    # "sonic" is a substring of "sonicdrivein" — drop shorter keys contained in longer ones
    out = []
    for k in keys:
        if not any(k != o and k in o for o in out):
            out.append(k)
    return out


def parse_expiry(subject: str, body: str, sent: date) -> tuple[date | None, date | None]:
    """Return (valid_from, valid_until). Conservative: unclear -> sent+7d."""
    text = f"{subject}\n{body}"
    # "Valid thru 11/1/2026"
    m = re.search(r"[Vv]alid thru\s+(\d{1,2})/(\d{1,2})/(\d{2,4})", text)
    if m:
        mo, dy, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        yr = yr + 2000 if yr < 100 else yr
        return sent, date(yr, mo, dy)
    # "Offer ... from 10/2 - 10/4/26"
    m = re.search(r"from\s+(\d{1,2})/(\d{1,2})\s*-\s*(\d{1,2})/(\d{1,2})/(\d{2,4})", text)
    if m:
        mo1, dy1, mo2, dy2, yr = (int(m.group(i)) for i in range(1, 6))
        yr = yr + 2000 if yr < 100 else yr
        return date(yr, mo1, dy1), date(yr, mo2, dy2)
    # "use your exclusive offer by Sunday, October 11" / "ends Sunday"
    m = re.search(
        r"(?:by|ends?|through)\s+(?:(monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+)?"
        r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})",
        text, re.I,
    )
    if m:
        month = datetime.strptime(m.group(2)[:3], "%b").month
        day = int(m.group(3))
        yr = sent.year + (1 if (month, day) < (sent.month, sent.day) else 0)
        return sent, date(yr, month, day)
    low = text.lower()
    if "today only" in low or "ends tonight" in low:
        return sent, sent
    if re.search(r"every\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", low):
        return sent, None  # recurring — open-ended, picker caps at 30d
    return sent, sent + timedelta(days=7)


def extract_keywords(title: str) -> str:
    """Heuristic item keywords from a deal title."""
    t = re.sub(r"[^\w\s$%]", " ", title.lower())
    t = re.sub(r"\$\s*[\d.]+", " ", t)          # prices
    t = re.sub(r"\b\d+\s*(pc|piece|%)\b", " ", t)  # counts/percents
    words = [w for w in t.split() if w and w not in STOPWORDS and len(w) > 2]
    # de-pluralize lightly for matching ("sticks" -> "stick")
    normed = []
    for w in words:
        if w.endswith("ies"):
            w = w[:-3] + "y"
        elif w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        normed.append(w)
    seen, out = set(), []
    for w in normed:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return ", ".join(out[:4])


def clean_title(subject: str) -> str:
    t = re.sub(r"^joshua[:,]?\s+", "", subject, flags=re.I)  # "Joshua: Claim your…"
    t = re.sub(r"[^\w\s$%.,'!&+-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:120]


def parse_promo(
    sender: str,
    subject: str,
    body: str,
    sent: date,
    today: date | None = None,
) -> dict | None:
    """Turn one promo email into a deal dict, or None if it's just fluff."""
    today = today or date.today()
    chain = detect_chain(sender, subject)
    if not chain:
        return None
    low = subject.lower()
    # skip pure brand/marketing noise
    if not re.search(r"\$|bogo|free|%|off\b|deal|for \$|¢", low):
        return None
    if re.search(r"merch|newsletter|survey|feedback|points expire|gift card", low):
        return None
    valid_from, valid_until = parse_expiry(subject, body, sent)
    if valid_until and valid_until < today:
        return None  # already expired
    title = clean_title(subject)
    # description: first concrete-looking sentence from the body
    desc = None
    for line in body.splitlines():
        line = line.strip()
        if re.match(r"^(Subject|From|Date|To|Message-ID):", line, re.I):
            continue
        if 20 < len(line) < 220 and re.search(r"\$|bogo|free|%|off\b", line, re.I):
            if not re.search(r"unsubscribe|privacy|terms|http", line, re.I):
                desc = re.sub(r"\s+", " ", line)[:300]
                break
    return {
        "restaurant": chain,
        "title": title,
        "description": desc,
        "valid_from": valid_from.isoformat() if valid_from else None,
        "valid_until": valid_until.isoformat() if valid_until else None,
        "item_keywords": extract_keywords(title) or None,
        "source": "email",
    }
