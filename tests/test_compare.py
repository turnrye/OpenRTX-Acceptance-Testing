# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Runs on different firmware, side by side."""

import json
from pathlib import Path

from oat.compare import compare, load_run, main, rows

ROOT = Path(__file__).resolve().parents[1]


def run(firmware, deviation, dsp=None):
    return {"suite": "Bandwidth", "firmware": firmware, "dsp": dsp,
            "started": "2026-11-01T10:00:00+00:00", "radio": "Retevis C62/C2",
            "hardware": "Retevis C62 2501R0621100413",
            "records": [
                {"test": "70cm: Wide", "kind": "capture", "transmissions": [
                    {"peak_deviation_hz": deviation, "tone_hz": 1000.0,
                     "audio_file": "capture01/tx1.wav"}]},
                {"test": "70cm: Wide", "kind": "limit",
                 "field": "peak_deviation_hz", "value": deviation},
                {"test": "70cm: Wide", "kind": "operator", "text": "Set up"},
                {"test": "70cm: Power", "kind": "power",
                 "settings": ["1 W", "5 W"], "relative_db": [0.0, 6.5]}]}


def test_rows_line_up_measurements_and_skip_bookkeeping():
    r = rows(run("v1", 2400.0))
    assert r[("70cm: Wide", "capture tx1 peak_deviation_hz")] == 2400.0
    assert ("70cm: Wide", "capture tx1 tone_hz") not in r
    assert r[("70cm: Power", "power 5 W (dB)")] == 6.5
    assert not any("audio_file" in k[1] for k in r)
    assert ("70cm: Wide", "capture tx1 tone_hz") in rows(run("v1", 1.0), True)


def test_table_has_a_column_per_run():
    text = compare([run("stock", 2300.0, "stock"),
                    run("v0.4-g797187c8f", 2350.0, "7dd697c9aa"),
                    run("v0.4-ga02fb70c", 2410.0, "9579b617bb")])
    header = next(line for line in text.splitlines()
                  if line.startswith("| Test"))
    assert "DSP 7dd697c9" in header and "DSP 9579b617" in header
    row = next(line for line in text.splitlines()
               if "capture tx1 peak_deviation_hz" in line)
    assert row.endswith("| 2300 | 2350 | 2410 |")
    assert "Retevis C62 2501R0621100413" in text


def test_reads_published_runs(capsys):
    folders = sorted({p.parent for p in
                      (ROOT / "results").rglob("measurements.json")})[:2]
    data = [load_run(f) for f in folders]
    assert all(d["records"] for d in data)
    main([str(f) for f in folders])
    out = capsys.readouterr().out
    assert out.count("| Test |") >= 1


def test_reads_an_output_directory(tmp_path):
    bench = tmp_path / "bench-Bandwidth"
    bench.mkdir()
    (bench / "measurements.json").write_text(json.dumps(run("x", 1.0)))
    assert load_run(tmp_path)["firmware"] == "x"
