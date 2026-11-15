<!--
SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors

SPDX-License-Identifier: GPL-3.0-or-later
-->

# M17 microphone chain on two radios: 20-minute runbook

Two radios measured together, baseline firmware then full (all four `MIC_EXP` flags), from
OpenRTX `7f9b6c2ad` (`private/mic-exp`). How it works and what it measures:
[device-mic-campaign.md](device-mic-campaign.md).

| Radio | Radio file | Frequency | Flash |
|---|---|---|---|
| A: Connect Systems CS7000-PLUS | `radios/cs7000p.yaml` | 445.925 MHz | DFU, `dfu-util` |
| B: TYT MD-UV3x0, M17 mod | `radios/mduv3x0.yaml` | 445.975 MHz | bootloader, `radio_tool` |

## Before the session (not timed)

Everything below runs from this repository's checkout of branch `device-mic-campaign`:

```sh
cd <this checkout> && uv sync
I=~/workspace/openrtx-m17-mic-runs/device-builds/images
sha256sum $I/*                               # note them; the runs record them too
ls ~/workspace/openrtx-m17-mic-runs/speech-osr/OSR_us_000_0010_8k.wav
R=runs/mic-device-$(date +%Y%m%d)            # where the runs go
```

- Fill in the units: replace the serial numbers below (`<A serial>`, `<B serial>`).
- The bench files are local: `bench/k0ret-pair.local.yaml` (from
  `bench/radio-pair.example.yaml`: RTL-SDR at 250 kS/s, the Scarlett found as `find:Scarlett`,
  console operator), `bench/k0ret-pair.local.args` (bench, calibration, radios, speech file) and
  `calibration/k0ret.local.yaml`.
- Print the sentence for the live voice test: **"The birch canoe slid on the smooth planks."**
- Check `dfu-util` and `~/workspace/OpenRTX/subprojects/radio_tool/radio_tool` run, and that
  you can put each radio into its flashing mode (below).

## 0:00 to 5:00, set up

1. **RF.** Both radios on **low power**. Either each through its own attenuator (30 dB or more)
   into a two-way combiner in front of the bench's attenuator chain to the RTL-SDR, or each into
   a dummy load with the RTL-SDR on a short antenna about a metre away. Never a radio straight
   into the RTL-SDR.
2. **Audio.** Plug the Scarlett 4i4 in and the speaker into its output 1 (or the monitor
   outputs). Check it is found and plays:
   ```sh
   aplay -l | grep -i scarlett
   aplay -D plughw:CARD=$(aplay -l | sed -n 's/^card [0-9]*: \([^ ]*\) \[Scarlett.*/\1/p' | head -1),DEV=0 \
       ~/workspace/openrtx-m17-mic-runs/speech-osr/OSR_us_000_0010_8k.wav
   ```
   If no card matches, set `playback_device` and `capture_device` in
   `bench/k0ret-pair.local.yaml` to the `plughw:CARD=<id>,DEV=0` that `aplay -l` shows.
3. **Placement.** Radios side by side, mics facing the speaker, each about **10 cm** from it at
   the same height and angle. Don't move anything until the end.
4. **Radio settings** (both, and check them again after each flash): M17 mode; A on
   **445.925 MHz**, B on **445.975 MHz**; low power; M17 callsign set (yours), destination
   empty or @ALL, CAN 0, no encryption; volume down. Leave every audio setting at the same value
   for both phases.

## 5:00 to 6:30, flash baseline

The operator flashes; the commands are:

