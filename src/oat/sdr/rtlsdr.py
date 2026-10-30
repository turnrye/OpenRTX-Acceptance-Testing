# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""RTL-SDR dongles through rtl_sdr."""

import subprocess
import threading
import time
from pathlib import Path

from oat.iq import IqFormat
from oat.sdr.base import Capture, Receiver, require_tool, stop_process

# A stalled dongle stops writing; restart it after this long without growth
STALL_S = 3.0
POLL_S = 0.5


class RtlSdrReceiver(Receiver):
    """
    RTL-SDR recording to cu8 files. Some dongles drop off USB when a nearby
    transmitter keys up; when the file stops growing, recording restarts
    into a new segment, so a capture may hold short gaps.
    """

    # Typical RTL2832U/R820T absolute maximum RF input
    max_input_dbm = 10.0

    def __init__(self, gain_db: float = 0.0, device: int = 0):
        self.gain_db = gain_db
        self.device = device
        self._proc: subprocess.Popen | None = None
        self._segments: list[Path] = []
        self._restarts = 0
        self._stop = threading.Event()
        self._watchdog: threading.Thread | None = None
        self._args: tuple[float, float, Path] | None = None

    def _segment_path(self, base: Path, index: int) -> Path:
        if index == 0:
            return base
        return base.with_name(f"{base.stem}-{index + 1:02d}{base.suffix}")

    def _launch(self) -> None:
        center_hz, rate, base = self._args
        path = self._segment_path(base, len(self._segments))
        self._segments.append(path)
        # Sync mode (-S): in the default async mode some dongles stall when a
        # nearby transmitter keys up
        cmd = [require_tool("rtl_sdr"), "-S", "-d", str(self.device),
               "-f", str(int(center_hz)), "-s", str(int(rate)),
               "-g", str(self.gain_db), str(path)]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)

    def _watch(self) -> None:
        last_size, last_change = -1, time.monotonic()
        while not self._stop.wait(POLL_S):
            path = self._segments[-1]
            size = path.stat().st_size if path.exists() else 0
            now = time.monotonic()
            if size != last_size:
                last_size, last_change = size, now
            elif now - last_change > STALL_S:
                stop_process(self._proc)
                self._restarts += 1
                self._launch()
                last_size, last_change = -1, now

    def start(self, center_hz: float, rate: float, path: Path) -> None:
        self._args = (center_hz, rate, Path(path))
        self._segments, self._restarts = [], 0
        self._stop.clear()
        self._launch()
        self._watchdog = threading.Thread(target=self._watch, daemon=True)
        self._watchdog.start()

    def stop(self) -> Capture:
        if self._args is None:
            raise RuntimeError("receiver not started")
        self._stop.set()
        self._watchdog.join()
        stop_process(self._proc)
        center_hz, rate, _ = self._args
        self._args = None
        # A restart right before stopping may never have created its file
        written = [p for p in self._segments if p.exists()]
        return Capture(written, rate, center_hz, IqFormat.CU8, self._restarts)
