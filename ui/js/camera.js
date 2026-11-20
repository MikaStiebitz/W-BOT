// Camera basics for the UI: frame loop (one request at a time, adapts to the connection) and board <-> image projection.
// Projection as in wbot/camera/geometry.py: board (mm, z in front) -> homography H -> undistorted px -> lens model -> raw px.
import { store } from './store.js';
import { api } from './api.js';

/** Frame loop: fetches /api/camera/frame.jpg and delivers ImageBitmaps. Pauses while nobody watches. */
export function frameLoop({ view = 'raw', width = null, fps = 10, onFrame, onError, quality = 72 }) {
  let on = false, busy = false, timer = null, lastErr = 0;
  const period = () => 1000 / Math.max(1, typeof fps === 'function' ? fps() : fps);
  async function tick() {
    timer = null;
    if (!on || busy) return;
    if (document.hidden) { timer = setTimeout(tick, 500); return; }
    busy = true;
    const t0 = performance.now();
    try {
      const w = typeof width === 'function' ? width() : width;
      const res = await fetch(`/api/camera/frame.jpg?view=${view}&q=${quality}${w ? `&w=${Math.round(w)}` : ''}`, { cache: 'no-store' });
      if (!res.ok) {
        let msg = `Camera picture not available (${res.status}).`;
        try { msg = (await res.json()).error || msg; } catch { /* kein JSON */ }
        throw Object.assign(new Error(msg), { status: res.status });
      }
      const blob = await res.blob();
      const bmp = await createImageBitmap(blob);
      onFrame?.(bmp, { seq: Number(res.headers.get('X-Frame-Seq')), t: Number(res.headers.get('X-Frame-T')) });
    } catch (e) {
      if (e instanceof TypeError) e = new Error('Camera picture not reachable (server restarting?). Retrying ...');
      if (Date.now() - lastErr > 3000) { lastErr = Date.now(); onError?.(e); }
      busy = false;
      if (on) timer = setTimeout(tick, 1500);
      return;
    }
    busy = false;
    if (on) timer = setTimeout(tick, Math.max(0, period() - (performance.now() - t0)));
  }
  return {
    start() { if (!on) { on = true; tick(); } },
    stop() { on = false; clearTimeout(timer); timer = null; },
    get running() { return on; },
    setView(v) { view = v; },
  };
}

/* ---------- Projektion ---------- */
function applyH(H, x, y) {
  const w = H[2][0] * x + H[2][1] * y + H[2][2];
  return [(H[0][0] * x + H[0][1] * y + H[0][2]) / w, (H[1][0] * x + H[1][1] * y + H[1][2]) / w, w];
}
function distort(g, u, v) {
  if (!g.K || !g.dist) return [u, v];
  const K = g.K, [k1 = 0, k2 = 0, p1 = 0, p2 = 0, k3 = 0] = g.dist;
  const x = (u - K[0][2]) / K[0][0], y = (v - K[1][2]) / K[1][1], r2 = x * x + y * y;
  const rad = 1 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2;
  const xd = x * rad + 2 * p1 * x * y + p2 * (r2 + 2 * x * x), yd = y * rad + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y;
  return [xd * K[0][0] + K[0][2], yd * K[1][1] + K[1][2]];
}
function undistort(g, u, v) {
  if (!g.K || !g.dist) return [u, v];
  let [x, y] = [u, v];
  for (let i = 0; i < 12; i++) {           // fixed-point iteration, accurate enough for webcam distortion
    const [du, dv] = distort(g, x, y);
    x += u - du; y += v - dv;
  }
  return [x, y];
}

/** Board point (mm, height z in front of the board) -> raw image px, or null behind the horizon. */
export function toImage(g, x, y, z = 0) {
  if (!g) return null;
  if (z) { const c = g.center, k = c[2] / (c[2] - z); x = c[0] + (x - c[0]) * k; y = c[1] + (y - c[1]) * k; }
  const [u, v, w] = applyH(g.H, x, y);
  if (!(w > 0) || !Number.isFinite(u)) return null;
  return distort(g, u, v);
}

/** Raw image px -> board point (mm) on the board. */
export function toBoard(g, u, v) {
  if (!g) return null;
  const [uu, vv] = undistort(g, u, v);
  const [x, y, w] = applyH(g.H_inv, uu, vv);
  return Number.isFinite(x) && w !== 0 ? [x, y] : null;
}

/** Polyline (mm) into the image, densely sampled (straight board lines bend through the lens). */
export function polyToImage(g, pts, step = 25) {
  const out = [];
  for (let i = 0; i < pts.length; i++) {
    const [x, y] = pts[i];
    if (i > 0 && g.K) {
      const [px, py] = pts[i - 1], n = Math.min(40, Math.ceil(Math.hypot(x - px, y - py) / step));
      for (let k = 1; k < n; k++) out.push(toImage(g, px + (x - px) * k / n, py + (y - py) * k / n));
    }
    out.push(toImage(g, x, y));
  }
  return out;
}

/** Reload the camera info (quietly). */
export async function refreshCamera() {
  try { store.set({ camera: await api.camera({ quiet: true }) }); } catch { /* Server weg: egal */ }
  return store.get('camera');
}
