# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
The emulator bench: an OpenRTX Linux build (openrtx_linux) standing in for a
radio's microphone chain, with the radio file's mic_chain model in front of
it. The Robot library opens one per run of acceptance/m17/mic-chain.robot;
the run's firmware is one variant (a set of C defines, emu_campaign.VARIANTS)
of the OpenRTX checkout, built with the radio's CONFIG_MIC_* values.

Paths to the checkout, the build root, the speech files and the take cache
belong to the tester's machine: they come from a local bench file or the
command line, never from a committed file, and never reach the results.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

import numpy as np

from oat import emu
from oat import emu_campaign as camp
from oat import emu_report
from oat.sanitize import scrub

REPO = Path(__file__).resolve().parents[2]
METHODS = REPO / "docs" / "emulator-mic-campaign.md"
SPEECH = ["OSR_us_000_0010_8k.wav", "OSR_us_000_0011_8k.wav",
          "OSR_us_000_0030_8k.wav", "OSR_us_000_0031_8k.wav"]
# Bench settings that are paths on the tester's machine
LOCAL = ("openrtx", "build_root", "speech_dir", "takes", "reuse_takes")


def is_emulator(bench: dict) -> bool:
    return bench.get("type") == "emulator"


def split_firmware(firmware: str) -> tuple[str, str]:
    """'agc@62e065ae5' -> ('agc', '62e065ae5'), refusing anything else."""
    variant, _, sha = str(firmware).partition("@")
    if not variant or len(sha) < 7 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError(f"FIRMWARE {firmware!r}: on the emulator bench it is "
                         "<variant>@<OpenRTX commit sha>, such as baseline@62e065ae5")
    return variant, sha


def settings(bench: dict, overrides: dict) -> dict:
    """The bench's emulator settings with any non-empty overrides on top."""
    out = dict(bench.get("emulator") or {})
    out.update({k: v for k, v in overrides.items() if v not in (None, "")})
    for k in LOCAL:
        if out.get(k):
            out[k] = (Path(os.path.expanduser(str(out[k]))).resolve()
                      if k != "reuse_takes" else
                      [Path(os.path.expanduser(str(p))).resolve()
                       for p in str(out[k]).split(",") if p])
    return out


