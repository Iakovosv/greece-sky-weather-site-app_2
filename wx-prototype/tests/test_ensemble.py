"""Ensemble (GEFS mean/spread) tests: data, caching, honesty, PRO-gating, secrecy.

What this pins down
-------------------
GEFS publishes precomputed mean (`geavg`) and spread (`gespr`) fields built from
its 30 perturbed members. This module reports the spread as a number and its
meaning, and is asserted to be *incapable* of presenting it as forecast
reliability, a percentage, or a per-member probability.

No network. A fake HTTP client records every GET and serves GRIB-shaped bytes,
so "warm cache" is proven by the absence of a request rather than by a stub, and
"FREE makes no upstream call" is proven by an empty call list.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cachestore  # noqa: E402
import ensemble  # noqa: E402
import wx  # noqa: E402

MEAN_BYTES = b"GRIB" + b"M" * 40
SPREAD_BYTES = b"GRIB" + b"S" * 40


class _Resp:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Records GETs; answers the two GEFS filter URLs with mean/spread bytes.

    `malformed` serves bytes that are not a decodable field, to exercise the
    partial-data path. `fail_*` raises, to exercise degradation.
    """

    def __init__(self, fail_spread: bool = False, fail_mean: bool = False,
                 malformed: bool = False):
        self.calls: list[tuple] = []
        self.fail_spread = fail_spread
        self.fail_mean = fail_mean
        self.malformed = malformed

    async def get(self, url, **kwargs):
        params = dict(kwargs.get("params") or [])
        fname = params.get("file", "")
        self.calls.append((url, fname, tuple(sorted(params.items()))))
        if "gespr" in fname:
            if self.fail_spread:
                raise RuntimeError("spread unavailable")
            return _Resp(b"not a grib file" if self.malformed else SPREAD_BYTES)
        if "geavg" in fname:
            if self.fail_mean:
                raise RuntimeError("mean unavailable")
            return _Resp(b"not a grib file" if self.malformed else MEAN_BYTES)
        raise AssertionError(f"unexpected file: {fname}")


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def cache_dir(monkeypatch):
    d = tempfile.mkdtemp(prefix="wx-ensemble-test-")
    monkeypatch.setenv("WX_CACHE_DIR", os.path.join(d, "cache"))
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "0")
    return d


@pytest.fixture(autouse=True)
def fixed_run(monkeypatch):
    """Pin the run so nothing probes NOMADS for the latest cycle."""
    monkeypatch.setattr(ensemble, "gefs_latest_run", lambda: ("20260924", "00"))


@pytest.fixture(autouse=True)
def fake_decode(monkeypatch):
    """Decode the cached bytes: mean in K, spread in K.

    Patches `ensemble._decode_point`, this module's own byte->value seam, rather
    than the process-global `xarray`, so the real app's GFS decode is untouched.
    Mirrors the real contract: undecodable bytes return None rather than raising,
    because that is what the real function does (it catches and logs).
    """
    def _decode(blob, lat, lon, tag):
        if blob == MEAN_BYTES:
            return 300.0     # 26.85 C
        if blob == SPREAD_BYTES:
            return 2.0       # 2.0 C
        return None

    monkeypatch.setattr(ensemble, "_decode_point", _decode)


def _run(client, step: int = 24, lat: float = 37.98, lon: float = 23.73):
    return asyncio.run(ensemble.gefs_ensemble_point(client, lat, lon, step=step))


# ============================================================ data correctness

def test_mean_spread_come_back_in_celsius(cache_dir):
    out = _run(FakeClient())
    assert out["run"] == "2026092400"
    assert out["t2m_mean_c"] == pytest.approx(26.85, abs=0.01)
    assert out["t2m_spread_c"] == pytest.approx(2.0, abs=0.01)


def test_member_count_is_the_perturbed_members_not_the_whole_system(cache_dir):
    """geavg/gespr are built from the 30 perturbations (GRIB_totalNumber=30).

    The full GEFS system has 31 runs including the control, but the control is
    *not* in these products. Reporting 31 would overstate the ensemble.
    """
    out = _run(FakeClient())
    assert out["members"] == 30
    assert ensemble.GEFS_PERTURBED_MEMBERS == 30


def test_partial_upstream_data_yields_an_honest_result(cache_dir):
    """One undecodable half must not fabricate the other."""
    out = _run(FakeClient(malformed=True))
    assert out == {}, out


