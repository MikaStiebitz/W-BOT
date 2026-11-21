// Draw tab: drop an image (PNG, JPEG, SVG ...) or pick a sample, see exactly what will be plotted, press Draw.
import { h, fmt, fmtDur, debounce, lsGet, lsSet } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, gate, toast, notice, clearNotice, confirmDialog } from '../ui.js';
import { numField, selectField, toggle, slider, segmented, swatch } from '../widgets.js';
import { caps } from '../caps.js';
import { isLiveJob } from '../live.js';
import { kvGet, kvSet, kvDel } from '../persist.js';

const JOB_LABEL = { planning: 'Planning …', running: 'Drawing', paused: 'Paused', done: 'Done', cancelled: 'Cancelled', error: 'Error' };
const DEFAULT_LAYOUT = { scale: 1, rotate: 0, dx: 0, dy: 0, fit: true };
const ACCEPT = 'image/png,image/jpeg,image/webp,image/bmp,image/gif,image/svg+xml,.svg,.png,.jpg,.jpeg,.webp,.bmp,.gif';
const IMAGE_RE = /\.(png|jpe?g|webp|bmp|gif|svg)$/i;

let openFile = () => {};                         // set by mountDraw: used by the window-wide drop / paste handlers
export function importFile(file) { openFile(file); }

