"""Result/state/event types of :class:`wbot.fluidnc.client.FluidNCClient` (re-exported there)."""
from __future__ import annotations

from dataclasses import dataclass


class ClientError(RuntimeError):
    """Misuse or unrecoverable condition in the client (not a controller error)."""


class StreamActiveError(ClientError):
    """``send()``/``query()``/second ``stream()`` while a stream is running."""


class CommandFailed(ClientError):
    """A ``query()`` did not finish with ``ok``; ``.reply`` has the details."""

    def __init__(self, reply: "Reply"):
        super().__init__(f"{reply.line!r}: {reply.status}" + (f" ({reply.text})" if reply.text else ""))
        self.reply = reply


@dataclass(frozen=True)
class Reply:
    """Outcome of one sent line.

    ``status``: ``"ok"`` | ``"error"`` (controller said ``error:N``) | ``"timeout"`` |
    ``"lost"`` (link gone) | ``"reset"`` (controller restarted) | ``"estop"`` | ``"closed"``.
    """
    ok: bool
    status: str
    line: str = ""
    code: int | None = None            # N of error:N
    text: str = ""                     # error text / failure reason
    lines: tuple[str, ...] = ()        # payload lines received before the ok (queries)

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class LineError:
    index: int
    line: str
    code: int
    text: str


@dataclass(frozen=True)
class StreamResult:
    """Outcome of ``stream()``.

    ``status``: ``"done"`` | ``"error"`` (a line was rejected) | ``"cancelled"`` |
    ``"alarm"`` | ``"lost"`` | ``"reset"`` | ``"estop"`` | ``"timeout"`` (no ack for too long).
    ``acked`` counts lines the controller has acknowledged (accepted into its planner; after
    ``cancelled`` the planner was flushed, so lines beyond what already ran never execute).
    """
    status: str
    total: int
    sent: int
    acked: int
    errors: tuple[LineError, ...] = ()
    message: str = ""
    final_state: str = ""              # machine state at the end ("Idle", "Alarm", ...)

    @property
    def ok(self) -> bool:
        return self.status == "done"


@dataclass(frozen=True)
class BootInfo:
    firmware: str | None               # "FluidNC v3.9.9"
    banner: str | None
    machine_state: str
    alarm: tuple[int, str] | None
    mpos: tuple[float, ...] | None
    ver_line: str | None = None        # raw "$I" VER payload
    boot_lines: tuple[str, ...] = ()   # non-status lines received since open (boot log)


@dataclass(frozen=True)
class ClientState:
    link: str                          # "closed" | "open" | "lost"
    machine_state: str                 # "Unknown" until the first status report
    substate: int | None
    mpos: tuple[float, ...] | None
    wpos: tuple[float, ...] | None
    wco: tuple[float, ...] | None
    feed: float | None
    alarm: tuple[int, str] | None
    last_error: str | None
    last_status_age_s: float | None    # None: no status received yet
    planner_free: int | None
    rx_free: int | None
    firmware: str | None
    resetting: bool = False            # soft reset sent, banner/fresh status not yet seen
    streaming: bool = False
    in_flight_bytes: int = 0
    link_error: str | None = None


# --- listener events (besides the protocol events Status/Message/Alarm/Banner) --------
@dataclass(frozen=True)
class LinkEvent:
    state: str                         # "open" | "lost" | "closed"
    reason: str = ""


@dataclass(frozen=True)
class Traffic:
    direction: str                     # "tx" | "rx"   (status polling excluded)
    text: str
