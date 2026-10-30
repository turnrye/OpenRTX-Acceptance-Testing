# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Bench and radio descriptions, from YAML files."""

from pathlib import Path

import yaml

from oat.calibration import Calibration

# Headroom kept below a receiver's damage level when a radio transmits into it
SAFETY_MARGIN_DB = 10.0


def load_yaml(path: Path) -> dict:
    """Read a YAML file into a dictionary."""
    with open(path) as f:
        return yaml.safe_load(f) or {}


def check_receiver_safe(radio: dict, cal: Calibration, max_input_dbm: float,
                        freq_hz: float) -> None:
    """
    Refuse a setup where the radio at full power could damage the receiver,
    given the calibrated loss of the radio-to-receiver path at freq_hz.
    """
    loss = cal.loss("to_receiver", freq_hz)
    level = radio["max_tx_power_dbm"] - loss
    limit = max_input_dbm - SAFETY_MARGIN_DB
    if level > limit:
        raise ValueError(
            f"{radio['name']} at full power would put {level:.0f} dBm into "
            f"the receiver through {loss:.1f} dB, above its {limit:.0f} dBm "
            f"safe level; add at least {level - limit:.0f} dB of attenuation "
            f"and recalibrate the to_receiver path")
