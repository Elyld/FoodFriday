"""Pure receipt-parsing helpers — no I/O.

Turns an order confirmation/receipt email (sender, subject, body, sent date)
into a visit dict, or None when the email isn't a recognized receipt.

Patterns were validated against Josh's real mail during the manual backfill
(scripts/parse_receipt_items.py and the seed-building parsers). Chain-specific
total regexes are tried first; a conservative generic fallback
("Total ... $X" on exactly one unambiguous line) catches the rest. When a
chain is recognized but no total parses, the visit is still logged with
total=None (confirmation-only email) so recency weighting sees it.
"""

from __future__ import annotations

import re

# ---------- item parsers (moved from scripts/parse_receipt_items.py) ----------


def parse_chipotle_items(body: str) -> list[str]:
    """Table rows like `| Chicken Bowl | |`; customization rows have commas."""
    items = []
    noise = re.compile(r"payment method|grams|impact|howgood|powered by|order summary", re.I)
    for line in body.splitlines():
        m = re.match(r"^\|\s*([^|]+?)\s*\|\s*\|$", line.strip())
        if not m:
            continue
        name = m.group(1).strip()
        if not name or ", " in name or "$(" in name or name.startswith("$"):
            continue
        if len(name) > 60 or noise.search(name):
            continue
        items.append(name)
    return items


def parse_sonic_items(body: str) -> list[str]:
    """`Nx` row then item-name row (blank lines ok); modifiers start with • or + $."""
    items = []
    expect_item = False
    for line in body.splitlines():
        line = line.strip()
        if re.match(r"^\|\s*\d+x\s*\|$", line):
            expect_item = True
            continue
        if expect_item and re.match(r"^(\|\s*\|)?$", line):
            continue  # blank or empty table row between quantity and item
        if expect_item:
            m = re.match(r"^\|\s*([^|•+]+?)\s*\|$", line)
            expect_item = False
            if m:
                name = m.group(1).strip()
                if name and not name.startswith("$"):
                    items.append(name)
    return items


def parse_spangles_items(body: str) -> list[str]:
    """After 'Your receipt': `Nx` line, then the item name on the next line."""
    items = []
    in_receipt = False
    expect_item = False
    for line in body.splitlines():
        s = line.strip()
        if "Your receipt" in s:
            in_receipt = True
            continue
        if not in_receipt:
            continue
        if re.match(r"^\d+[x×]$", s):
            expect_item = True
            continue
        if expect_item and s:
            expect_item = False
            if ", " not in s and not s.startswith("$"):
                items.append(s)
    return items


def parse_caseys_items(body: str) -> list[str]:
    """After 'Order Summary': lines between --- separators; skip $-promo lines."""
    items = []
    in_summary = False
    for line in body.splitlines():
        s = line.strip()
        if "Order Summary" in s:
            in_summary = True
            continue
        if not in_summary:
            continue
        if s in ("---", ""):
            continue
        if s.startswith("$") or s.startswith("|"):
            continue  # promo/deal line or table markup, not an item
        if re.match(r"^(Subtotal|Total|Tax|Payment|Thank you)", s, re.I):
            break
        if re.match(r"^(Original|Thin|Stuffed|Hand.?Tossed)\s+(Crust\s+)?(Small|Medium|Large)$", s, re.I):
            continue  # size modifier, not an item
        if "cash" in s.lower() and "casey" in s.lower():
            continue  # loyalty line
        items.append(s)
    # dedupe, keep order
    return list(dict.fromkeys(items))


# ---------- total parsing ----------

_AMT = r"([\d,]+\.\d{2})"  # one capture group: the amount


