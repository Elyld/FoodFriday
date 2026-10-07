"""Server-rendered HTML pages for FoodFriday."""

from __future__ import annotations

from html import escape as _esc

from app.version import APP_NAME


def esc(s) -> str:
    return _esc("" if s is None else str(s))


def fmt_date(iso: str | None) -> str:
    """2026-07-09 -> Jul 9, 2026. Pass through anything unparseable."""
    if not iso:
        return "never"
    try:
        from datetime import date as _date

        return _date.fromisoformat(iso).strftime("%b %-d, %Y")
    except ValueError:
        return iso


def layout(title: str, body: str, active: str = "") -> str:
    def nav(href: str, label: str, key: str) -> str:
        cls = ' class="active"' if active == key else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · {esc(APP_NAME)}</title>
<link rel="stylesheet" href="/static/css/style.css">
</head>
<body>
<header class="topbar">
  <span class="brand">🍽️ {esc(APP_NAME)}</span>
  <nav>
    {nav("/", "Pick", "home")}
    {nav("/restaurants", "Restaurants", "restaurants")}
    {nav("/history", "History", "history")}
    {nav("/import", "Import", "import")}
  </nav>
</header>
<main>{body}</main>
<div class="toast" id="toast"></div>
<script src="/static/js/app.js"></script>
</body>
</html>"""


def home_page() -> str:
    body = """
<div class="hero">
  <h1>What's for dinner Friday?</h1>
  <p>Three contenders, picked from your rotation. Tap the winner.</p>
  <button class="btn-primary" id="pick-btn" onclick="pickDinner()">🎲 Pick 3 for us</button>
</div>
<div id="pick-area"></div>
<div class="pick-actions" id="pick-actions" style="display:none">
  <button class="btn-secondary" onclick="reroll()">↻ Reroll all three</button>
</div>
"""
    return layout("Pick dinner", body, "home")


def restaurants_page(restaurants: list[dict], deals: list[dict]) -> str:
    if not restaurants:
        rows = '<div class="empty">No restaurants yet. Add your first spot below — or <a href="/import">import</a> your history.</div>'
    else:
        trs = []
        for r in restaurants:
            star = "★" if r["favorite"] else "☆"
            star_cls = "star" if r["favorite"] else "star off"
            price = "$" * (r["price_tier"] or 1)
            last = fmt_date(r["last_visit"])
            in_picks = r.get("include_in_picks", True)
            picks_btn = (
                '<button class="btn-secondary btn-small" onclick="toggleInPicks(%d)" title="Include in Friday picks">🎲 in picks</button>' % r["id"]
                if in_picks else
                '<button class="btn-secondary btn-small muted" onclick="toggleInPicks(%d)" title="Skipped in Friday picks — tap to include">🚫 skipped</button>' % r["id"]
            )
            skip_badge = '' if in_picks else ' <span class="skip-badge">skipped in picks</span>'
            trs.append(f"""<tr>
<td data-label="Name"><strong>{esc(r["name"])}</strong>{skip_badge}<br><span style="color:#7a6552;font-size:.85rem">{esc(r["cuisine"] or "")}</span></td>
<td data-label="Price" class="price">{price}</td>
<td data-label="Visits">{r["visit_count"]}</td>
<td data-label="Last visit">{esc(last)}</td>
<td data-label="Actions" style="white-space:nowrap">
  <button class="{star_cls}" onclick="toggleFav({r["id"]})" title="Favorite">{star}</button>
  {picks_btn}
  <button class="btn-secondary btn-small" onclick="quickLog({r["id"]}, '{esc(r["name"]).replace(chr(39), "")}')">We ate here tonight</button>
  <button class="btn-danger btn-small" onclick="delRestaurant({r["id"]}, '{esc(r["name"]).replace(chr(39), "")}')">Delete</button>
</td></tr>""")
        rows = "<table class='grid'><thead><tr><th>Name</th><th>Price</th><th>Visits</th><th>Last visit</th><th>Actions</th></tr></thead><tbody>" + "".join(trs) + "</tbody></table>"

    options = "".join(
        f'<option value="{r["id"]}">{esc(r["name"])}</option>' for r in restaurants
    )
    if deals:
        deal_items = []
        for d in deals:
            cls = "deal" if d["active"] else "deal expired"
            kw = f'<div class="deal-kw">items: {esc(d["item_keywords"])}</div>' if d["item_keywords"] else ""
            when = []
            if d["valid_from"]:
                when.append("from " + fmt_date(d["valid_from"]))
            if d["valid_until"]:
                when.append("until " + fmt_date(d["valid_until"]))
            when_s = f'<div class="deal-when">{" · ".join(when)}</div>' if when else ""
            state = "active" if d["active"] else "expired"
            deal_items.append(f"""<div class="{cls}">
  <div class="deal-head"><strong>🏷️ {esc(d["title"])}</strong>
    <span class="deal-state">{state}</span>
    <span class="deal-rest">{esc(d["restaurant_name"] or "anywhere")}</span>
    <button class="btn-danger btn-small" onclick="delDeal({d["id"]})">Delete</button></div>
  {f'<div class="deal-desc">{esc(d["description"])}</div>' if d["description"] else ""}
  {when_s}{kw}
