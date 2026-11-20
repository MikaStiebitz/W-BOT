// Reiter "Simulator" (nur bei Transport sim): Ist/Soll-Abweichung und gezielt Fehler einbauen.
import { h, fmt, lsSet } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, toast } from '../ui.js';
import { numField } from '../widgets.js';
import { connectBody, NEUTRAL } from '../simopts.js';

const PRESETS = [
  ['Keine Fehler', {}],
  ['Anker rechts 3 mm daneben', { anchor_right_dx: 3 }],
  ['Riemen links 0,4 % länger', { scale_left: 1.004 }],
  ['Stift 2 mm versetzt', { pen_dx: 2, pen_dy: -2 }],
  ['Alles zusammen', { anchor_left_dy: -2, anchor_right_dx: 3, scale_left: 1.003, scale_right: 0.997, pen_dx: 1, pen_dy: -1 }],
];

const FIELDS = [
  ['anchor_left_dx', 'Anker links X-Versatz', 'mm', 0.1], ['anchor_left_dy', 'Anker links Y-Versatz', 'mm', 0.1],
  ['anchor_right_dx', 'Anker rechts X-Versatz', 'mm', 0.1], ['anchor_right_dy', 'Anker rechts Y-Versatz', 'mm', 0.1],
  ['scale_left', 'Riemenmaß links', '×', 0.001], ['scale_right', 'Riemenmaß rechts', '×', 0.001],
  ['pen_dx', 'Stift-Versatz X', 'mm', 0.1], ['pen_dy', 'Stift-Versatz Y', 'mm', 0.1],
  ['radius', 'Riemenscheiben-Radius', 'mm', 0.01],
];

