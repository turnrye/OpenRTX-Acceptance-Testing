# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Run an M17 microphone-chain campaign on the OpenRTX emulator: build each
firmware variant, send every test signal through it, score the decoded audio
and write data, charts, REPORT.md and METHODS.md into one directory.

    uv run python scripts/emu-mic-campaign.py --openrtx ~/workspace/OpenRTX \\
        --profile cs7000p --variants baseline prime_dc keep_sum hpf agc full \\
        --speech-dir speech-osr --out runs/mic-campaign

Takes already in --out for the same binary and input are reused, so an
interrupted or extended campaign reruns only what is missing. The method is
docs/emulator-mic-campaign.md.
"""

import argparse
import dataclasses
import datetime as dt
import importlib.metadata
import json
import platform
import shlex
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from oat import emu
from oat import emu_campaign as camp
from oat import emu_report

REPO = Path(__file__).resolve().parents[1]
SPEECH = ["OSR_us_000_0010_8k.wav", "OSR_us_000_0011_8k.wav",
          "OSR_us_000_0030_8k.wav", "OSR_us_000_0031_8k.wav"]


def parse_segments(text: list[str]) -> list[tuple[float, float]]:
    """'3:-30 3:-10 6:-30' -> [(3, -30), (3, -10), (6, -30)]."""
    return [(float(s.split(":")[0]), float(s.split(":")[1])) for s in text]


def recipes(a, speech_dir: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if "tones" in a.tests:
        out["tones"] = [camp.tone_recipe(f, lv, a.tone_seconds)
                        for f in a.tone_freqs for lv in a.tone_levels]
    files = [speech_dir / s for s in a.speech]
    if "speech" in a.tests:
        out["speech"] = [camp.speech_recipe(f, lv, a.seconds)
                         for f in files for lv in a.speech_levels]
    if "step" in a.tests:
        out["step"] = [camp.step_recipe(parse_segments(a.step))]
    if "gate" in a.tests:
        out["gate"] = [camp.gate_recipe(files[0], nl, a.gate_peak)
                       for nl in a.gate_noise]
    return out


def level_of(r: dict):
    if r["kind"] in ("tone", "speech"):
        return r["peak_dbfs"]
    if r["kind"] == "gate":
        return r["noise_dbfs"]
    return None


def tool_versions() -> dict:
    v = {"python": platform.python_version()}
    for p in ("numpy", "scipy", "pystoi", "matplotlib"):
        try:
            v[p] = importlib.metadata.version(p)
        except importlib.metadata.PackageNotFoundError:
            pass
    for tool in ("c2enc", "c2dec", "meson", "cc"):
        path = shutil.which(tool)
        if not path:
            continue
        v[tool] = Path(path).name
        if tool in ("meson", "cc"):
            r = subprocess.run([path, "--version"], capture_output=True, text=True)
            v[tool] = r.stdout.splitlines()[0] if r.stdout else ""
    q = subprocess.run(["dpkg-query", "-W", "-f=${Version}", "codec2"],
                       capture_output=True, text=True) if shutil.which("dpkg-query") else None
    if q is not None and q.returncode == 0:
        v["codec2_package"] = q.stdout.strip()
    return v


def oat_provenance() -> dict:
    def git(*args):
        return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True,
                              text=True).stdout.strip()
    return {"commit": git("rev-parse", "HEAD"),
            "describe": git("describe", "--always", "--dirty", "--tags")}


def home_relative(text: str) -> str:
    return text.replace(str(Path.home()), "~")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--openrtx", type=Path, required=True, help="OpenRTX checkout")
    ap.add_argument("--profile", default="cs7000p", choices=emu.PROFILES)
    ap.add_argument("--noise-lsb", type=float, default=2.0,
                    help="ADC noise, LSB RMS (default 2)")
    ap.add_argument("--variants", nargs="+", default=list(camp.VARIANTS),
                    choices=camp.VARIANTS)
    ap.add_argument("--tests", nargs="+", default=list(camp.TESTS), choices=camp.TESTS)
    ap.add_argument("--build-root", type=Path, default=None,
                    help="where build_emu_<profile>_<variant> go (default: --openrtx)")
    ap.add_argument("--binary", action="append", default=[], metavar="VARIANT=PATH",
                    help="use a prebuilt openrtx_linux for a variant instead of building")
    ap.add_argument("--speech-dir", type=Path, default=None,
                    help="directory holding the Open Speech Repository files")
    ap.add_argument("--speech", nargs="+", default=SPEECH)
    ap.add_argument("--seconds", type=float, default=12.0, help="speech length")
    ap.add_argument("--speech-levels", type=float, nargs="+",
                    default=[-40, -30, -20, -12, -6, -1])
    ap.add_argument("--tone-freqs", type=float, nargs="+", default=[1000, 400])
    ap.add_argument("--tone-levels", type=float, nargs="+",
                    default=[float(x) for x in range(-50, 1, 3)] + [0.0])
    ap.add_argument("--tone-seconds", type=float, default=2.0)
    ap.add_argument("--step", nargs="+", default=["3:-30", "3:-10", "6:-30"],
                    metavar="SECONDS:DBFS")
    ap.add_argument("--step-overs", type=int, default=2,
                    help="key-ups of the step signal in one emulator session")
    ap.add_argument("--gate-noise", type=float, nargs="+", default=[-55, -40],
                    help="noise bed RMS levels, dBFS of ADC range")
    ap.add_argument("--gate-peak", type=float, default=-12.0)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--keep-raw", action="store_true",
                    help="keep each take's mic.raw and tx.raw")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    a.tone_levels = sorted(set(a.tone_levels))
    if {"speech", "gate"} & set(a.tests) and a.speech_dir is None:
        ap.error("--speech-dir is needed for the speech and gate tests")

    started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    profile = dataclasses.replace(emu.PROFILES[a.profile], noise_lsb=a.noise_lsb)
    out = a.out
    (out / "data").mkdir(parents=True, exist_ok=True)
    openrtx = a.openrtx.resolve()
    prov = emu.provenance(openrtx)
    diff = prov.pop("diff")
    if diff:
        (out / "openrtx.patch").write_text(diff + "\n")
    prov["patch"] = "openrtx.patch" if diff else None

    # Build (or adopt) one binary per variant.
    prebuilt = dict(b.split("=", 1) for b in a.binary)
    build_root = (a.build_root or openrtx).resolve()
    binaries, vmeta = {}, {}
    for v in a.variants:
        defines = camp.VARIANTS[v]
        if v in prebuilt:
            binary = Path(prebuilt[v]).resolve()
            src = {"prebuilt": binary.parent.name}
        else:
            bdir = build_root / f"build_emu_{profile.name}_{v}"
            print(f"building {v} in {bdir.name} ...", flush=True)
            binary = emu.build(openrtx, bdir, profile, defines)
            src = {"build_dir": bdir.name}
        binaries[v] = (binary, emu.sha256(binary))
        vmeta[v] = {"defines": list(defines), "c_args": " ".join([profile.c_args(), *defines]),
                    **src, "binary_sha256": binaries[v][1],
                    "float_chain": camp.float_chain(defines)}

    speech_dir = a.speech_dir.resolve() if a.speech_dir else None
    sig = recipes(a, speech_dir) if speech_dir else recipes(a, Path("."))

    # Every emulator take, then every reference, in a thread pool.
    lock = threading.Lock()
    done = [0]
    jobs = [(t, v, r) for t, rs in sig.items() for r in rs for v in a.variants]
    total = len(jobs)

    def emu_job(job):
        t, v, r = job
        mic = camp.mic_input(r, profile, speech_dir)
        tid = camp.take_id(r)
        overs = a.step_overs if t == "step" else 1
        meta = camp.run_take(binaries[v][0], binaries[v][1], r, mic, profile,
                             out / "takes" / v / tid, overs=overs, keep_raw=a.keep_raw)
        with lock:
            done[0] += 1
            print(f"[{done[0]}/{total}] {v} {tid}"
                  f"{' (cached)' if meta['cached'] else ''}"
                  f"{' ERROR ' + meta['error'] if 'error' in meta else ''}", flush=True)
        return job, meta

    with ThreadPoolExecutor(a.jobs) as pool:
        results = list(pool.map(emu_job, jobs))

    def score_job(item):
        (t, v, r), meta = item
        tid = camp.take_id(r)
        base = {"variant": v, "profile": profile.name, "test": t,
                "signal": camp.signal_name(r), "level": level_of(r), "take": tid}
        if "error" in meta:
            return [{**base, "error": meta["error"]}], []
        mic = camp.mic_input(r, profile, speech_dir)
        clean = camp.clean_source(r, speech_dir)
        rows, envs = [], []
        for i in range(meta["overs_keyed"]):
            vo = camp.read_voice(out / "takes" / v / tid, i)
            pre = camp.read_voice(out / "takes" / v / tid, i, "precodec")
            m, env = camp.score(r, vo, pre, mic, profile, clean)
            if vmeta[v]["float_chain"]:
                m["model_clip_fraction"] = None
            rows.append({**base, "over": i + 1, **m, **meta["overs"][i]})
            for name, (hop, e) in env.items():
                envs.append({"variant": v, "test": name, "signal": base["signal"],
                             "over": i + 1, "t_s": (np.arange(len(e)) + 0.5) * hop,
                             "level_dbfs": e})
        return rows, envs

    def ref_job(item):
        t, r = item
        mic = camp.mic_input(r, profile, speech_dir)
        clean = camp.clean_source(r, speech_dir)
        rows, envs = [], []
        for name, (pre, x) in camp.references(r, mic, profile, clean).items():
            v = f"codec-only ({name})"
            m, env = camp.score(r, x, pre, mic, profile, clean)
            if name != "nominal":
                m["model_clip_fraction"] = None
            rows.append({"variant": v, "profile": profile.name, "test": t,
                         "signal": camp.signal_name(r), "level": level_of(r),
                         "take": camp.take_id(r), "over": 1, **m})
            for en, (hop, e) in env.items():
                envs.append({"variant": v, "test": en, "signal": camp.signal_name(r),
                             "over": 1, "t_s": (np.arange(len(e)) + 0.5) * hop,
                             "level_dbfs": e})
        return rows, envs

    print("scoring ...", flush=True)
    with ThreadPoolExecutor(a.jobs) as pool:
        scored = list(pool.map(score_job, results))
        scored += list(pool.map(ref_job, [(t, r) for t, rs in sig.items() for r in rs]))
    rows = [x for rs, _ in scored for x in rs]
    envs = [x for _, es in scored for x in es]

    for t in sig:
        emu_report.write_csv(out / "data" / f"{t}.csv", [r for r in rows if r["test"] == t])
    if envs:
        emu_report.write_csv(out / "data" / "envelopes.csv", [
            {"variant": e["variant"], "test": e["test"], "signal": e["signal"],
             "over": e["over"], "t_s": float(t), "level_dbfs": float(l)}
            for e in envs for t, l in zip(e["t_s"], e["level_dbfs"])])
    hl = emu_report.headline(rows, a.variants)
    emu_report.write_csv(out / "data" / "headline.csv", hl)

    finished = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    meta = {
        "started": started, "finished": finished,
        "command": home_relative(shlex.join([Path(sys.argv[0]).name, *sys.argv[1:]])),
        "openrtx": prov, "oat": oat_provenance(), "tools": tool_versions(),
        "profile": vars(profile), "variants": vmeta, "tests": list(sig),
        "adc_seed": camp.ADC_SEED, "tail_s": camp.TAIL_S,
        "speech_files": {s: emu.sha256(speech_dir / s) for s in a.speech} if speech_dir else {},
        "signals": {t: [{"take": camp.take_id(r), **r} for r in rs] for t, rs in sig.items()},
        "step_overs": a.step_overs,
    }
    (out / "run.json").write_text(json.dumps(meta, indent=2))

    print("charts and report ...", flush=True)
    ch = emu_report.charts(rows, envs, a.variants, out / "charts")
    (out / "REPORT.md").write_text(emu_report.report(meta, rows, a.variants, ch) + "\n")
    methods = (REPO / "docs" / "emulator-mic-campaign.md").read_text()
    (out / "METHODS.md").write_text(
        methods + f"\n## This run\n\n```sh\n{meta['command']}\n```\n\n"
        f"OpenRTX `{prov['describe']}`, suite `{meta['oat']['describe']}`, "
        f"started {started}.\n")
    print(f"done: {out / 'REPORT.md'}")


if __name__ == "__main__":
    main()
