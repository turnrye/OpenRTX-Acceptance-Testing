# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
The calibration walk-through, with the sim SDR backends and a fake
LibreVNA-GUI: never real instruments.
"""

import datetime
import threading

import numpy as np
import pytest
import yaml

from oat.calibrate import Aborted, Fragment, add_months, carrier_offset, \
    main, measure_audio, measure_frequency, measure_paths, \
    measure_receiver, measure_transmitter, parse_steps, read_notes, \
    write_calibration
from oat.calibration import load_calibration, read_touchstone_s21
from oat.iq import read_iq
from oat.operator import ScriptedOperator
from oat.sdr.librevna import LibreVNA
from oat.sdr.sim import SimBurst, SimReceiver, SimTransmitter
from test_librevna import FakeLibreVNA

TODAY = datetime.date(2026, 11, 1)
TX_CFG = {"type": "sim", "rate": 2000000, "tune_offset_hz": -250000}
RX_CFG = {"type": "sim", "rate": 250000, "tune_offset_hz": -75000}
FREQS = [146580000, 445975000]


@pytest.fixture
def fake():
    server = FakeLibreVNA()
    thread = threading.Thread(target=server.serve_forever, args=(0.01,),
                              daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def vna(fake):
    with LibreVNA("127.0.0.1", fake.port, timeout_s=1.0, poll_s=0.01) as v:
        yield v


class LevelTransmitter(SimTransmitter):
    """
    A sim transmitter the fake LibreVNA can hear: its carrier is -60 dBm at
    gain 0, plus the gain, less the pad, and its gain stops at 47 dB.
    """

    def __init__(self, fake, base_db, pad_db):
        super().__init__()
        self.fake, self.base_db, self.pad_db = fake, base_db, pad_db
        self.gains = []

    def start(self, path, center_hz, rate, repeat=True, gain_offset_db=0.0):
        super().start(path, center_hz, rate, repeat, gain_offset_db)
        applied = min(gain_offset_db, 47 - self.base_db)
        gain = self.base_db + applied
        self.gains.append(gain)
        level = -60 + gain - self.pad_db
        self.fake.signal = lambda: (center_hz + 250000, level)
        return applied

    def stop(self):
        super().stop()
        self.fake.signal = lambda: None


def test_c1_sweeps_both_paths(fake, vna, tmp_path):
    op = ScriptedOperator()
    frag = measure_paths(vna, op, tmp_path, "bench", TODAY, points=51)
    assert frag.data["method"] == "vna"
    assert frag.data["expires"] == datetime.date(2027, 5, 1)
    assert frag.data["paths"] == {
        "to_receiver": {"touchstone": "bench-to_receiver.local.s2p"},
        "from_transmitter": {"touchstone": "bench-from_transmitter.local.s2p"}}
    loss = read_touchstone_s21(tmp_path / "bench-to_receiver.local.s2p")
    assert loss.at(446e6) == pytest.approx(60, abs=1e-3)
    assert "VNA:FREQuency:CENTer 350000000" in fake.log
    assert len(op.log) == 2 and "to_receiver" in op.log[0].text
    assert "loss 60.0-60.0 dB" in frag.notes["paths.to_receiver"]


def test_c1_stops_without_a_vna_calibration(fake, vna, tmp_path):
    fake.calibration = "NONE"
    with pytest.raises(Aborted, match="SOLT"):
        measure_paths(vna, ScriptedOperator(["no"]), tmp_path, "b", TODAY)


def test_c2_reads_the_transmitter_through_the_pad(fake, vna, tmp_path):
    cfg = {**TX_CFG, "tx_vga_db": 10}
    tx = LevelTransmitter(fake, 10, 20)
    frag = measure_transmitter(vna, tx, cfg, ScriptedOperator(), FREQS,
                               [0, 20, 47, 50], 20, tmp_path, TODAY,
                               settle_s=0)
    table = frag.data["levels"]["transmitter"]
    assert table["instrument"] == "sim" and table["amp"] is False
    # Gains are what the transmitter applied; 50 clamps to 47 and merges
    assert table["points"][146580000] == [[0, -60.0], [20, -40.0],
                                          [47, -13.0]]
    assert set(table["points"]) == set(FREQS)
    # Offset from the tuning by the bench's tune offset, at IQ_AMPLITUDE
    path, center, rate, repeat = tx.log[0]
    assert center == 146580000 - 250000 and repeat
    iq = read_iq(path, tx.fmt)
    assert np.mean(np.abs(iq)) == pytest.approx(0.9, abs=0.01)
    assert carrier_offset(iq, rate, 250000, 1000) == pytest.approx(250000,
                                                                   abs=5)
    assert not tx.active


def test_c2_refuses_a_missing_carrier(fake, vna, tmp_path):
    with pytest.raises(ValueError, match="no carrier"):
        measure_transmitter(vna, SimTransmitter(), TX_CFG,
                            ScriptedOperator(), FREQS, [0], 20, tmp_path,
                            TODAY, settle_s=0)


def carrier_receiver(level_db, offsets_hz):
    """A sim receiver with one steady carrier per capture."""
    rx = SimReceiver(gap_s=0.0)
    for offset in offsets_hz:
        rx.queue_scene([SimBurst(deviation_hz=0, duration_s=0.5,
                                 level_db=level_db)], offset)
    return rx


def test_c3_full_scale_level(fake, vna, tmp_path):
    rx = carrier_receiver(-10, [75000, 75000])
    frag = measure_receiver(vna, rx, RX_CFG, ScriptedOperator(), FREQS,
                            -30, 10, tmp_path, TODAY, capture_s=0)
    entry = frag.data["levels"]["receiver"][0]
    assert entry["instrument"] == "sim" and entry["settings"] == {}
    # The carrier reads 20 log10(0.9) - 10 dBFS at -40 dBm in
    expected = -40 - (20 * np.log10(0.9) - 10)
    for freq, (f, dbm) in zip(FREQS, entry["full_scale_dbm"]):
        assert f == freq and dbm == pytest.approx(expected, abs=0.1)
    assert fake.commands("GENerator:FREQuency") == [
        "GENerator:FREQuency 146580000", "GENerator:FREQuency 445975000"]
    assert fake.commands("GENerator:PORT")[-1] == "GENerator:PORT 0"


def test_c3_records_the_receiver_settings(fake, vna, tmp_path):
    rx = carrier_receiver(-10, [75000])
    cfg = {**RX_CFG, "type": "hackrf", "serial": "abc", "vga_db": 8}
    frag = measure_receiver(vna, rx, cfg, ScriptedOperator(), FREQS[:1],
                            -30, 10, tmp_path, TODAY, capture_s=0)
    entry = frag.data["levels"]["receiver"][0]
    assert entry["instrument"] == "hackrf:abc"
    assert entry["settings"] == {"lna_db": 0, "vga_db": 8, "amp": False}


def test_c3_refuses_clipping_and_silence(fake, vna, tmp_path):
    with pytest.raises(ValueError, match="clips"):
        measure_receiver(vna, carrier_receiver(3, [75000]), RX_CFG,
                         ScriptedOperator(), FREQS[:1], -30, 10, tmp_path,
                         TODAY, capture_s=0)
    with pytest.raises(ValueError, match="no clean carrier"):
        measure_receiver(vna, SimReceiver(gap_s=0.5), RX_CFG,
                         ScriptedOperator(), FREQS[:1], -30, 10, tmp_path,
                         TODAY, capture_s=0)
    assert fake.commands("GENerator:PORT")[-1] == "GENerator:PORT 0"


def test_c4_from_the_generator(fake, vna, tmp_path):
    # A receiver 2 ppm fast reads the carrier low by 2 ppm
    freq = 462650000
    rx = carrier_receiver(-10, [75000 - 2e-6 * freq])
    frag = measure_frequency(vna, rx, RX_CFG, ScriptedOperator([""]), freq,
                             -30, tmp_path, TODAY, capture_s=0)
    ppm = frag.data["frequency_correction_ppm"]["sim"]
    assert ppm == pytest.approx(2.0, abs=0.02)
    assert "LibreVNA generator" in frag.notes["frequency_correction_ppm.sim"]
    assert "GENerator:FREQuency 462650000" in fake.log


def test_c4_from_a_known_carrier_without_a_vna(tmp_path):
    rx = carrier_receiver(-10, [75000 + 1.5e-6 * 162.55e6])
    op = ScriptedOperator(["162.55", "50"])
    frag = measure_frequency(None, rx, RX_CFG, op, 446e6, -30, tmp_path,
                             TODAY, capture_s=0)
    assert frag.data["frequency_correction_ppm"]["sim"] == pytest.approx(
        -1.5, abs=0.02)
    note = frag.notes["frequency_correction_ppm.sim"]
    assert "162.5500 MHz +-50 Hz (+-0.31 ppm)" in note
    with pytest.raises(Aborted):
        measure_frequency(None, rx, RX_CFG, ScriptedOperator([""]), 446e6,
                          -30, tmp_path, TODAY, capture_s=0)


def test_c5_audio_levels_typed_by_the_operator():
    frag = measure_audio(ScriptedOperator(["12.5", ""]), TODAY)
    assert frag.data == {"paths": {"audio_out": {"mvrms_at_0dbfs": 12.5}}}
    assert measure_audio(ScriptedOperator(["", ""]), TODAY).data == {}
    with pytest.raises(ValueError):
        measure_audio(ScriptedOperator(["-1"]), TODAY)


def all_fragments(fake, vna, tmp_path):
    tx = LevelTransmitter(fake, 0, 20)
    return [
        measure_paths(vna, ScriptedOperator(), tmp_path, "bench", TODAY,
                      points=11),
        measure_transmitter(vna, tx, TX_CFG, ScriptedOperator(), FREQS,
                            [0, 47], 20, tmp_path, TODAY, settle_s=0),
        measure_receiver(vna, carrier_receiver(-10, [75000, 75000]), RX_CFG,
                         ScriptedOperator(), FREQS, -30, 10, tmp_path, TODAY,
                         capture_s=0),
        measure_frequency(vna, carrier_receiver(-10, [75000]), RX_CFG,
                          ScriptedOperator([""]), FREQS[1], -30, tmp_path,
                          TODAY, capture_s=0),
        measure_audio(ScriptedOperator(["12.5", "950"]), TODAY),
    ]


def test_written_file_loads_with_every_section(fake, vna, tmp_path):
    path = tmp_path / "bench.local.yaml"
    write_calibration(path, all_fragments(fake, vna, tmp_path), TODAY)
    cal = load_calibration(path)
    assert cal.method == "vna" and cal.measured == TODAY
    assert cal.calibrated() == [
        "levels.transmitter", "levels.receiver", "frequency_correction_ppm",
        "paths.audio_out", "paths.audio_in"]
    assert cal.loss("to_receiver", 446e6) == pytest.approx(60, abs=1e-3)
    assert cal.transmitter_levels.level(146.58e6, 47) == pytest.approx(-13)
    assert cal.receiver_full_scale_dbm({"type": "sim"}, 300e6) == \
        pytest.approx(-29.1, abs=0.1)
    assert cal.frequency_correction_ppm({"type": "sim"}) == pytest.approx(
        0, abs=0.02)
    text = path.read_text()
    assert "# LibreVNA sweep 100-600 MHz" in text
    assert "\nlevels:\n  # LibreVNA spectrum analyser" in text


EXISTING = """\
# My bench
method: power-meter
measured: 2026-06-01
expires: 2026-12-01

