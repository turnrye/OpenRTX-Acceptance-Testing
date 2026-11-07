# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The PTT port is chosen before the run, or the run stops untouched."""

import pytest
from robot.api import TestSuite as RobotSuite

from oat import audio
from oat.operator import ScriptedOperator
from oat.preflight import Preflight


@pytest.fixture
def ports(monkeypatch):
    """Serial ports present, all usable; no port chosen yet this run."""
    present = []
    monkeypatch.setattr(audio, "serial_ports", lambda: list(present))
    monkeypatch.setattr(audio, "_usable", lambda p: p in present)
    monkeypatch.setattr(audio, "_chosen_ports", {})
    return present


def test_a_present_device_is_used_without_asking(ports):
    ports.append("/dev/cu.usbserial-1310")
    operator = ScriptedOperator([])
    assert audio.choose_ptt_port("/dev/cu.usbserial-1310", operator) \
        == "/dev/cu.usbserial-1310"
    assert operator.log == []


def test_enter_takes_the_only_port_and_is_remembered(ports):
    ports.append("/dev/cu.usbserial-1310")
    operator = ScriptedOperator([""])
    assert audio.choose_ptt_port("/dev/cu.usbserial-11310", operator) \
        == "/dev/cu.usbserial-1310"
    assert "is missing" in operator.log[0].text
    assert audio.choose_ptt_port("/dev/cu.usbserial-11310", operator) \
        == "/dev/cu.usbserial-1310"
    assert len(operator.log) == 1


def test_the_operator_names_one_of_several(ports):
    ports += ["/dev/cu.usbserial-1310", "/dev/cu.PL2303G-USBtoUART1"]
    operator = ScriptedOperator(["", "/dev/cu.usbserial-1310"])
    assert audio.choose_ptt_port(None, operator) == "/dev/cu.usbserial-1310"
    assert "names no PTT port" in operator.log[0].text


def test_no_port_present_is_an_error(ports):
    with pytest.raises(ValueError, match="no serial ports are present"):
        audio.choose_ptt_port("/dev/cu.usbserial-1310", ScriptedOperator([]))


def bench_file(tmp_path, ptt="{line: rts}"):
    path = tmp_path / "bench.yaml"
    path.write_text("operator: {type: scripted, default: ''}\n"
                    f"audio: {{type: sim, ptt: {ptt}}}\n")
    return str(path)


def suite_with_a_test():
    suite = RobotSuite(name="S")
    suite.tests.create(name="T")
    return suite


def test_preflight_empties_the_run_when_no_port_is_found(ports, tmp_path):
    suite = suite_with_a_test()
    suite.visit(Preflight(bench_file(tmp_path)))
    assert not suite.has_tests


def test_preflight_keeps_the_run_and_its_choice(ports, tmp_path):
    ports.append("/dev/cu.usbserial-1310")
    suite = suite_with_a_test()
    suite.visit(Preflight(bench_file(tmp_path)))
    assert suite.has_tests
    # Open Bench gets the same port without asking again
    assert audio.choose_ptt_port(None, ScriptedOperator([])) \
        == "/dev/cu.usbserial-1310"
