# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Interfaces every SDR backend implements."""

import shutil
import signal
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from oat.iq import IqFormat, iter_iq, read_iq


@dataclass
class Capture:
    """A finished capture: one or more IQ files recorded back to back."""

    paths: list[Path]
    rate: float
    center_hz: float
    fmt: IqFormat
    # Gaps between files, where a receiver had to restart
    restarts: int = field(default=0)

    def load(self) -> np.ndarray:
        """
        All samples, files concatenated. A segment a restarting receiver never
        got to write is skipped.
        """
        parts = [read_iq(p, self.fmt) for p in self.paths
                 if p.exists() and p.stat().st_size]
        if not parts:
            return np.zeros(0, dtype=np.complex64)
        return np.concatenate(parts)

    def blocks(self):
        """
        The samples as load() returns them, a block at a time, so a capture
        at several MS/s needn't fit in memory.
        """
        for p in self.paths:
            if p.exists() and p.stat().st_size:
                yield from iter_iq(p, self.fmt)


class Receiver(ABC):
    """An SDR recording IQ samples to files."""

    # Highest safe input level, in dBm, before the receiver is damaged
    max_input_dbm: float = 0.0

    @abstractmethod
    def start(self, center_hz: float, rate: float, path: Path) -> None:
        """Start recording into path (and further segments next to it)."""

    @abstractmethod
    def stop(self) -> Capture:
        """Stop recording and return the capture."""


class Transmitter(ABC):
    """An SDR playing IQ samples."""

    @abstractmethod
    def start(self, path: Path, center_hz: float, rate: float,
              repeat: bool = True, gain_offset_db: float = 0.0) -> float:
        """
        Start playing the IQ file at path, looping if repeat, with the gain
        raised by gain_offset_db over the bench setting. Returns the offset
        actually applied, which the transmitter's range may limit.
        """

    @abstractmethod
    def stop(self) -> None:
        """Stop playing."""

    @property
    @abstractmethod
    def fmt(self) -> IqFormat:
        """IQ file format this transmitter plays."""


def require_tool(name: str) -> str:
    """Path of a command line tool, or a clear error if it isn't installed."""
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{name} not found; install it and retry")
    return path


def stop_process(proc: subprocess.Popen, timeout_s: float = 5.0) -> None:
    """Stop an SDR tool the way Ctrl-C does, so it closes the device."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
