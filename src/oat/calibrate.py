# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Measure a bench's calibration with a LibreVNA and write it as
calibration/<station>.local.yaml, in the format calibration/example.yaml
documents:

- C1: the RF path losses, swept with the VNA (paths.to_receiver and
  paths.from_transmitter, as Touchstone files next to the YAML)
- C2: the transmitter's output by gain setting, read on the LibreVNA's
  spectrum analyser (levels.transmitter)
- C3: the receiver's full-scale input level, from the LibreVNA's generator
  (levels.receiver)
- C4: the receiver's reference error, from the generator or a known carrier
  (frequency_correction_ppm)
- C5: the audio interface's full-scale voltages, as the operator measured
  them (paths.audio_out, paths.audio_in)

Steps not run keep what an existing file has. Each step is a function
returning its part of the file, so tests can drive it with fakes.

    uv run python -m oat.calibrate --bench bench/mybench.local.yaml \\
        --radio radios/c62.yaml --steps C2,C3 --pad-db 30
"""

import argparse
import datetime
import json
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

from oat.calibration import AUDIO_PATHS, load_calibration
from oat.config import load_yaml
from oat.iq import write_iq
from oat.operator import Operator, make_operator, write_atomic
from oat.sdr import make_receiver, make_transmitter
from oat.sdr.base import Receiver, Transmitter
from oat.sdr.librevna import DEFAULT_HOST, DEFAULT_PORT, LibreVNA, \
    LibreVNAError, write_s2p
from oat.signals import fm_tone

STEPS = ("C1", "C2", "C3", "C4", "C5")
# The example calibration's band frequencies, when no radio is given
DEFAULT_FREQS = (146580000, 445975000, 462650000)
# tx_vga_db steps for the transmitter table; 47 is the HackRF's top
DEFAULT_GAINS = (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 47)
EXPIRY_MONTHS = 6
# Receiver settings a level table records, per bench receiver type
RECEIVER_SETTINGS = {"rtlsdr": ("gain_db",),
                     "hackrf": ("lna_db", "vga_db", "amp")}
# Keep the LibreVNA's input below this; the pad has to take the rest
SA_MAX_INPUT_DBM = 0.0
# Share of a capture's power the carrier must hold for its power to count
MIN_CARRIER_FRACTION = 0.5
# How far from nominal to look for a carrier, in ppm of its frequency
SEARCH_PPM = 50.0
# Time a real receiver takes to settle after starting, dropped from captures
SETTLE_S = 0.05
SECTION_ORDER = ("method", "measured", "expires", "paths", "levels",
                 "frequency_correction_ppm")
PATH_ORDER = ("to_receiver", "from_transmitter") + AUDIO_PATHS


class Aborted(Exception):
    """The operator stopped a step."""


@dataclass
class Fragment:
    """
    What a step measured: part of the calibration file, and comments to put
    above its sections, keyed by dotted name (frequency_correction_ppm.rtlsdr).
    """

    data: dict
    notes: dict[str, str] = field(default_factory=dict)


def add_months(day: datetime.date, months: int) -> datetime.date:
    """The same day some months on, or the month's last day if it's short."""
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    for d in range(day.day, 0, -1):
        try:
            return datetime.date(year, month, d)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def instrument_name(cfg: dict) -> str:
    """A bench section's name in a calibration: type, or type:serial."""
    if cfg.get("serial"):
        return f"{cfg['type']}:{cfg['serial']}"
    return str(cfg["type"])


def receiver_settings(cfg: dict) -> dict:
    """The receiver settings a level table must match, from the bench."""
    defaults = {"gain_db": 0, "lna_db": 0, "vga_db": 0, "amp": False}
    return {k: cfg.get(k, defaults[k])
            for k in RECEIVER_SETTINGS.get(cfg["type"], ())}


def capture_iq(receiver: Receiver, center_hz: float, rate: float, path: Path,
               capture_s: float) -> np.ndarray:
    """Record for capture_s and return the samples, settling time dropped."""
    receiver.start(center_hz, rate, path)
    time.sleep(capture_s)
    iq = receiver.stop().load()
    settle = int(SETTLE_S * rate)
    return iq[settle:] if len(iq) > 4 * settle else iq


