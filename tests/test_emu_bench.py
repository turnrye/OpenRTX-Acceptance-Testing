# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The emulator bench: its files, firmware names and settings (no emulator needed)."""

from pathlib import Path

import numpy as np
import pytest
import yaml

from oat import emu, emu_bench
from oat import emu_campaign as camp
from oat.calibration import load_calibration
from oat.compare import rows
from oat.sanitize import private_paths

ROOT = Path(__file__).resolve().parents[1]
LIMITS = {"max_pre_onset_peak_dbfs", "max_pre_speech_spread_db", "max_pre_clip_percent",
          "max_stoi_deficit", "max_pre_step_rise_db", "max_pre_attack_s",
          "max_pre_overshoot_db", "max_pre_pause_drift_db", "max_listen_stoi_deficit"}


def radios_with_models():
    out = {}
    for f in sorted((ROOT / "radios").glob("*.yaml")):
        data = yaml.safe_load(f.read_text())
        if "mic_chain" in data:
            out[f.stem] = data
    return out


def test_radio_models_have_sources_and_limits():
    models = radios_with_models()
    assert {"cs7000p", "cs7000", "mduv3x0", "md3x0", "mod17", "c62",
            "emulator-bare"} <= set(models)
    for name, radio in models.items():
        mic = radio["mic_chain"]
        assert set(mic["sources"]) == {"oversample", "gain", "adc_bits", "bias",
                                       "noise_lsb"}, name
        assert set(mic["limits"]) == LIMITS, name
        emu.Profile.from_radio(name, mic)


def test_radio_models_match_the_firmware():
    models = radios_with_models()
    expect = {"cs7000p": (6, 32), "cs7000": (8, 32), "mduv3x0": (8, 32),
              "md3x0": (8, 32), "mod17": (1, 12), "c62": (1, 13),
              "emulator-bare": (1, 1)}
    for name, (oversample, gain) in expect.items():
        mic = models[name]["mic_chain"]
        assert (mic["oversample"], mic["gain"]) == (oversample, gain), name


def test_bare_model_is_clean_signed_input():
    p = emu.Profile.from_radio("emulator-bare",
                               radios_with_models()["emulator-bare"]["mic_chain"])
    assert (p.adc_bits, p.bias, p.noise_lsb) == (16, False, 0.0)
    x = emu.adc_quantize(np.array([0.0, 0.5, -0.5, 2.0]), p)
    assert list(x) == [0, 16384, -16384, 32767]


def test_unbiased_adc_is_signed_at_its_bits():
    p = emu.Profile("x", 1, 1, 12, 0.0, False)
    assert list(emu.adc_quantize(np.array([0.5, -2.0]), p)) == [1024, -2048]
    biased = emu.Profile("y", 1, 1, 12, 0.0, True)
    assert list(emu.adc_quantize(np.array([0.5]), biased)) == [3072]


def test_from_radio_refuses_an_unknown_bias():
    with pytest.raises(ValueError, match="bias"):
        emu.Profile.from_radio("x", {"oversample": 1, "gain": 1, "adc_bits": 12,
                                     "noise_lsb": 0, "bias": "half"})


def test_bench_and_calibration_files():
    bench = yaml.safe_load((ROOT / "bench" / "emulator.yaml").read_text())
    assert emu_bench.is_emulator(bench)
    assert bench["station"] == "emulator"
    assert not any(bench["emulator"].get(k) for k in emu_bench.LOCAL)
    cal = load_calibration(ROOT / "calibration" / "emulator.yaml")
    assert cal.summary()["method"] == "model"


def test_firmware_names_variant_and_commit():
    assert emu_bench.split_firmware("agc@7f9b6c2ad") == ("agc", "7f9b6c2ad")
    for bad in ("agc", "agc@xyz", "@7f9b6c2ad", "agc@7f9b6"):
        with pytest.raises(ValueError):
            emu_bench.split_firmware(bad)


def test_settings_overrides_and_paths(tmp_path):
    bench = {"emulator": {"jobs": 6, "openrtx": str(tmp_path), "takes": None}}
    s = emu_bench.settings(bench, {"jobs": "", "takes": str(tmp_path / "t"),
                                   "reuse_takes": f"{tmp_path}/a,{tmp_path}/b"})
    assert s["jobs"] == 6
    assert s["openrtx"] == tmp_path.resolve()
    assert s["takes"] == (tmp_path / "t").resolve()
    assert s["reuse_takes"] == [(tmp_path / "a").resolve(), (tmp_path / "b").resolve()]


def test_split_tap_finds_the_second_over():
    rng = np.random.default_rng(0)
    ref = np.concatenate([0.01 * rng.normal(size=8000), 0.3 * rng.normal(size=8000),
                          0.01 * rng.normal(size=16000)])
    first = ref[: len(ref) - 1600]                 # the encoder stopped 0.2 s early
    second = ref[: len(ref) - 800]
    tap = np.round(np.concatenate([first, second]) * 32767).astype(np.int16)
    a, b = camp.split_tap(tap, ref, 2)
    assert len(a) == len(first) // 160 * 160
    assert abs(len(b) - len(second)) <= 160


def test_urls_are_not_private_paths():
    assert private_paths('"http://www.w3.org/1999/xlink" https://a.org/b/c') == []
    assert private_paths("at /home/someone/x") == ["/home/someone/x"]
    assert private_paths("file:///tmp/run/x.wav") == ["/tmp/run/x.wav"]


def test_svg_charts_have_no_doctype(tmp_path):
    from oat import emu_report
    plt = emu_report._plt()
    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1])
    emu_report._save(fig, tmp_path, "x")
    text = (tmp_path / "x.svg").read_text()
    assert "DOCTYPE" not in text
    assert private_paths(text) == []


def test_compare_shows_figures_not_takes():
    run = {"records": [
        {"test": "Speech Level Sweep", "kind": "mic-take", "stoi": 0.7},
        {"test": "Speech Level Sweep", "kind": "mic-chain", "stoi_deficit": 0.03},
        {"test": "Speech Level Sweep", "kind": "limit", "field": "stoi_deficit",
         "value": 0.03}]}
    shown = rows(run)
    assert ("Speech Level Sweep", "mic-chain stoi_deficit") in shown
    assert not any("mic-take" in label for _, label in shown)
    assert any("mic-take" in label for _, label in rows(run, everything=True))
