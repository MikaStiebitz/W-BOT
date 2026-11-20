"""XMODEM(-CRC) sender over a :class:`wbot.fluidnc.link.Link`, to upload ``config.yaml``.

FluidNC switches the port to XMODEM after the line ``$Xmodem/Receive=<file>``; from then on
the wire carries raw binary packets, not lines.

.. warning::
   :func:`upload_config` reads and writes the link directly.  It MUST be run while **no**
   ``FluidNCClient`` reader thread (or any other consumer) is reading the same link -
   otherwise the reader eats the receiver's ``C``/ACK/NAK bytes and the transfer stalls
   or corrupts.  Stop/close the client first (or use a link nobody else reads), and
   reboot the controller afterwards (``$Bye`` / soft reset) so it loads the new file.

Protocol (128-byte blocks)::

    sender                          receiver
                                    'C'            (CRC mode requested; NAK = checksum mode)
    SOH blk ~blk data[128] crc16 ->
                                    ACK | NAK (resend) | CAN (abort)
    ...
    EOT                          ->
                                    ACK
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

from .fluidnc.link import Link, LinkError

log = logging.getLogger(__name__)

SOH, EOT, ACK, NAK, CAN, SUB = 0x01, 0x04, 0x06, 0x15, 0x18, 0x1A
CRC_REQUEST = ord("C")
BLOCK_SIZE = 128
_FILENAME_OK = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")


class XmodemError(IOError):
    """Upload failed (timeout, too many retries, refused)."""


class XmodemCancelled(XmodemError):
    """The receiver aborted the transfer (CAN)."""


def crc16_xmodem(data: bytes) -> int:
    """CRC-16/XMODEM (poly 0x1021, init 0)."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def build_packet(block: int, chunk: bytes, *, crc_mode: bool = True) -> bytes:
    """One 128-byte packet; ``chunk`` is padded with 0x1A (SUB)."""
    if not 0 < len(chunk) <= BLOCK_SIZE:
        raise ValueError("chunk must be 1..128 bytes")
    data = chunk.ljust(BLOCK_SIZE, bytes([SUB]))
    blk = block & 0xFF
    tail = crc16_xmodem(data).to_bytes(2, "big") if crc_mode else bytes([sum(data) & 0xFF])
    return bytes([SOH, blk, 255 - blk]) + data + tail


def split_blocks(payload: bytes) -> list[bytes]:
    return [payload[i:i + BLOCK_SIZE] for i in range(0, len(payload), BLOCK_SIZE)]


class _Session:
    def __init__(self, link: Link, deadline: float, ack_timeout: float) -> None:
        self.link, self.deadline, self.ack_timeout = link, deadline, ack_timeout
        self.text = bytearray()          # everything that was not protocol (lines of text)

    def check_deadline(self) -> None:
        if time.monotonic() > self.deadline:
            self.abort()
            raise XmodemError("XMODEM upload timed out")

    def abort(self) -> None:
        try:
            self.link.write(bytes([CAN, CAN, CAN]))
        except LinkError:
            pass

    def wait_start(self, start_timeout: float) -> bool:
        """Wait for the receiver's ``C`` (returns True = CRC mode) or NAK (False = checksum)."""
        end = min(time.monotonic() + start_timeout, self.deadline)
        pending = b""
        while time.monotonic() < end:
            chunk = self.link.read(min(0.1, max(end - time.monotonic(), 0.001)))
            if not chunk:
                continue
            pending += chunk
            # complete text lines (echo, [MSG:...]) are kept; a *bare* 'C' or NAK at the
            # end of the buffer (nothing else on its line) is the receiver's start request.
            head, sep, tail = pending.rpartition(b"\n")
            if sep:
                self.text += head + sep
                pending = tail
            stripped = pending.lstrip(b"\r")
            if stripped and set(stripped) == {CRC_REQUEST}:
                return True
            if stripped and set(stripped) == {NAK}:
                return False
        raise XmodemError("The controller does not start XMODEM (no 'C' received). "
                          "Is a reader still running on the port? Firmware too old?")

    def read_reply(self, first_block: bool) -> int | None:
        """Next ACK/NAK/CAN within ``ack_timeout`` (None = timeout). Other bytes are skipped."""
        end = time.monotonic() + self.ack_timeout
        while time.monotonic() < end:
            chunk = self.link.read(min(0.05, max(end - time.monotonic(), 0.001)))
            for i, b in enumerate(chunk):
                if b in (ACK, NAK, CAN):
                    self.text += chunk[i + 1:]      # e.g. "ok" glued to the EOT ACK
                    return b
                if b != CRC_REQUEST or not first_block:
                    self.text.append(b)             # stray chatter, kept for the caller
                # 'C' during the first block = repeated start request: ignore
        return None


