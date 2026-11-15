# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for the emulator microphone bench helpers (no emulator needed)."""

import numpy as np
import pytest

from oat import emu


def _speech_like(seconds=3.0, rate=8000, seed=1):
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * rate)) / rate
    env = (np.sin(2 * np.pi * 3 * t) > 0).astype(float)
    return env * (np.sin(2 * np.pi * 220 * t) + 0.1 * rng.normal(size=len(t)))


def test_adc_shape_bias_rate_and_level():
    p = emu.PROFILES["cs7000p"]
    x = emu.adc_shape(_speech_like(), 8000, p, -20.0)
    assert len(x) == pytest.approx(3 * 8000 * p.oversample, abs=p.oversample)
    assert x.min() >= 0 and x.max() <= 4095
    assert np.median(x) == pytest.approx(2048, abs=2)
    peak = np.max(np.abs(x.astype(float) - 2048)) / 2048
    assert 20 * np.log10(peak) == pytest.approx(-20.0, abs=0.5)


def test_chain_clip_fraction_follows_the_gain():
    p = emu.PROFILES["cs7000p"]          # gain 32: clips above -6 dBFS of ADC
    quiet = emu.adc_shape(_speech_like(), 8000, p, -12.0)
    loud = emu.adc_shape(_speech_like(), 8000, p, -1.0)
    assert emu.chain_clip_fraction(quiet, p) == 0.0
    assert emu.chain_clip_fraction(loud, p) > 0.01


def test_levels_active_level_and_onset():
    x = 0.1 * _speech_like()
    x[:400] = 0.9                         # a 50 ms key-up click
    lv = emu.levels(x, skip_s=0.1)
    assert lv["onset_peak_dbfs"] == pytest.approx(20 * np.log10(0.9), abs=0.1)
    assert lv["peak_dbfs"] < -15
    assert -25 < lv["active_dbfs"] < -15
    assert lv["clip_fraction"] == 0.0


def test_align_recovers_a_delay():
    ref = _speech_like()
    delayed = np.concatenate([np.zeros(800), ref])[: len(ref)]
    a, b = emu.align(ref, delayed)
    assert len(a) == len(b)
    assert np.corrcoef(a, b)[0, 1] > 0.95
