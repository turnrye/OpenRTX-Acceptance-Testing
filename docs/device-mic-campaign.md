<!--
SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors

SPDX-License-Identifier: GPL-3.0-or-later
-->

# M17 microphone chain on radios: methods

`acceptance/m17/mic-chain-device.robot` measures the M17 transmit microphone chain on real
radios: two at once, each flashed with the same firmware state, keyed together by the operator,
with speech played from one speaker in front of both mics. It is the bench counterpart of the
emulator campaign (`docs/emulator-mic-campaign.md`): the emulator compares firmware variants on a
model of the microphone, ADC and gain; this measures what the radios' real microphones, preamps
and ADCs deliver, and where real speech lands on the ADC. Run it once per firmware image
(baseline, then the change) and compare. The timed bench procedure is
`docs/device-mic-campaign-runbook.md`.

## The bench

```
speaker ─► (air, ~10 cm) ─► radio A mic ─► A's ADC, encodeFunc, codec2, M17 ─► RF on f_A ─┐
        └► (air, ~10 cm) ─► radio B mic ─► B's ADC, encodeFunc, codec2, M17 ─► RF on f_B ─┤
                                                                                         ▼
          one RTL-SDR capture at 250 kS/s, tuned 30 kHz below the lower radio
                 │ channelise each radio: mix f_X to 0 Hz, low-pass 12.5 kHz, decimate to 50 kS/s
                 ▼
          OAT M17 decoder (oat.m17.phy) per radio ─► codec2 3200 payload ─► c2dec ─► voice
```

- **Two radios, one capture.** Radio A (CS7000-PLUS, `radios/cs7000p.yaml`) and radio B
  (MD-UV3x0 with the M17 mod, `radios/mduv3x0.yaml`) transmit on their files' 70cm frequencies,
  445.925 and 445.975 MHz, 50 kHz apart (`FREQUENCY_A_HZ`, `FREQUENCY_B_HZ` change them). The
  receiver is tuned 30 kHz below the lower one, so the radios sit at +30 and +80 kHz in the
  capture: clear of the receiver's DC spike, with each one's IQ image (at -30 and -80 kHz) clear
  of both, and inside 80% of the Nyquist band (`device_mic.pair_tuning`).
- **Channelising.** Both carriers are on at the same time, so the capture's power envelope can't
  tell their transmissions apart. Each radio is first cut out of the capture on its own
  (`device_mic.channelise`: mixed to 0 Hz, a 201-tap low-pass at 12.5 kHz, decimated to 50 kS/s;
  the other radio, 50 kHz away, is in the stop band) and then measured and decoded as any M17
  capture (`oat.m17.phy.measure_m17_capture`), transmissions found by that channel's own power.
  A unit test sends two simultaneous M17 streams with different sources through it and gets each
  back whole.
- **RF path.** Both radios at low power, either each through its own attenuator into a combiner
  in front of the bench's attenuator chain, or each into a dummy load with the receiver picking up
  their leakage. The run checks each radio's full power against the calibrated loss to the
  receiver, and warns when a capture clips.
- **Audio.** The bench's audio interface (a Focusrite Scarlett 4i4 here, found in `aplay -l` by
  `find:Scarlett`) plays into a speaker. Both mics sit about 10 cm from it, side by side, at the
  same distance and angle. The level is not calibrated in SPL: at the start of each run the
  operator sets the speaker volume once with the clip at 0 dB (loud but not distorting) and
  reports the setting, which is recorded (`Playback` in the metadata, `pair.playback` in
  `measurements.json`). Keep it the same for every firmware phase.
- **Keying.** The operator keys both radios together for every take (no PTT cable): the
  prompts say when. Overs start a little apart; each radio's takes are found on its own channel.

## Tests

| Test | What happens | Figures per radio |
|---|---|---|
| Key-up And Silence | 3 overs of 3 s, nobody speaking | onset peak (first 100 ms), floor |
| Played Speech | the clip at -20, -10 and 0 dB from the speaker, one over each | active level, peak, spread over the three, STOI, ESTOI, listening STOI at -45 and -35 dBFS |
| Live Voice | the operator reads one sentence quietly, normally, loudly | active level, peak, spread |

