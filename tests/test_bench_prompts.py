# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Setup prompts that follow the bench, and measured listening tests."""

import pytest

from oat import audio
from oat import robot_library
from oat.robot_library import OpenRTXAcceptance


class FakeBuiltIn:
    def get_variable_value(self, name, default=None):
        return default


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(robot_library, "BuiltIn", FakeBuiltIn)
    lib = OpenRTXAcceptance()
    lib.run_dir = tmp_path
    return lib


def test_cabling_without_an_audio_interface():
    lib = OpenRTXAcceptance()
    lib.bench = {}
    assert lib.bench_cabling() == "programming cable out"


def test_cabling_names_the_audio_interface():
    lib = OpenRTXAcceptance()
    lib.bench = {"audio": {"type": "sim"}}
    lib.audio = audio.SimAudio()
    assert lib.bench_cabling() == ("audio interface on the accessory jack, "
                                   "programming cable out")
    lib.bench["audio"]["name"] = "Digirig Mobile"
    assert lib.bench_cabling() == ("Digirig Mobile on the accessory jack, "
                                   "programming cable out")


def test_listening_volume_is_the_operators_without_audio():
    lib = OpenRTXAcceptance()
    lib.set_radio_volume_once = lambda: pytest.fail("volume set by level")
    assert lib.set_listening_volume() == "volume comfortable"
    assert lib.set_listening_volume("volume unchanged") == "volume unchanged"


def test_listening_volume_is_set_once_by_level_with_audio():
    lib = OpenRTXAcceptance()
    lib.audio = audio.SimAudio()
    calls = []

    def set_once():
        # As Set Radio Volume Once: the first call sets, later ones return it
        calls.append(1)
        lib.radio_volume = lib.radio_volume or {"knob": "12 o'clock"}
        return lib.radio_volume

    lib.set_radio_volume_once = set_once
    assert lib.set_listening_volume() == "volume unchanged"
    assert lib.set_listening_volume("volume unchanged") == "volume unchanged"
    assert len(calls) == 2 and lib.radio_volume == {"knob": "12 o'clock"}


def test_an_interferers_tone_is_measured_under_the_wanted_one(lib, tmp_path):
    x = audio.tone(1000, 3.0, -12) + audio.tone(400, 3.0, -32)
    path = audio.write_wav(tmp_path / "speaker.wav", x)
    m = {"file": "speaker.wav", "tone_hz": 1000.0,
         "tone_amplitude": audio.tone_amplitude(x, audio.RATE, 1000)}
    assert lib.audio_component_db(m, 400) == pytest.approx(-20, abs=0.2)
    assert lib.records[-1]["kind"] == "component"
    assert lib.records[-1]["file"] == path.name


def test_a_value_is_recorded_without_a_limit(lib):
    lib.record_value("SINAD drop", "1.25", "dB")
    assert lib.records[-1] == {"test": "", "band": None, "kind": "value",
                               "field": "SINAD drop", "value": 1.25,
                               "unit": "dB"}
