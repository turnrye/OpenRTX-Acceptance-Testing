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

`scripts/export-run.sh <run>` writes both. Each records its provenance: the firmware, this
repository's commit, the station's public name, and the method, dates and fingerprint of the bench
calibration. They never hold calibration values, absolute paths, serial numbers or raw captures; the
script and CI both check. The results website shows them (see the README).

## Rules

- **Append only.** A wrong result is superseded by a new run, never edited.
- **One folder per run**, added in a PR with the change it supports or with the release.
- **Results are read against their suite commit.** Limits may change later; a result keeps the meaning
  it had when it ran.
