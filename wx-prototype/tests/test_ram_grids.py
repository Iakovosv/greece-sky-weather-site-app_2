"""Correctness of the in-memory grid interpolation.

The claims worth testing here are the ones that would silently produce a
plausible-looking but wrong forecast rather than an exception:

  * descending latitude is normalised, so a point does not read the mirrored
    corner of the box;
  * u and v are interpolated separately and speed/direction derived after, so a
    velocity that crosses the 0/360 seam is not averaged into the opposite
    direction;
  * a single assignment swaps the live grid, so no reader sees a half-built run;
  * a failed refresh keeps the previous run instead of blanking the site;
  * a field missing from some steps does not slide the whole series by an hour.

The synthetic grid is deliberately linear in (step, lat, lon), which makes
bilinear interpolation exact. That lets the tests assert exact values rather than
approximate ones, so a subtle index error cannot hide behind a tolerance.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import grids  # noqa: E402


# --------------------------------------------------------------- interpolation

def test_bilinear_at_a_grid_node_returns_that_node():
    g = grids.synthetic_grid()
    # axis[j] values: lat 34..42, lon 18..30 -> value = lat + 2*lon + 3*step
    v = grids.bilinear(g, "t2m_c", 0, 34.0, 18.0)
    assert v == pytest.approx(34.0 + 2 * 18.0)


def test_bilinear_midpoint_is_the_arithmetic_mean_for_a_linear_field():
    g = grids.synthetic_grid()
    lat0, lat1 = g.lat[0], g.lat[1]
    lon0, lon1 = g.lon[0], g.lon[1]
    mid = grids.bilinear(g, "t2m_c", 0, (lat0 + lat1) / 2, (lon0 + lon1) / 2)
    expect = (lat0 + 2 * lon0) + ((lat1 + 2 * lon1) - (lat0 + 2 * lon0)) / 2
    assert mid == pytest.approx(expect)


def test_bilinear_reproduces_the_analytic_field_anywhere_inside():
    """For value = lat + 2*lon + 3*step, bilinear is exact at every interior point."""
    g = grids.synthetic_grid(steps=[0, 1, 2])
    for lat in (34.5, 36.13, 38.0, 40.7, 41.9):
        for lon in (18.4, 21.0, 24.586, 28.2, 29.8):
            for step in (0, 1, 2):
                assert grids.bilinear(g, "t2m_c", step, lat, lon) == pytest.approx(
                    lat + 2 * lon + 3 * step, abs=1e-4)


def test_bilinear_outside_latitude_returns_none_not_a_clamped_edge():
    """Latitude is not periodic: a point beyond the box has no honest answer, and
    clamping would invent a forecast from the nearest row."""
    g = grids.synthetic_grid()
    assert grids.bilinear(g, "t2m_c", 0, 10.0, 24.0) is None
    assert grids.bilinear(g, "t2m_c", 0, 60.0, 24.0) is None


def test_bilinear_missing_step_or_field_returns_none():
    g = grids.synthetic_grid(steps=[0, 1])
    assert grids.bilinear(g, "t2m_c", 99, 38.0, 24.0) is None
    assert grids.bilinear(g, "nope", 0, 38.0, 24.0) is None


def test_non_finite_values_are_reported_missing():
    g = grids.synthetic_grid()
    g.vars["t2m_c"][0, 4, 4] = np.nan
    # A corner touching the NaN must not propagate a NaN into the answer.
    assert grids.bilinear(g, "t2m_c", 0, float(g.lat[4]), float(g.lon[4])) is None


def test_step_lookup_is_by_value_not_position():
    """GFS switches from hourly to 3-hourly, so `steps` is not contiguous and the
    step number must not be used as an index."""
    g = grids.synthetic_grid(steps=[0, 120, 123])
    assert g.step_index(123) == 2
    # value = lat + 2*lon + 3*step, so step 123 must carry the +369 term.
    v = grids.bilinear(g, "t2m_c", 123, 34.0, 18.0)
    assert v == pytest.approx(34.0 + 36.0 + 3 * 123)


# --------------------------------------------------------------- wind handling

def test_wind_speed_and_direction_are_derived_after_interpolation():
    """The failure this guards: averaging the *directions* of a 350 deg and a
    10 deg wind gives 180 deg, which is due south instead of due north. The fix is
    to interpolate u and v and derive the angle afterwards."""
    g = grids.synthetic_grid(fields=())
    # Two adjacent longitudes, u/v chosen so the true answer is symmetric.
    lat = np.array([34.0, 35.0])
    lon = np.array([18.0, 19.0])
    u = np.array([[1.0, 1.0], [1.0, 1.0]], dtype=np.float32)
    v = np.array([[0.0, 0.0], [0.0, 0.0]], dtype=np.float32)
    g.lat, g.lon = lat, lon
    g.steps = [0]
    g.vars = {"u10": u[None, :, :], "v10": v[None, :, :], "t2m_c": u[None, :, :]}

    rows = grids.surface_rows(g, 34.5, 18.5, [0])
    assert len(rows) == 1
    r = rows[0]
    # u=+1 (westerly), v=0 -> wind from the west is 270 deg.
    assert r["wind_kmh"] == pytest.approx(3.6, abs=1e-6)
    assert r["wind_dir"] == pytest.approx(270.0, abs=1e-6)


def test_opposing_components_cancel_instead_of_averaging_to_a_wrong_direction():
    """Two cells with exactly opposite v. Averaging directions would give a
    confident 'north' or 'south'; averaging components gives calm, which is the
    truthful answer."""
    g = grids.synthetic_grid(fields=())
    lat = np.array([34.0, 35.0])
    lon = np.array([18.0, 19.0])
    u = np.zeros((2, 2), dtype=np.float32)
    v = np.array([[-1.0, -1.0], [1.0, 1.0]], dtype=np.float32)  # south above, north below
    g.lat, g.lon, g.steps = lat, lon, [0]
    g.vars = {"u10": u[None], "v10": v[None], "t2m_c": u[None]}

    # Exactly on the boundary between the two rows: v cancels, speed is zero.
    r = grids.surface_rows(g, 34.5, 18.5, [0])[0]
    assert r["wind_kmh"] == pytest.approx(0.0, abs=1e-6)


# --------------------------------------------------------------- surface rows

def test_surface_rows_omit_steps_without_temperature():
    """A step with no temperature is not a usable forecast hour; emitting it would
    put a blank row in the meteogram that looks like a data gap in the UI."""
    g = grids.synthetic_grid(steps=[0, 1])
    del g.vars["t2m_c"]
    g.vars["u10"] = np.zeros((2, 9, 9), dtype=np.float32)
    g.vars["v10"] = np.zeros((2, 9, 9), dtype=np.float32)
    assert grids.surface_rows(g, 38.0, 24.0, [0, 1]) == []


def test_surface_rows_match_the_per_point_field_names():
    """Downstream (/api/brief, simple_view, bias) reads these exact keys. A rename
    here would not raise, it would just quietly drop fields from the response."""
    g = grids.synthetic_grid(fields=("t2m_c", "rh2_pct", "u10", "v10",
                                     "precip_mm", "gust_kmh", "cape", "cloud_pct"))
    r = grids.surface_rows(g, 38.0, 24.0, [0])[0]
    for key in ("step", "t2m_c", "rh2_pct", "wind_kmh", "wind_dir",
                "u10", "v10", "precip_mm", "gust_kmh", "cape", "cloud_pct"):
        assert key in r, f"{key} missing from the RAM row"


# --------------------------------------------------------------- store / swap

def test_replace_is_atomic_for_readers():
    """A reader holding a reference from before the swap must keep seeing the old
    grid. If the store mutated in place, a request could mix two runs."""
    store = grids.GridStore()
    older = grids.synthetic_grid(run="2026010100")
    store.replace("gfs", older)
    held = store.get("gfs")

    newer = grids.synthetic_grid(run="2026010200")
    store.replace("gfs", newer)

    assert store.get("gfs").run == "2026010200"
    assert held.run == "2026010100", "the in-flight reference was mutated"


def test_failed_refresh_keeps_the_previous_grid():
    """The whole point of the fallback: an outage at refresh time degrades to a
    stale-but-served forecast, never to an error page."""
    store = grids.GridStore()
    good = grids.synthetic_grid(run="2026010100")
    store.replace("gfs", good)

    store.mark_failure("gfs", "TimeoutError: NOMADS")

    assert store.get("gfs") is good, "the last good run was dropped on failure"
    h = store.health()["gfs"]
    assert h["stale"] is True
    assert h["run"] == "2026010100"
    assert "NOMADS" in h["last_error"]


def test_failure_before_any_success_is_visible_in_health():
    """If it never loaded, /api/health must say so rather than showing nothing -
    a silent scheduler is the hardest kind to notice."""
    store = grids.GridStore()
    store.mark_failure("icon", "HTTPStatusError: 404")
    h = store.health()["icon"]
    assert h["run"] is None
    assert "404" in h["last_error"]


def test_health_reports_age_so_a_stalled_scheduler_is_obvious():
    store = grids.GridStore()
    g = grids.synthetic_grid()
    g.loaded_at -= 7200  # pretend the grid is two hours old
    store.replace("gfs", g)
    assert store.health()["gfs"]["age_s"] == pytest.approx(7200, abs=5)


def test_replace_success_clears_the_stale_flag():
    store = grids.GridStore()
    store.mark_failure("gfs", "boom")
    store.replace("gfs", grids.synthetic_grid())
    assert store.health()["gfs"]["stale"] is False


# --------------------------------------------------------------- feature flag

@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("nonsense", False),
])
def test_feature_flag_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("WX_USE_RAM_GRIDS", value)
    assert grids.flag_enabled() is expected


def test_feature_flag_defaults_off(monkeypatch):
    """Off by default so a fresh deploy keeps the proven per-point path until the
    RAM path has been observed running."""
    monkeypatch.delenv("WX_USE_RAM_GRIDS", raising=False)
    assert grids.flag_enabled() is False


# --------------------------------------------------------------- orography

def test_model_orography_interpolates_and_defaults_to_none():
    g = grids.synthetic_grid(fields=())
    assert grids.model_orography(g, 38.0, 24.0) is None
    g.meta["orog"] = np.full((g.lat.size, g.lon.size), 100.0, dtype=np.float32)
    assert grids.model_orography(g, 38.0, 24.0) == pytest.approx(100.0)


def test_a_point_far_outside_a_regional_grid_is_rejected_not_wrapped():
    """A regional grid's edges are real boundaries. Wrapping longitude folds a
    point in Algeria back to 29 E and answers it with Greek weather - a wrong
    forecast presented as a real one, which is worse than an error."""
    g = grids.synthetic_grid(fields=("t2m_c",))
    # synthetic_grid is the Greek box.
    for name, la, lo in (("Algeria", 35.0, 5.0),
                         ("London", 51.5, -0.13),
                         ("Cairo", 30.0, 31.2),
                         ("Berlin", 52.5, 13.4)):
        assert grids.bilinear(g, "t2m_c", g.steps[0], la, lo) is None, name


def test_a_point_inside_the_regional_grid_still_works():
    """The rejection must not become an excuse to drop the domain edges."""
    g = grids.synthetic_grid(fields=("t2m_c",))
    assert grids.bilinear(g, "t2m_c", g.steps[0], 37.98, 23.72) is not None
    assert grids.bilinear(g, "t2m_c", g.steps[0], 36.44, 28.22) is not None  # Rhodes
    # The exact corner is inside.
    assert grids.bilinear(g, "t2m_c", g.steps[0], g.lat[0], g.lon[0]) is not None


def test_a_global_grid_still_wraps_at_the_dateline():
    """The wrap exists for a real reason: a global grid's seam must not be a
    boundary. Removing it for regional grids must not remove it here."""
    lat = np.linspace(-90.0, 90.0, 19)
    lon = np.linspace(0.0, 359.0, 360)
    g = grids.GridSpec(model="gfs", run="x", lat=lat, lon=lon, steps=[0],
                       vars={"t2m_c": np.full((1, lat.size, lon.size), 7.0, np.float32)})
    # -0.5 deg should wrap to 359.5, not be rejected.
    assert grids.bilinear(g, "t2m_c", 0, 10.0, -0.5) == pytest.approx(7.0)
    assert grids.bilinear(g, "t2m_c", 0, 10.0, 359.5) == pytest.approx(7.0)


def test_covers_is_true_inside_and_false_outside_a_regional_grid():
    g = grids.synthetic_grid(fields=("t2m_c",))
    assert grids.covers(g, 37.98, 23.72)          # Athens
    assert grids.covers(g, 40.64, 22.94)          # Thessaloniki
    assert grids.covers(g, 36.44, 28.22)          # Rhodes, near the east edge
    assert not grids.covers(g, 52.52, 13.40)      # Berlin
    assert not grids.covers(g, 35.00, 5.00)       # Algeria
    assert not grids.covers(g, 51.51, -0.13)      # London


def test_covers_is_true_anywhere_on_a_global_grid():
    lat = np.linspace(-90.0, 90.0, 19)
    lon = np.linspace(0.0, 359.0, 360)
    g = grids.GridSpec(model="gfs", run="x", lat=lat, lon=lon, steps=[0],
                       vars={"t2m_c": np.zeros((1, 19, 360), np.float32)})
    assert grids.covers(g, 10.0, -0.5)
    assert grids.covers(g, 52.0, 200.0)
    assert not grids.covers(g, 95.0, 10.0)


def test_a_point_far_outside_the_grid_in_latitude_is_rejected():
    g = grids.synthetic_grid(fields=("t2m_c",))
    assert grids.bilinear(g, "t2m_c", g.steps[0], 10.0, 23.72) is None
    assert grids.bilinear(g, "t2m_c", g.steps[0], 70.0, 23.72) is None


def test_a_mis_shaped_plane_degrades_instead_of_raising():
    """Defensive: pairing an array with the wrong axes must not throw an
    IndexError from inside a request handler."""
    g = grids.synthetic_grid(fields=())
    g.meta["orog"] = np.zeros((3, 3), dtype=np.float32)  # wrong shape
    assert grids.model_orography(g, 38.0, 24.0) is None


def test_model_orography_uses_its_own_axes_when_it_has_them():
    """The real failure this replaces: the orography is decoded from its own GRIB
    subset, so its coordinates need not match the surface grid. Interpolating it
    against the main axes returned None for every point, silently disabling the
    lapse-rate correction that topographic accuracy depends on."""
    g = grids.synthetic_grid(fields=())
    # Orog kept on a *different* pad than the main grid, as the live fetch does.
    ola = np.linspace(33.5, 42.5, 37)
    olo = np.linspace(17.5, 30.5, 53)
    g.meta["orog"] = np.full((ola.size, olo.size), 300.0, dtype=np.float32)
    g.meta["orog_lat"], g.meta["orog_lon"] = ola, olo
    assert grids.model_orography(g, 38.0, 24.0) == pytest.approx(300.0)