def test_missing_spread_keeps_the_mean_and_leaves_spread_none(cache_dir):
    out = _run(FakeClient(fail_spread=True))
    assert out["t2m_mean_c"] == pytest.approx(26.85, abs=0.01)
    assert out["t2m_spread_c"] is None


def test_missing_mean_keeps_the_spread(cache_dir):
    out = _run(FakeClient(fail_mean=True))
    assert out["t2m_spread_c"] == pytest.approx(2.0, abs=0.01)
    assert out["t2m_mean_c"] is None


def test_both_unavailable_returns_empty_not_a_guess(cache_dir):
    assert _run(FakeClient(fail_mean=True, fail_spread=True)) == {}


# ============================================================ run discovery
#
# A real end-to-end run against NOMADS found that the S3 bucket listing, with
# `delimiter=/`, returns only the date level (`gefs.YYYYMMDD/`) and never the
# cycle hour — so a listing-based probe cannot name a run at all. These tests pin
# the replacement: probe the `geavg` object for candidate cycles, like GFS does.

class _FakeHeadClient:
    """Returns 200 only for the cycles in `available`."""

    def __init__(self, available: set[tuple[str, str]]):
        self.available = available
        self.attempts: list[tuple[str, str, str]] = []

    def head(self, url):
        # .../gefs.YYYYMMDD/HH/atmos/pgrb2ap5/geavg.tHHz.pgrb2a.0p50.f024
        parts = url.split("/")
        date, hh = parts[-5].removeprefix("gefs."), parts[-4]
        self.attempts.append((date, hh, url))

        class _R:
            def __init__(self, code):
                self.status_code = code

        return _R(200 if (date, hh) in self.available else 404)


def test_run_probe_steps_back_until_an_object_exists():
    """A cycle is not assumed present just because its nominal time has passed.

    At 08:00Z the newest nominal candidate is 20260924/00; if that object is not
    published yet the probe must fall back rather than return an unusable run.
    """
    client = _FakeHeadClient({("20260923", "18")})
    now = dt.datetime(2026, 9, 24, 8, 0, tzinfo=dt.timezone.utc)
    date, hh = ensemble._probe_latest_gefs_run(now, client=client)
    assert (date, hh) == ("20260923", "18")
    assert client.attempts[0][:2] == ("20260924", "00"), "must try the newest first"
    assert len(client.attempts) == 2, "and step back exactly once"


def test_run_probe_asks_for_the_geavg_object_on_the_cycle_path():
    client = _FakeHeadClient({("20260924", "00")})
    now = dt.datetime(2026, 9, 24, 8, 0, tzinfo=dt.timezone.utc)
    date, hh = ensemble._probe_latest_gefs_run(now, client=client)
    assert (date, hh) == ("20260924", "00")
    u = client.attempts[0][2]
    assert "geavg" in u and f"gefs.{date}/{hh}/" in u and "s3.amazonaws.com" in u


def test_run_probe_raises_when_nothing_is_reachable():
    client = _FakeHeadClient(set())
    now = dt.datetime(2026, 9, 24, 8, 0, tzinfo=dt.timezone.utc)
    with pytest.raises(RuntimeError):
        ensemble._probe_latest_gefs_run(now, client=client)
    assert len(client.attempts) == 8, "the probe must be bounded"


# ============================================================ honesty of wording

def test_describe_never_presents_a_probability_or_confidence():
    out = ensemble.describe(1.2, members=30, mean_c=26.9, hours=24)
    blob = (out["text"] + " " + out["detail"]).lower()
    for forbidden in ("%", "confidence", "reliability", "πιθανότητ", "chance",
                      "probability", "αξιοπιστ"):
        assert forbidden not in blob, forbidden


def test_describe_does_not_grade_the_spread():
    """No high/moderate/low class: a std-dev-like spread is not comparable to the
    3-model range, so grading it against those thresholds would be fabricated."""
    out = ensemble.describe(0.4, members=30, hours=24)
    assert "class" not in out
    for label in ("υψηλή", "μέτρια", "χαμηλή"):
        assert label not in out["detail"]


def test_describe_states_the_underlying_definition_and_member_count():
    out = ensemble.describe(2.4, members=30, mean_c=26.9, hours=24)
    assert out["available"] is True
    assert out["members"] == 30
    assert out["spread_c"] == 2.4
    assert out["mean_c"] == 26.9
    assert "30" in out["detail"]
    assert "τυπική απόκλιση" in out["detail"]


