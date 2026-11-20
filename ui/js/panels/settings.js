// Tab "Settings": view and change key config values (GET/PUT /api/config), load the firmware config.
import { h, getPath, setPath, copyText, downloadBlob } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, toast } from '../ui.js';
import { numField, selectField } from '../widgets.js';

const G = (title, hint, fields) => ({ title, hint, fields });
const N = (path, label, unit, step = 'any') => ({ path, label, unit, step, kind: 'num' });
const XY = (path, label, unit = 'mm') => ({ path, label, unit, kind: 'xy' });

const GROUPS = [
  G('Board', null, [N('board.width_mm', 'Width', 'mm'), N('board.height_mm', 'Height', 'mm'), N('board.margin_mm', 'Safety margin', 'mm')]),
  G('Motion', null, [N('motion.travel_feed_mm_min', 'Rapid', 'mm/min', 50), N('motion.draw_feed_mm_min', 'Drawing', 'mm/min', 50),
    N('motion.jog_feed_mm_min', 'Jog', 'mm/min', 50), N('motion.acceleration_mm_s2', 'Acceleration', 'mm/s²', 10), XY('motion.park_position', 'Park position')]),
  G('Predistortion', 'Kinematics accuracy: a smaller tolerance means more points.', [N('motion.predistort_tolerance_mm', 'Tolerance', 'mm', 0.005),
    N('motion.max_chord_mm', 'Longest chord', 'mm'), N('motion.min_chord_mm', 'Shortest chord', 'mm')]),
  G('Pen servo', null, [N('servos.pen.up_deg', 'Up angle', '°', 0.5), N('servos.pen.down_deg', 'Down angle', '°', 0.5), N('servos.pen.settle_s', 'Settle time', 's', 0.05),
    N('servos.pen.speed_deg_s', 'Speed', '°/s', 10), N('servos.pen.center_us', 'Center', 'µs', 1), N('servos.pen.us_per_deg', 'Pulse per degree', 'µs/°', 0.001)]),
  G('Revolver servo', null, [{ path: 'servos.revolver.slots_deg', label: 'Slot angles', unit: '°', kind: 'list' }, N('servos.revolver.settle_s', 'Settle time', 's', 0.05),
    N('servos.revolver.speed_deg_s', 'Speed', '°/s', 10), N('servos.revolver.center_us', 'Center', 'µs', 1), N('servos.revolver.us_per_deg', 'Pulse per degree', 'µs/°', 0.001)]),
  G('Geometry', 'Caution: these values determine predistortion accuracy. Change them only after measuring.', [XY('geometry.anchor_left', 'Left anchor'), XY('geometry.anchor_right', 'Right anchor'),
    N('geometry.pulley_pitch_radius_mm', 'Pulley radius', 'mm', 0.001), N('geometry.belt_scale_left', 'Belt scale left', '×', 0.0005), N('geometry.belt_scale_right', 'Belt scale right', '×', 0.0005),
    XY('geometry.pen_offset_mm', 'Pen offset'), { path: 'geometry.model', label: 'Model', kind: 'select', options: [['tangent', 'tangent (exact)'], ['naive', 'naive (no predistortion)']] }]),
];

