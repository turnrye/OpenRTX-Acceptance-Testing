# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The LibreVNA SCPI client, against a fake LibreVNA-GUI on localhost."""

import socketserver
import threading

import numpy as np
import pytest

from oat.calibration import read_touchstone_s21
from oat.sdr.librevna import LibreVNA, LibreVNAError, parse_trace, write_s2p

# Commands the fake accepts; anything else sets the command error bit
COMMANDS = {
    "*RST", "DEVICE:CONNECT", "DEVICE:MODE", "GENERATOR:FREQUENCY",
    "GENERATOR:LVL", "GENERATOR:PORT", "SA:FREQUENCY:CENTER",
    "SA:FREQUENCY:SPAN", "SA:ACQUISITION:RBW", "SA:ACQUISITION:WINDOW",
    "SA:ACQUISITION:DETECTOR", "SA:ACQUISITION:AVG",
    "SA:ACQUISITION:SINGLE", "VNA:FREQUENCY:CENTER", "VNA:FREQUENCY:SPAN",
    "VNA:ACQUISITION:POINTS", "VNA:ACQUISITION:IFBW",
    "VNA:ACQUISITION:AVG", "VNA:ACQUISITION:SINGLE", "VNA:STIMULUS:LVL"}
SA_POINTS = 401
NOISE_DBM = -100.0
S21_DB = -60.0


