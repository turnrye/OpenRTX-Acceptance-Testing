# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Compare runs of acceptance/m17/mic-chain-device.robot (one per firmware
image): one report per radio, each variant against the baseline, with charts
and the tables of every run. A run given after another of the same firmware
(a rerun of some tests, robot -t) replaces those tests.

    uv run python scripts/device-mic-report.py runs/mic-device-baseline \\
        runs/mic-device-full --out runs/mic-device-report
"""

import argparse
import json
from pathlib import Path

from oat import device_mic
from oat import emu_campaign as camp
from oat.sanitize import scrub

REPO = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="+", type=Path, help="run output directories")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    per_radio: dict[str, dict] = {}
    for run in a.runs:
        for folder in sorted(run.glob("bench-*-*/data/rows.json")):
            folder = folder.parent.parent
            m = json.loads((folder / "measurements.json").read_text())
            entry = per_radio.setdefault(m["radio_file"], {
                "name": m["radio"], "rows": [], "hardware": set(), "firmware": [],
                "suite_commit": set(), "station": m["station"], "playback": set()})
            new = json.loads((folder / "data" / "rows.json").read_text())
            # A later run (a rerun of some tests) replaces those tests' rows
            again = {(r["variant"], r["test"]) for r in new}
            entry["rows"] = [r for r in entry["rows"]
                             if (r["variant"], r["test"]) not in again] + new
            entry["hardware"].add(m["hardware"])
            entry["firmware"].append(m["firmware"])
            entry["suite_commit"].add(m["suite_commit"])
            entry["playback"].add(str((m.get("pair") or {}).get("playback")))
    if not per_radio:
        raise SystemExit("no bench-*-<radio>/data/rows.json in the runs")
    for stem, e in per_radio.items():
        out = a.out / stem
        camp.write_tables(out / "data", e["rows"], [])
        ch = device_mic.charts(e["rows"], stem, out / "charts")
        meta = {"hardware": "; ".join(sorted(e["hardware"])),
                "firmware": list(dict.fromkeys(e["firmware"])),
                "suite_commit": ", ".join(sorted(e["suite_commit"])),
                "station": e["station"], "playback": "; ".join(sorted(e["playback"]))}
        (out / "REPORT.md").write_text(scrub(device_mic.report(
            e["rows"], stem, e["name"], meta, ch)) + "\n")
        (out / "METHODS.md").write_text((REPO / "docs" / "device-mic-campaign.md").read_text())
        print(out / "REPORT.md")


if __name__ == "__main__":
    main()
