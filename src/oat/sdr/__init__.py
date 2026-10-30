# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""SDR backends, built from the bench configuration."""

from oat.iq import IqFormat
from oat.sdr.base import Capture, Receiver, Transmitter
from oat.sdr.hackrf import HackRFReceiver, HackRFTransmitter
from oat.sdr.rtlsdr import RtlSdrReceiver
from oat.sdr.sim import ReplayReceiver, SimReceiver, SimTransmitter


def make_receiver(cfg: dict) -> Receiver:
    """Receiver from a bench 'receiver' section."""
    kind = cfg["type"]
    if kind == "rtlsdr":
        return RtlSdrReceiver(cfg.get("gain_db", 0.0), cfg.get("device", 0))
    if kind == "hackrf":
        return HackRFReceiver(cfg.get("lna_db", 16), cfg.get("vga_db", 20),
                              cfg.get("amp", False), cfg.get("serial"))
    if kind == "sim":
        return SimReceiver()
    if kind == "replay":
        return ReplayReceiver(cfg["recording"],
                              IqFormat(cfg.get("format", "cu8")))
    raise ValueError(f"unknown receiver type {kind!r}")


def make_transmitter(cfg: dict) -> Transmitter:
    """Transmitter from a bench 'transmitter' section."""
    kind = cfg["type"]
    if kind == "hackrf":
        return HackRFTransmitter(cfg.get("tx_vga_db", 0), cfg.get("amp", False),
                                 cfg.get("serial"))
    if kind == "sim":
        return SimTransmitter()
    raise ValueError(f"unknown transmitter type {kind!r}")


__all__ = ["Capture", "Receiver", "Transmitter", "make_receiver",
           "make_transmitter"]
