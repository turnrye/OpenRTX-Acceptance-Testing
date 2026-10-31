# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""IQ sample files in the formats the SDR command line tools use."""

from enum import Enum
from pathlib import Path

import numpy as np


# Zero of rtl_sdr's unsigned samples, also its full scale
CU8_ZERO = 127.5
# Full scale of hackrf_transfer's signed samples, symmetric around zero
CS8_FULL = 127


class IqFormat(str, Enum):
    """Interleaved 8-bit IQ file formats."""

    CU8 = "cu8"  # unsigned, rtl_sdr
    CS8 = "cs8"  # signed, hackrf_transfer


def read_iq(path: Path, fmt: IqFormat) -> np.ndarray:
    """Read an IQ file into complex samples scaled to about +-1."""
    if fmt == IqFormat.CU8:
        raw = (np.fromfile(path, dtype=np.uint8).astype(np.float32)
               - CU8_ZERO) / CU8_ZERO
    else:
        raw = np.fromfile(path, dtype=np.int8).astype(np.float32) / CS8_FULL
    raw = raw[: len(raw) // 2 * 2]
    return (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)


def iter_iq(path: Path, fmt: IqFormat, samples: int = 1 << 20):
    """Read an IQ file as read_iq does, a block of samples at a time."""
    dtype = np.uint8 if fmt == IqFormat.CU8 else np.int8
    with open(path, "rb") as f:
        while True:
            raw = np.fromfile(f, dtype=dtype, count=2 * samples)
            if not len(raw):
                return
            raw = raw.astype(np.float32)
            raw = ((raw - CU8_ZERO) / CU8_ZERO if fmt == IqFormat.CU8
                   else raw / CS8_FULL)
            raw = raw[: len(raw) // 2 * 2]
            yield (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)


def write_iq(path: Path, iq: np.ndarray, fmt: IqFormat) -> None:
    """Write complex samples scaled to about +-1, clipping at full scale."""
    out = np.empty(2 * len(iq), dtype=np.float32)
    out[0::2] = iq.real
    out[1::2] = iq.imag
    if fmt == IqFormat.CU8:
        out = np.clip(np.round(out * CU8_ZERO + CU8_ZERO), 0, 255)
        out.astype(np.uint8).tofile(path)
    else:
        out = np.clip(np.round(out * CS8_FULL), -CS8_FULL, CS8_FULL)
        out.astype(np.int8).tofile(path)


def clipped_fraction(path: Path, fmt: IqFormat) -> float:
    """Fraction of samples at the converter's full scale, a sign of overload."""
    if fmt == IqFormat.CU8:
        raw = np.fromfile(path, dtype=np.uint8)
        return float(np.mean((raw == 0) | (raw == 255)))
    raw = np.fromfile(path, dtype=np.int8)
    return float(np.mean((raw == -128) | (raw == 127)))
