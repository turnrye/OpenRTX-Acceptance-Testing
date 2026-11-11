# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Frames a radio's KISS TNC reports to the host: SLIP-framed KISS data frames
on a serial port, each an AX.25 frame without its FCS. The radio's console
text can share the port, so anything between two FENDs that isn't a valid
data frame is set aside, not taken as a frame.
"""

import os
import select
import termios
import threading
import time
import tty
from dataclasses import dataclass, field
from pathlib import Path

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD


@dataclass
class Heard:
    """One KISS data frame and when the host had it."""

    time: float
    frame: bytes
    port: int = 0


@dataclass
class Deframer:
    """
    Splits a byte stream into KISS data frames. Feed it bytes as they come;
    it keeps a frame split across reads. Candidates that aren't data frames
    (another command, a bad escape, console text) are counted as junk.
    """

    buf: bytearray = field(default_factory=bytearray)
    escaped: bool = False
    bad: bool = False
    junk: int = 0

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """The (port, frame) pairs completed by these bytes."""
        out = []
        for b in data:
            if b == FEND:
                if self.buf:
                    cmd = self.buf[0]
                    if self.bad or self.escaped or (cmd & 0x0F) != 0 \
                            or len(self.buf) < 2:
                        self.junk += 1
                    else:
                        out.append((cmd >> 4, bytes(self.buf[1:])))
                self.buf.clear()
                self.escaped = self.bad = False
            elif self.escaped:
                self.escaped = False
                if b == TFEND:
                    self.buf.append(FEND)
                elif b == TFESC:
                    self.buf.append(FESC)
                else:
                    self.bad = True
            elif b == FESC:
                self.escaped = True
            else:
                self.buf.append(b)
        return out


def kiss_encode(frame: bytes, port: int = 0) -> bytes:
    """A KISS data frame as the radio sends it, for tests."""
    body = bytes([(port & 0x0F) << 4]) + bytes(frame)
    body = body.replace(bytes([FESC]), bytes([FESC, TFESC]))
    body = body.replace(bytes([FEND]), bytes([FESC, TFEND]))
    return bytes([FEND]) + body + bytes([FEND])


class Tnc:
    """What a bench's TNC heard, in order, with the time it heard it."""

    def __init__(self):
        self._heard: list[Heard] = []
        self._lock = threading.Lock()
        self.deframer = Deframer()

    def now(self) -> float:
        """The time the TNC stamps frames with."""
        return time.monotonic()

    def _add(self, frame: bytes, port: int = 0, at: float | None = None):
        with self._lock:
            self._heard.append(Heard(self.now() if at is None else at,
                                     frame, port))

    def heard_since(self, start: float) -> list[Heard]:
        """The frames heard at or after start."""
        with self._lock:
            return [h for h in self._heard if h.time >= start]

    @property
    def junk(self) -> int:
        """Candidates between FENDs that weren't KISS data frames."""
        return self.deframer.junk

    def close(self) -> None:
        """Stop listening."""


class SerialTnc(Tnc):
    """
    A TNC on a serial port: the radio's USB CDC port, or its UART through
    a USB-serial adapter (the C62's), which needs `baud`; a CDC port ignores
    it. Opening the port raises DTR, as hosts do, so the radio sees a
    reader. Every byte read is appended to `log` when given, as evidence of
    what the radio sent.
    """

    def __init__(self, port: str, log: Path | None = None,
                 baud: int | None = None):
        super().__init__()
        self.port = port
        self._fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        tty.setraw(self._fd, termios.TCSANOW)
        if baud:
            speed = getattr(termios, f"B{int(baud)}", None)
            if speed is None:
                os.close(self._fd)
                raise ValueError(f"{port}: no standard rate {baud} baud")
            attrs = termios.tcgetattr(self._fd)
            attrs[4] = attrs[5] = speed
            termios.tcsetattr(self._fd, termios.TCSANOW, attrs)
        termios.tcflush(self._fd, termios.TCIFLUSH)
        self._log = open(log, "ab") if log else None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True,
                                        name=f"kiss {port}")
        self._thread.start()

    def _read(self):
        while not self._stop.is_set():
            ready, _, _ = select.select([self._fd], [], [], 0.2)
            if not ready:
                continue
            try:
                data = os.read(self._fd, 4096)
            except BlockingIOError:
                continue
            except OSError:
                # The port went away (unplugged, radio rebooted)
                break
            if not data:
                continue
            if self._log:
                self._log.write(data)
                self._log.flush()
            for port, frame in self.deframer.feed(data):
                self._add(frame, port)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        try:
            os.close(self._fd)
        except OSError:
            pass
        if self._log:
            self._log.close()


def make_tnc(cfg: dict | None, log: Path | None = None,
             clock=None) -> Tnc | None:
    """The bench's TNC from its `tnc:` section, or None without one."""
    if not cfg:
        return None
    kind = cfg.get("type")
    if kind == "serial":
        return SerialTnc(cfg["port"], log, cfg.get("baud"))
    if kind == "sim":
        # It decodes with Dire Wolf's atest; without it, there's no TNC
        import shutil
        if shutil.which("atest") is None:
            return None
        from oat.sdr.sim import SimTnc
        return SimTnc(clock)
    raise ValueError(f"unknown tnc type {kind!r}: serial or sim")
