# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tables, headline metrics, charts and REPORT.md of an emulator microphone
campaign (oat.emu_campaign). Rows are plain dicts, one per take and over,
with at least variant, test, signal and level; references are rows whose
variant starts with "codec-only".
"""

from __future__ import annotations

import csv
import math
import re
from pathlib import Path

import numpy as np

from oat.emu_campaign import VARIANTS

GREY = "#8a8986"
# Categorical slots in fixed order (the dataviz reference palette, light mode).
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300",
         "#6250d6", "#e34948"]
MARKERS = ["o", "s", "^", "D", "v", "P", "X", "*"]
REF_STYLE = {"codec-only (nominal)": ("#0b0b0b", "--"),
             "codec-only (ideal)": ("#0b0b0b", "--"),
             "codec-only (clean)": ("#52514e", ":")}


def colour(variant: str) -> str:
    """One colour per variant, by its place in VARIANTS, baseline grey."""
    if variant == "baseline":
        return GREY
    names = [v for v in VARIANTS if v != "baseline"]
    i = names.index(variant) if variant in names else len(names)
    return SLOTS[i % len(SLOTS)]


def marker(variant: str) -> str:
    names = list(VARIANTS)
    return MARKERS[(names.index(variant) if variant in names else 0) % len(MARKERS)]


def is_ref(variant: str) -> bool:
    return variant.startswith("codec-only")


# --- tables ------------------------------------------------------------------

def write_csv(path: Path, rows: list[dict]) -> None:
    """A tidy table: the union of the rows' keys, first-seen order."""
    cols: list[str] = []
    for r in rows:
        cols += [k for k in r if k not in cols]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: _cell(r.get(k)) for k in cols})


def _cell(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    if isinstance(v, float):
        return f"{v:.6g}"
    return v


def _num(v) -> float:
    return float("nan") if v is None or v == "" else float(v)


def mean_by(rows: list[dict], key: str, metric: str, **match) -> dict:
    """{key value: mean of metric} over the rows that match."""
    acc: dict = {}
    for r in rows:
        if any(r.get(k) != v for k, v in match.items()) or "error" in r:
            continue
        x = _num(r.get(metric))
        if math.isfinite(x):
            acc.setdefault(r[key], []).append(x)
    return {k: float(np.mean(v)) for k, v in sorted(acc.items())}


def headline(rows: list[dict], variants: list[str]) -> list[dict]:
    """
    The headline metrics per variant:
      speech_spread_db   max - min over input levels of the output active
                         level (averaged over speech files)
      stoi_mean          mean STOI over speech takes
      stoi_deficit       mean of (ideal codec-only STOI - variant STOI) per take
      pre_spread_db      the same for the pre-codec level (the tap)
      onset_median_dbfs  median key-up peak (first 100 ms) of the speech takes,
      pre_onset_median_dbfs  after codec2 and before it
      pre_clip_max       worst pre-codec clip fraction over the speech takes,
                         measured at the tap
      attack_s, release_s, release_rate_db_s, rise_db
                         of the step test's second over (first if only one),
                         from the pre-codec envelope when there is one
      speech_to_pause_db, pause_drift_db
                         of the gate test, averaged over noise levels
    """
    ideal = {(r["signal"], r["level"]): _num(r.get("stoi")) for r in rows
             if r["test"] == "speech" and r["variant"] == "codec-only (ideal)"}
    out = []
    for v in variants:
        h: dict = {"variant": v}
        sp = [r for r in rows if r["variant"] == v and r["test"] == "speech"
              and "error" not in r]
        if sp:
            by = mean_by(rows, "level", "active_dbfs", variant=v, test="speech")
            h["speech_spread_db"] = max(by.values()) - min(by.values())
            st = [_num(r["stoi"]) for r in sp]
            h["stoi_mean"] = float(np.mean(st))
            d = [ideal[(r["signal"], r["level"])] - _num(r["stoi"]) for r in sp
                 if (r["signal"], r["level"]) in ideal]
            h["stoi_deficit"] = float(np.mean(d)) if d else float("nan")
            pre = mean_by(rows, "level", "pre_active_dbfs", variant=v, test="speech")
            if pre:
                h["pre_spread_db"] = max(pre.values()) - min(pre.values())
            h["onset_median_dbfs"] = float(np.median(
                [_num(r["onset_peak_dbfs"]) for r in sp]))
            po = [_num(r.get("pre_onset_peak_dbfs")) for r in sp]
            if all(math.isfinite(x) for x in po):
                h["pre_onset_median_dbfs"] = float(np.median(po))
            clips = [_num(r.get("pre_clip_fraction")) for r in sp]
            if all(math.isfinite(c) for c in clips):
                h["pre_clip_max"] = max(clips)
        st = [r for r in rows if r["variant"] == v and r["test"] == "step"
              and "error" not in r]
        if st:
            s = max(st, key=lambda r: r.get("over", 1))
            p = "pre_" if "pre_attack_s" in s else ""
            for k in ("attack_s", "release_s", "release_rate_db_s", "rise_db"):
                h[k] = s.get(p + k)
        ga = [r for r in rows if r["variant"] == v and r["test"] == "gate"
              and "error" not in r]
        if ga:
            h["speech_to_pause_db"] = float(np.mean([_num(r["speech_to_pause_db"]) for r in ga]))
            h["pause_drift_db"] = float(np.mean([_num(r["pause_drift_db"]) for r in ga]))
        out.append(h)
    return out


def _f(v, fmt="{:.1f}", none="—") -> str:
    x = _num(v) if not isinstance(v, (int, float)) or v is None else v
    if x is None or not math.isfinite(x):
        return none
    return _nz(fmt.format(x))


def _nz(text: str) -> str:
    """A formatted number without the sign of a negative zero ("-0.0")."""
    try:
        return text.lstrip("-+") if float(text) == 0 else text
    except ValueError:
        return text


def _delta(v, b, fmt="{:+.1f}") -> str:
    v, b = _num(v), _num(b)
    if not (math.isfinite(v) and math.isfinite(b)):
        return ""
    return fmt.format(v - b)


# --- charts ------------------------------------------------------------------

def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "figure.dpi": 110, "savefig.dpi": 150, "font.size": 10,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "axes.axisbelow": True, "grid.color": "#e4e3df", "grid.linewidth": 0.8,
        "axes.edgecolor": "#52514e", "axes.labelcolor": "#0b0b0b",
        "xtick.color": "#52514e", "ytick.color": "#52514e",
        "legend.frameon": False, "lines.linewidth": 2})
    return plt


