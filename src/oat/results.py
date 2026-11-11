# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Runs as published: merging reruns of some of a run's tests into it, so one
result tells the whole story. rebot --merge does the same for output.xml.
"""

import json
from pathlib import Path

from robot.api import ExecutionResult

# measurements.json fields a rerun must share with the run it repeats
SAME_RUN = ("schema", "suite", "station", "radio", "radio_file", "firmware",
            "hardware", "dsp", "suite_commit", "calibration")
# Suite metadata that must match too, when either run has it
SAME_METADATA = ("Firmware", "Radio", "Station", "Calibration", "Hardware",
                 "DSP", "Suite commit")


def run_measurements(run: Path) -> dict:
    """
    A run's measurements. A run of several suites (robot given several
    files) has a bench-*/measurements.json per suite: they are combined
    under the run's top suite, each suite's capture files named under its
    bench folder. Refuses suites run on different firmware, radios or
    benches.
    """
    found = sorted(Path(run).glob("bench-*/measurements.json"))
    if not found:
        raise ValueError(f"{run}: no bench-*/measurements.json")
    parts = [json.loads(f.read_text()) for f in found]
    if len(parts) == 1:
        return parts[0]
    first = parts[0]
    differ = sorted({k for p in parts[1:] for k in SAME_RUN
                     if k != "suite" and p.get(k) != first.get(k)})
    if differ:
        raise ValueError(f"{run}: its suites differ in {', '.join(differ)}")
    combined = {**first,
                "suite": ExecutionResult(str(Path(run) / "output.xml"))
                .suite.name,
                "started": min(p["started"] for p in parts),
                "suites": [p["suite"] for p in parts],
                "records": [renamed(r, f"{f.parent.name}/")
                            for f, p in zip(found, parts)
                            for r in p["records"]],
                "operator": [x for p in parts for x in p.get("operator", [])]}
    if any("tnc_junk" in p for p in parts):
        combined["tnc_junk"] = sum(p.get("tnc_junk", 0) for p in parts)
    return combined


def merge_reruns(run: Path, reruns: list[Path]) -> dict:
    """
    A run's measurements with each rerun's tests in place of the run's own:
    the run's records for a rerun test are replaced by the rerun's, its
    capture files named under rerun<N>/. Refuses a rerun of a different
    firmware, radio, bench, calibration or suite commit.
    """
    merged = run_measurements(run)
    meta = ExecutionResult(str(Path(run) / "output.xml")).suite.metadata
    for i, rerun in enumerate(reruns, 1):
        again = run_measurements(rerun)
        result = ExecutionResult(str(Path(rerun) / "output.xml"))
        differ = [k for k in SAME_RUN if again.get(k) != merged.get(k)]
        differ += [k for k in SAME_METADATA
                   if result.suite.metadata.get(k) != meta.get(k)]
        if differ:
            raise ValueError(f"{rerun} is not a rerun of {run}: "
                             f"{', '.join(differ)} differ")
        tests = [t.name for t in result.suite.all_tests]
        records = [renamed(r, f"rerun{i}/") for r in again["records"]]
        merged["records"] = replace_tests(merged["records"], records, tests)
        merged["operator"] = merged.get("operator", []) + again.get(
            "operator", [])
        merged.setdefault("reruns", []).append(
            {"started": again["started"], "tests": tests})
    return merged


def replace_tests(records: list[dict], new: list[dict],
                  tests: list[str]) -> list[dict]:
    """
    Records with each of the tests' records swapped for the new ones, where
    the old ones were; tests the records lacked go at the end.
    """
    out, placed = [], set()
    for r in records:
        test = r.get("test")
        if test not in tests:
            out.append(r)
        elif test not in placed:
            out += [n for n in new if n.get("test") == test]
            placed.add(test)
    return out + [n for n in new if n.get("test") not in placed]


def renamed(record: dict, prefix: str) -> dict:
    """A record with its capture files named under prefix."""
    if "files" not in record:
        return record
    return {**record, "files": [prefix + f for f in record["files"]]}
