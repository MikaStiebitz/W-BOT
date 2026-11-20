"""The image pipeline: bytes in, a ready-to-plot toolpath out.

    upload / camera
        -> preprocess          (resize, denoise, levels, perspective)
        -> colour separation   (one mask per marker)
        -> tracing per layer   (contours / bitmap / centreline / hatch / stipple / tsp)
        -> pixels to board mm  (fit into the safe area)
        -> optimise            (simplify, merge, order)
        -> Toolpath + preview SVG + honest statistics

Every stage reports what it did.  The UI is expected to show the estimated
duration and colour-change count *before* anything is sent to the machine —
tuning a threshold and discovering forty minutes later that it traced sensor
noise is the failure mode this is designed to prevent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Sequence

import numpy as np

from ..config import MachineConfig
from ..core.geometry import Drawing, Layer, Polyline
from ..core.safearea import SafeArea
from ..core.toolpath import Stroke, Toolpath, ToolpathStats
from ..paths.fitting import FitResult, fit_drawing
from ..paths.optimize import OptimizeReport, optimize_toolpath
from . import colorsep as colorsep_mod
from .colorsep import ColorSepMode, ColorSepParams, ColorSeparation, separate
from .halftone import (HatchParams, StippleParams, hatch, stipple, stipple_dots,
                       tsp_path)
from .preprocess import (ImagingError, PreprocessParams, decode_image,
                         load_image, preprocess, to_gray)
from .trace_bitmap import BitmapParams, trace_bitmap
from .trace_centerline import CenterlineParams, trace_centerline
from .trace_contours import ContourParams, trace_contours

__all__ = [
    "TraceMode",
    "PipelineParams",
    "PipelineResult",
    "run_pipeline",
    "TRACE_MODES",
]

TraceMode = Literal["contours", "bitmap", "centerline", "halftone", "stipple", "tsp"]
TRACE_MODES: tuple[str, ...] = ("contours", "bitmap", "centerline",
                                "halftone", "stipple", "tsp")


@dataclass
class PipelineParams:
    """Everything the UI exposes, in one object."""

    trace_mode: TraceMode = "contours"
    preprocess: PreprocessParams = field(default_factory=PreprocessParams)
    contours: ContourParams = field(default_factory=ContourParams)
    bitmap: BitmapParams = field(default_factory=BitmapParams)
    centerline: CenterlineParams = field(default_factory=CenterlineParams)
    hatch: HatchParams = field(default_factory=HatchParams)
    stipple: StippleParams = field(default_factory=StippleParams)
    colorsep: ColorSepParams = field(default_factory=ColorSepParams)

    #: "single" ignores colour separation entirely and uses one marker.
    color_mode: ColorSepMode | Literal["single"] = "single"
    #: Explicit revolver order; ``None`` keeps the separation's own order.
    color_order: Sequence[int] | None = None

    #: In palette mode, draw each colour region as its outline plus a hatch
    #: fill, so a red disc comes out as a red disc rather than a red ring.
    fill_regions: bool = True
    #: Hatch pitch of the region fill, in source-image pixels.
    fill_spacing_px: float = 7.0

    # -- placement ------------------------------------------------------
    fit_mode: Literal["contain", "fill", "none"] = "contain"
    fit_margin_mm: float = 15.0
    align: tuple[str, str] = ("center", "center")
    max_scale: float | None = None

    # -- post-processing -------------------------------------------------
    optimize: bool = True
    simplify_tolerance_mm: float | None = None
    merge_tolerance_mm: float | None = None
    min_path_length_mm: float | None = None
    optimize_time_budget_s: float | None = None

    #: Hard ceiling on strokes; protects against tracing pure noise.
    max_paths: int = 20000
    tsp_time_budget_s: float = 5.0

    def validate(self) -> None:
        if self.trace_mode not in TRACE_MODES:
            raise ImagingError(
                f"unknown trace_mode {self.trace_mode!r}; "
                f"expected one of {', '.join(TRACE_MODES)}")
        if self.fit_mode not in ("contain", "fill", "none"):
            raise ImagingError(f"unknown fit_mode {self.fit_mode!r}")
        if self.max_paths < 1:
            raise ImagingError("max_paths must be >= 1")
        if self.fill_spacing_px < 1.0:
            raise ImagingError("fill_spacing_px must be >= 1")
        self.preprocess.validate()
        self.colorsep.validate()


@dataclass
class PipelineResult:
    """Toolpath plus everything needed to explain it to the user."""

    toolpath: Toolpath
    drawing: Drawing
    stats: ToolpathStats
    fit: FitResult | None = None
    optimize_report: OptimizeReport | None = None
    separation: ColorSeparation | None = None
    source_size_px: tuple[int, int] = (0, 0)
    elapsed_s: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def preview_svg(self, cfg: MachineConfig,
                    stroke_width_mm: float | None = None,
                    colors: Sequence[str] | None = None) -> str:
        """SVG preview. ``colors`` (hex per revolver slot) lets the caller
        render with the AMS magazine's actual marker colours."""
        palette = list(colors) if colors else [c.hex for c in cfg.colors]
        return self.drawing.to_svg(
            cfg.board.width_mm, cfg.board.height_mm, colors=palette,
            stroke_width_mm=stroke_width_mm or cfg.gondola.marker_line_width_mm)

    def to_dict(self) -> dict:
        return {
            "stats": self.stats.to_dict(),
            "n_paths": self.toolpath and len(self.toolpath.strokes),
            "source_size_px": list(self.source_size_px),
            "elapsed_s": round(self.elapsed_s, 3),
            "fit": self.fit.to_dict() if self.fit else None,
            "optimize": (self.optimize_report.to_dict()
                         if self.optimize_report else None),
            "separation": self.separation.to_dict() if self.separation else None,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------

def _trace_layer(image_bgr: np.ndarray, mask: np.ndarray | None,
                 params: PipelineParams, role: str,
                 density: float, sep_mode: str = "single") -> list[Polyline]:
    """Run the configured tracer over one colour layer."""
    mode = params.trace_mode
    working = image_bgr
    if mask is not None:
        # Blank everything this marker is not responsible for, so the tracer
        # only ever sees its own share of the picture.
        white = np.full_like(image_bgr, 255)
        m3 = mask[:, :, None] > 0
        working = np.where(m3, image_bgr, white)

    # Palette layers are *regions* of one colour, and the mask already is that
    # region — outline the mask directly and optionally hatch-fill it, instead
    # of running an edge detector over a flat area (which only ever finds the
    # border anyway, and noisily). This is what makes a red disc come out as
    # a red disc rather than a thin red ring.
    if (sep_mode == "palette" and mask is not None
            and mode in ("contours", "bitmap")):
        from .tracing import trace_filled_regions   # noqa: PLC0415
        out: list[Polyline] = []
        for pts, closed in trace_filled_regions(
                mask, min_points=params.bitmap.min_points,
                min_area_px=params.bitmap.min_area_px,
                include_holes=True):
            pl = Polyline(pts, closed=closed).simplify(
                params.bitmap.simplify_px).dedupe()
            if len(pl) >= 3:
                out.append(pl)
        if params.fill_regions:
            spacing = max(params.fill_spacing_px, 1.0)
            fill = HatchParams(angle_deg=45.0,
                               min_spacing_px=spacing,
                               max_spacing_px=spacing,
                               white_threshold=250,
                               min_run_px=3.0)
            out.extend(hatch(image_bgr, fill, mask=mask))
        return out

    if role == "shade" and mode in ("contours", "bitmap", "centerline"):
        # A "shade" layer has no meaningful outline — hatch it instead.
        hp = HatchParams(**{**params.hatch.__dict__})
        hp.min_spacing_px = max(params.hatch.min_spacing_px / max(density, 0.05),
                                1.0)
        hp.max_spacing_px = max(params.hatch.max_spacing_px / max(density, 0.05),
                                hp.min_spacing_px)
        return hatch(image_bgr, hp, mask=mask)

    if mode == "contours":
        return trace_contours(working, params.contours)
    if mode == "bitmap":
        return trace_bitmap(working, params.bitmap)
    if mode == "centerline":
        return trace_centerline(working, params.centerline)
    if mode == "halftone":
        hp = HatchParams(**{**params.hatch.__dict__})
        if density < 1.0:
            hp.min_spacing_px = max(params.hatch.min_spacing_px / max(density, 0.05), 1.0)
            hp.max_spacing_px = max(params.hatch.max_spacing_px / max(density, 0.05),
                                    hp.min_spacing_px)
        return hatch(image_bgr, hp, mask=mask)
    if mode == "stipple":
        sp = StippleParams(**{**params.stipple.__dict__})
        sp.n_points = max(int(params.stipple.n_points * density), 1)
        pts = stipple(working, sp)
        return stipple_dots(pts, sp.dot_radius_px)
    if mode == "tsp":
        sp = StippleParams(**{**params.stipple.__dict__})
        sp.n_points = max(int(params.stipple.n_points * density), 1)
        pts = stipple(working, sp)
        if len(pts) < 2:
            return []
        return [tsp_path(pts, time_budget_s=params.tsp_time_budget_s)]
    raise ImagingError(f"unhandled trace mode {mode!r}")   # pragma: no cover


def _pixels_to_board(paths: Sequence[Polyline], height_px: int) -> list[Polyline]:
    """Flip the Y axis: images grow downwards, the board grows upwards."""
    out = []
    for p in paths:
        pts = p.points.copy()
        pts[:, 1] = (height_px - 1) - pts[:, 1]
        out.append(Polyline(pts, closed=p.closed))
    return out


def run_pipeline(source: str | Path | bytes | np.ndarray,
                 cfg: MachineConfig,
                 params: PipelineParams | None = None,
                 *,
                 safe_area: SafeArea | None = None) -> PipelineResult:
    """Run the whole chain. ``source`` may be a path, raw bytes or a BGR array."""
    t0 = time.monotonic()
    params = params or PipelineParams()
    params.validate()
    sa = safe_area if safe_area is not None else SafeArea(cfg)
    warnings: list[str] = []

    # -- 1. load & condition -------------------------------------------
    if isinstance(source, (str, Path)):
        image = load_image(source)
    elif isinstance(source, (bytes, bytearray, memoryview)):
        image = decode_image(bytes(source))
    elif isinstance(source, np.ndarray):
        image = source
    else:
        raise ImagingError(f"unsupported image source type {type(source).__name__}")

    conditioned = preprocess(image, params.preprocess)
    h, w = conditioned.shape[:2]

    # -- 2. colour separation ------------------------------------------
    sep_params = params.colorsep
    sep_params.mode = params.color_mode
    if not sep_params.marker_colors:
        sep_params.marker_colors = [c.rgb for c in cfg.colors]
    if sep_params.slot_indices is not None \
            and any(int(i) >= cfg.n_colors for i in sep_params.slot_indices):
        raise ImagingError(
            f"slot_indices {list(sep_params.slot_indices)} reference a slot "
            f"this machine does not have (0..{cfg.n_colors - 1})")
    separation = separate(conditioned, sep_params)
    if not separation.layers:
        warnings.extend(separation.notes)
        empty = Toolpath([])
        return PipelineResult(
            toolpath=empty, drawing=Drawing([]),
            stats=empty.stats(cfg), separation=separation,
            source_size_px=(w, h), elapsed_s=time.monotonic() - t0,
            warnings=warnings + ["nothing to draw: the image separated into no layers"])

    # -- 3. trace each layer -------------------------------------------
    layers: list[Layer] = []
    total_paths = 0
    for layer in separation.layers:
        mask = layer.mask if separation.mode != "single" else None
        paths = _trace_layer(conditioned, mask, params, layer.role,
                             layer.density, sep_mode=separation.mode)
        paths = _pixels_to_board(paths, h)
        if total_paths + len(paths) > params.max_paths:
            keep = max(params.max_paths - total_paths, 0)
            warnings.append(
                f"marker {layer.slot + 1} produced {len(paths)} paths; kept "
                f"{keep} to stay under max_paths={params.max_paths}. "
                f"Raise the tracing thresholds or max_paths.")
            paths = paths[:keep]
        total_paths += len(paths)
        if paths:
            layers.append(Layer(layer.slot, paths,
                                name=f"marker{layer.slot + 1}"))

    drawing = Drawing(layers, metadata={
        "trace_mode": params.trace_mode,
        "color_mode": params.color_mode,
        "source_size_px": [w, h],
    }).non_empty()

    if drawing.n_paths == 0:
        empty = Toolpath([])
        return PipelineResult(
            toolpath=empty, drawing=drawing, stats=empty.stats(cfg),
            separation=separation, source_size_px=(w, h),
            elapsed_s=time.monotonic() - t0,
            warnings=warnings + [
                "nothing to draw: tracing found no paths. Lower canny_low / "
                "canny_high or raise the contrast."])

    # -- 4. place it on the board --------------------------------------
    fitted, fit = fit_drawing(drawing, sa, mode=params.fit_mode,
                              margin_mm=params.fit_margin_mm,
                              align=params.align, max_scale=params.max_scale)
    if fit.note:
        warnings.append(fit.note)

    toolpath = Toolpath.from_drawing(fitted, params.color_order)

    # -- 5. optimise ----------------------------------------------------
    report: OptimizeReport | None = None
    if params.optimize:
        toolpath, report = optimize_toolpath(
            toolpath, cfg,
            simplify_tolerance_mm=params.simplify_tolerance_mm,
            merge_tolerance_mm=params.merge_tolerance_mm,
            min_path_length_mm=params.min_path_length_mm,
            time_budget_s=params.optimize_time_budget_s,
            color_order=params.color_order)
        warnings.extend(report.notes)
        fitted = Drawing(
            [Layer(c, [s.polyline for s in toolpath.strokes if s.color_index == c])
             for c in toolpath.colors_used],
            metadata=dict(fitted.metadata))

    # -- 6. final safety check -----------------------------------------
    violations = toolpath.validate(sa, raise_on_violation=False)
    if violations:
        warnings.append(
            f"{len(violations)} point(s) fell outside the safe area after "
            f"optimisation and would be refused by the G-code writer: "
            f"{violations[0]}")

    stats = toolpath.stats(cfg)
    if stats.n_color_changes > max(len(toolpath.colors_used) - 1, 0):
        warnings.append(
            f"{stats.n_color_changes} colour changes — expected at most "
            f"{max(len(toolpath.colors_used) - 1, 0)}")

    return PipelineResult(
        toolpath=toolpath,
        drawing=fitted,
        stats=stats,
        fit=fit,
        optimize_report=report,
        separation=separation,
        source_size_px=(w, h),
        elapsed_s=time.monotonic() - t0,
        warnings=warnings,
    )
