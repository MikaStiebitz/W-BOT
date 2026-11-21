// Canvas editor state: the elements on the board (text, formulas, plots, symbols, images, samples),
// selection, undo, and the preview request that renders them exactly as they will be plotted.
import { store } from '../store.js';
import { api } from '../api.js';
import { debounce, lsGet, lsSet } from '../util.js';
import { kvGet, kvSet, kvDel } from '../persist.js';

export const TYPE_LABEL = { text: 'Text', math: 'Formula', plot: 'Plot', symbol: 'Symbol', image: 'Image', pattern: 'Sample' };

const DEFAULTS = {
  text: { text: 'Hello!', font: 'simple', size: 40, align: 'center', spacing: 1, tracking: 0, mono: false },
  math: { latex: '\\frac{-b \\pm \\sqrt{b^2-4ac}}{2a}', size: 30 },
  plot: { w: 420, h: 280, xmin: -6.5, xmax: 6.5, ymin: null, ymax: null, grid: true, ticks: true, labels: true,
    axes: 'cross', xlabel: '', ylabel: '', title: '', curves: [{ kind: 'y', expr: 'sin(x)', color: 0 }] },
  symbol: { kind: 'icon', sym: 'heart', size: 100 },
  image: { size: 300 },
  pattern: { size: 300 },
};

let seq = Date.now() % 100000;
const newId = () => 'e' + (++seq).toString(36);
const files = new Map();                       // element id -> original File (to re-trace an image with other options)
export const fileOf = (id) => files.get(id);
export const setFile = (id, f) => { files.set(id, f); kvSet('file:' + id, f); };            // kept in the browser too

const clone = (v) => JSON.parse(JSON.stringify(v));
const past = [], future = [];
let token = 0, lastEdit = { id: null, key: null, t: 0 };

export const elements = () => store.get('elements') ?? [];
export const getEl = (id) => elements().find((e) => e.id === id) ?? null;
export const selected = () => getEl(store.get('selection'));
export const info = (id) => store.get('preview')?.elements?.find((e) => e.id === id) ?? null;

export function safeRect() {
  const b = store.get('config')?.board ?? { width_mm: 1500, height_mm: 1000, margin_mm: 40 };
  const m = b.margin_mm ?? 0, l = b.margin_left_mm ?? m, bo = b.margin_bottom_mm ?? m, r = b.margin_right_mm ?? m, t = b.margin_top_mm ?? m;
  return [l, bo, b.width_mm - r, b.height_mm - t];
}

function snapshot(key) {                       // history entry; edits of the same field within 800 ms share one entry
  const now = Date.now();
  const same = key && lastEdit.key === key && now - lastEdit.t < 800;
  lastEdit = { key, t: now };
  if (!same) { past.push(JSON.stringify(elements())); if (past.length > 60) past.shift(); future.length = 0; }
}

function commit(list, selection = store.get('selection')) {
  store.set({ elements: list, selection: list.some((e) => e.id === selection) ? selection : null });
  save(); recompute();
}

export function add(type, props = {}, { select = true, at = null } = {}) {
  snapshot(null);
  const [x0, y0, x1, y1] = safeRect();
  const n = elements().length;
  const el = { id: newId(), type, rot: 0, color: store.get('selectedPen') ?? 0, ...clone(DEFAULTS[type] ?? {}), ...props };
  const slot = [[0, 0], [-1, 1], [1, 1], [-1, -1], [1, -1], [0, 1.6], [0, -1.6]][n % 7];   // new elements fan out instead of stacking
  el.x = at ? at[0] : (x0 + x1) / 2 + slot[0] * (x1 - x0) * 0.2;
  el.y = at ? at[1] : (y0 + y1) / 2 + slot[1] * (y1 - y0) * 0.15;
  commit([...elements(), el], select ? el.id : store.get('selection'));
  store.set({ selection: select ? el.id : store.get('selection'), fresh: el.id });
  return el;
}

/** Changes fields of one element. `key` merges rapid edits of the same field (typing, dragging a slider) into one undo step. */
export function update(id, patch, key = null) {
  const cur = getEl(id);
  if (!cur) return;
  snapshot(key ? id + key : null);
  commit(elements().map((e) => (e.id === id ? { ...e, ...patch } : e)));
}

