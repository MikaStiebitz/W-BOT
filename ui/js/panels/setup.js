// Setup tab: one click connects, finds the camera view, the whiteboard and the motors, and sets the gondola position.
import { h, fmt, sleep } from '../util.js';
import { icon } from '../icons.js';
import { store, viewing } from '../store.js';
import { api } from '../api.js';
import { run, gate, toast, notice, confirmDialog } from '../ui.js';
import { segmented, numField } from '../widgets.js';
import { caps } from '../caps.js';
import { connectBody } from '../simopts.js';
import { mountCameraView } from '../camera-view.js';
import { refreshCamera } from '../camera.js';

const xy = (v) => `${fmt(v[0], 0)} | ${fmt(v[1], 0)}`;
const MARK = { idle: '○', run: '…', ok: '✓', warn: '!', err: '✕' };

export function mountSetup(root) {
  const sect = (title, ...kids) => h('div', { class: 'sect' }, title ? h('div', { class: 'sect-h' }, h('h2', null, title)) : null, ...kids);

  /* ---------- Steps ---------- */
  const rows = {};
  const mkRow = (key, label) => {
    const mark = h('span', { class: 'st-mark', 'aria-hidden': 'true' }, MARK.idle), text = h('span', { class: 'st-text muted' });
    rows[key] = { el: h('li', { class: 'st', dataset: { s: 'idle' } }, mark, h('span', { class: 'st-l' }, label), text), mark, text };
    return rows[key].el;
  };
  const list = h('ul', { class: 'steps' }, mkRow('connect', 'Connect'), mkRow('camera', 'Camera'), mkRow('board', 'Whiteboard'), mkRow('motors', 'Motors'), mkRow('position', 'Gondola position'));
  function mark(key, state, text = '') {
    const r = rows[key]; r.el.dataset.s = state; r.mark.textContent = MARK[state]; r.text.textContent = text;
    if (state === 'run') r.mark.classList.add('spin'); else r.mark.classList.remove('spin');
  }
  const resetRows = () => Object.keys(rows).forEach((k) => mark(k, 'idle', ''));

  const goBtn = h('button', { class: 'btn primary big', type: 'button' }, icon('target'), 'Set up automatically');
  const goNote = h('div', { class: 'gate-note', role: 'status' });

  /* ---------- Camera source ---------- */
  const cur = () => store.get('camera')?.settings?.source ?? { kind: 'none' };
  const kind = segmented({ label: 'Camera source', fill: true, value: cur().kind ?? 'none', options: [
    { value: 'device', label: 'Webcam' }, { value: 'url', label: 'Stream' }, { value: 'sim', label: 'Simulator', title: 'A virtual camera that films the simulator' }, { value: 'none', label: 'Off' }],
  onChange: (v) => (v === 'device' ? findDevices() : setSource({ kind: v })) });
  const devs = h('div', { class: 'cam-devs' });
  const url = h('input', { type: 'text', placeholder: 'http://192.168.1.50:8080/video', 'aria-label': 'Stream address' });
  const urlBtn = h('button', { class: 'btn', type: 'button' }, 'Use stream');
  urlBtn.addEventListener('click', () => run(urlBtn, () => setSource({ kind: 'url', url: url.value.trim() })));
  const urlRow = h('div', { class: 'row' }, h('div', { class: 'field', style: { flex: '1 1 200px' } }, url), urlBtn);
  const camStatus = h('div', { class: 'muted', role: 'status', style: { fontSize: '13px' } });

  async function setSource(src) {
    store.set({ camera: await api.cameraPut({ source: { ...cur(), ...src } }) });
    await waitForFrame();
  }
  async function waitForFrame(ms = 14000) {
    const t = Date.now();
    while (Date.now() - t < ms) {
      const c = await refreshCamera();
      if (c?.source?.size) return true;
      if (c?.source?.error) return false;
      await sleep(700);
    }
    return false;
  }
  let devList = [];
  function paintDevs() {
    devs.replaceChildren(...(devList.length ? devList.map((d) => h('button', { class: 'cam-dev', type: 'button', 'aria-pressed': String(cur().kind === 'device' && cur().index === d.index),
      onClick: (e) => run(e.currentTarget, () => setSource({ kind: 'device', index: d.index, width: 1920, height: 1080 })) },
    h('img', { src: d.thumb, alt: `Preview of camera ${d.index}` }), h('b', null, `Camera ${d.index}`), h('span', { class: 'muted' }, d.blank ? 'no picture (virtual?)' : `${d.size[0]} × ${d.size[1]}`))) : []));
  }
  async function findDevices() {
    devs.replaceChildren(h('span', { class: 'muted' }, 'Looking for cameras … (a few seconds)'));
    try {
      devList = await api.cameraDevices();
      if (!devList.length) devs.replaceChildren(h('span', { class: 'muted' }, 'No camera found. Plug in a webcam and try again.')); else paintDevs();
    } catch { devs.replaceChildren(); }
    return devList;
  }
  const camBox = h('details', { class: 'more' }, h('summary', null, 'Camera source'),
    h('div', { class: 'stack more-body' }, kind.el, devs, urlRow, camStatus));
  camBox.addEventListener('toggle', () => { if (camBox.open && cur().kind === 'device' && !devList.length) findDevices(); });   // previews of every camera, black ones marked

  /* ---------- Live view + motor result ---------- */
  const viewBox = h('div');
  const view = mountCameraView(viewBox);
  const result = h('div', { class: 'stack', hidden: true });
  let motors = null;

  function showMotors() {
    result.replaceChildren();
    result.hidden = !motors;
    if (!motors) return;
    const m = motors;
    const big = Math.max(...(m.deviation_mm ?? [0]));
    result.append(
      h('div', { class: 'stats' },
        h('div', { class: 'stat' }, h('b', null, xy(m.anchor_left)), h('span', null, `Left motor (config ${xy(m.config_left)})`)),
        h('div', { class: 'stat' }, h('b', null, xy(m.anchor_right)), h('span', null, `Right motor (config ${xy(m.config_right)})`)),
        h('div', { class: 'stat' }, h('b', null, `± ${fmt(m.uncertainty_mm, 0)} mm`), h('span', null, 'Measurement accuracy'))),
      h('p', { class: 'muted', style: { fontSize: '14px' } }, m.applied ? 'Applied. The drawing geometry now uses these positions.'
        : `The saved motor positions are off by ${fmt(big, 0)} mm. Drag the two points in the picture if they are not on the pulley centres, then apply.`),
      ...(m.warnings ?? []).map((w) => h('div', { class: 'note warn' }, w)),
      m.applied ? null : h('div', { class: 'grid2' }, applyBtn, remeasureBtn));
  }
  const applyBtn = h('button', { class: 'btn primary', type: 'button' }, icon('save'), 'Apply motor positions');
  const remeasureBtn = h('button', { class: 'btn', type: 'button' }, 'Measure again');
  remeasureBtn.addEventListener('click', () => run(remeasureBtn, async () => {
    const pts = view.editPoints();
    if (!pts || pts.length < 2) { toast('Place both points on the motor pulleys first.', 'error'); return; }
    const [a, b] = pts[0][0] <= pts[1][0] ? pts : [pts[1], pts[0]];
    motors = { ...(await api.cameraAnchors({ left: a, right: b })), image_points: [a, b] };
    showMotors();
  }));
  applyBtn.addEventListener('click', () => run(applyBtn, async () => {
    const pts = motors.image_points;
    const ok = await confirmDialog({ title: 'Apply the measured motor positions?',
      text: `Left ${xy(motors.config_left)} → ${xy(motors.anchor_left)} mm, right ${xy(motors.config_right)} → ${xy(motors.anchor_right)} mm. Every drawing is pre-distorted with these positions from now on.${motors.area?.rect ? ` The drawing area is adjusted to fit: X ${fmt(motors.area.rect[0], 0)}…${fmt(motors.area.rect[2], 0)}, Y ${fmt(motors.area.rect[1], 0)}…${fmt(motors.area.rect[3], 0)} mm.` : ''}`, ok: 'Apply' });
    if (!ok) return;
    const res = await api.cameraAnchors({ left: pts[0], right: pts[1], apply: true });
    if (res.config) store.set({ config: res.config });
    motors = { ...motors, applied: true };
    view.edit(null);
    mark('motors', 'ok', `${xy(res.anchor_left ?? motors.anchor_left)} | ${xy(res.anchor_right ?? motors.anchor_right)} mm`);
    showMotors();
    toast('Motor positions applied.', 'ok');
  }));

  /* ---------- Gondola position ---------- */
  const px = numField({ label: 'Gondola X', unit: 'mm', step: 1, value: 750 });
  const py = numField({ label: 'Gondola Y', unit: 'mm', step: 1, value: 500 });
  const posBtn = h('button', { class: 'btn', type: 'button' }, icon('target'), 'Set gondola position');
  posBtn.addEventListener('click', () => run(posBtn, async () => {
    const x = px.get(), y = py.get();
    if (x == null || y == null) { toast('Enter the X and Y where the gondola hangs.', 'error'); return; }
    await api.reference(x, y);
    toast('Gondola position set.', 'ok');
  }));
  const findBtn = h('button', { class: 'btn', type: 'button', title: 'Uses the camera: finds where the two belts meet' }, icon('camera'), 'Find with the camera');
  findBtn.addEventListener('click', () => run(findBtn, async () => {
    await api.gondolaLocate(true);
    const p = store.get('state')?.reference?.physical;
    mark('position', 'ok', p ? `X ${fmt(p[0], 0)} · Y ${fmt(p[1], 0)} mm (camera)` : 'set');
    toast('Gondola position set from the camera.', 'ok');
  }));
  const posBox = h('details', { class: 'more' }, h('summary', null, 'Gondola position'),
    h('div', { class: 'stack more-body' }, h('p', { class: 'muted', style: { fontSize: '14px' } }, 'Tell the software where the gondola hangs right now, find it with the camera, or click that spot on the board.'),
      findBtn, h('div', { class: 'grid2' }, px.el, py.el), posBtn));

  async function stepPosition() {
    const s = store.get('state');
    if (!s?.reference?.set) {
      let at = null;
      const tr = store.get('camTrack');
      if (s?.connection?.transport === 'sim') {
        const info = store.get('simInfo') ?? await api.simulator({ quiet: true }).catch(() => null);
        at = info?.physical ?? s?.reference?.boot_physical ?? [0, 0];
      } else {
        try { await api.gondolaLocate(true, { quiet: true }); }          // the camera sees where the belts meet
        catch { if (tr?.pen && Date.now() / 1000 - tr.t < 3) at = tr.pen; }
        if (store.get('state')?.reference?.set) { const p = store.get('state').reference.physical; mark('position', 'ok', `X ${fmt(p[0], 0)} · Y ${fmt(p[1], 0)} mm (camera)`); return true; }
      }
      if (!at) { mark('position', 'warn', 'Click where the gondola hangs on the board, or enter it below.'); posBox.open = true; return false; }
      try { await api.reference(at[0], at[1]); }
      catch (e) { mark('position', 'err', 'Rejected (see the message). Enter where the gondola really hangs below.'); posBox.open = true; return false; }
    }
    const p = store.get('state')?.reference?.physical;
    mark('position', 'ok', p ? `X ${fmt(p[0], 0)} · Y ${fmt(p[1], 0)} mm` : 'set');
    return true;
  }

  const fallback = h('div', { class: 'note warn', hidden: true },
    h('span', { class: 'grow' }, 'The board could not be found automatically. Make sure all four corners are in the picture and well lit, or mark the corners by hand.'),
    h('button', { class: 'btn sm', type: 'button', onClick: () => window.dispatchEvent(new CustomEvent('wbot:open-more', { detail: 'kamera' })) }, 'Mark corners by hand'));
  const showFallback = () => { fallback.hidden = false; };

  /* ---------- The one-click run ---------- */
  async function autoSetup() {
    resetRows(); motors = null; showMotors(); view.edit(null); fallback.hidden = true;
    // 1 connect
    let s = store.get('state');
    mark('connect', 'run');
    if (s?.connection?.status !== 'connected') {
      try { await api.connect(connectBody()); } catch { mark('connect', 'err', 'Connecting failed.'); return; }
      s = store.get('state');
    }
    mark('connect', 'ok', s?.connection?.transport === 'sim' ? 'Simulator' : (s?.connection?.port || s?.connection?.host || 'connected'));
    // 2 + 3 + 4 camera, board and motors. The server keeps a working camera or picks the first real webcam
    // (black virtual cameras are skipped); only the simulator falls back to its virtual camera.
    mark('camera', 'run', 'Looking for a camera …'); mark('board', 'run'); mark('motors', 'run');
    let r, cam;
    const attempt = async () => { r = await api.autoSetup(); cam = await refreshCamera(); };
    try {
      await attempt();
      // the virtual camera is only a fallback when nothing real was ever chosen: a chosen webcam is never swapped out
      const chosen = cam?.settings?.source?.kind ?? 'none';
      if ((!cam?.source?.size || cam?.source?.error) && s?.connection?.transport === 'sim' && (chosen === 'none' || chosen === 'sim')) { await setSource({ kind: 'sim' }); await attempt(); }
    } catch { mark('camera', 'err', 'Detection failed.'); mark('board', 'idle'); mark('motors', 'idle'); return; }
    if (!cam?.source?.size) {
      mark('camera', 'err', r?.errors?.[0] || cam?.source?.error || 'No picture from any camera. Check the webcam or choose a stream.');
      mark('board', 'idle'); mark('motors', 'idle'); camBox.open = true; return;
    }
    const sk = cam.settings?.source;
    mark('camera', 'ok', `${sk?.kind === 'device' ? `Camera ${sk.index}` : sk?.kind === 'sim' ? 'Simulator camera' : 'Stream'} · ${cam.source.size[0]} × ${cam.source.size[1]}`);
    if (r.board) mark('board', 'ok', `error ${fmt(r.board.rms_mm, 1)} mm`);
    else { mark('board', 'err', r.errors?.[0] || 'The whiteboard was not found.'); showFallback(); }
    if (r.motors) {
      motors = r.motors; showMotors();
      view.edit(r.motors.image_points, { labels: ['Left motor', 'Right motor'], max: 2 });
      mark('motors', 'warn', 'Found. Check the points, then apply.');
    } else mark('motors', r.board ? 'err' : 'idle', r.board ? (r.errors?.[0] || 'The motors were not found.') : '');
    // 5 position
    await stepPosition();
    if (r.board && r.motors) notice('setup', 'Almost done: check the motor points in the picture and apply them.', 'info');
  }
  goBtn.addEventListener('click', () => run(goBtn, autoSetup));

  function paint() {
    const st = store.get('state'), c = caps(st, store.get('job')), cam = store.get('camera');
    gate(goBtn, c.alarm ? 'The machine is in alarm. Unlock it first.' : c.jobActive ? 'A drawing is running.' : '');
    goNote.textContent = '';
    const s = cur();
    kind.set(s.kind ?? 'none');
    urlRow.hidden = s.kind !== 'url'; devs.hidden = s.kind !== 'device';
    if (document.activeElement !== url) url.value = s.url ?? '';
    camStatus.textContent = !cam?.source ? 'No camera active.' : cam.source.error ? cam.source.error : cam.source.size ? `${cam.source.size[0]} × ${cam.source.size[1]} px · ${fmt(cam.source.fps, 1)} fps` : 'Waiting for the first picture …';
    if (devList.length) paintDevs();
    viewBox.hidden = !cam?.source;
    if (cam?.source && viewing('setup')) view.start(); else view.stop();
    if (!cam?.source && !camBox._shown) { camBox._shown = true; camBox.open = true; }
    // keep the position row honest if the reference changes elsewhere
    if (rows.position.el.dataset.s === 'ok' && !st?.reference?.set) mark('position', 'idle', '');
  }

  root.append(
    sect(null, goBtn, goNote, list, fallback),
    sect(null, viewBox, result),
    sect(null, camBox, posBox));
  store.on(['camera', 'state', 'job', 'tab'], paint, { immediate: true });
  showMotors();
}
