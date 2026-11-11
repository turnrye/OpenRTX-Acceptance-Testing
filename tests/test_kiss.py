# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The radio's KISS frames are read back exactly, whatever else is on the port."""

import os
import shutil
import time

import numpy as np
import pytest

from oat import aprs
from oat.kiss import FEND, Deframer, SerialTnc, kiss_encode
from oat.robot_library import OpenRTXAcceptance
from oat.sdr.sim import SimClock, SimTnc


def frame(info: str, path=("WIDE1-1",)) -> bytes:
    """An AX.25 UI frame as KISS carries it: without its FCS."""
    return aprs.ui_frame("N9OAT", info, list(path))[:-2]


def test_escaped_bytes_come_back():
    f = frame(">\xc0\xdb\xc0" * 30)
    assert Deframer().feed(kiss_encode(f)) == [(0, f)]


def test_console_text_between_frames_is_set_aside():
    a, b = frame(">one"), frame(">two")
    d = Deframer()
    data = b"boot ok\r\n" + kiss_encode(a) + b"APRS: rx\r\n" + kiss_encode(b)
    assert d.feed(data) == [(0, a), (0, b)]
    # Both stretches of text end at a FEND
    assert d.junk == 2


def test_a_frame_split_across_reads_is_kept():
    f = frame(">split")
    raw = kiss_encode(f)
    d = Deframer()
    out = []
    for i in range(0, len(raw), 3):
        out += d.feed(raw[i:i + 3])
    assert out == [(0, f)]


def test_bad_escapes_and_other_commands_are_junk():
    d = Deframer()
    assert d.feed(bytes([FEND, 0x00, 0x41, 0xDB, 0x41, FEND])) == []
    assert d.feed(bytes([FEND, 0x01, 0x05, FEND])) == []
    assert d.junk == 2


def test_tnc2_text_matches_dire_wolf():
    f = frame(":N0CALL-7 :hi{01", ("WIDE1*", "WIDE2-1"))
    assert aprs.tnc2(f) == "N9OAT>APZOAT,WIDE1*,WIDE2-1::N0CALL-7 :hi{01"
    # The * goes after the last digipeater that has repeated the frame
    f = frame(">x", ("DIGI1*", "DIGI2*", "WIDE2-1"))
    assert aprs.tnc2(f) == "N9OAT>APZOAT,DIGI1,DIGI2*,WIDE2-1:>x"


def test_back_to_back_frames_share_one_preamble():
    a, b = frame(">a"), frame(">b")
    one = aprs.hdlc_bits(a, 10, 3)
    two = aprs.hdlc_bits([a, b], 10, 3, flags_between=1)
    assert len(two) == len(one) + 8 + len(aprs.hdlc_bits(b, 0, 0))


def test_clock_error_stretches_the_audio():
    tones = np.ones(1200, dtype=np.uint8)
    fast = aprs.afsk(tones, 48000, baud=1200 * 1.01)
    assert len(fast) == pytest.approx(48000 / 1.01, abs=2)


class _Bench:
    """Just enough of the library for the level arithmetic."""

    def __init__(self, vga):
        self.bench = {"transmitter": {"type": "hackrf", "tx_vga_db": vga}}


@pytest.mark.parametrize("level, expected", [
    (0.0, (0.0, 0.0)), (-3.0, (-3.0, 0.0)), (-2.5, (-2.0, 0.5)),
    (-35.0, (-30.0, 5.0)), (10.0, (10.0, 0.0))])
def test_relative_levels_use_gain_steps_then_backoff(level, expected):
    got = OpenRTXAcceptance._relative_level(_Bench(30), level)
    assert got == pytest.approx(expected)


def test_relative_levels_out_of_reach_fail():
    with pytest.raises(AssertionError, match="below"):
        OpenRTXAcceptance._relative_level(_Bench(30), -45.0)
    with pytest.raises(AssertionError, match="above"):
        OpenRTXAcceptance._relative_level(_Bench(30), 20.0)


def test_serial_tnc_reads_frames_from_a_port(tmp_path):
    controller, port = os.openpty()
    log = tmp_path / "tnc.log"
    tnc = SerialTnc(os.ttyname(port), log)
    try:
        start = tnc.now()
        f = frame(">over the port \xc0")
        os.write(controller, b"console\r\n" + kiss_encode(f))
        deadline = time.monotonic() + 2
        while not tnc.heard_since(start) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert [h.frame for h in tnc.heard_since(start)] == [f]
    finally:
        tnc.close()
        os.close(controller)
        os.close(port)
    assert log.read_bytes().endswith(kiss_encode(f))


@pytest.mark.skipif(shutil.which("atest") is None,
                    reason="Dire Wolf (atest) not installed")
def test_sim_tnc_hears_strong_frames_and_loses_weak_ones():
    tnc = SimTnc(SimClock())
    frames = [frame(f">sim {i}") for i in range(3)]
    audio = aprs.packet_audio([aprs.ui_frame("N9OAT", f">sim {i}",
                                             ["WIDE1-1"]) for i in range(3)],
                              48000)
    assert tnc.hear(audio, 48000, frames, 3000.0, -90.0) == 3
    assert [h.frame for h in tnc.heard_since(0.0)] == frames
    assert tnc.hear(audio, 48000, frames, 3000.0, -140.0) == 0
