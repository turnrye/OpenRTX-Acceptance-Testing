<!--
SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors

SPDX-License-Identifier: GPL-3.0-or-later
-->

# M17 receive trace

A test-only addition to OpenRTX that reports what the M17 receiver decoded, one text line per
event, over the radio's USB serial port. `acceptance/m17/trace.robot` reads it to score every
frame the bench sends: decoded, garbled or missed, when the link setup data arrived, which frames
went to the speaker, and how much of the receive thread's stack was used.

It is never part of a release. `apply_trace.py` adds it to an OpenRTX tree, which is how a trace
build of any commit is made, so the same firmware can be measured before and after a change.

```sh
firmware/m17-trace/build_trace_fw.sh <OpenRTX ref> <label> [outdir]
```

builds `openrtx_mduv3x0_trace_<label>-<sha>_wrap.bin` for the MD-UV380/390, the radio with a USB
serial port on master. Flash it as any MD-UV3x0 image (`radio_tool -d 0 -f -i <file>`).

## How it works

Four calls go into `OpMode_M17::rxState`: after the lock check (commands and lock changes), after
each decoded frame, and after each frame handed to the codec. The trace is silent until the host
sends `TRACE ON`. The receive thread only formats lines into a 4 KB ring buffer; a low priority
thread writes them out, so a slow or absent host never holds up reception. Lines that don't fit
are counted, and the count is in every heartbeat.

## Protocol

Commands end with a newline: `ID`, `TRACE ON`, `TRACE OFF`. Lines are ASCII, fields separated by
spaces, times in ms since boot:

| Line | Meaning |
|---|---|
| `ID <label>` | The build's label and OpenRTX commit |
| `ON <ms>`, `OFF <ms>` | Tracing started or stopped |
| `HB <ms> <stack> <free> <dropped>` | Once a second: the receive thread's stack size, its least free stack since boot, lines dropped |
| `LOCK <ms> <0\|1>` | The demodulator's lock changed |
| `LSF <ms> <valid> <F\|I> <hex>` | The link setup data changed: its 30 bytes, from a link setup frame (`F`) or rebuilt from the LICH (`I`) |
| `STR <ms> <fn> <lsf valid> <hex>` | A stream frame: the frame number and 16 payload bytes the decoder holds after it, which are the previous frame's when it rejected this one |
| `PKT <ms> <counter> <eof> <hex>` | A packet frame: its counter, end flag and 25 data bytes |
| `EOT <ms>` | End of transmission frame |
| `FRM <ms> <type>` | Any other frame type (`M17::FrameType`) |
| `PLAY <ms> <fn>` | A stream frame went to the speaker |

## Anchors

`apply_trace.py` finds its places by anchor lines that master and the soft-decision decoder
(OpenRTX#524) share, and writes nothing unless each is found exactly once. If OpenRTX moves on,
the script stops with the anchor that moved; update its pattern.
