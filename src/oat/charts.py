# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Charts of sweep results, as SVG and PNG files next to the run's results and
as a PNG the Robot log embeds.
"""

import base64
import io
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def modulation_sweep(points: list[dict], cal: tuple[int, int],
                     deviation_limits: tuple[float, float] | None,
                     best: dict | None, title: str,
                     out_base: Path) -> tuple[list[Path], str]:
    """
    EVM and outer deviation against Mod1, one line per Mod2, from a
    modulation sweep's points (mod1, mod2, evm_percent, outer_deviation_hz;
    points without a measurement are left out).

    @param points: the sweep's points.
    @param cal: the radio's calibration pair, Mod1 and Mod2.
    @param deviation_limits: the outer deviation's acceptance range, shaded.
    @param best: the point picked, marked, or None.
    @param title: the chart's title.
    @param out_base: path without suffix for the SVG and PNG files.
    @return the files written, and the PNG as base64 for the log.
    """
    fig, (ax_evm, ax_dev) = plt.subplots(2, 1, sharex=True, figsize=(8, 7))
    for mod2 in sorted({p["mod2"] for p in points}):
        line = sorted((p for p in points if p["mod2"] == mod2
                       and p.get("evm_percent") is not None),
                      key=lambda p: p["mod1"])
        if not line:
            continue
        x = [p["mod1"] for p in line]
        ax_evm.plot(x, [p["evm_percent"] for p in line], marker="o",
                    label=f"Mod2 {mod2}")
        ax_dev.plot(x, [p["outer_deviation_hz"] for p in line], marker="o",
                    label=f"Mod2 {mod2}")

    if deviation_limits is not None:
        ax_dev.axhspan(*deviation_limits, color="green", alpha=0.1,
                       label="outer deviation limits")
    ax_evm.axvline(cal[0], color="grey", linestyle=":",
                   label=f"calibration Mod1 {cal[0]}")
    ax_dev.axvline(cal[0], color="grey", linestyle=":")
    if best is not None:
        ax_evm.plot(best["mod1"], best["evm_percent"], marker="*",
                    markersize=16, color="black", linestyle="none",
                    label=f"picked {best['mod1']}/{best['mod2']}")
        ax_dev.plot(best["mod1"], best["outer_deviation_hz"], marker="*",
                    markersize=16, color="black", linestyle="none")

    ax_evm.set_ylabel("EVM (%)")
    ax_dev.set_ylabel("outer deviation (Hz)")
    ax_dev.set_xlabel("Mod1 (HR_C6000 0x46)")
    ax_evm.set_title(title)
    ax_evm.grid(True, alpha=0.3)
    ax_dev.grid(True, alpha=0.3)
    ax_evm.legend(fontsize="small")
    ax_dev.legend(fontsize="small")
    fig.tight_layout()

    out_base.parent.mkdir(parents=True, exist_ok=True)
    files = []
    for suffix in (".svg", ".png"):
        path = out_base.with_suffix(suffix)
        fig.savefig(path, dpi=100)
        files.append(path)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80)
    plt.close(fig)
    return files, base64.b64encode(buf.getvalue()).decode("ascii")
