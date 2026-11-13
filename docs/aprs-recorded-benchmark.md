<!--
SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors

SPDX-License-Identifier: GPL-3.0-or-later
-->

# APRS receive against a recording of real traffic

`acceptance/aprs/recorded.robot` plays a recording of off-air APRS traffic into the radio as FM
and counts the frames the radio reports on its KISS TNC against what Dire Wolf decodes from the
same audio.

## Cabled only

The recording is track 1 of the WA8LMF TNC Test CD (<http://www.wa8lmf.net/TNCtest/>). It carries
real callsigns, and its author asks that it never be played over the air: an igate that heard it
would put years-old positions on the internet.

- HackRF TX port → the bench's attenuator chain → the radio's antenna socket. No antenna on
  either, and none on a tee.
- Use a frequency that isn't an APRS channel, so a leak can't be gated: 146.580 MHz, not 144.390.
  Copy `radios/mduv3x0.yaml` to `radios/mduv3x0-recording.local.yaml` and set the 2m
  `frequency_hz` to `146580000`.
- Leave the HackRF's amplifier off (`amp: false`).

## Once: the track and the reference

```sh
mkdir -p ~/workspace/aprs-benchmark/work && cd ~/workspace/aprs-benchmark/work
T="../data/cd/Audio-Tracks/01_40-Mins-Traffic -on-144.39.flac"
sox "$T" -c 1 -b 16 t1.wav remix 1        # the flat (discriminator) track, left channel
atest -B 1200 t1.wav > dw1.txt            # Dire Wolf's frames: 1005 with 1.8.1
```

Play the flat track. The radio's FM receiver de-emphasizes, and nothing here pre-emphasizes, as
with a transmitter fed flat audio. Track 2 is already de-emphasized and would be so twice.

## The bench

- Firmware: a build that reports frames as KISS on the USB serial port. On the MD-UV390 that is
  branch `aprs/v2-rf-test` (it defines `ENABLE_STDIO` for this).
- Bench file: add the radio's port as the TNC, with nothing else reading it.

  ```yaml
  tnc:
    type: serial
    port: /dev/cu.usbmodem1101
  ```

- Radio: APRS mode (macro menu, mode key) on the test frequency.
- Level: the bench's ordinary gain is enough. Aim for about -70 dBm at the radio, well above its
  sensitivity and far from overload; this test is about decoding, not weak signals.

## Runs

A two-minute first run (95 reference frames), to see frames arrive:

```sh
uv run robot -A bench/mybench.local.args -v RADIO:radios/mduv3x0-recording.local.yaml \
    -v FIRMWARE:<git describe> -v "HARDWARE:TYT MD-UV390 <serial>" \
    -v TRACK:$HOME/workspace/aprs-benchmark/work/t1.wav \
    -v REFERENCE:$HOME/workspace/aprs-benchmark/work/dw1.txt \
    -v DURATION_S:120 -d runs/aprs-recording-smoke acceptance/aprs/recorded.robot
```

The whole track at several deviations: drop `DURATION_S` and add
`-v DEVIATIONS_HZ:1500,2500,3000,3500,4500`. Offline, the demodulator decodes well only in a
narrow range of input level, so the deviation sweep is the interesting part.

- Time: 26 minutes per deviation, plus a few minutes writing the IQ file first. Five deviations
  take about two and a half hours.
- Disk: 6.2 GB free in the run folder (the IQ file for one pass at 2 MS/s; it is deleted after
  each pass).
- Results: `measurements.json` has one `aprs-recording` record per pass (reference frames, frames
  in common, extra, the fraction heard). `aprs-recording-NNN-missed.txt` lists the reference
  frames the radio didn't report, and `tnc.log` every byte the radio sent.

## Reading the result

- `common / reference` is the figure to compare with the offline replay of the same track
  (`aprs_replay_tool` in the firmware tree: 992 of 1005 at its best level).
- `extra` are frames the radio decoded and Dire Wolf didn't; a handful is normal.
- `junk` counts stretches between KISS frame ends that weren't frames, such as console text.
