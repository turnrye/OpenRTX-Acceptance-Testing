# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Signal generation and measurement agree with each other."""

import numpy as np
import pytest

from oat.analysis import find_transmissions, measure_capture
from oat.iq import IqFormat, iter_iq, read_iq, write_iq
from oat.signals import fm_carriers, fm_tone, silence

RATE = 250e3


def burst_capture(deviations, offset_hz=75e3, gap_s=1.0, seconds=3.0,
                  noise=0.01, seed=0):
    """Bursts of a 1 kHz FM tone separated by silence, with noise."""
    parts = [silence(gap_s, RATE)]
    for dev in deviations:
        parts += [fm_tone(1000, dev, seconds, RATE, offset_hz),
                  silence(gap_s, RATE)]
    iq = np.concatenate(parts)
    rng = np.random.default_rng(seed)
    n = rng.normal(scale=noise, size=(len(iq), 2))
    return iq + (n[:, 0] + 1j * n[:, 1])


@pytest.mark.parametrize("deviation", [1500, 2500, 5000])
def test_peak_deviation_and_tone(deviation):
    m, = measure_capture(burst_capture([deviation]), RATE, 75e3)
    assert m.peak_deviation_hz == pytest.approx(deviation, rel=0.03)
    assert m.rms_deviation_hz == pytest.approx(deviation / np.sqrt(2),
                                               rel=0.03)
    assert m.tone_hz == pytest.approx(1000, abs=5)
    assert m.carrier_offset_hz == pytest.approx(75e3, abs=20)


def test_occupied_bandwidth_follows_carson():
    narrow, wide = measure_capture(burst_capture([2500, 5000]), RATE, 75e3)
    # Carson's rule, 2 * (deviation + tone), is close to the 99% bandwidth
    assert narrow.occupied_bandwidth_hz == pytest.approx(7000, rel=0.15)
    assert wide.occupied_bandwidth_hz == pytest.approx(12000, rel=0.15)


def test_bursts_are_found_and_short_ones_ignored():
    iq = np.concatenate([burst_capture([3000]), fm_tone(1000, 3000, 0.5, RATE,
                                                        75e3)])
    spans = find_transmissions(iq, RATE)
    assert len(spans) == 1


def test_noise_alone_has_no_transmission():
    rng = np.random.default_rng(1)
    n = rng.normal(scale=0.01, size=(int(3 * RATE), 2))
    assert find_transmissions(n[:, 0] + 1j * n[:, 1], RATE) == []


def test_measurement_survives_decimation():
    rate = 2e6
    iq = np.concatenate([silence(0.5, rate), fm_tone(1000, 4000, 2.0, rate,
                                                     200e3),
                         silence(0.5, rate)])
    m, = measure_capture(iq, rate, 200e3)
    assert m.peak_deviation_hz == pytest.approx(4000, rel=0.03)


def test_capture_read_in_blocks_measures_as_read_whole(tmp_path):
    """A HackRF-rate capture, read in blocks, measures as when read whole."""
    rate = 2e6
    iq = np.concatenate([silence(0.5, rate), fm_tone(1000, 4000, 2.0, rate,
                                                     250e3),
                         silence(0.5, rate)])
    path = tmp_path / "capture.cs8"
    write_iq(path, iq, IqFormat.CS8)
    whole, = measure_capture(read_iq(path, IqFormat.CS8), rate, 250e3)
    # An odd block size, so blocks split the decimation steps unevenly
    blocks, = measure_capture(iter_iq(path, IqFormat.CS8, samples=100_003),
                              rate, 250e3)
    assert blocks.peak_deviation_hz == pytest.approx(4000, rel=0.03)
    assert blocks.peak_deviation_hz == pytest.approx(whole.peak_deviation_hz,
                                                     rel=0.01)
    assert blocks.carrier_offset_hz == pytest.approx(whole.carrier_offset_hz,
                                                     abs=1)
    assert blocks.start_s == pytest.approx(whole.start_s, abs=1e-3)


