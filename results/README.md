<!--
SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Results

Exports of acceptance runs that serve as evidence: a release's acceptance run, or the run behind a
merged feature. Exploratory and debugging runs stay on the tester's machine.

```
results/<firmware sha>/<radio file>/<yyyymmdd-hhmmss>-<station>-<suite>/
    output.xml          Robot's output, sanitized
    measurements.json   measurements, limit checks and operator answers
```

`scripts/export-run.sh <run>` writes both. If some tests had to be run again (a missed key-up, a
wrong setting), rerun just those into a new output directory with the same firmware and bench, and
export them together, `scripts/export-run.sh -r <rerun> <run>`: the rerun replaces those tests in
both files, Robot's log marks them as re-executed with their old status, and `measurements.json`
lists the reruns. Each records its provenance: the firmware, the radio unit, this repository's
commit, the station's public name, and the method, dates and fingerprint of the bench calibration.
Units of the same model measure differently, so name the radio with its model and serial number,
`-M "Hardware:Retevis C62 2501R0621100413"`. The bench stays private: results never hold its
instruments' serial numbers, its calibration values, absolute paths or raw captures. The script and
CI check for absolute paths. The results website shows the rest (see the README).

## Rules

- **Append only.** A wrong result is superseded by a new run, never edited.
- **One folder per run**, added in a PR with the change it supports or with the release. A run's
  reruns are merged into it, not published beside it.
- **Results are read against their suite commit.** Limits may change later; a result keeps the meaning
  it had when it ran.