def _send_with_retries(s: _Session, packet: bytes, what: str, *, first_block: bool,
                       max_retries: int) -> None:
    for attempt in range(1, max_retries + 1):
        s.check_deadline()
        s.link.write(packet)
        reply = s.read_reply(first_block)
        if reply == ACK:
            return
        if reply == CAN:
            raise XmodemCancelled(f"The controller aborted the upload ({what})")
        log.debug("%s: %s, attempt %d/%d", what, "NAK" if reply == NAK else "timeout",
                  attempt, max_retries)
    s.abort()
    raise XmodemError(f"{what}: {max_retries} attempts without ACK - upload aborted")


def _collect_response(s: _Session, response_timeout: float) -> str:
    """Read the text FluidNC prints after the transfer until ``ok``/``error:`` or quiet."""
    end = time.monotonic() + response_timeout
    buf = bytearray(s.text)
    s.text.clear()
    while time.monotonic() < end:
        buf += s.link.read(0.1)
        lines = buf.decode("utf-8", "replace").replace("\r", "").split("\n")
        if any(ln.strip() == "ok" or ln.strip().startswith("error:") for ln in lines):
            break
    text = buf.decode("utf-8", "replace").replace("\r", "")
    if re.search(r"^error:\d+", text, re.MULTILINE):
        raise XmodemError(f"The controller reports an error after the transfer: {text.strip()}")
    if not re.search(r"^ok\s*$", text, re.MULTILINE):
        log.warning("no 'ok' received after the XMODEM transfer (response: %r)", text.strip())
    return text


def upload_config(link: Link, text: str, *, filename: str = "config.yaml",
                  progress: Callable[[int, int], None] | None = None,
                  timeout: float = 120.0, start_timeout: float = 10.0,
                  ack_timeout: float = 5.0, response_timeout: float = 3.0,
                  max_retries: int = 10) -> str:
    """Upload ``text`` as ``filename`` to the FluidNC local filesystem.

    MUST run while no ``FluidNCClient`` reader is consuming ``link`` (see module docstring).

    ``progress(blocks_acked, blocks_total)`` is called after every acknowledged block.
    ``timeout`` is the overall limit in seconds.  Returns the text the controller printed
    (``ok``, ``[MSG:...]``); raises :class:`XmodemError` / :class:`XmodemCancelled` on failure.
    """
    if not _FILENAME_OK.match(filename):
        raise ValueError(f"invalid file name: {filename!r}")
    payload = text.encode("utf-8")
    if not payload:
        raise ValueError("an empty configuration will not be uploaded")
    blocks = split_blocks(payload)
    s = _Session(link, time.monotonic() + timeout, ack_timeout)

    # drop stale bytes (boot banner, status reports) so they cannot be mistaken for protocol
    drain_end = time.monotonic() + 1.0
    while link.read(0.05) and time.monotonic() < drain_end:
        pass
    link.write(f"$Xmodem/Receive={filename}\n".encode("ascii"))
    crc_mode = s.wait_start(start_timeout)
    log.info("XMODEM %s: %d bytes in %d blocks", "CRC" if crc_mode else "checksum",
             len(payload), len(blocks))

    for i, chunk in enumerate(blocks):
        _send_with_retries(s, build_packet(i + 1, chunk, crc_mode=crc_mode),
                           f"Block {i + 1}/{len(blocks)}", first_block=(i == 0),
                           max_retries=max_retries)
        if progress:
            progress(i + 1, len(blocks))

    _send_with_retries(s, bytes([EOT]), "EOT", first_block=False, max_retries=max_retries)
    return _collect_response(s, response_timeout)


__all__ = ["upload_config", "crc16_xmodem", "build_packet", "split_blocks", "XmodemError",
           "XmodemCancelled"]
