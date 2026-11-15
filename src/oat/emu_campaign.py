# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
An M17 microphone-chain campaign on the OpenRTX emulator: firmware variants
(sets of C defines) against a fixed set of test signals, scored the same way.

Signals are recipes (plain dicts) that regenerate their microphone input bit
for bit, so a run keeps recipes, seeds and checksums rather than the inputs.
Each take (one variant, one signal) runs the emulator once and keeps its
decoded audio; the metrics are computed again from that audio on every
invocation, so changing a metric never needs the emulator again.

docs/emulator-mic-campaign.md describes the method.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

from oat import emu

RATE = emu.CODEC2_RATE
TAIL_S = 0.5                 # silence (ADC bias and noise) after every signal
END_GUARD_S = 0.2            # left out at the end of every output
TARGET_ACTIVE_DBFS = -21.0   # the level the codec references are set to
ADC_SEED = 17

# Variant name -> the C defines it adds to the profile's c_args.
VARIANTS: dict[str, tuple[str, ...]] = {
    "baseline": (),
    "prime_dc": ("-DMIC_EXP_PRIME_DC",),
    "keep_sum": ("-DMIC_EXP_KEEP_SUM",),
    "hpf": ("-DMIC_EXP_HPF",),
    "agc": ("-DMIC_EXP_AGC",),
    "full": ("-DMIC_EXP_PRIME_DC", "-DMIC_EXP_KEEP_SUM", "-DMIC_EXP_HPF",
             "-DMIC_EXP_AGC"),
    # AGC tuning alternatives, read alongside agc: no fast gain reduction,
    # slower attack (and, for agc_mid, a softer limiter knee).
    "agc_slow": ("-DMIC_EXP_AGC", "-DMIC_EXP_AGC_FASTDOWN=0.0f",
                 "-DMIC_EXP_AGC_ATTACK=0.1f"),
    "agc_mid": ("-DMIC_EXP_AGC", "-DMIC_EXP_AGC_FASTDOWN=0.0f",
                "-DMIC_EXP_AGC_ATTACK=0.3f", "-DMIC_EXP_AGC_KNEE=-3.0f"),
}

TESTS = ("tones", "speech", "step", "gate", "listen")


# --- signals -----------------------------------------------------------------

def tone_recipe(freq_hz: float, peak_dbfs: float, seconds: float = 2.0) -> dict:
    return {"kind": "tone", "freq_hz": float(freq_hz),
            "peak_dbfs": float(peak_dbfs), "seconds": float(seconds)}


def speech_recipe(path: Path, peak_dbfs: float, seconds: float = 12.0) -> dict:
    return {"kind": "speech", "file": Path(path).name,
            "sha256": emu.sha256(path), "peak_dbfs": float(peak_dbfs),
            "seconds": float(seconds)}


def step_recipe(segments: list[tuple[float, float]], f0_hz: float = 140.0,
                seed: int = 3) -> dict:
    """A sustained synthetic vowel through (seconds, peak dBFS) segments."""
    return {"kind": "step", "segments": [[float(s), float(p)] for s, p in segments],
            "f0_hz": float(f0_hz), "seed": int(seed)}


def gate_recipe(path: Path, noise_dbfs: float, peak_dbfs: float = -12.0,
                speech_s: float = 3.0, pause_s: float = 4.0, bursts: int = 3,
                noise: str = "pink", seed: int = 5) -> dict:
    """Speech bursts, each followed by a pause, over a constant noise bed."""
    return {"kind": "gate", "file": Path(path).name, "sha256": emu.sha256(path),
            "noise_dbfs": float(noise_dbfs), "peak_dbfs": float(peak_dbfs),
            "speech_s": float(speech_s), "pause_s": float(pause_s),
            "bursts": int(bursts), "noise": noise, "seed": int(seed)}


def take_id(r: dict) -> str:
    """A readable, unique name for a signal recipe."""
    k = r["kind"]
    if k == "tone":
        return f"tone_{r['freq_hz']:.0f}Hz_{r['peak_dbfs']:+.0f}dBFS"
    if k == "speech":
        return f"speech_{Path(r['file']).stem}_{r['peak_dbfs']:+.0f}dBFS"
    if k == "step":
        steps = "_".join(f"{s:g}s{p:+.0f}" for s, p in r["segments"])
        return f"step_{steps}"
    if k == "gate":
        return (f"gate_{Path(r['file']).stem}_{r['peak_dbfs']:+.0f}dBFS"
                f"_{r['noise']}{r['noise_dbfs']:+.0f}dBFS")
    raise ValueError(k)


