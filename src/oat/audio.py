# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Audio into and out of the radio, through a USB sound interface on its
accessory connector: test signals for the mic input, captures of the
speaker output, and the measurements made on them, which also apply to the
audio demodulated from a transmission.

The interface is driven through ALSA's aplay and arecord on Linux, or sox's
coreaudio driver on macOS, so no audio library is needed; a simulated one
stands in for self-tests. An AIOC-style
interface can also key the radio, through a serial port's DTR or RTS line.
"""

import glob
import os
import re
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

from oat.analysis import spectral_peak

RATE = 48000
FULL_SCALE = 32767
# Measurement band for distortion and SINAD: the radio's voice band and
# its harmonics, clear of hum below and of the interface's anti-alias
# filter above
BAND_HZ = (50.0, 15000.0)
# Half width of the notch that removes the fundamental, in Hz: wider than
# the main lobe of the Hann window over a few hundred ms of audio
NOTCH_HZ = 40.0
# Envelope smoothing for finding tone bursts and gaps
ENVELOPE_S = 0.005
# Tone bursts are found this far above the quiet between them
SEGMENT_THRESHOLD_DB = 10.0
# Settling skipped at each end of a tone burst before measuring it
SEGMENT_SETTLE_S = 0.1

# ITU-T O.41 psophometric weighting for telephone circuits, dB by frequency
PSOPHOMETRIC = np.array([
    (16.66, -85.0), (50, -63.0), (100, -41.0), (200, -21.0), (300, -10.6),
    (400, -6.3), (500, -3.6), (600, -2.0), (700, -0.9), (800, 0.0),
    (900, 0.6), (1000, 1.0), (1200, 0.0), (1400, -0.9), (1600, -1.7),
    (1800, -2.4), (2000, -3.0), (2500, -4.2), (3000, -5.6), (3500, -8.5),
    (4000, -15.0), (4500, -25.0), (5000, -36.0), (6000, -43.0)])
WEIGHTINGS = ("none", "psophometric")


# Signals ------------------------------------------------------------------

def dbfs_to_amplitude(level_dbfs: float) -> float:
    """Peak amplitude, of full scale, of a sine at a level in dBFS."""
    return 10 ** (level_dbfs / 20)


def tone(freq_hz: float, duration_s: float, level_dbfs: float = -20.0,
         rate: int = RATE) -> np.ndarray:
    """A sine whose peak is level_dbfs below full scale, as float +-1."""
    t = np.arange(int(duration_s * rate)) / rate
    return dbfs_to_amplitude(level_dbfs) * np.sin(2 * np.pi * freq_hz * t)


def tone_sequence(freqs_hz: list[float], tone_s: float = 1.0,
                  gap_s: float = 0.3, level_dbfs: float = -20.0,
                  rate: int = RATE) -> np.ndarray:
    """Tones in turn, each followed by silence, for a frequency response."""
    parts = []
    for f in freqs_hz:
        burst = tone(f, tone_s, level_dbfs, rate)
        ramp = min(len(burst) // 4, int(0.005 * rate))
        if ramp:
            fade = np.linspace(0, 1, ramp)
            burst[:ramp] *= fade
            burst[-ramp:] *= fade[::-1]
        parts += [burst, np.zeros(int(gap_s * rate))]
    return np.concatenate(parts) if parts else np.zeros(0)


def speech_like(duration_s: float, rate: int = RATE, seed: int = 7,
                level_dbfs: float = -20.0) -> np.ndarray:
    """
    Speech-shaped noise with a syllabic rhythm, repeatable from its seed: a
    stand-in for a speech recording when none is given. Noise filtered to
    the long-term speech spectrum (a 300-3400 Hz band with a 6 dB/octave
    fall above 500 Hz), switched on and off at about 4 Hz. Its RMS is
    level_dbfs below a full-scale sine.
    """
    rng = np.random.default_rng(seed)
    n = int(duration_s * rate)
    noise = rng.normal(size=n)
    band = signal.butter(4, (300, 3400), "bandpass", fs=rate, output="sos")
    tilt = signal.butter(1, 500, "lowpass", fs=rate, output="sos")
    x = signal.sosfilt(tilt, signal.sosfilt(band, noise))
    t = np.arange(n) / rate
    syllables = np.clip(np.sin(2 * np.pi * 4.0 * t
                               + 2 * np.sin(2 * np.pi * 0.7 * t)), 0, None)
    x *= syllables ** 0.5
    rms = np.sqrt(np.mean(x ** 2)) or 1.0
    return x / rms * dbfs_to_amplitude(level_dbfs) / np.sqrt(2)


def write_wav(path: Path, audio: np.ndarray, rate: int = RATE) -> Path:
    """Write float audio (+-1 full scale) as a 16-bit WAV file."""
    wavfile.write(path, rate, np.clip(np.round(audio * FULL_SCALE),
                                      -FULL_SCALE, FULL_SCALE)
                  .astype(np.int16))
    return Path(path)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """A WAV file as mono float, +-1 full scale, and its rate."""
    rate, data = wavfile.read(path)
    if data.dtype.kind == "i":
        data = data / float(np.iinfo(data.dtype).max)
    elif data.dtype == np.uint8:
        data = (data.astype(float) - 128) / 127
    data = np.asarray(data, dtype=float)
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data, int(rate)


# Measurements -------------------------------------------------------------

def rms_dbfs(x: np.ndarray) -> float:
    """RMS level relative to a full-scale sine (a sine at 0 dBFS reads 0)."""
    rms = np.sqrt(np.mean(np.asarray(x, dtype=float) ** 2))
    return float(20 * np.log10(max(rms * np.sqrt(2), 1e-12)))


def _spectrum(x: np.ndarray, rate: int):
    window = np.hanning(len(x))
    spec = np.abs(np.fft.rfft(x * window)) ** 2
    return np.fft.rfftfreq(len(x), 1 / rate), spec


def weighting_db(freqs: np.ndarray, weighting: str) -> np.ndarray:
    """A weighting curve at the given frequencies, in dB."""
    if weighting == "none":
        return np.zeros(len(freqs))
    if weighting != "psophometric":
        raise ValueError(f"weighting must be one of {', '.join(WEIGHTINGS)}")
    table = PSOPHOMETRIC
    db = np.interp(np.log10(np.maximum(freqs, 1.0)), np.log10(table[:, 0]),
                   table[:, 1], left=table[0, 1], right=table[-1, 1])
    return db


def fundamental(x: np.ndarray, rate: int,
                band: tuple[float, float] = (100.0, 10000.0)) -> float:
    """Frequency of the strongest component in a band."""
    freqs, spec = _spectrum(x, rate)
    idx = np.flatnonzero((freqs >= band[0]) & (freqs <= band[1]))
    return float(freqs[idx[np.argmax(spec[idx])]])


@dataclass
class Distortion:
    """A tone's distortion: what is left once the tone is removed."""

    tone_hz: float
    level_dbfs: float  # of the whole signal, in the band
    sinad_db: float
    thd_n_percent: float
    weighting: str


