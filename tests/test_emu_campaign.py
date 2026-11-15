# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for the emulator microphone campaign (no emulator needed)."""

import csv
import json

import numpy as np
import pytest
from scipy.io import wavfile

from oat import emu
from oat import emu_campaign as camp
from oat import emu_report

P = emu.PROFILES["cs7000p"]


@pytest.fixture
def speech_dir(tmp_path):
    rate = 8000
    t = np.arange(10 * rate) / rate
    x = (np.sin(2 * np.pi * 2 * t) > 0) * np.sin(2 * np.pi * 300 * t)
    wavfile.write(tmp_path / "s.wav", rate, (x * 20000).astype(np.int16))
    return tmp_path


def test_variants_full_is_every_single_change():
    singles = {d for v, ds in camp.VARIANTS.items() if v in ("prime_dc", "keep_sum", "hpf", "agc")
               for d in ds}
    assert set(camp.VARIANTS["full"]) == singles
    assert camp.VARIANTS["baseline"] == ()


def test_float_chain():
    assert camp.float_chain(camp.VARIANTS["agc"])
    assert camp.float_chain(camp.VARIANTS["full"])
    assert camp.float_chain(camp.VARIANTS["agc_mid"])
    assert not camp.float_chain(camp.VARIANTS["keep_sum"])


def test_variant_defines_keep_their_float_suffix():
    p = emu.PROFILES["cs7000p"]
    args = " ".join([p.c_args(), *camp.VARIANTS["agc_slow"]]).split()
    assert "-DMIC_EXP_AGC_ATTACK=0.1f" in args


def test_tone_level_rate_and_tail():
    r = camp.tone_recipe(1000, -20, 1.0)
    x = camp.synth(r, P)
    rate = 8000 * P.oversample
    assert len(x) == int((1.0 + camp.TAIL_S) * rate)
    assert 20 * np.log10(np.max(np.abs(x))) == pytest.approx(-20, abs=0.01)
    assert np.all(x[rate:] == 0)
    assert camp.take_id(r) == "tone_1000Hz_-20dBFS"


def test_mic_input_is_bit_exact(speech_dir):
    r = camp.speech_recipe(speech_dir / "s.wav", -12, 4.0)
    a = camp.mic_input(r, P, speech_dir)
    b = camp.mic_input(r, P, speech_dir)
    assert camp.mic_sha256(a) == camp.mic_sha256(b)
    assert np.median(a) == pytest.approx(2048, abs=2)


