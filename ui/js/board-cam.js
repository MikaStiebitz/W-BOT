// Camera layer of the board view: rectified camera picture (top-down, mm) under the drawing, with a blend.
// Slider left = rendered drawing only, right = camera only; "Skeleton" keeps preview and trace visible on top.
import { h, lsGet, lsSet } from './util.js';
import { icon } from './icons.js';
import { store } from './store.js';
import { frameLoop } from './camera.js';

export function mountBoardCam(bar, invalidate, sizeHint) {
  const o = { on: false, blend: 0.6, skeleton: true, ...lsGet('wbot-board-cam', {}) };
  let bmp = null;
  const save = () => lsSet('wbot-board-cam', o);
  const btn = h('button', { class: 'btn sm', type: 'button', 'aria-pressed': String(o.on), title: 'Show the straightened camera picture under the drawing' }, icon('camera'), 'Camera');
  const range = h('input', { type: 'range', min: 0, max: 100, step: 1, value: String(Math.round(o.blend * 100)), 'aria-label': 'Blend: drawing to camera', title: 'Left: drawing, right: camera', style: { width: '110px' } });
  const skel = h('button', { class: 'btn sm', type: 'button', 'aria-pressed': String(o.skeleton), title: 'Keep preview and trace clearly visible over the camera picture' }, 'Skeleton');
  const note = h('span', { class: 'muted', style: { fontSize: '12px' }, hidden: true }, 'reconnecting ...');
  const grp = h('span', { class: 'row', style: { gap: '6px', alignItems: 'center', flexWrap: 'nowrap' } }, h('span', { class: 'muted', style: { fontSize: '12px' } }, 'Drawing'), range, h('span', { class: 'muted', style: { fontSize: '12px' } }, 'Camera'), skel, note);
  bar.append(btn, grp);

  // no error pop-ups: the loop retries by itself (server restart, camera re-opening); a quiet hint is enough
  const loop = frameLoop({ view: 'board', fps: () => Math.min(15, store.get('camera')?.settings?.view?.fps ?? 10), width: sizeHint, onFrame: (b) => { bmp?.close?.(); bmp = b; note.hidden = true; invalidate(); },
    onError: (e) => { note.hidden = false; note.title = e.message; } });

  function sync() {
    const cam = store.get('camera'), ready = !!cam?.calibrated && !!cam?.source;
    btn.disabled = !ready;
    btn.title = ready ? 'Show the straightened camera picture under the drawing' : 'Choose a camera and align it to the board first (Camera tab).';
    const active = o.on && ready;
    btn.setAttribute('aria-pressed', String(active));
    grp.hidden = !active;
    if (active && !loop.running) loop.start();
    if (!active && loop.running) { loop.stop(); bmp?.close?.(); bmp = null; }
    invalidate();
  }
  btn.addEventListener('click', () => { o.on = !o.on; save(); sync(); });
  range.addEventListener('input', () => { o.blend = Number(range.value) / 100; save(); invalidate(); });
  skel.addEventListener('click', () => { o.skeleton = !o.skeleton; skel.setAttribute('aria-pressed', String(o.skeleton)); save(); invalidate(); });
  store.on('camera', sync, { immediate: true });

  return {
    /** Draws the camera picture (after the board, before preview/trace). */
    draw(ctx, w2s, scale) {
      const cam = store.get('camera');
      if (!o.on || !bmp || !cam?.calibrated) return;
      const [x0, y0, x1, y1] = cam.view_rect, [sx, sy] = w2s(x0, y1);
      ctx.save();
      const cov = cam.coverage ?? [];
      if (cov.length >= 3) {
        ctx.beginPath();
        cov.forEach(([x, y], i) => { const [px, py] = w2s(x, y); if (i) ctx.lineTo(px, py); else ctx.moveTo(px, py); });
        ctx.closePath(); ctx.clip();
      }
      ctx.globalAlpha = o.blend; ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(bmp, sx, sy, (x1 - x0) * scale, (y1 - y0) * scale);
      ctx.restore();
    },
    /** Opacity for preview and trace. */
    inkAlpha() {
      if (!o.on || !bmp) return 1;
      return o.skeleton ? Math.max(0.45, 1 - o.blend * 0.55) : 1 - o.blend;
    },
  };
}
