# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
M17 frames as symbols and back: the Link Setup Frame (LSF), stream frames
with their Link Information Channel (LICH), the preamble and the End of
Transmission marker, with the CRC, Golay code, convolutional code,
puncturing, interleaver and randomizer they use.

Constants and procedures follow the M17 Protocol Specification, part I
(physical and data link layers, v2.0.2, 2025-09-28), and libm17, the M17
Foundation's reference C library; tests/test_m17_codec.py checks this
module against vectors generated from libm17
(scripts/m17-reference-vectors.py).
"""

from dataclasses import dataclass

import numpy as np

SYMBOL_RATE = 4800
SYMBOLS_PER_FRAME = 192  # 40 ms
SYNC_SYMBOLS = 8
PAYLOAD_SYMBOLS = SYMBOLS_PER_FRAME - SYNC_SYMBOLS
PAYLOAD_BITS = 2 * PAYLOAD_SYMBOLS  # 368 type-4 bits

SYNC_LSF = 0x55F7
SYNC_STREAM = 0xFF5D
SYNC_PACKET = 0x75FF
SYNC_BERT = 0xDF55
EOT_MARKER = 0x555D

# Dibit (MSB first) to symbol, spec table "Dibit Symbol Mapping"
SYMBOL_MAP = np.array([+1, +3, -1, -3], dtype=np.int8)

# Data type values of the LSF TYPE field
TYPE_STREAM = 0x0001
TYPE_VOICE = 0x0004
TYPE_VOICE_STREAM = TYPE_STREAM | TYPE_VOICE

CRC_POLY = 0x5935
CALLSIGN_ALPHABET = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-/."
BROADCAST = 0xFFFFFFFFFFFF
HASH_BASE = 40 ** 9  # start of the #-address range

# Golay(24, 12) parity of each data bit, LSB first
GOLAY_MATRIX = (0x8EB, 0x93E, 0xA97, 0xDC6, 0x367, 0x6CD,
                0xD99, 0x3DA, 0x7B4, 0xF68, 0x63B, 0xC75)

# Puncture patterns: P1 for the LSF, P2 for stream frames
P1 = np.array([1] + [1, 0, 1, 1] * 15, dtype=bool)
P2 = np.array([1] * 11 + [0], dtype=bool)

RANDOMIZER = bytes.fromhex(
    "D6B5E23082FF8462BA4E9690D898DD5D0CC85243911DF8"
    "6E682F35DA14EACD76198DD580D133871357182D2978C3")


def _bits(data: bytes, count: int | None = None) -> np.ndarray:
    """Bytes as bits, MSB first."""
    bits = np.unpackbits(np.frombuffer(bytes(data), dtype=np.uint8))
    return bits if count is None else bits[:count]


def _bytes(bits) -> bytes:
    return np.packbits(np.asarray(bits, dtype=np.uint8)).tobytes()


RANDOMIZER_BITS = _bits(RANDOMIZER).astype(bool)
# Quadratic permutation polynomial interleaver, f(i) = 45 i + 92 i^2 mod 368
INTERLEAVE = (45 * np.arange(PAYLOAD_BITS)
              + 92 * np.arange(PAYLOAD_BITS) ** 2) % PAYLOAD_BITS


# Payload fields -----------------------------------------------------------

def crc16(data: bytes) -> int:
    """The M17 CRC: polynomial 0x5935, initial value 0xFFFF."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc <<= 1
            if crc & 0x10000:
                crc = (crc ^ CRC_POLY) & 0xFFFF
    return crc


def encode_callsign(call: str) -> bytes:
    """A callsign as the 6-byte base-40 address, first character lowest."""
    call = call.upper()
    if call == "@ALL":
        return BROADCAST.to_bytes(6, "big")
    hashed = call.startswith("#")
    body = call[1:] if hashed else call
    if len(body) > (8 if hashed else 9):
        raise ValueError(f"callsign {call!r} is too long")
    value = 0
    for ch in reversed(body):
        if ch not in CALLSIGN_ALPHABET:
            raise ValueError(f"{ch!r} isn't in the M17 callsign alphabet")
        value = value * 40 + CALLSIGN_ALPHABET.index(ch)
    return (value + (HASH_BASE if hashed else 0)).to_bytes(6, "big")


