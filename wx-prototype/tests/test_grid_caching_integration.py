"""Integration guarantees for the shared-grid cache.

These cover the claims that only make sense against the real request handlers,
not against the store or the scheduler in isolation:

  * with the flag on, many distinct locations cost **zero** GFS upstream calls -
    the property that motivated the work, since downloads must scale with model
    runs and not with users or points;
  * the forecast and the notification path read the **same** grid, so an alert
    and the forecast it is derived from cannot disagree;
  * with the flag **off**, none of this new machinery engages - no disk read, no
    archive written, and the per-point path behaves exactly as before.

Every test is hermetic: `WX_CACHE_DIR` is a tmp dir and the network entry points
are replaced so an accidental call is a visible failure rather than a slow pass.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app        # noqa: E402
import entitlements as ent  # noqa: E402
import grids      # noqa: E402
import wx         # noqa: E402


@pytest.fixture(autouse=True)
def clean_store(monkeypatch, tmp_path):
    monkeypatch.setenv("WX_CACHE_DIR", str(tmp_path / "cache"))
    grids.STORE._current.clear()
    grids.STORE._previous.clear()
    grids.STORE.stats.clear()
    yield
    grids.STORE._current.clear()
    grids.STORE.stats.clear()


def _gfs_grid(run: str = "2026010100") -> grids.GridSpec:
    """A GFS-shaped grid covering Greece, with the surface fields the path reads."""
    steps = list(range(1, 25))
    lat = np.linspace(34.0, 42.0, 33)
    lon = np.linspace(18.0, 30.0, 49)
    shape = (len(steps), lat.size, lon.size)
    mk = lambda v: np.full(shape, v, dtype=np.float32)
    return grids.GridSpec(
        model="gfs", run=run, lat=lat, lon=lon, steps=steps,
        vars={"t2m_c": mk(15.0), "rh2_pct": mk(60.0), "u10": mk(3.0),
              "v10": mk(-1.0), "precip_mm": mk(0.0), "cloud_pct": mk(20.0)},
        meta={"orog": np.full((lat.size, lon.size), 120.0, dtype=np.float32)})


def _forbid_edge_calls(monkeypatch):
    """Profile, ICON per-point and ECMWF are separate; let them fail cheaply.

    Only the GFS path is under test, so these are allowed to be unavailable. They
    must not mask a GFS call, which is why GFS entries get their own counter.
    """
    async def prof_boom(*a, **k):
        raise RuntimeError("profile unavailable")

    monkeypatch.setattr(wx, "gfs_profile_dataset", prof_boom)
    monkeypatch.setattr(wx, "icon_eu_latest_run", lambda *a, **k: "2026010100")
    monkeypatch.setattr(wx, "ecmwf_point", prof_boom)


# ------------------------------------------------------- many locations, no scaling

def test_many_locations_cost_zero_gfs_upstream_calls(monkeypatch):
    """The commercial property: N users at N points must not mean N downloads.

    With the shared grid loaded, every point in Greece is answered by reading
    numbers out of RAM. NOMADS is wired to raise, so any residual per-point fetch
    turns this into a failure instead of a slow, correct pass.
    """
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "1")
    monkeypatch.setitem(grids.STORE._current, "gfs", _gfs_grid())

    async def nomads_boom(*a, **k):
        raise AssertionError("a GFS point fetch happened despite the shared grid")

    async def series_boom(*a, **k):
        raise AssertionError("the per-point series path was used despite the grid")

    monkeypatch.setattr(wx, "nomads_get", nomads_boom)
    monkeypatch.setattr(wx, "gfs_surface_series", series_boom)
    monkeypatch.setattr(wx, "latest_gfs_run", lambda *a, **k: ("20260101", "00"))
    _forbid_edge_calls(monkeypatch)

    client = TestClient(app.app)
    points = [(37.98, 23.73), (37.10, 25.38), (40.64, 22.94),
              (35.34, 25.13), (38.25, 21.73), (39.62, 19.92)]
    for lat, lon in points:
        r = client.get("/api/brief", params={"lat": lat, "lon": lon, "hours": 72})
        assert r.status_code == 200, r.text
        assert r.json()["meta"]["gfs_run"] == "2026010100"


def test_the_same_point_twice_is_answered_from_ram_both_times(monkeypatch):
    """Same location, same run: no fetch on the first request or the second."""
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "1")
    monkeypatch.setitem(grids.STORE._current, "gfs", _gfs_grid())

    async def boom(*a, **k):
        raise AssertionError("network touched for a RAM-served point")

    monkeypatch.setattr(wx, "nomads_get", boom)
    monkeypatch.setattr(wx, "gfs_surface_series", boom)
    monkeypatch.setattr(wx, "latest_gfs_run", lambda *a, **k: ("20260101", "00"))
    _forbid_edge_calls(monkeypatch)

    client = TestClient(app.app)
    for _ in range(3):
        r = client.get("/api/brief", params={"lat": 37.98, "lon": 23.73, "hours": 24})
        assert r.status_code == 200, r.text


def test_without_the_flag_each_location_still_uses_the_per_point_path(monkeypatch):
    """The contrast that documents unchanged behaviour when the flag is off.

    Three distinct locations must each hit the per-point series exactly once -
    that is the pre-existing shape, and this work must not have altered it.
    """
    monkeypatch.delenv("WX_USE_RAM_GRIDS", raising=False)
    seen: list = []

    async def fake_series(lat, lon, hours=48):
        seen.append((round(lat, 3), round(lon, 3)))
        return [{"step": s, "t2m_c": 20.0, "u10": 1.0, "v10": 0.0,
                 "wind_kmh": 3.6, "wind_dir": 270.0, "precip_mm": 0.0}
                for s in range(1, 25)]

    async def fake_orog(*a, **k):
        return 100.0

    monkeypatch.setattr(wx, "latest_gfs_run", lambda *a, **k: ("20260101", "00"))
    monkeypatch.setattr(wx, "gfs_surface_series", fake_series)
    monkeypatch.setattr(wx, "gfs_profile_dataset", lambda *a, **k: _async_boom())
    monkeypatch.setattr(wx, "gfs_orography", fake_orog)
    monkeypatch.setattr(wx, "icon_eu_latest_run", lambda *a, **k: "2026010100")
    monkeypatch.setattr(wx, "ecmwf_point", lambda *a, **k: _async_boom())

    client = TestClient(app.app)
    for lat, lon in ((37.98, 23.73), (37.10, 25.38), (40.64, 22.94)):
        r = client.get("/api/brief", params={"lat": lat, "lon": lon, "hours": 72})
        assert r.status_code == 200, r.text

    assert len(seen) == 3, f"expected one per-point fetch per location, got {seen}"


async def _async_boom():
    raise RuntimeError("unavailable")


# ------------------------------------------------------- forecast and notifications

def test_forecast_and_notifications_read_the_same_grid(monkeypatch):
    """An alert is derived from the same numbers the forecast shows.

    Both go through `_notify_series` and `_build_brief`, which share the store.
    The notification series is driven directly here so the assertion is about the
    source, not about whether a rule happened to fire.
    """
    import asyncio

    monkeypatch.setenv("WX_USE_RAM_GRIDS", "1")
    grid = _gfs_grid(run="2026010100")
    monkeypatch.setitem(grids.STORE._current, "gfs", grid)

    async def boom(*a, **k):
        raise AssertionError("notifications did not use the shared grid")

    monkeypatch.setattr(wx, "nomads_get", boom)
    monkeypatch.setattr(wx, "gfs_surface_series", boom)

    rows = asyncio.run(app._notify_series(37.98, 23.73))
    assert rows is not None and rows, "notifications produced no series from the grid"
    # This test is about *which source* the notifications read, not about the
    # units on the way through. Comparing against the same normalization applied
    # to the grid rows is what proves they share a source: if notifications had
    # gone to the network instead, this process would have made a call and the
    # assertion above would already have failed.
    same = app._normalize_hours(grids.surface_rows(grid, 37.98, 23.73, [1]))[0]
    assert rows[0]["t"] == pytest.approx(same["t"])
    assert rows[0]["precip"] == pytest.approx(same["precip"])


# ------------------------------------------------------- flag off: nothing engages

def test_flag_off_never_reads_a_persisted_grid(monkeypatch):
    """A grid sitting on disk must be ignored while the flag is off.

    This is the safety property: enabling-by-accident is impossible because the
    request path only consults the archive when the flag says so. The archive is
    present and valid, and the flag is the only thing standing between it and RAM.
    """
    monkeypatch.delenv("WX_USE_RAM_GRIDS", raising=False)
    grids.save_grid(_gfs_grid(run="2026010100"), scope="greece")
    assert grids._disk_run_counts()["gfs|greece"] == 1

    async def fake_series(lat, lon, hours=48):
        return [{"step": s, "t2m_c": 20.0, "u10": 1.0, "v10": 0.0,
                 "wind_kmh": 3.6, "wind_dir": 270.0, "precip_mm": 0.0}
                for s in range(1, 25)]

    monkeypatch.setattr(wx, "latest_gfs_run", lambda *a, **k: ("20260101", "00"))
    monkeypatch.setattr(wx, "gfs_surface_series", fake_series)
    monkeypatch.setattr(wx, "gfs_profile_dataset", lambda *a, **k: _async_boom())
    monkeypatch.setattr(wx, "gfs_orography", lambda *a, **k: _async_val(100.0))
    monkeypatch.setattr(wx, "icon_eu_latest_run", lambda *a, **k: "2026010100")
    monkeypatch.setattr(wx, "ecmwf_point", lambda *a, **k: _async_boom())

    client = TestClient(app.app)
    r = client.get("/api/brief", params={"lat": 37.98, "lon": 23.73, "hours": 24})
    assert r.status_code == 200, r.text
    assert grids.STORE.get("gfs") is None, "a persisted grid was loaded while the flag is off"


async def _async_val(v):
    return v


def test_flag_off_the_scheduler_never_persists(monkeypatch):
    """With the flag off, `start()` is the only entry point and it does nothing."""
    monkeypatch.delenv("WX_USE_RAM_GRIDS", raising=False)
    import scheduler
    assert scheduler.start() is None
    assert grids.disk_bytes() == 0


def test_flag_off_is_the_default(monkeypatch):
    """The default must stay off: a deploy that has not opted in is unchanged."""
    monkeypatch.delenv("WX_USE_RAM_GRIDS", raising=False)
    assert grids.flag_enabled() is False
    for value in ("", "0", "off", "no", "false", "random"):
        monkeypatch.setenv("WX_USE_RAM_GRIDS", value)
        assert grids.flag_enabled() is False, f"{value!r} enabled the flag"


def test_health_reports_persistence_only_when_relevant(monkeypatch, tmp_path):
    """`persist` is always reported so an operator can see retention working.

    Off by default means an empty archive set, not a hidden key: the value is the
    evidence that nothing was written.
    """
    import scheduler
    monkeypatch.delenv("WX_USE_RAM_GRIDS", raising=False)
    health = scheduler.DEFAULT_TARGETS  # touch the module for import-time wiring
    assert set(health) == {"gfs", "icon"}
    assert grids.GridStore().health()["persist"]["disk_runs"] == {}


# ------------------------------------------------------- pro endpoint safety

def test_pro_endpoint_still_gated_with_the_ram_path_on(monkeypatch):
    """Turning the cache on must not leak PRO data to a free caller.

    The shared grid is the GFS primary series, which is free. The expert payload
    remains gated by the token, and this is asserted here because a caching change
    that served the grid without re-checking the tier would be a real regression.
    """
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "1")
    monkeypatch.setitem(grids.STORE._current, "gfs", _gfs_grid())
    _forbid_edge_calls(monkeypatch)

    async def boom(*a, **k):
        raise AssertionError("unexpected GFS network call")

    monkeypatch.setattr(wx, "nomads_get", boom)
    monkeypatch.setattr(wx, "gfs_surface_series", boom)
    monkeypatch.setattr(wx, "latest_gfs_run", lambda *a, **k: ("20260101", "00"))

    client = TestClient(app.app)
    free = client.get("/api/brief", params={"lat": 37.98, "lon": 23.73, "hours": 24})
    assert free.status_code == 200
    assert free.json()["meta"]["hours"] == 24
    # A free caller gets the paywall marker, never the expert data itself.
    expert = free.json().get("expert") or {}
    assert expert.get("locked") is True
    assert "sounding" not in expert and "model_grid" not in expert

    pro = client.get("/api/brief", params={
        "lat": 37.98, "lon": 23.73, "hours": 240,
        "token": ent.issue_token("pro", "passcode")})
    assert pro.status_code == 200
    assert pro.json()["meta"]["hours"] == 240
    assert (pro.json().get("expert") or {}).get("locked") is not True
