# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Calibration files, Touchstone imports and the receiver overload check."""

import datetime
from pathlib import Path

import pytest

from oat.calibration import load_calibration, read_touchstone_s21
from oat.config import check_receiver_safe

DATA = Path(__file__).parent / "data"
RADIO = {"name": "Test radio", "max_tx_power_dbm": 37}


@pytest.mark.parametrize("name", ["path-db.s2p", "path-ma.s2p",
                                  "path-ri.s2p"])
def test_touchstone_formats_agree(name):
    path = read_touchstone_s21(DATA / name)
    assert path.at(140e6) == pytest.approx(60.0, abs=0.01)
    assert path.at(450e6) == pytest.approx(61.0, abs=0.01)
    assert path.at(295e6) == pytest.approx(60.5, abs=0.01)


def test_outside_the_calibrated_range_is_refused():
    path = read_touchstone_s21(DATA / "path-db.s2p")
    with pytest.raises(ValueError, match="outside the calibrated range"):
        path.at(900e6)


def write_cal(tmp_path, body):
    path = tmp_path / "cal.yaml"
    path.write_text(body)
    return path


def test_calibration_file_with_points_and_touchstone(tmp_path):
    (tmp_path / "rx.s2p").write_text((DATA / "path-db.s2p").read_text())
    cal = load_calibration(write_cal(tmp_path, """
method: vna
measured: 2026-10-20
expires: 2027-04-20
paths:
  to_receiver: {touchstone: rx.s2p}
  from_transmitter: {points: [[0, 30]]}
"""))
    assert cal.loss("to_receiver", 450e6) == pytest.approx(61.0, abs=0.01)
    assert cal.loss("from_transmitter", 2.4e9) == 30
    assert not cal.expired(datetime.date(2027, 4, 20))
    assert cal.expired(datetime.date(2027, 4, 21))
    summary = cal.summary()
    assert summary["method"] == "vna" and len(summary["sha256"]) == 12


def test_fingerprint_follows_the_touchstone_data(tmp_path):
    body = "method: vna\nmeasured: 2026-10-20\nexpires: 2027-04-20\n" \
           "paths: {to_receiver: {touchstone: rx.s2p}}\n"
    (tmp_path / "rx.s2p").write_text((DATA / "path-db.s2p").read_text())
    first = load_calibration(write_cal(tmp_path, body)).sha256
    (tmp_path / "rx.s2p").write_text((DATA / "path-ma.s2p").read_text())
    assert load_calibration(write_cal(tmp_path, body)).sha256 != first


def test_method_and_dates_are_required(tmp_path):
    with pytest.raises(ValueError, match="method"):
        load_calibration(write_cal(tmp_path, "measured: 2026-10-20\n"))
    with pytest.raises(ValueError, match="expires"):
        load_calibration(write_cal(
            tmp_path, "method: assumed\nmeasured: 2026-10-20\n"))


def test_receiver_overload_is_refused(tmp_path):
    def cal(loss):
        return load_calibration(write_cal(tmp_path, f"""
method: assumed
measured: 2026-10-20
expires: 2099-01-01
paths: {{to_receiver: {{points: [[0, {loss}]]}}}}
"""))
    check_receiver_safe(RADIO, cal(60), 10.0, 446e6)
    with pytest.raises(ValueError, match="add at least 10 dB"):
        check_receiver_safe(RADIO, cal(27), 10.0, 446e6)


LEVELS = """
method: vna
measured: 2026-11-01
expires: 2027-05-01
paths:
  to_receiver: {points: [[0, 60]]}
  from_transmitter: {points: [[100000000, 50], [500000000, 52]]}
  audio_out: {mvrms_at_0dbfs: 12.5}
levels:
  transmitter:
    instrument: hackrf
    amp: false
    points:
      146580000: [[0, -58], [10, -48], [47, -11]]
      445975000: [[47, -15], [0, -62], [10, -52]]
  receiver:
    - instrument: rtlsdr
      settings: {gain_db: 0}
      full_scale_dbm: [[146580000, -10], [445975000, -12]]
    - instrument: rtlsdr
      settings: {gain_db: 20}
      full_scale_dbm: [[0, -30]]
frequency_correction_ppm:
  rtlsdr: 1.5
  hackrf: -0.5
  hackrf:abc: 0.25
"""


