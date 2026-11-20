"""Objects at the board (people, a ladder, a chair ...): outlines, a stick skeleton, a "see-through" filter.

* Only the board and its surroundings are watched (``roi`` from the caller); the gondola and its
  belts move by design and are masked out.
* Foreground = colour difference to a remembered background picture, after compensating the webcam's
  automatic exposure (it re-adjusts when something big enters). If most of the watched area changes at
  once it is a lighting change: the background is re-learned and nothing is reported.
* Any large enough difference blob is an object; on/off with hysteresis so one noisy frame does not flip it.
* Skeleton: the medial axis of the silhouette (scikit-image ``skeletonize``), drawn as short segments.
  It is a shape skeleton, not a joint model: honest, cheap and fun.
* Filter: pixels under the person are replaced by the last clean background of that spot.
"""
from __future__ import annotations

import time

import cv2
import numpy as np

WORK_W = 480
MIN_AREA_FRAC = 0.008          # of the watched area (board surroundings)
MAX_OBJECTS = 6
DIFF_MIN = 22.0                # colour difference (0..255) to the remembered background
STILL_DIFF = 6.0               # frame-to-frame change below this = not moving
STILL_UPDATES = 20             # ~8 s without any movement: absorbed into the background (ghost)
ON_FRAMES, OFF_FRAMES = 3, 6


