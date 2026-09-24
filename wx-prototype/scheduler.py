"""Background refresher that fills the in-memory grids.

Split from grids.py on purpose: grids.py holds arrays and does arithmetic and is
testable with no network at all; everything here touches the network and cfgrib.
The refresh *policy* (when to reload, what to do on failure) is still pure, so it
can be tested without downloading anything.

Cadence comes from each model's published run schedule, not from a fixed timer:
GFS cycles every 6 h with ~4.5 h lag, ICON-EU every 6 h with ~3 h lag, ECMWF
open data every 6 h with a longer lag. Polling faster than the model produces new
data just wastes bandwidth and invite HTTP 403s.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import tempfile

import httpx
import numpy as np
import xarray as xr

import grids
import wx

log = logging.getLogger("wx.scheduler")

# Steps the RAM path keeps. GFS is hourly to f120, then 3-hourly; PRO needs 10 days.
GFS_MAX_HOURS = int(os.environ.get("WX_RAM_GFS_HOURS", "240"))
# ICON-EU is one ~1 MB whole-of-Europe file per variable per step, so only the
# handful of steps the model-comparison grid actually shows are kept.
ICON_STEPS = (0, 6, 12, 18, 24, 48)

# How long a grid may go unrefreshed before it is considered stale in /api/health.
REFRESH_INTERVAL_S = int(os.environ.get("WX_RAM_REFRESH_S", str(6 * 3600)))
# If a refresh keeps failing, retry sooner than a full cycle.
RETRY_INTERVAL_S = int(os.environ.get("WX_RAM_RETRY_S", str(20 * 60)))


def gfs_steps(max_hours: int = GFS_MAX_HOURS) -> list[int]:
    """Forecast steps that GFS actually publishes, given the hourly->3-hourly shift.

    Requesting a nonexistent f121 is a hard failure, so the list is derived from
    the cadence rather than assumed uniform.
    """
    hourly = list(range(1, min(max_hours, 120) + 1))
    if max_hours <= 120:
        return hourly
    return hourly + list(range(123, max_hours + 1, 3))


def _bbox_params(bbox: dict, pad: float = 1.0) -> list[tuple[str, object]]:
    return [("subregion", ""),
            ("leftlon", bbox["west"] - pad), ("rightlon", bbox["east"] + pad),
            ("toplat", bbox["north"] + pad), ("bottomlat", bbox["south"] - pad)]


def _as_ascending(da: xr.DataArray) -> xr.DataArray:
    """Return the array with latitude ascending.

    GFS publishes latitude descending. Interpolating against a descending axis
    silently mirrors the domain, so this normalisation is not cosmetic: it is the
    difference between a correct Greek forecast and one read from the wrong
    hemisphere of the box.
    """
    latname = "latitude" if "latitude" in da.dims else "lat"
    if float(da[latname][0]) > float(da[latname][-1]):
        return da.sortby(latname)
    return da


def _decode_surface(path: str, bbox: dict) -> dict[str, np.ndarray]:
    """Decode one GFS surface GRIB2 file into {name: (lat, lon)} planes.

    Each GRIB typeOfLevel needs its own `open_dataset`: cfgrib cannot merge
    heightAboveGround=2 m with heightAboveGround=10 m in one dataset.
    """
    out: dict[str, np.ndarray] = {}
    lat_axis: np.ndarray | None = None
    lon_axis: np.ndarray | None = None

    for keys, names in (
        ({"typeOfLevel": "heightAboveGround", "level": 2}, {"t2m": "t2m_c", "r2": "rh2_pct"}),
        ({"typeOfLevel": "heightAboveGround", "level": 10}, {"u10": "u10", "v10": "v10"}),
        ({"typeOfLevel": "surface"}, {"tp": "precip_mm", "gust": "gust_kmh", "cape": "cape",
                                      "cin": "cin"}),
        ({"typeOfLevel": "atmosphere", "stepType": "instant"}, {"tcc": "cloud_pct"}),
    ):
        try:
            ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={
                "indexpath": "", "filter_by_keys": keys})
        except Exception:
            continue
        try:
            if not ds.data_vars:
                continue
            ds = _as_ascending(ds)
            if lat_axis is None:
                lat_axis = np.asarray(ds["latitude"].values, dtype=np.float64)
                lon_axis = np.asarray(ds["longitude"].values, dtype=np.float64)
            for src, dst in names.items():
                if src in ds:
                    out[dst] = np.asarray(ds[src].values, dtype=np.float32)
        finally:
            ds.close()

    if lat_axis is not None:
        out["_lat"] = lat_axis
        out["_lon"] = lon_axis
    return out


def _subset_bbox(plane: np.ndarray, lat: np.ndarray, lon: np.ndarray,
                 bbox: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Trim a global/continental plane to the Greece box.

    DWD publishes ICON-EU as one whole-Europe file with no server-side subset, so
    a full 657x1377 plane is ~3.6 MB per field per step. Slicing it here is the
    difference between ~1 MB and ~65 MB of resident memory for the ICON grid.
    Returns None if the box does not intersect the plane at all.
    """
    li = np.where((lat >= bbox["south"]) & (lat <= bbox["north"]))[0]
    lj = np.where((lon >= bbox["west"]) & (lon <= bbox["east"]))[0]
    if li.size == 0 or lj.size == 0:
        return None
    i0, i1 = int(li[0]), int(li[-1]) + 1
    j0, j1 = int(lj[0]), int(lj[-1]) + 1
    return plane[i0:i1, j0:j1], lat[i0:i1], lon[j0:j1]


