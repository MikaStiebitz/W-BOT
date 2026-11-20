"""Shared fixtures. Everything runs against the simulator — no hardware."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wbbot.config import MachineConfig, load_config          # noqa: E402
from wbbot.core.geometry import Drawing, Layer, Polyline     # noqa: E402
from wbbot.core.kinematics import (NaiveKinematics,          # noqa: E402
                                   TangentKinematics)
from wbbot.core.safearea import SafeArea                     # noqa: E402
from wbbot.core.toolpath import Toolpath                     # noqa: E402
from wbbot.driver.machine import Machine                     # noqa: E402
from wbbot.driver.simulator import FluidNCSimulator          # noqa: E402
from wbbot.driver.transport import SimulatorTransport        # noqa: E402


@pytest.fixture(scope="session")
def machine_yaml() -> Path:
    return ROOT / "config" / "machine.yaml"


@pytest.fixture
def cfg(machine_yaml) -> MachineConfig:
    """Pristine machine config, ignoring any local calibration.yaml."""
    return load_config(machine_yaml, calibration_path=ROOT / "does-not-exist.yaml")


@pytest.fixture
def naive(cfg) -> NaiveKinematics:
    return NaiveKinematics.from_config(cfg)


@pytest.fixture
def tangent(cfg) -> TangentKinematics:
    return TangentKinematics.from_config(cfg)


@pytest.fixture
def safe_area(cfg) -> SafeArea:
    return SafeArea(cfg)


@pytest.fixture
def square() -> Polyline:
    return Polyline.from_points(
        [[300, 300], [700, 300], [700, 700], [300, 700]], closed=True)


@pytest.fixture
def triangle() -> Polyline:
    return Polyline.from_points([[900, 300], [1300, 300], [1100, 700]], closed=True)


@pytest.fixture
def three_color_drawing(square, triangle) -> Drawing:
    line = Polyline.from_points([[200, 850], [1300, 850]])
    return Drawing([Layer(0, [square]), Layer(1, [triangle]), Layer(2, [line])])


@pytest.fixture
def toolpath(three_color_drawing) -> Toolpath:
    return Toolpath.from_drawing(three_color_drawing)


@pytest.fixture
def simulator(cfg) -> FluidNCSimulator:
    """Fast virtual controller: 300x real time keeps the suite quick."""
    return FluidNCSimulator(cfg, time_scale=300.0, tick_hz=400)


@pytest.fixture(scope="session")
def reference_image():
    """Deterministic synthetic 'photo': flat colours, a gradient and noise.

    Generated rather than committed so the imaging regression numbers cannot
    drift with an image file nobody looks at.
    """
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    img = np.full((600, 900, 3), 245, np.uint8)
    cv2.rectangle(img, (60, 60), (380, 300), (40, 40, 40), -1)       # dark block
    cv2.circle(img, (620, 180), 110, (40, 40, 200), -1)              # red disc
    tri = np.array([[500, 560], [820, 560], [660, 340]], np.int32)
    cv2.fillPoly(img, [tri], (190, 90, 20))                          # blue tri
    for x in range(900):                                             # gradient
        img[400:520, x] = np.clip(
            img[400:520, x].astype(int) - int(120 * x / 900), 0, 255)
    rng = np.random.default_rng(0)
    return np.clip(img.astype(int) + rng.normal(0, 6, img.shape),
                   0, 255).astype(np.uint8)


@pytest.fixture(scope="session")
def line_art_image():
    """Thick hand-drawn-looking strokes: the centreline tracer's use case."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    img = np.full((400, 600, 3), 255, np.uint8)
    cv2.line(img, (60, 200), (540, 200), (0, 0, 0), 9)
    cv2.line(img, (300, 60), (300, 340), (0, 0, 0), 9)
    cv2.circle(img, (150, 300), 60, (0, 0, 0), 9)
    return img


@pytest.fixture
async def machine(cfg, simulator):
    m = Machine(cfg, transport=SimulatorTransport(simulator), simulator=simulator)
    await m.connect()
    try:
        yield m
    finally:
        try:
            await m.disconnect()
        except Exception:
            pass
