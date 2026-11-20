// Popover for a click on the board: adjust coordinates, "Go there" (with confirmation) or set the position reference.
import { h, fmt, clamp } from './util.js';
import { icon } from './icons.js';
import { api } from './api.js';
import { run, gate, toast } from './ui.js';
import { numField } from './widgets.js';
import { caps } from './caps.js';
import { ApiError } from './util.js';

export function mountTargetPopover(wrap, store) {
  const set = (patch) => { const t = store.get('target'); if (t) store.set({ target: { ...t, ...patch } }); };
  const xf = numField({ label: 'X', unit: 'mm', step: 0.1, onInput: (v) => v != null && set({ x: v }) });
  const yf = numField({ label: 'Y', unit: 'mm', step: 0.1, onInput: (v) => v != null && set({ y: v }) });
  const close = h('button', { class: 'btn ghost sm icon-only', type: 'button', 'aria-label': 'Close', title: 'Close (Esc)',
    onClick: () => store.set({ target: null }) }, icon('x'));
  const info = h('p', { class: 'muted', style: { fontSize: '13.5px' } });
  const reason = h('div', { class: 'gate-note' });
  const goBtn = h('button', { class: 'btn primary', type: 'button' }, icon('target'), 'Go there');
  const refBtn = h('button', { class: 'btn', type: 'button' });
  const refErr = h('div', { class: 'note danger', role: 'alert', hidden: true });
  const pop = h('div', { class: 'pop', hidden: true, role: 'dialog', 'aria-label': 'Target on the board' },
    h('h3', null, h('span', null, 'Point on the board'), close),
    h('div', { class: 'grid2' }, xf.el, yf.el), info, reason, goBtn, refBtn, refErr);
  wrap.append(pop);

  const feed = () => store.get('feed') || store.get('config')?.motion?.travel_feed_mm_min;

  goBtn.addEventListener('click', () => run(goBtn, async () => {
    const t = store.get('target'); if (!t) return;
    await api.goto(t.x, t.y, feed());
    toast(`Moving to X ${fmt(t.x, 1)} · Y ${fmt(t.y, 1)} mm.`, 'ok');
    store.set({ target: null });
  }));
  refBtn.addEventListener('click', () => run(refBtn, async () => {
    const t = store.get('target'); if (!t) return;
    refErr.hidden = true;
    try { await api.reference(t.x, t.y); } catch (e) { if (e instanceof ApiError) { refErr.textContent = e.message; refErr.hidden = false; } throw e; }
    toast(`Position set: gondola at X ${fmt(t.x, 1)} · Y ${fmt(t.y, 1)} mm.`, 'ok');
    store.set({ target: null });
  }));
  pop.addEventListener('keydown', (e) => { if (e.key === 'Escape') store.set({ target: null }); });

  let lastKey = '';
  function update() {
    const t = store.get('target');
    pop.hidden = !t;
    if (!t) { lastKey = ''; refErr.hidden = true; return; }
    const s = store.get('state'), cfg = store.get('config'), c = caps(s, store.get('job'));
    xf.set(t.x); yf.set(t.y);
    const r = cfg?.board ? [cfg.board.margin_mm, cfg.board.margin_mm, cfg.board.width_mm - cfg.board.margin_mm, cfg.board.height_mm - cfg.board.margin_mm] : s?.limits?.safe_rect;
    const inRange = r && t.x >= r[0] && t.x <= r[2] && t.y >= r[1] && t.y <= r[3];
    const p = s?.reference?.set ? s.position?.physical : null;
    info.textContent = p ? `Distance to gondola: ${fmt(Math.hypot(t.x - p[0], t.y - p[1]), 1)} mm.` : 'The gondola has no position reference yet.';
    const goReason = c.abs || (!inRange ? `Outside the allowed range (X ${fmt(r?.[0], 0)}…${fmt(r?.[2], 0)}, Y ${fmt(r?.[1], 0)}…${fmt(r?.[3], 0)} mm).` : '');
    gate(goBtn, goReason);
    reason.textContent = goReason;
    goBtn.hidden = !c.referenced;
    reason.hidden = !c.referenced;
    gate(refBtn, c.move);
    refBtn.classList.toggle('primary', !c.referenced);
    const lbl = c.referenced ? 'Reset position here' : 'Set gondola position';
    if (refBtn._lbl !== lbl) { refBtn._lbl = lbl; refBtn.replaceChildren(icon('target'), lbl); }
    refBtn.dataset.title = c.referenced
      ? 'Choose this only if the gondola is really at this point. It changes how the board maps to the firmware.'
      : 'Tells the software the gondola hangs at this point. Absolute moves are possible only after that.';
    if (!refBtn._reason) refBtn.title = refBtn.dataset.title;
    const key = `${t.sx}|${t.sy}`;
    if (key !== lastKey) {                          // position only on a new click, after filling the fields
      lastKey = key;
      const w = wrap.clientWidth, hh = wrap.clientHeight, ph = pop.offsetHeight;
      pop.style.left = clamp(t.sx + 16, 8, Math.max(8, w - pop.offsetWidth - 8)) + 'px';
      // below the click if it fits, otherwise above it, always fully inside the board view (never clipped)
      const below = t.sy + 16, above = t.sy - ph - 16;
      pop.style.top = clamp(below + ph <= hh - 8 ? below : (above >= 8 ? above : 8), 8, Math.max(8, hh - ph - 8)) + 'px';
    }
  }
  store.on(['target', 'state', 'config', 'job'], update, { immediate: true });
}