def distortion(x: np.ndarray, rate: int, tone_hz: float | None = None,
               weighting: str = "none",
               band: tuple[float, float] = BAND_HZ) -> Distortion:
    """
    SINAD and THD+N of a tone: the power in the band against the power left
    when a notch removes the tone, optionally both weighted. With no
    tone_hz, the strongest component is the tone.
    """
    x = np.asarray(x, dtype=float)
    x = x - np.mean(x)
    f0 = tone_hz or fundamental(x, rate)
    freqs, spec = _spectrum(x, rate)
    spec = spec * 10 ** (weighting_db(freqs, weighting) / 10)
    hi = min(band[1], rate / 2 * 0.95)
    inband = (freqs >= band[0]) & (freqs <= hi)
    notch = np.abs(freqs - f0) <= NOTCH_HZ
    total = np.sum(spec[inband])
    residual = np.sum(spec[inband & ~notch])
    residual = max(residual, total * 1e-12)
    return Distortion(
        tone_hz=float(f0),
        level_dbfs=rms_dbfs(x),
        sinad_db=float(10 * np.log10(total / residual)),
        thd_n_percent=float(100 * np.sqrt(residual / total)),
        weighting=weighting)


def tone_amplitude(x: np.ndarray, rate: int, freq_hz: float,
                   search_hz: float = 20.0) -> float:
    """Peak amplitude of the sine nearest freq_hz, from the spectrum."""
    return spectral_peak(np.asarray(x, dtype=float), rate,
                         (freq_hz - search_hz, freq_hz + search_hz))[1]