def dbfs(iq: np.ndarray) -> float:
    """
    Power relative to a full-scale complex sinusoid. The mean is removed
    first: an 8-bit receiver's DC offset isn't the carrier.
    """
    x = iq - np.mean(iq)
    return float(10 * np.log10(np.mean(np.abs(x) ** 2)))


def clipped(iq: np.ndarray) -> float:
    """Fraction of samples at the converter's full scale."""
    return float(np.mean((np.abs(iq.real) >= 0.99)
                         | (np.abs(iq.imag) >= 0.99)))


def _spectrum(iq: np.ndarray, rate: float, expected_hz: float,
              search_hz: float) -> tuple[np.ndarray, np.ndarray, int]:
    """Power spectrum, its frequencies, and the peak bin near expected_hz."""
    n = len(iq)
    spectrum = np.abs(np.fft.fftshift(np.fft.fft(
        (iq - np.mean(iq)) * np.hanning(n)))) ** 2
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1 / rate))
    window = np.flatnonzero(np.abs(freqs - expected_hz) <= search_hz)
    if not len(window):
        raise ValueError("the search window is outside the capture")
    return spectrum, freqs, int(window[np.argmax(spectrum[window])])


def carrier_fraction(iq: np.ndarray, rate: float, expected_hz: float,
                     search_hz: float) -> float:
    """
    Share of the capture's power in the strongest carrier near expected_hz:
    the peak bin and the Hann window's main lobe around it.
    """
    spectrum, _, i = _spectrum(iq, rate, expected_hz, search_hz)
    return float(spectrum[max(0, i - 3):i + 4].sum() / spectrum.sum())


def carrier_offset(iq: np.ndarray, rate: float, expected_hz: float,
                   search_hz: float) -> float:
    """
    Frequency of the strongest carrier within search_hz of expected_hz,
    relative to the capture's centre, interpolated between FFT bins.
    """
    spectrum, freqs, i = _spectrum(iq, rate, expected_hz, search_hz)
    n = len(iq)
    if 0 < i < n - 1:
        # Parabola through the log magnitudes of the peak and its neighbours
        a, b, c = np.log(spectrum[i - 1:i + 2] + 1e-30)
        shift = 0.5 * (a - c) / (a - 2 * b + c) if a - 2 * b + c else 0.0
    else:
        shift = 0.0
    return float(freqs[i] + shift * rate / n)


# C1

PATH_INSTRUCTIONS = {
    "to_receiver": "Connect the to_receiver chain (the attenuators between "
                   "the radio's antenna port and the receiver, with their "
                   "cables) between LibreVNA port 1 (radio side) and port 2",
    "from_transmitter": "Connect the from_transmitter chain (transmitter "
                        "output to the radio's antenna port, with its "
                        "cables) between LibreVNA port 1 (transmitter side) "
                        "and port 2",
}


def measure_paths(vna: LibreVNA, operator: Operator, out_dir: Path,
                  stem: str, today: datetime.date, start_hz: float = 100e6,
                  stop_hz: float = 600e6, points: int = 501,
                  ifbw_hz: float = 1000, level_dbm: float = -10) -> Fragment:
    """C1: sweep each RF path and write it as <stem>-<path>.local.s2p."""
    cal = vna.calibration_active()
    if cal.strip().upper() in ("", "NONE"):
        ok, _ = operator.confirm(
            "LibreVNA-GUI has no VNA calibration active. Losses measured "
            "without a SOLT calibration at the cable ends are wrong. "
            "Continue anyway?")
        if not ok:
            raise Aborted("calibrate the LibreVNA (SOLT, at the ends of the "
                          "cables the paths use) and run C1 again")
    paths, notes = {}, {}
    for name, text in PATH_INSTRUCTIONS.items():
        operator.instruct(text)
        freqs, s = vna.vna_sweep(start_hz=start_hz, stop_hz=stop_hz,
                                 points=points, ifbw_hz=ifbw_hz,
                                 level_dbm=level_dbm)
        file = f"{stem}-{name}.local.s2p"
        write_s2p(out_dir / file, freqs, s,
                  f"{name}, LibreVNA, {cal} calibration, {today}")
        paths[name] = {"touchstone": file}
        loss = -20 * np.log10(np.abs(s["S21"]) + 1e-30)
        notes[f"paths.{name}"] = (
            f"LibreVNA sweep {start_hz / 1e6:g}-{stop_hz / 1e6:g} MHz, "
            f"{points} points, {ifbw_hz:g} Hz IFBW: loss "
            f"{loss.min():.1f}-{loss.max():.1f} dB")
    data = {"method": "vna", "measured": today,
            "expires": add_months(today, EXPIRY_MONTHS), "paths": paths}
    return Fragment(data, notes)