export function mountSimulator(root) {
  const sect = (title, ...kids) => h('div', { class: 'sect' }, h('div', { class: 'sect-h' }, h('h2', null, title)), ...kids);
  const hist = [];
  let lastInfo = null;

  /* ---------- Live ---------- */
  const errNow = h('div', { class: 'pulse' });
  const kv = h('dl', { class: 'kv' });
  const spark = h('canvas', { class: 'spark', 'aria-label': 'Verlauf der Abweichung' });
  const jobErr = h('div', { class: 'stats' });
  const jobErrNote = h('p', { class: 'muted', style: { fontSize: '14px' } });
  const proof = sect('Genauigkeit der gezeichneten Spur',
    h('p', { class: 'muted', style: { fontSize: '14px' } }, 'Abweichung der gezeichneten Spur von der Sollkontur. Das ist der Genauigkeitsnachweis des letzten Zeichnens im Simulator.'),
    jobErr, jobErrNote);
  const live = sect('Ist/Soll-Vergleich',
    h('p', { class: 'muted', style: { fontSize: '14px' } }, 'Soll ist die Position, die die Software für den Stift annimmt. Ist ist die Position in der simulierten echten Maschine. Die Differenz zeigt, wie gut Vorverzerrung und Referenz wirken.'),
    errNow, kv, spark);

  function paintSpark() {
    const dpr = window.devicePixelRatio || 1, w = spark.clientWidth, hh = spark.clientHeight;
    if (!w) return;
    spark.width = w * dpr; spark.height = hh * dpr;
    const c = spark.getContext('2d'); c.setTransform(dpr, 0, 0, dpr, 0, 0);
    const cs = getComputedStyle(document.documentElement);
    const max = Math.max(0.5, ...hist);
    c.strokeStyle = cs.getPropertyValue('--board-truth').trim() || '#d4237a'; c.lineWidth = 2; c.beginPath();
    hist.forEach((v, i) => { const x = (i / Math.max(1, hist.length - 1)) * (w - 8) + 4, y = hh - 6 - (v / max) * (hh - 14); i ? c.lineTo(x, y) : c.moveTo(x, y); });
    c.stroke();
    c.fillStyle = cs.getPropertyValue('--text-3').trim(); c.font = `11px ${cs.getPropertyValue('--font-mono')}`;
    c.fillText(`${fmt(max, 2)} mm`, 6, 12);
  }

  function paintProof() {
    const e = store.get('simInfo')?.job_error;
    const cell = (v, l) => h('div', { class: 'stat' }, h('b', null, v == null ? '–' : `${fmt(v, 3)} mm`), h('span', null, l));
    if (!e) { jobErr.replaceChildren(cell(null, 'Maximum'), cell(null, 'Mittel'), cell(null, '95. Perzentil')); jobErrNote.textContent = 'Noch keine Auswertung. Sie erscheint, sobald im Simulator etwas gezeichnet wurde.'; return; }
    jobErr.replaceChildren(cell(e.max_mm, 'Maximum'), cell(e.mean_mm, 'Mittel'), cell(e.p95_mm, '95. Perzentil'));
    jobErrNote.textContent = e.samples != null ? `Ausgewertet an ${fmt(e.samples, 0)} Stützpunkten.` : '';
  }

  function paintLive() {
    const i = store.get('simInfo'), s = store.get('state');
    const connected = s?.connection?.status === 'connected' && s.connection.transport === 'sim';
    const row = (k, v) => [h('dt', null, k), h('dd', null, v)];
    if (!connected || !i) { errNow.textContent = '–'; kv.replaceChildren(...row('Status', connected ? 'Warte auf Daten …' : 'Nicht mit dem Simulator verbunden')); return; }
    errNow.replaceChildren(i.error_mm == null ? '–' : fmt(i.error_mm, 2), h('small', null, 'mm aktuell'));
    const xy = (p) => (p ? `${fmt(p[0], 2)} | ${fmt(p[1], 2)}` : 'unbekannt');
    kv.replaceChildren(...row('Soll (Software)', xy(i.intended)), ...row('Ist (simuliert)', xy(i.physical)),
      ...row('Abweichung Maximum', i.error_max_mm == null ? '–' : `${fmt(i.error_max_mm, 2)} mm`),
      ...row('Abweichung Mittel', i.error_mean_mm == null ? '–' : `${fmt(i.error_mean_mm, 2)} mm`),
      ...row('Spur-Segmente', fmt(i.trace_segments, 0)));
    if (i !== lastInfo) {
      lastInfo = i;
      if (i.error_mm != null) { hist.push(i.error_mm); if (hist.length > 150) hist.shift(); }
      paintSpark();
    }
  }

  /* ---------- Fehler einbauen ---------- */
  const opts = () => store.get('simOpts');
  const save = (o) => { store.set({ simOpts: o }); lsSet('wbot-simopts', o); };
  const fields = {};
  const grid = h('div', { class: 'cfg-grid' });
  for (const [key, label, unit, step] of FIELDS) {
    const f = numField({ label, unit, step, value: opts().perturb?.[key] ?? NEUTRAL[key] ?? '',
      hint: key === 'radius' ? 'leer = Nennwert aus der Config' : undefined,
      onChange: (v) => save({ ...opts(), perturb: { ...opts().perturb, [key]: v } }) });
    fields[key] = f; grid.append(f.el);
  }
  const sx = numField({ label: 'Echter Start X', unit: 'mm', step: 1, value: opts().start?.[0] ?? 0, onChange: (v) => save({ ...opts(), start: [v ?? 0, opts().start?.[1] ?? 0] }) });
  const sy = numField({ label: 'Echter Start Y', unit: 'mm', step: 1, value: opts().start?.[1] ?? 0, onChange: (v) => save({ ...opts(), start: [opts().start?.[0] ?? 0, v ?? 0] }) });
  const presets = h('div', { class: 'row' }, PRESETS.map(([name, p]) => h('button', { class: 'btn sm', type: 'button', onClick: () => {
    const pert = { ...NEUTRAL, ...p };
    for (const [k] of FIELDS) fields[k].input.value = pert[k] == null ? '' : String(pert[k]);
    save({ ...opts(), perturb: pert });
  } }, name)));
  const reconnect = h('button', { class: 'btn primary', type: 'button' }, icon('plug'), 'Mit diesen Fehlern neu verbinden');
  reconnect.addEventListener('click', () => run(reconnect, async () => {
    store.set({ transport: 'sim' }); lsSet('wbot-transport', 'sim');
    if (store.get('state')?.connection?.status === 'connected') await api.disconnect();
    hist.length = 0;
    await api.connect(connectBody());
    toast('Simulator neu verbunden. Referenz neu setzen.', 'ok');
  }));
  const active = h('div', { class: 'row', style: { alignItems: 'center' } });
  const err = sect('Fehler einbauen, um die Vorverzerrung zu testen',
    h('p', { class: 'muted', style: { fontSize: '14px' } }, 'Die simulierte Maschine weicht absichtlich von dem ab, was die Software annimmt. Danach ein Testbild zeichnen und prüfen, wie groß die Abweichung wird. Die Fehler gelten ab der nächsten Verbindung. Das Simulator-Tempo wählst du oben bei „Verbinden“.'),
    presets, grid, h('div', { class: 'grid2' }, sx.el, sy.el), reconnect,
    h('div', { class: 'note warn' }, 'Beim Neuverbinden geht die Referenz verloren und die Spur wird gelöscht.'), active);

  root.append(proof, live, err);

  function paintActive() {
    const p = store.get('simInfo')?.perturb ?? {};
    const ks = Object.keys(p);
    active.replaceChildren(h('span', { class: 'field-label' }, 'Aktive Fehler:'),
      ...(ks.length ? ks.map((k) => h('span', { class: 'chip warn' }, `${k} = ${p[k]}`)) : [h('span', { class: 'chip ok' }, 'keine')]));
  }

  store.on(['simInfo', 'state'], () => { paintLive(); paintProof(); paintActive(); }, { immediate: true });
  window.addEventListener('themechange', paintSpark);

  /* ---------- Polling nur wenn sichtbar ---------- */
  let timer = null;
  async function poll() {
    const s = store.get('state');
    if (s?.connection?.status !== 'connected' || s.connection.transport !== 'sim') return;
    try { store.set({ simInfo: await api.simulator({ quiet: true }) }); } catch { /* still */ }
  }
  store.on(['tab', 'state'], () => {
    const want = store.get('tab') === 'sim' && store.get('state')?.connection?.transport === 'sim';
    if (want && !timer) { poll(); timer = setInterval(poll, 1000); }
    if (!want && timer) { clearInterval(timer); timer = null; }
  }, { immediate: true });
}
