// Drawing JSON (contract): {name, strokes:[{color:int(0-based), points:[[x,y],...]}]} in board mm.
// Tolerates `pts` instead of `points`.

export function strokesOf(drawing) {
  const out = [];
  for (const s of drawing?.strokes ?? []) {
    const pts = Array.isArray(s) ? s : (s.points ?? s.pts ?? []);
    if (pts.length) out.push({ points: pts, color: Array.isArray(s) ? 0 : (s.color ?? 0) });
  }
  return out;
}

export function drawingBounds(drawing) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const s of strokesOf(drawing)) for (const [x, y] of s.points) {
    if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
  }
  return Number.isFinite(x0) ? [x0, y0, x1, y1] : null;
}
