// "Add" bar and symbol library of the canvas editor. Chips can be clicked or dragged onto the board.
import { h } from '../util.js';
import { icon } from '../icons.js';
import { api } from '../api.js';
import { store } from '../store.js';
import * as M from './model.js';
import { DRAG_TYPE } from './board-edit.js';

const NS = 'http://www.w3.org/2000/svg';
let catalogP = null;
export const loadCatalog = () => (catalogP ??= api.elementsCatalog({ quiet: true }).catch(() => { catalogP = null; return null; }));

/** Inline SVG of flat strokes [[x0,y0,x1,y1,...],...] (y up). Uses currentColor, so it follows the theme. */
export function strokesSvg(strokes, { pad = 4, cls = 'thumb-svg' } = {}) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const s of strokes) for (let i = 0; i < s.length; i += 2) { x0 = Math.min(x0, s[i]); x1 = Math.max(x1, s[i]); y0 = Math.min(y0, s[i + 1]); y1 = Math.max(y1, s[i + 1]); }
  if (!Number.isFinite(x0)) { x0 = y0 = 0; x1 = y1 = 1; }
  const w = Math.max(x1 - x0, 1e-3), hh = Math.max(y1 - y0, 1e-3), k = 100 / Math.max(w, hh);
  const d = strokes.map((s) => 'M' + Array.from({ length: s.length / 2 }, (_, i) => `${((s[2 * i] - x0) * k + pad).toFixed(1)} ${((y1 - s[2 * i + 1]) * k + pad).toFixed(1)}`).join('L')).join('');
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${(w * k + 2 * pad).toFixed(1)} ${(hh * k + 2 * pad).toFixed(1)}`);
  svg.setAttribute('class', cls); svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  const path = document.createElementNS(NS, 'path');
  path.setAttribute('d', d); path.setAttribute('fill', 'none'); path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', '3.2'); path.setAttribute('stroke-linecap', 'round'); path.setAttribute('stroke-linejoin', 'round');
  path.setAttribute('vector-effect', 'non-scaling-stroke');
  svg.append(path);
  return svg;
}

/** A button that adds an element on click and can be dragged onto the board. */
export function chip({ type, props, label, title, child, cls = 'chip-btn', onClick }) {
  const b = h('button', { type: 'button', class: cls, title: title ?? label, 'aria-label': title ?? label, draggable: 'true' }, child ?? label);
  b.addEventListener('dragstart', (e) => {
    e.dataTransfer.setData(DRAG_TYPE, JSON.stringify({ type, props }));
    e.dataTransfer.effectAllowed = 'copy';
  });
  b.addEventListener('click', () => (onClick ? onClick() : M.add(type, props)));
  return b;
}

export function mountLibrary(root, { onFile }) {
  const fileIn = h('input', { type: 'file', hidden: true, accept: 'image/png,image/jpeg,image/webp,image/bmp,image/gif,image/svg+xml,.svg' });
  fileIn.addEventListener('change', () => { const f = fileIn.files?.[0]; fileIn.value = ''; if (f) onFile(f); });

  const big = (ic, label, title, type, props, onClick) => chip({ type, props, title, cls: 'add-btn', onClick,
    child: [icon(ic), h('span', null, label)] });
  const symBtn = big('symbol', 'Symbol', 'Pictograms, emoji, maths and arrows', 'symbol', { kind: 'icon', sym: 'smiley', size: 100 }, () => toggleSymbols());
  const imgBtn = h('button', { type: 'button', class: 'add-btn', title: 'Choose an image to trace, or drop one anywhere', onClick: () => fileIn.click() }, icon('image'), h('span', null, 'Image'));
  const bar = h('div', { class: 'add-bar', role: 'group', 'aria-label': 'Add to the board' },
    big('text', 'Text', 'Add a text box', 'text', {}), big('formula', 'Formula', 'Add a formula', 'math', {}),
    big('plot', 'Plot', 'Add a function graph', 'plot', {}), symBtn, imgBtn, fileIn);

  /* ---------- symbol library ---------- */
  const tabs = h('div', { class: 'seg fill sym-tabs', role: 'group', 'aria-label': 'Symbol groups' });
  const grid = h('div', { class: 'sym-grid' });
  const emojiIn = h('input', { type: 'text', maxlength: '8', placeholder: 'Paste an emoji', 'aria-label': 'Emoji' });
  const emojiBox = h('div', { class: 'row-s emoji-in', hidden: true }, emojiIn,
    h('button', { class: 'btn sm', type: 'button', onClick: () => { const v = emojiIn.value.trim(); if (v) { pick('emoji', v); emojiIn.value = ''; } } }, 'Add'),
    h('span', { class: 'muted', style: { fontSize: '12px' } }, 'Windows: Win + .'));
  const panel = h('div', { class: 'sym-panel', hidden: true }, tabs, emojiBox, grid,
    h('p', { class: 'muted sym-hint' }, 'Click to add, or drag onto the board. Emoji outside the drawn set are traced from the system emoji font.'));
  let cat = null, group = 'icons', replaceId = null;

  function pick(kind, sym) {
    const sel = M.selected();
    if (replaceId && sel?.id === replaceId && sel.type === 'symbol') M.update(replaceId, { kind, sym });
    else M.add('symbol', { kind, sym, size: kind === 'glyph' ? 70 : 100 });
  }
  const symChip = (kind, sym, child, title) => chip({ type: 'symbol', props: { kind, sym, size: kind === 'glyph' ? 70 : 100 }, title, cls: 'sym-chip', child, onClick: () => pick(kind, sym) });

  function paintGroup() {
    grid.replaceChildren(); emojiBox.hidden = group !== 'emoji';
    if (!cat) { grid.append(h('p', { class: 'muted' }, 'Loading …')); return; }
    if (group === 'icons') for (const i of cat.icons) grid.append(symChip('icon', i.id, strokesSvg(i.strokes), i.label));
    else if (group === 'emoji') for (const c of cat.emoji) grid.append(symChip('emoji', c, h('span', { class: 'emoji-ch' }, c), `Emoji ${c}`));
    else for (const g of cat.glyph_groups.filter((x) => x.name === group)) for (const gl of g.glyphs) grid.append(symChip('glyph', gl.id, strokesSvg(gl.strokes), gl.id));
  }
  function paintTabs() {
    const names = ['icons', 'emoji', ...(cat?.glyph_groups.map((g) => g.name) ?? [])];
    tabs.replaceChildren(...names.map((n) => h('button', { type: 'button', 'aria-pressed': String(n === group), onClick: () => { group = n; paintTabs(); paintGroup(); } },
      { icons: 'Pictures', emoji: 'Emoji' }[n] ?? n)));
  }
  function toggleSymbols(open, replace = null) {
    replaceId = replace;
    panel.hidden = open == null ? !panel.hidden : !open;
    symBtn.classList.toggle('on', !panel.hidden);
    if (!panel.hidden && !cat) loadCatalog().then((c) => { cat = c; paintTabs(); paintGroup(); });
    paintTabs(); paintGroup();
  }
  // a symbol was selected: keep the panel closed but let "Change symbol" reopen it in replace mode
  root.append(bar, panel);
  return { openSymbols: (id) => toggleSymbols(true, id), pickFile: () => fileIn.click() };
}

export async function fontSamples() { const c = await loadCatalog(); return c?.fonts ?? []; }
export async function catalogOf() { return loadCatalog(); }
