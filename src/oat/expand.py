# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Robot Framework pre-run modifier that fits a suite to the radio under test.

Tests declare what they need with tags: `mode:<mode>` for an operating mode,
and `per-band` to run once on every band. The radio file declares what the
radio has (`bands`, `modes`). Before the run, tests needing a mode the radio
lacks are dropped, and each per-band test is copied once per band, grouped by
band so the operator retunes once. Use it as

    robot --prerunmodifier oat.expand.Expand:radios/c62.yaml ...

which a bench's argument file (bench/example.args) carries.
"""

from pathlib import Path

from robot.api import SuiteVisitor

from oat.config import load_yaml

PER_BAND = "per-band"
MODE_PREFIX = "mode:"


def required_modes(tags) -> set[str]:
    """Modes a test needs, from its mode:<mode> tags."""
    return {t[len(MODE_PREFIX):] for t in tags if t.startswith(MODE_PREFIX)}


class Expand(SuiteVisitor):
    """Drop tests the radio can't run; copy per-band tests for each band."""

    def __init__(self, radio: str):
        self.radio = load_yaml(Path(radio))
        self.bands = list(self.radio.get("bands", {}))
        self.modes = set(self.radio.get("modes", []))
        if not self.bands:
            raise ValueError(f"{radio}: no bands declared")

    def start_suite(self, suite):
        runnable = [t for t in suite.tests
                    if required_modes(t.tags) <= self.modes]
        per_band = [t for t in runnable if PER_BAND in t.tags]
        once = [t for t in runnable if PER_BAND not in t.tags]
        expanded = []
        for band in self.bands:
            for test in per_band:
                if test.setup:
                    raise ValueError(f"{test.name}: per-band tests can't "
                                     f"have their own [Setup]")
                copy = test.deepcopy(name=f"{band}: {test.name}")
                copy.tags.add(f"band:{band}")
                copy.setup.config(name="Use Band", args=(band,))
                expanded.append(copy)
        suite.tests = expanded + once

    def visit_test(self, test):
        """Tests are handled per suite."""
