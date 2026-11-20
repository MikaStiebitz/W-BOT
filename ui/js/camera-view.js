// Live camera view (original perspective) with projected overlay and draggable points for aligning.
// "Zoom to board" crops the picture to the board plus the motors once the camera is aligned; the server
// then delivers just enough resolution for that crop, so the board shows more detail.
import { h, fmt, lsGet, lsSet } from './util.js';
import { store } from './store.js';
import { frameLoop, toImage, polyToImage } from './camera.js';
import { strokesOf } from './drawing.js';
import { colorHex } from './widgets.js';

const CORNER_LABELS = ['1 top left', '2 top right', '3 bottom right', '4 bottom left'];

export function mountCameraView(root) {
  const canvas = h('canvas', { class: 'cam-canvas', role: 'img', 'aria-label': 'Camera picture with overlay' });
  const msg = h('div', { class: 'cam-msg' }, 'No camera selected.');
  let zoom = lsGet('wbot-cam-zoom', true);
  const zoomBtn = h('button', { class: 'btn sm', type: 'button', title: 'Show only the board and the motors, or the whole picture',
    style: { position: 'absolute', top: '6px', right: '6px', zIndex: 2, opacity: 0.9 } });
  const wrap = h('div', { class: 'cam-wrap' }, canvas, msg, zoomBtn);
  root.append(wrap);
  const ctx = canvas.getContext('2d');
  let bmp = null, W = 0, Hh = 0, dpr = 1, dirty = false, edit = null, drag = -1, overlay = true;
  let cr = { x: 0, y: 0, w: 1920, h: 1080 }, kk = 1;     // crop (full-resolution px) and its scale to the canvas

  const full = () => store.get('camera')?.source?.size ?? (bmp ? [bmp.width, bmp.height] : [1920, 1080]);
  const loop = frameLoop({ view: 'raw', fps: () => store.get('camera')?.settings?.view?.fps ?? 10,
    width: () => (edit ? null : Math.min(full()[0], Math.round(W * dpr * full()[0] / cr.w))),
    onFrame: (b) => { bmp?.close?.(); bmp = b; msg.hidden = true; layout(); invalidate(); },
    onError: (e) => { msg.hidden = false; msg.textContent = e.message; } });

  function crop() {
    const [fw, fh] = full(), cam = store.get('camera'), g = cam?.geometry, b = store.get('config')?.board;
    if (!zoom || edit || !g || !b) return { x: 0, y: 0, w: fw, h: fh };
    const az = cam.settings?.anchor_z_mm ?? 41;
    const pts = [[-120, -80, 0], [b.width_mm + 120, -80, 0], [-120, b.height_mm + 280, az], [b.width_mm + 120, b.height_mm + 280, az]]
      .map(([x, y, z]) => toImage(g, x, y, z)).filter(Boolean);
    if (pts.length < 4) return { x: 0, y: 0, w: fw, h: fh };
    let x0 = Math.min(...pts.map((q) => q[0])), x1 = Math.max(...pts.map((q) => q[0]));
    let y0 = Math.min(...pts.map((q) => q[1])), y1 = Math.max(...pts.map((q) => q[1]));
    x0 = Math.max(0, x0); y0 = Math.max(0, y0); x1 = Math.min(fw, x1); y1 = Math.min(fh, y1);
    if (x1 - x0 < 0.2 * fw || y1 - y0 < 0.2 * fh) return { x: 0, y: 0, w: fw, h: fh };
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  }

  function layout() {
    const r = wrap.getBoundingClientRect();
    cr = crop();
    W = Math.max(50, Math.round(r.width)); kk = W / cr.w; Hh = Math.round(cr.h * kk);
    dpr = window.devicePixelRatio || 1;
    wrap.style.height = `${Hh}px`;
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(Hh * dpr)) { canvas.width = Math.round(W * dpr); canvas.height = Math.round(Hh * dpr); }
    zoomBtn.textContent = zoom ? 'Whole picture' : 'Zoom to board';
    zoomBtn.hidden = !store.get('camera')?.geometry || !!edit;
  }
  new ResizeObserver(() => { layout(); invalidate(); }).observe(wrap);
  zoomBtn.addEventListener('click', () => { zoom = !zoom; lsSet('wbot-cam-zoom', zoom); layout(); invalidate(); });
  const T = (x, y) => [(x - cr.x) * kk, (y - cr.y) * kk];          // full-resolution image px -> canvas
  function invalidate() { if (!dirty) { dirty = true; requestAnimationFrame(draw); } }

  function path(pts, close = false) {
    ctx.beginPath(); let pen = false;
    for (const p of pts) { if (!p) { pen = false; continue; } const [x, y] = T(p[0], p[1]); if (pen) ctx.lineTo(x, y); else ctx.moveTo(x, y); pen = true; }
    if (close) ctx.closePath();
  }

  function draw() {
    dirty = false;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = '#20262b'; ctx.fillRect(0, 0, W, Hh);
    if (bmp) {
      const s = bmp.width / full()[0];
      ctx.drawImage(bmp, cr.x * s, cr.y * s, cr.w * s, cr.h * s, 0, 0, W, Hh);
    }
    const cam = store.get('camera'), g = cam?.geometry, cfg = store.get('config');
    if (overlay && g && cfg) { ctx.globalAlpha = edit ? 0.45 : 1; drawOverlay(g, cfg); ctx.globalAlpha = 1; }
    if (edit) drawEdit();
  }

  function drawOverlay(g, cfg) {
    const b = cfg.board, st = store.get('state');
    ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    const label = (text, color, x, y) => { const q = toImage(g, x, y); if (!q) return; const [px, py] = T(...q);
      ctx.font = '600 12px system-ui'; ctx.lineWidth = 3; ctx.strokeStyle = 'rgba(0,0,0,.6)'; ctx.strokeText(text, px + 4, py - 5); ctx.fillStyle = color; ctx.fillText(text, px + 4, py - 5); };
    ctx.strokeStyle = '#ff8c00'; ctx.lineWidth = 2; path(polyToImage(g, [[0, 0], [b.width_mm, 0], [b.width_mm, b.height_mm], [0, b.height_mm], [0, 0]]));
    ctx.stroke();
    label('Board', '#ff8c00', 0, b.height_mm);
    const sr = st?.limits?.safe_rect;
    const rectPoly = (r) => polyToImage(g, [[r[0], r[1]], [r[2], r[1]], [r[2], r[3]], [r[0], r[3]], [r[0], r[1]]]);
    if (sr) {                                                    // allowed drawing area: tinted, always visible
      ctx.fillStyle = 'rgba(255,210,77,.10)'; path(rectPoly(sr), true); ctx.fill();
      ctx.setLineDash([6, 5]); ctx.strokeStyle = '#ffd24d'; ctx.lineWidth = 1.6; path(rectPoly(sr)); ctx.stroke(); ctx.setLineDash([]);
      label('Drawing area', '#ffd24d', sr[0], sr[3]);
    }
    const ap = store.get('anchorPreview');                       // measured motors, not applied yet
    if (ap) {
      if (ap.rect) { ctx.setLineDash([8, 5]); ctx.strokeStyle = '#3ddc84'; ctx.lineWidth = 2.2; path(rectPoly(ap.rect)); ctx.stroke(); ctx.setLineDash([]); label('New drawing area', '#3ddc84', ap.rect[0], ap.rect[1]); }
      const z = store.get('camera')?.settings?.anchor_z_mm ?? 41;
      for (const a of [ap.left, ap.right]) { const q = toImage(g, a[0], a[1], z); if (q) { const [x, y] = T(...q); ctx.strokeStyle = '#3ddc84'; ctx.lineWidth = 2.4; ctx.beginPath(); ctx.arc(x, y, 9, 0, 7); ctx.stroke(); } }
    }
    const az = store.get('camera')?.settings?.anchor_z_mm ?? 41;
    ctx.font = '600 12px system-ui';
    for (const [i, a] of [cfg.geometry.anchor_left, cfg.geometry.anchor_right].entries()) {   // anchors as configured
      const q = toImage(g, a[0], a[1], az);
      if (!q) continue;
      const [x, y] = T(...q);
      ctx.strokeStyle = '#ff8c00'; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(x, y, 8, 0, 7); ctx.stroke();
      ctx.fillStyle = '#ff8c00'; ctx.fillText(i ? 'Anchor R' : 'Anchor L', x + 11, y - 9);
    }
    const pv = store.get('preview');
    if (pv?.drawing) {
      ctx.lineWidth = 1.6;
      for (const s of strokesOf(pv.drawing)) {
        ctx.strokeStyle = colorHex(cfg, s.color) === '#111111' ? '#ff3df2' : colorHex(cfg, s.color);
        ctx.globalAlpha = 0.85; path(s.points.map(([x, y]) => toImage(g, x, y))); ctx.stroke();
      }
      ctx.globalAlpha = 1;
      const bb = pv.bounds;
      if (Array.isArray(bb) && bb.length === 4) label('Preview', '#ff3df2', bb[0], bb[3]);
    }
    const ac = store.get('autocal');
    for (const p of ac?.points ?? []) {
      const q = toImage(g, ...p.target);
      if (!q) continue;
      const [x, y] = T(...q);
      ctx.strokeStyle = p.measured ? '#2ecc71' : '#e74c3c'; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(x, y, 7, 0, 7); ctx.stroke();
    }
    const phys = st?.reference?.set ? st.position?.physical : null;
    if (phys) {
      const q = toImage(g, ...phys);
      if (q) {
        const [x, y] = T(...q);
        ctx.strokeStyle = '#00d26a'; ctx.lineWidth = 2.4;
        ctx.beginPath(); ctx.moveTo(x - 14, y); ctx.lineTo(x + 14, y); ctx.moveTo(x, y - 14); ctx.lineTo(x, y + 14); ctx.stroke();
      }
    }
    const pr = store.get('presence');
    if (pr?.outline && Date.now() / 1000 - pr.t < 2) {          // objects at the board: outlines, skeleton of the largest
      ctx.fillStyle = pr.present ? 'rgba(255,80,160,.18)' : 'rgba(255,255,255,.08)';
      ctx.strokeStyle = '#ff50a0'; ctx.lineWidth = 2;
      for (const o of pr.outlines ?? [pr.outline]) { path(o, true); ctx.fill(); ctx.stroke(); }
      ctx.strokeStyle = 'rgba(255,225,77,.8)'; ctx.lineWidth = 2; ctx.beginPath();
      for (const [a, b2, c, d] of pr.skeleton ?? []) { ctx.moveTo(...T(a, b2)); ctx.lineTo(...T(c, d)); }
      ctx.stroke();
      if (pr.present && pr.bbox) {
        const [x, y] = T(pr.bbox[0], pr.bbox[1]);
        ctx.font = '600 13px system-ui'; ctx.fillStyle = '#ff50a0'; ctx.fillText(pr.guard === 'protect' ? 'Board blocked: machine waits' : 'Object', x, Math.max(14, y - 6));
      }
    }
    const tr = store.get('camTrack');
    if (tr?.corners && Date.now() / 1000 - tr.t < 3) {
      ctx.strokeStyle = '#1f9bff'; ctx.lineWidth = 2; path(tr.corners, true); ctx.stroke();
      if (tr.pen) {
        const q = toImage(g, ...tr.pen);
        if (q) { const [x, y] = T(...q); ctx.beginPath(); ctx.arc(x, y, 10, 0, 7); ctx.stroke(); }
      }
      if (tr.dev_mm != null && !tr.moving) {
        const [x, y] = T(...tr.corners[1]);
        ctx.font = '600 13px system-ui'; ctx.fillStyle = '#1f9bff';
        ctx.fillText(`Off by ${fmt(tr.dev_mm, 1)} mm`, x + 8, y);
      }
    }
  }

  function drawEdit() {
    const pts = edit.points;
    ctx.strokeStyle = '#00e0ff'; ctx.lineWidth = 2; path(pts, true); ctx.stroke();
    ctx.font = '600 12px system-ui';
    pts.forEach((p, i) => {
      const [x, y] = T(p[0], p[1]);
      ctx.fillStyle = 'rgba(0,224,255,.25)'; ctx.beginPath(); ctx.arc(x, y, 13, 0, 7); ctx.fill();
      ctx.strokeStyle = '#00e0ff'; ctx.beginPath(); ctx.arc(x, y, 13, 0, 7); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(x - 5, y); ctx.lineTo(x + 5, y); ctx.moveTo(x, y - 5); ctx.lineTo(x, y + 5); ctx.stroke();
      ctx.fillStyle = '#fff'; ctx.strokeStyle = '#000'; ctx.lineWidth = 3;
      const lbl = edit.labels?.[i] ?? CORNER_LABELS[i] ?? String(i + 1);
      ctx.strokeText(lbl, x + 16, y - 10); ctx.fillText(lbl, x + 16, y - 10); ctx.lineWidth = 2;
    });
    if (drag >= 0 && bmp) {                  // 4x magnifier around the dragged point
      const p = pts[drag], s = bmp.width / full()[0], R = 70, z = 4, [px] = T(p[0], p[1]);
      const lx = px > W / 2 ? 12 + R : W - 12 - R, ly = 12 + R, half = R / z / kk;   // half: full-res px shown on each side
      ctx.save(); ctx.beginPath(); ctx.arc(lx, ly, R, 0, 7); ctx.clip();
      ctx.drawImage(bmp, (p[0] - half) * s, (p[1] - half) * s, 2 * half * s, 2 * half * s, lx - R, ly - R, 2 * R, 2 * R);
      ctx.restore();
      ctx.strokeStyle = '#00e0ff'; ctx.beginPath(); ctx.arc(lx, ly, R, 0, 7); ctx.moveTo(lx - 12, ly); ctx.lineTo(lx + 12, ly); ctx.moveTo(lx, ly - 12); ctx.lineTo(lx, ly + 12); ctx.stroke();
    }
  }

  /* ---------- dragging points ---------- */
  const local = (e) => { const r = canvas.getBoundingClientRect(); return [(e.clientX - r.left) / kk + cr.x, (e.clientY - r.top) / kk + cr.y]; };
  canvas.addEventListener('pointerdown', (e) => {
    if (!edit) return;
    const p = local(e);
    let best = -1, bd = 30 / kk;
    edit.points.forEach((q, i) => { const d = Math.hypot(q[0] - p[0], q[1] - p[1]); if (d < bd) { bd = d; best = i; } });
    if (best < 0 && edit.points.length < edit.max) { edit.points.push(p); best = edit.points.length - 1; edit.onChange?.(edit.points); }
    drag = best;
    if (drag >= 0) { try { canvas.setPointerCapture(e.pointerId); } catch { /* synthetic event */ } }
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

  store.on(['camera', 'config'], () => { layout(); invalidate(); });
  store.on(['camTrack', 'preview', 'autocal', 'state', 'presence', 'anchorPreview'], invalidate);
  layout();
  return {
    start: () => loop.start(), stop: () => loop.stop(),
    setOverlay(v) { overlay = v; invalidate(); },
    /** Edit mode: points in full-resolution image px; null ends it (always shows the whole picture). */
    edit(points, { onChange, labels, max = 4 } = {}) { edit = points ? { points: points.map((p) => [...p]), onChange, labels, max } : null; drag = -1; layout(); invalidate(); },
    editPoints: () => edit?.points ?? null,
  };
}
