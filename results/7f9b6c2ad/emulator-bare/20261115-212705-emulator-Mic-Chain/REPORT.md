# M17 microphone chain: emulator campaign

OpenRTX `v0.4.5-53-g7f9b6c2ad` (private/mic-exp), profile **emulator-bare** (oversample 1, gain 1, 16-bit ADC, noise 0.0 LSB RMS). Run 2026-11-15T21:27:05+00:00 to 2026-11-15T21:27:34+00:00, suite `007c0aba8a`.

How the bench works, what each metric means and its limits: [METHODS.md](METHODS.md). Raw tables are in `data/`, provenance in `run.json`.

Variants:

- **hpf**: `-DMIC_EXP_HPF`

## Headline

Δ columns are the variant minus baseline. Spread: active speech level, max − min over the input levels (lower is a steadier level), into codec2 (pre-codec, from the tap) and after it. Attack and release are from the pre-codec envelope. STOI deficit: codec-only (ideal) STOI minus the variant's, mean over takes (lower is better). Step and gate figures are described under their tests.

| variant | pre-codec spread dB | Δ | spread dB | Δ | STOI | Δ | STOI deficit | Δ | listening STOI worst | Δ | listening deficit worst | Δ | pre-codec onset dBFS | Δ | onset dBFS | Δ | pre-codec clip max % | attack s | release s | speech/pause dB | Δ | pause drift dB | Δ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| hpf | 36.3 |  | 34.7 |  | 0.769 |  | 0.037 |  | 0.291 |  | 0.349 |  | -50.2 |  | -50.4 |  | 0.00 | 0.00 | 0.00 | 28.8 |  | 0.2 |  |

## Speech level sweep

![speech_pre](charts/level-speech-precodec.png)

![speech](charts/level-speech.png)

![stoi](charts/stoi.png)

![onset](charts/onset.png)

Pre-codec active level (dBFS, tap), mean over speech files, Δ vs baseline in brackets:

| variant | -40 dBFS | -30 dBFS | -20 dBFS | -12 dBFS | -6 dBFS | -1 dBFS |
|---|---|---|---|---|---|---|
| hpf | -53.6 | -45.3 | -36.3 | -28.3 | -22.3 | -17.3 |
| codec-only (nominal) | -53.5 | -45.6 | -37.0 | -29.0 | -23.0 | -18.0 |
| codec-only (ideal) | -24.5 | -22.4 | -21.0 | -21.0 | -21.0 | -21.0 |

Output active level (dBFS), mean over speech files, Δ vs baseline in brackets:

| variant | -40 dBFS | -30 dBFS | -20 dBFS | -12 dBFS | -6 dBFS | -1 dBFS |
|---|---|---|---|---|---|---|
| hpf | -50.8 | -44.4 | -35.0 | -26.9 | -20.9 | -16.1 |
| codec-only (nominal) | -50.3 | -44.4 | -34.8 | -26.8 | -20.7 | -16.0 |
| codec-only (ideal) | -22.4 | -20.2 | -18.7 | -18.8 | -18.8 | -18.8 |

STOI, mean over speech files, Δ vs baseline in brackets:

| variant | -40 dBFS | -30 dBFS | -20 dBFS | -12 dBFS | -6 dBFS | -1 dBFS |
|---|---|---|---|---|---|---|
| hpf | 0.629 | 0.750 | 0.796 | 0.812 | 0.814 | 0.814 |
| codec-only (nominal) | 0.627 | 0.752 | 0.798 | 0.806 | 0.807 | 0.806 |
| codec-only (ideal) | 0.798 | 0.806 | 0.809 | 0.806 | 0.809 | 0.808 |

Pre-codec clip, % of the samples codec2 receives at int16 full scale, measured at the tap (in brackets, the integer-chain model as a cross-check, where it applies):

| variant | -40 dBFS | -30 dBFS | -20 dBFS | -12 dBFS | -6 dBFS | -1 dBFS |
|---|---|---|---|---|---|---|
| hpf | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| codec-only (nominal) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |

## Static level curve (tones)

![tones_pre](charts/level-tones-precodec.png)

![tones](charts/level-tones.png)

tone 1000 Hz: pre-codec RMS (dBFS, tap) by input peak (dBFS of ADC range).