def _decode_icon(path: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Decode one ICON-EU single-level file into (plane, lat, lon).

    This relies on the `regular-lat-lon` product DWD publishes alongside the
    native rotated grid. The rotated one would need a CDO remap before any of this
    arithmetic makes sense.
    """
    ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    try:
        ds = _as_ascending(ds)
        v = list(ds.data_vars)[0]
        latname = "latitude" if "latitude" in ds.dims else "lat"
        lonname = "longitude" if "longitude" in ds.dims else "lon"
        return (np.asarray(ds[v].values, dtype=np.float32),
                np.asarray(ds[latname].values, dtype=np.float64),
                np.asarray(ds[lonname].values, dtype=np.float64))
    finally:
        ds.close()


def _write_tmp(blob: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".grib2", dir=wx.cache_dir())
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
    return path


async def build_gfs(client: httpx.AsyncClient) -> grids.GridSpec:
    """Download and decode the whole GFS surface series for the Greece box.

    This runs in the background once per run. It is the only place that pays the
    GRIB decode cost, which is the entire point of the exercise: it used to be
    paid per user request.
    """
    date, hh = wx.latest_gfs_run()
    run = f"{date}{hh}"
    steps = gfs_steps()
    bbox = grids.GREEK_BBOX

    lat_axis = lon_axis = None
    orog_plane = None

    async def one(step: int):
        params = [("file", f"gfs.t{hh}z.pgrb2.0p25.f{step:03d}")]
        params += [(f"var_{v}", "on") for v in wx.GFS_SFC_VARS]
        params += [(f"lev_{lv}", "on") for lv in wx.GFS_SFC_LEVS]
        params += _bbox_params(bbox)
        params += [("dir", f"/gfs.{date}/{hh}/atmos")]
        blob = await wx.nomads_get(client, params)
        path = _write_tmp(blob)
        try:
            return step, _decode_surface(path, bbox)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    results = await asyncio.gather(*(one(s) for s in steps), return_exceptions=True)

    # Orography is a single field valid for the whole run, so fetch it once.
    async def fetch_orog():
        # Same bbox and pad as the surface fields, so the returned plane shares
        # the main grid's axes. Using a different pad here reproduces the exact
        # failure this replaces: a plane on one set of coordinates paired with
        # another set of axes, which the shape guard rejects as "no orography".
        params = [("file", f"gfs.t{hh}z.pgrb2.0p25.f012"), ("var_HGT", "on"),
                  ("lev_surface", "on")] + _bbox_params(bbox)
        params += [("dir", f"/gfs.{date}/{hh}/atmos")]
        blob = await wx.nomads_get(client, params)
        path = _write_tmp(blob)
        try:
            ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={
                "indexpath": "", "filter_by_keys": {"typeOfLevel": "surface"}})
            try:
                ds = _as_ascending(ds)
                v = list(ds.data_vars)[0]
                plane = np.asarray(ds[v].values, dtype=np.float32)
                la = np.asarray(ds["latitude"].values, dtype=np.float64)
                lo = np.asarray(ds["longitude"].values, dtype=np.float64)
                return plane, la, lo
            finally:
                ds.close()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    try:
        plane, ola, olo = await fetch_orog()
        orog = (plane, ola, olo)
    except Exception as e:
        orog = None
        log.warning("GFS orography unavailable, elevation correction will fall back: %s", e)

    # Collect per step, then keep only fields present in *every* kept step. A
    # field that some steps publish and others do not would otherwise be stacked
    # against a step index that no longer lines up with it, silently shifting one
    # hour onto another.
    by_step: dict[int, dict[str, np.ndarray]] = {}
    for r in results:
        if isinstance(r, Exception) or r is None:
            continue
        step, decoded = r
        if "_lat" not in decoded:
            continue
        if lat_axis is None:
            lat_axis, lon_axis = decoded["_lat"], decoded["_lon"]
        by_step[step] = {k: v for k, v in decoded.items() if not k.startswith("_")}

    core = ("t2m_c", "u10", "v10")
    kept = sorted(s for s, d in by_step.items() if all(c in d for c in core))
    if not kept:
        raise RuntimeError("GFS refresh produced no step with the core surface fields")

    common = set(by_step[kept[0]])
    for s in kept[1:]:
        common &= set(by_step[s])
    cubes = {name: np.stack([by_step[s][name] for s in kept]).astype(np.float32)
             for name in sorted(common)}

    # Unit fixups so the RAM path reproduces the per-point path's conventions.
    if "gust_kmh" in cubes:
        cubes["gust_kmh"] = cubes["gust_kmh"] * 3.6
    if "t2m_c" in cubes:
        cubes["t2m_c"] = cubes["t2m_c"] - 273.15

    meta: dict = {}
    if orog is not None:
        # The orography carries its own axes: it is read back on whatever grid the
        # GRIB subset produces, which need not match the main fields' coordinates.
        meta["orog"], meta["orog_lat"], meta["orog_lon"] = orog

    return grids.GridSpec(model="gfs", run=run, lat=lat_axis, lon=lon_axis,
                          steps=sorted(kept), vars=cubes, meta=meta)


async def build_icon(client: httpx.AsyncClient, bbox: dict | None = None) -> grids.GridSpec:
    """ICON-EU at the few steps the comparison grid uses.

    Each variable/step is a ~1 MB whole-of-Europe bz2 file, and DWD gives no
    server-side subset, so the decode is followed by a trim. `bbox` chooses how
    much is kept: the Greece box is the high-resolution home market and the
    default, while `grids.EUROPE_BBOX` keeps the whole published domain so a point
    anywhere in Europe can be served from RAM.

    The trade is memory and it is real: measured live, the Greece box is 1.79 MB
    and the full domain is 65.1 MB, a 36x difference. That is why the wide scope
    is a separate choice rather than the default, and why the GFS grid is still
    what covers the long hourly series.
    """
    run = wx.icon_eu_latest_run()
    bbox = bbox or grids.GREEK_BBOX
    want = {"t2m": "t2m_c", "precip": "precip_mm", "cape_ml": "cape"}

    cubes: dict[str, list[np.ndarray]] = {}
    lat_axis = lon_axis = None
    kept: list[int] = []

    async def one(var: str, step: int):
        short = wx.ICON_EU_VARS[var]
        name = (f"icon-eu_europe_regular-lat-lon_single-level_"
                f"{run}_{step:03d}_{short.upper()}.grib2.bz2")
        r = await client.get(f"{wx.DWD}/{run[8:10]}/{short}/{name}")
        r.raise_for_status()
        import bz2
        path = _write_tmp(bz2.decompress(r.content))
        try:
            plane, la, lo = _decode_icon(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        # DWD gives no server-side subset for ICON-EU, so the whole-Europe grid is
        # trimmed here before it reaches RAM.
        sub = _subset_bbox(plane, la, lo, bbox)
        if sub is None:
            raise RuntimeError("ICON-EU plane does not cover the Greece box")
        return var, step, sub

    jobs = [(v, s) for v in want for s in ICON_STEPS]
    results = await asyncio.gather(*(one(v, s) for v, s in jobs), return_exceptions=True)

    per_step: dict[int, dict[str, np.ndarray]] = {}
    for (v, s), r in zip(jobs, results):
        if isinstance(r, Exception) or r is None:
            continue
        _, _, (plane, la, lo) = r
        if lat_axis is None:
            lat_axis, lon_axis = la, lo
        per_step.setdefault(s, {})[want[v]] = plane

    if lat_axis is None or not per_step:
        raise RuntimeError("ICON refresh produced no usable fields")

    # Same alignment rule as GFS: a field must exist at every kept step, or its
    # cube would be shifted relative to `steps`.
    steps_ok = [s for s in sorted(per_step) if per_step[s]]
    common = set(per_step[steps_ok[0]])
    for s in steps_ok[1:]:
        common &= set(per_step[s])
    if not common:
        raise RuntimeError("ICON refresh: no field present at every step")

    cubes = {name: np.stack([per_step[s][name] for s in steps_ok]).astype(np.float32)
             for name in sorted(common)}

    if "t2m_c" in cubes:
        cubes["t2m_c"] = cubes["t2m_c"] - 273.15

    return grids.GridSpec(model="icon", run=run, lat=lat_axis, lon=lon_axis,
                          steps=steps_ok, vars=cubes)


async def refresh_once(store: grids.GridStore, builders: dict | None = None) -> dict:
    """Try every model once, keeping the last good grid on failure.

    Never raises for a single model's failure: one unreachable source must not
    take down the others, and must not blank the site while a good previous run is
    still in memory.
    """
    builders = builders or {"gfs": build_gfs,
                            "icon": lambda c: build_icon(c, grids.icon_bbox())}
    out: dict[str, str] = {}
    async with httpx.AsyncClient(headers=wx.UA, timeout=180) as client:
        for model, build in builders.items():
            try:
                grid = await build(client)
                store.replace(model, grid)
                out[model] = f"ok run={grid.run} steps={len(grid.steps)}"
                log.info("RAM grid %s refreshed: run=%s steps=%d vars=%s",
                         model, grid.run, len(grid.steps), sorted(grid.vars))
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:160]}"
                store.mark_failure(model, msg)
                out[model] = f"failed: {msg}"
                log.warning("RAM grid %s refresh failed (%s); keeping previous run",
                            model, msg)
    return out


async def run_forever(store: grids.GridStore, interval_s: int = REFRESH_INTERVAL_S,
                      retry_s: int = RETRY_INTERVAL_S,
                      builders: dict | None = None) -> None:
    """Refresh loop. Sleeps a full interval after a clean pass, a short retry after
    a partial one, so a transient outage recovers in minutes rather than hours."""
    while True:
        try:
            results = await refresh_once(store, builders)
        except Exception as e:  # never let the loop die
            log.exception("RAM grid refresh pass crashed: %s", e)
            results = {}
        failed = any(not v.startswith("ok") for v in results.values()) or not results
        await asyncio.sleep(retry_s if failed else interval_s)


def start(store: grids.GridStore | None = None) -> asyncio.Task | None:
    """Kick off the background refresher, if the feature flag is on.

    Returns the task so the caller can cancel it at shutdown; returns None when the
    flag is off, so importing this module never starts network traffic by itself.
    """
    if not grids.flag_enabled():
        log.info("WX_USE_RAM_GRIDS is off; RAM grid scheduler not started")
        return None
    return asyncio.create_task(run_forever(store or grids.STORE))
