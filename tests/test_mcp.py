"""MCP layer: safe expression evaluation, plotting primitives, every tool.

Milestone 10 asks that an AI client can plot a function and write text; the whole
tool surface is exercised against the simulator here.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from wbbot.core.safearea import SafeArea
from wbbot.mcp import plotting as P
from wbbot.mcp.expressions import (ExpressionError, compile_expression,
                                   validate_expression)
from wbbot.mcp.plotting import PlotError, PlotWindow
from wbbot.mcp.server import TOOL_SPECS
from wbbot.mcp.service import PlotterService


@pytest.fixture
async def service(machine):
    return PlotterService(machine, default_wait=True, default_timeout_s=60.0)


# ======================================================================
# expression safety
# ======================================================================

@pytest.mark.parametrize("expr", [
    "__import__('os').system('echo hi')",
    "().__class__.__bases__",
    "lambda: 1",
    "eval('1+1')",
    "open('/etc/passwd')",
    "x.__class__",
    "globals()",
    "exec('x=1')",
    "'a'*10",
])
def test_dangerous_expressions_are_rejected(expr):
    with pytest.raises(ExpressionError):
        compile_expression(expr, ("x",))


def test_unknown_names_are_rejected():
    with pytest.raises(ExpressionError, match="unknown name"):
        compile_expression("sin(x) + banana", ("x",))
    with pytest.raises(ExpressionError, match="unknown name|undefined variable"):
        compile_expression("x + y", ("x",))


def test_overlong_expressions_are_rejected():
    with pytest.raises(ExpressionError, match="2000"):
        compile_expression("x+" * 2000 + "1", ("x",))


def test_attribute_access_is_rejected():
    with pytest.raises(ExpressionError):
        compile_expression("x.real", ("x",))


@pytest.mark.parametrize("expr,x,expected", [
    ("x**2", 3.0, 9.0),
    ("x^2", 3.0, 9.0),                      # ^ means power, as a human writes it
    ("sin(x)", 0.0, 0.0),
    ("2x", 4.0, 8.0),                       # implicit multiplication
    ("exp(-x^2/12)", 0.0, 1.0),
    ("sqrt(x)", 16.0, 4.0),
    ("abs(-x)", 5.0, 5.0),
    ("pi", 0.0, math.pi),
    ("log(e)", 0.0, 1.0),
])
def test_valid_expressions_evaluate(expr, x, expected):
    compiled = compile_expression(expr, ("x",))
    got = compiled(np.array([x]))
    assert float(np.asarray(got).ravel()[0]) == pytest.approx(expected, abs=1e-9)


def test_constant_expression_broadcasts():
    compiled = compile_expression("3", ("x",))
    out = compiled(np.linspace(0, 1, 7))
    assert out.shape == (7,) and np.allclose(out, 3.0)


def test_multivariable_expression():
    compiled = compile_expression("x*y", ("x", "y"))
    assert float(compiled(np.array([3.0]), np.array([4.0]))[0]) == 12.0


# ======================================================================
# plotting primitives
# ======================================================================

def test_circle_geometry():
    c = P.circle(100, 200, 50)
    assert c.closed
    r = np.linalg.norm(c.points - np.array([100, 200]), axis=1)
    assert np.allclose(r, 50.0, atol=0.25)


def test_circle_resolution_scales_with_radius():
    assert len(P.circle(0, 0, 5).points) < len(P.circle(0, 0, 400).points)


def test_arc_endpoints():
    a = P.arc(0, 0, 100, 0, 90)
    assert np.allclose(a.points[0], [100, 0], atol=0.1)
    assert np.allclose(a.points[-1], [0, 100], atol=0.1)
    assert not a.closed


def test_rectangle_and_rounded_rectangle():
    r = P.rectangle(10, 20, 100, 50)
    assert r.closed and len(r.points) == 4
    assert r.bounds == (10, 20, 110, 70)
    rr = P.rectangle(10, 20, 100, 50, corner_radius=10)
    assert rr.closed and len(rr.points) > 4
    assert rr.bounds == pytest.approx((10, 20, 110, 70), abs=0.3)


def test_bezier_passes_through_its_endpoints():
    ctrl = [[0, 0], [50, 100], [150, -50], [200, 0]]
    b = P.bezier(ctrl)
    assert np.allclose(b.points[0], ctrl[0])
    assert np.allclose(b.points[-1], ctrl[-1])


def test_bezier_degree_is_free():
    assert len(P.bezier([[0, 0], [100, 0]]).points) > 2       # linear
    assert len(P.bezier([[0, 0], [50, 90], [100, 0]]).points) > 2   # quadratic


def test_degenerate_shapes_are_rejected():
    with pytest.raises(PlotError):
        P.circle(0, 0, -5)
    with pytest.raises(PlotError):
        P.rectangle(0, 0, 0, 10)
    with pytest.raises(PlotError):
        P.polygon([[0, 0]])
    with pytest.raises(PlotError):
        P.bezier([[0, 0]])


# ----------------------------------------------------------------------
# function plotting
# ----------------------------------------------------------------------

def test_smooth_function_is_one_segment():
    paths, _, info = P.plot_function("sin(x)", x_min=-6, x_max=6,
                                     box=(100, 100, 800, 400))
    assert info["segments"] == 1
    assert len(paths) == 1


def test_tan_is_split_at_its_poles_not_drawn_through_them():
    """The behaviour that separates a plotter from a naive sampler."""
    paths, _, info = P.plot_function("tan(x)", x_min=-6.5, x_max=6.5,
                                     y_min=-4, y_max=4,
                                     box=(100, 100, 800, 400))
    assert info["segments"] >= 4, "tan(x) has 4 poles in this range"
    assert any("split" in n for n in info["notes"])
    # No path may span the full height, which is what a drawn-through pole is.
    for p in paths:
        span = p.points[:, 1].max() - p.points[:, 1].min()
        assert span < 400 * 0.99


def test_one_over_x_is_split_at_zero():
    paths, _, info = P.plot_function("1/x", x_min=-5, x_max=5, y_min=-10,
                                     y_max=10, box=(100, 100, 800, 400))
    assert info["segments"] == 2


def test_auto_y_range_ignores_the_poles():
    """A single pole must not squash the whole curve to one line."""
    _, _, info = P.plot_function("tan(x)", x_min=-6.5, x_max=6.5,
                                 box=(100, 100, 800, 400))
    lo, hi = info["y_range"]
    assert hi - lo < 200, f"auto y range {info['y_range']} was set by the poles"


def test_function_with_no_finite_values_is_rejected():
    with pytest.raises(PlotError, match="no finite values"):
        P.plot_function("sqrt(x)", x_min=-10, x_max=-1,
                        box=(100, 100, 800, 400))


def test_bad_range_is_rejected():
    with pytest.raises(PlotError):
        P.plot_function("x", x_min=5, x_max=5, box=(0, 0, 100, 100))


def test_parametric_circle_is_round():
    paths, window, _ = P.plot_parametric("cos(t)", "sin(t)", t_min=0,
                                         t_max=2 * math.pi,
                                         box=(100, 100, 400, 400))
    pts = paths[0].points
    centre = pts.mean(axis=0)
    r = np.linalg.norm(pts - centre, axis=1)
    assert r.std() / r.mean() < 0.02, "the parametric circle is not round"


def test_polar_rose_has_the_right_number_of_petals():
    paths, _, info = P.plot_polar("2*cos(3*theta)", theta_min=0,
                                  theta_max=2 * math.pi,
                                  box=(100, 100, 400, 400))
    assert paths
    pts = paths[0].points
    centre = np.array([info["max_r"], info["max_r"]])
    # Three petals means the radius returns to ~0 six times.
    r = np.linalg.norm(pts - pts.mean(axis=0), axis=1)
    crossings = int(np.sum((r[:-1] > r.max() * 0.5) != (r[1:] > r.max() * 0.5)))
    assert crossings >= 6


# ----------------------------------------------------------------------
# axes
# ----------------------------------------------------------------------

def test_axes_have_ticks_and_labels():
    window = PlotWindow.fit((-10, 10), (-5, 5), (100, 100, 800, 400))
    paths, info = P.draw_axes(window, x_range=(-10, 10), y_range=(-5, 5))
    assert info["x_ticks"] > 3 and info["y_ticks"] > 3
    assert len(paths) > info["x_ticks"] + info["y_ticks"]     # ticks + labels


def test_axes_tick_spacing_is_round():
    window = PlotWindow.fit((0, 97), (0, 3.3), (100, 100, 800, 400))
    _, info = P.draw_axes(window, x_range=(0, 97), y_range=(0, 3.3))
    for step in info["tick_spacing"]:
        mantissa = step / (10 ** math.floor(math.log10(step)))
        assert mantissa == pytest.approx(round(mantissa * 2) / 2, abs=1e-6)


def test_absurd_tick_spacing_is_rejected():
    window = PlotWindow.fit((0, 1000), (0, 1000), (100, 100, 800, 400))
    with pytest.raises(PlotError, match="tick spacing"):
        P.draw_axes(window, x_range=(0, 1000), y_range=(0, 1000),
                    tick_spacing=(0.5, 0.5))


def test_plot_window_maps_correctly():
    window = PlotWindow.fit((0, 10), (0, 5), (100, 200, 400, 200))
    assert window.to_board_point(0, 0) == pytest.approx((100, 200))
    assert window.to_board_point(10, 5) == pytest.approx((500, 400))


# ----------------------------------------------------------------------
# equations
# ----------------------------------------------------------------------

def test_equation_outline_renders():
    paths, info = P.write_equation(r"\frac{a}{b}", 100, 100, 40, mode="outline")
    assert paths and info["mode"] == "outline"


def test_equation_stroke_flattens_nested_fractions():
    _, info = P.write_equation(r"\frac{-b \pm \sqrt{b^2-4ac}}{2a}",
                               100, 100, 30, mode="stroke")
    assert info["rendered_text"] == "(-b ± √(b^2-4ac))/(2a)"


@pytest.mark.parametrize("latex,expected", [
    (r"\frac{1}{2}", "(1)/(2)"),
    (r"\frac{\frac{a}{b}}{c}", "((a)/(b))/(c)"),
    (r"\text{Area} = \pi r^2", "Area = π r^2"),
    (r"\sqrt{x^2 + y^2}", "√(x^2 + y^2)"),
])
def test_latex_to_plain_conversions(latex, expected):
    from wbbot.mcp.plotting import _latex_to_plain
    assert _latex_to_plain(latex) == expected


def test_equation_warns_when_outline_is_too_small():
    _, info = P.write_equation("x^2", 100, 100, 8, mode="outline")
    assert any("fill in" in n for n in info.get("notes", []))


def test_equation_skeleton_produces_open_strokes():
    paths, info = P.write_equation("ab", 100, 100, 60, mode="skeleton")
    assert paths and info["mode"] == "skeleton"
    assert all(not p.closed for p in paths)


# ======================================================================
# the service, against the simulator
# ======================================================================

async def test_board_info_describes_the_machine(service):
    result = await service.get_board_info()
    assert result["ok"]
    assert result["board_width_mm"] == 1500.0
    assert len(result["colors"]) == 3
    assert result["safe_area"]["coverage_percent"] > 90
    assert "lower-left" in result["origin"]
    json.dumps(result)


async def test_status_reports_position_and_state(service):
    result = await service.get_status()
    assert result["ok"] and result["state"] == "idle"
    assert "position" in result and result["pen_state"] in ("up", "down")


async def test_draw_circle_completes(service, simulator):
    result = await service.draw_circle(750, 500, 200, color=0)
    assert result["ok"], result.get("error")
    assert result["completed"]
    assert result["estimated_duration_s"] > 0
    bounds = simulator.drawn_bounds()
    assert bounds is not None
    assert bounds[0] == pytest.approx(550, abs=5)
    assert bounds[2] == pytest.approx(950, abs=5)


async def test_preview_only_draws_nothing(service, simulator):
    result = await service.draw_circle(750, 500, 200, preview_only=True)
    assert result["ok"] and result["preview_only"]
    assert result["preview_svg"].startswith("<svg")
    assert simulator.drawn == []


async def test_out_of_bounds_is_refused_with_a_reason(service, simulator):
    result = await service.draw_circle(750, 500, 5000)
    assert not result["ok"]
    assert "drawable area" in result["error"]
    assert result["violations"]
    assert result["safe_area_bounds"]
    assert simulator.drawn == []


async def test_unknown_colour_is_refused(service):
    result = await service.draw_circle(750, 500, 100, color=9)
    assert not result["ok"] and "colour 9" in result["error"]


async def test_write_text_reports_its_size(service, simulator):
    result = await service.write_text("HELLO", 300, 500, height_mm=60)
    assert result["ok"] and result["completed"]
    assert result["text_width_mm"] > 100
    assert result["text_height_mm"] == pytest.approx(60, abs=0.5)
    assert simulator.drawn


async def test_write_text_reports_unsupported_characters(service):
    result = await service.write_text("hi 你好", 300, 500,
                                      height_mm=40, preview_only=True)
    assert result["ok"]
    assert result["unsupported_characters"] == ["你", "好"]


async def test_plot_function_end_to_end(service, simulator):
    """Milestone 10: an AI client can plot a function."""
    result = await service.plot_function("sin(x)", x_min=-6.3, x_max=6.3)
    assert result["ok"], result.get("error")
    assert result["completed"]
    assert result["plot"]["segments"] == 1
    assert result["plot"]["axes"]["x_ticks"] > 3
    assert simulator.drawn


async def test_plot_function_rejects_a_dangerous_expression(service):
    result = await service.plot_function("__import__('os')")
    assert not result["ok"]
    assert "not allowed" in result["error"] or "unknown name" in result["error"]


async def test_plot_function_reports_pole_splitting(service):
    result = await service.plot_function("tan(x)", x_min=-6.5, x_max=6.5,
                                         y_min=-4, y_max=4, preview_only=True)
    assert result["ok"]
    assert result["plot"]["segments"] >= 4


async def test_plot_data_styles(service):
    points = [[i, math.sin(i)] for i in range(12)]
    for style in ("line", "scatter", "bar", "step"):
        result = await service.plot_data(points, style=style, preview_only=True)
        assert result["ok"], f"{style}: {result.get('error')}"
        assert result["n_paths"] > 0


async def test_write_equation_through_the_service(service, simulator):
    result = await service.write_equation(r"E = mc^2", 400, 500, height_mm=60)
    assert result["ok"], result.get("error")
    assert result["completed"] and simulator.drawn


async def test_move_pen_and_colour_primitives(service, simulator):
    assert (await service.move_to(600, 400))["ok"]
    assert (await service.pen_down())["ok"]
    assert (await service.pen_up())["ok"]
    result = await service.set_color(2)
    assert result["ok"] and result["color"] == 2


async def test_move_to_outside_is_refused(service):
    result = await service.move_to(5000, 400)
    assert not result["ok"] and "not reachable" in result["error"]


async def test_pen_down_outside_is_refused(service, simulator):
    simulator.position[0] = -400.0
    import asyncio
    await asyncio.sleep(0.15)
    result = await service.pen_down()
    assert not result["ok"]


async def test_busy_machine_refuses_new_work(service, machine, toolpath):
    await machine.run_toolpath(toolpath)
    result = await service.draw_circle(750, 500, 100, wait=False)
    assert not result["ok"] and "busy" in result["error"]
    await machine.cancel()


async def test_pause_resume_cancel_through_the_service(service, machine, toolpath):
    await machine.run_toolpath(toolpath)
    assert (await service.pause())["ok"]
    assert (await service.resume())["ok"]
    result = await service.cancel()
    assert result["ok"] and "resume_from_stroke" in result


async def test_emergency_stop_and_clear(service, machine, toolpath):
    await machine.run_toolpath(toolpath)
    result = await service.emergency_stop()
    assert result["ok"] and result["state"] == "estop"
    blocked = await service.draw_circle(750, 500, 100)
    assert not blocked["ok"] and "estop" in blocked["error"]
    assert (await service.clear_alarm())["ok"]


async def test_calibration_round_trip_through_the_service(service, cfg):
    from wbbot.calib.solve import simulate_measurements
    result = await service.run_calibration(n_x=3, n_y=3, preview_only=True)
    assert result["ok"]
    targets = result["pattern"]["targets"]
    assert len(targets) == 9
    assert result["instructions"]

    truth = dict(ax_l=-103.0, ay_l=1348.0, ax_r=1602.0, ay_r=1351.0,
                 k_left=1.003, k_right=0.998, c_left=2.0, c_right=-1.0,
                 dx=1.0, dy=-2.0)
    nominal = [(t["nominal_x"], t["nominal_y"]) for t in targets]
    measured = simulate_measurements(cfg, nominal, true_params=truth,
                                     model="tangent", noise_mm=0.3, seed=8)
    payload = [{"index": t["index"], "x": m.measured[0], "y": m.measured[1]}
               for t, m in zip(targets, measured)]
    solved = await service.submit_calibration(payload)
    assert solved["ok"], solved.get("error")
    assert solved["result"]["mean_error_mm"] < 2.0
    assert "mean deviation" in solved["report"]


async def test_camera_tools_fail_clearly_without_a_camera(service):
    result = await service.read_board()
    assert not result["ok"]
    assert "camera" in result["error"].lower()


async def test_draw_image_from_a_data_uri(service, reference_image):
    cv2 = pytest.importorskip("cv2")
    import base64
    ok_, buf = cv2.imencode(".png", reference_image)
    uri = "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode()
    result = await service.preview_image(uri, mode="contours")
    assert result["ok"], result.get("error")
    assert result["preview_svg"].startswith("<svg")
    assert result["stats"]["draw_length_mm"] > 0


async def test_remote_urls_are_refused_with_advice(service):
    result = await service.draw_image("https://example.com/cat.png")
    assert not result["ok"] and "remote URLs" in result["error"]


# ======================================================================
# tool surface
# ======================================================================

def test_every_declared_tool_exists_on_the_service(machine):
    service = PlotterService(machine)
    for name, description, properties, required in TOOL_SPECS:
        assert hasattr(service, name), f"tool {name} has no implementation"
        assert description and description[0].isupper()
        for key in required:
            assert key in properties, f"{name}: required {key} is not declared"


def test_tool_schemas_are_valid_json_schema_fragments():
    for name, _, properties, required in TOOL_SPECS:
        json.dumps({"type": "object", "properties": properties,
                    "required": required})
        for key, schema in properties.items():
            assert "type" in schema or "enum" in schema, f"{name}.{key}"


def test_tool_names_are_unique():
    names = [spec[0] for spec in TOOL_SPECS]
    assert len(names) == len(set(names))


def test_the_spec_required_tools_are_all_present():
    """Section 5.9 of the brief lists these by name."""
    names = {spec[0] for spec in TOOL_SPECS}
    required = {
        "get_status", "home", "run_calibration", "pause", "resume", "cancel",
        "emergency_stop", "get_board_info", "move_to", "draw_polyline",
        "draw_line", "set_color", "pen_up", "pen_down", "draw_circle",
        "draw_arc", "draw_rect", "draw_polygon", "draw_bezier", "write_text",
        "plot_function", "plot_parametric", "plot_polar", "draw_axes",
        "plot_data", "write_equation", "draw_image", "preview_image",
        "capture_board", "find_free_space", "read_board",
    }
    assert required <= names, f"missing tools: {sorted(required - names)}"


def test_server_instructions_state_the_coordinate_system():
    from wbbot.mcp.server import SERVER_INSTRUCTIONS
    assert "LOWER-LEFT" in SERVER_INSTRUCTIONS
    assert "millimetre" in SERVER_INSTRUCTIONS.lower()
    assert "REFUSED" in SERVER_INSTRUCTIONS


def test_mcp_server_builds_if_the_sdk_is_installed(machine):
    pytest.importorskip("mcp")
    from wbbot.mcp.server import build_server
    server = build_server(PlotterService(machine))
    assert server is not None


async def test_dispatch_runs_a_tool_and_returns_json(machine):
    from wbbot.mcp.server import dispatch
    service = PlotterService(machine)
    payload = await dispatch(service, "get_board_info", {})
    assert payload["ok"] and payload["board_width_mm"] == 1500.0
    json.dumps(payload, default=str)


async def test_dispatch_never_raises(machine):
    """A bad call must come back as an error payload, not kill the server."""
    from wbbot.mcp.server import dispatch
    service = PlotterService(machine)

    unknown = await dispatch(service, "make_coffee", {})
    assert not unknown["ok"] and "unknown tool" in unknown["error"]

    bad_args = await dispatch(service, "draw_circle", {"nonsense": 1})
    assert not bad_args["ok"] and "bad arguments" in bad_args["error"]

    refused = await dispatch(service, "draw_circle",
                             {"cx": 9e9, "cy": 0, "radius": 10})
    assert not refused["ok"]


async def test_built_server_lists_and_calls_tools(machine):
    """Exercise the real SDK wiring, not just that build_server returned."""
    types = pytest.importorskip("mcp.types")
    from wbbot.mcp.server import build_server

    server = build_server(PlotterService(machine))
    entry = server.get_request_handler("tools/list")
    if entry is None:                       # pragma: no cover - SDK 1.x shape
        pytest.skip("this MCP SDK does not expose request handlers directly")

    listed = await entry.handler(None, None)
    names = {t.name for t in listed.tools}
    assert "get_board_info" in names and "plot_function" in names
    assert len(names) == len(TOOL_SPECS)

    call = server.get_request_handler("tools/call").handler
    result = await call(None, types.CallToolRequestParams(
        name="get_board_info", arguments={}))
    payload = json.loads(result.content[0].text)
    assert payload["ok"] and payload["board_width_mm"] == 1500.0
    assert not result.is_error

    failed = await call(None, types.CallToolRequestParams(
        name="draw_circle", arguments={"cx": 750, "cy": 500, "radius": 99999}))
    assert failed.is_error
    assert not json.loads(failed.content[0].text)["ok"]


# ======================================================================
# text alignment
# ======================================================================

def test_text_alignment_shifts_the_whole_block_against_the_anchor():
    """`align` must move the block, not merely align lines inside it.

    Aligning only within the block makes `align` a silent no-op for
    single-line text, because one line is always as wide as its own block —
    which is what every axis label is.

    The comparison is between alignments rather than against absolute ink
    extents, because alignment works on the advance box: a glyph's side
    bearing legitimately leaves a few millimetres of air at the edge.
    """
    from wbbot.core.hershey import default_font
    font = default_font()
    width, _ = font.measure("ABC", 30.0)

    def ink_left(align: str) -> float:
        paths = font.render("ABC", 100.0, 0.0, 30.0, align=align)
        return float(np.vstack([p.points for p in paths])[:, 0].min())

    left, centre, right = ink_left("left"), ink_left("center"), ink_left("right")
    assert centre - left == pytest.approx(-width / 2, abs=0.01)
    assert right - left == pytest.approx(-width, abs=0.01)


def test_multiline_text_centres_its_lines_on_each_other():
    from wbbot.core.hershey import default_font
    font = default_font()
    paths = font.render("AB\nCDEF", 100.0, 0.0, 30.0, align="center")
    ys = np.concatenate([p.points[:, 1] for p in paths])
    split = (ys.min() + ys.max()) / 2

    def centre_of(upper: bool) -> float:
        xs = np.concatenate([
            p.points[:, 0] for p in paths
            if (p.points[:, 1].mean() > split) == upper])
        return (float(xs.min()) + float(xs.max())) / 2

    assert abs(centre_of(True) - centre_of(False)) < 6.0, \
        "the two lines are not centred on each other"


def test_axis_labels_default_to_a_readable_size():
    """A 6 mm digit drawn with a 2 mm marker is a smudge."""
    from wbbot.mcp.plotting import MIN_READABLE_TEXT_MM
    window = PlotWindow.fit((-10, 10), (-1, 1), (100, 100, 800, 400))
    _, info = P.draw_axes(window, x_range=(-10, 10), y_range=(-1, 1))
    assert info["label_height_mm"] >= MIN_READABLE_TEXT_MM
    assert not info["notes"]


def test_small_axis_labels_are_flagged():
    window = PlotWindow.fit((-10, 10), (-1, 1), (100, 100, 800, 400))
    _, info = P.draw_axes(window, x_range=(-10, 10), y_range=(-1, 1),
                          label_height_mm=5.0)
    assert any("fills them in" in n for n in info["notes"])
