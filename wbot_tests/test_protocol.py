"""Pure parsing tests for wbot.fluidnc.protocol."""
from __future__ import annotations

import math

import pytest

from wbot.fluidnc import protocol as p


def test_status_idle_basic():
    st = p.parse_status("<Idle|MPos:0.000,1.500,-2.000,90.000|FS:0,0>")
    assert st is not None
    assert st.state == "Idle" and st.substate is None
    assert st.mpos == (0.0, 1.5, -2.0, 90.0)
    assert st.feed == 0.0 and st.spindle == 0.0
    assert st.wpos is None and st.wco is None
    assert st.planner_free is None and st.rx_free is None


def test_status_three_axes_only():
    st = p.parse_status("<Run|MPos:10.000,20.000,0.000|FS:1500,0>")
    assert st.state == "Run" and st.mpos == (10.0, 20.0, 0.0) and st.feed == 1500.0


@pytest.mark.parametrize("line,state,sub", [
    ("<Hold:0|MPos:0,0,0|FS:0,0>", "Hold", 0),
    ("<Hold:1|MPos:0,0,0|FS:0,0>", "Hold", 1),
    ("<Door:0|MPos:0,0,0>", "Door", 0),
    ("<Alarm|MPos:0,0,0>", "Alarm", None),
    ("<Jog|MPos:5,5,0|FS:3000,0>", "Jog", None),
    ("<Check|MPos:0,0,0>", "Check", None),
    ("<Home|MPos:0,0,0>", "Home", None),
    ("<Sleep|MPos:0,0,0>", "Sleep", None),
])
def test_status_states(line, state, sub):
    st = p.parse_status(line)
    assert (st.state, st.substate) == (state, sub)


def test_status_optional_fields():
    st = p.parse_status("<Run|WPos:1.0,2.0|WCO:10,20|Bf:15,127|FS:500,0|Ov:100,100,100|Pn:XYZ>")
    assert st.wpos == (1.0, 2.0) and st.wco == (10.0, 20.0)
    assert st.planner_free == 15 and st.rx_free == 127
    assert st.ov == (100.0, 100.0, 100.0) and st.pins == "XYZ"
    assert st.mpos is None


def test_status_rejects_non_status():
    for bad in ("ok", "", "<>", "<Idle", "Idle>", "error:3", "[MSG:INFO: x]"):
        assert p.parse_status(bad) is None


def test_status_tolerates_garbage_numbers():
    st = p.parse_status("<Idle|MPos:1.0,abc,3.0|FS:x>")
    assert st.mpos[0] == 1.0 and math.isnan(st.mpos[1]) and st.mpos[2] == 3.0


def test_parse_line_ok_error_alarm():
    assert isinstance(p.parse_line("ok"), p.Ok)
    assert isinstance(p.parse_line("ok\r"), p.Ok)
    e = p.parse_line("error:20")
    assert isinstance(e, p.Error) and e.code == 20 and e.text == p.ERROR_TEXT[20]
    a = p.parse_line("ALARM:3")
    assert isinstance(a, p.Alarm) and a.code == 3 and a.text == p.ALARM_TEXT[3]
    unknown = p.parse_line("error:999")
    assert isinstance(unknown, p.Error) and "999" in unknown.text


def test_parse_line_messages():
    m = p.parse_line("[MSG:INFO: Kinematic system: WallPlotter]")
    assert isinstance(m, p.Message) and m.kind == "INFO" and m.text == "Kinematic system: WallPlotter"
    m = p.parse_line("[MSG:ERR: Config error]")
    assert m.kind == "ERR" and m.text == "Config error"
    m = p.parse_line("[MSG:Reset to continue]")
    assert isinstance(m, p.Message) and m.kind == "" and "Reset to continue" in m.text


def test_parse_line_banner_and_version():
    b = p.parse_line("Grbl 3.9 [FluidNC v3.9.9 (wifi) '$' for help]")
    assert isinstance(b, p.Banner) and b.version == "FluidNC v3.9.9"
    assert p.parse_ver_line("[VER:3.9 FluidNC v3.9.9:]") == "3.9 FluidNC v3.9.9"
    assert p.parse_ver_line("[OPT:PH,35,128]") is None


def test_parse_line_reply_status_empty():
    r = p.parse_line("[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]")
    assert isinstance(r, p.Reply) and r.text.startswith("[GC:")
    assert isinstance(p.parse_line("<Idle|MPos:0,0,0>"), p.Status)
    assert p.parse_line("") is None and p.parse_line("\r") is None


def test_tables_cover_standard_codes():
    for code in list(range(1, 39)):
        if code != 18 and code != 19:
            assert code in p.ERROR_TEXT, code
    for code in range(1, 12):
        assert code in p.ALARM_TEXT
    assert p.error_text(12345).startswith("unknown")
