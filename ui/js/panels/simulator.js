// Tab "Simulator" (sim transport only): actual vs. intended deviation, and deliberate error injection.
import { h, fmt, lsSet } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, toast } from '../ui.js';
import { numField } from '../widgets.js';
import { connectBody, NEUTRAL } from '../simopts.js';

const PRESETS = [
  ['No errors', {}],
  ['Right anchor 3 mm off', { anchor_right_dx: 3 }],
  ['Left belt 0.4% longer', { scale_left: 1.004 }],
  ['Pen offset 2 mm', { pen_dx: 2, pen_dy: -2 }],
  ['All combined', { anchor_left_dy: -2, anchor_right_dx: 3, scale_left: 1.003, scale_right: 0.997, pen_dx: 1, pen_dy: -1 }],
];

const FIELDS = [
  ['anchor_left_dx', 'Left anchor X offset', 'mm', 0.1], ['anchor_left_dy', 'Left anchor Y offset', 'mm', 0.1],
  ['anchor_right_dx', 'Right anchor X offset', 'mm', 0.1], ['anchor_right_dy', 'Right anchor Y offset', 'mm', 0.1],
  ['scale_left', 'Belt scale left', '×', 0.001], ['scale_right', 'Belt scale right', '×', 0.001],
  ['pen_dx', 'Pen offset X', 'mm', 0.1], ['pen_dy', 'Pen offset Y', 'mm', 0.1],
  ['radius', 'Pulley radius', 'mm', 0.01],
];

