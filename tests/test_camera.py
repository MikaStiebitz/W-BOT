"""Camera: homography, board read-back, free-space search.

Uses :class:`StillCamera` with synthetic board photographs, so the whole
perception path is testable without a webcam.
"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from wbbot.camera.boardmap import BoardMap, BoardMapError      # noqa: E402
from wbbot.camera.capture import CameraError, StillCamera      # noqa: E402
from wbbot.camera.readback import (encode_png, find_free_space,  # noqa: E402
                                   read_board)
from wbbot.core.safearea import SafeArea                       # noqa: E402

BOARD_W, BOARD_H = 1500.0, 1000.0


@pytest.fixture
def straight_corners():
    """A camera looking dead-on at the board, 1 px per 2 mm."""
    return [[0, 500], [750, 500], [750, 0], [0, 0]]      # BL, BR, TR, TL


@pytest.fixture
def board_map(straight_corners):
    return BoardMap.from_corners(straight_corners, BOARD_W, BOARD_H)


def board_photo(width=750, height=500, marks=()):
    """A synthetic photo of a whiteboard: bright, with dark marks."""
    img = np.full((height, width, 3), 238, np.uint8)
    for (x, y, w, h) in marks:
        cv2.rectangle(img, (x, y), (x + w, y + h), (25, 25, 25), -1)
    return img


# ----------------------------------------------------------------------
# homography
# ----------------------------------------------------------------------

def test_corners_map_to_board_corners(board_map, straight_corners):
    got = board_map.pixel_to_board(straight_corners)
    expected = [[0, 0], [BOARD_W, 0], [BOARD_W, BOARD_H], [0, BOARD_H]]
    assert np.allclose(got, expected, atol=1e-6)


def test_round_trip_pixel_board_pixel(board_map):
    points = [[100, 100], [400, 250], [700, 480]]
    back = board_map.board_to_pixel(board_map.pixel_to_board(points))
    assert np.allclose(back, points, atol=1e-6)


def test_reprojection_error_is_zero_for_the_calibration_corners(
        board_map, straight_corners):
    assert board_map.reprojection_error(straight_corners) < 1e-6


def test_perspective_view_still_maps_correctly():
    """A camera at an angle — the case the homography actually exists for."""
    skewed = [[40, 470], [720, 495], [690, 30], [70, 15]]
    bm = BoardMap.from_corners(skewed, BOARD_W, BOARD_H)
    assert bm.reprojection_error(skewed) < 1e-6
    centre = bm.pixel_to_board([[(40 + 720 + 690 + 70) / 4,
                                 (470 + 495 + 30 + 15) / 4]])[0]
    assert 0 < centre[0] < BOARD_W and 0 < centre[1] < BOARD_H


def test_degenerate_corners_are_rejected():
    with pytest.raises(BoardMapError):
        BoardMap.from_corners([[0, 0], [0, 0], [0, 0], [0, 0]],
                              BOARD_W, BOARD_H)
    with pytest.raises(BoardMapError):
        BoardMap.from_corners([[0, 0], [1, 1]], BOARD_W, BOARD_H)


def test_singular_matrix_is_rejected():
    with pytest.raises(BoardMapError):
        BoardMap(np.zeros((3, 3)), BOARD_W, BOARD_H)


def test_rectify_produces_a_board_shaped_image(board_map):
    img = board_photo()
    out = board_map.rectify(img, px_per_mm=0.5)
    assert out.shape[1] == pytest.approx(BOARD_W * 0.5, abs=1)
    assert out.shape[0] == pytest.approx(BOARD_H * 0.5, abs=1)


def test_rectify_flips_y_so_row_zero_is_the_board_top(board_map):
    """Board coordinates grow upwards; images grow downwards."""
    img = board_photo(marks=[(300, 20, 150, 60)])       # near the photo top
    out = board_map.rectify(img, px_per_mm=0.5)
    gray = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
    dark_rows = np.where((gray < 128).any(axis=1))[0]
    assert dark_rows.min() < out.shape[0] * 0.25, "the mark should be near row 0"


def test_from_config_returns_none_without_a_homography(cfg):
    assert BoardMap.from_config(cfg) is None


def test_from_config_reads_a_stored_homography(cfg, board_map):
    calibrated = cfg.replace(camera={"homography": board_map.to_list(),
                                     "enabled": True})
    loaded = BoardMap.from_config(calibrated)
    assert loaded is not None
    assert np.allclose(loaded.matrix, board_map.matrix)


# ----------------------------------------------------------------------
# camera
# ----------------------------------------------------------------------

def test_still_camera_lifecycle():
    camera = StillCamera(board_photo())
    with pytest.raises(CameraError):
        camera.capture()
    camera.open()
    frame = camera.capture()
    assert frame.shape[2] == 3 and camera.captures == 1
    camera.close()
    assert not camera.is_open


def test_still_camera_context_manager():
    with StillCamera(board_photo()) as camera:
        assert camera.capture() is not None


def test_still_camera_returns_copies():
    camera = StillCamera(board_photo())
    camera.open()
    first = camera.capture()
    first[:] = 0
    assert camera.capture().mean() > 100, "capture() handed out its own buffer"


def test_still_camera_rejects_a_bad_array():
    with pytest.raises(CameraError):
        StillCamera(np.zeros((10, 10)))


def test_make_camera_refuses_when_disabled(cfg):
    from wbbot.camera.capture import make_camera
    with pytest.raises(CameraError, match="disabled"):
        make_camera(cfg)


# ----------------------------------------------------------------------
# read-back
# ----------------------------------------------------------------------

def test_empty_board_reads_as_empty(board_map):
    reading = read_board(board_photo(), board_map, px_per_mm=0.5)
    assert reading.coverage_percent < 2.0
    assert "empty" in reading.describe().lower()


def test_marks_are_found_in_board_coordinates(board_map):
    # A block in the photo's lower-left maps to the board's lower-left.
    reading = read_board(board_photo(marks=[(50, 400, 120, 80)]),
                         board_map, px_per_mm=0.5)
    assert reading.regions
    biggest = reading.regions[0]
    cx, cy = biggest.center
    assert cx < BOARD_W / 2, "the mark should be in the left half"
    assert cy < BOARD_H / 2, "the mark should be in the lower half"


def test_two_separate_marks_read_as_two_regions(board_map):
    reading = read_board(board_photo(marks=[(50, 50, 100, 80),
                                            (600, 380, 100, 80)]),
                         board_map, px_per_mm=0.5)
    assert len(reading.regions) == 2


def test_nearby_strokes_group_into_one_region(board_map):
    """An equation should read as "one thing", not forty pen marks.

    The photo is half board scale, so 6 px of photo gap is 12 mm on the board —
    about the spacing between handwritten characters, which is exactly what the
    grouping radius is tuned for.
    """
    marks = [(200 + i * 12, 200, 8, 40) for i in range(8)]
    reading = read_board(board_photo(marks=marks), board_map, px_per_mm=1.0)
    assert len(reading.regions) == 1


def test_widely_separated_marks_stay_separate(board_map):
    """Grouping must not swallow genuinely distinct content."""
    marks = [(100, 200, 40, 40), (600, 200, 40, 40)]
    reading = read_board(board_photo(marks=marks), board_map, px_per_mm=1.0)
    assert len(reading.regions) == 2


def test_description_is_useful_to_an_ai(board_map):
    reading = read_board(board_photo(marks=[(100, 100, 200, 100)]),
                         board_map, px_per_mm=0.5)
    text = reading.describe()
    assert "mm" in text and "region" in text
    assert "centred at" in text


def test_reading_serialises(board_map):
    import json
    reading = read_board(board_photo(marks=[(100, 100, 80, 80)]),
                         board_map, px_per_mm=0.5)
    payload = reading.to_dict(include_image=True)
    json.dumps(payload)
    assert payload["image_png_base64"]
    assert payload["regions"][0]["center"]


def test_encode_png_round_trips():
    import base64
    img = board_photo(80, 60)
    raw = base64.b64decode(encode_png(img))
    decoded = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == img.shape


def test_empty_frame_is_rejected(board_map):
    with pytest.raises(BoardMapError):
        read_board(np.zeros((0, 0, 3), np.uint8), board_map)


# ----------------------------------------------------------------------
# free space
# ----------------------------------------------------------------------

def test_free_space_on_an_empty_board(board_map, cfg):
    reading = read_board(board_photo(), board_map, px_per_mm=0.5)
    spots = find_free_space(reading, 300, 200, safe_area=SafeArea(cfg))
    assert spots
    assert all(s.width == 300 and s.height == 200 for s in spots)


def test_free_space_avoids_existing_ink(board_map, cfg):
    """The behaviour behind 'write the answer next to the question'."""
    # Ink across the whole left half of the photo.
    reading = read_board(board_photo(marks=[(0, 0, 375, 500)]),
                         board_map, px_per_mm=0.5)
    spots = find_free_space(reading, 400, 300, safe_area=SafeArea(cfg),
                            margin_mm=10)
    assert spots
    for spot in spots:
        assert spot.x > BOARD_W * 0.4, (
            f"free space at x={spot.x} overlaps the inked left half")


def test_free_space_returns_nothing_when_the_board_is_full(board_map, cfg):
    """A fully covered board must be reported as full.

    Fixed thresholding is used here on purpose: adaptive thresholding compares
    each pixel to its neighbours and therefore cannot distinguish a uniformly
    dark board from a uniformly bright one.
    """
    reading = read_board(board_photo(marks=[(0, 0, 750, 500)]),
                         board_map, px_per_mm=0.5, adaptive=False)
    assert reading.coverage_percent > 95
    assert find_free_space(reading, 400, 300, safe_area=SafeArea(cfg)) == []


def test_a_uniformly_dark_frame_is_flagged_rather_than_called_empty(board_map):
    """The adaptive-threshold blind spot must be reported, not hidden."""
    reading = read_board(board_photo(marks=[(0, 0, 750, 500)]),
                         board_map, px_per_mm=0.5, adaptive=True)
    assert any("uniformly dark" in n for n in reading.notes)


def test_clearance_is_finite_on_an_empty_board(board_map, cfg):
    reading = read_board(board_photo(), board_map, px_per_mm=0.5)
    spots = find_free_space(reading, 200, 150, safe_area=SafeArea(cfg))
    assert spots
    assert all(0 <= s.clearance_mm < 10_000 for s in spots)


def test_free_space_respects_the_safe_area(board_map, cfg):
    reading = read_board(board_photo(), board_map, px_per_mm=0.5)
    sa = SafeArea(cfg)
    for spot in find_free_space(reading, 200, 150, safe_area=sa, max_results=8):
        corners = [(spot.x, spot.y), (spot.x + spot.width, spot.y),
                   (spot.x + spot.width, spot.y + spot.height),
                   (spot.x, spot.y + spot.height)]
        assert np.all(sa.contains_many(corners))


def test_free_space_is_ranked_by_clearance(board_map, cfg):
    reading = read_board(board_photo(marks=[(0, 0, 200, 500)]),
                         board_map, px_per_mm=0.5)
    spots = find_free_space(reading, 200, 150, safe_area=SafeArea(cfg),
                            max_results=5)
    clearances = [s.clearance_mm for s in spots]
    assert clearances == sorted(clearances, reverse=True)


def test_free_space_results_are_distinct(board_map, cfg):
    reading = read_board(board_photo(), board_map, px_per_mm=0.5)
    spots = find_free_space(reading, 200, 150, safe_area=SafeArea(cfg),
                            max_results=5)
    for i, a in enumerate(spots):
        for b in spots[i + 1:]:
            assert abs(a.x - b.x) > 50 or abs(a.y - b.y) > 40


def test_free_space_rejects_bad_dimensions(board_map):
    reading = read_board(board_photo(), board_map, px_per_mm=0.5)
    with pytest.raises(ValueError):
        find_free_space(reading, 0, 100)


def test_free_space_serialises(board_map, cfg):
    import json
    reading = read_board(board_photo(), board_map, px_per_mm=0.5)
    spots = find_free_space(reading, 200, 150, safe_area=SafeArea(cfg))
    json.dumps([s.to_dict() for s in spots])


# ----------------------------------------------------------------------
# the reference scenario, end to end
# ----------------------------------------------------------------------

async def test_ai_can_see_the_board_and_find_room_next_to_it(machine, cfg,
                                                             board_map):
    """Section 5.9's reference loop, minus the handwriting recognition.

    A human writes something on the left of the board. The AI photographs it,
    finds free space beside it, and writes there without overlapping.
    """
    from wbbot.mcp.service import PlotterService

    photo = board_photo(marks=[(40, 180, 300, 120)])       # 'equation' on the left
    service = PlotterService(machine, default_wait=True, default_timeout_s=120.0)
    service.attach_camera(StillCamera(photo), board_map)
    service._camera.open()

    seen = await service.read_board()
    assert seen["ok"] and seen["regions"], "the AI could not see the writing"
    written = seen["regions"][0]
    assert written["center"][0] < BOARD_W / 2

    room = await service.find_free_space(width_mm=400, height_mm=120,
                                         margin_mm=30)
    assert room["ok"], room.get("error")
    spot = room["best"]
    assert spot["x"] > written["x"] + written["width"] * 0.5, (
        "the free space overlaps the existing writing")

    result = await service.write_text("x = 4", spot["x"] + 10,
                                      spot["y"] + 40, height_mm=60)
    assert result["ok"], result.get("error")
    assert result["completed"]