def test_old_calibrations_load_without_levels(tmp_path):
    cal = load_calibration(write_cal(tmp_path, """
method: assumed
measured: 2026-10-20
expires: 2099-01-01
paths: {to_receiver: {points: [[0, 60]]}}
"""))
    assert cal.calibrated() == []
    assert "calibrated" not in cal.summary()
    assert cal.frequency_correction_ppm({"type": "rtlsdr"}) is None
    with pytest.raises(LookupError, match="levels.transmitter"):
        cal.transmitter_setting({"type": "hackrf"}, 446e6, -60, 10)
    with pytest.raises(LookupError, match="levels.receiver"):
        cal.receiver_full_scale_dbm({"type": "rtlsdr"}, 446e6)


def test_summary_names_sections_never_values(tmp_path):
    cal = load_calibration(write_cal(tmp_path, LEVELS))
    summary = cal.summary()
    assert summary["calibrated"] == [
        "levels.transmitter", "levels.receiver", "frequency_correction_ppm",
        "paths.audio_out"]
    assert "12.5" not in str(summary) and "-58" not in str(summary)
    # Audio levels are not RF paths
    assert set(cal.paths) == {"to_receiver", "from_transmitter"}
    assert cal.audio_full_scale_mvrms("audio_out") == 12.5
    with pytest.raises(LookupError, match="audio_in"):
        cal.audio_full_scale_mvrms("audio_in")


def test_transmitter_levels_interpolate_in_gain_and_frequency(tmp_path):
    table = load_calibration(write_cal(tmp_path, LEVELS)).transmitter_levels
    assert table.level(146.58e6, 5) == pytest.approx(-53)
    assert table.level(445.975e6, 0) == pytest.approx(-62)
    middle = (146.58e6 + 445.975e6) / 2
    assert table.level(middle, 10) == pytest.approx(-50)
    with pytest.raises(ValueError, match="outside the transmitter level"):
        table.level(900e6, 10)


def test_transmitter_setting_reaches_a_level(tmp_path):
    cal = load_calibration(write_cal(tmp_path, LEVELS))
    hackrf = {"type": "hackrf", "serial": "xyz"}
    gain, backoff = cal.transmitter_setting(hackrf, 146.58e6, -50.5, 10)
    assert gain == 8 and backoff == pytest.approx(0.5)
    # Below the lowest gain, the rest is digital back-off, within limits
    gain, backoff = cal.transmitter_setting(hackrf, 146.58e6, -65, 10)
    assert gain == 0 and backoff == pytest.approx(7)
    with pytest.raises(ValueError, match="step attenuator"):
        cal.transmitter_setting(hackrf, 146.58e6, -75, 10)
    with pytest.raises(ValueError, match="above the transmitter"):
        cal.transmitter_setting(hackrf, 146.58e6, -5, 10)
    with pytest.raises(LookupError, match="amp off"):
        cal.transmitter_setting({**hackrf, "amp": True}, 146.58e6, -50, 10)
    with pytest.raises(LookupError, match="not this bench"):
        cal.transmitter_setting({"type": "sim"}, 146.58e6, -50, 10)


def test_receiver_levels_match_instrument_and_settings(tmp_path):
    cal = load_calibration(write_cal(tmp_path, LEVELS))
    assert cal.receiver_full_scale_dbm({"type": "rtlsdr"}, 146.58e6) == -10
    assert cal.receiver_full_scale_dbm(
        {"type": "rtlsdr", "gain_db": 20}, 146.58e6) == -30
    with pytest.raises(LookupError, match="gain_db"):
        cal.receiver_full_scale_dbm({"type": "rtlsdr", "gain_db": 10}, 446e6)
    with pytest.raises(LookupError):
        cal.receiver_full_scale_dbm({"type": "hackrf"}, 446e6)


def test_frequency_correction_per_instrument(tmp_path):
    cal = load_calibration(write_cal(tmp_path, LEVELS))
    assert cal.frequency_correction_ppm({"type": "rtlsdr"}) == 1.5
    assert cal.frequency_correction_ppm({"type": "hackrf"}) == -0.5
    assert cal.frequency_correction_ppm(
        {"type": "hackrf", "serial": "abc"}) == 0.25
    assert cal.frequency_correction_ppm({"type": "sim"}) is None


def test_example_calibration_loads(tmp_path):
    example = (Path(__file__).parents[1] / "calibration" / "example.yaml")
    (tmp_path / "to_receiver.local.s2p").write_text(
        (DATA / "path-db.s2p").read_text())
    cal = load_calibration(write_cal(tmp_path, example.read_text()))
    assert len(cal.calibrated()) == 5
