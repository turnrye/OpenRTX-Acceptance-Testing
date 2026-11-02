# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Robot Framework keywords for acceptance tests on a bench."""

import datetime
import json
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
from robot.api import logger
from robot.api.deco import keyword, library
from robot.libraries.BuiltIn import BuiltIn
from scipy.io import wavfile

from oat.analysis import measure_capture
from oat.calibration import load_calibration
from oat.config import check_receiver_safe, load_yaml
from oat.iq import clipped_fraction, write_iq
from oat.sanitize import scrub
from oat.operator import make_operator
from oat.sdr import make_receiver, make_transmitter
from oat.sdr.sim import SimBurst, SimReceiver, SimTransmitter, link_loopback
from oat.m17 import codec as m17_codec
from oat.m17 import phy as m17_phy
from oat.m17 import tools as m17_tools
from oat.m17.phy import measure_m17_capture
from oat.signals import fm_carriers, fm_tone, silence, tone

# Length of the looped transmitter signal; whole cycles of 1 kHz and CTCSS
TX_LOOP_S = 2.0
# Appended to every capture instruction: transmissions are found against the
# noise between them, and stopping the receiver loses its last moments
CAPTURE_GUIDANCE = ("Leave a second of silence before keying up and after the "
                    "last release, then press Enter.")
# Fraction of a capture's samples at full scale above which its levels read
# low: the receiver needs more attenuation
MAX_CLIPPED = 0.001
# Version of the measurements.json layout
RESULTS_SCHEMA = 1
# Most the transmitter may scale its samples below full scale to reach a
# level under its lowest gain setting; more costs the 8-bit converter
# too much of its resolution
MAX_DIGITAL_BACKOFF_DB = 10.0


def chain_direction(tags) -> str | None:
    """Which way a test needs the attenuator chain, from its tx/rx tag."""
    if "tx" in tags:
        return "tx"
    if "rx" in tags:
        return "rx"
    return None


def receiver_clipped(capture) -> float:
    """Fraction of a capture's samples at the converter's full scale."""
    sizes = [p.stat().st_size for p in capture.paths]
    if not sum(sizes):
        return 0.0
    return sum(clipped_fraction(p, capture.fmt) * n
               for p, n in zip(capture.paths, sizes)) / sum(sizes)


def voice_levels(pcm: np.ndarray) -> dict:
    """Peak and near-full-scale fraction of decoded 16-bit voice."""
    x = np.abs(np.asarray(pcm, dtype=float))
    if not len(x):
        return {"voice_peak_dbfs": float("-inf"), "voice_clipped": 0.0}
    return {"voice_peak_dbfs": float(20 * np.log10(max(x.max(), 1) / 32767)),
            "voice_rms_dbfs": float(10 * np.log10(
                max(np.mean(x ** 2), 1) / 32767 ** 2)),
            # Within 0.1 dB of full scale
            "voice_clipped": float(np.mean(x >= 32767 * 10 ** (-0.1 / 20)))}


def suite_commit() -> str:
    """This repository's commit, with -dirty for uncommitted changes."""
    repo = Path(__file__).resolve().parents[2]
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "describe", "--always", "--dirty",
             "--abbrev=10"], capture_output=True, text=True,
            check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


