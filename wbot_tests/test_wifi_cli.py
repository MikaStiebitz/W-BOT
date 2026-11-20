"""CLI: wifi-commands only prints, probe needs exactly one target, sim-server arguments."""
from __future__ import annotations

import pytest

from wbot.cli import main
from wbot.wifi_setup import wifi_commands


def test_wifi_commands_prints_placeholder_never_a_password(capsys):
    assert main(["wifi-commands", "--ssid", "Heimnetz", "--hostname", "wbot"]) == 0
    out = capsys.readouterr().out
    assert "$Sta/SSID=Heimnetz" in out and "$Sta/Password=<YOUR-WIFI-PASSWORD>" in out
    assert "wbot.local" in out and "UNVERIFIED" in out and "NO authentication" in out
    assert "$S" in out and "$Telnet/Enable" in out


@pytest.mark.parametrize("ssid,host", [("", "wbot"), ("a\nb", "wbot"), ("x" * 33, "wbot"),
                                       ("ok", "bad host"), ("ok", "a.b"), ("ok", "-x")])
def test_wifi_commands_rejects_bad_input(ssid, host):
    with pytest.raises(ValueError):
        wifi_commands(ssid, host)


def test_probe_needs_exactly_one_target(capsys):
    assert main(["probe"]) == 2
    assert main(["probe", "--port", "COM9", "--board-host", "x.local"]) == 2
    assert "exactly one of" in capsys.readouterr().err
