# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Write the M17 reference vectors that tests/test_m17_codec.py checks oat.m17
against, from libm17, the M17 Foundation's reference C library: callsign
encodings, CRCs, Golay codewords, and the symbols of Link Setup Frames,
stream frames, the preamble and the End of Transmission marker.

Build libm17 as a shared library anywhere outside this repository, then

    gcc -O2 -shared -fPIC -I<libm17> <libm17>/{encode,decode,phy,math,payload}/*.c \\
        <libm17>/m17.c -o libm17.so -lm
    uv run python scripts/m17-reference-vectors.py libm17.so <libm17 commit> \\
        > tests/data/m17-reference.json
"""

import ctypes
import json
import sys

LSFS = [
    # destination, source, TYPE, META
    ("@ALL", "N0CALL", 0x0005, bytes(14)),
    ("KC1ABC-M", "K0RET/P", 0x0005, bytes(range(14))),
    ("#OPENRTX", "AB1CDE", 0x0002, b"OpenRTX bench\0"),
]
STREAM_FNS = [0, 1, 5, 0x7FFF, 0x8006]
PAYLOAD = bytes(range(0x30, 0x40))


class Lsf(ctypes.Structure):
    _fields_ = [("dst", ctypes.c_uint8 * 6), ("src", ctypes.c_uint8 * 6),
                ("type", ctypes.c_uint8 * 2), ("meta", ctypes.c_uint8 * 14),
                ("crc", ctypes.c_uint8 * 2)]


def main() -> None:
    lib = ctypes.CDLL(sys.argv[1])
    out = {"source": f"libm17 {sys.argv[2] if len(sys.argv) > 2 else ''}"
                     .strip(),
           "callsigns": {}, "crc": {}, "golay": {}, "lsf": [], "stream": []}

    def symbols(fn, *args) -> list[int]:
        buf = (ctypes.c_int8 * 192)()
        fn(buf, *args)
        return list(buf)

    for call in ["N0CALL", "@ALL", "#OPENRTX", "K0RET/P", "AB1CDE", "A",
                 "KC1ABC-M"]:
        raw = (ctypes.c_uint8 * 6)()
        lib.encode_callsign_bytes(raw, call.encode())
        out["callsigns"][call] = bytes(raw).hex()
    lib.CRC_M17.restype = ctypes.c_uint16
    for data in [b"", b"A", b"123456789", bytes(range(256))]:
        out["crc"][data.hex()] = lib.CRC_M17(data, len(data))
    lib.golay24_encode.restype = ctypes.c_uint32
    for value in [0, 1, 0x800, 0xABC, 0xFFF]:
        out["golay"][str(value)] = lib.golay24_encode(ctypes.c_uint16(value))

    cnt = ctypes.c_uint32(0)
    pre = (ctypes.c_int8 * 192)()
    lib.gen_preamble_i8(pre, ctypes.byref(cnt), 0)
    out["preamble_lsf"] = list(pre)
    cnt = ctypes.c_uint32(0)
    eot = (ctypes.c_int8 * 192)()
    lib.gen_eot_i8(eot, ctypes.byref(cnt))
    out["eot"] = list(eot)

    lib.LSF_CRC.restype = ctypes.c_uint16
    for dst, src, kind, meta in LSFS:
        lsf = Lsf()
        lib.encode_callsign_bytes(lsf.dst, dst.encode())
        lib.encode_callsign_bytes(lsf.src, src.encode())
        lsf.type[0], lsf.type[1] = kind >> 8, kind & 0xFF
        ctypes.memmove(lsf.meta, meta, 14)
        crc = lib.LSF_CRC(ctypes.byref(lsf))
        lsf.crc[0], lsf.crc[1] = crc >> 8, crc & 0xFF
        entry = {"dst": dst, "src": src, "type": kind, "meta": meta.hex(),
                 "bytes": bytes(lsf).hex(),
                 "symbols": symbols(lib.gen_frame_i8, None, 0,
                                    ctypes.byref(lsf), 0, 0)}
        out["lsf"].append(entry)
        if len(out["stream"]):
            continue
        payload = (ctypes.c_uint8 * 16)(*PAYLOAD)
        for i, fn in enumerate(STREAM_FNS):
            out["stream"].append({
                "lsf": bytes(lsf).hex(), "lich_cnt": i % 6, "fn": fn,
                "payload": PAYLOAD.hex(),
                "symbols": symbols(lib.gen_frame_i8, payload, 1,
                                   ctypes.byref(lsf), i % 6, fn)})
    json.dump(out, sys.stdout, indent=None, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