| variant | -50 | -47 | -44 | -41 | -38 | -35 | -32 | -29 | -26 | -23 | -20 | -17 | -14 | -11 | -8 | -5 | -2 | +0 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| hpf | -53.0 | -50.0 | -47.0 | -44.0 | -41.0 | -38.0 | -35.0 | -32.0 | -29.0 | -26.0 | -23.0 | -20.0 | -17.0 | -14.0 | -11.0 | -8.0 | -5.0 | -3.0 |
| codec-only (nominal) | -53.0 | -50.0 | -47.0 | -44.0 | -41.0 | -38.0 | -35.0 | -32.0 | -29.0 | -26.0 | -23.0 | -20.0 | -17.0 | -14.0 | -11.0 | -8.0 | -5.0 | -3.0 |

tone 1000 Hz: output RMS after codec2 (dBFS) by input peak (dBFS of ADC range). codec2 alone loses level on pure tones, so read the output against the codec-only row.

| variant | -50 | -47 | -44 | -41 | -38 | -35 | -32 | -29 | -26 | -23 | -20 | -17 | -14 | -11 | -8 | -5 | -2 | +0 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| hpf | -36.6 | -40.5 | -35.6 | -30.6 | -35.9 | -35.5 | -40.2 | -47.4 | -28.9 | -32.2 | -29.3 | -31.1 | -31.8 | -28.7 | -25.5 | -22.4 | -20.9 | -38.3 |
| codec-only (nominal) | -46.4 | -38.1 | -39.0 | -36.8 | -50.6 | -34.0 | -50.6 | -47.4 | -35.3 | -31.4 | -38.0 | -34.9 | -31.0 | -28.7 | -32.5 | -22.4 | -25.9 | -15.6 |

tone 400 Hz: pre-codec RMS (dBFS, tap) by input peak (dBFS of ADC range).

| variant | -50 | -47 | -44 | -41 | -38 | -35 | -32 | -29 | -26 | -23 | -20 | -17 | -14 | -11 | -8 | -5 | -2 | +0 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| hpf | -53.0 | -50.0 | -47.0 | -44.0 | -41.0 | -38.0 | -35.0 | -32.0 | -29.0 | -26.0 | -23.0 | -20.0 | -17.0 | -14.0 | -11.0 | -8.0 | -5.0 | -3.1 |
| codec-only (nominal) | -53.0 | -50.0 | -47.0 | -44.0 | -41.0 | -38.0 | -35.0 | -32.0 | -29.0 | -26.0 | -23.0 | -20.0 | -17.0 | -14.0 | -11.0 | -8.0 | -5.0 | -3.0 |

tone 400 Hz: output RMS after codec2 (dBFS) by input peak (dBFS of ADC range). codec2 alone loses level on pure tones, so read the output against the codec-only row.

| variant | -50 | -47 | -44 | -41 | -38 | -35 | -32 | -29 | -26 | -23 | -20 | -17 | -14 | -11 | -8 | -5 | -2 | +0 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| hpf | -35.8 | -35.7 | -34.0 | -33.6 | -30.6 | -28.8 | -34.3 | -43.8 | -29.5 | -35.8 | -34.6 | -27.0 | -30.5 | -24.6 | -21.1 | -24.1 | -18.5 | -15.6 |
| codec-only (nominal) | -26.1 | -28.8 | -32.0 | -28.9 | -31.8 | -34.3 | -29.3 | -43.8 | -35.4 | -26.1 | -34.5 | -35.4 | -35.4 | -28.3 | -25.7 | -25.5 | -14.0 | -14.1 |

## Level step (AGC dynamics)

![step_pre](charts/envelope-step-precodec.png)

![step](charts/envelope-step.png)

Settled levels are medians of the 50 ms envelope; attack and release are the time to stay within 2 dB of the settled level after each step; rise is the output step for the +20 dB input step (20 dB for a linear chain).

