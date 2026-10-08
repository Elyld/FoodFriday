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
    {nav("/discover", "🧭 Discover", "discover")}
    {nav("/spending", "💰 Spending", "spending")}
    {nav("/history", "History", "history")}
    {nav("/import", "Import", "import")}
    {nav("/settings", "Settings", "settings")}
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
  <div class="mode-toggle">
    <button class="btn-secondary btn-small mode-btn active" id="mode-friday" onclick="setPickMode('friday')">🍽️ Friday picks</button>
    <button class="btn-secondary btn-small mode-btn" id="mode-new" onclick="setPickMode('new')">✨ Somewhere new</button>
  </div>
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
            track = r.get("track_visits", True)
            track_btn = (
                '<button class="btn-secondary btn-small" onclick="toggleTrack(%d)" title="Receipt scanner logs visits here">🧾 tracking</button>' % r["id"]
                if track else
                '<button class="btn-secondary btn-small muted" onclick="toggleTrack(%d)" title="Receipt scanner skips this place — tap to track">📵 not tracked</button>' % r["id"]
            )
            track_badge = '' if track else ' <span class="skip-badge">receipts off</span>'
            trs.append(f"""<tr>
<td data-label="Name"><strong>{esc(r["name"])}</strong>{skip_badge}{track_badge}<br><span style="color:#7a6552;font-size:.85rem">{esc(r["cuisine"] or "")}</span></td>
<td data-label="Price" class="price">{price}</td>
<td data-label="Visits">{r["visit_count"]}</td>
<td data-label="Last visit">{esc(last)}</td>
<td data-label="Actions" style="white-space:nowrap">
  <button class="{star_cls}" onclick="toggleFav({r["id"]})" title="Favorite">{star}</button>
  {picks_btn}
  {track_btn}
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
            in_picks = "" if v.get("exclude_from_picks") else "checked"
            trs.append(f"""<tr>
<td data-label="Date">{fmt_date(v["visited_at"])}</td>
<td data-label="Restaurant"><strong>{esc(v["restaurant_name"])}</strong></td>
<td data-label="Total">{total}</td>
<td data-label="Source">{esc(v["source"] or "manual")}</td>
<td data-label="In picks"><label class="check pick-check"><input type="checkbox" {in_picks} onchange="toggleVisitPicks({v["id"]}, this.checked)" title="Uncheck to keep this trip out of Friday picks"> <span>in picks</span></label></td>
<td data-label=""><button class="btn-danger btn-small" onclick="delVisit({v["id"]})">Delete</button></td>
</tr>""")
        body = "<h2>History</h2><p style=\"color:#7a6552;font-size:.9rem\">Uncheck “in picks” on a trip to keep it out of Friday's picker (kid's solo runs, breakfast pitstops…). It stays in your history and spending totals either way.</p><table class='grid'><thead><tr><th>Date</th><th>Restaurant</th><th>Total</th><th>Source</th><th>In picks</th><th></th></tr></thead><tbody>" + "".join(trs) + "</tbody></table>"
    return layout("History", body, "history")


def discover_page() -> str:
    body = """
<h2>🧭 Discover nearby</h2>
<p style="color:#7a6552">New places around you, from Yelp — spots already in your list are hidden.
Add one and it's in the rotation (and eligible for "✨ Somewhere new").</p>
<div id="discover-setup"></div>
<div class="card-form" id="discover-controls" style="display:none">
  <div class="form-row">
    <div class="field"><label>Radius</label>
      <select id="disc-radius">
        <option value="5">5 km</option>
        <option value="10" selected>10 km</option>
        <option value="25">25 km</option>
      </select></div>
    <div class="field"><label>&nbsp;</label>
      <div style="display:flex;gap:.6rem;flex-wrap:wrap">
        <button class="btn-primary btn-small" onclick="searchDiscover(false)">🔍 Search nearby</button>
        <button class="btn-secondary btn-small" onclick="searchDiscover(true)">↻ Refresh (live)</button>
      </div></div>
  </div>
  <div id="discover-cache-note" style="color:#7a6552;font-size:.85rem"></div>
</div>
<div id="discover-results"></div>
"""
    return layout("Discover", body, "discover")


def spending_page(a: dict) -> str:
    def bar(label: str, value: float, pct: float) -> str:
        return f"""<div class="bar-row">
  <span class="bar-label">{esc(label)}</span>
  <span class="bar-track"><span class="bar-fill" style="width:{pct:.1f}%"></span></span>
  <span class="bar-val">${value:,.2f}</span>
</div>"""

    if a["monthly"]:
        monthly = "".join(
            bar(m["label"], m["total"], (m["total"] / a["max_month"] * 100) if a["max_month"] else 0)
            for m in a["monthly"]
        )
    else:
        monthly = '<div class="empty">No spending logged yet.</div>'

    if a["top_restaurants"]:
        top = "".join(
            f"""<tr><td data-label="Restaurant"><strong>{esc(r["name"])}</strong></td>
<td data-label="Visits">{r["visits"]}</td><td data-label="Total">${r["total"]:,.2f}</td></tr>"""
            for r in a["top_restaurants"]
        )
        top_tbl = f"<table class='grid'><thead><tr><th>Restaurant</th><th>Visits</th><th>Total</th></tr></thead><tbody>{top}</tbody></table>"
    else:
        top_tbl = '<div class="empty">No spending logged yet.</div>'

    if a["cuisines"]:
        cuis = "".join(
            bar(c["name"], c["total"], (c["total"] / a["max_cuisine"] * 100) if a["max_cuisine"] else 0)
            for c in a["cuisines"]
        )
    else:
        cuis = '<div class="empty">No spending logged yet.</div>'

    note = (
        f'<p style="color:#7a6552;font-size:.85rem">{a["without_totals"]} visit(s) without a total were excluded from these numbers.</p>'
        if a["without_totals"] else ""
    )

    body = f"""
<h2>💰 Spending</h2>
<div class="stat-row">
  <div class="stat"><div class="stat-num">${a["total"]:,.2f}</div><div class="stat-label">all-time</div></div>
  <div class="stat"><div class="stat-num">${a["average"]:,.2f}</div><div class="stat-label">avg / visit</div></div>
  <div class="stat"><div class="stat-num">{a["visit_count"]}</div><div class="stat-label">visits with totals</div></div>
</div>
{note}
<h3>Last 6 months</h3>
<div class="bars">{monthly}</div>
<h3>Top restaurants</h3>
{top_tbl}
<h3>By cuisine</h3>
<div class="bars">{cuis}</div>
"""
    return layout("Spending", body, "spending")


def settings_page(s: dict) -> str:
    enabled = "checked" if s["deal_scan_enabled"] else ""
    r_enabled = "checked" if s.get("receipt_scan_enabled", True) else ""
    last_run = fmt_date(s["deal_scan_last_run"][:10]) if s.get("deal_scan_last_run") else "never"
    last_result = s.get("deal_scan_last_result") or "—"
    r_last_run = fmt_date(s["receipt_scan_last_run"][:10]) if s.get("receipt_scan_last_run") else "never"
    r_last_result = s.get("receipt_scan_last_result") or "—"
    scan_status = s.get("deal_scan_status") or "idle"
    accounts = s.get("email_accounts") or []
    status = (
        "✅ configured — scans daily"
        if s["configured"]
        else "⚠️ not configured"
    )
    scan_line = (
        "<p style=\"color:#7a6552;font-size:.85rem\">🔄 <strong>Scan running…</strong> "
        "this usually takes 1–3 minutes. The result will appear below when it's done.</p>"
        if scan_status == "running"
        else (
            f"<p style=\"color:#7a6552;font-size:.85rem\">Last scan: {esc(last_run)} — "
            f"deals: {esc(last_result)} · receipts: {esc(r_last_result)}</p>"
        )
    )
    if accounts:
        acct_rows = "".join(
            f"""<div class="acct-row"><span class="acct-label">{esc(a["label"] or "—")}</span>
<span class="acct-addr">{esc(a["address"])}</span>
<span class="acct-set">password set</span>
<button class="btn-danger btn-small" type="button" onclick="deleteAccount({a["id"]})">Remove</button></div>"""
            for a in accounts
        )
    else:
        acct_rows = '<div class="empty">No email accounts yet — add one below.</div>'
    body = f"""
<h2>Settings</h2>
<h3>📧 Scan email accounts</h3>
<p style="color:#7a6552">{status}. The scanner reads each account's mail over IMAP
(read-only — nothing is marked read or deleted): promo emails become deals,
and order receipts become visits in your history. A deal or receipt from a
chain that isn't in your list yet gets its restaurant auto-added
so the Friday boost works.</p>
<div class="card-form">
  {acct_rows}
  <h4 style="margin:.8rem 0 .4rem">Add an account</h4>
  <div class="form-row">
    <div class="field"><label>Label (optional)</label>
      <input id="a-label" maxlength="120" placeholder="mine, family…"></div>
    <div class="field"><label>Gmail address</label>
      <input id="a-address" maxlength="255" placeholder="you@gmail.com"></div>
  </div>
  <div class="field"><label>App password</label>
    <input id="a-password" type="password" maxlength="255" placeholder="paste from myaccount.google.com/apppasswords" autocomplete="new-password"></div>
  <div style="display:flex;gap:.6rem;flex-wrap:wrap">
    <button class="btn-primary btn-small" type="button" onclick="addAccount()">Add account</button>
  </div>
</div>
<form class="card-form" id="settings-form" onsubmit="return saveSettings(event)">
  <div class="form-row">
    <div class="field"><label>Daily scan time</label>
      <input id="s-time" type="time" value="{esc(s["deal_scan_time"])}"></div>
    <div class="field"><label>&nbsp;</label>
      <label class="check"><input id="s-enabled" type="checkbox" {enabled}> deal scanning enabled</label>
      <label class="check"><input id="s-receipt-enabled" type="checkbox" {r_enabled}> receipt scanning enabled</label></div>
  </div>
  <div style="display:flex;gap:.6rem;flex-wrap:wrap">
    <button class="btn-primary btn-small" type="submit">Save</button>
    <button class="btn-secondary btn-small" type="button" onclick="scanNow()">🔍 Scan now</button>
  </div>
  {scan_line}
</form>
<div id="scan-result"></div>
<p style="color:#7a6552;font-size:.9rem"><strong>One-time setup per account:</strong> the Google account needs
2-step verification, then create an app password at
<code>myaccount.google.com/apppasswords</code> and paste it above. Each password is stored only
in this app's own database, never leaves your server except to log in to Gmail's IMAP,
and is never shown back to you.</p>
<h3>🧭 Discover (Yelp)</h3>
<p style="color:#7a6552">Find new places around you on the <a href="/discover">Discover</a> tab.
Needs a free Yelp API key: <code>developer.yelp.com</code> → Create App → Starter plan
(free, no credit card). Results are cached for 24 hours so the free quota lasts.</p>
<form class="card-form" id="discover-settings-form" onsubmit="return saveDiscoverSettings(event)">
  <div class="field"><label>Yelp API key</label>
    <input id="y-key" type="password" maxlength="255" placeholder="{'set — leave blank to keep' if s["yelp_api_key_set"] else 'paste your Yelp Fusion API key'}" autocomplete="new-password"></div>
  <div class="form-row">
    <div class="field"><label>Home latitude</label>
      <input id="y-lat" maxlength="20" placeholder="39.048" value="{esc(s["home_lat"])}"></div>
    <div class="field"><label>Home longitude</label>
      <input id="y-lon" maxlength="20" placeholder="-95.678" value="{esc(s["home_lon"])}"></div>
  </div>
  <div style="display:flex;gap:.6rem;flex-wrap:wrap">
    <button class="btn-primary btn-small" type="submit">Save</button>
    <button class="btn-secondary btn-small" type="button" onclick="useMyLocation()">📍 Use my location</button>
  </div>
</form>
<h3>🔔 Friday nudge (Discord)</h3>
<p style="color:#7a6552">Every Friday morning, FoodFriday posts the 3 picks to your Discord.
Create a webhook in your server: channel settings → Integrations → Webhooks → New Webhook → Copy URL.</p>
<form class="card-form" id="nudge-settings-form" onsubmit="return saveNudgeSettings(event)">
  <div class="field"><label>Discord webhook URL</label>
    <input id="n-webhook" type="password" maxlength="500" placeholder="{'set — leave blank to keep' if s["discord_webhook_set"] else 'https://discord.com/api/webhooks/…'}"></div>
  <div class="form-row">
    <div class="field"><label>Friday nudge time</label>
      <input id="n-time" type="time" value="{esc(s["friday_nudge_time"])}"></div>
    <div class="field"><label>&nbsp;</label>
      <label class="check"><input id="n-enabled" type="checkbox" {'checked' if s["friday_nudge_enabled"] else ""}> nudge enabled</label></div>
  </div>
  <div style="display:flex;gap:.6rem;flex-wrap:wrap">
    <button class="btn-primary btn-small" type="submit">Save</button>
    <button class="btn-secondary btn-small" type="button" onclick="sendTestNudge()">📣 Send test</button>
    <button class="btn-danger btn-small" type="button" onclick="clearIntegrations()">Forget Yelp key &amp; webhook</button>
  </div>
  <p style="color:#7a6552;font-size:.85rem">Last nudge: {esc(s.get("friday_nudge_last_result") or "never")}</p>
  <div id="nudge-result"></div>
</form>
<h3>🎲 Picker</h3>
<form class="card-form" onsubmit="return savePickerSettings(event)">
  <label class="check"><input id="p-avoid-cuisine" type="checkbox" {'checked' if s["avoid_repeat_cuisine"] else ""}>
    Don't pick the same cuisine as the most recent visit</label>
  <div style="margin-top:.6rem"><button class="btn-primary btn-small" type="submit">Save</button></div>
</form>
"""
    return layout("Settings", body, "settings")


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
