// Kamera-Ebene der Brettansicht: entzerrtes Kamerabild (Draufsicht in mm) unter der Zeichnung, mit Überblendung.
// Regler links = nur gerenderte Zeichnung, rechts = nur Kamerabild; „Skelett" hält Vorschau und Spur sichtbar darüber.
import { h, lsGet, lsSet } from './util.js';
import { icon } from './icons.js';
import { store } from './store.js';
import { frameLoop } from './camera.js';
import { toast } from './ui.js';

export function mountBoardCam(bar, invalidate, sizeHint) {
  const o = { on: false, blend: 0.6, skeleton: true, ...lsGet('wbot-board-cam', {}) };
  let bmp = null;
  const save = () => lsSet('wbot-board-cam', o);
  const btn = h('button', { class: 'btn sm', type: 'button', 'aria-pressed': String(o.on), title: 'Entzerrtes Kamerabild unter die Zeichnung legen' }, icon('camera'), 'Kamera');
  const range = h('input', { type: 'range', min: 0, max: 100, step: 1, value: String(Math.round(o.blend * 100)), 'aria-label': 'Überblenden: Zeichnung bis Kamera', title: 'Links: Zeichnung, rechts: Kamerabild', style: { width: '110px' } });
  const skel = h('button', { class: 'btn sm', type: 'button', 'aria-pressed': String(o.skeleton), title: 'Vorschau und Spur auch über dem Kamerabild deutlich zeigen' }, 'Skelett');
  const grp = h('span', { class: 'row', style: { gap: '6px', alignItems: 'center', flexWrap: 'nowrap' } }, h('span', { class: 'muted', style: { fontSize: '12px' } }, 'Zeichnung'), range, h('span', { class: 'muted', style: { fontSize: '12px' } }, 'Kamera'), skel);
  bar.append(btn, grp);

  const loop = frameLoop({ view: 'board', fps: 6, width: sizeHint, onFrame: (b) => { bmp?.close?.(); bmp = b; invalidate(); },
    onError: (e) => { if (o.on) toast(`Kamera: ${e.message}`, 'error'); } });

  function sync() {
    const cam = store.get('camera'), ready = !!cam?.calibrated && !!cam?.source;
    btn.disabled = !ready;
    btn.title = ready ? 'Entzerrtes Kamerabild unter die Zeichnung legen' : 'Erst im Reiter „Kamera“ eine Kamera wählen und auf die Tafel einmessen.';
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
    /** Zeichnet das Kamerabild (nach dem Brett, vor Vorschau/Spur). */
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
    /** Deckkraft für Vorschau und Spur. */
    inkAlpha() {
      if (!o.on || !bmp) return 1;
      return o.skeleton ? Math.max(0.45, 1 - o.blend * 0.55) : 1 - o.blend;
    },
  };
}