| variant | over | where | before dBFS | loud dBFS | after dBFS | rise dB | attack s | overshoot dB | release s | dip dB | release dB/s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| hpf | 1 | pre-codec | -38.2 | -18.2 | -38.2 | 20.0 | 0.00 | 0.0 | 0.00 | 0.0 | — |
| hpf | 1 | output | -38.3 | -17.9 | -38.3 | 20.3 | 0.00 | 0.1 | 0.05 | 0.0 | — |
| codec-only (nominal) | 1 | pre-codec | -38.3 | -18.3 | -38.3 | 20.0 | 0.00 | 0.0 | 0.00 | 0.0 | — |
| codec-only (nominal) | 1 | output | -38.3 | -17.9 | -38.3 | 20.3 | 0.00 | 0.1 | 0.05 | 0.0 | — |
| hpf | 2 | pre-codec | -38.2 | -18.2 | -38.2 | 20.0 | 0.00 | 0.0 | 0.00 | 0.0 | — |
| hpf | 2 | output | -38.3 | -17.9 | -38.3 | 20.3 | 0.00 | 0.1 | 0.05 | 0.0 | — |

## Listening-condition intelligibility

![listen](charts/listening.png)

The speech sweep's decoded audio at its own level (a fixed receiver volume, no normalisation) with pink noise added at the listener, STOI against the clean source. Worst: the lowest mean STOI over the input and noise levels; deficit: codec-only (ideal) minus the variant in the same cell, at its largest.

| variant | mean STOI | Δ | worst STOI | at input / noise dBFS | Δ | worst deficit vs ideal |
|---|---|---|---|---|---|---|
| hpf | 0.559 |  | 0.291 | -40 / -35 |  | 0.349 |

Noise -45 dBFS: STOI by input peak, Δ vs baseline in brackets:

| variant | -40 dBFS | -30 dBFS | -20 dBFS | -12 dBFS | -6 dBFS | -1 dBFS |
|---|---|---|---|---|---|---|
| hpf | 0.364 | 0.483 | 0.606 | 0.690 | 0.732 | 0.764 |
| codec-only (ideal) | 0.714 | 0.730 | 0.740 | 0.738 | 0.740 | 0.740 |
| codec-only (nominal) | 0.368 | 0.489 | 0.612 | 0.689 | 0.729 | 0.754 |

Noise -35 dBFS: STOI by input peak, Δ vs baseline in brackets:

| variant | -40 dBFS | -30 dBFS | -20 dBFS | -12 dBFS | -6 dBFS | -1 dBFS |
|---|---|---|---|---|---|---|
| hpf | 0.291 | 0.356 | 0.486 | 0.590 | 0.651 | 0.700 |
| codec-only (ideal) | 0.633 | 0.657 | 0.671 | 0.668 | 0.671 | 0.671 |
| codec-only (nominal) | 0.296 | 0.368 | 0.494 | 0.590 | 0.652 | 0.694 |

## Silence and gate

![gate](charts/gate.png)

![gate_env_pre](charts/envelope-gate-precodec.png)

![gate_env](charts/envelope-gate.png)

Pause level is the RMS in the pauses, 0.3 s in from each edge; drift is the last second of a pause against its first (positive: the noise is pumped up during the pause); trend is the last pause against the first.

| variant | noise dBFS | where | speech dBFS | pause dBFS | speech/pause dB | drift dB | worst drift dB | trend dB |
|---|---|---|---|---|---|---|---|---|
| hpf | -55 | pre-codec | -25.2 | -60.6 | 35.4 | 0.1 | 0.4 | 0.0 |
| hpf | -55 | output | -23.6 | -61.0 | 37.4 | 0.1 | 0.3 | 0.0 |
| codec-only (nominal) | -55 | pre-codec | -26.7 | -55.3 | 28.6 | 0.3 | 1.2 | 1.9 |
| codec-only (nominal) | -55 | output | -23.4 | -61.1 | 37.7 | 0.1 | 0.5 | -0.5 |
| hpf | -40 | pre-codec | -27.3 | -45.6 | 18.2 | 0.1 | 0.4 | 0.0 |
| hpf | -40 | output | -25.5 | -45.9 | 20.3 | 0.2 | 0.5 | -0.1 |
| codec-only (nominal) | -40 | pre-codec | -26.9 | -40.3 | 13.4 | 0.4 | 1.3 | 1.9 |
| codec-only (nominal) | -40 | output | -25.0 | -46.0 | 21.0 | 0.2 | 0.5 | -0.3 |

