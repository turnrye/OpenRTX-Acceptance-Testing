# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Outside tools for M17 voice: Codec2's c2enc and c2dec (the codec2 package),
for the 3200 bit/s voice frames stream frames carry, and m17-demod from
m17-cxx-demod, an independent decoder to check this library's against.
Each is optional: callers ask whether it's there and skip clearly if not.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

from oat.m17.phy import BASEBAND_RATE, baseband_pcm

CODEC2_MODE = "3200"
CODEC2_RATE = 8000
# Bytes of one 20 ms Codec2 3200 frame; a stream frame carries two
CODEC2_FRAME_BYTES = 8
STREAM_PAYLOAD_BYTES = 16
M17_DEMOD = "m17-demod"


def available(tool: str) -> bool:
    """Whether a command line tool is on the PATH."""
    return shutil.which(tool) is not None


def require(tool: str, package: str) -> str:
    """A tool's path, or a clear error naming the package to install."""
    path = shutil.which(tool)
    if path is None:
        raise RuntimeError(f"{tool} not found: install {package}")
    return path


def to_codec2_rate(audio: np.ndarray, rate: int) -> np.ndarray:
    """Mono audio, any rate, as float at Codec2's 8 kHz."""
    audio = np.asarray(audio, dtype=float)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    g = np.gcd(CODEC2_RATE, int(rate))
    return signal.resample_poly(audio, CODEC2_RATE // g, int(rate) // g)


def codec2_encode(pcm: np.ndarray) -> bytes:
    """8 kHz s16 audio as Codec2 3200 frames, through c2enc."""
    c2enc = require("c2enc", "codec2")
    with tempfile.TemporaryDirectory() as tmp:
        raw, out = Path(tmp) / "in.raw", Path(tmp) / "out.c2"
        np.asarray(pcm, dtype="<i2").tofile(raw)
        subprocess.run([c2enc, CODEC2_MODE, str(raw), str(out)], check=True,
                       capture_output=True)
        return out.read_bytes()


def codec2_decode(data: bytes) -> np.ndarray:
    """Codec2 3200 frames as 8 kHz s16 audio, through c2dec."""
    c2dec = require("c2dec", "codec2")
    with tempfile.TemporaryDirectory() as tmp:
        bits, out = Path(tmp) / "in.c2", Path(tmp) / "out.raw"
        bits.write_bytes(data)
        subprocess.run([c2dec, CODEC2_MODE, str(bits), str(out)], check=True,
                       capture_output=True)
        return np.fromfile(out, dtype="<i2")


def stream_payloads(frames: bytes) -> list[bytes]:
    """Codec2 frames grouped two to a stream frame, the last padded."""
    usable = len(frames) // CODEC2_FRAME_BYTES * CODEC2_FRAME_BYTES
    frames = frames[:usable]
    if len(frames) % STREAM_PAYLOAD_BYTES:
        frames += frames[-CODEC2_FRAME_BYTES:]
    return [frames[i:i + STREAM_PAYLOAD_BYTES]
            for i in range(0, len(frames), STREAM_PAYLOAD_BYTES)]


def tone_payloads(tone_hz: float, duration_s: float,
                  level_dbfs: float = -20.0) -> list[bytes]:
    """
    Stream payloads of a Codec2-encoded tone. Codec2 models voice as
    harmonics of a pitch, so it rebuilds a tone only approximately: a 1 kHz
    tone at -20 dBFS comes back within a few Hz, louder ones and some other
    frequencies land on a neighbouring harmonic.
    """
    t = np.arange(int(duration_s * CODEC2_RATE)) / CODEC2_RATE
    pcm = 10 ** (level_dbfs / 20) * 32767 * np.sin(2 * np.pi * tone_hz * t)
    return stream_payloads(codec2_encode(pcm.astype(np.int16)))


def wav_payloads(path: Path, gain_db: float = 0.0) -> list[bytes]:
    """Stream payloads of a WAV file, Codec2-encoded."""
    rate, audio = wavfile.read(path)
    if audio.dtype.kind == "f":
        audio = audio * 32767
    elif audio.dtype == np.int32:
        audio = audio / 65536
    elif audio.dtype == np.uint8:
        audio = (audio.astype(float) - 128) * 256
    pcm = to_codec2_rate(audio, rate) * 10 ** (gain_db / 20)
    return stream_payloads(codec2_encode(
        np.clip(pcm, -32767, 32767).astype(np.int16)))


def random_payloads(count: int, seed: int = 17) -> list[bytes]:
    """Payloads of random bytes, for tests that need no voice."""
    rng = np.random.default_rng(seed)
    return [rng.bytes(STREAM_PAYLOAD_BYTES) for _ in range(count)]


def write_codec2_wav(path: Path, payload: bytes) -> np.ndarray:
    """Decode a stream's payload with c2dec and save it as an 8 kHz WAV."""
    pcm = codec2_decode(payload)
    wavfile.write(path, CODEC2_RATE, pcm)
    return pcm


def m17_demod(dev_hz: np.ndarray, invert: bool = False,
              timeout_s: float = 60.0) -> dict | None:
    """
    Run m17-demod (m17-cxx-demod) on demodulated baseband at 48 kHz, in Hz
    of deviation. Returns what it reported: the source and destination of
    each LSF it decoded, and its raw diagnostic text; None when m17-demod
    isn't installed.

    m17-demod reads 48 kHz s16le discriminator output on stdin, as rtl_fm
    writes it, and with -l prints each LSF on stderr.
    UNVERIFIED: the input scale (an rtl_fm-style +-pi rad/sample at
    +-16384) and the "SRC: ..., DEST: ..." layout of its LSF lines are from
    m17-cxx-demod's source as remembered, not checked against an installed
    build; check both once it's installed, and adjust LSF_LINE if needed.
    """
    path = shutil.which(M17_DEMOD)
    if path is None:
        return None
    cmd = [path, "-l", "-d"] + (["-i"] if invert else [])
    done = subprocess.run(cmd, input=baseband_pcm(dev_hz).tobytes(),
                          capture_output=True, timeout=timeout_s)
    text = done.stderr.decode(errors="replace")
    lsfs = [{"source": m.group(1).strip(), "destination": m.group(2).strip()}
            for m in LSF_LINE.finditer(text)]
    return {"lsfs": lsfs, "audio_bytes": len(done.stdout),
            "returncode": done.returncode, "text": text[-4000:]}


# m17-demod's LSF line, as in "SRC: K0RET, DEST: @ALL, ..."
LSF_LINE = re.compile(r"SRC:\s*([^,\n]*),\s*DEST:\s*([^,\n]*)")
# Kept for callers that need the rate m17-demod expects
M17_DEMOD_RATE = BASEBAND_RATE