def envelope_db(x: np.ndarray, rate: int,
                smooth_s: float = ENVELOPE_S) -> np.ndarray:
    """Short-term power, in dB."""
    win = max(1, int(smooth_s * rate))
    power = np.convolve(np.asarray(x, dtype=float) ** 2, np.ones(win) / win,
                        mode="same")
    return 10 * np.log10(power + 1e-20)


@dataclass
class Segment:
    """A tone burst in a recording."""

    start_s: float
    duration_s: float
    tone_hz: float
    amplitude: float  # peak, in the recording's unit
    level_db: float = field(default=0.0)  # relative to the reference tone


def tone_segments(x: np.ndarray, rate: int, min_s: float = 0.3,
                  threshold_db: float = SEGMENT_THRESHOLD_DB
                  ) -> list[Segment]:
    """
    The tone bursts in a recording, each with its frequency and amplitude,
    found against the quiet between them. Bursts need not be in a known
    order: each one's frequency says which it is.
    """
    env = envelope_db(x, rate)
    floor = np.percentile(env, 10)
    on = (env > floor + threshold_db).astype(np.int8)
    edges = np.flatnonzero(np.diff(on)) + 1
    if on[0]:
        edges = np.r_[0, edges]
    if on[-1]:
        edges = np.r_[edges, len(on)]
    settle = int(SEGMENT_SETTLE_S * rate)
    out = []
    for a, b in zip(edges[0::2], edges[1::2]):
        if (b - a) / rate < min_s:
            continue
        seg = x[a + settle:b - settle]
        f0 = fundamental(seg, rate, (50.0, rate / 2 * 0.9))
        f0, amplitude = spectral_peak(seg, rate, (f0 - 20, f0 + 20))
        out.append(Segment(a / rate, (b - a) / rate, f0, amplitude))
    return out


def response(segments: list[Segment], reference_hz: float = 1000.0,
             tolerance_hz: float = 30.0) -> list[Segment]:
    """
    Tone levels relative to the reference tone, one per frequency (the
    median of repeats), lowest frequency first.
    """
    by_freq: dict[float, list[Segment]] = {}
    for s in segments:
        key = next((k for k in by_freq if abs(k - s.tone_hz) <= tolerance_hz),
                   s.tone_hz)
        by_freq.setdefault(key, []).append(s)
    ref = [s for k, v in by_freq.items() if abs(k - reference_hz)
           <= tolerance_hz for s in v]
    if not ref:
        raise ValueError(f"no {reference_hz:g} Hz reference tone found")
    ref_amp = float(np.median([s.amplitude for s in ref]))
    out = []
    for k in sorted(by_freq):
        group = by_freq[k]
        amp = float(np.median([s.amplitude for s in group]))
        out.append(Segment(group[0].start_s, group[0].duration_s,
                           float(np.median([s.tone_hz for s in group])), amp,
                           float(20 * np.log10(amp / ref_amp))))
    return out


def response_errors(levels: list[Segment], slope_db_per_octave: float,
                    reference_hz: float = 1000.0) -> list[float]:
    """Each tone's level less the ideal slope through the reference, dB."""
    return [s.level_db - slope_db_per_octave * np.log2(s.tone_hz
                                                        / reference_hz)
            for s in levels]


