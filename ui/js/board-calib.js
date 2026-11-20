// Kalibrier-Overlay für die Brettansicht: nummerierte Zielpunkte, Messabweichung und Restfehler-Pfeile (×20).
export const CAL_SCALE = 20;

function arrow(ctx, x0, y0, x1, y1) {
  ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
  const a = Math.atan2(y1 - y0, x1 - x0), l = 6;
  ctx.beginPath(); ctx.moveTo(x1, y1);
  ctx.lineTo(x1 - l * Math.cos(a - 0.45), y1 - l * Math.sin(a - 0.45));
  ctx.moveTo(x1, y1);
  ctx.lineTo(x1 - l * Math.cos(a + 0.45), y1 - l * Math.sin(a + 0.45));
  ctx.stroke();
}

/** Wird aus board.draw() innerhalb des Clips aufgerufen. */
export function drawCalibration(ctx, w2s, th, calib, view, W, RL = 0) {
  if (!calib?.targets?.length) return;
  const cs = getComputedStyle(document.documentElement);
  const danger = cs.getPropertyValue('--danger').trim() || '#c0271f', ok = cs.getPropertyValue('--ok').trim() || '#1b7f3b';
  ctx.lineCap = 'round'; ctx.lineJoin = 'round';

  // gestrichelt: Messung minus Soll, ×20 (vor der Auswertung sichtbar)
  ctx.setLineDash([3, 3]); ctx.lineWidth = 1.2; ctx.strokeStyle = th.truth;
  calib.targets.forEach(([x, y], i) => {
    const m = calib.measured[i];
    if (!m) return;
    const [sx, sy] = w2s(x, y), [ex, ey] = w2s(x + (m[0] - x) * CAL_SCALE, y + (m[1] - y) * CAL_SCALE);
    ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(ex, ey); ctx.stroke();
  });
  ctx.setLineDash([]);

  // durchgezogen: Restfehler nach der Auswertung, ×20
  const res = calib.result;
  if (res && !res.stale) {
    ctx.lineWidth = 2;
    calib.rowIdx.forEach((ti, k) => {
      const v = res.residual_vectors?.[k], t = calib.targets[ti];
      if (!v || !t) return;
      const [sx, sy] = w2s(t[0], t[1]), [ex, ey] = w2s(t[0] + v[0] * CAL_SCALE, t[1] + v[1] * CAL_SCALE);
      ctx.strokeStyle = res.outliers?.includes(k) ? danger : th.accent;
      arrow(ctx, sx, sy, ex, ey);
    });
  }

  // nummerierte Zielpunkte
  ctx.font = `600 11px ${th.font}`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  calib.targets.forEach(([x, y], i) => {
    const [sx, sy] = w2s(x, y), hot = calib.hot === i, filled = !!calib.measured[i];
    const r = hot ? 12 : 9;
    ctx.beginPath(); ctx.arc(sx, sy, r, 0, 7);
    ctx.fillStyle = filled ? ok : th.surface; ctx.fill();
    ctx.lineWidth = hot ? 3 : 1.6; ctx.strokeStyle = hot ? th.truth : (filled ? ok : th.accent); ctx.stroke();
    ctx.fillStyle = filled ? '#fff' : th.ink; ctx.fillText(String(i + 1), sx, sy + 0.5);
  });

  // Legende
  const bw0 = 262, lx = (W + RL) / 2 + bw0 / 2, ly = 14;
  ctx.textAlign = 'left'; ctx.textBaseline = 'middle'; ctx.font = `12px ${th.font}`; ctx.fillStyle = th.ink;
  const lines = [['gestrichelt: Messung − Soll', th.truth, true], ['Pfeil: Restfehler nach Auswertung', th.accent, false],
    [`Beides ${CAL_SCALE}-fach vergrößert`, null, false]];
  const bw = bw0, bh = lines.length * 18 + 8;
  ctx.fillStyle = cs.getPropertyValue('--surface').trim(); ctx.globalAlpha = .9;
  ctx.fillRect(lx - bw, ly - 10, bw, bh); ctx.globalAlpha = 1;
  ctx.strokeStyle = th.edge; ctx.lineWidth = 1; ctx.strokeRect(lx - bw + .5, ly - 9.5, bw - 1, bh - 1);
  lines.forEach(([t, col, dash], i) => {
    const y = ly + 4 + i * 18;
    ctx.fillStyle = cs.getPropertyValue('--text').trim(); ctx.fillText(t, lx - bw + (col ? 38 : 10), y);
    if (col) { ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.setLineDash(dash ? [3, 3] : []); ctx.beginPath(); ctx.moveTo(lx - bw + 8, y); ctx.lineTo(lx - bw + 30, y); ctx.stroke(); ctx.setLineDash([]); }
  });
}
