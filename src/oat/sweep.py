# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""
The modulation sweep of the HR_C6000's M17 modem: the grid of modulation
pairs around a radio's calibration, and the pick among the measured points.
"""


def modulation_grid(cal_mod1: int, mod1_from: float, mod1_to: float,
                    mod1_steps: int, mod2_fractions: list[float]
                    ) -> list[tuple[int, int]]:
    """
    Modulation pairs to sweep: Mod1 from mod1_from to mod1_to times the
    calibration's Mod1 in mod1_steps steps, for each Mod2 as a fraction of
    the calibration's Mod1. Mod2 is scaled from Mod1 because some radios
    calibrate Mod2 to 0 (the MD-UV3x0); values are clamped to 0-255 and
    repeats dropped, in sweep order.

    @param cal_mod1: the calibration's Mod1.
    @param mod1_from: lowest Mod1, as a fraction of cal_mod1.
    @param mod1_to: highest Mod1, as a fraction of cal_mod1.
    @param mod1_steps: Mod1 values, at least 2.
    @param mod2_fractions: Mod2 values, as fractions of cal_mod1.
    @return (mod1, mod2) pairs, Mod2 in the outer loop.
    """
    steps = max(2, int(mod1_steps))
    span = float(mod1_to) - float(mod1_from)
    mod1s = [round(cal_mod1 * (float(mod1_from) + span * i / (steps - 1)))
             for i in range(steps)]
    pairs = []
    for f in mod2_fractions:
        mod2 = min(255, max(0, round(cal_mod1 * float(f))))
        for mod1 in mod1s:
            pair = (min(255, max(0, mod1)), mod2)
            if pair not in pairs:
                pairs.append(pair)
    return pairs


def pick_modulation(points: list[dict],
                    deviation_limits: tuple[float, float] | None,
                    ratio_limits: tuple[float, float] | None) -> dict | None:
    """
    The point with the lowest EVM among those decoded with their outer
    deviation and deviation ratio inside the limits.

    @param points: measured points (mod1, mod2, evm_percent,
    outer_deviation_hz, deviation_ratio, decoded).
    @param deviation_limits: the outer deviation's acceptance range.
    @param ratio_limits: the deviation ratio's acceptance range.
    @return the point, or None when none qualifies.
    """
    def inside(value, limits):
        return limits is None or (value is not None
                                  and limits[0] <= value <= limits[1])

    good = [p for p in points
            if p.get("decoded") and p.get("evm_percent") is not None
            and inside(p.get("outer_deviation_hz"), deviation_limits)
            and inside(p.get("deviation_ratio"), ratio_limits)]
    return min(good, key=lambda p: p["evm_percent"]) if good else None
