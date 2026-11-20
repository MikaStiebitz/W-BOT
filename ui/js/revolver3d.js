// Interactive 3D revolver: a tiny software renderer on a 2D canvas (no libraries).
// The drum turns about the horizontal X axis, the pens fan out towards the whiteboard (-Z).
// Drag to orbit, click a pen to select it, arrow keys switch pens. The sample stroke on the board
// uses the selected pen's colour and real line width.
const RAD = Math.PI / 180;
const R_DRUM = 52, HALF_W = 15, R_PEN_IN = 40, R_BARREL = 8.5, L_END = 150, L_TIP = 172;
const BOARD_Z = -L_TIP;

function rgb(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec((hex || '').trim()) || /^#?([0-9a-f]{3})$/i.exec((hex || '').trim());
  if (!m) return [60, 60, 60];
  let s = m[1]; if (s.length === 3) s = [...s].map((c) => c + c).join('');
  return [parseInt(s.slice(0, 2), 16), parseInt(s.slice(2, 4), 16), parseInt(s.slice(4, 6), 16)];
}
const mix = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);
const css = (c, k = 1) => `rgb(${Math.round(Math.min(255, c[0] * k))},${Math.round(Math.min(255, c[1] * k))},${Math.round(Math.min(255, c[2] * k))})`;