class EmulatorSession:
    """One variant of the firmware, built for one radio's mic chain."""

    def __init__(self, bench: dict, radio: dict, radio_file: Path, firmware: str,
                 variant: str, **overrides):
        s = settings(bench, overrides)
        if not s.get("openrtx"):
            raise ValueError("The emulator bench needs the OpenRTX checkout: "
                             "-v OPENRTX:<path>, or emulator.openrtx in a local "
                             "bench file")
        if "mic_chain" not in radio:
            raise ValueError(f"{radio_file}: no mic_chain section, so the radio "
                             "can't be emulated")
        if variant not in camp.VARIANTS:
            raise ValueError(f"VARIANT {variant!r}: one of {', '.join(camp.VARIANTS)}")
        fw_variant, fw_sha = split_firmware(firmware)
        if fw_variant != variant:
            raise ValueError(f"FIRMWARE names variant {fw_variant!r}, "
                             f"VARIANT is {variant!r}")
        self.openrtx = s["openrtx"]
        prov = emu.provenance(self.openrtx)
        if not prov["sha"].startswith(fw_sha):
            raise ValueError(f"FIRMWARE names OpenRTX {fw_sha}, the checkout is at "
                             f"{prov['sha'][:12]}")
        self.patch = prov.pop("diff")
        self.provenance = prov
        self.radio_file = Path(radio_file)
        self.mic = radio["mic_chain"]
        self.profile = emu.Profile.from_radio(self.radio_file.stem, self.mic)
        self.limits = self.mic.get("limits") or {}
        self.variant = variant
        self.defines = camp.VARIANTS[variant]
        self.float_chain = camp.float_chain(self.defines)
        self.jobs = int(s.get("jobs", 6))
        self.speech_dir = s.get("speech_dir")
        self.takes = s.get("takes") or (REPO / "runs" / "emulator-takes"
                                        / self.radio_file.stem)
        self.reuse = s.get("reuse_takes") or []
        build_root = s.get("build_root") or self.openrtx
        self.build_dir = Path(build_root) / f"build_emu_{self.radio_file.stem}_{variant}"
        self.binary = emu.build(self.openrtx, self.build_dir, self.profile, self.defines)
        self.binary_sha = emu.sha256(self.binary)
        self.rows: list[dict] = []
        self.envs: list[dict] = []
        self.recipes: dict[str, list[dict]] = {}
        self.started = datetime.datetime.now(datetime.timezone.utc)

    def describe(self) -> dict:
        """Provenance for the suite metadata and run.json: names, never paths."""
        return {
            "variant": self.variant, "defines": list(self.defines),
            "c_args": " ".join([self.profile.c_args(), *self.defines]),
            "float_chain": self.float_chain,
            "openrtx": {**self.provenance, "patch": "openrtx.patch" if self.patch else None},
            "build_dir": self.build_dir.name, "binary_sha256": self.binary_sha,
            "profile": vars(self.profile),
            "mic_chain_sources": self.mic.get("sources", {}),
        }

    def measure(self, test: str, recipes: list[dict], step_overs: int = 2,
                progress=print) -> list[dict]:
        """Run one test's signals (cached takes reused) and keep the rows."""
        if test in ("speech", "gate") and not self.speech_dir:
            raise ValueError("The speech and gate tests need the speech files: "
                             "-v SPEECH_DIR:<directory>")
        rows, envs, _ = camp.run_variant(
            self.variant, self.binary, self.binary_sha, self.float_chain,
            {test: recipes}, self.profile, self.takes, self.speech_dir, self.jobs,
            step_overs, self.reuse, progress=progress)
        self.rows = [r for r in self.rows if r["test"] != test] + rows
        self.envs = [e for e in self.envs if e["test"].split("_")[0] != test] + envs
        self.recipes[test] = recipes
        errors = [r for r in rows if "error" in r]
        if errors:
            raise RuntimeError("; ".join(f"{r['take']}: {r['error']}" for r in errors))
        return rows

    def measured(self, test: str) -> bool:
        return test in self.recipes

    def summary(self, test: str) -> dict:
        """The figures a test checks, from its rows (pre_ before codec2)."""
        mine = [r for r in self.rows if r["variant"] == self.variant and r["test"] == test]
        h = next(iter(emu_report.headline(self.rows, [self.variant])), {})

        def worst(key, f=max):
            vals = [r.get(key) for r in mine if r.get(key) is not None]
            vals = [float(v) for v in vals if np.isfinite(v)]
            return f(vals) if vals else None

        if test == "tones":
            return {"pre_clip_max_percent": 100 * (worst("pre_clip_fraction") or 0.0),
                    "post_clip_max_percent": 100 * (worst("post_clip_fraction") or 0.0)}
        if test == "speech":
            return {"pre_speech_spread_db": h.get("pre_spread_db"),
                    "speech_spread_db": h.get("speech_spread_db"),
                    "stoi_mean": h.get("stoi_mean"), "stoi_deficit": h.get("stoi_deficit"),
                    "pre_clip_max_percent": 100 * (worst("pre_clip_fraction") or 0.0)}
        if test == "onset":
            mine = [r for r in self.rows if r["variant"] == self.variant
                    and r["test"] == "speech"]
            return {"pre_onset_max_dbfs": worst("pre_onset_peak_dbfs"),
                    "pre_onset_median_dbfs": worst("pre_onset_peak_dbfs", np.median),
                    "onset_max_dbfs": worst("onset_peak_dbfs"),
                    "onset_median_dbfs": worst("onset_peak_dbfs", np.median)}
        if test == "step":
            last = max(mine, key=lambda r: r.get("over", 1))
            keys = ("rise_db", "attack_s", "overshoot_db", "release_s",
                    "release_rate_db_s", "loud_dbfs", "after_dbfs")
            return {"over": last.get("over", 1),
                    **{f"pre_{k}": last.get(f"pre_{k}") for k in keys},
                    **{k: last.get(k) for k in keys}}
        if test == "gate":
            return {"pre_pause_drift_max_db": worst("pre_pause_drift_max_db"),
                    "pre_speech_to_pause_db": worst("pre_speech_to_pause_db", min),
                    "pause_drift_max_db": worst("pause_drift_max_db"),
                    "speech_to_pause_db": worst("speech_to_pause_db", min)}
        raise ValueError(test)

    def write_artifacts(self, run_dir: Path, meta: dict) -> None:
        """
        data/ (CSVs, rows.json), charts/ (PNG and SVG), REPORT.md, METHODS.md,
        run.json and openrtx.patch next to measurements.json. Every text is
        scrubbed of absolute paths.
        """
        run_dir = Path(run_dir)
        if not self.rows:
            return
        camp.write_tables(run_dir / "data", self.rows, self.envs)
        emu_report.write_csv(run_dir / "data" / "headline.csv",
                             emu_report.headline(self.rows, [self.variant]))
        finished = datetime.datetime.now(datetime.timezone.utc)
        run = {
            "started": self.started.isoformat(timespec="seconds"),
            "finished": finished.isoformat(timespec="seconds"),
            **meta, **self.describe(),
            "variants": {self.variant: {"defines": list(self.defines)}},
            "tools": tool_versions(), "adc_seed": camp.ADC_SEED,
            "tail_s": camp.TAIL_S,
            "speech_files": {r["file"]: r["sha256"] for rs in self.recipes.values()
                             for r in rs if "sha256" in r},
            "signals": {t: [{"take": camp.take_id(r), **r} for r in rs]
                        for t, rs in self.recipes.items()},
        }
        (run_dir / "run.json").write_text(scrub(json.dumps(run, indent=2)) + "\n")
        if self.patch:
            (run_dir / "openrtx.patch").write_text(self.patch + "\n")
        ch = emu_report.charts(self.rows, self.envs, [self.variant], run_dir / "charts")
        (run_dir / "REPORT.md").write_text(
            scrub(emu_report.report(run, self.rows, [self.variant], ch)) + "\n")
        (run_dir / "METHODS.md").write_text(scrub(
            METHODS.read_text() + f"\n## This run\n\nVariant `{self.variant}` "
            f"({' '.join(self.defines) or 'no extra defines'}) of OpenRTX "
            f"`{self.provenance['describe']}`, radio file "
            f"`{self.radio_file.stem}`, suite `{meta['oat']['describe']}`, started "
            f"{run['started']}.\n"))


def tool_versions() -> dict:
    import importlib.metadata
    import platform
    import shutil
    import subprocess
    v = {"python": platform.python_version()}
    for p in ("numpy", "scipy", "pystoi", "matplotlib", "robotframework"):
        try:
            v[p] = importlib.metadata.version(p)
        except importlib.metadata.PackageNotFoundError:
            pass
    for tool in ("meson", "cc"):
        path = shutil.which(tool)
        if path:
            r = subprocess.run([path, "--version"], capture_output=True, text=True)
            v[tool] = r.stdout.splitlines()[0] if r.stdout else ""
    if shutil.which("dpkg-query"):
        q = subprocess.run(["dpkg-query", "-W", "-f=${Version}", "codec2"],
                           capture_output=True, text=True)
        if q.returncode == 0:
            v["codec2_package"] = q.stdout.strip()
    return v
