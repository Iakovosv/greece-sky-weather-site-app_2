"""GEFS ensemble mean/spread — a distribution-aware companion to model agreement.

Why this exists
---------------
The forecast page's agreement indicator is the spread between GFS, ICON-EU and
ECMWF: three deterministic runs. It is honest but thin. GEFS publishes a real
ensemble, and its precomputed **mean** and **spread** products (`geavg` /
`gespr`) let the page show how far the ensemble members sit from their mean at
the same point and time.

What these numbers actually are (verified, not assumed)
-------------------------------------------------------
From the NCEP GEFS product documentation and the GRIB metadata itself:

* `gespr` is the ensemble spread, defined by NCEP as the spread "similar to
  standard deviation" about the ensemble mean, in the same unit as the field.
* Both `geavg` and `gespr` carry `GRIB_dataType = "pf"` and
  `GRIB_totalNumber = 30`, and NCEP states they "are generated using only
  perturbed members" — i.e. the 30 perturbations, **not** the control member.
  So the ensemble size behind these fields is 30, and that is what is reported.

What this is not — and why there is no "high/moderate/low" label
----------------------------------------------------------------
This is *agreement between ensemble members*, never the probability that a
forecast verifies. A spread is small when members agree, including when they
agree on the same error.

It is also **not the same statistic** as the 3-model figure, which is a
max-minus-min range across three models. A standard-deviation-like spread and a
3-point range are not comparable, so reusing the 3-model thresholds (1.5/3.0 °C)
would be a fabricated comparison. This module therefore reports the number, its
unit and its member count, and classifies nothing. The filtered GRIB does not
expose whether the field is a variance, a standard deviation or a range in a way
that can be told apart here, so the page states it as a spread and stops.

Percentiles and per-member probabilities are deliberately absent: they cannot be
recovered from mean+spread, and presenting them would pass off a narrower
quantity as the full distribution.

Licence
-------
NOAA/NWS products are US public domain (17 USC §105) and may be used for any
lawful purpose; the NWS asks that the source not be implied as an endorsement and
that NWS material not be presented as official government material. Same
`LICENSES.md` entry as GFS. Attribution to NOAA is given in the UI.

Why the GEFS grib filter, and not 30 member files
-------------------------------------------------
30 members at 0.5° are on the order of 470 MB per step. `geavg`/`gespr` are single
fields, and the NOMADS grib filter subsets them server-side to the Greek box, so
a step costs a few KB. The spread is precomputed upstream, so this is also cheaper
in CPU than recomputing it — and identical in meaning.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os

import httpx
import numpy as np
import xarray as xr

import cachestore
import wx

log = logging.getLogger("wx.ensemble")

GEFS_FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gefs_atmos_0p50a.pl"
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

def _decode_point(blob: bytes, lat: float, lon: float, tag: str) -> float | None:
    """Pick the nearest cell from a filtered GRIB blob, or None if undecodable.

    The blob goes to a per-process temp file that is removed in `finally`, so a
    crash cannot leave a half-written file that a later read decodes as garbage.
    """
    path = os.path.join(cachestore.cache_dir(),
                        f"ensemble-{os.getpid()}-{tag}.grib2")
    try:
        with open(path, "wb") as f:
            f.write(blob)
        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
        try:
            v = list(ds.data_vars)[0]
            return float(ds[v].sel(latitude=lat, longitude=lon, method="nearest").values)
        finally:
            ds.close()
    except Exception as e:  # noqa: BLE001 - one bad field must not fail the request
        log.warning("GEFS ensemble %s decode failed: %s", tag, type(e).__name__)
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def _fetch_field(client: httpx.AsyncClient, kind: str, run: tuple[str, str],
                       step: int, lat: float, lon: float) -> float | None:
    """One mean or spread field for the Greek box, in Kelvin, or None.

    `kind` is "geavg" (mean) or "gespr" (spread). The cache key is
    provider/kind/run/step — never the coordinate: the response is the whole
    Greek box, so every PRO user asking about any Greek location on the same run
    shares one download. Concurrent callers for the same cold key are collapsed
    by `app._single_flight`, which wraps the whole ensemble request.
    """
    date, hh = run
    key = f"ensemble|gefs|{kind}|{date}{hh}|{step}|t2m"
    blob = cachestore.get(key, ttl=ENSEMBLE_TTL_S)
    if blob is None:
        west, east, south, north = _GEFS_BOX
        params = [
            ("dir", f"/gefs.{date}/{hh}/atmos/pgrb2ap5"),
            ("file", f"{kind}.t{hh}z.pgrb2a.0p50.f{step:03d}"),
            ("var_TMP", "on"), ("lev_2_m_above_ground", "on"),
            ("subregion", "on"),
            ("leftlon", str(west)), ("rightlon", str(east)),
            ("toplat", str(north)), ("bottomlat", str(south)),
        ]
        try:
            r = await client.get(GEFS_FILTER, params=params)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001 - mean and spread degrade independently
            log.warning("GEFS ensemble %s fetch failed (run=%s%s step=%s): %s",
                        kind, date, hh, step, type(e).__name__)
            # During a run rollout the newest step may not be published yet, so a
            # stale real field beats an empty card. Only on a failed fetch.
            stale = cachestore.get_stale(key, max_age=ENSEMBLE_MAX_STALE_S)
            if stale is None:
                return None
            log.info("GEFS ensemble %s serving stale cache (run=%s%s step=%s)",
                     kind, date, hh, step)
            return _decode_point(stale, lat, lon, f"{kind}-{step}")
        cachestore.put(key, r.content)
        blob = r.content
    return _decode_point(blob, lat, lon, f"{kind}-{step}")


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
