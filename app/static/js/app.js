/* FoodFriday interactions: pick flow, quick logging, CRUD. */

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2200);
}

async function api(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return r.status === 204 ? null : r.json();
}

// ---- Pick flow ----
let currentPicks = [];   // restaurant ids currently shown
let vetoed = [];         // ids ruled out this round

function priceStr(tier) { return '$'.repeat(tier || 1); }

function fmtDate(iso) {
  if (!iso) return 'Never been';
  const d = new Date(iso + 'T12:00:00');
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function renderPicks(picks) {
  currentPicks = picks.map(p => p.id);
  const area = document.getElementById('pick-area');
  document.getElementById('pick-actions').style.display = 'flex';
  area.innerHTML = '<div class="cards">' + picks.map(p => `
    <div class="pick-card">
      <button class="veto" title="Not this one — replace it" onclick="vetoCard(${p.id})">✕</button>
      <div class="price">${priceStr(p.price_tier)}</div>
      <h3>${escapeHtml(p.name)}</h3>
      <div class="cuisine">${escapeHtml(p.cuisine || '')}${p.favorite ? ' ★' : ''}</div>
      ${(p.deal_titles || []).map(t => `<div class="deal-badge">🏷️ ${escapeHtml(t)}</div>`).join('')}
      <div><span class="reason">${escapeHtml(p.reason)}</span></div>
      <div class="last">${p.last_visit ? 'Last visit: ' + fmtDate(p.last_visit) : 'Never been'}</div>
      <button class="eat" onclick="eatHere(${p.id}, ${JSON.stringify(p.name)})">We ate here ✓</button>
    </div>`).join('') + '</div>';
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function fetchPicks(keep, veto) {
  const area = document.getElementById('pick-area');
  area.innerHTML = '<div class="spinner">🎲 Picking…</div>';
  try {
    const data = await api('POST', '/api/pick', { keep_ids: keep, veto_ids: veto });
    if (!data.picks.length) {
      area.innerHTML = '<div class="empty">No restaurants yet. <a href="/restaurants">Add some</a> and spin again.</div>';
      return;
    }
    renderPicks(data.picks);
  } catch (e) {
    area.innerHTML = '<div class="empty">Couldn\'t pick: ' + escapeHtml(e.message) + '</div>';
  }
}

function pickDinner() { vetoed = []; fetchPicks([], []); }
function reroll() { vetoed = []; fetchPicks([], []); }

function vetoCard(id) {
  vetoed.push(id);
  const keep = currentPicks.filter(x => x !== id);
  fetchPicks(keep, vetoed);
}

async function eatHere(id, name) {
  try {
    await api('POST', '/api/visits', { restaurant_id: id });
    toast('Logged! Enjoy ' + name + ' 🎉');
    fetchPicks([], []);
  } catch (e) { toast('Couldn\'t log: ' + e.message); }
}

// ---- Restaurants ----
async function addRestaurant(e) {
  e.preventDefault();
  const body = {
    name: document.getElementById('f-name').value.trim(),
    cuisine: document.getElementById('f-cuisine').value.trim() || null,
    price_tier: parseInt(document.getElementById('f-price').value, 10),
    address: document.getElementById('f-address').value.trim() || null,
    notes: document.getElementById('f-notes').value.trim() || null,
  };
  try {
    await api('POST', '/api/restaurants', body);
    location.reload();
  } catch (e) { toast('Couldn\'t add: ' + e.message); }
  return false;
}

async function toggleFav(id) {
  try { await api('POST', '/api/restaurants/' + id + '/favorite'); location.reload(); }
  catch (e) { toast('Couldn\'t update: ' + e.message); }
}

async function toggleInPicks(id) {
  try { await api('POST', '/api/restaurants/' + id + '/in-picks'); location.reload(); }
  catch (e) { toast('Couldn\'t update: ' + e.message); }
}

async function quickLog(id, name) {
  try {
    await api('POST', '/api/visits', { restaurant_id: id });
    toast('Logged tonight at ' + name + ' 🎉');
    location.reload();
  } catch (e) { toast('Couldn\'t log: ' + e.message); }
}

async function delRestaurant(id, name) {
  if (!confirm('Delete ' + name + ' and all its visits?')) return;
  try { await api('DELETE', '/api/restaurants/' + id); location.reload(); }
  catch (e) { toast('Couldn\'t delete: ' + e.message); }
}

async function delVisit(id) {
  if (!confirm('Delete this visit?')) return;
  try { await api('DELETE', '/api/visits/' + id); location.reload(); }
  catch (e) { toast('Couldn\'t delete: ' + e.message); }
}

// ---- Deals ----
async function addDeal(e) {
  e.preventDefault();
  const rid = document.getElementById('d-restaurant').value;
  const body = {
    restaurant_id: rid ? parseInt(rid, 10) : null,
    title: document.getElementById('d-title').value.trim(),
    description: document.getElementById('d-desc').value.trim() || null,
    valid_from: document.getElementById('d-from').value || null,
    valid_until: document.getElementById('d-until').value || null,
    item_keywords: document.getElementById('d-kw').value.trim() || null,
  };
  try {
    await api('POST', '/api/deals', body);
    location.reload();
  } catch (e) { toast('Couldn\'t add deal: ' + e.message); }
  return false;
}

async function delDeal(id) {
  if (!confirm('Delete this deal?')) return;
  try { await api('DELETE', '/api/deals/' + id); location.reload(); }
  catch (e) { toast('Couldn\'t delete deal: ' + e.message); }
}

// ---- Settings (deal scanner) ----
async function saveSettings(e) {
  e.preventDefault();
  const body = {
    gmail_address: document.getElementById('s-address').value.trim() || null,
    gmail_app_password: document.getElementById('s-password').value || null,
    deal_scan_enabled: document.getElementById('s-enabled').checked,
    deal_scan_time: document.getElementById('s-time').value || null,
  };
  try {
    await api('PUT', '/api/settings', body);
    toast('Settings saved ✓');
    location.reload();
  } catch (err) { toast('Couldn\'t save: ' + err.message); }
  return false;
}

async function scanNow() {
  const box = document.getElementById('scan-result');
  box.innerHTML = '<div class="spinner">🔍 Scan started in the background — this usually takes 1–3 minutes. Watching for the result…</div>';
  try {
    const data = await api('POST', '/api/deals/scan');
    // 202 {"status": "started"} — poll below for completion.
    // Poll the settings endpoint until the background scan finishes.
    const deadline = Date.now() + 6 * 60 * 1000;
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 10000));
      const s = await api('GET', '/api/settings');
      if (s.deal_scan_status !== 'running') {
        const result = s.deal_scan_last_result || 'done';
        box.innerHTML = '<div class="preview-box"><strong>Scan done:</strong> ' + escapeHtml(result) + '.</div>';
        toast('Scan complete 🎉');
        return;
      }
    }
    box.innerHTML = '<div class="empty">Still scanning — refresh the Settings page to see the result.</div>';
  } catch (err) {
    const msg = /already running/.test(err.message)
      ? 'A scan is already running — check back in a bit.'
      : 'Scan failed: ' + err.message;
    box.innerHTML = '<div class="empty">' + escapeHtml(msg) + '</div>';
  }
}

