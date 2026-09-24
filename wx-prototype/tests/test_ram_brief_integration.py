"""End-to-end proof that /api/brief is served from RAM with no GFS network use.

The unit tests cover the arithmetic. This covers the claim that actually matters
commercially: with the flag on and a grid loaded, a forecast request performs no
NOMADS fetch and no GRIB decode. That is verified by making every network entry
point raise, so any accidental call becomes a visible failure rather than a slow
but correct response.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import entitlements as ent  # noqa: E402
import grids  # noqa: E402
import wx  # noqa: E402


def _gfs_grid(run: str = "2026010100") -> grids.GridSpec:
    """A geographically plausible GFS-shaped grid: descending source latitude,
    turned ascending, with the fields the surface path reads."""
    steps = list(range(1, 25))
    lat = np.linspace(34.0, 42.0, 33)
    lon = np.linspace(18.0, 30.0, 49)
    LON, LAT = np.meshgrid(lon, lat)
    mk = lambda base, s: (base( LAT, LON) + 0.01 * s).astype(np.float32)
    g = grids.GridSpec(
        model="gfs", run=run, lat=lat, lon=lon, steps=steps,
        vars={
            "t2m_c": np.stack([mk(lambda a, b: 15.0 + 0.0 * a, s) for s in steps]),
            "rh2_pct": np.stack([mk(lambda a, b: 60.0 + 0.0 * a, s) for s in steps]),
            "u10": np.stack([mk(lambda a, b: 3.0 + 0.0 * a, s) for s in steps]),
            "v10": np.stack([mk(lambda a, b: -1.0 + 0.0 * a, s) for s in steps]),
            "precip_mm": np.stack([mk(lambda a, b: 0.0 + 0.0 * a, s) for s in steps]),
            "cloud_pct": np.stack([mk(lambda a, b: 20.0 + 0.0 * a, s) for s in steps]),
        },
        meta={"orog": np.full((lat.size, lon.size), 120.0, dtype=np.float32)})
    return g


def _no_network(monkeypatch):
    """Make every GFS network and decode entry point fail loudly."""
    def boom(*a, **k):
        raise AssertionError("RAM path touched the network")

    async def aboom(*a, **k):
        raise AssertionError("RAM path touched the network")

    monkeypatch.setattr(wx, "latest_gfs_run", boom)
    monkeypatch.setattr(wx, "gfs_surface_series", aboom)
    monkeypatch.setattr(wx, "gfs_surface_step", aboom)
    monkeypatch.setattr(wx, "gfs_orography", aboom)
    monkeypatch.setattr(wx, "nomads_get", aboom)


def test_brief_serves_gfs_from_ram_without_touching_the_network(monkeypatch):
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "1")
    monkeypatch.setitem(grids.STORE._current, "gfs", _gfs_grid())
    _no_network(monkeypatch)

    # The sounding is a separate source and is allowed to fail; it must not stop
    # the surface series from being served out of RAM.
    async def prof_boom(*a, **k):
        raise RuntimeError("profile unavailable")

    monkeypatch.setattr(wx, "gfs_profile_dataset", prof_boom)
    monkeypatch.setattr(wx, "icon_eu_latest_run", lambda *a, **k: "2026010100")
    monkeypatch.setattr(wx, "ecmwf_point", prof_boom)

    client = TestClient(app.app)
    r = client.get("/api/brief", params={"lat": 37.98, "lon": 23.72, "hours": 24})
    assert r.status_code == 200, r.text

    body = r.json()
    assert body["meta"]["gfs_run"] == "2026010100"
    # 24 hourly steps, served from RAM.
    assert len(body["simple"]["hours"]) == 24
    assert body["simple"]["hours"][0]["t"] == pytest.approx(15.0, abs=0.5)


def test_brief_still_works_when_the_flag_is_off(monkeypatch):
    """Turning the flag off must leave the original per-point path untouched."""
    monkeypatch.delenv("WX_USE_RAM_GRIDS", raising=False)
    called = {"series": 0}

    async def fake_series(lat, lon, hours=48):
        called["series"] += 1
        return [{"step": s, "t2m_c": 20.0, "u10": 1.0, "v10": 0.0,
                 "wind_kmh": 3.6, "wind_dir": 270.0, "precip_mm": 0.0}
                for s in range(1, 25)]

    async def fake_prof(*a, **k):
        raise RuntimeError("no profile")

    async def fake_orog(*a, **k):
        return 100.0

    monkeypatch.setattr(wx, "latest_gfs_run", lambda *a, **k: ("20260101", "00"))
    monkeypatch.setattr(wx, "gfs_surface_series", fake_series)
    monkeypatch.setattr(wx, "gfs_profile_dataset", fake_prof)
    monkeypatch.setattr(wx, "gfs_orography", fake_orog)
    monkeypatch.setattr(wx, "icon_eu_latest_run", lambda *a, **k: "2026010100")
    monkeypatch.setattr(wx, "ecmwf_point", fake_prof)

    client = TestClient(app.app)
    r = client.get("/api/brief", params={"lat": 37.98, "lon": 23.72, "hours": 24})
    assert r.status_code == 200, r.text
    assert called["series"] == 1, "the per-point path was not used when the flag is off"


def test_brief_degrades_to_the_per_point_path_on_a_cold_cache(monkeypatch):
    """Flag on but nothing loaded yet: the request must not 500, it must fall back
    to the live fetch. Otherwise enabling the flag would break the site until the
    first refresh finished."""
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "1")
    grids.STORE._current.pop("gfs", None)

    fallback = {"used": False}

    async def fake_series(lat, lon, hours=48):
        fallback["used"] = True
        return [{"step": s, "t2m_c": 20.0, "u10": 1.0, "v10": 0.0,
                 "wind_kmh": 3.6, "wind_dir": 270.0, "precip_mm": 0.0}
                for s in range(1, 25)]

    async def fake_prof(*a, **k):
        raise RuntimeError("no profile")

    monkeypatch.setattr(wx, "latest_gfs_run", lambda *a, **k: ("20260101", "00"))
    monkeypatch.setattr(wx, "gfs_surface_series", fake_series)
    monkeypatch.setattr(wx, "gfs_profile_dataset", fake_prof)
    monkeypatch.setattr(wx, "gfs_orography", lambda *a, **k: _async(100.0))
    monkeypatch.setattr(wx, "icon_eu_latest_run", lambda *a, **k: "2026010100")
    monkeypatch.setattr(wx, "ecmwf_point", fake_prof)

    client = TestClient(app.app)
    r = client.get("/api/brief", params={"lat": 37.98, "lon": 23.72, "hours": 24})
    assert r.status_code == 200, r.text
    assert fallback["used"] is True


def test_brief_falls_back_for_a_point_outside_the_greek_grid(monkeypatch):
    """The RAM grid is regional and the per-point path is not. With the flag on, a
    point in Berlin must still be answered by the server-side-subset path rather
    than 502 - turning the flag on must not shrink the served area."""
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "1")
    # A loaded, healthy GFS grid covering Greece only.
    monkeypatch.setitem(grids.STORE._current, "gfs", _gfs_grid())
    fallback = {"used": False}

    async def fake_series(lat, lon, hours=48):
        fallback["used"] = True
        return [{"step": s, "t2m_c": 18.0, "u10": 1.0, "v10": 0.0,
                 "wind_kmh": 3.6, "wind_dir": 270.0, "precip_mm": 0.0}
                for s in range(1, 25)]

    async def no_prof(*a, **k):
        raise RuntimeError("no profile")

    async def fake_orog(*a, **k):
        return 50.0

    monkeypatch.setattr(wx, "latest_gfs_run", lambda *a, **k: ("20260101", "00"))
    monkeypatch.setattr(wx, "gfs_surface_series", fake_series)
    monkeypatch.setattr(wx, "gfs_profile_dataset", no_prof)
    monkeypatch.setattr(wx, "gfs_orography", fake_orog)
    monkeypatch.setattr(wx, "icon_eu_latest_run", lambda *a, **k: "2026010100")
    monkeypatch.setattr(wx, "ecmwf_point", no_prof)

    client = TestClient(app.app)
    r = client.get("/api/brief", params={"lat": 52.52, "lon": 13.40, "hours": 24})
    assert r.status_code == 200, r.text
    assert fallback["used"] is True, "out-of-region point did not fall back"


def _icon_grid(run: str = "2026010100") -> grids.GridSpec:
    """An ICON-shaped grid whose temperature is in Celsius, exactly as
    scheduler.build_icon stores it. Anything reading it must convert at the
    boundary, because the per-point path yields raw Kelvin."""
    steps = [0, 6, 12, 18, 24, 48]
    lat = np.linspace(34.0, 42.0, 33)
    lon = np.linspace(18.0, 30.0, 49)
    n = steps.__len__()
    return grids.GridSpec(
        model="icon", run=run, lat=lat, lon=lon, steps=steps,
        vars={"t2m_c": np.full((n, lat.size, lon.size), 14.0, np.float32),
              "precip_mm": np.full((n, lat.size, lon.size), 0.5, np.float32),
              "cape": np.full((n, lat.size, lon.size), 120.0, np.float32)})


def test_icon_temperature_from_ram_is_reported_in_celsius(monkeypatch):
    """The ICON grid stores Celsius; the per-point path returns Kelvin and every
    consumer subtracts 273.15 itself. Getting the boundary wrong renders the ICON
    row as about -259 C next to a GFS row in Celsius - a wrong number, on the very
    card whose job is to compare the two models."""
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "1")
    monkeypatch.setitem(grids.STORE._current, "gfs", _gfs_grid())
    monkeypatch.setitem(grids.STORE._current, "icon", _icon_grid())

    async def no_prof(*a, **k):
        raise RuntimeError("no profile")

    monkeypatch.setattr(wx, "gfs_surface_series", no_prof)
    monkeypatch.setattr(wx, "gfs_profile_dataset", no_prof)
    monkeypatch.setattr(wx, "icon_eu_latest_run", lambda *a, **k: "2026010100")
    monkeypatch.setattr(wx, "ecmwf_point", no_prof)

    client = TestClient(app.app)
    # Pro tier: the model comparison grid is expert-only.
    r = client.get("/api/brief", params={"lat": 37.98, "lon": 23.72, "hours": 24,
                                         "token": ent.issue_token("pro", "passcode")})
    assert r.status_code == 200, r.text
    grid = r.json()["expert"]["model_grid"]
    icon_row = next(g for g in grid if "ICON" in g["model"])
    # 14 C in the grid must surface as 14, not 14 - 273.15.
    assert icon_row["t_now"] == pytest.approx(14.0, abs=0.01)
    assert icon_row["t_24h"] == pytest.approx(14.0, abs=0.01)


def _icon_europe_grid(run: str = "2026010100") -> grids.GridSpec:
    """An ICON grid at the real European extent (lat 29.5-70.5, lon -23.5-62.5),
    with only a handful of points so it stays cheap to allocate in a test. The
    axes matter more than the density: Berlin and Madrid must be inside."""
    steps = [0, 6, 12, 18, 24, 48]
    lat = np.linspace(29.5, 70.5, 21)
    lon = np.linspace(-23.5, 62.5, 29)
    n = len(steps)
    shape = (n, lat.size, lon.size)
    return grids.GridSpec(
        model="icon", run=run, lat=lat, lon=lon, steps=steps,
        vars={"t2m_c": np.full(shape, 11.0, np.float32),
              "precip_mm": np.full(shape, 0.2, np.float32),
              "cape": np.full(shape, 80.0, np.float32)})


def test_icon_europe_scope_serves_a_point_far_outside_greece(monkeypatch):
    """The continental upgrade. With the Greece-only grid, Berlin fell back to the
    25 km per-point path; with the European extent it is answered at 7 km from
    RAM. This is the difference the scope flag buys."""
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "1")
    monkeypatch.setenv("WX_RAM_ICON_SCOPE", "europe")
    monkeypatch.setitem(grids.STORE._current, "gfs", _gfs_grid())
    monkeypatch.setitem(grids.STORE._current, "icon", _icon_europe_grid())

    async def no_prof(*a, **k):
        raise RuntimeError("no profile")

    async def icon_boom(*a, **k):
        raise AssertionError("Berlin used the 25 km per-point ICON path")

    # Berlin is outside the GFS grid too, so the per-point GFS path must still
    # answer - that path is not what this test is about.
    async def gfs_ok(lat, lon, hours=48):
        return [{"step": s, "t2m_c": 17.0, "u10": 1.0, "v10": 0.0,
                 "wind_kmh": 3.6, "wind_dir": 270.0, "precip_mm": 0.0}
                for s in range(1, 25)]

    monkeypatch.setattr(wx, "gfs_surface_series", gfs_ok)
    monkeypatch.setattr(wx, "latest_gfs_run", lambda *a, **k: ("20260101", "00"))

    async def orog_ok(*a, **k):
        return 40.0

    monkeypatch.setattr(wx, "gfs_orography", orog_ok)
    monkeypatch.setattr(wx, "gfs_profile_dataset", no_prof)
    monkeypatch.setattr(wx, "icon_eu_point", icon_boom)
    monkeypatch.setattr(wx, "ecmwf_point", no_prof)

    client = TestClient(app.app)
    r = client.get("/api/brief", params={"lat": 52.52, "lon": 13.40, "hours": 24,
                                         "token": ent.issue_token("pro", "passcode")})
    assert r.status_code == 200, r.text
    grid = r.json()["expert"]["model_grid"]
    icon_row = next(g for g in grid if "ICON" in g["model"])
    assert icon_row["t_now"] == pytest.approx(11.0, abs=0.01)


def test_greece_scope_still_falls_back_for_berlin(monkeypatch):
    """The default scope must keep the old behaviour: Berlin has no ICON data in a
    Greek grid, so it must use the per-point path rather than come back empty."""
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "1")
    monkeypatch.delenv("WX_RAM_ICON_SCOPE", raising=False)
    monkeypatch.setitem(grids.STORE._current, "gfs", _gfs_grid())
    monkeypatch.setitem(grids.STORE._current, "icon", _icon_grid())

    async def no_prof(*a, **k):
        raise RuntimeError("no profile")

    fallback = {"used": False}

    async def icon_fallback(*a, **k):
        fallback["used"] = True
        return 275.0

    async def gfs_ok(lat, lon, hours=48):
        return [{"step": s, "t2m_c": 17.0, "u10": 1.0, "v10": 0.0,
                 "wind_kmh": 3.6, "wind_dir": 270.0, "precip_mm": 0.0}
                for s in range(1, 25)]

    async def orog_ok(*a, **k):
        return 40.0

    monkeypatch.setattr(wx, "gfs_surface_series", gfs_ok)
    monkeypatch.setattr(wx, "latest_gfs_run", lambda *a, **k: ("20260101", "00"))
    monkeypatch.setattr(wx, "gfs_orography", orog_ok)
    monkeypatch.setattr(wx, "gfs_profile_dataset", no_prof)
    monkeypatch.setattr(wx, "icon_eu_point", icon_fallback)
    monkeypatch.setattr(wx, "ecmwf_point", no_prof)

    client = TestClient(app.app)
    r = client.get("/api/brief", params={"lat": 52.52, "lon": 13.40, "hours": 24,
                                         "token": ent.issue_token("pro", "passcode")})
    assert r.status_code == 200, r.text
    assert fallback["used"] is True


def test_brief_uses_ram_for_a_point_inside_the_greek_grid(monkeypatch):
    """The complement of the test above: the fallback must not swallow points the
    grid can actually answer, otherwise the RAM path would never run at all."""
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "1")
    monkeypatch.setitem(grids.STORE._current, "gfs", _gfs_grid())

    async def series_boom(*a, **k):
        raise AssertionError("in-region point used the network path")

    async def no_prof(*a, **k):
        raise RuntimeError("no profile")

    monkeypatch.setattr(wx, "gfs_surface_series", series_boom)
    monkeypatch.setattr(wx, "gfs_profile_dataset", no_prof)
    monkeypatch.setattr(wx, "icon_eu_latest_run", lambda *a, **k: "2026010100")
    monkeypatch.setattr(wx, "ecmwf_point", no_prof)

    client = TestClient(app.app)
    r = client.get("/api/brief", params={"lat": 37.98, "lon": 23.72, "hours": 24})
    assert r.status_code == 200, r.text
    assert len(r.json()["simple"]["hours"]) == 24


async def _async(v):
    return v
