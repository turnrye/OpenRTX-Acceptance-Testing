<!--
SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors

SPDX-License-Identifier: GPL-3.0-or-later
-->

# OpenRTX acceptance testing

Acceptance tests for radios running [OpenRTX](https://github.com/OpenRTX/OpenRTX), on a
bench of software-defined radios. An SDR captures what the radio transmits and measures it; another
SDR transmits test signals into the radio. Until radios can be driven remotely, an operator sets the
radio up, keys it, and answers the questions that need ears.

The tests are [Robot Framework](https://robotframework.org) suites, run with Robot's own tools. This
repository adds a Python library, `oat`, that Robot loads: the keywords that drive the bench, the
measurements, and two Robot modifiers (one fits a suite to the radio, one sanitizes results).

## Safety

- Transmit into attenuators or a dummy load, never into an SDR directly. 5 W into an SDR destroys it.
  Every run checks the radio's full power against the calibrated loss to the receiver, and refuses to
  capture when the receiver could be overloaded.
- The HackRF's RF amplifier stays off unless the bench file asks for it.
- Transmitting on the air needs the licence for that frequency. Keep the radio and the SDR on cables.

## Setup

```sh
uv sync
cp bench/example.yaml bench/mybench.local.yaml              # your instruments
cp calibration/example.yaml calibration/mybench.local.yaml  # your measured RF paths
cp bench/example.args bench/mybench.local.args              # points Robot at both
```

Benches, calibrations and argument files belong to each tester and stay out of git (`*.local.*`). A
calibration lists each RF path's loss by frequency, as points or as a two-port Touchstone (`.s2p`)
export from a VNA, with how and when it was measured. Results record the method, dates and a
fingerprint of the file, never its values.

The SDR backends call `hackrf_transfer` and `rtl_sdr`, so `hackrf` and `librtlsdr` must be installed.

## Running

```sh
# The keywords on a simulated bench, without hardware
uv run robot -d runs/selftest acceptance/selftest.robot

# FM bandwidth acceptance on a real bench: the transmit tests, on every band
uv run robot -A bench/mybench.local.args -v FIRMWARE:v0.4.5-88-g1ceed9f8 \
    -d runs/bandwidth-tx --include tx acceptance/fm/bandwidth.robot
```

The argument file (`-A`) carries the bench, calibration and radio, and `oat.expand`. `FIRMWARE`
names what the radio runs, ideally a `git describe` of its build. All four are mandatory: a run
without them stops before any instrument starts. Tags select what runs: `--include tx`,
`--include rx`, `--include band:2m`, `--exclude mode:m17`.

Before anything starts, the run shows its plan: the radio and firmware, the bench, each band's
frequency and calibrated losses, which way the attenuator chain must face, and the tests to run.

### One SDR or two

A HackRF can both capture the radio and transmit into it, so with `type: hackrf` for both the
receiver and the transmitter (`bench/example.yaml` shows how), the attenuator chain stays between the
radio and the HackRF for the whole run. With separate instruments, such as an RTL-SDR receiving and
a HackRF transmitting, the transmit tests on every band run first, then a prompt asks for the chain
to move, then the receive tests run.

Each run writes Robot's `report.html`, `log.html` and `output.xml` to the output directory, with the
firmware, radio, station and calibration as suite metadata. Next to them, `bench-<suite>/` holds the
raw captures, the WAV audio of each transmission and `measurements.json`, with every measurement,
limit check and operator answer. The log shows each capture's measurements as a table with a player
for each transmission's audio.

### Radios and their capabilities

A radio file (`radios/`) says what the radio has: its `bands`, each with the frequency tests use,
its `modes`, and the limits tests check. Tests say what they need with tags: `mode:fm`, `mode:m17`,
and `per-band` for tests to repeat on every band. `oat.expand`, the pre-run modifier in the argument
file, fits the suite to the radio: each per-band test is repeated per band, named for it
(`2m: FM TX At 25 kHz ...`, tagged `band:2m`), and tests needing a mode the radio lacks are left
out. Transmit tests (`tx`) on every band come before receive tests (`rx`), grouped by band within
each.

### Operator prompts

With `operator: {type: console}` in the bench file, prompts appear in the terminal running the
tests. With `type: file`, a run writes each prompt to `<directory>/prompt.json` and waits for
`<directory>/answer-<id>.txt`, so someone else can answer it: `echo yes > runs/operator/answer-3.txt`,
or `no: crackles on peaks`.

### Stock firmware as a reference

A suite runs the same way on any firmware: flash the stock firmware and run it with
`-v FIRMWARE:stock-1.2 --exclude mode:m17`. The results website compares runs side by side.
Stock firmware names its power settings differently from OpenRTX; pass them lowest first with
`-v "POWER_SETTINGS:Low,High"`.

### Transmit power

The power test keys up once at each of the radio file's `power_settings` and checks that each
transmits at least `power_min_step_db` above the one before, as received through the bench. The
levels are relative, so no power meter is needed, and the test fails if the receiver clipped,
which would hide the higher settings: add attenuation if it does.

### Measuring a recording

```sh
uv run python -m oat.analysis capture.cu8 --rate 250000 --offset 75000 --audio-dir audio/
```

lists each transmission's carrier offset, peak and RMS deviation, dominant tone, 99% occupied
bandwidth, and CTCSS tone and deviation.

## Sharing results

Runs stay local. To publish one as evidence (a release's acceptance run, or the run behind a merged
feature), add it to `results/` and open a pull request:

```sh
scripts/export-run.sh runs/bandwidth-tx
```

It writes `results/<firmware sha>/<radio>/<start time>-<station>-<suite>/` with the run's `output.xml`, passed
through Robot's `rebot` with the `oat.sanitize` pre-rebot modifier, and its `measurements.json`. It
refuses to overwrite a run, and removes the export again if an absolute path survived, so nothing
from your machine reaches a commit. See [results/README.md](results/README.md).

### The results website

GitHub Pages publishes `results/` on every push to `main` (`.github/workflows/pages.yml`), with the
standard tools: Robot's `rebot` writes each run's `log.html` and `report.html`, and
[robotframework-dashboard](https://marketsquare.github.io/robotframework-dashboard/) is the front
page, with every run grouped by radio, filterable by firmware, radio and the suite metadata, and
linking to each run's log. Each log links to its suite on GitHub as it was when the run was made,
and to that commit (`oat.sanitize.SourceLink`); a run made from a commit that was never pushed
links to the suite on `main` instead, so push before you run.

## Layout

| Path | What |
|---|---|
| `acceptance/` | Robot suites, by feature |
| `bench/` | Bench files and Robot argument files. `*.local.*` stays out of git |
| `calibration/` | RF path losses per bench. `*.local.*` stays out of git |
| `radios/` | Radio files: bands, modes, limits |
| `results/` | Published evidence runs, append-only |
| `scripts/export-run.sh` | Adds a run to `results/` |
| `src/oat/` | The Robot library: SDR backends, signals, analysis, operator, keywords, modifiers |
| `tests/` | Unit tests of the library (`uv run pytest`) |
| `docs/DESIGN.md` | Goals and how the pieces fit |
