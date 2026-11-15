<!--
SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors

SPDX-License-Identifier: GPL-3.0-or-later
-->

# M17 microphone chain on the emulator: methods

`scripts/emu-mic-campaign.py` measures what OpenRTX's M17 transmit audio chain does to a
microphone signal, for a set of firmware variants that differ only in C defines, against the same
test signals. It runs on the OpenRTX Linux emulator, not on a radio: it is evidence for a change
to the firmware's signal processing, to be confirmed on a bench (`acceptance/m17/tx.robot`) for
everything the emulator cannot model.

## The bench

```
recipe ─► float signal ─► ADC model ─► mic.raw ─► openrtx_linux ─► tx.raw ─► FM modulator ─► IQ
                         (adc_quantize)          (encodeFunc,      (48 kHz        (OAT, nominal
                                                  codec2, M17       baseband)      deviation)
                                                  modulator)
IQ ─► OAT M17 decoder (oat.m17.phy) ─► codec2 3200 frames ─► c2dec ─► voice.wav ─► metrics
```

1. **Signal.** Each test signal is a recipe (a JSON object in `run.json`): a tone, a speech file at
   a peak level, a level step or a gate pattern. `oat.emu_campaign.synth` turns it into floats at
   the ADC's sample rate (8 kHz times the profile's oversampling: 48 kHz for the CS7000-PLUS),
   where 1.0 is the ADC's half range, and appends 0.5 s of silence.
2. **ADC model** (`oat.emu.adc_quantize`). The floats are multiplied by the half range (2048 codes
   for a 12-bit converter), white Gaussian noise of the profile's RMS in LSB is added (2 LSB by
   default, seeded), and the result is rounded, offset to mid-scale (2048) and clipped to the
   converter's range (0 to 4095). These are the int16 samples the firmware's `encodeFunc` would
   read from the ADC's DMA buffer.
