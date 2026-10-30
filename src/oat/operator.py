# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
The person at the bench. Until radios can be driven remotely, an operator
sets the radio up, keys it, and makes the assertions that need ears or eyes.
"""

import json
import os
import sys
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

YES = {"y", "yes", "pass", "ok", "true"}
NO = {"n", "no", "fail", "false"}


@dataclass
class Exchange:
    """One prompt and the operator's answer, kept for the report."""

    kind: str  # instruct, confirm or ask
    text: str
    answer: str


class Operator(ABC):
    """Asks the person at the bench."""

    def __init__(self):
        self.log: list[Exchange] = []

    @abstractmethod
    def _prompt(self, kind: str, text: str) -> str:
        """Show a prompt and return the raw answer."""

    def instruct(self, text: str) -> str:
        """Ask for an action and wait until it's done; returns any note."""
        answer = self._prompt("instruct", text)
        self.log.append(Exchange("instruct", text, answer))
        return answer

    def confirm(self, text: str) -> tuple[bool, str]:
        """Ask a yes/no question; returns the verdict and any note."""
        while True:
            answer = self._prompt("confirm", text).strip()
            verdict, _, note = answer.partition(":")
            if verdict.strip().lower() in YES | NO:
                self.log.append(Exchange("confirm", text, answer))
                return verdict.strip().lower() in YES, note.strip()

    def ask(self, text: str) -> str:
        """Ask for a value or an observation."""
        answer = self._prompt("ask", text).strip()
        self.log.append(Exchange("ask", text, answer))
        return answer

    def exchanges(self) -> list[dict]:
        """The log as plain dictionaries."""
        return [asdict(e) for e in self.log]


def write_atomic(path: Path, text: str) -> None:
    """Write a file so readers see the old content or the new, never half."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class ConsoleOperator(Operator):
    """
    Prompts on the terminal running the tests. Robot Framework captures what
    keywords print into its log, so prompts go to the real stdout instead.
    """

    HINTS = {"instruct": "press Enter when done",
             "confirm": "yes/no, optionally ': note'",
             "ask": "type the answer"}

    def _prompt(self, kind: str, text: str) -> str:
        out = sys.__stdout__
        out.write(f"\n\n>>> OPERATOR: {text}\n    ({self.HINTS[kind]})\n    > ")
        out.flush()
        return sys.stdin.readline().rstrip("\n")


class FileOperator(Operator):
    """
    Prompts through files, for a test run with no terminal: each prompt is
    written to <dir>/prompt.json, and the run waits for <dir>/answer-<id>.txt.
    Whoever relays the prompt writes the answer file.
    """

    def __init__(self, directory: Path, poll_s: float = 0.5,
                 timeout_s: float | None = None):
        super().__init__()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.poll_s = poll_s
        self.timeout_s = timeout_s
        self._next_id = 1

    def _prompt(self, kind: str, text: str) -> str:
        prompt_id = self._next_id
        self._next_id += 1
        answer = self.directory / f"answer-{prompt_id}.txt"
        answer.unlink(missing_ok=True)
        write_atomic(self.directory / "prompt.json", json.dumps(
            {"id": prompt_id, "kind": kind, "text": text,
             "answer_file": str(answer)}, indent=2) + "\n")
        start = time.monotonic()
        while not answer.exists():
            if (self.timeout_s is not None
                    and time.monotonic() - start > self.timeout_s):
                raise TimeoutError(f"no answer to prompt {prompt_id}: {text}")
            time.sleep(self.poll_s)
        (self.directory / "prompt.json").unlink(missing_ok=True)
        return answer.read_text().strip()


class ScriptedOperator(Operator):
    """Answers from a list, for self-tests."""

    def __init__(self, answers: list[str] | None = None,
                 default: str = "yes"):
        super().__init__()
        self.answers = deque(answers or [])
        self.default = default

    def _prompt(self, kind: str, text: str) -> str:
        return self.answers.popleft() if self.answers else self.default


def make_operator(cfg: dict) -> Operator:
    """Operator from a bench 'operator' section."""
    kind = cfg.get("type", "console")
    if kind == "console":
        return ConsoleOperator()
    if kind == "file":
        return FileOperator(Path(cfg.get("directory", "runs/operator")),
                            timeout_s=cfg.get("timeout_s"))
    if kind == "scripted":
        return ScriptedOperator(cfg.get("answers"), cfg.get("default", "yes"))
    raise ValueError(f"unknown operator type {kind!r}")
