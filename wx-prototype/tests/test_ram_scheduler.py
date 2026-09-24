"""The scheduler's refresh policy and grid normalisation.

Only the pure pieces are tested here - latitude normalisation and the
keep-old-run-on-failure rule - because those are where a bug produces a wrong
answer rather than a loud crash. The download and GRIB decode paths need the
network and are exercised by the live check instead.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import grids  # noqa: E402
import scheduler  # noqa: E402


def test_descending_latitude_is_flipped_ascending():
    """GFS publishes latitude descending. Interpolating against it without
    normalising silently mirrors the domain, so a point in Crete reads the
    latitudinally opposite corner of the box. This is the single most damaging
    silent bug in the whole path."""
    data = xr.DataArray(
        np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32),
        dims=("latitude", "longitude"),
        coords={"latitude": [42.0, 34.0], "longitude": [18.0, 30.0]})
    fixed = scheduler._as_ascending(data)
    assert list(fixed["latitude"].values) == [34.0, 42.0]
    # Values must travel with their coordinate, not be reversed independently.
    assert float(fixed.sel(latitude=34.0, longitude=18.0)) == 3.0
    assert float(fixed.sel(latitude=42.0, longitude=18.0)) == 1.0


def test_already_ascending_latitude_is_left_alone():
    data = xr.DataArray(
        np.ones((2, 2), dtype=np.float32),
        dims=("latitude", "longitude"),
        coords={"latitude": [34.0, 42.0], "longitude": [18.0, 30.0]})
    fixed = scheduler._as_ascending(data)
    assert list(fixed["latitude"].values) == [34.0, 42.0]


def test_a_centred_point_is_unaffected_by_which_way_lat_runs():
    """The behavioural consequence: the same physical point must interpolate to
    the same value before and after normalisation. If flipping were wrong, this
    is the assertion that would catch it."""
    values = np.array([[10.0, 20.0],
                       [30.0, 40.0]], dtype=np.float32)
    asc = xr.DataArray(values, dims=("latitude", "longitude"),
                       coords={"latitude": [34.0, 42.0], "longitude": [18.0, 30.0]})
    desc = xr.DataArray(values[::-1], dims=("latitude", "longitude"),
                        coords={"latitude": [42.0, 34.0], "longitude": [18.0, 30.0]})
    a = scheduler._as_ascending(asc)
    b = scheduler._as_ascending(desc)
    np.testing.assert_allclose(a.values, b.values)


def test_gfs_steps_matches_the_published_cadence():
    """Hourly to 120, then every 3 hours. Requesting f121 is a hard 404, so the
    list must not assume uniform hourly output."""
    steps = scheduler.gfs_steps(240)
    assert steps[:3] == [1, 2, 3]
    assert 120 in steps
    assert 121 not in steps
    assert 123 in steps
    assert steps[-1] <= 240
    assert all((s % 3 == 0) for s in steps if s > 120)


def test_gfs_steps_stays_hourly_when_the_window_is_short():
    steps = scheduler.gfs_steps(72)
    assert steps == list(range(1, 73))



def test_refresh_once_keeps_the_old_grid_when_a_builder_fails():
    """The fallback contract: a failing builder must not remove the grid that is
    already being served."""
    store = grids.GridStore()
    good = grids.synthetic_grid(model="gfs", run="2026010100")
    store.replace("gfs", good)

    async def boom(client):
        raise RuntimeError("NOMADS is down")

    results = asyncio.run(scheduler.refresh_once(store, builders={"gfs": boom}))

    assert "failed" in results["gfs"]
    assert store.get("gfs") is good
    assert store.health()["gfs"]["stale"] is True



def test_refresh_once_publishes_a_successful_build():
    store = grids.GridStore()

    async def ok(client):
        return grids.synthetic_grid(model="icon", run="2026010200")

    results = asyncio.run(scheduler.refresh_once(store, builders={"icon": ok}))

    assert results["icon"].startswith("ok")
    assert store.get("icon").run == "2026010200"
    assert store.health()["icon"]["stale"] is False



def test_one_failing_model_does_not_stop_the_others():
    """GFS down must not cost you ICON: the models are independent sources."""
    store = grids.GridStore()

    async def boom(client):
        raise RuntimeError("nope")

    async def ok(client):
        return grids.synthetic_grid(model="icon", run="2026010200")

    results = asyncio.run(
        scheduler.refresh_once(store, builders={"gfs": boom, "icon": ok}))

    assert "failed" in results["gfs"]
    assert results["icon"].startswith("ok")
    assert store.get("icon") is not None


def test_scheduler_is_not_started_when_the_flag_is_off(monkeypatch):
    """Importing the module must not begin network traffic on its own; the flag
    is the only thing that turns the refresher on."""
    monkeypatch.delenv("WX_USE_RAM_GRIDS", raising=False)
    assert scheduler.start() is None


# --------------------------------------------------------------- bbox subsetting

def test_icon_scope_defaults_to_greece(monkeypatch):
    """The default must stay the cheap one: europe costs an order of magnitude
    more resident memory, so it cannot be what a bare flag switch hands you."""
    monkeypatch.delenv("WX_RAM_ICON_SCOPE", raising=False)
    assert grids.icon_bbox() is grids.GREEK_BBOX


def test_icon_scope_europe_is_opt_in(monkeypatch):
    monkeypatch.setenv("WX_RAM_ICON_SCOPE", "europe")
    assert grids.icon_bbox() is grids.EUROPE_BBOX


def test_icon_scope_rejects_an_unknown_value_by_falling_back_to_greece(monkeypatch):
    """A typo must not silently buy the expensive scope."""
    monkeypatch.setenv("WX_RAM_ICON_SCOPE", "eurpe")
    assert grids.icon_bbox() is grids.GREEK_BBOX


def test_europe_bbox_contains_berlin_and_madrid_but_greek_does_not():
    """The whole point of the wide scope: a point the Greek grid cannot answer is
    inside the European one. Berlin at 52.5 N and Madrid at 40.4 N both sit north
    of the Greek box, so the GFS per-point path at 25 km was all they had."""
    eu = grids.EUROPE_BBOX
    for la, lo in ((52.52, 13.40), (40.42, -3.70), (41.90, 12.50), (48.85, 2.35)):
        assert eu["south"] <= la <= eu["north"], (la, lo)
        assert eu["west"] <= lo <= eu["east"], (la, lo)
        # ...and outside the Greek box, which is what makes this a real widening.
        assert not (grids.GREEK_BBOX["south"] <= la <= grids.GREEK_BBOX["north"]
                    and grids.GREEK_BBOX["west"] <= lo <= grids.GREEK_BBOX["east"])


def test_the_europe_bbox_matches_the_icon_eu_domain():
    """Hardcoded from the DWD regular-lat-lon product. A wrong box would either
    drop real coverage or make _subset_bbox raise at refresh time."""
    assert grids.EUROPE_BBOX == {"north": 70.5, "south": 29.5,
                                 "west": -23.5, "east": 62.5}


def test_subset_bbox_trims_to_the_greece_box():
    """ICON-EU arrives as a whole-Europe grid with no server-side subset. Keeping
    it whole would hold ~65 MB resident instead of ~1 MB, so the trim is load
    bearing, not an optimisation."""
    lat = np.linspace(29.5, 70.5, 657)
    lon = np.linspace(-23.5, 62.5, 1377)
    plane = np.ones((657, 1377), dtype=np.float32)
    out = scheduler._subset_bbox(plane, lat, lon, {"north": 43.0, "south": 33.0,
                                                   "west": 17.0, "east": 31.0})
    assert out is not None
    p, la, lo = out
    assert la[0] >= 33.0 and la[-1] <= 43.0
    assert lo[0] >= 17.0 and lo[-1] <= 31.0
    assert p.shape == (la.size, lo.size)
    # The trim must be a real reduction, not a no-op.
    assert p.size < plane.size / 10


def test_subset_bbox_preserves_values_at_the_right_coordinates():
    """A slice that shifted by one index would still look plausible. Tie the
    values to their coordinates so an off-by-one cannot hide."""
    lat = np.array([30.0, 35.0, 40.0, 45.0])
    lon = np.array([10.0, 20.0, 30.0, 40.0])
    plane = (lat[:, None] * 10 + lon[None, :]).astype(np.float32)
    p, la, lo = scheduler._subset_bbox(plane, lat, lon,
                                       {"north": 42.0, "south": 34.0,
                                        "west": 18.0, "east": 32.0})
    assert list(la) == [35.0, 40.0]
    assert list(lo) == [20.0, 30.0]
    assert float(p[0, 0]) == 35.0 * 10 + 20.0
    assert float(p[1, 1]) == 40.0 * 10 + 30.0


def test_subset_bbox_returns_none_when_there_is_no_overlap():
    lat = np.array([-60.0, -50.0])
    lon = np.array([-170.0, -160.0])
    plane = np.zeros((2, 2), dtype=np.float32)
    assert scheduler._subset_bbox(plane, lat, lon, {"north": 43.0, "south": 33.0,
                                                    "west": 17.0, "east": 31.0}) is None


def test_subset_bbox_agrees_with_the_untrimmed_value_at_a_point():
    """End-to-end on the trim: interpolating Athens on the trimmed grid must give
    the same answer as on the full Europe grid. If the slice had slipped, the
    Athens value would change even though both grids 'look' fine."""
    lat = np.linspace(29.5, 70.5, 657)
    lon = np.linspace(-23.5, 62.5, 1377)
    LON, LAT = np.meshgrid(lon, lat)
    plane = (LAT * 1.0 + LON * 0.5).astype(np.float32)

    full = grids.GridSpec(model="icon", run="x", lat=lat, lon=lon, steps=[0],
                          vars={"t2m_c": plane[None]})
    p, la, lo = scheduler._subset_bbox(plane, lat, lon,
                                       {"north": 43.0, "south": 33.0,
                                        "west": 17.0, "east": 31.0})
    trimmed = grids.GridSpec(model="icon", run="x", lat=la, lon=lo, steps=[0],
                             vars={"t2m_c": p[None]})

    a = grids.bilinear(full, "t2m_c", 0, 37.9838, 23.7275)
    b = grids.bilinear(trimmed, "t2m_c", 0, 37.9838, 23.7275)
    assert a == pytest.approx(b, abs=1e-3), "trimming shifted the data"

