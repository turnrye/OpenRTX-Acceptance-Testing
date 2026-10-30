# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Baseband test signals."""

import numpy as np

# Below full scale, so the 8-bit converters never clip
IQ_AMPLITUDE = 0.9


def tone(freq_hz: float, duration_s: float, rate: float) -> np.ndarray:
    """Unit-amplitude sine tone."""
    t = np.arange(int(duration_s * rate)) / rate
    return np.sin(2 * np.pi * freq_hz * t)


def fm_modulate(audio: np.ndarray, rate: float, deviation_hz: float,
                offset_hz: float = 0.0) -> np.ndarray:
    """
    Frequency-modulate audio onto a carrier at offset_hz from the centre.

    Audio at +-1 deviates the carrier by +-deviation_hz.
    """
    inst_hz = offset_hz + deviation_hz * audio
    phase = 2 * np.pi * np.cumsum(inst_hz) / rate
    return (IQ_AMPLITUDE * np.exp(1j * phase)).astype(np.complex64)


def fm_tone(tone_hz: float, deviation_hz: float, duration_s: float,
            rate: float, offset_hz: float = 0.0, ctcss_hz: float = 0.0,
            ctcss_deviation_hz: float = 0.0) -> np.ndarray:
    """FM carrier modulated by a tone, plus an optional CTCSS tone."""
    inst = deviation_hz * tone(tone_hz, duration_s, rate)
    if ctcss_hz > 0:
        inst = inst + ctcss_deviation_hz * tone(ctcss_hz, duration_s, rate)
    return fm_modulate(inst, rate, 1.0, offset_hz)


def silence(duration_s: float, rate: float) -> np.ndarray:
    """No carrier, for gaps between transmissions."""
    return np.zeros(int(duration_s * rate), dtype=np.complex64)


def fm_carriers(carriers: list[dict], duration_s: float,
                rate: float) -> np.ndarray:
    """
    Several FM carriers at once, for receiver tests with an interferer. Each
    carrier is a dict of fm_tone()'s arguments plus level_db, relative to the
    strongest, and optionally on: False. Amplitudes scale so that the carriers'
    peaks add up to IQ_AMPLITUDE, counting carriers that are off: switching
    one off leaves the others at the same level.
    """
    gains = [10 ** (c.get("level_db", 0.0) / 20) for c in carriers]
    scale = IQ_AMPLITUDE / sum(gains)
    total = np.zeros(int(duration_s * rate), dtype=np.complex128)
    for c, gain in zip(carriers, gains):
        if not c.get("on", True):
            continue
        args = {k: v for k, v in c.items() if k not in ("level_db", "on")}
        tone_iq = fm_tone(duration_s=duration_s, rate=rate, **args)
        total += (scale * gain / IQ_AMPLITUDE) * tone_iq
    return total.astype(np.complex64)
