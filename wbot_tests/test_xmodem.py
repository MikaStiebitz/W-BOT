"""XMODEM sender tests against an in-memory XMODEM-CRC receiver (never a real port)."""
from __future__ import annotations

import threading
import time

import pytest

from wbot.fluidnc.link import Link, LinkError
from wbot.xmodem_upload import (ACK, CAN, EOT, NAK, SOH, SUB, XmodemCancelled, XmodemError,
                                build_packet, crc16_xmodem, upload_config)

FAST = dict(start_timeout=0.5, ack_timeout=0.05, response_timeout=0.2, timeout=10.0)


class FakeReceiver(Link):
    """In-memory FluidNC-like receiver: a line parser that switches to XMODEM-CRC."""
    name = "fake-xmodem"

    def __init__(self, *, mode: str = "crc", corrupt_packets: int = 0, drop_acks: int = 0,
                 cancel_at_block: int | None = None, chatter: bytes = b"", start: bool = True,
                 nak_first_eot: bool = False, final: bytes = b"ok\r\n", stale: bytes = b"",
                 start_after_echo: bool = True) -> None:
        self._open = True
        self._lock = threading.Lock()
        self.out = bytearray(stale)             # bytes waiting for the sender to read
        self.mode, self.corrupt, self.drop_acks = mode, corrupt_packets, drop_acks
        self.cancel_at, self.chatter, self.start = cancel_at_block, chatter, start
        self.nak_first_eot, self.final = nak_first_eot, final
        self.line = bytearray()
        self.state = "line"                     # line -> xfer
        self.buf = bytearray()
        self.expect = 1
        self.received = bytearray()
        self.filename: str | None = None
        self.packets_seen = 0
        self.naks_sent = 0
        self.eots = 0
        self.writes: list[bytes] = []

    # --- Link ---
    def open(self) -> None: self._open = True
    def close(self) -> None: self._open = False
    @property
    def is_open(self) -> bool: return self._open

    def read(self, timeout: float = 0.05) -> bytes:
        if not self._open:
            raise LinkError("closed")
        end = time.monotonic() + timeout
        while True:
            with self._lock:
                if self.out:
                    data, self.out = bytes(self.out), bytearray()
                    return data
            if time.monotonic() >= end:
                return b""
            time.sleep(0.001)

    def write(self, data: bytes) -> None:
        if not self._open:
            raise LinkError("closed")
        self.writes.append(data)
        with self._lock:
            for b in data:
                self._feed(b)

    # --- receiver state machine ---
    def _reply(self, b: bytes) -> None:
        self.out += b

    def _feed(self, b: int) -> None:
        if self.state == "line":
            if b == 0x0A:
                cmd = self.line.decode().strip()
                self.line.clear()
                if cmd.startswith("$Xmodem/Receive="):
                    self.filename = cmd.split("=", 1)[1]
                    self.state = "xfer"
                    self._reply(self.chatter)
                    if self.start:
                        self._reply(b"C" if self.mode == "crc" else bytes([NAK]))
                else:
                    self._reply(b"error:3\r\n")
            else:
                self.line.append(b)
            return
        # xfer
        self.buf.append(b)
        if self.buf[0] == EOT:
            self.buf.clear()
            self.eots += 1
            if self.nak_first_eot and self.eots == 1:
                self._reply(bytes([NAK]))
            else:
                self._reply(bytes([ACK]) + self.final)
                self.state = "done"
            return
        if self.buf[0] == CAN:
            self.buf.clear()
            return
        need = 3 + 128 + (2 if self.mode == "crc" else 1)
        if self.buf[0] != SOH:
            self.buf.clear()
            return
        if len(self.buf) < need:
            return
        pkt = bytes(self.buf)
        self.buf.clear()
        self._on_packet(pkt)

    def _on_packet(self, pkt: bytes) -> None:
        self.packets_seen += 1
        if self.corrupt > 0:
            self.corrupt -= 1
            pkt = pkt[:10] + bytes([pkt[10] ^ 0x55]) + pkt[11:]
        blk, inv, data, tail = pkt[1], pkt[2], pkt[3:131], pkt[131:]
        good = blk + inv == 255
        if self.mode == "crc":
            good = good and int.from_bytes(tail, "big") == crc16_xmodem(data)
        else:
            good = good and tail[0] == sum(data) & 0xFF
        if not good:
            self.naks_sent += 1
            self._reply(bytes([NAK]))
            return
        if self.cancel_at is not None and blk == self.cancel_at:
            self._reply(bytes([CAN, CAN]))
            return
        if blk == self.expect & 0xFF:
            self.received += data
            self.expect += 1
        elif blk != (self.expect - 1) & 0xFF:           # neither next nor duplicate
            self._reply(bytes([CAN, CAN]))
            return
        if self.drop_acks > 0:
            self.drop_acks -= 1                          # ACK lost on the wire
            return
        self._reply(bytes([ACK]))

    @property
    def file_bytes(self) -> bytes:
        return bytes(self.received).rstrip(bytes([SUB]))


TEXT = "".join(f"line{i}: {i * 7}\n" for i in range(60))     # ~ 700 bytes -> 6 blocks


def test_crc16_known_vector():
    assert crc16_xmodem(b"123456789") == 0x31C3


