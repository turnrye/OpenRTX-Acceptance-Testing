# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The attenuator chain moves only when the tests change direction."""

from types import SimpleNamespace

from oat.operator import ScriptedOperator
from oat.robot_library import OpenRTXAcceptance


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
