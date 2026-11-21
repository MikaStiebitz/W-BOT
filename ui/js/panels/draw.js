// Draw tab: a small canvas editor. Add text, formulas, plots, symbols and images, place them on the board,
// see exactly what will be plotted, press Draw.
import { h, fmt, fmtDur, lsGet, lsSet } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, gate, notice, clearNotice, confirmDialog, choiceDialog } from '../ui.js';
import { slider } from '../widgets.js';
import { caps } from '../caps.js';
import { isLiveJob } from '../live.js';
import { drawingBounds } from '../drawing.js';
import * as M from '../editor/model.js';
import { mountLibrary } from '../editor/library.js';
import { mountProps } from '../editor/props.js';
import { TRACE_DEFAULT } from '../editor/props-misc.js';
import { setSymbolsOpener } from '../editor/bridge.js';

const JOB_LABEL = { planning: 'Planning …', running: 'Drawing', paused: 'Paused', done: 'Done', cancelled: 'Cancelled', error: 'Error' };
const IMAGE_RE = /\.(png|jpe?g|webp|bmp|gif|svg)$/i;

let openFile = () => {};                         // set by mountDraw: used by the window-wide drop / paste handlers
export function importFile(file) { openFile(file); }

const label = (e) => {
  if (e.type === 'text') return (e.text || '').split('\n')[0].slice(0, 28) || 'Text';
  if (e.type === 'math') return (e.latex || '').slice(0, 28) || 'Formula';
  if (e.type === 'plot') return 'Plot ' + (e.curves ?? []).map((c) => c.expr).filter(Boolean).join(', ').slice(0, 22);
  if (e.type === 'symbol') return e.kind === 'emoji' ? e.sym : (e.sym || 'Symbol').replace(/^.*:/, '');
  if (e.type === 'image') return e.name || 'Image';
  return e.pattern || 'Sample';
};
const ICON = { text: 'text', math: 'formula', plot: 'plot', symbol: 'symbol', image: 'image', pattern: 'layers' };

