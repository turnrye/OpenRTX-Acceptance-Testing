# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Bench calibration: the loss of each cable and attenuator chain on a bench,
by frequency, and optionally the instruments' own levels and frequency
errors: what the transmitter puts out at each gain setting, what a full-scale
reading on the receiver means in dBm, how far each instrument's reference
oscillator is off, and the voltage of a full-scale tone on the audio
interface. Every tester's equipment differs, so it comes from a file that
tester measured, never from the suites.
"""

import datetime
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

METHODS = ("vna", "power-meter", "assumed")
# Entries under paths: that are audio levels, not RF losses
AUDIO_PATHS = ("audio_out", "audio_in")
# Receiver settings a level table must match, with the backends' defaults
RECEIVER_SETTING_DEFAULTS = {"gain_db": 0.0, "lna_db": 0, "vga_db": 0,
                             "amp": False}
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


def _interp_freq(freqs_hz: np.ndarray, values: np.ndarray, freq_hz: float,
                 what: str) -> float:
    """Linear interpolation by frequency; one point is flat."""
    if len(freqs_hz) == 1:
        return float(values[0])
    if not freqs_hz[0] <= freq_hz <= freqs_hz[-1]:
        raise ValueError(
            f"{freq_hz / 1e6:.3f} MHz is outside the {what} calibrated range "
            f"{freqs_hz[0] / 1e6:.3f}-{freqs_hz[-1] / 1e6:.3f} MHz")
    return float(np.interp(freq_hz, freqs_hz, values))


@dataclass
class TransmitterLevels:
    """
    The transmitter's output, in dBm, against its gain setting, measured at
    a few frequencies with the RF amplifier in a stated state. The level is
    that of a constant-envelope signal at signals.IQ_AMPLITUDE, which every
    FM and M17 signal the bench generates is.
    """

    instrument: str
    amp: bool
    freqs_hz: np.ndarray
    gains_db: list[np.ndarray]  # per frequency, increasing
    dbm: list[np.ndarray]

    def level(self, freq_hz: float, gain_db: float) -> float:
        """Output at a gain setting and frequency, interpolated in both."""
        at_freqs = []
        for gains, dbm in zip(self.gains_db, self.dbm):
            if not gains[0] <= gain_db <= gains[-1]:
                raise ValueError(f"gain {gain_db:g} dB is outside the "
                                 f"transmitter level table "
                                 f"({gains[0]:g}-{gains[-1]:g} dB)")
            at_freqs.append(np.interp(gain_db, gains, dbm))
        return _interp_freq(self.freqs_hz, np.array(at_freqs), freq_hz,
                            "transmitter level")

    def gain_range(self) -> tuple[float, float]:
        """Gain settings every frequency's table covers."""
        return (max(g[0] for g in self.gains_db),
                min(g[-1] for g in self.gains_db))


@dataclass
class ReceiverLevels:
    """
    What a full-scale reading means for one receiver at given settings: the
    input level, in dBm, of a carrier that reads 0 dBFS, by frequency.
    dBFS is relative to a complex sinusoid at the converter's full scale.
    """

    instrument: str
    settings: dict
    freqs_hz: np.ndarray
    full_scale_dbm: np.ndarray

    def matches(self, cfg: dict) -> bool:
        """Whether a bench receiver section is this instrument, set alike."""
        if not instrument_matches(self.instrument, cfg):
            return False
        return all(cfg.get(k, RECEIVER_SETTING_DEFAULTS.get(k)) == v
                   for k, v in self.settings.items())

    def at(self, freq_hz: float) -> float:
        """Full-scale input level at a frequency."""
        return _interp_freq(self.freqs_hz, self.full_scale_dbm, freq_hz,
                            "receiver level")


def instrument_matches(name: str, cfg: dict) -> bool:
    """
    Whether a calibration's instrument name means a bench section: its type,
    or type:serial to tell two of a kind apart.
    """
    kind, _, serial = str(name).partition(":")
    if kind != cfg.get("type"):
        return False
    return not serial or serial == str(cfg.get("serial", cfg.get("device")))