def test_packet_format_and_padding():
    pkt = build_packet(1, b"abc")
    assert len(pkt) == 133 and pkt[0] == SOH and pkt[1] == 1 and pkt[2] == 254
    assert pkt[3:6] == b"abc" and set(pkt[6:131]) == {SUB}
    assert int.from_bytes(pkt[131:], "big") == crc16_xmodem(pkt[3:131])
    assert build_packet(256, b"x")[1] == 0                         # wraps


def test_clean_upload_roundtrip():
    rx = FakeReceiver()
    prog: list[tuple[int, int]] = []
    out = upload_config(rx, TEXT, progress=lambda a, b: prog.append((a, b)), **FAST)
    assert rx.filename == "config.yaml"
    assert rx.file_bytes.decode() == TEXT
    assert len(rx.received) % 128 == 0
    assert "ok" in out
    n = (len(TEXT.encode()) + 127) // 128
    assert prog == [(i, n) for i in range(1, n + 1)]
    assert rx.writes[0] == b"$Xmodem/Receive=config.yaml\n"
    assert rx.writes[-1] == bytes([EOT])


def test_exact_multiple_of_block_size_has_no_extra_block():
    text = "a" * 256
    rx = FakeReceiver()
    upload_config(rx, text, **FAST)
    assert rx.packets_seen == 2 and rx.file_bytes.decode() == text


def test_custom_filename_and_bad_filename():
    rx = FakeReceiver()
    upload_config(rx, "x: 1\n", filename="alt.yaml", **FAST)
    assert rx.filename == "alt.yaml"
    for bad in ("../config.yaml", "a b.yaml", "", "x\n.yaml", "a;b"):
        with pytest.raises(ValueError):
            upload_config(FakeReceiver(), "x", filename=bad, **FAST)
    with pytest.raises(ValueError):
        upload_config(FakeReceiver(), "", **FAST)


def test_corrupted_blocks_are_nakked_and_resent():
    rx = FakeReceiver(corrupt_packets=3)
    upload_config(rx, TEXT, **FAST)
    assert rx.naks_sent == 3
    assert rx.file_bytes.decode() == TEXT


def test_lost_ack_causes_resend_and_receiver_dedups():
    rx = FakeReceiver(drop_acks=2)
    upload_config(rx, TEXT, **FAST)
    assert rx.file_bytes.decode() == TEXT                          # no duplicated block in the file
    assert rx.packets_seen > (len(TEXT) + 127) // 128


def test_too_many_naks_aborts_with_can():
    rx = FakeReceiver(corrupt_packets=1000)
    with pytest.raises(XmodemError, match="Versuche"):
        upload_config(rx, TEXT, max_retries=3, **FAST)
    assert bytes([CAN, CAN, CAN]) in rx.writes


def test_receiver_cancel():
    rx = FakeReceiver(cancel_at_block=3)
    with pytest.raises(XmodemCancelled):
        upload_config(rx, TEXT, **FAST)


def test_no_start_character_times_out():
    rx = FakeReceiver(start=False)
    with pytest.raises(XmodemError, match="startet XMODEM nicht"):
        upload_config(rx, TEXT, start_timeout=0.15, ack_timeout=0.05, response_timeout=0.1, timeout=5)


def test_text_before_start_with_capital_c_is_not_mistaken_for_start():
    rx = FakeReceiver(chatter=b"[MSG:INFO: Config file will be overwritten]\r\n[MSG:INFO: C\r\n")
    upload_config(rx, TEXT, **FAST)
    assert rx.file_bytes.decode() == TEXT


def test_stale_bytes_in_the_input_are_discarded():
    rx = FakeReceiver(stale=b"<Idle|MPos:0,0,0|FS:0,0>\r\nC\r\n")
    upload_config(rx, TEXT, **FAST)
    assert rx.file_bytes.decode() == TEXT


def test_checksum_mode_fallback():
    rx = FakeReceiver(mode="checksum")
    upload_config(rx, TEXT, **FAST)
    assert rx.file_bytes.decode() == TEXT


def test_nak_on_first_eot_is_retried():
    rx = FakeReceiver(nak_first_eot=True)
    upload_config(rx, TEXT, **FAST)
    assert rx.eots == 2 and rx.file_bytes.decode() == TEXT


def test_response_text_is_returned_and_error_raises():
    rx = FakeReceiver(final=b"[MSG:INFO: Config saved]\r\nok\r\n")
    out = upload_config(rx, TEXT, **FAST)
    assert "Config saved" in out and "ok" in out
    with pytest.raises(XmodemError, match="Fehler"):
        upload_config(FakeReceiver(final=b"error:20\r\n"), TEXT, **FAST)


def test_missing_ok_is_not_fatal():
    out = upload_config(FakeReceiver(final=b""), TEXT, **FAST)
    assert "ok" not in out


def test_overall_deadline():
    rx = FakeReceiver(drop_acks=10_000)
    t0 = time.monotonic()
    with pytest.raises(XmodemError):
        upload_config(rx, TEXT, start_timeout=0.5, ack_timeout=0.02, response_timeout=0.1,
                      timeout=0.3, max_retries=10_000)
    assert time.monotonic() - t0 < 3.0


def test_link_error_propagates():
    rx = FakeReceiver()
    rx.close()
    with pytest.raises(LinkError):
        upload_config(rx, TEXT, **FAST)


def test_large_file_block_numbers_wrap():
    text = "k: " + "v" * 40_000 + "\n"                  # > 255 blocks
    rx = FakeReceiver()
    upload_config(rx, text, **FAST)
    assert rx.file_bytes.decode() == text
