"""Image pipeline: every trace mode, colour separation, and the guard rails.

These are regression tests with tolerances, not pixel-exact assertions: the
point is to notice when a change makes the tracer emit ten times as many paths
or stop finding the shapes at all.
"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
pytestmark = pytest.mark.imaging

from wbbot.core.safearea import SafeArea                        # noqa: E402
from wbbot.imaging.colorsep import (ColorSepParams, rgb_to_lab,  # noqa: E402
                                    separate)
from wbbot.imaging.halftone import (HatchParams, StippleParams,  # noqa: E402
                                    hatch, stipple, stipple_dots, tsp_path)
from wbbot.imaging.pipeline import (TRACE_MODES, PipelineParams,  # noqa: E402
                                    run_pipeline)
from wbbot.imaging.preprocess import (ImagingError,              # noqa: E402
                                      PreprocessParams,
                                      correct_perspective, decode_image,
                                      preprocess, to_gray)
from wbbot.imaging.trace_bitmap import BitmapParams, trace_bitmap  # noqa: E402
from wbbot.imaging.trace_centerline import (CenterlineParams,    # noqa: E402
                                            trace_centerline)
from wbbot.imaging.trace_contours import ContourParams, trace_contours  # noqa: E402
from wbbot.imaging.tracing import (thin_mask, trace_filled_regions,  # noqa: E402
                                   trace_thin_mask)


# ----------------------------------------------------------------------
# preprocessing
# ----------------------------------------------------------------------

def test_preprocess_resizes_and_keeps_three_channels(reference_image):
    out = preprocess(reference_image, PreprocessParams(max_dimension_px=200))
    assert max(out.shape[:2]) == 200
    assert out.shape[2] == 3


def test_preprocess_never_upscales(reference_image):
    out = preprocess(reference_image, PreprocessParams(max_dimension_px=4000))
    assert out.shape[:2] == reference_image.shape[:2]


def test_gamma_follows_the_standard_convention(reference_image):
    """``out = in ** (1/gamma)``: >1 brightens, <1 darkens, as in PIL."""
    base = float(to_gray(preprocess(reference_image, PreprocessParams())).mean())
    bright = float(to_gray(preprocess(
        reference_image, PreprocessParams(gamma=2.0))).mean())
    dark = float(to_gray(preprocess(
        reference_image, PreprocessParams(gamma=0.5))).mean())
    assert bright > base > dark


def test_invert(reference_image):
    normal = preprocess(reference_image, PreprocessParams(denoise=False))
    inverted = preprocess(reference_image,
                          PreprocessParams(denoise=False, invert=True))
    assert np.allclose(normal.astype(int) + inverted.astype(int), 255, atol=1)


def test_bad_params_are_rejected(reference_image):
    with pytest.raises(ImagingError):
        preprocess(reference_image, PreprocessParams(gamma=0.0))
    with pytest.raises(ImagingError):
        preprocess(reference_image, PreprocessParams(max_dimension_px=1))
    with pytest.raises(ImagingError):
        preprocess(reference_image,
                   PreprocessParams(perspective_quad=[[0, 0], [1, 1]]))


def test_perspective_correction_squares_a_quad():
    img = np.full((400, 400, 3), 255, np.uint8)
    cv2.rectangle(img, (100, 100), (300, 300), (0, 0, 0), -1)
    quad = [[100, 100], [300, 110], [295, 300], [105, 295]]
    out = correct_perspective(img, quad, (200, 200))
    assert out.shape[:2] == (200, 200)
    # The rectified patch should be nearly all ink.
    assert float((to_gray(out) < 128).mean()) > 0.9


def test_decode_image_roundtrip(reference_image):
    ok, buf = cv2.imencode(".png", reference_image)
    assert ok
    decoded = decode_image(buf.tobytes())
    assert decoded.shape == reference_image.shape


def test_decode_rejects_garbage():
    with pytest.raises(ImagingError):
        decode_image(b"this is not an image")
    with pytest.raises(ImagingError):
        decode_image(b"")


# ----------------------------------------------------------------------
# low-level tracing
# ----------------------------------------------------------------------

def test_trace_thin_mask_follows_a_line_once():
    """The whole reason for not using findContours on Canny output."""
    mask = np.zeros((50, 200), np.uint8)
    mask[25, 20:180] = 255
    paths = trace_thin_mask(mask)
    assert len(paths) == 1
    total = float(np.sum(np.linalg.norm(np.diff(paths[0], axis=0), axis=1)))
    assert total == pytest.approx(159.0, abs=2.0), "the line was traced twice"


def test_trace_thin_mask_splits_at_a_junction():
    mask = np.zeros((100, 100), np.uint8)
    mask[50, 10:90] = 255
    mask[10:50, 50] = 255
    paths = trace_thin_mask(mask)
    assert len(paths) >= 3, "a T junction should yield three branches"


def test_trace_thin_mask_handles_a_closed_loop():
    mask = np.zeros((100, 100), np.uint8)
    cv2.circle(mask, (50, 50), 30, 255, 1)
    paths = trace_thin_mask(mask)
    assert len(paths) >= 1
    assert len(paths[0]) > 100


def test_trace_thin_mask_on_empty_input():
    assert trace_thin_mask(np.zeros((20, 20), np.uint8)) == []


def test_trace_filled_regions_finds_holes():
    mask = np.zeros((200, 200), np.uint8)
    cv2.circle(mask, (100, 100), 70, 255, -1)
    cv2.circle(mask, (100, 100), 30, 0, -1)
    rings = trace_filled_regions(mask, include_holes=True)
    assert len(rings) == 2
    outer = trace_filled_regions(mask, include_holes=False)
    assert len(outer) == 1


def test_thin_mask_reduces_a_thick_bar_to_one_pixel():
    mask = np.zeros((60, 200), np.uint8)
    mask[20:40, 20:180] = 255
    thin = thin_mask(mask)
    assert (thin > 0).sum() < (mask > 0).sum() / 5


# ----------------------------------------------------------------------
# tracers
# ----------------------------------------------------------------------

def test_contours_find_the_shapes(reference_image):
    paths = trace_contours(reference_image, ContourParams())
    assert 3 <= len(paths) <= 400
    assert all(len(p) >= 2 for p in paths)
    total = sum(p.length for p in paths)
    assert total > 500


def test_contour_thresholds_change_the_output(reference_image):
    few = trace_contours(reference_image, ContourParams(canny_low=150, canny_high=250))
    many = trace_contours(reference_image, ContourParams(canny_low=20, canny_high=60))
    assert sum(p.length for p in many) > sum(p.length for p in few)


def test_contour_params_validate():
    with pytest.raises(ImagingError):
        ContourParams(canny_low=200, canny_high=100).validate()


def test_bitmap_traces_closed_silhouettes(reference_image):
    paths = trace_bitmap(reference_image, BitmapParams())
    assert paths
    assert all(p.closed for p in paths)
    assert all(len(p) >= 3 for p in paths)


def test_bitmap_min_area_filters_specks(reference_image):
    small = trace_bitmap(reference_image, BitmapParams(min_area_px=1.0))
    large = trace_bitmap(reference_image, BitmapParams(min_area_px=5000.0))
    assert len(large) <= len(small)


def test_centerline_collapses_strokes(line_art_image):
    centre = trace_centerline(line_art_image, CenterlineParams())
    outline = trace_bitmap(line_art_image, BitmapParams())
    centre_len = sum(p.length for p in centre)
    outline_len = sum(p.length for p in outline)
    assert centre_len < outline_len * 0.75, (
        "centreline tracing must be shorter than outlining the same strokes")
    assert centre_len > 500


# ----------------------------------------------------------------------
# halftone
# ----------------------------------------------------------------------

def test_hatch_is_denser_where_the_image_is_darker():
    img = np.zeros((200, 400, 3), np.uint8)
    img[:, :200] = 30            # dark half
    img[:, 200:] = 200           # light half
    lines = hatch(img, HatchParams(angle_deg=0.0, min_spacing_px=3,
                                   max_spacing_px=20))
    assert lines
    left = sum(p.length for p in lines
               if p.points[:, 0].mean() < 200)
    right = sum(p.length for p in lines
                if p.points[:, 0].mean() >= 200)
    assert left > right


def test_hatch_skips_white(reference_image):
    white = np.full((200, 200, 3), 255, np.uint8)
    assert hatch(white, HatchParams()) == []


def test_cross_hatch_adds_lines(reference_image):
    single = hatch(reference_image, HatchParams(cross_hatch=False))
    double = hatch(reference_image, HatchParams(cross_hatch=True))
    assert len(double) > len(single)


def test_hatch_respects_a_mask(reference_image):
    mask = np.zeros(reference_image.shape[:2], np.uint8)
    mask[:, :300] = 255
    lines = hatch(reference_image, HatchParams(), mask=mask)
    assert lines
    assert max(p.points[:, 0].max() for p in lines) < 360


def test_stipple_concentrates_on_dark_areas():
    img = np.full((200, 400, 3), 250, np.uint8)
    img[:, :100] = 10
    pts = stipple(img, StippleParams(n_points=800, relaxation_rounds=3, seed=1))
    assert len(pts) == 800
    assert float((pts[:, 0] < 120).mean()) > 0.8


def test_stipple_of_a_blank_image_is_empty():
    white = np.full((100, 100, 3), 255, np.uint8)
    assert len(stipple(white, StippleParams(n_points=100))) == 0


def test_stipple_dots_are_drawable():
    pts = np.array([[10.0, 10.0], [20.0, 20.0]])
    dots = stipple_dots(pts, radius_px=1.0)
    assert len(dots) == 2
    assert all(d.length > 0 for d in dots)


def test_tsp_visits_every_point_once():
    rng = np.random.default_rng(2)
    pts = rng.uniform(0, 100, size=(120, 2))
    path = tsp_path(pts, time_budget_s=0.5)
    assert len(path) == len(pts)
    assert np.allclose(np.sort(path.points, axis=0), np.sort(pts, axis=0))


def test_tsp_is_shorter_than_the_input_order():
    rng = np.random.default_rng(3)
    pts = rng.uniform(0, 100, size=(150, 2))
    naive = float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1)))
    path = tsp_path(pts, time_budget_s=1.0)
    assert path.length < naive * 0.5


def test_tsp_degenerate_inputs():
    assert len(tsp_path(np.zeros((0, 2)))) == 0
    assert len(tsp_path(np.array([[1.0, 2.0]]))) == 1


# ----------------------------------------------------------------------
# colour separation
# ----------------------------------------------------------------------

def test_rgb_to_lab_separates_lightness_from_chroma():
    """L tracks lightness; neutrals sit at the chroma origin (a=b=128)."""
    black = rgb_to_lab((0, 0, 0))
    grey = rgb_to_lab((128, 128, 128))
    white = rgb_to_lab((255, 255, 255))
    assert black[0] < grey[0] < white[0]
    for neutral in (black, grey, white):
        assert neutral[1] == pytest.approx(128, abs=1)
        assert neutral[2] == pytest.approx(128, abs=1)
    # A saturated colour must be far from the neutral axis.
    red = rgb_to_lab((211, 47, 47))
    assert np.hypot(red[1] - 128, red[2] - 128) > 40


def test_palette_assignment_sends_each_colour_to_its_own_marker():
    """The behaviour that actually matters: red ink onto the red pen."""
    img = np.full((300, 300, 3), 255, np.uint8)
    img[0:100, :] = (17, 17, 17)          # BGR black
    img[100:200, :] = (47, 47, 211)       # BGR red
    img[200:300, :] = (192, 101, 21)      # BGR blue
    sep = separate(img, ColorSepParams(
        mode="palette",
        marker_colors=[(17, 17, 17), (211, 47, 47), (21, 101, 192)]))
    by_slot = {l.slot: l for l in sep.layers}
    assert set(by_slot) == {0, 1, 2}
    # Each marker's mask must cover its own band of the image.
    assert by_slot[0].mask[50, 150] > 0, "black band did not go to the black pen"
    assert by_slot[1].mask[150, 150] > 0, "red band did not go to the red pen"
    assert by_slot[2].mask[250, 150] > 0, "blue band did not go to the blue pen"


def test_palette_mode_uses_each_marker_once(reference_image):
    sep = separate(reference_image, ColorSepParams(mode="palette"))
    slots = [l.slot for l in sep.layers]
    assert sorted(slots) == [0, 1, 2]
    assert len(set(slots)) == 3, "two clusters collapsed onto one marker"


def test_palette_masks_do_not_overlap(reference_image):
    sep = separate(reference_image, ColorSepParams(mode="palette"))
    stacked = np.stack([(l.mask > 0) for l in sep.layers])
    assert stacked.sum(axis=0).max() <= 1


def test_tonal_mode_assigns_roles(reference_image):
    sep = separate(reference_image, ColorSepParams(mode="tonal"))
    roles = [l.role for l in sep.layers]
    assert roles == ["outline", "shade", "shade"]
    assert sep.layers[1].density < sep.layers[2].density


def test_single_mode_makes_one_layer(reference_image):
    sep = separate(reference_image, ColorSepParams(mode="single"))
    assert len(sep.layers) == 1 and sep.layers[0].slot == 0


def test_separation_params_validate():
    with pytest.raises(ImagingError):
        ColorSepParams(mode="rainbow").validate()      # type: ignore[arg-type]
    with pytest.raises(ImagingError):
        ColorSepParams(marker_colors=[(0, 0)]).validate()
    with pytest.raises(ImagingError):
        ColorSepParams(tonal_cuts=(50, 200)).validate()


def test_separation_serialises(reference_image):
    import json
    json.dumps(separate(reference_image, ColorSepParams(mode="tonal")).to_dict())


# ----------------------------------------------------------------------
# full pipeline
# ----------------------------------------------------------------------

@pytest.mark.parametrize("mode", TRACE_MODES)
def test_every_trace_mode_produces_a_valid_toolpath(cfg, reference_image, mode):
    sa = SafeArea(cfg)
    params = PipelineParams(trace_mode=mode, color_mode="single")
    params.stipple.n_points = 400
    params.tsp_time_budget_s = 0.5
    params.optimize_time_budget_s = 0.5
    result = run_pipeline(reference_image, cfg, params, safe_area=sa)

    assert len(result.toolpath.strokes) > 0, f"{mode} produced nothing"
    assert result.stats.draw_length_mm > 100
    assert result.stats.total_duration_s > 0
    # Everything must be inside the safe area — no exceptions.
    assert np.all(sa.contains_many(result.toolpath.all_points()))
    assert result.toolpath.validate(sa, raise_on_violation=False) == []


def test_tsp_mode_makes_exactly_one_stroke(cfg, reference_image):
    params = PipelineParams(trace_mode="tsp", color_mode="single")
    params.stipple.n_points = 300
    params.tsp_time_budget_s = 0.5
    result = run_pipeline(reference_image, cfg, params)
    assert len(result.toolpath.strokes) == 1
    assert result.stats.n_color_changes == 0


@pytest.mark.parametrize("color_mode", ["palette", "tonal"])
def test_three_colour_jobs_never_exceed_two_changes(cfg, reference_image,
                                                    color_mode):
    """Milestone 8: three colours, at most two revolver changes."""
    params = PipelineParams(trace_mode="contours", color_mode=color_mode)
    params.optimize_time_budget_s = 0.5
    result = run_pipeline(reference_image, cfg, params)
    assert len(result.toolpath.colors_used) == 3
    assert result.stats.n_color_changes == 2


def test_explicit_colour_order_is_honoured(cfg, reference_image):
    params = PipelineParams(trace_mode="contours", color_mode="palette",
                            color_order=[2, 1, 0])
    params.optimize_time_budget_s = 0.5
    result = run_pipeline(reference_image, cfg, params)
    assert result.toolpath.colors_used == (2, 1, 0)


def test_optimisation_reduces_travel(cfg, reference_image):
    sa = SafeArea(cfg)
    raw = run_pipeline(reference_image, cfg,
                       PipelineParams(trace_mode="contours", optimize=False),
                       safe_area=sa)
    opt = run_pipeline(reference_image, cfg,
                       PipelineParams(trace_mode="contours", optimize=True),
                       safe_area=sa)
    assert opt.stats.travel_length_mm < raw.stats.travel_length_mm
    assert opt.optimize_report is not None


def test_max_paths_is_enforced_and_reported(cfg, reference_image):
    params = PipelineParams(trace_mode="contours", max_paths=5)
    params.contours.canny_low = 10
    params.contours.canny_high = 40
    params.contours.min_length_px = 0.0
    result = run_pipeline(reference_image, cfg, params)
    assert len(result.toolpath.strokes) <= 5
    assert any("max_paths" in w for w in result.warnings)


def test_blank_image_reports_instead_of_failing(cfg):
    white = np.full((200, 200, 3), 255, np.uint8)
    result = run_pipeline(white, cfg, PipelineParams(trace_mode="contours"))
    assert result.toolpath.is_empty
    assert any("nothing to draw" in w for w in result.warnings)


def test_fit_places_the_drawing_inside_the_safe_area(cfg, reference_image):
    sa = SafeArea(cfg)
    result = run_pipeline(reference_image, cfg,
                          PipelineParams(fit_margin_mm=50.0), safe_area=sa)
    x0, y0, x1, y1 = result.toolpath.bounds
    assert x0 >= sa.bounds[0] and y0 >= sa.bounds[1]
    assert x1 <= sa.bounds[2] and y1 <= sa.bounds[3]


def test_y_axis_is_flipped_from_image_to_board(cfg):
    """Images grow downwards, the board grows upwards."""
    img = np.full((400, 400, 3), 255, np.uint8)
    cv2.rectangle(img, (150, 20), (250, 60), (0, 0, 0), -1)   # near image TOP
    result = run_pipeline(img, cfg, PipelineParams(trace_mode="bitmap",
                                                   optimize=False))
    pts = result.toolpath.all_points()
    centre_y = pts[:, 1].mean()
    board_mid = cfg.board.height_mm / 2
    assert centre_y > board_mid, "the shape should end up in the upper board half"


def test_result_serialises_and_previews(cfg, reference_image):
    import json
    result = run_pipeline(reference_image, cfg, PipelineParams())
    json.dumps(result.to_dict())
    svg = result.preview_svg(cfg)
    assert svg.startswith("<svg") and "</svg>" in svg


def test_pipeline_accepts_encoded_bytes(cfg, reference_image):
    ok, buf = cv2.imencode(".png", reference_image)
    assert ok
    result = run_pipeline(buf.tobytes(), cfg, PipelineParams())
    assert len(result.toolpath.strokes) > 0


def test_unknown_trace_mode_is_rejected(cfg, reference_image):
    with pytest.raises(ImagingError, match="unknown trace_mode"):
        run_pipeline(reference_image, cfg,
                     PipelineParams(trace_mode="magic"))   # type: ignore[arg-type]


def test_pipeline_output_generates_valid_gcode(cfg, reference_image):
    """The point of the whole chain."""
    from wbbot.core.gcode import GCodeWriter
    result = run_pipeline(reference_image, cfg,
                          PipelineParams(trace_mode="contours",
                                         color_mode="palette"))
    prog = GCodeWriter(cfg).render_toolpath(result.toolpath, title="photo")
    assert prog.text.isascii()
    assert prog.stats.n_color_changes <= 2
    assert len([l for l in prog.lines if l.startswith("G1 X")]) > 10