export function mountDraw(root) {
  const sect = (title, ...kids) => h('div', { class: 'sect' }, title ? h('div', { class: 'sect-h' }, h('h2', null, title)) : null, ...kids);
  const note = () => h('div', { class: 'gate-note', role: 'status' });
  let speed = lsGet('wbot-speed', 1), lastJobState = null;
  M.initModel();

  /* ---------- 1. Add ---------- */
  const busyChip = h('span', { class: 'chip', hidden: true, role: 'status' }, 'Tracing the image …');
  const addBox = h('div', { class: 'stack' });
  const lib = mountLibrary(addBox, { onFile: (f) => loadFile(f) });
  setSymbolsOpener((id) => lib.openSymbols(id));
  const hint = h('p', { class: 'muted add-hint' }, 'Click to add, or drag onto the board. Drop an image anywhere to trace it.');

  /* ---------- 2. Elements ---------- */
  const list = h('div', { class: 'el-list', role: 'listbox', 'aria-label': 'Elements on the board' });
  const listHead = h('div', { class: 'sect-h' }, h('h2', null, 'On the board'),
    h('div', { class: 'row-s' },
      h('button', { class: 'btn sm ghost icon-only', type: 'button', title: 'Undo (Ctrl+Z)', 'aria-label': 'Undo', onClick: () => M.undo() }, icon('undo')),
      h('button', { class: 'btn sm ghost icon-only', type: 'button', title: 'Redo (Ctrl+Y)', 'aria-label': 'Redo', onClick: () => M.redo() }, icon('redo')),
      h('button', { class: 'btn sm ghost', type: 'button', title: 'Remove everything from the board', onClick: async () => {
        if (!M.elements().length) return;
        if (await confirmDialog({ title: 'Clear the board?', text: 'All elements are removed. You can undo this with Ctrl+Z.', ok: 'Clear' })) M.clearAll();
      } }, 'Clear')));
  function paintList() {
    const els = M.elements(), sel = store.get('selection');
    listHead.hidden = list.hidden = !els.length;
    list.replaceChildren(...[...els].reverse().map((e) => {
      const err = M.info(e.id)?.error;
      return h('div', { class: 'el-row' + (e.id === sel ? ' sel' : '') + (err ? ' bad' : ''), role: 'option', tabindex: '0', 'aria-selected': String(e.id === sel), title: err || label(e),
        onClick: () => M.select(e.id), onKeydown: (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); M.select(e.id); } } },
      icon(ICON[e.type] ?? 'layers'), h('span', { class: 'el-name' }, label(e)), err ? h('span', { class: 'chip danger' }, 'Error') : null,
      h('button', { class: 'btn sm ghost icon-only', type: 'button', title: 'Delete', 'aria-label': 'Delete', onClick: (ev) => { ev.stopPropagation(); M.remove(e.id); } }, icon('x')));
    }));
  }

  /* ---------- 3. Properties ---------- */
  const propsBox = h('div', { class: 'props' });
  mountProps(propsBox);

  /* ---------- 4. Samples ---------- */
  const samples = h('details', { class: 'more' }, h('summary', null, 'Test patterns and samples'), h('div', { class: 'pat-list more-body' }));
  function paintSamples() {
    const ps = store.get('patterns'), box = samples.querySelector('.pat-list');
    samples.hidden = !ps.length;
    box.replaceChildren(...ps.map((p) => h('button', { class: 'btn sm', type: 'button', title: p.description,
      onClick: () => M.add('pattern', { pattern: p.id, params: Object.fromEntries(p.params.map((q) => [q.id, q.default])), size: 400 }) }, p.name)));
  }

  /* ---------- Preview summary ---------- */
  const summary = h('div', { class: 'summary', hidden: true });
  const warns = h('div', { class: 'stack' });
  function paintPreview() {
    const pv = store.get('preview');
    summary.replaceChildren(); warns.replaceChildren();
    summary.hidden = !pv || !(pv.stats?.strokes || pv.pending);
    if (!pv) return;
    const st = pv.stats ?? {};
    const chip = (ic, v, title) => h('span', { class: 'sum', title }, ic ? icon(ic) : null, v);
    const e = pv.estimate, learned = e && e.samples >= 3 && Math.abs(e.factor - 1) > 0.02;
    const basis = !e ? '' : learned ? `Model estimate corrected by ${e.samples} finished drawings (real time was ×${e.factor} of the model).`
      : 'Model estimate from path length, feed rates, acceleration and pen moves. It gets better with every finished drawing.';
    if (pv.pending) summary.append(chip(null, 'Calculating the time …'));
    else if (st.strokes) summary.append(chip('play', `≈ ${fmtDur(pv.estimated_s)}`, basis), chip(null, `${fmt(st.strokes, 0)} paths`), chip(null, `${fmt((st.draw_mm ?? 0) / 1000, 1)} m of line`));
    if (st.color_changes) summary.append(chip('refresh', `${fmt(st.color_changes, 0)} pen changes`));
    if (pv.elements?.some((e) => e.clipped)) warns.append(h('div', { class: 'note warn' }, 'Something reaches past the drawing area (dashed line). The red parts are not drawn.'));
    const seen = new Set();
    for (const e of M.elements()) for (const w of e.warnings ?? []) if (!seen.has(w)) { seen.add(w); warns.append(h('div', { class: 'note warn' }, w)); }
    for (const w of pv.warnings ?? []) if (!seen.has(w)) { seen.add(w); warns.append(h('div', { class: 'note warn' }, w)); }
  }

  /* ---------- Loading images ---------- */
  async function loadFile(file) {
    if (!file) return;
    const okType = file.type.startsWith('image/') || IMAGE_RE.test(file.name || '');
    if (!okType) { notice('upload', `“${file.name}” is not an image. Use PNG, JPEG, WebP, BMP, GIF or SVG.`, 'error'); return; }
    if (file.size > 30e6) { notice('upload', `“${file.name}” is ${(file.size / 1e6).toFixed(0)} MB. The maximum is 30 MB.`, 'error'); return; }
    clearNotice('upload');
    store.set({ tab: 'draw' });
    busyChip.hidden = false;
    const trace = { ...TRACE_DEFAULT, ...lsGet('wbot-trace', {}) };
    try {
      const r = await api.importFile(file, { mode: trace.mode, detail: trace.detail, invert: trace.invert, use_pen_colors: trace.colors, shade: trace.shade, pen: store.get('selectedPen') ?? 0 });
      const b = drawingBounds(r.drawing);
      const size = b ? Math.max(b[2] - b[0], b[3] - b[1]) * 0.7 : 300;
      const el = M.add('image', { drawing: r.drawing, info: r.info ?? null, warnings: r.warnings ?? [], name: file.name || 'image', trace, size });
      M.setFile(el.id, file);
    } catch { /* notice came from api.js */ } finally { busyChip.hidden = true; }
  }
  openFile = loadFile;

  /* ---------- Run ---------- */
  const jobState = h('span', { class: 'chip' });
  const bar = h('i'), progress = h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100' }, bar);
  const jobInfo = h('div', { class: 'muted mono', style: { fontSize: '13px' } });
  const jobMsg = h('div', { class: 'muted' });
  const drawBtn = h('button', { class: 'btn primary big', type: 'button' }, icon('pen'), 'Draw');
  const dryBtn = h('button', { class: 'btn sm', type: 'button', title: 'Moves everything with the pen up' }, icon('play'), 'Test run (pen up)');
  const pauseBtn = h('button', { class: 'btn', type: 'button' }, icon('pause'), 'Pause');
  const resumeBtn = h('button', { class: 'btn', type: 'button' }, icon('play'), 'Resume');
  const cancelBtn = h('button', { class: 'btn danger', type: 'button' }, icon('stop'), 'Cancel');
  const startNote = note();
  const speedSl = slider({ min: 25, max: 150, step: 5, value: Math.round(speed * 100), label: 'Speed', onInput: (v) => { speed = v / 100; speedLbl.textContent = `${v} %`; lsSet('wbot-speed', speed); } });
  const speedLbl = h('span', { class: 'mono-val', style: { minWidth: '48px', textAlign: 'right' } }, `${Math.round(speed * 100)} %`);
  const speedRow = h('details', { class: 'more' }, h('summary', null, 'Speed'), h('div', { class: 'row-s more-body' }, speedSl.input, speedLbl));

  /** With a calibrated camera: is something already written where this will be drawn? true = go on. */
  async function checkWriting() {
    const pv = store.get('preview');
    if (!store.get('camera')?.calibrated || !pv?.drawing) return true;
    let r;
    try { r = await api.overlap(pv.drawing, { quiet: true }); } catch { return true; }          // no camera view: just draw
    if ((r?.overlap_pct ?? 0) < 0.5) return true;
    const sug = r.suggestion, pct = r.overlap_pct < 10 ? r.overlap_pct.toFixed(1) : Math.round(r.overlap_pct);
    const choice = await choiceDialog({ title: 'Something is already written here',
      text: `This drawing runs over writing on the board (${pct} % of its length).${sug ? ' A free spot is nearby.' : ' No free spot of this size was found.'}`,
      choices: [{ value: 'cancel', label: 'Cancel' }, ...(sug ? [{ value: 'move', label: 'Move to the free spot', kind: 'primary' }] : []), { value: 'draw', label: 'Draw over it', kind: sug ? '' : 'primary' }] });
    if (choice === 'draw') return true;
    if (choice === 'move' && sug) {
      M.moveAll(sug.dx, sug.dy);
      notice('writing', 'Moved to a free spot. Check the preview, then press Draw.', 'info');
    }
    return false;
  }

  async function start(dry) {
    if (!dry && ['serial', 'wifi'].includes(store.get('state')?.connection?.transport)) {
      const ok = await confirmDialog({ title: 'Start drawing?', text: 'The real machine will move and draw. Is the gondola free and the board clear? The space bar stops everything at any time.', ok: 'Start drawing' });
      if (!ok) return;
    }
    if (!dry && !(await checkWriting())) return;
    const first = M.elements().find((e) => e.type === 'text' && e.text)?.text?.split('\n')[0];
    const job = await api.startJob({ ...M.planBody(), dry_run: dry, speed_scale: speed, name: first ? first.slice(0, 30) : 'Drawing' });
    store.set({ job });
  }
  drawBtn.addEventListener('click', () => run(drawBtn, () => start(false)));
  dryBtn.addEventListener('click', () => run(dryBtn, () => start(true)));
  pauseBtn.addEventListener('click', () => run(pauseBtn, () => api.hold()));
  resumeBtn.addEventListener('click', () => run(resumeBtn, () => api.resume()));
  cancelBtn.addEventListener('click', () => run(cancelBtn, async () => { await api.cancel(); }));

  function paintJob() {
    const job = store.get('job'), s = store.get('state'), c = caps(s, job), active = c.jobActive;
    const showJob = !!job && (active || ['done', 'error', 'cancelled'].includes(job.state));
    for (const el of [jobState, progress, jobInfo, jobMsg]) el.hidden = !showJob;
    if (job && showJob) {
      jobState.textContent = `${job.name ? job.name + ': ' : ''}${JOB_LABEL[job.state] ?? job.state}${job.dry_run ? ' (test run)' : ''}`;
      jobState.className = 'chip ' + (job.state === 'done' ? 'ok' : job.state === 'error' ? 'danger' : job.state === 'cancelled' ? 'warn' : '');
      const p = Math.max(0, Math.min(1, job.progress ?? 0));
      bar.style.transform = `scaleX(${p})`; progress.setAttribute('aria-valuenow', String(Math.round(p * 100)));
      jobInfo.textContent = `${fmt(p * 100, 0)} %  ·  ${fmtDur(job.elapsed_s)} elapsed${active ? `  ·  ~${fmtDur(job.eta_s)} left` : ''}`;
      jobMsg.textContent = job.message ?? '';
      if (lastJobState && lastJobState !== job.state) {
        if (job.state === 'done' && !isLiveJob(job)) notice('job', 'Drawing finished.', 'ok');
        if (job.state === 'error') notice('job', `The job stopped: ${job.message || 'unknown error'}`, 'error');
      }
      lastJobState = job.state;
    }
    const pv = store.get('preview');
    const noSrc = !M.elements().length ? 'Add something to draw first.' : !pv?.drawing?.strokes?.length ? 'Nothing to draw yet. Check the elements for errors.' : '';
    const why = noSrc || c.abs;
    gate(drawBtn, why); gate(dryBtn, why); startNote.textContent = noSrc || active ? '' : c.abs;
    pauseBtn.hidden = !(active && job.state !== 'paused'); resumeBtn.hidden = !(active && job.state === 'paused'); cancelBtn.hidden = !active;
    drawBtn.hidden = dryBtn.hidden = active;
    gate(pauseBtn, c.connected ? '' : 'Not connected.'); gate(resumeBtn, c.connected ? '' : 'Not connected.');
  }

  root.append(
    sect(null, addBox, hint, busyChip),
    sect(null, listHead, list),
    sect(null, propsBox),
    sect(null, summary, warns, samples),
    sect(null, drawBtn, dryBtn, h('div', { class: 'row' }, pauseBtn, resumeBtn, cancelBtn), startNote, jobState, progress, jobInfo, jobMsg, speedRow));

  store.on(['elements', 'selection', 'preview'], paintList, { immediate: true });
  store.on('patterns', paintSamples, { immediate: true });
  store.on(['job', 'state', 'preview', 'elements'], paintJob, { immediate: true });
  store.on(['preview', 'elements'], paintPreview, { immediate: true });

  /* ---------- Window-wide drop and paste ---------- */
  const over = h('div', { class: 'drop-overlay', hidden: true }, h('div', null, icon('upload'), h('b', null, 'Drop to import')));
  document.body.append(over);
  let depth = 0;
  window.addEventListener('dragenter', (e) => { if (e.dataTransfer?.types?.includes('Files')) { depth++; over.hidden = false; } });
  window.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) over.hidden = true; });
  window.addEventListener('dragover', (e) => { if (e.dataTransfer?.types?.includes('Files')) e.preventDefault(); });
  window.addEventListener('drop', (e) => {
    depth = 0; over.hidden = true;
    const f = e.dataTransfer?.files?.[0];
    if (!f) return;
    e.preventDefault();
    loadFile(f);
  });
  window.addEventListener('paste', (e) => {
    const t = e.target; if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) return;
    const f = [...(e.clipboardData?.files ?? [])].find((x) => x.type.startsWith('image/'));
    if (f) { e.preventDefault(); loadFile(f); }
  });
}
