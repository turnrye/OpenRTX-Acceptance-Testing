# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Two radios in one capture: channelising, scoring, and the suite on a recording."""

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
import robot
import yaml
from scipy.io import wavfile

from oat import device_mic
from oat.audio import find_alsa_device
from oat.iq import IqFormat, write_iq
from oat.m17 import codec, phy, tools
from oat.results import run_measurements

ROOT = Path(__file__).resolve().parents[1]
RATE = 250000


def _speech(seconds=4.0, rate=8000, seed=2):
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * rate)) / rate
    # Syllable-like bursts of random length, so alignment has one answer
    edges = np.cumsum(rng.uniform(0.08, 0.4, 64))
    env = (np.searchsorted(edges, t) % 2 == 0).astype(float)
    x = env * sum(np.sin(2 * np.pi * f * (1 + 0.05 * np.sin(2 * np.pi * 0.7 * t)) * t
                         + rng.uniform(0, 6)) / k
                  for k, f in enumerate((160, 320, 640, 1200, 2100, 2900), 1))
    return x / np.max(np.abs(x))


def _payloads(audio, frames=None, seed=1):
    if tools.available("c2enc"):
        pcm = np.round(audio * 0.3 * 32767).astype(np.int16)
        return tools.stream_payloads(tools.codec2_encode(pcm))
    return tools.random_payloads(frames or 50, seed)


def _two_radios(payloads_a, payloads_b, offsets, overs=1, gap_s=0.8, noise=0.01):
    """Both radios keyed together `overs` times, one capture, cu8-scale IQ."""
    parts = []
    rng = np.random.default_rng(5)
    for _ in range(overs):
        a = phy.m17_iq(codec.stream_symbols(codec.Lsf(source="AAAA"), payloads_a), RATE,
                       offset_hz=offsets[0])
        b = phy.m17_iq(codec.stream_symbols(codec.Lsf(source="BBBB"), payloads_b), RATE,
                       offset_hz=offsets[1])
        n = max(len(a), len(b))
        both = np.zeros(n, np.complex64)
        both[:len(a)] += 0.4 * a
        both[200:len(b)] += 0.4 * b[: len(b) - 200]       # B keys 1 ms later
        gap = np.zeros(int(gap_s * RATE), np.complex64)
        parts += [gap, both]
    parts.append(np.zeros(int(gap_s * RATE), np.complex64))
    x = np.concatenate(parts)
    return x + noise * (rng.normal(size=len(x)) + 1j * rng.normal(size=len(x)))


def test_pair_tuning_places_both_on_one_side():
    t = device_mic.pair_tuning(445.925e6, 445.975e6, RATE)
    assert t.center_hz == pytest.approx(445.895e6)
    assert t.offsets_hz == pytest.approx((30e3, 80e3))
    with pytest.raises(ValueError, match="apart"):
        device_mic.pair_tuning(445.925e6, 445.935e6, RATE)
    with pytest.raises(ValueError, match="fit"):
        device_mic.pair_tuning(445.925e6, 446.025e6, RATE)


def test_channelise_blocks_match_one_pass():
    rng = np.random.default_rng(0)
    x = (rng.normal(size=60000) + 1j * rng.normal(size=60000)).astype(np.complex64)
    whole = np.concatenate(list(device_mic.channelise(x, RATE, 30e3)))
    parts = np.concatenate(list(device_mic.channelise(
        [x[:12345], x[12345:40000], x[40000:]], RATE, 30e3)))
    assert len(whole) == len(parts) == 12000
    assert np.allclose(whole, parts, atol=1e-4)


def test_two_simultaneous_carriers_are_measured_apart():
    tuning = device_mic.pair_tuning(445.925e6, 445.975e6, RATE)
    pa = tools.random_payloads(40, 1)
    pb = tools.random_payloads(40, 2)
    iq = _two_radios(pa, pb, tuning.offsets_hz, overs=2)
    for off, source, payloads in ((tuning.offsets_hz[0], "AAAA", pa),
                                  (tuning.offsets_hz[1], "BBBB", pb)):
        found = device_mic.measure_channel([iq[:400000], iq[400000:]], RATE, off)
        assert len(found) == 2
        for m in found:
            assert m.lsf_crc_ok and m.source == source
            assert m.stream_frames == 40
            assert m.payload == b"".join(payloads)


def test_adc_level_undoes_the_fixed_gain():
    mic = {"gain": 32, "adc_bits": 12}
    # -20 dBFS of the ADC, times 32 into int16: -20 + 30.1 - 24.1 = -14 dBFS
    assert device_mic.adc_level(-13.98, mic) == pytest.approx(-20, abs=0.01)
    assert device_mic.adc_level(None, mic) is None


def test_score_finds_speech_seconds_after_key_up():
    clean = _speech(4.0)
    v = np.concatenate([np.zeros(16000), 0.2 * clean, np.zeros(4000)])
    m = device_mic.score_take("speech", v, clean)
    assert m["stoi"] > 0.95
    assert m["listen_stoi_45"] > m["listen_stoi_35"]
    s = device_mic.score_take("silence", np.zeros(8000) + 1e-4)
    assert "stoi" not in s and s["onset_peak_dbfs"] < -70


def test_summary_spread_and_listening():
    rows = [{"radio": "x", "test": "speech", "level": lv, "active_dbfs": a,
             "peak_dbfs": a + 12, "onset_peak_dbfs": -40.0, "stoi": 0.7, "estoi": 0.5,
             "listen_stoi_45": 0.6, "listen_stoi_35": s}
            for lv, a, s in ((-20, -40.0, 0.3), (-10, -30.0, 0.5), (0, -20.0, 0.6))]
    s = device_mic.summary(rows, "x", "speech")
    assert s["active_spread_db"] == 20
    assert s["listen_stoi_35_worst"] == 0.3
    assert device_mic.summary(rows, "y", "speech") == {"radio": "y", "takes": 0}