</div>""")
        deal_list = '<div class="deal-list">' + "".join(deal_items) + "</div>"
    else:
        deal_list = '<div class="empty">No deals yet. Add one below — active deals boost that restaurant on Friday.</div>'

    body = f"""
<h2>Restaurants</h2>
<form class="card-form" id="add-form" onsubmit="return addRestaurant(event)">
  <h3>Add a restaurant</h3>
  <div class="form-row">
    <div class="field"><label>Name</label><input id="f-name" required maxlength="255"></div>
    <div class="field"><label>Cuisine</label><input id="f-cuisine" maxlength="120" placeholder="Mexican, BBQ, sushi…"></div>
  </div>
  <div class="form-row">
    <div class="field"><label>Price</label><select id="f-price"><option value="1">$</option><option value="2" selected>$$</option><option value="3">$$$</option></select></div>
    <div class="field"><label>Address (optional)</label><input id="f-address" maxlength="255"></div>
  </div>
  <div class="field"><label>Notes</label><input id="f-notes" maxlength="500" placeholder="Great patio, kids love it…"></div>
  <button class="btn-primary btn-small" type="submit">Add restaurant</button>
</form>
{rows}
<h2 style="margin-top:2rem">🏷️ Deals</h2>
<p style="color:#7a6552">Active deals give a restaurant a boost on Friday — extra if the deal covers something you've actually ordered.</p>
<form class="card-form" id="deal-form" onsubmit="return addDeal(event)">
  <h3>Add a deal</h3>
  <div class="form-row">
    <div class="field"><label>Restaurant</label><select id="d-restaurant"><option value="">Anywhere (chain-wide)</option>{options}</select></div>
    <div class="field"><label>Title</label><input id="d-title" required maxlength="255" placeholder="$1.49 Mozz Sticks today"></div>
  </div>
  <div class="form-row">
    <div class="field"><label>Valid from (optional)</label><input id="d-from" type="date"></div>
    <div class="field"><label>Valid until (optional)</label><input id="d-until" type="date"></div>
  </div>
  <div class="field"><label>Description (optional)</label><input id="d-desc" maxlength="500"></div>
  <div class="field"><label>Item keywords (optional, comma-separated)</label><input id="d-kw" maxlength="500" placeholder="mozz sticks, corn dog"></div>
  <button class="btn-primary btn-small" type="submit">Add deal</button>
</form>
{deal_list}
"""
    return layout("Restaurants", body, "restaurants")


def history_page(visits: list[dict]) -> str:
    if not visits:
        body = '<h2>History</h2><div class="empty">No visits logged yet. Tap <strong>“We ate here tonight”</strong> on any restaurant, or pick a winner on the <a href="/">home page</a>.</div>'
    else:
        trs = []
        for v in visits:
            total = f"${v['total']:.2f}" if v["total"] is not None else "—"
            trs.append(f"""<tr>
<td data-label="Date">{fmt_date(v["visited_at"])}</td>
<td data-label="Restaurant"><strong>{esc(v["restaurant_name"])}</strong></td>
<td data-label="Total">{total}</td>
<td data-label="Source">{esc(v["source"] or "manual")}</td>
<td data-label=""><button class="btn-danger btn-small" onclick="delVisit({v["id"]})">Delete</button></td>
</tr>""")
        body = "<h2>History</h2><table class='grid'><thead><tr><th>Date</th><th>Restaurant</th><th>Total</th><th>Source</th><th></th></tr></thead><tbody>" + "".join(trs) + "</tbody></table>"
    return layout("History", body, "history")


def import_page() -> str:
    body = """
<h2>Import</h2>
<p>Bring in restaurants and visits from a JSON file. You'll get a preview before anything is saved, and re-importing the same file won't create duplicates.</p>
<form class="card-form" id="import-form" onsubmit="return previewImport(event)">
  <div class="field"><label>Seed JSON file</label><input type="file" id="import-file" accept=".json,application/json" required></div>
  <button class="btn-primary btn-small" type="submit">Preview import</button>
</form>
<div id="import-preview"></div>
<div class="preview-box">
  <strong>Format:</strong>
  <pre style="overflow-x:auto;font-size:.85rem">{
  "restaurants": [
    {"name": "Taco Town", "cuisine": "Mexican",
     "price_tier": 1, "notes": "", "favorite": false}
  ],
  "visits": [
    {"restaurant": "Taco Town",
     "visited_at": "2026-06-05", "total": 32.50,
     "source": "import", "external_id": "gmail:abc123",
     "items": ["Bowl", "Chips"]}
  ],
  "deals": [
    {"restaurant": "Taco Town", "title": "$1.49 Mozz Sticks today",
     "description": "App-only", "valid_from": "2026-10-07",
     "valid_until": "2026-10-07", "item_keywords": ["mozz sticks"],
     "source": "email"}
  ]
}</pre>
</div>
"""
    return layout("Import", body, "import")
