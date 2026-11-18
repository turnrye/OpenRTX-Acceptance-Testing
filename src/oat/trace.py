# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
The M17 receive trace: what a trace build of OpenRTX reports over its USB
serial port about every frame it decoded (firmware/m17-trace/README.md
describes the lines), read and scored against what the bench sent.

SerialTrace reads a radio; SimTrace stands in for one on the simulated
bench, decoding the bench's own M17 symbols through noise with oat.m17's
decoder, so the suite runs without hardware.
"""

import os
import select
import termios
import threading
import time
import tty
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from oat.m17 import codec


@dataclass
class Event:
    """One trace line: when the host read it, its keyword and fields."""

    time: float
    kind: str
    fields: list[str] = field(default_factory=list)

    @property
    def ms(self) -> int | None:
        """The radio's time stamp, for the lines that carry one."""
        if self.kind in ("ID",) or not self.fields:
            return None
        try:
            return int(self.fields[0])
        except ValueError:
            return None


def parse_line(line: str, at: float) -> Event | None:
    """A trace line as an Event; None for an empty line."""
    parts = line.strip().split()
    if not parts:
        return None
    return Event(at, parts[0], parts[1:])


class Trace:
    """The trace lines a radio sent, in order, with the time they arrived."""

    def __init__(self):
        self._events: list[Event] = []
        self._lock = threading.Lock()
        self.label: str | None = None

    def now(self) -> float:
        """The time events are stamped with."""
        return time.monotonic()

    def _add(self, line: str, at: float | None = None) -> None:
        e = parse_line(line, self.now() if at is None else at)
        if e is None:
            return
        if e.kind == "ID":
            self.label = " ".join(e.fields)
        with self._lock:
            self._events.append(e)

    def events_since(self, start: float) -> list[Event]:
        """The events read at or after start."""
        with self._lock:
            return [e for e in self._events if e.time >= start]

    def command(self, text: str) -> None:
        """Send a command to the radio."""

    def close(self) -> None:
        """Stop the trace and stop listening."""


class SerialTrace(Trace):
    """
    The trace of a radio on its USB serial port. Opening it asks for the
    build's label and starts the trace; every byte read goes to `log`.
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
        self._partial = b""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True,
                                        name=f"trace {port}")
        self._thread.start()
        self.command("ID")
        self.command("TRACE ON")

    def command(self, text: str) -> None:
        os.write(self._fd, text.encode("ascii") + b"\n")

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
            at = self.now()
            lines = (self._partial + data).split(b"\n")
            self._partial = lines.pop()
            for line in lines:
                self._add(line.decode("ascii", "replace"), at)

    def close(self) -> None:
        try:
            self.command("TRACE OFF")
        except OSError:
            pass
        self._stop.set()
        self._thread.join(timeout=1.0)
        try:
            os.close(self._fd)
        except OSError:
            pass
        if self._log:
            self._log.close()


class SimTrace(Trace):
    """
    The simulated radio's trace. It hears each M17 transmission the bench
    sends as symbols with Gaussian noise whose rms is 0.3 of the inner
    symbol level at -118 dBm and grows 1 dB per dB below, decodes them with
    oat.m17, and reports what a radio would.
    """

    STACK = 1024
    REFERENCE_DBM = -118.0
    REFERENCE_SIGMA = 0.3

    def __init__(self, clock=None):
        super().__init__()
        from oat.sdr.sim import SimClock
        self.clock = clock or SimClock()
        self.label = "sim"
        self._seed = 0
        self._ms = 0

    def now(self) -> float:
        return self.clock.now

    def _line(self, text: str) -> None:
        self._add(text)

    def sigma(self, level_dbm: float | None) -> float:
        """Noise rms on the symbols at a level, in inner-level units."""
        if level_dbm is None:
            return 0.0
        return self.REFERENCE_SIGMA * 10 ** ((self.REFERENCE_DBM
                                              - float(level_dbm)) / 20)

    def hear(self, symbols: np.ndarray, level_dbm: float | None) -> None:
        """Hear one transmission of M17 symbols, frame by frame."""
        self._seed += 1
        rng = np.random.default_rng(self._seed)
        sigma = self.sigma(level_dbm)
        x = symbols.astype(float) + rng.normal(scale=sigma, size=len(symbols))
        n = len(symbols) // codec.SYMBOLS_PER_FRAME
        lsf_bytes, lsf_valid, lich = None, False, []
        locked = False
        for i in range(n):
            frame = x[i * codec.SYMBOLS_PER_FRAME:(i + 1)
                      * codec.SYMBOLS_PER_FRAME]
            self._ms += 40
            dist = {k: int(np.sum(np.sign(frame[:8])
                                  != np.sign(codec.sync_symbols(w))))
                    for k, w in (("lsf", codec.SYNC_LSF),
                                 ("stream", codec.SYNC_STREAM),
                                 ("packet", codec.SYNC_PACKET),
                                 ("eot", codec.EOT_MARKER))}
            kind = min(dist, key=dist.get)
            if dist[kind] > 1:
                continue
            if not locked and kind in ("lsf", "stream"):
                locked = True
                self._line(f"LOCK {self._ms} 1")
            if not locked:
                continue
            payload = frame[8:]
            if kind == "lsf":
                raw, ok, _ = codec.decode_lsf_bytes(payload)
                if raw != lsf_bytes:
                    lsf_bytes, lsf_valid = raw, ok
                    self._line(f"LSF {self._ms} {int(ok)} F {raw.hex()}")
            elif kind == "stream":
                f = codec.decode_stream_frame(payload)
                lich.append(f)
                if not lsf_valid:
                    raw = self._lsf_from_lich(lich[-6:])
                    if raw is not None:
                        lsf_bytes, lsf_valid = raw, True
                        self._line(f"LSF {self._ms} 1 I {raw.hex()}")
                fn = f.fn | (0x8000 if f.last else 0)
                self._line(f"STR {self._ms} {fn} {int(lsf_valid)} "
                           f"{f.payload.hex()}")
                if lsf_valid:
                    self._line(f"PLAY {self._ms} {fn}")
            elif kind == "packet":
                p = codec.decode_packet_frame(payload)
                self._line(f"PKT {self._ms} {p.counter} {int(p.eof)} "
                           f"{p.data.hex()}")
            elif kind == "eot":
                self._line(f"EOT {self._ms}")
                break
        if locked:
            self._ms += 40
            self._line(f"LOCK {self._ms} 0")
        self._line(f"HB {self._ms} {self.STACK} {self.STACK // 3} 0")


    @staticmethod
    def _lsf_from_lich(frames: list) -> bytes | None:
        """The 30 LSF bytes from six LICH chunks, if they pass the CRC."""
        chunks = {}
        for f in frames:
            if f.counter < 6 and f.lich_errors >= 0:
                chunks.setdefault(f.counter, f.lich)
        if len(chunks) < 6:
            return None
        raw = b"".join(chunks[i] for i in range(6))
        if codec.crc16(raw[:28]) != int.from_bytes(raw[28:30], "big"):
            return None
        return raw


def make_trace(cfg: dict | None, log: Path | None = None,
               clock=None) -> Trace | None:
    """The bench's trace from its `trace:` section, or None without one."""
    if not cfg:
        return None
    kind = cfg.get("type")
    if kind == "serial":
        return SerialTrace(cfg["port"], log, cfg.get("baud"))
    if kind == "sim":
        return SimTrace(clock)
    raise ValueError(f"unknown trace type {kind!r}: serial or sim")


