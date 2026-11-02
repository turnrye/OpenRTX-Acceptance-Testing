# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""M17 frames agree with libm17, and decode back to what was sent."""

import json
from pathlib import Path

import numpy as np
import pytest

from oat.m17 import codec
from oat.m17.codec import Lsf

REFERENCE = json.loads((Path(__file__).parent / "data"
                        / "m17-reference.json").read_text())


@pytest.mark.parametrize("call, raw", REFERENCE["callsigns"].items())
def test_callsigns_encode_as_libm17_does(call, raw):
    assert codec.encode_callsign(call).hex() == raw
    assert codec.decode_callsign(bytes.fromhex(raw)) == call


@pytest.mark.parametrize("data, crc", REFERENCE["crc"].items())
def test_crc_matches_libm17_and_the_specification(data, crc):
    assert codec.crc16(bytes.fromhex(data)) == crc


def test_crc_test_vectors_from_the_specification():
    assert codec.crc16(b"") == 0xFFFF
    assert codec.crc16(b"A") == 0x206E
    assert codec.crc16(b"123456789") == 0x772B


@pytest.mark.parametrize("value, word", REFERENCE["golay"].items())
def test_golay_matches_libm17(value, word):
    assert codec.golay_encode(int(value)) == word


def test_golay_corrects_three_errors():
    word = codec.golay_encode(0xA5C)
    for errors in [0, 1 << 3, 1 << 0 | 1 << 23, 1 << 5 | 1 << 11 | 1 << 17]:
        assert codec.golay_decode(word ^ errors) == (
            0xA5C, bin(errors).count("1"))


def test_interleaver_is_the_specified_permutation():
    seq = codec.INTERLEAVE
    assert list(seq[:8]) == [0, 137, 90, 227, 180, 317, 270, 39]
    assert list(seq[-4:]) == [188, 325, 278, 47]
    assert sorted(seq) == list(range(368))


def test_preamble_and_eot_match_libm17():
    assert list(codec.preamble()) == REFERENCE["preamble_lsf"]
    assert list(codec.eot()) == REFERENCE["eot"]


@pytest.mark.parametrize("ref", REFERENCE["lsf"], ids=lambda r: r["src"])
def test_lsf_frames_match_libm17(ref):
    lsf = Lsf(ref["dst"], ref["src"], ref["type"], bytes.fromhex(ref["meta"]))
    assert lsf.to_bytes().hex() == ref["bytes"]
    assert list(codec.lsf_frame(lsf)) == ref["symbols"]


@pytest.mark.parametrize("ref", REFERENCE["stream"], ids=lambda r: str(r["fn"]))
def test_stream_frames_match_libm17(ref):
    lsf, ok = Lsf.from_bytes(bytes.fromhex(ref["lsf"]))
    assert ok
    symbols = codec.stream_frame(lsf, ref["lich_cnt"], ref["fn"],
                                 bytes.fromhex(ref["payload"]))
    assert list(symbols) == ref["symbols"]


def noisy(symbols, sigma, seed=0):
    rng = np.random.default_rng(seed)
    return symbols + rng.normal(scale=sigma, size=len(symbols))


def test_lsf_decodes_through_noise():
    lsf = Lsf("#OPENRTX", "K0RET/P", codec.TYPE_VOICE_STREAM, b"meta")
    frame = codec.lsf_frame(lsf).astype(float)
    decoded, ok, metric = codec.decode_lsf(noisy(frame[8:], 0.5))
    assert ok and decoded.source == "K0RET/P"
    assert decoded.destination == "#OPENRTX"
    assert decoded.meta.rstrip(b"\0") == b"meta"
    clean = codec.decode_lsf(frame[8:])
    assert clean[1] and clean[2] == 0


def test_inverted_lsf_fails_its_crc():
    frame = codec.lsf_frame(Lsf()).astype(float)
    assert not codec.decode_lsf(-frame[8:])[1]


def test_stream_frames_decode_and_rebuild_the_lsf():
    lsf = Lsf("@ALL", "AB1CDE")
    payloads = [bytes([i] * 16) for i in range(7)]
    symbols = codec.stream_symbols(lsf, payloads, preamble_symbols=384)
    assert len(symbols) == 192 * (2 + 1 + 7 + 1)
    frames = []
    for i in range(7):
        start = 192 * (3 + i)
        assert list(symbols[start:start + 8]) == list(
            codec.sync_symbols(codec.SYNC_STREAM))
        frames.append(codec.decode_stream_frame(
            noisy(symbols[start + 8:start + 192].astype(float), 0.4, i)))
    assert [f.fn for f in frames] == list(range(7))
    assert [f.last for f in frames] == [False] * 6 + [True]
    assert [f.payload for f in frames] == payloads
    rebuilt, ok = codec.lsf_from_lich(frames)
    assert ok and rebuilt.source == "AB1CDE"
