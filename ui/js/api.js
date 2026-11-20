/* API-Schicht der UI. Alle HTTP-Aufrufe und der WebSocket laufen hier durch.

   Vollständige Liste der Aufrufe, die die UI macht (Vertrag: docs/ARCHITECTURE.md):
     GET  /api/state                      -> State                        (Start, nach WS-Reconnect)
     GET  /api/ports                      -> [{device, description}]      (Port-Auswahl)
     POST /api/connect {transport, port?, baudrate?, sim?:{speed, physical_start, perturb}} -> State
                                          (transport 'wifi': {host, protocol:'telnet'|'websocket', port?} statt port/baudrate)
     POST /api/disconnect                 -> State
     POST /api/reference {x, y}           -> State   (422 reference_unreachable: deutscher Text wird angezeigt)
     POST /api/jog {dx, dy, feed?}        -> {ok}
     POST /api/goto {x, y, feed?, pen:"up"} -> {ok}
     POST /api/park                       -> {ok}
     POST /api/pen {state:"up"|"down"} | {angle}            -> State
     POST /api/revolver {slot:1..3} | {angle}               -> State   (hebt Stift automatisch)
     POST /api/hold | /api/resume | /api/cancel | /api/estop | /api/unlock | /api/reset -> State
     POST /api/gcode {line}               -> {replies:[...]}              (Konsole)
     GET  /api/patterns                   -> [{id,name,description,params:[Param]}]
     POST /api/preview {pattern:{id,params}} | {drawing}, layout:{scale,rotate,dx,dy,fit}
                                          -> {drawing, bounds, estimated_s, warnings, stats}
     POST /api/jobs {pattern|drawing, layout, dry_run, speed_scale} -> Job
     GET  /api/job                        -> Job | null
     POST /api/svg (multipart "file")     -> {drawing}
     GET  /api/config                     -> Config
     PUT  /api/config {Teil-Patch}        -> Config
     GET  /api/firmware-config            -> text/plain (YAML)
     GET  /api/simulator                  -> {physical, intended, firmware, error_mm, job_error:{max_mm,mean_mm,p95_mm,samples}?, perturb, trace} | null
     GET  /api/calibration/targets?n_x&n_y&inset_mm -> {targets:[[x,y]], pattern?:{id,params}}   (Reiter Kalibrieren)
     POST /api/calibration/fit {measurements:[{target,measured}], free:null} -> CalibResult
     POST /api/calibration/apply (gleicher Body)       -> {applied, result, config}
     Kamera und Maßband-Kalibrierung: siehe Liste in wbot/camera_api.py (GET/PUT /api/camera, /api/camera/frame.jpg, ...)
     WS   /ws  <- {type:"state"|"log"|"job"|"trace"|"camera"|"autocal", data|...}   (Client sendet nichts)

   NICHT benutzt: POST /api/servo (die UI nutzt /api/pen {angle} und /api/revolver {angle};
   /api/servo hebt laut Backend ebenfalls den Stift vor dem Revolver, ist aber doppelt gemoppelt).

   Fehler: HTTP 4xx/5xx mit {error, code} -> Toast mit dem deutschen `error`-Text, danach ApiError.
*/
import { ApiError } from './util.js';
import { store } from './store.js';
import { toast } from './ui.js';

const NET_MSG = 'Keine Verbindung zum Server. Läuft die W-BOT-Software noch?';

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
      return [`Ungültige Eingabe (${d}).`, 'validation'];
    }
    if (typeof data.detail === 'string') return [data.detail, 'error'];
  }
  if (status === 404) return ['Nicht gefunden (Server kennt diese Funktion nicht).', 'not_found'];
  return [`Serverfehler ${status}.`, 'http_' + status];
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
    if (!quiet) toast(NET_MSG, 'error');
    throw new ApiError(NET_MSG, 'network', 0);
  }
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
  startJob: (body) => post('/jobs', body),
  job: (o) => get('/job', o),
  svg: (file) => { const f = new FormData(); f.append('file', file); return request('POST', '/svg', undefined, { form: f }); },
  config: (o) => get('/config', o),
  putConfig: (patch) => request('PUT', '/config', patch),
  firmwareConfig: () => get('/firmware-config'),
  simulator: (o) => get('/simulator', o),
  calTargets: (nx, ny, inset, o) => get(`/calibration/targets?n_x=${nx}&n_y=${ny}&inset_mm=${inset}`, o),
  calFit: (body) => request('POST', '/calibration/fit', body),
  calApply: (body) => request('POST', '/calibration/apply', body),
  // Kamera (wbot/camera_api.py)
  camera: (o) => get('/camera', o),
  cameraPut: (patch) => request('PUT', '/camera', patch),
  cameraDevices: () => get('/camera/devices'),
  cameraSuggest: () => get('/camera/suggest-board'),
  cameraCalibrate: (body) => request('POST', '/camera/calibrate', body),
  cameraMarkers: () => request('POST', '/camera/calibrate/markers'),
  cameraUncalibrate: () => request('DELETE', '/camera/calibrate'),
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

/* ---------- WebSocket mit Backoff ---------- */
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
      try { onMessage(msg); } catch (e) { console.error('WS-Nachricht fehlerhaft behandelt', e); }
    };
    ws.onclose = () => { store.set({ ws: 'closed' }); retry(); };
    ws.onerror = () => { try { ws.close(); } catch { /* egal */ } };
  }

  function retry() {
    if (stopped || timer) return;
    const delay = Math.min(8000, 400 * 2 ** Math.min(tries++, 5)) * (0.8 + Math.random() * 0.4);
    timer = setTimeout(() => { timer = null; connect(); }, delay);
  }

  connect();
  return { stop() { stopped = true; clearTimeout(timer); try { ws?.close(); } catch { /* egal */ } } };
}
