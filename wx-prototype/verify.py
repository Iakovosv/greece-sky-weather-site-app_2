"""Forecast verification against ERA5 reanalysis.

This answers the one question the rest of the product could not: *how wrong are
we, actually?* Everything here compares an **archived** model forecast against
ERA5 truth for the same valid time, so the score cannot be inflated by looking
at the model's own analysis.

Two keyless, commercially-licensable sources make this possible:

* ``noaa-gfs-bdp-pds`` (AWS Open Data) keeps every GFS cycle back to 2021 and
  ships a ``.idx`` beside each GRIB2, so one variable is a single HTTP range
  request instead of a ~500 MB file.
* ARCO-ERA5 (Google's public bucket) is ERA5 on a Zarr store, chunked one hour of
  the whole globe at a time. One coordinate pair costs one chunk (~1.9 MB).

Both are the *same data* the operational paths already use, so no new licence
obligation is introduced: GFS stays public domain, ERA5 stays CC BY 4.0.

ERA5 lags real time by about five days (the ERA5T early-release stream), which is
why the default verification window stops a week back rather than at yesterday.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import math
import os
import time

import httpx
import numpy as np

from wx import UA, cache_dir, cache_get, cache_put

# ARCHIVE_BASE is the AWS Open Data mirror of the same NOAA GFS product that
# NOMADS serves, kept because NOMADS only retains a few cycles.
ARCHIVE_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
ARCO_ERA5 = "gcs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"

# Matched to the GFS cadence the operational path already uses.
LEADS_H = (24, 48, 72, 96, 120)
BOX_DEG = 0.75          # +/- around the point, to blunt grid representativeness error
ERA5_LAG_DAYS = 6       # ERA5T publication lag, plus a margin
DEFAULT_DAYS = 4        # run dates to average over


# ------------------------------------------------------------ pure maths

def box_mean(field: np.ndarray, lat_axis: np.ndarray, lon_axis: np.ndarray,
             lat: float, lon: float, box: float = BOX_DEG) -> float:
    """Mean of `field` over a lat/lon box centred on the point.

    A single nearest grid point carries its own sampling error on top of the
    model's: at 0.25 deg the Athens cell is ~25 km across, so comparing two
    different grids point-to-point measures the grids as much as the forecast.
    Averaging a small box makes the comparison about the air mass instead.
    """
    lat_axis = np.asarray(lat_axis, dtype=float)
    lon_axis = np.asarray(lon_axis, dtype=float)
    lon_wrapped = ((lon + 180) % 360) - 180
    lon_hit = np.abs(((lon_axis + 180) % 360) - 180 - lon_wrapped) <= box
    if not lon_hit.any():
        lon_hit = np.abs(lon_axis - lon) <= box
    lat_hit = np.abs(lat_axis - lat) <= box
    if not lat_hit.any() or not lon_hit.any():
        return float("nan")
    sub = np.asarray(field, dtype=float)[np.ix_(np.nonzero(lat_hit)[0],
                                                np.nonzero(lon_hit)[0])]
    if sub.size == 0 or np.all(np.isnan(sub)):
        return float("nan")
    return float(np.nanmean(sub))


def scores(errors: list[float]) -> dict:
    """Bias, MAE, RMSE and sample count for a list of forecast-minus-truth errors.

    All three are reported because they answer different questions: bias says
    whether the model runs warm or cold, MAE says how big a typical miss is, and
    RMSE punishes the occasional large miss that MAE hides.
    """
    vals = [float(e) for e in errors if e is not None and not math.isnan(e)]
    n = len(vals)
    if n == 0:
        return {"n": 0, "bias": None, "mae": None, "rmse": None}
    arr = np.asarray(vals, dtype=float)
    return {
        "n": n,
        "bias": round(float(arr.mean()), 2),
        "mae": round(float(np.abs(arr).mean()), 2),
        "rmse": round(float(np.sqrt((arr ** 2).mean())), 2),
    }


def verification_plan(now: dt.datetime | None = None,
                      days: int = DEFAULT_DAYS) -> list[dict]:
    """Run dates and steps that can actually be checked against ERA5 right now.

    Each entry is a GFS 00z run whose *last* verification step already sits behind
    the ERA5T publication lag, so no request lands on a time ERA5 does not have.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    newest_valid = (now - dt.timedelta(days=ERA5_LAG_DAYS)).date()
    plan = []
    for back in range(days):
        run_date = newest_valid - dt.timedelta(days=LEADS_H[-1] // 24 + back)
        for lead in LEADS_H:
            valid = dt.datetime.combine(run_date, dt.time(0), tzinfo=dt.timezone.utc)
            valid += dt.timedelta(hours=lead)
            if valid.date() > newest_valid:
                continue
            plan.append({"run_date": run_date.strftime("%Y%m%d"), "cycle": "00",
                         "step": lead, "lead_h": lead, "valid": valid})
    return plan


# --------------------------------------------------------------- ERA5 side

_ARCO_GROUP = None


def _arco_group():
    """Lazy Zarr handle for ARCO-ERA5.

    Deliberately lazy: the verification path is the only caller, and importing
    zarr/gcsfs on every server boot would be wasted work for the common request.
    """
    global _ARCO_GROUP
    if _ARCO_GROUP is None:
        import zarr
        from zarr.storage import FsspecStore
        store = FsspecStore.from_url(ARCO_ERA5, storage_options={"token": "anon"})
        _ARCO_GROUP = zarr.open_group(store, mode="r")
    return _ARCO_GROUP


def era5_available_range() -> tuple[str, str]:
    """Date range the ERA5 store currently covers, straight from its metadata."""
    import json as _json
    import urllib.request
    url = (ARCO_ERA5.replace("gcs://gcp-public-data-arco-era5/",
                             "https://storage.googleapis.com/gcp-public-data-arco-era5/")
           + "/.zattrs")
    with urllib.request.urlopen(url, timeout=20) as r:
        a = _json.load(r)
    return a.get("valid_time_start"), a.get("valid_time_stop_era5t")


def era5_times(valid_times: list[dt.datetime]) -> np.ndarray:
    """Map wall-clock times onto the store's hour index."""
    g = _arco_group()
    hours = np.asarray(g["time"][:], dtype="int64")
    origin = np.datetime64("1900-01-01T00")
    index = {}
    for t in valid_times:
        want = np.datetime64(t.replace(tzinfo=None).replace(minute=0, second=0,
                                                           microsecond=0), "h")
        offset = int((want - origin) / np.timedelta64(1, "h"))
        pos = int(np.argmin(np.abs(hours - offset)))
        index[t] = pos
    return index


def era5_fields(valid_times: list[dt.datetime]) -> dict:
    """2 m temperature and 10 m wind for the given hours, cached per hour.

    Each hour lives in its own chunk, so the whole globe is one HTTP request and
    slicing our box out of it is free. That is why this reads one time at a time
    rather than one point at a time.
    """
    g = _arco_group()
    positions = era5_times(valid_times)
    lat = np.asarray(g["latitude"][:], dtype=float)
    lon = np.asarray(g["longitude"][:], dtype=float)
    out = {}
    for t, pos in positions.items():
        key = f"era5|{t.strftime('%Y%m%d%H')}|t2m+wind10"
        blob = cache_get(key, ttl=90 * 24 * 3600)
        if blob is None:
            t2m = np.asarray(g["2m_temperature"][pos, :, :], dtype=float) - 273.15
            u10 = np.asarray(g["10m_u_component_of_wind"][pos, :, :], dtype=float)
            v10 = np.asarray(g["10m_v_component_of_wind"][pos, :, :], dtype=float)
            ws = np.hypot(u10, v10) * 3.6
            buf = np.stack([t2m, ws]).astype("float32")
            cache_put(key, buf.tobytes())
            blob = buf.tobytes()
        arr = np.frombuffer(blob, dtype="float32").reshape(2, lat.size, lon.size)
        out[t] = {"t2m": arr[0], "wind_kmh": arr[1]}
    return {"lat": lat, "lon": lon, "fields": out}


# ---------------------------------------------------------------- GFS side

async def _gfs_ranges(client: httpx.AsyncClient, run_date: str, cycle: str,
                      step: int) -> dict[str, tuple[int, int | None]]:
    """Byte ranges for the surface variables we verify, from the GRIB2 `.idx`.

    The index lists every record's byte offset, so we can pull just 2 m
    temperature and 10 m wind out of a ~500 MB file.
    """
    key = f"gfs-idx|{run_date}{cycle}|{step}"
    blob = cache_get(key, ttl=90 * 24 * 3600)
    if blob is None:
        base = f"{ARCHIVE_BASE}/gfs.{run_date}/{cycle}/atmos/gfs.t{cycle}z.pgrb2.0p25.f{step:03d}"
        r = await client.get(base + ".idx")
        r.raise_for_status()
        blob = r.content
        cache_put(key, blob)
    rows = []
    for line in blob.decode().strip().split("\n"):
        p = line.split(":")
        if len(p) >= 5:
            try:
                rows.append((int(p[0]), int(p[1]), p[3], p[4]))
            except ValueError:
                continue
    wanted = {":TMP:2 m above ground:": "t2m",
              ":UGRD:10 m above ground:": "u10",
              ":VGRD:10 m above ground:": "v10"}
    picks: dict[str, tuple[int, int | None]] = {}
    for i, (_num, off, var, lev) in enumerate(rows):
        key_name = f":{var}:{lev}:"
        if key_name in wanted:
            nxt = rows[i + 1][1] if i + 1 < len(rows) else None
            picks[wanted[key_name]] = (off, nxt)
    return picks


async def gfs_archived_step(client: httpx.AsyncClient, lat: float, lon: float,
                            run_date: str, cycle: str, step: int) -> dict:
    """Archived GFS surface fields for one lead time, as box means."""
    key = f"gfs-arch|{run_date}{cycle}|{step}|{lat:.2f},{lon:.2f}|{BOX_DEG}"
    blob = cache_get(key, ttl=90 * 24 * 3600)
    if blob is None:
        import json as _json
        picks = await _gfs_ranges(client, run_date, cycle, step)
        if not picks:
            raise RuntimeError("no verifiable variables in GFS index")
        base = f"{ARCHIVE_BASE}/gfs.{run_date}/{cycle}/atmos/gfs.t{cycle}z.pgrb2.0p25.f{step:03d}"
        out_raw: dict[str, np.ndarray] = {}
        lat_axis = lon_axis = None
        for name in ("t2m", "u10", "v10"):
            if name not in picks:
                continue
            off, nxt = picks[name]
            headers = {"Range": f"bytes={off}-{nxt - 1}" if nxt else f"bytes={off}-"}
            r = await client.get(base, headers=headers)
            r.raise_for_status()
            field, lat_axis, lon_axis = _decode_grib_point(r.content, name)
            out_raw[name] = field
        t2m = box_mean(out_raw["t2m"] - 273.15, lat_axis, lon_axis, lat, lon)
        ws = float("nan")
        if "u10" in out_raw and "v10" in out_raw:
            u = box_mean(out_raw["u10"], lat_axis, lon_axis, lat, lon)
            v = box_mean(out_raw["v10"], lat_axis, lon_axis, lat, lon)
            if not (math.isnan(u) or math.isnan(v)):
                ws = math.hypot(u, v) * 3.6
        payload = {"t2m": t2m, "wind_kmh": ws}
        cache_put(key, _json.dumps(payload).encode())
        return payload
    import json as _json
    return _json.loads(blob)


def _decode_grib_point(blob: bytes, var: str):
    """Decode one GRIB2 message into (field, lat_axis, lon_axis).

    The byte-range slice is a valid single-message GRIB2 file, which cfgrib reads
    directly. The shortName differs by variable (2t/t2m, 10u/u10, ...), so the
    data variable is taken positionally rather than by name.
    """
    import xarray as xr
    path = os.path.join(cache_dir(), f"verify-{os.getpid()}-{hashlib.md5(blob[:512]).hexdigest()[:8]}.grib2")
    with open(path, "wb") as f:
        f.write(blob)
    try:
        ds = xr.open_dataset(path, engine="cfgrib",
                             backend_kwargs={"indexpath": ""})
        name = list(ds.data_vars)[0]
        field = np.asarray(ds[name].values, dtype=float)
        lat_axis = np.asarray(ds["latitude"].values, dtype=float)
        lon_axis = np.asarray(ds["longitude"].values, dtype=float)
        ds.close()
        return field, lat_axis, lon_axis
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ------------------------------------------------------------- assembling

def _compare(gfs: dict, era5: dict, lat: float, lon: float,
             ref_lat: np.ndarray, ref_lon: np.ndarray) -> dict:
    """One forecast-vs-truth row. `era5` is a single hour's {'t2m', 'wind_kmh'}."""
    e_t = box_mean(era5["t2m"], ref_lat, ref_lon, lat, lon)
    e_w = box_mean(era5["wind_kmh"], ref_lat, ref_lon, lat, lon)
    g_t = gfs.get("t2m")
    g_w = gfs.get("wind_kmh")
    rec = {"gfs_t2m_c": None if g_t is None or math.isnan(g_t) else round(g_t, 2),
           "era5_t2m_c": None if math.isnan(e_t) else round(e_t, 2),
           "gfs_wind_kmh": None if g_w is None or math.isnan(g_w) else round(g_w, 1),
           "era5_wind_kmh": None if math.isnan(e_w) else round(e_w, 1)}
    rec["temp_error_c"] = (round(g_t - e_t, 2)
                           if rec["gfs_t2m_c"] is not None and rec["era5_t2m_c"] is not None
                           else None)
    rec["wind_error_kmh"] = (round(g_w - e_w, 1)
                             if rec["gfs_wind_kmh"] is not None and rec["era5_wind_kmh"] is not None
                             else None)
    return rec


async def verify(lat: float, lon: float, days: int = DEFAULT_DAYS) -> dict:
    """Verify archived GFS against ERA5 at a point, grouped by lead time."""
    plan = verification_plan(days=days)
    if not plan:
        return {"ok": False, "error": "no verifiable window available yet"}

    valid_times = sorted({p["valid"] for p in plan})
    try:
        era5 = await asyncio.to_thread(era5_fields, valid_times)
    except Exception as e:
        return {"ok": False, "error": f"ERA5 unavailable: {type(e).__name__}: {str(e)[:120]}"}

    rows = []
    async with httpx.AsyncClient(headers=UA, timeout=90) as client:
        async def one(p):
            try:
                gfs = await gfs_archived_step(client, lat, lon, p["run_date"],
                                              p["cycle"], p["step"])
            except Exception as e:
                return {**p, "error": f"{type(e).__name__}: {str(e)[:80]}"}
            rec = _compare(gfs, era5["fields"][p["valid"]], lat, lon,
                           era5["lat"], era5["lon"])
            rec.update({"run_date": p["run_date"], "cycle": p["cycle"],
                        "step": p["step"], "lead_h": p["lead_h"],
                        "valid": p["valid"].strftime("%Y-%m-%dT%H:%MZ")})
            return rec
        rows = await asyncio.gather(*(one(p) for p in plan))

    ok_rows = [r for r in rows if "error" not in r and r.get("temp_error_c") is not None]
    by_lead = []
    for lead in LEADS_H:
        sel = [r for r in ok_rows if r["lead_h"] == lead]
        by_lead.append({"lead_h": lead, "temperature": scores([r["temp_error_c"] for r in sel]),
                        "wind": scores([r["wind_error_kmh"] for r in sel
                                        if r.get("wind_error_kmh") is not None])})

    all_t = [r["temp_error_c"] for r in ok_rows]
    return {
        "ok": bool(ok_rows),
        "point": {"lat": round(lat, 3), "lon": round(lon, 3)},
        "truth": "ERA5 reanalysis (Copernicus/ECMWF, CC BY 4.0), ARCO mirror",
        "forecast": "GFS archived cycles (NOAA/NWS, public domain), AWS Open Data",
        "box_deg": BOX_DEG,
        "window": {"first_valid": ok_rows[0]["valid"] if ok_rows else None,
                   "last_valid": ok_rows[-1]["valid"] if ok_rows else None,
                   "run_dates": sorted({r["run_date"] for r in rows})},
        "overall_temperature": scores(all_t),
        "overall_wind": scores([r["wind_error_kmh"] for r in ok_rows
                                if r.get("wind_error_kmh") is not None]),
        "by_lead": by_lead,
        "rows": sorted(ok_rows, key=lambda r: (r["run_date"], r["lead_h"])),
        "failed": [r for r in rows if "error" in r],
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
    }
