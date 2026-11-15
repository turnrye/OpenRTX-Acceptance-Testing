# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Drive the OpenRTX Linux emulator as a microphone-to-M17 test bench.

The emulator (OpenRTX `openrtx_linux`, built with the Linux microphone input:
OPENRTX_MIC_FILE / OPENRTX_M17_TX_BASEBAND) runs the firmware's real M17
encode path: microphone stream, encodeFunc's decimator, DC block and gain,
codec2, the M17 modulator. This module feeds it a microphone file shaped like
a given radio's ADC, keys a transmission from the emulator shell, and decodes
the baseband it writes with OAT's own M17 decoder and c2dec.

What the emulator cannot model is the analog front end (microphone, preamp,
the ADC's own noise and response). `adc_shape` stands in for it: speech scaled
to a chosen level of the ADC's range, quantised to its bit depth around its
mid-scale bias, with white noise in LSBs.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from math import gcd
from pathlib import Path

import numpy as np
from scipy import signal

from oat.m17 import phy, tools

CODEC2_RATE = 8000
FRAME_SAMPLES = 1920          # one 40 ms M17 frame at 48 kHz
FRAMES_OUTSIDE_STREAM = 3     # preamble, LSF and EOT around the stream frames


@dataclass
class Profile:
    """The microphone chain settings of one radio, as its hwconfig.h has them."""
    name: str
    oversample: int = 1
    gain: int = 1
    adc_bits: int = 12
    noise_lsb: float = 0.5

    def c_args(self) -> str:
        return (f"-DCONFIG_MIC_GAIN={self.gain} "
                f"-DCONFIG_MIC_OVERSAMPLE={self.oversample}")


PROFILES = {
    "linux": Profile("linux", 1, 1, 16, 0.0),
    "cs7000p": Profile("cs7000p", 6, 32),
    "cs7000": Profile("cs7000", 8, 32),
    "mduv3x0": Profile("mduv3x0", 8, 32),
    "md3x0": Profile("md3x0", 8, 32),
    "mod17": Profile("mod17", 1, 12),
}


def sha256(path: Path) -> str:
    """The SHA-256 of a file, hex."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def provenance(repo: Path) -> dict:
    """The exact OpenRTX source a build came from, with any uncommitted diff."""
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    return {
        "sha": git("rev-parse", "HEAD"),
        "describe": git("describe", "--always", "--dirty", "--tags"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "diff": git("diff", "HEAD"),
    }


def build(repo: Path, build_dir: Path, profile: Profile,
          extra_c_args: tuple[str, ...] | list[str] = ()) -> Path:
    """
    Configure and build openrtx_linux for a profile, with firmware codec2 and
    any extra C arguments (a variant's defines). An existing build directory is
    reconfigured only when its C arguments differ; ninja rebuilds only what
    changed.
    """
    build_dir = Path(build_dir)
    c_args = " ".join([profile.c_args(), *extra_c_args])
    if not (build_dir / "build.ninja").exists():
        subprocess.run(["meson", "setup", str(build_dir), "-Dsystem_codec2=false",
                        f"-Dc_args={c_args}"],
                       cwd=repo, check=True, capture_output=True)
    elif configured_c_args(build_dir) != c_args.split():
        subprocess.run(["meson", "configure", str(build_dir), f"-Dc_args={c_args}"],
                       cwd=repo, check=True, capture_output=True)
    subprocess.run(["meson", "compile", "-C", str(build_dir), "openrtx_linux"],
                   cwd=repo, check=True, capture_output=True)
    return build_dir / "openrtx_linux"


def configured_c_args(build_dir: Path) -> list[str]:
    """The c_args a meson build directory is configured with."""
    info = Path(build_dir) / "meson-info" / "intro-buildoptions.json"
    for opt in json.loads(info.read_text()):
        if opt["name"] == "c_args":
            return list(opt["value"])
    return []


def adc_shape(speech: np.ndarray, rate: int, profile: Profile,
              peak_dbfs: float, seed: int = 17) -> np.ndarray:
    """
    Speech (float, any scale) as the radio's ADC would deliver it to
    encodeFunc: at 8 kHz times the oversampling ratio, its peak at peak_dbfs of
    the ADC's half range, quantised around mid-scale, as int16 samples.
    """
    x = to_adc_rate(speech, rate, profile)
    x = x / (np.max(np.abs(x)) + 1e-12) * 10 ** (peak_dbfs / 20)
    return adc_quantize(x, profile, seed)


def to_adc_rate(x: np.ndarray, rate: int, profile: Profile) -> np.ndarray:
    """Resample float audio to the ADC's rate, 8 kHz times the oversampling."""
    out_rate = CODEC2_RATE * profile.oversample
    g = gcd(rate, out_rate)
    return signal.resample_poly(np.asarray(x, dtype=float), out_rate // g, rate // g)


def adc_quantize(x: np.ndarray, profile: Profile, seed: int = 17) -> np.ndarray:
    """
    Float samples at the ADC's rate, 1.0 meaning its half range, as the ADC
    delivers them: white noise of noise_lsb RMS added, rounded, biased to
    mid-scale and clipped to the converter's range, as int16.
    """
    half = 2 ** (profile.adc_bits - 1)
    x = np.asarray(x, dtype=float) * half
    rng = np.random.default_rng(seed)
    x = x + rng.normal(0, profile.noise_lsb, len(x))
    if profile.adc_bits >= 16:
        return np.clip(np.round(x), -32768, 32767).astype(np.int16)
    return np.clip(np.round(x + half), 0, 2 * half - 1).astype(np.int16)


@dataclass
class TxRun:
    baseband: Path
    log: str
    overs: list = field(default_factory=list)


def transmit(binary: Path, mic: np.ndarray, rate: int, workdir: Path,
             key_s: float | None = None, timeout_s: float = 120,
             overs: int = 1, gap_ms: int = 1500) -> TxRun:
    """
    Key M17 transmissions in a fresh emulator: switch the VFO to M17
    (macro MONI+5), PTT for the length of the microphone file (or key_s),
    overs times with gap_ms between them, then quit. The microphone file
    plays from its start at every key-up. Returns the TX baseband file.
    """
    workdir = Path(workdir)
    (workdir / "state").mkdir(parents=True, exist_ok=True)
    mic_path = workdir / "mic.raw"
    mic.astype("<i2").tofile(mic_path)
    tx_path = workdir / "tx.raw"
    tx_path.unlink(missing_ok=True)
    key_ms = int(1000 * (key_s if key_s is not None else len(mic) / rate))
    lines = ["sleep 1500", "keycombo MONI 5", "sleep 800"]
    for i in range(overs):
        if i:
            lines.append(f"sleep {gap_ms}")
        lines += ["ptt", f"sleep {key_ms}", "ptt"]
    script = "\n".join([*lines, "sleep 1500", "quit", ""])
    env = dict(os.environ, XDG_STATE_HOME=str(workdir / "state"),
               SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy",
               OPENRTX_MIC_FILE=str(mic_path),
               OPENRTX_M17_TX_BASEBAND=str(tx_path))
    proc = subprocess.run([str(binary)], input=script, text=True, env=env,
                          cwd=workdir, capture_output=True,
                          timeout=timeout_s + overs * key_ms / 1000)
    return TxRun(tx_path, proc.stdout + proc.stderr)


def _iq_from_baseband(bb: np.ndarray) -> tuple[np.ndarray, int]:
    """FM-modulate DAC-scale emulator baseband to IQ at nominal deviation."""
    bb = bb.astype(float) - np.median(bb)
    dev = bb / (np.percentile(np.abs(bb), 99) + 1e-12) * phy.NOMINAL_OUTER_HZ
    rate = phy.BASEBAND_RATE
    pad = np.zeros(int(0.05 * rate))
    iq = np.exp(2j * np.pi * np.cumsum(np.concatenate([pad, dev, pad])) / rate)
    iq[:len(pad)] *= 1e-3
    iq[-len(pad):] *= 1e-3
    return iq, len(pad)


def decode(baseband: Path) -> list:
    """Every transmission in an emulator TX baseband file, in order."""
    raw = np.fromfile(baseband, dtype="<i2")
    overs, i = [], 0
    while len(raw) - i > 4 * FRAME_SAMPLES:
        iq, pad = _iq_from_baseband(raw[i:])
        m = phy.measure_m17(iq, phy.BASEBAND_RATE, 0, len(iq), 0.0, lead=pad)
        if m.stream_frames == 0:
            break
        overs.append(m)
        i += (m.stream_frames + FRAMES_OUTSIDE_STREAM) * FRAME_SAMPLES
    return overs


def voice(m) -> np.ndarray:
    """A decoded transmission's audio, float at 8 kHz."""
    return tools.codec2_decode(m.payload).astype(float) / 32768


def chain_clip_fraction(mic: np.ndarray, profile: Profile) -> float:
    """
    The fraction of samples encodeFunc saturates before codec2 for this
    microphone input: decimate by averaging, remove DC, multiply by the gain,
    count the ones past int16 full scale. (The DC block's start-up transient
    is excluded by removing the mean instead.)
    """
    n = len(mic) // profile.oversample * profile.oversample
    x = mic[:n].astype(float).reshape(-1, profile.oversample).mean(axis=1)
    x = (x - x.mean()) * profile.gain
    return float(np.mean(np.abs(x) >= 32767))


def codec_only(speech8k: np.ndarray) -> np.ndarray:
    """Reference: the same speech straight through codec2 3200, float."""
    pcm = np.clip(np.round(speech8k * 32767), -32768, 32767).astype(np.int16)
    return tools.codec2_decode(tools.codec2_encode(pcm)).astype(float) / 32768


def db(x: float) -> float:
    return float(20 * np.log10(x + 1e-12))


def levels(x: np.ndarray, rate: int = CODEC2_RATE, skip_s: float = 0.0) -> dict:
    """
    Level statistics of 8 kHz float audio: active speech level (20 ms frames
    within 30 dB of the loudest, above -60 dBFS; a simplified ITU-T P.56),
    peak, clipped fraction and the level of the quietest tenth of frames.
    The first skip_s seconds (a key-up transient) are reported apart as
    onset_peak_dbfs and left out of the rest.
    """
    k = int(skip_s * rate)
    onset = db(float(np.max(np.abs(x[:k])))) if k else None
    x = x[k:]
    n = rate // 50
    frames = x[: len(x) // n * n].reshape(-1, n)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    rms_db = 20 * np.log10(rms + 1e-12)
    active = (rms_db > max(rms_db.max() - 30, -60))
    act = float(np.sqrt(np.mean(frames[active] ** 2))) if active.any() else 0.0
    return {
        "active_dbfs": db(act),
        "peak_dbfs": db(float(np.max(np.abs(x)))),
        "clip_fraction": float(np.mean(np.abs(x) >= 32766 / 32768)),
        "floor_dbfs": float(np.percentile(rms_db, 10)),
        "active_fraction": float(np.mean(active)),
        "onset_peak_dbfs": onset,
    }


def _envelope(x: np.ndarray, hop: int) -> np.ndarray:
    n = len(x) // hop * hop
    e = 10 * np.log10(np.mean(x[:n].reshape(-1, hop) ** 2, axis=1) + 1e-10)
    return np.maximum(e, e.max() - 40) - np.mean(np.maximum(e, e.max() - 40))


def find_lag(ref: np.ndarray, x: np.ndarray, max_lag_s: float = 0.5,
             rate: int = CODEC2_RATE) -> int:
    """
    The delay of x behind ref, in samples. A vocoder does not keep the
    waveform, so it comes from the cross-correlation of 2.5 ms log-energy
    envelopes, searched within +-max_lag_s.
    """
    hop = rate // 400
    er, ex = _envelope(ref, hop), _envelope(x, hop)
    m = int(max_lag_s * rate / hop)
    best, lag = -np.inf, 0
    for k in range(-m, m + 1):
        a = er[max(0, -k): len(er) - max(0, k)]
        b = ex[max(0, k): max(0, k) + len(a)]
        n = min(len(a), len(b))
        if n < 100:
            continue
        c = float(np.dot(a[:n], b[:n]) / n)
        if c > best:
            best, lag = c, k
    return lag * hop


def align(ref: np.ndarray, x: np.ndarray, max_lag_s: float = 0.5,
          rate: int = CODEC2_RATE) -> tuple[np.ndarray, np.ndarray]:
    """Trim two signals to a common alignment (see find_lag)."""
    lag = find_lag(ref, x, max_lag_s, rate)
    if lag > 0:
        x = x[lag:]
    else:
        ref = ref[-lag:]
    n = min(len(ref), len(x))
    return ref[:n], x[:n]


def stoi(clean: np.ndarray, test: np.ndarray, rate: int = CODEC2_RATE) -> float:
    """Short-time objective intelligibility of aligned 8 kHz audio."""
    from pystoi import stoi as _stoi
    a, b = align(clean, test)
    return float(_stoi(a, b, rate, extended=False))
