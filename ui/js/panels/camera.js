// Camera tab: choose the source, live view with overlay, align the camera to the board. More parts in camera-*.js.
import { h, fmt } from '../util.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { run, toast, gate } from '../ui.js';
import { segmented, selectField, toggle, numField } from '../widgets.js';
import { mountCameraView } from '../camera-view.js';
import { refreshCamera, toImage } from '../camera.js';
import { mountCameraMore } from './camera-more.js';
import { mountAnchors } from './camera-anchors.js';
import { mountPeople } from './camera-people.js';

const sect = (title, ...kids) => h('div', { class: 'sect' }, h('div', { class: 'sect-h' }, h('h2', null, title)), ...kids);
const p = (text) => h('p', { class: 'muted', style: { fontSize: '14px' } }, text);
const CORNERS = ['top left', 'top right', 'bottom right', 'bottom left'];

export function mountCamera(root) {
  /* ---------- 1. source ---------- */
  const cur = () => store.get('camera')?.settings ?? {};
  const kind = segmented({ label: 'Camera source', fill: true, value: cur().source?.kind ?? 'none', options: [
    { value: 'none', label: 'Off' }, { value: 'device', label: 'Webcam' }, { value: 'url', label: 'Stream' }, { value: 'sim', label: 'Simulator', title: 'Virtual camera filming the simulator' }],
  onChange: (v) => setSource({ kind: v }) });
  const idx = numField({ label: 'Device no.', min: 0, max: 9, step: 1, value: cur().source?.index ?? 0, onChange: (v) => v != null && setSource({ kind: 'device', index: Math.round(v) }) });
  const res = selectField({ label: 'Resolution', value: '1920x1080', options: [['1920x1080', '1920 × 1080'], ['2560x1440', '2560 × 1440'], ['3840x2160', '3840 × 2160'], ['1280x720', '1280 × 720']],
    onChange: (v) => { const [w, hh] = v.split('x').map(Number); setSource({ width: w, height: hh }); } });
  const url = h('input', { type: 'text', placeholder: 'http://192.168.1.50:8080/video', 'aria-label': 'Stream address' });
  const urlBtn = h('button', { class: 'btn', type: 'button' }, 'Connect');
  urlBtn.addEventListener('click', () => run(urlBtn, () => setSource({ kind: 'url', url: url.value.trim() })));
  const rot = selectField({ label: 'Rotate', value: '0', options: [['0', '0°'], ['90', '90°'], ['180', '180°'], ['270', '270°']],
    onChange: (v) => put({ rotate: Number(v) }) });
  const mir = toggle({ label: 'Mirror', onChange: (v) => put({ mirror: v }) });
  const vfps = selectField({ label: 'Live view', value: '10', options: [['5', '5 fps'], ['10', '10 fps'], ['15', '15 fps'], ['30', 'max']],
    onChange: (v) => put({ view: { fps: Number(v) } }) });
  const devBtn = h('button', { class: 'btn', type: 'button' }, icon('refresh'), 'Find webcams');
  const devs = h('div', { class: 'cam-devs' });
  devBtn.addEventListener('click', () => run(devBtn, async () => {
    devs.replaceChildren(h('span', { class: 'muted' }, 'Searching, this takes a few seconds ...'));
    const list = await api.cameraDevices();
    devs.replaceChildren(...(list.length ? list.map((d) => h('button', { class: 'cam-dev', type: 'button', dataset: { index: String(d.index) },
      onClick: () => run(devBtn, () => setSource({ kind: 'device', index: d.index })) },
    h('img', { src: d.thumb, alt: `Preview of device ${d.index}` }), h('b', null, `Device ${d.index}`),
    h('span', { class: 'muted' }, d.blank ? 'no picture (virtual camera?)' : `${d.size[0]} × ${d.size[1]}`)))
      : [h('span', { class: 'muted' }, 'No webcam found. Plugged in? Then search again.')]));
  }));
  const status = h('div', { class: 'muted mono', role: 'status', style: { fontSize: '13px' } });
  const devRow = h('div', { class: 'row' }, idx.el, res.el, devBtn);
  const urlRow = h('div', { class: 'row' }, h('div', { class: 'field', style: { flex: '1 1 220px' } }, h('label', { class: 'field-label' }, 'Stream address (MJPEG, RTSP)'), url), urlBtn);

  async function put(patch) { store.set({ camera: await api.cameraPut(patch) }); }
  async function setSource(src) { await put({ source: { ...cur().source, ...src } }); setTimeout(refreshCamera, 800); setTimeout(refreshCamera, 2500); }

  /* ---------- 2. live view ---------- */
  const viewBox = h('div');
  const view = mountCameraView(viewBox);
  const ov = toggle({ label: 'Overlay', checked: true, onChange: (v) => view.setOverlay(v) });
  let visible = false;          // stream only while the view is on screen (independent of the tab layout)
  new IntersectionObserver((es) => { visible = es.some((e) => e.isIntersecting); paint(); }).observe(viewBox);

  /* ---------- 3. align to the board ---------- */
  const calInfo = h('div', { class: 'stats' });
  const calNote = h('div', { class: 'stack' });
  const autoBtn = h('button', { class: 'btn primary', type: 'button' }, 'Detect board');
  const mkBtn = h('button', { class: 'btn', type: 'button' }, 'Use markers');
  const handBtn = h('button', { class: 'btn', type: 'button' }, icon('target'), 'Set corners by hand');
  const clrBtn = h('button', { class: 'btn sm', type: 'button' }, icon('trash'), 'Clear alignment');
  const printLink = h('a', { class: 'btn sm', href: '/api/camera/markers.svg', target: '_blank', rel: 'noopener' }, icon('print'), 'Marker sheet');
  const editBox = h('div', { class: 'stack', hidden: true });
  let boardPts = [];

  mkBtn.addEventListener('click', () => run(mkBtn, async () => {
    const r = await api.cameraMarkers();
    toast(`Aligned with markers ${r.markers.join(', ')} (residual ${fmt(r.rms_mm, 2)} mm).`, 'ok');
    showWarnings(r.warnings); await refreshCamera();
  }));
  autoBtn.addEventListener('click', () => run(autoBtn, async () => {
    const r = await api.cameraSuggest();
    startEdit(r.image_points, r.board_points, 'Check the four points, drag them if needed, then apply.');
  }));
  handBtn.addEventListener('click', () => {
    const cam = store.get('camera'), b = store.get('config')?.board ?? { width_mm: 1500, height_mm: 1000 };
    const bp = [[0, b.height_mm], [b.width_mm, b.height_mm], [b.width_mm, 0], [0, 0]];
    const [w, hh] = cam?.source?.size ?? [1920, 1080];
    let ip = [[w * .2, hh * .2], [w * .8, hh * .2], [w * .8, hh * .8], [w * .2, hh * .8]];
    if (cam?.geometry) ip = bp.map(([x, y]) => toImage(cam.geometry, x, y) ?? [w / 2, hh / 2]);
    startEdit(ip, bp, 'Drag the four points onto the board corners (a magnifier appears while dragging).');
  });
  clrBtn.addEventListener('click', () => run(clrBtn, async () => { store.set({ camera: await api.cameraUncalibrate() }); }));

  function startEdit(imgPts, brdPts, hint) {
    boardPts = brdPts.map((q) => [...q]);
    view.edit(imgPts);
    const inputs = boardPts.map((q, i) => {
      const xi = h('input', { type: 'number', value: String(q[0]), step: 'any', 'aria-label': `Point ${i + 1} board x` });
      const yi = h('input', { type: 'number', value: String(q[1]), step: 'any', 'aria-label': `Point ${i + 1} board y` });
      xi.addEventListener('change', () => { boardPts[i][0] = Number(xi.value); });
      yi.addEventListener('change', () => { boardPts[i][1] = Number(yi.value); });
      return [h('span', null, CORNERS[i] ?? `Point ${i + 1}`), xi, yi];
    });
    const ok = h('button', { class: 'btn primary', type: 'button' }, 'Apply');
    const cancel = h('button', { class: 'btn', type: 'button', onClick: stopEdit }, 'Cancel');
    ok.addEventListener('click', () => run(ok, async () => {
      const r = await api.cameraCalibrate({ image_points: view.editPoints(), board_points: boardPts, method: 'corners' });
      toast(`Board aligned (residual ${fmt(r.rms_mm, 2)} mm).`, 'ok'); showWarnings(r.warnings);
      stopEdit(); await refreshCamera();
    }));
    const coords = h('details', null, h('summary', { class: 'muted' }, 'Board coordinates of the points'),
      h('div', { class: 'cam-pts' }, h('b', null, 'Point'), h('b', null, 'x (mm)'), h('b', null, 'y (mm)'), ...inputs.flat()));
    editBox.replaceChildren(h('div', { class: 'note' }, hint), coords, h('div', { class: 'grid2' }, ok, cancel));
    editBox.hidden = false;
  }
  function stopEdit() { view.edit(null); editBox.hidden = true; }
  function showWarnings(ws) { calNote.replaceChildren(...(ws ?? []).map((w) => h('div', { class: 'note warn' }, w))); }

  function paint() {
    const cam = store.get('camera'), s = cam?.settings, src = cam?.source;
    if (s) {
      kind.set(s.source.kind); idx.set(s.source.index); rot.select.value = String(s.rotate ?? 0); mir.set(!!s.mirror);
      vfps.select.value = String(s.view?.fps ?? 10);
      res.select.value = `${s.source.width}x${s.source.height}`;
      if (document.activeElement !== url) url.value = s.source.url ?? '';
    }
    const k = s?.source?.kind ?? 'none';
    for (const b of devs.querySelectorAll('.cam-dev')) b.setAttribute('aria-pressed', String(k === 'device' && Number(b.dataset.index) === s?.source?.index));
    devRow.hidden = k !== 'device'; devs.hidden = k !== 'device'; urlRow.hidden = k !== 'url';
    status.textContent = !src ? 'No camera active.' : src.error ? src.error
      : src.size ? `${src.size[0]} × ${src.size[1]} px, camera delivers ${fmt(src.fps, 0)} fps` : 'Waiting for the first picture ...';
    if (src && visible) view.start(); else view.stop();
    const g = cam?.geometry;
    const covPct = (() => {
      const c = cam?.coverage ?? [], b = store.get('config')?.board;
      if (c.length < 3 || !b) return 0;
      let a = 0; c.forEach((q, i) => { const n = c[(i + 1) % c.length]; a += q[0] * n[1] - n[0] * q[1]; });
      return Math.abs(a / 2) / (b.width_mm * b.height_mm) * 100;
    })();
    const cell = (v, l) => h('div', { class: 'stat' }, h('b', null, v), h('span', null, l));
    calInfo.replaceChildren(...(g ? [cell('yes', 'Aligned'), cell(`${fmt(covPct, 0)} %`, 'Board in view'), cell(`${fmt(g.rms_mm, 1)} mm`, 'Residual')]
      : [cell('no', 'Aligned')]));
    for (const b of [mkBtn, autoBtn, handBtn]) gate(b, src ? '' : 'Choose a camera first.');
    clrBtn.hidden = !g;
  }

  root.append(
    sect('1 Camera', kind.el, devRow, devs, urlRow, h('div', { class: 'grid3' }, rot.el, vfps.el, h('div', { style: { alignSelf: 'end' } }, mir.el)), status),
    sect('2 Live view', viewBox, ov.el,
      h('p', { class: 'field-hint' }, 'Orange: board edge · yellow: drawing area · green cross: pen (software) · blue circle: pen (camera).')),
    sect('3 Align to the board', calInfo, autoBtn, editBox, calNote,
      h('details', null, h('summary', { class: 'muted' }, 'Other ways'),
        h('div', { class: 'stack', style: { marginTop: '8px' } }, h('div', { class: 'grid2' }, handBtn, mkBtn),
          h('div', { class: 'row' }, printLink, clrBtn)))),
  );
  mountAnchors(root, sect, p, view);
  mountCameraMore(root, sect, p);
  mountPeople(root, sect, p);
  store.on(['camera', 'tab', 'moreOpen'], paint, { immediate: true });
  setInterval(() => { if (visible && store.get('camera')?.source) refreshCamera(); }, 3000);
}
