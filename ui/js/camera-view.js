// Livebild der Kamera (Originalperspektive) mit projiziertem Overlay und verschiebbaren Eckpunkten zum Einmessen.
import { h, fmt } from './util.js';
import { store } from './store.js';
import { frameLoop, toImage, polyToImage } from './camera.js';
import { strokesOf } from './drawing.js';
import { colorHex } from './widgets.js';

const CORNER_LABELS = ['1 oben links', '2 oben rechts', '3 unten rechts', '4 unten links'];

export function mountCameraView(root) {
  const canvas = h('canvas', { class: 'cam-canvas', role: 'img', 'aria-label': 'Kamerabild mit Overlay' });
  const msg = h('div', { class: 'cam-msg' }, 'Keine Kamera gewählt.');
  const wrap = h('div', { class: 'cam-wrap' }, canvas, msg);
  root.append(wrap);
  const ctx = canvas.getContext('2d');
  let bmp = null, W = 0, Hh = 0, dpr = 1, dirty = false, edit = null, drag = -1, overlay = true;

  const full = () => store.get('camera')?.source?.size ?? (bmp ? [bmp.width, bmp.height] : [1920, 1080]);
  const loop = frameLoop({ view: 'raw', fps: 8, width: () => (edit ? null : Math.min(full()[0], Math.round(W * dpr))),
    onFrame: (b) => { bmp?.close?.(); bmp = b; msg.hidden = true; layout(); invalidate(); },
    onError: (e) => { msg.hidden = false; msg.textContent = e.message; } });

  function layout() {
    const r = wrap.getBoundingClientRect();
    const [fw, fh] = full();
    W = Math.max(50, Math.round(r.width)); Hh = Math.round(W * fh / fw);
    dpr = window.devicePixelRatio || 1;
    wrap.style.height = `${Hh}px`;
    if (canvas.width !== Math.round(W * dpr)) { canvas.width = Math.round(W * dpr); canvas.height = Math.round(Hh * dpr); }
  }
  new ResizeObserver(() => { layout(); invalidate(); }).observe(wrap);
  const k = () => W / full()[0];                         // Bildpixel -> Canvas
  function invalidate() { if (!dirty) { dirty = true; requestAnimationFrame(draw); } }

  function path(pts, close = false) {
    ctx.beginPath(); let pen = false;
    for (const p of pts) { if (!p) { pen = false; continue; } const x = p[0] * k(), y = p[1] * k(); if (pen) ctx.lineTo(x, y); else ctx.moveTo(x, y); pen = true; }
    if (close) ctx.closePath();
  }

  function draw() {
    dirty = false;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = '#20262b'; ctx.fillRect(0, 0, W, Hh);
    if (bmp) ctx.drawImage(bmp, 0, 0, W, Hh);
    const cam = store.get('camera'), g = cam?.geometry, cfg = store.get('config');
    if (overlay && g && cfg && !edit) drawOverlay(g, cfg, cam);
    if (edit) drawEdit();
  }

  function drawOverlay(g, cfg) {
    const b = cfg.board, st = store.get('state');
    ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    ctx.strokeStyle = '#ff8c00'; ctx.lineWidth = 2; path(polyToImage(g, [[0, 0], [b.width_mm, 0], [b.width_mm, b.height_mm], [0, b.height_mm], [0, 0]]));
    ctx.stroke();
    const sr = st?.limits?.safe_rect;
    if (sr) { ctx.setLineDash([6, 5]); ctx.strokeStyle = '#ffd24d'; ctx.lineWidth = 1.4; path(polyToImage(g, [[sr[0], sr[1]], [sr[2], sr[1]], [sr[2], sr[3]], [sr[0], sr[3]], [sr[0], sr[1]]])); ctx.stroke(); ctx.setLineDash([]); }
    const pv = store.get('preview');
    if (pv?.drawing) {
      ctx.lineWidth = 1.6;
      for (const s of strokesOf(pv.drawing)) {
        ctx.strokeStyle = colorHex(cfg, s.color) === '#111111' ? '#ff3df2' : colorHex(cfg, s.color);
        ctx.globalAlpha = 0.85; path(s.points.map(([x, y]) => toImage(g, x, y))); ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
    const ac = store.get('autocal');
    for (const p of ac?.points ?? []) {
      const q = toImage(g, ...p.target);
      if (!q) continue;
      ctx.strokeStyle = p.measured ? '#2ecc71' : '#e74c3c'; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(q[0] * k(), q[1] * k(), 7, 0, 7); ctx.stroke();
    }
    const phys = st?.reference?.set ? st.position?.physical : null;
    if (phys) {
      const q = toImage(g, ...phys);
      if (q) {
        const x = q[0] * k(), y = q[1] * k();
        ctx.strokeStyle = '#00d26a'; ctx.lineWidth = 2.4;
        ctx.beginPath(); ctx.moveTo(x - 14, y); ctx.lineTo(x + 14, y); ctx.moveTo(x, y - 14); ctx.lineTo(x, y + 14); ctx.stroke();
      }
    }
    const tr = store.get('camTrack');
    if (tr?.corners && Date.now() / 1000 - tr.t < 3) {
      ctx.strokeStyle = '#1f9bff'; ctx.lineWidth = 2; path(tr.corners, true); ctx.stroke();
      if (tr.pen) {
        const q = toImage(g, ...tr.pen);
        if (q) { ctx.beginPath(); ctx.arc(q[0] * k(), q[1] * k(), 10, 0, 7); ctx.stroke(); }
      }
      if (tr.dev_mm != null && !tr.moving) {
        ctx.font = '600 13px system-ui'; ctx.fillStyle = '#1f9bff';
        ctx.fillText(`Abweichung ${fmt(tr.dev_mm, 1)} mm`, tr.corners[1][0] * k() + 8, tr.corners[1][1] * k());
      }
    }
  }

  function drawEdit() {
    const pts = edit.points;
    ctx.strokeStyle = '#00e0ff'; ctx.lineWidth = 2; path(pts, true); ctx.stroke();
    ctx.font = '600 12px system-ui';
    pts.forEach((p, i) => {
      const x = p[0] * k(), y = p[1] * k();
      ctx.fillStyle = 'rgba(0,224,255,.25)'; ctx.beginPath(); ctx.arc(x, y, 13, 0, 7); ctx.fill();
      ctx.strokeStyle = '#00e0ff'; ctx.beginPath(); ctx.arc(x, y, 13, 0, 7); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(x - 5, y); ctx.lineTo(x + 5, y); ctx.moveTo(x, y - 5); ctx.lineTo(x, y + 5); ctx.stroke();
      ctx.fillStyle = '#fff'; ctx.strokeStyle = '#000'; ctx.lineWidth = 3;
      const lbl = edit.labels?.[i] ?? CORNER_LABELS[i] ?? String(i + 1);
      ctx.strokeText(lbl, x + 16, y - 10); ctx.fillText(lbl, x + 16, y - 10); ctx.lineWidth = 2;
    });
    if (drag >= 0 && bmp) {                  // Lupe 4x um den gezogenen Punkt
      const p = pts[drag], s = bmp.width / full()[0], R = 70, z = 4;
      const lx = p[0] * k() > W / 2 ? 12 + R : W - 12 - R, ly = 12 + R;
      ctx.save(); ctx.beginPath(); ctx.arc(lx, ly, R, 0, 7); ctx.clip();
      ctx.drawImage(bmp, p[0] * s - R / z * s / k(), p[1] * s - R / z * s / k(), 2 * R / z * s / k(), 2 * R / z * s / k(), lx - R, ly - R, 2 * R, 2 * R);
      ctx.restore();
      ctx.strokeStyle = '#00e0ff'; ctx.beginPath(); ctx.arc(lx, ly, R, 0, 7); ctx.moveTo(lx - 12, ly); ctx.lineTo(lx + 12, ly); ctx.moveTo(lx, ly - 12); ctx.lineTo(lx, ly + 12); ctx.stroke();
    }
  }

  /* ---------- Eckpunkte ziehen ---------- */
  const local = (e) => { const r = canvas.getBoundingClientRect(); return [(e.clientX - r.left) / k(), (e.clientY - r.top) / k()]; };
  canvas.addEventListener('pointerdown', (e) => {
    if (!edit) return;
    const p = local(e);
    let best = -1, bd = 30 / k();
    edit.points.forEach((q, i) => { const d = Math.hypot(q[0] - p[0], q[1] - p[1]); if (d < bd) { bd = d; best = i; } });
    if (best < 0 && edit.points.length < edit.max) { edit.points.push(p); best = edit.points.length - 1; edit.onChange?.(edit.points); }
    drag = best;
    if (drag >= 0) { try { canvas.setPointerCapture(e.pointerId); } catch { /* egal */ } }
    invalidate();
  });
  canvas.addEventListener('pointermove', (e) => {
    if (!edit || drag < 0) return;
    const [fw, fh] = full(), p = local(e);
    edit.points[drag] = [Math.min(fw, Math.max(0, p[0])), Math.min(fh, Math.max(0, p[1]))];
    invalidate();
  });
  const end = () => { if (drag >= 0) { drag = -1; edit?.onChange?.(edit.points); invalidate(); } };
  canvas.addEventListener('pointerup', end); canvas.addEventListener('pointercancel', end);

  store.on(['camera', 'camTrack', 'preview', 'autocal', 'state'], invalidate);
  layout();
  return {
    start: () => loop.start(), stop: () => loop.stop(),
    setOverlay(v) { overlay = v; invalidate(); },
    /** Bearbeitungsmodus: points in Bildpixeln (voll aufgelöst); null beendet ihn. */
    edit(points, { onChange, labels, max = 4 } = {}) { edit = points ? { points: points.map((p) => [...p]), onChange, labels, max } : null; drag = -1; invalidate(); },
    editPoints: () => edit?.points ?? null,
  };
}
