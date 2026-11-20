"""The allowed drawing area that fits the motors where they really are.

``workspace.suggest_rect`` looks for a rectangle centred on the board. When the motors are not at the
board corners (measured by the camera, or simply mounted elsewhere) the usable region - between and
below the motors, where both belts pull and small belt errors stay small - is off-centre. This finds
the largest axis-aligned rectangle of grid cells that are inside the board margin, reachable with an
error amplification <= ``max_amp`` and a belt tension >= ``min_tension_n`` on both belts.
"""
from __future__ import annotations

import numpy as np

from .config import Config
from .workspace import amplification, tensions


def _largest_rectangle(ok: np.ndarray) -> tuple[int, int, int, int] | None:
    """Largest all-True rectangle in a boolean grid (rows, cols) -> (r0, c0, r1, c1) inclusive."""
    rows, cols = ok.shape
    heights = np.zeros(cols, int)
    best, best_area = None, 0
    for r in range(rows):
        heights = np.where(ok[r], heights + 1, 0)
        stack: list[int] = []
        for c in range(cols + 1):
            hgt = heights[c] if c < cols else 0
            while stack and heights[stack[-1]] >= hgt:
                top = stack.pop()
                left = stack[-1] + 1 if stack else 0
                area = heights[top] * (c - left)
                if area > best_area:
                    best_area, best = area, (r - heights[top] + 1, left, r, c - 1)
            stack.append(c)
    return best


def suggest_area(cfg: Config, max_amp: float = 5.0, min_tension_n: float = 0.2, step: float = 10.0,
                 edge_mm: float = 10.0) -> dict:
    """{rect: [x0, y0, x1, y1], margins: {left, bottom, right, top}} for ``cfg`` (its anchors)."""
    b = cfg.board
    xs = np.arange(edge_mm, b.width_mm - edge_mm + 1e-9, step)
    ys = np.arange(edge_mm, b.height_mm - edge_mm + 1e-9, step)
    gx, gy = np.meshgrid(xs, ys)                       # rows = y
    p = np.column_stack([gx.ravel(), gy.ravel()])
    with np.errstate(all="ignore"):
        amp = amplification(cfg, p)
        ten = tensions(cfg, p).min(axis=1)
    ok = (np.isfinite(amp) & (amp <= max_amp) & (ten >= min_tension_n)).reshape(gy.shape)
    r = _largest_rectangle(ok)
    if r is None:
        raise ValueError("no usable drawing area with these motor positions")
    r0, c0, r1, c1 = r
    x0, x1, y0, y1 = float(xs[c0]), float(xs[c1]), float(ys[r0]), float(ys[r1])
    return {"rect": [x0, y0, x1, y1],
            "margins": {"margin_left_mm": round(x0, 1), "margin_bottom_mm": round(y0, 1),
                        "margin_right_mm": round(b.width_mm - x1, 1), "margin_top_mm": round(b.height_mm - y1, 1)},
            "max_amp": max_amp, "min_tension_n": min_tension_n}