@pytest.mark.parametrize("fmt", list(IqFormat))
def test_iq_round_trip(tmp_path, fmt):
    iq = fm_tone(1000, 3000, 0.1, RATE, 10e3)
    path = tmp_path / f"x.{fmt.value}"
    write_iq(path, iq, fmt)
    back = read_iq(path, fmt)
    assert np.max(np.abs(back - iq)) < 0.01


@pytest.mark.parametrize("ctcss_hz, ctcss_dev", [(67.0, 300), (100.0, 500),
                                                  (254.1, 750)])
def test_ctcss_tone_and_deviation(ctcss_hz, ctcss_dev):
    iq = np.concatenate([silence(1.0, RATE),
                         fm_tone(1000, 2000, 3.0, RATE, 75e3, ctcss_hz,
                                 ctcss_dev),
                         silence(1.0, RATE)])
    m, = measure_capture(iq, RATE, 75e3)
    assert m.ctcss_hz == pytest.approx(ctcss_hz, abs=0.1)
    assert m.ctcss_deviation_hz == pytest.approx(ctcss_dev, rel=0.05)
    assert m.tone_hz == pytest.approx(1000, abs=5)


def test_carriers_sit_at_their_offsets_and_levels():
    iq = fm_carriers([{"tone_hz": 1000, "deviation_hz": 1500,
                       "offset_hz": 0.0, "level_db": -30},
                      {"tone_hz": 400, "deviation_hz": 2500,
                       "offset_hz": 12500.0}], 1.0, RATE)
    spec = np.abs(np.fft.fftshift(np.fft.fft(iq))) ** 2
    freqs = np.fft.fftshift(np.fft.fftfreq(len(iq), 1 / RATE))

    def band_power(centre):
        return spec[np.abs(freqs - centre) < 5000].sum()

    ratio_db = 10 * np.log10(band_power(12500) / band_power(0))
    assert ratio_db == pytest.approx(30, abs=1)
    assert np.max(np.abs(iq)) <= 0.9 + 1e-6


def test_switching_a_carrier_off_keeps_the_others_level():
    def carriers(on):
        return [{"tone_hz": 1000, "deviation_hz": 1500, "offset_hz": 0.0,
                 "level_db": -30},
                {"tone_hz": 400, "deviation_hz": 2500, "offset_hz": 12500.0,
                 "on": on}]
    both = fm_carriers(carriers(True), 1.0, RATE)
    alone = fm_carriers(carriers(False), 1.0, RATE)
    spec_both = np.abs(np.fft.fft(both)) ** 2
    spec_alone = np.abs(np.fft.fft(alone)) ** 2
    freqs = np.fft.fftfreq(len(both), 1 / RATE)
    near = np.abs(freqs) < 5000
    assert spec_alone[near].sum() == pytest.approx(spec_both[near].sum(),
                                                   rel=0.02)
    assert np.abs(alone).max() == pytest.approx(0.9 / (1 + 10 ** 1.5),
                                                rel=1e-3)


def test_no_tone_is_reported_without_audio():
    iq = np.concatenate([silence(1.0, RATE),
                         fm_tone(1000, 0, 3.0, RATE, 75e3, 100.0, 500),
                         silence(1.0, RATE)])
    m, = measure_capture(iq, RATE, 75e3)
    assert m.tone_hz == 0.0
    assert m.ctcss_hz == pytest.approx(100.0, abs=0.1)


def test_capture_skips_segments_never_written(tmp_path):
    from oat.sdr.base import Capture
    first = tmp_path / "c.cu8"
    write_iq(first, fm_tone(1000, 3000, 0.1, RATE, 10e3), IqFormat.CU8)
    empty = tmp_path / "c-02.cu8"
    empty.touch()
    capture = Capture([first, empty, tmp_path / "c-03.cu8"], RATE, 0.0,
                      IqFormat.CU8, restarts=2)
    assert len(capture.load()) == int(0.1 * RATE)
