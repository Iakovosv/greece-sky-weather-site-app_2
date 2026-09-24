"""Ingestion layer for commercially-licensable weather data.

Every source in here is safe for commercial use:

* GFS      - NOAA/NWS, US public domain (17 USC 105)
* ICON-EU  - DWD, CC BY 4.0 (under GeoNutzV)
* ECMWF    - CC BY 4.0, best-effort only: the open-data servers rate-limit (429)
             and return intermittent 404s, so this is never the primary source.
* Photon   - OSM-derived geocoding (ODbL for the data, free service)

No source here has a non-commercial clause, unlike the Open-Meteo free tier.
"""
from __future__ import annotations

import asyncio
import bz2
import datetime as dt
import hashlib
import json
import logging
import os
import time

import httpx
import numpy as np
import xarray as xr

import cachestore
import config

log = logging.getLogger("wx.wx")

UA = {"User-Agent": "greece-sky-weather/0.2 (+https://github.com/Iakovosv/greece-sky-weather-site-app)"}
NOMADS = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
GFS_PROD = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod"
DWD = "https://opendata.dwd.de/weather/nwp/icon-eu/grib"
ECMWF_OD = "https://data.ecmwf.int/forecasts"
PHOTON = "https://photon.komoot.io/api/"

CACHE_DIR = os.environ.get("WX_CACHE_DIR", "/tmp/wx-cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# Memoization for the "which GFS cycle is published" probe. Five minutes is well
# inside the six-hour publication cadence, so it can never name a stale run.
RUN_LOOKUP_TTL_S = float(os.environ.get("WX_RUN_LOOKUP_TTL_S") or 300)
# {model: (monotonic_at_store, (date, hh))}. Plain dict, guarded by the GIL for
# the read/write of a single key; the probe itself is idempotent, so a rare
# double-probe after a cache expiry is harmless.
_run_lookup_cache: dict[str, tuple[float, tuple[str, str]]] = {}


# ---------------------------------------------------------------- request guards
#
# The only callers that reach these are the endpoint handlers, and each of them
# validates the coordinate first. They are kept here as a second line of defence
# for internal callers (the scheduler, a test), because the cost of getting it
# wrong is a whole-global GRIB download rather than an error message.

class OutOfRange(ValueError):
    """A coordinate that cannot be used against a weather model."""


def check_point(lat: float, lon: float) -> None:
    """Raise OutOfRange unless (lat, lon) is a real point on Earth."""
    reason = config.coord_error(lat, lon)
    if reason:
        raise OutOfRange(reason)


# ---------------------------------------------------------------- cache
#
# Delegated to cachestore, which adds a size ceiling, eviction and recovery from
# a truncated entry. `WX_CACHE_DIR` is resolved on every call so a `.env` loaded
# by app.py actually reaches it, and so tests can redirect it.

def cache_dir() -> str:
    return cachestore.cache_dir()


def _cache_path(key: str) -> str:
    return cachestore._path(key)


def cache_get(key: str, ttl: int) -> bytes | None:
    return cachestore.get(key, ttl)


def cache_put(key: str, blob: bytes) -> None:
    cachestore.put(key, blob)


def cache_stats() -> dict:
    return cachestore.stats()


_NOMADS_SEM: asyncio.Semaphore | None = None


def _nomads_sem() -> asyncio.Semaphore:
    """One shared limit for every NOMADS request in the process.

    The surface series, the sounding profile and the orography lookup are issued
    concurrently; without a shared cap they add up to ~10 parallel fetches and the
    server starts returning 403. Created lazily so it binds to the running loop.
    """
    global _NOMADS_SEM
    if _NOMADS_SEM is None:
        _NOMADS_SEM = asyncio.Semaphore(4)
    return _NOMADS_SEM


async def nomads_get(client: httpx.AsyncClient, params: list) -> bytes:
    """GET the NOMADS GRIB filter with a shared concurrency cap and backoff.

    NOMADS rejects bursts with 403 rather than 429, so a single failed request
    does not mean the run is missing. Retry the 403s before giving up.
    """
    delay = 1.0
    last: Exception | None = None
    for attempt in range(5):
        async with _nomads_sem():
            r = await client.get(NOMADS, params=params)
        if r.status_code == 200:
            return r.content
        last = httpx.HTTPStatusError(
            f"{r.status_code}", request=r.request, response=r)
        if r.status_code not in (403, 429, 500, 502, 503):
            break
        await asyncio.sleep(delay)
        delay *= 2
    raise last if last else RuntimeError("NOMADS request failed")


# ---------------------------------------------------------------- GFS (public domain)

def latest_gfs_run(now: dt.datetime | None = None) -> tuple[str, str]:
    """Most recent GFS cycle that is actually published (runs lag ~3.5-5 h).

    Memoized for `RUN_LOOKUP_TTL_S`. The probe is up to eight outbound requests,
    and it sits behind `/api/health`, which is deliberately exempt from the rate
    limiter so monitoring can poll it. Without this cache, a monitoring loop (or
    an attacker) polling health would hammer NOMADS and risk the 403 burst-block
    that takes the forecast down with it. A GFS cycle is published every six
    hours, so a few minutes of staleness cannot change the answer.

    `now` is an explicit override for tests and internal callers; it bypasses the
    cache, because a caller that names a time wants that time's answer.
    """
    if now is None:
        cached = _run_lookup_cache.get("gfs")
        if cached is not None and (time.monotonic() - cached[0]) < RUN_LOOKUP_TTL_S:
            return cached[1]
        resolved = _probe_latest_gfs_run(dt.datetime.now(dt.timezone.utc))
        _run_lookup_cache["gfs"] = (time.monotonic(), resolved)
        return resolved
    return _probe_latest_gfs_run(now)


def _probe_latest_gfs_run(now: dt.datetime) -> tuple[str, str]:
    cycle = (now - dt.timedelta(hours=4, minutes=30)).replace(minute=0, second=0, microsecond=0)
    cycle = cycle.replace(hour=(cycle.hour // 6) * 6)
    with httpx.Client(headers=UA, timeout=20) as c:
        for _ in range(8):
            date, hh = cycle.strftime("%Y%m%d"), f"{cycle.hour:02d}"
            try:
                r = c.get(f"{GFS_PROD}/gfs.{date}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f000.idx")
                if r.status_code == 200:
                    return date, hh
            except httpx.HTTPError:
                pass
            cycle -= dt.timedelta(hours=6)
    raise RuntimeError("no GFS cycle reachable")


GFS_SFC_VARS = ("TMP", "RH", "APCP", "UGRD", "VGRD", "GUST", "CAPE", "TCDC")
GFS_SFC_LEVS = ("2_m_above_ground", "10_m_above_ground", "surface", "entire_atmosphere")


def _varsig() -> str:
    """Short signature of the requested variable/level sets, for cache keys."""
    raw = ",".join(GFS_SFC_VARS) + "|" + ",".join(GFS_SFC_LEVS)
    return hashlib.sha1(raw.encode()).hexdigest()[:8]


async def gfs_surface_step(client: httpx.AsyncClient, lat: float, lon: float,
                           date: str, hh: str, step: int) -> dict:
    """One forecast hour of surface fields, server-side subset to a tiny box."""
    # An out-of-range coordinate is not a NOMADS error: the filter clamps the
    # sub-region to the whole grid and returns a full-planet field, which is how a
    # single `lat=999` request produced a 419 MB cache entry. Reject before any
    # request is made.
    check_point(lat, lon)
    # The variable set is part of the key: without it, adding a field to
    # GFS_SFC_VARS keeps serving cached blobs that predate the field.
    key = f"gfs-sfc|{date}{hh}|{step}|{lat:.2f},{lon:.2f}|{_varsig()}"
    blob = cache_get(key, ttl=3 * 3600)
    if blob is None:
        params = [("file", f"gfs.t{hh}z.pgrb2.0p25.f{step:03d}")]
        params += [(f"var_{v}", "on") for v in GFS_SFC_VARS]
        params += [(f"lev_{lv}", "on") for lv in GFS_SFC_LEVS]
        params += [("subregion", ""), ("leftlon", lon - 1.25), ("rightlon", lon + 1.25),
                   ("toplat", lat + 1.25), ("bottomlat", lat - 1.25),
                   ("dir", f"/gfs.{date}/{hh}/atmos")]
        blob = await nomads_get(client, params)
        cache_put(key, blob)

    path = os.path.join(cache_dir(), f"gfs-sfc-{os.getpid()}-{step}.grib2")
    with open(path, "wb") as f:
        f.write(blob)

    out: dict = {"step": step}
    try:
        # Each level type is opened separately: cfgrib cannot merge
        # heightAboveGround=2m with heightAboveGround=10m in one dataset.
        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={
            "indexpath": "", "filter_by_keys": {"typeOfLevel": "heightAboveGround", "level": 2}})
        out["t2m_c"] = float(ds["t2m"].sel(latitude=lat, longitude=lon, method="nearest").values) - 273.15
        # r2 is the 2 m relative humidity; it lets the caller derive a cloud base.
        if "r2" in ds:
            out["rh2_pct"] = float(ds["r2"].sel(latitude=lat, longitude=lon, method="nearest").values)
        ds.close()

        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={
            "indexpath": "", "filter_by_keys": {"typeOfLevel": "heightAboveGround", "level": 10}})
        u = float(ds["u10"].sel(latitude=lat, longitude=lon, method="nearest").values)
        v = float(ds["v10"].sel(latitude=lat, longitude=lon, method="nearest").values)
        ds.close()
        out["wind_kmh"] = float(np.hypot(u, v) * 3.6)
        out["wind_dir"] = float((np.degrees(np.arctan2(-u, -v)) + 360) % 360)
        out["u10"], out["v10"] = u, v

        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={
            "indexpath": "", "filter_by_keys": {"typeOfLevel": "surface"}})
        if "tp" in ds:
            out["precip_mm"] = float(ds["tp"].sel(latitude=lat, longitude=lon, method="nearest").values)
        if "gust" in ds:
            out["gust_kmh"] = float(ds["gust"].sel(latitude=lat, longitude=lon, method="nearest").values) * 3.6
        if "cape" in ds:
            out["cape"] = float(ds["cape"].sel(latitude=lat, longitude=lon, method="nearest").values)
        if "cin" in ds:
            out["cin"] = float(ds["cin"].sel(latitude=lat, longitude=lon, method="nearest").values)
        ds.close()

        # Total cloud cover drives the sky icon on the simple view. It lives on
        # its own typeOfLevel, so it needs a separate open like the 2 m/10 m split.
        # GFS publishes TCDC:entire atmosphere twice, once instantaneous and once
        # as an interval average, so cfgrib cannot pick a message without the
        # stepType. The instantaneous field is the one that describes the sky at
        # the forecast hour itself, which is what an hourly icon should show.
        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={
            "indexpath": "", "filter_by_keys": {
                "typeOfLevel": "atmosphere", "stepType": "instant"}})
        if "tcc" in ds:
            # Already percent in GRIB; do not scale again.
            out["cloud_pct"] = float(ds["tcc"].sel(latitude=lat, longitude=lon, method="nearest").values)
        ds.close()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return out


