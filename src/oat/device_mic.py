# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
The M17 microphone chain on radios: two radios on two nearby frequencies,
keyed together and captured by one receiver, each channelised out of the
capture and measured on its own; their decoded voice is scored as the
emulator campaign scores it (docs/device-mic-campaign.md).

Pure functions here, the bench in robot_library (Open Radio Pair Bench) and
acceptance/m17/mic-chain-device.robot.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

from oat import emu
from oat import emu_campaign as camp
from oat.m17.phy import measure_m17_capture

RATE = emu.CODEC2_RATE
# Each radio's channel out of the capture: low-pass edge and output rate
CHANNEL_CUTOFF_HZ = 12.5e3
CHANNEL_RATE = 50e3
# Where the lower radio sits above the receiver's centre, clear of its DC
# spike, with both radios' images (at minus their offsets) clear of both
LOW_OFFSET_HZ = 30e3
# The longest delay between key-up and played speech the alignment searches
MAX_LAG_S = 6.0
ONSET_S = 0.1
LISTEN_NOISE_DBFS = camp.LISTEN_NOISE_DBFS


@dataclass
class PairTuning:
    center_hz: float
    offsets_hz: tuple[float, float]


def pair_tuning(freq_a_hz: float, freq_b_hz: float, rate: float,
                low_offset_hz: float = LOW_OFFSET_HZ,
                channel_hz: float = CHANNEL_CUTOFF_HZ) -> PairTuning:
    """
    The receiver frequency that puts both radios on the positive side of
    the capture, the lower one low_offset_hz above the centre: each one's
    IQ image lands on the negative side, away from both, and both channels
    stay inside 80% of the capture's Nyquist band.
    """
    low = min(freq_a_hz, freq_b_hz)
    center = low - low_offset_hz
    offs = (freq_a_hz - center, freq_b_hz - center)
    if abs(freq_a_hz - freq_b_hz) < 2 * channel_hz:
        raise ValueError(f"the radios are {abs(freq_a_hz - freq_b_hz) / 1e3:.1f} kHz "
                         f"apart: at least {2 * channel_hz / 1e3:.0f} kHz is needed")
    if max(offs) + channel_hz > 0.4 * rate:
        raise ValueError(f"{max(offs) / 1e3:.0f} kHz from the centre does not fit a "
                         f"{rate / 1e3:.0f} kS/s capture: bring the radios closer")
    return PairTuning(center, offs)


