# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The attenuator chain moves only when the tests change direction."""

import json
from pathlib import Path
from types import SimpleNamespace

import robot
import yaml

from oat.config import load_yaml
from oat.operator import ScriptedOperator
from oat.robot_library import OpenRTXAcceptance

ROOT = Path(__file__).resolve().parents[1]

# A suite of one test, on the bench in {bench}
SUITE = """*** Settings ***
Library           oat.robot_library.OpenRTXAcceptance
Suite Setup       Open Bench    {bench}    {root}/calibration/sim.yaml    {root}/radios/c62.yaml    sim    simulated radio    dsp=sim
Suite Teardown    Close Bench

*** Test Cases ***
{name}
    [Tags]    {tag}
    No Operation
"""


def library(receiver: dict, transmitter: dict) -> OpenRTXAcceptance:
    """The library on a bench of the given instruments, operator scripted."""
    lib = OpenRTXAcceptance()
    lib.bench = {"receiver": receiver, "transmitter": transmitter}
    lib.operator = ScriptedOperator()
    return lib


def prompts(lib: OpenRTXAcceptance, *tests: set) -> list[str]:
    """Start tests with the given tags; return what the operator was told."""
    for tags in tests:
        lib.start_test(SimpleNamespace(tags=tags), None)
    return [e.text for e in lib.operator.log]


def test_separate_instruments_move_the_chain_once():
    lib = library({"type": "rtlsdr"}, {"type": "hackrf"})
    assert prompts(lib, {"tx"}, {"tx"}, {"rx"}, set(), {"rx"}) == [
        "Move the attenuator chain: from the hackrf to the radio"]


def test_one_hackrf_keeps_the_chain_in_place():
    lib = library({"type": "hackrf"}, {"type": "hackrf"})
    assert prompts(lib, {"tx"}, {"rx"}, {"tx"}) == []


def test_two_hackrfs_are_separate_instruments():
    lib = library({"type": "hackrf", "serial": "a"},
                  {"type": "hackrf", "serial": "b"})
    assert len(prompts(lib, {"rx"}, {"tx"})) == 1


def test_the_chain_moves_between_the_suites_of_one_run(tmp_path):
    # Simulated instruments with their own serials count as two, so the
    # chain moves; the second suite starts where the first one left it
    bench = load_yaml(ROOT / "bench" / "sim.yaml")
    bench["receiver"]["serial"] = "rx"
    bench["transmitter"]["serial"] = "tx"
    bench_file = tmp_path / "separate.yaml"
    bench_file.write_text(yaml.safe_dump(bench))
    suites = tmp_path / "Run"
    suites.mkdir()
    for order, (name, tag) in enumerate((("First", "rx"), ("Second", "tx"))):
        (suites / f"{order}__{name.lower()}.robot").write_text(SUITE.format(
            bench=bench_file, root=ROOT, name=f"{name} Test", tag=tag))
    out = tmp_path / "out"
    assert robot.run(str(suites), outputdir=str(out), console="none",
                     report="NONE", log="NONE") == 0

    def moves(suite: str) -> list[str]:
        """The chain moves the operator was asked for in a suite."""
        report = json.loads(
            (out / f"bench-Run.{suite}" / "measurements.json").read_text())
        return [e["text"] for e in report["operator"]
                if e["text"].startswith("Move the attenuator chain")]

    assert moves("First") == []
    assert moves("Second") == [
        "Move the attenuator chain: from the radio to the sim"]
