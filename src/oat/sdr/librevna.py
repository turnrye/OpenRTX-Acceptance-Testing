# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
LibreVNA through the SCPI server of LibreVNA-GUI, for calibrating a bench:
VNA sweeps of the RF paths, its spectrum analyser for levels and its
generator as a known carrier.

The GUI must be running with its SCPI server on (Preferences, General, or
SCPIServer.enabled in LibreVNA-GUI.conf); it can run without a window as
`LibreVNA-GUI --no-gui --port 19542`. Commands are lines of ASCII; a query
ends in '?' and gets one line back.

The command names were found in the v1.6.1 GUI. Argument and reply formats
come from the LibreVNA ProgrammingGuide as remembered, not from hardware;
each such place is marked UNVERIFIED with what to check.
"""

import socket
import time
from pathlib import Path

import numpy as np

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 19542
# S-parameters in the column order of two-port Touchstone files
S_PARAMS = ("S11", "S21", "S12", "S22")
# *ESR? bits: query, device dependent, execution and command errors
ESR_ERRORS = 0x04 | 0x08 | 0x10 | 0x20
# Equivalent noise bandwidth over 3 dB bandwidth for each SA window
# (Harris, 1978). The integration below needs the noise bandwidth of a trace
# point; LibreVNA states RBW, taken here as the 3 dB bandwidth. The ratios are
# all within 0.6 dB of one.
# UNVERIFIED: whether LibreVNA's RBW is the window's 3 dB or its noise
# bandwidth (SpectrumAnalyzer source, or measure a noise source's level
# against RBW).
ENBW_PER_RBW = {"NONE": 1.00 / 0.89, "HANN": 1.50 / 1.44,
                "KAISER": 1.80 / 1.71, "FLATTOP": 3.77 / 3.72}


class LibreVNAError(RuntimeError):
    """The GUI refused a command, or answered nothing or an error."""


class LibreVNA:
    """A connection to LibreVNA-GUI's SCPI server."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout_s: float = 5.0, check_commands: bool = True,
                 poll_s: float = 0.1):
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        # Ask *ESR? after each command, which also waits for it to finish
        self.check_commands = check_commands
        self.poll_s = poll_s
        self._sock: socket.socket | None = None
        self._buffer = b""

    def connect(self) -> "LibreVNA":
        """Open the TCP connection to the GUI."""
        try:
            self._sock = socket.create_connection((self.host, self.port),
                                                  timeout=self.timeout_s)
        except OSError as e:
            raise LibreVNAError(
                f"no LibreVNA-GUI SCPI server at {self.host}:{self.port} "
                f"({e}): start the GUI with its SCPI server on") from e
        # A command and the *ESR? after it are two small writes; without
        # this, each waits out the peer's delayed acknowledgement
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._buffer = b""
        return self

    def close(self) -> None:
        """Close the connection; the GUI keeps running."""
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> "LibreVNA":
        return self.connect() if self._sock is None else self

    def __exit__(self, *exc) -> None:
        self.close()

    def _send(self, line: str) -> None:
        if self._sock is None:
            raise LibreVNAError("not connected to LibreVNA-GUI")
        self._sock.sendall(line.encode("ascii") + b"\n")

    def _readline(self, timeout_s: float) -> str:
        deadline = time.monotonic() + timeout_s
        while b"\n" not in self._buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                raise LibreVNAError("timed out waiting for a reply")
            self._sock.settimeout(left)
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                raise LibreVNAError("timed out waiting for a reply") from None
            if not chunk:
                raise LibreVNAError("LibreVNA-GUI closed the connection")
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line.decode("ascii", "replace").strip()

    def query(self, text: str, timeout_s: float | None = None) -> str:
        """Send a query and return its reply line."""
        self._send(text)
        try:
            reply = self._readline(timeout_s or self.timeout_s)
        except LibreVNAError as e:
            raise LibreVNAError(f"{text}: {e}") from None
        # UNVERIFIED: a failed query is expected to answer ERROR (older
        # GUIs) or nothing, with the *ESR? query error bit set (1.5 on)
        if not reply or reply.upper() == "ERROR":
            raise LibreVNAError(f"{text}: {reply or 'empty reply'}")
        return reply

    def cmd(self, text: str, check: bool | None = None) -> None:
        """
        Send a command. Commands answer nothing, so errors are read from the
        event status register afterwards.
        """
        self._send(text)
        if self.check_commands if check is None else check:
            # UNVERIFIED: *ESR? (present in the 1.6.1 binary) returning the
            # register as a decimal integer, with the IEEE 488.2 bits
            esr = int(float(self.query("*ESR?")))
            if esr & ESR_ERRORS:
                raise LibreVNAError(f"{text}: refused (*ESR? {esr})")

    def idn(self) -> str:
        """The GUI's identification, manufacturer to version."""
        return self.query("*IDN?")

    def connect_device(self, serial: str | None = None) -> str:
        """Have the GUI connect to a LibreVNA; returns its serial number."""
        # UNVERIFIED: DEVice:CONNect? answering the serial, or "Not
        # connected" (that string is in the binary), and DEVice:CONNect
        # without an argument taking the first device found
        current = self.query("DEVice:CONNect?")
        if current.lower() != "not connected" and serial in (None, current):
            return current
        self.cmd(f"DEVice:CONNect {serial}" if serial else "DEVice:CONNect",
                 check=False)
        current = self.query("DEVice:CONNect?", timeout_s=10.0)
        if current.lower() == "not connected":
            raise LibreVNAError("LibreVNA-GUI found no LibreVNA: plug it in")
        return current

    def status(self) -> dict[str, bool]:
        """The device's unlocked, ADC overload and unlevelled flags."""
        # UNVERIFIED: these queries answering TRUE or FALSE
        return {name: self.query(f"DEVice:STAtus:{node}?").upper() == "TRUE"
                for name, node in (("unlocked", "UNLOcked"),
                                   ("overload", "ADCOVERload"),
                                   ("unlevelled", "UNLEVel"))}

    def check_status(self) -> None:
        """Fail when the device reports a fault that spoils a reading."""
        faults = [name for name, on in self.status().items() if on]
        if faults:
            raise LibreVNAError(f"LibreVNA reports {', '.join(faults)}")

    def mode(self, mode: str) -> None:
        """Switch the GUI to VNA, GEN or SA."""
        self.cmd(f"DEVice:MODE {mode}")

    def wait_finished(self, prefix: str, timeout_s: float = 30.0) -> None:
        """Poll <prefix>:ACQuisition:FINished? until a sweep completes."""
        deadline = time.monotonic() + timeout_s
        while True:
            # UNVERIFIED: FINished? answering TRUE or FALSE, and turning
            # FALSE as soon as a setting or SINGLE TRUE restarts the sweep
            if self.query(f"{prefix}:ACQuisition:FINished?").upper() == "TRUE":
                return
            if time.monotonic() > deadline:
                raise LibreVNAError(f"{prefix} sweep didn't finish within "
                                    f"{timeout_s:g} s")
            time.sleep(self.poll_s)

    # Generator

    def generator_cw(self, freq_hz: float, dbm: float, port: int = 1) -> None:
        """A continuous carrier from a port."""
        self.mode("GEN")
        # UNVERIFIED: GENerator:FREQuency in Hz and GENerator:LVL in dBm,
        # both as plain numbers
        self.cmd(f"GENerator:FREQuency {int(round(freq_hz))}")
        self.cmd(f"GENerator:LVL {dbm:.2f}")
        self.cmd(f"GENerator:PORT {port}")

    def generator_off(self) -> None:
        """Stop the generator."""
        # UNVERIFIED: port 0 meaning off
        self.cmd("GENerator:PORT 0")

    # Spectrum analyser

    def sa_trace(self, center_hz: float, span_hz: float, rbw_hz: float,
                 window: str = "FLATTOP", detector: str = "+PEAK",
                 averages: int = 1, port: int = 1,
                 timeout_s: float = 30.0) -> tuple[np.ndarray, np.ndarray]:
        """One spectrum analyser sweep: frequencies in Hz, levels in dBm."""
        self.mode("SA")
        self.cmd(f"SA:FREQuency:CENTer {int(round(center_hz))}")
        self.cmd(f"SA:FREQuency:SPAN {int(round(span_hz))}")
        self.cmd(f"SA:ACQuisition:RBW {int(round(rbw_hz))}")
        self.cmd(f"SA:ACQuisition:WINDow {window}")
        self.cmd(f"SA:ACQuisition:DETector {detector}")
        self.cmd(f"SA:ACQuisition:AVG {averages}")
        # UNVERIFIED: SINGLE TRUE starting a fresh sweep that then stops
        self.cmd("SA:ACQuisition:SINGLE TRUE")
        self.wait_finished("SA", timeout_s)
        reply = self.query(f"SA:TRACe:DATA? PORT{port}")
        freqs, dbm = parse_trace(reply, 2)
        return freqs, dbm

    def sa_peak(self, center_hz: float, span_hz: float,
                rbw_hz: float) -> tuple[float, float]:
        """
        The strongest signal in a span: its frequency in Hz and level in
        dBm. The flat-top window reads a carrier's level within a few
        hundredths of a dB wherever it falls between points.
        """
        freqs, dbm = self.sa_trace(center_hz, span_hz, rbw_hz, "FLATTOP",
                                   "+PEAK")
        self._check_overload()
        i = int(np.argmax(dbm))
        return float(freqs[i]), float(dbm[i])

    def sa_channel_power(self, center_hz: float, bandwidth_hz: float,
                         rbw_hz: float, window: str = "HANN") -> float:
        """
        Total power in a channel, in dBm. Each trace point is the power in
        one resolution filter, so summing the points over the channel counts
        every part of the spectrum (point spacing / noise bandwidth) times
        over; scaling by that ratio gives the channel's power. The average
        detector keeps noise-like signals from reading high, and the spacing
        must stay below the RBW or narrow signals between points are lost.
        """
        freqs, dbm = self.sa_trace(center_hz, 1.5 * bandwidth_hz, rbw_hz,
                                   window, "AVERAGE")
        self._check_overload()
        inside = np.abs(freqs - center_hz) <= bandwidth_hz / 2
        if inside.sum() < 2:
            raise LibreVNAError("too few trace points in the channel: "
                                "lower the RBW")
        spacing = float(np.median(np.diff(freqs)))
        if spacing > rbw_hz:
            raise LibreVNAError(f"trace points {spacing:g} Hz apart are "
                                f"wider than the {rbw_hz:g} Hz RBW")
        enbw = rbw_hz * ENBW_PER_RBW[window.upper()]
        total_mw = np.sum(10 ** (dbm[inside] / 10)) * spacing / enbw
        return float(10 * np.log10(total_mw))

    def _check_overload(self) -> None:
        if self.status()["overload"]:
            raise LibreVNAError("LibreVNA ADC overload: add attenuation")

    # VNA

    def vna_sweep(self, center_hz: float | None = None,
                  span_hz: float | None = None, *,
                  start_hz: float | None = None, stop_hz: float | None = None,
                  points: int = 501, ifbw_hz: float = 1000,
                  level_dbm: float = -10, timeout_s: float = 60.0
                  ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        """
        One sweep with the GUI's active calibration: frequencies in Hz and
        the four complex S-parameters. Give a centre and span, or a start
        and stop; the GUI has no STARt command, so either becomes a centre
        and span.
        """
        if start_hz is not None and stop_hz is not None:
            center_hz = (start_hz + stop_hz) / 2
            span_hz = stop_hz - start_hz
        if center_hz is None or span_hz is None:
            raise ValueError("give center_hz and span_hz, or start_hz and "
                             "stop_hz")
        self.mode("VNA")
        self.cmd(f"VNA:FREQuency:CENTer {int(round(center_hz))}")
        self.cmd(f"VNA:FREQuency:SPAN {int(round(span_hz))}")
        self.cmd(f"VNA:ACQuisition:POINTS {int(points)}")
        self.cmd(f"VNA:ACQuisition:IFBW {int(round(ifbw_hz))}")
        self.cmd(f"VNA:STIMulus:LVL {level_dbm:.2f}")
        self.cmd("VNA:ACQuisition:AVG 1")
        self.cmd("VNA:ACQuisition:SINGLE TRUE")
        self.wait_finished("VNA", timeout_s)
        freqs = None
        s = {}
        for name in S_PARAMS:
            # UNVERIFIED: trace names S11..S22 (the GUI's default traces;
            # VNA:TRACe:LIST? shows them) and DATA? replying
            # [freq,real,imag],... in linear units
            f, re, im = parse_trace(self.query(f"VNA:TRACe:DATA? {name}"), 3)
            if freqs is None:
                freqs = f
            elif len(f) != len(freqs) or not np.allclose(f, freqs):
                raise LibreVNAError(f"{name} has different frequencies from "
                                    f"the other traces")
            s[name] = re + 1j * im
        return freqs, s

    def calibration_active(self) -> str:
        """The type of the VNA calibration in use, as the GUI names it."""
        # UNVERIFIED: the node is spelled ACTIVE in the 1.6.1 binary (not
        # ACTive, which belongs to de-embedding); check its reply for no
        # calibration (expected "NONE" or empty)
        return self.query("VNA:CALibration:ACTIVE?")

    def load_calibration(self, path: Path) -> None:
        """Load a calibration file saved from the GUI."""
        # UNVERIFIED: LOAD? taking a path on the GUI's machine and
        # answering TRUE on success
        if self.query(f"VNA:CALibration:LOAD? {path}").upper() != "TRUE":
            raise LibreVNAError(f"LibreVNA-GUI couldn't load {path}")


def parse_trace(reply: str, width: int) -> tuple[np.ndarray, ...]:
    """
    Trace data as the GUI sends it, [a,b],[a,b],... (or with three values a
    point), as one array per column.
    """
    # UNVERIFIED: the bracketed tuple format, as the ProgrammingGuide's
    # Python examples parse it
    text = reply.replace("[", " ").replace("]", " ").replace(",", " ")
    try:
        values = np.array([float(v) for v in text.split()])
    except ValueError:
        raise LibreVNAError(f"unreadable trace data: {reply[:80]}") from None
    if not len(values) or len(values) % width:
        raise LibreVNAError(f"trace data isn't {width} values a point")
    rows = values.reshape(-1, width)
    return tuple(rows[:, i] for i in range(width))


def write_s2p(path: Path, freqs: np.ndarray, s: dict[str, np.ndarray],
              comment: str = "") -> None:
    """A two-port Touchstone file, real and imaginary, 50 ohms."""
    lines = [f"! {line}" for line in comment.splitlines()]
    lines.append("# HZ S RI R 50")
    for i, f in enumerate(freqs):
        values = []
        for name in S_PARAMS:
            values += [s[name][i].real, s[name][i].imag]
        lines.append(f"{f:.0f} " + " ".join(f"{v:.9g}" for v in values))
    Path(path).write_text("\n".join(lines) + "\n")
