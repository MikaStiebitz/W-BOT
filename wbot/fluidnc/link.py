"""Raw byte channel to a FluidNC controller (serial port or in-process emulator)."""
from __future__ import annotations

from abc import ABC, abstractmethod


class LinkError(IOError):
    """The channel is closed or was lost."""


class Link(ABC):
    name: str = "link"

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Thread-safe. Raises LinkError when the channel is gone."""

    @abstractmethod
    def read(self, timeout: float = 0.05) -> bytes:
        """Return available bytes, ``b""`` after ``timeout`` seconds without data.
        Raises LinkError when the channel is closed or lost."""

    @property
    @abstractmethod
    def is_open(self) -> bool: ...