3. **Firmware.** `openrtx_linux` is built per variant with meson (`-Dsystem_codec2=false`, so the
   firmware's own codec2), with `-Dc_args=` set to the profile's `CONFIG_MIC_GAIN` and
   `CONFIG_MIC_OVERSAMPLE` plus the variant's defines, each in its own build directory
   (`build_emu_<profile>_<variant>`). The emulator reads the microphone from `OPENRTX_MIC_FILE`
   (raw s16le at the stream's rate, from the start of the file at every key-up, silence after it
   ends) and writes the M17 modulator's output to `OPENRTX_M17_TX_BASEBAND`. A fresh emulator,
   with its own state directory and no display or audio device, is driven from its shell: switch
   the VFO to M17 (`keycombo MONI 5`), key for the length of the microphone file, unkey, quit. So
   the signal passes through the real `encodeFunc` (decimator, DC block, gain, and any
   experimental processing), the real codec2 3200 encoder and the real M17 framing and modulator,
   in real time.
4. **Decoder.** The baseband is FM-modulated to IQ at nominal deviation (outer symbols ±2.4 kHz),
   then decoded by OAT's M17 decoder, the same code that measures a real radio's transmissions,
   and the codec2 payload is decoded with Codec2's `c2dec` (3200 mode). Each over keyed gives one
   decoded transmission; its LSF CRC and frame count are kept with the take.
5. **Metrics** are computed from the decoded audio (8 kHz) and the recipe. They are recomputed on
   every invocation from the saved `voice.wav`, so the emulator only runs again when the binary or
   the input changes.

Every take is a fresh boot of the emulator, so every take measures the **first transmission after
power-on**. That matters for state the firmware keeps between transmissions: the AGC's gain
persists (it is static) and starts at 0 dB with a 1 s fast-acquisition phase after boot. The level
step test therefore keys its signal twice in one session (`--step-overs 2`) and reports both overs:
the first is the cold start, the second the steady-state case, starting from the gain the first
left behind.

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
the profile's c_args. The firmware branch defines what each does:

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
longer saturates, so the pre-codec clip model (below) does not apply: those variants report it as
N/A, not 0.

## Test signals

| Test | Signal | Levels |
|---|---|---|
| `tones` | 1 kHz and 400 Hz sine, 2 s | peak -50 to -2 dBFS in 3 dB steps, and 0 dBFS |
| `speech` | four Harvard-sentence recordings, first 12 s | peak -40, -30, -20, -12, -6, -1 dBFS |
| `step` | a sustained synthetic vowel: 3 s at -30, 3 s at -10, 6 s at -30 dBFS peak, keyed twice in one session | |
| `gate` | the first 9 s of the first speech file as three 3 s bursts at -12 dBFS peak, each followed by a 4 s pause, over a pink noise bed | noise bed -55 and -40 dBFS RMS |

The key-up onset is read from the speech takes.

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
`run.json`, the ADC noise seed (17), the seeds in the recipe, the profile and, for speech, the
source file (`emu_campaign.mic_input`). Each take's `take.json` records the SHA-256 of the exact
samples sent, so a regenerated input can be checked against it. `--keep-raw` keeps each take's
`mic.raw` and the emulator's `tx.raw`.

## References

Codec2 changes what it codes: it loses about 13 dB on a 1 kHz tone, and its intelligibility has a
ceiling of its own. Every firmware result is therefore read against the same input sent straight
through codec2 (the system's `c2enc` and `c2dec`, 3200 mode), so that the firmware's own effect is
the difference:

- **codec-only (nominal)**: the same ADC samples through an ideal linear chain at the profile's
  fixed gain (bias removed, decimated with a proper anti-alias filter, multiplied by the gain,
  clipped at full scale), then codec2. What a perfect implementation of today's fixed-gain chain
  would give. Every test has it.
- **codec-only (ideal)**: the same ADC samples through the ideal chain, unclipped and set to
  -21 dBFS active level, then codec2. The best any firmware could do with that ADC input. Speech
  only; the STOI deficit is measured against it.
- **codec-only (clean)**: the clean source file at -21 dBFS active level, then codec2, without
  the ADC. The codec's own ceiling. Speech only.

## Metrics

Output levels are dBFS of the decoded audio, where 1.0 is int16 full scale; RMS is plain RMS (a
full-scale sine reads -3 dBFS). Every output leaves out its last 0.2 s.

- **Output RMS** (tones): the RMS from 0.5 s into the tone to 0.3 s before its end.
- **Active speech level**: a simplified ITU-T P.56. The audio is cut into 20 ms frames; frames
  within 30 dB of the loudest frame and above -60 dBFS are active; the level is the RMS over the
  active frames. **Spread**, the headline level metric, is the maximum minus the minimum, over the
  input levels, of the active level averaged over the speech files: 0 dB means the output level
  does not depend on how loud the speaker is.
- **Peak**: the largest sample magnitude, leaving out the first 100 ms.
- **Onset peak** (key-up click): the largest sample magnitude in the first 100 ms of the decoded
  audio. The test signals all start quietly, so a loud onset is the firmware's own transient.
- **Floor**: the 10th percentile of the 20 ms frame levels.
- **Pre-codec clip**: the fraction of samples the integer chain saturates before codec2, from a
  model of it applied to the input (`emu.chain_clip_fraction`: average each block of oversampled
  codes, remove the mean, multiply by the gain, count the magnitudes at int16 full scale). It is
  a property of the input under the integer chain, valid for baseline, prime_dc and keep_sum
  (whose sum, multiplied by the gain and divided by the oversampling ratio, clips at the same
  point), and N/A for the float chain. The emulator does not expose the samples codec2 receives.
- **Post-codec clip**: the fraction of decoded samples at full scale.
- **STOI**: short-time objective intelligibility (pystoi, not extended) of the decoded speech
  against the clean source, both at 8 kHz, after alignment. A vocoder does not keep the waveform,
  so the alignment is the lag maximising the cross-correlation of 2.5 ms log-energy envelopes
  (floored 40 dB below their peak, mean removed), searched within ±0.5 s (`emu.find_lag`).
  **STOI deficit** is codec-only (ideal) STOI minus the variant's, per take, averaged over the
  takes: what the firmware chain loses against the best possible processing of the same ADC input.
- **Envelope**: the RMS level in consecutive 50 ms blocks of the output, aligned to the input by
  the same envelope lag, found against the ideal chain's output. Kept in `data/envelopes.csv`.
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
- **Gate**: in the aligned output, the speech level is the active level over the three bursts; the
  **pause level** is the RMS in each pause from 0.3 s after its start to 0.3 s before its end,
  power-averaged over the pauses; **speech/pause** is their difference. **Drift** is the RMS of the
  last second of a pause minus that of its first second (positive: the noise is pumped up as the
  pause goes on), averaged over the pauses, with the worst one; **trend** is the last pause's level
  minus the first's.

## Running it

Once, the speech files:

```sh
uv sync
mkdir -p ~/speech-osr && cd ~/speech-osr
for n in 0010 0011 0030 0031; do
  curl -O "https://www.voiptroubleshooter.com/open_speech/american/OSR_us_000_${n}_8k.wav"
done
```

`c2enc` and `c2dec` (the `codec2` package), meson, ninja and OpenRTX's Linux build dependencies
must be installed.

The campaign, from this repository:

```sh
uv run python scripts/emu-mic-campaign.py --openrtx ~/OpenRTX --profile cs7000p \
    --variants baseline prime_dc keep_sum hpf agc full agc_slow agc_mid \
    --speech-dir ~/speech-osr --out runs/mic-campaign
```

- `--tests tones speech step gate` selects tests; `--speech-levels`, `--tone-levels`,
  `--tone-freqs`, `--step SECONDS:DBFS ...`, `--step-overs`, `--gate-noise`, `--gate-peak` and
  `--seconds` change the signals, `--noise-lsb` the ADC noise.
- `--build-root DIR` puts the build directories somewhere other than the OpenRTX checkout;
  `--binary VARIANT=PATH` uses a prebuilt `openrtx_linux` for a variant (its SHA-256 is still
  recorded, but not its source).
- `--jobs` sets how many emulators run at once (default 6). Each runs in real time, so a campaign
  takes the sum of the signals' lengths plus about 4 s per take, divided by the jobs: roughly
  2 minutes per variant at 6 jobs.
- A take is skipped when its directory already holds a decoded take for the same binary SHA-256,
  input SHA-256 and number of overs, so an interrupted campaign resumes, and adding a variant or a
  level runs only the new takes. Rebuilt firmware changes the binary and reruns its takes.

## Output

```
run.json        provenance: OpenRTX sha, describe, branch; this repository's describe;
                tool versions; profile; variants with their defines and binary SHA-256;
                every signal recipe; speech file SHA-256; dates; the command
openrtx.patch   the uncommitted OpenRTX changes, when the checkout was dirty
REPORT.md       headline metrics and tables per test, each variant against baseline
METHODS.md      this document, and the run's command
data/           tones.csv, speech.csv, step.csv, gate.csv: one row per take and over
                (variant, profile, test, signal, level, metrics); envelopes.csv; headline.csv
charts/         PNG and SVG: level-tones, level-speech, stoi, envelope-step, envelope-gate,
                onset, gate
takes/<variant>/<take>/
                take.json (recipe, binary and input SHA-256, frames, LSF CRC),
                voice.wav (decoded first over), voice-2.wav (second over, step test)
```

`run.json` names files without their directories, and the command has the home directory as `~`.

## Why a script and not a Robot suite

The suites in `acceptance/` are procedures for a radio on a bench: each run names the unit, the
bench and its calibration, and its results go to `results/` as evidence about that radio. A
campaign on the emulator has none of those: it compares firmware variants on a model, needs
builds, many parallel takes and charts, and its evidence is the campaign directory, attached to the
change it supports. The bench measurements it cannot replace (the Codec2 input level, the key-up
transient, speech deviation) stay in `m17/tx.robot`.

## Limits

- **The emulator is not a radio.** It has the firmware's digital chain and nothing else: no
  microphone, preamp, anti-alias filter or ADC, and its timing is a desktop's. Absolute levels
  depend on the ADC model; the comparisons between variants are what it measures.
- **Bench confirmation is needed** for the ADC's real bias and noise (which set the key-up click
  and the floor), for the level real speech reaches at the ADC (which says where on the sweeps a
  user is), and for the analog response.
- Every take is the first transmission after boot, except the step test's second over.
- The pre-codec clip is a model, not a measurement, and only of the integer chain.
- STOI models intelligibility under noise and distortion; it is not tuned to vocoders. Read small
  differences (under about 0.01) as no difference, and listen to the `voice.wav` files.
- Each take is one boot and one realisation of the noise; differences smaller than the spread
  between speech files are not established.
- The tone curve shows codec2's behaviour on tones as much as the firmware's: read it against the
  codec-only row.