# C2

def measure_transmitter(vna: LibreVNA, transmitter: Transmitter,
                        tx_cfg: dict, operator: Operator, freqs: list[float],
                        gains: list[float], pad_db: float, workdir: Path,
                        today: datetime.date, settle_s: float = 0.5,
                        span_hz: float = 100e3,
                        rbw_hz: float = 1e3) -> Fragment:
    """
    C2: the transmitter's output in dBm at each gain and frequency, as the
    LibreVNA's spectrum analyser reads it through a pad of pad_db, added
    back. The signal is a carrier at signals.IQ_AMPLITUDE, the level every
    bench signal has, put beside the transmitter's tuning so its LO leakage
    isn't read.
    """
    rate = float(tx_cfg.get("rate", 2e6))
    offset = float(tx_cfg.get("tune_offset_hz", 0))
    base = float(tx_cfg.get("tx_vga_db", 0))
    # 10 ms: a whole number of cycles at any offset in 100 Hz steps, so the
    # looped file has no phase jump
    iq_path = workdir / f"carrier.{transmitter.fmt.value}"
    write_iq(iq_path, fm_tone(1000, 0, 0.01, rate, -offset), transmitter.fmt)
    operator.instruct(
        f"Connect the transmitter output straight to LibreVNA port 1 through "
        f"the {pad_db:g} dB pad, so the LibreVNA sees under "
        f"{SA_MAX_INPUT_DBM:g} dBm. Leave the radio out of it")
    points = {}
    for f in freqs:
        table = {}
        for gain in gains:
            applied = transmitter.start(iq_path, f + offset, rate, True,
                                        gain - base)
            try:
                time.sleep(settle_s)
                peak_hz, dbm = vna.sa_peak(f, span_hz, rbw_hz)
            finally:
                transmitter.stop()
            if abs(peak_hz - f) > span_hz / 4:
                raise ValueError(f"no carrier near {f / 1e6:.3f} MHz (the "
                                 f"strongest is at {peak_hz / 1e6:.4f} MHz): "
                                 f"check the connection")
            if dbm > SA_MAX_INPUT_DBM:
                raise ValueError(f"{dbm:.1f} dBm at the LibreVNA is too "
                                 f"much: use a bigger pad")
            table[round(base + applied, 2)] = round(dbm + pad_db, 2)
        points[int(round(f))] = [[g, table[g]] for g in sorted(table)]
    data = {"levels": {"transmitter": {
        "instrument": instrument_name(tx_cfg),
        "amp": bool(tx_cfg.get("amp", False)), "points": points}}}
    note = (f"LibreVNA spectrum analyser through a {pad_db:g} dB pad, "
            f"{rbw_hz:g} Hz RBW, {today}")
    return Fragment(data, {"levels.transmitter": note})


# C3

