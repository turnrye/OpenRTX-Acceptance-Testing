# M17 microphone chain: emulator campaign

OpenRTX `v0.4.5-53-g7f9b6c2ad` (private/mic-exp), profile **cs7000p** (oversample 6, gain 32, 12-bit ADC, noise 2.0 LSB RMS). Run 2026-11-15T21:25:57+00:00 to 2026-11-15T21:26:29+00:00, suite `007c0aba8a`.

How the bench works, what each metric means and its limits: [METHODS.md](METHODS.md). Raw tables are in `data/`, provenance in `run.json`.

Variants:

- **prime_dc**: `-DMIC_EXP_PRIME_DC`

## Headline

Δ columns are the variant minus baseline. Spread: active speech level, max − min over the input levels (lower is a steadier level), into codec2 (pre-codec, from the tap) and after it. Attack and release are from the pre-codec envelope. STOI deficit: codec-only (ideal) STOI minus the variant's, mean over takes (lower is better). Step and gate figures are described under their tests.

| variant | pre-codec spread dB | Δ | spread dB | Δ | STOI | Δ | STOI deficit | Δ | listening STOI worst | Δ | listening deficit worst | Δ | pre-codec onset dBFS | Δ | onset dBFS | Δ | pre-codec clip max % | attack s | release s | speech/pause dB | Δ | pause drift dB | Δ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| prime_dc | 37.8 |  | 34.2 |  | 0.715 |  | 0.020 |  | 0.321 |  | 0.249 |  | -42.2 |  | -47.2 |  | 1.81 | 0.00 | 0.00 | 28.5 |  | 0.1 |  |

## Speech level sweep

![speech_pre](charts/level-speech-precodec.png)

![speech](charts/level-speech.png)

![stoi](charts/stoi.png)

![onset](charts/onset.png)

Pre-codec active level (dBFS, tap), mean over speech files, Δ vs baseline in brackets:

| variant | -40 dBFS | -30 dBFS | -20 dBFS | -12 dBFS | -6 dBFS | -1 dBFS |
|---|---|---|---|---|---|---|
| prime_dc | -50.3 | -41.7 | -31.1 | -23.0 | -17.0 | -12.5 |
| codec-only (nominal) | -49.4 | -41.3 | -31.1 | -23.0 | -17.0 | -12.5 |
| codec-only (ideal) | -23.6 | -21.7 | -21.0 | -21.0 | -21.0 | -21.0 |

Output active level (dBFS), mean over speech files, Δ vs baseline in brackets:

| variant | -40 dBFS | -30 dBFS | -20 dBFS | -12 dBFS | -6 dBFS | -1 dBFS |
|---|---|---|---|---|---|---|
| prime_dc | -47.7 | -39.1 | -29.0 | -20.9 | -15.3 | -13.5 |
| codec-only (nominal) | -47.6 | -39.0 | -29.0 | -20.9 | -15.3 | -13.6 |
| codec-only (ideal) | -22.6 | -19.0 | -18.8 | -18.9 | -18.9 | -18.9 |

STOI, mean over speech files, Δ vs baseline in brackets:

| variant | -40 dBFS | -30 dBFS | -20 dBFS | -12 dBFS | -6 dBFS | -1 dBFS |
|---|---|---|---|---|---|---|
| prime_dc | 0.552 | 0.668 | 0.750 | 0.778 | 0.794 | 0.749 |
| codec-only (nominal) | 0.573 | 0.682 | 0.759 | 0.789 | 0.795 | 0.757 |
| codec-only (ideal) | 0.571 | 0.689 | 0.762 | 0.790 | 0.798 | 0.800 |

Pre-codec clip, % of the samples codec2 receives at int16 full scale, measured at the tap (in brackets, the integer-chain model as a cross-check, where it applies):

| variant | -40 dBFS | -30 dBFS | -20 dBFS | -12 dBFS | -6 dBFS | -1 dBFS |
|---|---|---|---|---|---|---|
| prime_dc | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.77 (0.73) |
| codec-only (nominal) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.78 (0.73) |

## Static level curve (tones)

![tones_pre](charts/level-tones-precodec.png)

![tones](charts/level-tones.png)

tone 1000 Hz: pre-codec RMS (dBFS, tap) by input peak (dBFS of ADC range).

