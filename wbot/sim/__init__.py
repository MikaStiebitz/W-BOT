"""W-BOT simulator: FluidNC emulator (naive firmware model) + physical plant (true belt geometry)."""
from __future__ import annotations

from ..config import Config
from .emulator import FluidNCEmulator
from .plant import Horn, Plant, TraceSeg, build_truth


def make_sim_link(cfg: Config, **kw) -> FluidNCEmulator:
    """New (unopened) emulator link. Keyword arguments: see ``FluidNCEmulator``."""
    return FluidNCEmulator(cfg, **kw)


__all__ = ["FluidNCEmulator", "Plant", "Horn", "TraceSeg", "build_truth", "make_sim_link"]