def measure_receiver(vna: LibreVNA, receiver: Receiver, rx_cfg: dict,
                     operator: Operator, freqs: list[float], gen_dbm: float,
                     pad_db: float, workdir: Path, today: datetime.date,
                     capture_s: float = 0.5) -> Fragment:
    """
    C3: the input level that reads 0 dBFS, at the bench's receiver
    settings, from a LibreVNA carrier of gen_dbm through a pad of pad_db.
    """
    rate = float(rx_cfg.get("rate", 250000))
    offset = float(rx_cfg.get("tune_offset_hz", 0))
    level = gen_dbm - pad_db
    if level > receiver.max_input_dbm:
        raise ValueError(f"{level:.1f} dBm is above the receiver's "
                         f"{receiver.max_input_dbm:g} dBm maximum input")
    operator.instruct(f"Connect LibreVNA port 1 to the receiver input "
                      f"through the {pad_db:g} dB pad")
    table = []
    try:
        for f in freqs:
            vna.generator_cw(f, gen_dbm)
            iq = capture_iq(receiver, f + offset, rate,
                            workdir / f"rx-{int(f)}.iq", capture_s)
            if clipped(iq) > 1e-3:
                raise ValueError(f"the receiver clips at {f / 1e6:.3f} MHz: "
                                 f"lower --gen-dbm or add pad")
            # Noise or another signal would read as carrier power
            if carrier_fraction(iq, rate, -offset,
                                SEARCH_PPM * 1e-6 * f) < MIN_CARRIER_FRACTION:
                raise ValueError(f"no clean carrier at {f / 1e6:.3f} MHz: "
                                 f"check the connection")
            power = dbfs(iq)
            table.append([int(round(f)), round(level - power, 2)])
    finally:
        vna.generator_off()
    entry = {"instrument": instrument_name(rx_cfg),
             "settings": receiver_settings(rx_cfg), "full_scale_dbm": table}
    note = (f"{entry['instrument']}: LibreVNA generator at {gen_dbm:g} dBm "
            f"through a {pad_db:g} dB pad, {today}")
    return Fragment({"levels": {"receiver": [entry]}},
                    {"levels.receiver": note})


# C4

def measure_frequency(vna: LibreVNA | None, receiver: Receiver, rx_cfg: dict,
                      operator: Operator, freq_hz: float, gen_dbm: float,
                      workdir: Path, today: datetime.date,
                      capture_s: float = 0.5,
                      search_ppm: float = SEARCH_PPM) -> Fragment:
    """
    C4: the receiver's reference error in ppm, positive when it runs fast
    and so reads carriers low, against the LibreVNA's generator or a
    carrier whose frequency the operator knows.
    """
    # The FFT peak is only the carrier when the carrier dominates: an
    # unmodulated one, or FM between transmissions
    question = ("Frequency of a known carrier to measure against, in MHz "
                "(a NOAA weather station between words, say)")
    if vna is not None:
        question += (f", or leave blank to use the LibreVNA generator at "
                     f"{freq_hz / 1e6:.3f} MHz")
    answer = operator.ask(question)
    if not answer and vna is None:
        raise Aborted("C4 needs a known carrier without a LibreVNA")
    if answer:
        true_hz = float(answer) * 1e6
        uncertainty_hz = float(operator.ask(
            "Uncertainty of that carrier's frequency, in Hz") or 0)
        operator.instruct(f"Connect the receiver to the "
                          f"{true_hz / 1e6:.4f} MHz carrier")
        source = (f"a carrier known to be {true_hz / 1e6:.4f} MHz "
                  f"+-{uncertainty_hz:g} Hz "
                  f"(+-{uncertainty_hz / true_hz * 1e6:.2f} ppm)")
    else:
        true_hz = freq_hz
        operator.instruct("Connect LibreVNA port 1 to the receiver input "
                          "through a pad")
        vna.generator_cw(true_hz, gen_dbm)
        source = (f"the LibreVNA generator at {true_hz / 1e6:.4f} MHz, "
                  f"its own reference error included")
    rate = float(rx_cfg.get("rate", 250000))
    offset = float(rx_cfg.get("tune_offset_hz", 0))
    center = true_hz + offset
    try:
        iq = capture_iq(receiver, center, rate, workdir / "ppm.iq", capture_s)
    finally:
        if not answer:
            vna.generator_off()
    found = carrier_offset(iq, rate, -offset, search_ppm * 1e-6 * true_hz)
    measured_hz = center + found
    # Fast reference: the receiver tunes high, so carriers read low
    ppm = round((true_hz - measured_hz) / true_hz * 1e6, 3)
    name = instrument_name(rx_cfg)
    return Fragment({"frequency_correction_ppm": {name: ppm}},
                    {f"frequency_correction_ppm.{name}":
                     f"against {source}, {today}"})