def _save(fig, out: Path, name: str) -> str:
    out.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    for ext in ("png", "svg"):
        fig.savefig(out / f"{name}.{ext}", bbox_inches="tight")
    # The DOCTYPE's public identifier reads like a path to the export's check
    svg = out / f"{name}.svg"
    svg.write_text(re.sub(r"<!DOCTYPE[^>]*>\s*", "", svg.read_text()))
    import matplotlib.pyplot as plt
    plt.close(fig)
    return f"charts/{name}.png"


def _series(ax, x, y, v):
    if is_ref(v):
        c, ls = REF_STYLE.get(v, ("#0b0b0b", "--"))
        ax.plot(x, y, color=c, ls=ls, lw=1.5, label=v)
    else:
        ax.plot(x, y, color=colour(v), marker=marker(v), ms=6, label=v,
                zorder=3 if v != "baseline" else 2)


def chart_tones(rows, variants, out, pre: bool = False) -> str | None:
    tones = [r for r in rows if r["test"] == "tones"]
    metric = "pre_rms_dbfs" if pre else "rms_dbfs"
    if not any(r.get(metric) is not None for r in tones):
        return None
    plt = _plt()
    sigs = sorted({r["signal"] for r in tones})
    fig, axes = plt.subplots(1, len(sigs), figsize=(5.2 * len(sigs), 4.2),
                             sharey=True, squeeze=False)
    for ax, s in zip(axes[0], sigs):
        for v in [*variants, "codec-only (nominal)"]:
            by = mean_by(tones, "level", metric, variant=v, signal=s)
            if by:
                _series(ax, list(by), list(by.values()), v)
        ax.set_title(f"{s}: level {'into codec2 (tap)' if pre else 'after codec2'}")
        ax.set_xlabel("Input peak (dBFS of ADC range)")
    axes[0][0].set_ylabel(("Pre-codec" if pre else "Output") + " RMS (dBFS)")
    axes[0][-1].legend(loc="lower right", fontsize=8)
    return _save(fig, out, "level-tones-precodec" if pre else "level-tones")


