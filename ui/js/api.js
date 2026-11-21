/* API layer of the UI: every HTTP call and the WebSocket go through here (contract: docs/ARCHITECTURE.md).

     GET  /api/state, /api/ports, /api/config, /api/patterns, /api/job, /api/simulator
     POST /api/connect | /disconnect | /reference | /jog | /goto | /park | /pen | /revolver
     POST /api/hold | /resume | /cancel | /estop | /unlock | /reset | /gcode
     POST /api/preview {pattern|drawing, layout} -> {drawing, bounds, estimated_s, warnings, stats}
     POST /api/jobs {pattern|drawing, layout, dry_run, speed_scale}
     POST /api/import (multipart file + options JSON) -> {drawing, info?, warnings?}   (SVG, PNG, JPEG, ...)
     PUT  /api/config {partial patch}
     Camera and tape calibration: see the list in wbot/camera_api.py.
     WS   /ws  <- {type:"state"|"log"|"job"|"trace"|"camera"|"autocal"|"presence", data}   (the client sends nothing)

   Errors: HTTP 4xx/5xx with {error, code} -> one dismissible notice with the English `error` text, then ApiError.
*/
import { ApiError } from './util.js';
import { store } from './store.js';
import { toast, notice, clearNotice, confirmDialog } from './ui.js';

const NET_MSG = 'Cannot reach the W-BOT server. Is `wbot serve` still running?';

async function parse(res) {
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('json')) { try { return await res.json(); } catch { return null; } }
  try { return await res.text(); } catch { return null; }
}

function errorText(data, status) {
  if (data && typeof data === 'object') {
    if (typeof data.error === 'string') return [data.error, data.code || 'error'];
    if (Array.isArray(data.detail)) {                       // FastAPI-Validierung (422)
      const d = data.detail.map((x) => `${(x.loc || []).slice(1).join('.')}: ${x.msg}`).join('; ');
      return [`Invalid input (${d}).`, 'validation'];
    }
    if (typeof data.detail === 'string') return [data.detail, 'error'];
  }
  if (status === 404) return ['Not found (the server does not know this function).', 'not_found'];
  return [`Server error ${status}.`, 'http_' + status];
}

async function request(method, path, body, { quiet = false, form = null } = {}) {
  let res;
  try {
    res = await fetch('/api' + path, {
      method,
      headers: form ? undefined : (body !== undefined ? { 'Content-Type': 'application/json' } : undefined),
      body: form ?? (body !== undefined ? JSON.stringify(body) : undefined),
    });
  } catch {
    // the header already shows a banner while the live connection is down: no second message then
    if (!quiet && store.get('ws') === 'open') notice('network', NET_MSG, 'error', { ms: 0 });      // one sticky notice, not one per failed call
    throw new ApiError(NET_MSG, 'network', 0);
  }
  clearNotice('network');
  const data = await parse(res);
  if (!res.ok) {
    const [msg, code] = errorText(data, res.status);
    if (!quiet) toast(msg, 'error');
    throw new ApiError(msg, code, res.status);
  }
  return data;
}

function isState(d) { return d && typeof d === 'object' && d.connection && d.machine; }
function applyState(d) { if (isState(d)) store.set({ state: d }); return d; }

const get = (p, o) => request('GET', p, undefined, o);
const post = (p, b = {}, o) => request('POST', p, b, o).then(applyState);

