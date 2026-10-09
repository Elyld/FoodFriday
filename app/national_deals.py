"""National promo watcher — polls Reddit's r/fastfood RSS for chain deals.

No API key, no scraping arms race: one polite Atom fetch per scan with a
real User-Agent. Conservative by design — a post only becomes a deal when it
names a recognized chain AND matches a deal pattern. Everything lands with
source="national" and stays user-deletable via the existing deals UI.
"""

from __future__ import annotations

import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta

logger = logging.getLogger(__name__)

REDDIT_FEEDS = [
    "https://www.reddit.com/r/fastfood/new/.rss?limit=50",
]
FETCH_TIMEOUT = 30  # seconds
USER_AGENT = "FoodFriday/1.0 (personal deal scan; one fetch per day)"

# (regex, canonical display name) — matched against the lowercased title.
# His own restaurant names are checked first (see match_chain); these cover
# major chains he hasn't added yet.
CHAIN_PATTERNS: list[tuple[str, str]] = [
    (r"mcdonald'?s", "McDonald's"),
    (r"burger king", "Burger King"),
    (r"wendy'?s", "Wendy's"),
    (r"taco bell", "Taco Bell"),
    (r"\bkfc\b", "KFC"),
    (r"chick-?fil-?a", "Chick-fil-A"),
    (r"chipotle", "Chipotle"),
    (r"domino'?s", "Domino's"),
    (r"pizza hut", "Pizza Hut"),
    (r"papa john'?s", "Papa John's"),
    (r"little caesars", "Little Caesars"),
    (r"subway", "Subway"),
    (r"arby'?s", "Arby's"),
    (r"\bsonic\b", "Sonic"),
    (r"dairy queen", "Dairy Queen"),
    (r"whataburger", "Whataburger"),
    (r"in-?n-?out", "In-N-Out"),
    (r"five guys", "Five Guys"),
    (r"shake shack", "Shake Shack"),
    (r"panera", "Panera"),
    (r"qdoba", "Qdoba"),
    (r"moe'?s", "Moe's"),
    (r"jimmy john'?s", "Jimmy John's"),
    (r"jersey mike'?s", "Jersey Mike's"),
    (r"firehouse subs", "Firehouse Subs"),
    (r"popeyes", "Popeyes"),
    (r"church'?s( chicken)?", "Church's Chicken"),
    (r"raising cane'?s", "Raising Cane's"),
    (r"zaxby'?s", "Zaxby's"),
    (r"bojangles", "Bojangles"),
    (r"culver'?s", "Culver's"),
    (r"steak '?n shake", "Steak 'n Shake"),
    (r"white castle", "White Castle"),
    (r"del taco", "Del Taco"),
    (r"el pollo loco", "El Pollo Loco"),
    (r"jack in the box", "Jack in the Box"),
    (r"carl'?s jr", "Carl's Jr"),
    (r"hardee'?s", "Hardee's"),
    (r"panda express", "Panda Express"),
    (r"schlotzsky'?s", "Schlotzsky's"),
    (r"\bbww\b|buffalo wild wings", "Buffalo Wild Wings"),
    (r"dunkin'?|dunkin donuts", "Dunkin'"),
    (r"starbucks", "Starbucks"),
    (r"krispy kreme", "Krispy Kreme"),
    (r"\ba&w\b", "A&W"),
    (r"long john silver'?s", "Long John Silver's"),
    (r"dave'?s hot chicken", "Dave's Hot Chicken"),
]

# Deal-intent patterns (all matched case-insensitively against the title).
DEAL_PATTERNS: list[str] = [
    r"\b\d+\s*% off\b",
    r"\bhalf (price|off)\b",
    r"\bbogo\b",
    r"\bbuy one\b.{0,20}\bget\b",
    r"\bfree\b",
    r"\$\s*\d+(\.\d{2})?\s*(off\b|pizza|burger|meal|deal)",
    r"\b\d+\s*for\s*\$\s*\d+",
    r"\bcoupon\b",
    r"\bpromo( code)?\b",
]

_BRACKET_RE = re.compile(r"^\s*[\[\(][^\]\)]*[\]\)]\s*")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

DEFAULT_DEAL_DAYS = 14  # assumed validity when the post states no end date


def _norm(s: str | None) -> str:
    # Reddit titles love curly quotes — normalize them before matching.
    s = (s or "").replace("’", "'").replace("‘", "'").replace("ʼ", "'")
    return " ".join(s.strip().lower().split())


def match_chain(title: str, own_names: list[str] | None = None) -> str | None:
    """Canonical chain name for a post title, or None.

    His own restaurant names win (so "Schlotzsky's" matches his entry even
    with odd spelling); then the builtin major-chain patterns.
    """
    t = _norm(title)
    for name in own_names or []:
        n = _norm(name).replace("'", "")
        if n and len(n) >= 4 and n in t.replace("'", ""):
            return name.strip()
    for pattern, canonical in CHAIN_PATTERNS:
        if re.search(pattern, t):
            return canonical
    return None