# C5

def measure_audio(operator: Operator, today: datetime.date) -> Fragment:
    """C5: the audio interface's full-scale voltages, as measured by hand."""
    hints = {"audio_out": "the RMS millivolts at the radio's mic input of a "
                          "1 kHz sine played at 0 dBFS",
             "audio_in": "the RMS millivolts of a 1 kHz sine at the "
                         "interface input that reads 0 dBFS"}
    paths, notes = {}, {}
    for direction in AUDIO_PATHS:
        answer = operator.ask(f"{direction}: {hints[direction]} (blank to "
                              f"skip)")
        if not answer:
            continue
        mv = float(answer)
        if mv <= 0:
            raise ValueError(f"{direction} must be above 0 mVrms")
        paths[direction] = {"mvrms_at_0dbfs": mv}
        notes[f"paths.{direction}"] = f"measured by hand, {today}"
    return Fragment({"paths": paths} if paths else {}, notes)


# The file

def sections(fragment: Fragment) -> list[str]:
    """Dotted names of what a fragment sets."""
    names = []
    for key, value in fragment.data.items():
        if key in ("paths", "frequency_correction_ppm"):
            names += [f"{key}.{k}" for k in value]
        elif key == "levels":
            names += [f"levels.{k}" for k in value]
        else:
            names.append(key)
    return names


def merge(data: dict, fragment: Fragment) -> dict:
    """
    A calibration with a fragment's sections replaced. Receiver tables are
    replaced only for the same instrument and settings.
    """
    out = dict(data)
    for key, value in fragment.data.items():
        if key in ("paths", "frequency_correction_ppm"):
            out[key] = {**(out.get(key) or {}), **value}
        elif key == "levels":
            levels = dict(out.get("levels") or {})
            for name, spec in value.items():
                if name == "receiver":
                    kept = [t for t in levels.get("receiver") or []
                            if not any(t.get("instrument") == n["instrument"]
                                       and (t.get("settings") or {})
                                       == n["settings"] for n in spec)]
                    levels["receiver"] = kept + spec
                else:
                    levels[name] = spec
            out["levels"] = levels
        else:
            out[key] = value
    return out


def read_notes(text: str) -> dict[str, str]:
    """
    Comments directly above the sections of an existing file, two levels
    deep, so a rewrite keeps them.
    """
    notes, pending, parent = {}, [], None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            pending = []
            continue
        if stripped.startswith("#"):
            pending.append(stripped[1:].strip())
            continue
        indent = len(line) - len(line.lstrip())
        m = re.match(r"([^\s#-][^\s#]*?)\s*:(\s|$)", stripped)
        name = None
        if m and indent == 0:
            parent = name = m.group(1).strip("'\"")
        elif m and indent == 2 and parent:
            name = f"{parent}.{m.group(1).strip(chr(39) + chr(34))}"
        if name and pending:
            notes[name] = "\n".join(pending)
        pending = []
    return notes


def _scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if isinstance(v, (float, np.floating)):
        # PyYAML only reads a number with a dot, and no bare exponent, as a
        # float
        text = np.format_float_positional(float(v))
        return text + "0" if text.endswith(".") else text
    if isinstance(v, datetime.date):
        return v.isoformat()
    s = str(v)
    if re.fullmatch(r"[A-Za-z_][\w.:/-]*", s) and yaml.safe_load(s) == s:
        return s
    return json.dumps(s)


def _flow(v) -> str:
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_scalar(k)}: {_flow(x)}"
                               for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_flow(x) for x in v) + "]"
    return _scalar(v)


def _is_scalar(v) -> bool:
    return not isinstance(v, (dict, list, tuple))


