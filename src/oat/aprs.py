# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
APRS packets for receive tests, sent the way a TNC sends them: AX.25 UI
frames (messages, positions, status) with their FCS, framed by HDLC flags,
bit-stuffed and NRZI-coded, as 1200 baud Bell 202 AFSK audio. The audio goes
onto an FM carrier like any other test signal. The tests check the audio
with Dire Wolf's atest, an independent decoder.
"""

import re

import numpy as np

BAUD = 1200
MARK_HZ = 1200.0
SPACE_HZ = 2200.0
FLAG = 0x7E
# Experimental tocall (the APZ prefix), so test traffic can't pass for a
# real product's
TOCALL = "APZOAT"
# The space tone's level over the mark's from a transmitter that
# pre-emphasizes its audio: 6 dB/octave from 1200 to 2200 Hz
PREEMPHASIS_TWIST_DB = float(20 * np.log10(SPACE_HZ / MARK_HZ))
# An AX.25 frame's longest information field
MAX_INFO_BYTES = 256

ADDRESS = re.compile(r"^([A-Z0-9]{1,6})(?:-(\d{1,2}))?(\*?)$")


def parse_address(text: str) -> tuple[str, int, bool]:
    """
    A station address as AX.25 holds it: callsign, SSID (0-15) and whether
    a digipeater has repeated it (a trailing *). Lower case is accepted.
    """
    m = ADDRESS.match(text.strip().upper())
    if not m or int(m.group(2) or 0) > 15:
        raise ValueError(f"not an AX.25 address: {text!r}")
    return m.group(1), int(m.group(2) or 0), bool(m.group(3))


def encode_address(text: str, last: bool = False,
                   command: bool = False) -> bytes:
    """
    Seven address bytes: the callsign padded to six characters and shifted
    left a bit, then the SSID byte. Its top bit is the command bit for the
    destination and source, and the has-been-repeated bit for a
    digipeater; its bottom bit marks the last address.
    """
    call, ssid, repeated = parse_address(text)
    out = bytes((ord(c) << 1) for c in call.ljust(6))
    top = 0x80 if (command or repeated) else 0
    return out + bytes([top | 0x60 | (ssid << 1) | int(last)])


def fcs(data: bytes) -> bytes:
    """The frame check sequence, CRC-16/X.25, sent low byte first."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    crc ^= 0xFFFF
    return bytes([crc & 0xFF, crc >> 8])


def ui_frame(source: str, info: str, path: list[str] | None = None,
             destination: str = TOCALL) -> bytes:
    """An AX.25 UI frame with its FCS: addresses, control, PID, info."""
    path = list(path or [])
    body = info.encode("latin-1")
    if len(body) > MAX_INFO_BYTES:
        raise ValueError(f"information field is {len(body)} bytes, more "
                         f"than {MAX_INFO_BYTES}")
    addresses = [destination, source] + path
    frame = b"".join(
        encode_address(a, last=(i == len(addresses) - 1), command=(i == 0))
        for i, a in enumerate(addresses))
    frame += bytes([0x03, 0xF0]) + body
    return frame + fcs(frame)


def message_info(addressee: str, text: str, number: str = "") -> str:
    """An APRS message's information field: :ADDRESSEE:text{number."""
    return f":{addressee:<9}:{text}" + (f"{{{number}" if number else "")


def _bits_lsb_first(data: bytes) -> list[int]:
    return [(byte >> i) & 1 for byte in data for i in range(8)]


def hdlc_bits(frame: bytes, preamble_flags: int = 40,
              tail_flags: int = 3) -> np.ndarray:
    """
    The bits sent for a frame: flags, the frame bit-stuffed (a 0 after five
    1s in a row), flags. Bytes go least significant bit first.
    """
    flag = _bits_lsb_first(bytes([FLAG]))
    stuffed, ones = [], 0
    for bit in _bits_lsb_first(frame):
        stuffed.append(bit)
        ones = ones + 1 if bit else 0
        if ones == 5:
            stuffed.append(0)
            ones = 0
    return np.array(flag * preamble_flags + stuffed + flag * tail_flags,
                    dtype=np.uint8)


def nrzi(bits: np.ndarray) -> np.ndarray:
    """NRZI: a 0 changes the tone, a 1 keeps it. Starts on the mark tone."""
    tone = np.empty(len(bits), dtype=np.uint8)
    state = 1
    for i, bit in enumerate(bits):
        if not bit:
            state ^= 1
        tone[i] = state
    return tone


def afsk(tones: np.ndarray, rate: float, twist_db: float = 0.0) -> np.ndarray:
    """
    Bell 202 audio for a sequence of tone states (1 mark, 0 space), with a
    continuous phase. twist_db raises the space tone over the mark; the
    louder tone peaks at 1.
    """
    per_bit = rate / BAUD
    n = int(round(len(tones) * per_bit))
    which = tones[np.minimum((np.arange(n) / per_bit).astype(int),
                             len(tones) - 1)]
    freq = np.where(which == 1, MARK_HZ, SPACE_HZ)
    phase = 2 * np.pi * np.cumsum(freq) / rate
    space = 10 ** (float(twist_db) / 20)
    amplitude = np.where(which == 1, 1.0, space) / max(1.0, space)
    return amplitude * np.sin(phase)


def packet_audio(frame: bytes, rate: float, twist_db: float = 0.0,
                 txdelay_s: float = 0.3) -> np.ndarray:
    """One transmission's audio: TXDELAY of flags, the frame, tail flags."""
    flags = max(1, int(round(txdelay_s * BAUD / 8)))
    return afsk(nrzi(hdlc_bits(frame, flags)), rate, twist_db)
