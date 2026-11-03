# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The audio interface's mixer is set from the bench file and read back."""

import os
import stat

import pytest

from oat.audio import AlsaAudio, alsa_card, make_audio, mixer_readback

# What amixer prints for an AB13X USB dongle (a BTECH APRS cable)
PCM = """Simple mixer control 'PCM',0
  Capabilities: pvolume pswitch pswitch-joined
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 255
  Mono:
  Front Left: Playback 102 [40%] [on]
  Front Right: Playback 102 [40%] [on]
"""


def test_card_from_device_names():
    assert alsa_card("plughw:CARD=Audio,DEV=0") == "Audio"
    assert alsa_card("sysdefault:CARD=Audio") == "Audio"
    assert alsa_card("hw:2,0") == "2"
    assert alsa_card("default") is None


def test_readback_lists_each_level_once():
    assert mixer_readback(PCM) == "Playback 102 [40%] [on]"
    assert mixer_readback("  Mono: Capture 128 [50%] [on]") \
        == "Capture 128 [50%] [on]"
    assert mixer_readback("  Front Left: Playback 255 [100%] [off]") \
        == "Playback 255 [100%] [off]"


@pytest.fixture
def fake_amixer(tmp_path, monkeypatch):
    """An amixer that logs its arguments and prints PCM's report."""
    log = tmp_path / "calls"
    tool = tmp_path / "amixer"
    tool.write_text(f'#!/bin/sh\necho "$@" >> {log}\ncat <<EOF\n{PCM}EOF\n')
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    return log


def test_mixer_is_set_on_the_device_card(fake_amixer):
    audio = make_audio({"type": "alsa",
                        "playback_device": "plughw:CARD=Audio,DEV=0",
                        "capture_device": "plughw:CARD=Audio,DEV=0",
                        "mixer": {"PCM": "102 unmute", "Mic": "50%"}})
    assert audio.apply_mixer() == {"PCM": "Playback 102 [40%] [on]",
                                   "Mic": "Playback 102 [40%] [on]"}
    assert fake_amixer.read_text().splitlines() == [
        "-c Audio sset PCM 102 unmute", "-c Audio sset Mic 50%"]


def test_no_mixer_section_touches_nothing(fake_amixer):
    assert AlsaAudio("plughw:CARD=Audio,DEV=0").apply_mixer() == {}
    assert not fake_amixer.exists()


def test_mixer_needs_a_card():
    with pytest.raises(ValueError, match="names its card|naming its card"):
        AlsaAudio("default", "default", mixer={"PCM": 1}).apply_mixer()