def chart_speech_level(rows, variants, out, pre: bool = False) -> str | None:
    sp = [r for r in rows if r["test"] == "speech"]
    metric = "pre_active_dbfs" if pre else "active_dbfs"
    if not any(r.get(metric) is not None for r in sp):
        return None
    plt = _plt()
    fig, ax = plt.subplots(figsize=(6.4, 4.4))
    for v in [*variants, "codec-only (nominal)"]:
        by = mean_by(sp, "level", metric, variant=v)
        if by:
            _series(ax, list(by), list(by.values()), v)
    ax.axhline(-21, color="#52514e", lw=1, ls=":")
    ax.annotate("−21 dBFS (AGC target)", (ax.get_xlim()[0], -21), xytext=(4, 4),
                textcoords="offset points", fontsize=8, color="#52514e")
    ax.set_xlabel("Input speech peak (dBFS of ADC range)")
    if pre:
        ax.set_ylabel("Active speech level into codec2 (dBFS)")
        ax.set_title("Speech level in vs pre-codec (tap; mean over speech files)")
    else:
        ax.set_ylabel("Output active speech level (dBFS)")
        ax.set_title("Speech level in vs out after codec2 (mean over speech files)")
    ax.legend(fontsize=8)
    return _save(fig, out, "level-speech-precodec" if pre else "level-speech")


def chart_stoi(rows, variants, out) -> str | None:
    sp = [r for r in rows if r["test"] == "speech"]
    if not sp:
        return None
    plt = _plt()
    fig, ax = plt.subplots(figsize=(6.4, 4.4))
    for v in [*variants, "codec-only (ideal)", "codec-only (clean)"]:
        by = mean_by(sp, "level", "stoi", variant=v)
        if by:
            _series(ax, list(by), list(by.values()), v)
    ax.set_xlabel("Input speech peak (dBFS of ADC range)")
    ax.set_ylabel("STOI vs clean source")
    ax.set_title("Intelligibility (mean over speech files)")
    ax.legend(fontsize=8, loc="lower right")
    return _save(fig, out, "stoi")


def chart_envelopes(envs, variants, test, out, title) -> str | None:
    """envs: rows of variant, test, over, t_s array, level_dbfs array."""
    e = [x for x in envs if x["test"] == test]
    if not e:
        return None
    plt = _plt()
    overs = sorted({x["over"] for x in e})
    sigs = sorted({x["signal"] for x in e})
    fig, axes = plt.subplots(len(sigs), len(overs), figsize=(6.2 * len(overs), 3.6 * len(sigs)),
                             sharex=True, sharey=True, squeeze=False)
    for i, s in enumerate(sigs):
        for j, o in enumerate(overs):
            ax = axes[i][j]
            for v in [*variants, "codec-only (nominal)"]:
                for x in e:
                    if x["variant"] == v and x["over"] == o and x["signal"] == s:
                        if is_ref(v):
                            ax.plot(x["t_s"], x["level_dbfs"], color="#0b0b0b",
                                    ls="--", lw=1.2, label=v)
                        else:
                            ax.plot(x["t_s"], x["level_dbfs"], color=colour(v),
                                    lw=1.6, label=v)
            what = "first over (fresh boot)" if o == 1 else f"over {o} (same session)"
            ax.set_title(f"{s}: {what}" if len(sigs) > 1 else what, fontsize=10)
            ax.set_ylabel(("Pre-codec" if test.endswith("_precodec") else "Output")
                          + " level, 50 ms RMS (dBFS)")
    for ax in axes[-1]:
        ax.set_xlabel("Time from input start (s)")
    axes[0][-1].legend(fontsize=8, loc="lower right")
    fig.suptitle(title)
    return _save(fig, out, "envelope-" + test.replace("_", "-"))


def chart_onset(rows, variants, out) -> str | None:
    sp = [r for r in rows if r["test"] == "speech" and r["variant"] in variants
          and "error" not in r]
    if not sp:
        return None
    plt = _plt()
    panels = [("onset_peak_dbfs", "after codec2")]
    if all(r.get("pre_onset_peak_dbfs") is not None for r in sp):
        panels.insert(0, ("pre_onset_peak_dbfs", "into codec2 (tap)"))
    fig, axes = plt.subplots(1, len(panels), figsize=(6.0 * len(panels), 3.8),
                             sharey=True, squeeze=False)
    vs = [v for v in variants if any(r["variant"] == v for r in sp)]
    floor = min(-80.0, min(_num(r[k]) for r in sp for k, _ in panels) - 5)
    for ax, (key, what) in zip(axes[0], panels):
        for i, v in enumerate(vs):
            x = [_num(r[key]) for r in sp if r["variant"] == v]
            med = float(np.median(x))
            ax.bar(i, med - floor, bottom=floor, width=0.6, color=colour(v))
            ax.errorbar(i, med, yerr=[[med - min(x)], [max(x) - med]], color="#0b0b0b",
                        capsize=4, lw=1)
            ax.annotate(_nz(f"{med:.1f}"), (i + 0.3, med), xytext=(4, 0),
                        textcoords="offset points", ha="left", va="center", fontsize=8)
        ax.set_xticks(range(len(vs)), vs, rotation=20 if len(vs) > 4 else 0)
        ax.set_ylim(floor, 3)
        ax.set_title(what, fontsize=10)
    axes[0][0].set_ylabel("Peak in first 100 ms (dBFS)")
    fig.suptitle("Key-up onset peak (median, range over speech takes)")
    return _save(fig, out, "onset")


