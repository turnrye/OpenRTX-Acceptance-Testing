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
export from a VNA, with how and when it was measured. Results record the method, dates, which
optional sections the file has and a fingerprint of the file, never its values.

The losses are enough for relative tests. A calibration can also hold, each optional:

- `levels.transmitter`: the transmitter's output in dBm against its gain setting, at a few
  frequencies. With the `from_transmitter` loss, keywords transmit at a level in dBm at the radio's
  antenna port (`level_dbm=`), and fail clearly when it's out of reach.
- `levels.receiver`: per receiver and gain setting, the input level that reads full scale. Captures
  then report each transmission's power in dBm, at the receiver and at the radio
  (`Radio Power In dBm`).
- `frequency_correction_ppm`: each instrument's reference error. Carrier offsets and frequency errors
  are corrected with it, and transmitted signals moved by it. At 446 MHz one ppm is 446 Hz.
- `paths.audio_out` and `paths.audio_in`: the voltage of a full-scale tone on the audio interface,
  for audio levels in millivolts.

Tests that need them are tagged `needs:abs-level` or `needs:audio`; a bench without them excludes
those tags. `calibration/example.yaml` shows every section.

The SDR backends call `hackrf_transfer` and `rtl_sdr`, so `hackrf` and `librtlsdr` must be installed.
The audio interface goes through ALSA's `aplay` and `arecord` (`alsa-utils`). For M17 voice,
Codec2's `c2enc` and `c2dec` (the `codec2` package) encode generated payloads and decode the
radio's; without them, tests that need voice skip and the rest still run. `m17-demod` from
m17-cxx-demod, when on the PATH, decodes each M17 capture a second time, independently.

### Measuring a calibration

`python -m oat.calibrate` measures a calibration with a LibreVNA, through LibreVNA-GUI's SCPI
server (port 19542; `LibreVNA-GUI --no-gui` runs it without a window). It walks through five
steps: C1 sweeps both RF paths into `.s2p` files, C2 reads the transmitter's output at each gain
on the spectrum analyser, C3 feeds the receiver a known carrier for its full-scale level, C4
measures the receiver's reference error against the generator or a known carrier, and C5 records
the audio voltages measured by hand. It writes `calibration/<station>.local.yaml` and keeps the
sections not measured again:

```sh
uv run python -m oat.calibrate --bench bench/mybench.local.yaml --radio radios/c62.yaml \
    --steps C2,C3 --pad-db 30
```

The LibreVNA's command names come from its GUI; their argument and reply formats are marked
UNVERIFIED in `src/oat/sdr/librevna.py` until checked against the hardware.

## Running

```sh
# The keywords on a simulated bench, without hardware
uv run robot -d runs/selftest acceptance/selftest.robot

# The bench measuring itself through the attenuator chain, no radio connected
uv run robot -A bench/mybench.local.args -v FIRMWARE:v0.4.5-88-g1ceed9f8 \
    -v "HARDWARE:Retevis C62 2501R0621100413" -v DSP:9579b617 \
    -d runs/selftest-bench acceptance/selftest-bench.robot

# FM bandwidth acceptance on a real bench: the transmit tests, on every band
uv run robot -A bench/mybench.local.args -v FIRMWARE:v0.4.5-88-g1ceed9f8 \
    -v "HARDWARE:Retevis C62 2501R0621100413" -v DSP:9579b617 \
    -d runs/bandwidth-tx --include tx acceptance/fm/bandwidth.robot
```

The argument file (`-A`) carries the bench, calibration and radio, and `oat.expand`. `FIRMWARE`
names what the radio runs, ideally a `git describe` of its build, and `HARDWARE` the unit, by model
and serial number: units of one model measure differently. All five are mandatory: a run without
them stops before any instrument starts. Radios whose audio runs through a separately flashed DSP,
such as the C62 and C2 (`dsp_image: true` in the radio file), also need `DSP`, the image's sha256
or enough of it to tell images apart: two firmware states can differ in their DSP image alone. On
the C62, for example, the 16 kHz image starts `7dd697c9` and the 48 kHz one `9579b617`; factory
firmware is `-v DSP:stock`. Tags select what runs: `--include tx`, `--include rx`,
`--include band:2m`, `--exclude mode:m17`, and `--exclude needs:audio` or
`--exclude needs:abs-level` on a bench without an audio interface or absolute level calibration.
On a bench of SDRs alone, add `-A bench/sdr-only.args` after the bench's own argument file: it
excludes `needs:audio` and adds a `Profile` to the suite metadata, so a published run shows that its
audio tests were left out on purpose.

### The suites

| Suite | What it covers |
|---|---|
| `selftest.robot` | The keywords, on the simulated bench |
| `selftest-bench.robot` | The bench through its attenuator chain, no radio: the deviation scale at the Bessel null, a known FM deviation and a known M17 stream read back, and their spread over five repeats, which sets the tolerance for comparing firmware states. Needs separate receiver and transmitter. Run it at the start of every evidence session |
| `fm/bandwidth.robot` | FM deviation, bandwidth, CTCSS and power steps at both bandwidths, adjacent channels, and FM restored after M17 |
| `audio-levels.robot` | Setting up an audio interface: the clean mic level range, and hum and noise. Not evidence |
| `fm/audio.robot` | FM audio through the audio interface: mic sensitivity, response, distortion, speech deviation, mic level after cold boots, 12 dB SINAD sensitivity, receive response and dropouts |
| `m17/tx.robot` | M17 transmit: deviations and ratio, symbol rate, carrier, bandwidth, EVM, decode rate, preamble, long transmissions, power, Codec2 input level and aliasing |
| `m17/rx.robot` | M17 receive of a generated stream: callsign, Codec2 tone, sensitivity, carrier offset, speaker images |