def longest_gap(x: np.ndarray, rate: int, drop_db: float = 20.0) -> float:
    """
    The longest stretch, in seconds, where a steady signal falls drop_db
    below its median level: a dropout in what should be continuous audio.
    """
    env = envelope_db(x, rate)
    low = env < np.median(env) - drop_db
    longest = run = 0
    for v in low:
        run = run + 1 if v else 0
        longest = max(longest, run)
    return longest / rate


def images(x: np.ndarray, rate: int, tone_hz: float,
           image_hz: list[float]) -> dict[float, float]:
    """Level of each image frequency relative to the tone, in dB."""
    ref = tone_amplitude(x, rate, tone_hz)
    return {f: float(20 * np.log10(max(tone_amplitude(x, rate, f), 1e-12)
                                   / max(ref, 1e-12)))
            for f in image_hz}


# Interfaces ---------------------------------------------------------------

class AudioInterface(ABC):
    """A sound interface on the radio's mic input and speaker output."""

    rate: int = RATE

    @abstractmethod
    def play(self, path: Path, repeat: bool = False) -> None:
        """Start playing a WAV file into the radio, looping if repeat."""

    @abstractmethod
    def stop_playing(self) -> None:
        """Stop playing."""

    @abstractmethod
    def record(self, path: Path, seconds: float) -> Path:
        """Record the radio's audio for some seconds into a WAV file."""

    def wait_playing(self) -> None:
        """Wait for a file played once to end."""

    _monitor: subprocess.Popen | None = None

    def _monitor_command(self) -> list[str] | None:
        """
        The command playing the capture on the monitor device, or None where
        there is nothing to play it on.
        """
        return None

    def start_monitor(self) -> None:
        """
        Play the radio's speaker, as the interface captures it, on the
        monitor device for the operator to hear: the interface's plug can
        cut the radio's own speaker.
        """
        self.stop_monitor()
        cmd = self._monitor_command()
        if cmd is not None:
            self._monitor = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)

    def stop_monitor(self) -> None:
        """Stop playing the capture on the monitor device."""
        if self._monitor is not None and self._monitor.poll() is None:
            os.killpg(self._monitor.pid, 15)
            self._monitor.wait()
        self._monitor = None


def check_monitor_device(playback_device: str, monitor_device: str) -> None:
    """
    Refuse a monitor device that is the interface's own playback device: the
    radio's speaker would play back into its mic.

    @param playback_device: the interface's device for the radio's mic.
    @param monitor_device: where the operator hears the radio.
    """
    if monitor_device == playback_device:
        raise ValueError(
            f"audio monitor_device {monitor_device!r} is the interface's own "
            "playback device, so the radio's speaker would feed its mic: "
            "name the computer's output instead")


def alsa_card(device: str) -> str | None:
    """The card of an ALSA device name: CARD=Audio, or hw:2 / plughw:2,0."""
    m = re.search(r"CARD=([^,]+)", device)
    if m:
        return m.group(1)
    m = re.match(r"(?:plug)?hw:(\d+)", device)
    return m.group(1) if m else None


def mixer_readback(text: str) -> str:
    """The levels in amixer's report of a control, e.g. 'Playback 102 [40%]'."""
    levels = re.findall(
        r"(Playback|Capture) (\d+) \[(\d+)%\](?:.*?\[(on|off)\])?", text)
    seen = []
    for kind, raw, pct, switch in levels:
        entry = f"{kind} {raw} [{pct}%]" + (f" [{switch}]" if switch else "")
        if entry not in seen:
            seen.append(entry)
    return ", ".join(seen)


