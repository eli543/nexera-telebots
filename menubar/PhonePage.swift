// The phone companion is served as one document with no external dependencies.
enum PhonePage {
    static let html = #"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#ececec" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#111111" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Telebots">
<meta name="referrer" content="no-referrer">
<title>Telebots</title>
<link id="app-manifest" rel="manifest" href="/manifest.webmanifest">
<link id="app-icon" rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<style>
:root {
  color-scheme: light dark;
  --background: #ececec;
  --ink: #1c1c1e;
  --glass: rgba(255,255,255,.77);
  --rim: rgba(255,255,255,.9);
  --rule: rgba(0,0,0,.105);
  --wash: rgba(0,0,0,.045);
  --press: rgba(0,0,0,.1);
  --capsule: rgba(255,255,255,.58);
  --outline: rgba(0,0,0,.26);
  --red: #9e1017;
  --red-fill: #bf141a;
  --shadow: 0 6px 22px rgba(0,0,0,.035), inset 0 1px 0 var(--rim);
  --switch: #292929;
  font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
  font-synthesis: none;
  -webkit-font-smoothing: antialiased;
  -webkit-text-size-adjust: 100%;
}
@media (prefers-color-scheme: dark) {
  :root {
    --background: #111111;
    --ink: #f4f4f5;
    --glass: rgba(37,37,39,.8);
    --rim: rgba(255,255,255,.1);
    --rule: rgba(255,255,255,.105);
    --wash: rgba(255,255,255,.05);
    --press: rgba(255,255,255,.13);
    --capsule: rgba(255,255,255,.065);
    --outline: rgba(255,255,255,.3);
    --red: #ff9c96;
    --shadow: 0 6px 22px rgba(0,0,0,.1), inset 0 1px 0 var(--rim);
    --switch: #909090;
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html { min-height: 100%; background: var(--background); }
body {
  margin: 0;
  min-height: 100vh;
  min-height: 100dvh;
  color: var(--ink);
  background: radial-gradient(ellipse at 15% 0%, var(--wash), transparent 70%), var(--background);
  -webkit-tap-highlight-color: transparent;
}
button { color: inherit; font: inherit; cursor: pointer; touch-action: manipulation; }
button:disabled { opacity: .36; cursor: default; }
button:focus-visible { outline: 2px solid var(--ink); outline-offset: 3px; }
button, a { -webkit-tap-highlight-color: transparent; }
button:enabled:active { background-color: var(--press); }
svg { display: block; flex-shrink: 0; }
.app {
  max-width: 452px;
  margin: 0 auto;
  padding: max(22px, env(safe-area-inset-top)) 16px max(24px, env(safe-area-inset-bottom));
}
.header { padding: 0 2px; margin-bottom: 16px; }
.header-main { display: flex; align-items: center; gap: 12px; min-height: 52px; }
.mark {
  width: 42px;
  height: 42px;
  flex: 0 0 42px;
  border-radius: 11px;
  background: #131313;
  box-shadow: inset 0 0 0 1px rgba(255,255,255,.12);
  display: grid;
  grid-template-columns: repeat(var(--dot-columns, 3), 6px);
  align-content: center;
  justify-content: center;
  gap: 4px;
}
.mark-dot { width: 6px; height: 6px; border-radius: 50%; background: #f7f7f7; }
.mark-dot.down { background: transparent; border: 1.5px solid #ff6b66; }
.brand { flex: 1; min-width: 0; }
h1 { margin: 0 0 3px; font-size: 14px; line-height: 1.2; font-weight: 750; letter-spacing: 2.2px; }
.summary { margin: 0; font-size: 13px; line-height: 1.3; font-variant-numeric: tabular-nums; }
.header-tools { display: flex; align-items: center; gap: 0; }
.icon-button { border: 0; padding: 0; min-width: 44px; height: 44px; border-radius: 50%; background: transparent; display: grid; place-items: center; }
.ring { width: 28px; height: 28px; margin-left: 2px; }
.ring circle { stroke: var(--ink); }
.ring circle.down { stroke: var(--red); }
.header-problem { color: var(--red); font-size: 12px; font-weight: 600; line-height: 1.4; margin: 6px 0 0 54px; }
.glass { border-radius: 22px; background: var(--glass); box-shadow: var(--shadow); -webkit-backdrop-filter: blur(28px) saturate(1.15); backdrop-filter: blur(28px) saturate(1.15); }
.legend { padding: 14px; margin-bottom: 12px; border-radius: 16px; background: var(--wash); font-size: 12px; line-height: 1.5; }
.legend dl { display: grid; grid-template-columns: 68px 1fr; gap: 8px 10px; margin: 0; }
.legend dt { font-weight: 600; }
.legend dd { margin: 0; }
.rows { padding: 2px 0; overflow: hidden; }
.row { position: relative; padding: 8px 12px 12px; }
.row + .row::before { content: ""; position: absolute; top: 0; left: 28px; right: 12px; height: 1px; background: var(--rule); }
.row-top { display: flex; align-items: center; gap: 8px; }
.row-toggle { flex: 1; min-width: 0; min-height: 44px; border: 0; border-radius: 9px; padding: 0; background: transparent; text-align: left; display: grid; grid-template-columns: 8px minmax(0,1fr) auto; gap: 8px; align-items: center; }
.dot { display: block; width: 8px; height: 8px; border-radius: 50%; background: var(--ink); }
.dot.down { background: transparent; border: 1.7px solid var(--red); }
.name { font-size: 14px; font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.status { display: inline-flex; align-items: center; justify-content: flex-end; gap: 4px; font-size: 12px; min-width: 36px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.status.alert { color: var(--red); font-weight: 600; }
.capsule { min-height: 44px; padding: 0 13px; border-radius: 999px; border: 1px solid var(--rule); background: var(--capsule); font-size: 12px; font-weight: 550; line-height: 1.2; display: inline-flex; align-items: center; justify-content: center; gap: 6px; }
.reconnect { flex: 0 0 auto; min-width: 92px; }
.detail, .row-note { margin: 0 0 0 16px; font-size: 12px; line-height: 1.5; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.row-note { margin-top: 4px; }
.auto-paused { font-weight: 650; }
.auto-note { flex: 1 0 100%; margin: 0; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
.row-note.error { color: var(--red); font-weight: 600; }
.expanded { margin: 9px 0 0 16px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.uptime { margin: 0; font-size: 12px; line-height: 1.5; font-variant-numeric: tabular-nums; }
.fresh { margin-left: auto; border-color: var(--outline); background: transparent; }
.config-problem { padding: 16px; font-size: 13px; line-height: 1.5; }
.config-problem h2 { margin: 0 0 6px; font-size: 14px; }
.config-problem p { margin: 0; overflow-wrap: anywhere; }
.footer { margin-top: 12px; }
.bulk { width: 100%; min-height: 46px; background: var(--red-fill); border-color: transparent; color: #fff; margin-bottom: 12px; font-size: 13px; font-weight: 650; }
.bulk:enabled:active { background: #9e1017; }
.watchdog { padding: 12px 14px; display: flex; gap: 12px; align-items: center; justify-content: space-between; }
.watchdog h2 { font-size: 14px; font-weight: 650; margin: 0 0 3px; }
.caption { font-size: 12px; line-height: 1.45; margin: 0; }
.caption.alert { color: var(--red); }
.switch { flex: 0 0 54px; min-width: 54px; min-height: 44px; border: 0; background: transparent; padding: 6px 0; border-radius: 12px; }
.switch-track { display: block; position: relative; width: 50px; height: 30px; border-radius: 16px; background: var(--press); box-shadow: inset 0 0 0 1px var(--rule); transition: background .16s; }
.switch-knob { position: absolute; top: 3px; left: 3px; width: 24px; height: 24px; border-radius: 50%; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.18); transition: transform .16s; }
.switch[aria-checked="true"] .switch-track { background: var(--switch); }
.switch[aria-checked="true"] .switch-knob { transform: translateX(20px); }
.footer-note { margin: 0; padding: 0 14px 12px; color: var(--red); font-size: 12px; font-weight: 600; line-height: 1.5; overflow-wrap: anywhere; }
.footer-actions { border-top: 1px solid var(--rule); margin: 0 12px; padding: 12px 0; display: flex; align-items: center; gap: 8px; }
.footer-actions .icon-button { margin-left: auto; }
.confirmation { display: flex; align-items: flex-start; gap: 6px; padding: 0 14px 14px; margin: 0; font-size: 12px; line-height: 1.5; font-variant-numeric: tabular-nums; }
.confirmation svg { margin-top: 2px; }
.spinner { display: inline-block; width: 14px; height: 14px; border: 1.5px solid var(--outline); border-top-color: var(--ink); border-radius: 50%; animation: spin .8s linear infinite; flex-shrink: 0; }
@keyframes spin { to { transform: rotate(360deg); } }
.quiet { min-height: 100vh; min-height: 100dvh; display: grid; place-items: center; padding: max(24px, env(safe-area-inset-top)) 30px max(24px, env(safe-area-inset-bottom)); }
.quiet p { max-width: 280px; margin: 0; font-size: 16px; line-height: 1.6; text-align: center; text-wrap: balance; }
dialog { width: calc(100% - 24px); max-width: 428px; margin: auto auto max(12px, env(safe-area-inset-bottom)); padding: 22px 18px 18px; border: 1px solid var(--rim); border-radius: 25px; background: var(--glass); color: var(--ink); box-shadow: 0 12px 70px rgba(0,0,0,.24); -webkit-backdrop-filter: blur(32px); backdrop-filter: blur(32px); }
dialog::backdrop { background: rgba(0,0,0,.32); -webkit-backdrop-filter: blur(3px); backdrop-filter: blur(3px); }
dialog h2 { font-size: 19px; line-height: 1.3; letter-spacing: -.3px; margin: 0 0 10px; }
dialog p { font-size: 14px; line-height: 1.55; margin: 0 0 20px; }
.sheet-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.sheet-actions .capsule { min-height: 48px; font-size: 14px; }
.sheet-confirm { background: var(--ink); color: var(--background); }
.sheet-confirm:enabled:active { background: var(--ink); opacity: .8; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
@media (max-width: 350px) {
  .app { padding-left: 10px; padding-right: 10px; }
  .row { padding-left: 10px; padding-right: 10px; }
  .row-top { gap: 6px; }
  .reconnect { min-width: 82px; padding: 0 10px; }
  .name { font-size: 13px; }
  .status { font-size: 11px; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }
  .spinner { border-right-color: var(--ink); }
}
</style>
</head>
<body>
<main id="app" class="app" hidden aria-label="Telebots fleet">
  <header class="header">
    <div class="header-main">
      <div id="mark" class="mark" aria-hidden="true"></div>
      <div class="brand"><h1>TELEBOTS</h1><p id="summary" class="summary"></p></div>
      <div class="header-tools">
        <button id="legend-toggle" class="icon-button" type="button" aria-label="Show legend" aria-expanded="false" aria-controls="legend"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M9.6 8.8a2.5 2.5 0 0 1 4.9.6c0 1.8-2.5 2.1-2.5 3.7"/><circle cx="12" cy="16.3" r=".6" fill="currentColor" stroke="none"/></svg></button>
        <svg id="status-ring" class="ring" viewBox="0 0 28 28" role="img" aria-label="Fleet status"></svg>
      </div>
    </div>
    <p id="header-problem" class="header-problem" hidden></p>
  </header>
  <aside id="legend" class="legend" aria-label="What the fleet indicators mean" hidden>
    <dl>
      <dt>Dots</dt><dd>Filled when connected. Hollow when down.</dd>
      <dt>Heartbeat</dt><dd>Time since the bot's last liveness ping.</dd>
      <dt>ctx</dt><dd>Context window used by this conversation.</dd>
      <dt>restarts/h</dt><dd>Watchdog restarts in the last hour.</dd>
      <dt>stall</dt><dd>A message waited 10+ minutes while the bot was idle. Phone alerts can be off; "phone notified" appears only after an alert was accepted.</dd>
      <dt>paused</dt><dd>Auto-recovery paused: the watchdog stopped restarting that bot after too many restarts in an hour. Tap the row for details.</dd>
      <dt>Snapshot</dt><dd id="legend-snapshot">Saves every bot's current conversation so Restore can bring each one back later. Snapshot keeps your auto-recovery settings as they are. Bots already off or paused stay that way.</dd>
    </dl>
  </aside>
  <section id="rows" class="rows glass" aria-label="Bots"></section>
  <footer class="footer">
    <button id="reconnect-all" class="capsule bulk" type="button" hidden>Reconnect all down</button>
    <section class="glass" aria-label="Fleet controls">
      <div class="watchdog">
        <div><h2>Watchdog</h2><p id="watchdog-caption" class="caption"></p></div>
        <button id="watchdog" class="switch" type="button" role="switch" aria-label="Auto-recovery" aria-checked="false" aria-describedby="watchdog-caption"><span class="switch-track" aria-hidden="true"><span class="switch-knob"></span></span></button>
      </div>
      <p id="footer-note" class="footer-note" role="status" hidden></p>
      <div class="footer-actions">
        <button id="snapshot" class="capsule" type="button" aria-label="Snapshot"></button>
        <button id="restore" class="capsule" type="button" aria-label="Restore" disabled></button>
        <button id="refresh" class="icon-button" type="button" aria-label="Refresh now: check every bot again"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 7v5h-5M19.6 12a7.7 7.7 0 1 0-2.1 5.3M20 12l-2.8-3.2"/></svg></button>
      </div>
      <p id="confirmation" class="confirmation" role="status" hidden></p>
    </section>
  </footer>
</main>
<section id="quiet" class="quiet" aria-live="polite"><p id="quiet-text"></p></section>
<dialog id="confirm-sheet" aria-labelledby="sheet-title" aria-describedby="sheet-description">
  <h2 id="sheet-title"></h2><p id="sheet-description"></p>
  <div class="sheet-actions"><button id="sheet-cancel" class="capsule" type="button" autofocus>Cancel</button><button id="sheet-confirm" class="capsule sheet-confirm" type="button"></button></div>
</dialog>
<script>
(async () => {
  'use strict';
  const storageKey = 'telebots.phoneToken';
  const missingTokenMessage = 'No link token. Open the link you were sent once so this phone learns it.';
  const $ = id => document.getElementById(id);
  const app = $('app');
  const quiet = $('quiet');
  const sheet = $('confirm-sheet');
  const expanded = new Set();
  const rowViews = new Map();
  const localPending = new Set();
  let state = null;
  let token = '';
  let polling = null;
  let refreshQueued = false;
  let sheetAction = null;
  let sheetTrigger = null;
  let localError = '';
  let nextRowID = 0;
  const icons = {
    pulse: '<svg width="13" height="13" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 10h4l2-5 4 10 2-5h6"/></svg>',
    check: '<svg width="14" height="14" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="9" fill="currentColor"/><path d="m5.5 10 3 3 6-6" fill="none" stroke="var(--glass)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    camera: '<svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6.2 5.5 7.5 3.8h5l1.3 1.7h3a1 1 0 0 1 1 1v8.7a1 1 0 0 1-1 1H3.2a1 1 0 0 1-1-1V6.5a1 1 0 0 1 1-1z"/><circle cx="10" cy="10.7" r="3.2"/></svg>',
    restore: '<svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3v4h4M3.3 7a7 7 0 1 1-.2 5M10 6v4l3 1.7"/></svg>',
    spinner: '<span class="spinner" aria-hidden="true"></span>'
  };

  try { token = localStorage.getItem(storageKey) || ''; } catch (_) { }
  function learnFragmentToken() {
    const fragmentToken = new URLSearchParams(location.hash.slice(1)).get('t');
    if (!fragmentToken || !/^[a-f0-9]{64}$/i.test(fragmentToken)) return false;
    token = fragmentToken.toLowerCase();
    try { localStorage.setItem(storageKey, token); } catch (_) { }
    return true;
  }
  learnFragmentToken();
  // Home-screen WebKit can have separate storage. Keep the fragment in Safari
  // until installation, then remove it once the standalone app has saved it.
  const standalone = navigator.standalone === true || window.matchMedia('(display-mode: standalone)').matches;
  if (standalone && location.hash) history.replaceState(null, '', location.pathname + location.search);

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function icon(kind) {
    const span = node('span');
    span.innerHTML = icons[kind];
    return span.firstElementChild;
  }

  function setText(id, text) {
    const element = $(id);
    element.textContent = text || '';
    element.hidden = !text;
  }

  function closeSheet(restoreFocus = true) {
    sheetAction = null;
    if (sheet.open) sheet.close();
    if (restoreFocus && sheetTrigger && sheetTrigger.isConnected) sheetTrigger.focus();
    sheetTrigger = null;
  }

  function showQuiet(message) {
    closeSheet(false);
    app.hidden = true;
    quiet.hidden = false;
    $('quiet-text').textContent = message;
  }

  function fail(error) {
    if (error.status === 401 || error.status === 403) {
      showQuiet('Open the current phone link from Telebots Bar.');
    } else {
      showQuiet('Turn on Tailscale on this phone, then pull to refresh');
    }
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 14000);
    try {
      const response = await fetch(path, {
        ...options,
        cache: 'no-store',
        credentials: 'omit',
        redirect: 'error',
        signal: controller.signal,
        headers: { ...options.headers, Authorization: 'Bearer ' + token }
      });
      if (!response.ok) {
        const error = new Error('The request could not run.');
        error.status = response.status;
        try {
          const body = await response.json();
          if (typeof body.error === 'string') error.message = body.error;
          else if (typeof body.message === 'string') error.message = body.message;
        } catch (_) { }
        throw error;
      }
      return response;
    } finally {
      clearTimeout(timeout);
    }
  }

  function mark(bots) {
    const motif = $('mark');
    motif.replaceChildren();
    const columns = bots.length <= 3 ? Math.max(1, bots.length) : Math.ceil(bots.length / 2);
    motif.style.setProperty('--dot-columns', columns);
    for (const bot of bots) motif.append(node('span', 'mark-dot' + (bot.connected ? '' : ' down')));
    const ring = $('status-ring');
    ring.replaceChildren();
    ring.setAttribute('aria-label', 'Status ring, ' + state.summary);
    const circleLength = 2 * Math.PI * 10.2;
    const segment = circleLength / Math.max(1, bots.length);
    bots.forEach((bot, index) => {
      const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      const values = { cx: 14, cy: 14, r: 10.2, fill: 'none', 'stroke-width': 2.5,
        'stroke-linecap': 'round', 'stroke-dasharray': Math.max(1, segment - 4) + ' ' + circleLength,
        'stroke-dashoffset': -index * segment, transform: 'rotate(-90 14 14)' };
      for (const [key, value] of Object.entries(values)) circle.setAttribute(key, value);
      if (!bot.connected) circle.classList.add('down');
      ring.append(circle);
    });
  }

  function toggleRow(key) {
    expanded.has(key) ? expanded.delete(key) : expanded.add(key);
    const bot = state && state.bots.find(item => item.key === key);
    if (bot) updateRow(rowViews.get(key), bot);
  }

  function createRow(bot) {
    const row = node('article', 'row');
    const top = node('div', 'row-top');
    const toggle = node('button', 'row-toggle');
    toggle.type = 'button';
    const dot = node('span', 'dot');
    dot.setAttribute('aria-hidden', 'true');
    const name = node('span', 'name');
    const status = node('span', 'status');
    status.setAttribute('aria-hidden', 'true');
    toggle.append(dot, name, status);
    const reconnect = node('button', 'capsule reconnect', 'Reconnect');
    reconnect.type = 'button';
    reconnect.addEventListener('click', () => {
      const current = state.bots.find(item => item.key === bot.key);
      if (!current) return;
      if (current.connected) confirm('reconnect', current, reconnect);
      else void action('reconnect', current);
    });
    toggle.addEventListener('click', () => toggleRow(bot.key));
    top.append(toggle, reconnect);
    const detail = node('p', 'detail');
    const note = node('p', 'row-note');
    const extra = node('div', 'expanded');
    extra.id = 'details-' + nextRowID++;
    const autoNote = node('p', 'auto-note');
    const uptime = node('p', 'uptime');
    const fresh = node('button', 'capsule fresh', 'Fresh start');
    fresh.type = 'button';
    fresh.addEventListener('click', () => {
      const current = state.bots.find(item => item.key === bot.key);
      if (current) confirm('fresh', current, fresh);
    });
    extra.append(autoNote, uptime, fresh);
    row.append(top, detail, note, extra);
    row.addEventListener('click', event => {
      if (!event.target.closest('button')) toggleRow(bot.key);
    });
    toggle.setAttribute('aria-controls', extra.id);
    return { row, toggle, dot, name, status, reconnect, detail, note, extra, autoNote, uptime, fresh };
  }

  function updateRow(view, bot) {
    if (!view) return;
    const busy = bot.in_flight || localPending.has(bot.key);
    const fleetBusy = state.snapshot.busy || state.restore_busy || localPending.has('snapshot') || localPending.has('restore');
    const open = expanded.has(bot.key);
    view.name.textContent = bot.name;
    view.dot.classList.toggle('down', !bot.connected);
    view.toggle.setAttribute('aria-expanded', String(open));
    view.toggle.setAttribute('aria-label', bot.name + ', ' + (busy ? 'reconnecting' : bot.status_text) + (bot.auto_paused ? ', auto-recovery paused' : '') + '. ' + (open ? 'Hide' : 'Show') + ' details');
    view.reconnect.disabled = busy || fleetBusy || !!state.problem;
    view.reconnect.setAttribute('aria-label', 'Reconnect ' + bot.name);
    view.fresh.disabled = busy || fleetBusy || !!state.problem;
    view.fresh.setAttribute('aria-label', 'Fresh start ' + bot.name);
    view.status.replaceChildren();
    view.status.classList.toggle('alert', !busy && !bot.connected);
    view.status.title = bot.status_tip || '';
    if (busy) view.status.append(icon('spinner'));
    else if (bot.succeeded) view.status.append(icon('check'));
    else {
      if (bot.connected && bot.heartbeat_age_seconds !== null) view.status.append(icon('pulse'));
      view.status.append(document.createTextNode(bot.status_text || 'Unknown'));
    }
    // The watchdog's crash-loop breaker: a quiet marker on the detail line, and its
    // explanation in the expanded row (a phone has no hover for a tooltip).
    const detailText = (bot.detail_line || '') + (bot.stall ? ' · stall suspected' : '');
    view.detail.replaceChildren();
    if (detailText) view.detail.append(document.createTextNode(detailText));
    if (bot.auto_paused) {
      if (detailText) view.detail.append(document.createTextNode(' · '));
      const marker = node('span', 'auto-paused', 'auto-recovery paused');
      marker.title = bot.auto_paused_tip || 'Auto-recovery paused by the watchdog\'s crash-loop breaker.';
      view.detail.append(marker);
    }
    view.detail.hidden = !view.detail.textContent;
    view.autoNote.textContent = bot.auto_paused ? (bot.auto_paused_tip || 'Auto-recovery paused by the watchdog\'s crash-loop breaker.') : '';
    view.autoNote.hidden = !view.autoNote.textContent;
    view.note.textContent = bot.note || bot.last_error || '';
    view.note.classList.toggle('error', !!bot.last_error);
    view.note.hidden = !view.note.textContent;
    view.extra.hidden = !open;
    view.uptime.textContent = bot.uptime_line || bot.launch_mode || '';
    view.uptime.hidden = !view.uptime.textContent;
  }

  function labeledButton(id, label, glyph, busy) {
    const button = $(id);
    button.replaceChildren(icon(busy ? 'spinner' : glyph), document.createTextNode(label));
    button.setAttribute('aria-busy', String(busy));
  }

  function snapshotLine(snapshot) {
    if (!snapshot.present) return '';
    if (snapshot.line) return snapshot.line;
    let time = 'time unknown';
    if (typeof snapshot.time === 'number') {
      time = new Intl.DateTimeFormat('en-US', { weekday: 'long', hour: 'numeric', minute: '2-digit', hour12: true, timeZoneName: 'short' })
        .format(new Date(snapshot.time * 1000)).replace(' AM', 'am').replace(' PM', 'pm');
    }
    return 'Snapshot saved · ' + snapshot.sessions + ' sessions · ' + time;
  }

  function render() {
    if (!state) return;
    quiet.hidden = true;
    app.hidden = false;
    $('summary').textContent = state.summary || '';
    setText('header-problem', state.header_problem);
    $('header-problem').title = state.header_problem_tip || '';
    mark(state.bots);
    const rows = $('rows');
    if (state.problem) {
      rowViews.clear();
      const problem = node('div', 'config-problem');
      problem.append(node('h2', '', 'Fleet config problem'), node('p', '', state.problem));
      rows.replaceChildren(problem);
    } else {
      const keys = new Set(state.bots.map(bot => bot.key));
      for (const [key, view] of rowViews) {
        if (!keys.has(key)) { view.row.remove(); rowViews.delete(key); }
      }
      if (!rowViews.size) rows.replaceChildren();
      for (const [index, bot] of state.bots.entries()) {
        let view = rowViews.get(bot.key);
        if (!view) { view = createRow(bot); rowViews.set(bot.key, view); }
        updateRow(view, bot);
        if (rows.children[index] !== view.row) rows.insertBefore(view.row, rows.children[index] || null);
      }
    }
    const down = state.bots.filter(bot => !bot.connected);
    const snapshotBusy = state.snapshot.busy || localPending.has('snapshot');
    const restoreBusy = state.restore_busy || localPending.has('restore');
    const botBusy = state.bots.some(bot => bot.in_flight || localPending.has(bot.key)) || localPending.has('reconnect_all_down');
    $('reconnect-all').hidden = !down.length;
    $('reconnect-all').disabled = !!state.problem || snapshotBusy || restoreBusy || localPending.has('reconnect_all_down') || !down.some(bot => !bot.in_flight && !localPending.has(bot.key));
    const watchdog = state.watchdog;
    $('watchdog-caption').textContent = watchdog.caption;
    $('watchdog-caption').classList.toggle('alert', watchdog.state !== 'on' || watchdog.alert === true);
    $('watchdog').setAttribute('aria-checked', String(watchdog.state === 'on'));
    $('watchdog').disabled = watchdog.state === 'unavailable' || botBusy || snapshotBusy || restoreBusy || localPending.has('watchdog_on') || localPending.has('watchdog_off') || !!state.problem;
    labeledButton('snapshot', 'Snapshot', 'camera', snapshotBusy);
    labeledButton('restore', 'Restore', 'restore', restoreBusy);
    $('snapshot').disabled = snapshotBusy || restoreBusy || botBusy || !!state.problem;
    const snapshotHelp = state.snapshot.button_tip || 'Save every bot\'s current conversation so Restore can bring each one back later. Snapshot keeps your auto-recovery settings as they are. Bots already off or paused stay that way.';
    $('snapshot').title = snapshotHelp;
    $('legend-snapshot').textContent = snapshotHelp;
    $('restore').disabled = !state.snapshot.present || snapshotBusy || restoreBusy || botBusy || !!state.problem;
    $('restore').title = state.snapshot.tip || 'No snapshot saved';
    setText('footer-note', localError || state.footer_note);
    const confirmation = state.confirmation || snapshotLine(state.snapshot);
    const line = $('confirmation');
    line.replaceChildren();
    line.hidden = !confirmation;
    if (confirmation) line.append(icon('check'), node('span', '', confirmation));
  }

  async function refresh() {
    if (document.hidden) return;
    if (!/^[a-f0-9]{64}$/.test(token)) {
      showQuiet(missingTokenMessage);
      return;
    }
    if (polling) { refreshQueued = true; return polling; }
    $('refresh').setAttribute('aria-busy', 'true');
    polling = (async () => {
      try {
        const response = await request('/api/state');
        const next = await response.json();
        if (!Array.isArray(next.bots) || !next.watchdog || !next.snapshot) throw new Error('Incomplete state');
        state = next;
        render();
      } catch (error) {
        fail(error);
      } finally {
        polling = null;
        $('refresh').setAttribute('aria-busy', 'false');
        if (refreshQueued) { refreshQueued = false; void refresh(); }
      }
    })();
    return polling;
  }

  function confirm(kind, bot, trigger) {
    if (sheet.open || bot.in_flight || localPending.has(bot.key)) return;
    sheetTrigger = trigger;
    sheetAction = { kind, key: bot.key };
    $('sheet-title').textContent = (kind === 'fresh' ? 'Fresh start for ' : 'Reconnect ') + bot.name + '?';
    $('sheet-description').textContent = kind === 'fresh'
      ? 'This ends the current conversation and starts a new, empty one. Reconnect keeps the conversation.'
      : bot.name + ' is connected. Restart it and resume its current conversation?';
    $('sheet-confirm').textContent = kind === 'fresh' ? 'Fresh start' : 'Reconnect';
    sheet.showModal();
    $('sheet-cancel').focus();
  }

  async function action(kind, bot = null, confirmed = false) {
    const key = bot ? bot.key : kind;
    if (localPending.has(key)) return;
    localPending.add(key);
    localError = '';
    render();
    let failed = null;
    try {
      const body = { action: kind };
      if (bot) body.bot = bot.key;
      if (confirmed) body.confirm = true;
      await request('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    } catch (error) {
      if (!error.status || error.status === 401 || error.status === 403) failed = error;
      else localError = error.message;
    } finally {
      localPending.delete(key);
      if (failed) fail(failed);
      else await refresh();
    }
  }

  $('legend-toggle').addEventListener('click', () => {
    const open = $('legend').hidden;
    $('legend').hidden = !open;
    $('legend-toggle').setAttribute('aria-expanded', String(open));
    $('legend-toggle').setAttribute('aria-label', open ? 'Hide legend' : 'Show legend');
  });
  $('sheet-cancel').addEventListener('click', () => closeSheet());
  sheet.addEventListener('cancel', event => { event.preventDefault(); closeSheet(); });
  sheet.addEventListener('click', event => { if (event.target === sheet) closeSheet(); });
  $('sheet-confirm').addEventListener('click', () => {
    const next = sheetAction;
    closeSheet();
    if (!next || !state) return;
    const bot = state.bots.find(item => item.key === next.key);
    if (bot) void action(next.kind, bot, true);
  });
  $('reconnect-all').addEventListener('click', () => void action('reconnect_all_down'));
  $('watchdog').addEventListener('click', () => {
    if (state) void action(state.watchdog.state === 'on' ? 'watchdog_off' : 'watchdog_on');
  });
  $('snapshot').addEventListener('click', () => void action('snapshot'));
  $('restore').addEventListener('click', () => void action('restore'));
  $('refresh').addEventListener('click', () => { localError = ''; void refresh(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) void refresh(); });
  window.addEventListener('pageshow', () => { if (!document.hidden) void refresh(); });
  window.addEventListener('hashchange', () => {
    if (!learnFragmentToken()) return;
    if (standalone) history.replaceState(null, '', location.pathname + location.search);
    void refresh();
  });
  window.addEventListener('online', () => void refresh());
  let touchStart = null;
  document.addEventListener('touchstart', event => {
    touchStart = window.scrollY <= 0 && event.touches.length === 1 ? event.touches[0].clientY : null;
  }, { passive: true });
  document.addEventListener('touchend', event => {
    if (touchStart !== null && event.changedTouches.length === 1 && event.changedTouches[0].clientY - touchStart > 85) void refresh();
    touchStart = null;
  }, { passive: true });
  setInterval(() => { if (!document.hidden) void refresh(); }, 5000);
  if (!/^[a-f0-9]{64}$/.test(token)) showQuiet(missingTokenMessage);
  else await refresh();
})();
</script>
</body>
</html>
"""#
}
