# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""APRS packets are framed as AX.25 says, and an independent decoder reads them."""

import shutil
import subprocess

import numpy as np
import pytest
from scipy.io import wavfile

from oat import aprs
from oat.analysis import demodulated_audio
from oat.signals import fm_modulate


def test_fcs_matches_the_crc16_x25_check_value():
    # The check value of CRC-16/X.25 for "123456789", sent low byte first
    assert aprs.fcs(b"123456789") == bytes([0x6E, 0x90])


def test_addresses_encode_shifted_with_their_ssid():
    assert aprs.encode_address("n0call-7", last=True) == \
        bytes([0x9C, 0x60, 0x86, 0x82, 0x98, 0x98, 0x6F])
    # Destination: command bit set; a repeated digipeater: H bit set
    assert aprs.encode_address("APZOAT", command=True)[-1] == 0xE0
    assert aprs.encode_address("WIDE1*")[-1] == 0xE0 | (0 << 1)
    assert aprs.parse_address("WIDE2-1*") == ("WIDE2", 1, True)


@pytest.mark.parametrize("bad", ["", "TOOLONGCALL", "N0CALL-16", "N0-CALL"])
def test_bad_addresses_are_refused(bad):
    with pytest.raises(ValueError):
        aprs.parse_address(bad)


def test_five_ones_are_followed_by_a_stuffed_zero():
    bits = aprs.hdlc_bits(b"\xff", preamble_flags=0, tail_flags=0)
    assert bits.tolist() == [1, 1, 1, 1, 1, 0, 1, 1, 1]


def test_message_addressee_is_padded_to_nine():
    assert aprs.message_info("N0CALL-7", "hi", "01") == ":N0CALL-7 :hi{01"
    assert aprs.message_info("N0CALL", "hi") == ":N0CALL   :hi"


def test_an_oversized_information_field_is_refused():
    with pytest.raises(ValueError):
        aprs.ui_frame("N9OAT", "x" * (aprs.MAX_INFO_BYTES + 1))


def test_preemphasis_twist_raises_the_space_tone():
    tones = np.array([1] * 12 + [0] * 12, dtype=np.uint8)
    audio = aprs.afsk(tones, 48000, aprs.PREEMPHASIS_TWIST_DB)
    mark, space = audio[:480], audio[480:]
    ratio_db = 20 * np.log10(np.max(np.abs(space)) / np.max(np.abs(mark)))
    assert ratio_db == pytest.approx(aprs.PREEMPHASIS_TWIST_DB, abs=0.1)
    assert np.max(np.abs(audio)) <= 1.0


@pytest.mark.skipif(shutil.which("atest") is None,
                    reason="Dire Wolf (atest) not installed")
@pytest.mark.parametrize("twist_db", [0.0, aprs.PREEMPHASIS_TWIST_DB])
def test_dire_wolf_decodes_our_packets_off_the_air(tmp_path, twist_db):
    # Through the same FM modulation the transmitter uses, demodulated
    rate = 250000
    packets = [
        ("N9OAT", aprs.message_info("N0CALL-7", "hello", "01"), ["WIDE1-1"]),
        ("N9OAT-15", "!4903.50N/07201.75W-test", ["WIDE1*", "WIDE2-1"]),
        ("N9OAT", ">" + "s" * 200, []),
    ]
    iq = np.concatenate(
        [np.concatenate([fm_modulate(aprs.packet_audio(
            aprs.ui_frame(src, info, path), rate, twist_db), rate, 3000.0),
            np.zeros(rate // 4, dtype=np.complex64)])
         for src, info, path in packets])
    audio = demodulated_audio(iq, rate)
    wav = tmp_path / "aprs.wav"
    wavfile.write(wav, 48000, (audio / np.max(np.abs(audio)) * 16000)
                  .astype(np.int16))
    out = subprocess.run(["atest", "-B", "1200", str(wav)],
                         capture_output=True, text=True).stdout
    assert "N9OAT>APZOAT,WIDE1-1::N0CALL-7 :hello{01" in out
    assert "N9OAT-15>APZOAT,WIDE1*,WIDE2-1:!4903.50N/07201.75W-test" in out
    assert "N9OAT>APZOAT:>" + "s" * 200 in out
