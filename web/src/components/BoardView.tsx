import { useCallback, useEffect, useRef, useState } from "react";
import { api, ws } from "../api";
import { useStore } from "../store";
import type { Point } from "../types";

/**
 * The board, drawn to a single canvas.
 *
 * Everything is one canvas rather than DOM nodes per path: a traced photo has
 * thousands of polylines and the browser would collapse trying to lay out an
 * SVG element for each. Redraws are driven by requestAnimationFrame so a
 * 12 Hz status feed and a 60 Hz mouse never queue up behind each other.
 *
 * In sketch mode the stroke being drawn lives in a ref, not in state — the
 * rAF loop reads it directly, so a fast mouse never forces React renders.
 */
export function BoardView() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 900, height: 600 });
  const localStrokeRef = useRef<Point[] | null>(null);
  const pendingLocalRef = useRef<{ points: Point[]; color: number }[]>([]);
  const frameRef = useRef(0);

  const {
    config, status, safeArea, heatmap, showHeatmap, showSafeArea, showTravel,
    plannedPaths, mode, ams, sketchActive, sketchColor, sketchStrokes,
  } = useStore();

  // A queued echo from the server replaces the oldest optimistic local copy —
  // count *new* stroke ids, not arbitrary status changes, or the optimistic
  // stroke would vanish before its echo arrives.
  const knownStrokeCountRef = useRef(0);
  useEffect(() => {
    const count = Object.keys(sketchStrokes).length;
    const added = count - knownStrokeCountRef.current;
    knownStrokeCountRef.current = count;
    for (let i = 0; i < added && pendingLocalRef.current.length; i++) {
      pendingLocalRef.current.shift();
    }
  }, [sketchStrokes]);

  // -- responsive sizing -------------------------------------------------
  useEffect(() => {
    const element = wrapRef.current;
    if (!element) return;
    const observer = new ResizeObserver(() => {
      const rect = element.getBoundingClientRect();
      setSize({ width: Math.max(rect.width, 200), height: Math.max(rect.height, 150) });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const boardWidth = config?.board.width_mm ?? 1500;
  const boardHeight = config?.board.height_mm ?? 1000;

  /** Board millimetres -> canvas pixels (Y flipped: the board grows upward). */
  const transform = useCallback(() => {
    const pad = 28;
    const scale = Math.min(
      (size.width - pad * 2) / boardWidth,
      (size.height - pad * 2) / boardHeight,
    );
    const offsetX = (size.width - boardWidth * scale) / 2;
    const offsetY = (size.height - boardHeight * scale) / 2;
    return {
      scale,
      toPx: (x: number, y: number): [number, number] => [
        offsetX + x * scale,
        offsetY + (boardHeight - y) * scale,
      ],
      toMm: (px: number, py: number): [number, number] => [
        (px - offsetX) / scale,
        boardHeight - (py - offsetY) / scale,
      ],
    };
  }, [size, boardWidth, boardHeight]);

  // Keep the backing store in step with the CSS size here rather than inside
  // the animation loop: requestAnimationFrame does not fire in a hidden tab,
  // and a canvas left at its default 300x150 would be stretched and blurry the
  // moment the tab became visible again.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(size.width * dpr);
    canvas.height = Math.round(size.height * dpr);
  }, [size]);

  // -- rendering ---------------------------------------------------------
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const slotHex = (index: number) =>
      ams[index]?.hex ?? config?.colors[index]?.hex ?? "#111";

    const drawPolyline = (points: Point[], stroke: string, width: number,
                          alpha = 1, dash: number[] = []) => {
      if (points.length < 2) return;
      const { toPx } = transform();
      ctx.strokeStyle = stroke;
      ctx.globalAlpha = alpha;
      ctx.lineWidth = width;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.setLineDash(dash);
      ctx.beginPath();
      points.forEach(([x, y], index) => {
        const [px, py] = toPx(x, y);
        index === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
      });
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
    };

    const render = () => {
      const dpr = window.devicePixelRatio || 1;
      if (canvas.width !== Math.round(size.width * dpr)) {
        canvas.width = Math.round(size.width * dpr);
        canvas.height = Math.round(size.height * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, size.width, size.height);

      const { scale, toPx } = transform();
      const strokePx = Math.max((config?.marker_line_width_mm ?? 2) * scale, 0.9);

      // board
      const [bx, by] = toPx(0, boardHeight);
      ctx.fillStyle = "#ffffff";
      ctx.shadowColor = "rgba(15, 30, 60, 0.12)";
      ctx.shadowBlur = 14;
      ctx.fillRect(bx, by, boardWidth * scale, boardHeight * scale);
      ctx.shadowBlur = 0;
      ctx.strokeStyle = "#c3ccd9";
      ctx.lineWidth = 1.5;
      ctx.strokeRect(bx, by, boardWidth * scale, boardHeight * scale);

      // heatmap
      if (showHeatmap && heatmap) {
        const cellW = ((heatmap.x[1] ?? 25) - (heatmap.x[0] ?? 0)) * scale;
        const cellH = ((heatmap.y[1] ?? 25) - (heatmap.y[0] ?? 0)) * scale;
        for (let j = 0; j < heatmap.y.length; j++) {
          for (let i = 0; i < heatmap.x.length; i++) {
            const amp = heatmap.error_amplification[j][i];
            const tension = heatmap.min_tension[j][i];
            const feasible = heatmap.feasible[j][i];
            const [px, py] = toPx(heatmap.x[i], heatmap.y[j]);
            if (!feasible) {
              ctx.fillStyle = "rgba(220, 40, 40, 0.28)";
            } else {
              const bad = Math.min(Math.max((amp - 1) / 2.5, 0), 1);
              const slack = Math.min(Math.max((0.9 - tension) / 0.9, 0), 1);
              const severity = Math.max(bad, slack * 0.7);
              const hue = 120 - severity * 110;
              ctx.fillStyle = `hsla(${hue}, 75%, 50%, 0.30)`;
            }
            ctx.fillRect(px - cellW / 2, py - cellH / 2, cellW + 1, cellH + 1);
          }
        }
      }

      // safe area
      if (showSafeArea && safeArea.length) {
        ctx.strokeStyle = "#2e7d32";
        ctx.setLineDash([7, 5]);
        ctx.lineWidth = 1.5;
        for (const ring of safeArea) {
          ctx.beginPath();
          ring.forEach(([x, y], index) => {
            const [px, py] = toPx(x, y);
            index === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
          });
          ctx.closePath();
          ctx.stroke();
        }
        ctx.setLineDash([]);
      }

      // planned paths (image mode / calibration pattern)
      if (plannedPaths.length) {
        let previousEnd: Point | null = null;
        for (const path of plannedPaths) {
          if (path.points.length < 2) continue;
          if (showTravel && previousEnd) {
            drawPolyline([previousEnd, path.points[0]],
                         "rgba(120, 140, 170, 0.4)", 0.7, 1, [3, 4]);
          }
          drawPolyline(path.closed
            ? [...path.points, path.points[0]] : path.points,
            slotHex(path.color), strokePx, 0.5);
          previousEnd = path.points[path.points.length - 1];
        }
      }

      // sketch strokes: queued faint, drawing dashed, done solid
      for (const stroke of Object.values(sketchStrokes)) {
        const hex = slotHex(stroke.color);
        if (stroke.status === "queued") {
          drawPolyline(stroke.points, hex, strokePx, 0.28);
        } else if (stroke.status === "drawing") {
          drawPolyline(stroke.points, hex, strokePx + 1, 0.55, [6, 4]);
        } else if (stroke.status === "done") {
          drawPolyline(stroke.points, hex, strokePx, 1);
        } else {
          drawPolyline(stroke.points, "#c62828", 1, 0.5, [2, 3]);
        }
      }

      // optimistic local strokes not yet echoed by the server
      for (const local of pendingLocalRef.current) {
        drawPolyline(local.points, slotHex(local.color), strokePx, 0.28);
      }
      // the stroke under the cursor right now
      if (localStrokeRef.current && localStrokeRef.current.length > 1) {
        drawPolyline(localStrokeRef.current, slotHex(sketchColor),
                     strokePx, 0.6);
      }

      // gondola + belts
      if (status) {
        const [px, py] = toPx(status.position.x, status.position.y);
        if (config) {
          ctx.strokeStyle = "rgba(150, 160, 175, 0.55)";
          ctx.lineWidth = 1;
          for (const anchor of [config.geometry.anchor_left, config.geometry.anchor_right]) {
            const [ax, ay] = toPx(anchor[0], anchor[1]);
            ctx.beginPath();
            ctx.moveTo(ax, ay);
            ctx.lineTo(px, py);
            ctx.stroke();
            ctx.fillStyle = "#6b7280";
            ctx.beginPath();
            ctx.arc(ax, ay, 4, 0, Math.PI * 2);
            ctx.fill();
          }
        }
        const penDown = status.pen_down;
        ctx.fillStyle = penDown ? slotHex(status.color) : "rgba(255,255,255,0.92)";
        ctx.strokeStyle = "#1f2937";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(px, py, penDown ? 7 : 9, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      }

      frameRef.current = requestAnimationFrame(render);
    };

    render();
    return () => cancelAnimationFrame(frameRef.current);
  }, [size, transform, config, status, safeArea, heatmap, showHeatmap,
      showSafeArea, showTravel, plannedPaths, sketchStrokes, sketchColor,
      ams, boardWidth, boardHeight]);

  // -- pointer handling --------------------------------------------------
  const toBoard = useCallback((event: React.PointerEvent): Point => {
    const rect = canvasRef.current!.getBoundingClientRect();
    return transform().toMm(event.clientX - rect.left, event.clientY - rect.top);
  }, [transform]);

  const onPointerDown = (event: React.PointerEvent) => {
    const point = toBoard(event);
    if (sketchActive) {
      (event.target as Element).setPointerCapture(event.pointerId);
      localStrokeRef.current = [point];
    } else if (mode === "manual") {
      void useStore.getState().run(() => api.move(point[0], point[1]));
    }
  };

  const onPointerMove = (event: React.PointerEvent) => {
    if (!sketchActive || !localStrokeRef.current) return;
    const point = toBoard(event);
    const last = localStrokeRef.current[localStrokeRef.current.length - 1];
    const dx = point[0] - last[0];
    const dy = point[1] - last[1];
    if (dx * dx + dy * dy >= 1.0) {          // >= 1 mm of real movement
      localStrokeRef.current.push(point);
    }
  };

  const finishStroke = () => {
    const points = localStrokeRef.current;
    localStrokeRef.current = null;
    if (!points || points.length < 2) return;
    const color = useStore.getState().sketchColor;
    pendingLocalRef.current.push({ points, color });
    ws.send("stroke", { points, color });
  };

  const onPointerUp = (event: React.PointerEvent) => {
    (event.target as Element).releasePointerCapture?.(event.pointerId);
    if (sketchActive) finishStroke();
  };

  const onPointerLeave = () => {
    if (sketchActive) finishStroke();
  };

  const cursorStyle = sketchActive ? "crosshair"
    : mode === "manual" ? "pointer" : "default";

  return (
    <div className="board-wrap" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        style={{ width: size.width, height: size.height, cursor: cursorStyle,
                 touchAction: "none" }}
        onPointerMove={onPointerMove}
        onPointerDown={onPointerDown}
        onPointerUp={onPointerUp}
        onPointerLeave={onPointerLeave}
      />
      <div className="board-overlays">
        <label>
          <input type="checkbox" checked={showSafeArea}
                 onChange={() => useStore.getState().toggleSafeArea()} />
          Sicherer Bereich
        </label>
        <label>
          <input type="checkbox" checked={showHeatmap}
                 onChange={() => useStore.getState().toggleHeatmap()} />
          Genauigkeits-Heatmap
        </label>
        <label>
          <input type="checkbox" checked={showTravel}
                 onChange={() => useStore.getState().toggleTravel()} />
          Leerfahrten
        </label>
      </div>
      {sketchActive && (
        <div className="board-badge">
          Vormalen aktiv — Farbe{" "}
          <span className="swatch"
                style={{ background: ams[sketchColor]?.hex ?? "#111" }} />
        </div>
      )}
    </div>
  );
}
