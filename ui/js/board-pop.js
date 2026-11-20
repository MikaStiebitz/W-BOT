// Popover für einen Klick aufs Brett: Koordinaten anpassen, "Dorthin fahren" (Bestätigung) oder Referenz setzen.
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
  const close = h('button', { class: 'btn ghost sm icon-only', type: 'button', 'aria-label': 'Schließen', title: 'Schließen (Esc)',
    onClick: () => store.set({ target: null }) }, icon('x'));
  const info = h('p', { class: 'muted', style: { fontSize: '13.5px' } });
  const reason = h('div', { class: 'gate-note' });
  const goBtn = h('button', { class: 'btn primary', type: 'button' }, icon('target'), 'Dorthin fahren');
  const refBtn = h('button', { class: 'btn', type: 'button' });
  const refWarn = h('p', { class: 'field-hint' });
  const refErr = h('div', { class: 'note danger', role: 'alert', hidden: true });
  const pop = h('div', { class: 'pop', hidden: true, role: 'dialog', 'aria-label': 'Ziel auf dem Brett' },
    h('h3', null, h('span', null, 'Punkt auf dem Brett'), close),
    h('div', { class: 'grid2' }, xf.el, yf.el), info, reason, goBtn, refBtn, refErr, refWarn);
  wrap.append(pop);

  const feed = () => store.get('feed') || store.get('config')?.motion?.travel_feed_mm_min;

  goBtn.addEventListener('click', () => run(goBtn, async () => {
    const t = store.get('target'); if (!t) return;
    await api.goto(t.x, t.y, feed());
    toast(`Fahre nach X ${fmt(t.x, 1)} · Y ${fmt(t.y, 1)} mm.`, 'ok');
    store.set({ target: null });
  }));
  refBtn.addEventListener('click', () => run(refBtn, async () => {
    const t = store.get('target'); if (!t) return;
    refErr.hidden = true;
    try { await api.reference(t.x, t.y); } catch (e) { if (e instanceof ApiError) { refErr.textContent = e.message; refErr.hidden = false; } throw e; }
    toast(`Referenz gesetzt: Gondel steht bei X ${fmt(t.x, 1)} · Y ${fmt(t.y, 1)} mm.`, 'ok');
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
    info.textContent = p ? `Abstand zur Gondel: ${fmt(Math.hypot(t.x - p[0], t.y - p[1]), 1)} mm.` : 'Die Gondel hat noch keine Referenz.';
    const goReason = c.abs || (!inRange ? `Außerhalb des erlaubten Bereichs (X ${fmt(r?.[0], 0)}…${fmt(r?.[2], 0)}, Y ${fmt(r?.[1], 0)}…${fmt(r?.[3], 0)} mm).` : '');
    gate(goBtn, goReason);
    reason.textContent = goReason;
    goBtn.hidden = !c.referenced;
    reason.hidden = !c.referenced;
    gate(refBtn, c.move);
    refBtn.classList.toggle('primary', !c.referenced);
    const lbl = c.referenced ? 'Referenz hier neu setzen' : 'Gondel steht jetzt hier';
    if (refBtn._lbl !== lbl) { refBtn._lbl = lbl; refBtn.replaceChildren(icon('target'), lbl); }
    refWarn.textContent = c.referenced
      ? 'Nur wählen, wenn die Gondel wirklich an diesem Punkt steht. Das ändert die Zuordnung Brett zu Firmware.'
      : 'Teilt der Software mit, dass die Gondel jetzt an diesem Punkt steht. Erst danach sind absolute Fahrten möglich.';
    const key = `${t.sx}|${t.sy}`;
    if (key !== lastKey) {                          // nur beim neuen Klick positionieren, nach dem Befüllen
      lastKey = key;
      const w = wrap.clientWidth, hh = wrap.clientHeight;
      pop.style.left = clamp(t.sx + 16, 8, Math.max(8, w - pop.offsetWidth - 8)) + 'px';
      pop.style.top = clamp(t.sy + 16, 8, Math.max(8, hh - pop.offsetHeight - 8)) + 'px';
    }
  }
  store.on(['target', 'state', 'config', 'job'], update, { immediate: true });
}