def decode_callsign(raw: bytes) -> str:
    """A 6-byte address as a callsign; reserved values come back as hex."""
    value = int.from_bytes(raw, "big")
    if value == BROADCAST:
        return "@ALL"
    prefix = ""
    if value >= HASH_BASE:
        if value >= HASH_BASE + 40 ** 8:
            return f"reserved:{raw.hex()}"
        prefix, value = "#", value - HASH_BASE
    chars = []
    while value:
        chars.append(CALLSIGN_ALPHABET[value % 40])
        value //= 40
    return prefix + "".join(chars)


@dataclass
class Lsf:
    """Link Setup Frame contents: addresses, TYPE and META."""

    destination: str = "@ALL"
    source: str = "N0CALL"
    type: int = TYPE_VOICE_STREAM
    meta: bytes = bytes(14)

    def to_bytes(self) -> bytes:
        """The 30 bytes sent: fields, then their CRC."""
        body = (encode_callsign(self.destination) + encode_callsign(self.source)
                + self.type.to_bytes(2, "big") + bytes(self.meta).ljust(14, b"\0"))
        return body + crc16(body).to_bytes(2, "big")

    @classmethod
    def from_bytes(cls, raw: bytes) -> tuple["Lsf", bool]:
        """Contents of 30 LSF bytes, and whether their CRC holds."""
        return (cls(decode_callsign(raw[0:6]), decode_callsign(raw[6:12]),
                    int.from_bytes(raw[12:14], "big"), bytes(raw[14:28])),
                crc16(raw[:28]) == int.from_bytes(raw[28:30], "big"))


# Golay(24, 12) ------------------------------------------------------------

def golay_encode(data: int) -> int:
    """24-bit codeword: the 12 data bits, then their 12 parity bits."""
    parity = 0
    for i in range(12):
        if data >> i & 1:
            parity ^= GOLAY_MATRIX[i]
    return (data << 12) | parity


def _syndrome(word: int) -> int:
    return (word & 0xFFF) ^ (golay_encode(word >> 12) & 0xFFF)


def _golay_table() -> dict[int, int]:
    """Error pattern of each syndrome: the code is perfect, so every one of
    the 4096 syndromes belongs to exactly one pattern of up to 3 errors."""
    table = {0: 0}
    for a in range(24):
        table[_syndrome(1 << a)] = 1 << a
        for b in range(a + 1, 24):
            table[_syndrome(1 << a | 1 << b)] = 1 << a | 1 << b
            for c in range(b + 1, 24):
                e = 1 << a | 1 << b | 1 << c
                table[_syndrome(e)] = e
    return table


_GOLAY_ERRORS = _golay_table()


def golay_decode(word: int) -> tuple[int, int]:
    """The 12 data bits of a received codeword, and the errors corrected."""
    error = _GOLAY_ERRORS[_syndrome(word)]
    return ((word ^ error) >> 12) & 0xFFF, bin(error).count("1")


# Convolutional code -------------------------------------------------------

def conv_encode(bits: np.ndarray, puncture: np.ndarray) -> np.ndarray:
    """
    Rate 1/2, K=5 encoder (G1 = 1 + D^3 + D^4, G2 = 1 + D + D^2 + D^4),
    flushed with 4 zeros, then punctured by a repeating pattern.
    """
    ud = np.concatenate([np.zeros(4, np.uint8), np.asarray(bits, np.uint8),
                         np.zeros(4, np.uint8)])
    n = len(bits) + 4
    g1 = ud[4:4 + n] ^ ud[1:1 + n] ^ ud[0:n]
    g2 = ud[4:4 + n] ^ ud[3:3 + n] ^ ud[2:2 + n] ^ ud[0:n]
    out = np.empty(2 * n, dtype=np.uint8)
    out[0::2], out[1::2] = g1, g2
    keep = np.resize(puncture, len(out))
    return out[keep]


def _trellis():
    """Next state and outputs per state and input bit; the state holds the
    last four inputs, newest in bit 3."""
    nxt = np.zeros((16, 2), dtype=np.int64)
    out = np.zeros((16, 2, 2), dtype=np.uint8)
    for s in range(16):
        d3, d2, d1, d0 = s >> 3 & 1, s >> 2 & 1, s >> 1 & 1, s & 1
        for b in range(2):
            nxt[s, b] = (b << 3) | (s >> 1)
            out[s, b] = (b ^ d1 ^ d0, b ^ d3 ^ d2 ^ d0)
    return nxt, out