def chart_gate(rows, variants, out) -> str | None:
    ga = [r for r in rows if r["test"] == "gate" and "error" not in r]
    if not ga:
        return None
    plt = _plt()
    levels = sorted({r["level"] for r in ga})
    vs = [v for v in [*variants, "codec-only (nominal)"] if any(r["variant"] == v for r in ga)]
    fig, axes = plt.subplots(1, len(levels), figsize=(5.6 * len(levels), 4),
                             sharey=True, squeeze=False)
    for ax, lv in zip(axes[0], levels):
        for i, v in enumerate(vs):
            rs = [r for r in ga if r["variant"] == v and r["level"] == lv]
            if not rs:
                continue
            sp = float(np.mean([_num(r["speech_active_dbfs"]) for r in rs]))
            pa = float(np.mean([_num(r["pause_dbfs"]) for r in rs]))
            c = "#0b0b0b" if is_ref(v) else colour(v)
            ax.bar(i - 0.19, sp + 90, bottom=-90, width=0.36, color=c)
            ax.bar(i + 0.19, pa + 90, bottom=-90, width=0.36, color=c, alpha=0.45,
                   hatch="//", edgecolor="white")
        ax.set_xticks(range(len(vs)), [v.replace("codec-only (nominal)", "codec-only")
                                       for v in vs], rotation=20)
        ax.set_title(f"Noise bed {lv:+.0f} dBFS RMS")
        ax.set_ylim(-90, 0)
    axes[0][0].set_ylabel("Output level (dBFS)")
    from matplotlib.patches import Patch
    axes[0][-1].legend(handles=[Patch(color="#52514e", label="speech (active level)"),
                                Patch(color="#52514e", alpha=0.45, hatch="//",
                                      label="pauses (RMS)")], fontsize=8)
    fig.suptitle("Speech vs pause level over a noise bed")
    return _save(fig, out, "gate")


def charts(rows, envs, variants, out: Path) -> dict:
    return {k: p for k, p in {
        "tones": chart_tones(rows, variants, out),
        "tones_pre": chart_tones(rows, variants, out, pre=True),
        "speech": chart_speech_level(rows, variants, out),
        "speech_pre": chart_speech_level(rows, variants, out, pre=True),
        "stoi": chart_stoi(rows, variants, out),
        "step": chart_envelopes(envs, variants, "step", out,
                                "Level step response after codec2: +20 dB then −20 dB at the ADC"),
        "step_pre": chart_envelopes(envs, variants, "step_precodec", out,
                                    "Level step response into codec2 (tap): +20 dB then −20 dB "
                                    "at the ADC"),
        "gate_env": chart_envelopes(envs, variants, "gate", out,
                                    "Speech bursts and pauses over a noise bed"),
        "gate_env_pre": chart_envelopes(envs, variants, "gate_precodec", out,
                                        "Speech bursts and pauses over a noise bed, into codec2 "
                                        "(tap)"),
        "onset": chart_onset(rows, variants, out),
        "gate": chart_gate(rows, variants, out),
    }.items() if p}


# --- report ------------------------------------------------------------------

def _table(head: list[str], body: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(head) + " |", "|" + "---|" * len(head),
            *["| " + " | ".join(r) + " |" for r in body], ""]


