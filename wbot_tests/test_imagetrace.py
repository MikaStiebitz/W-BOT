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


def _render() -> bytes:
    """A light object on a light grey floor with a soft shadow and a dark part - like a CAD render."""
    import numpy as np
    from PIL import ImageFilter
    im = Image.new("RGB", (600, 450), (166, 166, 166))
    shadow = Image.new("L", im.size, 0)
    ImageDraw.Draw(shadow).ellipse((60, 300, 330, 400), fill=60)
    im.paste((120, 120, 120), mask=shadow.filter(ImageFilter.GaussianBlur(12)))
    d = ImageDraw.Draw(im)
    d.rectangle((220, 80, 420, 360), fill=(182, 182, 182))          # faint: only 16 grey levels above the floor
    d.rectangle((260, 140, 380, 200), fill=(30, 30, 30))            # dark panel inside
    d.ellipse((440, 300, 520, 340), fill=(240, 240, 240))
    a = np.asarray(im).astype(np.int16) + np.random.default_rng(1).integers(-1, 2, (450, 600, 3))
    buf = io.BytesIO()
    Image.fromarray(a.clip(0, 255).astype("uint8")).save(buf, "PNG")
    return buf.getvalue()


def _px(r, cfg, x, y, w=600, h=450):
    """Image pixel -> board mm (same fit as trace_image)."""
    x0, y0, x1, y1 = cfg.safe_rect()
    s = min((x1 - x0) / w, (y1 - y0) / h)
    return x0 + ((x1 - x0) - w * s) / 2 + x * s, y0 + ((y1 - y0) - h * s) / 2 + (h - y) * s, s


def _ink_near(r, cfg, x, y, tol_px=4):
    """Is any drawn segment within ``tol_px`` image pixels of pixel (x, y)?"""
    import numpy as np
    bx, by, s = _px(r, cfg, x, y)
    q = np.array([bx, by])
    for t in r.drawing.strokes:
        a, b = t.pts[:-1], t.pts[1:]
        ab = b - a
        u = np.clip(((q - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0, 1)
        if (np.hypot(*(a + ab * u[:, None] - q).T) <= tol_px * s).any():
            return True
    return False


def test_photo_mode_outlines_a_light_object_on_a_light_floor():
    cfg = load_config()
    r = trace_image(_render(), cfg, TraceOptions(mode="photo"))
    assert r.info["mode"] == "photo"
    for x in range(230, 420, 25):                                   # top and bottom rim of the faint box
        assert _ink_near(r, cfg, x, 80) and _ink_near(r, cfg, x, 360), x
    for y in range(90, 360, 25):                                    # left and right rim
        assert _ink_near(r, cfg, 220, y) and _ink_near(r, cfg, 420, y), y
    assert _ink_near(r, cfg, 320, 140) and _ink_near(r, cfg, 320, 200)   # inner panel
    assert not _ink_near(r, cfg, 70, 350, tol_px=6)                 # soft shadow rim is not drawn


def test_auto_picks_photo_for_renders_and_lines_for_line_art():
    cfg = load_config()
    assert trace_image(_render(), cfg, TraceOptions(mode="auto")).info["mode"] == "photo"
    assert trace_image(_png(), cfg, TraceOptions(mode="auto")).info["mode"] == "lines"


def test_photo_detail_controls_the_amount_of_line():
    import numpy as np
    from PIL import ImageFilter
    rng = np.random.default_rng(3)
    a = rng.integers(0, 255, (60, 80, 3)).astype("uint8")
    im = Image.fromarray(a).resize((800, 600), Image.BICUBIC).filter(ImageFilter.GaussianBlur(2))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    cfg = load_config()
    low = trace_image(buf.getvalue(), cfg, TraceOptions(mode="photo", detail=0)).drawing.length()
    high = trace_image(buf.getvalue(), cfg, TraceOptions(mode="photo", detail=100)).drawing.length()
    assert 0 < low < high


def test_shading_hatches_dark_areas_in_long_strokes():
    cfg = load_config()
    plain = trace_image(_render(), cfg, TraceOptions(mode="photo"))
    shaded = trace_image(_render(), cfg, TraceOptions(mode="photo", shade=True))
    extra = len(shaded.drawing.strokes) - len(plain.drawing.strokes)
    assert shaded.drawing.length() > plain.drawing.length() * 1.2
    assert 0 < extra <= 3                                            # one zigzag stroke per dark patch
