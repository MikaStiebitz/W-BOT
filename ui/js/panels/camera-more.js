// Reiter "Kamera", zweiter Teil: Objektiv-Kalibrierung, Gondel-Marker (Tracking) und die automatische Kalibrierung.
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

  /* ---------- 4. Objektiv ---------- */
  const lo = { cols: 9, rows: 6, square: 25, ...lsGet('wbot-lens', {}) };
  const keep = () => lsSet('wbot-lens', lo);
  const cols = numField({ label: 'Innere Ecken waagrecht', min: 3, max: 20, step: 1, value: lo.cols, onChange: (v) => { lo.cols = v; keep(); } });
  const rows = numField({ label: 'Innere Ecken senkrecht', min: 3, max: 20, step: 1, value: lo.rows, onChange: (v) => { lo.rows = v; keep(); } });
  const sq = numField({ label: 'Kästchen', unit: 'mm', min: 3, max: 200, step: 0.1, value: lo.square, onChange: (v) => { lo.square = v; keep(); } });
  const capBtn = h('button', { class: 'btn', type: 'button' }, icon('camera'), 'Aufnahme');
  const calcBtn = h('button', { class: 'btn primary', type: 'button' }, 'Objektiv berechnen');
  const lensClr = h('button', { class: 'btn sm', type: 'button' }, icon('trash'), 'Zurücksetzen');
  const lensInfo = h('div', { class: 'muted', role: 'status', style: { fontSize: '13px' } });
  capBtn.addEventListener('click', () => run(capBtn, async () => { const r = await api.lensCapture(lo.cols, lo.rows); toast(`Aufnahme ${r.views} gespeichert.`, 'ok'); await refreshCamera(); }));
  calcBtn.addEventListener('click', () => run(calcBtn, async () => { const r = await api.lensCompute(lo.square); toast(`Objektiv kalibriert (Restfehler ${fmt(r.lens.rms_px, 2)} px).`, 'ok'); await refreshCamera(); }));
  lensClr.addEventListener('click', () => run(lensClr, async () => { store.set({ camera: await api.lensClear() }); }));

  /* ---------- 5. Gondel-Marker ---------- */
  const gm = () => store.get('camera')?.settings?.markers?.gondola ?? {};
  const putG = async (patch) => { store.set({ camera: await api.cameraPut({ markers: { gondola: patch } }) }); };
  const gOn = toggle({ label: 'Gondel-Marker verfolgen', onChange: (v) => putG({ enabled: v }) });
  const gId = numField({ label: 'Marker-Nr.', min: 0, max: 49, step: 1, onChange: (v) => v != null && putG({ id: Math.round(v) }) });
  const gSize = numField({ label: 'Kantenlänge', unit: 'mm', min: 10, max: 200, step: 0.5, onChange: (v) => v && putG({ size_mm: v }) });
  const gH = numField({ label: 'Abstand zur Tafel', unit: 'mm', min: 0, max: 200, step: 0.5, hint: 'Markerfläche vor der Tafel (für die Schrägsicht-Korrektur).', onChange: (v) => v != null && putG({ height_mm: v }) });
  const learnBtn = h('button', { class: 'btn', type: 'button' }, 'Versatz einlernen');
  const latBtn = h('button', { class: 'btn', type: 'button' }, 'Kamera-Verzögerung messen');
  const trackInfo = h('div', { class: 'stats' });
  learnBtn.addEventListener('click', () => run(learnBtn, async () => { const r = await api.learnOffset(); toast(`Versatz Marker → Stift: ${fmt(r.offset_mm[0], 1)} | ${fmt(r.offset_mm[1], 1)} mm`, 'ok'); await refreshCamera(); }));
  latBtn.addEventListener('click', () => run(latBtn, async () => { const r = await api.latency(); toast(`Kamera-Verzögerung etwa ${r.latency_ms} ms.`, 'ok'); await refreshCamera(); }));

  /* ---------- 6. Automatisch kalibrieren ---------- */
  const ag = { nx: 5, ny: 4, inset: 20, ...lsGet('wbot-autocal', {}) };
  const keepA = () => lsSet('wbot-autocal', ag);
  const anx = numField({ label: 'Spalten', min: 2, max: 15, step: 1, value: ag.nx, onChange: (v) => { ag.nx = v; keepA(); } });
  const any = numField({ label: 'Zeilen', min: 2, max: 11, step: 1, value: ag.ny, onChange: (v) => { ag.ny = v; keepA(); } });
  const ains = numField({ label: 'Armlänge', unit: 'mm', min: 5, max: 20, step: 1, value: ag.inset, onChange: (v) => { ag.inset = v; keepA(); } });
  const goBtn = h('button', { class: 'btn primary', type: 'button' }, icon('pen'), 'Kreuze zeichnen und messen');
  const measBtn = h('button', { class: 'btn', type: 'button' }, icon('camera'), 'Nur messen');
  const stopBtn = h('button', { class: 'btn danger', type: 'button' }, icon('stop'), 'Abbrechen');
  const applyBtn = h('button', { class: 'btn', type: 'button' }, icon('save'), 'Korrektur übernehmen');
  const bar = h('i'), prog = h('div', { class: 'progress' }, bar);
  const step = h('div', { class: 'muted', role: 'status', style: { fontSize: '14px' } });
  const out = h('div', { class: 'stack' });
  const gateNote = h('div', { class: 'gate-note', role: 'status' });

  async function startAuto(draw) {
    if (draw && ['serial', 'wifi'].includes(store.get('state')?.connection?.transport)) {
      const ok = await confirmDialog({ title: 'Messkreuze zeichnen und messen?', text: 'Die Maschine zeichnet ein Kreuzraster, fährt die Gondel dann nacheinander nach links und rechts oben aus dem Bild und misst die Kreuze mit der Kamera. Brett frei? Not-Halt mit der Leertaste.', ok: 'Starten' });
      if (!ok) return;
    }
    store.set({ autocal: await api.autocal({ n_x: ag.nx, n_y: ag.ny, inset_mm: ag.inset, draw }) });
  }
  goBtn.addEventListener('click', () => run(goBtn, () => startAuto(true)));
  measBtn.addEventListener('click', () => run(measBtn, () => startAuto(false)));
  stopBtn.addEventListener('click', () => run(stopBtn, async () => { store.set({ autocal: await api.autocalCancel() }); }));
  applyBtn.addEventListener('click', () => run(applyBtn, async () => {
    const ac = store.get('autocal');
    const ok = await confirmDialog({ title: 'Korrektur übernehmen?', text: 'Die von der Kamera gemessene Geometrie wird in die Konfiguration geschrieben. Zum Prüfen danach noch einmal messen.', ok: 'Übernehmen' });
    if (!ok) return;
    const r = await api.calApply({ measurements: ac.measurements, free: null });
    if (r.config) store.set({ config: r.config });
    toast(r.applied ? 'Korrektur übernommen und gespeichert.' : 'Nichts übernommen: keine klare Verbesserung.', r.applied ? 'ok' : 'info');
    store.set({ autocal: { ...ac, applied: !!r.applied } });
  }));

  function paint() {
    const cam = store.get('camera'), s = cam?.settings;
    const lens = s?.lens;
    lensInfo.textContent = `${cam?.lens_views ?? 0} Aufnahmen gesammelt. ` + (lens ? `Objektiv kalibriert: Brennweite ${fmt(lens.K[0][0], 0)} px, Verzeichnung k1 ${fmt(lens.dist[0], 3)}, Restfehler ${fmt(lens.rms_px, 2)} px.` : 'Kein Objektivmodell (die Brennweite wird geschätzt, Verzeichnung bleibt unkorrigiert).');
    lensClr.hidden = !lens && !(cam?.lens_views);
    const g = gm();
    gOn.set(!!g.enabled); gId.set(g.id); gSize.set(g.size_mm); gH.set(g.height_mm);
    const tr = store.get('camTrack'), fresh = tr && Date.now() / 1000 - tr.t < 3;
    trackInfo.replaceChildren(cell(!cam?.calibrated ? '–' : fresh && tr.seen ? 'ja' : 'nein', 'Marker im Bild'),
      cell(fresh && tr.moving ? 'fährt' : fresh && tr.dev_mm != null ? `${fmt(tr.dev_mm, 1)} mm` : '–', 'Abweichung Kamera/Soll (im Stillstand)'),
      cell(g.offset_mm ? `${fmt(g.offset_mm[0], 1)} | ${fmt(g.offset_mm[1], 1)}` : '–', 'Versatz Marker → Stift (mm)'),
      cell(`${fmt(s?.latency_ms ?? 0, 0)} ms`, 'Kamera-Verzögerung'));
    const tw = !cam?.calibrated ? 'Erst einmessen.' : !g.enabled ? 'Tracking ist aus.' : '';
    gate(learnBtn, tw); gate(latBtn, tw);

    const c = caps(store.get('state'), store.get('job')), ac = store.get('autocal');
    const busy = ac && ['starting', 'drawing', 'measuring'].includes(ac.state);
    const why = !cam?.source ? 'Erst eine Kamera wählen.' : !cam?.calibrated ? 'Erst die Kamera auf die Tafel einmessen (Abschnitt 3).' : c.abs || (busy ? 'Läuft gerade.' : '');
    gate(goBtn, why); gate(measBtn, why); gateNote.textContent = why;
    stopBtn.hidden = !busy;
    bar.style.transform = `scaleX(${Math.min(1, ac?.progress ?? 0)})`;
    const job = store.get('job');
    step.textContent = !ac || ac.state === 'idle' ? '' : `${ac.step ?? ''}${ac.state === 'drawing' && job?.eta_s != null ? `, noch ca. ${fmtDur(job.eta_s)}` : ''}`;
    out.replaceChildren();
    if (ac?.points) {
      const found = ac.points.filter((q) => q.measured).length;
      const hidden = ac.points.filter((q) => q.status === 'verdeckt').length;
      out.append(h('div', { class: 'stats' }, cell(`${found} / ${ac.points.length}`, 'Kreuze erkannt'), cell(String(hidden), 'verdeckt')));
    }
    for (const w of ac?.warnings ?? []) out.append(h('div', { class: 'note warn' }, w));
    if (ac?.state === 'error') out.append(h('div', { class: 'note danger' }, ac.error));
    const r = ac?.result;
    if (r) {
      out.append(h('div', { class: 'stats' }, cell(`${fmt(r.rms_before, 2)} → ${fmt(r.rms_after, 2)} mm`, 'RMS vorher → nachher'),
        cell(`${fmt(r.max_before, 2)} → ${fmt(r.max_after, 2)} mm`, 'Größter Fehler'), cell(r.improved ? 'ja' : 'nein', 'klare Verbesserung')));
      for (const w of r.warnings ?? []) out.append(h('div', { class: 'note warn' }, w));
      const gp = r.patch?.geometry;
      if (gp) out.append(h('p', { class: 'mono', style: { fontSize: '13px', whiteSpace: 'pre-line' } }, Object.entries(gp).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.map((x) => fmt(x, 2)).join(' | ') : fmt(v, 5)}`).join('\n')));
    }
    const can = r?.improved && !ac.applied && Object.keys(r.patch ?? {}).length;
    gate(applyBtn, can ? '' : ac?.applied ? 'Bereits übernommen.' : 'Kein Korrekturvorschlag.');
    applyBtn.classList.toggle('primary', !!can);
    applyBtn.hidden = !r;
  }

  root.append(
    sect('4 Objektiv (optional)',
      p('Weitwinkel-Webcams verzeichnen am Rand um einige Millimeter. Ein gedrucktes Schachbrett auf fester Pappe 10 bis 15 Mal aus verschiedenen Winkeln und Abständen vor die Kamera halten und jeweils „Aufnahme“ drücken, dann berechnen. Danach neu einmessen.'),
      h('div', { class: 'grid3' }, cols.el, rows.el, sq.el), h('div', { class: 'grid2' }, capBtn, calcBtn), lensInfo, h('div', { class: 'row' }, lensClr)),
    sect('5 Gondel-Marker (Stift-Tracking)',
      p('Ein Marker vorn auf der Gondel zeigt, wo der Stift wirklich ist. Weicht er im Stillstand stark von der Soll-Position ab (übersprungener Riemenzahn, falsche Referenz), erscheint eine Warnung. Nach dem Aufkleben einmal bei gesetzter Referenz „Versatz einlernen“.'),
      gOn.el, h('div', { class: 'grid3' }, gId.el, gSize.el, gH.el), trackInfo, h('div', { class: 'grid2' }, learnBtn, latBtn)),
    sect('6 Automatisch kalibrieren',
      p('Die Maschine zeichnet ein Kreuzraster, fährt die Gondel zur Seite, die Kamera misst jedes Kreuz auf Bruchteile eines Millimeters und daraus werden Anker und Riemenmaß angepasst (wie im Reiter Kalibrieren, nur ohne Lineal). Übernommen wird erst auf Knopfdruck.'),
      h('div', { class: 'grid3' }, anx.el, any.el, ains.el), h('div', { class: 'grid2' }, goBtn, measBtn), gateNote, prog, step, stopBtn, out, applyBtn),
  );
  store.on(['camera', 'camTrack', 'autocal', 'state', 'job'], paint, { immediate: true });
  api.autocalState({ quiet: true }).then((s) => { if (s?.state && s.state !== 'idle') store.set({ autocal: s }); }).catch(() => {});
}