async def gfs_surface_series(lat: float, lon: float, hours: int = 48) -> list[dict]:
    """Surface series up to `hours`, respecting GFS's actual output cadence.

    GFS publishes hourly steps only to f120; beyond that it is 3-hourly. Requesting
    a nonexistent f121 would fail, so the step list is built from the cadence
    rather than assuming everything is hourly.
    """
    check_point(lat, lon)
    date, hh = latest_gfs_run()
    steps = gfs_steps(hours)

    async with httpx.AsyncClient(headers=UA, timeout=90) as client:
        async def one(s: int):
            try:
                return await gfs_surface_step(client, lat, lon, date, hh, s)
            except Exception as e:
                return {"step": s, "error": f"{type(e).__name__}: {str(e)[:80]}"}
        rows = await asyncio.gather(*(one(s) for s in steps))

    rows = [r for r in rows if "error" not in r]
    rows.sort(key=lambda r: r["step"])
    return rows


def gfs_steps(hours: int) -> list[int]:
    """Forecast steps that genuinely exist: hourly to 120, then every 3 hours."""
    hourly = [s for s in range(1, min(hours, 120) + 1)]
    if hours <= 120:
        return hourly
    return hourly + list(range(123, hours + 1, 3))


async def gfs_profile_dataset(client: httpx.AsyncClient, lat: float, lon: float,
                              step: int = 12, with_run_time: bool = False):
    """Full vertical profile on pressure levels, for Skew-T and instability indices.

    Returns the Dataset normally, or (Dataset, "YYYYMMDDHH") when with_run_time is
    set. The run identity is returned rather than re-derived by the caller because
    latest_gfs_run probes the network; deriving it twice could name a different
    cycle than the one actually downloaded.
    """
    check_point(lat, lon)
    date, hh = latest_gfs_run()
    key = f"gfs-prof|{date}{hh}|{step}|{lat:.2f},{lon:.2f}"
    blob = cache_get(key, ttl=3 * 3600)
    if blob is None:
        params = [("file", f"gfs.t{hh}z.pgrb2.0p25.f{step:03d}")]
        params += [(f"var_{v}", "on") for v in ("TMP", "RH", "HGT", "UGRD", "VGRD")]
        params += [("subregion", ""), ("leftlon", lon - 1.0), ("rightlon", lon + 1.0),
                   ("toplat", lat + 1.0), ("bottomlat", lat - 1.0),
                   ("dir", f"/gfs.{date}/{hh}/atmos")]
        blob = await nomads_get(client, params)
        cache_put(key, blob)

    path = os.path.join(cache_dir(), f"gfs-prof-{os.getpid()}-{step}.grib2")
    with open(path, "wb") as f:
        f.write(blob)
    ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={
        "indexpath": "", "filter_by_keys": {"typeOfLevel": "isobaricInhPa"}})
    if with_run_time:
        return ds, f"{date}{hh}"
    return ds