class AlsaAudio(AudioInterface):
    """An ALSA sound device, through aplay and arecord."""

    def __init__(self, playback_device: str = "default",
                 capture_device: str = "default", rate: int = RATE,
                 mixer: dict | None = None, monitor_device: str = "default"):
        self.playback_device = playback_device
        self.capture_device = capture_device
        self.rate = rate
        self.mixer = dict(mixer or {})
        self.monitor_device = monitor_device
        self._proc: subprocess.Popen | None = None
        self._looping = False

    def apply_mixer(self) -> dict:
        """
        Set the interface's mixer controls from the bench file, so a desktop
        sound server or an earlier session can't leave other levels, and
        return what each control reads back. Values go to amixer as given:
        a raw step (102), a percentage ("40%") or dB ("-6dB"), followed by
        unmute (or mute) to set the control's switch too: "102 unmute". A
        desktop sound server that mutes a device it manages leaves the
        switch off, which silences the interface whatever the level.
        """
        if not self.mixer:
            return {}
        card = alsa_card(self.capture_device) or alsa_card(
            self.playback_device)
        if card is None:
            raise ValueError("audio mixer needs a device naming its card, "
                             "such as plughw:CARD=Audio,DEV=0")
        applied = {}
        for control, value in self.mixer.items():
            proc = subprocess.run(
                [self._tool("amixer"), "-c", card, "sset", str(control),
                 *str(value).split()], capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"amixer couldn't set {control!r} on card "
                                   f"{card}: {proc.stderr.strip()}")
            applied[str(control)] = mixer_readback(proc.stdout) or str(value)
        return applied

    @staticmethod
    def _tool(name: str) -> str:
        path = shutil.which(name)
        if path is None:
            raise RuntimeError(f"{name} not found; install alsa-utils")
        return path

    def play(self, path: Path, repeat: bool = False) -> None:
        self.stop_playing()
        if repeat:
            # aplay has no loop option: a shell loop restarts it
            cmd = ["sh", "-c", 'while :; do "$0" -q -D "$1" "$2" || exit; '
                   'done', self._tool("aplay"), self.playback_device,
                   str(path)]
        else:
            cmd = [self._tool("aplay"), "-q", "-D", self.playback_device,
                   str(path)]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE,
                                      start_new_session=True)

    def wait_playing(self) -> None:
        if self._proc is not None:
            self._proc.wait()

    def stop_playing(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            os.killpg(self._proc.pid, 15)
            self._proc.wait()
        self._proc = None

    def _monitor_command(self) -> list[str]:
        check_monitor_device(self.playback_device, self.monitor_device)
        return ["sh", "-c", '"$0" -q -D "$1" -f S16_LE -c 1 -r "$2" | '
                '"$3" -q -D "$4"', self._tool("arecord"), self.capture_device,
                str(self.rate), self._tool("aplay"), self.monitor_device]

    def record(self, path: Path, seconds: float) -> Path:
        subprocess.run([self._tool("arecord"), "-q", "-D",
                        self.capture_device, "-f", "S16_LE", "-c", "1",
                        "-r", str(self.rate), "-d",
                        str(max(1, int(np.ceil(float(seconds))))),
                        str(path)], check=True)
        return Path(path)


class CoreAudio(AudioInterface):
    """
    A macOS sound device, through sox's coreaudio driver. Devices are named
    as sox lists them (`sox -V6 -n -t coreaudio x`), such as "USB Audio
    Device" for a Digirig, or "default". macOS has no amixer: the levels
    are the device's own, set in Audio MIDI Setup.
    """

    def __init__(self, playback_device: str = "default",
                 capture_device: str = "default", rate: int = RATE,
                 monitor_device: str = "default"):
        self.playback_device = playback_device
        self.capture_device = capture_device
        self.rate = rate
        self.monitor_device = monitor_device
        self._proc: subprocess.Popen | None = None

    @staticmethod
    def _sox() -> str:
        path = shutil.which("sox")
        if path is None:
            raise RuntimeError("sox not found; install it (brew install sox)")
        return path

    def play(self, path: Path, repeat: bool = False) -> None:
        self.stop_playing()
        cmd = [self._sox(), "-q", str(path), "-t", "coreaudio",
               self.playback_device] + (["repeat", "-"] if repeat else [])
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE)

    def wait_playing(self) -> None:
        if self._proc is not None:
            self._proc.wait()

    def stop_playing(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            self._proc.wait()
        self._proc = None

    def _monitor_command(self) -> list[str]:
        check_monitor_device(self.playback_device, self.monitor_device)
        return [self._sox(), "-q", "-t", "coreaudio", self.capture_device,
                "-t", "coreaudio", self.monitor_device]

    def record(self, path: Path, seconds: float) -> Path:
        subprocess.run([self._sox(), "-q", "-t", "coreaudio",
                        self.capture_device, "-c", "1", "-r", str(self.rate),
                        "-b", "16", str(path), "trim", "0",
                        str(float(seconds))], check=True)
        return Path(path)


class SimAudio(AudioInterface):
    """
    A simulated interface and radio. Its speaker plays what `source`
    returns for each recording, a function of the number of samples and the
    rate; without one, or when it returns None, the interface's output
    loops back to its input.
    """

    def __init__(self, rate: int = RATE, seed: int = 3):
        self.rate = rate
        self.playing: Path | None = None
        self.repeat = False
        self.log: list[tuple[Path, bool]] = []
        self.source = None
        self._rng = np.random.default_rng(seed)

    def play(self, path: Path, repeat: bool = False) -> None:
        self.playing, self.repeat = Path(path), repeat
        self.log.append((Path(path), repeat))

    def stop_playing(self) -> None:
        self.playing = None

    def record(self, path: Path, seconds: float) -> Path:
        n = int(float(seconds) * self.rate)
        audio = self.source(n, self.rate) if self.source else None
        if audio is not None:
            pass
        elif self.playing is not None:
            played, rate = read_wav(self.playing)
            g = np.gcd(rate, self.rate)
            played = signal.resample_poly(played, self.rate // g, rate // g)
            audio = (np.resize(played, n) if self.repeat
                     else np.pad(played[:n], (0, max(0, n - len(played)))))
        else:
            audio = np.zeros(n)
        audio = audio + self._rng.normal(scale=1e-4, size=n)
        return write_wav(path, audio, self.rate)


class SerialPtt:
    """
    Keys the radio through a serial port's modem control line: DTR on an AIOC
    by default, RTS on a Digirig Mobile. Linux raises both lines when a port
    opens, so opening puts the keying line at rest and holds the other low.
    Uses the termios ioctls directly, so no serial library is needed.
    """

    def __init__(self, device: str, line: str = "dtr",
                 active_high: bool = True):
        import fcntl
        import termios
        self._fcntl, self._termios = fcntl, termios
        bits = {"dtr": termios.TIOCM_DTR, "rts": termios.TIOCM_RTS}
        if line not in bits:
            raise ValueError("ptt line must be dtr or rts")
        # The port opens at the first key-up, so a wrong one is caught here,
        # before a run has started, without touching the lines
        if not os.access(device, os.R_OK | os.W_OK):
            raise ValueError(f"PTT port {device} is missing or not writable "
                             "(is the user in the dialout group?)")
        self.device, self.bit, self.active_high = device, bits[line], \
            active_high
        self.other_bit = bits["rts" if line == "dtr" else "dtr"]
        self._fd: int | None = None

    def _ioctl(self, request: int, bit: int) -> None:
        import struct
        self._fcntl.ioctl(self._fd, request, struct.pack("I", bit))

    def _open(self) -> None:
        if self._fd is None:
            self._fd = os.open(self.device,
                               os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            self._ioctl(self._termios.TIOCMBIC, self.other_bit)
            self._ioctl(self._request(False), self.bit)

    def _set(self, on: bool) -> None:
        try:
            self._open()
            self._ioctl(self._request(on), self.bit)
        except OSError:
            # The interface dropped off USB, as RF on its cable can make it
            # do, and came back: open it again, once
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
            self._open()
            self._ioctl(self._request(on), self.bit)

    def _request(self, on: bool) -> int:
        return (self._termios.TIOCMBIS if on == self.active_high
                else self._termios.TIOCMBIC)

    def key(self) -> None:
        """Key the transmitter."""
        self._set(True)
        # Let the radio's transmitter come up before audio matters
        time.sleep(0.2)

    def unkey(self) -> None:
        """Release the transmitter; nothing to do if it was never keyed."""
        if self._fd is not None:
            self._set(False)

    def close(self) -> None:
        """Release, then close the port."""
        if self._fd is not None:
            try:
                self.unkey()
            finally:
                # A port that dropped off USB released its lines with it
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None


def make_audio(cfg: dict | None) -> AudioInterface | None:
    """Audio interface from a bench 'audio' section, or None without one."""
    if not cfg:
        return None
    kind = cfg.get("type", "alsa")
    if kind == "alsa":
        return AlsaAudio(cfg.get("playback_device", "default"),
                         cfg.get("capture_device", "default"),
                         int(cfg.get("rate", RATE)), cfg.get("mixer"),
                         cfg.get("monitor_device", "default"))
    if kind == "coreaudio":
        if cfg.get("mixer"):
            raise ValueError("audio mixer needs ALSA; on macOS set the levels "
                             "in Audio MIDI Setup and leave mixer out")
        return CoreAudio(cfg.get("playback_device", "default"),
                         cfg.get("capture_device", "default"),
                         int(cfg.get("rate", RATE)),
                         cfg.get("monitor_device", "default"))
    if kind == "sim":
        return SimAudio(int(cfg.get("rate", RATE)))
    raise ValueError(f"unknown audio type {kind!r}")


# Where a PTT line's serial port can be: Linux USB serial devices, and the
# macOS call-out devices other than the built-in ones. macOS names a USB
# adapter by where it's plugged in, so the name changes with the port.
SERIAL_PORT_GLOBS = ("/dev/serial/by-id/*", "/dev/ttyUSB*", "/dev/ttyACM*",
                     "/dev/cu.*")
BUILT_IN_PORTS = ("Bluetooth", "debug-console")
# How many answers the operator gets to name a port
PTT_PORT_ASKS = 3
# The ports chosen this run, by the bench file's name for them ("" for none),
# so the operator is asked once
_chosen_ports: dict[str, str] = {}


def serial_ports() -> list[str]:
    """The serial ports present that a PTT line could be on."""
    found = []
    for pattern in SERIAL_PORT_GLOBS:
        for path in sorted(glob.glob(pattern)):
            if path not in found and not any(b in path
                                             for b in BUILT_IN_PORTS):
                found.append(path)
    return found


def _usable(path: str) -> bool:
    return bool(path) and os.access(path, os.R_OK | os.W_OK)


def choose_ptt_port(device: str | None, operator) -> str:
    """
    The serial port that keys the radio: the bench file's device when it's
    present, otherwise one the operator picks from the ports present (Enter
    takes the only one), asked once a run. Raises ValueError when no port is
    present or the answers name none.
    """
    if _usable(device):
        return device
    key = device or ""
    if key in _chosen_ports:
        return _chosen_ports[key]
    what = (f"PTT port {device} is missing" if device
            else "The bench file names no PTT port")
    for _ in range(PTT_PORT_ASKS):
        ports = serial_ports()
        if not ports:
            raise ValueError(f"{what}, and no serial ports are present: plug "
                             "the audio interface in")
        only = ports[0] if len(ports) == 1 else ""
        answer = operator.ask(
            f"{what}. Serial ports present: {', '.join(ports)}. Which one "
            "keys the radio?" + (f" (Enter for {only})" if only else ""))
        answer = answer or only
        if _usable(answer):
            _chosen_ports[key] = answer
            return answer
    raise ValueError(f"{what}, and no answer named a usable port")


def make_ptt(cfg: dict | None, operator=None) -> SerialPtt | None:
    """
    PTT from a bench audio section's 'ptt', or None for the operator. With
    an operator, a missing or unnamed device is chosen by choose_ptt_port.
    """
    if not cfg or not cfg.get("ptt"):
        return None
    ptt = cfg["ptt"]
    device = ptt.get("device")
    if operator is not None:
        device = choose_ptt_port(device, operator)
    return SerialPtt(device, ptt.get("line", "dtr"),
                     ptt.get("active_high", True))
