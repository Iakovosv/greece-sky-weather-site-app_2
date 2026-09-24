"""Regression tests for the double Celsius conversion in `_normalize_hours`.

The bug: both forecast sources already produce `t2m_c` in Celsius - the
per-point path converts at decode time and the shared grid converts when it is
built - but `_normalize_hours` subtracted another 273.15. A mundane 25 °C
afternoon therefore reached the alert rules as -248 °C, so every PRO push
subscriber got a false "extreme cold" warning and the heat rule could never
fire at all.

The value used here is a known Celsius figure rather than one derived from the
same code under test. That is the property that failed before: the original
assertion compared against `source_value - 273.15`, which agreed with the bug
and so passed while the alert was wrong.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app      # noqa: E402
import notify   # noqa: E402


COLD_RULE = "\U0001f321\ufe0f"  # the thermometer emoji on the temp alert title


def _row(step: int, t2m_c: float) -> dict:
    """One row shaped exactly as `wx.gfs_surface_step` returns it."""
    return {"step": step, "t2m_c": t2m_c, "rh2_pct": 50.0, "u10": 1.0, "v10": 1.0,
            "wind_kmh": 5.1, "wind_dir": 315.0, "precip_mm": 0.0}


def _series(t2m_c: float, n: int = 24) -> list[dict]:
    return [_row(s, t2m_c) for s in range(1, n + 1)]


# --------------------------------------------------- per-point path

def test_per_point_celsius_is_not_converted_again():
    """25 °C in must be 25 °C out, not -248 °C.

    This is the exact value the production bug mangled, asserted against the
    literal rather than against anything the conversion itself produced.
    """
    out = app._normalize_hours(_series(25.0))
    assert out, "no hours survived normalization"
    for h in out:
        assert h["t"] == pytest.approx(25.0, abs=0.01), h["t"]


def test_per_point_preserves_a_range_of_celsius_values():
    """The pass-through must hold across the scale, not just at one point.

    A fix that ran the conversion only when the input looked cold would pass the
    25 °C test and still be wrong; these are the temperatures the rules actually
    distinguish.
    """
    for celsius in (-10.0, 0.0, 15.5, 25.0, 39.9, 40.0, 45.0):
        out = app._normalize_hours(_series(celsius))
        assert out[0]["t"] == pytest.approx(celsius, abs=0.01), celsius


# --------------------------------------------------- RAM-grid path

def test_ram_grid_celsius_is_not_converted_again():
    """`grids.surface_rows` output is already Celsius and must pass unchanged.

    The grid is built by `scheduler.build_gfs`, which subtracts 273.15 once; the
    rows it yields carry that same Celsius figure.
    """
    import numpy as np
    import grids

    g = grids.synthetic_grid(model="gfs", run="2026010100", steps=[0, 1, 2])
    # Overwrite with a known Celsius value so the assertion is independent of
    # what the synthetic builder happens to generate.
    g.vars["t2m_c"] = np.full_like(g.vars["t2m_c"], 25.0, dtype=np.float32)

    rows = grids.surface_rows(g, 37.0, 23.0, [1])
    assert rows and rows[0]["t2m_c"] == pytest.approx(25.0, abs=0.01)

    out = app._normalize_hours(rows)
    assert out[0]["t"] == pytest.approx(25.0, abs=0.01), out[0]["t"]


def test_both_paths_agree_on_the_same_celsius_value():
    """The two sources must feed `evaluate` the same number for the same weather.

    This is the equivalence the commercial claim rests on: an alert raised from
    the shared grid and one raised from the per-point path describe the same
    temperature. Before the fix they agreed only because both were equally wrong.
    """
    import numpy as np
    import grids

    g = grids.synthetic_grid(model="gfs", run="2026010100", steps=[0, 1, 2])
    g.vars["t2m_c"] = np.full_like(g.vars["t2m_c"], 25.0, dtype=np.float32)
    g.vars["rh2_pct"] = np.full_like(g.vars["t2m_c"], 50.0, dtype=np.float32)

    from_grid = app._normalize_hours(grids.surface_rows(g, 37.0, 23.0, [1]))[0]
    from_point = app._normalize_hours(_series(25.0))[0]

    assert from_grid["t"] == pytest.approx(from_point["t"], abs=0.01)
    assert from_grid["rh"] == pytest.approx(from_point["rh"], abs=0.01)


# --------------------------------------------------- the alerts themselves

def test_a_normal_summer_hour_produces_no_temperature_alert():
    """25 °C must be silent. This is the user-visible failure the fix targets.

    Asserted through `notify.evaluate`, because a correct `t` that the rules
    still mishandle would be no fix at all.
    """
    hours = app._normalize_hours(_series(25.0))
    alerts = [a for a in notify.evaluate(hours, run_utc="2026092406") if a.rule == "temp"]
    assert alerts == [], [a.body for a in alerts]


def test_the_false_extreme_cold_alert_is_gone():
    """The precise symptom: a -248 °C title must not be reachable from 25 °C.

    Named explicitly so a regression is unmissable rather than merely a count.
    """
    hours = app._normalize_hours(_series(25.0))
    alerts = notify.evaluate(hours, run_utc="2026092406")
    cold = [a for a in alerts if COLD_RULE in a.title]
    assert cold == [], [a.body for a in cold]


def test_hot_threshold_is_still_reachable():
    """40 °C must still raise heat - the rule the bug made unreachable.

    Dry air is used so `feels` stays under its own threshold and the alert
    reports the air temperature, which is the value being asserted here.
    """
    rows = _series(40.0)
    for r in rows:
        r["rh2_pct"] = 10.0
    hours = app._normalize_hours(rows)
    assert hours[0]["t"] == pytest.approx(40.0, abs=0.01)
    alerts = [a for a in notify.evaluate(hours, run_utc="2026092406") if a.rule == "temp"]
    assert len(alerts) == 1
    assert "ζέστη" in alerts[0].title
    assert "40" in alerts[0].body, alerts[0].body


def test_cold_threshold_is_still_reachable():
    """-10 °C must still raise cold, now only for genuinely cold weather."""
    hours = app._normalize_hours(_series(-10.0))
    alerts = [a for a in notify.evaluate(hours, run_utc="2026092406") if a.rule == "temp"]
    assert len(alerts) == 1
    assert "κρύο" in alerts[0].title
    assert "-10" in alerts[0].body


# --------------------------------------------------- feels-like

def test_feels_like_is_computed_from_celsius_not_a_shifted_value():
    """`feels` has the same failure mode: `apparent_temp` takes Celsius.

    A double conversion would push the pair (35 °C air, feels-like 41 °C) to
    (-238 °C, -232 °C), so the feels-like heat rule would silently never fire.
    Asserted against the Celsius figure the UI shows.
    """
    rows = _series(35.0)
    for r in rows:
        r["rh2_pct"] = 60.0
        r["wind_kmh"] = 5.0
    out = app._normalize_hours(rows)

    expected = app.apparent_temp(35.0, 60.0, 5.0)
    assert out[0]["feels"] == pytest.approx(expected, abs=0.01)
    # Sanity: this is a plausible Celsius feels-like, not a shifted one.
    assert 30.0 < out[0]["feels"] < 55.0


def test_feels_like_heat_rule_fires_on_a_hot_humid_hour():
    """The end-to-end feels path: hot and humid must reach the feels threshold."""
    rows = _series(35.0)
    for r in rows:
        r["rh2_pct"] = 85.0
        r["wind_kmh"] = 3.0
    alerts = [a for a in notify.evaluate(app._normalize_hours(rows),
                                         run_utc="2026092406") if a.rule == "temp"]
    assert alerts, "a hot humid hour raised nothing"
    assert "αίσθηση" in alerts[0].body, alerts[0].body


# --------------------------------------------------- integration: both sources

def test_notify_series_agrees_between_the_two_paths(monkeypatch):
    """End to end through `_notify_series`: grid-on and grid-off must match.

    With the flag on the series comes from the shared grid; with it off, from the
    per-point path. For the same weather the evaluated alerts must be the same,
    which is the property a subscriber cares about.
    """
    import asyncio
    import numpy as np
    import grids

    air_c = 22.0

    g = grids.synthetic_grid(model="gfs", run="2026010100", steps=list(range(1, 25)))
    g.vars["t2m_c"] = np.full_like(g.vars["t2m_c"], air_c, dtype=np.float32)
    g.vars["rh2_pct"] = np.full_like(g.vars["t2m_c"], 50.0, dtype=np.float32)
    g.vars["u10"] = np.full_like(g.vars["t2m_c"], 1.0, dtype=np.float32)
    g.vars["v10"] = np.full_like(g.vars["t2m_c"], 1.0, dtype=np.float32)
    g.vars["precip_mm"] = np.zeros_like(g.vars["t2m_c"], dtype=np.float32)

    monkeypatch.setenv("WX_USE_RAM_GRIDS", "1")
    monkeypatch.setitem(grids.STORE._current, "gfs", g)
    monkeypatch.setattr(app.wx, "latest_gfs_run", lambda *a, **k: ("20260101", "00"))
    on = asyncio.run(app._notify_series(37.0, 23.0))

    grids.STORE._current.clear()
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "0")

    async def per_point(lat, lon, hours=24):
        return [_row(s, air_c) for s in range(1, hours + 1)]

    monkeypatch.setattr(app.wx, "gfs_surface_series", per_point)
    off = asyncio.run(app._notify_series(37.0, 23.0))

    assert on and off
    assert on[0]["t"] == pytest.approx(off[0]["t"], abs=0.5), (on[0]["t"], off[0]["t"])
    a_on = notify.evaluate(on, run_utc="2026010100")
    a_off = notify.evaluate(off, run_utc="2026010100")
    assert sorted(a.rule for a in a_on) == sorted(a.rule for a in a_off)


def test_normalize_hours_still_tolerates_a_missing_temperature():
    """A row without a temperature must stay None, not become a bogus number."""
    out = app._normalize_hours([{"step": 1, "precip_mm": 0.0}])
    assert out and out[0]["t"] is None
    assert out[0]["feels"] is None
