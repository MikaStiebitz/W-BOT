// Reiter "Einstellungen": wichtige Config-Werte ansehen/ändern (GET/PUT /api/config), Firmware-Config laden.
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
  G('Brett', null, [N('board.width_mm', 'Breite', 'mm'), N('board.height_mm', 'Höhe', 'mm'), N('board.margin_mm', 'Sicherheitsrand', 'mm')]),
  G('Bewegung', null, [N('motion.travel_feed_mm_min', 'Eilgang', 'mm/min', 50), N('motion.draw_feed_mm_min', 'Zeichnen', 'mm/min', 50),
    N('motion.jog_feed_mm_min', 'Jog', 'mm/min', 50), N('motion.acceleration_mm_s2', 'Beschleunigung', 'mm/s²', 10), XY('motion.park_position', 'Park-Position')]),
  G('Vorverzerrung', 'Genauigkeit der Kinematik: kleinere Toleranz = mehr Stützpunkte.', [N('motion.predistort_tolerance_mm', 'Toleranz', 'mm', 0.005),
    N('motion.max_chord_mm', 'Längste Sehne', 'mm'), N('motion.min_chord_mm', 'Kürzeste Sehne', 'mm')]),
  G('Stift-Servo', null, [N('servos.pen.up_deg', 'Winkel oben', '°', 0.5), N('servos.pen.down_deg', 'Winkel unten', '°', 0.5), N('servos.pen.settle_s', 'Wartezeit', 's', 0.05),
    N('servos.pen.speed_deg_s', 'Tempo', '°/s', 10), N('servos.pen.center_us', 'Mitte', 'µs', 1), N('servos.pen.us_per_deg', 'Impuls je Grad', 'µs/°', 0.001)]),
  G('Revolver-Servo', null, [{ path: 'servos.revolver.slots_deg', label: 'Slot-Winkel', unit: '°', kind: 'list' }, N('servos.revolver.settle_s', 'Wartezeit', 's', 0.05),
    N('servos.revolver.speed_deg_s', 'Tempo', '°/s', 10), N('servos.revolver.center_us', 'Mitte', 'µs', 1), N('servos.revolver.us_per_deg', 'Impuls je Grad', 'µs/°', 0.001)]),
  G('Geometrie', 'Vorsicht: Diese Werte bestimmen die Genauigkeit der Vorverzerrung. Nur nach Nachmessen ändern.', [XY('geometry.anchor_left', 'Anker links'), XY('geometry.anchor_right', 'Anker rechts'),
    N('geometry.pulley_pitch_radius_mm', 'Scheibenradius', 'mm', 0.001), N('geometry.belt_scale_left', 'Riemenmaß links', '×', 0.0005), N('geometry.belt_scale_right', 'Riemenmaß rechts', '×', 0.0005),
    XY('geometry.pen_offset_mm', 'Stift-Versatz'), { path: 'geometry.model', label: 'Modell', kind: 'select', options: [['tangent', 'tangent (genau)'], ['naive', 'naiv (keine Vorverzerrung)']] }]),
];

export function mountSettings(root) {
  const ctl = [];            // {path, kind, get(), set(v)}
  const orig = () => store.get('config');
  const dirtyNote = h('span', { class: 'chip', hidden: true }, 'Ungespeicherte Änderungen');
  const saveBtn = h('button', { class: 'btn primary', type: 'button' }, icon('save'), 'Speichern');
  const resetBtn = h('button', { class: 'btn', type: 'button' }, icon('refresh'), 'Zurücksetzen');
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

  /* Farben */
  const colorBox = h('div', { class: 'cfg-colors' });
  let colorRows = [];
  function loadColors(cols) {
    colorRows = cols.map((c) => ({ name: h('input', { type: 'text', value: c.name, 'aria-label': 'Farbname', onInput: markDirty }), hex: h('input', { type: 'color', value: c.hex, 'aria-label': 'Farbwert', onInput: markDirty }) }));
    colorBox.replaceChildren(...colorRows.flatMap((r) => [r.hex, r.name]));
  }
  const colors = () => colorRows.map((r) => ({ name: r.name.value.trim() || 'Farbe', hex: r.hex.value }));

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
    if (!Object.keys(p).length) { toast('Keine Änderungen.', 'info'); return; }
    const cfg = await api.putConfig(p);
    store.set({ config: cfg });
    toast('Einstellungen gespeichert.', 'ok');
    load();
  }));
  resetBtn.addEventListener('click', () => { load(); toast('Änderungen verworfen.', 'info'); });

  root.append(h('div', { class: 'sect', style: { position: 'sticky', top: '0', zIndex: '3', background: 'var(--surface)', paddingBottom: 'var(--sp-3)' } },
    h('div', { class: 'row', style: { alignItems: 'center' } }, saveBtn, resetBtn, dirtyNote)));
  for (const g of GROUPS) {
    root.append(h('div', { class: 'sect' }, h('h2', null, g.title), g.hint ? h('div', { class: 'note warn' }, g.hint) : null,
      h('div', { class: 'cfg-grid' }, g.fields.map(build))));
  }
  root.append(h('div', { class: 'sect' }, h('h2', null, 'Farben (Revolver-Slots)'), colorBox,
    h('p', { class: 'field-hint' }, 'Die Anzahl der Farben ist fest an die Revolver-Slots gekoppelt.')));

  /* Firmware-Config */
  const pre = h('pre', { class: 'fw mono', hidden: true });
  let fwText = '';
  const getFw = async () => (fwText = await api.firmwareConfig());
  const showBtn = h('button', { class: 'btn', type: 'button' }, 'Anzeigen');
  const dlBtn = h('button', { class: 'btn', type: 'button' }, icon('download'), 'Herunterladen');
  const cpBtn = h('button', { class: 'btn', type: 'button' }, icon('copy'), 'Kopieren');
  showBtn.addEventListener('click', () => run(showBtn, async () => { if (!pre.hidden) { pre.hidden = true; showBtn.textContent = 'Anzeigen'; return; } await getFw(); pre.textContent = fwText; pre.hidden = false; showBtn.textContent = 'Ausblenden'; }));
  dlBtn.addEventListener('click', () => run(dlBtn, async () => { await getFw(); downloadBlob(fwText, 'config.yaml', 'text/yaml'); toast('config.yaml heruntergeladen.', 'ok'); }));
  cpBtn.addEventListener('click', () => run(cpBtn, async () => { await getFw(); toast((await copyText(fwText)) ? 'Firmware-Config kopiert.' : 'Kopieren nicht möglich.', 'info'); }));
  root.append(h('div', { class: 'sect' }, h('h2', null, 'Firmware-Config (FluidNC)'),
    h('p', { class: 'muted', style: { fontSize: '14px' } }, 'Wird aus den Werten oben erzeugt. Die Datei wird hier nur geladen und nicht auf die Maschine hochgeladen.'),
    h('div', { class: 'row' }, showBtn, dlBtn, cpBtn), pre));

  store.on('config', () => { if (!Object.keys(patch()).length) load(); }, { immediate: true });
}