def _inline(v, depth: int) -> bool:
    if _is_scalar(v):
        return True
    if isinstance(v, dict):
        return depth >= 3 and all(_is_scalar(x) for x in v.values())
    return len(v) <= 16 and all(
        _is_scalar(x) or (isinstance(x, (list, tuple))
                          and all(_is_scalar(y) for y in x)) for x in v)


def _ordered(mapping: dict, order: tuple) -> list:
    keys = [k for k in order if k in mapping]
    return keys + [k for k in mapping if k not in keys]


def _block(mapping: dict, indent: int, prefix: str,
           notes: dict[str, str]) -> list[str]:
    pad = " " * indent
    depth = prefix.count(".") + 1 if prefix else 0
    order = {"": SECTION_ORDER, "paths": PATH_ORDER}.get(prefix, ())
    lines = []
    for key in _ordered(mapping, order):
        value = mapping[key]
        name = f"{prefix}.{key}" if prefix else str(key)
        # A blank line between top-level sections, the dates kept together
        if depth == 0 and lines and key not in ("measured", "expires"):
            lines.append("")
        if depth < 2 and name in notes:
            lines += [f"{pad}# {n}".rstrip() for n in notes[name].splitlines()]
        if _inline(value, depth + 1):
            lines.append(f"{pad}{_scalar(key)}: {_flow(value)}")
        elif isinstance(value, dict):
            lines.append(f"{pad}{_scalar(key)}:")
            lines += _block(value, indent + 2, name, notes)
        else:
            lines.append(f"{pad}{_scalar(key)}:")
            for item in value:
                if isinstance(item, dict):
                    sub = _block(item, indent + 4, f"{name}.-", notes)
                    lines.append(f"{pad}  - {sub[0].lstrip()}")
                    lines += sub[1:]
                else:
                    lines.append(f"{pad}  - {_flow(item)}")
    return lines


def render(data: dict, notes: dict[str, str], header: str = "") -> str:
    """A calibration as YAML text, laid out as calibration/example.yaml."""
    lines = [f"# {h}".rstrip() for h in header.splitlines()]
    if lines:
        lines.append("")
    return "\n".join(lines + _block(data, 0, "", notes)) + "\n"


def _plain(v):
    """Data as YAML reads it back, for checking a rendering."""
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, (np.integer, np.floating)):
        return v.item()
    return v


def write_calibration(path: Path, fragments: list[Fragment],
                      today: datetime.date) -> dict:
    """
    Merge fragments into the calibration at path, creating it if need be,
    and write it. Returns the merged data.
    """
    path = Path(path)
    data, notes = {}, {}
    if path.exists():
        text = path.read_text()
        data = yaml.safe_load(text) or {}
        notes = read_notes(text)
    for fragment in fragments:
        for name in sections(fragment):
            notes.pop(name, None)
        data = merge(data, fragment)
        notes.update(fragment.notes)
    if "method" not in data:
        # The loader needs a method and dates even with no paths measured
        data = merge(data, Fragment({
            "method": "assumed", "measured": today,
            "expires": add_months(today, EXPIRY_MONTHS)}))
        notes["method"] = "No RF paths measured yet: run C1"
    text = render(data, notes, f"Written by python -m oat.calibrate, {today}."
                               f"\nSee calibration/example.yaml.")
    if yaml.safe_load(text) != _plain(data):
        raise RuntimeError("calibration rendering doesn't read back alike")
    write_atomic(path, text)
    load_calibration(path)
    return data


# Command line

def parse_steps(text: str) -> list[str]:
    """C1,C3 as a list in the order they run."""
    steps = {s.strip().upper() for s in text.split(",") if s.strip()}
    unknown = steps - set(STEPS)
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown steps {', '.join(sorted(unknown))}; use "
            f"{', '.join(STEPS)}")
    return [s for s in STEPS if s in steps]