@dataclass
class Calibration:
    """A bench's measured RF paths, and how and when they were measured."""

    method: str
    measured: datetime.date
    expires: datetime.date
    paths: dict[str, PathLoss]
    sha256: str  # of the calibration file, to tell calibrations apart
    transmitter_levels: TransmitterLevels | None = None
    receiver_levels: list[ReceiverLevels] = field(default_factory=list)
    # Reference oscillator error per instrument, ppm, positive when fast
    frequency_ppm: dict[str, float] = field(default_factory=dict)
    # RMS millivolts of a full-scale sine, per audio direction
    audio_mvrms: dict[str, float] = field(default_factory=dict)

    def loss(self, path: str, freq_hz: float) -> float:
        """Loss of a named path at a frequency."""
        if path not in self.paths:
            raise KeyError(f"calibration has no '{path}' path")
        return self.paths[path].at(freq_hz)

    def frequency_correction_ppm(self, cfg: dict) -> float | None:
        """
        How far a bench instrument's reference oscillator runs fast, in ppm,
        or None when the calibration doesn't say. A receiver then reads
        carriers low by ppm * f; a transmitter puts them out high by it.
        type:serial entries win over type ones.
        """
        found = None
        for name, ppm in self.frequency_ppm.items():
            if instrument_matches(name, cfg):
                if ":" in name:
                    return float(ppm)
                found = float(ppm)
        return found

    def receiver_full_scale_dbm(self, cfg: dict, freq_hz: float) -> float:
        """
        The receiver input level that reads 0 dBFS, for a bench receiver
        section. Fails when no table matches the receiver and its settings.
        """
        for table in self.receiver_levels:
            if table.matches(cfg):
                return table.at(freq_hz)
        settings = {k: cfg[k] for k in RECEIVER_SETTING_DEFAULTS if k in cfg}
        raise LookupError(
            f"no receiver level table for a {cfg.get('type')} at {settings}: "
            f"measure one (levels.receiver) or don't ask for absolute levels")

    def transmitter_setting(self, cfg: dict, freq_hz: float, dbm: float,
                            max_backoff_db: float) -> tuple[int, float]:
        """
        The transmitter gain setting, and the digital back-off below full
        scale, that put out `dbm` at the transmitter's output. The gain is
        the lowest that reaches the level, so the back-off stays under a
        gain step; at the bottom of the range up to max_backoff_db more is
        allowed. Fails when the level is out of reach.
        """
        table = self.transmitter_levels
        if table is None:
            raise LookupError("no transmitter level table: measure one "
                              "(levels.transmitter) to transmit at a level "
                              "in dBm")
        if not instrument_matches(table.instrument, cfg):
            raise LookupError(f"the transmitter level table is for "
                              f"{table.instrument}, not this bench's "
                              f"{cfg.get('type')}")
        if bool(cfg.get("amp", False)) != table.amp:
            raise LookupError(f"the transmitter level table was measured with "
                              f"the amp {'on' if table.amp else 'off'}")
        low, high = table.gain_range()
        gains = np.arange(int(np.ceil(low)), int(np.floor(high)) + 1)
        levels = np.array([table.level(freq_hz, g) for g in gains])
        if dbm > levels[-1] + 1e-9:
            raise ValueError(
                f"{dbm:.1f} dBm is above the transmitter's {levels[-1]:.1f} "
                f"dBm at {freq_hz / 1e6:.3f} MHz: remove attenuation from "
                f"the from_transmitter path")
        reach = np.flatnonzero(levels >= dbm - 1e-9)
        gain = int(gains[reach[0]])
        backoff = float(levels[reach[0]] - dbm)
        if backoff > max_backoff_db:
            raise ValueError(
                f"{dbm:.1f} dBm is below the transmitter's {levels[0]:.1f} "
                f"dBm at {freq_hz / 1e6:.3f} MHz by more than the "
                f"{max_backoff_db:g} dB it may back off digitally: add a "
                f"step attenuator to the from_transmitter path and "
                f"recalibrate it")
        return gain, backoff

    def audio_full_scale_mvrms(self, direction: str) -> float:
        """RMS millivolts of a full-scale sine on audio_out or audio_in."""
        if direction not in self.audio_mvrms:
            raise LookupError(f"no paths.{direction} audio level: measure it "
                              f"to read audio in millivolts")
        return self.audio_mvrms[direction]

    def calibrated(self) -> list[str]:
        """Which optional sections the file has, by name, never values."""
        names = []
        if self.transmitter_levels is not None:
            names.append("levels.transmitter")
        if self.receiver_levels:
            names.append("levels.receiver")
        if self.frequency_ppm:
            names.append("frequency_correction_ppm")
        names += [f"paths.{a}" for a in AUDIO_PATHS if a in self.audio_mvrms]
        return names

    def expired(self, today: datetime.date | None = None) -> bool:
        """Whether the calibration is past its expiry date."""
        return (today or datetime.date.today()) > self.expires

    def summary(self) -> dict:
        """What results record about the calibration: no values, no names."""
        summary = {"method": self.method,
                   "measured": self.measured.isoformat(),
                   "expires": self.expires.isoformat(),
                   "sha256": self.sha256[:12]}
        if self.calibrated():
            summary["calibrated"] = self.calibrated()
        return summary


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
    specs = data.get("paths") or {}
    paths = {name: _path_loss(spec, path.parent)
             for name, spec in specs.items() if name not in AUDIO_PATHS}
    audio = {name: float(spec["mvrms_at_0dbfs"])
             for name, spec in specs.items() if name in AUDIO_PATHS}
    digest = hashlib.sha256(raw)
    for spec in specs.values():
        if "touchstone" in spec:
            digest.update((path.parent / spec["touchstone"]).read_bytes())
    levels = data.get("levels") or {}
    return Calibration(
        method, _date(data, "measured", path), _date(data, "expires", path),
        paths, digest.hexdigest(),
        transmitter_levels=_transmitter_levels(levels.get("transmitter"),
                                               path),
        receiver_levels=[_receiver_levels(t, path)
                         for t in levels.get("receiver") or []],
        frequency_ppm={str(k): float(v) for k, v in
                       (data.get("frequency_correction_ppm") or {}).items()},
        audio_mvrms=audio)


