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
    for name, cuisine, tier, fav, days_ago in DEMO:
        r = c.post("/api/restaurants", json={
            "name": name, "cuisine": cuisine, "price_tier": tier, "favorite": fav,
        }).json()
        if days_ago is not None:
            c.post("/api/visits", json={
                "restaurant_id": r["id"],
                "visited_at": (today - timedelta(days=days_ago)).isoformat(),
                "total": round(20 + (hash(name) % 4000) / 100, 2),
            })


def main():
    seed()
    env = {**os.environ, "PYTHONPATH": str(BASE)}
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
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])

            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto("http://127.0.0.1:4002/", wait_until="networkidle")
            page.click("#pick-btn")
            page.wait_for_timeout(900)
            page.screenshot(path=str(out / "home-picks.png"))

            page.goto("http://127.0.0.1:4002/restaurants", wait_until="networkidle")
            page.screenshot(path=str(out / "restaurants.png"))

            mob = browser.new_page(viewport={"width": 390, "height": 844})
            mob.goto("http://127.0.0.1:4002/", wait_until="networkidle")
            mob.click("#pick-btn")
            mob.wait_for_timeout(900)
            mob.screenshot(path=str(out / "mobile-home.png"))

            browser.close()
        print("shots done:", sorted(p.name for p in out.glob("*.png")))
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
