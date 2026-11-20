"""AMS marker magazine: slot state, persistence, and pipeline wiring."""

from __future__ import annotations

import numpy as np
import pytest

from wbbot.ams import Ams, AmsError


@pytest.fixture
def ams(cfg, tmp_path):
    return Ams(cfg, tmp_path / "ams.yaml")


# ----------------------------------------------------------------------
# slot state
# ----------------------------------------------------------------------

def test_defaults_come_from_the_machine_config(ams, cfg):
    assert len(ams.slots) == cfg.n_colors
    assert ams.slots[0].hex == cfg.colors[0].hex
    assert ams.slots[0].name == cfg.colors[0].name
    assert all(s.enabled for s in ams.slots)


def test_set_slot_colour_and_name(ams):
    slot = ams.set_slot(1, name="grün", hex="#2e7d32")
    assert slot.hex == "#2e7d32"
    assert slot.name == "grün"
    assert slot.rgb == (0x2e, 0x7d, 0x32)


def test_changes_survive_a_restart(cfg, tmp_path):
    first = Ams(cfg, tmp_path / "ams.yaml")
    first.set_slot(2, name="orange", hex="#ff9800", enabled=False)
    second = Ams(cfg, tmp_path / "ams.yaml")
    assert second.slots[2].hex == "#ff9800"
    assert second.slots[2].name == "orange"
    assert not second.slots[2].enabled


def test_invalid_hex_is_rejected(ams):
    with pytest.raises(AmsError, match="#rrggbb"):
        ams.set_slot(0, hex="red")
    with pytest.raises(AmsError):
        ams.set_slot(0, hex="#12345g")


def test_unknown_slot_is_rejected(ams):
    with pytest.raises(AmsError, match="does not exist"):
        ams.set_slot(9, name="x")


def test_the_last_enabled_slot_cannot_be_disabled(ams):
    ams.set_slot(0, enabled=False)
    ams.set_slot(1, enabled=False)
    with pytest.raises(AmsError, match="at least one marker"):
        ams.set_slot(2, enabled=False)
    assert ams.slots[2].enabled


def test_enabled_helpers_report_the_right_slots(ams):
    ams.set_slot(1, enabled=False)
    assert ams.slot_indices() == [0, 2]
    assert len(ams.marker_colors()) == 2
    with pytest.raises(AmsError, match="disabled"):
        ams.require_enabled(1)
    assert ams.require_enabled(0).index == 0


def test_corrupt_file_falls_back_to_defaults(cfg, tmp_path):
    path = tmp_path / "ams.yaml"
    path.write_text("slots: [{index: not-a-number}]\n- broken", encoding="utf-8")
    ams = Ams(cfg, path)
    assert ams.slots[0].hex == cfg.colors[0].hex


def test_to_dict_is_json_safe(ams):
    import json
    json.dumps(ams.to_dict())


# ----------------------------------------------------------------------
# pipeline wiring
# ----------------------------------------------------------------------

@pytest.mark.imaging
def test_separation_maps_layers_onto_the_loaded_slots(cfg, ams):
    """Two enabled bays: layers must come back tagged 0 and 2, never 1."""
    cv2 = pytest.importorskip("cv2")
    from wbbot.imaging.colorsep import ColorSepParams, separate

    ams.set_slot(0, hex="#111111")
    ams.set_slot(2, hex="#d32f2f")
    ams.set_slot(1, enabled=False)

    img = np.full((200, 300, 3), 255, np.uint8)
    img[:100, :] = (17, 17, 17)          # black band (BGR)
    img[100:, :] = (47, 47, 211)         # red band

    sep = separate(img, ColorSepParams(
        mode="palette",
        marker_colors=ams.marker_colors(),
        slot_indices=ams.slot_indices()))
    slots = sorted(l.slot for l in sep.layers)
    assert slots == [0, 2], f"layers went to slots {slots}, bay 1 is empty"


