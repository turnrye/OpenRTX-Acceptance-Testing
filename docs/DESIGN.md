<!--
SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Design

## Goal

Every OpenRTX feature that reaches a radio's RF or audio gets an acceptance test that runs on a
bench: a radio, SDRs, attenuators, and a computer. The tests start manual where they must be and
become automatic as radios learn to be driven and to report on themselves. The same suites should
grow into a compliance report: measurements against the regulatory limits, per radio, per release.

## Pieces

- **Suites** (`acceptance/`) are Robot Framework files that read as the test procedure: what to set
  up, what to send, what to measure, what must hold. They hold no measurement code.
- **Keywords** (`src/oat/robot_library.py`) connect a suite to the bench. Each run records
  captures, measurements, limit checks and operator answers in `measurements.json`, next to Robot's
  own log, and the bench details as suite metadata.
- **Instruments** (`src/oat/sdr/`) hide each SDR behind two interfaces: a receiver that records IQ
  files, a transmitter that plays them. HackRF and RTL-SDR go through their command line tools;
  `sim` and `replay` stand in for hardware in self-tests and for re-analysing recordings. The
  simulated receiver and transmitter share a clock, so a loopback through the bench can be timed
  as on real hardware. A LibreVNA, through LibreVNA-GUI's SCPI server, measures the bench itself
  (`oat.calibrate`).
- **Audio** (`audio.py`) drives a USB sound interface on the radio's accessory connector through
  ALSA's tools, and measures level, SINAD, THD+N, response and dropouts, on the speaker or on the
  audio demodulated from a transmission. A simulated interface and radio stand in for it.
- **M17** (`src/oat/m17/`) builds and decodes M17 frames (`codec`), generates and measures the
  4FSK signal (`phy`), and wraps Codec2 and m17-demod where they are installed (`tools`). The codec
  is checked against vectors generated from libm17.
- **Signals and analysis** (`signals.py`, `analysis.py`) generate test signals and measure
  captures: transmissions found by their power envelope, then carrier offset, peak and RMS deviation,
  dominant tone, 99% occupied bandwidth and received level. Every capture also records how much
  of it hit the receiver's full scale, since a clipped capture reads levels low. Unit tests check one against the other.
- **The operator** (`operator.py`) is the person at the bench. Instructions wait until done;
  confirmations are assertions that fail the test on "no". Prompts go to a terminal, or through files
  so a run without a terminal can be relayed.
- **Bench, calibration and radio files** (`bench/`, `calibration/`, `radios/`) keep the setup and
  the limits out of the suites. Each tester's equipment and its losses differ, so benches and
  calibrations are local and passed in at run time, as station and environment files are in OpenHTF
  and labgrid. A calibration is a loss table per RF path, from points or a VNA's Touchstone export,
  as RF test systems keep external attenuation tables. The radio's full power and the calibrated loss
  are checked before any capture. A calibration may also hold the instruments' levels (the
  transmitter's output per gain setting, the receiver's full scale in dBm), their reference errors,
  and the audio interface's levels; results name which sections it has, never their values.
- **Comparison** (`compare.py`) lays the runs of several firmware states side by side, as the
  evidence for a change that must leave a radio as good as it was.
- **No command of its own.** Everything runs through Robot's tools: `robot` with a bench's
  argument file, `rebot` with `oat.sanitize` to publish a run (`scripts/export-run.sh`), and, in the
  Pages workflow only, `rebot` and robotframework-dashboard to build the results website. Evidence
  runs are checked into `results/`, append-only and checked in CI.

## Roadmap

1. **Now.** The operator drives the radio. Transmit is measured automatically; receive is judged by
   ear. First suite: FM channel bandwidth (`acceptance/fm/bandwidth.robot`).
2. **Audio injection, then radio control.** Done: transmit audio injected electrically into the mic
   pin replaces the acoustic tone, and the speaker is recorded, through a USB sound interface on
   the accessory connector (`fm/audio.robot`, `m17/`); an AIOC keys the radio too. Next, a CAT or
   rtxlink connection lets keywords set frequency, bandwidth and mode, replacing most operator
   instructions. Where one connector carries both, as the C62's programming port does, injection
   and CAT can't run together: tests with injected audio keep PTT on the interface or the operator.
3. **Readback.** Debug functions that report the radio's received audio, RSSI and decoder state turn
   the receive confirmations into measurements.
4. **Calibrated levels.** Started: a calibration can hold the transmitter's and receivers' levels
   and the instruments' reference errors, measured with a LibreVNA (`oat.calibrate`), which make
   sensitivity, absolute power and frequency error measurable; the bench self-test checks the chain
   with physical references. Next: spurious emission tests against the regulatory limits.
5. **More radios and modes.** M17 is measured and generated (`m17/`); APRS and DMR where
   supported, one radio file per target.

## Conventions

- Limits live in radio files, never in suites, and name their source.
- A test either measures or asks; it never guesses a pass.
- Tests that depend on absolute levels (sensitivity, power, spurious emissions) will need a measured
  calibration, not an assumed one; relative tests such as deviation, bandwidth and the power
  steps do not.
- Tests that need the audio interface are tagged `needs:audio`, and those that need absolute
  levels `needs:abs-level`, so a bench without them excludes them.
- Every run names the firmware, the unit and, for radios with a separately flashed DSP, the DSP
  image, so runs of different firmware states can't be confused.
- Suites tag their tests `tx`, `rx` and `manual`, so a run can pick what the bench can do, and
  declare what they need from the radio: `mode:<mode>` per operating mode, `per-band` to repeat on
  every band. `oat.expand` fits a suite to the radio file before it runs; tests never name a
  frequency or a band themselves.
- Each test opens by stating the full radio setup it needs, so it stands alone when tags select a
  subset of the suite.
