# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Audio test signals and the measurements made on them."""

import struct
import termios

import numpy as np
import pytest

from oat import audio

RATE = audio.RATE


def test_tone_level_reads_back():
    x = audio.tone(1000, 1.0, -20)
    assert audio.rms_dbfs(x) == pytest.approx(-20, abs=0.01)
    assert audio.tone_amplitude(x, RATE, 1000) == pytest.approx(0.1, rel=0.002)


@pytest.mark.parametrize("noise_db", [-12, -20, -40])
def test_sinad_of_a_tone_in_noise(noise_db):
    rng = np.random.default_rng(0)
    signal_ = audio.tone(1000, 2.0, -10)
    noise = rng.normal(size=len(signal_))
    # Noise power noise_db below the tone, within the measurement band
    from scipy import signal
    sos = signal.butter(8, audio.BAND_HZ, "bandpass", fs=RATE, output="sos")
    noise = signal.sosfiltfilt(sos, noise)
    noise *= np.sqrt(np.mean(signal_ ** 2) * 10 ** (noise_db / 10)
                     / np.mean(noise ** 2))
    d = audio.distortion(signal_ + noise, RATE)
    expected = 10 * np.log10(1 + 10 ** (-noise_db / 10))
    assert d.sinad_db == pytest.approx(expected, abs=0.3)
    assert d.tone_hz == pytest.approx(1000, abs=2)


def test_thd_of_a_known_harmonic():
    x = audio.tone(1000, 1.0, -10) + audio.tone(3000, 1.0, -30)
    d = audio.distortion(x, RATE)
    assert d.thd_n_percent == pytest.approx(10, rel=0.02)
    assert d.sinad_db == pytest.approx(20.04, abs=0.1)


def test_psophometric_weighting_shapes_the_noise():
    # Hum at 100 Hz weighs 42 dB less than at 1 kHz
    x = audio.tone(1000, 1.0, -10) + audio.tone(100, 1.0, -30)
    flat = audio.distortion(x, RATE, 1000)
    weighted = audio.distortion(x, RATE, 1000, "psophometric")
    assert weighted.sinad_db - flat.sinad_db == pytest.approx(42, abs=1)
    with pytest.raises(ValueError):
        audio.distortion(x, RATE, 1000, "c-message-ish")


