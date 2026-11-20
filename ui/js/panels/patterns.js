// Reiter "Testbilder": Muster mit Parametern, Layout, Live-Vorschau, Zeichnen / Trockenlauf, Job-Steuerung, SVG-Import.
import { h, fmt, fmtDur, debounce, lsGet, lsSet } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, gate, toast, confirmDialog } from '../ui.js';
import { numField, selectField, toggle, slider } from '../widgets.js';
import { caps } from '../caps.js';

const JOB_LABEL = { planning: 'Plane …', running: 'Läuft', paused: 'Pausiert', done: 'Fertig', cancelled: 'Abgebrochen', error: 'Fehler' };
const DEFAULT_LAYOUT = { scale: 1, rotate: 0, dx: 0, dy: 0, fit: false };

export function mountPatterns(root) {
  const sect = (title, ...kids) => h('div', { class: 'sect' }, h('div', { class: 'sect-h' }, h('h2', null, title)), ...kids);
  const note = () => h('div', { class: 'gate-note', role: 'status' });

  let selected = lsGet('wbot-pattern', null);
  const values = {};                        // pro Muster gemerkte Parameter
  const layout = { ...DEFAULT_LAYOUT };
  let source = { kind: 'pattern' };         // oder {kind:'svg', name, drawing}
  let speed = 1, livePreview = true, pvToken = 0, lastJobState = null;

  /* ---------- Auswahl ---------- */
  const list = h('div', { class: 'pat-list' });
  const desc = h('p', { class: 'muted', style: { fontSize: '14px' } });
  const fileIn = h('input', { type: 'file', accept: '.svg,image/svg+xml', hidden: true });
  const svgBtn = h('button', { class: 'btn', type: 'button' }, icon('upload'), 'SVG laden …');
  const svgChip = h('div', { class: 'row', style: { alignItems: 'center' }, hidden: true });
  svgBtn.addEventListener('click', () => fileIn.click());
  fileIn.addEventListener('change', () => {
    const f = fileIn.files?.[0]; fileIn.value = '';
    if (!f) return;
    run(svgBtn, async () => {
      const r = await api.svg(f);
      source = { kind: 'svg', name: f.name, drawing: r.drawing };
      toast(`SVG „${f.name}“ geladen.`, 'ok');
      renderSource(); schedule(true);
    });
  });
  function renderSource() {
    svgChip.hidden = source.kind !== 'svg';
    svgChip.replaceChildren(h('span', { class: 'chip ok' }, `SVG: ${source.name ?? ''}`),
      h('button', { class: 'btn sm', type: 'button', onClick: () => { source = { kind: 'pattern' }; renderSource(); schedule(true); } }, 'Entfernen'));
    paramBox.hidden = source.kind === 'svg';
    list.hidden = source.kind === 'svg';
    desc.hidden = source.kind === 'svg';
  }

  /* ---------- Parameter-Formular ---------- */
  const paramBox = h('div', { class: 'stack' });
  const pat = () => store.get('patterns').find((p) => p.id === selected);

  function choiceOpts(p) { return (p.choices ?? []).map((c) => (typeof c === 'object' ? [c.value, c.label ?? c.value] : [c, c])); }
  function renderParams() {
    const p = pat();
    paramBox.replaceChildren();
    if (!p) return;
    const vals = (values[p.id] ??= Object.fromEntries(p.params.map((q) => [q.id, q.default])));
    const change = () => schedule();
    for (const q of p.params) {
      if (q.kind === 'bool') {
        const t = toggle({ label: q.label, checked: !!vals[q.id], onChange: (v) => { vals[q.id] = v; change(); } });
        paramBox.append(t.el);
      } else if (q.kind === 'choice') {
        const f = selectField({ label: q.label, options: choiceOpts(q), value: String(vals[q.id]), onChange: (v) => { vals[q.id] = v; change(); } });
        paramBox.append(f.el);
      } else {
        const isInt = q.kind === 'int', step = isInt ? 1 : 'any';
        const num = numField({ label: q.label, unit: q.unit, min: q.min, max: q.max, step, value: vals[q.id] });
        const wrap = h('div', { class: 'param' }, h('span', { class: 'field-label' }, q.label));
        num.el.querySelector('.field-label').hidden = true;
        const setV = (v) => { if (v == null) return; if (q.min != null) v = Math.max(q.min, v); if (q.max != null) v = Math.min(q.max, v); if (isInt) v = Math.round(v); vals[q.id] = v; };
        if (q.min != null && q.max != null) {
          const sl = slider({ min: q.min, max: q.max, step: isInt ? 1 : (q.max - q.min) / 200, value: vals[q.id], label: q.label,
            onInput: (v) => { setV(v); num.input.value = String(vals[q.id]); change(); } });
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
    if (!ps.length) { list.replaceChildren(h('p', { class: 'muted' }, 'Keine Testbilder vom Server erhalten.')); return; }
    if (!ps.some((p) => p.id === selected)) selected = ps[0].id;
    list.replaceChildren(...ps.map((p) => h('button', { class: 'btn sm', type: 'button', 'aria-pressed': String(p.id === selected), title: p.description,
      onClick: () => { selected = p.id; lsSet('wbot-pattern', selected); renderList(); renderParams(); schedule(true); } }, p.name)));
    desc.textContent = pat()?.description ?? '';
  }

  /* ---------- Layout ---------- */
  const mkNum = (key, label, unit, step) => numField({ label, unit, step, value: layout[key], onChange: (v) => { layout[key] = v ?? DEFAULT_LAYOUT[key]; schedule(); } });
  const lScale = mkNum('scale', 'Skalierung', '×', 0.05), lRot = mkNum('rotate', 'Drehung', '°', 1);
  const lDx = mkNum('dx', 'Versatz X', 'mm', 1), lDy = mkNum('dy', 'Versatz Y', 'mm', 1);
  const fitT = toggle({ label: 'An erlaubten Bereich anpassen', checked: false, onChange: (v) => { layout.fit = v; schedule(); } });
  const resetL = h('button', { class: 'btn sm', type: 'button', onClick: () => {
    Object.assign(layout, DEFAULT_LAYOUT); fitT.set(false);
    lScale.input.value = '1'; lRot.input.value = '0'; lDx.input.value = '0'; lDy.input.value = '0'; schedule(true);
  } }, 'Zurücksetzen');
  const liveT = toggle({ label: 'Vorschau automatisch aktualisieren', checked: true, onChange: (v) => { livePreview = v; if (v) schedule(true); } });
  const pvNow = h('button', { class: 'btn sm', type: 'button' }, 'Vorschau erzeugen');
  const pvHide = h('button', { class: 'btn sm', type: 'button', onClick: () => store.set({ preview: null }) }, 'Vorschau ausblenden');
  pvNow.addEventListener('click', () => run(pvNow, doPreview));

  /* ---------- Vorschau ---------- */
  const stats = h('div', { class: 'stats' });
  const warns = h('div', { class: 'stack' });
  const body = () => ({
    ...(source.kind === 'svg' ? { drawing: source.drawing } : { pattern: { id: selected, params: values[selected] ?? {} } }),
    layout: { ...layout },
  });
  async function doPreview() {
    if (source.kind !== 'svg' && !pat()) return;
    const my = ++pvToken;
    try {
      const r = await api.preview(body());
      if (my !== pvToken) return;                  // veraltete Antwort
      store.set({ preview: r });
    } catch { /* Toast kam aus api.js */ }
  }
  const debounced = debounce(doPreview, 350);
  function schedule(now = false) { if (!livePreview && !now) return; if (now) { debounced.cancel(); doPreview(); } else debounced(); }

  function paintPreview() {
    const pv = store.get('preview');
    stats.replaceChildren(); warns.replaceChildren();
    pvHide.hidden = !pv;
    if (!pv) return;
    const st = pv.stats ?? {};
    const cell = (v, l) => h('div', { class: 'stat' }, h('b', null, v), h('span', null, l));
    stats.append(cell(fmtDur(pv.estimated_s), 'geschätzte Dauer'), cell(fmt(st.strokes, 0), 'Striche'), cell(`${fmt((st.draw_mm ?? 0) / 1000, 2)} m`, 'Zeichenweg'),
      cell(`${fmt((st.travel_mm ?? 0) / 1000, 2)} m`, 'Leerfahrt'), cell(fmt(st.color_changes, 0), 'Farbwechsel'), cell(fmt(st.lines, 0), 'G-Code-Zeilen'));
    for (const w of pv.warnings ?? []) warns.append(h('div', { class: 'note warn' }, w));
  }

  /* ---------- Job ---------- */
  const jobState = h('span', { class: 'chip' });
  const bar = h('i'), progress = h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100' }, bar);
  const jobInfo = h('div', { class: 'muted mono' });
  const jobMsg = h('div', { class: 'muted' });
  const drawBtn = h('button', { class: 'btn primary', type: 'button' }, icon('pen'), 'Zeichnen');
  const dryBtn = h('button', { class: 'btn', type: 'button' }, icon('play'), 'Trockenlauf (Stift oben)');
  const pauseBtn = h('button', { class: 'btn', type: 'button' }, icon('pause'), 'Pause');
  const resumeBtn = h('button', { class: 'btn', type: 'button' }, icon('play'), 'Weiter');
  const cancelBtn = h('button', { class: 'btn danger', type: 'button' }, icon('stop'), 'Abbrechen');
  const startNote = note();
  const speedSl = slider({ min: 25, max: 150, step: 5, value: 100, label: 'Tempo', onInput: (v) => { speed = v / 100; speedLbl.textContent = `${v} %`; } });
  const speedLbl = h('span', { class: 'mono-val', style: { minWidth: '48px', textAlign: 'right' } }, '100 %');
  speedSl.input.style.cssText = 'flex:1 1 120px;width:auto;min-width:100px';

  async function start(dry) {
    if (!dry && ['serial', 'wifi'].includes(store.get('state')?.connection?.transport)) {
      const ok = await confirmDialog({ title: 'Zeichnen starten?', text: 'Die echte Maschine fährt los und zeichnet mit dem Stift. Gondel und Brett frei? Mit Not-Halt (Leertaste) lässt sich jederzeit stoppen.', ok: 'Zeichnen starten' });
      if (!ok) return;
    }
    const job = await api.startJob({ ...body(), dry_run: dry, speed_scale: speed });
    store.set({ job });
    toast(dry ? 'Trockenlauf gestartet.' : 'Zeichnen gestartet.', 'ok');
  }
  drawBtn.addEventListener('click', () => run(drawBtn, () => start(false)));
  dryBtn.addEventListener('click', () => run(dryBtn, () => start(true)));
  pauseBtn.addEventListener('click', () => run(pauseBtn, () => api.hold()));
  resumeBtn.addEventListener('click', () => run(resumeBtn, () => api.resume()));
  cancelBtn.addEventListener('click', () => run(cancelBtn, async () => { await api.cancel(); toast('Job abgebrochen.', 'info'); }));

  function paintJob() {
    const job = store.get('job'), s = store.get('state'), c = caps(s, job);
    const active = c.jobActive;
    jobState.hidden = !job;
    if (job) {
      jobState.textContent = `${job.name ? job.name + ': ' : ''}${JOB_LABEL[job.state] ?? job.state}${job.dry_run ? ' (Trockenlauf)' : ''}`;
      jobState.className = 'chip ' + (job.state === 'done' ? 'ok' : job.state === 'error' ? 'danger' : job.state === 'cancelled' ? 'warn' : '');
      const p = Math.max(0, Math.min(1, job.progress ?? 0));
      bar.style.transform = `scaleX(${p})`; progress.setAttribute('aria-valuenow', String(Math.round(p * 100)));
      jobInfo.textContent = `${fmt(p * 100, 0)} %  ·  Zeile ${fmt(job.line, 0)} von ${fmt(job.total, 0)}  ·  vergangen ${fmtDur(job.elapsed_s)}  ·  noch ca. ${active ? fmtDur(job.eta_s) : '–'}`;
      jobMsg.textContent = job.message ?? '';
      if (lastJobState && lastJobState !== job.state) {
        if (job.state === 'done') toast('Job fertig.', 'ok');
        if (job.state === 'error') toast(`Job-Fehler: ${job.message || 'unbekannt'}`, 'error');
      }
      lastJobState = job.state;
    } else { bar.style.transform = 'scaleX(0)'; jobInfo.textContent = 'Kein Job aktiv.'; jobMsg.textContent = ''; }
    const noSrc = source.kind !== 'svg' && !pat() ? 'Keine Testbilder verfügbar.' : '';
    const why = c.abs || noSrc;
    gate(drawBtn, why); gate(dryBtn, why); startNote.textContent = why;
    pauseBtn.hidden = !(active && job.state !== 'paused'); resumeBtn.hidden = !(active && job.state === 'paused'); cancelBtn.hidden = !active;
    gate(pauseBtn, c.connected ? '' : 'Nicht verbunden.'); gate(resumeBtn, c.connected ? '' : 'Nicht verbunden.');
  }

  root.append(
    sect('Testbild', svgBtn, fileIn, svgChip, list, desc, paramBox),
    sect('Layout auf dem Brett',
      h('div', { class: 'grid2' }, lScale.el, lRot.el, lDx.el, lDy.el), fitT.el, h('div', { class: 'row' }, resetL)),
    sect('Vorschau', h('div', { class: 'row', style: { alignItems: 'center' } }, liveT.el), h('div', { class: 'row' }, pvNow, pvHide), stats, warns),
    sect('Ausführen',
      h('div', { class: 'row', style: { alignItems: 'center' } }, h('span', { class: 'field-label', style: { flex: 'none' } }, 'Tempo'), speedSl.input, speedLbl),
      h('div', { class: 'grid2' }, drawBtn, dryBtn), startNote,
      jobState, progress, jobInfo, jobMsg,
      h('div', { class: 'row' }, pauseBtn, resumeBtn, cancelBtn)));

  store.on('patterns', () => { renderList(); renderParams(); schedule(true); }, { immediate: true });
  store.on(['job', 'state'], paintJob, { immediate: true });
  store.on('preview', paintPreview, { immediate: true });
  renderSource();
}