export const api = {
  state: (o) => get('/state', o).then(applyState),
  ports: (o) => get('/ports', o),
  connect: (b) => post('/connect', b),
  disconnect: () => post('/disconnect'),
  reference: (x, y) => post('/reference', { x, y }),
  jog: (dx, dy, feed) => post('/jog', { dx, dy, ...(feed ? { feed } : {}) }),
  goto: (x, y, feed) => post('/goto', { x, y, pen: 'up', ...(feed ? { feed } : {}) }),
  park: () => post('/park'),
  pen: (arg) => post('/pen', typeof arg === 'number' ? { angle: arg } : { state: arg }),
  revolver: (arg) => post('/revolver', arg.angle != null ? { angle: arg.angle } : { slot: arg.slot }),
  hold: () => post('/hold'),
  resume: () => post('/resume'),
  cancel: () => post('/cancel'),
  estop: () => post('/estop'),
  unlock: () => post('/unlock'),
  reset: () => post('/reset'),
  gcode: (line) => post('/gcode', { line }),
  patterns: (o) => get('/patterns', o),
  preview: (body, o) => request('POST', '/preview', body, o),
  startJob: async (body) => {
    try { return await post('/jobs', body, { quiet: true }); }
    catch (e) {
      if (e.code === 'person_present') {          // camera guard: someone stands at the board
        const ok = await confirmDialog({ title: 'The board is blocked', text: 'The job does not start while something is in front of the board. Start it automatically once the board has been free for 3 seconds?', ok: 'Start when free' });
        if (ok) { await request('POST', '/camera/queue-job', body); toast('The job waits until the board is free.', 'ok'); return store.get('job'); }
      } else toast(e.message, 'error');
      throw e;
    }
  },
  /** Like startJob, but without dialogs or notices: the caller decides what an error means (live draw). */
  startJobQuiet: (body) => post('/jobs', body, { quiet: true }),
  job: (o) => get('/job', o),
  importFile: (file, options = {}) => { const f = new FormData(); f.append('file', file); f.append('options', JSON.stringify(options)); return request('POST', '/import', undefined, { form: f }); },
  svg: (file) => { const f = new FormData(); f.append('file', file); return request('POST', '/svg', undefined, { form: f }); },
  config: (o) => get('/config', o),
  putConfig: (patch) => request('PUT', '/config', patch),
  firmwareConfig: () => get('/firmware-config'),
  simulator: (o) => get('/simulator', o),
  calTargets: (nx, ny, inset, o) => get(`/calibration/targets?n_x=${nx}&n_y=${ny}&inset_mm=${inset}`, o),
  calFit: (body) => request('POST', '/calibration/fit', body),
  calApply: (body) => request('POST', '/calibration/apply', body),
  // Camera (wbot/camera_api.py)
  camera: (o) => get('/camera', o),
  cameraPut: (patch) => request('PUT', '/camera', patch),
  cameraDevices: () => get('/camera/devices'),
  cameraSuggest: () => get('/camera/suggest-board'),
  cameraCalibrate: (body) => request('POST', '/camera/calibrate', body),
  cameraMarkers: () => request('POST', '/camera/calibrate/markers'),
  cameraUncalibrate: () => request('DELETE', '/camera/calibrate'),
  gondolaLocate: (apply = false, o) => request('POST', '/camera/gondola/locate', { apply }, o).then(applyState),
  occupancy: (cell = 10, o) => get(`/camera/occupancy?cell=${cell}`, o),
  overlap: (drawing, o) => request('POST', '/camera/overlap', { drawing }, o),
  autoSetup: () => request('POST', '/camera/auto-setup'),
  cameraMotors: () => get('/camera/motors/suggest'),
  cameraAnchors: (body) => request('POST', '/camera/anchors', body),
  lensCapture: (cols, rows) => request('POST', '/camera/lens/capture', { cols, rows }),
  lensCompute: (square_mm) => request('POST', '/camera/lens/compute', { square_mm }),
  lensClear: () => request('DELETE', '/camera/lens'),
  learnOffset: () => request('POST', '/camera/track/learn-offset'),
  latency: () => request('POST', '/camera/track/latency'),
  cameraMeasure: (body) => request('POST', '/camera/measure', body),
  autocal: (body) => request('POST', '/camera/autocal', body),
  autocalState: (o) => get('/camera/autocal', o),
  autocalCancel: () => request('POST', '/camera/autocal/cancel'),
  tapePlan: (cols, rows, arm) => get(`/calibration/tape?cols=${cols}&rows=${rows}&arm_mm=${arm}`),
  tapeFit: (body) => request('POST', '/calibration/tape/fit', body),
  tapeApply: (body) => request('POST', '/calibration/tape/apply', body),
};

/* ---------- WebSocket with backoff ---------- */
export function startWs({ onMessage, onOpen }) {
  let ws = null, tries = 0, timer = null, stopped = false;

  function connect() {
    if (stopped) return;
    store.set({ ws: 'connecting' });
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    try { ws = new WebSocket(`${proto}://${location.host}/ws`); } catch { return retry(); }
    ws.onopen = () => { tries = 0; store.set({ ws: 'open' }); try { onOpen?.(); } catch (e) { console.error(e); } };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      try { onMessage(msg); } catch (e) { console.error('Failed to handle WS message', e); }
    };
    ws.onclose = () => { store.set({ ws: 'closed' }); retry(); };
    ws.onerror = () => { try { ws.close(); } catch { /* ignore */ } };
  }

  function retry() {
    if (stopped || timer) return;
    const delay = Math.min(8000, 400 * 2 ** Math.min(tries++, 5)) * (0.8 + Math.random() * 0.4);
    timer = setTimeout(() => { timer = null; connect(); }, delay);
  }

  connect();
  return { stop() { stopped = true; clearTimeout(timer); try { ws?.close(); } catch { /* ignore */ } } };
}
