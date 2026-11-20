"""The pre-2026-11 machine layout, for tests that deliberately need it (regression coverage).

Default geometry today: anchors (47, 1041) / (1453, 1041) and per-side margins (safe rect 120,10,1380,900).
Legacy: anchors (-100, 1350) / (1600, 1350) well above the board and uniform 10 mm margins.
"""
from __future__ import annotations

from wbot.config import Config, deep_update, load_config

LEGACY_PATCH = {
    "geometry": {"anchor_left": [-100.0, 1350.0], "anchor_right": [1600.0, 1350.0]},
    "board": {"margin_mm": 10.0, "margin_left_mm": 10.0, "margin_right_mm": 10.0,
              "margin_bottom_mm": 10.0, "margin_top_mm": 10.0},
}


def legacy_cfg(base: Config | None = None) -> Config:
    """``base`` (default: the shipped config) with the old anchors and 10 mm margins."""
    return deep_update(base or load_config(), LEGACY_PATCH)