export function mountSimulator(root) {
  const sect = (title, ...kids) => h('div', { class: 'sect' }, h('div', { class: 'sect-h' }, h('h2', null, title)), ...kids);
  const hist = [];
  let lastInfo = null;

  /* ---------- Live ---------- */
  const errNow = h('div', { class: 'pulse' });
  const kv = h('dl', { class: 'kv' });
  const spark = h('canvas', { class: 'spark', 'aria-label': 'Deviation over time' });
  const jobErr = h('div', { class: 'stats' });
  const jobErrNote = h('p', { class: 'muted', style: { fontSize: '14px' } });
  const proof = sect('Drawn-trace accuracy',
    h('p', { class: 'muted', style: { fontSize: '14px' } }, 'Deviation of the drawn trace from the target contour: the accuracy check for the last drawing in the simulator.'),
    jobErr, jobErrNote);
  const live = sect('Actual vs. intended',
    h('p', { class: 'muted', style: { fontSize: '14px' } }, 'Intended is where the software thinks the pen is. Actual is its position in the simulated real machine. The difference shows how well predistortion and the position reference work.'),
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
    if (!e) { jobErr.replaceChildren(cell(null, 'Maximum'), cell(null, 'Mean'), cell(null, '95th percentile')); jobErrNote.textContent = 'No result yet. It appears once something has been drawn in the simulator.'; return; }
    jobErr.replaceChildren(cell(e.max_mm, 'Maximum'), cell(e.mean_mm, 'Mean'), cell(e.p95_mm, '95th percentile'));
    jobErrNote.textContent = e.samples != null ? `Evaluated at ${fmt(e.samples, 0)} points.` : '';
  }

  function paintLive() {
    const i = store.get('simInfo'), s = store.get('state');
    const connected = s?.connection?.status === 'connected' && s.connection.transport === 'sim';
    const row = (k, v) => [h('dt', null, k), h('dd', null, v)];
    if (!connected || !i) { errNow.textContent = '–'; kv.replaceChildren(...row('Status', connected ? 'Waiting for data …' : 'Not connected to the simulator')); return; }
    errNow.replaceChildren(i.error_mm == null ? '–' : fmt(i.error_mm, 2), h('small', null, 'mm now'));
    const xy = (p) => (p ? `${fmt(p[0], 2)} | ${fmt(p[1], 2)}` : 'unknown');
    kv.replaceChildren(...row('Intended (software)', xy(i.intended)), ...row('Actual (simulated)', xy(i.physical)),
      ...row('Max deviation', i.error_max_mm == null ? '–' : `${fmt(i.error_max_mm, 2)} mm`),
      ...row('Mean deviation', i.error_mean_mm == null ? '–' : `${fmt(i.error_mean_mm, 2)} mm`),
      ...row('Trace segments', fmt(i.trace_segments, 0)));
    if (i !== lastInfo) {
      lastInfo = i;
      if (i.error_mm != null) { hist.push(i.error_mm); if (hist.length > 150) hist.shift(); }
      paintSpark();
    }
  }

  /* ---------- Error injection ---------- */
  const opts = () => store.get('simOpts');
  const save = (o) => { store.set({ simOpts: o }); lsSet('wbot-simopts', o); };
  const fields = {};
  const grid = h('div', { class: 'cfg-grid' });
  for (const [key, label, unit, step] of FIELDS) {
    const f = numField({ label, unit, step, value: opts().perturb?.[key] ?? NEUTRAL[key] ?? '',
      hint: key === 'radius' ? 'empty = nominal value from config' : undefined,
      onChange: (v) => save({ ...opts(), perturb: { ...opts().perturb, [key]: v } }) });
    fields[key] = f; grid.append(f.el);
  }
  const sx = numField({ label: 'Actual start X', unit: 'mm', step: 1, value: opts().start?.[0] ?? 0, onChange: (v) => save({ ...opts(), start: [v ?? 0, opts().start?.[1] ?? 0] }) });
  const sy = numField({ label: 'Actual start Y', unit: 'mm', step: 1, value: opts().start?.[1] ?? 0, onChange: (v) => save({ ...opts(), start: [opts().start?.[0] ?? 0, v ?? 0] }) });
  const presets = h('div', { class: 'row' }, PRESETS.map(([name, p]) => h('button', { class: 'btn sm', type: 'button', onClick: () => {
    const pert = { ...NEUTRAL, ...p };
    for (const [k] of FIELDS) fields[k].input.value = pert[k] == null ? '' : String(pert[k]);
    save({ ...opts(), perturb: pert });
  } }, name)));
  const reconnect = h('button', { class: 'btn primary', type: 'button' }, icon('plug'), 'Reconnect with these errors');
  reconnect.addEventListener('click', () => run(reconnect, async () => {
    store.set({ transport: 'sim' }); lsSet('wbot-transport', 'sim');
    if (store.get('state')?.connection?.status === 'connected') await api.disconnect();
    hist.length = 0;
    await api.connect(connectBody());
    toast('Simulator reconnected. Set the position reference again.', 'ok');
  }));
  const active = h('div', { class: 'row', style: { alignItems: 'center' } });
  const err = sect('Inject errors to test predistortion',
    h('p', { class: 'muted', style: { fontSize: '14px' } }, 'The simulated machine deliberately deviates from what the software assumes. Then draw a test pattern and check how large the deviation gets. Errors apply from the next connection. Set the simulator speed under “Connect” at the top.'),
    presets, grid, h('div', { class: 'grid2' }, sx.el, sy.el), reconnect,
    h('div', { class: 'note warn' }, 'Reconnecting clears the position reference and the trace.'), active);

  root.append(proof, live, err);

  function paintActive() {
    const p = store.get('simInfo')?.perturb ?? {};
    const ks = Object.keys(p);
    active.replaceChildren(h('span', { class: 'field-label' }, 'Active errors:'),
      ...(ks.length ? ks.map((k) => h('span', { class: 'chip warn' }, `${k} = ${p[k]}`)) : [h('span', { class: 'chip ok' }, 'none')]));
  }

  store.on(['simInfo', 'state'], () => { paintLive(); paintProof(); paintActive(); }, { immediate: true });
  window.addEventListener('themechange', paintSpark);

  /* ---------- Poll only while visible ---------- */
  let timer = null;
  async function poll() {
    const s = store.get('state');
    if (s?.connection?.status !== 'connected' || s.connection.transport !== 'sim') return;
    try { store.set({ simInfo: await api.simulator({ quiet: true }) }); } catch { /* silent */ }
  }
  store.on(['tab', 'state'], () => {
    const want = store.get('tab') === 'sim' && store.get('state')?.connection?.transport === 'sim';
    if (want && !timer) { poll(); timer = setInterval(poll, 1000); }
    if (!want && timer) { clearInterval(timer); timer = null; }
  }, { immediate: true });
}
