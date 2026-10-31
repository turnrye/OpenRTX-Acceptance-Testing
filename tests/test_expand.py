# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Fitting a suite to the radio: tests per band, and only supported modes."""

import pytest
from robot.running import TestSuite as RobotSuite

from oat.expand import Expand

SUITE = """
*** Test Cases ***
Wide
    [Tags]    per-band    mode:fm    tx
    No Operation
Narrow
    [Tags]    per-band    mode:fm    tx
    No Operation
Listen
    [Tags]    per-band    mode:fm    rx
    No Operation
M17 Round Trip
    [Tags]    per-band    mode:fm    mode:m17
    No Operation
Once
    [Tags]    mode:fm
    No Operation
DMR
    [Tags]    per-band    mode:dmr
    No Operation
"""


def radio_file(tmp_path, modes):
    path = tmp_path / "radio.yaml"
    path.write_text("name: Test\nmax_tx_power_dbm: 37\nbands:\n"
                    "  2m: {frequency_hz: 146580000}\n"
                    "  70cm: {frequency_hz: 445975000}\n"
                    f"modes: [{', '.join(modes)}]\n")
    return path


def expanded(tmp_path, suite_text, modes):
    path = tmp_path / "suite.robot"
    path.write_text(suite_text)
    suite = RobotSuite.from_file_system(path)
    suite.visit(Expand(str(radio_file(tmp_path, modes))))
    return suite


def test_transmit_tests_on_every_band_come_before_receive(tmp_path):
    suite = expanded(tmp_path, SUITE, ["fm", "m17"])
    assert [t.name for t in suite.tests] == [
        "2m: Wide", "2m: Narrow", "70cm: Wide", "70cm: Narrow",
        "2m: Listen", "70cm: Listen",
        "2m: M17 Round Trip", "70cm: M17 Round Trip", "Once"]
    first = suite.tests[0]
    assert "band:2m" in first.tags and "tx" in first.tags
    assert first.setup.name == "Use Band" and first.setup.args == ("2m",)
    assert not suite.tests[-1].setup


def test_tests_needing_a_missing_mode_are_dropped(tmp_path):
    suite = expanded(tmp_path, SUITE, ["fm"])
    names = [t.name for t in suite.tests]
    assert "2m: M17 Round Trip" not in names
    assert not any("DMR" in n for n in names)
    assert len(names) == 7


def test_per_band_tests_cannot_have_their_own_setup(tmp_path):
    text = SUITE.replace("Wide\n    [Tags]", "Wide\n    [Setup]    No Operation\n    [Tags]")
    with pytest.raises(ValueError, match="own \\[Setup\\]"):
        expanded(tmp_path, text, ["fm"])