def report(meta: dict, rows: list[dict], variants: list[str], charts_: dict) -> str:
    o = meta["openrtx"]
    p = meta["profile"]
    L = ["# M17 microphone chain: emulator campaign", "",
         f"OpenRTX `{o['describe']}` ({o['branch']}), profile **{p['name']}** "
         f"(oversample {p['oversample']}, gain {p['gain']}, {p['adc_bits']}-bit ADC, "
         f"noise {p['noise_lsb']} LSB RMS). Run {meta['started']} to {meta['finished']}, "
         f"suite `{meta['oat']['describe']}`.", "",
         "How the bench works, what each metric means and its limits: "
         "[METHODS.md](METHODS.md). Raw tables are in `data/`, provenance in `run.json`.",
         "", "Variants:", ""]
    for v in variants:
        d = meta["variants"][v]["defines"]
        L.append(f"- **{v}**: {' '.join(f'`{x}`' for x in d) if d else 'no extra defines'}")
    L.append("")

    errs = [r for r in rows if "error" in r]
    if errs:
        L += ["**Failed takes:**", ""]
        L += [f"- {r['variant']} {r['take']}: {r['error']}" for r in errs]
        L.append("")

    hl = headline(rows, variants)
    base = next((h for h in hl if h["variant"] == "baseline"), {})
    L += ["## Headline", "",
          "Δ columns are the variant minus baseline. Spread: active speech level, max − min over "
          "the input levels (lower is a steadier level), into codec2 (pre-codec, from the tap) and "
          "after it. Attack and release are from the pre-codec envelope. STOI deficit: codec-only (ideal) STOI "
          "minus the variant's, mean over takes (lower is better). Step and gate figures are "
          "described under their tests.", ""]
    cols = [("pre_spread_db", "pre-codec spread dB", "{:.1f}", "{:+.1f}"),
            ("speech_spread_db", "spread dB", "{:.1f}", "{:+.1f}"),
            ("stoi_mean", "STOI", "{:.3f}", "{:+.3f}"),
            ("stoi_deficit", "STOI deficit", "{:.3f}", "{:+.3f}"),
            ("pre_onset_median_dbfs", "pre-codec onset dBFS", "{:.1f}", "{:+.1f}"),
            ("onset_median_dbfs", "onset dBFS", "{:.1f}", "{:+.1f}"),
            ("pre_clip_max", "pre-codec clip max %", "{:.2f}", None),
            ("attack_s", "attack s", "{:.2f}", None),
            ("release_s", "release s", "{:.2f}", None),
            ("speech_to_pause_db", "speech/pause dB", "{:.1f}", "{:+.1f}"),
            ("pause_drift_db", "pause drift dB", "{:.1f}", "{:+.1f}")]
    cols = [c for c in cols if any(c[0] in h for h in hl)]
    head = ["variant"]
    for k, name, _, d in cols:
        head.append(name)
        if d:
            head.append("Δ")
    body = []
    for h in hl:
        row = [h["variant"]]
        for k, _, f, d in cols:
            val = h.get(k)
            if k == "pre_clip_max":
                row.append(_f(None if val is None else 100 * val, f))
            else:
                row.append(_f(val, f, none="did not settle" if k.endswith("_s") and k in h else "—"))
            if d:
                row.append("" if h["variant"] == "baseline" else _delta(val, base.get(k), d))
        body.append(row)
    L += _table(head, body)

    sp = [r for r in rows if r["test"] == "speech"]
    if sp:
        L += ["## Speech level sweep", ""]
        for c in ("speech_pre", "speech", "stoi", "onset"):
            if c in charts_:
                L += [f"![{c}]({charts_[c]})", ""]
        levels = sorted({r["level"] for r in sp})
        refs = ["codec-only (nominal)", "codec-only (ideal)"]
        for metric, name, f, df in (("pre_active_dbfs", "Pre-codec active level (dBFS, tap)",
                                     "{:.1f}", "{:+.1f}"),
                                    ("active_dbfs", "Output active level (dBFS)", "{:.1f}", "{:+.1f}"),
                                    ("stoi", "STOI", "{:.3f}", "{:+.3f}")):
            L += [f"{name}, mean over speech files, Δ vs baseline in brackets:", ""]
            bb = mean_by(sp, "level", metric, variant="baseline")
            body = []
            for v in [*variants, *refs]:
                by = mean_by(sp, "level", metric, variant=v)
                if not by:
                    continue
                cells = []
                for lv in levels:
                    s = _f(by.get(lv), f)
                    if not is_ref(v) and v != "baseline" and lv in by and lv in bb:
                        s += f" ({_delta(by[lv], bb[lv], df)})"
                    cells.append(s)
                body.append([v, *cells])
            L += _table(["variant", *[f"{lv:+.0f} dBFS" for lv in levels]], body)
        body = []
        for v in [*variants, "codec-only (nominal)"]:
            by = mean_by(sp, "level", "pre_clip_fraction", variant=v)
            model = mean_by(sp, "level", "model_clip_fraction", variant=v)
            if not by:
                continue
            body.append([v, *[_f(None if lv not in by else 100 * by[lv], "{:.2f}")
                              + ("" if lv not in model else f" ({100 * model[lv]:.2f})")
                              for lv in levels]])
        L += ["Pre-codec clip, % of the samples codec2 receives at int16 full scale, measured at "
              "the tap (in brackets, the integer-chain model as a cross-check, where it applies):",
              "", *_table(["variant", *[f"{lv:+.0f} dBFS" for lv in levels]], body)]

    tn = [r for r in rows if r["test"] == "tones"]
    if tn:
        L += ["## Static level curve (tones)", ""]
        for c in ("tones_pre", "tones"):
            if c in charts_:
                L += [f"![{c}]({charts_[c]})", ""]
        for s in sorted({r["signal"] for r in tn}):
            levels = sorted({r["level"] for r in tn if r["signal"] == s})
            for metric, what in (("pre_rms_dbfs", "pre-codec RMS (dBFS, tap)"),
                                 ("rms_dbfs", "output RMS after codec2 (dBFS)")):
                body = []
                for v in [*variants, "codec-only (nominal)"]:
                    by = mean_by(tn, "level", metric, variant=v, signal=s)
                    if by:
                        body.append([v, *[_f(by.get(lv)) for lv in levels]])
                if not body:
                    continue
                note = (" codec2 alone loses level on pure tones, so read the output against "
                        "the codec-only row." if metric == "rms_dbfs" else "")
                L += [f"{s}: {what} by input peak (dBFS of ADC range).{note}", "",
                      *_table(["variant", *[f"{lv:+.0f}" for lv in levels]], body)]

    st = [r for r in rows if r["test"] == "step" and "error" not in r]
    if st:
        L += ["## Level step (AGC dynamics)", ""]
        for c in ("step_pre", "step"):
            if c in charts_:
                L += [f"![{c}]({charts_[c]})", ""]
        L += ["Settled levels are medians of the 50 ms envelope; attack and release are the time "
              "to stay within 2 dB of the settled level after each step; rise is the output step "
              "for the +20 dB input step (20 dB for a linear chain).", ""]
        body = []
        for r in sorted(st, key=lambda r: (r.get("over", 1), _order(r["variant"], variants))):
            for p, where in (("pre_", "pre-codec"), ("", "output")):
                if p + "rise_db" not in r:
                    continue
                body.append([r["variant"], str(r.get("over", 1)), where,
                             _f(r[p + "before_dbfs"]), _f(r[p + "loud_dbfs"]),
                             _f(r[p + "after_dbfs"]), _f(r[p + "rise_db"]),
                             _f(r[p + "attack_s"], "{:.2f}", "did not settle"),
                             _f(r[p + "overshoot_db"]),
                             _f(r[p + "release_s"], "{:.2f}", "did not settle"),
                             _f(r[p + "dip_db"]), _f(r[p + "release_rate_db_s"])])
        L += _table(["variant", "over", "where", "before dBFS", "loud dBFS", "after dBFS", "rise dB",
                     "attack s", "overshoot dB", "release s", "dip dB", "release dB/s"], body)

    ga = [r for r in rows if r["test"] == "gate" and "error" not in r]
    if ga:
        L += ["## Silence and gate", ""]
        for c in ("gate", "gate_env_pre", "gate_env"):
            if c in charts_:
                L += [f"![{c}]({charts_[c]})", ""]
        L += ["Pause level is the RMS in the pauses, 0.3 s in from each edge; drift is the last "
              "second of a pause against its first (positive: the noise is pumped up during the "
              "pause); trend is the last pause against the first.", ""]
        body = []
        for r in sorted(ga, key=lambda r: (r["level"], _order(r["variant"], variants))):
            for p, where in (("pre_", "pre-codec"), ("", "output")):
                if p + "pause_dbfs" not in r:
                    continue
                body.append([r["variant"], f"{r['level']:+.0f}", where,
                             _f(r[p + "speech_active_dbfs"]), _f(r[p + "pause_dbfs"]),
                             _f(r[p + "speech_to_pause_db"]), _f(r[p + "pause_drift_db"]),
                             _f(r[p + "pause_drift_max_db"]), _f(r[p + "pause_trend_db"])])
        L += _table(["variant", "noise dBFS", "where", "speech dBFS", "pause dBFS", "speech/pause dB",
                     "drift dB", "worst drift dB", "trend dB"], body)
    return "\n".join(L)


def _order(v, variants):
    return variants.index(v) if v in variants else len(variants)