def step_to_utc(run_utc: str, step: int) -> str:
    """Valid time of a forecast step, ISO in UTC. run_utc is 'YYYYMMDDHH'."""
    run = dt.datetime.strptime(run_utc, "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
    return (run + dt.timedelta(hours=step)).strftime("%Y-%m-%dT%H:%MZ")


# ---------------------------------------------------------------- ICON-EU (DWD, CC BY 4.0)

ICON_EU_VARS = {"t2m": "t_2m", "precip": "tot_prec", "gust": "vmax_10m",
                "wind_u": "u_10m", "wind_v": "v_10m", "cape_ml": "cape_ml"}


def icon_eu_latest_run(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    d = now - dt.timedelta(hours=4)
    return d.strftime("%Y%m%d") + "00"


async def icon_eu_point(client: httpx.AsyncClient, lat: float, lon: float,
                        var: str, step: int, run: str | None = None) -> float:
    """ICON-EU is 7 km over Europe, hourly to +24 h then 3-hourly.

    One bz2 file per variable per step covering all of Europe, so these downloads
    are ~1 MB each and must be cached aggressively.
    """
    check_point(lat, lon)
    run = run or icon_eu_latest_run()
    short = ICON_EU_VARS[var]
    key = f"icon-eu|{run}|{var}|{step}"
    blob = cache_get(key, ttl=6 * 3600)
    if blob is None:
        name = (f"icon-eu_europe_regular-lat-lon_single-level_"
                f"{run}_{step:03d}_{short.upper()}.grib2.bz2")
        r = await client.get(f"{DWD}/{run[8:10]}/{short}/{name}")
        r.raise_for_status()
        blob = bz2.decompress(r.content)
        cache_put(key, blob)

    path = os.path.join(cache_dir(), f"icon-eu-{os.getpid()}-{short}-{step}.grib2")
    with open(path, "wb") as f:
        f.write(blob)
    try:
        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
        v = list(ds.data_vars)[0]
        val = float(ds[v].sel(latitude=lat, longitude=lon, method="nearest").values)
        ds.close()
        return val
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------- ECMWF (CC BY 4.0, best effort)

# Published ECMWF open-data files are immutable once a run is out, so the TTL
# only bounds how long a copy may linger on disk. Six hours matches ICON-EU and
# is shorter than the run cadence, so a superseded run is never served for long.
ECMWF_TTL_S = 6 * 3600


async def ecmwf_point(client: httpx.AsyncClient, lat: float, lon: float,
                      step: int = 24, hh: str = "00") -> dict:
    """ECMWF open data via HTTP Range requests on the byte offsets in its .index.

    Best-effort by design: the servers rate-limit (HTTP 429) and have returned
    intermittent 404s for steps that do exist, so callers must degrade gracefully
    and must never treat this as the primary source.

    Both upstream reads are cached, because without it every forecast re-fetched
    the same one index plus five Range slices: the grib2 is fetched per parameter,
    so a single point cost six GETs and a repeat request cost six more. That is
    also what produced the 429s that drop the model comparison. The keys are
    `stem`-based, not point-based: the Range slice is the whole field, so the
    point is selected after decode and two users asking for the same run share
    one download.
    """
    check_point(lat, lon)
    date = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=8)).strftime("%Y%m%d")
    stem = f"{ECMWF_OD}/{date}/{hh}z/ifs/0p25/oper/{date}{hh}0000-{step}h-oper-fc"

    index_key = f"ecmwf-index|{date}{hh}|{step}"
    blob = cache_get(index_key, ttl=ECMWF_TTL_S)
    if blob is None:
        r = await client.get(f"{stem}.index")
        r.raise_for_status()
        blob = r.content
        cache_put(index_key, blob)
    rows = [json.loads(line) for line in blob.decode().splitlines() if line.strip()]

    out: dict = {}
    for row in rows:
        if row.get("levtype") != "sfc" or row.get("param") not in ("2t", "10u", "10v", "msl", "tp"):
            continue
        param = row["param"]
        grib_key = f"ecmwf-grib|{date}{hh}|{step}|{param}"
        grib = cache_get(grib_key, ttl=ECMWF_TTL_S)
        if grib is None:
            start, length = row["_offset"], row["_length"]
            rr = await client.get(f"{stem}.grib2",
                                  headers={**UA, "Range": f"bytes={start}-{start + length - 1}"})
            rr.raise_for_status()
            grib = rr.content
            cache_put(grib_key, grib)
        path = os.path.join(cache_dir(), f"ec-{os.getpid()}-{row['param']}.grib2")
        with open(path, "wb") as f:
            f.write(grib)
        try:
            ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={
                "indexpath": "", "filter_by_keys": {"shortName": param}})
            v = list(ds.data_vars)[0]
            out[param] = float(ds[v].sel(latitude=lat, longitude=lon, method="nearest").values)
            ds.close()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        await asyncio.sleep(0.4)  # this endpoint rate-limits
    return out