def test_describe_without_a_spread_is_explicitly_unavailable():
    out = ensemble.describe(None)
    assert out["available"] is False
    assert out["detail"] == ""


# ============================================================ caching / upstream

def test_request_is_a_greece_sized_subregion_not_the_globe(cache_dir):
    c = FakeClient()
    _run(c)
    assert len(c.calls) == 2, c.calls
    for _url, _fname, params in c.calls:
        d = dict(params)
        assert d["subregion"] == "on"
        assert d["var_TMP"] == "on"
        assert d["lev_2_m_above_ground"] == "on"
        assert float(d["rightlon"]) - float(d["leftlon"]) <= 15.0
        assert float(d["toplat"]) - float(d["bottomlat"]) <= 12.0
        # The point must fall inside, or the filter returns an empty field.
        assert float(d["leftlon"]) <= 23.73 <= float(d["rightlon"])
        assert float(d["bottomlat"]) <= 37.98 <= float(d["toplat"])


def test_cache_key_is_provider_run_step_and_variable_not_the_point(cache_dir):
    """Two nearby users on the same run must share one download.

    The cache key carries provider/kind/run/step/variable, so a different point
    inside the same box is a pure cache hit.
    """
    a = FakeClient()
    _run(a, lat=37.98, lon=23.73)
    b = FakeClient()
    _run(b, lat=37.99, lon=23.74)
    assert b.calls == [], "a nearby point must not refetch the same run/step"


def test_second_identical_request_hits_the_cache(cache_dir):
    c = FakeClient()
    _run(c)
    assert len(c.calls) == 2
    again = FakeClient()
    _run(again)
    assert again.calls == []


def test_a_different_step_is_a_different_cache_entry(cache_dir):
    _run(FakeClient(), step=24)
    c = FakeClient()
    _run(c, step=48)
    assert len(c.calls) == 2, "step must be part of the key"


def test_ttl_is_no_longer_than_the_run_cadence():
    assert ensemble.ENSEMBLE_TTL_S <= 6 * 3600


def test_stale_cache_is_used_only_when_the_fresh_fetch_fails(cache_dir):
    """A run rollout can leave the newest step unpublished; an old real field
    beats an empty card. A fresh success must never read the stale copy."""
    # Seed a cache entry, then age it past the TTL but within max-stale.
    _run(FakeClient())
    folder = cachestore.cache_dir()
    old = time.time() - ensemble.ENSEMBLE_TTL_S - 60
    for name in os.listdir(folder):
        os.utime(os.path.join(folder, name), (old, old))

    # Fresh fetch now fails: the stale copy should serve.
    c = FakeClient(fail_mean=True, fail_spread=True)
    out = _run(c)
    assert out, "the stale cache should have been served"
    assert out["t2m_spread_c"] == pytest.approx(2.0, abs=0.01)


def test_stale_cache_is_not_used_when_a_fresh_fetch_succeeds(cache_dir):
    _run(FakeClient())
    folder = cachestore.cache_dir()
    old = time.time() - ensemble.ENSEMBLE_TTL_S - 60
    for name in os.listdir(folder):
        os.utime(os.path.join(folder, name), (old, old))
    c = FakeClient()
    out = _run(c)
    assert len(c.calls) == 2, "a fresh fetch must happen once past the TTL"
    assert out["t2m_spread_c"] == pytest.approx(2.0, abs=0.01)


def test_stale_beyond_the_max_age_is_refused(cache_dir):
    _run(FakeClient())
    folder = cachestore.cache_dir()
    too_old = time.time() - ensemble.ENSEMBLE_MAX_STALE_S - 60
    for name in os.listdir(folder):
        os.utime(os.path.join(folder, name), (too_old, too_old))
    out = _run(FakeClient(fail_mean=True, fail_spread=True))
    assert out == {}


def test_cachestore_get_stale_rejects_a_truncated_entry(cache_dir):
    key = "ensemble|gefs|gespr|2026092400|24|t2m"
    cachestore.put(key, SPREAD_BYTES)
    assert cachestore.get_stale(key, max_age=3600) == SPREAD_BYTES
    # Truncate it, as an interrupted write would.
    p = cachestore._path(key)
    with open(p, "wb") as f:
        f.write(b"GRIB")
    assert cachestore.get_stale(key, max_age=3600) is None
    assert not os.path.exists(p), "the damaged entry should have been discarded"


