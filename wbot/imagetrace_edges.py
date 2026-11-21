"""Edge tracing for photos and renders: the outlines and inner detail lines of objects.

Unlike the threshold modes, this works on colour and brightness *changes*, so a white
object on a light grey background still gets its full outline.

Pipeline (all in working pixels):
    edge-preserving smoothing -> Lab gradient (strongest of L, a, b per pixel) ->
    Canny with thresholds above the image noise -> link edge pixels into long polylines
    (straight through junctions) -> bridge small gaps -> keep the most important lines
    within a length budget (plot time) -> smooth -> simplify

On a plain background (renders, product shots) the object silhouette is found separately
by flooding the background from the border, so the outline is closed even where it is
faint, and soft cast shadows are left out. Optional hatching shades the dark areas.
"""
from __future__ import annotations

import math

import numpy as np

_NB = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


# ------------------------------------------------------------------------------ edges
def noise_level(cv2, rgb: np.ndarray) -> float:
    """Robust estimate of the pixel noise (grey levels; about 0 for renders and graphics)."""
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    lap = cv2.filter2D(g, -1, np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32))
    return float(math.sqrt(math.pi / 2) / 6 * np.median(np.abs(lap[1:-1, 1:-1])))


def edge_map(cv2, rgb: np.ndarray, detail: int) -> tuple[np.ndarray, np.ndarray, float]:
    """-> (bool mask of 1 px wide edges, gradient magnitude, upper Canny threshold)."""
    h, w = rgb.shape[:2]
    side = max(h, w)
    noise = noise_level(cv2, rgb)
    d = max(5, int(round(side / 150)) | 1)
    img = cv2.bilateralFilter(rgb, d, 20 + 10 * noise, d)        # flattens texture, keeps edges
    img = cv2.GaussianBlur(img, (0, 0), 0.6 + side / 2000)
    lab = cv2.cvtColor(img, cv2.COLOR_RGB2Lab).astype(np.float32)
    lab[..., 1:] *= 1.6                                           # OpenCV squeezes a/b; colour edges count like light edges
    best_dx = np.zeros((h, w), np.float32)
    best_dy = np.zeros((h, w), np.float32)
    best_m = np.full((h, w), -1.0, np.float32)
    for c in range(3):
        dx = cv2.Sobel(lab[..., c], cv2.CV_32F, 1, 0, ksize=3)
        dy = cv2.Sobel(lab[..., c], cv2.CV_32F, 0, 1, ksize=3)
        m = dx * dx + dy * dy
        pick = m > best_m
        best_dx[pick], best_dy[pick], best_m[pick] = dx[pick], dy[pick], m[pick]
    mag = np.sqrt(best_m)
    # thresholds: a floor above the image noise (clean renders keep their faint edges, grainy
    # photos do not draw their grain) and a share of the image's real edges
    k = min(max(detail, 0), 100) / 100.0
    floor = (14.0 + 14.0 * noise) * (2.0 - k)
    strong = mag[mag > floor]
    hi = float(np.percentile(strong, 70 - 50 * k)) if strong.size >= 50 else floor * 2
    hi = max(hi, floor)
    lo = max(hi * 0.45, floor * 0.7)
    dx16 = np.clip(best_dx, -32767, 32767).astype(np.int16)
    dy16 = np.clip(best_dy, -32767, 32767).astype(np.int16)
    e = cv2.Canny(dx16, dy16, lo, hi, L2gradient=True)
    e[:1, :] = e[-1:, :] = 0
    e[:, :1] = e[:, -1:] = 0
    return e > 0, mag, hi


