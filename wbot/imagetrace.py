"""Raster image (PNG, JPEG, ...) -> :class:`Drawing`: the pen paths that will be plotted.

Pipeline: decode -> downscale -> split into pen layers (one dark layer, or one layer per
configured pen colour) -> trace each layer -> polylines in board millimetres.

Tracing modes:
    lines    centre lines of dark strokes (line art, handwriting, logos)
    outline  contours of dark regions (silhouettes, photos)
    fill     horizontal hatching with the pen's line width (solid areas)
    auto     lines for thin strokes, outline for everything else
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field

import numpy as np

from .config import Config
from .drawing import Drawing, Stroke

MAX_BYTES = 30_000_000
MAX_STROKES = 20_000
MODES = ("auto", "lines", "outline", "fill")


class ImageError(ValueError):
    """The image cannot be traced (the message is shown to the user)."""


@dataclass
class TraceOptions:
    mode: str = "auto"
    detail: int = 50                 # 0..100: working resolution and path precision
    invert: bool = False             # light ink on a dark background
    use_pen_colors: bool = False     # one layer per pen colour instead of a single dark layer
    pen: int = 0                     # pen slot for the single-layer case
    threshold: int | None = None     # 0..255 darkness cut-off, None = automatic
    fit_rect: tuple[float, float, float, float] | None = None
    pen_widths_mm: list[float] = field(default_factory=lambda: [2.0, 2.0, 2.0])


@dataclass
class TraceResult:
    drawing: Drawing
    info: dict
    warnings: list[str]


def _imports():
    try:
        import cv2
        from skimage.morphology import skeletonize
        from PIL import Image, ImageOps
    except ImportError as exc:        # pragma: no cover
        raise ImageError("Image import needs the 'imaging' extras: pip install opencv-python-headless scikit-image Pillow") from exc
    return cv2, skeletonize, Image, ImageOps


def decode(data: bytes):
    """Bytes -> RGB uint8 array (transparency becomes white, EXIF rotation applied)."""
    _, _, Image, ImageOps = _imports()
    if len(data) > MAX_BYTES:
        raise ImageError(f"Image file too large ({len(data) / 1e6:.0f} MB, maximum {MAX_BYTES / 1e6:.0f} MB).")
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as exc:  # noqa: BLE001
        raise ImageError("This file is not a readable image. Use PNG, JPEG, WebP, BMP or GIF.") from exc
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        im = bg
    return np.asarray(im.convert("RGB"), dtype=np.uint8)


def colorsys_hsv(c: tuple[int, int, int]) -> tuple[float, float, float]:
    import colorsys
    return colorsys.rgb_to_hsv(c[0] / 255, c[1] / 255, c[2] / 255)


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return 0, 0, 0


# ------------------------------------------------------------------------------ layers
def _layers(rgb: np.ndarray, opts: TraceOptions, pens: list[tuple[int, int, int]]) -> list[tuple[int, np.ndarray]]:
    """-> [(pen slot, bool mask of ink)]"""
    cv2, *_ = _imports()
    img = cv2.GaussianBlur(rgb, (3, 3), 0)
    if opts.invert:
        img = 255 - img
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    if opts.use_pen_colors and len(pens) > 0:
        hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV_FULL).astype(np.float32)
        hue, sat, val = hsv[..., 0] * (2 * math.pi / 255), hsv[..., 1] / 255, hsv[..., 2] / 255
        ink = np.maximum(1 - val, sat * 0.9 * (val > 0.25)) > 0.42           # dark or clearly coloured
        pens_hsv = [colorsys_hsv(c) for c in pens]
        neutral = [i for i, (_, sv, _) in enumerate(pens_hsv) if sv < 0.3]
        coloured = [i for i in range(len(pens)) if i not in neutral]
        best = np.full(gray.shape, -1, dtype=np.int32)
        gray_px = sat < 0.3
        if neutral:
            best[gray_px] = min(neutral, key=lambda i: pens_hsv[i][2])           # darkest neutral pen
        if coloured:
            dist = np.stack([np.abs(np.angle(np.exp(1j * (hue - pens_hsv[i][0] * 2 * math.pi)))) for i in coloured])
            pick = np.array(coloured)[dist.argmin(axis=0)]
            best[~gray_px] = pick[~gray_px]
        elif neutral:
            best[~gray_px] = min(neutral, key=lambda i: pens_hsv[i][2])
        out = []
        for i in range(len(pens)):
            m = ink & (best == i)
            if m.any():
                out.append((i, m))
        return out
    if opts.threshold is not None:
        cut = float(opts.threshold)
    else:
        cut, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        cut = min(max(cut, 60.0), 215.0)
    return [(min(max(opts.pen, 0), max(0, len(pens) - 1)), gray < cut)]


# ------------------------------------------------------------------------------ tracing
_NB = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


def _skeleton_paths(sk: np.ndarray) -> list[np.ndarray]:
    """Walk a one pixel wide skeleton into polylines (x, y) pixel coordinates, each pixel run once."""
    ys, xs = np.nonzero(sk)
    n = len(ys)
    if n == 0:
        return []
    h, w = sk.shape
    index = np.full((h, w), -1, dtype=np.int64)
    index[ys, xs] = np.arange(n)
    nb = np.full((n, 8), -1, dtype=np.int64)
    for k, (dy, dx) in enumerate(_NB):
        yy, xx = ys + dy, xs + dx
        ok = (yy >= 0) & (yy < h) & (xx >= 0) & (xx < w)
        nb[ok, k] = index[yy[ok], xx[ok]]
    nbl = [[int(j) for j in row if j >= 0] for row in nb]
    # staircase corners: a diagonal link is redundant when both pixels also touch a common pixel
    sets = [set(r) for r in nbl]
    for i in range(n):
        for j in list(nbl[i]):
            if i < j and abs(int(ys[i]) - int(ys[j])) == 1 and abs(int(xs[i]) - int(xs[j])) == 1 and (sets[i] & sets[j]):
                sets[i].discard(j)
                sets[j].discard(i)
    nbl = [sorted(s) for s in sets]
    deg = np.array([len(r) for r in nbl])
    used: set[tuple[int, int]] = set()
    paths: list[list[int]] = []

    def edge(a: int, b: int) -> tuple[int, int]:
        return (a, b) if a < b else (b, a)

    def walk(a: int, b: int) -> list[int]:
        path = [a, b]
        used.add(edge(a, b))
        prev, cur = a, b
        while deg[cur] == 2:
            nxt = [j for j in nbl[cur] if j != prev and edge(cur, j) not in used]
            if not nxt:
                break
            j = nxt[0]
            used.add(edge(cur, j))
            path.append(j)
            prev, cur = cur, j
        return path

    nodes = [i for i in range(n) if deg[i] != 2]
    for a in nodes:
        for b in nbl[a]:
            if edge(a, b) not in used:
                paths.append(walk(a, b))
    for a in range(n):                                  # closed loops without any node
        for b in nbl[a]:
            if edge(a, b) not in used:
                paths.append(walk(a, b))
    xy = np.stack([xs, ys], axis=1).astype(np.float32)
    out = []
    for p in paths:
        if len(p) < 2:
            continue
        q = xy[p]
        if len(q) > 12 and 0 < float(np.hypot(*(q[0] - q[-1]))) <= 3.0:        # a ring that just misses its start
            q = np.vstack([q, q[:1]])
        out.append(q)
    return out


def _smooth(p: np.ndarray, passes: int = 2) -> np.ndarray:
    if len(p) < 4:
        return p
    closed = bool(np.all(p[0] == p[-1]))
    q = p.copy()
    for _ in range(passes):
        if closed:
            core = q[:-1]
            core = (np.roll(core, 1, 0) + 2 * core + np.roll(core, -1, 0)) / 4
            q = np.vstack([core, core[:1]])
        else:
            q[1:-1] = (q[:-2] + 2 * q[1:-1] + q[2:]) / 4
    return q


def _simplify(cv2, p: np.ndarray, eps: float) -> np.ndarray:
    if len(p) < 3:
        return p
    closed = bool(np.all(p[0] == p[-1]))
    core = p[:-1] if closed else p
    out = cv2.approxPolyDP(core.reshape(-1, 1, 2).astype(np.float32), eps, closed).reshape(-1, 2)
    return np.vstack([out, out[:1]]) if closed else out


def _trace_lines(cv2, skeletonize, mask: np.ndarray, eps: float, min_px: float) -> list[np.ndarray]:
    m = mask.astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    sk = skeletonize(m > 0)
    out = []
    for p in _skeleton_paths(sk):
        p = _simplify(cv2, _smooth(p), eps)
        if len(p) >= 2 and float(np.hypot(*np.diff(p, axis=0).T).sum()) >= min_px:
            out.append(p)
    return out


def _trace_outline(cv2, mask: np.ndarray, eps: float, min_px: float) -> list[np.ndarray]:
    m = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    cs, _ = cv2.findContours(m, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    out = []
    for c in cs:
        p = c.reshape(-1, 2).astype(np.float32)
        if len(p) < 4:
            continue
        p = np.vstack([p, p[:1]])
        p = _simplify(cv2, _smooth(p), eps)
        if float(np.hypot(*np.diff(p, axis=0).T).sum()) >= min_px:
            out.append(p)
    return out


def _trace_fill(mask: np.ndarray, step_px: float) -> list[np.ndarray]:
    h, w = mask.shape
    out = []
    flip = False
    y = step_px / 2
    while y < h:
        row = mask[int(y)]
        d = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
        starts, ends = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
        for a, b in zip(starts, ends):
            if b - a >= 2:
                seg = np.array([[a, y], [b - 1, y]], dtype=np.float32)
                out.append(seg[::-1] if flip else seg)
        flip = not flip
        y += step_px
    return out


def _is_thin(mask: np.ndarray, skeleton_len: float) -> bool:
    """Strokes up to ~1.4 % of the image size count as lines (average stroke width = area / centre-line length)."""
    area = float(mask.sum())
    return skeleton_len > 0 and area / skeleton_len <= max(6.0, 0.014 * max(mask.shape))


# ------------------------------------------------------------------------------ main
def trace_image(data: bytes | np.ndarray, cfg: Config, opts: TraceOptions | None = None) -> TraceResult:
    opts = opts or TraceOptions()
    if opts.mode not in MODES:
        raise ImageError(f"Unknown mode '{opts.mode}'.")
    cv2, skeletonize, *_ = _imports()
    rgb = decode(data) if isinstance(data, (bytes, bytearray)) else data
    h0, w0 = rgb.shape[:2]
    if min(h0, w0) < 8:
        raise ImageError("The image is too small.")
    detail = min(max(int(opts.detail), 0), 100)
    side = 500 + detail * 10                                  # long side in working pixels: 500..1500
    k = side / max(h0, w0)
    if k < 1.0:
        rgb = cv2.resize(rgb, (max(1, round(w0 * k)), max(1, round(h0 * k))), interpolation=cv2.INTER_AREA)
    h, w = rgb.shape[:2]

    rect = opts.fit_rect or cfg.safe_rect()
    rw, rh = rect[2] - rect[0], rect[3] - rect[1]
    s = min(rw / w, rh / h)                                   # mm per working pixel
    ox = rect[0] + (rw - w * s) / 2
    oy = rect[1] + (rh - h * s) / 2

    pens = [_hex_rgb(c.hex) for c in cfg.colors]
    layers = _layers(rgb, opts, pens)
    eps = 0.5 + (100 - detail) / 100 * 1.5                    # path simplification in px
    min_px = 2.0 + (100 - detail) / 100 * 6.0                 # drop specks shorter than this
    warnings: list[str] = []
    strokes: list[Stroke] = []
    used_modes: set[str] = set()
    for slot, mask in layers:
        mode = opts.mode
        if mode == "auto":
            sk = skeletonize(cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)) > 0)
            mode = "lines" if _is_thin(mask, float(sk.sum())) else "outline"
        used_modes.add(mode)
        if mode == "lines":
            paths = _trace_lines(cv2, skeletonize, mask, eps, min_px)
        elif mode == "outline":
            paths = _trace_outline(cv2, mask, eps, min_px)
        else:
            wmm = opts.pen_widths_mm[slot] if slot < len(opts.pen_widths_mm) else 2.0
            paths = _trace_fill(mask, max(1.0, 0.9 * wmm / s))
        for p in paths:
            pts = np.column_stack([ox + p[:, 0] * s, oy + (h - p[:, 1]) * s])      # image Y points down, board Y up
            strokes.append(Stroke(pts, slot))
    if not strokes:
        raise ImageError("Nothing to draw was found. Try a different mode, move the detail slider, or turn on Invert.")
    if len(strokes) > MAX_STROKES:
        strokes.sort(key=lambda t: -t.length())
        strokes = strokes[:MAX_STROKES]
        warnings.append(f"The image is very detailed: only the {MAX_STROKES} longest paths are kept. Lower the detail.")
    d = Drawing(strokes, "image")
    info = {"mode": "+".join(sorted(used_modes)), "strokes": len(strokes), "pixels": [w0, h0],
            "length_m": round(d.length() / 1000, 2), "pens": sorted({t.color for t in strokes})}
    return TraceResult(d, info, warnings)
