"""Errors raised by the machine layer (carry a German message, a slug and an HTTP status)."""
from __future__ import annotations


class MachineError(Exception):
    def __init__(self, message: str, code: str = "error", status: int = 409):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status