export function remove(id) {
  snapshot(null); files.delete(id); kvDel('file:' + id);
  commit(elements().filter((e) => e.id !== id), store.get('selection') === id ? null : store.get('selection'));
}

export function duplicate(id) {
  const e = getEl(id);
  if (!e) return;
  snapshot(null);
  const copy = { ...clone(e), id: newId(), x: e.x + 15, y: e.y - 15 };
  if (files.has(id)) files.set(copy.id, files.get(id));
  commit([...elements(), copy], copy.id);
  store.set({ selection: copy.id });
}

export function reorder(id, dir) {             // dir: 'front' | 'back' | 'up' | 'down'
  const list = [...elements()], i = list.findIndex((e) => e.id === id);
  if (i < 0) return;
  const [e] = list.splice(i, 1);
  const j = dir === 'front' ? list.length : dir === 'back' ? 0 : dir === 'up' ? Math.min(list.length, i + 1) : Math.max(0, i - 1);
  list.splice(j, 0, e);
  snapshot(null); commit(list);
}

export function select(id) { if (store.get('selection') !== id) store.set({ selection: id }); }

export function clearAll() { for (const id of files.keys()) kvDel('file:' + id); snapshot(null); files.clear(); commit([], null); }

/** Moves every element by (dx, dy) mm. */
export function moveAll(dx, dy) {
  snapshot(null);
  commit(elements().map((e) => ({ ...e, x: e.x + dx, y: e.y + dy })));
}

export function undo() {
  if (!past.length) return;
  future.push(JSON.stringify(elements()));
  commit(JSON.parse(past.pop()));
}
export function redo() {
  if (!future.length) return;
  past.push(JSON.stringify(elements()));
  commit(JSON.parse(future.pop()));
}

/** Scales an element's main size (text/formula height, symbol/image width, plot width and height) by k. */
export function scaled(el, k) {
  const p = {};
  if (el.type === 'plot') { p.w = el.w * k; p.h = el.h * k; }
  else if (el.size) p.size = el.size * k;
  return p;
}

/* ---------- Preview: the same drawing the job will plot ---------- */
// compose() is fast and gives the board its picture at once; planning the pen path (time estimate, warnings) takes
// seconds for big drawings, so it runs afterwards, one request at a time.
export const planBody = () => ({ elements: elements().map((e) => ({ ...e })) });

let planning = false, planAgain = false;
async function run() {
  const my = ++token;
  if (!elements().length) { store.set({ preview: null }); return; }
  try {
    const r = await api.compose(planBody(), { quiet: true });
    if (my !== token) return;
    store.set({ preview: { ...r, stats: null, estimated_s: null, warnings: [], pending: true }, previewSeq: my });
    planLater();
  } catch { if (my === token) store.set({ preview: null }); }
}
async function plan() {
  if (planning) { planAgain = true; return; }
  planning = true;
  const my = token;
  try {
    const r = await api.preview(planBody(), { quiet: true });
    const cur = store.get('preview');
    if (my === token && cur) store.set({ preview: { ...cur, ...r, elements: cur.elements, drawing: cur.drawing, pending: false } });
  } catch { /* the estimate is optional */ } finally {
    planning = false;
    if (planAgain || my !== token) { planAgain = false; planLater(); }
  }
}
const planLater = debounce(plan, 900);
const later = debounce(run, 120);
export function recompute(now = false) { if (now) { later.cancel(); run(); } else later(); }

/* ---------- Persistence (plain JSON; images are kept as traced paths) ---------- */
const save = debounce(() => {
  const json = JSON.stringify(elements());
  if (json.length < 3_000_000) lsSet('wbot-elements', elements());
}, 400);

export function initModel() {
  store.set({ elements: lsGet('wbot-elements', []) ?? [], selection: null });
  for (const e of elements()) if (e.type === 'image') kvGet('file:' + e.id).then((f) => { if (f) files.set(e.id, f); });
  store.on('config', () => recompute());
  recompute(true);
}
