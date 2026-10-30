# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Robot Framework pre-rebot modifier that removes what belongs to the tester's
machine from a run's output before it is published: absolute paths, which
carry user names and the bench layout. Paths inside this repository become
relative to it; others shrink to their file name.

    rebot --prerebotmodifier oat.sanitize.Sanitize output.xml

scripts/export-run.sh applies it. SourceLink, below, is the results website's
counterpart: it links each published log to its suite on GitHub.
"""

import os
import re
import subprocess
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


def repo_url() -> str:
    """
    This repository's web address: GITHUB_SERVER_URL/GITHUB_REPOSITORY in
    GitHub Actions, otherwise the origin remote, made https.
    """
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if server and repo:
        return f"{server}/{repo}"
    origin = subprocess.run(["git", "-C", str(REPO), "remote", "get-url",
                             "origin"], capture_output=True, text=True,
                            check=True).stdout.strip()
    match = re.match(r"(?:git@([^:]+):|https?://([^/]+)/)(.+?)(?:\.git)?$",
                     origin)
    if match is None:
        raise ValueError(f"can't make a web address of {origin}")
    return f"https://{match.group(1) or match.group(2)}/{match.group(3)}"


def known_commit(describe: str) -> str | None:
    """
    The sha in a `git describe --always --dirty` string, if the commit is on
    GitHub, that is reachable from a remote-tracking ref (fetch refs/pull/*
    too, for commits of merged pull requests); None otherwise, as for a
    commit squashed away or never pushed.
    """
    match = re.search(r"(?:^|-g)([0-9a-f]{7,40})(?:-dirty)?$", describe)
    if match is None:
        return None
    sha = match.group(1)
    found = subprocess.run(["git", "-C", str(REPO), "for-each-ref",
                            "--count=1", "--contains", sha, "refs/remotes"],
                           capture_output=True, text=True)
    return sha if found.returncode == 0 and found.stdout.strip() else None


class SourceLink(ResultVisitor):
    """
    Pre-rebot modifier for the results website. Robot's log links a suite's
    source relative to the log, which is a dead link once published. This
    replaces it with a "Source" metadata link to the file on GitHub at the
    run's suite commit, and makes "Suite commit" a link to that commit. When
    the commit isn't in the repository (squashed away, or never pushed), the
    source links to main and the commit stays text. Needs a clone with full
    history, so the commit check is meaningful.

        rebot --prerebotmodifier oat.sanitize.SourceLink output.xml
    """

    def __init__(self):
        self.url = repo_url()
        self.ref = None

    def start_suite(self, suite):
        if self.ref is None:
            described = suite.metadata.get("Suite commit", "")
            sha = known_commit(described)
            self.ref = sha or "main"
            if described:
                dirty = (" (with uncommitted changes)"
                         if described.endswith("-dirty") else "")
                suite.metadata["Suite commit"] = (
                    f"[{self.url}/commit/{sha}|{sha}]{dirty}" if sha
                    else f"{described} (not on GitHub)")
        if suite.source:
            path = relative(str(suite.source))
            suite.metadata["Source"] = (f"[{self.url}/blob/{self.ref}/{path}"
                                        f"|{path}]")
            suite.source = None
