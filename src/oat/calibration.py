# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
RF path calibration: the loss of each cable and attenuator chain on a bench,
by frequency. Every tester's equipment differs, so it comes from a file that
tester measured, never from the suites.
"""

import datetime
import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

METHODS = ("vna", "power-meter", "assumed")
# Touchstone frequency units, in Hz
FREQ_UNITS = {"HZ": 1.0, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9}


@dataclass
class PathLoss:
    """Loss of one RF path, in dB, at increasing frequencies."""

    freqs_hz: np.ndarray
    loss_db: np.ndarray

    def at(self, freq_hz: float) -> float:
        """
        Loss at a frequency, interpolated linearly. A single point is a flat
        loss; otherwise the frequency must lie within the table.
        """
        if len(self.freqs_hz) == 1:
            return float(self.loss_db[0])
        if not self.freqs_hz[0] <= freq_hz <= self.freqs_hz[-1]:
            raise ValueError(
                f"{freq_hz / 1e6:.3f} MHz is outside the calibrated range "
                f"{self.freqs_hz[0] / 1e6:.3f}-{self.freqs_hz[-1] / 1e6:.3f} MHz")
        return float(np.interp(freq_hz, self.freqs_hz, self.loss_db))


@dataclass
class Calibration:
    """A bench's measured RF paths, and how and when they were measured."""

    method: str
    measured: datetime.date
    expires: datetime.date
    paths: dict[str, PathLoss]
    sha256: str  # of the calibration file, to tell calibrations apart

    def loss(self, path: str, freq_hz: float) -> float:
        """Loss of a named path at a frequency."""
        if path not in self.paths:
            raise KeyError(f"calibration has no '{path}' path")
        return self.paths[path].at(freq_hz)

    def expired(self, today: datetime.date | None = None) -> bool:
        """Whether the calibration is past its expiry date."""
        return (today or datetime.date.today()) > self.expires

    def summary(self) -> dict:
        """What results record about the calibration: no values, no names."""
        return {"method": self.method, "measured": self.measured.isoformat(),
                "expires": self.expires.isoformat(),
                "sha256": self.sha256[:12]}


def read_touchstone_s21(path: Path, port: str = "s21") -> PathLoss:
    """
    Loss from a two-port Touchstone (.s2p) file, as a VNA exports it: the
    negative of S21 (or S12) in dB.
    """
    unit, fmt = "GHZ", "MA"
    values = []
    for line in Path(path).read_text().splitlines():
        line = line.split("!", 1)[0].strip()
        if not line:
            continue
        if line.startswith("#"):
            opts = line[1:].upper().split()
            unit = next((o for o in opts if o in FREQ_UNITS), unit)
            fmt = next((o for o in opts if o in ("MA", "DB", "RI")), fmt)
            continue
        values += [float(v) for v in line.split()]
    if len(values) % 9:
        raise ValueError(f"{path}: not two-port Touchstone data")
    rows = np.array(values).reshape(-1, 9)
    col = {"s21": 3, "s12": 5}[port.lower()]
    a, b = rows[:, col], rows[:, col + 1]
    if fmt == "DB":
        s_db = a
    elif fmt == "MA":
        s_db = 20 * np.log10(a)
    else:
        s_db = 20 * np.log10(np.abs(a + 1j * b))
    freqs = rows[:, 0] * FREQ_UNITS[unit]
    order = np.argsort(freqs)
    return PathLoss(freqs[order], -s_db[order])


def _path_loss(spec: dict, base: Path) -> PathLoss:
    if "touchstone" in spec:
        return read_touchstone_s21(base / spec["touchstone"],
                                   spec.get("port", "s21"))
    points = np.array(spec["points"], dtype=float)
    order = np.argsort(points[:, 0])
    return PathLoss(points[order, 0], points[order, 1])


def load_calibration(path: Path) -> Calibration:
    """Read a calibration file; Touchstone files are relative to it."""
    path = Path(path)
    raw = path.read_bytes()
    data = yaml.safe_load(raw) or {}
    method = data.get("method")
    if method not in METHODS:
        raise ValueError(f"{path}: method must be one of {', '.join(METHODS)}")
    paths = {name: _path_loss(spec, path.parent)
             for name, spec in (data.get("paths") or {}).items()}
    digest = hashlib.sha256(raw)
    for spec in (data.get("paths") or {}).values():
        if "touchstone" in spec:
            digest.update((path.parent / spec["touchstone"]).read_bytes())
    return Calibration(method, _date(data, "measured", path),
                       _date(data, "expires", path), paths, digest.hexdigest())


def _date(data: dict, key: str, path: Path) -> datetime.date:
    value = data.get(key)
    if isinstance(value, datetime.date):
        return value
    if value is None:
        raise ValueError(f"{path}: '{key}' date is required")
    return datetime.date.fromisoformat(str(value))