| variant | -50 | -47 | -44 | -41 | -38 | -35 | -32 | -29 | -26 | -23 | -20 | -17 | -14 | -11 | -8 | -5 | -2 | +0 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| prime_dc | -46.8 | -43.9 | -41.0 | -38.1 | -35.1 | -32.1 | -29.2 | -26.2 | -23.2 | -20.2 | -17.2 | -14.2 | -11.2 | -8.2 | -5.2 | -2.3 | -1.7 | -1.1 |
| codec-only (nominal) | -46.9 | -43.9 | -41.0 | -38.0 | -35.0 | -32.0 | -29.0 | -26.0 | -23.0 | -20.0 | -17.0 | -14.0 | -11.0 | -8.0 | -5.0 | -2.5 | -1.2 | -1.2 |

tone 1000 Hz: output RMS after codec2 (dBFS) by input peak (dBFS of ADC range). codec2 alone loses level on pure tones, so read the output against the codec-only row.

| variant | -50 | -47 | -44 | -41 | -38 | -35 | -32 | -29 | -26 | -23 | -20 | -17 | -14 | -11 | -8 | -5 | -2 | +0 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| prime_dc | -52.4 | -51.0 | -49.1 | -46.3 | -42.5 | -39.3 | -35.8 | -33.1 | -31.9 | -31.3 | -30.3 | -29.6 | -27.7 | -26.4 | -24.5 | -21.2 | -20.9 | -19.8 |
| codec-only (nominal) | -53.2 | -51.7 | -49.3 | -46.2 | -42.3 | -39.1 | -34.9 | -32.6 | -31.8 | -31.1 | -29.9 | -29.2 | -28.1 | -26.3 | -23.5 | -21.4 | -18.5 | -18.5 |

tone 400 Hz: pre-codec RMS (dBFS, tap) by input peak (dBFS of ADC range).

| variant | -50 | -47 | -44 | -41 | -38 | -35 | -32 | -29 | -26 | -23 | -20 | -17 | -14 | -11 | -8 | -5 | -2 | +0 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| prime_dc | -46.8 | -43.9 | -40.9 | -38.0 | -35.0 | -32.0 | -29.0 | -26.0 | -23.0 | -20.0 | -17.0 | -14.0 | -11.0 | -8.0 | -5.0 | -2.3 | -1.5 | -1.1 |
| codec-only (nominal) | -46.8 | -43.9 | -40.9 | -38.0 | -35.0 | -32.0 | -29.0 | -26.0 | -23.0 | -20.0 | -17.0 | -14.0 | -11.0 | -8.0 | -5.0 | -2.4 | -1.4 | -1.1 |

tone 400 Hz: output RMS after codec2 (dBFS) by input peak (dBFS of ADC range). codec2 alone loses level on pure tones, so read the output against the codec-only row.

| variant | -50 | -47 | -44 | -41 | -38 | -35 | -32 | -29 | -26 | -23 | -20 | -17 | -14 | -11 | -8 | -5 | -2 | +0 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| prime_dc | -50.9 | -49.8 | -48.5 | -46.4 | -43.0 | -39.7 | -36.4 | -32.1 | -28.1 | -23.6 | -20.4 | -19.0 | -17.3 | -16.2 | -15.7 | -11.4 | -10.0 | -12.4 |
| codec-only (nominal) | -51.7 | -50.8 | -49.2 | -46.1 | -43.0 | -39.6 | -35.5 | -32.0 | -28.2 | -23.2 | -20.9 | -19.1 | -17.8 | -17.3 | -16.2 | -13.5 | -7.5 | -12.0 |

## Level step (AGC dynamics)

![step_pre](charts/envelope-step-precodec.png)

![step](charts/envelope-step.png)

Settled levels are medians of the 50 ms envelope; attack and release are the time to stay within 2 dB of the settled level after each step; rise is the output step for the +20 dB input step (20 dB for a linear chain).