paths:
  # Two 30 dB pads
  to_receiver: {points: [[0, 60]]}
  audio_out: {mvrms_at_0dbfs: 11}
levels:
  receiver:
    - instrument: sim
      settings: {}
      full_scale_dbm: [[0, 5.0]]
    - instrument: rtlsdr
      settings: {gain_db: 20}
      full_scale_dbm: [[0, -30]]
frequency_correction_ppm:
  # Against WWV
  rtlsdr: 1.8
  sim: 9.0
"""


def test_merges_into_an_existing_file(fake, vna, tmp_path):
    path = tmp_path / "bench.local.yaml"
    path.write_text(EXISTING)
    fragments = all_fragments(fake, vna, tmp_path)
    write_calibration(path, [fragments[2], fragments[3]], TODAY)
    cal = load_calibration(path)
    # Not re-measured: kept, with its comments and dates
    assert cal.method == "power-meter"
    assert cal.measured == datetime.date(2026, 6, 1)
    assert cal.loss("to_receiver", 1e9) == 60
    assert cal.audio_full_scale_mvrms("audio_out") == 11
    assert cal.frequency_correction_ppm({"type": "rtlsdr"}) == 1.8
    text = path.read_text()
    assert "  # Two 30 dB pads\n  to_receiver:" in text
    assert "  # Against WWV\n  rtlsdr: 1.8" in text
    # Re-measured: replaced, the other receiver's table kept
    assert cal.frequency_correction_ppm({"type": "sim"}) == pytest.approx(
        0, abs=0.02)
    assert [t.instrument for t in cal.receiver_levels] == ["rtlsdr", "sim"]
    assert cal.receiver_full_scale_dbm({"type": "sim"}, 300e6) == \
        pytest.approx(-29.1, abs=0.1)
    # A later C1 replaces the paths and dates
    write_calibration(path, [fragments[0]], TODAY)
    cal = load_calibration(path)
    assert cal.method == "vna" and cal.expires == datetime.date(2027, 5, 1)
    assert "touchstone" in yaml.safe_load(path.read_text())["paths"][
        "to_receiver"]
    assert cal.audio_full_scale_mvrms("audio_out") == 11


def test_new_file_without_paths_still_loads(tmp_path):
    path = tmp_path / "x.local.yaml"
    write_calibration(path, [Fragment({"paths": {
        "audio_in": {"mvrms_at_0dbfs": 900.0}}})], TODAY)
    cal = load_calibration(path)
    assert cal.method == "assumed" and cal.calibrated() == ["paths.audio_in"]
    assert "run C1" in path.read_text()


def test_notes_and_helpers():
    notes = read_notes("# head\n\n# a\nmethod: vna\npaths:\n  # b\n  x: 1\n"
                       "frequency_correction_ppm:\n  # c\n  hackrf:abc: 1\n")
    assert notes == {"method": "a", "paths.x": "b",
                     "frequency_correction_ppm.hackrf:abc": "c"}
    assert add_months(datetime.date(2026, 8, 31), 6) == \
        datetime.date(2027, 2, 28)
    assert parse_steps("c3, C1") == ["C1", "C3"]
    with pytest.raises(Exception, match="C9"):
        parse_steps("C9")


def test_command_line(fake, tmp_path):
    bench = tmp_path / "bench.yaml"
    bench.write_text(yaml.safe_dump({
        "station": "simbench", "receiver": RX_CFG, "transmitter": TX_CFG,
        "operator": {"type": "scripted", "answers": ["", "", "7.5", ""]}}))
    out = tmp_path / "cal" / "simbench.local.yaml"
    assert main(["--bench", str(bench), "--output", str(out),
                 "--steps", "C1,C5", "--port", str(fake.port)]) == 0
    cal = load_calibration(out)
    assert cal.calibrated() == ["paths.audio_out"]
    assert (out.parent / "simbench-to_receiver.local.s2p").exists()