export function mountSettings(root) {
  const ctl = [];            // {path, kind, get(), set(v)}
  const orig = () => store.get('config');
  const dirtyNote = h('span', { class: 'chip', hidden: true }, 'Unsaved changes');
  const saveBtn = h('button', { class: 'btn primary', type: 'button' }, icon('save'), 'Save');
  const resetBtn = h('button', { class: 'btn', type: 'button' }, icon('refresh'), 'Reset');
  const markDirty = () => { dirtyNote.hidden = !Object.keys(patch()).length; };

  function build(f) {
    if (f.kind === 'num') {
      const c = numField({ label: f.label, unit: f.unit, step: f.step, onInput: markDirty });
      ctl.push({ path: f.path, kind: 'num', get: c.get, set: (v) => { c.input.value = v ?? ''; } });
      return c.el;
    }
    if (f.kind === 'select') {
      const c = selectField({ label: f.label, options: f.options, onChange: markDirty });
      ctl.push({ path: f.path, kind: 'select', get: () => c.select.value, set: (v) => { c.select.value = v; } });
      return c.el;
    }
    const n = f.kind === 'xy' ? 2 : 3;
    const cs = Array.from({ length: n }, (_, i) => numField({ label: `${f.label}${f.kind === 'xy' ? (i ? ' Y' : ' X') : ` ${i + 1}`}`, unit: f.unit, onInput: markDirty }));
    ctl.push({ path: f.path, kind: 'arr', get: () => { const v = cs.map((c) => c.get()); return v.some((x) => x == null) ? null : v; },
      set: (v) => cs.forEach((c, i) => { c.input.value = v?.[i] ?? ''; }) });
    return h('div', { style: { display: 'contents' } }, cs.map((c) => c.el));
  }

  /* Colors */
  const colorBox = h('div', { class: 'cfg-colors' });
  let colorRows = [];
  function loadColors(cols) {
    colorRows = cols.map((c) => ({ name: h('input', { type: 'text', value: c.name, 'aria-label': 'Color name', onInput: markDirty }), hex: h('input', { type: 'color', value: c.hex, 'aria-label': 'Color value', onInput: markDirty }) }));
    colorBox.replaceChildren(...colorRows.flatMap((r) => [r.hex, r.name]));
  }
  const colors = () => colorRows.map((r) => ({ name: r.name.value.trim() || 'Color', hex: r.hex.value }));

  function patch() {
    const cfg = orig(), out = {};
    if (!cfg) return out;
    for (const c of ctl) {
      const v = c.get();
      if (v == null) continue;
      if (JSON.stringify(v) !== JSON.stringify(getPath(cfg, c.path))) setPath(out, c.path, v);
    }
    if (colorRows.length && JSON.stringify(colors()) !== JSON.stringify(cfg.colors.map((x) => ({ name: x.name, hex: x.hex.toLowerCase() })))) out.colors = colors();
    return out;
  }
  function load() {
    const cfg = orig();
    if (!cfg) return;
    for (const c of ctl) c.set(getPath(cfg, c.path));
    loadColors(cfg.colors);
    markDirty();
  }

  saveBtn.addEventListener('click', () => run(saveBtn, async () => {
    const p = patch();
    if (!Object.keys(p).length) { toast('No changes.', 'info'); return; }
    const cfg = await api.putConfig(p);
    store.set({ config: cfg });
    toast('Settings saved.', 'ok');
    load();
  }));
  resetBtn.addEventListener('click', () => { load(); toast('Changes discarded.', 'info'); });

  root.append(h('div', { class: 'sect', style: { position: 'sticky', top: '0', zIndex: '3', background: 'var(--surface)', paddingBottom: 'var(--sp-3)' } },
    h('div', { class: 'row', style: { alignItems: 'center' } }, saveBtn, resetBtn, dirtyNote)));
  for (const g of GROUPS) {
    root.append(h('div', { class: 'sect' }, h('h2', null, g.title), g.hint ? h('div', { class: 'note warn' }, g.hint) : null,
      h('div', { class: 'cfg-grid' }, g.fields.map(build))));
  }
  root.append(h('div', { class: 'sect' }, h('h2', null, 'Colors (revolver slots)'), colorBox,
    h('p', { class: 'field-hint' }, 'The number of colors is tied to the revolver slots.')));

  /* Firmware config */
  const pre = h('pre', { class: 'fw mono', hidden: true });
  let fwText = '';
  const getFw = async () => (fwText = await api.firmwareConfig());
  const showBtn = h('button', { class: 'btn', type: 'button' }, 'Show');
  const dlBtn = h('button', { class: 'btn', type: 'button' }, icon('download'), 'Download');
  const cpBtn = h('button', { class: 'btn', type: 'button' }, icon('copy'), 'Copy');
  showBtn.addEventListener('click', () => run(showBtn, async () => { if (!pre.hidden) { pre.hidden = true; showBtn.textContent = 'Show'; return; } await getFw(); pre.textContent = fwText; pre.hidden = false; showBtn.textContent = 'Hide'; }));
  dlBtn.addEventListener('click', () => run(dlBtn, async () => { await getFw(); downloadBlob(fwText, 'config.yaml', 'text/yaml'); toast('config.yaml downloaded.', 'ok'); }));
  cpBtn.addEventListener('click', () => run(cpBtn, async () => { await getFw(); toast((await copyText(fwText)) ? 'Firmware config copied.' : 'Copy not available.', 'info'); }));
  root.append(h('div', { class: 'sect' }, h('h2', null, 'Firmware config (FluidNC)'),
    h('p', { class: 'muted', style: { fontSize: '14px' } }, 'Generated from the values above. The file is only loaded here, not uploaded to the machine.'),
    h('div', { class: 'row' }, showBtn, dlBtn, cpBtn), pre));

  store.on('config', () => { if (!Object.keys(patch()).length) load(); }, { immediate: true });
}
