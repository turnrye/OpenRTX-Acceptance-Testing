# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Published logs link to their suite on GitHub, at the run's commit."""

from pathlib import Path

import pytest
from robot.api import ExecutionResult

from oat import sanitize
from oat.sanitize import SourceLink, known_commit

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/example/acceptance"
RUN = next((ROOT / "results").rglob("output.xml"), None)


@pytest.fixture(autouse=True)
def github(monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/acceptance")


def linked(commit, monkeypatch, on_github):
    monkeypatch.setattr(sanitize, "known_commit",
                        lambda d: "abc1234" if on_github else None)
    result = ExecutionResult(str(RUN))
    result.suite.metadata["Suite commit"] = commit
    result.visit(SourceLink())
    return result.suite


@pytest.mark.skipif(RUN is None, reason="no published run to try")
def test_known_commit_links_source_and_commit(monkeypatch):
    suite = linked("abc1234-dirty", monkeypatch, on_github=True)
    assert suite.source is None
    path = "acceptance/fm/bandwidth.robot"
    assert suite.metadata["Source"] == f"[{REPO}/blob/abc1234/{path}|{path}]"
    assert suite.metadata["Suite commit"] == (
        f"[{REPO}/commit/abc1234|abc1234] (with uncommitted changes)")


@pytest.mark.skipif(RUN is None, reason="no published run to try")
def test_unknown_commit_falls_back_to_main(monkeypatch):
    suite = linked("2005df1680", monkeypatch, on_github=False)
    assert "/blob/main/" in suite.metadata["Source"]
    assert suite.metadata["Suite commit"] == "2005df1680 (not on GitHub)"


@pytest.mark.parametrize("described", ["unknown", "0000000", "v1-3-g0000000"])
def test_commits_not_on_github_are_none(described):
    assert known_commit(described) is None