def _transmitter_levels(spec: dict | None,
                        path: Path) -> TransmitterLevels | None:
    if not spec:
        return None
    points = spec.get("points") or {}
    if not points:
        raise ValueError(f"{path}: levels.transmitter needs points")
    freqs = sorted(points, key=float)
    gains, dbm = [], []
    for f in freqs:
        table = np.array(points[f], dtype=float)
        order = np.argsort(table[:, 0])
        gains.append(table[order, 0])
        dbm.append(table[order, 1])
    return TransmitterLevels(str(spec.get("instrument", "hackrf")),
                             bool(spec.get("amp", False)),
                             np.array([float(f) for f in freqs]), gains, dbm)


def _receiver_levels(spec: dict, path: Path) -> ReceiverLevels:
    if "instrument" not in spec or "full_scale_dbm" not in spec:
        raise ValueError(f"{path}: each levels.receiver entry needs "
                         f"instrument and full_scale_dbm")
    points = np.array(spec["full_scale_dbm"], dtype=float)
    order = np.argsort(points[:, 0])
    return ReceiverLevels(str(spec["instrument"]),
                          dict(spec.get("settings") or {}),
                          points[order, 0], points[order, 1])


def _date(data: dict, key: str, path: Path) -> datetime.date:
    value = data.get(key)
    if isinstance(value, datetime.date):
        return value
    if value is None:
        raise ValueError(f"{path}: '{key}' date is required")
    return datetime.date.fromisoformat(str(value))