- **A, CS7000-PLUS**: put it in DFU mode (its bootloader key combination, as for any OpenRTX
  flash of this radio; `lsusb` then shows `0483:df11`), then
  ```sh
  dfu-util -d 0483:df11 -a 0 -s 0x08100000 -D $I/cs7000p_baseline_7f9b6c2ad.bin
  ```
  (0x08100000 is the CS7000-PLUS load address in OpenRTX's `meson.build`.) Power-cycle.
- **B, MD-UV3x0**: hold PTT and the upper side button while switching on (the LED flashes),
  then
  ```sh
  ~/workspace/OpenRTX/subprojects/radio_tool/radio_tool -d 0 -f -i $I/mduv3x0_baseline_7f9b6c2ad_wrap.bin
  ```
  Power-cycle.
- Check each boots into OpenRTX with the settings of step 4.

## 6:30 to 11:00, phase 1: baseline

```sh
uv run robot -A bench/k0ret-pair.local.args -v FIRMWARE:baseline@7f9b6c2ad \
    -v "HARDWARE_A:Connect Systems CS7000-PLUS <A serial>" \
    -v "HARDWARE_B:TYT MD-UV380 <B serial>, M17 mod" \
    -v IMAGE_A:$I/cs7000p_baseline_7f9b6c2ad.bin \
    -v IMAGE_B:$I/mduv3x0_baseline_7f9b6c2ad_wrap.bin \
    -d $R/baseline acceptance/m17/mic-chain-device.robot
```

Follow the prompts (Enter continues):

1. The plan: check the frequencies and that both radios are on M17, continue.
2. **Speaker volume**: the clip loops at 0 dB. Turn the Scarlett's output (or the speaker) up
   until it is loud but clean at the mics, continue, then type the setting (for example
   "Scarlett monitor knob at 2 o'clock, speaker at max"). **Do not touch it again.**
3. **Key-up And Silence** (about 25 s): quiet room; key both radios together for 3 s, release,
   wait 2 s; three times; then Enter.
4. **Played Speech** (about 1 min): three times: press Enter, then at once key both radios and
   hold them; the clip plays 3 s later for 10 s; when the prompt says the clip is done, release
   both and press Enter.
5. **Live Voice** (about 40 s): three times, as prompted: key both, read the printed sentence
   quietly / normally / loudly at your usual distance from the mics (equal to both), release,
   Enter.

Robot prints each test's result. **The limits aren't checked here**: a test fails only when a
radio's transmissions weren't all found or decoded.

## 11:00 to 12:30, flash full

As for baseline, with `$I/cs7000p_full_7f9b6c2ad.bin` and `$I/mduv3x0_full_7f9b6c2ad_wrap.bin`.
Check the settings again.

## 12:30 to 17:00, phase 2: full

```sh
uv run robot -A bench/k0ret-pair.local.args -v FIRMWARE:full@7f9b6c2ad \
    -v "HARDWARE_A:Connect Systems CS7000-PLUS <A serial>" \
    -v "HARDWARE_B:TYT MD-UV380 <B serial>, M17 mod" \
    -v IMAGE_A:$I/cs7000p_full_7f9b6c2ad.bin \
    -v IMAGE_B:$I/mduv3x0_full_7f9b6c2ad_wrap.bin \
    -d $R/full acceptance/m17/mic-chain-device.robot
```

The same prompts. At the speaker-volume prompt leave the knob where it is and type the same
setting.

## 17:00 to 20:00, tear down and report

Radios off, then:

```sh
uv run python scripts/device-mic-report.py $R/baseline $R/full --out $R/report
uv run python -m oat.compare $R/baseline/bench-Mic-Chain-Device-cs7000p $R/full/bench-Mic-Chain-Device-cs7000p
uv run python -m oat.compare $R/baseline/bench-Mic-Chain-Device-mduv3x0 $R/full/bench-Mic-Chain-Device-mduv3x0
```

`$R/report/<radio>/REPORT.md` compares baseline and full per radio; the baseline phase also
gives where real speech lands on each radio's ADC. To publish (later, in a pull request), one
folder per radio per run:

```sh
for run in baseline full; do for radio in cs7000p mduv3x0; do
  scripts/export-run.sh -b bench-Mic-Chain-Device-$radio $R/$run
done; done
```

## If a take fails

- **Fewer transmissions than expected** for one radio (it didn't key, or keyed late): rerun just
  that test straight away, same firmware, into a new folder, for example
  ```sh
  uv run robot -A bench/k0ret-pair.local.args ... -t "Played Speech" -d $R/baseline-rerun \
      acceptance/m17/mic-chain-device.robot
  ```
  (the same `-v` options as the phase). The speaker-volume prompt comes again: don't touch the
  knob, type the same setting. Then give the rerun after its run to the report,
  `scripts/device-mic-report.py $R/baseline $R/baseline-rerun $R/full ...`, and to the export,
  `scripts/export-run.sh -b bench-Mic-Chain-Device-<radio> -r $R/baseline-rerun $R/baseline`.
- **Nothing decoded on one radio**: check its frequency and mode, that it transmits (its TX
  LED), and Robot's warning about receiver clipping (too strong: more attenuation or distance)
  or no carrier found (too weak). Rerun the test as above.
- **Receiver clipped** warning: the levels still decode; if time allows, add attenuation and
  rerun.
- **No sound / distorted**: stop (Ctrl-C), fix the speaker, rerun the whole phase; a phase
  is about 4 minutes.
- **Out of time**: Live Voice is the one to drop. Start phase 2 with
  `-t "Key-up And Silence" -t "Played Speech"` added, so it runs only those two tests.