| variant | over | where | before dBFS | loud dBFS | after dBFS | rise dB | attack s | overshoot dB | release s | dip dB | release dB/s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| prime_dc | 1 | pre-codec | -32.9 | -12.9 | -32.9 | 20.0 | 0.00 | 0.0 | 0.00 | 0.0 | — |
| prime_dc | 1 | output | -31.6 | -15.7 | -31.6 | 15.9 | 0.00 | 0.1 | 0.05 | 0.1 | — |
| codec-only (nominal) | 1 | pre-codec | -32.2 | -12.2 | -32.2 | 20.0 | 0.00 | 0.0 | 0.00 | 0.0 | — |
| codec-only (nominal) | 1 | output | -32.1 | -16.4 | -32.0 | 15.7 | 0.00 | 0.0 | 0.05 | 0.1 | — |
| prime_dc | 2 | pre-codec | -32.9 | -12.9 | -32.9 | 20.0 | 0.00 | 0.0 | 0.00 | 0.0 | — |
| prime_dc | 2 | output | -31.6 | -15.7 | -31.6 | 15.9 | 0.00 | 0.0 | 0.00 | 0.1 | — |

## Listening-condition intelligibility

![listen](charts/listening.png)

The speech sweep's decoded audio at its own level (a fixed receiver volume, no normalisation) with pink noise added at the listener, STOI against the clean source. Worst: the lowest mean STOI over the input and noise levels; deficit: codec-only (ideal) minus the variant in the same cell, at its largest.

| variant | mean STOI | Δ | worst STOI | at input / noise dBFS | Δ | worst deficit vs ideal |
|---|---|---|---|---|---|---|
| prime_dc | 0.593 |  | 0.321 | -40 / -35 |  | 0.249 |

Noise -45 dBFS: STOI by input peak, Δ vs baseline in brackets:

| variant | -40 dBFS | -30 dBFS | -20 dBFS | -12 dBFS | -6 dBFS | -1 dBFS |
|---|---|---|---|---|---|---|
| prime_dc | 0.427 | 0.547 | 0.653 | 0.714 | 0.750 | 0.721 |
| codec-only (ideal) | 0.573 | 0.687 | 0.736 | 0.739 | 0.736 | 0.733 |
| codec-only (nominal) | 0.432 | 0.556 | 0.667 | 0.729 | 0.755 | 0.734 |

Noise -35 dBFS: STOI by input peak, Δ vs baseline in brackets:

| variant | -40 dBFS | -30 dBFS | -20 dBFS | -12 dBFS | -6 dBFS | -1 dBFS |
|---|---|---|---|---|---|---|
| prime_dc | 0.321 | 0.427 | 0.552 | 0.634 | 0.687 | 0.678 |
| codec-only (ideal) | 0.570 | 0.655 | 0.670 | 0.669 | 0.667 | 0.664 |
| codec-only (nominal) | 0.323 | 0.432 | 0.563 | 0.650 | 0.697 | 0.693 |

## Silence and gate

![gate](charts/gate.png)

![gate_env_pre](charts/envelope-gate-precodec.png)

![gate_env](charts/envelope-gate.png)

Pause level is the RMS in the pauses, 0.3 s in from each edge; drift is the last second of a pause against its first (positive: the noise is pumped up during the pause); trend is the last pause against the first.

| variant | noise dBFS | where | speech dBFS | pause dBFS | speech/pause dB | drift dB | worst drift dB | trend dB |
|---|---|---|---|---|---|---|---|---|
| prime_dc | -55 | pre-codec | -20.9 | -52.1 | 31.3 | -0.1 | 0.1 | -0.2 |
| prime_dc | -55 | output | -17.9 | -54.4 | 36.4 | 0.2 | 0.3 | 0.1 |
| codec-only (nominal) | -55 | pre-codec | -20.9 | -49.3 | 28.4 | -1.8 | -0.9 | 1.2 |
| codec-only (nominal) | -55 | output | -17.7 | -54.8 | 37.1 | 0.5 | 0.9 | -0.5 |
| prime_dc | -40 | pre-codec | -21.2 | -37.6 | 16.4 | -0.1 | 0.1 | -0.2 |
| prime_dc | -40 | output | -19.4 | -39.9 | 20.5 | 0.1 | 0.2 | 0.1 |
| codec-only (nominal) | -40 | pre-codec | -21.1 | -34.5 | 13.4 | -1.9 | -1.1 | 1.3 |
| codec-only (nominal) | -40 | output | -19.2 | -40.5 | 21.2 | 0.8 | 1.2 | -0.3 |