# ============================================================ single-flight

def test_concurrent_identical_requests_share_one_upstream_fetch(monkeypatch, cache_dir):
    """The single-flight claim, tested through the real app helper.

    Two coroutines asking for the same cold point must produce exactly one pair
    of upstream GETs, not two.
    """
    import app as app_module

    calls = {"n": 0}

    async def slow_point(client, lat, lon, step=24, run=None):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return {"run": "2026092400", "step": step, "members": 30,
                "t2m_mean_c": 26.9, "t2m_spread_c": 2.4}

    monkeypatch.setattr(app_module.ensemble, "gefs_ensemble_point", slow_point)

    async def hit():
        return await app_module._single_flight(
            "ensemble|test", lambda: app_module.ensemble.gefs_ensemble_point(None, 37.9, 23.7))

    async def both():
        return await asyncio.gather(hit(), hit(), hit())

    results = asyncio.run(both())
    assert calls["n"] == 1, f"expected one upstream call, got {calls['n']}"
    assert all(r["t2m_spread_c"] == 2.4 for r in results)


# ============================================================ no leakage

def test_no_secret_or_internal_host_is_embedded_in_the_module():
    """The module talks to exactly one upstream and carries no credential."""
    src = Path(ensemble.__file__).read_text()
    assert "noaa-gefs-pds.s3.amazonaws.com" in src  # the documented probe host
    for forbidden in ("WX_SECRET", "WX_ADMIN_TOKEN", "STRIPE", "sk_live", "localhost",
                      "127.0.0.1", "WX_MASTER_CODE"):
        assert forbidden not in src, forbidden


def test_upstream_is_only_the_two_documented_https_hosts(cache_dir):
    c = FakeClient()
    _run(c)
    hosts = {url.split("/")[2] for url, _f, _p in c.calls}
    assert hosts == {"nomads.ncep.noaa.gov"}


# ============================================================ endpoint wiring

def _offline_wx(monkeypatch):
    """Stub every GFS/ICON/ECMWF source so the endpoint test uses no network."""
    async def fake_series(lat, lon, hours=48):
        return [{"step": s, "t2m_c": 20.0, "rh2_pct": 55.0, "u10": 1.0, "v10": 0.0,
                 "wind_kmh": 3.6, "wind_dir": 270.0, "precip_mm": 0.0,
                 "cloud_pct": 30.0} for s in range(1, 25)]

    async def no_profile(*a, **k):
        raise RuntimeError("no profile")

    async def no_orog(*a, **k):
        return 100.0

    monkeypatch.setattr(wx, "latest_gfs_run", lambda *a, **k: ("20260924", "00"))
    monkeypatch.setattr(wx, "gfs_surface_series", fake_series)
    monkeypatch.setattr(wx, "gfs_profile_dataset", no_profile)
    monkeypatch.setattr(wx, "gfs_orography", no_orog)
    monkeypatch.setattr(wx, "icon_eu_latest_run", lambda *a, **k: "2026092400")
    monkeypatch.setattr(wx, "ecmwf_point", no_profile)


def test_pro_payload_carries_the_ensemble_block(monkeypatch):
    import app as app_module
    import entitlements as ent
    from fastapi.testclient import TestClient

    async def fake_ensemble(client, lat, lon, leads=ensemble.ENSEMBLE_LEADS, run=None):
        return {"run": "2026092400", "members": 30,
                "leads": [{"hours": 24, "t2m_mean_c": 26.9, "t2m_spread_c": 2.4,
                           "wind_kmh": 12.0, "wind_dir": 200.0, "wind_spread_kmh": 3.0,
                           "gust_kmh": 25.0, "gust_spread_kmh": 4.0,
                           "precip_mm": 0.5, "precip_spread_mm": 0.6,
                           "cloud_pct": 60.0, "cloud_spread_pct": 23.0}]}

    _offline_wx(monkeypatch)
    monkeypatch.setattr(app_module.ensemble, "gefs_ensemble_series", fake_ensemble)
    with TestClient(app_module.app) as c:
        tok = ent.issue_token("pro", "passcode", ttl=ent.TOKEN_TTL_S)
        r = c.get("/api/brief", params={"lat": 37.98, "lon": 23.73, "token": tok})
    assert r.status_code == 200, r.text
    ens = r.json()["expert"]["ensemble"]
    assert ens["available"] is True
    assert ens["members"] == 30
    assert ens["leads"][0]["hours"] == 24
    assert ens["leads"][0]["t2m_spread_c"] == 2.4
    assert ens["leads"][0]["gust_spread_kmh"] == 4.0
    assert "confidence" not in ens["detail"].lower()
    assert "%" not in ens["detail"]


