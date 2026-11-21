// Board overlay for the canvas editor: select, move, resize and rotate elements, snap guides, drop from the palette.
// board.js calls down/move/up first; they return true when the editor used the event.
import { store } from '../store.js';
import { isTyping } from '../util.js';
import * as M from './model.js';

const HANDLE = 5, ROT_GAP = 26, SNAP_PX = 7;
const EVT = 'application/x-wbot-element';

export function mountBoardEdit({ canvas, local, w2s, s2w, scale, invalidate, area, busy }) {
  let drag = null, hoverId = null, guides = [], hold = null;     // hold: box of an element just dropped, until the server answers

  const active = () => store.get('tab') === 'draw' && !busy();
  const bboxOf = (id) => (hold?.id === id ? hold.box : M.info(id)?.bbox ?? null);
  const origin = (el) => [el.x, el.y];

  function hit(p) {                                    // topmost element under the screen point
    const [wx, wy] = s2w(...p), tol = 5 / scale();
    const list = M.elements();
    for (let i = list.length - 1; i >= 0; i--) {
      const b = bboxOf(list[i].id);
      if (b && wx >= b[0] - tol && wx <= b[2] + tol && wy >= b[1] - tol && wy <= b[3] + tol) return list[i].id;
    }
    return null;
  }

  /** Handle under the screen point for the selected element: 'rot' | 'nw' | 'ne' | 'sw' | 'se' | null. */
  function handleAt(p, box) {
    if (!box) return null;
    const hs = handles(box);
    for (const [k, [x, y]] of Object.entries(hs)) if (Math.hypot(p[0] - x, p[1] - y) <= HANDLE + 6) return k;
    return null;
  }
  function handles(b) {                                // screen positions
    const [x0, y0] = w2s(b[0], b[3]), [x1, y1] = w2s(b[2], b[1]);
    return { nw: [x0, y0], ne: [x1, y0], sw: [x0, y1], se: [x1, y1], rot: [(x0 + x1) / 2, y0 - ROT_GAP] };
  }

  /* ---------- live transform of the cached preview strokes (no server round trip while dragging) ---------- */
  function begin(el, mode, p) {
    const pv = store.get('preview'), meta = M.info(el.id);
    if (!pv?.drawing || !meta) return null;
    const strokes = pv.drawing.strokes.slice(meta.range[0], meta.range[1]);
    return { id: el.id, mode, el: { ...el }, start: s2w(...p), startScreen: p, box: meta.bbox, pv,
      range: meta.range, orig: strokes.map((s) => (s.points ?? s.pts).map((q) => [q[0], q[1]])), cur: { dx: 0, dy: 0, k: 1, rot: 0 } };
  }
  function applyLive(d) {
    const { dx, dy, k, rot } = d.cur, [cx, cy] = origin(d.el), c = Math.cos(rot), s = Math.sin(rot);
    for (let i = 0; i < d.orig.length; i++) {
      const st = d.pv.drawing.strokes[d.range[0] + i], dst = st.points ?? st.pts;
      d.orig[i].forEach((q, j) => {
        const x = (q[0] - cx) * k, y = (q[1] - cy) * k;
        dst[j] = [cx + x * c - y * s + dx, cy + x * s + y * c + dy];
      });
    }
    invalidate();
  }
  function liveBox(d) {                                // selection box of the dragged element
    const [cx, cy] = origin(d.el), { dx, dy, k, rot } = d.cur, c = Math.cos(rot), s = Math.sin(rot);
    const [b0, b1, b2, b3] = d.box;
    const pts = [[b0, b1], [b2, b1], [b2, b3], [b0, b3]].map(([x, y]) => {
      const a = (x - cx) * k, b = (y - cy) * k;
      return [cx + a * c - b * s + dx, cy + a * s + b * c + dy];
    });
    const xs = pts.map((q) => q[0]), ys = pts.map((q) => q[1]);
    return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
  }

  function snapMove(d, dx, dy) {                       // snap the dragged box to the board centre, margins and other elements
    const tol = SNAP_PX / scale();
    const cfg = store.get('config')?.board, [sx0, sy0, sx1, sy1] = M.safeRect();
    const xs = [sx0, sx1, (cfg?.width_mm ?? 0) / 2], ys = [sy0, sy1, (cfg?.height_mm ?? 0) / 2];
    for (const e of M.elements()) {
      const b = e.id === d.id ? null : bboxOf(e.id);
      if (b) { xs.push(b[0], b[2], (b[0] + b[2]) / 2); ys.push(b[1], b[3], (b[1] + b[3]) / 2); }
    }
    const b = d.box, mx = [b[0] + dx, b[2] + dx, (b[0] + b[2]) / 2 + dx], my = [b[1] + dy, b[3] + dy, (b[1] + b[3]) / 2 + dy];
    let bx = null, by = null;
    for (const m of mx) for (const t of xs) if (Math.abs(t - m) < tol && (!bx || Math.abs(t - m) < Math.abs(bx.d))) bx = { d: t - m, at: t };
    for (const m of my) for (const t of ys) if (Math.abs(t - m) < tol && (!by || Math.abs(t - m) < Math.abs(by.d))) by = { d: t - m, at: t };
    guides = [bx && ['v', bx.at], by && ['h', by.at]].filter(Boolean);
    return [dx + (bx?.d ?? 0), dy + (by?.d ?? 0)];
  }

  /* ---------- events from board.js ---------- */
  function down(e) {
    if (!active() || e.button !== 0) return false;
    const p = local(e);
    if (!inside(p)) return false;
    const sel = M.selected(), box = sel && bboxOf(sel.id);
    const hnd = sel ? handleAt(p, box) : null;
    let target = sel, mode = 'move';
    if (hnd) mode = hnd;
    else {
      const id = hit(p);
      if (!id) {
        if (sel) { M.select(null); invalidate(); return true; }       // a click on empty board only deselects
        return false;
      }
      M.select(id); target = M.getEl(id);
    }
    drag = begin(target, mode, p);
    if (!drag) return true;
    try { canvas.setPointerCapture(e.pointerId); } catch { /* synthetic */ }
    drag.pointer = e.pointerId; drag.moved = false;
    invalidate();
    return true;
  }

  function move(e) {
    const p = local(e);
    if (!drag) {
      if (active() && inside(p) && (e.pointerType === 'mouse')) {
        const sel = M.selected(), box = sel && bboxOf(sel.id), hnd = sel ? handleAt(p, box) : null, id = hnd ? sel.id : hit(p);
        canvas.style.cursor = hnd === 'rot' ? 'grab' : hnd ? (hnd === 'nw' || hnd === 'se' ? 'nwse-resize' : 'nesw-resize') : id ? 'move' : '';
        if (id !== hoverId) { hoverId = id; invalidate(); }
      } else if (canvas.style.cursor) canvas.style.cursor = '';
      return false;
    }
    if (e.pointerId !== drag.pointer) return true;
    const [wx, wy] = s2w(...p), d = drag, [cx, cy] = origin(d.el);
    if (!d.moved && Math.hypot(p[0] - d.startScreen[0], p[1] - d.startScreen[1]) < 3) return true;
    d.moved = true;
    if (d.mode === 'move') {
      const [dx, dy] = snapMove(d, wx - d.start[0], wy - d.start[1]);
      d.cur = { dx, dy, k: 1, rot: 0 };
    } else if (d.mode === 'rot') {
      let a = Math.atan2(wy - cy, wx - cx) - Math.atan2(d.start[1] - cy, d.start[0] - cx);
      if (e.shiftKey) { const deg = Math.round(((d.el.rot ?? 0) + a * 180 / Math.PI) / 15) * 15; a = (deg - (d.el.rot ?? 0)) * Math.PI / 180; }
      d.cur = { dx: 0, dy: 0, k: 1, rot: a };
    } else {
      const k = Math.max(0.05, Math.hypot(wx - cx, wy - cy) / Math.max(1e-6, Math.hypot(d.start[0] - cx, d.start[1] - cy)));
      d.cur = { dx: 0, dy: 0, k, rot: 0 };
    }
    applyLive(d);
    return true;
  }

  function up(e) {
    if (!drag || e.pointerId !== drag.pointer) return false;
    const d = drag; guides = [];
    if (d.moved) hold = { id: d.id, box: liveBox(d) };
    drag = null;
    if (d.moved) {
      const el = d.el, { dx, dy, k, rot } = d.cur;
      if (d.mode === 'move') M.update(el.id, { x: round(el.x + dx), y: round(el.y + dy) });
      else if (d.mode === 'rot') M.update(el.id, { rot: round((((el.rot ?? 0) + rot * 180 / Math.PI) % 360 + 540) % 360 - 180) });
      else M.update(el.id, M.scaled(el, k));
    }
    invalidate();
    return true;
  }
  const round = (v) => Math.round(v * 100) / 100;
  const inside = (p) => { const [x0, y0, x1, y1] = area(); return p[0] >= x0 && p[0] <= x1 && p[1] >= y0 && p[1] <= y1; };

  /* ---------- drawing ---------- */
  function draw(ctx, th) {
    if (!active()) return;
    const sel = M.selected(), [sx0, sy0, sx1, sy1] = M.safeRect();
    const outside = (b) => b && (b[0] < sx0 - 0.5 || b[1] < sy0 - 0.5 || b[2] > sx1 + 0.5 || b[3] > sy1 + 0.5);
    ctx.save();
    for (const e of M.elements()) {                    // errors and elements outside the drawing area
      const b = drag?.id === e.id ? liveBox(drag) : bboxOf(e.id);
      if (!b) continue;
      if (outside(b) && e.id !== sel?.id) box(ctx, b, '#c0271f', [3, 3], 1.2, 0.8);
      if (e.id === hoverId && e.id !== sel?.id && !drag) box(ctx, b, th.accent, [4, 3], 1, 0.7);
    }
    if (sel) {
      const b = drag?.id === sel.id ? liveBox(drag) : bboxOf(sel.id);
      if (b) {
        box(ctx, b, outside(b) ? '#c0271f' : th.accent, [], 1.6, 1);
        const hs = handles(b);
        ctx.strokeStyle = th.accent; ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.moveTo((hs.nw[0] + hs.ne[0]) / 2, hs.nw[1]); ctx.lineTo(hs.rot[0], hs.rot[1]); ctx.stroke();
        for (const [k, [x, y]] of Object.entries(hs)) {
          ctx.fillStyle = th.surface; ctx.beginPath();
          if (k === 'rot') ctx.arc(x, y, HANDLE + 1, 0, 7); else ctx.rect(x - HANDLE, y - HANDLE, HANDLE * 2, HANDLE * 2);
          ctx.fill(); ctx.stroke();
        }
        if (drag?.moved && drag.mode !== 'move') readout(ctx, th, drag, hs);
      }
    }
    ctx.setLineDash([5, 4]); ctx.strokeStyle = '#d4237a'; ctx.lineWidth = 1;
    const [ax, ay, aw, ah] = area();
    for (const [kind, v] of guides) {
      ctx.beginPath();
      if (kind === 'v') { const x = w2s(v, 0)[0]; ctx.moveTo(x, ay); ctx.lineTo(x, ah); } else { const y = w2s(0, v)[1]; ctx.moveTo(ax, y); ctx.lineTo(aw, y); }
      ctx.stroke();
    }
    ctx.restore();
  }
  function box(ctx, b, color, dash, lw, alpha) {
    const [x0, y0] = w2s(b[0], b[3]), [x1, y1] = w2s(b[2], b[1]);
    ctx.setLineDash(dash); ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.globalAlpha = alpha;
    ctx.strokeRect(x0 - 3, y0 - 3, x1 - x0 + 6, y1 - y0 + 6); ctx.globalAlpha = 1; ctx.setLineDash([]);
  }
  function readout(ctx, th, d, hs) {
    const txt = d.mode === 'rot' ? `${Math.round((((d.el.rot ?? 0) + d.cur.rot * 180 / Math.PI) % 360 + 540) % 360 - 180)}°` : `${Math.round(d.cur.k * 100)} %`;
    const [x, y] = d.mode === 'rot' ? hs.rot : hs.se;
    ctx.font = `12px ${th.font}`; ctx.textBaseline = 'middle';
    const w = ctx.measureText(txt).width + 12;
    ctx.fillStyle = th.accent; ctx.fillRect(x + 10, y - 10, w, 20);
    ctx.fillStyle = '#fff'; ctx.textAlign = 'left'; ctx.fillText(txt, x + 16, y);
  }

  /* ---------- drop from the palette ---------- */
  canvas.addEventListener('dragover', (e) => { if (e.dataTransfer?.types?.includes(EVT)) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
  canvas.addEventListener('drop', (e) => {
    const raw = e.dataTransfer?.getData(EVT);
    if (!raw) return;
    e.preventDefault(); e.stopPropagation();
    try {
      const { type, props } = JSON.parse(raw);
      const at = s2w(...local(e));
      store.set({ tab: 'draw' });
      M.add(type, props ?? {}, { at: [Math.round(at[0]), Math.round(at[1])] });
    } catch { /* not ours */ }
  });

  /* ---------- keyboard ---------- */
  window.addEventListener('keydown', (e) => {
    if (!active() || isTyping(e.target) || document.querySelector('dialog[open]')) return;
    const sel = M.selected(), ctrl = e.ctrlKey || e.metaKey, k = e.key;
    if (ctrl && k.toLowerCase() === 'z') { e.preventDefault(); e.shiftKey ? M.redo() : M.undo(); return; }
    if (ctrl && k.toLowerCase() === 'y') { e.preventDefault(); M.redo(); return; }
    if (!sel) return;
    if (ctrl && k.toLowerCase() === 'd') { e.preventDefault(); M.duplicate(sel.id); return; }
    if (k === 'Delete' || k === 'Backspace') { e.preventDefault(); M.remove(sel.id); return; }
    if (k === 'Escape') { if (drag) { cancel(); } else M.select(null); return; }
    const step = e.shiftKey ? 10 : 1, dir = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] }[k];
    if (dir) { e.preventDefault(); M.update(sel.id, { x: round(sel.x + dir[0] * step), y: round(sel.y + dir[1] * step) }, 'nudge'); }
  });
  function cancel() { if (!drag) return; drag.cur = { dx: 0, dy: 0, k: 1, rot: 0 }; applyLive(drag); drag = null; guides = []; invalidate(); }

  store.on('preview', () => { hold = null; });
  store.on(['elements', 'selection', 'preview', 'tab'], () => invalidate());
  return { down, move, up, draw };
}

export const DRAG_TYPE = EVT;
