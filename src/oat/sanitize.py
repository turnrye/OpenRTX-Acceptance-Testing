# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Robot Framework pre-rebot modifier that removes what belongs to the tester's
machine from a run's output before it is published: absolute paths, which
carry user names and the bench layout. Paths inside this repository become
relative to it; others shrink to their file name.

    rebot --prerebotmodifier oat.sanitize.Sanitize output.xml

scripts/export-run.sh applies it.
"""

import re
from pathlib import Path

from robot.api import ResultVisitor

REPO = Path(__file__).resolve().parents[2]
# Absolute paths: POSIX ones of two or more components, home-relative ones
# and Windows drive paths
ABSOLUTE_PATH = re.compile(r"(?<![\w.])(?:~/|[A-Za-z]:\\|/(?=[\w.-]+/))[^\s\"'<>]*")


def relative(path: str) -> str:
    """A path relative to the repository, or just its file name."""
    try:
        return str(Path(path).resolve().relative_to(REPO))
    except (ValueError, OSError):
        return Path(path).name


def scrub(text: str) -> str:
    """Text with its absolute paths made relative."""
    return ABSOLUTE_PATH.sub(lambda m: relative(m.group(0)), text)


class Sanitize(ResultVisitor):
    """Make every path in a result relative or bare."""

    def start_suite(self, suite):
        if suite.source:
            suite.source = relative(str(suite.source))
        suite.doc = scrub(suite.doc)
        for name in list(suite.metadata):
            suite.metadata[name] = scrub(suite.metadata[name])

    def start_test(self, test):
        test.message = scrub(test.message)

    def start_keyword(self, keyword):
        keyword.args = tuple(scrub(str(a)) for a in keyword.args)
        keyword.message = scrub(keyword.message)

    def visit_message(self, msg):
        msg.message = scrub(msg.message)


def private_paths(text: str) -> list[str]:
    """Absolute paths left in a text, such as a sanitized output.xml."""
    return ABSOLUTE_PATH.findall(text)
