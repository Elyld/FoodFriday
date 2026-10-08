"""Screenshot helper: seeds demo data, serves the app, captures screenshots.

Uses the fauna venv's playwright + Chrome for Testing, no proxy for localhost.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

for _var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_var, None)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ["FOODFRIDAY_DATA_DIR"] = "/tmp/ff-shots"

CHROME = str(Path.home() / "pw-chrome" / "chrome-linux64" / "chrome")

DEMO = [
    ("El Toro Loco", "Mexican", 1, True, 12),
    ("Sakura Sushi", "Sushi", 2, True, 45),
    ("Big Q BBQ", "BBQ", 2, False, 90),
    ("Noodle House", "Ramen", 1, False, 200),
    ("The Fancy Fork", "American", 3, False, None),
    ("Curry in a Hurry", "Indian", 2, True, 30),
    ("Pizza Piazza", "Pizza", 1, False, 8),
    ("Dragon Wok", "Chinese", 1, False, 60),
]


def seed():
    from fastapi.testclient import TestClient
    from app.database import init_db
    from app.main import app

    init_db()
    c = TestClient(app)
    today = date.today()
    ids = {}
    for name, cuisine, tier, fav, days_ago in DEMO:
        r = c.post("/api/restaurants", json={
            "name": name, "cuisine": cuisine, "price_tier": tier, "favorite": fav,
        }).json()
        ids[name] = r["id"]
        if days_ago is not None:
            c.post("/api/visits", json={
                "restaurant_id": r["id"],
                "visited_at": (today - timedelta(days=days_ago)).isoformat(),
                "total": round(20 + (hash(name) % 4000) / 100, 2),
            })
    # demo deal so the pick card shows the badge
    c.post("/api/deals", json={
        "restaurant_id": ids["El Toro Loco"],
        "title": "Taco Tuesday: $2 street tacos",
        "description": "Every Tuesday, dine-in only.",
        "valid_from": (today - timedelta(days=1)).isoformat(),
        "valid_until": (today + timedelta(days=6)).isoformat(),
        "item_keywords": "taco",
        "source": "manual",
    })
    # one excluded restaurant shows the skip marker
    c.post(f"/api/restaurants/{ids['Dragon Wok']}/in-picks")
    # fake Gmail config for the settings screenshot (never real creds)
    c.put("/api/settings", json={
        "gmail_address": "demo@example.com",
        "gmail_app_password": "abcd efgh ijkl mnop",
        "deal_scan_enabled": True,
        "deal_scan_time": "07:00",
        # discover + nudge demo config (fake key/webhook, never real)
        "yelp_api_key": "demo-yelp-key",
        "home_lat": "39.0483",
        "home_lon": "-95.6780",
        "discord_webhook_url": "https://discord.com/api/webhooks/demo/demo",
        "friday_nudge_enabled": True,
        "friday_nudge_time": "10:00",
        "avoid_repeat_cuisine": True,
    })
    from app.database import SessionLocal
    from app.models import Setting
    s = SessionLocal()
    try:
        s.merge(Setting(key="deal_scan_last_run", value="2026-10-07T07:00"))
        s.merge(Setting(key="deal_scan_last_result", value="2 new deals (1 new restaurant added)"))
        s.merge(Setting(key="friday_nudge_last_result", value="sent (2026-10-08T10:00)"))
        s.commit()
    finally:
        s.close()
    # prime the discover cache so the screenshot needs no live Yelp call
    from app.discover import store_cache
    s = SessionLocal()
    try:
        store_cache(s, 39.048, -95.678, 10.0, [
            {"yelp_id": "d1", "name": "The Rustic Spoon", "rating": 4.6,
             "price_tier": 2, "price_label": "$$", "cuisine": "American, Brunch",
             "address": "1200 SW Topeka Blvd", "distance_mi": 1.2},
            {"yelp_id": "d2", "name": "Pho Saigon", "rating": 4.8,
             "price_tier": 1, "price_label": "$", "cuisine": "Vietnamese, Pho",
             "address": "2815 SW 29th St", "distance_mi": 2.4},
            {"yelp_id": "d3", "name": "El Camino Real", "rating": 4.2,
             "price_tier": 1, "price_label": "$", "cuisine": "Mexican, Tacos",
             "address": "3420 SW Topeka Blvd", "distance_mi": 3.1},
        ])
    finally:
        s.close()
    return ids


def main():
    ids = seed()
    env = {**os.environ, "PYTHONPATH": str(BASE)}

    def api_post(path):
        req = urllib.request.Request(
            "http://127.0.0.1:4002" + path, data=b"", method="POST")
        urllib.request.urlopen(req, timeout=5).read()

    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "4002"],
        cwd=str(BASE), env=env,
    )
    try:
        for _ in range(30):
            try:
                urllib.request.urlopen("http://127.0.0.1:4002/api/health", timeout=2)
                break
            except Exception:
                time.sleep(1)

        sys.path.insert(0, str(Path.home() / "workspace" / "fauna" / ".venv" / "lib" / "python3.12" / "site-packages"))
        from playwright.sync_api import sync_playwright

        out = BASE / "docs" / "screenshots"
        out.mkdir(parents=True, exist_ok=True)

        # narrow the field so the pick shot deterministically includes the deal card
        # (Dragon Wok is already off from seed(); leave it alone)
        keep = {"El Toro Loco", "Sakura Sushi", "Big Q BBQ", "Dragon Wok"}
        narrowed = [rid for name, rid in ids.items() if name not in keep]
        for rid in narrowed:
            api_post(f"/api/restaurants/{rid}/in-picks")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])

            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto("http://127.0.0.1:4002/", wait_until="networkidle")
            page.click("#pick-btn")
            page.wait_for_timeout(900)
            page.screenshot(path=str(out / "home-picks.png"))

            # restore everyone, then shoot the restaurants page + deals section
            for rid in narrowed:
                api_post(f"/api/restaurants/{rid}/in-picks")
            page.goto("http://127.0.0.1:4002/restaurants", wait_until="networkidle")
            page.screenshot(path=str(out / "restaurants.png"))
            page.evaluate("document.querySelector('#deal-form').scrollIntoView()")
            page.wait_for_timeout(400)
            page.screenshot(path=str(out / "restaurants-deals.png"))

            mob = browser.new_page(viewport={"width": 390, "height": 844})
            mob.goto("http://127.0.0.1:4002/", wait_until="networkidle")
            mob.click("#pick-btn")
            mob.wait_for_timeout(900)
            mob.screenshot(path=str(out / "mobile-home.png"))

            # somewhere-new mode
            page.goto("http://127.0.0.1:4002/", wait_until="networkidle")
            page.click("#mode-new")
            page.wait_for_timeout(300)
            page.click("#pick-btn")
            page.wait_for_timeout(900)
            page.screenshot(path=str(out / "home-new-mode.png"))

            # discover page (served from the primed cache — no live Yelp call)
            page.goto("http://127.0.0.1:4002/discover", wait_until="networkidle")
            page.wait_for_timeout(600)
            page.click("text=🔍 Search nearby")
            page.wait_for_timeout(900)
            page.screenshot(path=str(out / "discover.png"))

            # spending dashboard
            page.goto("http://127.0.0.1:4002/spending", wait_until="networkidle")
            page.wait_for_timeout(400)
            page.screenshot(path=str(out / "spending.png"))

            page.goto("http://127.0.0.1:4002/settings", wait_until="networkidle")
            page.wait_for_timeout(400)
            page.screenshot(path=str(out / "settings.png"))

            browser.close()
        print("shots done:", sorted(p.name for p in out.glob("*.png")))
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
