// Calibration without a camera and without a coordinate system: draw labelled crosses, measure only distances with a tape.
import { h, fmt, lsGet, lsSet } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, gate, toast, confirmDialog } from '../ui.js';
import { caps } from '../caps.js';

const num = (s) => { const t = String(s ?? '').trim().replace(',', '.'); if (!t) return null; const v = Number(t); return Number.isFinite(v) ? v : null; };

export function mountTape(root, sect) {
  let plan = null, rows = [], result = null, applied = false;
  const saved = () => lsGet('wbot-tape', {});
  const drawBtn = h('button', { class: 'btn primary', type: 'button' }, icon('pen'), 'Draw tape points');
  const list = h('div', { class: 'stack', style: { gap: '6px' } });
  const fitBtn = h('button', { class: 'btn primary', type: 'button' }, 'Evaluate distances');
  const applyBtn = h('button', { class: 'btn', type: 'button' }, icon('save'), 'Apply correction');
  const out = h('div', { class: 'stack' });
  const why = h('div', { class: 'gate-note', role: 'status' });

  async function load() {
    try { plan = await api.tapePlan(3, 3, 12); } catch { return; }
    const sv = saved();
    rows = plan.pairs.map((q, i) => {
      const inp = h('input', { type: 'text', inputmode: 'decimal', autocomplete: 'off', 'aria-label': `Distance ${q.a} to ${q.b} in mm`, value: sv[`${q.a}${q.b}`] ?? '' });
      const res = h('span', { class: 'res mono', style: { textAlign: 'right' } });
      const row = h('div', { class: 'tape-row' }, h('b', null, `${q.a} – ${q.b}`), h('span', { class: 'muted' }, `target ${fmt(q.nominal_mm, 1)} mm`), inp, res);
      inp.addEventListener('input', () => { const all = saved(); all[`${q.a}${q.b}`] = inp.value; lsSet('wbot-tape', all); if (result) result.stale = true; paint(); });
      return { q, inp, res, row, i };
    });
    list.replaceChildren(...rows.map((r) => r.row));
    paint();
  }

  function collect() {
    const by = Object.fromEntries(plan.points.map((pt) => [pt.name, pt.xy]));
    return rows.filter((r) => num(r.inp.value) != null).map((r) => ({ a: by[r.q.a], b: by[r.q.b], measured_mm: num(r.inp.value), _i: r.i }));
  }

  drawBtn.addEventListener('click', () => run(drawBtn, async () => {
    if (['serial', 'wifi'].includes(store.get('state')?.connection?.transport)) {
      const ok = await confirmDialog({ title: 'Draw tape points?', text: 'The machine draws nine labelled crosses A to I across the board. Board clear?', ok: 'Draw' });
      if (!ok) return;
    }
    store.set({ job: await api.startJob({ pattern: plan.pattern, name: 'Tape points' }) });
    toast('Drawing the tape points.', 'ok');
  }));
  fitBtn.addEventListener('click', () => run(fitBtn, async () => {
    const pairs = collect();
    result = await api.tapeFit({ pairs: pairs.map(({ _i, ...x }) => x) });
    result.idx = pairs.map((x) => x._i); result.stale = false; applied = false; paint();
  }));
  applyBtn.addEventListener('click', () => run(applyBtn, async () => {
    const ok = await confirmDialog({ title: 'Apply correction?', text: 'Belt scale and anchors are corrected from the distances and saved.', ok: 'Apply' });
    if (!ok) return;
    const r = await api.tapeApply({ pairs: collect().map(({ _i, ...x }) => x) });
    if (r.config) store.set({ config: r.config });
    applied = !!r.applied;
    toast(applied ? 'Correction applied. Draw and measure again to check.' : 'Nothing applied: no clear improvement.', applied ? 'ok' : 'info');
    paint();
  }));

  function paint() {
    if (!plan) return;
    const c = caps(store.get('state'), store.get('job')), n = collect().length;
    gate(drawBtn, c.abs);
    const fw = !c.connected ? 'Not connected.' : !c.referenced ? 'No reference set.' : n < 3 ? 'Enter at least 3 distances (all is better).' : '';
    gate(fitBtn, fw); why.textContent = fw;
    const r = result && !result.stale ? result : null;
    rows.forEach((row) => {
      const k = r?.idx?.indexOf(row.i) ?? -1;
      row.res.textContent = k >= 0 ? fmt(r.residuals[k], 1) : '';
      row.row.classList.toggle('out', k >= 0 && r.suspicious?.includes(k + 1));
    });
    out.replaceChildren();
    if (result) {
      const cell = (v, l) => h('div', { class: 'stat' }, h('b', null, v), h('span', null, l));
      if (result.stale) out.append(h('div', { class: 'note warn' }, 'Values changed: evaluate again.'));
      out.append(h('div', { class: 'stats' }, cell(`${fmt(result.rms_before, 2)} → ${fmt(result.rms_after, 2)} mm`, 'Distance error RMS'),
        cell(String(result.n), 'Distances'), cell(result.improved ? 'yes' : 'no', 'Clear improvement')));
      for (const w of result.warnings ?? []) out.append(h('div', { class: 'note warn' }, w));
      const g = result.patch?.geometry;
      if (g) out.append(h('p', { class: 'mono', style: { fontSize: '13px', whiteSpace: 'pre-line' } }, Object.entries(g).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.map((x) => fmt(x, 2)).join(' | ') : fmt(v, 5)}`).join('\n')));
    }
    const can = r?.improved && !applied;
    gate(applyBtn, can ? '' : 'No correction to apply.'); applyBtn.classList.toggle('primary', !!can);
  }

  root.append(sect('Tape-measure calibration (no camera)',
    h('p', { class: 'muted', style: { fontSize: '14px' } }, 'The machine draws crosses A to I; measure only the centre-to-centre distances with a tape (taut, to ±1 mm).'),
    drawBtn, list, fitBtn, why, out, applyBtn));
  store.on(['state', 'job'], paint);
  load();
}