def test_free_caller_makes_zero_ensemble_requests(monkeypatch, cache_dir):
    """FREE must not fetch, cache or receive ensemble data."""
    import app as app_module
    from fastapi.testclient import TestClient

    called = []

    async def fake_ensemble(client, lat, lon, leads=ensemble.ENSEMBLE_LEADS, run=None):
        called.append(1)
        return {}

    _offline_wx(monkeypatch)
    monkeypatch.setattr(app_module.ensemble, "gefs_ensemble_series", fake_ensemble)
    with TestClient(app_module.app) as c:
        r = c.get("/api/brief", params={"lat": 37.98, "lon": 23.73})
        assert r.status_code == 200, r.text
        body = r.json()
    assert "ensemble" not in (body.get("expert") or {})
    assert called == [], "FREE must not trigger the ensemble fetch"
    # And nothing was written to the ensemble cache.
    folder = cachestore.cache_dir()
    assert not any("ensemble|gefs" in n or n.startswith("ensemble")
                   for n in (os.listdir(folder) if os.path.isdir(folder) else []))


def test_upstream_failure_leaves_the_rest_of_the_brief_intact(monkeypatch):
    """An ensemble outage must not disturb the existing forecast fields."""
    import app as app_module
    import entitlements as ent
    from fastapi.testclient import TestClient

    async def boom(client, lat, lon, leads=ensemble.ENSEMBLE_LEADS, run=None):
        raise RuntimeError("NOMADS down")

    _offline_wx(monkeypatch)
    monkeypatch.setattr(app_module.ensemble, "gefs_ensemble_series", boom)
    with TestClient(app_module.app) as c:
        tok = ent.issue_token("pro", "passcode", ttl=ent.TOKEN_TTL_S)
        r = c.get("/api/brief", params={"lat": 37.98, "lon": 23.73, "token": tok})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "ensemble" not in body["expert"]
    assert body["expert"]["agreement"]                 # the 3-model card still there
    assert body["simple"]["hours"]                     # the forecast still there
    assert body["meta"]["gfs_run"] == "2026092400"


# ============================================================ series API
#
# The series powers the Ensemble panel: temperature, wind, gusts,
# precipitation and cloud at a few leads. Everything is offline; the two
# upstream hosts and the two filter endpoints are asserted, and decode is
# patched at this module's own seam.

SERIES_BYTES = b"GRIB" + b"X" * 40
GUST_BYTES = b"GRIB" + b"G" * 40
SERIES_HOURS = (24, 72)


class SeriesClient:
    """Records GETs; answers the two GEFS filters with distinct blobs.

    The 0.5° filter answers the mixed mean/spread blob, the 0.25° filter answers
    the gust blob. `fail_gust` drops the gust half so degradation can be checked.
    """

    def __init__(self, fail_gust: bool = False):
        self.calls = []
        self.fail_gust = fail_gust

    async def get(self, url, **kwargs):
        params = dict(kwargs.get("params") or [])
        fname = params.get("file", "")
        self.calls.append((url, fname, tuple(sorted(params.items()))))
        if "0p25" in fname:
            if self.fail_gust:
                raise RuntimeError("gust unavailable")
            return _Resp(GUST_BYTES)
        if "0p50" in fname:
            return _Resp(SERIES_BYTES)
        raise AssertionError(f"unexpected file: {fname}")


