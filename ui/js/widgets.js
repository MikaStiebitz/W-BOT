// Formular-Bausteine: Zahlenfeld, Schalter, Segmente, Slider.
import { h } from './util.js';

let uid = 0;
export const nextId = (p = 'f') => `${p}${++uid}`;

export function numField({ label, value = '', min, max, step = 'any', unit, hint, onChange, onInput, cls = '' }) {
  const id = nextId('n');
  const input = h('input', { type: 'number', id, inputmode: 'decimal', min, max, step, value: value == null ? '' : String(value) });
  const el = h('div', { class: 'field ' + cls },
    h('label', { class: 'field-label', for: id }, label),
    h('span', { class: 'input-unit' }, input, unit ? h('span', { class: 'unit' }, unit) : null),
    hint ? h('span', { class: 'field-hint' }, hint) : null);
  const get = () => { const v = parseFloat(input.value); return Number.isFinite(v) ? v : null; };
  input.addEventListener('change', () => onChange?.(get()));
  input.addEventListener('input', () => onInput?.(get()));
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { input.dispatchEvent(new Event('change')); } });
  return {
    el, input, get,
    set(v) { if (document.activeElement !== input) input.value = v == null ? '' : String(v); },
    setLimits(lo, hi) { if (lo != null) input.min = lo; if (hi != null) input.max = hi; },
  };
}

export function selectField({ label, options, value, onChange, cls = '' }) {
  const id = nextId('s');
  const sel = h('select', { id }, options.map((o) => {
    const [v, l] = Array.isArray(o) ? o : [o, o];
    return h('option', { value: v }, l);
  }));
  if (value != null) sel.value = value;
  sel.addEventListener('change', () => onChange?.(sel.value));
  return { el: h('div', { class: 'field ' + cls }, h('label', { class: 'field-label', for: id }, label), sel), select: sel };
}

export function toggle({ label, checked = false, onChange }) {
  const input = h('input', { type: 'checkbox', checked });
  const el = h('label', { class: 'switch' }, input, h('span', { class: 'track' }), h('span', null, label));
  input.addEventListener('change', () => onChange?.(input.checked));
  return { el, input, get: () => input.checked, set(v) { input.checked = !!v; } };
}

/** Segmentiertes Auswahlfeld. options: [{value, label, title}] */
export function segmented({ options, value, onChange, label, fill = false }) {
  const btns = options.map((o) => h('button', {
    type: 'button', 'aria-pressed': String(o.value === value), title: o.title, dataset: { v: String(o.value) },
    onClick: () => { set(o.value); onChange?.(o.value); },
  }, o.label));
  const el = h('div', { class: 'seg' + (fill ? ' fill' : ''), role: 'group', 'aria-label': label }, btns);
  let cur = value;
  function set(v) {
    cur = v;
    options.forEach((o, i) => btns[i].setAttribute('aria-pressed', String(o.value === v)));
  }
  return { el, set, get: () => cur, buttons: btns };
}

/** Slider: onInput beim Ziehen, onChange beim Loslassen. */
export function slider({ min, max, step = 1, value = 0, onInput, onChange, label }) {
  const input = h('input', { type: 'range', min, max, step, value: String(value), 'aria-label': label });
  input.addEventListener('input', () => onInput?.(parseFloat(input.value)));
  input.addEventListener('change', () => onChange?.(parseFloat(input.value)));
  return {
    input,
    get: () => parseFloat(input.value),
    set(v) { if (document.activeElement !== input && !input._drag) input.value = String(v); },
    setLimits(lo, hi) { input.min = lo; input.max = hi; },
  };
}

/** Farbname → CSS-Farbe aus der Config (0-basierter Index). */
export function colorHex(cfg, idx) {
  return cfg?.colors?.[idx]?.hex ?? '#111111';
}
export function swatch(hex) { return h('span', { class: 'swatch', style: { background: hex } }); }