def _generic_total(text: str) -> float | None:
    """Exactly one unambiguous 'Total ... $X' line → amount, else None.

    Also handles the total label and the amount on adjacent lines
    ("Order total\\n$25.58"). Conservative on purpose: multiple amounts on
    total-lines (or none) means we don't guess — the visit is logged with
    total=None.
    """
    lines = text.splitlines()
    found: set[str] = set()
    for i, line in enumerate(lines):
        if "$" not in line:
            # total label on its own line, amount on the next non-empty line?
            if re.search(r"(?i)(?<!sub)total\s*:?\s*$", line.strip()):
                for nxt in lines[i + 1 : i + 3]:
                    m = re.fullmatch(r"\s*\$" + _AMT + r"\s*", nxt)
                    if m:
                        found.add(m.group(1))
                        break
                    if nxt.strip():
                        break
            continue
        if not re.search(r"(?i)(?<!sub)total", line):
            continue
        for m in re.finditer(r"\$" + _AMT, line):
            found.add(m.group(1))
    if len(found) == 1:
        return float(next(iter(found)).replace(",", ""))
    return None


def _try_patterns(text: str, patterns: list[str]) -> float | None:
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except (ValueError, IndexError):
                continue
    return None


def _last_amount(text: str) -> float | None:
    """Whataburger format: the order total is the last $ amount in the section."""
    amounts = re.findall(r"\$" + _AMT, text)
    if amounts:
        try:
            return float(amounts[-1].replace(",", ""))
        except ValueError:
            return None
    return None


# sender fragments, subject fragments (any-of), total regexes (in order)
RECEIPT_CHAINS: list[dict] = [
    {
        "name": "Chipotle",
        "sender": ("chipotle",),
        "subject": ("thanks for your order",),
        "total": [r"\|\s*Total\s*\|\s*\n+\|\s*\$" + _AMT],
        "items": parse_chipotle_items,
    },
    {
        "name": "Taco Bell",
        "sender": ("tacobell",),
        "subject": ("success, we got your order",),
        "total": [],
        "items": None,
    },
    {
        "name": "Domino's",
        "sender": ("dominos",),
        "subject": ("domino's order",),
        "total": [r"\|\s*Total:\s*\$" + _AMT + r"\s*\|"],
        "items": None,
    },
    {
        "name": "Casey's",
        "sender": ("casey",),
        "subject": ("thanks for your order",),
        "total": [r"\|\s*Total\s*\|(?:[^\n]*\n){1,3}\|\s*\$" + _AMT],
        "items": parse_caseys_items,
    },
    {
        "name": "Sonic",
        "sender": ("sonic",),
        "subject": ("your order is confirmed",),
        "total": [r"\|\s*Total\s*\|\s*\n+\|\s*\$" + _AMT],
        "items": parse_sonic_items,
    },
    {
        "name": "Spangles",
        "sender": ("spangles", "order.online"),
        "subject": ("your order from spangles",),
        "total": [r"Total Charged.*?\$" + _AMT],
        "items": parse_spangles_items,
    },
    {
        "name": "Chili's",
        "sender": ("chilis",),
        "subject": ("order confirmed",),
        "total": [r"\*\*Total\*\*\s*\*\*\$" + _AMT + r"\*\*"],
        "items": None,
    },
    {
        "name": "Panera",
        "sender": ("panera",),
        "subject": ("rapid pick-up",),
        "total": [r"Order Total\s*\$" + _AMT],
        "items": None,
    },
    {
        "name": "Buffalo Wild Wings",
        "sender": ("buffalowildwings",),
        "subject": ("order receipt",),
        "total": [r"\|\s*Total:\s*\|\s*\$" + _AMT],
        "items": None,
    },
    {
        "name": "Whataburger",
        "sender": ("whataburger",),
        "subject": ("order confirmed",),
        "total": "last_amount",
        "items": None,
    },
    {
        "name": "Little Caesars",
        "sender": ("littlecaesars",),
        "subject": ("pizza receipt",),
        "total": [r"\|\s*Order Total\s*\|\s*\$" + _AMT],
        "items": None,
    },
    {
        "name": "Arby's",
        "sender": ("arbys",),
        "subject": ("confirmed", "confirmation"),
        "total": [
            r"\|\s*ORDER TOTAL:\s*\|\s*\|\s*\$" + _AMT,
            r"\|\s*Total:\s*\|\s*\$" + _AMT,
        ],
        "items": None,
    },
    {
        "name": "KFC",
        "sender": ("kfc",),
        "subject": ("kfc order", "received your kfc"),
        "total": [r"\|\s*Total\s*\|\s*\$" + _AMT],
        "items": None,
    },
    {
        "name": "Wendy's",
        "sender": ("wendys",),
        "subject": ("digital order receipt",),
        "total": [r"\|\s*Total\s*\|\s*\$" + _AMT],
        "items": None,
    },
    {
        "name": "Dairy Queen",
        "sender": ("dairyqueen",),
        "subject": ("order confirmation",),
        "total": [r"\|\s*TOTAL\s*\|\s*\$" + _AMT],
        "items": None,
    },
    {
        "name": "Raising Cane's",
        "sender": ("raisingcanes",),
        "subject": ("order received",),
        "total": [r"\|\s*TOTAL\s*\|\s*\$" + _AMT],
        "items": None,
    },
    {
        "name": "Pizza Hut",
        "sender": ("pizzahut",),
        "subject": ("thank you for your pizza hut order", "order confirmation"),
        "total": [r"Order Total:\s*\$" + _AMT],
        "items": None,
    },
    {
        "name": "McDonald's",
        "sender": ("mcdonalds",),
        "subject": ("receipt", "thanks for your order", "order confirmation",
                    "your mcdonald"),
        "total": [r"(?<!sub)total\s*:?\s*\$" + _AMT],
        "items": None,
    },
    {
        "name": "Schlotzsky's",
        "sender": ("schlotzsky",),
        "subject": ("receipt", "thanks for your order", "order confirmation",
                    "your schlotzsky"),
        "total": [r"(?<!sub)total\s*:?\s*\$" + _AMT],
        "items": None,
    },
    {
        "name": "Church's Chicken",
        "sender": ("churchschicken",),
        "subject": ("receipt", "thanks for your order", "order confirmation",
                    "your church's"),
        "total": [r"(?<!sub)total\s*:?\s*\$" + _AMT],
        "items": None,
    },
    # delivery aggregators — generic total fallback only
    {
        "name": "DoorDash",
        "sender": ("doordash",),
        "subject": ("receipt",),
        "total": [],
        "items": None,
    },
    {
        "name": "Uber Eats",
        "sender": ("uber",),
        "subject": ("uber eats",),
        "total": [],
        "items": None,
    },
    {
        "name": "Grubhub",
        "sender": ("grubhub",),
        "subject": ("receipt",),
        "total": [],
        "items": None,
    },
]


