# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Sweep speech level through the OpenRTX emulator's M17 microphone chain.

For each speech file and each ADC level, shape the speech like the profile's
ADC, transmit it through an openrtx_linux build, decode the M17 baseband with
OAT's decoder and c2dec, and score it: active level, peak, clipping, floor and
STOI against the clean source. A codec-only reference (the clean speech at a
-21 dBFS active level straight through codec2) gives the ceiling.

    uv run python scripts/emu-mic-sweep.py --openrtx ~/workspace/OpenRTX \\
        --binary build_cs7kp/openrtx_linux --profile cs7000p \\
        --speech a.wav b.wav --levels -40 -30 -20 -12 -6 -1 --out runs/x
"""

import argparse
import dataclasses
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

from oat import emu


def load(path: Path, seconds: float) -> np.ndarray:
    rate, x = wavfile.read(path)
    x = x.astype(float)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if rate != emu.CODEC2_RATE:
        x = signal.resample_poly(x, emu.CODEC2_RATE, rate)
    x = x[: int(seconds * emu.CODEC2_RATE)]
    return x / (np.max(np.abs(x)) + 1e-12)


def at_active(x: np.ndarray, target_dbfs: float) -> np.ndarray:
    return x * 10 ** ((target_dbfs - emu.levels(x)["active_dbfs"]) / 20)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--openrtx", type=Path, required=True)
    ap.add_argument("--binary", type=Path, required=True)
    ap.add_argument("--profile", default="cs7000p", choices=emu.PROFILES)
    ap.add_argument("--speech", type=Path, nargs="+", required=True)
    ap.add_argument("--levels", type=float, nargs="+",
                    default=[-40, -30, -20, -12, -6, -1])
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--noise-lsb", type=float, default=None,
                    help="ADC noise in LSB RMS (default: the profile's)")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    profile = emu.PROFILES[a.profile]
    if a.noise_lsb is not None:
        profile = dataclasses.replace(profile, noise_lsb=a.noise_lsb)
    a.out.mkdir(parents=True, exist_ok=True)
    prov = emu.provenance(a.openrtx)
    (a.out / "openrtx.patch").write_text(prov.pop("diff"))
    meta = {"openrtx": prov, "binary": str(a.binary.resolve()),
            "profile": vars(profile), "levels": a.levels,
            "speech": [str(s) for s in a.speech], "seconds": a.seconds}
    (a.out / "run.json").write_text(json.dumps(meta, indent=2))

    jobs = []
    for s in a.speech:
        clean = load(s, a.seconds)
        ref = emu.codec_only(at_active(clean, -21.0))
        jobs.append((s, None, clean, ref))
        for lvl in a.levels:
            jobs.append((s, lvl, clean, None))

    def run(job):
        s, lvl, clean, ref = job
        if lvl is None:
            return {"speech": s.stem, "adc_peak_dbfs": None, "path": "codec-only",
                    **emu.levels(ref), "stoi": emu.stoi(clean, ref)}
        work = a.out / f"{s.stem}_{lvl:+.0f}"
        mic = emu.adc_shape(clean, emu.CODEC2_RATE, profile, lvl)
        tx = emu.transmit(a.binary, mic, emu.CODEC2_RATE * profile.oversample,
                          work)
        overs = emu.decode(tx.baseband)
        if not overs:
            return {"speech": s.stem, "adc_peak_dbfs": lvl, "path": "emulator",
                    "error": "no transmission decoded"}
        v = emu.voice(overs[0])
        wavfile.write(work / "voice.wav", emu.CODEC2_RATE,
                      np.round(v * 32767).astype(np.int16))
        return {"speech": s.stem, "adc_peak_dbfs": lvl, "path": "emulator",
                **emu.levels(v, skip_s=0.1), "stoi": emu.stoi(clean, v),
                "chain_clip_fraction": emu.chain_clip_fraction(mic, profile),
                "lsf_crc_ok": overs[0].lsf_crc_ok,
                "frames": overs[0].stream_frames}

    with ThreadPoolExecutor(a.jobs) as pool:
        rows = list(pool.map(run, jobs))
    (a.out / "results.json").write_text(json.dumps(rows, indent=2, default=str))

    print(f"OpenRTX {prov['describe']} ({prov['branch']}), profile {profile.name}"
          f" (oversample {profile.oversample}, gain {profile.gain},"
          f" ADC noise {profile.noise_lsb} LSB)")
    print("| speech | ADC peak | path | active dBFS | peak dBFS | pre-codec clip % | key-up click dBFS | floor dBFS | STOI |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if "error" in r:
            print(f"| {r['speech']} | {r['adc_peak_dbfs']} | {r['path']} | {r['error']} |||||")
            continue
        lvl = "—" if r["adc_peak_dbfs"] is None else f"{r['adc_peak_dbfs']:+.0f}"
        clip = r.get("chain_clip_fraction")
        onset = r.get("onset_peak_dbfs")
        print(f"| {r['speech']} | {lvl} | {r['path']} | {r['active_dbfs']:.1f} | "
              f"{r['peak_dbfs']:.1f} | "
              f"{'—' if clip is None else f'{100 * clip:.2f}'} | "
              f"{'—' if onset is None else f'{onset:.1f}'} | "
              f"{r['floor_dbfs']:.1f} | {r['stoi']:.3f} |")


if __name__ == "__main__":
    main()
