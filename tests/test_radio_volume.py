# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Set Radio Volume Once: the volume is set by level, once, and recorded."""

import numpy as np
import pytest

from oat import audio as audio_mod
from oat import robot_library
from oat.operator import ScriptedOperator
from oat.robot_library import OpenRTXAcceptance


class FakeBuiltIn:
    metadata = {}

    def get_variable_value(self, name, default=None):
        return default

    def set_suite_metadata(self, name, value, top=False):
        FakeBuiltIn.metadata[name] = value


class KnobAudio(audio_mod.AudioInterface):
    """A speaker whose level follows the operator turning the knob."""

    def __init__(self, levels_dbfs, clip_at=None):
        self.levels = list(levels_dbfs)
        self.clip_at = clip_at
        self.recorded = 0

    def play(self, path, repeat=False):
        pass

    def stop_playing(self):
        pass

    def record(self, path, seconds):
        level = self.levels[min(self.recorded, len(self.levels) - 1)]
        x = audio_mod.tone(1000, seconds, level_dbfs=level)
        if self.recorded == self.clip_at:
            x = np.clip(x * 4, -1, 1)
        self.recorded += 1
        return audio_mod.write_wav(path, x)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(robot_library, "BuiltIn", FakeBuiltIn)
    FakeBuiltIn.metadata = {}
    lib = OpenRTXAcceptance()
    lib.run_dir = tmp_path
    lib._frequency = lambda: 445975000.0
    lib.sent = []
    lib.start_transmitting_fm_tone = lambda *a, **k: lib.sent.append(a)
    lib.stop_transmitting = lambda: lib.sent.append("stop")
    return lib


def test_volume_is_turned_to_the_target_and_recorded(lib):
    lib.bench = {"audio": {"radio_volume": {"target_dbfs": -12}}}
    lib.audio = KnobAudio([-20, -6, -12.4], clip_at=1)
    lib.operator = ScriptedOperator(["", "", "", "1 o'clock"])
    v = lib.set_radio_volume_once()
    asked = [e.text for e in lib.operator.log]
    assert "Turn the volume up" in asked[1]
    assert "clipping" in asked[2] and "Turn the volume down" in asked[2]
    assert v["knob"] == "1 o'clock" and v["on_target"] and v["tries"] == 3
    assert abs(v["level_dbfs"] + 12.4) < 0.2
    assert lib.sent == [(3000.0, 1000.0), "stop"]
    assert "1 o'clock" in FakeBuiltIn.metadata["Radio volume"]
    assert lib.records[-1]["kind"] == "radio_volume"
    # Later tests keep it, without asking again
    assert lib.set_radio_volume_once() is v
    assert len(lib.operator.log) == len(asked)


def test_operator_can_accept_an_off_target_volume(lib):
    lib.bench = {"audio": {"radio_volume": {"target_dbfs": -12}}}
    lib.audio = KnobAudio([-30])
    lib.operator = ScriptedOperator(["", "accept", "full"])
    v = lib.set_radio_volume_once()
    assert not v["on_target"] and v["knob"] == "full"
    assert lib.sent[-1] == "stop"


def test_volume_never_reached_fails_and_stops_the_tone(lib):
    lib.bench = {"audio": {"radio_volume": {"target_dbfs": -12,
                                            "max_rounds": 3}}}
    lib.audio = KnobAudio([-30])
    lib.operator = ScriptedOperator(default="")
    with pytest.raises(AssertionError, match="didn't reach"):
        lib.set_radio_volume_once()
    assert lib.sent[-1] == "stop"


def test_without_a_target_the_operator_says_where_the_knob_is(lib):
    lib.bench = {"audio": {"type": "sim"}}
    lib.audio = None
    lib.operator = ScriptedOperator(["noon"])
    assert lib.set_radio_volume_once() == {"knob": "noon"}
    assert lib.sent == []
