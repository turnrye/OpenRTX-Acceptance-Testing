# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
A recording of off-air APRS traffic as a receive benchmark: the track goes
onto an FM carrier a block at a time, the radio's KISS TNC reports what it
decoded, and that is scored against what Dire Wolf's atest decodes from the
same audio. The recordings carry real callsigns, so they are for a cabled
bench only, never an antenna.
"""

import re
from collections import Counter
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from oat import aprs
from oat.signals import IQ_AMPLITUDE

# atest's report of a frame: when it ended, then the frame as TNC2 text
DECODED = re.compile(rb"DECODED\[\d+\]\s+(\d+):(\d+\.\d+)")
TNC2 = re.compile(rb"\[\d+\] (.*)$")
ESCAPE = re.compile(rb"<0x[0-9a-f]{2}>")


def open_track(path: Path, channel: int = 0) -> tuple[int, np.ndarray]:
    """
    A 16-bit WAV file's rate and one of its channels, memory-mapped so a
    long track is never read whole.
    """
    rate, data = wavfile.read(path, mmap=True)
    if data.dtype != np.int16:
        raise ValueError(f"{path}: 16-bit PCM WAV wanted, not {data.dtype}")
    if data.ndim > 1:
        data = data[:, int(channel)]
    return int(rate), data


def track_peak(samples: np.ndarray, block: int = 1 << 20) -> int:
    """The largest sample magnitude, found a block at a time."""
    peak = 0
    for i in range(0, len(samples), block):
        part = samples[i:i + block].astype(np.int32)
        peak = max(peak, int(np.abs(part).max(initial=0)))
    return peak


def fm_blocks(samples: np.ndarray, audio_rate: float, rate: float,
              deviation_hz: float, offset_hz: float = 0.0,
              peak: float | None = None, block_s: float = 1.0):
    """
    The track as FM, a block of IQ at a time: the track's peak deviates the
    carrier by deviation_hz. The audio is interpolated up to the
    transmitter's rate and the carrier's phase carries over between blocks,
    so the blocks join without a click.
    """
    peak = float(peak if peak is not None else track_peak(samples)) or 1.0
    step = audio_rate / rate
    per_block = int(block_s * rate)
    total = int(len(samples) / step)
    phase = 0.0
    for start in range(0, total, per_block):
        n = min(per_block, total - start)
        pos = (start + np.arange(n)) * step
        first = int(pos[0])
        last = min(int(pos[-1]) + 2, len(samples))
        audio = samples[first:last].astype(np.float64) / peak
        audio = np.interp(pos - first, np.arange(last - first), audio)
        inst = offset_hz + deviation_hz * audio
        ph = phase + 2 * np.pi * np.cumsum(inst) / rate
        phase = float(ph[-1] % (2 * np.pi))
        yield (IQ_AMPLITUDE * np.exp(1j * ph)).astype(np.complex64)


def strip_fcs(frame: bytes) -> bytes:
    """The frame without its FCS, if it ends in a valid one."""
    if len(frame) > 2 and aprs.fcs(frame[:-2]) == frame[-2:]:
        return frame[:-2]
    return frame


def tnc2(frame: bytes) -> bytes | None:
    """
    An AX.25 UI frame (with or without its FCS) as TNC2 text,
    SOURCE>DEST,PATH:information, or None if it isn't one.
    """
    frame = strip_fcs(frame)
    calls = []
    pos = 0
    while True:
        field = frame[pos:pos + 7]
        if len(field) < 7:
            return None
        call = bytes(b >> 1 for b in field[:6]).rstrip()
        ssid = (field[6] >> 1) & 0x0F
        calls.append(call + (b"-%d" % ssid if ssid else b""))
        pos += 7
        if field[6] & 1:
            break
    if len(calls) < 2 or frame[pos:pos + 2] != b"\x03\xf0":
        return None
    path = b",".join([calls[0]] + calls[2:])
    return calls[1] + b">" + path + b":" + frame[pos + 2:]


def normalize(line: bytes) -> bytes:
    """
    A TNC2 line reduced to what both decoders agree on: printable
    characters, without atest's escapes for the others or the marks on
    digipeaters that repeated the frame.
    """
    line = ESCAPE.sub(b"", line)
    return bytes(c for c in line if 32 <= c < 127 and c != ord("*")).rstrip()


def read_reference(path: Path) -> list[tuple[float, bytes]]:
    """
    The frames in atest's output, each with the time into the track it
    ended at.
    """
    out = []
    at = 0.0
    for line in Path(path).read_bytes().split(b"\n"):
        when = DECODED.search(line)
        if when:
            at = 60 * int(when.group(1)) + float(when.group(2))
            continue
        frame = TNC2.search(line.rstrip(b"\r"))
        if frame:
            out.append((at, normalize(frame.group(1))))
    return out


def score(reference: list[bytes], heard: list[bytes]) -> dict:
    """
    How the frames the radio reported compare with the reference decoder's:
    counted as multisets, since the same frame is often on the air twice.
    """
    want = Counter(reference)
    got = Counter(line for line in
                  (normalize(t) for t in map(tnc2, heard) if t is not None))
    common = sum((want & got).values())
    return {
        "reference": len(reference),
        "heard": len(heard),
        "common": common,
        "missed": sum((want - got).values()),
        "extra": sum((got - want).values()),
        "not_ui": sum(1 for f in heard if tnc2(f) is None),
        "rate": common / len(reference) if reference else 0.0,
        "missed_frames": [m.decode("latin-1")
                          for m in sorted((want - got).elements())],
    }