class FakeLibreVNA(socketserver.ThreadingTCPServer):
    """
    Answers like LibreVNA-GUI's SCPI server, from settings the commands
    left. The SA trace has a carrier wherever signal() says, the VNA a fixed
    S21 of -60 dB.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.log: list[str] = []
        self.state: dict[str, str] = {}
        self.esr = 0
        self.connected = True
        self.calibration = "SOLT"
        self.overload = False
        # Polls of FINished? answering FALSE after a sweep starts; None never
        self.finish_after: int | None = 1
        self._polls = 0
        # (freq Hz, dBm) of a carrier, or None; called for each SA trace
        self.signal = lambda: None
        # Flat level of every SA point, when set
        self.flat_dbm: float | None = None

    @property
    def port(self) -> int:
        return self.server_address[1]

    def commands(self, prefix: str = "") -> list[str]:
        return [c for c in self.log if c.upper().startswith(prefix.upper())]

    def answer(self, line: str) -> str | None:
        self.log.append(line)
        head, _, arg = line.partition(" ")
        node = head.upper()
        if not node.endswith("?"):
            if node not in COMMANDS:
                self.esr |= 0x20
            elif node.endswith(":SINGLE"):
                self._polls = 0
            if node == "DEVICE:CONNECT":
                self.connected = True
            self.state[node] = arg
            return None
        if node == "*IDN?":
            return "LibreVNA,LibreVNA-GUI,fake,v1.6.1"
        if node == "*ESR?":
            esr, self.esr = self.esr, 0
            return str(esr)
        if node == "DEVICE:CONNECT?":
            return "206830535532" if self.connected else "Not connected"
        if node == "DEVICE:STATUS:ADCOVERLOAD?":
            return "TRUE" if self.overload else "FALSE"
        if node.startswith("DEVICE:STATUS:"):
            return "FALSE"
        if node.endswith(":ACQUISITION:FINISHED?"):
            self._polls += 1
            done = (self.finish_after is not None
                    and self._polls > self.finish_after)
            return "TRUE" if done else "FALSE"
        if node == "SA:TRACE:DATA?":
            return self._sa_trace()
        if node == "VNA:TRACE:DATA?":
            return self._vna_trace(arg.upper())
        if node == "VNA:CALIBRATION:ACTIVE?":
            return self.calibration
        if node == "ERRQ?":
            return "ERROR"
        if node == "EMPTY?":
            return ""
        return None  # unknown queries go unanswered, as a hung GUI would

    def _sa_trace(self) -> str:
        center = float(self.state["SA:FREQUENCY:CENTER"])
        span = float(self.state["SA:FREQUENCY:SPAN"])
        freqs = np.linspace(center - span / 2, center + span / 2, SA_POINTS)
        dbm = np.full(SA_POINTS, self.flat_dbm or NOISE_DBM)
        signal = self.signal()
        if signal is not None and abs(signal[0] - center) <= span / 2:
            dbm[np.argmin(np.abs(freqs - signal[0]))] = signal[1]
        return ",".join(f"[{f:.1f},{d:.3f}]" for f, d in zip(freqs, dbm))

    def _vna_trace(self, name: str) -> str:
        center = float(self.state["VNA:FREQUENCY:CENTER"])
        span = float(self.state["VNA:FREQUENCY:SPAN"])
        points = int(self.state["VNA:ACQUISITION:POINTS"])
        freqs = np.linspace(center - span / 2, center + span / 2, points)
        value = (10 ** (S21_DB / 20) * np.exp(-1j * freqs / 1e8)
                 if name in ("S21", "S12") else np.full(points, 0.1 + 0.05j))
        return ",".join(f"[{f:.0f},{v.real:.9g},{v.imag:.9g}]"
                        for f, v in zip(freqs, value))


class Handler(socketserver.StreamRequestHandler):
    disable_nagle_algorithm = True

    def handle(self):
        for raw in self.rfile:
            reply = self.server.answer(raw.decode().strip())
            if reply is not None:
                self.wfile.write(reply.encode() + b"\n")


@pytest.fixture
def fake():
    server = FakeLibreVNA()
    thread = threading.Thread(target=server.serve_forever,
                              args=(0.01,), daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def vna(fake):
    with LibreVNA("127.0.0.1", fake.port, timeout_s=1.0, poll_s=0.01) as v:
        yield v


def test_identifies_and_formats_commands(fake, vna):
    assert vna.idn().startswith("LibreVNA,LibreVNA-GUI")
    vna.generator_cw(446.0e6, -30.5, port=2)
    vna.generator_off()
    assert fake.commands("GEN") == [
        "GENerator:FREQuency 446000000", "GENerator:LVL -30.50",
        "GENerator:PORT 2", "GENerator:PORT 0"]
    assert "DEVice:MODE GEN" in fake.log
    # Every command is checked through the event status register
    assert fake.log.count("*ESR?") == fake.log.count("DEVice:MODE GEN") + 4


def test_refused_command_and_error_replies(vna):
    with pytest.raises(LibreVNAError, match="refused"):
        vna.cmd("BOGUS:NODE 1")
    with pytest.raises(LibreVNAError, match="ERROR"):
        vna.query("ERRQ?")
    with pytest.raises(LibreVNAError, match="empty"):
        vna.query("EMPTY?")
    with pytest.raises(LibreVNAError, match="timed out"):
        vna.query("SILENT?", timeout_s=0.2)


def test_no_server_is_a_clear_error():
    server = FakeLibreVNA()
    port = server.port
    server.server_close()
    with pytest.raises(LibreVNAError, match="no LibreVNA-GUI"):
        LibreVNA("127.0.0.1", port, timeout_s=0.5).connect()


def test_connects_the_device(fake, vna):
    assert vna.connect_device() == "206830535532"
    fake.connected = False
    assert vna.connect_device() == "206830535532"
    assert "DEVice:CONNect" in fake.log


def test_waits_for_the_sweep_to_finish(fake, vna):
    fake.finish_after = 3
    fake.signal = lambda: (446.01e6, -42.0)
    vna.sa_peak(446e6, 100e3, 1e3)
    assert fake.log.count("SA:ACQuisition:FINished?") == 4
    fake.finish_after = None
    with pytest.raises(LibreVNAError, match="didn't finish"):
        vna.wait_finished("SA", timeout_s=0.1)


def test_sa_peak_reads_the_carrier(fake, vna):
    fake.signal = lambda: (446.01e6, -42.0)
    freq, dbm = vna.sa_peak(446e6, 100e3, 1e3)
    assert freq == pytest.approx(446.01e6, abs=300) and dbm == -42.0
    assert "SA:ACQuisition:WINDow FLATTOP" in fake.log
    assert "SA:TRACe:DATA? PORT1" in fake.log
    fake.overload = True
    with pytest.raises(LibreVNAError, match="overload"):
        vna.sa_peak(446e6, 100e3, 1e3)


def test_sa_channel_power_integrates_the_trace(fake, vna):
    # -80 dBm in every 1 kHz is -70 dBm in 10 kHz, less the Hann window's
    # noise bandwidth over its 3 dB bandwidth
    fake.flat_dbm = -80.0
    power = vna.sa_channel_power(446e6, 10e3, 1e3)
    assert power == pytest.approx(-70 - 10 * np.log10(1.5 / 1.44), abs=0.15)
    with pytest.raises(LibreVNAError, match="wider than"):
        vna.sa_channel_power(446e6, 10e3, 10)


def test_trace_parsing():
    f, d = parse_trace("[1e6,-10.5],[2e6,-11]", 2)
    assert list(f) == [1e6, 2e6] and list(d) == [-10.5, -11]
    with pytest.raises(LibreVNAError, match="3 values"):
        parse_trace("[1,2],[3,4]", 3)
    with pytest.raises(LibreVNAError, match="unreadable"):
        parse_trace("NaN,oops", 2)


def test_vna_sweep_sets_centre_and_span(fake, vna):
    freqs, s = vna.vna_sweep(start_hz=100e6, stop_hz=600e6, points=11,
                             ifbw_hz=100, level_dbm=-10)
    assert "VNA:FREQuency:CENTer 350000000" in fake.log
    assert "VNA:FREQuency:SPAN 500000000" in fake.log
    assert "VNA:ACQuisition:IFBW 100" in fake.log
    assert len(freqs) == 11 and freqs[0] == 100e6 and freqs[-1] == 600e6
    assert set(s) == {"S11", "S21", "S12", "S22"}
    assert np.allclose(20 * np.log10(np.abs(s["S21"])), -60)
    with pytest.raises(ValueError):
        vna.vna_sweep(points=11)


def test_s2p_round_trip(fake, vna, tmp_path):
    freqs, s = vna.vna_sweep(350e6, 500e6, points=21)
    path = tmp_path / "chain.s2p"
    write_s2p(path, freqs, s, "to_receiver\nLibreVNA")
    text = path.read_text()
    assert text.startswith("! to_receiver\n! LibreVNA\n# HZ S RI R 50\n")
    loss = read_touchstone_s21(path)
    assert loss.at(446e6) == pytest.approx(60, abs=1e-4)
    assert read_touchstone_s21(path, "s12").at(200e6) == pytest.approx(60)
    assert loss.freqs_hz[0] == 100e6
