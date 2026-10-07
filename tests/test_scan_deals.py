"""Tests for the Gmail promo-scan parser (scripts/scan_deals.py).

Uses redacted fixtures in tests/fixtures/ (marketing content only).
Run:  cd ~/workspace/foodfriday && PYTHONPATH=. .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from scan_deals import extract_keywords, parse_expiry, parse_promo  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> tuple[str, str, str, date]:
    text = (FIX / name).read_text()
    sender = re.search(r"^From:\s*(.+)$", text, re.M).group(1)
    subject = re.search(r"^Subject:\s*(.+)$", text, re.M).group(1).strip()
    sent = date.fromisoformat(re.search(r"Sent \(America/Chicago\):\s*(\d{4}-\d{2}-\d{2})", text).group(1))
    return sender, subject, text, sent


def test_arby_steak_bowl():
    sender, subject, body, sent = load("arby_steak_bowl.md")
    d = parse_promo(sender, subject, body, sent)
    assert d is not None
    assert d["restaurant"] == "Arby's"
    assert d["valid_until"] == "2026-11-01"  # "Valid thru 11/1/2026"
    assert d["source"] == "email"
    assert "sandwich" in (d["item_keywords"] or "")


def test_chipotle_free_chips():
    sender, subject, body, sent = load("chipotle_free_chips.md")
    d = parse_promo(sender, subject, body, sent)
    assert d is not None
    assert d["restaurant"] == "Chipotle"
    assert d["valid_until"] == "2026-10-11"  # "by Sunday, October 11"
    assert "joshua" not in d["title"].lower()  # name stripped from title
    assert "chip" in (d["item_keywords"] or "")


def test_schlotzsky_bogo_wednesday():
    sender, subject, body, sent = load("schlotzsky_bogo.md")
    d = parse_promo(sender, subject, body, sent)
    assert d is not None
    assert d["restaurant"] == "Schlotzsky's"
    assert d["valid_until"] is None  # recurring weekly -> open-ended
    assert "pizza" in (d["item_keywords"] or "")


def test_expired_deal_skipped():
    # Sonic $1.49 mozz sticks was "today only" on 2026-09-22 -> long expired
    sender, subject, body, sent = load("sonic_mozz_today.md")
    d = parse_promo(sender, subject, body, sent)
    assert d is None


def test_brand_fluff_skipped():
    d = parse_promo("Arby's <arbys@emails.arbys.com>",
                    "Don't miss 15% off merch", "body", date(2026, 10, 1))
    assert d is None  # merch, not food


def test_expiry_today_only():
    vf, vu = parse_expiry("TODAY ONLY! $0.99 Corn Dogs", "body", date(2026, 9, 15))
    assert (vf, vu) == (date(2026, 9, 15), date(2026, 9, 15))


def test_expiry_unclear_defaults_to_7_days():
    vf, vu = parse_expiry("Free Fries Friday", "some vague promo text", date(2026, 10, 1))
    assert (vf, vu) == (date(2026, 10, 1), date(2026, 10, 8))


def test_keywords_strip_promo_words():
    kw = extract_keywords("PSA: $1.49 Mozz Sticks today")
    assert "mozz" in kw and "stick" in kw
    assert "$" not in kw and "today" not in kw
