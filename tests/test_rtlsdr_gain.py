# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

from oat.sdr.rtlsdr import manual_gain


def test_zero_gain_is_lowest_manual_gain_not_auto():
    # rtl_sdr -g 0 means automatic gain
    assert manual_gain(0) == 0.1
    assert manual_gain(-3) == 0.1


def test_other_gains_pass_through():
    assert manual_gain(19.7) == 19.7
