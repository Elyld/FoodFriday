"""Tests for the receipt item parsers (scripts/parse_receipt_items.py).

Run:  cd ~/workspace/foodfriday && PYTHONPATH=. .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from parse_receipt_items import (  # noqa: E402
    parse_caseys,
    parse_chipotle,
    parse_sonic,
    parse_spangles,
)


def test_chipotle_skips_customizations_and_footer():
    body = """
| Josh | $15.50 |
| | |
| Chicken Bowl | |
| White Rice (Extra), Black Beans (Extra), Sour Cream | |
| Chips | |
| Tortilla on the Side | |
| PAYMENT METHOD | |
| Less Carbon in the Atmosphere 193.3 GRAMS | |
"""
    assert parse_chipotle(body) == ["Chicken Bowl", "Chips", "Tortilla on the Side"]


def test_sonic_quantity_then_item():
    body = """
| 1x |
| |
| Med SONIC Blast® made with M&M’S® Chocolate Candies |
| $5.99 |
| 1x |
| Sm Ched 'R' Bites |
| $3.99 |
| • Remove Ice |
"""
    items = parse_sonic(body)
    assert "Med SONIC Blast® made with M&M’S® Chocolate Candies" in items
    assert "Sm Ched 'R' Bites" in items
    assert not any("Remove Ice" in i for i in items)


def test_spangles_quantity_marker():
    body = """
###### Your receipt

Order #abc123

1x

Garlic Parmesan Bacon Cheese Steakburger Value Pak

Large Fry, Large Drink, Dr Pepper, No Seasoning

$18.98

2×

Fountain Drinks

$3.00
"""
    items = parse_spangles(body)
    assert items == ["Garlic Parmesan Bacon Cheese Steakburger Value Pak", "Fountain Drinks"]


def test_caseys_skips_promo_and_markup():
    body = """
Order Summary
---
 $5 8ct Wings with Any Large Pizza
Pepperoni Pizza
Original Large
---
 $5 8ct Wings with Any Large Pizza
Sausage Pizza
---
| | |
Subtotal
"""
    items = parse_caseys(body)
    assert "Pepperoni Pizza" in items
    assert "Sausage Pizza" in items
    assert not any(i.startswith("$") or i.startswith("|") for i in items)
    assert "Subtotal" not in items