class PresenceWatch:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.clean: np.ndarray | None = None
        self.prev: np.ndarray | None = None          # previous small frame (temporal change)
        self.still: np.ndarray | None = None         # updates a pixel has not changed
        self.mask: np.ndarray | None = None          # small person mask (uint8)
        self.k = 1.0
        self.n = 0
        self._on = self._off = 0
        self.present = False
        self.since = 0.0
        self.last_seen = 0.0
        self.state: dict = {"present": False}

    def update(self, img: np.ndarray, exclude: list[np.ndarray] | None = None, warmup: int = 6,
               roi: np.ndarray | None = None) -> dict:
        """``exclude``: polygons (full-resolution px) that move on purpose (gondola, belts).
        ``roi``: polygon (full-resolution px) of the board's surroundings; only objects there count."""
        h0, w0 = img.shape[:2]
        k = WORK_W / w0
        small = cv2.resize(img, (WORK_W, int(h0 * k)), interpolation=cv2.INTER_AREA).astype(np.float32)
        if self.clean is None or self.clean.shape != small.shape:
            self.reset()
            self.clean = small.copy()
            self.prev = small.copy()
            self.still = np.zeros(small.shape[:2], np.int32)
        self.k, self.n = k, self.n + 1
        H, W = small.shape[:2]
        area = np.full((H, W), 255, np.uint8)
        if roi is not None and len(roi) >= 3:
            area[:] = 0
            cv2.fillPoly(area, [np.round(np.asarray(roi) * k).astype(np.int32)], 255)
        for poly in exclude or []:
            cv2.fillPoly(area, [np.round(np.asarray(poly) * k).astype(np.int32)], 0)
        inside = area > 0
        # exposure: webcams re-adjust brightness when something big enters - compare at the same gain
        g_cur, g_ref = small.mean(axis=2), self.clean.mean(axis=2)
        if inside.any():
            gain = float(np.clip(np.median(g_ref[inside]) / max(np.median(g_cur[inside]), 1.0), 0.6, 1.6))
            small = small * gain
        diff = cv2.GaussianBlur(np.abs(small - self.clean).max(axis=2), (5, 5), 0)
        noise = float(np.median(diff[inside])) if inside.any() else 3.0
        fg = ((diff > max(DIFF_MIN, 4.0 * noise)) & inside).astype(np.uint8) * 255
        # a lighting change touches the whole picture, an object only a part of it: count changed grid cells
        cells = changed = 0
        for ys in np.array_split(np.arange(H), 6):
            for xs in np.array_split(np.arange(W), 8):
                ins = inside[np.ix_(ys, xs)]
                if ins.mean() < 0.5:
                    continue
                cells += 1
                changed += fg[np.ix_(ys, xs)][ins].mean() > 0.3 * 255
        lighting = (cells > 0 and changed > 0.6 * cells) or self.n < warmup   # learn it, do not report
        if lighting:
            self.clean = self.clean * 0.5 + small * 0.5
            fg[:] = 0
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        n, lab, st, _ = cv2.connectedComponentsWithStats(fg, 8)
        min_px = MIN_AREA_FRAC * max(int(inside.sum()), 1)
        objs = sorted((i for i in range(1, n) if st[i][4] > min_px), key=lambda i: -st[i][4])[:MAX_OBJECTS]
        seen = bool(objs)
        if seen:
            self._on, self._off = self._on + 1, 0
        else:
            self._off, self._on = self._off + 1, 0
        now = time.time()
        if not self.present and self._on >= ON_FRAMES:
            self.present, self.since = True, now
        elif self.present and self._off >= OFF_FRAMES:
            self.present = False
        mask = np.isin(lab, objs).astype(np.uint8) * 255 if objs else np.zeros_like(fg)
        self.mask = mask
        # temporal stillness: a pixel that has not changed for a while is scenery, not someone standing there
        moved = np.abs(small - self.prev).max(axis=2) > STILL_DIFF
        self.still = np.where(moved, 0, self.still + 1)
        self.prev = small
        # remembered background: learn where nothing stands; under an object only very slowly - unless the
        # "object" has been perfectly still for a while: that is a ghost (a lighting jump, something that was
        # in the start picture and left), so it is absorbed within a few seconds
        free = cv2.dilate(mask, np.ones((15, 15), np.uint8)) == 0
        if not lighting:
            ghost = ~free & (self.still >= STILL_UPDATES)
            slow = ~free & ~ghost
            self.clean[free] = self.clean[free] * 0.9 + small[free] * 0.1
            self.clean[ghost] = self.clean[ghost] * 0.7 + small[ghost] * 0.3
            self.clean[slow] = self.clean[slow] * 0.998 + small[slow] * 0.002
        st_out: dict = {"present": self.present, "t": now, "objects": len(objs), "adapting": bool(lighting)}
        if self.present:
            self.last_seen = now
            st_out["since"] = round(now - self.since, 1)
        if objs:
            x0 = min(st[i][0] for i in objs); y0 = min(st[i][1] for i in objs)
            x1 = max(st[i][0] + st[i][2] for i in objs); y1 = max(st[i][1] + st[i][3] for i in objs)
            st_out["bbox"] = [round(x0 / k), round(y0 / k), round(x1 / k), round(y1 / k)]
            outlines = []
            for i in objs:
                cnts, _ = cv2.findContours((lab == i).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                c = cv2.approxPolyDP(max(cnts, key=cv2.contourArea), 1.5, True).reshape(-1, 2)
                outlines.append((c / k).round(1).tolist())
            st_out["outlines"], st_out["outline"] = outlines, outlines[0]
            st_out["skeleton"] = self._skeleton((lab == objs[0]).astype(np.uint8) * 255, k)
        self.state = st_out
        return st_out

    @staticmethod
    def _skeleton(mask: np.ndarray, k: float) -> list[list[float]]:
        """Medial axis of the silhouette as short segments [x0, y0, x1, y1] in full-resolution px."""
        from skimage.morphology import skeletonize
        sk = skeletonize(cv2.GaussianBlur(mask, (15, 15), 0) > 127).astype(np.uint8)
        ys0 = np.nonzero(mask.any(axis=1))[0]
        spur = max(3, int(0.08 * (np.ptp(ys0) if len(ys0) else 0)))     # prune short side branches (noise)
        kern = np.ones((3, 3), np.uint8)
        for _ in range(spur):
            nb = cv2.filter2D(sk, -1, kern, borderType=cv2.BORDER_CONSTANT) - sk
            ends = (sk == 1) & (nb <= 1)
            if not ends.any():
                break
            sk[ends] = 0
        ys, xs = np.nonzero(sk)
        pts = set(zip(xs.tolist(), ys.tolist()))
        segs = []
        for x, y in pts:
            for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
                if (x + dx, y + dy) in pts:
                    segs.append([x / k, y / k, (x + dx) / k, (y + dy) / k])
        return [[round(v, 1) for v in s] for s in segs[:4000]]

    def filtered(self, img: np.ndarray) -> np.ndarray:
        """``img`` with the person replaced by the remembered background (soft edge)."""
        if self.mask is None or self.clean is None or not self.mask.any():
            return img
        h0, w0 = img.shape[:2]
        m = cv2.dilate(self.mask, np.ones((11, 11), np.uint8))
        m = cv2.GaussianBlur(cv2.resize(m, (w0, h0), interpolation=cv2.INTER_LINEAR), (0, 0), 4).astype(np.float32) / 255
        bg = cv2.resize(self.clean, (w0, h0), interpolation=cv2.INTER_LINEAR)
        return (img.astype(np.float32) * (1 - m[:, :, None]) + bg * m[:, :, None]).clip(0, 255).astype(np.uint8)
