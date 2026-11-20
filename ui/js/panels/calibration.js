// Reiter "Kalibrieren": Messkreuze zeichnen, mit dem Lineal messen, Geometrie anpassen, übernehmen.
import { h, fmt, fmtDur, lsGet, lsSet } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, gate, toast, confirmDialog } from '../ui.js';
import { numField } from '../widgets.js';
import { caps } from '../caps.js';
import { mountTape } from './tape.js';

const FREE_LABEL = { anchor_left: 'Anker links', anchor_right: 'Anker rechts', scale: 'Riemenmaß links und rechts', scale_left: 'Riemenmaß links',
  scale_right: 'Riemenmaß rechts', pen_offset: 'Stiftversatz', radius: 'Scheibenradius', reference: 'Referenz' };
const num = (s) => { const t = String(s ?? '').trim().replace(',', '.'); if (!t) return null; const v = Number(t); return Number.isFinite(v) ? v : null; };
const f2 = (v) => fmt(v, 2);

export function mountCalibration(root) {
  const grid = { nx: 5, ny: 4, inset: 20, ...lsGet('wbot-calib-grid', {}) };
  let targets = [], pattern = null, rows = [], result = null, hot = null, applied = false;
  const sig = () => `${grid.nx}x${grid.ny}@${grid.inset}`;
  const saved = () => lsGet('wbot-calib', {});
  const sect = (title, ...kids) => h('div', { class: 'sect' }, h('div', { class: 'sect-h' }, h('h2', null, title)), ...kids);
  const note = () => h('div', { class: 'gate-note', role: 'status' });

  /* ---------- 1. Zeichnen ---------- */
  const nx = numField({ label: 'Spalten', min: 2, max: 15, step: 1, value: grid.nx, onChange: (v) => setGrid('nx', v) });
  const ny = numField({ label: 'Zeilen', min: 2, max: 11, step: 1, value: grid.ny, onChange: (v) => setGrid('ny', v) });
  const ins = numField({ label: 'Randabstand und Armlänge', unit: 'mm', min: 2, max: 20, step: 1, value: grid.inset, onChange: (v) => setGrid('inset', v) });
  const drawBtn = h('button', { class: 'btn primary', type: 'button' }, icon('pen'), 'Messkreuze zeichnen');
  const dryBtn = h('button', { class: 'btn', type: 'button' }, icon('play'), 'Trockenlauf');
  const drawNote = note();
  const bar = h('i'), progress = h('div', { class: 'progress' }, bar);
  const jobLine = h('div', { class: 'muted mono', style: { fontSize: '13px' } });
  const noPattern = h('div', { class: 'note warn', hidden: true }, 'Mit diesen Einstellungen kann der Server die Kreuze nicht zeichnen. Der Randabstand (zugleich Armlänge) muss zwischen 2 und 20 mm liegen.');

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
    } catch { /* Toast kam aus api.js */ }
  }

  async function start(dry) {
    if (!pattern) return;
    if (!dry && ['serial', 'wifi'].includes(store.get('state')?.connection?.transport)) {
      const ok = await confirmDialog({ title: 'Messkreuze zeichnen?', text: 'Die echte Maschine fährt los und zeichnet die Kreuze. Gondel und Brett frei? Mit Not-Halt (Leertaste) lässt sich jederzeit stoppen.', ok: 'Zeichnen starten' });
      if (!ok) return;
    }
    const job = await api.startJob({ pattern, dry_run: dry });
    store.set({ job });
    toast(dry ? 'Trockenlauf gestartet.' : 'Messkreuze werden gezeichnet.', 'ok');
  }
  drawBtn.addEventListener('click', () => run(drawBtn, () => start(false)));
  dryBtn.addEventListener('click', () => run(dryBtn, () => start(true)));

  /* ---------- 2. Messwerte ---------- */
  const table = h('div', { class: 'cal-table' });
  const count = h('div', { class: 'muted', role: 'status' });
  const clearBtn = h('button', { class: 'btn sm', type: 'button', onClick: () => {
    rows.forEach((r) => { r.xi.value = ''; r.yi.value = ''; }); const all = saved(); delete all[sig()]; lsSet('wbot-calib', all); changed();
  } }, icon('trash'), 'Messwerte löschen');

  const camBtn = h('button', { class: 'btn sm', type: 'button', title: 'Die Kamera misst die gezeichneten Kreuze (Reiter Kamera: erst einmessen). Gondel vorher aus dem Weg fahren.' }, icon('camera'), 'Mit Kamera messen');
  camBtn.addEventListener('click', () => run(camBtn, async () => {
    const r = await api.cameraMeasure({ targets, arm_mm: grid.inset });
    let n = 0;
    r.points.forEach((q, i) => { if (q.measured && rows[i]) { rows[i].xi.value = fmt(q.measured[0], 2); rows[i].yi.value = fmt(q.measured[1], 2); n++; } });
    const hidden = r.points.filter((q) => q.status === 'verdeckt').length;
    toast(`Kamera: ${n} von ${r.total} Kreuzen gemessen${hidden ? `, ${hidden} von der Gondel verdeckt (Gondel wegfahren und noch einmal)` : ''}.`, n ? 'ok' : 'error');
    for (const w of r.warnings ?? []) toast(w, 'info');
    changed();
  }));

  function renderTable() {
    const sv = saved()[sig()] ?? [];
    rows = targets.map((t, i) => {
      const xi = h('input', { type: 'text', inputmode: 'decimal', autocomplete: 'off', 'aria-label': `Punkt ${i + 1} gemessenes X`, value: sv[i]?.[0] ?? '' });
      const yi = h('input', { type: 'text', inputmode: 'decimal', autocomplete: 'off', 'aria-label': `Punkt ${i + 1} gemessenes Y`, value: sv[i]?.[1] ?? '' });
      const res = h('span', { class: 'res' });
      const tr = h('div', { class: 'cal-row', onMouseenter: () => setHot(i), onMouseleave: () => setHot(null), onFocusin: () => setHot(i), onFocusout: () => setHot(null) },
        h('span', { class: 'nr' }, i + 1), h('span', { class: 'soll' }, `${fmt(t[0], 0)} | ${fmt(t[1], 0)}`), xi, yi, res);
      xi.addEventListener('input', changed); yi.addEventListener('input', changed);
      return { tr, xi, yi, res };
    });
    table.replaceChildren(h('div', { class: 'cal-head' }, h('span', null, 'Nr'), h('span', null, 'Soll (mm)'), h('span', null, 'Gemessen X'), h('span', null, 'Gemessen Y'), h('span', { style: { textAlign: 'right' } }, 'Rest')),
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

  /* ---------- 3. Auswerten ---------- */
  const fitBtn = h('button', { class: 'btn primary', type: 'button' }, 'Auswerten');
  const fitNote = note();
  const out = h('div', { class: 'stack' });
  fitBtn.addEventListener('click', () => run(fitBtn, async () => {
    const { meas, idx } = collect();
    if (meas.length < 3) { toast('Mindestens 3 Punkte mit X und Y messen.', 'error'); return; }
    result = await api.calFit({ measurements: meas, free: null });
    result.rowIdx = idx; result.stale = false; applied = false;
    publish(); paintAll();
  }));

  const applyBtn = h('button', { class: 'btn', type: 'button' }, icon('save'), 'Korrektur übernehmen');
  applyBtn.addEventListener('click', () => run(applyBtn, async () => {
    const ok = await confirmDialog({ title: 'Korrektur übernehmen?', text: 'Die neuen Geometrie-Werte werden in die Konfiguration geschrieben und gespeichert. Alle künftigen Zeichnungen werden damit korrigiert. Die bisherigen Werte stehen im Vergleich oben.', ok: 'Übernehmen' });
    if (!ok) return;
    const { meas } = collect();
    const r = await api.calApply({ measurements: meas, free: null });
    if (r.config) store.set({ config: r.config });
    if (r.applied) { applied = true; toast('Korrektur übernommen und gespeichert.', 'ok'); }
    else toast('Es wurde nichts übernommen: Die Auswertung bringt keine klare Verbesserung.', 'info');
    if (r.result) { r.result.rowIdx = result?.rowIdx ?? []; r.result.stale = !!r.applied; result = r.result; }
    publish(); paintAll();
  }));
  const applyNote = note();
  const afterHint = h('div', { class: 'note ok', hidden: true }, 'Übernommen. Zum Prüfen: Messkreuze noch einmal zeichnen und neu messen. Der Rest sollte jetzt deutlich kleiner sein. Die alten Messwerte gelten nicht mehr: bitte „Messwerte löschen“.');

  function diffRows(patch, cfg) {
    const g = patch?.geometry ?? {}, o = cfg?.geometry ?? {}, d = [];
    const xy = (v) => `${fmt(v[0], 2)} | ${fmt(v[1], 2)}`;
    const pair = (label, a, b, unit = 'mm') => d.push(h('dt', null, label), h('dd', null, `${xy(a)} → `, h('b', null, xy(b)), ` ${unit}`));
    if (g.anchor_left) pair('Anker links', o.anchor_left, g.anchor_left);
    if (g.anchor_right) pair('Anker rechts', o.anchor_right, g.anchor_right);
    const sc = (label, a, b) => d.push(h('dt', null, label), h('dd', null, `${fmt(a, 5)} → `, h('b', null, fmt(b, 5)), ` (${b >= a ? '+' : ''}${fmt((b / a - 1) * 100, 3)} %)`));
    if (g.belt_scale_left != null) sc('Maßstab links', o.belt_scale_left, g.belt_scale_left);
    if (g.belt_scale_right != null) sc('Maßstab rechts', o.belt_scale_right, g.belt_scale_right);
    if (g.pen_offset_mm) pair('Stiftversatz', o.pen_offset_mm, g.pen_offset_mm);
    if (g.pulley_pitch_radius_mm != null) d.push(h('dt', null, 'Scheibenradius'), h('dd', null, `${fmt(o.pulley_pitch_radius_mm, 3)} → `, h('b', null, fmt(g.pulley_pitch_radius_mm, 3)), ' mm'));
    return d;
  }

  function paintResult() {
    out.replaceChildren();
    afterHint.hidden = !applied;
    if (!result) return;
    const r = result, cfg = store.get('config');
    const cell = (v, l) => h('div', { class: 'stat' }, h('b', null, v), h('span', null, l));
    if (r.stale) out.append(h('div', { class: 'note warn' }, applied ? 'Diese Auswertung wurde übernommen.' : 'Die Messwerte wurden geändert. Bitte neu auswerten.'));
    out.append(h('div', { class: 'stats' }, cell(`${f2(r.rms_before)} → ${f2(r.rms_after)} mm`, 'Effektivwert (RMS) vorher → nachher'),
      cell(`${f2(r.max_before)} → ${f2(r.max_after)} mm`, 'Größter Fehler vorher → nachher'), cell(String(r.n), 'Punkte ausgewertet'),
      cell(r.improved ? 'ja' : 'nein', 'klare Verbesserung')));
    for (const w of r.warnings ?? []) out.append(h('div', { class: 'note warn' }, w));
    const names = (r.free ?? []).map((k) => FREE_LABEL[k] ?? k);
    out.append(h('p', { class: 'muted', style: { fontSize: '14px' } }, `Angepasst werden: ${names.length ? names.join(', ') : 'nichts'}.`));
    const d = diffRows(r.patch, cfg);
    const [cx, cy] = r.reference_correction ?? [0, 0];
    if (cx || cy) d.push(h('dt', null, 'Referenz-Korrektur'), h('dd', null, h('b', null, `${fmt(cx, 2)} | ${fmt(cy, 2)}`), ' mm'));
    if (d.length) {
      out.append(h('h3', null, 'Vorgeschlagene Änderungen'), h('dl', { class: 'diff' }, ...d),
        h('p', { class: 'field-hint' }, 'Die Firmware-Anker in der Maschine bleiben unverändert. Nur die Host-Geometrie wird korrigiert.'));
    } else out.append(h('div', { class: 'note danger' }, 'Kein Korrekturvorschlag. Die Konfiguration bleibt, wie sie ist.'));
    if (r.outliers?.length) out.append(h('div', { class: 'note danger' }, `Auffällige Punkte (rot in der Tabelle): ${r.outliers.map((k) => (r.rowIdx?.[k] ?? k) + 1).join(', ')}. Bitte diese Messwerte nachmessen.`));
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
    const noRef = pattern ? c.abs : 'Mit diesen Einstellungen nicht zeichenbar.';
    gate(drawBtn, noRef); gate(dryBtn, noRef); drawNote.textContent = noRef;
    noPattern.hidden = !!pattern || !targets.length;
    count.textContent = `${meas.length} von ${targets.length} Punkten gemessen. Leere Zeilen werden übersprungen. Mindestens 3, besser 12 bis 20.`;
    const fitWhy = !c.connected ? 'Nicht verbunden. Die Auswertung braucht die Referenz der Maschine.' : !c.referenced ? 'Keine Referenz gesetzt.' : meas.length < 3 ? 'Mindestens 3 Punkte mit X und Y eintragen.' : '';
    gate(fitBtn, fitWhy); fitNote.textContent = fitWhy;
    const cam = store.get('camera');
    gate(camBtn, !cam?.calibrated ? 'Erst im Reiter Kamera eine Kamera wählen und einmessen.' : '');
    const hasFix = !!result && !result.stale && !applied && (Object.keys(result.patch ?? {}).length || result.reference_correction?.some((v) => v));
    const applyWhy = !result ? 'Erst auswerten.' : result.stale ? 'Messwerte geändert oder bereits übernommen. Erst neu auswerten.' : !hasFix ? 'Kein Korrekturvorschlag vorhanden.' : '';
    gate(applyBtn, applyWhy); applyBtn.classList.toggle('primary', !applyWhy); applyNote.textContent = applyWhy;
    paintResult(); paintRows();
    const job = store.get('job');
    if (job && ['planning', 'running', 'paused'].includes(job.state) && /Messkreuz/i.test(job.name ?? '')) {
      bar.style.transform = `scaleX(${Math.min(1, job.progress ?? 0)})`; jobLine.textContent = `${fmt((job.progress ?? 0) * 100, 0)} %, noch ca. ${fmtDur(job.eta_s)}`;
    } else { bar.style.transform = 'scaleX(0)'; jobLine.textContent = ''; }
  }

  /* ---------- Aufbau ---------- */
  root.append(
    sect('1 Messkreuze zeichnen',
      h('p', { class: 'muted', style: { fontSize: '14px' } }, 'Die Maschine zeichnet ein Raster aus kleinen Kreuzen. Danach misst du ihre Mitten mit Lineal oder Maßband nach. Zuerst die Referenz setzen und die Gondel in den Zeichenbereich fahren, z. B. mit „Park“ (Reiter Fahren).'),
      h('div', { class: 'grid2' }, nx.el, ny.el), h('div', { class: 'grid2' }, ins.el, h('span')), noPattern,
      h('div', { class: 'grid2' }, drawBtn, dryBtn), drawNote, progress, jobLine),
    sect('2 Messwerte eintragen',
      h('p', { class: 'muted', style: { fontSize: '14px' } }, 'Miss den Abstand jeder Kreuzmitte von der linken unteren Ecke des Zeichenbereichs: X nach rechts, Y nach oben, in mm. Die nummerierten Markierungen auf der Brettansicht zeigen, welche Zeile zu welchem Kreuz gehört. Komma oder Punkt sind erlaubt.'),
      table, count, h('div', { class: 'row' }, clearBtn, camBtn)),
    sect('3 Auswerten', fitBtn, fitNote, out),
    sect('4 Übernehmen', applyBtn, applyNote, afterHint));
  mountTape(root, sect);

  store.on(['state', 'job', 'config', 'camera'], paintAll);
  loadTargets();
}
