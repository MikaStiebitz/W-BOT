"""Split an image across the three markers actually loaded in the revolver.

Two modes, because two very different things get asked for:

``palette``
    Quantise to k colours **in CIELab**, then assign each cluster to the
    nearest physical marker.  Lab is used rather than RGB because Euclidean
    distance in Lab approximates perceived colour difference (the CIE76 dE
    metric), whereas RGB distance is dominated by whichever channel happens to
    be largest.  "Nearest marker" then means nearest *to the eye*, which is the
    only definition that matters when the answer picks up a physical pen.

    Note that Lab separates lightness (L) from chroma (a, b) but does not
    weight them: two colours of similar lightness stay close even when their
    hues differ.  Clusters are therefore assigned to markers **greedily and
    exclusively** — each marker is used at most once — so a three-colour image
    cannot collapse onto a single pen just because its tones are alike.

``tonal``
    One marker draws the outlines, the other two shade with different hatch
    densities.  This is usually the better choice for photographs: a
    whiteboard has no grey, so tone has to come from line density anyway.

Either way the output is one mask per marker slot.  What gets *drawn* into
each mask (contours, hatching, stipple) is the pipeline's decision, not this
module's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Sequence

import numpy as np

from .preprocess import ImagingError, _cv2, to_gray

__all__ = [
    "ColorSepMode",
    "ColorSepParams",
    "ColorLayer",
    "ColorSeparation",
    "separate",
    "rgb_to_lab",
]

ColorSepMode = Literal["palette", "tonal", "single"]


def rgb_to_lab(rgb: Sequence[int]) -> np.ndarray:
    """Convert an 8-bit sRGB triple to CIELab (OpenCV's 0..255 Lab scale)."""
    cv2 = _cv2()
    patch = np.array([[[rgb[2], rgb[1], rgb[0]]]], dtype=np.uint8)   # BGR
    return cv2.cvtColor(patch, cv2.COLOR_BGR2LAB)[0, 0].astype(np.float32)


@dataclass
class ColorSepParams:
    mode: ColorSepMode = "tonal"
    #: sRGB of the markers physically loaded, in magazine order.
    marker_colors: Sequence[Sequence[int]] = field(
        default_factory=lambda: [(17, 17, 17), (211, 47, 47), (21, 101, 192)])
    #: Revolver slot index for each entry of ``marker_colors``. Lets the AMS
    #: pass only its *enabled* slots: two markers loaded in bays 0 and 2 give
    #: ``marker_colors=[rgb0, rgb2], slot_indices=[0, 2]`` and the layers come
    #: back tagged with the real revolver positions.
    slot_indices: Sequence[int] | None = None
    #: Weight of the chroma (a, b) channels against lightness when assigning
    #: clusters to markers. >1 prefers matching the hue over the brightness.
    chroma_weight: float = 1.0
    #: Explicit cluster -> slot override from the UI (cluster index -> slot).
    slot_override: dict[int, int] = field(default_factory=dict)
    #: Treat near-white as paper and never draw it.
    white_threshold: int = 235
    #: k-means restarts; more is steadier but slower.
    kmeans_attempts: int = 4
    kmeans_max_iter: int = 40
    seed: int = 0
    #: tonal mode: luminance cuts between outline / mid / dark, 0..255.
    tonal_cuts: tuple[int, int] = (170, 90)
    #: Remove specks smaller than this many pixels from each mask.
    min_region_px: int = 12

    def validate(self) -> None:
        if self.mode not in ("palette", "tonal", "single"):
            raise ImagingError(f"unknown colour separation mode {self.mode!r}")
        if not self.marker_colors:
            raise ImagingError("at least one marker colour must be configured")
        for i, c in enumerate(self.marker_colors):
            if len(c) != 3 or not all(0 <= int(v) <= 255 for v in c):
                raise ImagingError(
                    f"marker_colors[{i}] must be an RGB triple 0..255, got {c!r}")
        if not 0 <= self.white_threshold <= 255:
            raise ImagingError("white_threshold must be 0..255")
        a, b = self.tonal_cuts
        if not 0 <= b < a <= 255:
            raise ImagingError(
                f"tonal_cuts must satisfy 0 <= dark < light <= 255, got {self.tonal_cuts}")
        if self.slot_indices is not None:
            if len(self.slot_indices) != len(self.marker_colors):
                raise ImagingError(
                    f"slot_indices ({len(self.slot_indices)}) must match "
                    f"marker_colors ({len(self.marker_colors)})")
            if any(int(i) < 0 for i in self.slot_indices):
                raise ImagingError("slot_indices must be non-negative")
        if not 0.1 <= self.chroma_weight <= 5.0:
            raise ImagingError(
                f"chroma_weight must be 0.1..5, got {self.chroma_weight}")

    def slot_for(self, position: int) -> int:
        """Map a marker-list position to its physical revolver slot."""
        if self.slot_indices is not None:
            return int(self.slot_indices[position])
        return position


@dataclass
class ColorLayer:
    """One marker's share of the image."""

    slot: int                       # 0-based revolver index
    mask: np.ndarray                # uint8, 255 = this marker draws here
    rgb: tuple[int, int, int]
    pixel_count: int
    #: What the pipeline should do here: "outline" or "shade".
    role: str = "outline"
    #: Relative shading density 0..1 (tonal mode).
    density: float = 1.0

    @property
    def coverage(self) -> float:
        return self.pixel_count / max(self.mask.size, 1)


@dataclass
class ColorSeparation:
    layers: list[ColorLayer]
    mode: ColorSepMode
    #: Quantised preview image (BGR) showing what the markers can reproduce.
    preview_bgr: np.ndarray | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "layers": [
                {"slot": l.slot, "rgb": list(l.rgb), "role": l.role,
                 "density": round(l.density, 3),
                 "coverage_percent": round(100.0 * l.coverage, 2)}
                for l in self.layers
            ],
            "notes": list(self.notes),
        }


