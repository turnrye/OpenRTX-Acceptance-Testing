# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Robot Framework pre-run modifier that finds the bench's devices before
Robot opens its output files. A PTT port the bench file leaves out, or that
has moved, is chosen by the operator from the ports present; a device that
can't be found stops the run there, before any test, with the output
directory as it was, so an earlier run's results in it are kept. Use it as

    robot --prerunmodifier oat.preflight.Preflight:bench/<name>.local.yaml ...

which a bench's argument file (bench/example.args) carries. Open Bench
uses the port chosen here.
"""

import sys
from pathlib import Path

from robot.api import SuiteVisitor

from oat.audio import choose_ptt_port
from oat.config import load_yaml
from oat.operator import make_operator


class Preflight(SuiteVisitor):
    """Choose the bench's PTT port, or stop the run before it starts."""

    def __init__(self, bench: str):
        self.bench = load_yaml(Path(bench))
        self.done = False

    def start_suite(self, suite):
        if self.done:
            return
        self.done = True
        ptt = (self.bench.get("audio") or {}).get("ptt")
        if not ptt:
            return
        try:
            choose_ptt_port(ptt.get("device"),
                            make_operator(self.bench.get("operator", {})))
        except ValueError as e:
            # A suite left with no tests makes Robot stop without writing
            # output.xml, log.html or report.html
            print(f"[ PREFLIGHT ] {e}. Stopping before the run: the output "
                  "directory is as it was.", file=sys.__stderr__)
            suite.tests.clear()
            suite.suites.clear()
