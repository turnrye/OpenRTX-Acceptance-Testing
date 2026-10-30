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