def _despeckle(mask: np.ndarray, min_px: int) -> np.ndarray:
    if min_px <= 1:
        return mask
    cv2 = _cv2()
    n, labels, stats, _ = cv2.connectedComponentsWithStats(
        (mask > 0).astype(np.uint8), connectivity=8)
    out = np.zeros_like(mask)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_px:
            out[labels == i] = 255
    return out


def _separate_palette(img_bgr: np.ndarray,
                      params: ColorSepParams) -> ColorSeparation:
    cv2 = _cv2()
    h, w = img_bgr.shape[:2]
    n_slots = len(params.marker_colors)

    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    gray = to_gray(img_bgr)
    ink = gray < params.white_threshold
    if not ink.any():
        return ColorSeparation([], "palette", notes=[
            "the image is entirely brighter than white_threshold — nothing to draw"])

    samples = lab[ink].reshape(-1, 3).astype(np.float32)
    k = min(n_slots, len(np.unique(samples, axis=0)))
    if k < 1:
        k = 1
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
                params.kmeans_max_iter, 1.0)
    cv2.setRNGSeed(params.seed)
    _, labels, centers = cv2.kmeans(samples, k, None, criteria,
                                    params.kmeans_attempts,
                                    cv2.KMEANS_PP_CENTERS)
    labels = labels.ravel()

    marker_lab = np.array([rgb_to_lab(c) for c in params.marker_colors],
                          dtype=np.float32)
    # Assign clusters to markers greedily by Lab distance, one marker each,
    # so two clusters never collapse onto the same pen. chroma_weight scales
    # the a/b channels: >1 prefers hue matches over brightness matches, which
    # is usually what "put the red ink on the red pen" means.
    channel_scale = np.array([1.0, params.chroma_weight, params.chroma_weight],
                             dtype=np.float32)
    cost = np.linalg.norm(
        (centers[:, None, :] - marker_lab[None, :, :]) * channel_scale[None, None, :],
        axis=2)
    assignment: dict[int, int] = {}
    taken: set[int] = set()
    for cluster in np.argsort(cost.min(axis=1)):
        order = np.argsort(cost[cluster])
        for slot in order:
            if int(slot) not in taken:
                assignment[int(cluster)] = int(slot)
                taken.add(int(slot))
                break
        else:                                       # pragma: no cover
            assignment[int(cluster)] = int(np.argmin(cost[cluster]))
    assignment.update({int(c): int(s) for c, s in params.slot_override.items()})

    ink_idx = np.flatnonzero(ink.ravel())
    layers: list[ColorLayer] = []
    preview = np.full_like(img_bgr, 255)
    for cluster in range(k):
        position = assignment.get(cluster, cluster % n_slots)
        sel = ink_idx[labels == cluster]
        mask = np.zeros(h * w, dtype=np.uint8)
        mask[sel] = 255
        mask = _despeckle(mask.reshape(h, w), params.min_region_px)
        rgb = tuple(int(v) for v in params.marker_colors[position])
        preview[mask > 0] = (rgb[2], rgb[1], rgb[0])
        layers.append(ColorLayer(slot=params.slot_for(position), mask=mask,
                                 rgb=rgb,
                                 pixel_count=int((mask > 0).sum()),
                                 role="outline", density=1.0))

    layers.sort(key=lambda l: l.slot)
    notes = []
    if k < n_slots:
        notes.append(f"only {k} distinct colour(s) found; "
                     f"{n_slots - k} marker(s) will not be used")
    return ColorSeparation(layers, "palette", preview_bgr=preview, notes=notes)


