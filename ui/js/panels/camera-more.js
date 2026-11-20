// Camera tab, second part: automatic calibration with crosses, gondola marker (tracking), lens calibration.
import { h, fmt, fmtDur, lsGet, lsSet } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, toast, confirmDialog, gate } from '../ui.js';
import { numField, toggle } from '../widgets.js';
import { refreshCamera } from '../camera.js';
import { caps } from '../caps.js';

export function mountCameraMore(root, sect, p) {
  const cell = (v, l) => h('div', { class: 'stat' }, h('b', null, v), h('span', null, l));

  /* ---------- automatic calibration ---------- */
  const ag = { nx: 5, ny: 4, inset: 20, ...lsGet('wbot-autocal', {}) };
  const keepA = () => lsSet('wbot-autocal', ag);
  const anx = numField({ label: 'Columns', min: 2, max: 15, step: 1, value: ag.nx, onChange: (v) => { ag.nx = v; keepA(); } });
  const any = numField({ label: 'Rows', min: 2, max: 11, step: 1, value: ag.ny, onChange: (v) => { ag.ny = v; keepA(); } });
  const ains = numField({ label: 'Arm length', unit: 'mm', min: 5, max: 20, step: 1, value: ag.inset, onChange: (v) => { ag.inset = v; keepA(); } });
  const goBtn = h('button', { class: 'btn primary', type: 'button' }, icon('pen'), 'Draw crosses and measure');
  const measBtn = h('button', { class: 'btn', type: 'button' }, icon('camera'), 'Measure only');
  const stopBtn = h('button', { class: 'btn danger', type: 'button' }, icon('stop'), 'Cancel');
  const applyBtn = h('button', { class: 'btn', type: 'button' }, icon('save'), 'Apply correction');
  const bar = h('i'), prog = h('div', { class: 'progress' }, bar);
  const step = h('div', { class: 'muted', role: 'status', style: { fontSize: '14px' } });
  const out = h('div', { class: 'stack' });
  const gateNote = h('div', { class: 'gate-note', role: 'status' });

  async function startAuto(draw) {
    if (draw && ['serial', 'wifi'].includes(store.get('state')?.connection?.transport)) {
      const ok = await confirmDialog({ title: 'Draw and measure crosses?', text: 'The machine draws a grid of crosses, moves the gondola aside and the camera measures them. Board clear? Space bar = emergency stop.', ok: 'Start' });
      if (!ok) return;
    }
    store.set({ autocal: await api.autocal({ n_x: ag.nx, n_y: ag.ny, inset_mm: ag.inset, draw }) });
  }
  goBtn.addEventListener('click', () => run(goBtn, () => startAuto(true)));
  measBtn.addEventListener('click', () => run(measBtn, () => startAuto(false)));
  stopBtn.addEventListener('click', () => run(stopBtn, async () => { store.set({ autocal: await api.autocalCancel() }); }));
  applyBtn.addEventListener('click', () => run(applyBtn, async () => {
    const ac = store.get('autocal');
    const ok = await confirmDialog({ title: 'Apply correction?', text: 'The geometry measured by the camera is written to the configuration. Measure again afterwards to check.', ok: 'Apply' });
    if (!ok) return;
    const r = await api.calApply({ measurements: ac.measurements, free: null });
    if (r.config) store.set({ config: r.config });
    toast(r.applied ? 'Correction applied and saved.' : 'Nothing applied: no clear improvement.', r.applied ? 'ok' : 'info');
    store.set({ autocal: { ...ac, applied: !!r.applied } });
  }));

  /* ---------- gondola marker ---------- */
  const gm = () => store.get('camera')?.settings?.markers?.gondola ?? {};
  const putG = async (patch) => { store.set({ camera: await api.cameraPut({ markers: { gondola: patch } }) }); };
  const gOn = toggle({ label: 'Track the gondola marker', onChange: (v) => putG({ enabled: v }) });
  const gId = numField({ label: 'Marker no.', min: 0, max: 49, step: 1, onChange: (v) => v != null && putG({ id: Math.round(v) }) });
  const gSize = numField({ label: 'Size', unit: 'mm', min: 10, max: 200, step: 0.5, onChange: (v) => v && putG({ size_mm: v }) });
  const gH = numField({ label: 'In front of board', unit: 'mm', min: 0, max: 200, step: 0.5, onChange: (v) => v != null && putG({ height_mm: v }) });
  const learnBtn = h('button', { class: 'btn', type: 'button' }, 'Learn offset');
  const latBtn = h('button', { class: 'btn', type: 'button' }, 'Measure camera delay');
  const trackInfo = h('div', { class: 'stats' });
  learnBtn.addEventListener('click', () => run(learnBtn, async () => { const r = await api.learnOffset(); toast(`Offset marker → pen: ${fmt(r.offset_mm[0], 1)} | ${fmt(r.offset_mm[1], 1)} mm`, 'ok'); await refreshCamera(); }));
  latBtn.addEventListener('click', () => run(latBtn, async () => { const r = await api.latency(); toast(`Camera delay about ${r.latency_ms} ms.`, 'ok'); await refreshCamera(); }));

  /* ---------- lens ---------- */
  const lo = { cols: 9, rows: 6, square: 25, ...lsGet('wbot-lens', {}) };
  const keep = () => lsSet('wbot-lens', lo);
  const cols = numField({ label: 'Inner corners across', min: 3, max: 20, step: 1, value: lo.cols, onChange: (v) => { lo.cols = v; keep(); } });
  const rows = numField({ label: 'Inner corners down', min: 3, max: 20, step: 1, value: lo.rows, onChange: (v) => { lo.rows = v; keep(); } });
  const sq = numField({ label: 'Square', unit: 'mm', min: 3, max: 200, step: 0.1, value: lo.square, onChange: (v) => { lo.square = v; keep(); } });
  const capBtn = h('button', { class: 'btn', type: 'button' }, icon('camera'), 'Take shot');
  const calcBtn = h('button', { class: 'btn primary', type: 'button' }, 'Compute lens');
  const lensClr = h('button', { class: 'btn sm', type: 'button' }, icon('trash'), 'Reset');
  const lensInfo = h('div', { class: 'muted', role: 'status', style: { fontSize: '13px' } });
  capBtn.addEventListener('click', () => run(capBtn, async () => { const r = await api.lensCapture(lo.cols, lo.rows); toast(`Shot ${r.views} saved.`, 'ok'); await refreshCamera(); }));
  calcBtn.addEventListener('click', () => run(calcBtn, async () => { const r = await api.lensCompute(lo.square); toast(`Lens calibrated (residual ${fmt(r.lens.rms_px, 2)} px).`, 'ok'); await refreshCamera(); }));
  lensClr.addEventListener('click', () => run(lensClr, async () => { store.set({ camera: await api.lensClear() }); }));

  function paint() {
    const cam = store.get('camera'), s = cam?.settings;
    const c = caps(store.get('state'), store.get('job')), ac = store.get('autocal');
    const busy = ac && ['starting', 'drawing', 'measuring'].includes(ac.state);
    const why = !cam?.source ? 'Choose a camera first.' : !cam?.calibrated ? 'Align the camera to the board first.' : c.abs || (busy ? 'Running.' : '');
    gate(goBtn, why); gate(measBtn, why); gateNote.textContent = why;
    stopBtn.hidden = !busy;
    bar.style.transform = `scaleX(${Math.min(1, ac?.progress ?? 0)})`;
    const job = store.get('job');
    step.textContent = !ac || ac.state === 'idle' ? '' : `${ac.step ?? ''}${ac.state === 'drawing' && job?.eta_s != null ? `, about ${fmtDur(job.eta_s)} left` : ''}`;
    out.replaceChildren();
    if (ac?.points) {
      const found = ac.points.filter((q) => q.measured).length;
      const hidden = ac.points.filter((q) => q.status === 'hidden').length;
      out.append(h('div', { class: 'stats' }, cell(`${found} / ${ac.points.length}`, 'Crosses found'), cell(String(hidden), 'Hidden')));
    }
    for (const w of ac?.warnings ?? []) out.append(h('div', { class: 'note warn' }, w));
    if (ac?.state === 'error') out.append(h('div', { class: 'note danger' }, ac.error));
    const r = ac?.result;
    if (r) {
      out.append(h('div', { class: 'stats' }, cell(`${fmt(r.rms_before, 2)} → ${fmt(r.rms_after, 2)} mm`, 'RMS before → after'),
        cell(`${fmt(r.max_before, 2)} → ${fmt(r.max_after, 2)} mm`, 'Largest error'), cell(r.improved ? 'yes' : 'no', 'Clear improvement')));
      for (const w of r.warnings ?? []) out.append(h('div', { class: 'note warn' }, w));
      const gp = r.patch?.geometry;
      if (gp) out.append(h('p', { class: 'mono', style: { fontSize: '13px', whiteSpace: 'pre-line' } }, Object.entries(gp).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.map((x) => fmt(x, 2)).join(' | ') : fmt(v, 5)}`).join('\n')));
    }
    const can = r?.improved && !ac.applied && Object.keys(r.patch ?? {}).length;
    gate(applyBtn, can ? '' : ac?.applied ? 'Already applied.' : 'No correction to apply.');
    applyBtn.classList.toggle('primary', !!can);
    applyBtn.hidden = !r;

    const g = gm();
    gOn.set(!!g.enabled); gId.set(g.id); gSize.set(g.size_mm); gH.set(g.height_mm);
    const tr = store.get('camTrack'), fresh = tr && Date.now() / 1000 - tr.t < 3;
    trackInfo.replaceChildren(cell(!cam?.calibrated ? '–' : fresh && tr.seen ? 'yes' : 'no', 'Marker seen'),
      cell(fresh && tr.moving ? 'moving' : fresh && tr.dev_mm != null ? `${fmt(tr.dev_mm, 1)} mm` : '–', 'Camera vs. software'),
      cell(g.offset_mm ? `${fmt(g.offset_mm[0], 1)} | ${fmt(g.offset_mm[1], 1)}` : '–', 'Offset marker → pen (mm)'),
      cell(`${fmt(s?.latency_ms ?? 0, 0)} ms`, 'Camera delay'));
    const tw = !cam?.calibrated ? 'Align the board first.' : !g.enabled ? 'Tracking is off.' : '';
    gate(learnBtn, tw); gate(latBtn, tw);

    const lens = s?.lens;
    lensInfo.textContent = `${cam?.lens_views ?? 0} shots. ` + (lens ? `Lens: focal ${fmt(lens.K[0][0], 0)} px, k1 ${fmt(lens.dist[0], 3)}, residual ${fmt(lens.rms_px, 2)} px.` : 'No lens model (focal length estimated, distortion not corrected).');
    lensClr.hidden = !lens && !(cam?.lens_views);
  }

  root.append(
    sect('5 Precise calibration',
      p('Draws a grid of crosses, the camera measures each one and the anchors and belt scale are fitted. Nothing changes until you apply it.'),
      h('div', { class: 'grid3' }, anx.el, any.el, ains.el), h('div', { class: 'grid2' }, goBtn, measBtn), gateNote, prog, step, stopBtn, out, applyBtn),
    sect('6 Gondola marker',
      p('An ArUco marker on the gondola shows where the pen really is and warns when it is off (skipped belt tooth, wrong reference).'),
      gOn.el, h('div', { class: 'grid3' }, gId.el, gSize.el, gH.el), trackInfo, h('div', { class: 'grid2' }, learnBtn, latBtn)),
    sect('7 Lens (optional)',
      p('Corrects wide-angle distortion: hold a printed chessboard in front of the camera from 10-15 angles, take a shot each time, then compute. Align the board again afterwards.'),
      h('div', { class: 'grid3' }, cols.el, rows.el, sq.el), h('div', { class: 'grid2' }, capBtn, calcBtn), lensInfo, h('div', { class: 'row' }, lensClr)),
  );
  store.on(['camera', 'camTrack', 'autocal', 'state', 'job'], paint, { immediate: true });
  api.autocalState({ quiet: true }).then((s) => { if (s?.state && s.state !== 'idle') store.set({ autocal: s }); }).catch(() => {});
}
