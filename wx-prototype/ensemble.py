"""GEFS ensemble mean/spread — a distribution-aware companion to model agreement.

Why this exists
---------------
The forecast page's agreement indicator is the spread between GFS, ICON-EU and
ECMWF: three deterministic runs. It is honest but thin. GEFS publishes a real
ensemble, and its precomputed **mean** and **spread** products (`geavg` /
`gespr`) let the page show how far the ensemble members sit from their mean.

What these numbers actually are (verified against the live products, not assumed)
------------------------------------------------------------------------------
From the NCEP GEFS `.idx` labels and the GRIB metadata itself:

* Every `gespr` field is labelled by NCEP **"ens std dev"** — the ensemble
  standard deviation about the ensemble mean, in the unit of the field. It is a
  standard deviation, and the page says so.
* Both `geavg` and `gespr` carry `GRIB_dataType = "pf"` and
  `GRIB_totalNumber = 30`, and NCEP states they are "generated using only
  perturbed members" — so the ensemble size behind these fields is 30.

Which variables are genuinely available, and at what step
---------------------------------------------------------
Confirmed by probing real cycles (0.5° `pgrb2ap5`, plus 0.25° `pgrb2sp25` where
noted). Only fields that actually answered are used:

| Variable | Product | Level | Step type |
|---|---|---|---|
| 2 m temperature | `pgrb2ap5` | 2 m above ground | instantaneous |
| 10 m wind (U, V) | `pgrb2ap5` | 10 m above ground | instantaneous |
| total precipitation | `pgrb2ap5` | surface | **6-hour accumulation** |
| total cloud cover | `pgrb2ap5` | entire atmosphere | **6-hour average** |
| **wind gust** | **`pgrb2sp25`** (0.25°) | surface | instantaneous |

Two consequences are load-bearing and are surfaced in the UI rather than hidden:

* GEFS **has no gust field in the 0.5° product**, and no gust *spread* built from
  a wind-speed field either. Gusts come only from the 0.25° `geavg`/`gespr`, so
  they cost a separate, small, region-filtered request.
* Precipitation spread is a spread of a **6-hour accumulation**, not of a 24-hour
  total. The panel reports the 6-hour figure and never adds steps together.
  Wind spread is the magnitude of the **vector** spread `hypot(spread_u,
  spread_v)`, which is not the same statistic as a spread of wind speed; the UI
  says "vector" so the two are not confused.

What this is not — and why there is no "high/moderate/low" label
----------------------------------------------------------------
This is *agreement between ensemble members*, never the probability that a
forecast verifies. A spread is small when members agree, including when they
agree on the same error.

It is also **not the same statistic** as the 3-model figure, which is a
max-minus-min range across three models. A standard-deviation-like spread and a
3-point range are not comparable, so reusing the 3-model thresholds (1.5/3.0 °C)
would be a fabricated comparison. This module reports numbers, units and a
member count, and classifies nothing.

Percentiles and per-member probabilities are deliberately absent: they cannot be
recovered from mean+spread, and presenting them would pass off a narrower
quantity as the full distribution.

Licence
-------
NOAA/NWS products are US public domain (17 USC §105) and may be used for any
lawful purpose; the NWS asks that the source not be implied as an endorsement and
that NWS material not be presented as official government material. The 0.25°
gust product is the same NWS GEFS, so the same `LICENSES.md` entry covers both.
Attribution to NOAA is given in the UI.

Why the GEFS grib filter, and not member files
----------------------------------------------
30 members at 0.5° are hundreds of MB per step. `geavg`/`gespr` are single
fields, and the NOMADS grib filter subsets them server-side to the Greek box, so
a step costs a few KB. The spread is precomputed upstream, so this is also
cheaper in CPU than recomputing it — and identical in meaning.
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

import cachestore
import wx

log = logging.getLogger("wx.ensemble")

GEFS_FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gefs_atmos_0p50a.pl"
# Gusts exist only in the 0.25° product, which has its own filter endpoint.
GEFS_GUST_FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gefs_atmos_0p25s.pl"
GEFS_PDS = "https://noaa-gefs-pds.s3.amazonaws.com"

# The mean/spread fields are built from the 30 perturbed members (GRIB
# GRIB_totalNumber=30; NCEP: "generated using only perturbed members"), so this is
# 30, not the 31 the full system runs. Stated once so the wording and the UI
# cannot drift from the data.
GEFS_PERTURBED_MEMBERS = 30

# Published files are immutable once a run is out, so the TTL only bounds how
# long a copy lingers. Six hours matches the other models and is no longer than
# the run cadence.
ENSEMBLE_TTL_S = 6 * 3600

# How stale a cached field may be before it is preferred over an empty card
# during a run rollout. Only consulted when a fresh fetch has already failed.
ENSEMBLE_MAX_STALE_S = 24 * 3600

# The GEFS filter wants a box. A box covering all of Greece, not a point-sized
# square, so one cached download serves every Greek location on that run.
# Wider than GREEK_BBOX on purpose, so a coastal or island point still has all
# four interpolation neighbours inside the request.
_GEFS_BOX = (18.0, 30.0, 34.0, 42.0)   # west, east, south, north

# GEFS 0.5° is published every 3 hours. Every lead the panel reports is also a
# multiple of 24, which keeps the 6-hour precipitation accumulation window
# identical across leads, so two leads are never silently added together, and
# every lead is <= ent.PRO_HOURS.
ENSEMBLE_LEADS = (24, 72, 120, 168, 240)

_PRODUCT = "pgrb2ap5"
_GUST_PRODUCT = "pgrb2sp25"


def describe(spread_c: float | None, *, members: int = GEFS_PERTURBED_MEMBERS,
             mean_c: float | None = None, hours: int | None = None) -> dict:
    """The ensemble block as the UI should show it: a number and its meaning.

    No class, no grade, no percentage. There is deliberately nothing here that
    reads as "how likely the forecast is right" — including no comparison against
    the 3-model range, which is a different statistic.
    """
    if spread_c is None or not np.isfinite(spread_c):
        return {"available": False, "text": "άγνωστη", "detail": ""}
    spread_c = float(spread_c)
    window = f" στο βήμα +{hours} h" if hours else ""
    detail = (f"Η διασπορά των {members} διαταραγμένων μελών του GEFS{window} είναι "
              f"{spread_c:.1f}°C γύρω από τον μέσο όρο τους. Είναι ορισμού παρόμοια "
              f"με τυπική απόκλιση, οπότε δεν συγκρίνεται άμεσα με την απόκλιση των "
              f"τριών μοντέλων (μέγιστη μείον ελάχιστη). Είναι ένδειξη συμφωνίας "
              f"των μελών, όχι βεβαιότητα ότι η πρόγνωση θα επαληθευτεί.")
    out = {"available": True, "members": int(members),
           "spread_c": round(spread_c, 2), "hours": hours,
           "text": f"±{spread_c:.1f}°C", "detail": detail,
           "source": "NOAA GEFS (geavg/gespr)"}
    if mean_c is not None:
        out["mean_c"] = round(float(mean_c), 1)
    return out


# ---------------------------------------------------------------- run discovery

def gefs_latest_run(client: httpx.Client | None = None,
                    now: dt.datetime | None = None) -> tuple[str, str]:
    """The newest published GEFS (date, hour).

    Mirrors `wx._probe_latest_gfs_run`: step back from the expected cycle and
    probe the `geavg` object directly. The NODD bucket *listing* looks like the
    obvious route, but with `delimiter=/` it returns only the date level
    (`gefs.YYYYMMDD/`), not the cycle hour — so the run is settled by asking
    whether the actual object exists. `now` is an explicit override for tests;
    it bypasses the memo, because a caller that names a time wants that time.
    """
    if now is None:
        key = "ensemble|gefs|run-probe"
        blob = cachestore.get(key, ttl=wx.RUN_LOOKUP_TTL_S)
        if blob is not None:
            date, hh = blob.decode().split("|")
            return date, hh
        resolved = _probe_latest_gefs_run(dt.datetime.now(dt.timezone.utc),
                                          client=client)
        cachestore.put(key, f"{resolved[0]}|{resolved[1]}".encode())
        return resolved
    return _probe_latest_gefs_run(now, client=client)


def _probe_latest_gefs_run(now: dt.datetime,
                           client: httpx.Client | None = None) -> tuple[str, str]:
    """Newest cycle whose `geavg` object answers, the same way GFS is probed.

    GEFS runs lag their nominal cycle by a few hours, so a candidate is not
    assumed present just because its time has passed. Bounded to eight steps
    (two days of cycles) so a NOMADS outage fails fast instead of spinning.
    """
    cycle = (now - dt.timedelta(hours=4, minutes=30)).replace(minute=0, second=0, microsecond=0)
    cycle = cycle.replace(hour=(cycle.hour // 6) * 6)
    owns_client = client is None
    c = client or httpx.Client(headers=wx.UA, timeout=20)
    try:
        for _ in range(8):
            date, hh = cycle.strftime("%Y%m%d"), f"{cycle.hour:02d}"
            url = (f"{GEFS_PDS}/gefs.{date}/{hh}/atmos/pgrb2ap5/"
                   f"geavg.t{hh}z.pgrb2a.0p50.f024")
            try:
                if c.head(url).status_code == 200:
                    return date, hh
            except httpx.HTTPError:
                pass
            cycle -= dt.timedelta(hours=6)
    finally:
        if owns_client:
            c.close()
    raise RuntimeError("no GEFS cycle reachable")


def _run_or_default() -> tuple[str, str]:
    try:
        return gefs_latest_run()
    except Exception as e:  # noqa: BLE001 - a probe failure must not break a forecast
        log.warning("GEFS run probe failed (%s); using the 00Z cycle", type(e).__name__)
        date = (dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(hours=6)).strftime("%Y%m%d")
        return date, "00"


# ---------------------------------------------------------------- decode

class _GribFile:
    """A uniquely-named GRIB temp file in the cache dir, removed on exit.

    `tempfile.mkstemp` guarantees a unique name, so two coroutines decoding in
    parallel cannot open each other's bytes even if they share a tag. The file
    lives in the cache dir, matching `wx.py`, so a crash cannot scatter GRIB
    somewhere the size cap does not see.
    """

    def __init__(self, blob: bytes):
        self.blob = blob
        self.path = ""

    def __enter__(self) -> str:
        fd, self.path = tempfile.mkstemp(dir=cachestore.cache_dir(), suffix=".grib2")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(self.blob)
        except BaseException:
            self._unlink()
            raise
        return self.path

    def __exit__(self, *exc) -> None:
        self._unlink()

    def _unlink(self) -> None:
        try:
            os.unlink(self.path)
        except OSError:
            pass


def _decode_point(blob: bytes, lat: float, lon: float, tag: str) -> float | None:
    """Pick the nearest cell from a filtered GRIB blob, or None if undecodable."""
    try:
        with _GribFile(blob) as path:
            ds = xr.open_dataset(path, engine="cfgrib",
                                 backend_kwargs={"indexpath": ""})
            try:
                v = list(ds.data_vars)[0]
                return float(ds[v].sel(latitude=lat, longitude=lon,
                                       method="nearest").values)
            finally:
                ds.close()
    except Exception as e:  # noqa: BLE001 - one bad field must not fail the request
        log.warning("GEFS ensemble %s decode failed: %s", tag, type(e).__name__)
        return None


def _nearest(path: str, lat: float, lon: float, filt: dict) -> dict[str, float]:
    """One filtered open of a mixed-level GRIB file, nearest cell per variable.

    A single request can carry several variables at different levels (2 m
    temperature, 10 m wind, surface precipitation, atmospheric cloud), but cfgrib
    refuses to build one Dataset from two different `level` values under the same
    `typeOfLevel`. Opening the file once per level with `filter_by_keys` is the
    same technique `wx.gfs_surface_step` uses, and it keeps the download to one.
    """
    ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={
        "indexpath": "", "filter_by_keys": filt})
    try:
        out: dict[str, float] = {}
        for v in ds.data_vars:
            out[str(v)] = float(ds[v].sel(latitude=lat, longitude=lon,
                                          method="nearest").values)
        return out
    finally:
        ds.close()


def _decode_mixed(blob: bytes, lat: float, lon: float, tag: str) -> dict[str, float]:
    """Decode the four 0.5° ensemble fields from one cached blob.

    Missing levels are simply absent from the result: a file that lost one field
    degrades that field only, exactly like the single-field path.
    """
    out: dict[str, float] = {}
    try:
        with _GribFile(blob) as path:
            for filt in ({"typeOfLevel": "heightAboveGround", "level": 2},
                         {"typeOfLevel": "heightAboveGround", "level": 10},
                         {"typeOfLevel": "surface"},
                         {"typeOfLevel": "atmosphere"}):
                try:
                    out.update(_nearest(path, lat, lon, filt))
                except Exception as e:  # noqa: BLE001 - one level must not fail the rest
                    log.warning("GEFS ensemble %s level %s decode failed: %s",
                                tag, filt.get("level", filt.get("typeOfLevel")),
                                type(e).__name__)
    except Exception as e:  # noqa: BLE001
        log.warning("GEFS ensemble %s decode failed: %s", tag, type(e).__name__)
    return out


def _decode_gust(blob: bytes, lat: float, lon: float, tag: str) -> float | None:
    return _decode_point(blob, lat, lon, tag)


# ---------------------------------------------------------------- fetching

def _box_params(west: float, east: float, south: float, north: float) -> list[tuple]:
    return [("subregion", "on"), ("leftlon", str(west)), ("rightlon", str(east)),
            ("toplat", str(north)), ("bottomlat", str(south))]


async def _cached_get(client: httpx.AsyncClient, key: str, url: str,
                      params: list[tuple], label: str) -> bytes | None:
    """Cached GET with the module's one stale-read rule.

    A fresh hit returns immediately. On a failed fetch the last real field within
    `ENSEMBLE_MAX_STALE_S` is preferred to an empty card during a run rollout, and
    is only then consulted.
    """
    blob = cachestore.get(key, ttl=ENSEMBLE_TTL_S)
    if blob is not None:
        return blob
    try:
        r = await client.get(url, params=params)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001 - each field degrades independently
        log.warning("GEFS ensemble %s fetch failed: %s", label, type(e).__name__)
        stale = cachestore.get_stale(key, max_age=ENSEMBLE_MAX_STALE_S)
        if stale is not None:
            log.info("GEFS ensemble %s serving stale cache", label)
        return stale
    cachestore.put(key, r.content)
    return r.content


async def _fetch_field(client: httpx.AsyncClient, kind: str, run: tuple[str, str],
                       step: int, lat: float, lon: float) -> float | None:
    """One mean or spread temperature field for the Greek box, in Kelvin, or None.

    `kind` is "geavg" (mean) or "gespr" (spread). The cache key is
    provider/kind/run/step — never the coordinate: the response is the whole
    Greek box, so every PRO user asking about any Greek location on the same run
    shares one download. Concurrent callers for the same cold key are collapsed
    by `app._single_flight`, which wraps the whole ensemble request.
    """
    date, hh = run
    key = f"ensemble|gefs|{kind}|{date}{hh}|{step}|t2m"
    west, east, south, north = _GEFS_BOX
    params = [("dir", f"/gefs.{date}/{hh}/atmos/{_PRODUCT}"),
              ("file", f"{kind}.t{hh}z.pgrb2a.0p50.f{step:03d}"),
              ("var_TMP", "on"), ("lev_2_m_above_ground", "on")]
    params += _box_params(west, east, south, north)
    blob = await _cached_get(client, key, GEFS_FILTER, params,
                             f"{kind} t2m run={date}{hh} step={step}")
    if blob is None:
        return None
    return _decode_point(blob, lat, lon, f"{kind}-{step}")


async def _fetch_mixed(client: httpx.AsyncClient, kind: str, run: tuple[str, str],
                       step: int, lat: float, lon: float) -> dict[str, float]:
    """The 0.5° ensemble fields (temperature, wind, precipitation, cloud).

    One request carries all four variables and their levels; the decode splits
    them. Cache key is provider/kind/run/step, never the point.
    """
    date, hh = run
    key = f"ensemble|gefs|{kind}|{date}{hh}|{step}|mix"
    west, east, south, north = _GEFS_BOX
    params = [("dir", f"/gefs.{date}/{hh}/atmos/{_PRODUCT}"),
              ("file", f"{kind}.t{hh}z.pgrb2a.0p50.f{step:03d}"),
              ("var_TMP", "on"), ("lev_2_m_above_ground", "on"),
              ("var_UGRD", "on"), ("var_VGRD", "on"), ("lev_10_m_above_ground", "on"),
              ("var_APCP", "on"), ("lev_surface", "on"),
              ("var_TCDC", "on"), ("lev_entire_atmosphere", "on")]
    params += _box_params(west, east, south, north)
    blob = await _cached_get(client, key, GEFS_FILTER, params,
                             f"{kind} mix run={date}{hh} step={step}")
    if blob is None:
        return {}
    return _decode_mixed(blob, lat, lon, f"{kind}-{step}")


async def _fetch_gust(client: httpx.AsyncClient, kind: str, run: tuple[str, str],
                      step: int, lat: float, lon: float) -> float | None:
    """The 0.25° surface gust field, mean or spread, in m/s, or None.

    Gusts are absent from the 0.5° ensemble product entirely, so this is a
    separate request against the 0.25° filter. `pgrb2sp25` is regional, so the
    subset is about 1.6 kB per field and the same box/step sharing rule applies.
    """
    date, hh = run
    key = f"ensemble|gefs|{kind}|{date}{hh}|{step}|gust"
    west, east, south, north = _GEFS_BOX
    params = [("dir", f"/gefs.{date}/{hh}/atmos/{_GUST_PRODUCT}"),
              ("file", f"{kind}.t{hh}z.pgrb2s.0p25.f{step:03d}"),
              ("var_GUST", "on"), ("lev_surface", "on")]
    params += _box_params(west, east, south, north)
    blob = await _cached_get(client, key, GEFS_GUST_FILTER, params,
                             f"{kind} gust run={date}{hh} step={step}")
    if blob is None:
        return None
    return _decode_gust(blob, lat, lon, f"{kind}-gust-{step}")


# ---------------------------------------------------------------- public API

async def gefs_ensemble_point(client: httpx.AsyncClient, lat: float, lon: float,
                              step: int = 24,
                              run: tuple[str, str] | None = None) -> dict:
    """Mean and spread of the GEFS temperature ensemble at one point and step.

    Returns `t2m_mean_c` / `t2m_spread_c` in Celsius, either of which may be None
    when that half of the pair is unavailable. An empty dict means neither was
    retrievable — the caller should fall back to the 3-model spread rather than
    show a fabricated number.
    """
    wx.check_point(lat, lon)
    run = run or _run_or_default()
    mean, spread = await asyncio.gather(
        _fetch_field(client, "geavg", run, step, lat, lon),
        _fetch_field(client, "gespr", run, step, lat, lon),
    )
    out: dict = {"run": f"{run[0]}{run[1]}", "step": step,
                 "members": GEFS_PERTURBED_MEMBERS,
                 "t2m_mean_c": None, "t2m_spread_c": None}
    if mean is not None:
        out["t2m_mean_c"] = round(mean - 273.15, 2)
    if spread is not None:
        out["t2m_spread_c"] = round(spread, 2)
    # Neither half retrievable: the caller should fall back to the 3-model
    # spread rather than show a fabricated number, so say so explicitly.
    if out["t2m_mean_c"] is None and out["t2m_spread_c"] is None:
        return {}
    return out


def _wind(u: float | None, v: float | None) -> tuple[float | None, float | None]:
    """Speed (km/h) and meteorological direction (degrees) from U/V in m/s.

    Direction uses the same convention as `wx.gfs_surface_step` — the direction
    the wind blows *from*, zero at north.
    """
    if u is None or v is None:
        return None, None
    speed = float(np.hypot(u, v)) * 3.6
    direction = float((np.degrees(np.arctan2(-u, -v)) + 360) % 360)
    return round(speed, 1), round(direction, 0)


def _vector_spread(su: float | None, sv: float | None) -> float | None:
    """Magnitude of the vector spread `hypot(spread_u, spread_v)` in km/h.

    This is *not* a spread of wind speed: it is the length of the spread vector,
    so a wind that changes direction strongly can have a large vector spread with
    a small speed change. The UI labels it "vector" for that reason.
    """
    if su is None or sv is None:
        return None
    return round(float(np.hypot(su, sv)) * 3.6, 1)


def _finite(*vals) -> bool:
    return all(v is not None and np.isfinite(v) for v in vals)


def describe_series(series: dict | None, headline_hours: int = 24) -> dict:
    """The Ensemble panel: several variables, each with its own mean and spread.

    Built from real `geavg`/`gespr` fields only. Every figure carries the unit and
    member count; nothing is graded, and no probability, percentile or
    "confidence" is derived, because mean+spread cannot support one.
    """
    series = series or {}
    leads = series.get("leads") or []
    rows = []
    for lead in leads:
        if not isinstance(lead, dict):
            continue
        rows.append({
            "hours": lead.get("hours"),
            "t2m_c": lead.get("t2m_mean_c"),
            "t2m_spread_c": lead.get("t2m_spread_c"),
            "wind_kmh": lead.get("wind_kmh"),
            "wind_dir": lead.get("wind_dir"),
            "wind_spread_kmh": lead.get("wind_spread_kmh"),
            "gust_kmh": lead.get("gust_kmh"),
            "gust_spread_kmh": lead.get("gust_spread_kmh"),
            "precip_mm": lead.get("precip_mm"),
            "precip_spread_mm": lead.get("precip_spread_mm"),
            "cloud_pct": lead.get("cloud_pct"),
            "cloud_spread_pct": lead.get("cloud_spread_pct"),
        })
    if not rows:
        return {"available": False, "text": "μη διαθέσιμο",
                "detail": "Το σύνολο GEFS δεν ήταν διαθέσιμο για αυτό το σημείο."}

    members = int(series.get("members", GEFS_PERTURBED_MEMBERS))
    detail = (f"Μέσος όρος και διασπορά (τυπική απόκλιση, «ens std dev» κατά NCEP) των "
              f"{members} διαταραγμένων μελών του GEFS, ανά βήμα πρόγνωσης. Η θερμοκρασία, "
              f"ο άνεμος και οι ριπές είναι στιγμιαία πεδία· ο υετός είναι "
              f"συσσώρευση 6 ωρών και η νέφωση μέσος όρος 6 ωρών, όπως τα δημοσιεύει "
              f"το GEFS. Η διασπορά του ανέμου είναι το μέτρο του διανυσματικού "
              f"σφάλματος, όχι διασπορά ταχύτητας. Είναι ένδειξη συμφωνίας των μελών "
              f"και όχι βεβαιότητα ότι η πρόγνωση θα επαληθευτεί· επίσης διαφέρει "
              f"από τη σύγκλιση των τριών ντετερμινιστικών μοντέλων.")
    out = {"available": True, "members": members,
           "run": series.get("run"), "leads": rows, "detail": detail,
           "source": "NOAA GEFS geavg/gespr"}
    # A one-line headline for the section summary, taken from the requested lead
    # when it is present. This is the same number as the matching row, not a new
    # statistic and not a summary across leads.
    head = next((r for r in rows if r["hours"] == headline_hours), None) or rows[0]
    if head.get("t2m_spread_c") is not None:
        out["headline"] = {"hours": head["hours"],
                           "text": f"±{head['t2m_spread_c']:.1f}°C",
                           "t2m_c": head.get("t2m_c")}
    return out


async def gefs_ensemble_series(client: httpx.AsyncClient, lat: float, lon: float,
                               leads: tuple[int, ...] = ENSEMBLE_LEADS,
                               run: tuple[str, str] | None = None) -> dict:
    """Mean/spread for temperature, wind, gusts, precipitation and cloud.

    One GEFS step is fetched as: a single 0.5° request carrying temperature, wind,
    precipitation and cloud, plus a single 0.25° request carrying gusts. Both are
    cached per run/step, so every PRO user asking about any Greek location on the
    same run shares the downloads. Leads are fetched in sequence and the two
    requests within a lead run concurrently, which keeps the burst to two
    connections instead of a wide fan-out against NOMADS.

    Returns {"run", "members", "leads": [...]}. A half that is unavailable is
    absent rather than invented; an empty `leads` list means nothing was
    retrievable.
    """
    wx.check_point(lat, lon)
    run = run or _run_or_default()
    out = {"run": f"{run[0]}{run[1]}", "members": GEFS_PERTURBED_MEMBERS, "leads": []}
    for lead in leads:
        m_mix, s_mix, m_gust, s_gust = await asyncio.gather(
            _fetch_mixed(client, "geavg", run, lead, lat, lon),
            _fetch_mixed(client, "gespr", run, lead, lat, lon),
            _fetch_gust(client, "geavg", run, lead, lat, lon),
            _fetch_gust(client, "gespr", run, lead, lat, lon),
        )
        row: dict = {"hours": lead}

        if _finite(m_mix.get("t2m"), s_mix.get("t2m")):
            row["t2m_mean_c"] = round(m_mix["t2m"] - 273.15, 1)
            row["t2m_spread_c"] = round(s_mix["t2m"], 2)
        elif _finite(m_mix.get("t2m")):
            row["t2m_mean_c"] = round(m_mix["t2m"] - 273.15, 1)

        speed, direction = _wind(m_mix.get("u10"), m_mix.get("v10"))
        vspread = _vector_spread(s_mix.get("u10"), s_mix.get("v10"))
        if speed is not None:
            row["wind_kmh"] = speed
            row["wind_dir"] = direction
        if vspread is not None:
            row["wind_spread_kmh"] = vspread

        if _finite(m_gust):
            row["gust_kmh"] = round(m_gust * 3.6, 1)
        if _finite(s_gust):
            row["gust_spread_kmh"] = round(s_gust * 3.6, 1)

        # tp is a 6-hour accumulation in kg/m^2 (== mm); the spread is a spread of
        # that same 6-hour accumulation and is never summed across leads.
        if _finite(m_mix.get("tp")):
            row["precip_mm"] = round(m_mix["tp"], 1)
        if _finite(s_mix.get("tp")):
            row["precip_spread_mm"] = round(s_mix["tp"], 1)

        # tcc is a 6-hour average in percent.
        if _finite(m_mix.get("tcc")):
            row["cloud_pct"] = round(m_mix["tcc"], 0)
        if _finite(s_mix.get("tcc")):
            row["cloud_spread_pct"] = round(s_mix["tcc"], 0)

        if len(row) > 1:      # only "hours" means nothing at all came back
            out["leads"].append(row)
    return out
