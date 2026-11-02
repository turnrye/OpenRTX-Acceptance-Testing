# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Generated M17 signals measure as generated."""

import json
from pathlib import Path

import numpy as np
import pytest

from oat.m17 import codec, phy, tools
from oat.signals import silence

RATE = 250e3
REFERENCE = json.loads((Path(__file__).parent / "data"
                        / "m17-reference.json").read_text())


def transmission(frames=25, source="K0RET", preamble=192, seed=3):
    lsf = codec.Lsf("@ALL", source)
    return codec.stream_symbols(lsf, tools.random_payloads(frames, seed),
                                preamble_symbols=preamble)


def capture(*bursts, rate=RATE, noise=0.01, seed=0, gap_s=0.5):
    """Each burst: (symbols, m17_iq keyword arguments)."""
    parts = [silence(gap_s, rate)]
    for symbols, kwargs in bursts:
        parts += [phy.m17_iq(symbols, rate, **kwargs), silence(gap_s, rate)]
    iq = np.concatenate(parts)
    rng = np.random.default_rng(seed)
    n = rng.normal(scale=noise, size=(len(iq), 2))
    return (iq + (n[:, 0] + 1j * n[:, 1])).astype(np.complex64)


def test_rrc_matches_libm17():
    assert np.max(np.abs(phy.RRC - REFERENCE["rrc_taps_10"])) < 1e-6


def test_filters_together_read_symbol_deviations():
    symbols = transmission(10)
    dev = phy.baseband(symbols, 2400)
    matched = np.convolve(dev, phy.RRC_RX)
    delay = len(phy.RRC) - 1
    at = matched[delay::phy.SAMPLES_PER_SYMBOL][:len(symbols)]
    assert np.max(np.abs(at - 800.0 * symbols)) < 10
    # A steady symbol deviates the carrier by its nominal amount
    steady = phy.baseband(np.full(50, 3), 2400)
    assert steady[250] == pytest.approx(2400, rel=0.005)


@pytest.mark.parametrize("outer, inner, offset, inverted", [
    (2400, 800, 75e3, False),
    (2350, 790, 74e3, True),
    (2460, 835, 76.5e3, False),
])
def test_round_trip_recovers_the_signal(outer, inner, offset, inverted):
    m, = phy.measure_m17_capture(capture((transmission(), dict(
        outer_deviation_hz=outer, inner_deviation_hz=inner, offset_hz=offset,
        inverted=inverted))), RATE, 75e3)
    assert m.outer_deviation_hz == pytest.approx(outer, rel=0.005)
    assert m.inner_deviation_hz == pytest.approx(inner, rel=0.01)
    assert m.deviation_ratio == pytest.approx(outer / inner, rel=0.01)
    assert m.carrier_offset_hz == pytest.approx(offset, abs=5)
    assert m.polarity == ("inverted" if inverted else "normal")
    assert m.lsf_found and m.lsf_crc_ok and m.source == "K0RET"
    assert m.destination == "@ALL" and m.lsf_type == codec.TYPE_VOICE_STREAM
    assert m.stream_frames == 25 and m.frame_number_errors == 0
    assert m.last_frame_flagged and m.eot_found and m.lich_lsf_ok
    assert m.evm_percent < 2
    assert abs(m.symbol_rate_error_ppm) < 2
    assert m.payload == b"".join(tools.random_payloads(25, 3))


def test_symbol_rate_error_and_preamble_are_measured():
    # Sampled as if the radio's symbol clock ran 40 ppm slow
    iq = capture((transmission(100, preamble=768), {}), rate=RATE * 1.00004)
    m, = phy.measure_m17_capture(iq, RATE, 0.0)
    assert m.symbol_rate_error_ppm == pytest.approx(-40, abs=2)
    assert m.preamble_s == pytest.approx(0.160, abs=0.002)
    assert m.lsf_latency_s == pytest.approx(0.160, abs=0.003)
    assert m.stream_frames == 100 and m.frame_number_errors == 0


def test_receiver_reference_error_is_corrected():
    iq = capture((transmission(), dict(offset_hz=75e3)))
    m, = phy.measure_m17_capture(iq, RATE, 75e3, center_hz=446e6,
                                 frequency_correction_ppm=1.0)
    assert m.carrier_offset_hz == pytest.approx(75e3 + 446, abs=5)