def band_frequencies(radios: list[Path]) -> list[int]:
    """Every band frequency of the given radio files, or the defaults."""
    freqs = set()
    for path in radios:
        for band in (load_yaml(path).get("bands") or {}).values():
            freqs.add(int(band["frequency_hz"]))
    return sorted(freqs) or list(DEFAULT_FREQS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m oat.calibrate",
        description="Measure a bench calibration with a LibreVNA.")
    parser.add_argument("--bench", type=Path, required=True,
                        help="bench file whose instruments to calibrate")
    parser.add_argument("--output", type=Path,
                        help="calibration file to write or update (default "
                             "calibration/<station>.local.yaml)")
    parser.add_argument("--steps", type=parse_steps, default=list(STEPS),
                        help="steps to run, as C1,C3 (default all)")
    parser.add_argument("--radio", type=Path, action="append", default=[],
                        help="radio file whose band frequencies to measure "
                             "at; repeatable")
    parser.add_argument("--freqs",
                        help="frequencies in Hz, comma separated, instead")
    parser.add_argument("--gains", default=",".join(map(str, DEFAULT_GAINS)),
                        help="transmitter gains for C2, comma separated")
    parser.add_argument("--pad-db", type=float, default=0.0,
                        help="attenuator between the LibreVNA and the SDR in "
                             "C2 and C3, added back")
    parser.add_argument("--gen-dbm", type=float, default=-30.0,
                        help="LibreVNA generator level for C3 and C4")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="LibreVNA-GUI SCPI server host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="LibreVNA-GUI SCPI server port")
    parser.add_argument("--vna-cal", type=Path,
                        help="LibreVNA calibration file for the GUI to load "
                             "before C1")
    args = parser.parse_args(argv)

    bench = load_yaml(args.bench)
    station = bench.get("station", args.bench.stem.split(".")[0])
    output = args.output or Path("calibration") / f"{station}.local.yaml"
    stem = output.name.split(".")[0]
    freqs = ([int(float(f)) for f in args.freqs.split(",")] if args.freqs
             else band_frequencies(args.radio))
    gains = [float(g) for g in args.gains.split(",")]
    operator = make_operator(bench.get("operator") or {})
    today = datetime.date.today()
    output.parent.mkdir(parents=True, exist_ok=True)

    vna = None
    if set(args.steps) & {"C1", "C2", "C3", "C4"}:
        try:
            vna = LibreVNA(args.host, args.port).connect()
            print(f"LibreVNA-GUI: {vna.idn()}, device "
                  f"{vna.connect_device()}")
        except LibreVNAError as e:
            if set(args.steps) & {"C1", "C2", "C3"}:
                print(e, file=sys.stderr)
                return 1
            print(f"{e}; C4 needs a known carrier", file=sys.stderr)
            vna = None
    fragments = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            if "C1" in args.steps:
                if args.vna_cal:
                    vna.load_calibration(args.vna_cal)
                fragments.append(measure_paths(vna, operator, output.parent,
                                               stem, today))
            if "C2" in args.steps:
                tx_cfg = bench["transmitter"]
                fragments.append(measure_transmitter(
                    vna, make_transmitter(tx_cfg), tx_cfg, operator, freqs,
                    gains, args.pad_db, workdir, today))
            if {"C3", "C4"} & set(args.steps):
                rx_cfg = bench["receiver"]
                receiver = make_receiver(rx_cfg)
                if "C3" in args.steps:
                    fragments.append(measure_receiver(
                        vna, receiver, rx_cfg, operator, freqs, args.gen_dbm,
                        args.pad_db, workdir, today))
                if "C4" in args.steps:
                    fragments.append(measure_frequency(
                        vna, receiver, rx_cfg, operator, freqs[-1],
                        args.gen_dbm, workdir, today))
            if "C5" in args.steps:
                fragments.append(measure_audio(operator, today))
    except Aborted as e:
        print(f"stopped: {e}", file=sys.stderr)
        return 1
    finally:
        if vna is not None:
            vna.close()
    write_calibration(output, fragments, today)
    cal = load_calibration(output)
    print(f"wrote {output}: {', '.join(cal.calibrated()) or 'paths only'}, "
          f"expires {cal.expires}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
