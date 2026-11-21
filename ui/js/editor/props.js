// Properties of the selected element: shared controls (pen, position, rotation, order) and the dispatch to the type panels.
import { h } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { numField, slider, segmented, colorHex } from '../widgets.js';
import * as M from './model.js';
import { textPanel, mathPanel } from './props-text.js';
import { plotPanel } from './props-plot.js';
import { symbolPanel, imagePanel, patternPanel } from './props-misc.js';

const PANELS = { text: textPanel, math: mathPanel, plot: plotPanel, symbol: symbolPanel, image: imagePanel, pattern: patternPanel };

/** Slider with a value label. get/set work on the element; `key` merges rapid edits into one undo step. */
export function sliderRow({ label, min, max, step = 1, unit = '', get, set, key, digits = 0 }) {
  const val = h('span', { class: 'mono-val', style: { minWidth: '56px', textAlign: 'right' } });
  const fmtv = (v) => `${Number(v).toFixed(digits)}${unit ? ' ' + unit : ''}`;
  const sl = slider({ min, max, step, value: get(), label, onInput: (v) => { val.textContent = fmtv(v); set(v, key); } });
  val.textContent = fmtv(get());
  return { el: h('div', { class: 'row-s prop-row' }, h('span', { class: 'field-label', style: { flex: 'none', width: '64px' } }, label), sl.input, val),
    sync(v) { sl.set(v); val.textContent = fmtv(v); }, input: sl.input };
}

export function penRow(get, set) {
  const row = h('div', { class: 'pen-pick', role: 'group', 'aria-label': 'Pen' });
  const paint = () => {
    const pens = store.get('config')?.colors ?? [], cur = get();
    row.replaceChildren(...pens.map((p, i) => h('button', { type: 'button', class: 'pen-dot', 'aria-pressed': String(i === cur), title: `Pen ${i + 1}: ${p.name}`,
      'aria-label': `Pen ${i + 1}: ${p.name}`, style: { '--c': colorHex(store.get('config'), i) }, onClick: () => { set(i); } })));
  };
  paint();
  return { el: row, sync: paint };
}

export function mountProps(root) {
  const holder = h('div', { class: 'stack props-body' });
  const empty = h('p', { class: 'muted props-empty' }, 'Select something on the board, or add text, a formula, a plot or a symbol.');
  root.append(empty, holder);
  let built = null, builtId = null;

  function build(el) {
    holder.replaceChildren();
    const api = { get: () => M.getEl(el.id), set: (patch, key) => M.update(el.id, patch, key), id: el.id };
    const typePanel = PANELS[el.type]?.(api);
    const pen = penRow(() => M.getEl(el.id)?.color ?? 0, (i) => M.update(el.id, { color: i }));
    const px = numField({ label: 'X', unit: 'mm', step: 1, value: Math.round(el.x), onChange: (v) => v != null && M.update(el.id, { x: v }, 'x') });
    const py = numField({ label: 'Y', unit: 'mm', step: 1, value: Math.round(el.y), onChange: (v) => v != null && M.update(el.id, { y: v }, 'y') });
    const rot = sliderRow({ label: 'Rotate', min: -180, max: 180, step: 1, unit: '°', get: () => M.getEl(el.id)?.rot ?? 0, set: (v, k) => M.update(el.id, { rot: v }, k), key: 'rot' });
    const mk = (ic, title, fn, cls = '') => h('button', { class: 'btn sm ' + cls, type: 'button', title, 'aria-label': title, onClick: fn }, icon(ic), title);
    const actions = h('div', { class: 'row prop-actions' },
      mk('copy', 'Duplicate', () => M.duplicate(el.id)), mk('front', 'To front', () => M.reorder(el.id, 'front')),
      mk('back', 'To back', () => M.reorder(el.id, 'back')), h('span', { class: 'grow' }), mk('trash', 'Delete', () => M.remove(el.id), 'danger'));
    holder.append(typePanel?.el ?? null,
      el.type === 'image' ? null : h('div', { class: 'row-s' }, h('span', { class: 'field-label', style: { flex: 'none', width: '64px' } }, el.type === 'plot' ? 'Axes pen' : 'Pen'), pen.el),
      h('div', { class: 'grid2' }, px.el, py.el), rot.el, actions);
    return { typePanel, pen, px, py, rot };
  }

  function sync() {
    const el = M.selected();
    empty.hidden = !!el; holder.hidden = !el;
    if (!el) { built = null; builtId = null; return; }
    if (builtId !== el.id) {
      built = build(el); builtId = el.id;
      if (store.get('fresh') === el.id && (el.type === 'text' || el.type === 'math')) holder.querySelector('textarea')?.select();   // new text: type right away
    }
    if (store.get('fresh') && store.get('fresh') !== el.id) store.set({ fresh: null });
    built.typePanel?.sync?.(el); built.pen.sync(); built.px.set(Math.round(el.x * 10) / 10); built.py.set(Math.round(el.y * 10) / 10); built.rot.sync(el.rot ?? 0);
  }
  store.on(['elements', 'selection', 'config', 'preview'], sync, { immediate: true });
  // double click on the board: jump into the text field
  window.addEventListener('wbot-focus-edit', () => holder.querySelector('textarea')?.focus());
  return { sync };
}

export { segmented };
