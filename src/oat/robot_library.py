# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Robot Framework keywords for acceptance tests on a bench."""

import datetime
import json
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
from robot.api import logger
from robot.api.deco import keyword, library
from robot.libraries.BuiltIn import BuiltIn, RobotNotRunningError
from scipy.io import wavfile

from oat import aprs
from oat import audio as audio_mod
from oat.analysis import demodulated_audio, measure_capture, to_analysis_rate
from oat.calibration import load_calibration
from oat.config import check_receiver_safe, load_yaml
from oat.iq import IqFormat, clipped_fraction, write_iq
from oat.kiss import make_tnc
from oat.sanitize import scrub
from oat.operator import make_operator
from oat.sdr import make_receiver, make_transmitter
from oat.sdr.sim import SimBurst, SimReceiver, SimTransmitter, link_loopback
from oat.m17 import codec as m17_codec
from oat.m17 import phy as m17_phy
from oat.m17 import tools as m17_tools
from oat.m17.phy import measure_m17_capture
from oat.signals import fm_carriers, fm_modulate, fm_tone, silence, tone

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
# The simulated radio's input at the bench's gain, for relative levels
SIM_BENCH_LEVEL_DBM = -80.0


def chain_direction(tags) -> str | None:
    """Which way a test needs the attenuator chain, from its tx/rx tag."""
    if "tx" in tags:
        return "tx"
    if "rx" in tags:
        return "rx"
    return None


# Which way the attenuator chain faces, per run: "tx" from the radio to the
# receiver, "rx" from the transmitter to the radio. Each suite gets its own
# library, but the chain stays where the run's last suite left it.
_chain_by_run = {}


def receiver_clipped(capture) -> float:
    """Fraction of a capture's samples at the converter's full scale."""
    sizes = [p.stat().st_size for p in capture.paths]
    if not sum(sizes):
        return 0.0
    return sum(clipped_fraction(p, capture.fmt) * n
               for p, n in zip(capture.paths, sizes)) / sum(sizes)


