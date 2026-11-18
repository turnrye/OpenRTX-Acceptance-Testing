#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Build an MD-UV380/390 image with the M17 receive trace from any OpenRTX ref.
#
#   build_trace_fw.sh REF LABEL [OUTDIR]
#
# Makes a temporary detached worktree of REF in $OPENRTX (default
# ~/workspace/OpenRTX), applies the trace, builds openrtx_mduv3x0_wrap and
# copies it to OUTDIR (default ~/workspace/OpenRTX-test-builds) as
# openrtx_mduv3x0_trace_LABEL-SHA_wrap.bin. Commits nothing, flashes nothing.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
main="${OPENRTX:-$HOME/workspace/OpenRTX}"
[ $# -ge 2 ] || { sed -n '6,13p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
ref=$1
label=$2
out=${3:-$HOME/workspace/OpenRTX-test-builds}

sha=$(git -C "$main" rev-parse --verify --short=8 "$ref^{commit}")
tmp=${BUILD_TMP:-${TMPDIR:-/tmp}}
wt=$(mktemp -d "$tmp/m17-trace-build.XXXXXX")
rmdir "$wt"
cleanup() { git -C "$main" worktree remove --force "$wt" 2>/dev/null || true; }
trap cleanup EXIT

git -C "$main" worktree add --detach "$wt" "$sha" >/dev/null
for s in codec2 XPowersLib radio_tool catch2; do
    [ -e "$main/subprojects/$s" ] || continue
    rm -rf "$wt/subprojects/$s"
    ln -s "$main/subprojects/$s" "$wt/subprojects/$s"
done
python3 "$here/apply_trace.py" "$wt" "$label-$sha"

cd "$wt"
meson setup --cross-file cross_cm4.txt build_cm4 -Dcatch2:tests=false >/dev/null
meson compile -C build_cm4 openrtx_mduv3x0_bin openrtx_mduv3x0_wrap >/dev/null

# The stock MD-UV3x0 bootloader runs the image from 0x0800C000
objdump=$(command -v arm-miosix-eabi-objdump || command -v arm-none-eabi-objdump)
vma=$("$objdump" -h build_cm4/openrtx_mduv3x0 | awk '$2 == ".text" { print $4 }')
(( 16#$vma == 0x0800C000 )) || { echo ".text at 0x$vma, expected 0x0800C000" >&2; exit 1; }

mkdir -p "$out"
dst="$out/openrtx_mduv3x0_trace_${label}-${sha}_wrap.bin"
cp build_cm4/openrtx_mduv3x0_wrap "$dst"
size=$(arm-none-eabi-size build_cm4/openrtx_mduv3x0 | tail -1 | awk '{ print "text " $1 " data " $2 " bss " $3 }')
echo "$dst ($size)"
