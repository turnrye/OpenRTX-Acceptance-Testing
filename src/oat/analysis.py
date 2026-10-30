# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Measurements on captured FM transmissions."""

from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

# Working rate for analysis: wide enough for a 25 kHz channel and its
# sidebands, small enough to keep minute-long captures fast
ANALYSIS_RATE = 250e3
# Smoothing window of the power envelope used to find transmissions
ENVELOPE_S = 1e-3
# Key-up and key-down transients skipped at each end of a transmission
SETTLE_S = 0.3
# One-sided channel filter for demodulation, above a 25 kHz FM channel
CHANNEL_HZ = 12e3
# Audio band for deviation: voice and CTCSS, without demodulator noise
AUDIO_LPF_HZ = 5e3
AUDIO_RATE = 48000
# Search band for the dominant audio tone, above CTCSS
TONE_BAND_HZ = (300.0, 5000.0)
# Weakest audio tone reported, as deviation; below it the band is noise
MIN_TONE_HZ = 50.0
# Sub-audible band holding every standard CTCSS tone, 67.0 to 254.1 Hz
CTCSS_BAND_HZ = (60.0, 260.0)
# Span searched for occupied bandwidth around the carrier
OBW_SPAN_HZ = 50e3
OBW_FRACTION = 0.99


@dataclass
class FmMeasurement:
    """Measurements of one FM transmission."""

    start_s: float
    duration_s: float
    carrier_offset_hz: float  # from the capture centre
    peak_deviation_hz: float  # 99.9th percentile of |deviation|
    max_deviation_hz: float
    rms_deviation_hz: float
    tone_hz: float  # dominant audio tone
    ctcss_hz: float  # strongest sub-audible tone
    ctcss_deviation_hz: float  # its peak deviation
    occupied_bandwidth_hz: float  # 99% power
    power_db: float  # relative to converter full scale

    def as_dict(self) -> dict:
        """Plain dictionary, for reports."""
        return asdict(self)