def signal_name(r: dict) -> str:
    """The signal without its level, for grouping takes."""
    k = r["kind"]
    if k == "tone":
        return f"tone {r['freq_hz']:.0f} Hz"
    if k in ("speech", "gate"):
        return Path(r["file"]).stem
    return "vowel step"


def load_speech(path: Path, seconds: float) -> np.ndarray:
    """A speech file as 8 kHz float, cut to seconds, peak 1."""
    rate, x = wavfile.read(path)
    x = x.astype(float)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if rate != RATE:
        x = signal.resample_poly(x, RATE, rate)
    x = x[: int(seconds * RATE)]
    return x / (np.max(np.abs(x)) + 1e-12)


def vowel(n: int, rate: int, f0_hz: float, seed: int) -> np.ndarray:
    """
    A steady /a/-like vowel: harmonics of f0 up to 3.6 kHz shaped by three
    formants (700, 1220, 2600 Hz), seeded phases, peak 1. Codec2 models voiced
    speech as harmonics, so it passes this much closer to unchanged than a
    pure tone.
    """
    t = np.arange(n) / rate
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    for h in range(1, int(3600 // f0_hz) + 1):
        f = h * f0_hz
        a = 0.02 + sum(1 / (1 + ((f - fc) / bw) ** 2)
                       for fc, bw in ((700, 130), (1220, 70), (2600, 160)))
        x += a * np.sin(2 * np.pi * f * t + rng.uniform(0, 2 * np.pi))
    return x / np.max(np.abs(x))


def colored_noise(n: int, kind: str, seed: int) -> np.ndarray:
    """White or pink (1/f power) Gaussian noise, unit RMS."""
    rng = np.random.default_rng(seed)
    w = rng.normal(size=n)
    if kind == "pink":
        spec = np.fft.rfft(w)
        f = np.arange(len(spec), dtype=float)
        f[0] = 1.0
        spec /= np.sqrt(f)
        spec[0] = 0.0
        w = np.fft.irfft(spec, n)
    elif kind != "white":
        raise ValueError(kind)
    return w / np.sqrt(np.mean(w ** 2))


def content_seconds(r: dict) -> float:
    """The signal's length before the tail."""
    k = r["kind"]
    if k in ("tone", "speech"):
        return r["seconds"]
    if k == "step":
        return float(sum(s for s, _ in r["segments"]))
    return r["bursts"] * (r["speech_s"] + r["pause_s"])


def gate_segments(r: dict) -> tuple[list, list]:
    """(speech, pause) intervals of a gate signal, in seconds."""
    sp, pa, t = [], [], 0.0
    for _ in range(r["bursts"]):
        sp.append((t, t + r["speech_s"]))
        t += r["speech_s"]
        pa.append((t, t + r["pause_s"]))
        t += r["pause_s"]
    return sp, pa


def synth(r: dict, profile: emu.Profile, speech_dir: Path | None = None) -> np.ndarray:
    """
    A recipe's microphone input at the ADC's rate in floats, 1.0 meaning the
    ADC's half range, tail included. emu.adc_quantize turns it into codes.
    """
    rate = RATE * profile.oversample
    k = r["kind"]
    if k == "tone":
        n = int(r["seconds"] * rate)
        x = 10 ** (r["peak_dbfs"] / 20) * np.sin(
            2 * np.pi * r["freq_hz"] * np.arange(n) / rate)
    elif k == "speech":
        s = emu.to_adc_rate(_speech(r, speech_dir, r["seconds"]), RATE, profile)
        x = s / np.max(np.abs(s)) * 10 ** (r["peak_dbfs"] / 20)
    elif k == "step":
        n = int(content_seconds(r) * rate)
        g = np.concatenate([np.full(int(s * rate), 10 ** (p / 20))
                            for s, p in r["segments"]])
        g = np.pad(g, (0, max(0, n - len(g))), mode="edge")[:n]
        x = vowel(n, rate, r["f0_hz"], r["seed"]) * g
    elif k == "gate":
        n = int(content_seconds(r) * rate)
        burst = int(r["speech_s"] * rate)
        s = emu.to_adc_rate(_speech(r, speech_dir, r["bursts"] * r["speech_s"]),
                            RATE, profile)
        s = s / np.max(np.abs(s)) * 10 ** (r["peak_dbfs"] / 20)
        x = np.zeros(n)
        for i, (a, _) in enumerate(gate_segments(r)[0]):
            chunk = s[i * burst:(i + 1) * burst]
            x[int(a * rate): int(a * rate) + len(chunk)] = chunk
        x += 10 ** (r["noise_dbfs"] / 20) * colored_noise(n, r["noise"], r["seed"])
    else:
        raise ValueError(k)
    return np.concatenate([x, np.zeros(int(TAIL_S * rate))])


def _speech(r: dict, speech_dir: Path | None, seconds: float) -> np.ndarray:
    if speech_dir is None:
        raise ValueError(f"{r['file']}: no speech directory given")
    path = Path(speech_dir) / r["file"]
    if emu.sha256(path) != r["sha256"]:
        raise ValueError(f"{path}: checksum differs from the recipe's")
    return load_speech(path, seconds)


def mic_input(r: dict, profile: emu.Profile, speech_dir: Path | None = None) -> np.ndarray:
    """The recipe's ADC codes, as the emulator's microphone file holds them."""
    return emu.adc_quantize(synth(r, profile, speech_dir), profile, ADC_SEED)


def clean_source(r: dict, speech_dir: Path | None) -> np.ndarray | None:
    """The clean speech a speech take is scored against, 8 kHz float."""
    if r["kind"] != "speech":
        return None
    return _speech(r, speech_dir, r["seconds"])


# --- references --------------------------------------------------------------

def ideal_chain(mic: np.ndarray, profile: emu.Profile, clip: bool = True) -> np.ndarray:
    """
    The ADC codes through an ideal linear chain at the profile's gain: bias
    removed, decimated with a proper anti-alias filter, multiplied by the gain,
    in float full scale (clipped at +-1 unless clip is False).
    """
    x = mic.astype(float)
    x = x - x.mean()
    if profile.oversample > 1:
        x = signal.resample_poly(x, 1, profile.oversample)
    x = x * profile.gain / 32768
    return np.clip(x, -1, 1) if clip else x


def at_active(x: np.ndarray, target_dbfs: float = TARGET_ACTIVE_DBFS) -> np.ndarray:
    return x * 10 ** ((target_dbfs - emu.levels(x)["active_dbfs"]) / 20)


def references(r: dict, mic: np.ndarray, profile: emu.Profile,
               clean: np.ndarray | None) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """
    Codec-only references for a take, each straight through codec2 3200, as
    {name: (codec2's input, codec2's output)}:
      nominal: the same ADC codes through the ideal chain at the fixed gain;
      ideal:   the same codes, ideal chain, set to -21 dBFS active (speech);
      clean:   the clean source at -21 dBFS active, no ADC (speech).
    """
    ins = {"nominal": ideal_chain(mic, profile)}
    if r["kind"] == "speech":
        ins["ideal"] = at_active(ideal_chain(mic, profile, clip=False))
        ins["clean"] = at_active(clean)
    return {k: (codec_input(x), emu.codec_only(x)) for k, x in ins.items()}


# --- metrics -----------------------------------------------------------------

def rms_db(x: np.ndarray) -> float:
    return float(10 * np.log10(np.mean(np.asarray(x, float) ** 2) + 1e-12)) if len(x) else float("nan")


def envelope_db(x: np.ndarray, rate: int = RATE, hop_s: float = 0.05) -> np.ndarray:
    """RMS level in dBFS of consecutive hop_s blocks."""
    hop = int(hop_s * rate)
    n = len(x) // hop * hop
    return 10 * np.log10(np.mean(x[:n].reshape(-1, hop) ** 2, axis=1) + 1e-12)


def settle_time(env: np.ndarray, hop_s: float, start: float, stop: float,
                final: float, tol_db: float) -> float | None:
    """
    Seconds from start until env stays within tol_db of final up to stop:
    the end of the last block outside the band. None if it never settles.
    """
    t = (np.arange(len(env)) + 0.5) * hop_s
    sel = np.flatnonzero((t >= start) & (t < stop))
    out = sel[np.abs(env[sel] - final) > tol_db]
    if len(out) == 0:
        return 0.0
    if out[-1] == sel[-1]:
        return None
    return float(t[out[-1]] + hop_s / 2 - start)


def step_response(env: np.ndarray, hop_s: float, t_up: float, t_down: float,
                  t_end: float, tol_db: float = 2.0) -> dict:
    """
    Attack and release of a +step then -step in level, from an envelope
    aligned to the input: the settled levels before, during and after the
    loud segment (medians), the time to come within tol_db of each settled
    level, the overshoot after the rise and the dip after the fall, and the
    release rate (the slope of a line fitted to the recovery from the dip).
    """
    t = (np.arange(len(env)) + 0.5) * hop_s

    def med(a, b):
        return float(np.median(env[(t >= a) & (t < b)]))

    pre = med(t_up - 1.0, t_up - 0.1)
    up = med(t_down - 1.0, t_down - 0.1)
    down = med(t_end - 1.5, t_end - 0.1)
    attack = settle_time(env, hop_s, t_up, t_down - 0.1, up, tol_db)
    release = settle_time(env, hop_s, t_down, t_end - 0.1, down, tol_db)
    overshoot = float(np.max(env[(t >= t_up) & (t < t_up + 1.0)]) - up)
    after = (t >= t_down) & (t < t_down + 1.0)
    dip = float(down - np.min(env[after]))
    rate = float("nan")
    if dip > tol_db:
        # Fit the recovery: from the lowest point after the fall until the
        # envelope first comes within tol_db of the settled level.
        lo = int(np.flatnonzero(after)[np.argmin(env[after])])
        tail = np.flatnonzero((t > t[lo]) & (t < t_end - 0.1)
                              & (np.abs(env - down) <= tol_db))
        hi = int(tail[0]) if len(tail) else int(np.flatnonzero(t < t_end - 0.1)[-1])
        if hi - lo >= 2:
            rate = float(np.polyfit(t[lo:hi + 1], env[lo:hi + 1], 1)[0])
    return {"before_dbfs": pre, "loud_dbfs": up, "after_dbfs": down,
            "rise_db": up - pre, "fall_db": up - down,
            "attack_s": attack, "release_s": release,
            "overshoot_db": overshoot, "dip_db": dip, "release_rate_db_s": rate}


def pause_stats(x: np.ndarray, pauses: list, rate: int = RATE,
                guard_s: float = 0.3, edge_s: float = 1.0) -> dict:
    """
    Level in the pauses of an aligned output, guard_s in from each edge:
    the overall pause level (power mean), the drift within a pause (its last
    edge_s against its first, mean and worst over the pauses) and the change
    from the first pause to the last.
    """
    lv, drift = [], []
    for a, b in pauses:
        seg = x[int((a + guard_s) * rate): int((b - guard_s) * rate)]
        if len(seg) < 2 * edge_s * rate:
            continue
        e = int(edge_s * rate)
        lv.append(rms_db(seg))
        drift.append(rms_db(seg[-e:]) - rms_db(seg[:e]))
    if not lv:
        return {"pause_dbfs": float("nan"), "pause_drift_db": float("nan"),
                "pause_drift_max_db": float("nan"), "pause_trend_db": float("nan")}
    return {"pause_dbfs": float(10 * np.log10(np.mean(10 ** (np.array(lv) / 10)))),
            "pause_drift_db": float(np.mean(drift)),
            "pause_drift_max_db": float(np.max(drift)),
            "pause_trend_db": float(lv[-1] - lv[0])}


def speech_in(x: np.ndarray, segs: list, rate: int = RATE) -> np.ndarray:
    return np.concatenate([x[int(a * rate): int(b * rate)] for a, b in segs])


def _metrics(r: dict, x: np.ndarray, mic: np.ndarray, profile: emu.Profile,
             clean: np.ndarray | None, clip_at: float) -> tuple[dict, dict]:
    """Metrics of one 8 kHz stream (decoded or pre-codec) for its recipe."""
    n = int(content_seconds(r) * RATE)
    k = r["kind"]
    if k == "tone":
        seg = x[int(0.5 * RATE): max(int(0.5 * RATE) + 1, n - int(0.3 * RATE))]
        return {"rms_dbfs": rms_db(seg),
                "clip_fraction": float(np.mean(np.abs(seg) >= clip_at))}, {}
    lag = max(0, emu.find_lag(ideal_chain(mic, profile)[:n], x))
    if k == "speech":
        out = x[: lag + n - int(END_GUARD_S * RATE)]
        lv = emu.levels(out, skip_s=0.1)
        # The key-up onset has its own figures: clipping counts after it
        lv["clip_fraction"] = float(np.mean(np.abs(out[int(0.1 * RATE):]) >= clip_at))
        m = {**lv, "lag_ms": 1000 * lag / RATE}
        if clean is not None:
            m["stoi"] = emu.stoi(clean, out)
        return m, {}
    al = x[lag: lag + n]
    env = envelope_db(al)
    clip = float(np.mean(np.abs(al) >= clip_at))
    if k == "step":
        t_up = r["segments"][0][0]
        t_down = t_up + r["segments"][1][0]
        m = step_response(env, 0.05, t_up, t_down, content_seconds(r))
        return {**m, "clip_fraction": clip, "lag_ms": 1000 * lag / RATE}, {"step": (0.05, env)}
    sp, pa = gate_segments(r)
    act = emu.levels(speech_in(al, sp))["active_dbfs"]
    ps = pause_stats(al, pa)
    return {"speech_active_dbfs": act, **ps, "speech_to_pause_db": act - ps["pause_dbfs"],
            "clip_fraction": clip, "lag_ms": 1000 * lag / RATE}, {"gate": (0.05, env)}


PRE_CLIP = 32767 / 32768      # int16 full scale, either sign
POST_CLIP = 32766 / 32768


def score(r: dict, v: np.ndarray, pre: np.ndarray | None, mic: np.ndarray,
          profile: emu.Profile, clean: np.ndarray | None) -> tuple[dict, dict]:
    """
    Metrics of one over for its recipe: of the decoded output v, and of the
    pre-codec tap (the samples codec2 encoded, 8 kHz float) prefixed pre_,
    with the integer-chain clip model as a cross-check (model_clip_fraction).
    Also the envelopes to keep ({name: (hop_s, dB array)}), the pre-codec
    ones suffixed _precodec.
    """
    m, envs = _metrics(r, v, mic, profile, clean, POST_CLIP)
    m["post_clip_fraction"] = m.pop("clip_fraction")
    if pre is not None:
        pm, penv = _metrics(r, pre, mic, profile, None, PRE_CLIP)
        m.update({f"pre_{k}": val for k, val in pm.items()})
        envs.update({f"{k}_precodec": e for k, e in penv.items()})
    m["model_clip_fraction"] = emu.chain_clip_fraction(mic, profile)
    return m, envs


def codec_input(x: np.ndarray) -> np.ndarray:
    """Float audio as the int16 samples emu.codec_only gives codec2, as float."""
    return np.clip(np.round(x * 32767), -32768, 32767) / 32768


# --- takes -------------------------------------------------------------------

def mic_sha256(mic: np.ndarray) -> str:
    return hashlib.sha256(mic.astype("<i2").tobytes()).hexdigest()


def run_take(binary: Path, binary_sha: str, r: dict, mic: np.ndarray,
             profile: emu.Profile, workdir: Path, overs: int = 1,
             keep_raw: bool = False) -> dict:
    """
    Emulator transmissions of a recipe (overs key-ups in one session), unless
    workdir already holds them for this binary and this input. Leaves
    voice.wav (first over), voice-2.wav (second) ..., the pre-codec tap of
    each over as precodec.wav, precodec-2.wav ..., and take.json.
    """
    workdir = Path(workdir)
    meta_path = workdir / "take.json"
    msha = mic_sha256(mic)
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        if (meta.get("binary_sha256") == binary_sha and meta.get("mic_sha256") == msha
                and meta.get("overs_keyed", 1) == overs and "error" not in meta
                and all((workdir / voice_name(i)).exists() for i in range(overs))
                and (not meta.get("precodec") or all(
                    (workdir / voice_name(i, "precodec")).exists() for i in range(overs)))):
            meta["cached"] = True
            return meta
    workdir.mkdir(parents=True, exist_ok=True)
    tx = emu.transmit(binary, mic, RATE * profile.oversample, workdir, overs=overs)
    meta = {"recipe": r, "binary_sha256": binary_sha, "mic_sha256": msha,
            "overs_keyed": overs}
    decoded = emu.decode(tx.baseband) if tx.baseband.exists() else []
    if len(decoded) < overs:
        meta["error"] = f"{len(decoded)} of {overs} transmissions decoded"
        (workdir / "emulator.log").write_text(tx.log)
    else:
        meta["overs"] = []
        for i, m in enumerate(decoded[:overs]):
            pcm = emu.tools.codec2_decode(m.payload)
            wavfile.write(workdir / voice_name(i), RATE, pcm.astype(np.int16))
            meta["overs"].append({"frames": m.stream_frames,
                                  "lsf_crc_ok": bool(m.lsf_crc_ok)})
        meta["precodec"] = tx.precodec is not None
        if tx.precodec is not None:
            tap = np.fromfile(tx.precodec, dtype="<i2")
            parts = split_tap(tap, ideal_chain(mic, profile), overs)
            for i, part in enumerate(parts):
                wavfile.write(workdir / voice_name(i, "precodec"), RATE, part)
                meta["overs"][i]["precodec_blocks"] = len(part) // 160
    meta_path.write_text(json.dumps(meta, indent=2))
    if not keep_raw:
        (workdir / "mic.raw").unlink(missing_ok=True)
        (workdir / "tx.raw").unlink(missing_ok=True)
        (workdir / "precodec.raw").unlink(missing_ok=True)
        for f in workdir.glob("*.rtxc"):       # the emulator's settings file
            f.unlink()
        shutil.rmtree(workdir / "state", ignore_errors=True)
    meta["cached"] = False
    return meta


def _block_env(x: np.ndarray, block: int = 160) -> np.ndarray:
    """Log energy of consecutive codec2 blocks, floored 40 dB under the top."""
    n = len(x) // block
    e = 10 * np.log10(np.mean(np.asarray(x[: n * block], float).reshape(n, block) ** 2,
                              axis=1) + 1e-12)
    return np.maximum(e, e.max() - 40) if n else e


def split_tap(tap: np.ndarray, ref: np.ndarray, overs: int,
              block: int = 160) -> list[np.ndarray]:
    """
    A session's pre-codec tap (every over's codec2 blocks, back to back) cut
    into overs. The encoder runs for as long as each key-up lasts, which is
    not the number of M17 frames sent, so each over after the first starts
    where the input's level pattern (ref, the ideal chain's 8 kHz signal)
    best matches the tap's, within 20% of an over's length of where it is
    expected: the 20 ms block energies, mean removed, cross-correlated.
    """
    if overs <= 1:
        return [tap]
    e = _block_env(tap, block)
    r = _block_env(ref, block)
    r = r - r.mean()
    n_ref = len(r)
    starts = [0]
    for _ in range(1, overs):
        lo = starts[-1] + int(0.8 * n_ref)
        hi = min(starts[-1] + int(1.2 * n_ref), len(e) - n_ref // 4)
        best, at = -np.inf, None
        for k in range(lo, max(lo, hi) + 1):
            seg = e[k: k + n_ref]
            m = len(seg)
            if m < n_ref // 4:
                break
            c = float(np.dot(seg - seg.mean(), r[:m]) / m)
            if c > best:
                best, at = c, k
        starts.append(at if at is not None else min(lo, len(e)))
    bounds = [s * block for s in starts] + [len(tap) // block * block]
    return [tap[a:b] for a, b in zip(bounds, bounds[1:])]


def read_taps(workdir: Path, overs: int, ref: np.ndarray) -> list[np.ndarray | None]:
    """
    Each over's pre-codec tap as 8 kHz float: the parts saved with the take,
    joined and cut again by split_tap, so takes saved by an older split
    read the same as new ones.
    """
    parts = [read_voice(workdir, i, "precodec") for i in range(overs)]
    if overs <= 1 or any(p is None for p in parts):
        return parts
    pcm = np.round(np.concatenate(parts) * 32768).astype(np.int16)
    return [x.astype(float) / 32768 for x in split_tap(pcm, ref, overs)]


def voice_name(i: int, stem: str = "voice") -> str:
    return f"{stem}.wav" if i == 0 else f"{stem}-{i + 1}.wav"


def read_voice(workdir: Path, i: int = 0, stem: str = "voice") -> np.ndarray | None:
    """An over's decoded audio (stem voice) or pre-codec tap (precodec)."""
    path = Path(workdir) / voice_name(i, stem)
    if not path.exists():
        return None
    _, pcm = wavfile.read(path)
    return pcm.astype(float) / 32768


def float_chain(defines: tuple[str, ...] | list[str]) -> bool:
    """
    Whether a variant runs the firmware's float microphone chain (high-pass
    or AGC), where the fixed gain no longer saturates and
    emu.chain_clip_fraction, a model of the integer chain, does not apply:
    its model_clip_fraction cross-check is left out.
    """
    return any(d in ("-DMIC_EXP_HPF", "-DMIC_EXP_AGC") for d in defines)


# --- one variant through the tests ---------------------------------------------

def level_of(r: dict):
    """The level a take is filed under: peak for tones and speech, noise for gate."""
    if r["kind"] in ("tone", "speech"):
        return r["peak_dbfs"]
    if r["kind"] == "gate":
        return r["noise_dbfs"]
    return None


def default_recipes(tests, speech_dir: Path | None, speech: list[str],
                    seconds: float = 12.0,
                    speech_levels=(-40, -30, -20, -12, -6, -1),
                    tone_freqs=(1000, 400), tone_levels=None,
                    tone_seconds: float = 2.0,
                    step=((3, -30), (3, -10), (6, -30)),
                    gate_noise=(-55, -40), gate_peak: float = -12.0) -> dict:
    """The campaign's signals per test, by default as METHODS describes them."""
    if tone_levels is None:
        tone_levels = [float(x) for x in range(-50, 1, 3)] + [0.0]
    out: dict[str, list[dict]] = {}
    files = [Path(speech_dir) / s for s in speech] if speech_dir else []
    if "tones" in tests:
        out["tones"] = [tone_recipe(float(f), lv, float(tone_seconds)) for f in tone_freqs
                        for lv in sorted(set(float(x) for x in tone_levels))]
    if "speech" in tests:
        out["speech"] = [speech_recipe(f, float(lv), float(seconds)) for f in files
                         for lv in speech_levels]
    if "step" in tests:
        out["step"] = [step_recipe([(float(s), float(p)) for s, p in step])]
    if "gate" in tests:
        out["gate"] = [gate_recipe(files[0], float(nl), float(gate_peak))
                       for nl in gate_noise]
    return out


def cached_take(dirs: list[Path], binary_sha: str, mic_sha: str,
                overs: int) -> Path | None:
    """The first directory holding a finished take of this binary and input."""
    for d in dirs:
        meta_path = Path(d) / "take.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        if (meta.get("binary_sha256") == binary_sha and meta.get("mic_sha256") == mic_sha
                and meta.get("overs_keyed", 1) == overs and "error" not in meta
                and all((Path(d) / voice_name(i)).exists() for i in range(overs))
                and (not meta.get("precodec") or all(
                    (Path(d) / voice_name(i, "precodec")).exists()
                    for i in range(overs)))):
            return Path(d)
    return None


def run_variant(variant: str, binary: Path, binary_sha: str, float_chain_: bool,
                recipes_by_test: dict, profile: emu.Profile, takes: Path,
                speech_dir: Path | None, jobs: int = 6, step_overs: int = 2,
                also_look_in=(), keep_raw: bool = False,
                progress=print) -> tuple[list[dict], list[dict], dict]:
    """
    One variant through every recipe: emulator takes (cached under
    takes/<variant>/<take>, or reused from <dir>/<variant>/<take> for each
    dir in also_look_in), the codec-only references, and the scores of both.
    Returns rows, envelopes and {take id: take directory}.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    lock = threading.Lock()
    done = [0]
    work = [(t, r) for t, rs in recipes_by_test.items() for r in rs]
    total = len(work)
    where: dict[str, Path] = {}

    def emu_job(job):
        t, r = job
        mic = mic_input(r, profile, speech_dir)
        tid = take_id(r)
        overs = step_overs if t == "step" else 1
        home = Path(takes) / variant / tid
        found = cached_take([home, *[Path(d) / variant / tid for d in also_look_in]],
                            binary_sha, mic_sha256(mic), overs)
        if found is not None:
            meta = {**json.loads((found / "take.json").read_text()), "cached": True}
        else:
            found = home
            meta = run_take(binary, binary_sha, r, mic, profile, home, overs=overs,
                            keep_raw=keep_raw)
        with lock:
            done[0] += 1
            where[tid] = found
            progress(f"[{done[0]}/{total}] {variant} {tid}"
                     f"{' (cached)' if meta['cached'] else ''}"
                     f"{' ERROR ' + meta['error'] if 'error' in meta else ''}")
        return job, meta

    with ThreadPoolExecutor(jobs) as pool:
        results = list(pool.map(emu_job, work))

    def env_row(v, name, sig, over, hop, e):
        return {"variant": v, "test": name, "signal": sig, "over": over,
                "t_s": (np.arange(len(e)) + 0.5) * hop, "level_dbfs": e}

    def score_job(item):
        (t, r), meta = item
        tid = take_id(r)
        base = {"variant": variant, "profile": profile.name, "test": t,
                "signal": signal_name(r), "level": level_of(r), "take": tid}
        if "error" in meta:
            return [{**base, "error": meta["error"]}], []
        mic = mic_input(r, profile, speech_dir)
        clean = clean_source(r, speech_dir)
        rows, envs = [], []
        taps = read_taps(where[tid], meta["overs_keyed"], ideal_chain(mic, profile))
        for i in range(meta["overs_keyed"]):
            vo = read_voice(where[tid], i)
            pre = taps[i]
            m, env = score(r, vo, pre, mic, profile, clean)
            if float_chain_:
                m["model_clip_fraction"] = None
            rows.append({**base, "over": i + 1, **m, **meta["overs"][i]})
            envs += [env_row(variant, n, base["signal"], i + 1, hop, e)
                     for n, (hop, e) in env.items()]
        return rows, envs

    def ref_job(item):
        t, r = item
        mic = mic_input(r, profile, speech_dir)
        clean = clean_source(r, speech_dir)
        rows, envs = [], []
        for name, (pre, x) in references(r, mic, profile, clean).items():
            v = f"codec-only ({name})"
            m, env = score(r, x, pre, mic, profile, clean)
            if name != "nominal":
                m["model_clip_fraction"] = None
            rows.append({"variant": v, "profile": profile.name, "test": t,
                         "signal": signal_name(r), "level": level_of(r),
                         "take": take_id(r), "over": 1, **m})
            envs += [env_row(v, n, signal_name(r), 1, hop, e)
                     for n, (hop, e) in env.items()]
        return rows, envs

    with ThreadPoolExecutor(jobs) as pool:
        scored = list(pool.map(score_job, results)) + list(pool.map(ref_job, work))
    rows = [x for rs, _ in scored for x in rs]
    envs = [x for _, es in scored for x in es]
    return rows, envs, where


def write_tables(data: Path, rows: list[dict], envs: list[dict]) -> None:
    """A run's tidy tables: one CSV per test, the envelopes, and rows.json."""
    from oat import emu_report
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    for t in dict.fromkeys(r["test"] for r in rows):
        emu_report.write_csv(data / f"{t}.csv", [r for r in rows if r["test"] == t])
    if envs:
        emu_report.write_csv(data / "envelopes.csv", [
            {"variant": e["variant"], "test": e["test"], "signal": e["signal"],
             "over": e["over"], "t_s": float(t), "level_dbfs": float(lv)}
            for e in envs for t, lv in zip(e["t_s"], e["level_dbfs"])])
    (data / "rows.json").write_text(json.dumps(rows, indent=1, default=_plain) + "\n")


def _plain(v):
    if isinstance(v, np.generic):
        return v.item()
    raise TypeError(type(v))


def read_tables(data: Path) -> tuple[list[dict], list[dict]]:
    """Rows and envelopes as write_tables left them."""
    import csv
    data = Path(data)
    rows = json.loads((data / "rows.json").read_text())
    envs: dict = {}
    path = data / "envelopes.csv"
    if path.exists():
        with open(path) as f:
            for r in csv.DictReader(f):
                key = (r["variant"], r["test"], r["signal"], int(r["over"]))
                e = envs.setdefault(key, {"variant": key[0], "test": key[1],
                                          "signal": key[2], "over": key[3],
                                          "t_s": [], "level_dbfs": []})
                e["t_s"].append(float(r["t_s"]))
                e["level_dbfs"].append(float(r["level_dbfs"]))
    return rows, [{**e, "t_s": np.array(e["t_s"]),
                   "level_dbfs": np.array(e["level_dbfs"])} for e in envs.values()]


# --- listening-condition intelligibility --------------------------------------

LISTEN_NOISE_DBFS = (-45.0, -35.0)
LISTEN_SEED = 11


def listening(clean: np.ndarray, v: np.ndarray, noise_dbfs: float,
              seed: int = LISTEN_SEED) -> dict:
    """
    Intelligibility as a listener at a fixed receiver volume hears it: the
    decoded audio at its own level (no normalisation), aligned to the clean
    source, with pink noise of noise_dbfs RMS (of int16 full scale) added
    and the sum clipped at full scale, scored with STOI and ESTOI against
    the clean source. A quiet output is buried in the noise, which STOI on
    the bare output, normalising level per segment, cannot see.
    """
    from pystoi import stoi as _stoi
    a, b = emu.align(clean, v)
    noise = 10 ** (noise_dbfs / 20) * colored_noise(len(b), "pink", seed)
    x = np.clip(b + noise, -1, 1)
    return {"listen_stoi": float(_stoi(a, x, RATE, extended=False)),
            "listen_estoi": float(_stoi(a, x, RATE, extended=True)),
            "listen_active_dbfs": emu.levels(b)["active_dbfs"],
            "listen_snr_db": emu.levels(b)["active_dbfs"] - noise_dbfs}


def listening_rows(variant: str, recipes: list[dict], where: dict,
                   profile: emu.Profile, speech_dir: Path,
                   noise_levels=LISTEN_NOISE_DBFS, jobs: int = 6,
                   references_too: bool = True) -> list[dict]:
    """
    The speech sweep's takes re-scored under listening conditions (no new
    emulator runs), at each noise level, with the codec-only nominal and
    ideal references under the same conditions.
    """
    from concurrent.futures import ThreadPoolExecutor

    def job(r):
        tid = take_id(r)
        mic = mic_input(r, profile, speech_dir)
        clean = clean_source(r, speech_dir)
        n = int(content_seconds(r) * RATE)
        outs = {}
        v = read_voice(where[tid])
        lag = max(0, emu.find_lag(ideal_chain(mic, profile)[:n], v))
        outs[variant] = v[: lag + n - int(END_GUARD_S * RATE)]
        if references_too:
            refs = references(r, mic, profile, clean)
            for name in ("nominal", "ideal"):
                outs[f"codec-only ({name})"] = refs[name][1]
        rows = []
        for name, x in outs.items():
            for nl in noise_levels:
                rows.append({"variant": name, "profile": profile.name, "test": "listen",
                             "signal": signal_name(r), "level": level_of(r),
                             "noise_dbfs": float(nl), "take": tid,
                             **listening(clean, x, float(nl))})
        return rows

    with ThreadPoolExecutor(jobs) as pool:
        return [x for rs in pool.map(job, recipes) for x in rs]
