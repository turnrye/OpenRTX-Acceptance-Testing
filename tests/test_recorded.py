# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A recorded track goes out as clean FM and is scored against Dire Wolf."""

import numpy as np
from scipy.io import wavfile

from oat import aprs, recorded
from oat.signals import fm_modulate


def frame(source: str, info: str, path=("WIDE1-1",)) -> bytes:
    """An AX.25 UI frame without its FCS, as KISS carries it."""
    return aprs.ui_frame(source, info, list(path))[:-2]


def demodulate(iq: np.ndarray, rate: float) -> np.ndarray:
    """Instantaneous frequency in Hz."""
    return np.angle(iq[1:] * np.conj(iq[:-1])) * rate / (2 * np.pi)


def test_blocks_join_into_the_same_fm_as_one_piece():
    rate = 48000
    t = np.arange(rate) / rate
    samples = np.round(20000 * np.sin(2 * np.pi * 1200 * t)).astype(np.int16)
    blocks = list(recorded.fm_blocks(samples, rate, rate, 3000.0,
                                     offset_hz=250.0, block_s=0.1))
    assert len(blocks) == 10
    whole = fm_modulate(samples / 20000, rate, 3000.0, 250.0)
    assert np.allclose(np.concatenate(blocks), whole, atol=2e-3)


def test_the_track_peak_sets_the_deviation_after_upsampling():
    audio_rate, rate = 44100, 400000
    t = np.arange(audio_rate) / audio_rate
    samples = np.round(8000 * np.sin(2 * np.pi * 1700 * t)).astype(np.int16)
    iq = np.concatenate(list(recorded.fm_blocks(samples, audio_rate, rate,
                                                3000.0, block_s=0.25)))
    assert len(iq) == rate
    dev = demodulate(iq, rate)
    assert abs(np.max(np.abs(dev)) - 3000.0) < 30.0
    # No click where blocks join: the frequency never jumps
    assert np.max(np.abs(np.diff(dev))) < 100.0


def test_a_long_track_is_read_through_a_memory_map(tmp_path):
    path = tmp_path / "track.wav"
    stereo = np.zeros((1000, 2), dtype=np.int16)
    stereo[:, 0] = 1234
    stereo[:, 1] = -4321
    wavfile.write(path, 44100, stereo)
    rate, left = recorded.open_track(path, 0)
    _, right = recorded.open_track(path, 1)
    assert rate == 44100
    assert isinstance(left, np.memmap) or isinstance(left.base, np.memmap)
    assert recorded.track_peak(left) == 1234
    assert recorded.track_peak(right) == 4321


def test_frames_become_tnc2_text_with_or_without_their_fcs():
    full = aprs.ui_frame("N9OAT-7", ">hello", ["WIDE1-1", "N6EX-4*"])
    text = b"N9OAT-7>APZOAT,WIDE1-1,N6EX-4:>hello"
    assert recorded.tnc2(full) == text
    assert recorded.tnc2(full[:-2]) == text
    assert recorded.tnc2(b"not a frame") is None


def test_atest_output_is_read_with_its_times(tmp_path):
    out = tmp_path / "dw.txt"
    out.write_bytes(
        b"44100 samples per second.\n"
        b"\x1b[38;2;0;192;0m\nDECODED[1] 0:07.019 Digipeater N6EX-5 audio "
        b"level = 70(15/14)     \n"
        b"\x1b[38;2;0;192;0m[0] WA6YLB-4>APRS,N6EX-5*:$ULTW0000<0x0d><0x0a>\n"
        b"DECODED[2] 1:08.902 KD6FVP-2 audio level = 92(25/23)     \n"
        b"[0] KD6FVP-2>APS224,N6EX-1,WIDE1:>152343z[224]*We know<0x0d>\n"
        b"\n2 from t1.wav\n")
    ref = recorded.read_reference(out)
    assert ref == [(7.019, b"WA6YLB-4>APRS,N6EX-5:$ULTW0000"),
                   (68.902, b"KD6FVP-2>APS224,N6EX-1,WIDE1:>152343z[224]We know")]


def test_score_counts_repeats_and_what_each_side_alone_heard():
    a = frame("N9OAT", ">one\r")
    b = frame("N9OAT", ">two", path=("N6EX-1*", "WIDE1"))
    c = frame("N9OAT", ">three")
    reference = [recorded.normalize(recorded.tnc2(f)) for f in (a, a, b)]
    result = recorded.score(reference, [a, b + aprs.fcs(b), c, b"junk"])
    assert result["reference"] == 3 and result["heard"] == 4
    assert result["common"] == 2
    assert result["missed"] == 1 and result["extra"] == 1
    assert result["not_ui"] == 1
    assert abs(result["rate"] - 2 / 3) < 1e-9
    assert result["missed_frames"] == ["N9OAT>APZOAT,WIDE1-1:>one"]
