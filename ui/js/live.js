// Live draw: draw on the board view and the plotter follows, with a lag.
// Strokes are queued; whenever the machine is free, everything queued so far goes out as one small job
// (stroke order kept, no parking in between). A speed slider tries to keep up.
import { h } from './util.js';
import { icon } from './icons.js';
import { store } from './store.js';
import { api } from './api.js';
import { caps } from './caps.js';
import { slider } from './widgets.js';
import { notice, clearNotice } from './ui.js';

const MIN_STEP_MM = 3;          // sample spacing while drawing
const MIN_LEN_MM = 3;           // shorter scribbles are ignored
const MAX_PER_JOB = 60;
const NAME = 'Live draw';

export const live = {
  on: false,
  speed: 1.5,
  strokes: [],                  // {pts, color, state: 'drawing' | 'queued' | 'sent'}
  current: null,
};
let sending = false, retryAt = 0, retryTimer = null, onInk = () => {};
export const isLiveJob = (job) => job?.name === NAME;
export function onLiveInk(fn) { onInk = fn; }
const touchInk = () => { store.touch('live'); onInk(); };

function safeRect() {
  const cfg = store.get('config'), sr = store.get('state')?.limits?.safe_rect;
  if (sr) return sr;
  const b = cfg?.board;
  return b ? [b.margin_mm, b.margin_mm, b.width_mm - b.margin_mm, b.height_mm - b.margin_mm] : [0, 0, 1500, 1000];
}
const clampPt = (x, y) => { const r = safeRect(); return [Math.min(r[2], Math.max(r[0], x)), Math.min(r[3], Math.max(r[1], y))]; };

/** Douglas-Peucker, eps in mm. */
function simplify(pts, eps) {
  if (pts.length < 3) return pts;
  const keep = new Uint8Array(pts.length); keep[0] = keep[pts.length - 1] = 1;
  const stack = [[0, pts.length - 1]];
  while (stack.length) {
    const [a, b] = stack.pop(); let best = -1, bd = eps;
    const [ax, ay] = pts[a], [bx, by] = pts[b], dx = bx - ax, dy = by - ay, L = Math.hypot(dx, dy) || 1e-9;
    for (let i = a + 1; i < b; i++) {
      const d = Math.abs((pts[i][0] - ax) * dy - (pts[i][1] - ay) * dx) / L;
      if (d > bd) { bd = d; best = i; }
    }
    if (best > 0) { keep[best] = 1; stack.push([a, best], [best, b]); }
  }
  return pts.filter((_, i) => keep[i]);
}
const length = (pts) => pts.reduce((s, p, i) => (i ? s + Math.hypot(p[0] - pts[i - 1][0], p[1] - pts[i - 1][1]) : 0), 0);

/* ---------- drawing input (called by the board view) ---------- */
live.begin = (x, y) => {
  live.current = { pts: [clampPt(x, y)], color: store.get('selectedPen') ?? 0, state: 'drawing' };
  live.strokes.push(live.current); touchInk();
};
live.move = (x, y) => {
  const s = live.current; if (!s) return;
  const p = clampPt(x, y), last = s.pts[s.pts.length - 1];
  if (Math.hypot(p[0] - last[0], p[1] - last[1]) >= MIN_STEP_MM) { s.pts.push(p); touchInk(); }
};
live.end = () => {
  const s = live.current; live.current = null;
  if (!s) return;
  s.pts = simplify(s.pts, 1.0);
  if (length(s.pts) < MIN_LEN_MM) live.strokes.splice(live.strokes.indexOf(s), 1);
  else s.state = 'queued';
  touchInk(); pump();
};
live.queued = () => live.strokes.filter((s) => s.state === 'queued').length;
live.setOn = (v) => { live.on = !!v; if (!v) live.current = null; store.touch('live'); if (v) pump(); };
live.clear = () => { live.strokes = live.strokes.filter((s) => s === live.current); touchInk(); };
live.stop = async () => {
  live.strokes = live.strokes.filter((s) => s.state === 'sent' || s === live.current);
  touchInk();
  try { if (isLiveJob(store.get('job')) && caps(store.get('state'), store.get('job')).jobActive) await api.cancel(); } catch { /* notice shown by api.js */ }
};

