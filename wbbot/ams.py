"""AMS-style marker magazine: which colour sits in which revolver slot.

``machine.yaml`` carries build-time defaults; this module owns the *runtime*
truth — the markers actually loaded right now.  It persists to
``config/ams.yaml`` so a marker swap survives a restart, and everything
downstream reads the slot colours from here rather than from the machine
config: colour separation targets these colours, previews render with them,
the UI shows them, and the MCP board info reports them.

A slot can be disabled ("empty bay"): the pipeline then separates onto the
remaining markers only, and selecting the slot is refused.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import MachineConfig, default_config_dir, to_plain

log = logging.getLogger(__name__)

__all__ = ["AmsError", "AmsSlot", "Ams"]

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class AmsError(ValueError):
    """Invalid slot operation."""


@dataclass
class AmsSlot:
    """One revolver bay and the marker loaded in it."""

    index: int
    name: str
    hex: str
    enabled: bool = True

    @property
    def rgb(self) -> tuple[int, int, int]:
        h = self.hex.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    def to_dict(self) -> dict:
        return {"index": self.index, "name": self.name, "hex": self.hex,
                "enabled": self.enabled, "rgb": list(self.rgb)}


class Ams:
    """The magazine state: load, edit, persist."""

    def __init__(self, cfg: MachineConfig, path: str | Path | None = None):
        self.cfg = cfg
        self.path = Path(path) if path else default_config_dir() / "ams.yaml"
        self.slots: list[AmsSlot] = [
            AmsSlot(index=i, name=c.name, hex=c.hex, enabled=True)
            for i, c in enumerate(cfg.colors)
        ]
        self._load()

    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except Exception:
            log.warning("could not read %s; using machine defaults", self.path,
                        exc_info=True)
            return
        for entry in data.get("slots", []) or []:
            if not isinstance(entry, dict):
                continue
            try:
                index = int(entry.get("index", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= index < len(self.slots):
                continue
            slot = self.slots[index]
            name = entry.get("name")
            hex_value = entry.get("hex")
            if isinstance(name, str) and name.strip():
                slot.name = name.strip()[:40]
            if isinstance(hex_value, str) and _HEX_RE.match(hex_value.strip()):
                slot.hex = hex_value.strip().lower()
            if isinstance(entry.get("enabled"), bool):
                slot.enabled = entry["enabled"]
        # Never allow a state where every bay is disabled.
        if not any(s.enabled for s in self.slots) and self.slots:
            self.slots[0].enabled = True

    def save(self) -> Path:
        payload = {"slots": [{"index": s.index, "name": s.name,
                              "hex": s.hex, "enabled": s.enabled}
                             for s in self.slots]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(to_plain(payload), sort_keys=False,
                                      allow_unicode=True), encoding="utf-8")
        os.replace(tmp, self.path)
        return self.path

    # ------------------------------------------------------------------

    def slot(self, index: int) -> AmsSlot:
        if not 0 <= index < len(self.slots):
            raise AmsError(
                f"slot {index} does not exist (0..{len(self.slots) - 1})")
        return self.slots[index]

    def set_slot(self, index: int, *, name: str | None = None,
                 hex: str | None = None,
                 enabled: bool | None = None) -> AmsSlot:
        slot = self.slot(index)
        if name is not None:
            name = name.strip()
            if not name:
                raise AmsError("the slot name must not be empty")
            slot.name = name[:40]
        if hex is not None:
            hex = hex.strip()
            if not _HEX_RE.match(hex):
                raise AmsError(f"{hex!r} is not a colour like #rrggbb")
            slot.hex = hex.lower()
        if enabled is not None:
            if not enabled and sum(s.enabled for s in self.slots) <= 1 \
                    and slot.enabled:
                raise AmsError(
                    "the last enabled slot cannot be disabled — the plotter "
                    "needs at least one marker")
            slot.enabled = bool(enabled)
        self.save()
        return slot

    # ------------------------------------------------------------------

    def enabled_slots(self) -> list[AmsSlot]:
        return [s for s in self.slots if s.enabled]

    def require_enabled(self, index: int) -> AmsSlot:
        slot = self.slot(index)
        if not slot.enabled:
            raise AmsError(
                f"slot {index + 1} ({slot.name}) is disabled — enable it in "
                f"the marker magazine first")
        return slot

    def hex_list(self) -> list[str]:
        """Hex colour per slot, indexed by revolver slot (disabled included)."""
        return [s.hex for s in self.slots]

    def marker_colors(self) -> list[tuple[int, int, int]]:
        """RGB triples of the *enabled* slots, for colour separation."""
        return [s.rgb for s in self.enabled_slots()]

    def slot_indices(self) -> list[int]:
        """Revolver indices of the enabled slots, aligned with marker_colors."""
        return [s.index for s in self.enabled_slots()]

    def to_dict(self) -> dict:
        return {"slots": [s.to_dict() for s in self.slots],
                "enabled": self.slot_indices()}
