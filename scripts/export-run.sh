#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Add a bench run to the results tree, as
#   <results>/<firmware sha>/<radio>/<yyyymmdd-hhmmss>-<station>-<suite>/
#     output.xml         Robot's output, through rebot with oat.sanitize
#     measurements.json  the run's measurements and operator answers
# With -r, a rerun of some of the run's tests (robot -d <rerun> --include ...,
# same firmware, radio and bench) replaces those tests in both files, as
# rebot --merge does. Refuses to overwrite a run, and removes the export again
# if an absolute path survived, so nothing from the tester's machine reaches a
# commit.
#
# Usage: scripts/export-run.sh [-r rerun outputdir]... <robot outputdir> [results dir]

set -euo pipefail
usage="usage: scripts/export-run.sh [-r rerun]... <robot outputdir> [results dir]"
reruns=()
while getopts "r:" opt; do
    case $opt in
        r) reruns+=("$OPTARG") ;;
        *) echo "$usage" >&2; exit 1 ;;
    esac
done
shift $((OPTIND - 1))
run=${1:?$usage}
results=${2:-results}
outputs=("$run/output.xml")
merge=()
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
n=0
for r in ${reruns[@]+"${reruns[@]}"}; do
    n=$((n + 1))
    # A rerun of tests from only some of a run's suites is named after them;
    # rebot --merge takes it under the run's top suite name
    root=$(uv run python -c 'import sys; from pathlib import Path
from oat.results import rerun_root
print(rerun_root(Path(sys.argv[1]), Path(sys.argv[2])) or "")' "$run" "$r")
    if [ -n "$root" ]; then
        rc=0
        uv run rebot --name "$root" --output "$tmp/rerun$n.xml" --log NONE \
            --report NONE "$r/output.xml" >/dev/null || rc=$?
        if [ "$rc" -gt 250 ]; then
            echo "rebot failed ($rc)" >&2
            exit 1
        fi
        outputs+=("$tmp/rerun$n.xml")
    else
        outputs+=("$r/output.xml")
    fi
    merge=(--merge)
done

# The git sha in the firmware name (v0.4.5-88-g1ceed9f8 -> 1ceed9f8), or the
# name made path-safe when it has none (stock-1.2). A run of several suites
# is published as one, named after its top suite.
dest=$(uv run python - "$run" "$results" <<'PY'
import re, sys
from pathlib import Path
from oat.results import run_measurements
m = run_measurements(Path(sys.argv[1]))
safe = lambda s: re.sub(r"[^A-Za-z0-9._-]", "_", s)
sha = re.search(r"(?:^|-g)([0-9a-f]{7,40})$", m["firmware"])
firmware = sha.group(1) if sha else safe(m["firmware"])
start = m["started"][:19].replace("-", "").replace(":", "").replace("T", "-")
run = safe(f"{start}-{m['station']}-{m['suite']}")
print(f"{sys.argv[2]}/{firmware}/{m['radio_file']}/{run}")
PY
)
if [ -e "$dest" ]; then
    echo "$dest exists: results are append-only" >&2
    exit 1
fi
mkdir -p "$dest"
trap 'rm -rf "$dest"' ERR

# rebot exits with the number of failed tests; only above 250 is an error
rc=0
uv run rebot ${merge[@]+"${merge[@]}"} --prerebotmodifier oat.sanitize.Sanitize \
    --output "$dest/output.xml" --log NONE --report NONE \
    "${outputs[@]}" >/dev/null || rc=$?
if [ "$rc" -gt 250 ]; then
    echo "rebot failed ($rc)" >&2
    false
fi
uv run python - "$dest/measurements.json" "$run" ${reruns[@]+"${reruns[@]}"} <<'PY'
import json, sys
from pathlib import Path
from oat.results import merge_reruns
from oat.sanitize import scrub
merged = merge_reruns(Path(sys.argv[2]), [Path(p) for p in sys.argv[3:]])
Path(sys.argv[1]).write_text(scrub(json.dumps(merged, indent=2)) + "\n")
PY

uv run python - "$dest" <<'PY'
import sys
from pathlib import Path
from oat.sanitize import private_paths
left = [(p.name, x) for p in Path(sys.argv[1]).iterdir()
        for x in private_paths(p.read_text())]
if left:
    sys.exit(f"absolute paths left, export removed: {left[:3]}")
PY
echo "$dest"