def test_several_transmissions_and_noise():
    iq = capture((transmission(10, "AB1CDE"), dict(offset_hz=75e3)),
                 (transmission(12, "N0CALL"), dict(offset_hz=75e3,
                                                   inverted=True)),
                 noise=0.05)
    first, second = phy.measure_m17_capture(iq, RATE, 75e3)
    assert (first.source, first.polarity) == ("AB1CDE", "normal")
    assert (second.source, second.polarity) == ("N0CALL", "inverted")
    assert second.stream_frames == 12 and second.lsf_crc_ok
    assert first.evm_percent < 10


def test_occupied_bandwidth_of_nominal_m17():
    m, = phy.measure_m17_capture(capture((transmission(50), {})), RATE, 0.0)
    assert 8000 < m.occupied_bandwidth_hz < 10000


def test_hackrf_rate_capture_in_blocks(tmp_path):
    from oat.iq import IqFormat, iter_iq, write_iq
    rate = 2e6
    path = tmp_path / "m17.cs8"
    write_iq(path, capture((transmission(10), dict(offset_hz=250e3)),
                           rate=rate), IqFormat.CS8)
    m, = phy.measure_m17_capture(iter_iq(path, IqFormat.CS8, 100_003), rate,
                                 250e3, audio_dir=tmp_path)
    assert m.lsf_crc_ok and m.stream_frames == 10
    assert m.outer_deviation_hz == pytest.approx(2400, rel=0.01)
    assert (tmp_path / "tx1-baseband.wav").exists()


@pytest.mark.skipif(not (tools.available("c2enc") and tools.available("c2dec")),
                    reason="codec2 (c2enc, c2dec) not installed")
def test_codec2_tone_survives_the_round_trip(tmp_path):
    payloads = tools.tone_payloads(1000, 1.0, -20)
    symbols = codec.stream_symbols(codec.Lsf(), payloads)
    m, = phy.measure_m17_capture(capture((symbols, {})), RATE, 0.0)
    pcm = tools.write_codec2_wav(tmp_path / "voice.wav", m.payload)
    spec = np.abs(np.fft.rfft(pcm * np.hanning(len(pcm))))
    freqs = np.fft.rfftfreq(len(pcm), 1 / tools.CODEC2_RATE)
    assert freqs[np.argmax(spec)] == pytest.approx(1000, abs=30)


def test_m17_demod_is_optional():
    if tools.available(tools.M17_DEMOD):
        pytest.skip("m17-demod installed; covered by the next test")
    assert tools.m17_demod(np.zeros(4800)) is None


@pytest.mark.skipif(not tools.available(tools.M17_DEMOD),
                    reason="m17-demod (m17-cxx-demod) not installed")
def test_m17_demod_decodes_our_signal():
    m, = phy.measure_m17_capture(capture((transmission(25), {})), RATE, 0.0)
    result = tools.m17_demod(m.baseband)
    assert any(lsf["source"] == "K0RET" for lsf in result["lsfs"]), \
        result["text"]


def test_lsf_line_parsing():
    text = "\nSRC: K0RET, DEST: @ALL, TYPE: 0005, NONCE: 00, CRC: 1234\n"
    m = tools.LSF_LINE.search(text)
    assert (m.group(1), m.group(2)) == ("K0RET", "@ALL")


def test_symbols_before_the_first_frame_dont_derail_the_walk():
    # A real radio's carrier carries symbols before its preamble, and random
    # symbols loosely match some sync burst every few thousand symbols. The
    # walk used to start at such a match, find nothing 192 symbols later and
    # stop after one bogus frame (a C62 capture, 2026-11-01).
    rng = np.random.default_rng(5)
    lead = rng.choice([-3, -1, 1, 3], size=6000)
    symbols = np.concatenate([lead, transmission(40)])
    (m,) = phy.measure_m17_capture(capture((symbols, dict(offset_hz=75e3))),
                                   RATE, 75e3)
    assert m.lsf_found and m.lsf_crc_ok and m.source == "K0RET"
    assert m.stream_frames == 40 and m.frame_number_errors == 0
    assert m.eot_found
