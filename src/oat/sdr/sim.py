# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Backends without hardware, for self-tests and for replaying recordings."""

import shutil
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
from scipy import signal

from oat.iq import IqFormat, read_iq, write_iq
from oat.m17 import codec, phy, tools
from oat.sdr.base import Capture, Receiver, Transmitter
from oat.signals import fm_tone, silence

# Receiver noise relative to the simulated carrier
SIM_NOISE = 0.01


class SimClock:
    """
    Time on the simulated bench: waiting advances it at once, so a
    loopback of the simulated transmitter into the simulated receiver can
    be timed like a real one without taking the time.
    """

    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        """Let time pass."""
        self.now += float(seconds)


def loop_to_rate(iq: np.ndarray, rate_from: float, rate_to: float,
                 shift_hz: float) -> np.ndarray:
    """
    One period of a looped transmitter file, moved by shift_hz and
    resampled, wrapping around its ends so the loop stays seamless.
    """
    n = np.arange(len(iq))
    moved = iq * np.exp(2j * np.pi * shift_hz * n / rate_from)
    pad = min(len(iq), 4096)
    wrapped = np.concatenate([moved[-pad:], moved, moved[:pad]])
    up, down = int(round(rate_to)), int(round(rate_from))
    g = np.gcd(up, down)
    out = signal.resample_poly(wrapped, up // g, down // g)
    keep = int(round(pad * rate_to / rate_from))
    return out[keep:keep + int(round(len(iq) * rate_to / rate_from))]


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

    def __init__(self, gap_s: float = 1.0, seed: int = 1,
                 clock: SimClock | None = None,
                 transmitter: "SimTransmitter | None" = None):
        self.gap_s = gap_s
        self.scenes: deque[list[SimBurst]] = deque()
        self._rng = np.random.default_rng(seed)
        self._args: tuple[float, float, Path] | None = None
        self.clock = clock or SimClock()
        # With a transmitter, a capture with no scene queued holds what it
        # transmitted while the capture ran: a loopback through the bench
        self.transmitter = transmitter
        self._started = 0.0
        # Transmissions of the simulated radio, played like the transmitter's
        self.emissions: list["SimPlay"] = []

    def queue_scene(self, bursts: list[SimBurst],
                    frequency_offset_hz: float) -> None:
        """Queue the bursts the next capture holds."""
        self.scenes.append([replace(b, offset_hz=b.offset_hz
                                    + frequency_offset_hz)
                            for b in bursts])

    def start(self, center_hz: float, rate: float, path: Path) -> None:
        self._args = (center_hz, rate, Path(path))
        self._started = self.clock.now

    def stop(self) -> Capture:
        if self._args is None:
            raise RuntimeError("receiver not started")
        center_hz, rate, path = self._args
        self._args = None
        if not self.scenes and self._played(self._started, self.clock.now):
            iq = self._loopback(center_hz, rate)
        else:
            scene = self.scenes.popleft() if self.scenes else []
            parts = [silence(self.gap_s, rate)]
            for b in scene:
                parts += [b.iq(rate), silence(self.gap_s, rate)]
            iq = np.concatenate(parts)
        noise = self._rng.normal(scale=SIM_NOISE, size=(len(iq), 2))
        iq = iq + (noise[:, 0] + 1j * noise[:, 1]).astype(np.complex64)
        write_iq(path, iq, IqFormat.CU8)
        return Capture([path], rate, center_hz, IqFormat.CU8)


    def _played(self, start: float, stop: float) -> list["SimPlay"]:
        """What the transmitter and the radio sent during a span of time."""
        plays = [p for p in self.emissions
                 if p.started < stop and (p.stopped is None
                                          or p.stopped > start)]
        if self.transmitter is not None:
            plays += self.transmitter.played(start, stop)
        return plays

    def _loopback(self, center_hz: float, rate: float) -> np.ndarray:
        """What was sent while this capture ran, at its rate."""
        start, stop = self._started, self.clock.now
        out = np.zeros(int((stop - start) * rate), dtype=np.complex64)
        for play in self._played(start, stop):
            period = loop_to_rate(read_iq(play.path, IqFormat.CS8),
                                  play.rate, rate, play.center_hz - center_hz)
            a = int((max(play.started, start) - start) * rate)
            b = int((min(play.stopped if play.stopped is not None else stop,
                         stop) - start) * rate)
            n = b - a
            if n <= 0 or not len(period):
                continue
            # Where in its loop the file is when the capture first sees it
            skip = int(max(0.0, start - play.started) * rate)
            if play.repeat:
                idx = (skip + np.arange(n)) % len(period)
                out[a:b] += period[idx]
            else:
                seg = period[skip:skip + n]
                out[a:a + len(seg)] += seg
        return out


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


@dataclass
class SimPlay:
    """One file the simulated transmitter played, and when."""

    path: Path
    center_hz: float
    rate: float
    repeat: bool
    gain_offset_db: float
    started: float
    stopped: float | None = None


class SimTransmitter(Transmitter):
    """Records what it was asked to transmit, and when."""

    def __init__(self, clock: SimClock | None = None):
        self.log: list[tuple[Path, float, float, bool]] = []
        self.plays: list[SimPlay] = []
        self.active = False
        self.clock = clock or SimClock()

    @property
    def fmt(self) -> IqFormat:
        return IqFormat.CS8

    def start(self, path: Path, center_hz: float, rate: float,
              repeat: bool = True, gain_offset_db: float = 0.0) -> float:
        self.stop()
        self.log.append((Path(path), center_hz, rate, repeat))
        self.plays.append(SimPlay(Path(path), center_hz, rate, repeat,
                                  gain_offset_db, self.clock.now))
        self.active = True
        return gain_offset_db

    def stop(self) -> None:
        if self.active and self.plays:
            self.plays[-1].stopped = self.clock.now
        self.active = False

    def played(self, start: float, stop: float) -> list[SimPlay]:
        """The plays that overlap a span of time."""
        return [p for p in self.plays
                if p.started < stop and (p.stopped is None
                                         or p.stopped > start)]


class SimTnc:
    """
    The simulated radio's KISS TNC. It hears each APRS transmission the
    library sends: the AFSK audio scaled by its deviation, limited at 5 kHz,
    with noise that sets 12 dB SINAD at -118 dBm at 3 kHz deviation and
    rises 1 dB per dB below. Dire Wolf's atest decodes that audio; the TNC
    reports as many of the transmission's frames as atest decoded, in order.
    """

    RATE = 48000

    def __init__(self, clock: SimClock | None = None):
        from oat.kiss import Tnc
        self._tnc = Tnc()
        self.clock = clock or SimClock()
        self._seed = 0

    def now(self) -> float:
        return self.clock.now

    def heard_since(self, start: float):
        return self._tnc.heard_since(start)

    @property
    def junk(self) -> int:
        return 0

    def close(self) -> None:
        pass

    def hear(self, audio: np.ndarray, rate: float, frames: list[bytes],
             deviation_hz: float, level_dbm: float | None) -> int:
        """
        Hear one transmission (its audio before FM, peaking at 1); returns
        how many of its frames were decoded.
        """
        import subprocess
        import tempfile

        from scipy.io import wavfile
        g = np.gcd(int(rate), self.RATE)
        a = signal.resample_poly(audio, self.RATE // g, int(rate) // g)
        dev = np.clip(a * float(deviation_hz), -5000.0, 5000.0)
        sinad = 40.0 if level_dbm is None else min(40.0,
                                                   12 + level_dbm + 118)
        ref_rms = 3000.0 / np.sqrt(2)
        noise_rms = ref_rms / 10 ** (sinad / 20)
        self._seed += 1
        rng = np.random.default_rng(self._seed)
        noise = signal.sosfilt(signal.butter(4, (300, 3000), "bandpass",
                                             fs=self.RATE, output="sos"),
                               rng.normal(size=len(dev)))
        noise *= noise_rms / (np.sqrt(np.mean(noise ** 2)) or 1.0)
        pad = np.zeros(self.RATE // 4)
        out = np.concatenate([pad, dev + noise, pad]) / 5000.0 * 16000
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "tx.wav"
            wavfile.write(wav, self.RATE,
                          np.clip(out, -32767, 32767).astype(np.int16))
            # It echoes each frame's bytes, which needn't be UTF-8
            text = subprocess.run(["atest", "-B", "1200", str(wav)],
                                  capture_output=True,
                                  encoding="latin-1").stdout
        import re
        m = re.search(r"(\d+) packets? decoded", text)
        decoded = min(int(m.group(1)) if m else 0, len(frames))
        for frame in frames[:decoded]:
            self._tnc._add(frame, 0, self.clock.now)
        return decoded


def link_loopback(receiver: SimReceiver, transmitter: SimTransmitter) -> None:
    """Put a simulated receiver and transmitter on one clock and cable."""
    transmitter.clock = receiver.clock
    receiver.transmitter = transmitter
