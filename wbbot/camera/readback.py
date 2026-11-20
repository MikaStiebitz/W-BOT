"""What is on the board right now — the AI's eyes.

Two questions the MCP layer needs answered in **board millimetres**, because
that is the only coordinate system the plotter can act on:

* :func:`read_board` — where is there ink, how much, and roughly what shape?
* :func:`find_free_space` — where can I put something ``w x h`` mm without
  drawing over what is already there?

Free space is found with a summed-area table over an "occupied" mask, so a
1500 x 1000 mm board at 1 mm/px is answered in milliseconds rather than by
brute-force scanning.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from ..core.safearea import SafeArea
from .boardmap import BoardMap, BoardMapError

__all__ = [
    "BoardReading",
    "FreeSpace",
    "InkRegion",
    "read_board",
    "find_free_space",
    "encode_png",
]


def _cv2():
    try:
        import cv2  # noqa: PLC0415
        return cv2
    except ImportError as exc:  # pragma: no cover
        raise BoardMapError("OpenCV is required for board read-back") from exc


@dataclass(frozen=True)
class InkRegion:
    """One blob of ink, in board millimetres."""

    x: float
    y: float
    width: float
    height: float
    area_mm2: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)

    def to_dict(self) -> dict:
        return {"x": round(self.x, 1), "y": round(self.y, 1),
                "width": round(self.width, 1), "height": round(self.height, 1),
                "area_mm2": round(self.area_mm2, 1),
                "center": [round(v, 1) for v in self.center]}


@dataclass
class BoardReading:
    """A rectified look at the board."""

    image: np.ndarray                    # rectified BGR, board-aligned
    ink_mask: np.ndarray                 # uint8, 255 where something is drawn
    px_per_mm: float
    board_width_mm: float
    board_height_mm: float
    regions: list[InkRegion] = field(default_factory=list)
    coverage_percent: float = 0.0
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """A sentence or two an AI can reason about."""
        if not self.regions:
            return (f"The board ({self.board_width_mm:.0f} x "
                    f"{self.board_height_mm:.0f} mm) appears to be empty.")
        parts = [
            f"The board ({self.board_width_mm:.0f} x {self.board_height_mm:.0f} mm) "
            f"has {len(self.regions)} marked region(s) covering "
            f"{self.coverage_percent:.1f} % of its area."
        ]
        for i, r in enumerate(self.regions[:8], start=1):
            cx, cy = r.center
            parts.append(
                f"  {i}. {r.width:.0f} x {r.height:.0f} mm centred at "
                f"({cx:.0f}, {cy:.0f}) mm")
        if len(self.regions) > 8:
            parts.append(f"  ... and {len(self.regions) - 8} smaller region(s)")
        return "\n".join(parts)

    def to_dict(self, include_image: bool = False) -> dict:
        out = {
            "board_mm": [self.board_width_mm, self.board_height_mm],
            "px_per_mm": self.px_per_mm,
            "coverage_percent": round(self.coverage_percent, 2),
            "regions": [r.to_dict() for r in self.regions],
            "description": self.describe(),
            "notes": list(self.notes),
        }
        if include_image:
            out["image_png_base64"] = encode_png(self.image)
        return out


@dataclass(frozen=True)
class FreeSpace:
    """A rectangle of empty board, in board millimetres."""

    x: float
    y: float
    width: float
    height: float
    #: Smallest distance from this rectangle to any ink, in mm.
    clearance_mm: float = 0.0

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)

    def to_dict(self) -> dict:
        return {"x": round(self.x, 1), "y": round(self.y, 1),
                "width": round(self.width, 1), "height": round(self.height, 1),
                "center": [round(v, 1) for v in self.center],
                "clearance_mm": round(self.clearance_mm, 1)}


def encode_png(image: np.ndarray) -> str:
    cv2 = _cv2()
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        raise BoardMapError("could not encode the board image as PNG")
    return base64.b64encode(buf.tobytes()).decode("ascii")


# --------------------------------------------------------------------------

def read_board(frame: np.ndarray, board_map: BoardMap, *,
               px_per_mm: float = 1.0,
               ink_threshold: int = 190,
               min_region_mm2: float = 100.0,
               adaptive: bool = True) -> BoardReading:
    """Rectify a camera frame and find what has been drawn on it.

    Adaptive thresholding by default: a whiteboard photographed under room
    lighting has a strong brightness gradient, and a fixed threshold turns the
    darker half of an empty board into "ink".
    """
    cv2 = _cv2()
    if frame is None or frame.size == 0:
        raise BoardMapError("empty camera frame")

    rectified = board_map.rectify(frame, px_per_mm=px_per_mm)
    gray = cv2.cvtColor(rectified, cv2.COLOR_BGR2GRAY)
    notes: list[str] = []

    if adaptive:
        block = max(int(51 * px_per_mm) | 1, 3)
        mask = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                     cv2.THRESH_BINARY_INV, block, 12)
    else:
        _, mask = cv2.threshold(gray, ink_threshold, 255, cv2.THRESH_BINARY_INV)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    # Group nearby strokes so "an equation" reads as one region rather than
    # forty disconnected pen marks.
    group_px = max(int(round(12 * px_per_mm)) | 1, 3)
    grouped = cv2.dilate(mask, cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (group_px, group_px)))

    n, labels, stats, _ = cv2.connectedComponentsWithStats(grouped, connectivity=8)
    height_px = mask.shape[0]
    min_area_px = min_region_mm2 * (px_per_mm ** 2)

    regions: list[InkRegion] = []
    for i in range(1, n):
        area_px = stats[i, cv2.CC_STAT_AREA]
        if area_px < min_area_px:
            continue
        x_px = stats[i, cv2.CC_STAT_LEFT]
        y_px = stats[i, cv2.CC_STAT_TOP]
        w_px = stats[i, cv2.CC_STAT_WIDTH]
        h_px = stats[i, cv2.CC_STAT_HEIGHT]
        # Rectified images are top-down; board coordinates are bottom-up.
        x_mm = x_px / px_per_mm
        y_mm = (height_px - (y_px + h_px)) / px_per_mm
        # Report the true ink area inside the box, not the dilated one.
        true_area = float((mask[y_px:y_px + h_px, x_px:x_px + w_px] > 0).sum())
        regions.append(InkRegion(
            x=x_mm, y=y_mm,
            width=w_px / px_per_mm, height=h_px / px_per_mm,
            area_mm2=true_area / (px_per_mm ** 2)))

    regions.sort(key=lambda r: r.area_mm2, reverse=True)
    coverage = 100.0 * float((mask > 0).mean())
    if coverage > 60.0:
        notes.append(
            f"{coverage:.0f} % of the board reads as ink — the threshold is "
            f"probably picking up shadows or glare rather than marker")
    mean_level = float(gray.mean())
    if adaptive and coverage < 1.0 and mean_level < 110.0:
        # Adaptive thresholding compares each pixel to its neighbourhood, so a
        # *uniformly* dark frame looks exactly like a uniformly bright one.
        notes.append(
            f"the image is uniformly dark (mean level {mean_level:.0f}) but "
            f"almost nothing was detected as ink. Adaptive thresholding cannot "
            f"tell a fully covered board from an empty one — re-run with "
            f"adaptive=False and an explicit ink_threshold, or check the "
            f"exposure and the lens cap.")

    return BoardReading(image=rectified, ink_mask=mask, px_per_mm=px_per_mm,
                        board_width_mm=board_map.board_width_mm,
                        board_height_mm=board_map.board_height_mm,
                        regions=regions, coverage_percent=coverage, notes=notes)


def find_free_space(reading: BoardReading, width_mm: float, height_mm: float, *,
                    safe_area: SafeArea | None = None,
                    margin_mm: float = 20.0,
                    max_results: int = 5,
                    prefer: str = "largest_clearance") -> list[FreeSpace]:
    """Find places to put a ``width_mm x height_mm`` block of new content.

    Uses a summed-area table so every candidate position is tested in O(1).
    Results are sorted by how much empty space surrounds them, which is what
    makes an AI place its answer *next to* the question rather than jammed
    against it.
    """
    cv2 = _cv2()
    if width_mm <= 0 or height_mm <= 0:
        raise ValueError("requested free space must have positive dimensions")

    px = reading.px_per_mm
    mask = reading.ink_mask
    h_px, w_px = mask.shape

    need_w = int(round((width_mm + 2 * margin_mm) * px))
    need_h = int(round((height_mm + 2 * margin_mm) * px))
    if need_w <= 0 or need_h <= 0 or need_w > w_px or need_h > h_px:
        return []

    # cv2.integral accepts 8U/32F/64F only — an int32 input raises
    # "Unsupported format or combination of formats".
    occupied = (mask > 0).astype(np.uint8)
    integral = cv2.integral(occupied)          # (h+1, w+1), int32 sums

    # Distance to the nearest ink, for ranking. On a completely empty board
    # there is no ink to measure against and distanceTransform saturates, so
    # the result is clamped to something physically meaningful.
    free = (mask == 0).astype(np.uint8)
    distance = cv2.distanceTransform(free, cv2.DIST_L2, 3)
    max_clearance_px = float(np.hypot(h_px, w_px))
    distance = np.clip(distance, 0.0, max_clearance_px)

    step = max(int(round(10 * px)), 1)
    candidates: list[FreeSpace] = []
    for top in range(0, h_px - need_h + 1, step):
        for left in range(0, w_px - need_w + 1, step):
            bottom = top + need_h
            right = left + need_w
            total = (integral[bottom, right] - integral[top, right]
                     - integral[bottom, left] + integral[top, left])
            if total > 0:
                continue
            x_mm = (left / px) + margin_mm
            y_mm = ((h_px - bottom) / px) + margin_mm
            if safe_area is not None:
                corners = [(x_mm, y_mm),
                           (x_mm + width_mm, y_mm),
                           (x_mm + width_mm, y_mm + height_mm),
                           (x_mm, y_mm + height_mm)]
                if not bool(np.all(safe_area.contains_many(corners))):
                    continue
            cy = (top + bottom) // 2
            cx = (left + right) // 2
            clearance = float(distance[cy, cx]) / px
            candidates.append(FreeSpace(x=x_mm, y=y_mm, width=width_mm,
                                        height=height_mm,
                                        clearance_mm=clearance))

    if prefer == "largest_clearance":
        candidates.sort(key=lambda c: -c.clearance_mm)
    elif prefer == "top_left":
        candidates.sort(key=lambda c: (-c.y, c.x))
    elif prefer == "bottom_left":
        candidates.sort(key=lambda c: (c.y, c.x))

    # Drop near-duplicates so the caller gets genuinely different options.
    chosen: list[FreeSpace] = []
    for cand in candidates:
        if all(abs(cand.x - c.x) > width_mm * 0.5
               or abs(cand.y - c.y) > height_mm * 0.5 for c in chosen):
            chosen.append(cand)
        if len(chosen) >= max_results:
            break
    return chosen
