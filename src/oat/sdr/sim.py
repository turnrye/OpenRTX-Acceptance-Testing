# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Backends without hardware, for self-tests and for replaying recordings."""

import shutil
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from oat.iq import IqFormat, write_iq
from oat.m17 import codec, phy, tools
from oat.sdr.base import Capture, Receiver, Transmitter
from oat.signals import fm_tone, silence

# Receiver noise relative to the simulated carrier
SIM_NOISE = 0.01


@dataclass
class SimBurst:
    """One simulated FM transmission."""

    deviation_hz: float
    tone_hz: float = 1000.0
    duration_s: float = 3.0
    offset_hz: float = 0.0  # from the frequency under test
    ctcss_hz: float = 0.0
    ctcss_deviation_hz: float = 0.0
    level_db: float = 0.0  # relative to the usual simulated carrier
    # An M17 voice stream instead of FM: deviation_hz is then the outer
    # deviation, and this holds source, frames, inverted, preamble_symbols
    # and inner_deviation_hz, each optional
    m17: dict | None = None

    def iq(self, rate: float) -> np.ndarray:
        """The burst's samples at a rate."""
        gain = 10 ** (self.level_db / 20)
        if self.m17 is None:
            return gain * fm_tone(self.tone_hz, self.deviation_hz,
                                  self.duration_s, rate, self.offset_hz,
                                  self.ctcss_hz, self.ctcss_deviation_hz)
        m = self.m17
        symbols = codec.stream_symbols(
            codec.Lsf(source=m.get("source", "N0CALL")),
            tools.random_payloads(int(m.get("frames", 25))),
            int(m.get("preamble_symbols", codec.SYMBOLS_PER_FRAME)))
        return gain * phy.m17_iq(symbols, rate, self.deviation_hz,
                                 self.offset_hz, bool(m.get("inverted")),
                                 m.get("inner_deviation_hz"))


class SimReceiver(Receiver):
    """
    Writes a synthetic capture holding the next queued scene: the bursts a
    radio would transmit, separated by gaps, with receiver noise.
    """

    max_input_dbm = 10.0

    def __init__(self, gap_s: float = 1.0, seed: int = 1):
        self.gap_s = gap_s
        self.scenes: deque[list[SimBurst]] = deque()
        self._rng = np.random.default_rng(seed)
        self._args: tuple[float, float, Path] | None = None

    def queue_scene(self, bursts: list[SimBurst],
                    frequency_offset_hz: float) -> None:
        """Queue the bursts the next capture holds."""
        self.scenes.append([replace(b, offset_hz=b.offset_hz
                                    + frequency_offset_hz)
                            for b in bursts])

    def start(self, center_hz: float, rate: float, path: Path) -> None:
        self._args = (center_hz, rate, Path(path))

    def stop(self) -> Capture:
        if self._args is None:
            raise RuntimeError("receiver not started")
        center_hz, rate, path = self._args
        self._args = None
        scene = self.scenes.popleft() if self.scenes else []
        parts = [silence(self.gap_s, rate)]
        for b in scene:
            parts += [b.iq(rate), silence(self.gap_s, rate)]
        iq = np.concatenate(parts)
        noise = self._rng.normal(scale=SIM_NOISE, size=(len(iq), 2))
        iq = iq + (noise[:, 0] + 1j * noise[:, 1]).astype(np.complex64)
        write_iq(path, iq, IqFormat.CU8)
        return Capture([path], rate, center_hz, IqFormat.CU8)


class ReplayReceiver(Receiver):
    """Returns an existing recording as the capture, for re-analysis."""

    max_input_dbm = 10.0

    def __init__(self, recording: Path, fmt: IqFormat = IqFormat.CU8):
        self.recording = Path(recording)
        self.fmt = fmt
        self._args: tuple[float, float, Path] | None = None

    def start(self, center_hz: float, rate: float, path: Path) -> None:
        self._args = (center_hz, rate, Path(path))

    def stop(self) -> Capture:
        if self._args is None:
            raise RuntimeError("receiver not started")
        center_hz, rate, path = self._args
        self._args = None
        shutil.copyfile(self.recording, path)
        return Capture([path], rate, center_hz, self.fmt)


class SimTransmitter(Transmitter):
    """Records what it was asked to transmit."""

    def __init__(self):
        self.log: list[tuple[Path, float, float, bool]] = []
        self.active = False

    @property
    def fmt(self) -> IqFormat:
        return IqFormat.CS8

    def start(self, path: Path, center_hz: float, rate: float,
              repeat: bool = True, gain_offset_db: float = 0.0) -> float:
        self.log.append((Path(path), center_hz, rate, repeat))
        self.active = True
        return gain_offset_db

    def stop(self) -> None:
        self.active = False
