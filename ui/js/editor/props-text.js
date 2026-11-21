// Property panels for text boxes and formulas.
import { h } from '../util.js';
import { segmented, toggle } from '../widgets.js';
import * as M from './model.js';
import { sliderRow } from './props.js';
import { catalogOf, strokesSvg } from './library.js';

export function textPanel(api) {
  const el = api.get();
  const ta = h('textarea', { rows: '3', 'aria-label': 'Text', placeholder: 'Type your text', spellcheck: 'false' });
  ta.value = el.text ?? '';
  ta.addEventListener('input', () => api.set({ text: ta.value }, 'text'));
  const fonts = h('div', { class: 'font-grid', role: 'group', 'aria-label': 'Font' });
  catalogOf().then((c) => {
    fonts.replaceChildren(...(c?.fonts ?? []).map((f) => h('button', { type: 'button', class: 'font-btn', 'data-id': f.id, title: f.label, 'aria-pressed': String(api.get()?.font === f.id),
      onClick: () => api.set({ font: f.id }) }, strokesSvg(f.sample, { cls: 'font-sample' }), h('span', null, f.label))));
  });
  const size = sliderRow({ label: 'Size', min: 4, max: 160, step: 1, unit: 'mm', get: () => api.get().size, set: (v, k) => api.set({ size: v }, k), key: 'size' });
  const align = segmented({ label: 'Alignment', fill: true, value: el.align ?? 'center', options: [{ value: 'left', label: 'Left' }, { value: 'center', label: 'Centre' }, { value: 'right', label: 'Right' }],
    onChange: (v) => api.set({ align: v }) });
  const spacing = sliderRow({ label: 'Lines', min: 0.6, max: 2.2, step: 0.05, digits: 2, get: () => api.get().spacing ?? 1, set: (v, k) => api.set({ spacing: v }, k), key: 'spacing' });
  const tracking = sliderRow({ label: 'Letters', min: -2, max: 12, step: 0.5, digits: 1, get: () => api.get().tracking ?? 0, set: (v, k) => api.set({ tracking: v }, k), key: 'tracking' });
  const mono = toggle({ label: 'Fixed width (ASCII art)', checked: !!el.mono, onChange: (v) => api.set({ mono: v }) });
  const art = h('select', { 'aria-label': 'ASCII art examples' }, h('option', { value: '' }, 'Insert an ASCII art example …'));
  catalogOf().then((c) => { for (const a of c?.ascii_art ?? []) art.append(h('option', { value: a.name }, a.name)); art._items = c?.ascii_art ?? []; });
  art.addEventListener('change', () => {
    const a = art._items?.find((x) => x.name === art.value);
    if (a) { api.set({ text: a.text, mono: true, font: 'simple', align: 'center', size: 10 }); ta.value = a.text; }
    art.value = '';
  });
  const more = h('details', { class: 'more' }, h('summary', null, 'Spacing and ASCII art'), h('div', { class: 'stack more-body' }, spacing.el, tracking.el, mono.el, art));
  return {
    el: h('div', { class: 'stack' }, ta, fonts, size.el, align.el, more),
    sync(e) {
      if (document.activeElement !== ta) ta.value = e.text ?? '';
      size.sync(e.size); spacing.sync(e.spacing ?? 1); tracking.sync(e.tracking ?? 0); align.set(e.align ?? 'center'); mono.set(!!e.mono);
      for (const b of fonts.children) b.setAttribute('aria-pressed', String(b.dataset.id === e.font));
    },
  };
}

const SNIPPETS = [
  ['x²', '^{2}'], ['xₙ', '_{n}'], ['a⁄b', '\\frac{a}{b}'], ['√', '\\sqrt{x}'], ['Σ', '\\sum_{i=1}^{n} i'], ['∫', '\\int_{a}^{b} f(x)\\,dx'],
  ['lim', '\\lim_{x \\to 0}'], ['π', '\\pi'], ['α', '\\alpha'], ['θ', '\\theta'], ['≤', '\\leq'], ['≠', '\\neq'], ['→', '\\to'], ['∞', '\\infty'],
  ['( )', '\\left( x \\right)'], ['matrix', '\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}'],
];
const EXAMPLES = [
  ['Quadratic formula', '\\frac{-b \\pm \\sqrt{b^2-4ac}}{2a}'], ['Euler', 'e^{i\\pi} + 1 = 0'], ['Pythagoras', 'a^2 + b^2 = c^2'],
  ['Energy', 'E = mc^2'], ['Gauss integral', '\\int_{-\\infty}^{\\infty} e^{-x^2}\\,dx = \\sqrt{\\pi}'],
  ['Sum', '\\sum_{k=1}^{n} k = \\frac{n(n+1)}{2}'], ['Derivative', "f'(x) = \\lim_{h \\to 0} \\frac{f(x+h)-f(x)}{h}"],
  ['Matrix', 'A = \\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}'], ['Maxwell', '\\nabla \\cdot \\vec{E} = \\frac{\\rho}{\\epsilon_0}'],
];

export function mathPanel(api) {
  const el = api.get();
  const ta = h('textarea', { rows: '3', class: 'code', 'aria-label': 'Formula (LaTeX)', placeholder: 'x^2 + \\frac{a}{b}', spellcheck: 'false' });
  ta.value = el.latex ?? '';
  ta.addEventListener('input', () => api.set({ latex: ta.value }, 'latex'));
  const err = h('div', { class: 'note danger', role: 'status', hidden: true });
  const chips = h('div', { class: 'snippets', role: 'group', 'aria-label': 'Insert' }, SNIPPETS.map(([label, code]) => h('button', { type: 'button', class: 'snip', title: code, onClick: () => insert(code) }, label)));
  function insert(code) {
    const a = ta.selectionStart ?? ta.value.length, b = ta.selectionEnd ?? a;
    ta.value = ta.value.slice(0, a) + code + ta.value.slice(b);
    const brace = code.indexOf('{');
    const pos = a + (brace >= 0 ? brace + 1 : code.length);
    ta.focus(); ta.setSelectionRange(pos, pos);
    api.set({ latex: ta.value }, 'latex');
  }
  const ex = h('select', { 'aria-label': 'Examples' }, h('option', { value: '' }, 'Examples …'), EXAMPLES.map(([n]) => h('option', { value: n }, n)));
  ex.addEventListener('change', () => { const f = EXAMPLES.find((x) => x[0] === ex.value); if (f) { ta.value = f[1]; api.set({ latex: f[1] }); } ex.value = ''; });
  const size = sliderRow({ label: 'Size', min: 5, max: 120, step: 1, unit: 'mm', get: () => api.get().size, set: (v, k) => api.set({ size: v }, k), key: 'size' });
  return {
    el: h('div', { class: 'stack' }, ta, err, chips, ex, size.el,
      h('p', { class: 'muted', style: { fontSize: '12.5px' } }, 'LaTeX-style: ^ and _ for powers and indices, \\frac{a}{b}, \\sqrt{x}, Greek names like \\alpha.')),
    sync(e) {
      if (document.activeElement !== ta) ta.value = e.latex ?? '';
      size.sync(e.size);
      const msg = M.info(e.id)?.error;
      err.hidden = !msg; err.textContent = msg ?? '';
    },
  };
}
