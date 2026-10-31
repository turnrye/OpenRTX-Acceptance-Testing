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
            "suite_commit", "calibration")
# Suite metadata that must match too, when either run has it
SAME_METADATA = ("Firmware", "Radio", "Station", "Calibration", "Hardware",
                 "Suite commit")


def measurements_file(run: Path) -> Path:
    """The one bench-*/measurements.json of a run's output directory."""
    found = sorted(Path(run).glob("bench-*/measurements.json"))
    if len(found) != 1:
        raise ValueError(f"{run}: expected one bench-*/measurements.json")
    return found[0]


def merge_reruns(run: Path, reruns: list[Path]) -> dict:
    """
    A run's measurements with each rerun's tests in place of the run's own:
    the run's records for a rerun test are replaced by the rerun's, its
    capture files named under rerun<N>/. Refuses a rerun of a different
    firmware, radio, bench, calibration or suite commit.
    """
    merged = json.loads(measurements_file(run).read_text())
    meta = ExecutionResult(str(Path(run) / "output.xml")).suite.metadata
    for i, rerun in enumerate(reruns, 1):
        again = json.loads(measurements_file(rerun).read_text())
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