_NEXT, _OUT = _trellis()


def viterbi_decode(soft: np.ndarray, puncture: np.ndarray,
                   n_bits: int) -> tuple[np.ndarray, float]:
    """
    Decode n_bits from punctured soft bits, each the probability of a 1
    (0.5 for no idea). Returns the bits and the path metric: the summed
    distance between the soft bits and the chosen codeword, which is the
    number of bit errors corrected for hard bits.
    """
    steps = n_bits + 4
    full = np.full(2 * steps, 0.5)
    keep = np.resize(puncture, 2 * steps)
    full[keep] = np.asarray(soft, dtype=float)[:int(keep.sum())]
    pairs = full.reshape(steps, 2)
    metric = np.full(16, np.inf)
    metric[0] = 0.0
    prev_state = np.zeros((steps, 16), dtype=np.int64)
    # For each next state, its two predecessors and their input bit
    preds = np.array([[((ns & 7) << 1) | x for x in (0, 1)]
                      for ns in range(16)])
    bit_in = np.arange(16) >> 3
    for i in range(steps):
        r = pairs[i]
        # Cost of each (state, bit) branch
        branch = (np.abs(_OUT[:, :, 0] - r[0]) + np.abs(_OUT[:, :, 1] - r[1]))
        cand = metric[preds] + branch[preds, bit_in[:, None]]
        choice = np.argmin(cand, axis=1)
        prev_state[i] = preds[np.arange(16), choice]
        metric = cand[np.arange(16), choice]
    state, bits = 0, np.zeros(steps, dtype=np.uint8)
    for i in range(steps - 1, -1, -1):
        bits[i] = state >> 3
        state = prev_state[i, state]
    # Punctured positions cost 0.5 whichever bit the path has
    return bits[:n_bits], float(metric[0]) - 0.5 * int((~keep).sum())


# Frames -------------------------------------------------------------------

def sync_symbols(word: int) -> np.ndarray:
    """The 8 symbols of a sync burst."""
    return np.array([SYMBOL_MAP[(word >> (14 - 2 * i)) & 3]
                     for i in range(SYNC_SYMBOLS)], dtype=np.int8)


def bits_to_symbols(bits: np.ndarray) -> np.ndarray:
    """Type-4 bits, two per symbol, MSB first."""
    b = np.asarray(bits, dtype=np.uint8)
    return SYMBOL_MAP[2 * b[0::2] + b[1::2]]


def _physical(type3: np.ndarray) -> np.ndarray:
    """Type-3 bits interleaved and randomized into type-4 bits."""
    return type3[INTERLEAVE] ^ RANDOMIZER_BITS


def preamble(count: int = SYMBOLS_PER_FRAME) -> np.ndarray:
    """Alternating +3, -3 before an LSF, ending opposite its first symbol."""
    return np.resize(np.array([+3, -3], dtype=np.int8), count)


def eot() -> np.ndarray:
    """End of Transmission marker: 0x555D repeated for a frame."""
    return np.resize(sync_symbols(EOT_MARKER), SYMBOLS_PER_FRAME)


def lsf_frame(lsf: Lsf) -> np.ndarray:
    """LSF sync burst and payload: 192 symbols."""
    type3 = conv_encode(_bits(lsf.to_bytes()), P1)
    return np.concatenate([sync_symbols(SYNC_LSF),
                           bits_to_symbols(_physical(type3))])


def lich_bits(lsf: Lsf, counter: int) -> np.ndarray:
    """96 Golay-coded LICH bits: the counter's 40-bit chunk of the LSF."""
    raw = lsf.to_bytes()[5 * counter:5 * counter + 5] + bytes([counter << 5])
    words = [(raw[0] << 4) | (raw[1] >> 4), ((raw[1] & 0xF) << 8) | raw[2],
             (raw[3] << 4) | (raw[4] >> 4), ((raw[4] & 0xF) << 8) | raw[5]]
    return np.concatenate([_bits(golay_encode(w).to_bytes(3, "big"))
                           for w in words])