async function clearGmail() {
  if (!confirm('Forget the stored Gmail address and app password? The scanner will stop.')) return;
  try {
    await api('POST', '/api/settings/clear-gmail');
    location.reload();
  } catch (err) { toast('Couldn\'t clear: ' + err.message); }
}

let importPayload = null;

async function previewImport(e) {
  e.preventDefault();
  const file = document.getElementById('import-file').files[0];
  if (!file) return false;
  const fd = new FormData();
  fd.append('file', file);
  const box = document.getElementById('import-preview');
  box.innerHTML = '<div class="spinner">Reading…</div>';
  try {
    const r = await fetch('/api/import/preview', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.statusText);
    importPayload = await file.text();
    const dealItems = (data.deal_sample || []).map(s => '<li>🏷️ ' + escapeHtml(s) + '</li>').join('');
    box.innerHTML = `<div class="preview-box">
      <strong>${data.new_restaurants} new restaurants · ${data.new_visits} new visits · ${data.new_deals || 0} new deals</strong>
      ${data.skipped ? `<div style="color:#7a6552">${escapeHtml(data.skipped)}</div>` : ''}
      ${data.skipped_deals ? `<div style="color:#7a6552">${escapeHtml(data.skipped_deals)}</div>` : ''}
      <ul>${(data.sample || []).map(s => '<li>' + escapeHtml(s) + '</li>').join('')}${dealItems}</ul>
      <button class="btn-primary btn-small" onclick="confirmImport()">Import it ✓</button>
    </div>`;
  } catch (err) {
    box.innerHTML = '<div class="empty">Import failed: ' + escapeHtml(err.message) + '</div>';
  }
  return false;
}

async function confirmImport() {
  if (!importPayload) return;
  try {
    const data = await api('POST', '/api/import/confirm', JSON.parse(importPayload));
    document.getElementById('import-preview').innerHTML =
      `<div class="preview-box"><strong>Done:</strong> ${data.restaurants_added} restaurants, ${data.visits_added} visits, ${data.deals_added || 0} deals added${data.items_backfilled ? `, ${data.items_backfilled} visits got item details` : ''}.</div>`;
    toast('Import complete 🎉');
  } catch (e) { toast('Import failed: ' + e.message); }
}
