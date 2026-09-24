"""Tests for ERA5 verification and the camera configuration.

Both modules make claims the product depends on: that the published accuracy
number is computed the way it says, and that a camera with no feed is reported
as missing rather than silently rendered as broken.
"""
from __future__ import annotations

import datetime as dt
import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cameras as cams
import verify


# ------------------------------------------------------------ box_mean

def test_box_mean_averages_around_the_point_not_a_single_cell():
    # A 3x3 latch centred on (0, 0) in a field that is 0 everywhere but the centre.
    lat = np.array([-1.0, 0.0, 1.0])
    lon = np.array([-1.0, 0.0, 1.0])
    field = np.zeros((3, 3))
    field[1, 1] = 9.0
    # +/-1 deg box covers the whole grid: one 9 and eight 0s.
    assert verify.box_mean(field, lat, lon, 0.0, 0.0, box=1.0) == pytest.approx(1.0)
    # A box that excludes the neighbours reproduces the raw cell, which is exactly
    # the noisy comparison the function exists to avoid.
    assert verify.box_mean(field, lat, lon, 0.0, 0.0, box=0.0) == pytest.approx(9.0)


def test_box_mean_ignores_nan_neighbours():
    lat = np.array([0.0, 1.0])
    lon = np.array([0.0, 1.0])
    field = np.array([[4.0, np.nan], [np.nan, np.nan]])
    assert verify.box_mean(field, lat, lon, 0.0, 0.0, box=1.0) == pytest.approx(4.0)


def test_box_mean_outside_the_grid_is_nan_not_an_exception():
    lat = np.array([0.0, 1.0])
    lon = np.array([0.0, 1.0])
    field = np.zeros((2, 2))
    assert np.isnan(verify.box_mean(field, lat, lon, 60.0, 10.0, box=0.5))


# --------------------------------------------------------------- scores

def test_scores_reports_bias_mae_and_rmse_for_a_known_sample():
    # errors: -1, +1, +3  -> bias 1, MAE 5/3, RMSE sqrt(11/3)
    s = verify.scores([-1.0, 1.0, 3.0])
    assert s["n"] == 3
    assert s["bias"] == pytest.approx(1.0, abs=0.01)
    assert s["mae"] == pytest.approx(1.67, abs=0.01)
    assert s["rmse"] == pytest.approx(1.91, abs=0.01)


def test_scores_with_no_samples_is_all_none():
    assert verify.scores([]) == {"n": 0, "bias": None, "mae": None, "rmse": None}
    assert verify.scores([float("nan"), None])["n"] == 0


# ------------------------------------------------------- verification plan

def test_plan_never_asks_era5_for_a_time_it_does_not_have():
    """Every step must land behind the ERA5T publication lag."""
    now = dt.datetime(2026, 9, 23, 12, tzinfo=dt.timezone.utc)
    plan = verify.verification_plan(now, days=4)
    assert plan
    newest = (now - dt.timedelta(days=verify.ERA5_LAG_DAYS)).date()
    for p in plan:
        assert p["valid"].date() <= newest
        # 00z run + lead must equal the valid time, or the row is mislabelled
        expected = (dt.datetime.strptime(p["run_date"], "%Y%m%d").replace(tzinfo=dt.timezone.utc)
                    + dt.timedelta(hours=p["lead_h"]))
        assert p["valid"] == expected


def test_plan_leads_match_the_declared_horizons():
    plan = verify.verification_plan(dt.datetime(2026, 9, 23, tzinfo=dt.timezone.utc), days=2)
    assert {p["lead_h"] for p in plan} == set(verify.LEADS_H)
    assert len(plan) == len(verify.LEADS_H) * 2


# --------------------------------------------------------------- cameras

def _with_cameras(monkeypatch, value: str):
    monkeypatch.setenv("WX_CAMERAS", value)
    importlib.reload(cams)


def test_unconfigured_cameras_are_flagged_not_faked(monkeypatch):
    monkeypatch.delenv("WX_CAMERAS", raising=False)
    importlib.reload(cams)
    payload = cams.camera_payload(now=1_000_000.0)
    assert payload["configured_count"] == 0
    assert all(c["status"] == "not_configured" for c in payload["cameras"])
    assert payload["note"] is not None


def test_configured_camera_is_live_and_gets_a_cache_busting_stamp(monkeypatch):
    _with_cameras(monkeypatch, '[{"id":"ilioupoli","lat":37.93,"lon":23.75,'
                               '"snapshot":"https://cam.example/latest.jpg"}]')
    payload = cams.camera_payload(now=1_000_000.0)
    assert payload["configured_count"] == 1
    cam = payload["cameras"][0]
    assert cam["status"] == "live"
    assert payload["stamp"] == int(1_000_000.0 // 60)
    assert payload["note"] is None


def test_malformed_camera_env_falls_back_to_defaults(monkeypatch):
    _with_cameras(monkeypatch, "{this is not json")
    payload = cams.camera_payload()
    assert payload["configured_count"] == 0
    assert {c["id"] for c in payload["cameras"]} == {"ilioupoli", "glinado"}


def test_camera_typos_do_not_produce_invalid_types(monkeypatch):
    _with_cameras(monkeypatch, '[{"id":"x","lat":"not-a-number","lon":null,'
                               '"snapshot":"https://cam.example/x.jpg"}]')
    cam = cams.camera_payload()["cameras"][0]
    assert cam["lat"] is None and cam["lon"] is None
    assert cam["status"] == "live"        # a feed exists even if the coords are junk
    assert cam["name"] == "x"             # name falls back to the id, never missing


def test_camera_snapshot_that_is_not_a_public_url_is_not_configured(monkeypatch):
    """A snapshot must be a public http(s) URL. Anything else is reported as not
    configured rather than placed in an <img src>, so a paste error cannot become
    a broken card or a non-http scheme the browser would reject."""
    _with_cameras(monkeypatch, '[{"id":"x","snapshot":"javascript:alert(1)"},'
                               '{"id":"y","snapshot":"u"}]')
    by_id = {c["id"]: c for c in cams.camera_payload()["cameras"]}
    assert by_id["x"]["snapshot"] is None and by_id["x"]["status"] == "not_configured"
    assert by_id["y"]["snapshot"] is None and by_id["y"]["status"] == "not_configured"
