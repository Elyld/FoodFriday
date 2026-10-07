"""Scan Gmail promo emails for concrete restaurant deals.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/scan_deals.py            # print review table
    PYTHONPATH=. .venv/bin/python scripts/scan_deals.py --write   # also write seed/deals.seed.json

The seed file is local-only (gitignored): upload it on the /import page.
Only concrete offers are kept — vague brand fluff is skipped.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

# pure parsing lives in the app so the in-container IMAP scanner reuses it
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.deal_parse import (  # noqa: E402
    clean_title,
    detect_chain,
    extract_keywords,
    parse_expiry,
    parse_promo,
)

__all__ = ["clean_title", "detect_chain", "extract_keywords", "parse_expiry", "parse_promo"]

BASE = Path(__file__).resolve().parent.parent
SEED_PATH = BASE / "seed" / "deals.seed.json"

PROMO_QUERIES = [
    'from:sonicdrivein (subject:$ OR subject:free OR subject:deal OR subject:today OR subject:only) newer_than:30d',
    'from:chipotle (subject:$ OR subject:BOGO OR subject:free OR subject:deal OR subject:off OR subject:today OR subject:claim) newer_than:30d',
    'from:tacobell (subject:$ OR subject:BOGO OR subject:free OR subject:deal OR subject:off) newer_than:30d',
    'from:dominos (subject:$ OR subject:BOGO OR subject:free OR subject:deal OR subject:off) newer_than:30d',
    'from:arbys (subject:$ OR subject:BOGO OR subject:free OR subject:deal OR subject:off) newer_than:30d',
    'from:schlotzskys (subject:$ OR subject:BOGO OR subject:free OR subject:deal OR subject:off OR subject:today) newer_than:30d',
]


# ---------- Gmail I/O ----------

def _triage(query: str, max_n: int = 30) -> list[dict]:
    out = subprocess.run(
        ["hatch_gws_cli", "gmail", "+triage", "--query", query,
         "--max", str(max_n), "--format", "json"],
        capture_output=True, text=True, check=True,
    )
    text = out.stdout.strip()
    if not text.startswith("{"):
        return []  # e.g. "No messages found matching query: ..."
    return json.loads(text).get("messages", [])


def _read(mid: str) -> tuple[str, str, str, date]:
    out = subprocess.run(
        ["hatch_gws_cli", "gmail", "+read", "--id", mid, "--format", "json"],
        capture_output=True, text=True, check=True,
    )
    d = json.loads(out.stdout)
    path = d["message_file"]["path"]
    body = Path(path).read_text(errors="ignore")
    sent_raw = d.get("message_sent_at", {}).get("user_local", "")[:10]
    sent = date.fromisoformat(sent_raw)
    return d.get("from", ""), d.get("subject", ""), body, sent


def scan() -> list[dict]:
    seen: set[str] = set()
    deals: list[dict] = []
    for q in PROMO_QUERIES:
        for m in _triage(q):
            mid = m["id"]
            if mid in seen:
                continue
            seen.add(mid)
            try:
                sender, subject, body, sent = _read(mid)
            except Exception as e:
                print(f"  skip {mid}: {e}", file=sys.stderr)
                continue
            deal = parse_promo(sender, subject, body, sent)
            if deal:
                deals.append(deal)
    # dedupe by (restaurant, title)
    uniq: dict[tuple[str, str], dict] = {}
    for d in deals:
        uniq.setdefault((d["restaurant"], d["title"].lower()), d)
    return sorted(uniq.values(), key=lambda d: (d["restaurant"], d["title"]))


def main() -> None:
    deals = scan()
    print(f"{'chain':16} {'until':12} title")
    for d in deals:
        print(f"{d['restaurant']:16} {d['valid_until'] or 'open-ended':12} {d['title'][:60]}")
        if d["item_keywords"]:
            print(f"{'':16} {'':12} keywords: {d['item_keywords']}")
    if "--write" in sys.argv:
        SEED_PATH.parent.mkdir(parents=True, exist_ok=True)
        # include any restaurants not already in his DB as new entries
        chains = sorted({d["restaurant"] for d in deals})
        seed = {
            "_note": "DEALS — scanned from Gmail promos. Local only, never commit.",
            "restaurants": [
                {"name": c, "cuisine": None, "price_tier": 2, "notes": "added via deal scan",
                 "favorite": False}
                for c in chains
            ],
            "visits": [],
            "deals": deals,
        }
        SEED_PATH.write_text(json.dumps(seed, indent=1))
        print(f"\nwrote {SEED_PATH} ({len(deals)} deals)")


if __name__ == "__main__":
    main()