export function mountRevolver(wrap, { onSelect } = {}) {
  const canvas = document.createElement('canvas');
  canvas.className = 'rev-canvas'; canvas.tabIndex = 0; canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', '3D view of the pen revolver. Drag to rotate, click a pen to select it, arrow keys switch pens.');
  wrap.append(canvas);
  const ctx = canvas.getContext('2d');

  const S = { pens: [], slots: [-60, 0, 60], selected: 0, angle: 0, target: 0, drum: 0, yaw: 52, pitch: -12, zoom: 1, userYaw: false };
  let W = 0, H = 0, dpr = 1, raf = 0, running = false, faces = [], t0 = performance.now();

  function resize() {
    const r = wrap.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) return;
    dpr = window.devicePixelRatio || 1; W = Math.round(r.width); H = Math.round(r.height);
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
  }
  new ResizeObserver(resize).observe(wrap);

  /* ---------- geometry ---------- */
  function project(v) {                       // v = [x,y,z] scene -> screen
    const cy = Math.cos(S.yaw * RAD), sy = Math.sin(S.yaw * RAD), cp = Math.cos(S.pitch * RAD), sp = Math.sin(S.pitch * RAD);
    let [x, y, z] = v;
    [x, z] = [x * cy + z * sy, -x * sy + z * cy];          // yaw about Y
    [y, z] = [y * cp - z * sp, y * sp + z * cp];            // pitch about X
    const D = 760, k = (D / (D - z)) * S.scale;
    return [W / 2 + x * k, H * 0.42 - (y + 18) * k, z];
  }
  function rot(n) {                           // rotate a normal (no translation)
    const cy = Math.cos(S.yaw * RAD), sy = Math.sin(S.yaw * RAD), cp = Math.cos(S.pitch * RAD), sp = Math.sin(S.pitch * RAD);
    let [x, y, z] = n;
    [x, z] = [x * cy + z * sy, -x * sy + z * cy];
    [y, z] = [y * cp - z * sp, y * sp + z * cp];
    return [x, y, z];
  }
  const LIGHT = (() => { const l = [-0.35, 0.75, 0.6], n = Math.hypot(...l); return l.map((v) => v / n); })();

  function pushFace(pts, color, normal, extra = {}) {
    const p = pts.map(project);
    const z = p.reduce((s, q) => s + q[2], 0) / p.length;
    const n = rot(normal), lam = Math.max(0, n[0] * LIGHT[0] + n[1] * LIGHT[1] + n[2] * LIGHT[2]);
    faces.push({ p, z, k: 0.5 + 0.55 * lam, color, ...extra });
  }

  function buildScene() {
    faces = [];
    const dark = getComputedStyle(document.documentElement).getPropertyValue('--board-surface').trim();
    const boardCol = rgb(dark || '#f2f4f5');
    // whiteboard behind the pens
    const bw = 420, bh = 260;
    pushFace([[-bw, -bh + 40, BOARD_Z], [bw, -bh + 40, BOARD_Z], [bw, bh + 40, BOARD_Z], [-bw, bh + 40, BOARD_Z]], boardCol, [0, 0, 1], { board: true, z: -9999 });
    // drum: two discs + rim
    const N = 36;
    for (const sgn of [-1, 1]) {
      const ring = []; for (let i = 0; i < N; i++) { const a = i / N * 2 * Math.PI; ring.push([sgn * HALF_W, R_DRUM * Math.cos(a), R_DRUM * Math.sin(a)]); }
      pushFace(ring, [92, 100, 108], [sgn, 0, 0], { drum: true });
    }
    for (let i = 0; i < N; i++) {
      const a0 = i / N * 2 * Math.PI, a1 = (i + 1) / N * 2 * Math.PI;
      const rr = (a) => [Math.cos(a) * R_DRUM, Math.sin(a) * R_DRUM];
      const [y0, z0] = rr(a0), [y1, z1] = rr(a1);
      pushFace([[-HALF_W, y0, z0], [HALF_W, y0, z0], [HALF_W, y1, z1], [-HALF_W, y1, z1]], [120, 130, 140], [0, Math.cos((a0 + a1) / 2), Math.sin((a0 + a1) / 2)], { drum: true });
    }
    // axle
    for (const sgn of [-1, 1]) {
      const ring = []; for (let i = 0; i < 16; i++) { const a = i / 16 * 2 * Math.PI; ring.push([sgn * (HALF_W + 14), 12 * Math.cos(a), 12 * Math.sin(a)]); }
      pushFace(ring, [60, 66, 72], [sgn, 0, 0], { drum: true });
    }
    // pens
    S.pens.forEach((pen, i) => {
      const slot = S.slots[i] ?? 0;
      const a = (S.drum - slot) * RAD;                       // 0 = pen points at the board
      const d = [0, Math.sin(a), -Math.cos(a)], p = [1, 0, 0], q = [0, Math.cos(a), Math.sin(a)];
      const sel = i === S.selected, lift = sel ? 7 : 0;
      const base = rgb(pen.hex), barrel = mix(base, [236, 238, 240], 0.72);
      const tipR = 1.6 + Math.min(8, Math.max(0.3, pen.width_mm || 2)) * 0.55;
      const seg = 14;
      const at = (s, r, th) => [d[0] * (s + lift) + r * (Math.cos(th) * p[0] + Math.sin(th) * q[0]),
        d[1] * (s + lift) + r * (Math.cos(th) * p[1] + Math.sin(th) * q[1]),
        d[2] * (s + lift) + r * (Math.cos(th) * p[2] + Math.sin(th) * q[2])];
      const bands = [[R_PEN_IN, 66, R_BARREL, base], [66, 132, R_BARREL, barrel], [132, L_END, R_BARREL - 1.5, base]];
      for (const [s0, s1, r, col] of bands) {
        for (let k = 0; k < seg; k++) {
          const t0_ = k / seg * 2 * Math.PI, t1_ = (k + 1) / seg * 2 * Math.PI, tm = (t0_ + t1_) / 2;
          pushFace([at(s0, r, t0_), at(s1, r, t0_), at(s1, r, t1_), at(s0, r, t1_)], col,
            [Math.cos(tm) * p[0] + Math.sin(tm) * q[0], Math.cos(tm) * p[1] + Math.sin(tm) * q[1], Math.cos(tm) * p[2] + Math.sin(tm) * q[2]], { pen: i, sel });
        }
      }
      for (let k = 0; k < seg; k++) {                          // nib cone
        const t0_ = k / seg * 2 * Math.PI, t1_ = (k + 1) / seg * 2 * Math.PI, tm = (t0_ + t1_) / 2;
        pushFace([at(L_END, R_BARREL - 1.5, t0_), at(L_TIP, tipR, t0_), at(L_TIP, tipR, t1_), at(L_END, R_BARREL - 1.5, t1_)], base,
          [Math.cos(tm) * p[0] + Math.sin(tm) * q[0], Math.cos(tm) * p[1] + Math.sin(tm) * q[1], Math.cos(tm) * p[2] + Math.sin(tm) * q[2]], { pen: i, sel });
      }
      const capRing = []; for (let k = 0; k < seg; k++) capRing.push(at(R_PEN_IN, R_BARREL, k / seg * 2 * Math.PI));
      pushFace(capRing, mix(base, [0, 0, 0], 0.25), [-d[0], -d[1], -d[2]], { pen: i, sel });
    });
    faces.sort((a, b) => a.z - b.z);
  }

  /* ---------- drawing ---------- */
  function draw() {
    if (!W) return;
    S.scale = Math.min(W / 560, H / 400) * S.zoom;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const cs = getComputedStyle(document.documentElement);
    const accent = cs.getPropertyValue('--accent').trim() || '#0a7a74';
    buildScene();
    let sampleDone = false;
    for (const f of faces) {
      ctx.beginPath(); f.p.forEach((q, i) => (i ? ctx.lineTo(q[0], q[1]) : ctx.moveTo(q[0], q[1]))); ctx.closePath();
      ctx.fillStyle = f.board ? css(f.color, 1) : css(f.color, f.k);
      if (f.sel) { ctx.shadowColor = accent; ctx.shadowBlur = 16; } else ctx.shadowBlur = 0;
      ctx.fill();
      ctx.shadowBlur = 0;
      ctx.lineWidth = 0.5;
      ctx.strokeStyle = f.board ? 'rgba(0,0,0,.18)' : 'rgba(0,0,0,.16)';
      ctx.stroke();
      if (f.board && !sampleDone) { sampleDone = true; drawSample(); }
    }
    // labels under the pens
    ctx.font = `600 13px ${cs.getPropertyValue('--font-ui') || 'sans-serif'}`; ctx.textAlign = 'center';
    S.pens.forEach((pen, i) => {
      const a = (S.drum - (S.slots[i] ?? 0)) * RAD;
      const tip = project([0, Math.sin(a) * (L_TIP + 22), -Math.cos(a) * (L_TIP + 22)]);
      ctx.fillStyle = i === S.selected ? accent : (cs.getPropertyValue('--text-2').trim() || '#555');
      ctx.fillText(`${i + 1}`, tip[0], tip[1] + (Math.sin(a) >= 0 ? -6 : 14));
    });
  }

  function drawSample() {                       // the stroke the active pen leaves on the board (real width)
    const pen = S.pens[S.selected]; if (!pen) return;
    const w = Math.max(0.5, pen.width_mm || 2);
    ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.strokeStyle = pen.hex; ctx.lineWidth = Math.max(1, w * 1.9 * S.scale);
    ctx.beginPath();
    for (let i = 0; i <= 60; i++) {
      const x = -250 + i * (500 / 60), y = 22 * Math.sin(x / 70);        // passes through the pen tip at x = 0
      const q = project([x, y, BOARD_Z + 0.5]);
      i ? ctx.lineTo(q[0], q[1]) : ctx.moveTo(q[0], q[1]);
    }
    ctx.stroke();
  }

  /* ---------- loop ---------- */
  function frame(now) {
    raf = 0;
    if (!running) return;
    // ease the drum towards its target angle
    const dt = Math.min(0.05, (now - t0) / 1000); t0 = now;
    S.drum += (S.target - S.drum) * (1 - Math.exp(-dt * 7));
    if (Math.abs(S.target - S.drum) < 0.02) S.drum = S.target;
    if (!S.userYaw && !drag) S.yaw = 52 + 9 * Math.sin(now / 2800);
    draw();
    raf = requestAnimationFrame(frame);
  }
  function kick() { if (running && !raf) { t0 = performance.now(); raf = requestAnimationFrame(frame); } }

  /* ---------- input ---------- */
  let drag = null;
  canvas.addEventListener('pointerdown', (e) => { canvas.setPointerCapture(e.pointerId); drag = { x: e.clientX, y: e.clientY, moved: false, yaw: S.yaw, pitch: S.pitch }; });
  canvas.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.hypot(dx, dy) > 4) drag.moved = true;
    if (drag.moved) { S.yaw = drag.yaw + dx * 0.4; S.pitch = Math.max(-60, Math.min(30, drag.pitch + dy * 0.3)); S.userYaw = true; }
  });
  canvas.addEventListener('pointerup', (e) => {
    const d = drag; drag = null;
    if (!d || d.moved) return;
    const r = canvas.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    for (let i = faces.length - 1; i >= 0; i--) {                     // top-most pen face under the pointer
      const f = faces[i]; if (f.pen == null) continue;
      if (inside(f.p, x, y)) { onSelect?.(f.pen); return; }
    }
  });
  canvas.addEventListener('wheel', (e) => { e.preventDefault(); S.zoom = Math.max(0.6, Math.min(2.2, S.zoom * Math.exp(-e.deltaY * 0.001))); }, { passive: false });
  canvas.addEventListener('dblclick', () => { S.userYaw = false; S.pitch = -12; S.zoom = 1; });
  canvas.addEventListener('keydown', (e) => {
    const n = S.pens.length; if (!n) return;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { onSelect?.((S.selected + 1) % n); e.preventDefault(); }
    if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { onSelect?.((S.selected + n - 1) % n); e.preventDefault(); }
  });
  function inside(poly, x, y) {
    let c = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const [xi, yi] = poly[i], [xj, yj] = poly[j];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c;
    }
    return c;
  }

  return {
    /** pens: [{hex, width_mm}], slots: servo angle per pen, selected: index, angle: current drum angle (deg) */
    set({ pens, slots, selected, angle }) {
      if (pens) S.pens = pens;
      if (slots) S.slots = slots;
      if (selected != null) S.selected = selected;
      if (angle != null) S.target = angle;
      kick();
    },
    start() { if (!running) { running = true; resize(); S.drum = S.target; kick(); } },
    stop() { running = false; if (raf) cancelAnimationFrame(raf); raf = 0; },
  };
}
