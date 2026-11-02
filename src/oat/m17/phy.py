# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
M17 on the air: 4FSK signals for the transmitter, and measurements of what a
radio transmits.

The M17 Protocol Specification (part I, v2.0.2) sends 4800 symbols/s,
shaped by a root-raised-cosine filter (alpha 0.5) before the frequency
modulator and again after the receiver's discriminator, with the outer
symbols at +-2.4 kHz and the inner at +-0.8 kHz. Measurement follows the
receive chain: FM demodulation at 48 kHz, the matched RRC filter, symbol
timing from the spectral line the signal's square has at the symbol rate,
then the four levels, the sync bursts and the frames between them.
"""

from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.interpolate import CubicSpline
from scipy.io import wavfile

from oat.analysis import (find_transmissions, fm_demodulate,
                          occupied_bandwidth, reference_error_hz,
                          to_analysis_rate)
from oat.m17 import codec
from oat.signals import IQ_AMPLITUDE

BASEBAND_RATE = 48000
SAMPLES_PER_SYMBOL = BASEBAND_RATE // codec.SYMBOL_RATE  # 10
RRC_ALPHA = 0.5
RRC_SPAN = 8  # symbols
# Deviation of one symbol unit: +3 is 2.4 kHz, +1 is 0.8 kHz
NOMINAL_OUTER_HZ = 2400.0
HZ_PER_UNIT = NOMINAL_OUTER_HZ / 3
# Channel filter before the discriminator, one-sided: the 9 kHz M17 channel
# with room for a carrier off by a couple of kHz
CHANNEL_HZ = 7500.0
# Shortest transmission measured: preamble, LSF and EOT are 120 ms
MIN_DURATION_S = 0.1
# Searched around the nominal symbol rate for the symbol clock, Hz
SYMBOL_RATE_SEARCH_HZ = 20.0
# Symbol timing is fitted over blocks of this many seconds
TIMING_BLOCK_S = 0.25
# Mean squared distance, in symbol units, below which 8 symbols match a sync
# burst: an RMS error of one unit, halfway between neighbouring levels
SYNC_MATCH = 1.0
# How far, in symbols, a frame boundary is searched around where it should be
FRAME_SLIP = 1
# A preamble symbol must reach this far toward the outer level, in units
PREAMBLE_LEVEL = 2.0
SYNCS = {"lsf": codec.SYNC_LSF, "stream": codec.SYNC_STREAM,
         "packet": codec.SYNC_PACKET, "bert": codec.SYNC_BERT,
         "eot": codec.EOT_MARKER}
# s16 scale of the baseband WAV and of m17-demod's input: an FM
# discriminator's +-pi rad/sample at +-16384, as rtl_fm writes it
DISCRIMINATOR_FULL_SCALE = 16384


def rrc_taps(sps: int = SAMPLES_PER_SYMBOL, alpha: float = RRC_ALPHA,
             span: int = RRC_SPAN) -> np.ndarray:
    """Root-raised-cosine filter of unit energy, span symbols long, as
    libm17 has it."""
    t = np.arange(-span * sps // 2, span * sps // 2 + 1) / sps
    h = np.empty(len(t))
    for i, x in enumerate(t):
        if x == 0:
            h[i] = 1 - alpha + 4 * alpha / np.pi
        elif abs(abs(x) - 1 / (4 * alpha)) < 1e-9:
            h[i] = alpha / np.sqrt(2) * (
                (1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        else:
            h[i] = ((np.sin(np.pi * x * (1 - alpha))
                     + 4 * alpha * x * np.cos(np.pi * x * (1 + alpha)))
                    / (np.pi * x * (1 - (4 * alpha * x) ** 2)))
    return h / np.sqrt(np.sum(h ** 2))


RRC = rrc_taps()
# The receive filter passes a steady frequency unchanged, so the centre of
# the symbol levels is the carrier; the transmit filter is scaled so that
# the two together read each symbol's deviation at its instant. A steady
# symbol then deviates the carrier by its nominal amount, as an FSK tone.
RRC_RX = RRC / np.sum(RRC)
RRC_TX = RRC * np.sum(RRC) / np.sum(RRC ** 2)


# Generation ---------------------------------------------------------------

def baseband(symbols: np.ndarray, outer_deviation_hz: float = NOMINAL_OUTER_HZ,
             inner_deviation_hz: float | None = None) -> np.ndarray:
    """
    Frequency deviation, in Hz at BASEBAND_RATE, of a symbol stream through
    the transmit RRC filter. After the receiver's matched filter, symbol
    instants read the outer and inner deviations: the two filters together
    are a raised cosine of unit gain. The inner deviation defaults to a
    third of the outer, as the specification has it.
    """
    inner = (outer_deviation_hz / 3 if inner_deviation_hz is None
             else inner_deviation_hz)
    sym = np.asarray(symbols)
    levels = np.where(np.abs(sym) == 3, outer_deviation_hz, inner) \
        * np.sign(sym)
    impulses = np.zeros(len(sym) * SAMPLES_PER_SYMBOL)
    impulses[::SAMPLES_PER_SYMBOL] = levels
    return np.convolve(impulses, RRC_TX)


def resample(x: np.ndarray, rate_from: float, rate_to: float) -> np.ndarray:
    """Polyphase resampling between rates with an integer ratio."""
    up, down = int(round(rate_to)), int(round(rate_from))
    g = np.gcd(up, down)
    return signal.resample_poly(x, up // g, down // g)


def m17_iq(symbols: np.ndarray, rate: float,
           outer_deviation_hz: float = NOMINAL_OUTER_HZ,
           offset_hz: float = 0.0, inverted: bool = False,
           inner_deviation_hz: float | None = None) -> np.ndarray:
    """
    Complex baseband of a transmission at `rate`, carrier at offset_hz, at
    IQ_AMPLITUDE. Inverted sends every symbol with the wrong sign, as a
    transmitter with its modulator phase reversed would.
    """
    dev = baseband(-np.asarray(symbols) if inverted else symbols,
                   outer_deviation_hz, inner_deviation_hz)
    dev = resample(dev, BASEBAND_RATE, rate)
    phase = 2 * np.pi * np.cumsum(offset_hz + dev) / rate
    return (IQ_AMPLITUDE * np.exp(1j * phase)).astype(np.complex64)


# Measurement --------------------------------------------------------------

@dataclass
class M17Frame:
    """A frame found in a transmission."""

    kind: str  # lsf, stream, packet, bert or eot
    symbol: int  # index of its first sync symbol
    distance: float  # mean squared distance to its sync burst, in units


@dataclass
class M17Measurement:
    """Measurements of one M17 transmission."""

    start_s: float
    duration_s: float
    carrier_offset_hz: float  # from the capture centre
    outer_deviation_hz: float  # half the distance between the outer levels
    inner_deviation_hz: float
    deviation_ratio: float  # outer / inner, 3 nominally
    evm_percent: float  # RMS error from the fitted levels, of the outer one
    symbol_rate_hz: float
    symbol_rate_error_ppm: float
    polarity: str  # normal, or inverted
    preamble_s: float
    lsf_latency_s: float  # carrier on to the LSF sync burst; -1 without one
    lsf_found: bool
    lsf_crc_ok: bool
    source: str
    destination: str
    lsf_type: int
    stream_frames: int
    frame_number_errors: int  # stream frames not numbered in sequence
    last_frame_flagged: bool
    lich_lsf_ok: bool  # the LSF rebuilt from the LICH passes its CRC
    eot_found: bool
    occupied_bandwidth_hz: float
    power_db: float
    payload: bytes = field(default=b"", repr=False)
    baseband: np.ndarray = field(default=None, repr=False)

    def as_dict(self) -> dict:
        """Plain dictionary, for reports: without payload and baseband."""
        d = asdict(self)
        del d["payload"], d["baseband"]
        return d


def symbol_timing(x: np.ndarray, rate: float = BASEBAND_RATE,
                  nominal_hz: float = codec.SYMBOL_RATE
                  ) -> tuple[float, float]:
    """
    Symbol rate and the sample index of the first symbol instant, from the
    spectral line that the square of a Nyquist-filtered symbol stream has at
    the symbol rate, peaking at symbol instants. The rate comes from the
    line's frequency near nominal_hz, refined by fitting its phase across
    blocks, so a symbol clock off by tens of ppm stays locked over a long
    transmission.
    """
    e = (x - np.mean(x)) ** 2
    e = e - np.mean(e)
    n = np.arange(len(e))
    # Coarse: the line's peak, after mixing it to 0 Hz and summing 1 ms blocks
    block = int(rate // 1000)
    mixed = e * np.exp(-2j * np.pi * nominal_hz * n / rate)
    m = len(mixed) // block
    dec = mixed[:m * block].reshape(m, block).sum(axis=1)
    nfft = 1 << int(np.ceil(np.log2(max(64 * m, 1024))))
    spec = np.abs(np.fft.fft(dec, nfft))
    freqs = np.fft.fftfreq(nfft, block / rate)
    near = np.flatnonzero(np.abs(freqs) <= SYMBOL_RATE_SEARCH_HZ)
    k = near[np.argmax(spec[near])]
    a, b, c = spec[(k - 1) % nfft], spec[k], spec[(k + 1) % nfft]
    shift = 0.5 * (a - c) / (a - 2 * b + c) if a - 2 * b + c else 0.0
    f = nominal_hz + freqs[k] + shift * rate / block / nfft
    # Fine: the line's phase per block, fitted with a straight line
    size = int(TIMING_BLOCK_S * rate)
    blocks = len(e) // size
    if blocks >= 3:
        lo = e * np.exp(-2j * np.pi * f * n / rate)
        sums = lo[:blocks * size].reshape(blocks, size).sum(axis=1)
        centres = (np.arange(blocks) + 0.5) * size
        phase = np.unwrap(np.angle(sums))
        slope, intercept = np.polyfit(centres, phase, 1, w=np.abs(sums))
        f += slope * rate / (2 * np.pi)
        line = np.sum(e * np.exp(-2j * np.pi * f * n / rate))
    else:
        line = np.sum(e * np.exp(-2j * np.pi * f * n / rate))
    period = rate / f
    first = (-np.angle(line) / (2 * np.pi)) * period % period
    return float(f), float(first)


def four_levels(s: np.ndarray, iterations: int = 20
                ) -> tuple[np.ndarray, np.ndarray]:
    """
    The four symbol levels, lowest first, and which symbols they come from:
    clicks and the edges of a transmission, far beyond any symbol, are left
    out.
    """
    # The outer levels bound the symbols, whatever the mix of symbols
    low, high = np.percentile(s, [2, 98])
    centre, outer = (low + high) / 2, (high - low) / 2
    used = np.abs(s - centre) < 1.5 * outer
    x = s[used]
    levels = centre + np.array([-1, -1 / 3, 1 / 3, 1]) * outer
    for _ in range(iterations):
        idx = np.argmin(np.abs(x[:, None] - levels[None, :]), axis=1)
        levels = np.array([x[idx == i].mean() if np.any(idx == i)
                           else levels[i] for i in range(4)])
    return levels, used


def sync_distances(z: np.ndarray) -> dict[str, np.ndarray]:
    """Mean squared distance of every 8 symbols to each sync burst."""
    if len(z) < codec.SYNC_SYMBOLS:
        return {k: np.zeros(0) for k in SYNCS}
    windows = np.lib.stride_tricks.sliding_window_view(z, codec.SYNC_SYMBOLS)
    return {k: np.mean((windows - codec.sync_symbols(w)) ** 2, axis=1)
            for k, w in SYNCS.items()}


# Which frame kinds may follow which: a voice stream is LSF, stream frames,
# EOT; a stream picked up late starts on a stream frame
FOLLOWS = {None: {"lsf", "stream", "packet", "bert"},
           "lsf": {"stream", "packet", "eot"},
           "stream": {"stream", "eot"}, "packet": {"packet", "eot"},
           "bert": {"bert", "eot"}, "eot": set()}


def walk_frames(z: np.ndarray) -> list[M17Frame]:
    """
    The frames of a transmission in order: from the first sync burst, one
    every 192 symbols, until the EOT, a frame that can't follow the one
    before, or the end. Each boundary may slip by FRAME_SLIP symbols.
    """
    dist = sync_distances(z)
    if not len(dist["lsf"]):
        return []
    starts = ("lsf", "stream", "packet", "bert")
    best = np.min([dist[k] for k in starts], axis=0)
    hits = np.flatnonzero(best < SYNC_MATCH)
    if not len(hits):
        return []
    k = hits[0]
    # The best match of the first run of hits
    run = hits[(hits >= k) & (hits <= k + codec.SYNC_SYMBOLS)]
    k = int(run[np.argmin(best[run])])
    frames, prev = [], None
    while k < len(dist["lsf"]):
        lo, hi = max(0, k - FRAME_SLIP), min(len(dist["lsf"]),
                                             k + FRAME_SLIP + 1)
        allowed = FOLLOWS[prev]
        options = [(float(dist[kind][j]), j, kind) for kind in allowed
                   for j in range(lo, hi)]
        if not options:
            break
        d, j, kind = min(options)
        if d >= SYNC_MATCH:
            break
        frames.append(M17Frame(kind, int(j), d))
        if kind == "eot":
            break
        prev, k = kind, j + codec.SYMBOLS_PER_FRAME
    return frames


def preamble_symbols(z: np.ndarray, sync_at: int) -> int:
    """Length of the alternating outer-symbol run just before a sync burst."""
    count, expect = 0, -np.sign(z[sync_at]) if sync_at < len(z) else -1
    i = sync_at - 1
    while i >= 0 and np.sign(z[i]) == expect and abs(z[i]) >= PREAMBLE_LEVEL:
        count += 1
        expect = -expect
        i -= 1
    return count


def _score(frames: list[M17Frame]) -> int:
    """How much of a transmission a frame walk explains."""
    return sum(codec.SYMBOLS_PER_FRAME for _ in frames)


def measure_m17(iq: np.ndarray, rate: float, start: int, stop: int,
                nominal_offset_hz: float, lead: int = 0) -> M17Measurement:
    """
    Measure one M17 transmission, iq[start:stop], near nominal_offset_hz.
    The carrier came on `lead` samples after start.
    """
    seg = iq[start:stop]
    n = np.arange(len(seg))
    mixed = seg * np.exp(-2j * np.pi * nominal_offset_hz * n / rate)
    sos = signal.butter(8, 2 * CHANNEL_HZ, fs=rate, output="sos")
    coarse = nominal_offset_hz + np.mean(
        fm_demodulate(signal.sosfiltfilt(sos, mixed), rate))
    centred = seg * np.exp(-2j * np.pi * coarse * n / rate)
    channel = signal.sosfiltfilt(
        signal.butter(8, CHANNEL_HZ, fs=rate, output="sos"), centred)
    dev = resample(fm_demodulate(channel, rate), rate, BASEBAND_RATE)
    mf = np.convolve(dev, RRC_RX, mode="same")

    # Level and timing estimates use only what the carrier fully carries:
    # past the lead-in before key-up, and the matched filter's edges
    edge = RRC_SPAN * SAMPLES_PER_SYMBOL
    lo = int(lead * BASEBAND_RATE / rate) + edge
    hi = len(mf) - edge
    if hi - lo < 4 * edge:
        lo, hi = 0, len(mf)
    body = mf[lo:hi]
    centre0 = (np.percentile(body, 2) + np.percentile(body, 98)) / 2
    f_sym, first = symbol_timing(body - centre0)
    period = BASEBAND_RATE / f_sym
    # Symbol instants from the start of the segment, in step with the body's
    first = (first + lo) % period
    instants = first + period * np.arange(int((len(mf) - 1 - first) / period))
    sym_hz = CubicSpline(np.arange(len(mf)), mf)(instants) - centre0

    carried = sym_hz[(instants >= lo) & (instants < hi)]
    levels, used = four_levels(carried)
    inner = carried[used]
    idx = np.argmin(np.abs(inner[:, None] - levels[None, :]), axis=1)
    centre = float(np.mean(levels))
    units = np.array([-3, -1, 1, 3])[idx]
    scale = float(np.sum(units * (inner - centre)) / np.sum(units ** 2))
    error = inner - centre - units * scale
    z = (sym_hz - centre) / scale

    # Polarity: the reading whose frames make a valid transmission, the LSF
    # CRC deciding when both do
    walks = {}
    for polarity, sign in (("normal", 1), ("inverted", -1)):
        frames = walk_frames(sign * z)
        crc = False
        if frames and frames[0].kind == "lsf":
            k = frames[0].symbol
            crc = codec.decode_lsf(sign * z[k + 8:k + 192])[1]
        walks[polarity] = (crc, _score(frames), frames, sign)
    polarity = max(walks, key=lambda p: walks[p][:2])
    _, _, frames, sign = walks[polarity]
    z = sign * z

    lsf, crc_ok, latency, pre = None, False, -1.0, 0
    if frames and frames[0].kind == "lsf":
        k = frames[0].symbol
        if k + 192 <= len(z):
            lsf, crc_ok, _ = codec.decode_lsf(z[k + 8:k + 192])
        pre = preamble_symbols(z, k)
        latency = (instants[k] / BASEBAND_RATE) - lead / rate
    streams = []
    for f in frames:
        if f.kind == "stream" and f.symbol + 192 <= len(z):
            streams.append(codec.decode_stream_frame(
                z[f.symbol + 8:f.symbol + 192]))
    rebuilt = codec.lsf_from_lich(streams)
    if lsf is None and rebuilt is not None and rebuilt[1]:
        lsf = rebuilt[0]
    fn_errors = sum(1 for i, s in enumerate(streams) if s.fn != i & 0x7FFF)

    ppm_rate = f_sym / codec.SYMBOL_RATE - 1
    return M17Measurement(
        start_s=(start + lead) / rate,
        duration_s=(stop - start - lead) / rate,
        carrier_offset_hz=float(coarse + centre0 + centre),
        outer_deviation_hz=float((levels[3] - levels[0]) / 2),
        inner_deviation_hz=float((levels[2] - levels[1]) / 2),
        deviation_ratio=float((levels[3] - levels[0])
                              / (levels[2] - levels[1])),
        evm_percent=float(100 * np.sqrt(np.mean(error ** 2)) / (3 * scale)),
        symbol_rate_hz=f_sym,
        symbol_rate_error_ppm=float(1e6 * ppm_rate),
        polarity=polarity,
        preamble_s=pre / codec.SYMBOL_RATE,
        lsf_latency_s=float(latency),
        lsf_found=bool(frames and frames[0].kind == "lsf"),
        lsf_crc_ok=bool(crc_ok),
        source=lsf.source if lsf else "",
        destination=lsf.destination if lsf else "",
        lsf_type=lsf.type if lsf else -1,
        stream_frames=len(streams),
        frame_number_errors=fn_errors,
        last_frame_flagged=bool(streams and streams[-1].last),
        lich_lsf_ok=bool(rebuilt and rebuilt[1]),
        eot_found=any(f.kind == "eot" for f in frames),
        occupied_bandwidth_hz=occupied_bandwidth(centred, rate),
        power_db=float(10 * np.log10(np.mean(np.abs(seg[lead:]) ** 2))),
        payload=b"".join(s.payload for s in streams),
        baseband=(dev - centre0 - centre) * sign,
    )


def measure_m17_capture(iq, rate: float, nominal_offset_hz: float,
                        min_duration_s: float = MIN_DURATION_S,
                        audio_dir: Path | None = None,
                        center_hz: float = 0.0,
                        frequency_correction_ppm: float = 0.0
                        ) -> list[M17Measurement]:
    """
    Measure every M17 transmission in a capture, an array or blocks of one.
    With audio_dir, write each one's demodulated baseband there as
    tx<N>-baseband.wav (48 kHz, s16, the discriminator scale m17-demod
    reads). Carrier offsets and symbol rates are corrected for a receiver
    reference frequency_correction_ppm fast, tuned to center_hz.
    """
    iq, rate, offset = to_analysis_rate(iq, rate, nominal_offset_hz)
    spans = find_transmissions(iq, rate, min_duration_s, settle_s=0.0)
    results = []
    # Start a little early, so the matched filter sees the first symbols
    lead = int(0.005 * rate)
    for i, (a, b) in enumerate(spans):
        start = max(0, a - lead)
        m = measure_m17(iq, rate, start, b, offset, lead=a - start)
        m.carrier_offset_hz += (nominal_offset_hz - offset
                                + reference_error_hz(
                                    center_hz, frequency_correction_ppm))
        # A receiver ppm fast samples slow signals as faster
        m.symbol_rate_hz *= 1 + frequency_correction_ppm * 1e-6
        m.symbol_rate_error_ppm = 1e6 * (m.symbol_rate_hz
                                         / codec.SYMBOL_RATE - 1)
        if audio_dir is not None:
            save_baseband(Path(audio_dir) / f"tx{i + 1}-baseband.wav",
                          m.baseband)
        results.append(m)
    return results


def baseband_pcm(dev_hz: np.ndarray) -> np.ndarray:
    """Deviation as s16 at the discriminator scale (+-24 kHz full scale)."""
    scaled = dev_hz / (BASEBAND_RATE / 2) * DISCRIMINATOR_FULL_SCALE
    return np.clip(np.round(scaled), -32767, 32767).astype(np.int16)


def save_baseband(path: Path, dev_hz: np.ndarray) -> None:
    """Write demodulated M17 baseband as a 48 kHz WAV file."""
    wavfile.write(path, BASEBAND_RATE, baseband_pcm(dev_hz))
