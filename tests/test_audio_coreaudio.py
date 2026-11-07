# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A macOS audio interface plays and records through sox's coreaudio driver."""

import os
import stat

import pytest

from oat.audio import CoreAudio, make_audio

DEVICE = "USB Audio Device"


@pytest.fixture
def fake_sox(tmp_path, monkeypatch):
    """A sox that logs its arguments, one per line, and exits."""
    log = tmp_path / "calls"
    tool = tmp_path / "sox"
    tool.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" "--" >> {log}\n')
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    return log


def calls(log):
    """The fake sox's calls, each a list of its arguments."""
    return [c.strip("\n").split("\n")
            for c in log.read_text().split("--\n") if c]


def test_records_mono_at_the_rate_for_the_duration(fake_sox, tmp_path):
    CoreAudio(DEVICE, DEVICE).record(tmp_path / "r.wav", 1.5)
    assert calls(fake_sox) == [["-q", "-t", "coreaudio", DEVICE, "-c", "1",
                                "-r", "48000", "-b", "16",
                                str(tmp_path / "r.wav"), "trim", "0", "1.5"]]


@pytest.mark.parametrize("repeat, tail", [(False, []),
                                          (True, ["repeat", "-"])])
def test_plays_to_the_device_once_or_looping(fake_sox, tmp_path, repeat,
                                             tail):
    interface = CoreAudio(DEVICE, DEVICE)
    interface.play(tmp_path / "t.wav", repeat)
    interface.wait_playing()
    interface.stop_playing()
    assert calls(fake_sox) == [["-q", str(tmp_path / "t.wav"), "-t",
                                "coreaudio", DEVICE] + tail]


def test_config_names_the_devices():
    interface = make_audio({"type": "coreaudio", "playback_device": DEVICE,
                            "capture_device": DEVICE})
    assert isinstance(interface, CoreAudio)
    assert interface.playback_device == interface.capture_device == DEVICE


def test_a_mixer_section_is_refused_not_ignored():
    with pytest.raises(ValueError, match="Audio MIDI Setup"):
        make_audio({"type": "coreaudio", "mixer": {"PCM": 102}})