@pytest.mark.imaging
def test_pipeline_honours_slot_indices_end_to_end(cfg, ams, reference_image):
    pytest.importorskip("cv2")
    from wbbot.imaging.pipeline import PipelineParams, run_pipeline

    ams.set_slot(1, enabled=False)
    params = PipelineParams(trace_mode="contours", color_mode="palette")
    params.colorsep.marker_colors = ams.marker_colors()
    params.colorsep.slot_indices = ams.slot_indices()
    result = run_pipeline(reference_image, cfg, params)
    used = set(result.toolpath.colors_used)
    assert 1 not in used, "a stroke was assigned to the empty bay"
    assert used <= {0, 2}


@pytest.mark.imaging
def test_slot_indices_out_of_range_are_rejected(cfg, reference_image):
    pytest.importorskip("cv2")
    from wbbot.imaging.pipeline import PipelineParams, run_pipeline
    from wbbot.imaging.preprocess import ImagingError

    params = PipelineParams(trace_mode="contours", color_mode="palette")
    params.colorsep.marker_colors = [(0, 0, 0)]
    params.colorsep.slot_indices = [7]
    with pytest.raises(ImagingError, match="slot"):
        run_pipeline(reference_image, cfg, params)


@pytest.mark.imaging
def test_palette_fill_regions_add_hatch_inside_the_shapes(cfg, reference_image):
    """A red disc must come out as a disc, not a ring."""
    pytest.importorskip("cv2")
    from wbbot.imaging.pipeline import PipelineParams, run_pipeline

    filled = run_pipeline(reference_image, cfg,
                          PipelineParams(trace_mode="contours",
                                         color_mode="palette",
                                         fill_regions=True))
    outline_only = run_pipeline(reference_image, cfg,
                                PipelineParams(trace_mode="contours",
                                               color_mode="palette",
                                               fill_regions=False))
    assert filled.stats.draw_length_mm > outline_only.stats.draw_length_mm * 1.5, (
        "fill_regions did not add meaningful interior coverage")
    assert filled.stats.n_color_changes <= 2


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------

@pytest.fixture
def client(cfg, simulator, tmp_path, monkeypatch):
    # Point the AMS file (and any other config writes) at a temp dir so the
    # repo's config/ is never touched by a test.
    monkeypatch.setenv("WBBOT_CONFIG_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from wbbot.api.app import create_app
    from wbbot.driver.machine import Machine
    from wbbot.driver.transport import SimulatorTransport

    machine = Machine(cfg, transport=SimulatorTransport(simulator),
                      simulator=simulator)
    app = create_app(cfg, machine, serve_ui=False)
    with TestClient(app) as test_client:
        yield test_client


def test_ams_get_reports_slots_and_active(client):
    data = client.get("/api/ams").json()
    assert len(data["slots"]) == 3
    assert data["active"] == 0
    assert data["enabled"] == [0, 1, 2]


def test_ams_slot_update_and_select(client):
    updated = client.put("/api/ams/slot",
                         json={"index": 1, "hex": "#00aa55",
                               "name": "grün"}).json()
    assert updated["slots"][1]["hex"] == "#00aa55"

    selected = client.post("/api/ams/select", json={"index": 1}).json()
    assert selected["active"] == 1
    assert client.get("/api/status").json()["color"] == 1


def test_ams_rejects_bad_updates(client):
    assert client.put("/api/ams/slot",
                      json={"index": 0, "hex": "red"}).status_code == 400
    assert client.put("/api/ams/slot",
                      json={"index": 9, "name": "x"}).status_code == 400


def test_selecting_a_disabled_slot_is_refused(client):
    client.put("/api/ams/slot", json={"index": 2, "enabled": False})
    response = client.post("/api/ams/select", json={"index": 2})
    assert response.status_code == 400
    assert "disabled" in response.json()["detail"]


def test_preview_uses_the_ams_colours(client, reference_image):
    """Change a slot to green — the preview SVG must contain that green."""
    cv2 = pytest.importorskip("cv2")
    import io
    client.put("/api/ams/slot", json={"index": 0, "hex": "#00bb44"})
    ok, buf = cv2.imencode(".png", reference_image)
    files = {"file": ("t.png", io.BytesIO(buf.tobytes()), "image/png")}
    data = client.post("/api/image/preview?trace_mode=contours&color_mode=palette",
                       files=files).json()
    assert "#00bb44" in data["preview_svg"]