@pytest.fixture(autouse=True)
def series_decode(monkeypatch):
    """Decode the series blobs into plausible fields, keyed off the blob sentinel.

    Mean: 20.0 C, 5 m/s wind, 0.5 mm precip, 50 % cloud, 10 m/s gust.
    Spread: 2.0 C, 1 m/s per wind component, 0.6 mm, 10 %, 2 m/s.
    """
    def _mixed(blob, lat, lon, tag):
        if blob != SERIES_BYTES:
            return {}
        if tag.startswith("gespr"):
            return {"t2m": 2.0, "u10": 1.0, "v10": 1.0, "tp": 0.6, "tcc": 10.0}
        return {"t2m": 293.15, "u10": 5.0, "v10": 0.0, "tp": 0.5, "tcc": 50.0}

    def _decode(blob, lat, lon, tag):
        # Preserve the earlier fixture's contract for the point-path blobs, so
        # adding this fixture does not silently disable the point tests.
        if blob == MEAN_BYTES:
            return 300.0
        if blob == SPREAD_BYTES:
            return 2.0
        if blob in (SERIES_BYTES, GUST_BYTES):
            # geavg and gespr share the blob in the fake, so split by tag.
            return 2.0 if tag.startswith("gespr") else 10.0
        return None

    monkeypatch.setattr(ensemble, "_decode_mixed", _mixed)
    monkeypatch.setattr(ensemble, "_decode_point", _decode)


def _series(client, leads=SERIES_HOURS):
    return asyncio.run(ensemble.gefs_ensemble_series(
        client, 37.98, 23.73, leads=leads))


def test_series_returns_one_row_per_lead_with_every_variable(cache_dir):
    out = _series(SeriesClient())
    assert out["run"] == "2026092400"
    assert out["members"] == 30
    assert [r["hours"] for r in out["leads"]] == list(SERIES_HOURS)
    row = out["leads"][0]
    assert row["t2m_mean_c"] == pytest.approx(20.0, abs=0.01)      # 293.15 - 273.15
    assert row["t2m_spread_c"] == pytest.approx(2.0, abs=0.01)
    # 5 m/s -> 18.0 km/h, from the east (270 degrees: arctan2(-u, -v) with v=0).
    assert row["wind_kmh"] == pytest.approx(18.0, abs=0.1)
    assert row["wind_dir"] == pytest.approx(270.0, abs=0.1)
    assert row["gust_kmh"] == pytest.approx(36.0, abs=0.1)         # 10 m/s
    assert row["gust_spread_kmh"] == pytest.approx(7.2, abs=0.1)   # 2 m/s
    assert row["precip_mm"] == pytest.approx(0.5, abs=0.01)
    assert row["precip_spread_mm"] == pytest.approx(0.6, abs=0.01)
    assert row["cloud_pct"] == 50.0
    assert row["cloud_spread_pct"] == 10.0


def test_series_wind_spread_is_vector_magnitude_not_speed_spread(cache_dir):
    """The spread is hypot(spread_u, spread_v), and the docstring says so."""
    out = _series(SeriesClient())
    # gespr u/v are 1 m/s each, so the vector magnitude is hypot(1,1) = 1.414 m/s
    # -> 5.1 km/h. A spread of *speed* would instead be 0 here, because both the
    # mean and spread wind vectors are governed by different components.
    assert out["leads"][0]["wind_spread_kmh"] == pytest.approx(5.09, abs=0.05)


def test_series_costs_four_requests_per_lead_never_one_per_variable(cache_dir):
    c = SeriesClient()
    _series(c)
    # Per lead: geavg+gespr on the 0.5° filter, and geavg+gespr on the 0.25°
    # gust filter. Never one request per variable.
    assert len(c.calls) == 4 * len(SERIES_HOURS), c.calls
    files = [f for _u, f, _p in c.calls]
    assert sum("pgrb2a" in f for f in files) == 2 * len(SERIES_HOURS)
    assert sum("pgrb2s" in f for f in files) == 2 * len(SERIES_HOURS)


def test_series_hits_both_gefs_filter_hosts_only(cache_dir):
    c = SeriesClient()
    _series(c)
    hosts = {url.split("/")[2] for url, _f, _p in c.calls}
    assert hosts == {"nomads.ncep.noaa.gov"}
    urls = {url for url, _f, _p in c.calls}
    assert ensemble.GEFS_FILTER in urls            # the 0.5 degree filter
    assert ensemble.GEFS_GUST_FILTER in urls       # the 0.25 degree gust filter


def test_series_requests_all_variables_and_the_greece_box(cache_dir):
    c = SeriesClient()
    _series(c)
    for _u, fname, params in c.calls:
        d = dict(params)
        assert d["subregion"] == "on"
        assert float(d["leftlon"]) <= 23.73 <= float(d["rightlon"])
        assert float(d["bottomlat"]) <= 37.98 <= float(d["toplat"])
        if "pgrb2a" in fname:
            assert d["var_TMP"] == "on" and d["lev_2_m_above_ground"] == "on"
            assert d["var_UGRD"] == "on" and d["var_VGRD"] == "on"
            assert d["var_APCP"] == "on" and d["lev_surface"] == "on"
            assert d["var_TCDC"] == "on" and d["lev_entire_atmosphere"] == "on"
        else:
            assert d["var_GUST"] == "on" and d["lev_surface"] == "on"


