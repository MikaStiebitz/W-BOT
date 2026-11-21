// Property panels for symbols, traced images and test patterns.
import { h, debounce, lsSet } from '../util.js';
import { store } from '../store.js';
import { api as server } from '../api.js';
import { numField, selectField, toggle, slider, segmented } from '../widgets.js';
import * as M from './model.js';
import { sliderRow } from './props.js';
import { openSymbols } from './bridge.js';
import { catalogOf } from './library.js';

export function symbolPanel(api) {
  const size = sliderRow({ label: 'Size', min: 8, max: 600, step: 1, unit: 'mm', get: () => api.get().size, set: (v, k) => api.set({ size: v }, k), key: 'size' });
  const err = h('div', { class: 'note danger', role: 'status', hidden: true });
  const change = h('button', { class: 'btn sm', type: 'button', onClick: () => openSymbols(api.id) }, 'Change symbol …');
  return { el: h('div', { class: 'stack' }, change, err, size.el),
    sync(e) { size.sync(e.size); const m = M.info(e.id)?.error; err.hidden = !m; err.textContent = m ?? ''; } };
}

export const TRACE_DEFAULT = { mode: 'auto', detail: 50, invert: false, colors: false, shade: false };

/** Traces an image file with the element's options and stores the result in the element. */
export async function trace(id) {
  const el = M.getEl(id), file = M.fileOf(id);
  if (!el || !file) return;
  const o = { ...TRACE_DEFAULT, ...(el.trace ?? {}) };
  try {
    const r = await server.importFile(file, { mode: o.mode, detail: o.detail, invert: o.invert, use_pen_colors: o.colors, shade: o.shade, pen: el.color ?? 0 });
    if (M.getEl(id)) M.update(id, { drawing: r.drawing, info: r.info ?? null, warnings: r.warnings ?? [] });
  } catch { /* api.js already showed the notice */ }
}

export function imagePanel(api) {
  const el0 = api.get();
  const hasFile = () => !!M.fileOf(api.id) && !/svg/i.test(api.get()?.name ?? '');
  const opt = () => ({ ...TRACE_DEFAULT, ...(api.get()?.trace ?? {}) });
  const setOpt = (patch) => { const t = { ...opt(), ...patch }; lsSet('wbot-trace', t); api.set({ trace: t }); retrace(); };
  const retrace = debounce(() => trace(api.id), 350);
  const ALL_MODES = [
    { value: 'auto', label: 'Auto', title: 'Picks the best style for the image' },
    { value: 'photo', label: 'Photo', title: 'Outlines and inner lines of objects (photos, renders)' },
    { value: 'lines', label: 'Lines', title: 'Centre lines of thin strokes (line art, handwriting)' },
    { value: 'outline', label: 'Outline', title: 'Edges of dark areas (logos, silhouettes)' },
    { value: 'fill', label: 'Fill', title: 'Hatching with the pen width (solid areas, slow)' }];
  const mode = segmented({ label: 'Drawing style', fill: true, value: opt().mode, options: ALL_MODES, onChange: (v) => setOpt({ mode: v }) });
  let canPhoto = true;
  catalogOf().then((c) => { if (c?.trace_modes) { canPhoto = c.trace_modes.includes('photo'); mode.buttons.forEach((b) => { if (!c.trace_modes.includes(b.dataset.v)) b.hidden = true; }); } });
  const shade = toggle({ label: 'Shade dark areas', checked: opt().shade, onChange: (v) => setOpt({ shade: v }) });
  const colors = toggle({ label: 'Use all pen colours', checked: opt().colors, onChange: (v) => setOpt({ colors: v }) });
  const invert = toggle({ label: 'Invert (light on dark)', checked: opt().invert, onChange: (v) => setOpt({ invert: v }) });
  const size = sliderRow({ label: 'Size', min: 20, max: 1400, step: 5, unit: 'mm', get: () => api.get().size, set: (v, k) => api.set({ size: v }, k), key: 'size' });
  const opts = h('div', { class: 'stack' }, mode.el, detail.el, shade.el, colors.el, invert.el);
  const note = h('div', { class: 'muted file-meta' });
  return { el: h('div', { class: 'stack' }, note, size.el, opts),
    sync(e) {
      size.sync(e.size); opts.hidden = !hasFile();
      const o = opt(); mode.set(o.mode); detail.sync(o.detail); shade.el.hidden = !canPhoto || !(o.mode === 'photo' || (o.mode === 'auto' && e.info?.mode === 'photo'));
      colors.el.hidden = o.mode === 'photo'; invert.el.hidden = !shade.el.hidden; shade.set(o.shade); colors.set(o.colors); invert.set(o.invert);
      note.textContent = e.info ? `${e.name ?? 'Image'} · ${e.info.pixels?.join(' × ')} px · ${e.info.mode}` : (e.name ?? 'Image');
    } };
}

export function patternPanel(api) {
  const pat = () => store.get('patterns').find((p) => p.id === api.get()?.pattern);
  const size = sliderRow({ label: 'Size', min: 20, max: 1400, step: 5, unit: 'mm', get: () => api.get().size, set: (v, k) => api.set({ size: v }, k), key: 'size' });
  const box = h('div', { class: 'stack' });
  let shown = null;
  function paint() {
    const p = pat();
    box.replaceChildren();
    if (!p) return;
    const vals = { ...Object.fromEntries(p.params.map((q) => [q.id, q.default])), ...(api.get().params ?? {}) };
    const setV = (id, v) => { vals[id] = v; api.set({ params: { ...vals } }, 'p' + id); };
    box.append(h('p', { class: 'muted', style: { fontSize: '13.5px' } }, p.description));
    for (const q of p.params) {
      if (q.kind === 'bool') box.append(toggle({ label: q.label, checked: !!vals[q.id], onChange: (v) => setV(q.id, v) }).el);
      else if (q.kind === 'choice') box.append(selectField({ label: q.label, options: (q.choices ?? []).map((c) => (typeof c === 'object' ? [c.value, c.label ?? c.value] : [c, c])), value: String(vals[q.id]), onChange: (v) => setV(q.id, v) }).el);
      else {
        const isInt = q.kind === 'int';
        const num = numField({ label: q.label, unit: q.unit, min: q.min, max: q.max, step: isInt ? 1 : 'any', value: vals[q.id], onChange: (v) => { if (v != null) setV(q.id, isInt ? Math.round(v) : v); } });
        box.append(num.el);
      }
    }
  }
  return { el: h('div', { class: 'stack' }, size.el, box),
    sync(e) { size.sync(e.size); if (shown !== e.pattern) { shown = e.pattern; paint(); } } };
}
export { slider };
