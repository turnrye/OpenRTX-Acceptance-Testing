# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""HackRF One through hackrf_transfer."""

import subprocess
from pathlib import Path

from oat.iq import IqFormat
from oat.sdr.base import Capture, Receiver, Transmitter, require_tool, \
    stop_process

# hackrf_transfer's own limits
MIN_RATE = 2e6
MAX_TX_VGA_DB = 47


class HackRFTransmitter(Transmitter):
    """
    HackRF transmitting an IQ file. The RF amplifier stays off unless asked
    for: with it, the HackRF can put out over 10 dBm.
    """

    def __init__(self, tx_vga_db: int = 0, amp: bool = False,
                 serial: str | None = None):
        if not 0 <= tx_vga_db <= MAX_TX_VGA_DB:
            raise ValueError(f"tx_vga_db must be 0 to {MAX_TX_VGA_DB}")
        self.tx_vga_db = tx_vga_db
        self.amp = amp
        self.serial = serial
        self._proc: subprocess.Popen | None = None

    @property
    def fmt(self) -> IqFormat:
        return IqFormat.CS8

    def start(self, path: Path, center_hz: float, rate: float,
              repeat: bool = True, gain_offset_db: float = 0.0) -> float:
        if rate < MIN_RATE:
            raise ValueError(f"HackRF rate must be at least {MIN_RATE:g}")
        vga = int(round(min(MAX_TX_VGA_DB, self.tx_vga_db + gain_offset_db)))
        cmd = [require_tool("hackrf_transfer"), "-t", str(path),
               "-f", str(int(center_hz)), "-s", str(int(rate)),
               "-x", str(vga), "-a", "1" if self.amp else "0"]
        if repeat:
            cmd.append("-R")
        if self.serial:
            cmd += ["-d", self.serial]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE)
        return float(vga - self.tx_vga_db)

    def stop(self) -> None:
        if self._proc is not None:
            stop_process(self._proc)
            self._proc = None


class HackRFReceiver(Receiver):
    """
    HackRF recording to a cs8 file. Gains start at their minimum: a radio
    through a bench's attenuators still arrives far above what an 8-bit
    converter needs.
    """

    # HackRF One's documented maximum RF input; more can damage it
    max_input_dbm = -5.0

    def __init__(self, lna_db: int = 0, vga_db: int = 0, amp: bool = False,
                 serial: str | None = None):
        self.lna_db = lna_db
        self.vga_db = vga_db
        self.amp = amp
        self.serial = serial
        self._proc: subprocess.Popen | None = None
        self._capture: Capture | None = None

    def start(self, center_hz: float, rate: float, path: Path) -> None:
        if rate < MIN_RATE:
            raise ValueError(f"HackRF rate must be at least {MIN_RATE:g}")
        cmd = [require_tool("hackrf_transfer"), "-r", str(path),
               "-f", str(int(center_hz)), "-s", str(int(rate)),
               "-l", str(self.lna_db), "-g", str(self.vga_db),
               "-a", "1" if self.amp else "0"]
        if self.serial:
            cmd += ["-d", self.serial]
        self._capture = Capture([Path(path)], rate, center_hz, IqFormat.CS8)
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE)

    def stop(self) -> Capture:
        if self._proc is None or self._capture is None:
            raise RuntimeError("receiver not started")
        stop_process(self._proc)
        self._proc = None
        return self._capture
