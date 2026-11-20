// Reiter "Kamera": Quelle wählen, Livebild mit Overlay, Kamera auf die Tafel einmessen. Weitere Teile in camera-more.js.
import { h, fmt } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, toast, gate } from '../ui.js';
import { segmented, selectField, toggle, numField } from '../widgets.js';
import { mountCameraView } from '../camera-view.js';
import { refreshCamera, toImage } from '../camera.js';
import { mountCameraMore } from './camera-more.js';

const sect = (title, ...kids) => h('div', { class: 'sect' }, h('div', { class: 'sect-h' }, h('h2', null, title)), ...kids);
const p = (text) => h('p', { class: 'muted', style: { fontSize: '14px' } }, text);

export function mountCamera(root) {
  /* ---------- 1. Quelle ---------- */
  const cur = () => store.get('camera')?.settings ?? {};
  const kind = segmented({ label: 'Kameraquelle', fill: true, value: cur().source?.kind ?? 'none', options: [
    { value: 'none', label: 'Aus' }, { value: 'device', label: 'Gerät' }, { value: 'url', label: 'Stream' }, { value: 'sim', label: 'Simulator', title: 'Künstliche Kamera, die den Simulator filmt' }],
  onChange: (v) => setSource({ kind: v }) });
  const idx = numField({ label: 'Geräte-Nr.', min: 0, max: 9, step: 1, value: cur().source?.index ?? 0, onChange: (v) => v != null && setSource({ kind: 'device', index: Math.round(v) }) });
  const res = selectField({ label: 'Auflösung', value: '1920x1080', options: [['1920x1080', '1920 × 1080'], ['1280x720', '1280 × 720'], ['3840x2160', '3840 × 2160']],
    onChange: (v) => { const [w, hh] = v.split('x').map(Number); setSource({ width: w, height: hh }); } });
  const url = h('input', { type: 'text', placeholder: 'http://192.168.1.50:8080/video', 'aria-label': 'Stream-Adresse' });
  const urlBtn = h('button', { class: 'btn', type: 'button' }, 'Verbinden');
  urlBtn.addEventListener('click', () => run(urlBtn, () => setSource({ kind: 'url', url: url.value.trim() })));
  const rot = selectField({ label: 'Drehen', value: '0', options: [['0', '0°'], ['90', '90°'], ['180', '180°'], ['270', '270°']],
    onChange: (v) => put({ rotate: Number(v) }) });
  const mir = toggle({ label: 'Bild spiegeln', onChange: (v) => put({ mirror: v }) });
  const devBtn = h('button', { class: 'btn', type: 'button' }, icon('refresh'), 'Geräte suchen');
  const devs = h('div', { class: 'cam-devs' });
  devBtn.addEventListener('click', () => run(devBtn, async () => {
    devs.replaceChildren(h('span', { class: 'muted' }, 'Suche läuft, das dauert ein paar Sekunden ...'));
    const list = await api.cameraDevices();
    devs.replaceChildren(...(list.length ? list.map((d) => h('button', { class: 'cam-dev', type: 'button', 'aria-pressed': String(cur().source?.kind === 'device' && cur().source?.index === d.index),
      onClick: () => run(devBtn, () => setSource({ kind: 'device', index: d.index })) },
    h('img', { src: d.thumb, alt: `Vorschau Gerät ${d.index}` }), h('b', null, `Gerät ${d.index}`), h('span', { class: 'muted' }, `${d.size[0]} × ${d.size[1]}, ${d.backend}`)))
      : [h('span', { class: 'muted' }, 'Keine Kamera gefunden. Webcam eingesteckt? Dann erneut suchen.')]));
  }));
  const status = h('div', { class: 'muted mono', role: 'status', style: { fontSize: '13px' } });
  const devRow = h('div', { class: 'row' }, idx.el, res.el, devBtn);
  const urlRow = h('div', { class: 'row' }, h('div', { class: 'field', style: { flex: '1 1 220px' } }, h('label', { class: 'field-label' }, 'Stream-Adresse (MJPEG, RTSP)'), url), urlBtn);

  async function put(patch) { store.set({ camera: await api.cameraPut(patch) }); }
  async function setSource(src) { await put({ source: { ...cur().source, ...src } }); setTimeout(refreshCamera, 800); setTimeout(refreshCamera, 2500); }

  /* ---------- 2. Livebild ---------- */
  const viewBox = h('div');
  const view = mountCameraView(viewBox);
  const ov = toggle({ label: 'Overlay: Tafel, Zeichenfläche, Vorschau, Stift', checked: true, onChange: (v) => view.setOverlay(v) });

  /* ---------- 3. Einmessen ---------- */
  const calInfo = h('div', { class: 'stats' });
  const calNote = h('div', { class: 'stack' });
  const mkBtn = h('button', { class: 'btn primary', type: 'button' }, 'Marker erkennen');
  const autoBtn = h('button', { class: 'btn', type: 'button' }, 'Tafel automatisch finden');
  const handBtn = h('button', { class: 'btn', type: 'button' }, icon('target'), 'Ecken von Hand');
  const clrBtn = h('button', { class: 'btn sm', type: 'button' }, icon('trash'), 'Einmessung löschen');
  const printLink = h('a', { class: 'btn sm', href: '/api/camera/markers.svg', target: '_blank', rel: 'noopener' }, icon('print'), 'Marker-Blatt drucken');
  const editBox = h('div', { class: 'stack', hidden: true });
  let boardPts = [];

  mkBtn.addEventListener('click', () => run(mkBtn, async () => {
    const r = await api.cameraMarkers();
    toast(`Eingemessen mit Marker ${r.markers.join(', ')} (Restfehler ${fmt(r.rms_mm, 2)} mm).`, 'ok');
    showWarnings(r.warnings); await refreshCamera();
  }));
  autoBtn.addEventListener('click', () => run(autoBtn, async () => {
    const r = await api.cameraSuggest();
    startEdit(r.image_points, r.board_points, 'Vorschlag: Eckpunkte prüfen und an die Tafelecken ziehen, dann übernehmen.');
  }));
  handBtn.addEventListener('click', () => {
    const cam = store.get('camera'), b = store.get('config')?.board ?? { width_mm: 1500, height_mm: 1000 };
    const bp = [[0, b.height_mm], [b.width_mm, b.height_mm], [b.width_mm, 0], [0, 0]];
    const [w, hh] = cam?.source?.size ?? [1920, 1080];
    let ip = [[w * .2, hh * .2], [w * .8, hh * .2], [w * .8, hh * .8], [w * .2, hh * .8]];
    if (cam?.geometry) ip = bp.map(([x, y]) => toImage(cam.geometry, x, y) ?? [w / 2, hh / 2]);
    startEdit(ip, bp, 'Die vier Punkte auf die Tafelecken ziehen (beim Ziehen erscheint eine Lupe). Die Tafel-Koordinaten darunter gehören zu den Punkten.');
  });
  clrBtn.addEventListener('click', () => run(clrBtn, async () => { store.set({ camera: await api.cameraUncalibrate() }); }));

  function startEdit(imgPts, brdPts, hint) {
    boardPts = brdPts.map((q) => [...q]);
    view.edit(imgPts);
    const inputs = boardPts.map((q, i) => {
      const xi = h('input', { type: 'number', value: String(q[0]), step: 'any', 'aria-label': `Punkt ${i + 1} Tafel x` });
      const yi = h('input', { type: 'number', value: String(q[1]), step: 'any', 'aria-label': `Punkt ${i + 1} Tafel y` });
      xi.addEventListener('change', () => { boardPts[i][0] = Number(xi.value); });
      yi.addEventListener('change', () => { boardPts[i][1] = Number(yi.value); });
      return [h('span', null, ['oben links', 'oben rechts', 'unten rechts', 'unten links'][i] ?? `Punkt ${i + 1}`), xi, yi];
    });
    const ok = h('button', { class: 'btn primary', type: 'button' }, 'Übernehmen');
    const cancel = h('button', { class: 'btn', type: 'button', onClick: stopEdit }, 'Abbrechen');
    ok.addEventListener('click', () => run(ok, async () => {
      const r = await api.cameraCalibrate({ image_points: view.editPoints(), board_points: boardPts, method: 'corners' });
      toast(`Eingemessen (Restfehler ${fmt(r.rms_mm, 2)} mm).`, 'ok'); showWarnings(r.warnings);
      stopEdit(); await refreshCamera();
    }));
    editBox.replaceChildren(h('div', { class: 'note' }, hint), h('div', { class: 'cam-pts' }, h('b', null, 'Punkt'), h('b', null, 'Tafel x (mm)'), h('b', null, 'Tafel y (mm)'), ...inputs.flat()),
      h('p', { class: 'field-hint' }, 'Standard: die Ecken der Schreibfläche (0 | 0 ist unten links). Wer andere gut sichtbare Punkte nimmt, trägt deren Koordinaten ein.'),
      h('div', { class: 'grid2' }, ok, cancel));
    editBox.hidden = false;
  }
  function stopEdit() { view.edit(null); editBox.hidden = true; }
  function showWarnings(ws) { calNote.replaceChildren(...(ws ?? []).map((w) => h('div', { class: 'note warn' }, w))); }

  function paint() {
    const cam = store.get('camera'), s = cam?.settings, src = cam?.source;
    if (s) {
      kind.set(s.source.kind); idx.set(s.source.index); rot.select.value = String(s.rotate ?? 0); mir.set(!!s.mirror);
      res.select.value = `${s.source.width}x${s.source.height}`;
      if (document.activeElement !== url) url.value = s.source.url ?? '';
    }
    const k = s?.source?.kind ?? 'none';
    devRow.hidden = k !== 'device'; devs.hidden = k !== 'device'; urlRow.hidden = k !== 'url';
    status.textContent = !src ? 'Keine Kamera aktiv.' : src.error ? src.error
      : src.size ? `${src.size[0]} × ${src.size[1]} px, ${fmt(src.fps, 1)} Bilder/s` : 'Warte auf das erste Bild ...';
    if (src && store.get('tab') === 'kamera') view.start(); else view.stop();
    const g = cam?.geometry;
    const covPct = (() => {
      const c = cam?.coverage ?? [], b = store.get('config')?.board;
      if (c.length < 3 || !b) return 0;
      let a = 0; c.forEach((q, i) => { const n = c[(i + 1) % c.length]; a += q[0] * n[1] - n[0] * q[1]; });
      return Math.abs(a / 2) / (b.width_mm * b.height_mm) * 100;
    })();
    const cell = (v, l) => h('div', { class: 'stat' }, h('b', null, v), h('span', null, l));
    calInfo.replaceChildren(...(g ? [cell(`${fmt(g.rms_mm, 2)} mm`, 'Restfehler Einmessung'), cell(`${fmt(covPct, 0)} %`, 'Tafel im Bild'),
      cell(g.focal_estimated ? 'geschätzt' : 'kalibriert', 'Objektiv'), cell(s?.calib?.method === 'markers' ? 'Marker' : 'Ecken', 'Methode')]
      : [cell('nein', 'eingemessen')]));
    for (const b of [mkBtn, autoBtn, handBtn]) gate(b, src ? '' : 'Erst eine Kamera wählen.');
    clrBtn.hidden = !g;
  }

  root.append(
    sect('1 Kamera wählen', p('Eine Webcam am PC (einfach per USB einstecken und „Geräte suchen“), eine Stream-Adresse (z. B. Handy-App „IP Webcam“) oder die Simulator-Kamera. Die Kamera darf schräg stehen: das Bild wird auf die Tafel entzerrt.'),
      kind.el, devRow, devs, urlRow, h('div', { class: 'grid2' }, rot.el, h('div', { style: { alignSelf: 'end' } }, mir.el)), status),
    sect('2 Livebild', viewBox, ov.el,
      h('p', { class: 'field-hint' }, 'Orange: Tafelrand, gelb gestrichelt: erlaubte Zeichenfläche, grünes Kreuz: Stift laut Software, blauer Kreis: Stift laut Kamera (Gondel-Marker). In der Brettansicht oben schaltet „Kamera“ das entzerrte Bild unter die Zeichnung.')),
    sect('3 Auf die Tafel einmessen',
      p('Am genauesten mit vier ArUco-Markern an der Tafel (Blatt drucken, Marker mit der Mitte auf die angegebenen Koordinaten kleben). Ohne Marker: Tafel automatisch finden oder die vier Ecken von Hand ziehen.'),
      calInfo, h('div', { class: 'grid2' }, mkBtn, autoBtn), h('div', { class: 'grid2' }, handBtn, printLink), editBox, calNote, h('div', { class: 'row' }, clrBtn)),
  );
  mountCameraMore(root, sect, p);
  store.on(['camera', 'tab'], paint, { immediate: true });
  setInterval(() => { if (store.get('tab') === 'kamera' && store.get('camera')?.source) refreshCamera(); }, 3000);
}
