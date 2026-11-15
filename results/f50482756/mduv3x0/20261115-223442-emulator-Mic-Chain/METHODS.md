<!--
SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors

SPDX-License-Identifier: GPL-3.0-or-later
-->

# M17 microphone chain on the emulator: methods

`acceptance/m17/mic-chain.robot` measures what OpenRTX's M17 transmit audio chain does to a
microphone signal, on the **emulator bench** (`bench/emulator.yaml`): instead of a radio and SDRs,
an OpenRTX Linux emulator build of the firmware under test, with a chosen radio's microphone chain
modelled in front of it. Each run is one firmware variant (a set of C defines) for one radio
model; `scripts/emu-mic-campaign.py` runs the suite once per variant and compares them. It is
evidence for a change to the firmware's signal processing, to be confirmed on a real bench
(`acceptance/m17/tx.robot`) for everything the emulator cannot model.

The emulator bench is a bench like `bench/sim.yaml`: it stands in for hardware, and its runs are
ordinary Robot runs with `measurements.json`, published under `results/` like any other. They
are told apart from radio measurements by their station, `emulator`, their hardware, which always
starts "emulated", and their calibration method, `model`.

## The bench

```
recipe ─► float signal ─► ADC model ─► mic.raw ─► openrtx_linux ─► tx.raw ─► FM modulator ─► IQ
                         (adc_quantize)          (encodeFunc,      (48 kHz        (OAT, nominal
                                                  codec2, M17       baseband)      deviation)
                                                  modulator)
IQ ─► OAT M17 decoder (oat.m17.phy) ─► codec2 3200 frames ─► c2dec ─► voice.wav ─► metrics

openrtx_linux: encodeFunc ─► samples codec2 encodes ─► precodec.raw (tap) ─► precodec.wav ─► metrics
```

1. **Signal.** Each test signal is a recipe (a JSON object in `run.json`): a tone, a speech file at
   a peak level, a level step or a gate pattern. `oat.emu_campaign.synth` turns it into floats at
   the ADC's sample rate (8 kHz times the radio's oversampling: 48 kHz for the CS7000-PLUS),
   where 1.0 is the ADC's half range, and appends 0.5 s of silence.
2. **ADC model** (`oat.emu.adc_quantize`). The floats are multiplied by the half range (2048 codes
   for a 12-bit converter), white Gaussian noise of the radio's RMS in LSB is added (seeded), and
   the result is rounded, offset to mid-scale (2048) and clipped to the converter's range (0 to
   4095); without a bias, as for 16-bit signed sources, it is clipped around zero. These are the int16 samples the firmware's `encodeFunc` would
   read from the ADC's DMA buffer.
