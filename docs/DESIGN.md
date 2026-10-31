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
  `sim` and `replay` stand in for hardware in self-tests and for re-analysing recordings.
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
  are checked before any capture.
- **No command of its own.** Everything runs through Robot's tools: `robot` with a bench's
  argument file, `rebot` with `oat.sanitize` to publish a run (`scripts/export-run.sh`), and, in the
  Pages workflow only, `rebot` and robotframework-dashboard to build the results website. Evidence
  runs are checked into `results/`, append-only and checked in CI.

## Roadmap

1. **Now.** The operator drives the radio. Transmit is measured automatically; receive is judged by
   ear. First suite: FM channel bandwidth (`acceptance/fm/bandwidth.robot`).
2. **Radio control and audio injection.** A CAT or rtxlink connection over USB or the programming
   port lets keywords set frequency, bandwidth and mode and key the transmitter, replacing most
   operator instructions. Transmit audio injected electrically into the mic pin replaces the
   acoustic tone, for repeatable levels. Where the same connector carries both, as the C62's
   programming port does, injection and CAT can't run together: tests with injected audio keep the
   operator on PTT.
3. **Readback.** Debug functions that report the radio's received audio, RSSI and decoder state turn
   the receive confirmations into measurements.
4. **Calibrated levels.** A measured RF path (VNA or power meter) turns relative levels into dBm, for
   sensitivity, power and spurious emission tests against the regulatory limits.
5. **More radios and modes.** M17, APRS, DMR where supported, one radio file per target.

## Conventions

- Limits live in radio files, never in suites, and name their source.
- A test either measures or asks; it never guesses a pass.
- Tests that depend on absolute levels (sensitivity, power, spurious emissions) will need a measured
  calibration, not an assumed one; relative tests such as deviation, bandwidth and the power
  steps do not.
- Suites tag their tests `tx`, `rx` and `manual`, so a run can pick what the bench can do, and
  declare what they need from the radio: `mode:<mode>` per operating mode, `per-band` to repeat on
  every band. `oat.expand` fits a suite to the radio file before it runs; tests never name a
  frequency or a band themselves.
- Each test opens by stating the full radio setup it needs, so it stands alone when tags select a
  subset of the suite.