export function mountDraw(root) {
  const sect = (title, ...kids) => h('div', { class: 'sect' }, title ? h('div', { class: 'sect-h' }, h('h2', null, title)) : null, ...kids);
  const note = () => h('div', { class: 'gate-note', role: 'status' });

  const layout = { ...DEFAULT_LAYOUT, ...lsGet('wbot-layout', {}) };          // size, rotation and shift survive a reload
  const opt = { mode: 'auto', detail: 50, invert: false, colors: false, shade: false, ...lsGet('wbot-trace', {}) };
  let source = { kind: 'none' };                // {kind:'image', file, name, drawing, info} | {kind:'pattern'}
  let selected = lsGet('wbot-pattern', null);
  const values = lsGet('wbot-values', {});                                      // sample parameters per sample
  let speed = lsGet('wbot-speed', 1), pvToken = 0, trToken = 0, lastJobState = null, restoreSample = false, persistedFile = null, restoring = true;

  /* ---------- 1. Drop zone ---------- */
  const fileIn = h('input', { type: 'file', accept: ACCEPT, hidden: true, 'aria-label': 'Choose an image' });
  const drop = h('div', { class: 'drop', tabindex: '0', role: 'button', 'aria-label': 'Choose or drop an image' },
    h('div', { class: 'drop-ic' }, icon('upload')),
    h('div', { class: 'drop-t' }, 'Drop an image here'),
    h('div', { class: 'drop-s' }, 'PNG, JPEG, SVG · or click to browse · or paste'));
  drop.addEventListener('click', () => fileIn.click());
  drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); fileIn.click(); } });
  fileIn.addEventListener('change', () => { const f = fileIn.files?.[0]; fileIn.value = ''; if (f) loadFile(f); });
  for (const ev of ['dragenter', 'dragover']) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); });
  for (const ev of ['dragleave', 'drop']) drop.addEventListener(ev, () => drop.classList.remove('over'));

  const thumb = h('canvas', { class: 'thumb', width: 64, height: 64, 'aria-hidden': 'true' });
  const fileName = h('b', { class: 'file-name' });
  const fileMeta = h('span', { class: 'muted file-meta' });
  const fileBar = h('div', { class: 'file-bar', hidden: true },
    thumb, h('div', { class: 'grow' }, fileName, fileMeta),
    h('button', { class: 'btn sm', type: 'button', onClick: () => fileIn.click() }, 'Replace'),
    h('button', { class: 'btn sm ghost icon-only', type: 'button', title: 'Remove', 'aria-label': 'Remove image', onClick: clearSource }, icon('x')));

  /* ---------- 2. Trace options (images only) ---------- */
  const modeSeg = segmented({ label: 'Drawing style', fill: true, value: opt.mode, options: [
    { value: 'auto', label: 'Auto', title: 'Picks the best style for the image' },
    { value: 'photo', label: 'Photo', title: 'Outlines and inner lines of objects (photos, renders)' },
    { value: 'lines', label: 'Lines', title: 'Centre lines of thin strokes (line art, handwriting)' },
    { value: 'outline', label: 'Outline', title: 'Edges of dark areas (logos, silhouettes)' },
    { value: 'fill', label: 'Fill', title: 'Hatching with the pen width (solid areas, slow)' }],
  onChange: (v) => { opt.mode = v; optsChanged(); } });
  const detailSl = slider({ min: 0, max: 100, step: 5, value: opt.detail, label: 'Detail', onInput: (v) => { opt.detail = v; detailLbl.textContent = String(v); }, onChange: () => optsChanged() });
  const detailLbl = h('span', { class: 'mono-val', style: { minWidth: '30px', textAlign: 'right' } }, String(opt.detail));
  const colorsT = toggle({ label: 'Use all pen colours', checked: opt.colors, onChange: (v) => { opt.colors = v; optsChanged(); } });
  const invertT = toggle({ label: 'Invert (light on dark)', checked: opt.invert, onChange: (v) => { opt.invert = v; optsChanged(); } });
  const shadeT = toggle({ label: 'Shade dark areas', checked: opt.shade, onChange: (v) => { opt.shade = v; optsChanged(); } });
  const penRow = h('div', { class: 'pen-pick', role: 'group', 'aria-label': 'Pen' });
  const penLbl = h('span', { class: 'field-label' }, 'Pen');
  const optBox = h('div', { class: 'stack', hidden: true },
    modeSeg.el,
    h('div', { class: 'row-s' }, h('span', { class: 'field-label', style: { flex: 'none', width: '52px' } }, 'Detail'), detailSl.input, detailLbl),
    shadeT.el, colorsT.el, h('div', { class: 'row-s' }, penLbl, penRow), invertT.el);

  function paintPens() {
    const pens = store.get('config')?.colors ?? [], cur = store.get('selectedPen') ?? 0;
    penRow.replaceChildren(...pens.map((p, i) => h('button', { type: 'button', class: 'pen-dot', 'aria-pressed': String(i === cur), title: `Pen ${i + 1}: ${p.name}`,
      'aria-label': `Pen ${i + 1}: ${p.name}`, style: { '--c': p.hex }, onClick: () => { store.set({ selectedPen: i }); lsSet('wbot-pen', i); } })));
    // photo style draws edges with one pen: colour split and invert do not apply, shading does
    const photo = opt.mode === 'photo' || (opt.mode === 'auto' && !opt.colors && source.info?.mode === 'photo');
    shadeT.el.hidden = !photo;
    colorsT.el.hidden = opt.mode === 'photo';
    invertT.el.hidden = photo;
    penRow.hidden = penLbl.hidden = opt.colors && opt.mode !== 'photo';
  }

  /* ---------- 3. Samples ---------- */
  const list = h('div', { class: 'pat-list' });
  const desc = h('p', { class: 'muted', style: { fontSize: '14px' } });
  const paramBox = h('div', { class: 'stack' });
  const pat = () => store.get('patterns').find((p) => p.id === selected);
  const samples = h('details', { class: 'more' }, h('summary', null, 'Or try a sample'), h('div', { class: 'stack more-body' }, list, desc, paramBox));

  function choiceOpts(p) { return (p.choices ?? []).map((c) => (typeof c === 'object' ? [c.value, c.label ?? c.value] : [c, c])); }
  function renderParams() {
    const p = pat();
    paramBox.replaceChildren();
    if (!p) return;
    const vals = (values[p.id] ??= Object.fromEntries(p.params.map((q) => [q.id, q.default])));
    const change = () => { source = { kind: 'pattern' }; paintSource(); schedulePreview(true); };
    for (const q of p.params) {
      if (q.kind === 'bool') paramBox.append(toggle({ label: q.label, checked: !!vals[q.id], onChange: (v) => { vals[q.id] = v; change(); } }).el);
      else if (q.kind === 'choice') paramBox.append(selectField({ label: q.label, options: choiceOpts(q), value: String(vals[q.id]), onChange: (v) => { vals[q.id] = v; change(); } }).el);
      else {
        const isInt = q.kind === 'int';
        const num = numField({ label: q.label, unit: q.unit, min: q.min, max: q.max, step: isInt ? 1 : 'any', value: vals[q.id] });
        const wrap = h('div', { class: 'param' }, h('span', { class: 'field-label' }, q.label));
        num.el.querySelector('.field-label').hidden = true;
        const setV = (v) => { if (v == null) return; if (q.min != null) v = Math.max(q.min, v); if (q.max != null) v = Math.min(q.max, v); if (isInt) v = Math.round(v); vals[q.id] = v; };
        if (q.min != null && q.max != null) {
          const sl = slider({ min: q.min, max: q.max, step: isInt ? 1 : (q.max - q.min) / 200, value: vals[q.id], label: q.label,
            onInput: (v) => { setV(v); num.input.value = String(vals[q.id]); schedulePreview(); } });
          wrap.append(sl.input);
          num.input.addEventListener('change', () => { setV(num.get()); num.input.value = String(vals[q.id]); sl.input.value = String(vals[q.id]); change(); });
        } else num.input.addEventListener('change', () => { setV(num.get()); change(); });
        wrap.append(num.el);
        paramBox.append(wrap);
      }
    }
  }
  function renderList() {
    const ps = store.get('patterns');
    if (!ps.length) { list.replaceChildren(h('p', { class: 'muted' }, 'No samples available.')); return; }
    if (!ps.some((p) => p.id === selected)) selected = ps[0].id;
    list.replaceChildren(...ps.map((p) => h('button', { class: 'btn sm', type: 'button', 'aria-pressed': String(source.kind === 'pattern' && p.id === selected), title: p.description,
      onClick: () => { selected = p.id; lsSet('wbot-pattern', selected); source = { kind: 'pattern' }; paintSource(); renderList(); renderParams(); schedulePreview(true); } }, p.name)));
    desc.textContent = pat()?.description ?? '';
  }

  /* ---------- 4. Size & position ---------- */
  const scaleSl = slider({ min: 10, max: 300, step: 5, value: Math.round(layout.scale * 100), label: 'Size', onInput: (v) => { layout.scale = v / 100; scaleLbl.textContent = `${v} %`; schedulePreview(); } });
  const scaleLbl = h('span', { class: 'mono-val', style: { minWidth: '52px', textAlign: 'right' } }, `${Math.round(layout.scale * 100)} %`);
  const rotBtn = h('button', { class: 'btn sm', type: 'button', title: 'Rotate by 90°' }, icon('refresh'), layout.rotate ? `Rotate (${layout.rotate}°)` : 'Rotate');
  rotBtn.addEventListener('click', () => { layout.rotate = (layout.rotate + 90) % 360; rotBtn.lastChild.textContent = layout.rotate ? `Rotate (${layout.rotate}°)` : 'Rotate'; schedulePreview(true); });
  const dx = numField({ label: 'Shift X', unit: 'mm', step: 1, value: layout.dx, onChange: (v) => { layout.dx = v ?? 0; schedulePreview(true); } });
  const dy = numField({ label: 'Shift Y', unit: 'mm', step: 1, value: layout.dy, onChange: (v) => { layout.dy = v ?? 0; schedulePreview(true); } });
  const fitT = toggle({ label: 'Fit to the drawing area', checked: layout.fit, onChange: (v) => { layout.fit = v; schedulePreview(true); } });
  const resetBtn = h('button', { class: 'btn sm', type: 'button', onClick: () => {
    Object.assign(layout, DEFAULT_LAYOUT); fitT.set(true); scaleSl.input.value = '100'; scaleLbl.textContent = '100 %';
    dx.input.value = '0'; dy.input.value = '0'; rotBtn.lastChild.textContent = 'Rotate'; schedulePreview(true);
  } }, 'Reset');
  const sizeBox = h('details', { class: 'more' }, h('summary', null, 'Size and position'),
    h('div', { class: 'stack more-body' }, fitT.el,
      h('div', { class: 'row-s' }, h('span', { class: 'field-label', style: { flex: 'none', width: '52px' } }, 'Size'), scaleSl.input, scaleLbl),
      h('div', { class: 'grid2' }, dx.el, dy.el), h('div', { class: 'row' }, rotBtn, resetBtn)));

  /* ---------- Preview ---------- */
  const summary = h('div', { class: 'summary', hidden: true });
  const warns = h('div', { class: 'stack' });
  const busyChip = h('span', { class: 'chip', hidden: true, role: 'status' }, 'Working …');
  const body = () => ({
    ...(source.kind === 'image' ? { drawing: source.drawing } : { pattern: { id: selected, params: values[selected] ?? {} } }),
    layout: { ...layout },
  });
  const hasSource = () => source.kind === 'image' ? !!source.drawing : source.kind === 'pattern' && !!pat();

  async function doPreview() {
    if (!hasSource()) { store.set({ preview: null }); return; }
    const my = ++pvToken;
    try {
      const r = await api.preview(body());
      if (my !== pvToken) return;
      store.set({ preview: r });
    } catch { /* notice came from api.js */ }
  }
  const debouncedPreview = debounce(doPreview, 300);
  function schedulePreview(now = false) {
    lsSet('wbot-layout', layout); lsSet('wbot-values', values);               // saved with every change
    if (now) { debouncedPreview.cancel(); doPreview(); } else debouncedPreview();
  }

  function paintPreview() {
    const pv = store.get('preview');
    summary.replaceChildren(); warns.replaceChildren();
    summary.hidden = !pv;
    if (!pv) return;
    const st = pv.stats ?? {};
    const chip = (ic, v, title) => h('span', { class: 'sum', title }, ic ? icon(ic) : null, v);
    const e = pv.estimate, learned = e && e.samples >= 3 && Math.abs(e.factor - 1) > 0.02;
    const basis = !e ? '' : learned ? `Model estimate corrected by ${e.samples} finished drawings (real time was ×${e.factor} of the model).`
      : 'Model estimate from path length, feed rates, acceleration and pen moves. It gets better with every finished drawing.';
    summary.append(chip('play', `≈ ${fmtDur(pv.estimated_s)}`, basis), chip(null, `${fmt(st.strokes, 0)} paths`), chip(null, `${fmt((st.draw_mm ?? 0) / 1000, 1)} m of line`));
    if (st.color_changes) summary.append(chip('refresh', `${fmt(st.color_changes, 0)} pen changes`));
    const seen = new Set();
    for (const w of [...(pv.warnings ?? []), ...(source.warnings ?? [])]) if (!seen.has(w)) { seen.add(w); warns.append(h('div', { class: 'note warn' }, w)); }
  }

  /* ---------- Loading ---------- */
  function persistSource() {                      // the image itself goes to IndexedDB, everything small to localStorage
    if (restoring) return;                         // the first paint must not overwrite what is about to be restored
    lsSet('wbot-source', source.kind);
    if (source.kind === 'image') { if (persistedFile !== source.file) { persistedFile = source.file; kvSet('image', source.file); } }
    else { persistedFile = null; kvDel('image'); }
  }

  function paintSource() {
    persistSource();
    const img = source.kind === 'image';
    fileBar.hidden = !img;
    optBox.hidden = !(img && !source.isSvg);
    drop.hidden = img;
    if (img) {
      fileName.textContent = source.name;
      fileMeta.textContent = source.info ? `${source.info.pixels?.join(' × ')} px · ${source.info.mode}` : source.isSvg ? 'vector image' : 'tracing …';
    }
    samples.open = source.kind === 'none' ? samples.open : source.kind === 'pattern';
    renderThumb();
    paintPens();
  }

  function renderThumb() {
    const c = thumb.getContext('2d'); c.clearRect(0, 0, 64, 64);
    if (source.kind !== 'image' || !source.bitmap) return;
    const b = source.bitmap, k = Math.min(64 / b.width, 64 / b.height);
    c.fillStyle = '#fff'; c.fillRect(0, 0, 64, 64);
    c.drawImage(b, (64 - b.width * k) / 2, (64 - b.height * k) / 2, b.width * k, b.height * k);
  }

  async function loadFile(file, { restore = false } = {}) {
    if (!file) return;
    const okType = file.type.startsWith('image/') || IMAGE_RE.test(file.name || '');
    if (!okType) { notice('upload', `“${file.name}” is not an image. Use PNG, JPEG, WebP, BMP, GIF or SVG.`, 'error'); return; }
    if (file.size > 30e6) { notice('upload', `“${file.name}” is ${(file.size / 1e6).toFixed(0)} MB. The maximum is 30 MB.`, 'error'); return; }
    clearNotice('upload');
    if (!restore) store.set({ tab: 'draw' });
    const isSvg = /svg/i.test(file.type) || /\.svg$/i.test(file.name || '');
    source = { kind: 'image', file, name: file.name || 'image', isSvg, drawing: null, info: null, bitmap: null };
    if (!isSvg) createImageBitmap(file).then((b) => { if (source.file === file) { source.bitmap = b; renderThumb(); } }).catch(() => {});
    paintSource();
    await trace();
  }
  openFile = loadFile;

  async function trace() {
    if (source.kind !== 'image') return;
    const my = ++trToken, file = source.file;
    busyChip.hidden = false;
    try {
      const r = await api.importFile(file, { mode: opt.mode, detail: opt.detail, invert: opt.invert, use_pen_colors: opt.colors, shade: opt.shade, pen: store.get('selectedPen') ?? 0 });
      if (my !== trToken || source.file !== file) return;
      source.drawing = r.drawing; source.info = r.info ?? null; source.warnings = r.warnings ?? [];
      paintSource();
      await doPreview();
    } catch {
      if (my === trToken && source.file === file) { source.drawing = null; source.info = null; paintSource(); store.set({ preview: null }); }
    } finally { if (my === trToken) busyChip.hidden = true; }
  }
  const debouncedTrace = debounce(trace, 350);
  function optsChanged() { lsSet('wbot-trace', opt); paintPens(); debouncedTrace(); }

  function clearSource() { trToken++; pvToken++; source = { kind: 'none' }; store.set({ preview: null }); paintSource(); renderList(); }

  /* ---------- 5. Run ---------- */
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

  async function start(dry) {
    if (!dry && ['serial', 'wifi'].includes(store.get('state')?.connection?.transport)) {
      const ok = await confirmDialog({ title: 'Start drawing?', text: 'The real machine will move and draw. Is the gondola free and the board clear? The space bar stops everything at any time.', ok: 'Start drawing' });
      if (!ok) return;
    }
    const job = await api.startJob({ ...body(), dry_run: dry, speed_scale: speed, name: source.kind === 'image' ? source.name : undefined });
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
    const noSrc = !hasSource() ? 'Add an image or pick a sample first.' : source.kind === 'image' && source.drawing == null ? 'Preparing the image …' : '';
    const why = noSrc || c.abs;
    gate(drawBtn, why); gate(dryBtn, why); startNote.textContent = noSrc || active ? '' : c.abs;
    pauseBtn.hidden = !(active && job.state !== 'paused'); resumeBtn.hidden = !(active && job.state === 'paused'); cancelBtn.hidden = !active;
    drawBtn.hidden = dryBtn.hidden = active;
    gate(pauseBtn, c.connected ? '' : 'Not connected.'); gate(resumeBtn, c.connected ? '' : 'Not connected.');
  }

  root.append(
    sect(null, drop, fileBar, fileIn, optBox, busyChip, samples),
    sect(null, summary, warns, sizeBox),
    sect(null, drawBtn, dryBtn, h('div', { class: 'row' }, pauseBtn, resumeBtn, cancelBtn), startNote, jobState, progress, jobInfo, jobMsg, speedRow));

  store.on('patterns', () => {
    renderList(); renderParams();
    if (restoreSample && store.get('patterns').length) { restoreSample = false; restoring = false; source = { kind: 'pattern' }; paintSource(); renderList(); schedulePreview(true); }
  }, { immediate: true });
  store.on(['job', 'state', 'preview'], paintJob, { immediate: true });
  store.on('preview', paintPreview, { immediate: true });
  store.on(['config', 'selectedPen'], paintPens, { immediate: true });
  let colorSig = '';
  store.on('config', () => {                      // pen colours changed: the colour split of an image changes too
    const sig = (store.get('config')?.colors ?? []).map((c) => c.hex).join(',');
    if (colorSig && sig !== colorSig && opt.colors && source.kind === 'image' && !source.isSvg) debouncedTrace();
    colorSig = sig;
  }, { immediate: true });
  store.on('selectedPen', () => { if (!opt.colors && source.kind === 'image' && !source.isSvg) debouncedTrace(); });
  paintSource();

  /* ---------- Restore the drawing after a reload ---------- */
  (async () => {
    const kind = lsGet('wbot-source', 'none');
    if (kind === 'image') {
      const f = await kvGet('image');
      if (f) { persistedFile = f; await loadFile(f, { restore: true }); }
      restoring = false; persistSource();
    } else if (kind === 'pattern') { restoreSample = true; if (store.get('patterns').length) store.touch('patterns'); }
    else restoring = false;
  })();

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