def looks_like_deal(title: str) -> bool:
    """Does the title read as a promo announcement?"""
    t = _norm(title)
    return any(re.search(p, t) for p in DEAL_PATTERNS)


def clean_title(title: str) -> str:
    """Strip leading [Deal]/[US] style tags for a tidy deal title."""
    return _BRACKET_RE.sub("", title).strip()


def parse_valid_until(title: str, published: date) -> date:
    """Best-effort end date from the title; defaults to 14 days out."""
    t = _norm(title)

    # "Oct 5-11" / "Oct 5–11" / "Oct 5 to 11"
    m = re.search(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
        r"\s+(\d{1,2})\s*(?:[-–]|to)\s*(\d{1,2})\b",
        t,
    )
    if m:
        month = _MONTHS[m.group(1)[:3]]
        day = int(m.group(3))
        year = published.year
        try:
            end = date(year, month, day)
        except ValueError:
            return published + timedelta(days=DEFAULT_DEAL_DAYS)
        if end < published:  # range rolls into next year
            try:
                end = date(year + 1, month, day)
            except ValueError:
                return published + timedelta(days=DEFAULT_DEAL_DAYS)
        return end

    # "10/5-10/11" / "10/11"
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:\s*[-–]\s*(\d{1,2})/(\d{1,2}))?\b", t)
    if m:
        month, day = int(m.group(3) or m.group(1)), int(m.group(4) or m.group(2))
        year = published.year
        try:
            end = date(year, month, day)
        except ValueError:
            return published + timedelta(days=DEFAULT_DEAL_DAYS)
        if end < published:
            try:
                end = date(year + 1, month, day)
            except ValueError:
                return published + timedelta(days=DEFAULT_DEAL_DAYS)
        return end

    # "through Sunday" / "ends Friday" / "thru Monday"
    m = re.search(
        r"\b(?:through|thru|throughout|ends?|until)\s+"
        r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        t,
    )
    if m:
        target = _WEEKDAYS[m.group(1)]
        delta = (target - published.weekday()) % 7 or 7
        return published + timedelta(days=delta)

    return published + timedelta(days=DEFAULT_DEAL_DAYS)


def extract_deal(title: str, published: date,
                 own_names: list[str] | None = None,
                 link: str | None = None) -> dict | None:
    """A Reddit post title -> deal dict for store_deals, or None."""
    if not looks_like_deal(title):
        return None
    chain = match_chain(title, own_names)
    if not chain:
        return None
    valid_until = parse_valid_until(title, published)
    if valid_until < published:
        valid_until = published + timedelta(days=DEFAULT_DEAL_DAYS)
    return {
        "restaurant": chain,
        "title": clean_title(title),
        "description": None,
        "valid_from": published.isoformat(),
        "valid_until": valid_until.isoformat(),
        "item_keywords": "",
        "source": "national",
        "source_url": link,
    }


def fetch_posts(urlopen=None) -> list[dict]:
    """Raw posts from the configured Reddit RSS feeds."""
    posts: list[dict] = []
    opener = urlopen or urllib.request.urlopen
    for feed_url in REDDIT_FEEDS:
        try:
            req = urllib.request.Request(feed_url, headers={"User-Agent": USER_AGENT})
            with opener(req, timeout=FETCH_TIMEOUT) as resp:
                root = ET.fromstring(resp.read())
        except Exception as exc:  # noqa: BLE001 — a dead feed skips, never fails the scan
            logger.warning("national deals: feed failed %s: %s", feed_url, exc)
            continue
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("a:entry", ns):
            title_el = entry.find("a:title", ns)
            updated_el = entry.find("a:updated", ns)
            link_el = entry.find("a:link", ns)
            if title_el is None or not (title_el.text or "").strip():
                continue
            try:
                published = date.fromisoformat((updated_el.text or "")[:10])
            except (ValueError, TypeError, AttributeError):
                published = date.today()
            posts.append(
                {
                    "title": title_el.text.strip(),
                    "link": link_el.attrib.get("href") if link_el is not None else None,
                    "published": published,
                }
            )
    return posts


def fetch_national_deals(own_names: list[str] | None = None,
                         urlopen=None,
                         max_age_days: int = 21) -> list[dict]:
    """Deals parsed from recent Reddit posts. Skips posts older than
    max_age_days so stale reposts don't linger."""
    cutoff = date.today() - timedelta(days=max_age_days)
    deals: list[dict] = []
    seen_titles: set[str] = set()
    for post in fetch_posts(urlopen=urlopen):
        if post["published"] < cutoff:
            continue
        key = _norm(post["title"])
        if key in seen_titles:
            continue
        seen_titles.add(key)
        deal = extract_deal(post["title"], post["published"], own_names,
                            link=post["link"])
        if deal:
            deals.append(deal)
    return deals