def test_speech_recipe_checks_the_file(speech_dir):
    r = camp.speech_recipe(speech_dir / "s.wav", -12, 4.0)
    r["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="checksum"):
        camp.synth(r, P, speech_dir)


def test_step_segments_set_the_level():
    r = camp.step_recipe([(1, -30), (1, -10), (2, -30)])
    x = camp.synth(r, P)
    rate = 8000 * P.oversample
    quiet = np.max(np.abs(x[: rate]))
    loud = np.max(np.abs(x[rate: 2 * rate]))
    assert 20 * np.log10(loud / quiet) == pytest.approx(20, abs=0.1)
    assert 20 * np.log10(loud) == pytest.approx(-10, abs=0.1)


def test_gate_noise_bed_and_segments(speech_dir):
    r = camp.gate_recipe(speech_dir / "s.wav", -40, speech_s=2, pause_s=3, bursts=2)
    sp, pa = camp.gate_segments(r)
    assert sp == [(0, 2), (5, 7)] and pa == [(2, 5), (7, 10)]
    x = camp.synth(r, P, speech_dir)
    rate = 8000 * P.oversample
    pause = x[int(2.2 * rate): int(4.8 * rate)]
    assert 20 * np.log10(np.sqrt(np.mean(pause ** 2))) == pytest.approx(-40, abs=1.5)


def test_pink_noise_falls_3_db_per_octave():
    x = camp.colored_noise(1 << 16, "pink", 1)
    p = np.abs(np.fft.rfft(x)) ** 2
    lo = p[1000:2000].mean()
    hi = p[2000:4000].mean()
    assert 10 * np.log10(lo / hi) == pytest.approx(3, abs=0.5)
    assert np.sqrt(np.mean(x ** 2)) == pytest.approx(1)


def test_ideal_chain_applies_the_gain():
    r = camp.tone_recipe(1000, -30, 1.0)
    mic = camp.mic_input(r, P)
    y = camp.ideal_chain(mic, P)
    lvl = 20 * np.log10(np.max(np.abs(y[800:7200])))
    # -30 dBFS of 2048 codes, times 32, against 32768: -30 + 6.02 dB
    assert lvl == pytest.approx(-30 + 20 * np.log10(2048 * 32 / 32768), abs=0.3)


def test_envelope_db():
    x = np.concatenate([np.full(400, 0.1), np.full(400, 0.01)])
    e = camp.envelope_db(x, 8000, 0.05)
    assert e == pytest.approx([-20, -40], abs=0.01)


def _env(levels, hop=0.05):
    return np.concatenate([np.full(int(round(s / hop)), lv) for s, lv in levels])


def test_step_response_linear_chain():
    env = _env([(3, -40), (3, -20), (6, -40)])
    m = camp.step_response(env, 0.05, 3, 6, 12)
    assert m["rise_db"] == pytest.approx(20)
    assert m["fall_db"] == pytest.approx(20)
    assert m["attack_s"] == 0.0 and m["release_s"] == 0.0
    assert np.isnan(m["release_rate_db_s"])


def test_step_response_agc_like():
    hop = 0.05
    t = np.arange(int(12 / hop)) * hop + hop / 2
    env = np.full(len(t), -21.0)
    up = (t >= 3) & (t < 6)
    env[up] = -21 + 14 * np.exp(-(t[up] - 3) / 0.1)       # overshoot, settles
    down = t >= 6
    env[down] = np.minimum(-21.0, -41 + 10 * (t[down] - 6))   # recovers 10 dB/s
    m = camp.step_response(env, hop, 3, 6, 12)
    assert m["overshoot_db"] == pytest.approx(14 * np.exp(-0.25), abs=0.2)
    assert 0.15 < m["attack_s"] < 0.35
    assert m["release_s"] == pytest.approx(1.8, abs=0.1)
    assert m["dip_db"] == pytest.approx(20, abs=0.6)
    assert m["release_rate_db_s"] == pytest.approx(10, rel=0.05)


def test_settle_time_never_settles():
    env = np.linspace(-40, -20, 100)
    assert camp.settle_time(env, 0.05, 0, 5, -10, 2) is None


def test_pause_stats_drift():
    rate = 8000
    rng = np.random.default_rng(0)
    x = np.zeros(10 * rate)
    # pause from 2 to 6 s whose noise rises 6 dB, steady pause 7 to 10 s
    seg = rng.normal(size=4 * rate) * np.linspace(0.01, 0.02, 4 * rate)
    x[2 * rate: 6 * rate] = seg
    x[7 * rate:] = 0.01 * rng.normal(size=3 * rate)
    s = camp.pause_stats(x, [(2, 6), (7, 10)], rate)
    assert s["pause_drift_max_db"] == pytest.approx(4.4, abs=1.0)
    assert -0.5 < min(s["pause_drift_db"], s["pause_drift_max_db"])
    assert s["pause_dbfs"] < -30


def test_headline_spread_and_deficit():
    rows = []
    for lv, act, st in ((-30, -40.0, 0.6), (-10, -20.0, 0.8)):
        rows.append({"variant": "baseline", "test": "speech", "signal": "a",
                     "level": lv, "active_dbfs": act, "stoi": st,
                     "onset_peak_dbfs": -8.0, "pre_clip_fraction": 0.02,
                     "pre_active_dbfs": act + 2, "pre_onset_peak_dbfs": 0.0})
        rows.append({"variant": "agc", "test": "speech", "signal": "a", "level": lv,
                     "active_dbfs": -21.0 + lv / 100, "stoi": st + 0.05,
                     "onset_peak_dbfs": -40.0, "pre_clip_fraction": 0.0,
                     "pre_active_dbfs": -21.0, "pre_onset_peak_dbfs": -60.0})
        rows.append({"variant": "codec-only (ideal)", "test": "speech", "signal": "a",
                     "level": lv, "stoi": 0.85})
    h = {x["variant"]: x for x in emu_report.headline(rows, ["baseline", "agc"])}
    assert h["baseline"]["speech_spread_db"] == pytest.approx(20)
    assert h["agc"]["speech_spread_db"] == pytest.approx(0.2)
    assert h["baseline"]["stoi_deficit"] == pytest.approx(0.15)
    assert h["agc"]["stoi_deficit"] == pytest.approx(0.10)
    assert h["agc"]["pre_clip_max"] == 0.0
    assert h["baseline"]["pre_clip_max"] == 0.02
    assert h["baseline"]["pre_spread_db"] == pytest.approx(20)
    assert h["agc"]["pre_spread_db"] == pytest.approx(0)
    assert h["agc"]["pre_onset_median_dbfs"] == -60.0


def test_write_csv_union_of_columns(tmp_path):
    emu_report.write_csv(tmp_path / "t.csv", [{"a": 1, "b": float("nan")},
                                              {"a": 2.5, "c": None, "d": "x"}])
    rows = list(csv.DictReader(open(tmp_path / "t.csv")))
    assert list(rows[0]) == ["a", "b", "c", "d"]
    assert rows[0]["b"] == "" and rows[1]["d"] == "x"


def test_colours_follow_the_variant_not_the_selection():
    assert emu_report.colour("baseline") == emu_report.GREY
    assert emu_report.colour("agc") != emu_report.colour("hpf")
    assert len({emu_report.colour(v) for v in camp.VARIANTS}) == len(camp.VARIANTS)


def test_configured_c_args(tmp_path):
    (tmp_path / "meson-info").mkdir()
    (tmp_path / "meson-info" / "intro-buildoptions.json").write_text(json.dumps(
        [{"name": "c_args", "value": ["-DCONFIG_MIC_GAIN=32", "-DMIC_EXP_AGC"]}]))
    assert emu.configured_c_args(tmp_path) == ["-DCONFIG_MIC_GAIN=32", "-DMIC_EXP_AGC"]


def test_split_overs_shares_the_extra_blocks():
    blocks = [np.full(160, i, dtype=np.int16) for i in range(642)]
    parts = emu.split_overs(np.concatenate(blocks), [160, 160])
    assert [len(x) // 160 for x in parts] == [321, 321]
    assert parts[1][0] == 321
    one = emu.split_overs(np.zeros(173 * 160, dtype=np.int16), [86])
    assert len(one[0]) == 173 * 160


def test_score_measures_the_tap():
    r = camp.tone_recipe(1000, -3, 2.0)
    mic = camp.mic_input(r, P)
    n = int(2.5 * 8000)
    t = np.arange(n) / 8000
    out = 0.1 * np.sin(2 * np.pi * 1000 * t)
    pre = np.clip(1.5 * np.sin(2 * np.pi * 1000 * t), -32768 / 32768, 32767 / 32768)
    m, _ = camp.score(r, out, pre, mic, P, None)
    assert m["rms_dbfs"] == pytest.approx(-23, abs=0.1)
    assert m["post_clip_fraction"] == 0.0
    assert m["pre_clip_fraction"] > 0.3
    assert m["pre_rms_dbfs"] > -3
    assert m["model_clip_fraction"] > 0
    m, _ = camp.score(r, out, None, mic, P, None)
    assert "pre_clip_fraction" not in m


def test_step_score_has_both_envelopes():
    r = camp.step_recipe([(1.5, -30), (1.5, -10), (3, -30)])
    mic = camp.mic_input(r, P)
    pre, out = camp.references(r, mic, P, None)["nominal"]
    m, envs = camp.score(r, out, pre, mic, P, None)
    assert set(envs) == {"step", "step_precodec"}
    assert m["pre_rise_db"] == pytest.approx(20, abs=0.5)
    assert m["pre_attack_s"] <= 0.1