@library(scope="SUITE", auto_keywords=False)
class OpenRTXAcceptance:
    """
    Keywords to drive a bench: SDRs that transmit into and capture from the
    radio under test, and an operator for what can't be automated yet.

    The library also listens to the run, to know which tests it will execute
    after oat.expand and tag selection; Open Bench shows that plan.
    """

    ROBOT_LISTENER_API_VERSION = 3

    def __init__(self):
        self.ROBOT_LIBRARY_LISTENER = self
        self.planned = []
        self.band = None
        self.frequency_hz = None
        self._remembered = {}
        self.bench = None
        self.calibration = None
        self.radio = None
        self.radio_file = None
        self.firmware = None
        self.hardware = None
        self.dsp = None
        self.suite = None
        self.started = None
        self.output_dir = None
        self.receiver = None
        self.transmitter = None
        self.operator = None
        self.run_dir = None
        self.records = []
        self._captures = 0
        # A capture from Start Capture, until it's stopped
        self._open_capture = None
        # Which way the attenuator chain faces: "tx" from the radio to the
        # receiver, "rx" from the transmitter to the radio
        self._chain = None

    # Bench --------------------------------------------------------------

    @keyword
    def open_bench(self, bench: str, calibration: str, radio: str,
                   firmware: str, hardware: str, dsp: str = ""):
        """
        Load the bench, its calibration and the radio file, and prepare the
        instruments. All five are mandatory and differ from tester to tester,
        so suites take them from the command line (-v BENCH:... and so on).
        `firmware` names what the radio runs, such as a git describe;
        `hardware` names the unit, by model and serial number, since units of
        one model measure differently. `dsp` names the DSP image the radio's
        audio processor runs, such as its sha256; it is mandatory for radio
        files with dsp_image: true, whose firmware states can differ in it
        alone.
        """
        missing = [name for name, value in (
            ("BENCH", bench), ("CALIBRATION", calibration), ("RADIO", radio),
            ("FIRMWARE", firmware), ("HARDWARE", hardware)) if not value]
        if not missing and load_yaml(Path(radio)).get("dsp_image") \
                and not dsp:
            missing.append("DSP")
        if missing:
            raise ValueError(
                "Pass " + " ".join(f"-v {m}:..." for m in missing)
                + "; see bench/example.yaml and calibration/example.yaml")
        self.bench = load_yaml(Path(bench))
        self.dsp = str(dsp) if dsp else None
        self.calibration = load_calibration(Path(calibration))
        self.radio = load_yaml(Path(radio))
        self.radio_file = Path(radio)
        self.firmware = str(firmware)
        self.hardware = str(hardware)
        if self.calibration.expired():
            logger.warn(f"Calibration {calibration} expired on "
                        f"{self.calibration.expires}")
        self.receiver = make_receiver(self.bench["receiver"])
        self.transmitter = make_transmitter(self.bench["transmitter"])
        if isinstance(self.receiver, SimReceiver) and isinstance(
                self.transmitter, SimTransmitter):
            link_loopback(self.receiver, self.transmitter)
        self.operator = make_operator(self.bench.get("operator", {}))
        for band in self.radio["bands"].values():
            check_receiver_safe(self.radio, self.calibration,
                                self.receiver.max_input_dbm,
                                band["frequency_hz"])
        # Next to Robot's log, so the log can link captures and audio
        out = Path(BuiltIn().get_variable_value("${OUTPUT DIR}", "."))
        suite = BuiltIn().get_variable_value("${SUITE NAME}", "suite")
        self.suite = suite
        self.output_dir = out
        self.run_dir = out / f"bench-{suite.replace(' ', '_')}"
        # Start empty, so files from an earlier run can't be mistaken for these
        if self.run_dir.exists():
            shutil.rmtree(self.run_dir)
        self.run_dir.mkdir(parents=True)
        self.started = datetime.datetime.now(datetime.timezone.utc)
        logger.info(f"Results in {self.run_dir}")
        # Shown in Robot's report, and filterable in robotframework-dashboard
        cal = self.calibration.summary()
        for name, value in (
                ("Firmware", self.firmware),
                ("Radio", f"{self.radio['name']} ({self.radio_file.stem})"),
                ("Hardware", self.hardware),
                *((("DSP", self.dsp),) if self.dsp else ()),
                ("Station", self.bench.get("station", "unnamed")),
                ("Calibration", f"{cal['method']}, measured "
                                f"{cal['measured']} ({cal['sha256']})"),
                ("Suite commit", suite_commit())):
            BuiltIn().set_suite_metadata(name, value, top=True)
        self.operator.instruct(self._plan_summary())

    def start_suite(self, data, result):
        """Listener: note the tests this run will execute."""
        self.planned = [(t.name, set(t.tags)) for t in data.tests]

    def start_test(self, data, result):
        """
        Listener: when the tests change direction on a bench with separate
        instruments, ask for the attenuator chain to move first.
        """
        need = chain_direction(data.tags)
        if need is None or self.operator is None or self._one_instrument():
            return
        if self._chain is not None and need != self._chain:
            self.operator.instruct(f"Move the attenuator chain: "
                                   f"{self._chain_setup(need)}")
        self._chain = need

    def _one_instrument(self) -> bool:
        """Whether one SDR both receives and transmits, so the chain stays."""
        rx, tx = self.bench["receiver"], self.bench["transmitter"]
        return rx["type"] == tx["type"] and rx.get("serial") == tx.get("serial")

    def _chain_setup(self, direction: str) -> str:
        """Where the attenuator chain goes for tests in a direction."""
        if direction == "tx":
            return f"from the radio to the {self.bench['receiver']['type']}"
        return f"from the {self.bench['transmitter']['type']} to the radio"

    def _plan_summary(self) -> str:
        """What the run will do, and what the bench must look like for it."""
        tags = set().union(*(t for _, t in self.planned)) if self.planned \
            else set()
        rx_type = self.bench["receiver"]["type"]
        tx_type = self.bench["transmitter"]["type"]
        lines = [f"Plan for {self.suite}:",
                 f"  Radio: {self.hardware} as {self.radio['name']} "
                 f"({self.radio_file}), firmware {self.firmware}"
                 + (f", DSP image {self.dsp}" if self.dsp else ""),
                 f"  Bench: {self.bench.get('station', 'unnamed')}, receiver "
                 f"{rx_type}, transmitter {tx_type}, calibration "
                 f"{self.calibration.method} of {self.calibration.measured}"
                 + (f" with {', '.join(self.calibration.calibrated())}"
                    if self.calibration.calibrated() else "")]
        bands = [b for b in self.radio["bands"] if f"band:{b}" in tags]
        for band in bands:
            freq = self.radio["bands"][band]["frequency_hz"]
            losses = ", ".join(
                f"{path.replace('_', ' ')} {self.calibration.loss(path, freq):.1f} dB"
                for path in self.calibration.paths)
            lines.append(f"  {band}: {freq / 1e6:.3f} MHz ({losses})")
        modes = sorted(t[5:] for t in tags if t.startswith("mode:"))
        if modes:
            lines.append(f"  Modes: {', '.join(modes)}")
        order = list(dict.fromkeys(
            d for d in (chain_direction(t) for _, t in self.planned) if d))
        if order and self._one_instrument():
            lines.append(f"  Every test: attenuator chain between the radio "
                         f"and the {rx_type}, where it stays")
        for i, direction in enumerate(order):
            what = "Transmit" if direction == "tx" else "Receive"
            when = "first" if i == 0 else "then; you'll be asked to move it"
            if not self._one_instrument():
                lines.append(f"  {what} tests ({when}): attenuator chain "
                             f"{self._chain_setup(direction)}")
        lines.append(f"  Tests ({len(self.planned)}):")
        lines += [f"    - {name}" for name, _ in self.planned]
        lines.append("Check the setup, then continue to begin.")
        return "\n".join(lines)

    @keyword
    def require_separate_instruments(self):
        """
        Skip unless the bench receives and transmits with different
        instruments, as a loopback through the attenuator chain needs.
        """
        if self._one_instrument():
            BuiltIn().skip("The receiver and the transmitter are one "
                           "instrument, which can't loop back into itself")

    @keyword
    def use_band(self, band: str):
        """
        Make a band from the radio file current: captures and transmissions
        use its frequency. Sets ${BAND} and ${FREQUENCY_MHZ} for the test.
        oat.expand calls it in each per-band test's setup.
        """
        bands = self.radio["bands"]
        if band not in bands:
            raise ValueError(f"{self.radio['name']} has no band {band!r}; "
                             f"it has {', '.join(bands)}")
        self.band = band
        self.frequency_hz = float(bands[band]["frequency_hz"])
        builtin = BuiltIn()
        set_variable = (builtin.set_suite_variable
                        if builtin.get_variable_value("${TEST NAME}") is None
                        else builtin.set_test_variable)
        set_variable("${BAND}", band)
        set_variable("${FREQUENCY_MHZ}", f"{self.frequency_hz / 1e6:.3f}")

    def _frequency(self) -> float:
        if self.frequency_hz is None:
            raise RuntimeError("No band selected: tag the test per-band, or "
                               "call Use Band first")
        return self.frequency_hz

    @keyword
    def remember_result(self, name: str, value):
        """Keep a result for later tests on the same band."""
        self._remembered[(self.band, name)] = value

    @keyword
    def recall_result(self, name: str):
        """A result remembered earlier on the current band, or None."""
        return self._remembered.get((self.band, name))

    @keyword
    def close_bench(self):
        """
        Stop the instruments and write measurements.json: every capture,
        check and operator answer, with the run's provenance. Absolute paths
        are made relative, so the file can be published as it is.
        """
        if self.transmitter is not None:
            self.transmitter.stop()
        if self.run_dir is None:
            return
        report = {
            "schema": RESULTS_SCHEMA,
            "suite": self.suite,
            "started": self.started.isoformat(timespec="seconds"),
            "station": self.bench.get("station", "unnamed"),
            "radio": self.radio["name"],
            "radio_file": self.radio_file.stem,
            "firmware": self.firmware,
            "hardware": self.hardware,
            **({"dsp": self.dsp} if self.dsp else {}),
            "suite_commit": suite_commit(),
            "bands": {name: band["frequency_hz"]
                      for name, band in self.radio["bands"].items()},
            "calibration": self.calibration.summary(),
            "records": self.records,
            "operator": self.operator.exchanges(),
        }
        text = scrub(json.dumps(report, indent=2))
        (self.run_dir / "measurements.json").write_text(text + "\n")

    def _record(self, kind: str, **data):
        test = BuiltIn().get_variable_value("${TEST NAME}", "")
        self.records.append({"test": test, "band": self.band, "kind": kind,
                             **data})

    # Operator -----------------------------------------------------------

    @keyword
    def operator_does(self, instruction: str):
        """Ask the operator to do something and wait until it's done."""
        note = self.operator.instruct(instruction)
        self._record("operator", text=instruction, answer=note)

    @keyword
    def operator_confirms(self, question: str):
        """Ask the operator a yes/no question; fail the test on no."""
        ok, note = self.operator.confirm(question)
        self._record("assertion", text=question, passed=ok, note=note)
        if not ok:
            raise AssertionError(f"Operator: {question} -> no"
                                 + (f" ({note})" if note else ""))

    @keyword
    def operator_reports(self, question: str) -> str:
        """Ask the operator for a value or an observation."""
        answer = self.operator.ask(question)
        self._record("observation", text=question, answer=answer)
        return answer

    # Capturing the radio ------------------------------------------------

    @keyword
    def capture_radio_transmissions(self, instruction: str, expected: int = 1):
        """
        Record while the operator follows the instruction, then measure each
        FM transmission. Fails unless exactly `expected` are found. Returns
        the measurements, oldest first; each one's audio is saved as a WAV.
        """
        name, capture, audio_dir, clipped = self._capture(instruction)
        results = self._measure_fm(capture, audio_dir, clipped)
        self._record("capture", instruction=instruction, files=[
            str(p.relative_to(self.run_dir)) for p in capture.paths],
            restarts=capture.restarts, transmissions=results)
        self._log_table(name, results)
        self._expect(results, expected)
        return results

    def _capture(self, instruction: str):
        """
        Record while the operator follows an instruction. Returns the
        capture's name, the capture, a folder for its audio, and the
        fraction of samples at full scale.
        """
        rx = self.bench["receiver"]
        self._captures += 1
        name = f"capture{self._captures:02d}"
        path = self.run_dir / f"{name}.{self._iq_suffix()}"
        self.receiver.start(self._frequency() + rx.get("tune_offset_hz", 0),
                            rx["rate"], path)
        try:
            self.operator.instruct(f"{instruction} {CAPTURE_GUIDANCE}")
        finally:
            capture = self.receiver.stop()
        return (name, capture) + self._after_capture(name, capture)

    def _after_capture(self, name: str, capture):
        """A capture's audio folder and its clipped fraction, warned of."""
        audio_dir = self.run_dir / name
        audio_dir.mkdir(exist_ok=True)
        clipped = receiver_clipped(capture)
        if clipped > MAX_CLIPPED:
            logger.warn(f"{name}: {100 * clipped:.2f}% of samples at the "
                        f"receiver's full scale; levels read low")
        return audio_dir, clipped

    def _measure_fm(self, capture, audio_dir: Path, clipped: float,
                    min_duration_s: float = 1.0) -> list[dict]:
        """Measurements of every FM transmission in a capture."""
        rx = self.bench["receiver"]
        offset = rx.get("tune_offset_hz", 0)
        return [self._absolute({**m.as_dict(), "receiver_clipped": clipped,
                                "frequency_error_hz":
                                    m.carrier_offset_hz + offset,
                                "audio_file": str(
                                    (audio_dir / f"tx{i}.wav").relative_to(
                                        self.run_dir))})
                for i, m in enumerate(measure_capture(
                    capture.blocks(), capture.rate, -offset,
                    min_duration_s=min_duration_s, audio_dir=audio_dir,
                    center_hz=self._frequency() + offset,
                    frequency_correction_ppm=self._ppm(rx) or 0.0), 1)]

    @staticmethod
    def _expect(results: list, expected) -> None:
        if len(results) != int(expected):
            raise AssertionError(f"Expected {expected} transmission(s), "
                                 f"found {len(results)}")

    @keyword
    def start_capture(self):
        """
        Start recording on the test frequency, for a capture that keywords
        rather than the operator time: Stop Capture And Measure FM or M17
        ends it.
        """
        if self._open_capture is not None:
            raise RuntimeError("a capture is already running")
        rx = self.bench["receiver"]
        self._captures += 1
        name = f"capture{self._captures:02d}"
        path = self.run_dir / f"{name}.{self._iq_suffix()}"
        self.receiver.start(self._frequency() + rx.get("tune_offset_hz", 0),
                            rx["rate"], path)
        self._open_capture = name

    @keyword
    def wait_seconds(self, seconds: float):
        """Let time pass on the bench (at once on the simulated one)."""
        if isinstance(self.receiver, SimReceiver):
            self.receiver.clock.sleep(float(seconds))
        else:
            time.sleep(float(seconds))

    def _stop_capture(self):
        name = self._open_capture
        if name is None:
            raise RuntimeError("no capture running: Start Capture first")
        self._open_capture = None
        capture = self.receiver.stop()
        return (name, capture) + self._after_capture(name, capture)

    @keyword
    def stop_capture_and_measure_fm(self, expected: int = 1,
                                    min_duration_s: float = 1.0):
        """End a capture from Start Capture and measure its FM transmissions."""
        name, capture, audio_dir, clipped = self._stop_capture()
        results = self._measure_fm(capture, audio_dir, clipped,
                                   float(min_duration_s))
        self._record("capture", instruction="timed capture", files=[
            str(p.relative_to(self.run_dir)) for p in capture.paths],
            restarts=capture.restarts, transmissions=results)
        self._log_table(name, results)
        self._expect(results, expected)
        return results

    @keyword
    def stop_capture_and_measure_m17(self, expected: int = 1):
        """End a capture from Start Capture and measure its M17 transmissions."""
        name, capture, audio_dir, clipped = self._stop_capture()
        results = self._measure_m17(capture, audio_dir, clipped)
        self._record("m17-capture", instruction="timed capture", files=[
            str(p.relative_to(self.run_dir)) for p in capture.paths],
            restarts=capture.restarts, transmissions=results)
        self._log_m17_table(name, results)
        self._expect(results, expected)
        return results

    @keyword
    def capture_radio_m17_transmissions(self, instruction: str,
                                        expected: int = 1):
        """
        Record while the operator follows the instruction, then measure and
        decode each M17 transmission (oat.m17.phy). Fails unless exactly
        `expected` are found. Each one's demodulated baseband is saved as a
        48 kHz WAV, and its voice, through c2dec when Codec2 is installed.
        """
        name, capture, audio_dir, clipped = self._capture(instruction)
        results = self._measure_m17(capture, audio_dir, clipped)
        self._record("m17-capture", instruction=instruction, files=[
            str(p.relative_to(self.run_dir)) for p in capture.paths],
            restarts=capture.restarts, transmissions=results)
        self._log_m17_table(name, results)
        self._expect(results, expected)
        return results

    def _measure_m17(self, capture, audio_dir: Path,
                     clipped: float) -> list[dict]:
        """Measurements of every M17 transmission in a capture."""
        rx = self.bench["receiver"]
        offset = rx.get("tune_offset_hz", 0)
        results = []
        for i, m in enumerate(measure_m17_capture(
                capture.blocks(), capture.rate, -offset, audio_dir=audio_dir,
                center_hz=self._frequency() + offset,
                frequency_correction_ppm=self._ppm(rx) or 0.0), 1):
            d = {**m.as_dict(), "receiver_clipped": clipped,
                 "frequency_error_hz": m.carrier_offset_hz + offset,
                 "baseband_file": str((audio_dir / f"tx{i}-baseband.wav")
                                      .relative_to(self.run_dir))}
            if m.payload and m17_tools.available("c2dec"):
                voice = audio_dir / f"tx{i}-voice.wav"
                d.update(voice_levels(m17_tools.write_codec2_wav(
                    voice, m.payload)))
                d["voice_file"] = str(voice.relative_to(self.run_dir))
            demod = m17_tools.m17_demod(m.baseband)
            if demod is not None:
                d["m17_demod_sources"] = [x["source"] for x in demod["lsfs"]]
            results.append(self._absolute(d))
        return results

    def _log_m17_table(self, name: str, results: list[dict]):
        """Log M17 measurements as a table, with each one's voice."""
        cols = [("outer_deviation_hz", "outer Hz", ".0f"),
                ("inner_deviation_hz", "inner Hz", ".0f"),
                ("deviation_ratio", "ratio", ".2f"),
                ("evm_percent", "EVM %", ".1f"),
                ("symbol_rate_error_ppm", "symbols ppm", ".1f"),
                ("frequency_error_hz", "freq error Hz", ".0f"),
                ("preamble_s", "preamble s", ".3f"),
                ("stream_frames", "frames", "d"), ("source", "source", "s"),
                ("lsf_crc_ok", "LSF CRC", ""), ("polarity", "polarity", "s")]
        head = "".join(f"<th>{c}</th>" for _, c, _ in cols)
        rows = []
        for i, m in enumerate(results, 1):
            cells = "".join(f"<td>{format(m[k], f)}</td>" for k, _, f in cols)
            voice = ""
            if "voice_file" in m:
                src = (self.run_dir / m["voice_file"]).relative_to(
                    self.output_dir)
                voice = f'<audio controls src="{src}"></audio>'
            rows.append(f"<tr><td>tx{i}</td>{cells}<td>{voice}</td></tr>")
        logger.info(f"<b>{name}</b><table border=1><tr><th></th>{head}"
                    f"<th>voice</th></tr>{''.join(rows)}</table>", html=True)

    def _ppm(self, cfg: dict) -> float | None:
        """An instrument's calibrated reference error, ppm, if known."""
        return self.calibration.frequency_correction_ppm(cfg)

    def _absolute(self, m: dict) -> dict:
        """
        A measurement with its received level in dBm, at the receiver input
        and at the radio's antenna port, when the receiver's levels are
        calibrated at its settings; otherwise as it was.
        """
        try:
            full_scale = self.calibration.receiver_full_scale_dbm(
                self.bench["receiver"], self._frequency())
        except LookupError:
            return m
        dbm = m["power_db"] + full_scale
        return {**m, "power_dbm": dbm, "radio_power_dbm": dbm
                + self.calibration.loss("to_receiver", self._frequency())}

    def _log_table(self, name: str, results: list[dict]):
        """Log the measurements as a table, with each transmission's audio."""
        cols = [("peak_deviation_hz", "peak dev Hz"),
                ("rms_deviation_hz", "rms dev Hz"), ("tone_hz", "tone Hz"),
                ("occupied_bandwidth_hz", "OBW 99% Hz"),
                ("carrier_offset_hz", "offset Hz"), ("duration_s", "s")]
        head = "".join(f"<th>{c}</th>" for _, c in cols)
        rows = []
        for i, m in enumerate(results, 1):
            wav = (self.run_dir / name / f"tx{i}.wav").relative_to(
                self.output_dir)
            cells = "".join(f"<td>{m[k]:.0f}</td>" for k, _ in cols)
            rows.append(f"<tr><td>tx{i}</td>{cells}<td><audio controls "
                        f'src="{wav}"></audio></td></tr>')
        logger.info(f"<b>{name}</b><table border=1><tr><th></th>{head}"
                    f"<th>audio</th></tr>{''.join(rows)}</table>", html=True)

    def _iq_suffix(self) -> str:
        return "cs8" if self.bench["receiver"]["type"] == "hackrf" else "cu8"

    # Checks -------------------------------------------------------------

    @keyword
    def fm_transmission_should_meet_limits(self, measurement: dict,
                                           bandwidth: str):
        """
        Check peak deviation and occupied bandwidth against the radio file's
        limits for a channel bandwidth (BW_12_5 or BW_25).
        """
        limits = self.radio["fm"][bandwidth]
        checks = [("peak_deviation_hz", "max_peak_deviation_hz"),
                  ("occupied_bandwidth_hz", "max_occupied_bandwidth_hz")]
        failures = []
        for field, limit in checks:
            ok = measurement[field] <= limits[limit]
            self._record("limit", field=field, value=measurement[field],
                         limit=limits[limit], bandwidth=bandwidth, passed=ok)
            if not ok:
                failures.append(f"{field} {measurement[field]:.0f} Hz > "
                                f"{limits[limit]} Hz")
        if failures:
            raise AssertionError(f"{bandwidth}: " + "; ".join(failures))

    @keyword
    def fm_transmission_ctcss_should_meet_limits(self, measurement: dict,
                                                 bandwidth: str,
                                                 tone_hz: float):
        """
        Check a transmission's CTCSS tone: its frequency against the tone set
        on the radio, and its deviation against the radio file's range for the
        channel bandwidth.
        """
        limits = self.radio["fm"][bandwidth]
        tone_hz = float(tone_hz)
        tolerance = self.radio["fm"]["ctcss_frequency_tolerance"]
        low, high = limits["ctcss_deviation_hz"]
        error = abs(measurement["ctcss_hz"] - tone_hz) / tone_hz
        dev = measurement["ctcss_deviation_hz"]
        checks = [("ctcss_hz", measurement["ctcss_hz"], error <= tolerance,
                   f"{tone_hz} Hz +-{100 * tolerance:g}%"),
                  ("ctcss_deviation_hz", dev, low <= dev <= high,
                   f"{low}-{high} Hz")]
        failures = []
        for field, value, ok, limit in checks:
            self._record("limit", field=field, value=value, limit=limit,
                         bandwidth=bandwidth, passed=ok)
            if not ok:
                failures.append(f"{field} {value:.1f} Hz outside {limit}")
        if failures:
            raise AssertionError(f"{bandwidth}: " + "; ".join(failures))

    @keyword
    def m17_transmission_should_meet_limits(self, measurement: dict,
                                            checks: str = "all"):
        """
        Check an M17 transmission against the radio file's m17 limits.
        `checks` names some of deviation, ratio, symbol_rate, frequency,
        evm, bandwidth, preamble and polarity, comma-separated, or all.
        """
        limits = self.radio["m17"]
        m = measurement
        low, high = limits["outer_deviation_hz"]
        rlow, rhigh = limits["deviation_ratio"]
        table = {
            "deviation": ("outer_deviation_hz", m["outer_deviation_hz"],
                          low <= m["outer_deviation_hz"] <= high,
                          f"{low}-{high} Hz"),
            "ratio": ("deviation_ratio", m["deviation_ratio"],
                      rlow <= m["deviation_ratio"] <= rhigh,
                      f"{rlow}-{rhigh}"),
            "symbol_rate": ("symbol_rate_error_ppm", m["symbol_rate_error_ppm"],
                            abs(m["symbol_rate_error_ppm"])
                            <= limits["max_symbol_rate_error_ppm"],
                            f"+-{limits['max_symbol_rate_error_ppm']} ppm"),
            "frequency": ("frequency_error_hz", m["frequency_error_hz"],
                          abs(m["frequency_error_hz"])
                          <= limits["max_frequency_error_hz"],
                          f"+-{limits['max_frequency_error_hz']} Hz"),
            "evm": ("evm_percent", m["evm_percent"],
                    m["evm_percent"] <= limits["max_evm_percent"],
                    f"<= {limits['max_evm_percent']}%"),
            "bandwidth": ("occupied_bandwidth_hz", m["occupied_bandwidth_hz"],
                          m["occupied_bandwidth_hz"]
                          <= limits["max_occupied_bandwidth_hz"],
                          f"<= {limits['max_occupied_bandwidth_hz']} Hz"),
            "preamble": ("preamble_s", m["preamble_s"],
                         m["preamble_s"] >= limits["min_preamble_s"],
                         f">= {limits['min_preamble_s']} s"),
            "polarity": ("polarity", m["polarity"],
                         m["polarity"] == "normal", "normal"),
        }
        names = (list(table) if checks == "all"
                 else [c.strip() for c in str(checks).split(",")])
        unknown = [n for n in names if n not in table]
        if unknown:
            raise ValueError(f"unknown M17 checks: {', '.join(unknown)}")
        failures = []
        for name in names:
            field, value, ok, limit = table[name]
            self._record("limit", field=field, value=value, limit=limit,
                         mode="m17", passed=bool(ok))
            if not ok:
                shown = f"{value:.4g}" if isinstance(value, float) else value
                failures.append(f"{field} {shown} outside {limit}")
        if failures:
            raise AssertionError("M17: " + "; ".join(failures))

    @keyword
    def m17_transmissions_should_decode(self, measurements: list,
                                        source: str = ""):
        """
        Check that every M17 transmission decodes: an LSF that passes its
        CRC, from `source` when given, stream frames numbered in sequence
        with the last one flagged, and an EOT. Reports the decode rate.
        """
        failures = []
        for i, m in enumerate(measurements, 1):
            problems = []
            if not m["lsf_crc_ok"]:
                problems.append("no LSF with a good CRC")
            elif source and m["source"] != source.upper():
                problems.append(f"source {m['source']!r}, not {source!r}")
            if not m["stream_frames"]:
                problems.append("no stream frames")
            if m["frame_number_errors"]:
                problems.append(f"{m['frame_number_errors']} frames out of "
                                f"sequence")
            if m["stream_frames"] and not m["last_frame_flagged"]:
                problems.append("last frame not flagged")
            if not m["eot_found"]:
                problems.append("no EOT")
            if problems:
                failures.append(f"tx{i}: " + ", ".join(problems))
        decoded = len(measurements) - len(failures)
        self._record("m17-decode", source=source, transmissions=len(
            measurements), decoded=decoded, failures=failures,
            passed=not failures)
        BuiltIn().set_test_message(f"{decoded}/{len(measurements)} M17 "
                                   f"transmissions decode", append=True)
        if failures:
            raise AssertionError("; ".join(failures))

    @keyword
    def value_should_be_within(self, value: float, nominal: float,
                               tolerance: float, what: str = "value",
                               relative: bool = True):
        """
        Fail unless a value is within tolerance of nominal: a fraction of it
        when relative, otherwise in the value's own unit.
        """
        value, nominal, tolerance = float(value), float(nominal), \
            float(tolerance)
        error = abs(value - nominal) / abs(nominal) if relative \
            else abs(value - nominal)
        ok = error <= tolerance
        limit = (f"{nominal:g} +-{100 * tolerance:g}%" if relative
                 else f"{nominal:g} +-{tolerance:g}")
        self._record("limit", field=what, value=value, limit=limit, passed=ok)
        if not ok:
            raise AssertionError(f"{what} {value:.5g} outside {limit}")
        return value

    @keyword
    def value_should_be_at_most(self, value: float, limit: float,
                                what: str = "value"):
        """Fail unless a value is no more than a limit."""
        ok = float(value) <= float(limit)
        self._record("limit", field=what, value=float(value),
                     limit=f"<= {float(limit):g}", passed=ok)
        if not ok:
            raise AssertionError(f"{what} {float(value):.5g} above "
                                 f"{float(limit):g}")
        return float(value)

    @keyword
    def report_repeatability(self, what: str, *values) -> dict:
        """
        Record the spread of repeated readings of one quantity: mean,
        standard deviation and the range, also in percent and dB of the
        mean. The A-versus-B tolerance of the comparisons comes from these.
        """
        x = np.array([float(v) for v in values])
        if len(x) < 2:
            raise ValueError("repeatability needs two readings or more")
        mean = float(np.mean(x))
        stats = {"what": what, "values": x.tolist(), "mean": mean,
                 "std": float(np.std(x, ddof=1)),
                 "spread": float(np.ptp(x)),
                 "spread_percent": float(100 * np.ptp(x) / abs(mean))
                 if mean else float("nan"),
                 "spread_db": float(20 * np.log10(x.max() / x.min()))
                 if x.min() > 0 else float("nan")}
        self._record("repeatability", **stats)
        BuiltIn().set_test_message(
            f"{what}: mean {mean:.4g}, std {stats['std']:.3g}, spread "
            f"{stats['spread']:.3g} ({stats['spread_percent']:.2f}%)",
            append=True)
        return stats

    @keyword
    def ratio_should_be_between(self, numerator: float, denominator: float,
                                low: float, high: float, what: str = "ratio"):
        """Fail unless numerator / denominator is within [low, high]."""
        ratio = float(numerator) / float(denominator)
        ok = float(low) <= ratio <= float(high)
        self._record("ratio", what=what, value=ratio, low=float(low),
                     high=float(high), passed=ok)
        if not ok:
            raise AssertionError(f"{what} {ratio:.2f} not in [{low}, {high}]")
        return ratio

    @keyword
    def radio_power_in_dbm(self, measurement: dict) -> float:
        """
        A transmission's power at the radio's antenna port, in dBm: the
        received level through the calibrated receiver levels and the
        to_receiver loss. Fails when the receiver's levels aren't
        calibrated at its settings.
        """
        if "radio_power_dbm" not in measurement:
            raise AssertionError(
                "No absolute level: the calibration has no levels.receiver "
                "table for this receiver and its gain settings")
        return measurement["radio_power_dbm"]

    @keyword
    def radio_power_should_be_between(self, measurement: dict,
                                      low_dbm: float, high_dbm: float):
        """Fail unless a transmission's absolute power is in the range."""
        dbm = self.radio_power_in_dbm(measurement)
        ok = float(low_dbm) <= dbm <= float(high_dbm)
        self._record("limit", field="radio_power_dbm", value=dbm,
                     limit=f"{low_dbm}-{high_dbm} dBm", passed=ok)
        if not ok:
            raise AssertionError(f"radio power {dbm:.1f} dBm outside "
                                 f"{low_dbm}-{high_dbm} dBm")
        return dbm

    @keyword
    def power_settings(self) -> list:
        """
        The radio's TX power settings, lowest first: ${POWER_SETTINGS}
        (comma-separated, e.g. for stock firmware: Low,High) or the radio
        file's power_settings. Skips the test when neither lists any.
        """
        given = BuiltIn().get_variable_value("${POWER_SETTINGS}", "")
        settings = ([s.strip() for s in str(given).split(",") if s.strip()]
                    if given else list(self.radio.get("power_settings", [])))
        if not settings:
            BuiltIn().skip("No power settings: list power_settings in the "
                           "radio file or pass -v POWER_SETTINGS:Low,High")
        return settings

    @keyword
    def tx_power_should_rise_with_setting(self, settings: list,
                                          measurements: list):
        """
        Check that each power setting, lowest first, transmits at least the
        radio file's power_min_step_db (default 1 dB) above the one before,
        as received through the bench: relative levels, so no power meter is
        needed. Fails if the receiver clipped, which hides the top settings.
        """
        if len(settings) != len(measurements):
            raise AssertionError(f"{len(settings)} settings but "
                                 f"{len(measurements)} transmissions")
        clipped = max(m.get("receiver_clipped", 0.0) for m in measurements)
        step = float(self.radio.get("power_min_step_db", 1.0))
        base = measurements[0]["power_db"]
        levels = [m["power_db"] - base for m in measurements]
        self._record("power", settings=list(settings), relative_db=levels,
                     received_dbfs=[m["power_db"] for m in measurements],
                     receiver_clipped=clipped)
        logger.info("<table border=1><tr><th>Setting</th><th>Received dBFS"
                    "</th><th>Relative dB</th></tr>" + "".join(
                        f"<tr><td>{s}</td><td>{m['power_db']:.1f}</td>"
                        f"<td>{lvl:+.1f}</td></tr>"
                        for s, m, lvl in zip(settings, measurements, levels))
                    + "</table>", html=True)
        failures = []
        if clipped > MAX_CLIPPED:
            failures.append(f"receiver clipped on {100 * clipped:.2f}% of "
                            f"samples; add attenuation")
        for lower, higher, a, b in zip(settings, settings[1:], levels,
                                       levels[1:]):
            ok = b - a >= step
            self._record("limit", field=f"power step {lower} -> {higher}",
                         value=b - a, limit=f">= {step} dB", passed=ok)
            if not ok:
                failures.append(f"{higher} only {b - a:+.1f} dB over {lower}")
        if failures:
            raise AssertionError("; ".join(failures))
        BuiltIn().set_test_message(
            "Received levels: " + ", ".join(f"{s} {lvl:+.1f} dB"
                                            for s, lvl in zip(settings, levels)),
            append=True)

    # Transmitting into the radio ----------------------------------------

    @keyword
    def start_transmitting_fm_tone(self, deviation_hz: float,
                                   tone_hz: float = 1000.0,
                                   ctcss_hz: float = 0.0,
                                   ctcss_deviation_hz: float = 0.0,
                                   level_dbm: float | None = None):
        """
        Loop an FM tone on the test frequency until stopped. With level_dbm,
        at that level at the radio's antenna port (see Transmit Level).
        """
        tx = self.bench["transmitter"]
        iq = fm_tone(float(tone_hz), float(deviation_hz), TX_LOOP_S, tx["rate"],
                     self._tx_baseband_offset(), float(ctcss_hz),
                     float(ctcss_deviation_hz))
        level = self._play(iq, f"tx-{int(deviation_hz)}hz-{int(tone_hz)}",
                           level_dbm)
        self._record("transmit", deviation_hz=float(deviation_hz),
                     tone_hz=float(tone_hz), ctcss_hz=float(ctcss_hz),
                     **level)

    def _tx_baseband_offset(self, offset_hz: float = 0.0) -> float:
        """
        Where a signal offset_hz from the test frequency sits in the
        transmitter's baseband: past its tune offset, and corrected for its
        reference error, so it leaves on frequency.
        """
        tx = self.bench["transmitter"]
        ppm = self._ppm(tx) or 0.0
        return (-tx.get("tune_offset_hz", 0) + offset_hz
                - ppm * 1e-6 * self._frequency())

    def _play(self, iq: np.ndarray, name: str, level_dbm=None,
              repeat: bool = True) -> dict:
        """
        Write a signal and start the transmitter on it, at the bench's gain
        or at level_dbm at the radio's antenna port. Returns what to record
        about the level.
        """
        tx = self.bench["transmitter"]
        gain_offset, record = 0.0, {}
        if level_dbm not in (None, "", "None"):
            gain, backoff = self.transmit_level(level_dbm)
            iq = iq * np.float32(10 ** (-backoff / 20))
            gain_offset = gain - tx.get("tx_vga_db", 0)
            record = {"level_dbm": float(level_dbm), "tx_gain_db": gain,
                      "digital_backoff_db": backoff}
        path = self.run_dir / f"{name}.cs8"
        write_iq(path, iq, self.transmitter.fmt)
        applied = self.transmitter.start(path, self._frequency()
                                         + tx.get("tune_offset_hz", 0),
                                         tx["rate"], repeat=repeat,
                                         gain_offset_db=gain_offset)
        if abs(applied - gain_offset) > 0.5:
            raise RuntimeError(f"transmitter applied {applied:+.0f} dB "
                               f"instead of {gain_offset:+.0f} dB")
        return record

    @keyword
    def transmit_level(self, level_dbm: float) -> tuple[int, float]:
        """
        The transmitter gain setting and digital back-off for level_dbm at
        the radio's antenna port on the test frequency: the transmitter's
        calibrated output table, less the from_transmitter loss. Fails
        clearly when the bench isn't calibrated for it or the level is out
        of reach.
        """
        freq = self._frequency()
        out = float(level_dbm) + self.calibration.loss("from_transmitter",
                                                       freq)
        try:
            return self.calibration.transmitter_setting(
                self.bench["transmitter"], freq, out, MAX_DIGITAL_BACKOFF_DB)
        except (LookupError, ValueError) as e:
            raise AssertionError(f"Can't transmit {float(level_dbm):.1f} dBm "
                                 f"at the radio: {e}") from None

    @keyword
    def start_transmitting_with_interferer(self, deviation_hz: float,
                                           interferer_offset_hz: float,
                                           interferer_db: float,
                                           interferer_deviation_hz: float,
                                           tone_hz: float = 1000.0,
                                           interferer_tone_hz: float = 400.0,
                                           interferer: bool = True):
        """
        Loop the wanted FM tone on the test frequency plus an interferer,
        interferer_db stronger, interferer_offset_hz away, modulated with a
        different tone so the operator can tell which one they hear.

        The transmitter gain rises by interferer_db, so the wanted signal stays
        at the level a plain tone has. With interferer=off, only the wanted
        signal goes out, at that same level: the baseline that tells a weak
        signal from poor rejection.
        """
        tx = self.bench["transmitter"]
        offset = tx.get("tune_offset_hz", 0)
        db = float(interferer_db)
        wanted = {"tone_hz": float(tone_hz),
                  "deviation_hz": float(deviation_hz),
                  "offset_hz": self._tx_baseband_offset(), "level_db": -db}
        unwanted = {"tone_hz": float(interferer_tone_hz),
                    "deviation_hz": float(interferer_deviation_hz),
                    "offset_hz": self._tx_baseband_offset(
                        float(interferer_offset_hz)),
                    "level_db": 0.0, "on": bool(interferer)}
        iq = fm_carriers([wanted, unwanted], TX_LOOP_S, tx["rate"])
        state = "on" if interferer else "off"
        path = self.run_dir / (f"tx-adjacent-{int(float(interferer_offset_hz))}"
                               f"hz-{int(db)}db-{state}.cs8")
        write_iq(path, iq, self.transmitter.fmt)
        # The wanted carrier sits interferer_db below full scale; raise the
        # gain to put it back. The level of a single carrier scaled to
        # IQ_AMPLITUDE is the reference, so add the sum-of-carriers headroom.
        headroom = 20 * np.log10(1 + 10 ** (-db / 20))
        wanted_db = db + headroom
        applied = self.transmitter.start(
            path, self._frequency() + offset, tx["rate"],
            gain_offset_db=wanted_db)
        if applied < wanted_db - 0.5:
            logger.warn(f"Transmitter gain capped: the wanted signal is "
                        f"{wanted_db - applied:.1f} dB below a plain tone")
        self._record("transmit", deviation_hz=float(deviation_hz),
                     tone_hz=float(tone_hz),
                     interferer_offset_hz=float(interferer_offset_hz),
                     interferer_db=db, interferer=bool(interferer),
                     interferer_deviation_hz=float(interferer_deviation_hz),
                     interferer_tone_hz=float(interferer_tone_hz),
                     gain_offset_db=applied,
                     wanted_shortfall_db=max(0.0, wanted_db - applied))

    @keyword
    def start_transmitting_m17_stream(self, source: str = "N0CALL",
                                      destination: str = "@ALL",
                                      audio: str = "tone:1000",
                                      frames: int = 50,
                                      deviation_hz: float = 2400.0,
                                      offset_hz: float = 0.0,
                                      inverted: bool = False,
                                      preamble_ms: float = 40.0,
                                      gap_s: float = 1.0,
                                      level_dbm: float | None = None,
                                      loop: bool = True):
        """
        Loop an M17 voice stream on the test frequency until stopped:
        preamble, LSF from `source` to `destination`, stream frames, EOT,
        then gap_s without carrier, so the radio sees separate
        transmissions. `audio` is tone:<Hz> or tone:<Hz>:<dBFS> (Codec2,
        `frames` long), a WAV file (Codec2, its whole length), or random
        (random payload bytes, for decoding without Codec2). offset_hz moves
        the carrier off the channel; inverted sends the symbols with the
        wrong sign; level_dbm sets the level at the radio's antenna port.
        With loop=False the stream plays once.
        """
        payloads = self._m17_payloads(str(audio), int(frames))
        preamble = int(round(float(preamble_ms) / 1000 * m17_codec.SYMBOL_RATE))
        symbols = m17_codec.stream_symbols(
            m17_codec.Lsf(destination, source), payloads, preamble)
        tx = self.bench["transmitter"]
        iq = np.concatenate([
            m17_phy.m17_iq(symbols, tx["rate"], float(deviation_hz),
                           self._tx_baseband_offset(float(offset_hz)),
                           bool(inverted)),
            silence(float(gap_s), tx["rate"])])
        level = self._play(iq, f"tx-m17-{source.replace('/', '_')}-"
                               f"{int(float(offset_hz))}hz", level_dbm,
                           repeat=bool(loop))
        self._record("transmit", mode="m17", source=source,
                     destination=destination, audio=str(audio),
                     frames=len(payloads), deviation_hz=float(deviation_hz),
                     offset_hz=float(offset_hz), inverted=bool(inverted),
                     preamble_ms=float(preamble_ms), **level)

    @staticmethod
    def _m17_payloads(audio: str, frames: int) -> list[bytes]:
        """Stream payloads for an audio argument of the M17 keyword."""
        if audio == "random":
            return m17_tools.random_payloads(frames)
        if audio.startswith("tone:"):
            parts = audio.split(":")
            level = float(parts[2]) if len(parts) > 2 else -20.0
            return m17_tools.tone_payloads(
                float(parts[1]), frames * 0.04, level)[:frames]
        return m17_tools.wav_payloads(Path(audio))

    @keyword
    def stop_transmitting(self):
        """Stop the transmitter."""
        self.transmitter.stop()

    @keyword
    def create_audio_tone(self, tone_hz: float = 1000.0,
                          duration_s: float = 30.0,
                          level_dbfs: float = -6.0) -> str:
        """Write a tone WAV for the operator to play into the radio's mic."""
        rate = 48000
        level = 10 ** (float(level_dbfs) / 20)
        pcm = (level * 32767 * tone(float(tone_hz), float(duration_s), rate))
        path = self.run_dir / f"tone-{int(tone_hz)}hz.wav"
        wavfile.write(path, rate, pcm.astype(np.int16))
        return str(path.resolve())

    # Self-test ----------------------------------------------------------

    @keyword
    def simulate_radio_transmissions(self, *bursts):
        """
        With the simulated receiver, make the next capture hold one FM
        transmission per argument, on the test frequency. Each argument is the
        deviation in Hz, optionally followed by a CTCSS tone and its
        deviation, and by @ and a level in dB: 2500, 0:100:400, 2500@-6.
        """
        if not isinstance(self.receiver, SimReceiver):
            raise RuntimeError("only the sim receiver can simulate a radio")
        scene = []
        for spec in bursts:
            spec, _, level = str(spec).partition("@")
            dev, _, ctcss = spec.partition(":")
            ctcss_hz, _, ctcss_dev = ctcss.partition(":")
            scene.append(SimBurst(float(dev),
                                  ctcss_hz=float(ctcss_hz or 0),
                                  ctcss_deviation_hz=float(ctcss_dev or 0),
                                  level_db=float(level or 0)))
        self.receiver.queue_scene(scene, -self.bench["receiver"].get(
            "tune_offset_hz", 0))

    @keyword
    def simulate_radio_m17_transmissions(self, count: int = 1,
                                         deviation_hz: float = 2400.0,
                                         inner_deviation_hz: float | None = None,
                                         source: str = "N0CALL",
                                         frames: int = 25,
                                         inverted: bool = False,
                                         preamble_ms: float = 40.0,
                                         offset_hz: float = 0.0,
                                         level_db: float = 0.0):
        """
        With the simulated receiver, make the next capture hold `count` M17
        transmissions, as a radio would send them.
        """
        if not isinstance(self.receiver, SimReceiver):
            raise RuntimeError("only the sim receiver can simulate a radio")
        m17 = {"source": source, "frames": int(frames),
               "inverted": bool(inverted),
               "preamble_symbols": int(round(float(preamble_ms) / 1000
                                             * m17_codec.SYMBOL_RATE)),
               "inner_deviation_hz": (None if inner_deviation_hz in
                                      (None, "", "None")
                                      else float(inner_deviation_hz))}
        self.receiver.queue_scene(
            [SimBurst(float(deviation_hz), offset_hz=float(offset_hz),
                      level_db=float(level_db), m17=m17)
             for _ in range(int(count))],
            -self.bench["receiver"].get("tune_offset_hz", 0))