# ---------------------------------------------------------------- elevation

async def gfs_orography(client: httpx.AsyncClient, lat: float, lon: float,
                        date: str, hh: str) -> float | None:
    """Terrain height of the GFS grid cell, from the model's own surface geopotential.

    This is the reference height that GFS's 2 m temperature actually represents.
    It needs no external service, and it is the correct baseline for a lapse-rate
    correction: a user on a 1000 m ridge gets a different temperature from a model
    cell whose mean height is 236 m.
    """
    check_point(lat, lon)
    key = f"gfs-orog|{date}{hh}|{lat:.2f},{lon:.2f}"
    blob = cache_get(key, ttl=12 * 3600)
    if blob is None:
        params = [("file", f"gfs.t{hh}z.pgrb2.0p25.f012"), ("var_HGT", "on"),
                  ("lev_surface", "on"), ("subregion", ""),
                  ("leftlon", lon - 0.5), ("rightlon", lon + 0.5),
                  ("toplat", lat + 0.5), ("bottomlat", lat - 0.5),
                  ("dir", f"/gfs.{date}/{hh}/atmos")]
        blob = await nomads_get(client, params)
        cache_put(key, blob)

    path = os.path.join(cache_dir(), f"gfs-orog-{os.getpid()}.grib2")
    with open(path, "wb") as f:
        f.write(blob)
    try:
        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={
            "indexpath": "", "filter_by_keys": {"typeOfLevel": "surface"}})
        v = list(ds.data_vars)[0]
        val = float(ds[v].sel(latitude=lat, longitude=lon, method="nearest").values)
        ds.close()
        return val
    except Exception:
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def dem_elevation(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    """Measured terrain height at the exact point, for comparison with the model.

    Uses the OpenTopoData public API. It is free, MIT-licensed and self-hostable,
    but the public instance caps at 1000 calls/day and forbids bulk use - for a
    commercial deployment, self-host it or use a paid provider (see LICENSES.md).
    EU-DEM at 25 m is the right dataset for Greece; SRTM 90 m is the fallback.
    """
    for dataset, note in (("eudem25m", "EU-DEM 25 m (Copernicus)"),
                          ("srtm90m", "SRTM 90 m (NASA/USGS)")):
        try:
            r = await client.get(f"https://api.opentopodata.org/v1/{dataset}",
                                 params={"locations": f"{lat:.5f},{lon:.5f}"}, timeout=20)
            if r.status_code != 200:
                continue
            res = r.json().get("results") or []
            if res and res[0].get("elevation") is not None:
                return {"elevation_m": round(float(res[0]["elevation"]), 1),
                        "dataset": dataset, "dataset_note": note, "source": "OpenTopoData"}
        except Exception:
            continue
    return {"elevation_m": None, "dataset": None,
            "source": "OpenTopoData", "error": "elevation service unavailable"}


# ---------------------------------------------------------------- geocoding (Photon / OSM)

async def geocode(client: httpx.AsyncClient, query: str, lat: float | None = None,
                  lon: float | None = None, limit: int = 5) -> list[dict]:
    """Biasing with lat/lon matters a lot: without it, "Ηλιούπολη" can resolve to
    a bus stop in Cyprus instead of the Athens suburb.
    """
    params: dict = {"q": query, "limit": limit}
    if lat is not None and lon is not None:
        params |= {"lat": lat, "lon": lon}
    r = await client.get(PHOTON, params=params)
    r.raise_for_status()

    out = []
    for f in r.json().get("features", []):
        p = f["properties"]
        flo, fla = f["geometry"]["coordinates"]
        label = p.get("name") or p.get("city") or ""
        parts = [p.get("city"), p.get("state"), p.get("country")]
        admin = ", ".join(dict.fromkeys(x for x in parts if x and x != label))
        out.append({"name": label, "admin1": admin, "country": p.get("country"),
                    "countrycode": p.get("countrycode"), "latitude": fla, "longitude": flo,
                    "osm_type": p.get("osm_type"), "osm_id": p.get("osm_id")})
    return out


async def reverse_geocode(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    r = await client.get("https://photon.komoot.io/reverse/", params={"lat": lat, "lon": lon})
    r.raise_for_status()
    fs = r.json().get("features", [])
    if not fs:
        return {"name": f"{lat:.3f}, {lon:.3f}"}
    p = fs[0]["properties"]
    return {"name": p.get("name") or p.get("city") or f"{lat:.3f}, {lon:.3f}",
            "city": p.get("city"), "state": p.get("state"), "country": p.get("country")}
