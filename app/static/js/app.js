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
let pickMode = 'friday'; // 'friday' | 'new'
let includeCuisine = false;

function setPickMode(mode) {
  pickMode = mode;
  includeCuisine = false;
  vetoed = [];
  document.getElementById('mode-friday').classList.toggle('active', mode === 'friday');
  document.getElementById('mode-new').classList.toggle('active', mode === 'new');
  document.getElementById('pick-btn').textContent = mode === 'new' ? '✨ Pick 3 new spots' : '🎲 Pick 3 for us';
  document.getElementById('pick-area').innerHTML = '';
  document.getElementById('pick-actions').style.display = 'none';
}

function priceStr(tier) { return '$'.repeat(tier || 1); }

function fmtDate(iso) {
  if (!iso) return 'Never been';
  const d = new Date(iso + 'T12:00:00');
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function pickCardsHtml(picks) {
  return '<div class="cards">' + picks.map(p => `
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

function renderPicks(picks) {
  currentPicks = picks.map(p => p.id);
  document.getElementById('pick-actions').style.display = 'flex';
  document.getElementById('pick-area').innerHTML = pickCardsHtml(picks);
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function fetchPicks(keep, veto) {
  const area = document.getElementById('pick-area');
  area.innerHTML = '<div class="spinner">🎲 Picking…</div>';
  try {
    const data = await api('POST', '/api/pick', { keep_ids: keep, veto_ids: veto, mode: pickMode, include_cuisine: includeCuisine });
    if (!data.picks.length) {
      if (pickMode === 'new') {
        area.innerHTML = '<div class="empty">No untried places — <a href="/discover">hit Discover</a> to add some.</div>';
      } else {
        area.innerHTML = '<div class="empty">No restaurants yet. <a href="/restaurants">Add some</a> and spin again.</div>';
      }
      return;
    }
    let banner = '';
    if (data.skipped_cuisine) {
      banner = `<div class="preview-box">Skipping <strong>${escapeHtml(data.skipped_cuisine)}</strong> — that's what you had last time.
        <a href="#" onclick="includeCuisineAnyway();return false;">Include it anyway</a></div>`;
    }
    currentPicks = data.picks.map(p => p.id);
    document.getElementById('pick-actions').style.display = 'flex';
    area.innerHTML = banner + pickCardsHtml(data.picks);
  } catch (e) {
    area.innerHTML = '<div class="empty">Couldn\'t pick: ' + escapeHtml(e.message) + '</div>';
  }
}

function includeCuisineAnyway() {
  includeCuisine = true;
  vetoed = [];
  fetchPicks([], []);
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

async function toggleTrack(id) {
  try { await api('POST', '/api/restaurants/' + id + '/track-visits'); location.reload(); }
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

// ---- Settings (scanner) ----
async function saveSettings(e) {
  e.preventDefault();
  const body = {
    deal_scan_enabled: document.getElementById('s-enabled').checked,
    deal_scan_time: document.getElementById('s-time').value || null,
    receipt_scan_enabled: document.getElementById('s-receipt-enabled').checked,
  };
  try {
    await api('PUT', '/api/settings', body);
    toast('Settings saved ✓');
    location.reload();
  } catch (err) { toast('Couldn\'t save: ' + err.message); }
  return false;
}

async function addAccount() {
  const label = document.getElementById('a-label').value.trim() || null;
  const address = document.getElementById('a-address').value.trim();
  const password = document.getElementById('a-password').value;
  if (!address || !password) { toast('Address and app password are required'); return; }
  try {
    await api('POST', '/api/settings/email-accounts',
      { label, address, app_password: password });
    toast('Account added ✓');
    location.reload();
  } catch (err) { toast('Couldn\'t add account: ' + err.message); }
}

async function deleteAccount(id) {
  if (!confirm('Remove this email account? The scanner will stop checking it.')) return;
  try {
    await api('DELETE', '/api/settings/email-accounts/' + id);
    location.reload();
  } catch (err) { toast('Couldn\'t remove: ' + err.message); }
}

async function toggleVisitPicks(id, inPicks) {
  try {
    await api('PATCH', '/api/visits/' + id, { exclude_from_picks: !inPicks });
  } catch (err) {
    toast('Couldn\'t update: ' + err.message);
    location.reload();
  }
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
        const deals = s.deal_scan_last_result || 'done';
        const receipts = s.receipt_scan_last_result || '—';
        box.innerHTML = '<div class="preview-box"><strong>Scan done:</strong> deals: ' +
          escapeHtml(deals) + ' · receipts: ' + escapeHtml(receipts) + '.</div>';
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

// (clearGmail removed — email accounts are managed per-account above)

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

// ---- Discover (Yelp nearby) ----
let discoverBusinesses = [];

async function discoverInit() {
  try {
    const s = await api('GET', '/api/settings');
    if (!s.home_lat || !s.home_lon) {
      document.getElementById('discover-setup').innerHTML =
        '<div class="empty">Add your <strong>home location</strong> in ' +
        '<a href="/settings">Settings</a> to search for new places nearby. ' +
        'No API key needed — search runs on OpenStreetMap.</div>';
      return;
    }
    document.getElementById('discover-controls').style.display = 'block';
  } catch (e) {
    document.getElementById('discover-setup').innerHTML =
      '<div class="empty">Couldn\'t load settings: ' + escapeHtml(e.message) + '</div>';
  }
}

async function searchDiscover(refresh) {
  const box = document.getElementById('discover-results');
  const note = document.getElementById('discover-cache-note');
  const radius = parseFloat(document.getElementById('disc-radius').value);
  box.innerHTML = '<div class="spinner">🔍 Searching nearby…</div>';
  note.textContent = '';
  try {
    const data = await api('GET', `/api/discover?radius_km=${radius}&refresh=${refresh ? 1 : 0}`);
    discoverBusinesses = data.businesses;
    const via = data.provider === 'yelp' ? 'Yelp' : 'OpenStreetMap';
    if (data.cached && data.cached_at) {
      const d = new Date(data.cached_at);
      note.textContent = `Via ${via} · cached ${d.toLocaleString()} — hit Refresh for a live search.`;
    } else if (!data.cached) {
      note.textContent = `Via ${via} · live results, cached for 24h.`;
    }
    if (!discoverBusinesses.length) {
      box.innerHTML = '<div class="empty">Nothing new nearby — everything found is already in your list. 🎉</div>';
      return;
    }
    box.innerHTML = '<div class="cards">' + discoverBusinesses.map((b, i) => `
      <div class="pick-card">
        <div class="price">${escapeHtml(b.price_label || '')}</div>
        <h3>${escapeHtml(b.name)}</h3>
        <div class="cuisine">${escapeHtml(b.cuisine || '')}</div>
        <div>${b.rating != null ? '⭐ ' + b.rating : 'no rating'}${b.distance_mi != null ? ' · ' + b.distance_mi + ' mi' : ''}</div>
        <div class="last">${escapeHtml(b.address || '')}</div>
        <button class="eat" id="disc-add-${i}" onclick="addDiscovered(${i})">➕ Add to my restaurants</button>
      </div>`).join('') + '</div>';
  } catch (e) {
    box.innerHTML = '<div class="empty">Search failed: ' + escapeHtml(e.message) + '</div>';
  }
}

async function addDiscovered(i) {
  const b = discoverBusinesses[i];
  const btn = document.getElementById('disc-add-' + i);
  btn.disabled = true;
  try {
    const r = await api('POST', '/api/discover/add', {
      yelp_id: b.yelp_id, name: b.name, cuisine: b.cuisine || null,
      price_tier: b.price_tier, address: b.address || null, rating: b.rating,
    });
    btn.textContent = r.already ? 'Already in your list ✓' : 'Added ✓';
    toast(r.already ? 'Already in your list' : 'Added to your restaurants 🎉');
  } catch (e) {
    btn.disabled = false;
    toast('Couldn\'t add: ' + e.message);
  }
}

// ---- Settings: discover / nudge / picker ----
async function saveDiscoverSettings(e) {
  e.preventDefault();
  try {
    await api('PUT', '/api/settings', {
      yelp_api_key: document.getElementById('y-key').value || null,
      home_lat: document.getElementById('y-lat').value.trim() || null,
      home_lon: document.getElementById('y-lon').value.trim() || null,
    });
    toast('Discover settings saved ✓');
    location.reload();
  } catch (err) { toast('Couldn\'t save: ' + err.message); }
  return false;
}

function useMyLocation() {
  if (!navigator.geolocation) { toast('Geolocation not available in this browser'); return; }
  toast('Locating…');
  navigator.geolocation.getCurrentPosition(
    pos => {
      document.getElementById('y-lat').value = pos.coords.latitude.toFixed(4);
      document.getElementById('y-lon').value = pos.coords.longitude.toFixed(4);
      toast('Location filled in — hit Save ✓');
    },
    () => toast('Couldn\'t get your location'),
    { timeout: 10000 }
  );
}

async function saveNudgeSettings(e) {
  e.preventDefault();
  try {
    await api('PUT', '/api/settings', {
      discord_webhook_url: document.getElementById('n-webhook').value || null,
      friday_nudge_enabled: document.getElementById('n-enabled').checked,
      friday_nudge_time: document.getElementById('n-time').value || null,
    });
    toast('Nudge settings saved ✓');
    location.reload();
  } catch (err) { toast('Couldn\'t save: ' + err.message); }
  return false;
}

async function sendTestNudge() {
  const box = document.getElementById('nudge-result');
  box.innerHTML = '<div class="spinner">Sending test…</div>';
  try {
    await api('POST', '/api/settings/test-nudge');
    box.innerHTML = '<div class="preview-box"><strong>Test sent ✓</strong> — check your Discord channel.</div>';
  } catch (e) {
    box.innerHTML = '<div class="empty">Test failed: ' + escapeHtml(e.message) + '</div>';
  }
}

async function clearIntegrations() {
  if (!confirm('Forget the stored Yelp API key and Discord webhook? Discover and the Friday nudge will stop.')) return;
  try {
    await api('POST', '/api/settings/clear-integrations');
    location.reload();
  } catch (err) { toast('Couldn\'t clear: ' + err.message); }
}

async function savePickerSettings(e) {
  e.preventDefault();
  try {
    await api('PUT', '/api/settings', {
      avoid_repeat_cuisine: document.getElementById('p-avoid-cuisine').checked,
    });
    toast('Picker settings saved ✓');
  } catch (err) { toast('Couldn\'t save: ' + err.message); }
  return false;
}

// page init: run discover setup when on the discover page
if (document.getElementById('discover-results') && typeof discoverInit === 'function') {
  discoverInit();
}

// ---- Crave ("what am I feeling?") ----
function craveChip(t) {
  document.getElementById('crave-input').value = t;
  crave();
}

async function crave() {
  const input = document.getElementById('crave-input');
  const text = input.value.trim();
  const area = document.getElementById('crave-area');
  if (!text) return;
  area.innerHTML = '<div class="spinner">🤔 Thinking…</div>';
  try {
    const data = await api('POST', '/api/crave', { text });
    if (!data.suggestions.length) {
      area.innerHTML = '<div class="empty">Nothing matched — try different words, or <a href="/discover">discover</a> new spots.</div>';
      return;
    }
    let banner = '';
    const via = (data.intent && data.intent.via) || 'keyword';
    if (via === 'keyword') {
      banner = '<div class="preview-box">No AI key set — matching on keywords. Add an OpenRouter key in Settings for smarter parsing.</div>';
    }
    if (data.note) banner += '<div class="preview-box">' + escapeHtml(data.note) + '</div>';
    area.innerHTML = banner + '<div class="cards">' + data.suggestions.map(craveCardHtml).join('') + '</div>';
  } catch (e) {
    area.innerHTML = '<div class="empty">Couldn\'t suggest: ' + escapeHtml(e.message) + '</div>';
  }
}

function craveCardHtml(s) {
  const reasons = (s.reasons || []).map(r => '<div><span class="reason">' + escapeHtml(r) + '</span></div>').join('');
  const deals = (s.deal_titles || []).map(t => '<div class="deal-badge">🏷️ ' + escapeHtml(t) + '</div>').join('');
  if (s.kind === 'new') {
    const dist = s.distance_mi != null ? ' · ' + s.distance_mi + ' mi' : '';
    return `<div class="pick-card">
      <div class="price">${priceStr(s.price_tier)}</div>
      <h3>${escapeHtml(s.name)}</h3>
      <div class="cuisine">${escapeHtml(s.cuisine || '')} · ✨ New to you${dist}</div>
      ${s.address ? `<div class="last">${escapeHtml(s.address)}</div>` : ''}
      ${reasons}${deals}
      <button class="eat" onclick='craveAdd(this)' data-payload='${escapeHtml(JSON.stringify(s.add_payload))}'>➕ Add to my restaurants</button>
    </div>`;
  }
  return `<div class="pick-card">
    <div class="price">${priceStr(s.price_tier)}</div>
    <h3>${escapeHtml(s.name)}</h3>
    <div class="cuisine">${escapeHtml(s.cuisine || '')}${s.favorite ? ' ★' : ''}</div>
    ${reasons}${deals}
    <div class="last">${s.last_visit ? 'Last visit: ' + fmtDate(s.last_visit) : 'Never been'}</div>
    <button class="eat" onclick="craveEatHere(${s.id}, ${JSON.stringify(s.name)})">We ate here ✓</button>
  </div>`;
}

async function craveEatHere(id, name) {
  try {
    await api('POST', '/api/visits', { restaurant_id: id });
    toast('Logged! Enjoy ' + name + ' 🎉');
    crave();
  } catch (e) { toast('Couldn\'t log: ' + e.message); }
}

async function craveAdd(btn) {
  try {
    const payload = JSON.parse(btn.getAttribute('data-payload'));
    const r = await api('POST', '/api/discover/add', payload);
    toast('Added ' + r.name + ' ✓ — tap "We ate here" after you go.');
    crave();
  } catch (e) { toast('Couldn\'t add: ' + e.message); }
}

// ---- Crave settings (OpenRouter model dropdown) ----
let craveModelCache = [];

async function saveCraveSettings(e) {
  e.preventDefault();
  const key = document.getElementById('c-key').value;
  const model = document.getElementById('c-model').value;
  const payload = {
    crave_model: model || null,
    crave_model_free_only: document.getElementById('c-model-free-only').checked,
  };
  if (key) payload.openrouter_api_key = key;
  try {
    await api('PUT', '/api/settings', payload);
    document.getElementById('crave-settings-result').innerHTML =
      '<div class="notice">✓ Saved.</div>';
    document.getElementById('c-key').value = '';
  } catch (err) {
    document.getElementById('crave-settings-result').innerHTML =
      '<div class="notice">Couldn\'t save: ' + escapeHtml(err.message) + '</div>';
  }
  return false;
}

function renderCraveModelOptions() {
  const sel = document.getElementById('c-model-select');
  const inp = document.getElementById('c-model');
  const freeOnly = document.getElementById('c-model-free-only').checked;
  const saved = sel.getAttribute('data-saved') || '';
  const list = freeOnly ? craveModelCache.filter(m => m.free) : craveModelCache;
  let html = '';
  list.forEach(m => {
    const label = m.name + (m.free ? ' (free)' : '');
    const selAttr = (m.id === saved || (!saved && m.id === 'meta-llama/llama-3.3-70b-instruct:free')) ? ' selected' : '';
    html += `<option value="${escapeHtml(m.id)}"${selAttr}>${escapeHtml(label)}</option>`;
  });
  html += `<option value="__custom">Custom…</option>`;
  sel.innerHTML = html;
  // if the saved model isn't in the list, show the custom field with it
  const inList = list.some(m => m.id === saved);
  if (saved && !inList) {
    sel.value = '__custom';
    inp.value = saved;
    inp.style.display = '';
  } else {
    inp.value = sel.value === '__custom' ? inp.value : sel.value;
    if (sel.value !== '__custom') inp.style.display = 'none';
  }
}

function initCraveModels() {
  const sel = document.getElementById('c-model-select');
  if (!sel) return;
  const inp = document.getElementById('c-model');
  sel.addEventListener('change', function () {
    if (sel.value === '__custom') {
      inp.style.display = '';
      inp.focus();
    } else {
      inp.value = sel.value;
      inp.style.display = 'none';
    }
  });
  document.getElementById('c-model-free-only').addEventListener('change', renderCraveModelOptions);
  fetch('/api/openrouter-models')
    .then(r => { if (!r.ok) throw new Error('no catalog'); return r.json(); })
    .then(data => {
      craveModelCache = (data && data.models) || [];
      if (!craveModelCache.length) throw new Error('empty catalog');
      renderCraveModelOptions();
    })
    .catch(() => {
      // offline fallback: plain text field with the saved (or default) model
      sel.style.display = 'none';
      document.getElementById('c-model-free-only').parentElement.style.display = 'none';
      inp.style.display = '';
      inp.value = sel.getAttribute('data-saved') || '';
      inp.placeholder = 'Model id, e.g. meta-llama/llama-3.3-70b-instruct:free';
    });
}

// page init: crave model dropdown on the settings page
if (document.getElementById('c-model-select') && typeof initCraveModels === 'function') {
  initCraveModels();
}