- **Clip.** `OSR_us_000_0010_8k.wav` from the Open Speech Repository (Harvard sentences, male;
  free for any use), its first 10 s, normalised to a peak of -1 dBFS for 0 dB, resampled to the
  interface's rate. The same clip at 8 kHz is the STOI reference.
- **Sentence.** "The birch canoe slid on the smooth planks." (`SENTENCE`), printed for the
  operator.

## Metrics

All are of the decoded voice (8 kHz, c2dec's int16 scale), as in the emulator campaign:

- **Active level, peak, floor, onset**: as `docs/emulator-mic-campaign.md` defines them
  (simplified P.56 active level; the first 100 ms apart as the onset, which in a quiet over is the
  key-up transient; the floor is the 10th percentile of 20 ms frame levels).
- **Spread**: the largest minus the smallest active level over a test's takes. Played speech
  spans 20 dB at the input, so a fixed-gain chain shows about 20 dB and a working level control
  much less.
- **STOI and ESTOI** against the clip. The speech starts when the playback does, a second or
  two after key-up, so the alignment (the same envelope cross-correlation) searches ±6 s over
  lags that overlap at least half the shorter signal.
- **Listening STOI**: the decoded voice at its own level, as a receiver at a fixed volume
  plays it, with pink noise at -45 and -35 dBFS RMS added (seed 11), STOI against the clip: the
  emulator campaign's listening-condition test. STOI alone normalises level, so it can't see a
  quiet talker buried at the listener; this can.

## Where real speech lands on the ADC

With a fixed-gain chain (any variant without `MIC_EXP_AGC`, so the baseline phase), each played
and live take also gives the level at the ADC that its decoded level implies:

```
ADC dBFS = decoded dBFS − 20·log10(CONFIG_MIC_GAIN) − 20·log10(2^(bits−1) / 32768)
```

that is, minus the gain and plus 24.1 dB for a 12-bit ADC (its half range of 2048 codes against
int16's 32768), from the radio file's `mic_chain`. Decimation by averaging keeps the level. The
assumption is that codec2 keeps the active level: in the emulator campaign it read +0.6 to
+3.3 dB above codec2's input, about +2 dB, below clipping, so the ADC level is probably about
2 dB under the figure. This answers where on the emulator's sweeps (dBFS of the ADC range) a real
talker and the played clip sit, which sets the operating point for the AGC.

## Output

One run produces a folder per radio, next to Robot's output:

```
bench-Mic-Chain-Device/            the run's captures (IQ), each radio's baseband and voice
bench-Mic-Chain-Device-<radio>/    one per radio, exported on its own:
    measurements.json   that radio's takes (mic-take), each test's figures (mic-chain), its
                        captures, the operator's answers, and the pair: the other radio, the
                        receiver frequency, its offset, the flashed image's SHA-256, the
                        playback setting and its mic chain model
    REPORT.md, METHODS.md
    data/               silence.csv, speech.csv, live.csv, rows.json
    charts/             speech-level, intelligibility, onset, live-voice (PNG and SVG)
    voice/              each take's decoded voice, <test>-<take>.wav
```

`scripts/export-run.sh -b bench-Mic-Chain-Device-<radio file> <run>` publishes one radio as
`results/<OpenRTX sha>/<radio file>/<time>-<station>-Mic-Chain-Device/`, with its report, tables,
charts and voices. Without `-b` the export refuses a two-radio run. `python -m oat.compare` takes
the per-radio folders (`bench-Mic-Chain-Device-cs7000p` of the baseline run and of the full run,
side by side), and `scripts/device-mic-report.py` writes one report per radio comparing the runs.

## Limits

- The speaker, the room and the mic positions are not calibrated: levels compare firmware on one
  setup in one session, not radios against each other or against a standard. The two radios
  hear the speaker from slightly different places.
- The live voice is one person, three takes, and not repeatable; read it as an indication.
- One take per playback level and firmware: differences under about 1 dB in level or 0.01 in
  STOI are not established.
- The ADC level relies on the fixed gain and on codec2 keeping the active level (above).