def foreground(cv2, rgb: np.ndarray, barrier: np.ndarray) -> tuple[np.ndarray | None, np.ndarray]:
    """-> (object mask or None, crisp-edge mask). The object mask exists only for images on a
    plain background (renders, product photos).

    The background is flood-filled from the image border: it may change slowly (light
    falloff, soft shadows) but cannot cross a sharp step or an edge pixel. Whatever the
    flood does not reach is the object - also a white object on a light grey floor."""
    h, w = rgb.shape[:2]
    lab = cv2.cvtColor(cv2.GaussianBlur(rgb, (0, 0), 0.8), cv2.COLOR_RGB2Lab)
    # only crisp edges block the flood (fine-scale slope well above the coarse-scale slope):
    # the soft rim of a shadow does not, the faint but sharp rim of a light object does
    def slope(sigma: float) -> np.ndarray:
        g = cv2.GaussianBlur(lab[..., 0].astype(np.float32), (0, 0), sigma)
        return cv2.dilate(np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1)),
                          np.ones((3, 3), np.uint8))
    fine = slope(0.7)
    crisp = (fine > 20.0) & (fine > 2.0 * slope(3.0))
    b = max(2, int(0.03 * min(h, w)))
    ring = np.concatenate([lab[:b].reshape(-1, 3), lab[-b:].reshape(-1, 3),
                           lab[:, :b].reshape(-1, 3), lab[:, -b:].reshape(-1, 3)]).astype(np.float32)
    if np.percentile(np.linalg.norm(ring - np.median(ring, axis=0), axis=1), 75) > 12.0:
        return None, crisp                                        # busy border: a real scene
    mask = np.zeros((h + 2, w + 2), np.uint8)
    mask[1:-1, 1:-1] = cv2.dilate((barrier & crisp).astype(np.uint8), np.ones((5, 5), np.uint8))
    flags = 4 | cv2.FLOODFILL_MASK_ONLY | (2 << 8)
    tol = (2, 2, 2)
    border = [(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)]
    border += [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)]
    for x, y in border[::2]:
        if mask[y + 1, x + 1] == 0:
            cv2.floodFill(lab, mask, (x, y), 0, tol, tol, flags)
    bg = mask[1:-1, 1:-1] == 2
    on_border = np.concatenate([bg[0], bg[-1], bg[:, 0], bg[:, -1]])
    if on_border.mean() < 0.6:
        return None, crisp
    fg = (~bg).astype(np.uint8)
    _drop_shadows(cv2, lab, fg, bg, crisp)
    r = max(1, int(round(max(h, w) / 300)))
    ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, ker)                # edge lines alone are no object
    n, lbl, stats, _ = cv2.connectedComponentsWithStats(fg, connectivity=8)
    keep = np.zeros(n, bool)
    keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= 0.002 * h * w
    fg = keep[lbl]
    inv = (~fg).astype(np.uint8)                                  # fill small holes (specks inside the object)
    n, lbl, stats, _ = cv2.connectedComponentsWithStats(inv, connectivity=4)
    small = np.zeros(n, bool)
    small[1:] = stats[1:, cv2.CC_STAT_AREA] < 0.0005 * h * w
    fg = fg | small[lbl]
    # the flood stops at the widened barrier, 2 px short of the real rim: give them back
    fg = cv2.erode(fg.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    share = float(fg.mean())
    if not 0.01 <= share <= 0.85:
        return None, crisp
    return fg, crisp


def _drop_shadows(cv2, lab: np.ndarray, fg: np.ndarray, bg: np.ndarray, crisp: np.ndarray) -> None:
    """Hand cast shadows back to the background (in place): regions that are a darker shade
    of the background colour and meet the background along a mostly soft rim."""
    h, w = fg.shape
    ref = np.median(lab[bg].astype(np.float32), axis=0)
    f = lab.astype(np.float32)
    chroma = np.hypot(f[..., 1] - ref[1], f[..., 2] - ref[2])
    shade = (fg > 0) & (chroma < 6.0) & (f[..., 0] < ref[0] - 4) & (f[..., 0] > 0.55 * ref[0])
    n, lbl, stats, _ = cv2.connectedComponentsWithStats(shade.astype(np.uint8), connectivity=8)
    near_bg = cv2.dilate(bg.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < 0.006 * h * w:             # shadows are big
            continue
        x, y, bw, bh = stats[i, :4]
        sl = (slice(max(0, y - 2), y + bh + 2), slice(max(0, x - 2), x + bw + 2))
        comp = lbl[sl] == i
        rim = comp & near_bg[sl]
        if rim.sum() < 20:
            continue
        if float(crisp[sl][rim].mean()) < 0.35:
            fg[sl][comp] = 0


def looks_like_photo(cv2, rgb: np.ndarray) -> bool:
    """True when a dark/light threshold would lose real edges (photos, renders, shaded art);
    False for two-tone images (line art, text, silhouettes) where nearly every edge is the
    border between ink and paper."""
    k = 800 / max(rgb.shape[:2])
    small = cv2.resize(rgb, None, fx=k, fy=k, interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_LINEAR)
    e = edge_map(cv2, small, 50)[0]
    if e.sum() < 20:
        return False
    g = cv2.cvtColor(cv2.GaussianBlur(small, (3, 3), 0), cv2.COLOR_RGB2GRAY)
    cut, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ink = (g < min(max(cut, 60.0), 215.0)).astype(np.uint8)
    rim = cv2.dilate(cv2.morphologyEx(ink, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)), np.ones((5, 5), np.uint8))
    return float((e & (rim > 0)).sum()) / float(e.sum()) < 0.9


# ------------------------------------------------------------------------------ linking
def link_paths(mask: np.ndarray) -> list[np.ndarray]:
    """1 px wide pixel set -> polylines (x, y). At junctions the walk goes on along the
    straightest branch, so strokes stay long and pen lifts stay few."""
    ys, xs = np.nonzero(mask)
    n = len(ys)
    if n == 0:
        return []
    h, w = mask.shape
    index = np.full((h, w), -1, dtype=np.int64)
    index[ys, xs] = np.arange(n)
    nb = np.full((n, 8), -1, dtype=np.int64)
    for k, (dy, dx) in enumerate(_NB):
        yy, xx = ys + dy, xs + dx
        ok = (yy >= 0) & (yy < h) & (xx >= 0) & (xx < w)
        nb[ok, k] = index[yy[ok], xx[ok]]
    adj = [set(int(j) for j in row if j >= 0) for row in nb]
    # staircase corners: drop a diagonal link when both pixels share a 4-neighbour
    for i in range(n):
        for j in [j for j in adj[i] if j > i]:
            if ys[i] != ys[j] and xs[i] != xs[j] and (adj[i] & adj[j]):
                adj[i].discard(j)
                adj[j].discard(i)
    used: set[tuple[int, int]] = set()

    def key(a: int, b: int) -> tuple[int, int]:
        return (a, b) if a < b else (b, a)

    def step_dir(a: int, b: int) -> tuple[float, float]:
        return float(xs[b] - xs[a]), float(ys[b] - ys[a])

    def walk(a: int, b: int) -> list[int]:
        path = [a, b]
        used.add(key(a, b))
        while True:
            cur = path[-1]
            cand = [j for j in adj[cur] if key(cur, j) not in used]
            if not cand:
                break
            # direction over the last few pixels, so a pixel staircase does not steer
            back = path[max(0, len(path) - 5)]
            vx, vy = float(xs[cur] - xs[back]), float(ys[cur] - ys[back])
            vn = math.hypot(vx, vy) or 1.0

            def turn(j: int) -> float:
                dx, dy = step_dir(cur, j)
                return -(dx * vx + dy * vy) / ((math.hypot(dx, dy) or 1.0) * vn)

            j = min(cand, key=turn)
            if len(cand) > 1 and turn(j) > 0.0:                     # only sharp turns left: stop here
                break
            used.add(key(cur, j))
            path.append(j)
        return path

    deg = np.array([len(a) for a in adj])
    paths: list[list[int]] = []
    for a in np.argsort(deg != 1, kind="stable"):                  # start at free ends first
        a = int(a)
        for b in sorted(adj[a]):
            if key(a, b) not in used:
                p = walk(a, b)
                # a free end that was reached through a junction may continue backwards as well
                if deg[a] != 1:
                    nxt = [j for j in adj[a] if key(a, j) not in used]
                    if nxt:
                        q = walk(a, nxt[0])
                        p = q[::-1][:-1] + p
                paths.append(p)
    xy = np.stack([xs, ys], axis=1).astype(np.float32)
    out = []
    for p in paths:
        if len(p) < 2:
            continue
        q = xy[p]
        if len(q) > 8 and 0 < float(np.hypot(*(q[0] - q[-1]))) <= 2.0:   # ring that just misses its start
            q = np.vstack([q, q[:1]])
        out.append(q)
    return out


def _end_dir(p: np.ndarray, at_start: bool, span: int = 6) -> np.ndarray:
    """Unit vector pointing out of the path at one end."""
    if at_start:
        a, b = p[min(span, len(p) - 1)], p[0]
    else:
        a, b = p[max(0, len(p) - 1 - span)], p[-1]
    v = b - a
    n = float(np.hypot(*v))
    return v / n if n > 0 else v


def bridge_gaps(paths: list[np.ndarray], gap: float, min_cos: float = 0.3) -> list[np.ndarray]:
    """Join open path ends that nearly meet and point at each other (gaps in Canny edges).

    Every end is linked at most once (closest pairs first); the links then chain the paths
    together. A chain whose two ends get linked becomes a closed ring."""
    from scipy.spatial import cKDTree

    paths = [p for p in paths if len(p) >= 2]
    n = len(paths)
    open_ids = [i for i in range(n) if not np.all(paths[i][0] == paths[i][-1])]
    if len(open_ids) < 1:
        return paths
    # end e = 2*i (start of path i) or 2*i+1 (end of path i)
    ends = np.array([e for i in open_ids for e in (2 * i, 2 * i + 1)])
    pts = np.array([paths[e // 2][0] if e % 2 == 0 else paths[e // 2][-1] for e in ends], dtype=np.float64)
    dirs = np.array([_end_dir(paths[e // 2], e % 2 == 0) for e in ends], dtype=np.float64)
    pairs = cKDTree(pts).query_pairs(gap, output_type="ndarray")
    cand = []
    for u, v in pairs:
        link = pts[v] - pts[u]
        ln = float(np.hypot(*link))
        if ln > 0:
            lu = link / ln
            good = dirs[u] @ lu >= min_cos and dirs[v] @ -lu >= min_cos
        else:
            good = dirs[u] @ -dirs[v] >= min_cos
        if good:
            cand.append((ln, int(ends[u]), int(ends[v])))
    cand.sort()
    partner: dict[int, int] = {}
    for _, a, b in cand:
        if a in partner or b in partner:
            continue
        partner[a] = b
        partner[b] = a
    # walk every chain from a free end (or, for rings, from any end)
    seen = [False] * n
    out: list[np.ndarray] = []

    def walk(e: int) -> list[np.ndarray]:
        parts = []
        while True:
            i = e // 2
            seen[i] = True
            parts.append(paths[i] if e % 2 == 0 else paths[i][::-1])
            exit_end = e ^ 1                                         # leave by the other end of path i
            nxt = partner.get(exit_end)
            if nxt is None or seen[nxt // 2]:
                return parts
            e = nxt

    open_set = set(open_ids)
    for i in range(n):
        if i not in open_set:
            out.append(paths[i])
            seen[i] = True
    for e in ends:
        i = int(e) // 2
        if not seen[i] and int(e) not in partner:                    # free end: start of a chain
            out.append(np.vstack(walk(int(e))))
    for e in ends:                                                   # what is left are rings
        i = int(e) // 2
        if not seen[i]:
            q = np.vstack(walk(int(e)))
            out.append(np.vstack([q, q[:1]]))
    return out


# ------------------------------------------------------------------------------ shaping
def smooth(p: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian smoothing along the path (removes the pixel staircase, keeps the shape)."""
    if len(p) < 5 or sigma <= 0:
        return p
    closed = bool(np.all(p[0] == p[-1]))
    r = max(1, int(round(3 * sigma)))
    t = np.arange(-r, r + 1, dtype=np.float32)
    g = np.exp(-0.5 * (t / sigma) ** 2)
    g /= g.sum()
    if closed:
        core = p[:-1]
        if len(core) <= 2 * r:
            return p
        ext = np.vstack([core[-r:], core, core[:r]])
        sm = np.column_stack([np.convolve(ext[:, k], g, mode="valid") for k in (0, 1)])
        return np.vstack([sm, sm[:1]]).astype(np.float32)
    ext = np.vstack([np.repeat(p[:1], r, 0), p, np.repeat(p[-1:], r, 0)])
    sm = np.column_stack([np.convolve(ext[:, k], g, mode="valid") for k in (0, 1)])
    sm[0], sm[-1] = p[0], p[-1]
    return sm.astype(np.float32)


def path_len(p: np.ndarray) -> float:
    return float(np.hypot(*np.diff(p, axis=0).T).sum()) if len(p) > 1 else 0.0


def trace_edges(cv2, rgb: np.ndarray, detail: int, simplify) -> list[np.ndarray]:
    """rgb at working size -> smoothed, simplified polylines in working pixels."""
    h, w = rgb.shape[:2]
    side = max(h, w)
    e, mag, hi = edge_map(cv2, rgb, detail)
    k = min(max(detail, 0), 100) / 100.0
    silhouette: list[np.ndarray] = []
    outside = None
    fg, crisp = foreground(cv2, rgb, edge_map(cv2, rgb, 100)[0])
    if fg is not None:
        # plain background: the object outline comes from the mask (always complete), the
        # edge detector only adds the lines inside it
        cs, _ = cv2.findContours(fg.astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
        line = np.zeros((h, w), np.uint8)
        for c in cs:
            q = c.reshape(-1, 2).astype(np.float32)
            if len(q) >= 8:
                silhouette.append(np.vstack([q, q[:1]]))
        cv2.drawContours(line, cs, -1, 1, thickness=max(5, int(round(side / 150))) | 1)
        outside = cv2.erode(fg.astype(np.uint8), np.ones((3, 3), np.uint8)) == 0
        e = e & (line == 0)
    paths = link_paths(e)
    paths = bridge_gaps(paths, gap=max(3.0, side / 250))
    min_len = side * (0.03 - 0.022 * k)                             # drop specks: 3 % .. 0.8 % of the long side
    # line budget (plot time): the most important edges first - long and high-contrast
    budget = (w + h) * (6.0 + 24.0 * k ** 1.5)
    scored = []
    for p in paths:
        ln = path_len(p)
        if ln < min_len:
            continue
        ij = np.clip(np.rint(p).astype(np.int64), 0, [w - 1, h - 1])
        if outside is not None and outside[ij[:, 1], ij[:, 0]].mean() > 0.5                 and crisp[ij[:, 1], ij[:, 0]].mean() < 0.6:
            continue                                                # beside the object: soft shadow rims
        contrast = float(mag[ij[:, 1], ij[:, 0]].mean()) / hi
        scored.append((ln * min(contrast, 3.0), ln, p))
    scored.sort(key=lambda t: -t[0])
    scored = [(math.inf, path_len(p), p) for p in silhouette] + scored
    out = []
    total = 0.0
    for _, ln, p in scored:
        if total + ln > budget and total > 0:
            continue
        total += ln
        p = smooth(p, sigma=1.2 + side / 1000)
        p = simplify(p, 0.35 + (1 - k) * 0.5)
        if len(p) >= 2:
            out.append(p)
    return out


# ------------------------------------------------------------------------------ shading
def _hatch(cv2, mask: np.ndarray, step: float, angle_deg: float, min_len: float) -> list[np.ndarray]:
    """Parallel lines across ``mask`` at ``angle_deg``, ``step`` px apart, alternating direction."""
    h, w = mask.shape
    c = (w / 2.0, h / 2.0)
    rot = cv2.getRotationMatrix2D(c, angle_deg, 1.0)
    cos, sin = abs(rot[0, 0]), abs(rot[0, 1])
    nw, nh = int(h * sin + w * cos) + 2, int(h * cos + w * sin) + 2
    rot[0, 2] += nw / 2.0 - c[0]
    rot[1, 2] += nh / 2.0 - c[1]
    m = cv2.warpAffine(mask.astype(np.uint8), rot, (nw, nh), flags=cv2.INTER_NEAREST)
    back = cv2.invertAffineTransform(rot)
    roomy = cv2.dilate(m, np.ones((3, 3), np.uint8))                # the step may graze the rim

    def inside(a: tuple[float, float], b: tuple[float, float]) -> bool:
        n = max(2, int(math.hypot(b[0] - a[0], b[1] - a[1])) + 1)
        xs = np.clip(np.rint(np.linspace(a[0], b[0], n)).astype(int), 0, nw - 1)
        ys = np.clip(np.rint(np.linspace(a[1], b[1], n)).astype(int), 0, nh - 1)
        return bool(roomy[ys, xs].all())

    # zigzag: a row's segment continues the stroke that ended just above it when the
    # connecting step stays inside the area, so one pen-down covers a whole patch
    strokes: list[list[tuple[float, float]]] = []
    open_ends: list[int] = []                                     # strokes that ended on the last row
    flip = False
    y = step / 2
    while y < nh:
        row = m[int(y)]
        d = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
        segs = [(a, b - 1) for a, b in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]) if b - 1 - a >= min_len]
        if flip:
            segs = [(b, a) for a, b in segs[::-1]]
        now: list[int] = []
        free = list(open_ends)
        for a, b in segs:
            start, end = (float(a), y), (float(b), y)
            hit = None
            for k in free:
                if abs(strokes[k][-1][0] - start[0]) <= 2 * step and inside(strokes[k][-1], start):
                    hit = k
                    break
            if hit is None:
                strokes.append([start, end])
                now.append(len(strokes) - 1)
            else:
                free.remove(hit)
                strokes[hit] += [start, end]
                now.append(hit)
        open_ends = now
        flip = not flip
        y += step
    out = []
    for st in strokes:
        p = np.column_stack([np.array(st, dtype=np.float64), np.ones(len(st))]) @ back.T
        out.append(p.astype(np.float32))
    return out


def shade_dark(cv2, rgb: np.ndarray, step: float) -> list[np.ndarray]:
    """Hatching for dark areas: one direction for dark, a second one across it for very dark."""
    h, w = rgb.shape[:2]
    light = cv2.cvtColor(cv2.GaussianBlur(rgb, (0, 0), max(1.0, step / 3)), cv2.COLOR_RGB2Lab)[..., 0]
    r = max(1, int(round(step / 2)))
    ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    out = []
    for cut, angle in ((95, 45.0), (50, -45.0)):
        m = cv2.morphologyEx((light < cut).astype(np.uint8), cv2.MORPH_OPEN, ker)
        n, lbl, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
        keep = np.zeros(n, bool)
        keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= 4 * step * step
        out += _hatch(cv2, keep[lbl], step, angle, 1.5 * step)
    return out
