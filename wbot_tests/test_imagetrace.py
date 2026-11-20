import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from wbot.api.app import create_app
from wbot.config import load_config
from wbot.imagetrace import ImageError, TraceOptions, trace_image
from wbot.machine import Machine


def _png(kind="art") -> bytes:
    im = Image.new("RGB", (600, 400), "white")
    d = ImageDraw.Draw(im)
    d.ellipse((60, 60, 300, 340), outline="black", width=4)
    d.line((340, 80, 560, 340), fill="red", width=5)
    if kind == "blue":
        d.rectangle((380, 60, 560, 140), fill="blue")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


@pytest.mark.parametrize("mode", ["auto", "lines", "outline", "fill"])
def test_every_mode_traces_inside_the_safe_area(mode):
    cfg = load_config()
    r = trace_image(_png(), cfg, TraceOptions(mode=mode, use_pen_colors=True))
    x0, y0, x1, y1 = cfg.safe_rect()
    bx0, by0, bx1, by1 = r.drawing.bounds()
    assert x0 - 1 <= bx0 and bx1 <= x1 + 1 and y0 - 1 <= by0 and by1 <= y1 + 1
    assert r.info["strokes"] == len(r.drawing.strokes) > 0


def test_pen_colors_split_black_and_red():
    r = trace_image(_png(), load_config(), TraceOptions(mode="lines", use_pen_colors=True))
    assert set(r.info["pens"]) == {0, 1}          # black ring, red line - no stray blue from anti-aliasing


def test_single_pen_goes_to_the_chosen_slot():
    r = trace_image(_png(), load_config(), TraceOptions(mode="lines", pen=2))
    assert set(r.info["pens"]) == {2}


def test_blank_and_broken_images_give_clear_errors():
    blank = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(blank, "PNG")
    with pytest.raises(ImageError, match="Nothing to draw"):
        trace_image(blank.getvalue(), load_config())
    with pytest.raises(ImageError, match="not a readable image"):
        trace_image(b"definitely not an image", load_config())


def test_upload_endpoint_accepts_png_and_svg():
    m = Machine(load_config())
    with TestClient(create_app(m, serve_ui=False)) as c:
        r = c.post("/api/import", files={"file": ("a.png", _png(), "image/png")}, data={"options": '{"mode": "lines"}'})
        assert r.status_code == 200, r.text
        assert r.json()["info"]["mode"] == "lines" and r.json()["drawing"]["strokes"]
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="50mm" height="50mm"><rect x="5" y="5" width="40" height="40" fill="none" stroke="black"/></svg>'
        r = c.post("/api/import", files={"file": ("a.svg", svg, "image/svg+xml")})
        assert r.status_code == 200 and r.json()["drawing"]["strokes"]
        r = c.post("/api/import", files={"file": ("x.png", b"nope", "image/png")})
        assert r.status_code == 400 and r.json()["code"] == "bad_image"
    m.shutdown()