def test_response_of_a_tone_sweep_with_pre_emphasis():
    freqs = [300, 500, 1000, 2000, 3000]
    parts = []
    for f in freqs:
        # +6 dB per octave, as pre-emphasis
        level = -20 + 6 * np.log2(f / 1000)
        parts += [audio.tone(f, 0.8, level), np.zeros(int(0.3 * RATE))]
    x = np.concatenate([np.zeros(RATE // 2)] + parts)
    levels = audio.response(audio.tone_segments(x, RATE))
    assert [round(s.tone_hz) for s in levels] == freqs
    errors = audio.response_errors(levels, 6.0)
    assert max(abs(e) for e in errors) < 0.1


def test_tone_sequence_holds_its_tones():
    x = audio.tone_sequence([400, 1000, 2500], 0.5, 0.2, -12)
    found = audio.tone_segments(x, RATE)
    assert [round(s.tone_hz, -1) for s in found] == [400, 1000, 2500]
    assert all(s.amplitude == pytest.approx(10 ** (-12 / 20), rel=0.02)
               for s in found)


def test_a_dropout_is_found():
    x = audio.tone(1000, 2.0, -10)
    assert audio.longest_gap(x, RATE) < 0.01
    x[RATE // 2:RATE // 2 + int(0.12 * RATE)] *= 0.001
    assert audio.longest_gap(x, RATE) == pytest.approx(0.12, abs=0.01)


def test_images_relative_to_the_tone():
    x = audio.tone(1000, 1.0, -10) + audio.tone(7000, 1.0, -40)
    lv = audio.images(x, RATE, 1000, [7000, 9000])
    assert lv[7000] == pytest.approx(-30, abs=0.1)
    assert lv[9000] < -80


def test_speech_like_is_repeatable_and_at_its_level():
    a = audio.speech_like(3.0, level_dbfs=-24)
    assert np.array_equal(a, audio.speech_like(3.0, level_dbfs=-24))
    assert audio.rms_dbfs(a) == pytest.approx(-24, abs=0.01)


def test_sim_audio_loops_back_what_it_plays(tmp_path):
    sim = audio.SimAudio()
    path = audio.write_wav(tmp_path / "t.wav", audio.tone(1000, 0.5, -6))
    sim.play(path, repeat=True)
    back, rate = audio.read_wav(sim.record(tmp_path / "r.wav", 2.0))
    assert len(back) == 2 * RATE
    assert audio.rms_dbfs(back) == pytest.approx(-6, abs=0.05)


def test_audio_config():
    assert audio.make_audio(None) is None
    assert isinstance(audio.make_audio({"type": "sim"}), audio.SimAudio)
    assert audio.make_ptt({"type": "sim"}) is None
    with pytest.raises(ValueError):
        audio.make_audio({"type": "pulse"})


class FakeFcntl:
    """Records modem line changes instead of making them."""

    def __init__(self):
        self.calls = []

    def ioctl(self, fd, request, data):
        self.calls.append((request, struct.unpack("I", data)[0]))


def serial_ptt(monkeypatch, line, active_high=True):
    monkeypatch.setattr(audio.os, "access", lambda *a: True)
    monkeypatch.setattr(audio.os, "open", lambda *a: 7)
    monkeypatch.setattr(audio.os, "close", lambda fd: None)
    monkeypatch.setattr(audio.time, "sleep", lambda s: None)
    ptt = audio.SerialPtt("/dev/fake", line, active_high)
    ptt._fcntl = FakeFcntl()
    return ptt, ptt._fcntl.calls


def test_serial_ptt_keys_on_rts_with_dtr_held_low(monkeypatch):
    ptt, calls = serial_ptt(monkeypatch, "rts")
    ptt.key()
    assert calls == [(termios.TIOCMBIC, termios.TIOCM_DTR),
                     (termios.TIOCMBIC, termios.TIOCM_RTS),
                     (termios.TIOCMBIS, termios.TIOCM_RTS)]
    calls.clear()
    ptt.unkey()
    assert calls == [(termios.TIOCMBIC, termios.TIOCM_RTS)]


def test_serial_ptt_keys_on_dtr_with_rts_held_low(monkeypatch):
    ptt, calls = serial_ptt(monkeypatch, "dtr")
    ptt.key()
    assert calls[0] == (termios.TIOCMBIC, termios.TIOCM_RTS)
    assert calls[-1] == (termios.TIOCMBIS, termios.TIOCM_DTR)


def test_serial_ptt_active_low_rests_high(monkeypatch):
    ptt, calls = serial_ptt(monkeypatch, "rts", active_high=False)
    ptt.key()
    assert calls == [(termios.TIOCMBIC, termios.TIOCM_DTR),
                     (termios.TIOCMBIS, termios.TIOCM_RTS),
                     (termios.TIOCMBIC, termios.TIOCM_RTS)]


def test_serial_ptt_close_releases_before_closing(monkeypatch):
    ptt, calls = serial_ptt(monkeypatch, "rts")
    ptt.close()
    assert calls == []
    ptt.key()
    calls.clear()
    ptt.close()
    assert calls == [(termios.TIOCMBIC, termios.TIOCM_RTS)]


def test_serial_ptt_refuses_a_missing_port_before_the_run(monkeypatch):
    opened = []
    monkeypatch.setattr(audio.os, "open", lambda *a: opened.append(a))
    with pytest.raises(ValueError, match="/dev/nonexistent-ptt"):
        audio.make_ptt({"ptt": {"device": "/dev/nonexistent-ptt",
                                "line": "rts"}})
    assert opened == []


def test_serial_ptt_unkey_leaves_an_unopened_port_alone(monkeypatch):
    ptt, calls = serial_ptt(monkeypatch, "rts")
    ptt.unkey()
    assert calls == [] and ptt._fd is None


def test_serial_ptt_reopens_a_port_that_dropped_off_usb(monkeypatch):
    ptt, calls = serial_ptt(monkeypatch, "rts")
    ptt.key()
    gone = ptt._fcntl.ioctl
    def fail_once(fd, request, data):
        ptt._fcntl.ioctl = gone
        raise OSError(6, "Device not configured")
    ptt._fcntl.ioctl = fail_once
    calls.clear()
    ptt.key()
    assert calls == [(termios.TIOCMBIC, termios.TIOCM_DTR),
                     (termios.TIOCMBIC, termios.TIOCM_RTS),
                     (termios.TIOCMBIS, termios.TIOCM_RTS)]
