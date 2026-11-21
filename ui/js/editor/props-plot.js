// Property panel for function plots: curves, ranges, size and axis style.
import { h } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { numField, segmented, toggle, colorHex } from '../widgets.js';
import * as M from './model.js';
import { sliderRow } from './props.js';
import { catalogOf } from './library.js';

const KINDS = [['y', 'y = f(x)'], ['param', 'x(t), y(t)'], ['polar', 'r(t) polar'], ['points', 'Points']];

export function plotPanel(api) {
  const el0 = api.get();
  const curvesBox = h('div', { class: 'stack curves' });
  const err = h('div', { class: 'note danger', role: 'status', hidden: true });
  const setCurves = (fn, key) => api.set({ curves: fn(structuredClone(api.get().curves ?? [])) }, key);
  let sig = '';

  function paintCurves(e) {
    const hex = store.get('config')?.colors?.map((c) => c.hex).join() ?? '';
    const s = e.curves.map((c) => `${c.kind}${c.color ?? 0}`).join('|') + '#' + hex;
    if (s === sig) return;
    sig = s;
    curvesBox.replaceChildren(...e.curves.map((c, i) => curveRow(c, i, e.curves.length)));
  }
  function curveRow(c, i, n) {
    const kind = h('select', { 'aria-label': 'Curve type' }, KINDS.map(([v, l]) => h('option', { value: v }, l)));
    kind.value = c.kind ?? 'y';
    kind.addEventListener('change', () => { sig = ''; setCurves((cs) => { cs[i].kind = kind.value; if (kind.value === 'param' && !cs[i].expr2) { cs[i].expr = 'cos(t)'; cs[i].expr2 = 'sin(t)'; } if (kind.value === 'polar') cs[i].expr = 't/2'; if (kind.value === 'y' && /t/.test(cs[i].expr)) cs[i].expr = 'x'; return cs; }); });
    const colors = store.get('config')?.colors ?? [];
    const dot = h('button', { type: 'button', class: 'pen-dot', title: 'Next pen', 'aria-label': 'Change the pen of this curve', style: { '--c': colorHex(store.get('config'), c.color ?? 0) },
      onClick: () => { sig = ''; setCurves((cs) => { cs[i].color = ((cs[i].color ?? 0) + 1) % Math.max(1, colors.length); return cs; }); } });
    const del = h('button', { class: 'btn sm ghost icon-only', type: 'button', title: 'Remove curve', 'aria-label': 'Remove curve', disabled: n < 2, onClick: () => { sig = ''; setCurves((cs) => { cs.splice(i, 1); return cs; }); } }, icon('x'));
    const input = (key, ph, label) => {
      const t = h('input', { type: 'text', placeholder: ph, 'aria-label': label, spellcheck: 'false', class: 'code' });
      t.value = c[key] ?? '';
      t.addEventListener('input', () => setCurves((cs) => { cs[i][key] = t.value; return cs; }, `c${i}${key}`));
      return t;
    };
    let body;
    if (c.kind === 'param') {
      const r = h('div', { class: 'grid2' }, numField({ label: 't from', step: 'any', value: c.t0 ?? 0, onChange: (v) => setCurves((cs) => { cs[i].t0 = v ?? 0; return cs; }) }).el,
        numField({ label: 't to', step: 'any', value: c.t1 ?? 6.2832, onChange: (v) => setCurves((cs) => { cs[i].t1 = v ?? 6.2832; return cs; }) }).el);
      body = [input('expr', 'x(t), e.g. cos(t)', 'x of t'), input('expr2', 'y(t), e.g. sin(t)', 'y of t'), r];
    } else if (c.kind === 'polar') {
      body = [input('expr', 'r(t), e.g. 1 + cos(t)', 'r of t'), h('div', { class: 'grid2' }, numField({ label: 't from', step: 'any', value: c.t0 ?? 0, onChange: (v) => setCurves((cs) => { cs[i].t0 = v ?? 0; return cs; }) }).el,
        numField({ label: 't to', step: 'any', value: c.t1 ?? 6.2832, onChange: (v) => setCurves((cs) => { cs[i].t1 = v ?? 6.2832; return cs; }) }).el)];
    } else if (c.kind === 'points') {
      const ta = h('textarea', { rows: '3', class: 'code', 'aria-label': 'Points', placeholder: '0,0\n1,2\n2,1.5', spellcheck: 'false' });
      ta.value = (c.points ?? []).map((p) => p.join(', ')).join('\n');
      ta.addEventListener('input', () => setCurves((cs) => { cs[i].points = ta.value.split('\n').map((l) => l.split(/[ ,;\t]+/).map(Number).filter((v) => Number.isFinite(v))).filter((p) => p.length === 2); return cs; }, `c${i}pts`));
      body = [ta];
    } else body = [input('expr', 'f(x), e.g. sin(x)/x', 'f of x')];
    return h('div', { class: 'curve' }, h('div', { class: 'row-s' }, kind, dot, del), body);
  }

  const addBtn = h('button', { class: 'btn sm', type: 'button', onClick: () => { sig = ''; setCurves((cs) => { cs.push({ kind: 'y', expr: 'x', color: cs.length % Math.max(1, store.get('config')?.colors?.length ?? 1) }); return cs; }); } }, icon('plus'), 'Add curve');
  const preset = h('select', { 'aria-label': 'Example plots' }, h('option', { value: '' }, 'Examples …'));
  catalogOf().then((c) => { preset._items = c?.plot_presets ?? []; for (const p of preset._items) preset.append(h('option', { value: p.name }, p.name)); });
  preset.addEventListener('change', () => { const p = preset._items?.find((x) => x.name === preset.value); if (p) { sig = ''; api.set({ ymin: null, ymax: null, ...structuredClone(p.spec) }); } preset.value = ''; });

  const rng = (key, label) => numField({ label, step: 'any', value: el0[key] ?? '', onChange: (v) => api.set({ [key]: v }) });
  const xmin = rng('xmin', 'x min'), xmax = rng('xmax', 'x max'), ymin = rng('ymin', 'y min'), ymax = rng('ymax', 'y max');
  const yAuto = toggle({ label: 'Fit the height to the curves', checked: el0.ymin == null || el0.ymax == null, onChange: (v) => {
    if (v) api.set({ ymin: null, ymax: null }); else api.set({ ymin: -5, ymax: 5 });
  } });
  const w = sliderRow({ label: 'Width', min: 60, max: 900, step: 5, unit: 'mm', get: () => api.get().w, set: (v, k) => api.set({ w: v }, k), key: 'w' });
  const hh = sliderRow({ label: 'Height', min: 40, max: 700, step: 5, unit: 'mm', get: () => api.get().h, set: (v, k) => api.set({ h: v }, k), key: 'h' });
  const axes = segmented({ label: 'Axes', fill: true, value: el0.axes ?? 'cross', options: [{ value: 'cross', label: 'Axes' }, { value: 'box', label: 'Frame' }, { value: 'none', label: 'None' }], onChange: (v) => api.set({ axes: v }) });
  const grid = toggle({ label: 'Dotted grid', checked: el0.grid !== false, onChange: (v) => api.set({ grid: v }) });
  const labels = toggle({ label: 'Numbers on the axes', checked: el0.labels !== false, onChange: (v) => api.set({ labels: v }) });
  const txt = (key, label) => { const t = h('input', { type: 'text', 'aria-label': label, placeholder: label }); t.value = el0[key] ?? ''; t.addEventListener('input', () => api.set({ [key]: t.value }, key)); return t; };
  const xl = txt('xlabel', 'x label'), yl = txt('ylabel', 'y label'), title = txt('title', 'Title');
  const more = h('details', { class: 'more' }, h('summary', null, 'Range, size and labels'),
    h('div', { class: 'stack more-body' }, h('div', { class: 'grid2' }, xmin.el, xmax.el), yAuto.el, h('div', { class: 'grid2' }, ymin.el, ymax.el),
      w.el, hh.el, axes.el, grid.el, labels.el, h('div', { class: 'grid2' }, xl, yl), title));
  return {
    el: h('div', { class: 'stack' }, preset, curvesBox, addBtn, err, more),
    sync(e) {
      paintCurves(e);
      xmin.set(e.xmin); xmax.set(e.xmax); ymin.set(e.ymin ?? ''); ymax.set(e.ymax ?? '');
      const auto = e.ymin == null || e.ymax == null; yAuto.set(auto); ymin.input.disabled = ymax.input.disabled = auto;
      w.sync(e.w); hh.sync(e.h); axes.set(e.axes ?? 'cross'); grid.set(e.grid !== false); labels.set(e.labels !== false);
      const m = M.info(e.id)?.error; err.hidden = !m; err.textContent = m ?? '';
    },
  };
}
