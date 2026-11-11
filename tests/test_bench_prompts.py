# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Setup prompts that name the bench's cabling, and the listening monitor."""

import pytest

from oat import audio
from oat.robot_library import OpenRTXAcceptance


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


def test_monitor_refuses_the_interfaces_own_output():
    with pytest.raises(ValueError, match="feed its mic"):
        audio.check_monitor_device("USB Audio Device", "USB Audio Device")
    audio.check_monitor_device("USB Audio Device", "default")


def test_monitor_commands_play_the_capture_on_the_monitor_device(monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda name: f"/bin/{name}")
    core = audio.make_audio({"type": "coreaudio",
                             "playback_device": "USB Audio Device",
                             "capture_device": "USB Audio Device"})
    assert core._monitor_command() == [
        "/bin/sox", "-q", "-t", "coreaudio", "USB Audio Device",
        "-t", "coreaudio", "default"]
    alsa = audio.make_audio({"type": "alsa", "playback_device": "plughw:1",
                             "capture_device": "plughw:1",
                             "monitor_device": "plughw:0"})
    cmd = alsa._monitor_command()
    assert cmd[3:] == ["/bin/arecord", "plughw:1", str(audio.RATE),
                       "/bin/aplay", "plughw:0"]
    # Both on the default device would loop the speaker into the mic
    with pytest.raises(ValueError):
        audio.make_audio({"type": "alsa"})._monitor_command()


def test_monitor_starts_and_stops():
    sim = audio.SimAudio()
    sim.start_monitor()               # nothing to play it on
    assert sim._monitor is None
    sim._monitor_command = lambda: ["sleep", "30"]
    sim.start_monitor()
    proc = sim._monitor
    assert proc.poll() is None
    sim.stop_monitor()
    assert proc.poll() is not None and sim._monitor is None


def test_monitor_keywords_do_nothing_without_audio():
    lib = OpenRTXAcceptance()
    lib.start_monitoring_radio_audio()
    lib.stop_monitoring_radio_audio()


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
