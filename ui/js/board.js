// Board view (canvas): true to scale, grid, rulers, motors + belts, gondola, trace, preview, click target.
import { h, clamp, fmt, lsGet, lsSet, clear } from './util.js';
import { icon } from './icons.js';
import { store, viewing } from './store.js';
import { colorHex } from './widgets.js';
import { mountTargetPopover } from './board-pop.js';
import { strokesOf } from './drawing.js';
import { drawCalibration } from './board-calib.js';
import { mountBoardCam } from './board-cam.js';
import { live, mountLiveBar, onLiveInk } from './live.js';
import { api } from './api.js';

const RL = 42, RB = 24, PAD = 12;                       // Lineal links / unten, Rand
const NICE = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000];
const MINOR = { 5: 1, 10: 2, 20: 5, 50: 10, 100: 20, 200: 50, 500: 100, 1000: 200 };
const MAX_TRACE = 40000;

export function pushTrace(seg) {
  const t = store.get('trace');
  t.push(seg);
  if (t.length > MAX_TRACE) t.splice(0, 5000);
  store.touch('trace');
}

export function mountBoard(root) {
  const opts = { grid: true, travel: false, firmware: false, preview: true, ...lsGet('wbot-board', {}) };
  const canvas = h('canvas', { class: 'board-canvas', tabindex: '0', role: 'img',
    'aria-label': 'Board view. Click a point to pick a target.' });
  const readout = h('div', { class: 'board-readout', hidden: true });
  const simChip = h('div', { class: 'board-sim', hidden: true });
  const note = h('div', { class: 'note board-note', hidden: true, role: 'status' });
  const wrap = h('div', { class: 'board-wrap' }, canvas, readout, simChip);
  const ctx = canvas.getContext('2d');

  let W = 0, H = 0, dpr = 1, dirty = false, hover = null, th = {};
  const view = { scale: 1, tx: 0, ty: 0, mode: 'all' };

  /* ---------- Toolbar ---------- */
  const tog = (key, label, title) => {
    const b = h('button', { class: 'btn sm', type: 'button', 'aria-pressed': String(!!opts[key]), title,
      onClick: () => { opts[key] = !opts[key]; b.setAttribute('aria-pressed', String(opts[key])); lsSet('wbot-board', opts); invalidate(); } }, label);
    return b;
  };
  const vbtn = (label, title, fn, ic) => h('button', { class: 'btn sm' + (ic ? ' icon-only' : ''), type: 'button', title, 'aria-label': title, onClick: fn }, ic ? icon(ic) : label);
  const writingBtn = h('button', { class: 'btn sm', type: 'button', 'aria-pressed': 'false', title: 'Scan the board with the camera and mark what is already written (needs a calibrated camera)' }, 'Show writing');
  writingBtn.addEventListener('click', async () => {
    if (store.get('occupancy')) { store.set({ occupancy: null }); writingBtn.setAttribute('aria-pressed', 'false'); return; }
    writingBtn.disabled = true; writingBtn.textContent = 'Scanning …';
    try { store.set({ occupancy: await api.occupancy(10) }); writingBtn.setAttribute('aria-pressed', 'true'); }
    catch { writingBtn.setAttribute('aria-pressed', 'false'); }        // the notice came from api.js
    finally { writingBtn.disabled = false; writingBtn.textContent = store.get('occupancy') ? 'Hide writing' : 'Show writing'; }
  });
  const clearBtn = h('button', { class: 'btn sm', type: 'button', title: 'Clear the drawn trace from the view', onClick: () => { store.get('trace').length = 0; store.touch('trace'); } }, icon('trash'), 'Clear trace');
  const menu = h('div', { class: 'board-menu', hidden: true, role: 'group', 'aria-label': 'View options' },
    tog('grid', 'Grid', 'Show or hide the grid'), tog('preview', 'Preview', 'Show or hide the preview'),
    tog('travel', 'Travel', 'Show travel moves (pen up)'), tog('firmware', 'Firmware', 'Position the firmware believes'), writingBtn, clearBtn);
  const menuBtn = h('button', { class: 'btn sm', type: 'button', 'aria-expanded': 'false', title: 'View options',
    onClick: () => { menu.hidden = !menu.hidden; menuBtn.setAttribute('aria-expanded', String(!menu.hidden)); } }, icon('gear'), 'View');
  document.addEventListener('pointerdown', (e) => { if (!menu.hidden && !menu.contains(e.target) && !menuBtn.contains(e.target)) { menu.hidden = true; menuBtn.setAttribute('aria-expanded', 'false'); } });
  const liveBtn = h('button', { class: 'btn sm', type: 'button', 'aria-pressed': String(live.on), title: 'Draw on the board and let the plotter follow',
    onClick: () => { live.setOn(!live.on); liveBtn.setAttribute('aria-pressed', String(live.on)); store.set({ target: null }); invalidate(); } }, icon('pen'), 'Live draw');
  const bar = h('div', { class: 'board-bar' },
    vbtn('All', 'Show the whole machine including the motors', () => fit('all')),
    vbtn('Board', 'Zoom to the board', () => fit('board')),
    vbtn('', 'Zoom in', () => zoomBy(1.4), 'plus'), vbtn('', 'Zoom out', () => zoomBy(1 / 1.4), 'minus'),
    h('span', { class: 'sp' }), liveBtn, menuBtn, menu);
  const cam = mountBoardCam(bar, () => invalidate(), () => Math.min(1800, Math.max(400, (store.get('config')?.board?.width_mm ?? 1500) * view.scale * dpr)));
  let ga = 1;                                          // Opacity of preview and trace (camera blend)
  root.append(bar, note, wrap);
  mountTargetPopover(wrap, store, () => ({ w2s, s2w, W, H }));
  mountLiveBar(wrap);
  onLiveInk(() => invalidate());

  /* ---------- Theme & size ---------- */
  function readTheme() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n) => cs.getPropertyValue(n).trim();
    th = { outer: v('--board-outer'), surface: v('--board-surface'), edge: v('--board-edge'), grid: v('--board-grid'),
      major: v('--board-grid-major'), ink: v('--board-ink'), rbg: v('--ruler-bg'), rink: v('--ruler-ink'), safe: v('--board-safe'),
      belt: v('--board-belt'), truth: v('--board-truth'), accent: v('--accent'), font: v('--font-mono') };
  }
  function resize() {
    const r = wrap.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) return;
    dpr = window.devicePixelRatio || 1;
    W = Math.round(r.width); H = Math.round(r.height);
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    if (view.mode !== 'user') fit(view.mode); else invalidate();
  }
  new ResizeObserver(resize).observe(wrap);
  window.addEventListener('themechange', () => { readTheme(); invalidate(); });
  matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', () => setTimeout(() => { readTheme(); invalidate(); }, 0));

  /* ---------- Koordinaten ---------- */
  const w2s = (x, y) => [view.tx + x * view.scale, view.ty - y * view.scale];
  const s2w = (sx, sy) => [(sx - view.tx) / view.scale, (view.ty - sy) / view.scale];

  function bounds(mode) {
    const cfg = store.get('config');
    const b = cfg?.board ?? { width_mm: 1500, height_mm: 1000 }, g = cfg?.geometry;
    if (mode === 'board' || !g) return [-30, -30, b.width_mm + 30, b.height_mm + 30];
    const r = g.pulley_pitch_radius_mm + 30;
    return [Math.min(0, g.anchor_left[0] - r) - 10, -30, Math.max(b.width_mm, g.anchor_right[0] + r) + 10, Math.max(b.height_mm, g.anchor_left[1], g.anchor_right[1]) + r + 10];
  }
  function fit(mode) {
    if (!W) return;
    view.mode = mode;
    const [x0, y0, x1, y1] = bounds(mode);
    const aw = W - RL - PAD, ah = H - RB - PAD;
    view.scale = Math.min(aw / (x1 - x0), ah / (y1 - y0));
    view.tx = RL + (aw - (x1 - x0) * view.scale) / 2 - x0 * view.scale;
    view.ty = PAD + (ah + (y1 - y0) * view.scale) / 2 + y0 * view.scale;
    invalidate();
  }
  function zoomAt(px, py, k) {
    const s = clamp(view.scale * k, 0.05, 40);
    k = s / view.scale;
    view.tx = px - (px - view.tx) * k; view.ty = py - (py - view.ty) * k; view.scale = s; view.mode = 'user';
    invalidate();
  }
  const zoomBy = (k) => zoomAt(RL + (W - RL) / 2, (H - RB) / 2, k);

  /* ---------- Zeichnen ---------- */
  function invalidate() { if (!dirty) { dirty = true; requestAnimationFrame(draw); } }

  function draw() {
    dirty = false;
    if (!W || !th.outer) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = th.outer; ctx.fillRect(0, 0, W, H);
    const cfg = store.get('config');
    if (!cfg) return;
    ctx.save();
    ctx.beginPath(); ctx.rect(RL, 0, W - RL, H - RB); ctx.clip();
    drawBoard(cfg); cam.draw(ctx, w2s, view.scale); ga = cam.inkAlpha(); drawOccupancy(); drawPreview(cfg); drawTrace(cfg); drawLive(cfg); ga = 1; drawMachine(cfg); drawCamTrack(); drawCalibration(ctx, w2s, th, viewing('kalib') ? store.get('calib') : null, view, W, RL); drawTarget(); drawHover();
    ctx.restore();
    drawRulers();
  }

  function steps() {
    const major = NICE.find((s) => s * view.scale >= 64) ?? 1000;
    return [major, MINOR[major] && MINOR[major] * view.scale >= 6 ? MINOR[major] : 0];
  }

  function drawBoard(cfg) {
    const b = cfg.board, [bx, by] = w2s(0, b.height_mm), bw = b.width_mm * view.scale, bh = b.height_mm * view.scale;
    ctx.fillStyle = th.surface; ctx.fillRect(bx, by, bw, bh);
    if (opts.grid) {
      const [major, minor] = steps();
      const line = (step, color) => {
        ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.beginPath();
        for (let x = 0; x <= b.width_mm + 1e-6; x += step) { const sx = Math.round(view.tx + x * view.scale) + .5; ctx.moveTo(sx, by); ctx.lineTo(sx, by + bh); }
        for (let y = 0; y <= b.height_mm + 1e-6; y += step) { const sy = Math.round(view.ty - y * view.scale) + .5; ctx.moveTo(bx, sy); ctx.lineTo(bx + bw, sy); }
        ctx.stroke();
      };
      if (minor) line(minor, th.grid);
      line(major, th.major);
    }
    ctx.strokeStyle = th.edge; ctx.lineWidth = 2; ctx.strokeRect(bx, by, bw, bh);
    const m = b.margin_mm, [sx, sy] = w2s(m, b.height_mm - m);
    ctx.setLineDash([7, 5]); ctx.strokeStyle = th.safe; ctx.lineWidth = 1.5;
    ctx.strokeRect(sx, sy, (b.width_mm - 2 * m) * view.scale, (b.height_mm - 2 * m) * view.scale);
    ctx.setLineDash([]);
  }

  /** On-screen width of a pen's line: the real width in mm, but never thinner than `min` px. */
  function penPx(cfg, slot, min) { return Math.max(min, (cfg?.colors?.[slot]?.width_mm ?? 0) * view.scale); }

  /** What the camera sees written on the board: hatched red cells (cells[row][col], row 0 = top edge). */
  function drawOccupancy() {
    const oc = store.get('occupancy');
    if (!oc?.cells) return;
    const [x0, , , y1] = oc.rect, cm = oc.cell_mm, k = view.scale;
    ctx.fillStyle = 'rgba(215, 60, 60, 0.30)'; ctx.strokeStyle = 'rgba(215, 60, 60, 0.75)'; ctx.lineWidth = 1;
    oc.cells.forEach((row, r) => {
      let c = 0;
      while (c < row.length) {
        if (row[c] !== '1') { c++; continue; }
        let e = c; while (e < row.length && row[e] === '1') e++;
        const [sx, sy] = w2s(x0 + c * cm, y1 - r * cm);
        ctx.fillRect(sx, sy, (e - c) * cm * k, cm * k);
        c = e;
      }
    });
    for (const [bx0, by0, bx1, by1] of oc.boxes ?? []) {                       // outline of each piece of writing
      const [sx, sy] = w2s(bx0, by1);
      ctx.strokeRect(sx, sy, (bx1 - bx0) * k, (by1 - by0) * k);
    }
  }

  function drawPreview(cfg) {
    const pv = store.get('preview');
    if (!opts.preview || !pv?.drawing) return;
    ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.lineWidth = 1.4;
    let last = null;
    for (const s of strokesOf(pv.drawing)) {
      const pts = s.points;
      if (pts.length < 1) continue;
      if (opts.travel && last) {
        ctx.setLineDash([4, 4]); ctx.strokeStyle = th.belt; ctx.lineWidth = 1; ctx.globalAlpha = .6 * ga;
        ctx.beginPath(); ctx.moveTo(...w2s(...last)); ctx.lineTo(...w2s(...pts[0])); ctx.stroke();
        ctx.setLineDash([]); ctx.globalAlpha = 1;
      }
      ctx.strokeStyle = colorHex(cfg, s.color); ctx.globalAlpha = .8 * ga; ctx.lineWidth = penPx(cfg, s.color, 1.4);
      ctx.beginPath(); ctx.moveTo(...w2s(...pts[0]));
      for (let i = 1; i < pts.length; i++) ctx.lineTo(...w2s(...pts[i]));
      ctx.stroke(); ctx.globalAlpha = 1;
      if (opts.travel) { ctx.fillStyle = colorHex(cfg, s.color); ctx.beginPath(); const [a, b] = w2s(...pts[0]); ctx.arc(a, b, 2.5, 0, 7); ctx.fill(); }
      last = pts[pts.length - 1];
    }
  }

  function drawTrace(cfg) {
    const segs = store.get('trace'), n = segs.length;
    ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    let i = 0;
    while (i < n) {
      const s = segs[i], down = !!s[4];
      if (!down && !opts.travel) { i++; continue; }
      const c = s[5];
      ctx.beginPath(); ctx.moveTo(...w2s(s[0], s[1]));
      let j = i;
      while (j < n && !!segs[j][4] === down && segs[j][5] === c) { ctx.lineTo(...w2s(segs[j][2], segs[j][3])); j++; }
      if (down) { ctx.strokeStyle = colorHex(cfg, c); ctx.lineWidth = penPx(cfg, c, 2.6); ctx.globalAlpha = ga; ctx.setLineDash([]); }
      else { ctx.strokeStyle = th.belt; ctx.lineWidth = 1; ctx.globalAlpha = .55 * ga; ctx.setLineDash([3, 4]); }
      ctx.stroke();
      i = j;
    }
    ctx.globalAlpha = 1; ctx.setLineDash([]);
  }

  /** Strokes drawn in live mode: solid while drawing/queued, faint once sent (the plotter's own trace takes over). */
  function drawLive(cfg) {
    if (!live.strokes.length) return;
    ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    for (const st of live.strokes) {
      if (st.pts.length < 1) continue;
      const sent = st.state === 'sent';
      ctx.strokeStyle = colorHex(cfg, st.color); ctx.lineWidth = penPx(cfg, st.color, 1.6);
      ctx.globalAlpha = sent ? 0.3 : 0.95; ctx.setLineDash(st.state === 'queued' ? [6, 4] : []);
      ctx.beginPath(); ctx.moveTo(...w2s(...st.pts[0]));
      if (st.pts.length === 1) ctx.lineTo(...w2s(st.pts[0][0] + 0.01, st.pts[0][1]));
      for (let i = 1; i < st.pts.length; i++) ctx.lineTo(...w2s(...st.pts[i]));
      ctx.stroke();
    }
    ctx.globalAlpha = 1; ctx.setLineDash([]);
  }

  function label(text, x, y, color, align = 'left') {
    ctx.font = `12px ${th.font}`; ctx.fillStyle = color; ctx.textAlign = align; ctx.textBaseline = 'middle';
    ctx.fillText(text, x, y);
  }

  function drawMachine(cfg) {
    const st = store.get('state'), g = cfg.geometry;
    const narrow = Math.abs(g.anchor_right[0] - g.anchor_left[0]) * view.scale < 480;
    const connected = st?.connection?.status === 'connected';
    const phys = connected && st.reference?.set ? st.position?.physical : null;
    const tr = store.get('trace'), last = tr.length ? tr[tr.length - 1] : null;
    const truth = connected && st.connection.transport === 'sim' && last ? [last[2], last[3]] : null;
    const pen = phys || truth;

    for (const [key, a] of [['L', g.anchor_left], ['R', g.anchor_right]]) {
      const [ax, ay] = w2s(a[0], a[1]), r = Math.max(5, g.pulley_pitch_radius_mm * view.scale);
      if (pen) {
        const [px, py] = w2s(pen[0], pen[1]);
        ctx.strokeStyle = th.belt; ctx.lineWidth = 1.6; ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(px, py); ctx.stroke();
        const len = Math.hypot(pen[0] - a[0], pen[1] - a[1]);
        label(`${fmt(len, 1)} mm`, (ax + px) / 2 + 8, (ay + py) / 2, th.rink);
      }
      ctx.fillStyle = th.surface; ctx.strokeStyle = th.edge; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(ax, ay, r, 0, 7); ctx.fill(); ctx.stroke();
      ctx.beginPath(); ctx.arc(ax, ay, 2.5, 0, 7); ctx.fillStyle = th.edge; ctx.fill();
      label(narrow ? key : `Motor ${key === 'L' ? 'left' : 'right'} (${fmt(a[0], 0)} | ${fmt(a[1], 0)})`, ax + (key === 'L' ? r + 6 : -r - 6), ay - 2, th.rink, key === 'L' ? 'left' : 'right');
    }
    if (opts.firmware && connected && st.position?.firmware) {
      const [fx, fy] = w2s(...st.position.firmware);
      ctx.setLineDash([3, 3]); ctx.strokeStyle = th.ink; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(fx, fy - 9); ctx.lineTo(fx + 9, fy); ctx.lineTo(fx, fy + 9); ctx.lineTo(fx - 9, fy); ctx.closePath(); ctx.stroke(); ctx.setLineDash([]);
      label('FW', fx + 12, fy - 10, th.ink);
    }
    if (truth && phys) {            // Simulator: wo die Gondel wirklich ist
      const [px, py] = w2s(...phys), [tx, ty] = w2s(...truth);
      ctx.strokeStyle = th.truth; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(tx, ty); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(tx, ty - 8); ctx.lineTo(tx + 8, ty); ctx.lineTo(tx, ty + 8); ctx.lineTo(tx - 8, ty); ctx.closePath(); ctx.stroke();
    } else if (truth) {
      const [tx, ty] = w2s(...truth);
      ctx.strokeStyle = th.truth; ctx.lineWidth = 2; ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.arc(tx, ty, 9, 0, 7); ctx.stroke(); ctx.setLineDash([]);
    }
    if (phys) {                    // Gondel laut Software
      const [px, py] = w2s(...phys), down = st.pen && !st.pen.up;
      const slot = st.revolver?.slot, col = slot ? colorHex(cfg, slot - 1) : th.ink;
      ctx.strokeStyle = th.accent; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(px - 16, py); ctx.lineTo(px + 16, py); ctx.moveTo(px, py - 16); ctx.lineTo(px, py + 16); ctx.stroke();
      ctx.beginPath(); ctx.arc(px, py, 8, 0, 7); ctx.fillStyle = th.surface; ctx.fill(); ctx.stroke();
      if (down) { ctx.fillStyle = col; ctx.beginPath(); ctx.arc(px, py, 4.5, 0, 7); ctx.fill(); }
    }
  }

  function drawCamTrack() {          // Gondel-Marker: wo die Kamera den Stift sieht (Kreis) und die Abweichung zur Soll-Position
    const tr = store.get('camTrack');
    if (!tr?.pen || Date.now() / 1000 - tr.t > 3) return;
    const [x, y] = w2s(...tr.pen);
    ctx.strokeStyle = '#1f6feb'; ctx.lineWidth = 2.2; ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(x, y, 11, 0, 7); ctx.stroke();
    if (tr.soll) { const [sx, sy] = w2s(...tr.soll); ctx.lineWidth = 1.4; ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(x, y); ctx.stroke(); }
    if (tr.dev_mm != null && !tr.moving) label(`Camera ${fmt(tr.dev_mm, 1)} mm`, x + 14, y - 12, '#1f6feb');
  }

  function drawTarget() {
    const t = store.get('target');
    if (!t) return;
    const [x, y] = w2s(t.x, t.y);
    ctx.strokeStyle = th.accent; ctx.lineWidth = 2; ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(x, y, 11, 0, 7); ctx.moveTo(x - 17, y); ctx.lineTo(x - 5, y); ctx.moveTo(x + 5, y); ctx.lineTo(x + 17, y);
    ctx.moveTo(x, y - 17); ctx.lineTo(x, y - 5); ctx.moveTo(x, y + 5); ctx.lineTo(x, y + 17); ctx.stroke();
  }

  function drawHover() {
    if (!hover) return;
    const [x, y] = w2s(hover[0], hover[1]);
    ctx.strokeStyle = th.ink; ctx.globalAlpha = .35; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(x + .5, 0); ctx.lineTo(x + .5, H - RB); ctx.moveTo(RL, y + .5); ctx.lineTo(W, y + .5); ctx.stroke();
    ctx.setLineDash([]); ctx.globalAlpha = 1;
  }

  function drawRulers() {
    ctx.fillStyle = th.rbg; ctx.fillRect(0, 0, RL, H); ctx.fillRect(0, H - RB, W, RB);
    ctx.strokeStyle = th.edge; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(RL - .5, 0); ctx.lineTo(RL - .5, H - RB); ctx.lineTo(W, H - RB + .5); ctx.stroke();
    const [major, minor] = steps();
    ctx.font = `11px ${th.font}`; ctx.fillStyle = th.rink; ctx.strokeStyle = th.rink; ctx.globalAlpha = .75; ctx.lineWidth = 1;
    const [wx0, wy1] = s2w(RL, 0), [wx1, wy0] = s2w(W, H - RB);
    const first = (v, s) => Math.ceil(v / s) * s;
    ctx.beginPath(); ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    for (let x = first(wx0, minor || major); x <= wx1; x += minor || major) {
      const sx = Math.round(view.tx + x * view.scale) + .5, isMajor = Math.abs(x / major - Math.round(x / major)) < 1e-6;
      ctx.moveTo(sx, H - RB); ctx.lineTo(sx, H - RB + (isMajor ? 7 : 3));
      if (isMajor) ctx.fillText(String(Math.round(x)), sx, H - RB + 9);
    }
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    for (let y = first(wy0, minor || major); y <= wy1; y += minor || major) {
      const sy = Math.round(view.ty - y * view.scale) + .5, isMajor = Math.abs(y / major - Math.round(y / major)) < 1e-6;
      ctx.moveTo(RL, sy); ctx.lineTo(RL - (isMajor ? 7 : 3), sy);
      if (isMajor) ctx.fillText(String(Math.round(y)), RL - 9, sy);
    }
    ctx.stroke();
    ctx.textAlign = 'left'; ctx.textBaseline = 'top';
    ctx.fillText('mm', 6, H - RB + 7);
    ctx.globalAlpha = 1;
  }

  /* ---------- Eingabe: Hover, Klick, Pan, Pinch, Rad ---------- */
  const ptrs = new Map();
  let startPt = null, moved = false, pinch = null;
  const local = (e) => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };

  function updateReadout() {
    const cfg = store.get('config'), st = store.get('state');
    if (!hover || !cfg) { readout.hidden = true; return; }
    const [x, y] = hover, b = cfg.board, m = b.margin_mm;
    const inside = x >= m && x <= b.width_mm - m && y >= m && y <= b.height_mm - m;
    let txt = `X ${fmt(x, 1)}  Y ${fmt(y, 1)} mm`;
    const p = st?.reference?.set ? st.position?.physical : null;
    if (p) txt += `  · Distance to gondola ${fmt(Math.hypot(x - p[0], y - p[1]), 1)} mm`;
    if (!inside) txt += '  · outside the allowed area';
    readout.textContent = txt; readout.hidden = false;
  }

  let liveDown = null;                                 // pointer id of the stroke being drawn in live mode
  canvas.addEventListener('pointerdown', (e) => {
    if (live.on) {
      const p = local(e);
      if (liveDown == null && e.button === 0 && p[0] >= RL && p[1] <= H - RB) {
        try { canvas.setPointerCapture(e.pointerId); } catch { /* synthetic */ }
        liveDown = e.pointerId; live.begin(...s2w(...p));
      }
      return;
    }
    try { canvas.setPointerCapture(e.pointerId); } catch { /* synthetic */ }
    ptrs.set(e.pointerId, local(e));
    if (ptrs.size === 1) { startPt = local(e); moved = false; }
    if (ptrs.size === 2) { const [a, b] = [...ptrs.values()]; pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]) }; moved = true; }
  });
  canvas.addEventListener('pointermove', (e) => {
    if (liveDown === e.pointerId) { const q = local(e); live.move(...s2w(...q)); hover = s2w(...q); updateReadout(); return; }
    const p = local(e);
    if (e.pointerType === 'mouse' || !ptrs.size) { hover = p[0] >= RL && p[1] <= H - RB ? s2w(...p) : null; updateReadout(); invalidate(); }
    if (!ptrs.has(e.pointerId)) return;
    const prev = ptrs.get(e.pointerId);
    ptrs.set(e.pointerId, p);
    if (ptrs.size === 2 && pinch) {
      const [a, b] = [...ptrs.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]);
      zoomAt((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, d / pinch.d); pinch.d = d;
    } else if (ptrs.size === 1) {
      if (!moved && Math.hypot(p[0] - startPt[0], p[1] - startPt[1]) > 7) moved = true;
      if (moved) { view.tx += p[0] - prev[0]; view.ty += p[1] - prev[1]; view.mode = 'user'; invalidate(); }
    }
  });
  const end = (e) => {
    if (liveDown === e.pointerId) { liveDown = null; live.end(); return; }
    const was = ptrs.size;
    ptrs.delete(e.pointerId);
    if (ptrs.size < 2) pinch = null;
    if (e.type === 'pointerup' && was === 1 && !moved) {
      const p = local(e);
      if (p[0] >= RL && p[1] <= H - RB) {
        const [x, y] = s2w(...p);
        store.set({ target: { x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10, sx: p[0], sy: p[1] } });
      }
    }
  };
  canvas.addEventListener('pointerup', end);
  canvas.addEventListener('pointercancel', end);
  canvas.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse') { hover = null; updateReadout(); invalidate(); } });
  canvas.addEventListener('wheel', (e) => { e.preventDefault(); const p = local(e); zoomAt(p[0], p[1], Math.exp(-e.deltaY * 0.0015)); }, { passive: false });
  canvas.addEventListener('dblclick', () => fit(view.mode === 'all' ? 'board' : 'all'));
  canvas.addEventListener('keydown', (e) => { if (e.key === 'Escape') store.set({ target: null }); });

  /* ---------- Hinweise ---------- */
  function updateNotes() {
    const s = store.get('state'), connected = s?.connection?.status === 'connected';
    let cls = '', txt = '';
    if (!connected) { cls = 'note board-note'; txt = 'Not connected. Press “Connect” at the top, or run Setup. Board and motors are drawn to scale.'; }
    else if (!s.reference?.set) { cls = 'note warn board-note'; txt = 'Position unknown. Click on the board where the gondola hangs and choose “Set gondola position”, or run Setup.'; }
    note.hidden = !txt; if (txt) { note.className = cls; note.textContent = txt; }
    const tr = store.get('trace'), last = tr.at(-1), p = s?.position?.physical;
    if (connected && s.connection.transport === 'sim' && s.reference?.set && p && last && s.machine.state === 'idle' && !s.machine.busy) {
      simChip.hidden = false;
      simChip.textContent = `Actual vs. target error ${fmt(Math.hypot(last[2] - p[0], last[3] - p[1]), 2)} mm`;
    } else simChip.hidden = true;
  }

  store.on(['state', 'config', 'preview', 'trace', 'target', 'calib', 'tab', 'camTrack', 'occupancy'], () => { updateNotes(); updateReadout(); invalidate(); });
  store.on('config', () => { if (view.mode !== 'user') fit(view.mode); });
  readTheme(); resize(); updateNotes();
}