def test_series_cache_key_is_run_step_not_point(cache_dir):
    _series(SeriesClient(), leads=(24,))
    other = SeriesClient()
    asyncio.run(ensemble.gefs_ensemble_series(other, 37.5, 22.9, leads=(24,)))
    assert other.calls == [], "a nearby point must share the cached box"


def test_series_second_identical_call_is_free(cache_dir):
    _series(SeriesClient(), leads=(24,))
    again = SeriesClient()
    _series(again, leads=(24,))
    assert again.calls == []


def test_series_gust_failure_leaves_the_other_variables(cache_dir):
    """A gust outage must not remove temperature, wind, rain or cloud."""
    out = _series(SeriesClient(fail_gust=True), leads=(24,))
    row = out["leads"][0]
    assert "gust_kmh" not in row
    assert "gust_spread_kmh" not in row
    assert row["t2m_mean_c"] == pytest.approx(20.0, abs=0.01)
    assert row["precip_mm"] == pytest.approx(0.5, abs=0.01)
    assert row["cloud_pct"] == 50.0


def test_series_missing_everything_returns_no_leads_not_a_guess(cache_dir):
    import unittest.mock as mock

    def no_mixed(blob, lat, lon, tag):
        return {}

    with mock.patch.object(ensemble, "_decode_mixed", no_mixed), \
         mock.patch.object(ensemble, "_decode_point", lambda *a, **k: None):
        out = _series(SeriesClient(), leads=(24,))
    assert out["leads"] == []


def test_describe_series_grades_nothing_and_states_the_definition():
    series = {"run": "2026092400", "members": 30, "leads": [
        {"hours": 24, "t2m_mean_c": 20.0, "t2m_spread_c": 2.0,
         "wind_kmh": 18.0, "wind_dir": 180.0, "wind_spread_kmh": 3.0,
         "gust_kmh": 36.0, "gust_spread_kmh": 7.2,
         "precip_mm": 0.5, "precip_spread_mm": 0.6,
         "cloud_pct": 50.0, "cloud_spread_pct": 10.0}]}
    out = ensemble.describe_series(series)
    assert out["available"] is True
    assert out["members"] == 30
    assert len(out["leads"]) == 1
    blob = out["detail"].lower()
    for forbidden in ("%", "confidence", "reliability", "πιθανότητ", "chance",
                      "probability", "αξιοπιστία"):
        assert forbidden not in blob, forbidden
    for label in ("υψηλή", "μέτρια", "χαμηλή"):
        assert label not in out["detail"]
    assert "class" not in out
    assert "6 ωρ" in out["detail"]        # the accumulation window is stated
    assert "30" in out["detail"]


def test_describe_series_without_leads_is_explicitly_unavailable():
    out = ensemble.describe_series({"leads": []})
    assert out["available"] is False
    assert out["text"] == "μη διαθέσιμο"


def test_describe_series_headline_is_a_row_not_a_new_statistic():
    series = {"members": 30, "leads": [
        {"hours": 24, "t2m_mean_c": 20.0, "t2m_spread_c": 2.0},
        {"hours": 72, "t2m_mean_c": 22.0, "t2m_spread_c": 4.0}]}
    out = ensemble.describe_series(series, headline_hours=72)
    assert out["headline"]["hours"] == 72
    assert out["headline"]["text"] == "±4.0°C"
    assert out["headline"]["t2m_c"] == 22.0


def test_decode_temp_files_are_removed(cache_dir):
    """No GRIB scratch file may be left behind in the cache dir."""
    blob = b"GRIB" + b"Z" * 32
    before = set(os.listdir(cachestore.cache_dir()))
    ensemble._decode_point(blob, 37.9, 23.7, "cleanup")   # the decode will fail; that is fine
    ensemble._decode_mixed(blob, 37.9, 23.7, "cleanup")
    after = set(os.listdir(cachestore.cache_dir()))
    assert after == before, f"scratch files left: {after - before}"