/* ---------- queue -> jobs ---------- */
/** Why nothing can be sent right now ('' = free). */
export function liveBlock() {
  const st = store.get('state'), job = store.get('job'), c = caps(st, job);
  if (c.jobActive) return isLiveJob(job) ? '' : 'Another job is running.';
  const pr = store.get('presence');
  if (pr?.present && pr?.guard === 'protect') return 'Board blocked: something is in front of it. Waiting …';
  return c.abs;
}
async function pump() {
  if (!live.on || sending || !live.queued()) return;
  const job = store.get('job');
  if (job && caps(store.get('state'), job).jobActive) return;          // the next job event calls pump again
  if (liveBlock()) return;
  if (Date.now() < retryAt) return;
  const batch = live.strokes.filter((s) => s.state === 'queued').slice(0, MAX_PER_JOB);
  sending = true;
  try {
    const j = await api.startJobQuiet({ drawing: { name: NAME, strokes: batch.map((s) => ({ color: s.color, points: s.pts })) }, layout: {}, live: true, speed_scale: live.speed, name: NAME });
    for (const s of batch) s.state = 'sent';
    store.set({ job: j });
    clearNotice('live');
  } catch (e) {
    retryAt = Date.now() + 2000;                                         // keep the strokes, try again shortly
    clearTimeout(retryTimer); retryTimer = setTimeout(pump, 2100);
    if (e?.code !== 'person_present' && e?.code !== 'not_idle') notice('live', e?.message || 'The plotter did not accept the strokes.', 'error');   // no dialog for the camera guard
  } finally { sending = false; touchInk(); }
}
store.on(['job', 'state'], () => { pump(); if (live.on) store.touch('live'); });

/* ---------- floating control strip over the board ---------- */
export function mountLiveBar(wrap) {
  const hint = h('div', { class: 'live-hint' });
  const penRow = h('div', { class: 'pen-pick', role: 'group', 'aria-label': 'Pen' });
  const spLbl = h('span', { class: 'mono-val', style: { minWidth: '34px' } }, `${live.speed}×`);
  const sp = slider({ min: 0.5, max: 4, step: 0.5, value: live.speed, label: 'Plotter speed', onInput: (v) => { live.speed = v; spLbl.textContent = `${v}×`; } });
  sp.input.style.cssText = 'width:110px;min-width:80px';
  const stopBtn = h('button', { class: 'btn sm danger', type: 'button', title: 'Cancel the running job and drop everything queued', onClick: () => live.stop() }, icon('stop'), 'Stop');
  const clrBtn = h('button', { class: 'btn sm', type: 'button', title: 'Remove the drawn strokes from the view', onClick: () => live.clear() }, icon('trash'), 'Clear');
  const bar = h('div', { class: 'live-bar', hidden: true }, hint,
    h('div', { class: 'live-row' }, penRow, h('span', { class: 'field-label' }, 'Speed'), sp.input, spLbl, clrBtn, stopBtn));
  wrap.append(bar);

  function paint() {
    bar.hidden = !live.on;
    if (!live.on) return;
    const pens = store.get('config')?.colors ?? [], cur = store.get('selectedPen') ?? 0;
    const sig = pens.map((p) => p.hex).join() + '|' + cur;
    if (penRow._sig !== sig) {
      penRow._sig = sig;
      penRow.replaceChildren(...pens.map((p, i) => h('button', { type: 'button', class: 'pen-dot', 'aria-pressed': String(i === cur), style: { '--c': p.hex },
        title: `Pen ${i + 1}: ${p.name}`, 'aria-label': `Pen ${i + 1}: ${p.name}`, onClick: () => store.set({ selectedPen: i }) })));
    }
    const q = live.queued(), job = store.get('job'), running = isLiveJob(job) && caps(store.get('state'), job).jobActive;
    const why = liveBlock();
    hint.textContent = why && q ? `Waiting: ${why}` : why ? why
      : running ? `Plotting … ${q ? `${q} stroke${q > 1 ? 's' : ''} queued` : 'caught up'}`
        : q ? `${q} stroke${q > 1 ? 's' : ''} queued` : 'Draw on the board. The plotter follows.';
    hint.className = 'live-hint' + (why ? ' warn' : '');
  }
  store.on(['live', 'config', 'selectedPen', 'job', 'state'], paint, { immediate: true });
}
