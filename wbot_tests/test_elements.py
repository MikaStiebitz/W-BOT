"""Canvas-editor elements: fonts, formulas, plots, symbols and the compose/preview API."""
import numpy as np
import pytest
from fastapi.testclient import TestClient

from wbot.api.app import create_app
from wbot.config import Config, load_config
from wbot.elements import catalog, compose
from wbot.machine import Machine
from wbot.mathlayout import MathError, layout_math
from wbot.plotgraph import ExprError, compile_expr, render_plot
from wbot.textfont import FONTS, glyph, measure, text_strokes

CFG = Config()


@pytest.fixture()
def client():
    m = Machine(load_config())
    with TestClient(create_app(m, serve_ui=False)) as c:
        yield c
        m.shutdown()


@pytest.mark.parametrize("font", list(FONTS))
def test_every_font_draws_ascii_and_symbols(font):
    st = text_strokes("Hello 123 αβ ≤ → ä", 10, 0, 0, font=font)
    assert len(st) > 10
    assert all(np.isfinite(s.pts).all() for s in st)
    w, h = measure("Hello", 10, font=font)
    assert 20 < w < 100 and h == pytest.approx(10)


def test_mono_layout_has_fixed_pitch():
    w1, _ = measure("iiii", 14, mono=True)
    w2, _ = measure("WWWW", 14, mono=True)
    assert w1 == pytest.approx(w2)


def test_unknown_character_falls_back():
    assert glyph("☃")[1]          # snowman -> "?" glyph


@pytest.mark.parametrize("src", [
    r"E = mc^2", r"x = \frac{-b \pm \sqrt{b^2-4ac}}{2a}", r"\sum_{i=1}^{n} i^2", r"\int_0^\infty e^{-x^2}\,dx",
    r"\lim_{x \to 0} \frac{\sin x}{x}", r"\begin{pmatrix} a & b \\ c & d \end{pmatrix}", r"\sqrt[3]{x}",
    r"f(x)=\begin{cases} x & x>0 \\ 0 & x \le 0 \end{cases}", r"a \\ b", "x² + y₁ ≤ π"])
def test_formulas_lay_out(src):
    strokes, w, asc, desc = layout_math(src)
    assert strokes and w > 0 and asc > 0
    assert all(np.isfinite(p).all() for p in strokes)


@pytest.mark.parametrize("src", ["", r"\frac{1}", r"\unknown{x}", "a}", r"\left( x", r"\end{matrix}"])
def test_bad_formulas_raise_readable_errors(src):
    with pytest.raises(MathError):
        layout_math(src)


def test_expressions_are_whitelisted():
    f = compile_expr("2x^2 - 3 + sin(pi*x)")
    assert f(np.array([0.0, 1.0])) == pytest.approx([-3.0, -1.0])
    for bad in ["__import__('os').system('x')", "x.real", "open('f')", "lambda: 1", "x; y", "foo(x)"]:
        with pytest.raises(ExprError):
            compile_expr(bad)
    with pytest.raises(ExprError):
        compile_expr("9**9**9")(np.zeros(2))


def test_plot_breaks_at_asymptotes_and_stays_in_frame():
    st = render_plot({"w": 200, "h": 100, "xmin": -6, "xmax": 6, "ymin": -3, "ymax": 3,
                      "curves": [{"kind": "y", "expr": "tan(x)"}]})
    a = np.concatenate([s.pts for s in st])
    assert a[:, 0].min() >= -115 and a[:, 0].max() <= 100 + 1e-6
    assert a[:, 1].min() >= -65 and a[:, 1].max() <= 53


def test_compose_reports_boxes_ranges_and_errors():
    els = [{"id": "t", "type": "text", "text": "Hi", "x": 300, "y": 400, "size": 20},
           {"id": "m", "type": "math", "latex": r"\frac{", "x": 100, "y": 100},
           {"id": "s", "type": "symbol", "kind": "icon", "sym": "heart", "size": 50, "x": 700, "y": 500, "rot": 90}]
    d, meta = compose(els, CFG)
    assert [m["id"] for m in meta] == ["t", "m", "s"]
    assert meta[1]["error"] and meta[1]["bbox"] is None
    assert meta[0]["range"][1] == meta[2]["range"][0]
    assert len(d.strokes) == meta[2]["range"][1]
    x0, y0, x1, y1 = meta[2]["bbox"]
    assert (x0 + x1) / 2 == pytest.approx(700, abs=1) and (y0 + y1) / 2 == pytest.approx(500, abs=1)


def test_catalog_has_palettes():
    c = catalog()
    assert len(c["fonts"]) == len(FONTS) and c["icons"] and all(g["glyphs"] for g in c["glyph_groups"])


def test_preview_and_job_accept_elements(client):
    body = {"elements": [{"id": "a", "type": "text", "text": "W-BOT", "x": 750, "y": 500, "size": 30},
                         {"id": "b", "type": "plot", "x": 750, "y": 300, "w": 300, "h": 150,
                          "curves": [{"kind": "y", "expr": "sin(x)"}]}]}
    r = client.post("/api/preview", json=body)
    assert r.status_code == 200, r.text
    pv = r.json()
    assert [e["id"] for e in pv["elements"]] == ["a", "b"] and pv["stats"]["strokes"] > 5
    empty = client.post("/api/preview", json={"elements": [{"id": "x", "type": "math", "latex": r"\frac{"}]}).json()
    assert empty["elements"][0]["error"] and empty["drawing"]["strokes"] == []
    assert client.get("/api/elements/catalog").json()["fonts"]


def test_compose_is_fast_and_reports_elements(client):
    r = client.post("/api/compose", json={"elements": [{"id": "a", "type": "text", "text": "Hi", "x": 700, "y": 500}]})
    assert r.status_code == 200 and r.json()["elements"][0]["bbox"] and r.json()["drawing"]["strokes"]
    assert client.post("/api/compose", json={"elements": []}).json()["bounds"] is None


def test_parts_outside_the_safe_area_are_clipped_like_the_plotter_does():
    big = {"id": "t", "type": "text", "text": "HELLO", "size": 80, "x": 10, "y": 500}
    d, meta = compose([big], CFG)
    x0 = CFG.safe_rect()[0]
    assert meta[0]["clipped"] and meta[0]["bbox"][0] < x0               # box still covers the whole text
    assert min(s.pts[:, 0].min() for s in d.strokes) >= x0 - 1e-6        # but nothing is drawn outside