def _separate_tonal(img_bgr: np.ndarray,
                    params: ColorSepParams) -> ColorSeparation:
    cv2 = _cv2()
    gray = to_gray(img_bgr)
    light_cut, dark_cut = params.tonal_cuts
    n_slots = len(params.marker_colors)

    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 60, 160, L2gradient=True)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8))

    mid = ((gray < light_cut) & (gray >= dark_cut)).astype(np.uint8) * 255
    dark = (gray < dark_cut).astype(np.uint8) * 255

    specs = [
        (0, edges, "outline", 1.0),
        (1, mid, "shade", 0.45),
        (2, dark, "shade", 1.0),
    ][:max(n_slots, 1)]

    layers: list[ColorLayer] = []
    for position, mask, role, density in specs:
        mask = _despeckle(mask, params.min_region_px)
        rgb = tuple(int(v) for v in params.marker_colors[position])
        layers.append(ColorLayer(slot=params.slot_for(position), mask=mask,
                                 rgb=rgb,
                                 pixel_count=int((mask > 0).sum()),
                                 role=role, density=density))

    notes = []
    if n_slots < 3:
        notes.append(f"tonal mode is designed for 3 markers; {n_slots} configured")
    return ColorSeparation(layers, "tonal", notes=notes)


def _separate_single(img_bgr: np.ndarray,
                     params: ColorSepParams) -> ColorSeparation:
    gray = to_gray(img_bgr)
    mask = _despeckle((gray < params.white_threshold).astype(np.uint8) * 255,
                      params.min_region_px)
    rgb = tuple(int(v) for v in params.marker_colors[0])
    return ColorSeparation(
        [ColorLayer(slot=params.slot_for(0), mask=mask, rgb=rgb,
                    pixel_count=int((mask > 0).sum()), role="outline")],
        "single")


def separate(img_bgr: np.ndarray,
             params: ColorSepParams | None = None) -> ColorSeparation:
    """Split an image into one mask per marker slot."""
    params = params or ColorSepParams()
    params.validate()
    if img_bgr is None or img_bgr.size == 0:
        raise ImagingError("empty image")
    if img_bgr.ndim == 2:
        cv2 = _cv2()
        img_bgr = cv2.cvtColor(img_bgr, cv2.COLOR_GRAY2BGR)

    if params.mode == "palette":
        return _separate_palette(img_bgr, params)
    if params.mode == "tonal":
        return _separate_tonal(img_bgr, params)
    return _separate_single(img_bgr, params)
