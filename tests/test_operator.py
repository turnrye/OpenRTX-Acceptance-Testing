# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Operator prompts through files, as relayed runs use them."""

import json
import threading
import time

from oat.operator import FileOperator, ScriptedOperator


def answer_when_asked(directory, answers):
    """Answer each prompt that appears, as a person relaying them would."""
    def run():
        prompt = directory / "prompt.json"
        pending = list(answers)
        answered = set()
        while pending:
            try:
                data = json.loads(prompt.read_text())
            except FileNotFoundError:  # between prompts
                time.sleep(0.01)
                continue
            if data["id"] in answered:
                time.sleep(0.01)
                continue
            (directory / f"answer-{data['id']}.txt").write_text(
                pending.pop(0) + "\n")
            answered.add(data["id"])
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_file_operator_round_trip(tmp_path):
    op = FileOperator(tmp_path, poll_s=0.01, timeout_s=5)
    t = answer_when_asked(tmp_path, ["done", "no: too quiet", "louder"])
    assert op.instruct("Key up") == "done"
    assert op.confirm("Clean?") == (False, "too quiet")
    assert op.ask("Louder or quieter?") == "louder"
    t.join(timeout=5)
    assert [e["kind"] for e in op.exchanges()] == ["instruct", "confirm",
                                                    "ask"]


def test_confirm_asks_again_until_yes_or_no():
    op = ScriptedOperator(["maybe", "yes: fine"])
    assert op.confirm("Clean?") == (True, "fine")
