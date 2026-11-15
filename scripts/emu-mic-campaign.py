# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Run the M17 microphone-chain campaign: acceptance/m17/mic-chain.robot on the
emulator bench once per firmware variant, for one radio's mic chain model,
then a report comparing every variant with the baseline.

    uv run python scripts/emu-mic-campaign.py --radio radios/cs7000p.yaml \\
        --openrtx "$OPENRTX" --speech-dir "$SPEECH_DIR" \\
        --variants baseline prime_dc keep_sum hpf agc full --out runs/mic-cs7000p

Each variant is an ordinary Robot run in <out>/runs/<variant>/ (output.xml,
bench-Mic-Chain/ with measurements.json, tables, charts), to compare with
python -m oat.compare and publish with scripts/export-run.sh. Takes already
in the take cache for the same binary and input are reused. The method is
docs/emulator-mic-campaign.md.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from oat import emu_campaign as camp
from oat import emu_report
from oat.sanitize import scrub

REPO = Path(__file__).resolve().parents[1]
SUITE = REPO / "acceptance" / "m17" / "mic-chain.robot"


def short_sha(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--short=9", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


def robot_command(a, variant: str, firmware: str, out: Path) -> list[str]:
    cmd = [sys.executable, "-m", "robot", "-A", str(REPO / "bench" / "emulator.args"),
           "-v", f"BENCH:{a.bench}", "-v", f"RADIO:{a.radio}",
           "-v", f"VARIANT:{variant}", "-v", f"FIRMWARE:{firmware}",
           "-v", f"OPENRTX:{a.openrtx}", "-d", str(out)]
    for name, value in (("BUILD_ROOT", a.build_root), ("SPEECH_DIR", a.speech_dir),
                        ("TAKES", a.takes),
                        ("REUSE_TAKES", ",".join(map(str, a.reuse_takes))),
                        ("JOBS", a.jobs)):
        if value:
            cmd += ["-v", f"{name}:{value}"]
    for v in a.var:
        cmd += ["-v", v]
    for t in a.test:
        cmd += ["-t", t]
    return [*cmd, str(SUITE)]


def cross_report(out: Path, variants: list[str]) -> Path:
    """REPORT.md, tables and charts of every variant's run, side by side."""
    rows, envs, runs = [], [], {}
    for v in variants:
        bench = next((out / "runs" / v).glob("bench-*"), None)
        if bench is None or not (bench / "data" / "rows.json").exists():
            print(f"{v}: no tables, left out of the report", file=sys.stderr)
            continue
        r, e = camp.read_tables(bench / "data")
        seen = {x["variant"] for x in rows}
        rows += [x for x in r if x["variant"] == v or x["variant"] not in seen]
        envs += [x for x in e if x["variant"] == v or x["variant"] not in seen]
        runs[v] = json.loads((bench / "run.json").read_text())
    done = [v for v in variants if v in runs]
    if not done:
        raise SystemExit("no variant produced tables")
    meta = {**runs[done[0]],
            "variants": {v: {"defines": runs[v]["defines"],
                             "binary_sha256": runs[v]["binary_sha256"],
                             "firmware": runs[v]["firmware"]} for v in done},
            "started": min(runs[v]["started"] for v in done),
            "finished": max(runs[v]["finished"] for v in done),
            "runs": {v: f"runs/{v}" for v in done}}
    for k in ("variant", "defines", "c_args", "float_chain", "binary_sha256",
              "build_dir", "firmware"):
        meta.pop(k, None)
    camp.write_tables(out / "data", rows, envs)
    emu_report.write_csv(out / "data" / "headline.csv", emu_report.headline(rows, done))
    (out / "run.json").write_text(scrub(json.dumps(meta, indent=2)) + "\n")
    ch = emu_report.charts(rows, envs, done, out / "charts")
    (out / "REPORT.md").write_text(scrub(emu_report.report(meta, rows, done, ch)) + "\n")
    (out / "METHODS.md").write_text(scrub((REPO / "docs" / "emulator-mic-campaign.md")
                                          .read_text()))
    return out / "REPORT.md"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--radio", type=Path, required=True,
                    help="radio file whose mic_chain model goes in front of the "
                         "emulator (radios/emulator-bare.yaml for none)")
    ap.add_argument("--openrtx", type=Path, required=True, help="OpenRTX checkout")
    ap.add_argument("--variants", nargs="+", default=list(camp.VARIANTS),
                    choices=camp.VARIANTS)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--bench", type=Path, default=Path("bench/emulator.yaml"),
                    help="the emulator bench file, or a local copy holding the paths")
    ap.add_argument("--build-root", type=Path, default=None)
    ap.add_argument("--speech-dir", type=Path, default=None)
    ap.add_argument("--takes", type=Path, default=None,
                    help="take cache (default <out>/takes)")
    ap.add_argument("--reuse-takes", type=Path, nargs="*", default=[],
                    help="other take caches to reuse, such as an earlier campaign's takes/")
    ap.add_argument("--jobs", type=int, default=None)
    ap.add_argument("--var", action="append", default=[], metavar="NAME:VALUE",
                    help="a suite variable, such as 'SPEECH_LEVELS:-20 -6'")
    ap.add_argument("--test", action="append", default=[],
                    help="run only this test (Robot -t), repeatable")
    ap.add_argument("--report-only", action="store_true",
                    help="only rebuild the cross-variant report from the runs in --out")
    a = ap.parse_args()
    a.openrtx = a.openrtx.expanduser().resolve()
    a.out = a.out.expanduser().resolve()
    a.takes = (a.takes or a.out / "takes").expanduser().resolve()
    a.radio = a.radio.resolve()
    a.bench = a.bench if a.bench.is_absolute() else (REPO / a.bench).resolve()
    for k in ("build_root", "speech_dir"):
        if getattr(a, k):
            setattr(a, k, getattr(a, k).expanduser().resolve())
    a.reuse_takes = [p.expanduser().resolve() for p in a.reuse_takes]

    if not a.report_only:
        sha = short_sha(a.openrtx)
        for v in a.variants:
            cmd = robot_command(a, v, f"{v}@{sha}", a.out / "runs" / v)
            print(f"=== {v}", flush=True)
            rc = subprocess.run(cmd, cwd=REPO).returncode
            # Robot exits with the number of failed tests: limits may fail
            if rc > 250:
                raise SystemExit(f"{v}: robot failed ({rc})")
    report = cross_report(a.out, a.variants)
    runs = " ".join(str(a.out / "runs" / v) for v in a.variants)
    print(f"done: {report}\ncompare: uv run python -m oat.compare {runs}")


if __name__ == "__main__":
    main()
