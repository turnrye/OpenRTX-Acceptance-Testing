# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Runs side by side: the measurements of several runs of the same suites, on
different firmware states, as one Markdown table per suite with a column
per run, for a pull request comment.

    uv run python -m oat.compare runs/stock runs/before runs/after

Each argument is a run's output directory (holding bench-*/measurements.json)
or a published results folder (holding measurements.json).
"""

import argparse
import json
from pathlib import Path

# The fields shown per transmission by default: what the suites check or
# compare. --all shows every number recorded.
CAPTURE_FIELDS = ("peak_deviation_hz", "rms_deviation_hz", "tone_deviation_hz",
                  "occupied_bandwidth_hz", "ctcss_hz", "ctcss_deviation_hz",
                  "frequency_error_hz", "power_db", "radio_power_dbm")
M17_FIELDS = ("outer_deviation_hz", "inner_deviation_hz", "deviation_ratio",
              "evm_percent", "symbol_rate_error_ppm", "frequency_error_hz",
              "occupied_bandwidth_hz", "preamble_s", "lsf_latency_s",
              "stream_frames", "lsf_crc_ok", "polarity", "source",
              "voice_peak_dbfs", "voice_clipped", "radio_power_dbm")
AUDIO_FIELDS = ("tone_hz", "level_dbfs", "level_mvrms", "tone_amplitude",
                "sinad_db", "thd_n_percent", "longest_gap_s")
# Bookkeeping, never shown
SKIP = {"test", "band", "kind", "files", "file", "audio_file", "voice_file",
        "baseband_file", "instruction", "text", "note", "restarts", "rate",
        "unit", "values", "points"}


def load_run(path: Path) -> dict:
    """A run's measurements.json, from its folder."""
    path = Path(path)
    direct = path / "measurements.json"
    if direct.exists():
        return json.loads(direct.read_text())
    found = sorted(path.glob("bench-*/measurements.json"))
    if len(found) != 1:
        raise ValueError(f"{path}: expected one measurements.json")
    return json.loads(found[0].read_text())


def _shown(value) -> bool:
    return isinstance(value, (int, float, str, bool)) and value is not None


def rows(run: dict, everything: bool = False) -> dict[tuple, object]:
    """
    A run's measurements as {(test, label): value}. Repeated records of a
    kind within a test are numbered in order, so the same test on another
    run lines up with them.
    """
    out: dict[tuple, object] = {}
    seen: dict[tuple, int] = {}
    for r in run.get("records", []):
        test, kind = r.get("test", ""), r.get("kind", "")
        n = seen[(test, kind)] = seen.get((test, kind), 0) + 1
        tag = kind if n == 1 else f"{kind} {n}"
        if kind in ("capture", "m17-capture"):
            fields = CAPTURE_FIELDS if kind == "capture" else M17_FIELDS
            for i, t in enumerate(r.get("transmissions", []), 1):
                for k, v in t.items():
                    if (everything or k in fields) and k not in SKIP \
                            and _shown(v):
                        out[(test, f"{tag} tx{i} {k}")] = v
        elif kind == "limit":
            out[(test, f"{r.get('field')}")] = r.get("value")
        elif kind == "power":
            for s, db in zip(r.get("settings", []), r.get("relative_db", [])):
                out[(test, f"power {s} (dB)")] = db
        elif kind == "response":
            for lv in r.get("levels", []):
                out[(test, f"response {lv['tone_hz']:.0f} Hz (dB)")] = \
                    lv["level_db"]
        elif kind == "images":
            for f, db in r.get("levels_db", {}).items():
                out[(test, f"image {float(f):.0f} Hz (dB)")] = db
        elif kind == "audio":
            for k in (r if everything else AUDIO_FIELDS):
                if k in r and k not in SKIP and _shown(r[k]):
                    out[(test, f"{tag} {k}")] = r[k]
        elif kind in ("operator", "assertion", "transmit", "inject") \
                and not everything:
            continue
        else:
            for k, v in r.items():
                if k not in SKIP and _shown(v):
                    out[(test, f"{tag} {k}")] = v
    return out


def column(run: dict) -> str:
    """A run's column heading: firmware, DSP image, date."""
    head = run.get("firmware", "?")
    if run.get("dsp"):
        head += f" / DSP {str(run['dsp'])[:8]}"
    return f"{head} ({run.get('started', '')[:10]})"


def fmt(value) -> str:
    """A cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value).replace("|", "\\|")


def compare(runs: list[dict], everything: bool = False) -> str:
    """The runs as Markdown: a table per suite, a row per measurement."""
    out = []
    suites = list(dict.fromkeys(r.get("suite", "?") for r in runs))
    for suite in suites:
        group = [r for r in runs if r.get("suite", "?") == suite]
        tables = [rows(r, everything) for r in group]
        keys = list(dict.fromkeys(k for t in tables for k in t))
        out.append(f"### {suite}\n")
        radios = sorted({f"{r.get('radio')} ({r.get('hardware', '?')})"
                         for r in group})
        out.append(f"Radio: {', '.join(radios)}\n")
        out.append("| Test | Measurement | "
                   + " | ".join(column(r) for r in group) + " |")
        out.append("|---|---|" + "---:|" * len(group))
        last = None
        for test, label in keys:
            shown = test if test != last else ""
            last = test
            out.append(f"| {fmt(shown)} | {fmt(label)} | "
                       + " | ".join(fmt(t.get((test, label))) for t in tables)
                       + " |")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    """Print runs side by side: python -m oat.compare <run>..."""
    ap = argparse.ArgumentParser(prog="python -m oat.compare",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="+", type=Path,
                    help="run output directories or results folders")
    ap.add_argument("--all", action="store_true",
                    help="every number recorded, not only the checked ones")
    args = ap.parse_args(argv)
    print(compare([load_run(r) for r in args.runs], args.all))


if __name__ == "__main__":
    main()