def channelise(blocks, rate: float, offset_hz: float, out_rate: float = CHANNEL_RATE,
               cutoff_hz: float = CHANNEL_CUTOFF_HZ, taps: int = 201):
    """
    One channel of a capture (an array or blocks of one): moved from
    offset_hz to 0 Hz, low-passed at cutoff_hz and decimated to about
    out_rate. Yields blocks; the phase and filter carry across blocks.
    Returns nothing itself: see channel_rate for the output rate.
    """
    blocks = [blocks] if isinstance(blocks, np.ndarray) else blocks
    factor = max(1, int(rate // out_rate))
    h = signal.firwin(taps, cutoff_hz, fs=rate).astype(np.float32)
    history = np.zeros(len(h) - 1, dtype=np.complex64)
    done = 0
    for block in blocks:
        n = done + np.arange(len(block))
        mixed = (block * np.exp(-2j * np.pi * offset_hz / rate * n)).astype(np.complex64)
        padded = np.concatenate([history, mixed])
        filtered = signal.oaconvolve(padded, h, mode="valid")
        history = padded[len(padded) - len(history):]
        yield filtered[(-done) % factor::factor].astype(np.complex64)
        done += len(block)


def channel_rate(rate: float, out_rate: float = CHANNEL_RATE) -> float:
    return rate / max(1, int(rate // out_rate))


def measure_channel(blocks, rate: float, offset_hz: float, audio_dir: Path | None = None,
                    center_hz: float = 0.0, ppm: float = 0.0, min_duration_s: float = 0.5):
    """Every M17 transmission of one radio in a capture of two."""
    ch = np.concatenate(list(channelise(blocks, rate, offset_hz)) or
                        [np.zeros(0, np.complex64)])
    return measure_m17_capture(ch, channel_rate(rate), 0.0, min_duration_s,
                               audio_dir=audio_dir, center_hz=center_hz,
                               frequency_correction_ppm=ppm)


# --- scoring --------------------------------------------------------------------

def adc_level(decoded_dbfs: float | None, mic: dict) -> float | None:
    """
    The level at the ADC (dBFS of its half range) that a decoded level
    implies through a fixed-gain chain: minus the gain, and minus the step
    from the ADC's range to int16 (12 bits: +24.1 dB). Assumes codec2 keeps
    the active level: the emulator showed it reading about 2 dB high
    (+0.6 to +3.3 dB) below clipping.
    """
    if decoded_dbfs is None:
        return None
    return float(decoded_dbfs - 20 * np.log10(mic["gain"])
                 - 20 * np.log10(2 ** (mic["adc_bits"] - 1) / 32768))


def wide_align(clean: np.ndarray, v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return emu.align(clean, v, max_lag_s=MAX_LAG_S)


def score_take(test: str, v: np.ndarray, clean: np.ndarray | None = None,
               noise_levels=LISTEN_NOISE_DBFS) -> dict:
    """
    Figures of one decoded transmission: level statistics (the first 100 ms
    apart as the onset), and for played speech STOI and ESTOI against the
    source and the listening-condition STOI at each noise level.
    """
    lv = emu.levels(v, skip_s=ONSET_S)
    m = {"active_dbfs": lv["active_dbfs"], "peak_dbfs": lv["peak_dbfs"],
         "onset_peak_dbfs": lv["onset_peak_dbfs"], "floor_dbfs": lv["floor_dbfs"],
         "clip_fraction": lv["clip_fraction"], "seconds": len(v) / RATE}
    if test == "speech" and clean is not None:
        from pystoi import stoi as _stoi
        a, b = wide_align(clean, v)
        m["stoi"] = float(_stoi(a, b, RATE, extended=False))
        m["estoi"] = float(_stoi(a, b, RATE, extended=True))
        for nl in noise_levels:
            r = camp.listening(a, b, float(nl))
            m[f"listen_stoi_{abs(int(nl))}"] = r["listen_stoi"]
    return m


def spread(values) -> float | None:
    v = [float(x) for x in values if x is not None and np.isfinite(x)]
    return float(max(v) - min(v)) if len(v) > 1 else None


def summary(rows: list[dict], radio: str, test: str) -> dict:
    """A test's figures for one radio, from its take rows."""
    mine = [r for r in rows if r["radio"] == radio and r["test"] == test and "error" not in r]

    def vals(k):
        return [r[k] for r in mine if r.get(k) is not None and np.isfinite(r[k])]

    out = {"radio": radio, "takes": len(mine)}
    if not mine:
        return out
    if test == "silence":
        out.update(onset_max_dbfs=max(vals("onset_peak_dbfs")),
                   onset_median_dbfs=float(np.median(vals("onset_peak_dbfs"))),
                   floor_median_dbfs=float(np.median(vals("floor_dbfs"))))
    else:
        out.update(active_spread_db=spread(vals("active_dbfs")),
                   active_mean_dbfs=float(np.mean(vals("active_dbfs"))),
                   peak_max_dbfs=max(vals("peak_dbfs")),
                   onset_max_dbfs=max(vals("onset_peak_dbfs")))
        if vals("adc_active_dbfs"):
            out.update(adc_active_mean_dbfs=float(np.mean(vals("adc_active_dbfs"))),
                       adc_peak_max_dbfs=max(vals("adc_peak_dbfs")))
    if test == "speech":
        out.update(stoi_mean=float(np.mean(vals("stoi"))),
                   estoi_mean=float(np.mean(vals("estoi"))))
        for nl in LISTEN_NOISE_DBFS:
            k = f"listen_stoi_{abs(int(nl))}"
            if vals(k):
                out[f"{k}_mean"] = float(np.mean(vals(k)))
                out[f"{k}_worst"] = min(vals(k))
    return out


def speech_clip(path: Path, seconds: float = 10.0) -> np.ndarray:
    """The played clip's source at 8 kHz, peak -1 dBFS (the 0 dB playback level)."""
    x = camp.load_speech(Path(path), seconds)
    return x * 10 ** (-1 / 20)


def to_rate(x: np.ndarray, rate_from: int, rate_to: int) -> np.ndarray:
    g = np.gcd(int(rate_from), int(rate_to))
    return signal.resample_poly(x, rate_to // g, rate_from // g)


def write_voice(path: Path, pcm: np.ndarray) -> np.ndarray:
    """Save decoded int16 voice; returns it as float full scale."""
    path.parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(path, RATE, pcm.astype(np.int16))
    return pcm.astype(float) / 32768


# --- report ---------------------------------------------------------------------

def variant_of(firmware: str) -> str:
    return str(firmware).partition("@")[0] or str(firmware)


def report(rows: list[dict], radio: str, radio_name: str, meta: dict,
           charts: dict) -> str:
    """REPORT.md for one radio over one or more runs (variants)."""
    from oat.emu_report import _f, _delta, _table
    variants = list(dict.fromkeys(r["variant"] for r in rows if r["radio"] == radio))
    base = "baseline" if "baseline" in variants else variants[0]
    L = [f"# M17 microphone chain on a radio: {radio_name}", "",
         f"Radio file `{radio}`, hardware {meta.get('hardware', '?')}. OpenRTX "
         f"{', '.join(f'`{v}`' for v in meta.get('firmware', []))}; suite "
         f"`{meta.get('suite_commit', '?')}`; station {meta.get('station', '?')}; "
         f"playback setting: {meta.get('playback', 'not recorded')}.", "",
         "How it is measured: [METHODS.md](METHODS.md). Tables are in `data/`, each take's "
         "decoded voice in `voice/`.", ""]
    for c in ("speech", "listen", "onset", "live"):
        if c in charts:
            L += [f"![{c}]({charts[c]})", ""]

    def table(test, cols):
        head, body = ["variant"], []
        for k, name, fmt, d in cols:
            head += [name] + (["Δ"] if d else [])
        sb = summary([r for r in rows if r["variant"] == base], radio, test)
        for v in variants:
            s = summary([r for r in rows if r["variant"] == v], radio, test)
            if s["takes"] == 0:
                continue
            row = [v]
            for k, _, fmt, d in cols:
                row.append(_f(s.get(k), fmt))
                if d:
                    row.append("" if v == base else _delta(s.get(k), sb.get(k), d))
            body.append(row)
        return _table(head, body)

    L += ["## Key-up and silence", "",
          "Three quiet overs: the peak of the first 100 ms of decoded audio (the key-up "
          "transient) and the floor (10th percentile of 20 ms frames).", "",
          *table("silence", [("onset_max_dbfs", "onset worst dBFS", "{:.1f}", "{:+.1f}"),
                             ("onset_median_dbfs", "onset median dBFS", "{:.1f}", "{:+.1f}"),
                             ("floor_median_dbfs", "floor dBFS", "{:.1f}", "{:+.1f}")])]
    L += ["## Played speech", "",
          "The Harvard clip played at three levels from the speaker: decoded active level, its "
          "spread over the three, STOI and ESTOI against the source, and STOI as a listener "
          "hears it at a fixed volume over pink noise.", "",
          *table("speech", [("active_spread_db", "spread dB", "{:.1f}", "{:+.1f}"),
                            ("active_mean_dbfs", "active dBFS", "{:.1f}", "{:+.1f}"),
                            ("peak_max_dbfs", "peak dBFS", "{:.1f}", "{:+.1f}"),
                            ("stoi_mean", "STOI", "{:.3f}", "{:+.3f}"),
                            ("estoi_mean", "ESTOI", "{:.3f}", "{:+.3f}"),
                            ("listen_stoi_45_mean", "listening STOI -45", "{:.3f}", "{:+.3f}"),
                            ("listen_stoi_35_mean", "listening STOI -35", "{:.3f}", "{:+.3f}"),
                            ("listen_stoi_35_worst", "worst -35", "{:.3f}", "{:+.3f}")])]
    body = []
    for r in sorted((r for r in rows if r["radio"] == radio and r["test"] == "speech"
                     and "error" not in r), key=lambda r: (variants.index(r["variant"]),
                                                           r["level"])):
        body.append([r["variant"], f"{r['level']:+.0f}", _f(r["active_dbfs"]),
                     _f(r["peak_dbfs"]), _f(r["stoi"], "{:.3f}"),
                     _f(r.get("listen_stoi_45"), "{:.3f}"), _f(r.get("listen_stoi_35"), "{:.3f}"),
                     _f(r.get("adc_active_dbfs"))])
    L += ["Per take:", "", *_table(["variant", "playback dB", "active dBFS", "peak dBFS", "STOI",
                                    "listening -45", "listening -35", "at the ADC dBFS"], body)]
    L += ["## Live voice", "",
          "The operator reading one sentence quietly, normally and loudly: decoded active "
          "level and its spread.", "",
          *table("live", [("active_spread_db", "spread dB", "{:.1f}", "{:+.1f}"),
                          ("active_mean_dbfs", "active dBFS", "{:.1f}", "{:+.1f}"),
                          ("peak_max_dbfs", "peak dBFS", "{:.1f}", "{:+.1f}")])]
    body = []
    for r in sorted((r for r in rows if r["radio"] == radio and r["test"] in ("live", "speech")
                     and r.get("adc_active_dbfs") is not None and "error" not in r),
                    key=lambda r: (r["variant"], r["test"], float(r["level"] or 0))):
        body.append([r["variant"], r["test"], str(r.get("label") or f"{r['level']:+.0f} dB"),
                     _f(r["active_dbfs"]), _f(r["adc_active_dbfs"]), _f(r["adc_peak_dbfs"])])
    if body:
        L += ["## Real speech level at the ADC", "",
              "From the fixed-gain chain only (no AGC): decoded level minus the chain's gain "
              "(`CONFIG_MIC_GAIN`, and +24.1 dB from the 12-bit ADC's range to int16). Codec2 "
              "is assumed to keep the active level: in the emulator it read +0.6 to +3.3 dB "
              "above codec2's input (about +2 dB) below clipping, so the level at the ADC is "
              "likely about 2 dB under these figures. Peaks are of the decoded audio, and "
              "codec2 changes peaks more than active levels.", "",
              *_table(["variant", "test", "take", "decoded active dBFS", "ADC active dBFS",
                       "ADC peak dBFS"], body)]
    return "\n".join(L)


def charts(rows: list[dict], radio: str, out: Path) -> dict:
    """PNG and SVG charts of one radio over its variants."""
    from oat.emu_report import _plt, _save, colour, marker
    mine = [r for r in rows if r["radio"] == radio and "error" not in r]
    variants = list(dict.fromkeys(r["variant"] for r in mine))
    plt = _plt()
    made = {}
    sp = [r for r in mine if r["test"] == "speech"]
    if sp:
        fig, ax = plt.subplots(figsize=(6.2, 4.2))
        for v in variants:
            pts = sorted((r["level"], r["active_dbfs"]) for r in sp if r["variant"] == v)
            ax.plot(*zip(*pts), color=colour(v), marker=marker(v), label=v)
        ax.set_xlabel("Playback level (dB, relative to the set speaker volume)")
        ax.set_ylabel("Decoded active speech level (dBFS)")
        ax.set_title("Played speech: level in vs out")
        ax.legend(fontsize=8)
        made["speech"] = _save(fig, out, "speech-level")
        fig, axes = plt.subplots(1, 3, figsize=(15, 4.2), sharey=True)
        for ax, (k, what) in zip(axes, (("stoi", "STOI (level-normalised)"),
                                        ("listen_stoi_45", "listening, noise -45 dBFS"),
                                        ("listen_stoi_35", "listening, noise -35 dBFS"))):
            for v in variants:
                pts = sorted((r["level"], r[k]) for r in sp if r["variant"] == v
                             and r.get(k) is not None)
                if pts:
                    ax.plot(*zip(*pts), color=colour(v), marker=marker(v), label=v)
            ax.set_title(what, fontsize=10)
            ax.set_xlabel("Playback level (dB)")
        axes[0].set_ylabel("STOI against the source")
        axes[-1].legend(fontsize=8)
        made["listen"] = _save(fig, out, "intelligibility")
    si = [r for r in mine if r["test"] == "silence"]
    if si:
        fig, ax = plt.subplots(figsize=(5.2, 3.8))
        for i, v in enumerate(variants):
            x = [r["onset_peak_dbfs"] for r in si if r["variant"] == v]
            if not x:
                continue
            ax.bar(i, max(x) + 90, bottom=-90, width=0.6, color=colour(v))
        ax.set_xticks(range(len(variants)), variants)
        ax.set_ylim(-90, 3)
        ax.set_ylabel("Peak in the first 100 ms (dBFS), worst over")
        ax.set_title("Key-up transient")
        made["onset"] = _save(fig, out, "onset")
    li = [r for r in mine if r["test"] == "live"]
    if li:
        fig, ax = plt.subplots(figsize=(6.2, 4.0))
        labels = list(dict.fromkeys(r["label"] for r in li))
        w = 0.8 / max(1, len(variants))
        for j, v in enumerate(variants):
            for i, lab in enumerate(labels):
                x = [r["active_dbfs"] for r in li if r["variant"] == v and r["label"] == lab]
                if x:
                    ax.bar(i + (j - (len(variants) - 1) / 2) * w, x[0] + 90, bottom=-90,
                           width=w * 0.9, color=colour(v), label=v if i == 0 else None)
        ax.set_xticks(range(len(labels)), labels)
        ax.set_ylim(-90, 0)
        ax.set_ylabel("Decoded active speech level (dBFS)")
        ax.set_title("Live voice")
        ax.legend(fontsize=8)
        made["live"] = _save(fig, out, "live-voice")
    return made
