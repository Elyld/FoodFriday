"""Parse item names from saved receipt markdowns and backfill them onto the seed.

Reads the receipt markdowns saved under ~/workspace/email/gmail/ (from the
earlier email backfill — no Gmail re-read), matches them to visits in
seed/real-history.seed.json by (chain, date), and writes an `items` list
(newline-joined on import) onto each matched visit.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/parse_receipt_items.py          # report only
    PYTHONPATH=. .venv/bin/python scripts/parse_receipt_items.py --write  # update the seed

Then re-import the seed on the /import page — duplicate visits get their
`items` backfilled automatically.
"""

from __future__ import annotations

import glob
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SEED_PATH = BASE / "seed" / "real-history.seed.json"
MAIL_DIR = Path.home() / "workspace" / "email" / "gmail"


# ---------- chain parsers: markdown body -> [item names] ----------
# (moved to app/receipt_parse.py so the in-app receipt scanner can reuse them)

from app.receipt_parse import (
    parse_caseys_items as parse_caseys,
    parse_chipotle_items as parse_chipotle,
    parse_sonic_items as parse_sonic,
    parse_spangles_items as parse_spangles,
)


PARSERS = {
    "Chipotle": parse_chipotle,
    "Sonic": parse_sonic,
    "Spangles": parse_spangles,
    "Casey's": parse_caseys,
}


def chain_of(text: str) -> str | None:
    head = text[:2000].lower()
    if "chipotle" in head:
        return "Chipotle"
    if "sonic" in head:
        return "Sonic"
    if "spangles" in head:
        return "Spangles"
    if "casey" in head:
        return "Casey's"
    return None


def sent_date(text: str) -> str | None:
    m = re.search(r"Sent \(America/Chicago\):\s*(\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else None


def is_receipt(text: str) -> bool:
    subj = re.search(r"^Subject:\s*(.+)$", text, re.M)
    if not subj:
        return False
    low = subj.group(1).lower()
    return any(k in low for k in ["order", "receipt", "thanks for", "confirmation"])


def pair_and_parse() -> tuple[dict[int, list[str]], list[str]]:
    """Return (visit_index -> items, notes about skips)."""
    seed = json.loads(SEED_PATH.read_text())
    visits = seed["visits"]

    # index receipts by (chain, date)
    receipts: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for f in glob.glob(str(MAIL_DIR / "*.md")):
        p = Path(f)
        text = p.read_text(errors="ignore")
        if not is_receipt(text):
            continue
        chain, d = chain_of(text), sent_date(text)
        if chain in PARSERS and d:
            receipts[(chain, d)].append(p)

    # index seed visits by (restaurant, date)
    by_key: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, v in enumerate(visits):
        if v["restaurant"] in PARSERS:
            by_key[(v["restaurant"], v["visited_at"])].append(i)

    result: dict[int, list[str]] = {}
    notes: list[str] = []
    for key, idxs in sorted(by_key.items()):
        files = receipts.get(key, [])
        if not files:
            notes.append(f"no receipt file for {key[0]} {key[1]} ({len(idxs)} visit(s))")
            continue
        if len(files) != len(idxs):
            notes.append(
                f"count mismatch {key[0]} {key[1]}: {len(files)} files vs {len(idxs)} visits — skipped"
            )
            continue
        parser = PARSERS[key[0]]
        for i, p in zip(sorted(idxs), sorted(files)):
            items = parser(p.read_text(errors="ignore"))
            if items:
                result[i] = items
            else:
                notes.append(f"no items parsed: {p.name} ({key[0]} {key[1]})")
    return result, notes


def main() -> None:
    result, notes = pair_and_parse()
    print(f"matched visits with items: {len(result)}")
    shown = 0
    for i, items in sorted(result.items()):
        if shown < 6:
            print(f"  visit[{i}]: {items}")
            shown += 1
    if notes:
        print(f"\nskips ({len(notes)}):")
        for n in notes[:15]:
            print(f"  - {n}")
    if "--write" in sys.argv:
        seed = json.loads(SEED_PATH.read_text())
        for i, items in result.items():
            seed["visits"][i]["items"] = items
        SEED_PATH.write_text(json.dumps(seed, indent=1))
        print(f"\nwrote items onto {len(result)} visits in {SEED_PATH}")


if __name__ == "__main__":
    main()
