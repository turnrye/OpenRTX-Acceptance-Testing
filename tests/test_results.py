# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Exporting a run into the results tree, and the runs checked in there."""

import json
import subprocess
from pathlib import Path

import pytest
import robot
from robot.api import ExecutionResult

from oat.sanitize import private_paths

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "scripts" / "export-run.sh"
PROVENANCE = ("schema", "suite", "started", "station", "radio", "radio_file",
              "firmware", "suite_commit", "bands", "calibration")


def check_run(folder: Path) -> None:
    """A published run: both files present, provenance, no private paths."""
    xml = (folder / "output.xml").read_text()
    assert private_paths(xml) == [], folder
    meta = ExecutionResult(str(folder / "output.xml")).suite.metadata
    assert {"Firmware", "Station", "Calibration"} <= set(meta), folder
    text = (folder / "measurements.json").read_text()
    assert private_paths(text) == [], folder
    measurements = json.loads(text)
    assert all(k in measurements for k in PROVENANCE), folder
    assert measurements["firmware"] == meta["Firmware"], folder


@pytest.fixture(scope="module")
def selftest_run(tmp_path_factory):
    """Output directory of a self-test suite run on the simulated bench."""
    out = tmp_path_factory.mktemp("selftest")
    assert robot.run(str(ROOT / "acceptance" / "selftest.robot"),
                     outputdir=str(out), console="none", report="NONE",
                     log="NONE") == 0
    return out


def test_export_script_publishes_a_clean_run(selftest_run, tmp_path):
    results = tmp_path / "results"
    done = subprocess.run(["bash", str(EXPORT), str(selftest_run),
                           str(results)], capture_output=True, text=True,
                          cwd=ROOT)
    assert done.returncode == 0, done.stderr
    folder = Path(done.stdout.strip())
    assert folder.parts[-3:-1] == ("sim", "c62")
    assert folder.name.endswith("-sim-Selftest")
    check_run(folder)
    assert str(selftest_run) not in (folder / "output.xml").read_text()

    again = subprocess.run(["bash", str(EXPORT), str(selftest_run),
                            str(results)], capture_output=True, text=True,
                           cwd=ROOT)
    assert again.returncode == 1
    assert "append-only" in again.stderr


def test_checked_in_results_are_clean():
    for output in sorted((ROOT / "results").rglob("output.xml")):
        check_run(output.parent)
