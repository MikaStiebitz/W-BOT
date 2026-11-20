// Pens tab: interactive 3D revolver + colour, name and line width of every pen.
import { h, debounce, lsSet } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, gate, toast } from '../ui.js';
import { caps } from '../caps.js';
import { mountRevolver } from '../revolver3d.js';

const PALETTE = ['#111111', '#d32f2f', '#1565c0', '#2e7d32', '#ef6c00', '#6a1b9a', '#00838f', '#795548'];

export function mountPens(root) {
  const view = h('div', { class: 'rev-wrap' });
  const hint = h('p', { class: 'field-hint', style: { textAlign: 'center' } }, 'Drag to rotate · click a pen to select · double-click resets the view');
  const rev = mountRevolver(view, { onSelect: (i) => select(i) });
  const cards = h('div', { class: 'stack' });
  const note = h('div', { class: 'gate-note', role: 'status' });

  const cfgPens = () => store.get('config')?.colors ?? [];
  const slots = () => store.get('config')?.servos?.revolver?.slots_deg ?? [-60, 0, 60];
  const sel = () => Math.min(store.get('selectedPen') ?? 0, Math.max(0, cfgPens().length - 1));

  function select(i) { store.set({ selectedPen: i }); lsSet('wbot-pen', i); }

  const save = debounce(async () => {
    try {
      const r = await api.putConfig({ colors: cfgPens().map((c) => ({ name: (c.name || '').trim() || 'pen', hex: c.hex, width_mm: c.width_mm })) });
      store.set({ config: r });
    } catch { /* the notice came from api.js; the edit stays visible so nothing is lost */ }
  }, 450);

  function edit(i, patch) {
    const colors = cfgPens().map((c) => ({ ...c }));
    Object.assign(colors[i], patch);
    store.set({ config: { ...store.get('config'), colors } });          // live in every view; saved a moment later
    save();
  }

  function buildCards() {
    const pens = cfgPens();
    cards.replaceChildren(...pens.map((p, i) => {
      const color = h('input', { type: 'color', value: p.hex, 'aria-label': `Pen ${i + 1} colour`, onInput: (e) => edit(i, { hex: e.target.value }) });
      const name = h('input', { type: 'text', value: p.name, maxlength: '20', 'aria-label': `Pen ${i + 1} name`, onInput: (e) => edit(i, { name: e.target.value }) });
      const wv = h('span', { class: 'mono-val pen-w' }, `${p.width_mm.toFixed(1)} mm`);
      const width = h('input', { type: 'range', min: '0.3', max: '8', step: '0.1', value: String(p.width_mm), 'aria-label': `Pen ${i + 1} line width`,
        onInput: (e) => { const v = Number(e.target.value); wv.textContent = `${v.toFixed(1)} mm`; edit(i, { width_mm: v }); } });
      const pal = h('div', { class: 'pal' }, PALETTE.map((hex) => h('button', { type: 'button', class: 'pal-c', style: { background: hex }, title: hex, 'aria-label': `Use ${hex}`,
        onClick: () => { color.value = hex; edit(i, { hex }); } })));
      const go = h('button', { class: 'btn sm', type: 'button', onClick: (e) => run(e.currentTarget, async () => {
        select(i);
        await api.revolver({ slot: i + 1 });
        toast(`Pen ${i + 1} is in position.`, 'ok');
      }) }, icon('refresh'), 'Move here');
      go.dataset.pen = String(i);
      const card = h('div', { class: 'pen-card', 'aria-current': String(i === sel()), dataset: { i } },
        h('div', { class: 'pen-head', onClick: () => select(i) }, h('span', { class: 'pen-no' }, String(i + 1)), color, name, go),
        h('div', { class: 'pen-body' }, h('div', { class: 'pen-line' }, h('span', { class: 'field-label' }, 'Line width'), wv), width, pal));
      return card;
    }));
  }

  function paint() {
    const cfg = store.get('config'); if (!cfg) return;
    const pens = cfgPens();
    const sig = String(pens.length);
    if (cards._sig !== sig) { cards._sig = sig; buildCards(); }
    cards.querySelectorAll('.pen-card').forEach((c, i) => c.setAttribute('aria-current', String(i === sel())));
    const s = store.get('state'), c = caps(s, store.get('job'));
    const connected = c.connected;
    const angle = connected && s.revolver?.angle != null ? s.revolver.angle : (slots()[sel()] ?? 0);
    rev.set({ pens: pens.map((p) => ({ hex: p.hex, width_mm: p.width_mm })), slots: slots(), selected: sel(), angle });
    cards.querySelectorAll('button[data-pen]').forEach((b) => gate(b, c.move));
    note.textContent = c.move ? c.move.replace(/^Not connected\.?.*/i, 'Connect to turn the revolver. Colours and widths can be edited any time.') : '';
  }

  root.append(h('div', { class: 'sect' }, view, hint, cards, note));
  store.on(['config', 'state', 'selectedPen', 'job'], paint, { immediate: true });
  store.on('tab', () => { if (store.get('tab') === 'pens') { rev.start(); paint(); } else rev.stop(); }, { immediate: true });
}