3. **Firmware.** `openrtx_linux` is built per variant with meson (`-Dsystem_codec2=false`, so the
   firmware's own codec2), with `-Dc_args=` set to the radio's `CONFIG_MIC_GAIN` and
   `CONFIG_MIC_OVERSAMPLE` plus the variant's defines, each in its own build directory
   (`build_emu_<radio file>_<variant>`). The emulator reads the microphone from `OPENRTX_MIC_FILE`
   (raw s16le at the stream's rate, from the start of the file at every key-up, silence after it
   ends) and writes the M17 modulator's output to `OPENRTX_M17_TX_BASEBAND`. A fresh emulator,
   with its own state directory and no display or audio device, is driven from its shell: switch
   the VFO to M17 (`keycombo MONI 5`), key for the length of the microphone file, unkey, quit. So
   the signal passes through the real `encodeFunc` (decimator, DC block, gain, and any
   experimental processing), the real codec2 3200 encoder and the real M17 framing and modulator,
   in real time.

   **The pre-codec tap.** With `OPENRTX_MIC_PRECODEC_FILE` set (Linux builds of the experiment
   branch), the firmware appends every 160-sample block it passes to `codec2_encode` to that file
   as raw s16le: exactly what codec2 sees, at 8 kHz. `emu.transmit` sets it to `precodec.raw` in
   the take's directory. The tap writes only while the encoder runs, so the overs of a session are
   back to back; `emu.split_overs` cuts them apart by the decoded stream frames (two codec2 blocks
   per M17 frame), sharing out the block or so per over that the encoder runs beyond the frames
   sent. Each over's tap is kept as `precodec.wav` (`precodec-2.wav` for the second).
4. **Decoder.** The baseband is FM-modulated to IQ at nominal deviation (outer symbols ±2.4 kHz),
   then decoded by OAT's M17 decoder, the same code that measures a real radio's transmissions,
   and the codec2 payload is decoded with Codec2's `c2dec` (3200 mode). Each over keyed gives one
   decoded transmission; its LSF CRC and frame count are kept with the take.
5. **Metrics** are computed from the decoded audio (8 kHz), from the pre-codec tap (the same
   metrics, prefixed `pre_`, without STOI) and from the recipe. The tap shows what each variant
   does to the level without codec2's own artefacts; the decoded audio shows what a listener
   gets. They are recomputed on
   every invocation from the saved `voice.wav`, so the emulator only runs again when the binary or
   the input changes.

Every take is a fresh boot of the emulator, so every take measures the **first transmission after
power-on**. That matters for state the firmware keeps between transmissions: the AGC's gain
persists (it is static) and starts at 0 dB with a 1 s fast-acquisition phase after boot. The level
step test therefore keys its signal twice in one session (`--step-overs 2`) and reports both overs:
the first is the cold start, the second the steady-state case, starting from the gain the first
left behind.

### The radio's mic chain model

Each radio file that can be emulated has a `mic_chain` section: the firmware's `CONFIG_MIC_*`
values (oversampling, gain), the ADC (bits, a bias at mid-scale or none, noise in LSB RMS), each
with its `sources` (the `hwconfig.h` in the OpenRTX tree for the firmware values; "assumed until
bench-measured" for noise and bias), and the suite's `limits`. Files: `cs7000p` (6, 32),
`cs7000` (8, 32), `mduv3x0` (8, 32), `md3x0` (8, 32), `mod17` (1, 12), `c62` (1, 13; its CSK6
codec decimates before the firmware sees the audio, so the model is approximate), and
`emulator-bare`: no model at all, the Linux build's defaults (oversampling 1, gain 1) fed clean
signed 16-bit samples with no bias or noise, to measure the firmware's DSP on clean input.
`calibration/emulator.yaml` (method `model`) stands for the calibration: the emulator has no RF
or audio paths, and the model it relies on is the radio file's.

### What the ADC model assumes

- No analog front end: no microphone, preamp, filter or ADC frequency response. The ADC sees the
  test signal exactly.
- The bias sits exactly at mid-scale and does not move. A real bias is off-centre, and the DC block
  then has more to remove at key-up: the key-up click depends on it.
- The noise is white, Gaussian, of a fixed RMS in LSB, independent of the signal. Real converters
  add hum, interference and nonlinearity.
- Levels are given as **dBFS of the ADC range**: a peak at -20 dBFS is 0.1 of the half range,
  205 codes from the bias. Noise bed levels are RMS against the same half range (a full-scale sine
  is -3 dBFS RMS). How loud real speech arrives at the ADC is a bench question; the sweeps span a
  wide range so that the answer can be read off them once it is known.

## Variants

A variant is a name and a list of C defines (`VARIANTS` in `src/oat/emu_campaign.py`), added to
the radio's c_args. The firmware branch defines what each does:

| Variant | Defines | Change |
|---|---|---|
| baseline | none | the chain as merged |
| prime_dc | `MIC_EXP_PRIME_DC` | the DC block starts settled on the first sample: no key-up click |
| keep_sum | `MIC_EXP_KEEP_SUM` | the decimator keeps the sum of the oversampled block, the resolution averaging gains |
| hpf | `MIC_EXP_HPF` | a 2nd-order high-pass at about 110 Hz |
| agc | `MIC_EXP_AGC` | AGC to -21 dBFS with an adaptive silence gate and a soft limiter at -1 dBFS |
| full | all four above | the combination |
| agc_slow | `MIC_EXP_AGC`, `MIC_EXP_AGC_FASTDOWN=0.0f`, `MIC_EXP_AGC_ATTACK=0.1f` | AGC without fast gain reduction, slow attack |
| agc_mid | `MIC_EXP_AGC`, `MIC_EXP_AGC_FASTDOWN=0.0f`, `MIC_EXP_AGC_ATTACK=0.3f`, `MIC_EXP_AGC_KNEE=-3.0f` | between the two, with a softer limiter knee |

Each single change is compared with baseline on its own, and `full` shows the combination. The
two AGC tunings are read alongside `agc`: hand sweeps showed the default AGC losing about 0.025
STOI (male speech at -20 and -6 dBFS) to gain pumping at syllable rate, while the slow tunings
avoid that but overshoot to about -14 dBFS active and lose up to 0.09 STOI in the limiter (female
speech at -6 dBFS). The level step (attack, overshoot, release) and the STOI sweep are there to
show that trade-off.

With `MIC_EXP_HPF` or `MIC_EXP_AGC` the firmware runs a float chain in which the fixed gain no
longer saturates. The pre-codec clip is measured at the tap for every variant; the old model of
the integer chain is kept only as a cross-check, and left out for the float chain.

## Test signals

| Test | Signal | Levels |
|---|---|---|
| `tones` | 1 kHz and 400 Hz sine, 2 s | peak -50 to -2 dBFS in 3 dB steps, and 0 dBFS |
| `speech` | four Harvard-sentence recordings, first 12 s | peak -40, -30, -20, -12, -6, -1 dBFS |
| `step` | a sustained synthetic vowel: 3 s at -30, 3 s at -10, 6 s at -30 dBFS peak, keyed twice in one session | |
| `gate` | the first 9 s of the first speech file as three 3 s bursts at -12 dBFS peak, each followed by a 4 s pause, over a pink noise bed | noise bed -55 and -40 dBFS RMS |

The key-up onset is read from the speech takes, and the listening-condition test re-scores
them (below): neither needs emulator runs of its own.

- **Speech**: the Open Speech Repository's American English Harvard sentences,
  `OSR_us_000_0010_8k.wav` and `OSR_us_000_0011_8k.wav` (male), `OSR_us_000_0030_8k.wav` and
  `OSR_us_000_0031_8k.wav` (female), 8 kHz,
  <https://www.voiptroubleshooter.com/open_speech/american.html>, offered by the repository free
  for any use. They are not kept in this repository: download them into a directory and pass it as
  `--speech-dir`. `run.json` records each file's SHA-256 and the campaign refuses a file that
  differs from a recipe's.
- **Vowel** (`emu_campaign.vowel`): harmonics of 140 Hz up to 3.6 kHz, shaped by formants at
  700, 1220 and 2600 Hz, with seeded phases. Codec2 codes voiced speech as harmonics, so it passes
  a vowel much closer to unchanged than a pure tone, which makes the vowel the better probe of
  level dynamics.
- **Pink noise** (`emu_campaign.colored_noise`): seeded Gaussian noise shaped to 1/f power.

The microphone inputs are not stored. Each is regenerated bit for bit from its recipe in
`run.json`, the ADC noise seed (17), the seeds in the recipe, the radio's model and, for speech, the
source file (`emu_campaign.mic_input`). Each take's `take.json` records the SHA-256 of the exact
samples sent, so a regenerated input can be checked against it. `--keep-raw` keeps each take's
`mic.raw` and the emulator's `tx.raw`.

## References

Codec2 changes what it codes: it loses about 13 dB on a 1 kHz tone, and its intelligibility has a
ceiling of its own. Every firmware result is therefore read against the same input sent straight
through codec2 (the system's `c2enc` and `c2dec`, 3200 mode), so that the firmware's own effect is
the difference:

- **codec-only (nominal)**: the same ADC samples through an ideal linear chain at the radio's
  fixed gain (bias removed, decimated with a proper anti-alias filter, multiplied by the gain,
  clipped at full scale), then codec2. What a perfect implementation of today's fixed-gain chain
  would give. Every test has it.
- **codec-only (ideal)**: the same ADC samples through the ideal chain, unclipped and set to
  -21 dBFS active level, then codec2. The best any firmware could do with that ADC input. Speech
  only; the STOI deficit is measured against it.
- **codec-only (clean)**: the clean source file at -21 dBFS active level, then codec2, without
  the ADC. The codec's own ceiling. Speech only.

## Metrics

Levels are dBFS of the decoded audio or of the tap, where 1.0 is int16 full scale; RMS is plain
RMS (a full-scale sine reads -3 dBFS). Every output leaves out its last 0.2 s. Each metric below
is computed on both streams: plain names are after codec2, `pre_` names before it. For the
codec-only references, the pre-codec stream is the int16 signal handed to `c2enc`.

- **Output RMS** (tones): the RMS from 0.5 s into the tone to 0.3 s before its end.
- **Active speech level**: a simplified ITU-T P.56. The audio is cut into 20 ms frames; frames
  within 30 dB of the loudest frame and above -60 dBFS are active; the level is the RMS over the
  active frames. **Spread**, the headline level metric, is the maximum minus the minimum, over the
  input levels, of the active level averaged over the speech files: 0 dB means the level does not
  depend on how loud the speaker is. It is given before codec2 (the cleanest view of a level
  control) and after it.
- **Peak**: the largest sample magnitude, leaving out the first 100 ms.
- **Onset peak** (key-up click): the largest sample magnitude in the first 100 ms. The test
  signals all start quietly, so a loud onset is the firmware's own transient. Codec2 smooths it,
  so the pre-codec onset is the larger and the truer one (baseline's DC block start reaches full
  scale at the tap).
- **Floor**: the 10th percentile of the 20 ms frame levels.
- **Pre-codec clip** (`pre_clip_fraction`): the fraction of the samples codec2 receives that sit
  at int16 full scale (|x| ≥ 32767), measured at the tap, over the measured span (speech: the
  over after its first 100 ms, which the onset figures cover; tones: the steady window; step and gate: the aligned signal).
  `model_clip_fraction` is the earlier model of the integer chain applied to the input
  (`emu.chain_clip_fraction`: average each block of oversampled codes, remove the mean, multiply
  by the gain, count the magnitudes at full scale), kept as a cross-check for baseline, prime_dc
  and keep_sum and left blank for the float chain.
- **Post-codec clip**: the fraction of decoded samples at full scale.
- **STOI**: short-time objective intelligibility (pystoi, not extended) of the decoded speech
  against the clean source, both at 8 kHz, after alignment. A vocoder does not keep the waveform,
  so the alignment is the lag maximising the cross-correlation of 2.5 ms log-energy envelopes
  (floored 40 dB below their peak, mean removed), searched within ±0.5 s (`emu.find_lag`).
  **STOI deficit** is codec-only (ideal) STOI minus the variant's, per take, averaged over the
  takes: what the firmware chain loses against the best possible processing of the same ADC input.
- **Envelope**: the RMS level in consecutive 50 ms blocks, aligned to the input by the same
  envelope lag, found against the ideal chain's output, for the output (`step`, `gate`) and the
  tap (`step_precodec`, `gate_precodec`). Kept in `data/envelopes.csv`. The headline attack and
  release come from the pre-codec envelope.
- **Step response** (from the envelope): the settled levels are medians, before the step (the last
  0.9 s of the first segment), in the loud segment (its last 0.9 s) and after it (the last 1.4 s).
  **Rise** is the loud level minus the level before (20 dB for a linear chain). **Attack** is the
  time from the step up until the envelope stays within 2 dB of the loud settled level, and
  **release** the same after the step down, against the level after it; "did not settle" means it
  was still outside the band at the end of the segment. **Overshoot** is the highest level within
  1 s of the step up, above the loud settled level; **dip** is the settled level after the step
  minus the lowest level within 1 s of the step down; **release rate** is the slope, in dB/s, of a
  line fitted to the envelope from that lowest point until it first comes within 2 dB of the
  settled level (or to the end of the segment), so it is given even when the release does not
  settle (blank when the dip is within 2 dB).
- **Listening-condition intelligibility**: STOI normalises the level of each segment it
  compares, so it scores a quiet output exactly as a loud one, and cannot see what a level
  control is for: a talker who arrives at -48 dBFS is, at the fixed volume a receiver is set to,
  buried in whatever noise is around the listener. This test takes each decoded speech take as
  the receiver plays it at a fixed volume: at its own level, the decoded int16 scale standing for
  the listener's level, without any normalisation. It aligns it to the clean source (the same
  envelope lag), adds pink noise at a fixed level (-45 and -35 dBFS RMS of full scale by default,
  `LISTEN_NOISE`; seed 11, the same noise for every take), clips the sum at full scale and scores
  STOI and ESTOI (`pystoi`, extended) against the clean source. The codec-only nominal and ideal
  references are scored under the same conditions; the ideal one is the same input processed to
  -21 dBFS active, what a perfect level control would deliver. At -21 dBFS active the noise
  levels give 24 and 14 dB SNR; a -48 dBFS talker gets -3 and -13 dB. The figures are the mean
  STOI over the speech files in each cell of input level and noise level; the **worst** cell
  (lowest mean) with its input and noise level; and the **worst deficit**, the largest
  difference from the ideal reference in the same cell. The take's own level is kept as
  `listen_active_dbfs`, its SNR at the listener as `listen_snr_db`. It re-scores the speech
  sweep's takes: no emulator runs.
- **Gate**: in the aligned output, the speech level is the active level over the three bursts; the
  **pause level** is the RMS in each pause from 0.3 s after its start to 0.3 s before its end,
  power-averaged over the pauses; **speech/pause** is their difference. **Drift** is the RMS of the
  last second of a pause minus that of its first second (positive: the noise is pumped up as the
  pause goes on), averaged over the pauses, with the worst one; **trend** is the last pause's level
  minus the first's.

## Limits of the suite

The suite checks each test against the radio file's `mic_chain.limits`, all before codec2 except
the STOI deficit. They are the target of the microphone-chain change, not a standard, and
today's fixed-gain chain is expected to miss some:

| Test | Figure | Limit |
|---|---|---|
| Static Level Curve | pre-codec clip, worst tone | 0 % |
| Speech Level Sweep | pre-codec active level spread over the sweep | 6 dB |
| | pre-codec clip after the first 100 ms, worst take | 0 % |
| | STOI deficit against the ideal reference | 0.02 |
| Key-up Onset | pre-codec peak in the first 100 ms, worst take | -30 dBFS |
| AGC Step Response | pre-codec rise for the +20 dB step (second over) | 6 dB |
| | attack | 1 s |
| | overshoot | 6 dB |
| Silence Gate | pre-codec pause drift, worst pause | 3 dB |
| Listening-Condition Intelligibility | worst deficit against the ideal reference, any input and noise level | 0.05 |

Each test also records every take (`mic-take`, shown by `python -m oat.compare --all`) and its
figures (`mic-chain`).

## Running it

Once, the speech files:

```sh
uv sync
mkdir -p "$SPEECH_DIR" && cd "$SPEECH_DIR"
for n in 0010 0011 0030 0031; do
  curl -O "https://www.voiptroubleshooter.com/open_speech/american/OSR_us_000_${n}_8k.wav"
done
```

`c2enc` and `c2dec` (the `codec2` package), meson, ninja and OpenRTX's Linux build dependencies
must be installed. The tester's paths (the OpenRTX checkout, a build root, the speech directory,
the take cache) go in a local copy of the bench, `bench/emulator.local.yaml`, or on the command
line; they never reach the results.

One variant on one radio model, from this repository (`FIRMWARE` is `<variant>@<OpenRTX
commit>`, and must match the checkout):

```sh
uv run robot -A bench/emulator.args -v RADIO:radios/cs7000p.yaml \
    -v VARIANT:baseline -v FIRMWARE:baseline@$(git -C "$OPENRTX" rev-parse --short=9 HEAD) \
    -v OPENRTX:"$OPENRTX" -v SPEECH_DIR:"$SPEECH_DIR" \
    -d runs/mic-cs7000p-baseline acceptance/m17/mic-chain.robot
```

- `-v RADIO:radios/emulator-bare.yaml` runs the bare emulator instead.
- Suite variables make smaller runs: `TONE_FREQS`, `TONE_LEVELS`, `SPEECH_FILES`,
  `SPEECH_LEVELS`, `SPEECH_SECONDS`, `STEP`, `STEP_OVERS`, `GATE_NOISE`; `-t` picks tests.
- `BUILD_ROOT` puts the build directories somewhere other than the checkout; `TAKES` sets the
  take cache (default `runs/emulator-takes/<radio file>`), `REUSE_TAKES` lists other caches to
  reuse (comma separated); `JOBS` sets how many emulators run at once (default 6). Each runs in
  real time: a full run is about 2 minutes of emulator time per job slot per variant.
- A take is reused when a cache holds a decoded take for the same binary SHA-256, input SHA-256
  and number of overs (and its tap, when the firmware wrote one), so an interrupted run resumes
  and a rerun costs nothing. Rebuilt firmware changes the binary and reruns its takes.
- A run always writes its whole output folder, so to add a test to existing runs (such as the
  listening test), run the whole suite again with the same take cache: every take is reused and
  only the scoring runs. `-t` alone would leave a run with only that test.

Every variant, then a report comparing them (each variant is an ordinary run in
`runs/<variant>/` under `--out`):

```sh
uv run python scripts/emu-mic-campaign.py --radio radios/cs7000p.yaml --openrtx "$OPENRTX" \
    --speech-dir "$SPEECH_DIR" --variants baseline prime_dc keep_sum hpf agc full \
    --out runs/mic-cs7000p
uv run python -m oat.compare runs/mic-cs7000p/runs/baseline runs/mic-cs7000p/runs/agc
scripts/export-run.sh runs/mic-cs7000p/runs/agc
```

`--var NAME:VALUE` passes suite variables, `--test` picks tests, `--reuse-takes` adds caches and
`--report-only` rebuilds the comparison from runs already made. Running the campaign again with
the same `--out` (and so the same `takes/`) and firmware reuses every take: that is how a new
test, such as the listening test, is added to a campaign.

## Output

Each run writes, next to Robot's `output.xml` and in its `bench-Mic-Chain/` folder:

```
measurements.json  every take (mic-take), each test's figures (mic-chain) and limit checks,
                   with the emulator's provenance: variant, defines, OpenRTX commit, build,
                   binary SHA-256, the radio's mic chain model and its sources
run.json        provenance: OpenRTX sha, describe, branch; this repository's describe;
                tool versions; the model; the variant with its defines and binary SHA-256;
                every signal recipe; speech file SHA-256; dates
openrtx.patch   the uncommitted OpenRTX changes, when the checkout was dirty
REPORT.md       headline figures and tables per test
METHODS.md      this document
data/           tones.csv, speech.csv, step.csv, gate.csv, listen.csv: one row per take and over
                (variant, profile, test, signal, level, metrics), envelopes.csv,
                headline.csv, rows.json (the same rows, for the campaign's comparison)
charts/         PNG and SVG: level-tones(-precodec), level-speech(-precodec), stoi, listening,
                envelope-step(-precodec), envelope-gate(-precodec), onset, gate
```

`scripts/export-run.sh` publishes all of it under `results/<OpenRTX sha>/<radio file>/
<time>-emulator-Mic-Chain/`. The takes stay in the tester's cache, `<cache>/<variant>/<take>/`:
`take.json` (recipe, binary and input SHA-256, frames, LSF CRC), `voice.wav` and `voice-2.wav`
(decoded overs), `precodec.wav` and `precodec-2.wav` (the tap). They are regenerated exactly from
the recipes, seeds and firmware recorded in the run, so they are not published.

## Limits

- **The emulator is not a radio.** It has the firmware's digital chain and nothing else: no
  microphone, preamp, anti-alias filter or ADC, and its timing is a desktop's. Absolute levels
  depend on the ADC model; the comparisons between variants are what it measures.
- **Bench confirmation is needed** for the ADC's real bias and noise (which set the key-up click
  and the floor), for the level real speech reaches at the ADC (which says where on the sweeps a
  user is), and for the analog response.
- Every take is the first transmission after boot, except the step test's second over.
- The pre-codec tap needs the experiment branch's Linux tap; on firmware without it the `pre_`
  metrics are simply absent. The encoder runs for as long as each key-up and a few hundred
  milliseconds less than the input, so a session's tap is cut into overs where the input's level
  pattern matches (block energies cross-correlated), not by frame count.
- The listening test models the listener with one fixed volume and one noise spectrum; the
  absolute STOI depends on both, the comparison between variants and against the ideal
  reference is the result.
- STOI models intelligibility under noise and distortion; it is not tuned to vocoders. Read small
  differences (under about 0.01) as no difference, and listen to the `voice.wav` files.
- Each take is one boot and one realisation of the noise; differences smaller than the spread
  between speech files are not established.
- The tone curve shows codec2's behaviour on tones as much as the firmware's: read it against the
  codec-only row.

## This run

Variant `keep_sum` (-DMIC_EXP_KEEP_SUM) of OpenRTX `v0.4.5-54-gf50482756`, radio file `mduv3x0`, suite `0dd602b9be`, started 2026-11-15T22:34:43+00:00.
