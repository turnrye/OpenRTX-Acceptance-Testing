#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Add the M17 receive trace to an OpenRTX source tree.

    apply_trace.py TREE LABEL

Copies m17_trace.{hpp,cpp} into the tree, adds the source to meson.build and
puts four calls into OpMode_M17::rxState, each after an anchor line that
master and the soft-decision decoder (OpenRTX#524) share. Writes nothing
unless every anchor is found exactly once, so a tree that moved on never
gets half a trace.
"""

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent


def insert_after(text: str, pattern: str, make, name: str) -> str:
    """Insert make(indent) on a new line after the one line matching pattern."""
    hits = list(re.finditer(pattern, text, re.M))
    if len(hits) != 1:
        sys.exit(f"apply_trace: anchor '{name}' found {len(hits)} times, "
                 "expected 1")
    m = hits[0]
    indent = re.match(r"[ \t]*", m.group(0)).group(0)
    return text[: m.end()] + "\n" + make(indent) + text[m.end():]


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    tree, label = pathlib.Path(sys.argv[1]), sys.argv[2]
    opmode = tree / "openrtx/src/rtx/OpMode_M17.cpp"
    meson = tree / "meson.build"

    src = opmode.read_text()
    if "m17_trace" in src:
        sys.exit("apply_trace: the tree already has the trace")

    src = insert_after(src, r'^#include "rtx/OpMode_M17\.hpp"[ \t]*$',
                       lambda i: '#include "rtx/m17_trace.hpp"', "include")
    src = insert_after(src,
                       r"^[ \t]*bool\s+lock\s*=\s*demodulator\.isLocked\(\);"
                       r"[ \t]*$",
                       lambda i: f"{i}m17trace_poll();\n{i}m17trace_lock(lock);",
                       "lock")
    src = insert_after(src,
                       r"^[ \t]*auto\s+lsf\s*=\s*decoder\.getLsf\(\);[ \t]*$",
                       lambda i: (f"{i}m17trace_frame(static_cast<uint8_t>"
                                  "(type), decoder);"),
                       "frame")
    src = insert_after(src,
                       r"^[ \t]*codec_pushFrame\(sf\.data\(\)\s*\+\s*8,\s*"
                       r"false\);[ \t]*$",
                       lambda i: f"{i}m17trace_played(sf);", "play")

    build = insert_after(meson.read_text(),
                         r"^[ \t]*'openrtx/src/rtx/OpMode_M17\.cpp',[ \t]*$",
                         lambda i: f"{i}'openrtx/src/rtx/m17_trace.cpp',",
                         "meson")

    header = (HERE / "m17_trace.hpp").read_text().replace(
        "#ifndef M17_TRACE_LABEL",
        f'#define M17_TRACE_LABEL "{label}"\n#ifndef M17_TRACE_LABEL', 1)
    (tree / "openrtx/include/rtx/m17_trace.hpp").write_text(header)
    (tree / "openrtx/src/rtx/m17_trace.cpp").write_text(
        (HERE / "m17_trace.cpp").read_text())
    opmode.write_text(src)
    meson.write_text(build)
    print(f"apply_trace: trace '{label}' added to {tree}")


if __name__ == "__main__":
    main()
