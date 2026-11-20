// Entry point: builds the UI, starts the WebSocket and the initial data, keyboard shortcuts, tabs.
import { h } from './util.js';
import { store, persist } from './store.js';
import { api, startWs } from './api.js';
import { reportError } from './ui.js';
import { mountHeader, triggerEstop } from './panels/header.js';
import { mountBoard, pushTrace } from './board.js';
import { mountDraw } from './panels/draw.js';
import { mountPens } from './panels/pens.js';
import { mountSetup } from './panels/setup.js';
import { mountJog } from './panels/jog.js';
import { mountMore } from './panels/more.js';
import { refreshCamera } from './camera.js';
import { initSliders } from './sliderfx.js';

const TABS = [
  { id: 'draw', label: 'Draw', mount: mountDraw },
  { id: 'pens', label: 'Pens', mount: mountPens },
  { id: 'setup', label: 'Setup', mount: mountSetup },
  { id: 'fahren', label: 'Move', mount: mountJog },     // id kept: the jog panel listens for it
  { id: 'more', label: 'More', mount: mountMore },
];

/* ---------- Never leave an error unhandled ---------- */
window.addEventListener('unhandledrejection', (e) => { e.preventDefault(); reportError(e.reason); });
window.addEventListener('error', (e) => { console.error(e.error || e.message); });

/* ---------- Tabs ---------- */
function mountTabs() {
  const tabs = document.getElementById('tabs'), panels = document.getElementById('panels');
  const btns = {}, bodies = {};
  for (const t of TABS) {
    const b = h('button', { class: 'tab', role: 'tab', type: 'button', id: `tab-${t.id}`, 'aria-controls': `panel-${t.id}`, 'aria-selected': 'false', tabindex: '-1',
      onClick: () => select(t.id) }, t.label);
    const p = h('div', { class: 'panel', role: 'tabpanel', id: `panel-${t.id}`, 'aria-labelledby': `tab-${t.id}`, hidden: true });
    btns[t.id] = b; bodies[t.id] = p;
    tabs.append(b); panels.append(p);
    try { t.mount(p); } catch (e) { console.error('Panel ' + t.id + ' failed', e); p.append(h('div', { class: 'note danger' }, `The “${t.label}” tab could not be loaded: ${e.message}`)); }
  }
  function select(id) {
    const t = TABS.find((x) => x.id === id && (!x.when || x.when(store.get('state')))) ?? TABS[0];
    store.set({ tab: t.id });
  }
  tabs.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    const vis = TABS.filter((t) => !btns[t.id].hidden), i = vis.findIndex((t) => t.id === store.get('tab'));
    const n = vis[(i + (e.key === 'ArrowRight' ? 1 : vis.length - 1)) % vis.length];
    select(n.id); btns[n.id].focus();
  });
  store.on(['tab', 'state'], () => {
    const st = store.get('state');
    for (const t of TABS) btns[t.id].hidden = !!t.when && !t.when(st);
    let cur = store.get('tab');
    if (panels._cur !== cur) { panels._cur = cur; panels.scrollTop = 0; }
    if (!TABS.some((t) => t.id === cur && !btns[t.id].hidden)) { cur = 'draw'; store.set({ tab: cur }); }
    for (const t of TABS) {
      const on = t.id === cur;
      btns[t.id].setAttribute('aria-selected', String(on)); btns[t.id].tabIndex = on ? 0 : -1;
      if (bodies[t.id].hidden === on) bodies[t.id].hidden = !on;
    }
  }, { immediate: true });
  persist('wbot-tab', 'tab');
}

/* ---------- WebSocket messages ---------- */
function pushLog(rec) {
  const logs = store.get('logs'), n = store.get('logSeq') + 1;
  logs.push({ n, level: rec.level || 'info', text: String(rec.text ?? ''), t: rec.t });
  if (logs.length > 2000) logs.splice(0, 400);
  store.set({ logSeq: n }); store.touch('logs');
}

let prevConn = null;
async function onState(s) {
  const conn = s?.connection;
  const key = conn ? `${conn.status}|${conn.transport}` : null;
  if (key !== prevConn) {
    const wasSim = prevConn?.endsWith('|sim'), connected = key === 'connected|sim';
    prevConn = key;
    if (connected) loadSimTrace();
    else if (wasSim || conn?.status === 'disconnected') { store.get('trace').length = 0; store.set({ simInfo: null }); store.touch('trace'); }
  }
}
async function loadSimTrace() {
  try {
    const info = await api.simulator({ quiet: true });
    if (info?.trace) { const t = store.get('trace'); t.length = 0; t.push(...info.trace); store.touch('trace'); }
    store.set({ simInfo: info });
  } catch { /* not critical */ }
}

function onMessage(msg) {
  switch (msg?.type) {
    case 'state': store.set({ state: msg.data }); onState(msg.data); break;
    case 'log': pushLog(msg); break;
    case 'job': store.set({ job: msg.data }); break;
    case 'camera': store.set({ camTrack: msg.data }); break;
    case 'autocal': store.set({ autocal: msg.data }); break;
    case 'presence': store.set({ presence: msg.data }); break;
    case 'camera_watch': refreshCamera(); break;
    case 'trace': {
      const d = msg.data || {};
      if (d.clear) { store.get('trace').length = 0; store.touch('trace'); }
      else if (Array.isArray(d.seg) && d.seg.length >= 4) pushTrace(d.seg);
      break;
    }
    default: break;
  }
}

async function refreshAll(first = false) {
  const q = { quiet: !first };
  const [st, job, cfg, pats, ports, cam] = await Promise.allSettled([api.state(q), api.job({ quiet: true }), api.config(q), api.patterns({ quiet: true }), api.ports({ quiet: true }), api.camera({ quiet: true })]);
  if (cam.status === 'fulfilled') store.set({ camera: cam.value });
  if (st.status === 'fulfilled') onState(st.value);
  if (job.status === 'fulfilled') store.set({ job: job.value });
  if (cfg.status === 'fulfilled') {
    store.set({ config: cfg.value });
    if (!store.get('transport')) store.set({ transport: cfg.value?.connection?.transport ?? 'sim' });
  }
  if (pats.status === 'fulfilled' && Array.isArray(pats.value)) store.set({ patterns: pats.value });
  if (ports.status === 'fulfilled' && Array.isArray(ports.value)) store.set({ ports: ports.value });
}

/* ---------- Keyboard: emergency stop on the space bar ---------- */
const textEntry = (t) => !!t && (t.tagName === 'TEXTAREA' || t.isContentEditable || (t.tagName === 'INPUT' && ['text', 'search', 'url', 'email', 'password', 'tel'].includes(t.type)));
window.addEventListener('keydown', (e) => {
  if (e.code === 'Escape') store.set({ target: null });
  if (e.code !== 'Space' || textEntry(e.target)) return;
  e.preventDefault(); e.stopPropagation();
  if (!e.repeat) triggerEstop();
}, true);
window.addEventListener('keyup', (e) => { if (e.code === 'Space' && !textEntry(e.target)) { e.preventDefault(); e.stopPropagation(); } }, true);

/* ---------- Start ---------- */
initSliders();
mountHeader(document.getElementById('top'));
mountBoard(document.getElementById('board-col'));
mountTabs();
persist('wbot-step', 'step');
persist('wbot-transport', 'transport');
persist('wbot-port', 'port');
refreshAll(true);
startWs({ onMessage, onOpen: () => refreshAll(false) });