def plain(value):
    """numpy values as the Python ones json can write."""
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"{type(value).__name__} isn't JSON serializable")


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
        self.audio = None
        self.ptt = None
        # What the transmitter is sending, for the simulated radio's speaker
        self._tx_state = None
        self._sim_cache = {}
        self._audio_files = 0
        self.suite = None
        self.started = None
        self.output_dir = None
        self.receiver = None
        self.transmitter = None
        self.operator = None
        self.run_dir = None
        self.records = []
        # How the radio's volume was set for this run, once Set Radio
        # Volume Once has run
        self.radio_volume = None
        self._captures = 0
        # A capture from Start Capture, until it's stopped
        self._open_capture = None
        # The radio's KISS TNC, when the bench has one
        self.tnc = None
        # Differs from run to run, for test traffic (Run Tag)
        self.run_id = secrets.token_hex(2)
        self._aprs_sends = 0
        self._aprs_batches = 0

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
        self.audio = audio_mod.make_audio(self.bench.get("audio"))
        self.ptt = audio_mod.make_ptt(self.bench.get("audio"), self.operator)
        self.audio_mixer = {}
        if isinstance(self.audio, audio_mod.AlsaAudio):
            self.audio_mixer = self.audio.apply_mixer()
        if isinstance(self.audio, audio_mod.SimAudio) and isinstance(
                self.transmitter, SimTransmitter):
            self.audio.source = self._sim_speaker
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
        # Listening from the start, so no frame the radio reports is missed;
        # every byte from the port goes to tnc.log
        self.tnc = make_tnc(self.bench.get("tnc"), self.run_dir / "tnc.log",
                            getattr(self.receiver, "clock", None))
        # Shown in Robot's report, and filterable in robotframework-dashboard
        cal = self.calibration.summary()
        for name, value in (
                ("Firmware", self.firmware),
                ("Radio", f"{self.radio['name']} ({self.radio_file.stem})"),
                ("Hardware", self.hardware),
                *((("DSP", self.dsp),) if self.dsp else ()),
                ("Station", self.bench.get("station", "unnamed")),
                *((("TNC", self.bench["tnc"]["type"]),)
                  if self.bench.get("tnc") else ()),
                ("Calibration", f"{cal['method']}, measured "
                                f"{cal['measured']} ({cal['sha256']})"),
                *((("Audio mixer", "; ".join(
                    f"{k}: {v}" for k, v in self.audio_mixer.items())),)
                  if self.audio_mixer else ()),
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
        run = self._run_key()
        chain = _chain_by_run.get(run)
        if chain is not None and need != chain:
            self.operator.instruct(f"Move the attenuator chain: "
                                   f"{self._chain_setup(need)}")
        _chain_by_run[run] = need

    def _run_key(self):
        """
        The run this library belongs to: its output file, which the run's
        suites share. Outside a run, as in unit tests, the library itself.
        """
        try:
            return BuiltIn().get_variable_value("${OUTPUT FILE}")
        except RobotNotRunningError:
            return self

    def end_test(self, data, result):
        """
        Listener: a test that failed between Key Radio and Release Radio, or
        inside a timed capture, leaves the radio released and the capture
        stopped, so the next test starts clean.
        """
        if self.ptt is not None:
            try:
                self.ptt.unkey()
            except OSError as e:
                logger.warn(f"Couldn't release PTT on {self.ptt.device}: {e}")
        if self._open_capture is not None:
            self._open_capture = None
            self.receiver.stop()

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
        # The simulated pair is one type, but loops back by design
        if self._one_instrument() and not isinstance(self.receiver,
                                                     SimReceiver):
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
        if self.audio is not None:
            self.audio.stop_playing()
        if self.ptt is not None:
            try:
                self.ptt.close()
            except OSError as e:
                logger.warn(f"Couldn't release PTT on {self.ptt.device}: {e}")
        if self.tnc is not None:
            self.tnc.close()
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
            **({"audio_mixer": self.audio_mixer}
               if getattr(self, "audio_mixer", None) else {}),
            "records": self.records,
            **({"tnc_junk": self.tnc.junk} if self.tnc is not None else {}),
            "operator": self.operator.exchanges(),
        }
        text = scrub(json.dumps(report, indent=2, default=plain))
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

    # Radio volume -------------------------------------------------------

    @keyword
    def set_radio_volume_once(self) -> dict:
        """
        Have the operator set the radio's volume for the receive tests that
        record its speaker, once per run, and record the setting. Later calls
        return the first setting.

        A volume knob has no steps to name, so with audio.radio_volume
        target_dbfs in the bench file the volume is set by level: the
        transmitter sends a 1 kHz FM tone (reference_deviation_hz, default
        3 kHz, at reference_level_dbm when calibrated), the speaker is
        measured through the audio interface, and the operator turns the
        knob until it reads target_dbfs within tolerance_db. Without a
        target the operator just sets one and says where it is.
        """
        if self.radio_volume is not None:
            return self.radio_volume
        cfg = (self.bench.get("audio") or {}).get("radio_volume") or {}
        hint = f" ({cfg['note']})" if cfg.get("note") else ""
        if cfg.get("target_dbfs") is None:
            knob = self.operator_reports(
                "Set the radio volume where it will stay for the rest of "
                f"this run{hint}. Where is the knob (e.g. 1 o'clock)?")
            self.radio_volume = {"knob": knob}
        else:
            self.radio_volume = self._set_volume_by_level(cfg, hint)
        summary = f"knob {self.radio_volume['knob'] or 'not given'}"
        if "level_dbfs" in self.radio_volume:
            v = self.radio_volume
            summary = (f"{v['level_dbfs']:.1f} dBFS at the speaker for "
                       f"{v['reference_deviation_hz']:.0f} Hz deviation "
                       f"(target {v['target_dbfs']:g} dBFS), {summary}")
        self._record("radio_volume", **self.radio_volume)
        BuiltIn().set_suite_metadata("Radio volume", summary, top=True)
        return self.radio_volume

    def _set_volume_by_level(self, cfg: dict, hint: str) -> dict:
        """The live-level procedure of Set Radio Volume Once."""
        interface = self._need_audio()
        target = float(cfg["target_dbfs"])
        tolerance = float(cfg.get("tolerance_db", 1.0))
        deviation = float(cfg.get("reference_deviation_hz", 3000.0))
        rounds = int(cfg.get("max_rounds", 12))
        self.operator_does(
            f"Set the radio to FM, {self._frequency() / 1e6:.4f} MHz, 25 kHz "
            "bandwidth, no CTCSS, squelch open. Leave the audio cable in: "
            f"the speaker is measured through it{hint}")
        self.start_transmitting_fm_tone(
            deviation, 1000.0, level_dbm=cfg.get("reference_level_dbm"))
        try:
            for attempt in range(1, rounds + 1):
                path = interface.record(self._audio_path("volume"), 1.5)
                samples, _ = audio_mod.read_wav(path)
                level = audio_mod.rms_dbfs(samples)
                clipped = bool(np.max(np.abs(samples)) > 0.99)
                error = level - target
                if abs(error) <= tolerance and not clipped:
                    knob = self.operator.ask(
                        f"Speaker {level:.1f} dBFS: on target ({target:g} "
                        f"+-{tolerance:g} dB). Leave the volume there. Where "
                        "is the knob (e.g. 1 o'clock)?")
                    accepted = True
                    break
                turn = "down" if error > 0 or clipped else "up"
                why = " and clipping the interface" if clipped else ""
                answer = self.operator.ask(
                    f"Speaker {level:.1f} dBFS{why}; the target is "
                    f"{target:g} +-{tolerance:g} dB. Turn the volume {turn}, "
                    "then press Enter to measure again (or type accept to "
                    "keep it as it is)")
                if answer.lower().startswith("accept"):
                    knob = self.operator.ask("Where is the volume knob?")
                    accepted = False
                    logger.warn(f"Radio volume accepted off target: "
                                f"{level:.1f} dBFS for {target:g} dBFS")
                    break
            else:
                raise AssertionError(
                    f"The speaker didn't reach {target:g} dBFS in {rounds} "
                    f"tries (last {level:.1f} dBFS)")
        finally:
            self.stop_transmitting()
        return {"knob": knob, "level_dbfs": level, "target_dbfs": target,
                "tolerance_db": tolerance, "reference_deviation_hz": deviation,
                "on_target": accepted, "clipped": clipped, "tries": attempt}

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

    def _play(self, iq, name: str, level_dbm=None,
              repeat: bool = True, mode: str = "fm", level_db=None) -> dict:
        """
        Write a signal and start the transmitter on it: at the bench's gain,
        at level_dbm at the radio's antenna port, or level_db relative to
        the bench's gain (for a bench without a transmitter level table).
        iq is an array, or a sequence of arrays written one after another so
        a long signal never sits in memory whole. Returns what to record
        about the level.
        """
        tx = self.bench["transmitter"]
        gain_offset, backoff, record = 0.0, 0.0, {}
        if level_dbm not in (None, "", "None"):
            gain, backoff = self.transmit_level(level_dbm)
            gain_offset = gain - tx.get("tx_vga_db", 0)
            record = {"level_dbm": float(level_dbm), "tx_gain_db": gain,
                      "digital_backoff_db": backoff}
        elif level_db not in (None, "", "None"):
            gain_offset, backoff = self._relative_level(float(level_db))
            record = {"relative_level_db": float(level_db),
                      "tx_gain_offset_db": gain_offset,
                      "digital_backoff_db": backoff}
        path = self.run_dir / f"{name}.cs8"
        scale = np.float32(10 ** (-backoff / 20))
        parts = [iq] if isinstance(iq, np.ndarray) else iq
        path.unlink(missing_ok=True)
        for part in parts:
            write_iq(path, part * scale if backoff else part,
                     self.transmitter.fmt, append=True)
        applied = self.transmitter.start(path, self._frequency()
                                         + tx.get("tune_offset_hz", 0),
                                         tx["rate"], repeat=repeat,
                                         gain_offset_db=gain_offset)
        if abs(applied - gain_offset) > 0.5:
            raise RuntimeError(f"transmitter applied {applied:+.0f} dB "
                               f"instead of {gain_offset:+.0f} dB")
        self._tx_state = {"path": path, "level_dbm": record.get("level_dbm"),
                          "mode": mode}
        return record

    def _relative_level(self, level_db: float) -> tuple[float, float]:
        """
        The transmitter gain offset and digital back-off for level_db
        relative to the bench's gain: whole gain steps first, back-off
        (at most MAX_DIGITAL_BACKOFF_DB) below the bottom of the range.
        """
        tx = self.bench["transmitter"]
        if tx["type"] != "hackrf":
            # Only the HackRF has a gain range; the simulated one takes any
            return float(level_db), 0.0
        vga = float(tx.get("tx_vga_db", 0))
        top = 47.0 - vga
        if level_db > top + 1e-9:
            raise AssertionError(f"{level_db:+.0f} dB is above the "
                                 f"transmitter's range ({top:+.0f} dB)")
        offset = float(max(np.ceil(level_db - 1e-9), -vga))
        backoff = offset - level_db
        if backoff > MAX_DIGITAL_BACKOFF_DB + 1e-9:
            raise AssertionError(
                f"{level_db:+.0f} dB is below the transmitter's range "
                f"({-vga - MAX_DIGITAL_BACKOFF_DB:+.0f} dB): add "
                f"attenuation, or raise tx_vga_db")
        return offset, backoff

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
                           repeat=bool(loop), mode="m17")
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
    def aprs_message(self, addressee: str, text: str, number: str = "") -> str:
        """
        An APRS message's information field, :ADDRESSEE:text{number, for
        Start Transmitting APRS Packets.
        """
        return aprs.message_info(str(addressee), str(text), str(number))

    @keyword
    def start_transmitting_aprs_packets(self, *info: str,
                                        source: str = "N9OAT",
                                        path: str = "WIDE1-1",
                                        deviation_hz: float = 3000.0,
                                        twist_db: float = 0.0,
                                        gap_s: float = 1.5,
                                        txdelay_ms: float = 300.0,
                                        level_dbm=None,
                                        level_db=None,
                                        offset_hz: float = 0.0,
                                        baud_error_ppm: float = 0.0,
                                        per_transmission: int = 1,
                                        loop: bool = False) -> float:
        """
        Send APRS packets on the test frequency: TXDELAY of flags, the AX.25
        UI frames from `source` via `path` (digipeaters separated by commas,
        a * marking one that has repeated it, empty for none), then gap_s
        without carrier. Each transmission carries per_transmission frames
        back to back, one flag apart, as a TNC empties its queue.
        deviation_hz is the louder tone's peak deviation; twist_db raises
        the space tone over the mark, as a pre-emphasizing transmitter does
        (aprs.PREEMPHASIS_TWIST_DB, 5.3 dB), or lowers it when negative.
        offset_hz moves the carrier off frequency and baud_error_ppm the bit
        rate off 1200. The level is level_dbm at the radio, level_db
        relative to the bench's gain, or the bench's gain. Plays once
        unless loop; returns how long one pass lasts, in seconds, to wait
        before Stop Transmitting.
        """
        tx = self.bench["transmitter"]
        hops = [p.strip() for p in str(path).split(",") if p.strip()]
        offset = self._tx_baseband_offset(float(offset_hz))
        # KISS carries frames without their FCS
        frames = [aprs.ui_frame(str(source), str(text), hops)
                  for text in info]
        groups = [frames[i:i + int(per_transmission)]
                  for i in range(0, len(frames), int(per_transmission))]
        sim_level = (float(level_dbm) if level_dbm not in (None, "", "None")
                     else SIM_BENCH_LEVEL_DBM + float(level_db or 0.0))
        samples = 0

        def transmissions():
            nonlocal samples
            for group in groups:
                sound = aprs.packet_audio(group, tx["rate"], float(twist_db),
                                          float(txdelay_ms) / 1000,
                                          float(baud_error_ppm))
                if hasattr(self.tnc, "hear"):
                    self.tnc.hear(sound, tx["rate"],
                                  [f[:-2] for f in group],
                                  float(deviation_hz), sim_level)
                part = fm_modulate(sound, tx["rate"], float(deviation_hz),
                                   offset)
                gap = silence(float(gap_s), tx["rate"])
                samples += len(part) + len(gap)
                yield part
                yield gap

        self._aprs_sends += 1
        level = self._play(transmissions(), f"tx-aprs-{self._aprs_sends:03d}",
                           level_dbm, repeat=bool(loop), mode="aprs",
                           level_db=level_db)
        self._record("transmit", mode="aprs", source=str(source), path=hops,
                     packets=[str(t) for t in info],
                     deviation_hz=float(deviation_hz),
                     twist_db=float(twist_db), offset_hz=float(offset_hz),
                     baud_error_ppm=float(baud_error_ppm),
                     txdelay_ms=float(txdelay_ms),
                     per_transmission=int(per_transmission), **level)
        return samples / tx["rate"]

    @keyword
    def run_tag(self) -> str:
        """
        Four hex digits that differ from run to run, to put in test traffic:
        the radio's de-duplication then can't mistake a rerun's packets for
        the last run's, nor can the operator.
        """
        return self.run_id

    def _need_tnc(self):
        if self.tnc is None:
            BuiltIn().skip("No TNC: add a tnc section to the bench file "
                           "(see bench/example.yaml), or exclude needs:tnc")
        return self.tnc

    @keyword
    def send_aprs_test_frames(self, count: int = 20, kind: str = "status",
                              settle_s: float = 2.0, **options) -> dict:
        """
        Send `count` numbered test frames and count what the radio's TNC
        reported. kind is status (short status reports), full (eight
        digipeaters and a 256-byte information field: the largest frame),
        or escapes (a full-size frame full of the bytes KISS must escape,
        0xC0 and 0xDB). Every frame carries the run tag and its number, so
        each is accounted for: heard, missing, heard more than once, or
        corrupt (ours, but different from what was sent). Takes Start
        Transmitting APRS Packets' options. Returns the counts and the
        fraction heard.
        """
        tnc = self._need_tnc()
        count = int(count)
        tag = f"oat{self.run_id}"
        self._aprs_batches += 1
        batch = f"{tag}.{self._aprs_batches:03d}"
        info = []
        for i in range(count):
            label = f"{batch}.{i:04d}"
            if kind == "status":
                info.append(f">{label}")
            elif kind == "full":
                info.append(f">{label} " + "x" * (aprs.MAX_INFO_BYTES
                                                    - len(label) - 2))
            elif kind == "escapes":
                body = "\xc0\xdb" * aprs.MAX_INFO_BYTES
                info.append((f">{label} " + body)[:aprs.MAX_INFO_BYTES])
            else:
                raise ValueError("kind is status, full or escapes")
        if kind in ("full", "escapes") and "path" not in options:
            options["path"] = ",".join(f"DIGI{n}*" for n in range(1, 9))
        options.setdefault("gap_s", 0.5)
        hops = [p.strip() for p in str(options.get("path", "WIDE1-1"))
                .split(",") if p.strip()]
        source = str(options.get("source", "N9OAT"))
        sent = [aprs.ui_frame(source, text, hops)[:-2] for text in info]
        start = tnc.now()
        seconds = self.start_transmitting_aprs_packets(*info, **options)
        self.wait_seconds(seconds + float(settle_s))
        self.stop_transmitting()
        heard = tnc.heard_since(start)
        wanted = {f: n for n, f in enumerate(sent)}
        got, duplicates, corrupt = set(), 0, []
        for h in heard:
            if h.frame in wanted:
                if wanted[h.frame] in got:
                    duplicates += 1
                got.add(wanted[h.frame])
            elif batch.encode() in h.frame:
                corrupt.append(h.frame.hex())
        result = {
            "frames": kind, "sent": count, "heard": len(got),
            "missing": sorted(set(range(count)) - got),
            "duplicates": duplicates, "corrupt": corrupt,
            "rate": len(got) / count if count else 0.0,
            "level_dbm": options.get("level_dbm"),
            "relative_level_db": options.get("level_db"),
            "options": {k: str(v) for k, v in options.items()},
        }
        self._record("aprs-frames", **result)
        logger.info(f"{kind}: heard {len(got)} of {count}"
                    + (f", {duplicates} twice" if duplicates else "")
                    + (f", {len(corrupt)} corrupt" if corrupt else "")
                    + f" ({', '.join(f'{k}={v}' for k, v in options.items())})")
        return result

    @keyword
    def aprs_frames_should_be_heard(self, result: dict, minimum=None):
        """
        Fail unless the radio reported at least `minimum` of the frames (the
        radio file's aprs.min_frame_rate when not given), and none corrupt
        or twice.
        """
        if minimum in (None, "", "None"):
            minimum = self.radio.get("aprs", {}).get("min_frame_rate", 1.0)
        minimum = float(minimum)
        ok = (result["rate"] >= minimum - 1e-9 and not result["corrupt"]
              and not result["duplicates"])
        self._record("limit", field="aprs_frame_rate", value=result["rate"],
                     limit=f">= {minimum:g}, none corrupt or twice",
                     passed=ok)
        BuiltIn().set_test_message(
            f"{result['frames']}: {result['heard']}/{result['sent']}",
            append=True)
        if not ok:
            raise AssertionError(
                f"heard {result['heard']} of {result['sent']} "
                f"({result['rate']:.0%}, need {minimum:.0%}), "
                f"{len(result['corrupt'])} corrupt, "
                f"{result['duplicates']} twice; missing {result['missing']}")

    @keyword
    def find_aprs_sensitivity(self, start: float = 0.0, stop: float = -40.0,
                              step: float = 2.0, count: int = 20,
                              absolute=None, **options) -> dict:
        """
        Step the level down from `start` to `stop`, sending `count` test
        frames at each, until fewer than half are heard; the result has the
        curve and the levels where 90% and 50% of frames were still heard,
        interpolated. Levels are dBm at the radio with a transmitter level
        table (or absolute: true), otherwise dB relative to the bench's
        gain; compare relative results only within one session, on one
        bench. Takes Send APRS Test Frames' options.
        """
        if absolute in (None, "", "None"):
            absolute = self.calibration.transmitter_levels is not None
        absolute = str(absolute).lower() in ("true", "1", "yes")
        key = "level_dbm" if absolute else "level_db"
        points = []
        level = float(start)
        while level >= float(stop) - 1e-9:
            r = self.send_aprs_test_frames(count, **{key: level}, **options)
            points.append((level, r["rate"]))
            if r["rate"] < 0.5:
                break
            level -= float(step)

        def crossing(target):
            for (l1, r1), (l2, r2) in zip(points, points[1:]):
                if r1 >= target > r2:
                    return float(l1 + (target - r1) * (l2 - l1) / (r2 - r1))
            return None

        result = {"unit": "dBm" if absolute else "dB re bench",
                  "points": points, "count": int(count),
                  "level_90": crossing(0.9), "level_50": crossing(0.5),
                  "options": {k: str(v) for k, v in options.items()}}
        self._record("aprs-sensitivity", **result)
        rows = "".join(f"<tr><td>{l:+.1f}</td><td>{r:.0%}</td></tr>"
                       for l, r in points)
        logger.info(f"<table><tr><th>level ({result['unit']})</th>"
                    f"<th>heard</th></tr>{rows}</table>", html=True)
        if result["level_50"] is None:
            raise AssertionError(f"the frame rate never fell below 50% "
                                 f"between {start} and {stop}: {points}")
        BuiltIn().set_test_message(
            "90% at "
            + (f"{result['level_90']:+.1f}" if result["level_90"] is not None
               else "n/a")
            + f", 50% at {result['level_50']:+.1f} {result['unit']}",
            append=True)
        return result

    @keyword
    def stop_transmitting(self):
        """Stop the transmitter."""
        self.transmitter.stop()
        self._tx_state = None

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

    # Audio --------------------------------------------------------------

    def _need_audio(self) -> audio_mod.AudioInterface:
        if self.audio is None:
            raise AssertionError("No audio interface: add an audio section to "
                                 "the bench file, or exclude needs:audio")
        return self.audio

    def _audio_path(self, stem: str) -> Path:
        self._audio_files += 1
        return self.run_dir / f"audio{self._audio_files:02d}-{stem}.wav"

    def _injected_dbfs(self, level_dbfs, level_mv) -> float:
        """A level for the interface's output: dBFS, or from millivolts."""
        if level_mv not in (None, "", "None"):
            try:
                full = self.calibration.audio_full_scale_mvrms("audio_out")
            except LookupError as e:
                raise AssertionError(str(e)) from None
            return float(20 * np.log10(float(level_mv) / full))
        return float(level_dbfs)

    def _audio_out_mv(self, level_dbfs: float) -> float | None:
        try:
            return self.calibration.audio_full_scale_mvrms("audio_out") \
                * 10 ** (level_dbfs / 20)
        except LookupError:
            return None

    def _inject(self, samples: np.ndarray, stem: str, loop, wait,
                **record) -> str:
        interface = self._need_audio()
        path = audio_mod.write_wav(self._audio_path(stem), samples,
                                   interface.rate)
        interface.play(path, repeat=bool(loop))
        self._record("inject", file=str(path.relative_to(self.run_dir)),
                     loop=bool(loop), **record)
        if wait and not loop:
            interface.wait_playing()
        return str(path)

    @keyword
    def inject_audio_tone(self, tone_hz: float = 1000.0,
                          level_dbfs: float = -20.0,
                          level_mv: float | None = None,
                          duration_s: float = 4.0, loop: bool = True):
        """
        Play a tone into the radio's mic input through the audio interface,
        looping until Stop Injecting Audio unless loop=False. level_mv sets
        its RMS millivolts at the mic input, through the calibrated
        paths.audio_out level; otherwise level_dbfs sets it.
        """
        dbfs = self._injected_dbfs(level_dbfs, level_mv)
        rate = self._need_audio().rate
        return self._inject(audio_mod.tone(float(tone_hz), float(duration_s),
                                           dbfs, rate),
                            f"tone-{int(float(tone_hz))}hz", loop, True,
                            tone_hz=float(tone_hz), level_dbfs=dbfs,
                            level_mv=self._audio_out_mv(dbfs))

    @keyword
    def inject_audio_tones(self, freqs_hz: str = "300,500,1000,2000,3000",
                           level_dbfs: float = -30.0,
                           level_mv: float | None = None,
                           tone_s: float = 1.0, gap_s: float = 0.3,
                           loop: bool = False):
        """
        Play tones in turn, each followed by silence, into the mic input,
        for a frequency response; waits for the end unless loop=True.
        """
        freqs = [float(f) for f in str(freqs_hz).split(",")]
        dbfs = self._injected_dbfs(level_dbfs, level_mv)
        rate = self._need_audio().rate
        return self._inject(audio_mod.tone_sequence(
            freqs, float(tone_s), float(gap_s), dbfs, rate), "tones", loop,
            True, freqs_hz=freqs, level_dbfs=dbfs,
            level_mv=self._audio_out_mv(dbfs))

    @keyword
    def inject_audio_file(self, path: str, gain_db: float = 0.0,
                          loop: bool = False, wait: bool = True):
        """
        Play a WAV file into the mic input, its samples scaled by gain_db;
        waits for the end unless loop=True or wait=False.
        """
        samples, rate = audio_mod.read_wav(Path(path))
        interface = self._need_audio()
        if rate != interface.rate:
            g = np.gcd(rate, interface.rate)
            from scipy.signal import resample_poly
            samples = resample_poly(samples, interface.rate // g, rate // g)
        samples = samples * 10 ** (float(gain_db) / 20)
        clipped = float(np.mean(np.abs(samples) >= 1.0))
        if clipped:
            logger.warn(f"{100 * clipped:.2f}% of {path} clips at this gain")
        return self._inject(samples, Path(path).stem, loop, wait,
                            source=Path(path).name, gain_db=float(gain_db))

    @keyword("Create Speech-Like Audio")
    def create_speech_like_audio(self, duration_s: float = 10.0,
                                 level_dbfs: float = -20.0) -> str:
        """
        Write a repeatable speech-shaped test signal (audio.speech_like) as a
        WAV, for when no speech recording is given.
        """
        path = self._audio_path("speech-like")
        audio_mod.write_wav(path, audio_mod.speech_like(
            float(duration_s), level_dbfs=float(level_dbfs)))
        return str(path)

    @keyword
    def stop_injecting_audio(self):
        """Stop playing into the mic input."""
        if self.audio is not None:
            self.audio.stop_playing()

    @keyword
    def bench_keys_the_radio(self) -> bool:
        """
        Whether Key Radio keys the radio itself, through the audio
        interface's PTT line (or the simulated interface), rather than by
        asking the operator.
        """
        return self.ptt is not None or isinstance(self.audio,
                                                  audio_mod.SimAudio)

    @keyword
    def bench_transmits_at_calibrated_levels(self) -> bool:
        """
        Whether the calibration has a transmitter level table, so level_dbm
        arguments work; without one the transmitter plays at the bench
        file's gain, which is enough to compare before and after.
        """
        return self.calibration.transmitter_levels is not None

    @keyword
    def bench_has_audio(self) -> bool:
        """Whether the bench has an audio interface on the radio."""
        return self.audio is not None

    @keyword
    def key_radio(self):
        """
        Key the radio: through the audio interface's PTT line when the bench
        has one, otherwise by asking the operator to hold PTT.
        """
        if self.ptt is not None:
            self.ptt.key()
        else:
            self.operator.instruct("Key PTT and hold it, then continue.")
        if isinstance(self.audio, audio_mod.SimAudio) and isinstance(
                self.receiver, SimReceiver):
            self._sim_keyed = self.receiver.clock.now

    @keyword
    def release_radio(self):
        """Release PTT, as Key Radio keyed it."""
        if self.ptt is not None:
            self.ptt.unkey()
        else:
            self.operator.instruct("Release PTT, then continue.")
        if getattr(self, "_sim_keyed", None) is not None:
            self._sim_radio_transmits(self._sim_keyed,
                                      self.receiver.clock.now)
            self._sim_keyed = None

    def _sim_radio_transmits(self, start: float, stop: float):
        """
        The simulated radio's transmission while it was keyed: what the
        audio interface plays into its mic, pre-emphasized 6 dB/octave,
        -40 dBFS at 1 kHz deviating 1 kHz, limited at 5 kHz.
        """
        from oat.signals import fm_modulate
        from oat.sdr.sim import SimPlay
        rx = self.bench["receiver"]
        rate = rx["rate"]
        n = int((stop - start) * audio_mod.RATE)
        if n <= 0:
            return
        mic = np.zeros(n)
        if self.audio.playing is not None:
            played, r = audio_mod.read_wav(self.audio.playing)
            mic = (np.resize(played, n) if self.audio.repeat
                   else np.pad(played[:n], (0, max(0, n - len(played)))))
        spec = np.fft.rfft(mic)
        freqs = np.fft.rfftfreq(n, 1 / audio_mod.RATE)
        spec *= np.where(freqs <= 3500, freqs / 1000, 0.0)
        dev = np.clip(np.fft.irfft(spec, n) * 1000 / 10 ** (-40 / 20),
                      -5000, 5000)
        from scipy.signal import resample_poly
        g = np.gcd(int(rate), audio_mod.RATE)
        dev = resample_poly(dev, int(rate) // g, audio_mod.RATE // g)
        iq = fm_modulate(dev, rate, 1.0)
        self._captures_sim = getattr(self, "_captures_sim", 0) + 1
        path = self.run_dir / f"sim-radio-{self._captures_sim:02d}.cs8"
        write_iq(path, iq, IqFormat.CS8)
        self.receiver.emissions.append(SimPlay(
            path, self._frequency(), rate, False, 0.0, start, stop))

    def _audio_result(self, samples: np.ndarray, rate: int, unit: str,
                      file: Path, tone_hz=None,
                      weighting: str = "none") -> dict:
        """Level, tone, distortion and dropouts of a stretch of audio."""
        tone = float(tone_hz) if tone_hz not in (None, "", "None") else None
        d = audio_mod.distortion(samples, rate, tone, weighting)
        result = {"file": str(file.relative_to(self.run_dir)), "unit": unit,
                  "rate": rate, "tone_hz": d.tone_hz,
                  "tone_amplitude": audio_mod.tone_amplitude(samples, rate,
                                                             d.tone_hz),
                  "sinad_db": d.sinad_db, "thd_n_percent": d.thd_n_percent,
                  "weighting": weighting,
                  "longest_gap_s": audio_mod.longest_gap(samples, rate)}
        if unit == "dBFS":
            result["level_dbfs"] = d.level_dbfs
            try:
                full = self.calibration.audio_full_scale_mvrms("audio_in")
                result["level_mvrms"] = full * 10 ** (d.level_dbfs / 20)
            except LookupError:
                pass
        return result

    @keyword
    def capture_radio_audio(self, seconds: float = 3.0, tone_hz=None,
                            weighting: str = "none") -> dict:
        """
        Record the radio's speaker output through the audio interface and
        measure it: level (in mVrms too with a calibrated paths.audio_in),
        the tone, SINAD and THD+N (weighting none or psophometric), and the
        longest dropout. Returns the measurements.
        """
        interface = self._need_audio()
        path = interface.record(self._audio_path("speaker"), float(seconds))
        samples, rate = audio_mod.read_wav(path)
        result = self._audio_result(samples, rate, "dBFS", path, tone_hz,
                                    weighting)
        self._record("audio", **result)
        logger.info(f'<audio controls src="{path.relative_to(self.output_dir)}">'
                    f'</audio> {result["tone_hz"]:.0f} Hz, SINAD '
                    f'{result["sinad_db"]:.1f} dB', html=True)
        return result

    @keyword
    def measure_transmitted_audio(self, measurement: dict, tone_hz=None,
                                  weighting: str = "none") -> dict:
        """
        The audio measurements of Capture Radio Audio, on the audio
        demodulated from an FM transmission, in Hz of deviation.
        """
        path = self.run_dir / measurement["audio_file"]
        samples, rate = audio_mod.read_wav(path)
        # save_audio wrote 5 kHz of deviation at 0.8 of full scale
        result = self._audio_result(samples * 5000 / 0.8, rate, "Hz", path,
                                    tone_hz, weighting)
        self._record("audio", **result)
        return result

    def _samples(self, measurement: dict) -> tuple[np.ndarray, int]:
        samples, rate = audio_mod.read_wav(self.run_dir
                                           / measurement.get("file")
                                           if "file" in measurement
                                           else self.run_dir
                                           / measurement["audio_file"])
        return samples, rate

    @keyword
    def audio_sinad_should_be_at_least(self, measurement: dict,
                                       sinad_db: float):
        """Fail unless an audio measurement's SINAD reaches sinad_db."""
        ok = measurement["sinad_db"] >= float(sinad_db)
        self._record("limit", field="sinad_db", value=measurement["sinad_db"],
                     limit=f">= {float(sinad_db):g} dB",
                     weighting=measurement["weighting"], passed=ok)
        if not ok:
            raise AssertionError(f"SINAD {measurement['sinad_db']:.1f} dB < "
                                 f"{float(sinad_db):g} dB")

    @keyword
    def audio_distortion_should_meet_limits(self, measurement: dict,
                                            direction: str):
        """THD+N against the radio file's fm <direction>_max_thd_n_percent."""
        limit = float(self.radio["fm"][f"{direction}_max_thd_n_percent"])
        value = measurement["thd_n_percent"]
        ok = value <= limit
        self._record("limit", field=f"{direction} thd_n_percent", value=value,
                     limit=f"<= {limit:g}%", passed=ok)
        if not ok:
            raise AssertionError(f"{direction} THD+N {value:.1f}% > {limit:g}%")

    @keyword
    def measure_audio_response(self, measurement: dict,
                               reference_hz: float = 1000.0) -> list:
        """
        Levels of the tone bursts in a recording or a transmission's audio,
        relative to the reference tone, lowest frequency first.
        """
        if "file" in measurement:
            samples, rate = audio_mod.read_wav(self.run_dir
                                               / measurement["file"])
        else:
            samples, rate = audio_mod.read_wav(self.run_dir
                                               / measurement["audio_file"])
        levels = audio_mod.response(audio_mod.tone_segments(samples, rate),
                                    float(reference_hz))
        result = [{"tone_hz": s.tone_hz, "level_db": s.level_db}
                  for s in levels]
        self._record("response", reference_hz=float(reference_hz),
                     levels=result)
        logger.info("<table border=1><tr><th>Hz</th><th>dB</th></tr>"
                    + "".join(f"<tr><td>{r['tone_hz']:.0f}</td>"
                              f"<td>{r['level_db']:+.1f}</td></tr>"
                              for r in result) + "</table>", html=True)
        return result

    @keyword
    def audio_response_should_meet_limits(self, levels: list, direction: str,
                                          reference_hz: float = 1000.0):
        """
        Check a response against the radio file's fm
        <direction>_audio_response: within tolerance_db of a slope in dB per
        octave through the reference, over band_hz.
        """
        spec = self.radio["fm"][f"{direction}_audio_response"]
        low, high = spec["band_hz"]
        tol_low, tol_high = spec["tolerance_db"]
        slope = float(spec["slope_db_per_octave"])
        failures = []
        for r in levels:
            if not low <= r["tone_hz"] <= high:
                continue
            error = r["level_db"] - slope * np.log2(r["tone_hz"]
                                                    / float(reference_hz))
            ok = tol_low <= error <= tol_high
            self._record("limit", field=f"{direction} response at "
                         f"{r['tone_hz']:.0f} Hz", value=float(error),
                         limit=f"{tol_low:+g}/{tol_high:+g} dB of "
                               f"{slope:+g} dB/octave", passed=ok)
            if not ok:
                failures.append(f"{r['tone_hz']:.0f} Hz {error:+.1f} dB")
        if failures:
            raise AssertionError(f"{direction} response off its "
                                 f"{slope:+g} dB/octave: "
                                 + ", ".join(failures))

    @keyword
    def audio_should_have_no_dropouts(self, measurement: dict):
        """The longest dropout against the radio file's fm rx_max_dropout_s."""
        limit = float(self.radio["fm"]["rx_max_dropout_s"])
        gap = measurement["longest_gap_s"]
        ok = gap <= limit
        self._record("limit", field="longest_gap_s", value=gap,
                     limit=f"<= {limit:g} s", passed=ok)
        if not ok:
            raise AssertionError(f"{1000 * gap:.0f} ms dropout in the audio")

    @keyword
    def measure_audio_images(self, measurement: dict, tone_hz: float,
                             *image_hz) -> dict:
        """Level of image frequencies relative to the tone, in dB."""
        samples, rate = self._samples(measurement)
        levels = audio_mod.images(samples, rate, float(tone_hz),
                                  [float(f) for f in image_hz])
        self._record("images", tone_hz=float(tone_hz),
                     levels_db={str(k): v for k, v in levels.items()})
        BuiltIn().set_test_message(", ".join(
            f"{k:.0f} Hz {v:+.1f} dB" for k, v in levels.items()),
            append=True)
        return {str(int(k)): v for k, v in levels.items()}

    @keyword
    def mic_sensitivity(self, measurement: dict, injected_dbfs: float,
                        target_deviation_hz: float) -> dict:
        """
        The injected level that would give target_deviation_hz, scaled from
        one take at injected_dbfs (below the limiter, the path is linear):
        in dBFS of the interface, and in mVrms with a calibrated
        paths.audio_out.
        """
        got = float(measurement["tone_deviation_hz"])
        if got <= 0:
            raise AssertionError("No tone in the transmission")
        dbfs = float(injected_dbfs) + 20 * np.log10(float(target_deviation_hz)
                                                    / got)
        result = {"target_deviation_hz": float(target_deviation_hz),
                  "injected_dbfs": float(injected_dbfs),
                  "measured_deviation_hz": got, "sensitivity_dbfs": dbfs,
                  "sensitivity_mvrms": self._audio_out_mv(dbfs)}
        self._record("mic-sensitivity", **result)
        mv = result["sensitivity_mvrms"]
        BuiltIn().set_test_message(
            f"{float(target_deviation_hz):.0f} Hz deviation at {dbfs:.1f} "
            f"dBFS" + (f" ({mv:.2f} mVrms)" if mv is not None else ""),
            append=True)
        return result

    @keyword
    def spread_should_be_at_most(self, what: str, limit_db: float, *values):
        """
        Fail unless repeated readings of one level, in linear units such as
        Hz of deviation, spread over no more than limit_db.
        """
        stats = self.report_repeatability(what, *values)
        ok = stats["spread_db"] <= float(limit_db)
        self._record("limit", field=f"{what} spread_db",
                     value=stats["spread_db"], limit=f"<= {limit_db} dB",
                     passed=ok)
        if not ok:
            raise AssertionError(f"{what} spreads over "
                                 f"{stats['spread_db']:.2f} dB")

    @keyword
    def find_rx_sensitivity(self, deviation_hz: float,
                            target_sinad_db: float = 12.0,
                            start_dbm: float = -100.0,
                            stop_dbm: float = -130.0, step_db: float = 2.0,
                            weighting: str = "psophometric",
                            seconds: float = 2.0, tone_hz: float = 1000.0
                            ) -> dict:
        """
        Step an FM tone down from start_dbm at the radio's antenna port,
        recording the speaker at each level, until SINAD falls below the
        target; the sensitivity is where the SINAD crosses it, interpolated
        between the levels either side. Needs absolute transmitter levels
        and the audio interface.
        """
        points = []
        level = float(start_dbm)
        try:
            while level >= float(stop_dbm) - 1e-9:
                self.start_transmitting_fm_tone(deviation_hz, tone_hz,
                                                level_dbm=level)
                self.wait_seconds(0.5)
                a = self.capture_radio_audio(seconds, tone_hz, weighting)
                points.append((level, a["sinad_db"]))
                if a["sinad_db"] < float(target_sinad_db):
                    break
                level -= float(step_db)
        finally:
            self.stop_transmitting()
        result = {"deviation_hz": float(deviation_hz),
                  "target_sinad_db": float(target_sinad_db),
                  "weighting": weighting, "points": points,
                  "sensitivity_dbm": None}
        if len(points) >= 2 and points[-1][1] < float(target_sinad_db) \
                <= points[-2][1]:
            (l1, s1), (l2, s2) = points[-2], points[-1]
            result["sensitivity_dbm"] = float(
                l1 + (float(target_sinad_db) - s1) * (l2 - l1) / (s2 - s1))
        self._record("rx-sensitivity", **result)
        if result["sensitivity_dbm"] is None:
            raise AssertionError(f"SINAD never crossed {target_sinad_db} dB "
                                 f"between {start_dbm} and {stop_dbm} dBm: "
                                 f"{points}")
        BuiltIn().set_test_message(
            f"{target_sinad_db:g} dB SINAD at "
            f"{result['sensitivity_dbm']:.1f} dBm", append=True)
        return result

    def _sim_m17_voice(self, path: Path, rate: int) -> np.ndarray:
        """
        The simulated radio's M17 voice: the stream decoded, its Codec2
        frames through c2dec, and the 8 kHz audio held for each output
        sample, as an unfiltered rate change does; silence without c2dec.
        """
        from oat.iq import read_iq
        tx = self.bench["transmitter"]
        # Quiet around it, as transmissions are found against the noise
        quiet = silence(1.0, tx["rate"])
        found = measure_m17_capture(
            np.concatenate([quiet, read_iq(path, IqFormat.CS8), quiet]),
            tx["rate"], -tx.get("tune_offset_hz", 0))
        payload = b"".join(m.payload for m in found)
        if not payload or not m17_tools.available("c2dec"):
            return np.zeros(rate)
        voice = m17_tools.codec2_decode(payload) / 32767.0
        return np.repeat(voice, rate // m17_tools.CODEC2_RATE)

    def _sim_speaker(self, n: int, rate: int) -> np.ndarray:
        """
        The simulated radio's speaker: FM demodulation of what the
        simulated transmitter plays, de-emphasized, with noise that sets
        12 dB SINAD at -118 dBm and rises 1 dB per dB below.
        """
        state = self._tx_state
        rng = np.random.default_rng(self._audio_files)
        if state is None:
            # Nothing received: the interface loops back what it plays
            return None
        path = state["path"]
        # A file rewritten under the same name is a new signal
        key = (path, path.stat().st_mtime_ns)
        if key not in self._sim_cache and state["mode"] == "m17":
            self._sim_cache[key] = self._sim_m17_voice(path, rate)
        if key not in self._sim_cache:
            from oat.iq import IqFormat, read_iq
            from scipy.signal import resample_poly, sosfilt, butter
            tx = self.bench["transmitter"]
            iq, r, off = to_analysis_rate(read_iq(path, IqFormat.CS8),
                                          tx["rate"],
                                          -tx.get("tune_offset_hz", 0))
            n_ = np.arange(len(iq))
            iq = iq * np.exp(-2j * np.pi * off * n_ / r)
            dev = demodulated_audio(iq, r)
            g = np.gcd(rate, 48000)
            dev = resample_poly(dev, rate // g, 48000 // g)
            # De-emphasis, 6 dB/octave above 300 Hz, unity at 1 kHz
            deemph = butter(1, 300, fs=rate, output="sos")
            self._sim_cache[key] = sosfilt(deemph, dev) * (1000 / 300) \
                * 0.3 / 3000
        speaker = np.resize(self._sim_cache[key], n)
        level = state.get("level_dbm")
        sinad = 40.0 if level is None else min(40.0, 12 + level + 118)
        rms = np.sqrt(np.mean(speaker ** 2)) or 1e-6
        noise_rms = rms / np.sqrt(max(10 ** (sinad / 10) - 1, 1e-3))
        from scipy.signal import butter, sosfilt
        noise = sosfilt(butter(4, (300, 3000), "bandpass", fs=rate,
                               output="sos"), rng.normal(size=n))
        noise *= noise_rms / (np.sqrt(np.mean(noise ** 2)) or 1.0)
        return speaker + noise

    @keyword
    def radio_value(self, section: str, name: str):
        """A value from the radio file, such as aprs inbox_entries."""
        try:
            return self.radio[section][name]
        except KeyError:
            raise AssertionError(f"{self.radio_file}: no {section}.{name}") \
                from None

    @keyword
    def test_deviation(self, bandwidth: str) -> float:
        """The radio file's reference deviation for a channel bandwidth."""
        return float(self.radio["fm"]["test_deviation_hz"][bandwidth])

    @keyword
    def mic_level_spread_should_meet_limits(self, *deviations):
        """
        Fail unless the deviations one injected tone gave, take after take,
        spread over no more than the radio file's mic_level_max_spread_db.
        """
        self.spread_should_be_at_most(
            "mic level", float(self.radio["fm"]["mic_level_max_spread_db"]),
            *deviations)

    @keyword
    def report_ratio_in_db(self, what: str, numerator: float,
                           denominator: float) -> float:
        """Record 20 log10 of a ratio of amplitudes, as hum and noise is."""
        db = float(20 * np.log10(float(numerator) / float(denominator)))
        self._record("ratio-db", what=what, value=db)
        BuiltIn().set_test_message(f"{what}: {db:.1f} dB", append=True)
        return db

    @keyword
    def start_transmitting_fm_tones(self, deviation_hz: float,
                                    freqs_hz: str = "300,500,1000,2000,3000",
                                    tone_s: float = 1.0, gap_s: float = 0.3,
                                    level_dbm: float | None = None):
        """
        Loop FM tones in turn, each at the same deviation and followed by
        unmodulated carrier, for the radio's receive audio response.
        """
        tx = self.bench["transmitter"]
        rate = tx["rate"]
        freqs = [float(f) for f in str(freqs_hz).split(",")]
        mod = np.concatenate([np.concatenate([
            tone(f, float(tone_s), rate), np.zeros(int(float(gap_s) * rate))])
            for f in freqs])
        from oat.signals import fm_modulate
        iq = fm_modulate(mod, rate, float(deviation_hz),
                         self._tx_baseband_offset())
        level = self._play(iq, f"tx-tones-{int(float(deviation_hz))}hz",
                           level_dbm)
        self._record("transmit", deviation_hz=float(deviation_hz),
                     freqs_hz=freqs, **level)

    @keyword
    def codec2_alias_level(self, reference: dict, measurement: dict,
                           alias_hz: float, reference_hz: float = 1000.0
                           ) -> float:
        """
        How loud an alias is in a transmission's decoded voice, in dB
        relative to the reference tone in another take's voice: an injected
        tone above 4 kHz that the 8 kHz Codec2 input folds back.
        """
        for m in (reference, measurement):
            if "voice_file" not in m:
                BuiltIn().skip("No decoded voice: install codec2 (c2dec)")
        ref, rate = audio_mod.read_wav(self.run_dir / reference["voice_file"])
        got, rate2 = audio_mod.read_wav(self.run_dir
                                        / measurement["voice_file"])
        a_ref = audio_mod.tone_amplitude(ref, rate, float(reference_hz), 40)
        a_alias = audio_mod.tone_amplitude(got, rate2, float(alias_hz), 40)
        db = float(20 * np.log10(max(a_alias, 1e-9) / max(a_ref, 1e-9)))
        self._record("alias", alias_hz=float(alias_hz),
                     reference_hz=float(reference_hz), level_db=db)
        BuiltIn().set_test_message(f"{float(alias_hz):.0f} Hz alias at "
                                   f"{db:+.1f} dB", append=True)
        return db

    @keyword
    def find_m17_rx_sensitivity(self, start_dbm: float = -100.0,
                                stop_dbm: float = -130.0,
                                step_db: float = 2.0,
                                min_sinad_db: float = 10.0,
                                source: str = "OAT1",
                                seconds: float = 3.0,
                                tone_hz: float = 1000.0) -> dict:
        """
        Step a looped M17 stream carrying a Codec2 tone down from start_dbm,
        recording the speaker at each level, until the tone's SINAD falls
        below min_sinad_db. The sensitivity is the lowest level that still
        reached it.
        """
        points, last_good = [], None
        level = float(start_dbm)
        try:
            while level >= float(stop_dbm) - 1e-9:
                self.start_transmitting_m17_stream(
                    source, audio=f"tone:{tone_hz:g}", frames=50, gap_s=0.2,
                    level_dbm=level)
                self.wait_seconds(0.5)
                a = self.capture_radio_audio(seconds, tone_hz)
                points.append((level, a["sinad_db"]))
                if a["sinad_db"] < float(min_sinad_db):
                    break
                last_good = level
                level -= float(step_db)
        finally:
            self.stop_transmitting()
        result = {"min_sinad_db": float(min_sinad_db), "points": points,
                  "sensitivity_dbm": last_good}
        self._record("m17-rx-sensitivity", **result)
        if last_good is None:
            raise AssertionError(f"No audio at {start_dbm} dBm already")
        BuiltIn().set_test_message(f"M17 audio down to {last_good:.0f} dBm",
                                   append=True)
        return result