# Scoring ------------------------------------------------------------------

def score_stream(events: list[Event], payloads: list[bytes]) -> dict:
    """
    Score one voice transmission's trace against the payloads sent, frame
    numbers 0 up. Each STR line is decoded (its frame number and payload as
    sent, the first time), repeated (the decoder kept the previous frame
    after rejecting one, so the same frame comes again) or garbled
    (anything else). Played frames come from the PLAY lines, garbled ones
    being those whose last STR line was garbled.
    """
    sent = len(payloads)
    decoded, garbled, repeated = set(), 0, 0
    played, played_garbled = 0, 0
    previous, last_good = None, False
    first_lsf_frame, lsf_source, first_play_frame = None, None, None
    frames_seen = 0
    for e in events:
        if e.kind == "LSF" and len(e.fields) >= 4 and e.fields[1] == "1":
            if lsf_source is None:
                lsf_source = e.fields[2]
                first_lsf_frame = frames_seen
        elif e.kind == "STR" and len(e.fields) >= 4:
            frames_seen += 1
            fn = int(e.fields[1]) & 0x7FFF
            payload = bytes.fromhex(e.fields[3])
            key = (fn, payload)
            if key == previous:
                repeated += 1
            elif fn < sent and payloads[fn] == payload:
                decoded.add(fn)
                last_good = True
            else:
                garbled += 1
                last_good = False
            previous = key
        elif e.kind == "PLAY":
            played += 1
            if not last_good:
                played_garbled += 1
            if first_play_frame is None:
                first_play_frame = frames_seen - 1
    return {"sent": sent, "decoded": len(decoded),
            "missed": sent - len(decoded), "garbled": garbled,
            "repeated": repeated, "played": played,
            "played_garbled": played_garbled,
            "rate": len(decoded) / sent if sent else 0.0,
            "lsf_after_frames": first_lsf_frame, "lsf_from": lsf_source,
            "first_play_frame": first_play_frame}


def reassemble_packets(events: list[Event]) -> list[tuple[bytes, bool]]:
    """
    Packets rebuilt from PKT lines as OpenRTX's replay tool rebuilds them:
    frames in counter order, the last one's counter being its data bytes,
    and the CRC checked. Returns each packet with whether its CRC held;
    a frame out of order drops the packet being built.
    """
    packets, data, expected = [], b"", 0
    for e in events:
        if e.kind != "PKT" or len(e.fields) < 4:
            continue
        counter, eof = int(e.fields[1]), e.fields[2] == "1"
        chunk = bytes.fromhex(e.fields[3])
        if not eof:
            if counter != expected:
                data, expected = b"", 0
                if counter != 0:
                    continue
            data += chunk
            expected += 1
            continue
        if not 0 < counter <= codec.PACKET_FRAME_BYTES:
            data, expected = b"", 0
            continue
        data += chunk[:counter]
        ok = (len(data) >= 3
              and codec.crc16(data[:-2]) == int.from_bytes(data[-2:], "big"))
        packets.append((data, ok))
        data, expected = b"", 0
    return packets


def stack_report(events: list[Event]) -> dict:
    """
    The receive thread's stack from the HB lines: its size, the least free
    stack seen, lines dropped, and resets (the radio's clock going back).
    """
    beats = [e for e in events if e.kind == "HB" and len(e.fields) >= 4]
    if not beats:
        return {"beats": 0}
    resets = sum(1 for a, b in zip(beats, beats[1:]) if b.ms < a.ms)
    return {"beats": len(beats), "stack": int(beats[-1].fields[1]),
            "least_free": min(int(b.fields[2]) for b in beats),
            "dropped": int(beats[-1].fields[3]), "resets": resets}