On `bench/sim.yaml` the simulated radio transmits what the audio interface plays into its mic,
plays at its speaker what the simulated transmitter sends, and the transmitter loops back into
the receiver, so every suite except `m17/tx.robot` runs without hardware.

Before anything starts, the run shows its plan: the radio, its unit and firmware, the bench, each band's
frequency and calibrated losses, which way the attenuator chain must face, and the tests to run.

### One SDR or two

A HackRF can both capture the radio and transmit into it, so with `type: hackrf` for both the
receiver and the transmitter (`bench/example.yaml` shows how), the attenuator chain stays between the
radio and the HackRF for the whole run. With separate instruments, such as an RTL-SDR receiving and
a HackRF transmitting, the transmit tests on every band run first, then a prompt asks for the chain
to move, then the receive tests run.

Each run writes Robot's `report.html`, `log.html` and `output.xml` to the output directory, with the
firmware, radio, unit, station and calibration as suite metadata. Next to them, `bench-<suite>/` holds the
raw captures, the WAV audio of each transmission and `measurements.json`, with every measurement,
limit check and operator answer. The log shows each capture's measurements as a table with a player
for each transmission's audio.

### Radios and their capabilities

A radio file (`radios/`) says what the radio has: its `bands`, each with the frequency tests use,
its `modes`, and the limits tests check. Tests say what they need with tags: `mode:fm`, `mode:m17`,
and `per-band` for tests to repeat on every band. `oat.expand`, the pre-run modifier in the argument
file, fits the suite to the radio: each per-band test is repeated per band, named for it
(`2m: FM TX At 25 kHz ...`, tagged `band:2m`), and tests needing a mode the radio lacks are left
out. A band may list its own `modes`, as `radios/c2.yaml` keeps GMRS to FM; per-band tests then
run only on the bands with their modes. Transmit tests (`tx`) on every band come before receive tests (`rx`), grouped by band within
each.

### Operator prompts

With `operator: {type: console}` in the bench file, prompts appear in the terminal running the
tests. With `type: file`, a run writes each prompt to `<directory>/prompt.json` and waits for
`<directory>/answer-<id>.txt`, so someone else can answer it: `echo yes > runs/operator/answer-3.txt`,
or `no: crackles on peaks`.

### Stock firmware as a reference

A suite runs the same way on any firmware: flash the stock firmware and run it with
`-v FIRMWARE:stock-1.2 -v DSP:stock --exclude mode:m17`; don't assume stock firmware has M17. The
results website compares runs side by side. Stock firmware names its power settings differently
from OpenRTX; pass them lowest first with `-v "POWER_SETTINGS:Low,High"`.

To compare firmware states in a pull request, print their runs side by side:

```sh
uv run python -m oat.compare runs/stock runs/before runs/after
```

gives a Markdown table per suite, a row per measurement and a column per run, headed by its
firmware, DSP image and date. `--all` shows every number recorded.

### The audio interface

A USB sound interface on the radio's accessory connector replaces the acoustic tone and the ear:
an AIOC, or a CM108 dongle with a resistive pad to the mic pin and a loaded speaker tap. Its
`audio` section in the bench file names the ALSA devices, and the serial port and line that key the
radio: DTR on an AIOC, RTS on a Digirig Mobile (`bench/example.yaml`). Without a PTT line the
operator keys on request.
Keywords inject tones, tone sequences and WAV files, record the speaker, and measure level, SINAD
and THD+N (optionally psophometrically weighted), response and dropouts. With `paths.audio_out`
and `paths.audio_in` calibrated, levels read in millivolts.

On a new interface or mixer setting, run `acceptance/audio-levels.robot` first. It steps a tone up
in level and prints the deviation and THD+N at each step, plus the hum and noise with the mic input
silent, so you can find the clean range and compare the hum with and without an isolator. Its runs
aren't evidence, so keep them local.

A desktop sound server can mute or change an interface during a run. Turn its profile off with
`wpctl status` to find the device's id, then `wpctl set-profile <id> 0`, and set the levels in the
bench file's `mixer`.

### M17

M17 captures are measured from the IQ: FM demodulation at 48 kHz, the receive RRC filter, symbol
timing, then the outer and inner deviation and their ratio, EVM, symbol rate error, carrier,
bandwidth, preamble and polarity. Every frame is decoded in Python (LSF with its CRC and callsign,
LICH, stream frames, frame numbers, EOT), so no outside decoder is needed; the encoder and decoder
are checked symbol for symbol against libm17 (`scripts/m17-reference-vectors.py`). The bench
generates M17 streams the same way, with a Codec2 tone, a WAV file or random payload, at any
deviation, offset or polarity. Limits are the radio file's `m17` block.

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
bandwidth, and CTCSS tone and deviation. `oat.m17.phy.measure_m17_capture` does the same for M17.

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
| `scripts/m17-reference-vectors.py` | Writes the M17 test vectors from a libm17 build |
| `src/oat/` | The Robot library: SDR and VNA backends, signals, FM and M17 analysis, audio, operator, keywords, modifiers, calibration and comparison tools |
| `tests/` | Unit tests of the library (`uv run pytest`) |
| `docs/DESIGN.md` | Goals and how the pieces fit |