def detect_receipt(sender: str, subject: str) -> dict | None:
    """Return the chain spec when sender+subject identify a receipt, else None."""
    s = (sender or "").lower()
    subj = (subject or "").lower()
    for chain in RECEIPT_CHAINS:
        if not any(frag in s for frag in chain["sender"]):
            continue
        if not any(frag in subj for frag in chain["subject"]):
            continue
        return chain
    return None


def receipt_sender_domains() -> list[str]:
    """Unique sender fragments for IMAP FROM searches (longest first).

    Only IMAP-safe atoms are returned — a fragment with a space, quote, or
    other special character would make the SEARCH command unparseable
    (server responds BAD) and fail the whole receipt phase.
    """
    frags: set[str] = set()
    for chain in RECEIPT_CHAINS:
        frags.update(chain["sender"])
    safe = [f for f in frags if re.fullmatch(r"[A-Za-z0-9._-]+", f)]
    return sorted(safe, key=len, reverse=True)


def parse_receipt(chain: dict, body: str) -> dict:
    """Parse total (+ items where supported). Never raises on weird input."""
    total = None
    try:
        spec = chain["total"]
        if spec == "last_amount":
            total = _last_amount(body)
        elif spec:
            total = _try_patterns(body, spec)
        if total is None:
            total = _generic_total(body)
    except Exception:
        total = None
    items: list[str] = []
    parser = chain.get("items")
    if parser:
        try:
            items = parser(body) or []
        except Exception:
            items = []
    return {"total": total, "items": items}