def to_analysis_rate(iq: np.ndarray, rate: float,
                     offset_hz: float) -> tuple[np.ndarray, float, float]:
    """
    Decimate a capture to about ANALYSIS_RATE, first moving a signal at
    offset_hz to the centre so decimation keeps it. Returns the samples, their
    rate and the signal's offset in them.
    """
    factor = int(rate // ANALYSIS_RATE)
    if factor <= 1:
        return iq, rate, offset_hz
    n = np.arange(len(iq))
    centred = iq * np.exp(-2j * np.pi * offset_hz * n / rate)
    return (signal.resample_poly(centred, 1, factor).astype(np.complex64),
            rate / factor, 0.0)


def find_transmissions(iq: np.ndarray, rate: float,
                       min_duration_s: float = 1.0) -> list[tuple[int, int]]:
    """
    Sample ranges of the carriers in a capture.

    The threshold sits halfway, in dB, between the noise floor and the
    strongest carrier, so it adapts to the attenuation in the chain.
    """
    win = max(1, int(ENVELOPE_S * rate))
    power = np.convolve(np.abs(iq) ** 2, np.ones(win) / win, mode="same")
    floor = np.percentile(power, 10)
    peak = np.percentile(power, 99.5)
    if peak < 100 * floor:  # less than 20 dB above the floor: no carrier
        return []
    on = (power > np.sqrt(floor * peak)).astype(np.int8)
    edges = np.flatnonzero(np.diff(on)) + 1
    if on[0]:
        edges = np.r_[0, edges]
    if on[-1]:
        edges = np.r_[edges, len(on)]
    settle = int(SETTLE_S * rate)
    return [(int(a) + settle, int(b) - settle)
            for a, b in zip(edges[0::2], edges[1::2])
            if (b - a) / rate >= min_duration_s + 2 * SETTLE_S]


def fm_demodulate(iq: np.ndarray, rate: float) -> np.ndarray:
    """Instantaneous frequency, in Hz."""
    return np.angle(iq[1:] * np.conj(iq[:-1])) * rate / (2 * np.pi)


def occupied_bandwidth(iq: np.ndarray, rate: float,
                       fraction: float = OBW_FRACTION) -> float:
    """Bandwidth holding the given fraction of power, carrier at 0 Hz."""
    nper = int(rate / 100)  # 100 Hz bins
    freqs, psd = signal.welch(iq, rate, nperseg=nper, return_onesided=False)
    order = np.argsort(freqs)
    freqs, psd = freqs[order], psd[order]
    span = np.abs(freqs) <= OBW_SPAN_HZ
    freqs, psd = freqs[span], psd[span]
    cum = np.cumsum(psd) / np.sum(psd)
    tail = (1 - fraction) / 2
    lo = freqs[np.searchsorted(cum, tail)]
    hi = freqs[min(np.searchsorted(cum, 1 - tail), len(freqs) - 1)]
    return float(hi - lo)


def dominant_tone(audio: np.ndarray, rate: float) -> float:
    """
    Frequency of the strongest component in TONE_BAND_HZ, or 0 when nothing
    there reaches MIN_TONE_HZ of deviation (only noise, as with CTCSS alone).
    """
    window = np.hanning(len(audio))
    spec = np.abs(np.fft.rfft(audio * window))
    freqs = np.fft.rfftfreq(len(audio), 1 / rate)
    band = (freqs >= TONE_BAND_HZ[0]) & (freqs <= TONE_BAND_HZ[1])
    k = np.argmax(spec[band])
    if 2 * spec[band][k] / np.sum(window) < MIN_TONE_HZ:
        return 0.0
    return float(freqs[band][k])


def subaudible_tone(audio: np.ndarray, rate: float) -> tuple[float, float]:
    """
    Frequency and deviation (peak amplitude) of the strongest tone in
    CTCSS_BAND_HZ, from the spectral peak of the whole transmission. A CTCSS
    tone is a pure sine, so its amplitude is its deviation; reading it from
    the spectrum keeps the voice band and band-edge filtering out of it.
    The frequency is refined with a parabola through the peak's neighbours.
    """
    window = np.hanning(len(audio))
    n = 1 << int(np.ceil(np.log2(len(audio) * 4)))  # 4x zero padding
    spec = np.abs(np.fft.rfft(audio * window, n))
    freqs = np.fft.rfftfreq(n, 1 / rate)
    band = np.flatnonzero((freqs >= CTCSS_BAND_HZ[0])
                          & (freqs <= CTCSS_BAND_HZ[1]))
    k = band[np.argmax(spec[band])]
    a, b, c = np.log(spec[k - 1:k + 2] + 1e-12)
    curve = a - 2 * b + c
    shift = 0.5 * (a - c) / curve if curve else 0.0
    peak = np.exp(b - 0.25 * (a - c) * shift)
    return float((k + shift) * rate / n), float(2 * peak / np.sum(window))


def demodulated_audio(iq: np.ndarray, rate: float) -> np.ndarray:
    """Deviation in Hz at AUDIO_RATE, carrier offset removed."""
    dev = fm_demodulate(iq, rate)
    up, down = AUDIO_RATE, int(round(rate))
    g = np.gcd(up, down)
    audio = signal.resample_poly(dev, up // g, down // g)
    sos = signal.butter(6, AUDIO_LPF_HZ, fs=AUDIO_RATE, output="sos")
    audio = signal.sosfiltfilt(sos, audio)
    return audio - np.mean(audio)


def measure_fm(iq: np.ndarray, rate: float, start: int, stop: int,
               nominal_offset_hz: float,
               audio_path: Path | None = None) -> FmMeasurement:
    """
    Measure one transmission, iq[start:stop], near nominal_offset_hz.

    With audio_path, also write its demodulated audio there as a WAV file,
    5 kHz deviation at -2 dBFS, for listening.
    """
    seg = iq[start:stop]
    n = np.arange(len(seg))
    # Coarse carrier from the mean instantaneous frequency, then centred
    mixed = seg * np.exp(-2j * np.pi * nominal_offset_hz * n / rate)
    sos = signal.butter(8, 2 * CHANNEL_HZ, fs=rate, output="sos")
    coarse = nominal_offset_hz + np.mean(
        fm_demodulate(signal.sosfiltfilt(sos, mixed), rate))
    centred = seg * np.exp(-2j * np.pi * coarse * n / rate)
    channel = signal.sosfiltfilt(
        signal.butter(8, CHANNEL_HZ, fs=rate, output="sos"), centred)

    audio = demodulated_audio(channel, rate)
    if audio_path is not None:
        save_audio(audio_path, audio)
    dev = np.abs(audio)
    ctcss_hz, ctcss_dev = subaudible_tone(audio, AUDIO_RATE)
    return FmMeasurement(
        start_s=start / rate,
        duration_s=(stop - start) / rate,
        carrier_offset_hz=float(coarse),
        peak_deviation_hz=float(np.percentile(dev, 99.9)),
        max_deviation_hz=float(np.max(dev)),
        rms_deviation_hz=float(np.sqrt(np.mean(audio ** 2))),
        tone_hz=dominant_tone(audio, AUDIO_RATE),
        ctcss_hz=ctcss_hz,
        ctcss_deviation_hz=ctcss_dev,
        occupied_bandwidth_hz=occupied_bandwidth(centred, rate),
        power_db=float(10 * np.log10(np.mean(np.abs(seg) ** 2))),
    )


def measure_capture(iq: np.ndarray, rate: float, nominal_offset_hz: float,
                    min_duration_s: float = 1.0,
                    audio_dir: Path | None = None) -> list[FmMeasurement]:
    """
    Measure every FM transmission in a capture. With audio_dir, also write
    each one's audio there as tx<N>.wav, counting from 1.
    """
    iq, rate, offset = to_analysis_rate(iq, rate, nominal_offset_hz)
    spans = find_transmissions(iq, rate, min_duration_s)
    results = [measure_fm(iq, rate, a, b, offset,
                       None if audio_dir is None
                          else Path(audio_dir) / f"tx{i + 1}.wav")
               for i, (a, b) in enumerate(spans)]
    # Offsets stay relative to the capture centre
    for m in results:
        m.carrier_offset_hz += nominal_offset_hz - offset
    return results


def save_audio(path: Path, audio: np.ndarray,
               full_scale_hz: float = 5e3) -> None:
    """Write deviation audio at AUDIO_RATE to a 16-bit WAV file."""
    scaled = audio / full_scale_hz * 0.8 * 32767
    wavfile.write(path, AUDIO_RATE,
                  np.clip(scaled, -32767, 32767).astype(np.int16))


def main() -> None:
    """Measure the FM transmissions in a recording: python -m oat.analysis."""
    import argparse
    from oat.iq import IqFormat, read_iq

    ap = argparse.ArgumentParser(prog="python -m oat.analysis",
                                 description=main.__doc__)
    ap.add_argument("file", type=Path)
    ap.add_argument("--rate", type=float, default=250e3, help="samples/s")
    ap.add_argument("--offset", type=float, default=75e3,
                    help="signal frequency minus tuned frequency, Hz")
    ap.add_argument("--format", choices=[f.value for f in IqFormat],
                    default="cu8")
    ap.add_argument("--audio-dir", type=Path,
                    help="write each transmission's audio as tx<N>.wav")
    args = ap.parse_args()
    for i, m in enumerate(measure_capture(
            read_iq(args.file, IqFormat(args.format)), args.rate, args.offset,
            audio_dir=args.audio_dir), 1):
        print(f"tx{i}: " + ", ".join(f"{k} {v:.1f}"
                                     for k, v in m.as_dict().items()))


if __name__ == "__main__":
    main()
