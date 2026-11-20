# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

import pytest

from oat import sweep, trace


def test_grid_spans_mod1_for_each_mod2_fraction_of_the_calibration():
    grid = sweep.modulation_grid(100, 0.75, 1.25, 3, [0, 0.5])
    assert grid == [(75, 0), (100, 0), (125, 0), (75, 50), (100, 50),
                    (125, 50)]


def test_grid_clamps_to_a_byte_and_drops_repeats():
    grid = sweep.modulation_grid(240, 1.0, 1.25, 2, [0, 0, 2.0])
    assert grid == [(240, 0), (255, 0), (240, 255), (255, 255)]


def point(mod1, mod2, evm, outer=2400.0, ratio=3.0, decoded=True):
    return {"mod1": mod1, "mod2": mod2, "evm_percent": evm,
            "outer_deviation_hz": outer, "deviation_ratio": ratio,
            "decoded": decoded}


def test_pick_takes_the_lowest_evm_inside_the_limits():
    points = [point(100, 0, 3.0), point(110, 0, 1.5, outer=2600),
              point(105, 20, 2.0), point(90, 20, 1.0, decoded=False),
              point(95, 40, 2.5, ratio=3.3)]
    best = sweep.pick_modulation(points, (2280, 2520), (2.85, 3.15))
    assert (best["mod1"], best["mod2"]) == (105, 20)


def test_pick_is_none_when_nothing_qualifies():
    assert sweep.pick_modulation([point(100, 0, 2.0, outer=3000)],
                                 (2280, 2520), None) is None


def test_console_answers_through_the_sim_trace():
    t = trace.SimTrace()
    assert t.ask("MOD CAL").fields[1:] == ["CAL", "120", "60"]
    assert t.ask("MOD 100 30").fields[1:] == ["SET", "100", "30"]
    assert t.ask("MODEM CHIP").fields[1:] == ["CHIP"]
    assert t.ask("FREQ 446000000").fields[1:] == ["446000000", "446000000"]
    with pytest.raises(RuntimeError):
        t.ask("BOGUS")


def test_console_times_out_without_an_answer():
    t = trace.Trace()
    with pytest.raises(TimeoutError):
        t.ask("STAT", timeout_s=0.05)


def test_sweep_chart_writes_svg_png_and_a_log_image(tmp_path):
    charts = pytest.importorskip("oat.charts")
    points = [point(100, 0, 3.0), point(110, 0, 2.5, outer=2500),
              point(100, 20, 2.0), point(110, 20, 1.8, outer=2480),
              {"mod1": 120, "mod2": 20, "evm_percent": None,
               "outer_deviation_hz": None, "deviation_ratio": None,
               "decoded": False}]
    files, png = charts.modulation_sweep(points, (100, 0), (2280, 2520),
                                         points[3], "test", tmp_path / "s")
    assert [f.suffix for f in files] == [".svg", ".png"]
    assert all(f.stat().st_size > 1000 for f in files)
    assert len(png) > 1000