def stream_frame(lsf: Lsf, counter: int, fn: int, payload: bytes) -> np.ndarray:
    """
    A stream frame: sync burst, LICH chunk `counter` (0-5), frame number
    (bit 15 set on the last frame) and 16 payload bytes, two Codec2 3200
    frames for voice.
    """
    if len(payload) != 16:
        raise ValueError("a stream frame carries 16 payload bytes")
    data = _bits(fn.to_bytes(2, "big") + bytes(payload))
    type3 = np.concatenate([lich_bits(lsf, counter), conv_encode(data, P2)])
    return np.concatenate([sync_symbols(SYNC_STREAM),
                           bits_to_symbols(_physical(type3))])


def stream_symbols(lsf: Lsf, payloads: list[bytes],
                   preamble_symbols: int = SYMBOLS_PER_FRAME) -> np.ndarray:
    """
    A whole voice stream transmission: preamble, LSF, one stream frame per
    16-byte payload, the last flagged, then the EOT marker.
    """
    frames = [preamble(preamble_symbols), lsf_frame(lsf)]
    for i, payload in enumerate(payloads):
        fn = i & 0x7FFF
        if i == len(payloads) - 1:
            fn |= 0x8000
        frames.append(stream_frame(lsf, i % 6, fn, payload))
    frames.append(eot())
    return np.concatenate(frames)


# Decoding -----------------------------------------------------------------

def soft_bits(symbols: np.ndarray) -> np.ndarray:
    """
    Soft type-4 bits, the probability of each being 1, from payload symbols
    normalized to the +-1, +-3 levels: the MSB is 1 below zero, the LSB 1 on
    the outer levels, linear in between.
    """
    x = np.asarray(symbols, dtype=float)
    out = np.empty(2 * len(x))
    out[0::2] = np.clip((1 - x) / 2, 0, 1)
    out[1::2] = np.clip((np.abs(x) - 1) / 2, 0, 1)
    return out


def _type3_soft(payload_symbols: np.ndarray) -> np.ndarray:
    """Soft type-3 bits from 184 payload symbols."""
    soft = soft_bits(payload_symbols)
    soft = np.where(RANDOMIZER_BITS, 1 - soft, soft)
    out = np.empty(PAYLOAD_BITS)
    out[INTERLEAVE] = soft
    return out


def decode_lsf(payload_symbols: np.ndarray) -> tuple[Lsf, bool, float]:
    """LSF contents from its 184 payload symbols, CRC check, path metric."""
    bits, metric = viterbi_decode(_type3_soft(payload_symbols), P1, 240)
    lsf, ok = Lsf.from_bytes(_bytes(bits))
    return lsf, ok, metric


@dataclass
class StreamFrame:
    """A decoded stream frame."""

    counter: int  # LICH counter, 0-5
    lich: bytes  # its 5-byte chunk of the LSF
    fn: int  # frame number, without the last-frame flag
    last: bool
    payload: bytes
    metric: float  # Viterbi path metric of the payload
    lich_errors: int  # bits the Golay code corrected


def decode_stream_frame(payload_symbols: np.ndarray) -> StreamFrame:
    """A stream frame from its 184 payload symbols."""
    soft = _type3_soft(payload_symbols)
    hard = (soft[:96] > 0.5).astype(np.uint8)
    raw, errors = 0, 0
    for i in range(4):
        word = int("".join(map(str, hard[24 * i:24 * i + 24])), 2)
        data, e = golay_decode(word)
        raw = (raw << 12) | data
        errors += e
    lich = raw.to_bytes(6, "big")
    bits, metric = viterbi_decode(soft[96:], P2, 144)
    data = _bytes(bits)
    fn = int.from_bytes(data[:2], "big")
    return StreamFrame(lich[5] >> 5, lich[:5], fn & 0x7FFF, bool(fn & 0x8000),
                       data[2:], metric, errors)


def lsf_from_lich(frames: list[StreamFrame]) -> tuple[Lsf, bool] | None:
    """The LSF reassembled from the LICH of six consecutive counters."""
    chunks = {}
    for f in frames:
        if f.counter < 6:
            chunks.setdefault(f.counter, f.lich)
    if len(chunks) < 6:
        return None
    return Lsf.from_bytes(b"".join(chunks[i] for i in range(6)))
