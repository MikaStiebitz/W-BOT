// Tab "Calibrate": draw crosses, measure them with a ruler, fit the geometry, apply the correction.
import { h, fmt, fmtDur, lsGet, lsSet } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, gate, toast, confirmDialog } from '../ui.js';
import { numField } from '../widgets.js';
import { caps } from '../caps.js';
import { mountTape } from './tape.js';

const FREE_LABEL = { anchor_left: 'Left anchor', anchor_right: 'Right anchor', scale: 'Belt scale left and right', scale_left: 'Belt scale left',
  scale_right: 'Belt scale right', pen_offset: 'Pen offset', radius: 'Pulley radius', reference: 'Position reference' };
const num = (s) => { const t = String(s ?? '').trim().replace(',', '.'); if (!t) return null; const v = Number(t); return Number.isFinite(v) ? v : null; };
const f2 = (v) => fmt(v, 2);

export function mountCalibration(root) {
  const grid = { nx: 5, ny: 4, inset: 20, ...lsGet('wbot-calib-grid', {}) };
  let targets = [], pattern = null, rows = [], result = null, hot = null, applied = false;
  const sig = () => `${grid.nx}x${grid.ny}@${grid.inset}`;
  const saved = () => lsGet('wbot-calib', {});
  const sect = (title, ...kids) => h('div', { class: 'sect' }, h('div', { class: 'sect-h' }, h('h2', null, title)), ...kids);
  const note = () => h('div', { class: 'gate-note', role: 'status' });

  /* ---------- 1. Draw ---------- */
  const nx = numField({ label: 'Columns', min: 2, max: 15, step: 1, value: grid.nx, onChange: (v) => setGrid('nx', v) });
  const ny = numField({ label: 'Rows', min: 2, max: 11, step: 1, value: grid.ny, onChange: (v) => setGrid('ny', v) });
  const ins = numField({ label: 'Edge inset and arm length', unit: 'mm', min: 2, max: 20, step: 1, value: grid.inset, onChange: (v) => setGrid('inset', v) });
  const drawBtn = h('button', { class: 'btn primary', type: 'button' }, icon('pen'), 'Draw crosses');
  const dryBtn = h('button', { class: 'btn', type: 'button' }, icon('play'), 'Dry run');
  const drawNote = note();
  const bar = h('i'), progress = h('div', { class: 'progress' }, bar);
  const jobLine = h('div', { class: 'muted mono', style: { fontSize: '13px' } });
  const noPattern = h('div', { class: 'note warn', hidden: true }, 'The server cannot draw the crosses with these settings. The edge inset (also the arm length) must be between 2 and 20 mm.');

  function setGrid(k, v) {
    if (v == null) return;
    grid[k] = k === 'inset' ? v : Math.round(v);
    lsSet('wbot-calib-grid', grid);
    loadTargets();
  }

  async function loadTargets() {
    try {
      const r = await api.calTargets(grid.nx, grid.ny, grid.inset);
      targets = r.targets ?? []; pattern = r.pattern ?? null; result = null; applied = false;
      renderTable(); publish(); paintAll();
    } catch { /* toast already shown by api.js */ }
  }

  async function start(dry) {
    if (!pattern) return;
    if (!dry && ['serial', 'wifi'].includes(store.get('state')?.connection?.transport)) {
      const ok = await confirmDialog({ title: 'Draw crosses?', text: 'The real machine will start moving and draw the crosses. Gondola and board clear? Press E-stop (Space) to stop at any time.', ok: 'Start drawing' });
      if (!ok) return;
    }
    const job = await api.startJob({ pattern, dry_run: dry });
    store.set({ job });
    toast(dry ? 'Dry run started.' : 'Drawing crosses.', 'ok');
  }
  drawBtn.addEventListener('click', () => run(drawBtn, () => start(false)));
  dryBtn.addEventListener('click', () => run(dryBtn, () => start(true)));

  /* ---------- 2. Measurements ---------- */
  const table = h('div', { class: 'cal-table' });
  const count = h('div', { class: 'muted', role: 'status' });
  const clearBtn = h('button', { class: 'btn sm', type: 'button', onClick: () => {
    rows.forEach((r) => { r.xi.value = ''; r.yi.value = ''; }); const all = saved(); delete all[sig()]; lsSet('wbot-calib', all); changed();
  } }, icon('trash'), 'Clear measurements');

  const camBtn = h('button', { class: 'btn sm', type: 'button', title: 'The camera measures the drawn crosses (Camera tab: calibrate it first). Move the gondola out of the way beforehand.' }, icon('camera'), 'Measure with camera');
  camBtn.addEventListener('click', () => run(camBtn, async () => {
    const r = await api.cameraMeasure({ targets, arm_mm: grid.inset });
    let n = 0;
    r.points.forEach((q, i) => { if (q.measured && rows[i]) { rows[i].xi.value = fmt(q.measured[0], 2); rows[i].yi.value = fmt(q.measured[1], 2); n++; } });
    const hidden = r.points.filter((q) => ['verdeckt', 'hidden', 'occluded'].includes(q.status)).length;
    toast(`Camera: ${n} of ${r.total} crosses measured${hidden ? `, ${hidden} hidden by the gondola (move the gondola away and retry)` : ''}.`, n ? 'ok' : 'error');
    for (const w of r.warnings ?? []) toast(w, 'info');
    changed();
  }));

  function renderTable() {
    const sv = saved()[sig()] ?? [];
    rows = targets.map((t, i) => {
      const xi = h('input', { type: 'text', inputmode: 'decimal', autocomplete: 'off', 'aria-label': `Point ${i + 1} measured X`, value: sv[i]?.[0] ?? '' });
      const yi = h('input', { type: 'text', inputmode: 'decimal', autocomplete: 'off', 'aria-label': `Point ${i + 1} measured Y`, value: sv[i]?.[1] ?? '' });
      const res = h('span', { class: 'res' });
      const tr = h('div', { class: 'cal-row', onMouseenter: () => setHot(i), onMouseleave: () => setHot(null), onFocusin: () => setHot(i), onFocusout: () => setHot(null) },
        h('span', { class: 'nr' }, i + 1), h('span', { class: 'soll' }, `${fmt(t[0], 0)} | ${fmt(t[1], 0)}`), xi, yi, res);
      xi.addEventListener('input', changed); yi.addEventListener('input', changed);
      return { tr, xi, yi, res };
    });
    table.replaceChildren(h('div', { class: 'cal-head' }, h('span', null, 'No.'), h('span', null, 'Target (mm)'), h('span', null, 'Measured X'), h('span', null, 'Measured Y'), h('span', { style: { textAlign: 'right' } }, 'Residual')),
      ...rows.map((r) => r.tr));
  }
  function setHot(i) { hot = i; publish(); }

  function collect() {
    const meas = [], idx = [];
    rows.forEach((r, i) => {
      const x = num(r.xi.value), y = num(r.yi.value);
      if (x != null && y != null) { meas.push({ target: targets[i], measured: [x, y] }); idx.push(i); }
    });
    return { meas, idx };
  }
  function changed() {
    const all = saved();
    all[sig()] = rows.map((r) => [r.xi.value, r.yi.value]);
    lsSet('wbot-calib', all);
    if (result) result.stale = true;
    publish(); paintAll();
  }

  function publish() {
    const { idx } = collect();
    const measured = targets.map(() => null);
    rows.forEach((r, i) => { const x = num(r.xi.value), y = num(r.yi.value); if (x != null && y != null) measured[i] = [x, y]; });
    store.set({ calib: { targets, measured, hot, result, rowIdx: idx } });
  }

  /* ---------- 3. Fit ---------- */
  const fitBtn = h('button', { class: 'btn primary', type: 'button' }, 'Fit');
  const fitNote = note();
  const out = h('div', { class: 'stack' });
  fitBtn.addEventListener('click', () => run(fitBtn, async () => {
    const { meas, idx } = collect();
    if (meas.length < 3) { toast('Measure at least 3 points (X and Y).', 'error'); return; }
    result = await api.calFit({ measurements: meas, free: null });
    result.rowIdx = idx; result.stale = false; applied = false;
    publish(); paintAll();
  }));

  const applyBtn = h('button', { class: 'btn', type: 'button' }, icon('save'), 'Apply correction');
  applyBtn.addEventListener('click', () => run(applyBtn, async () => {
    const ok = await confirmDialog({ title: 'Apply correction?', text: 'The new geometry values will be written to the configuration and saved. All future drawings will be corrected with them. The previous values are shown in the comparison above.', ok: 'Apply' });
    if (!ok) return;
    const { meas } = collect();
    const r = await api.calApply({ measurements: meas, free: null });
    if (r.config) store.set({ config: r.config });
    if (r.applied) { applied = true; toast('Correction applied and saved.', 'ok'); }
    else toast('Nothing applied: the fit gives no clear improvement.', 'info');
    if (r.result) { r.result.rowIdx = result?.rowIdx ?? []; r.result.stale = !!r.applied; result = r.result; }
    publish(); paintAll();
  }));
  const applyNote = note();
  const afterHint = h('div', { class: 'note ok', hidden: true }, 'Applied. To check: draw the crosses again and re-measure. The residuals should now be much smaller. The old measurements no longer apply: use “Clear measurements”.');

  function diffRows(patch, cfg) {
    const g = patch?.geometry ?? {}, o = cfg?.geometry ?? {}, d = [];
    const xy = (v) => `${fmt(v[0], 2)} | ${fmt(v[1], 2)}`;
    const pair = (label, a, b, unit = 'mm') => d.push(h('dt', null, label), h('dd', null, `${xy(a)} → `, h('b', null, xy(b)), ` ${unit}`));
    if (g.anchor_left) pair('Left anchor', o.anchor_left, g.anchor_left);
    if (g.anchor_right) pair('Right anchor', o.anchor_right, g.anchor_right);
    const sc = (label, a, b) => d.push(h('dt', null, label), h('dd', null, `${fmt(a, 5)} → `, h('b', null, fmt(b, 5)), ` (${b >= a ? '+' : ''}${fmt((b / a - 1) * 100, 3)} %)`));
    if (g.belt_scale_left != null) sc('Scale left', o.belt_scale_left, g.belt_scale_left);
    if (g.belt_scale_right != null) sc('Scale right', o.belt_scale_right, g.belt_scale_right);
    if (g.pen_offset_mm) pair('Pen offset', o.pen_offset_mm, g.pen_offset_mm);
    if (g.pulley_pitch_radius_mm != null) d.push(h('dt', null, 'Pulley radius'), h('dd', null, `${fmt(o.pulley_pitch_radius_mm, 3)} → `, h('b', null, fmt(g.pulley_pitch_radius_mm, 3)), ' mm'));
    return d;
  }

  function paintResult() {
    out.replaceChildren();
    afterHint.hidden = !applied;
    if (!result) return;
    const r = result, cfg = store.get('config');
    const cell = (v, l) => h('div', { class: 'stat' }, h('b', null, v), h('span', null, l));
    if (r.stale) out.append(h('div', { class: 'note warn' }, applied ? 'This fit has been applied.' : 'The measurements changed. Fit again.'));
    out.append(h('div', { class: 'stats' }, cell(`${f2(r.rms_before)} → ${f2(r.rms_after)} mm`, 'RMS before → after'),
      cell(`${f2(r.max_before)} → ${f2(r.max_after)} mm`, 'Max error before → after'), cell(String(r.n), 'Points fitted'),
      cell(r.improved ? 'yes' : 'no', 'Clear improvement')));
    for (const w of r.warnings ?? []) out.append(h('div', { class: 'note warn' }, w));
    const names = (r.free ?? []).map((k) => FREE_LABEL[k] ?? k);
    out.append(h('p', { class: 'muted', style: { fontSize: '14px' } }, `Adjusted: ${names.length ? names.join(', ') : 'nothing'}.`));
    const d = diffRows(r.patch, cfg);
    const [cx, cy] = r.reference_correction ?? [0, 0];
    if (cx || cy) d.push(h('dt', null, 'Position correction'), h('dd', null, h('b', null, `${fmt(cx, 2)} | ${fmt(cy, 2)}`), ' mm'));
    if (d.length) {
      out.append(h('h3', null, 'Proposed changes'), h('dl', { class: 'diff' }, ...d),
        h('p', { class: 'field-hint' }, 'The firmware anchors on the machine stay unchanged. Only the host geometry is corrected.'));
    } else out.append(h('div', { class: 'note danger' }, 'No correction suggested. The configuration stays as it is.'));
    if (r.outliers?.length) out.append(h('div', { class: 'note danger' }, `Outlier points (red in the table): ${r.outliers.map((k) => (r.rowIdx?.[k] ?? k) + 1).join(', ')}. Please re-measure them.`));
  }

  function paintRows() {
    const r = result && !result.stale ? result : null;
    rows.forEach((row, i) => {
      const k = r?.rowIdx?.indexOf(i) ?? -1;
      row.res.textContent = k >= 0 ? f2(r.residuals[k]) : '';
      row.tr.classList.toggle('out', k >= 0 && !!r.outliers?.includes(k));
    });
  }

  function paintAll() {
    const c = caps(store.get('state'), store.get('job')), { meas } = collect();
    const noRef = pattern ? c.abs : 'Cannot be drawn with these settings.';
    gate(drawBtn, noRef); gate(dryBtn, noRef); drawNote.textContent = noRef;
    noPattern.hidden = !!pattern || !targets.length;
    count.textContent = `${meas.length} of ${targets.length} points measured. Empty rows are skipped. At least 3, ideally 12 to 20.`;
    const fitWhy = !c.connected ? 'Not connected. The fit needs the machine position reference.' : !c.referenced ? 'No position reference set.' : meas.length < 3 ? 'Enter at least 3 points (X and Y).' : '';
    gate(fitBtn, fitWhy); fitNote.textContent = fitWhy;
    const cam = store.get('camera');
    gate(camBtn, !cam?.calibrated ? 'First choose and calibrate a camera in the Camera tab.' : '');
    const hasFix = !!result && !result.stale && !applied && (Object.keys(result.patch ?? {}).length || result.reference_correction?.some((v) => v));
    const applyWhy = !result ? 'Fit first.' : result.stale ? 'Measurements changed or already applied. Fit again first.' : !hasFix ? 'No correction suggested.' : '';
    gate(applyBtn, applyWhy); applyBtn.classList.toggle('primary', !applyWhy); applyNote.textContent = applyWhy;
    paintResult(); paintRows();
    const job = store.get('job');
    if (job && ['planning', 'running', 'paused'].includes(job.state) && /Messkreuz|crosshair|calibration|cross/i.test(job.name ?? '')) {
      bar.style.transform = `scaleX(${Math.min(1, job.progress ?? 0)})`; jobLine.textContent = `${fmt((job.progress ?? 0) * 100, 0)} %, about ${fmtDur(job.eta_s)} left`;
    } else { bar.style.transform = 'scaleX(0)'; jobLine.textContent = ''; }
  }

  /* ---------- Layout ---------- */
  root.append(
    sect('1 Draw crosses',
      h('p', { class: 'muted', style: { fontSize: '14px' } }, 'The machine draws a grid of small crosses. Then you measure their centers with a ruler or tape. First set the position reference and move the gondola into the drawing area, e.g. with “Park” (Move tab).'),
      h('div', { class: 'grid2' }, nx.el, ny.el), h('div', { class: 'grid2' }, ins.el, h('span')), noPattern,
      h('div', { class: 'grid2' }, drawBtn, dryBtn), drawNote, progress, jobLine),
    sect('2 Enter measurements',
      h('p', { class: 'muted', style: { fontSize: '14px' } }, 'Measure each cross center from the bottom-left corner of the drawing area: X to the right, Y upward, in mm. The numbered markers on the board view show which row belongs to which cross. Comma or dot is accepted.'),
      table, count, h('div', { class: 'row' }, clearBtn, camBtn)),
    sect('3 Fit', fitBtn, fitNote, out),
    sect('4 Apply', applyBtn, applyNote, afterHint));
  mountTape(root, sect);

  store.on(['state', 'job', 'config', 'camera'], paintAll);
  loadTargets();
}