def test_find_alsa_device():
    listing = """**** List of PLAYBACK Hardware Devices ****
card 0: PCH [HDA Intel PCH], device 0: ALC257 Analog [ALC257 Analog]
card 2: Gen [Scarlett 4i4 4th Gen], device 0: USB Audio [USB Audio]
"""
    assert find_alsa_device("find:scarlett", listing) == "plughw:CARD=Gen,DEV=0"
    assert find_alsa_device("plughw:CARD=X,DEV=0", listing) == "plughw:CARD=X,DEV=0"
    with pytest.raises(ValueError, match="no ALSA card"):
        find_alsa_device("find:digirig", listing)


@pytest.fixture(scope="module")
def pair_run(tmp_path_factory):
    """The two-radio suite on a replayed synthetic recording of both radios."""
    tmp = tmp_path_factory.mktemp("pair")
    clean = _speech(4.0)
    wavfile.write(tmp / "speech.wav", 8000, np.round(clean * 20000).astype(np.int16))
    tuning = device_mic.pair_tuning(445.925e6, 445.975e6, RATE)
    iq = _two_radios(_payloads(clean), _payloads(0.5 * clean, seed=2),
                     tuning.offsets_hz, overs=3, gap_s=0.6)
    write_iq(tmp / "pair.cu8", iq, IqFormat.CU8)
    bench = {"station": "pairsim",
             "receiver": {"type": "replay", "recording": str(tmp / "pair.cu8"),
                          "rate": RATE},
             "transmitter": {"type": "sim", "rate": 2000000},
             "operator": {"type": "scripted", "default": "knob at 3 o'clock"},
             "audio": {"type": "sim"}}
    (tmp / "bench.yaml").write_text(yaml.safe_dump(bench))
    out = tmp / "run"
    robot.run(str(ROOT / "acceptance" / "m17" / "mic-chain-device.robot"),
              outputdir=str(out), console="none", report="NONE", log="NONE",
              variable=[f"BENCH:{tmp / 'bench.yaml'}",
                        f"CALIBRATION:{ROOT / 'calibration' / 'sim.yaml'}",
                        "FIRMWARE:baseline@7f9b6c2ad", "HARDWARE_A:CS7000-PLUS sim",
                        "HARDWARE_B:MD-UV380 sim, M17 mod", f"SPEECH_FILE:{tmp / 'speech.wav'}",
                        "SPEECH_SECONDS:4", f"RADIO_A:{ROOT / 'radios' / 'cs7000p.yaml'}",
                        f"RADIO_B:{ROOT / 'radios' / 'mduv3x0.yaml'}"])
    return out


def test_suite_writes_a_folder_per_radio(pair_run):
    for stem, hw in (("cs7000p", "CS7000-PLUS sim"), ("mduv3x0", "MD-UV380 sim, M17 mod")):
        folder = pair_run / f"bench-Mic-Chain-Device-{stem}"
        m = json.loads((folder / "measurements.json").read_text())
        assert m["radio_file"] == stem and m["hardware"] == hw
        assert m["pair"]["playback"] == "knob at 3 o'clock"
        takes = [r for r in m["records"] if r["kind"] == "mic-take"]
        assert {r["radio"] for r in takes} == {stem}
        assert sorted({r["test"] for r in takes}) == ["live", "silence", "speech"]
        assert (folder / "REPORT.md").exists() and (folder / "data" / "speech.csv").exists()
        if tools.available("c2dec"):
            assert len(list((folder / "voice").glob("*.wav"))) == 9
            speech = [r for r in takes if r["test"] == "speech"]
            assert all(r["stoi"] > 0.5 for r in speech)
            assert all(r["adc_active_dbfs"] is not None for r in speech)
    a = json.loads((pair_run / "bench-Mic-Chain-Device-cs7000p" / "data" / "rows.json")
                   .read_text())
    b = json.loads((pair_run / "bench-Mic-Chain-Device-mduv3x0" / "data" / "rows.json")
                   .read_text())
    if tools.available("c2dec"):
        # B was sent at half the level: the channels did not mix
        sa = np.mean([r["active_dbfs"] for r in a if r["test"] == "speech"])
        sb = np.mean([r["active_dbfs"] for r in b if r["test"] == "speech"])
        assert sa - sb == pytest.approx(6, abs=1.5)


def test_each_radio_exports_on_its_own(pair_run, tmp_path):
    with pytest.raises(ValueError, match="differ"):
        run_measurements(pair_run)
    for stem in ("cs7000p", "mduv3x0"):
        done = subprocess.run(["bash", str(ROOT / "scripts" / "export-run.sh"), "-b",
                               f"bench-Mic-Chain-Device-{stem}", str(pair_run),
                               str(tmp_path / "results")],
                              capture_output=True, text=True, cwd=ROOT)
        assert done.returncode == 0, done.stderr
        folder = Path(done.stdout.strip())
        assert folder.parts[-3:-1] == ("7f9b6c2ad", stem)
        assert folder.name.endswith("-pairsim-Mic-Chain-Device")
        assert (folder / "REPORT.md").exists() and (folder / "charts").is_dir()


def test_comparison_report(pair_run, tmp_path):
    done = subprocess.run(["uv", "run", "python", str(ROOT / "scripts" / "device-mic-report.py"),
                           str(pair_run), "--out", str(tmp_path / "report")],
                          capture_output=True, text=True, cwd=ROOT)
    assert done.returncode == 0, done.stderr
    for stem in ("cs7000p", "mduv3x0"):
        text = (tmp_path / "report" / stem / "REPORT.md").read_text()
        assert "## Played speech" in text and "baseline" in text
