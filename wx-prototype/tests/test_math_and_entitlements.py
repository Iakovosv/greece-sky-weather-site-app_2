"""Tests for the elevation/cloud-base maths and the entitlement flows.

These cover the pieces that carry a physical or security claim, so a future
change cannot quietly turn the marketing copy into a false statement.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app
import entitlements as ent
import wx


# --------------------------------------------------------------- cloud base

def test_cloud_base_matches_hand_computed_lcl():
    # T=25 C, RH=50% -> Td ~= 13.9 C, spread ~= 11.1 C, LCL ~= 1386 m
    t, rh = 25.0, 50.0
    td = app.dewpoint(t, rh)
    out = app.cloud_base_m(t, rh, elevation_m=100)
    assert out is not None
    assert out["base_agl_m"] == round(125.0 * (t - td))
    assert out["base_msl_m"] == out["base_agl_m"] + 100
    assert out["spread_c"] == round(t - td, 1)
    assert 1200 < out["base_agl_m"] < 1600


def test_cloud_base_saturates_toward_the_surface():
    out = app.cloud_base_m(20.0, 100.0)
    assert out is not None
    assert out["base_agl_m"] == 0


def test_cloud_base_rises_as_air_dries():
    moist = app.cloud_base_m(20.0, 80.0)
    dry = app.cloud_base_m(20.0, 40.0)
    assert moist is not None and dry is not None
    assert dry["base_agl_m"] > moist["base_agl_m"]


def test_cloud_base_is_none_without_the_inputs():
    assert app.cloud_base_m(None, 50.0) is None
    assert app.cloud_base_m(20.0, None) is None
    assert app.cloud_base_m(20.0, 0.0) is None


# ------------------------------------------------------------- lapse rate

def _profile(z_m, t_c):
    return xr.Dataset({
        "gh": ("isobaricInhPa", np.asarray(z_m, dtype=float)),
        "t": ("isobaricInhPa", np.asarray(t_c, dtype=float) + 273.15),
    })


def test_lapse_rate_is_read_from_a_steep_profile():
    # 9 C over 1000 m = 9.0e-3 C/m, inside the physical clamp
    prof = _profile([0, 500, 1000, 1500], [20, 15.5, 11, 6.5])
    rate, source = app.derive_lapse_rate(prof)
    assert source == "derived"
    assert 0.0089 < rate < 0.0091


def test_lapse_rate_falls_back_when_the_fit_is_absurd():
    # +3 C per 1000 m is an inversion, not a lapse rate
    prof = _profile([0, 500, 1000, 1500], [10, 11.5, 13, 14.5])
    rate, source = app.derive_lapse_rate(prof)
    assert source == "standard"
    assert rate == app.LAPSE_RATE_C_PER_M


def test_lapse_rate_uses_only_the_lowest_three_km():
    # The lowest 3 km give a healthy 9 C/km. Above that the profile is an
    # inversion so steep that a whole-column fit would be rejected by the clamp,
    # so getting "derived" at 9 C/km proves the 3 km filter is doing the work.
    prof = _profile([0, 1000, 2000, 3000, 8000, 12000],
                    [20, 11, 2, -7, 40, 60])
    rate, source = app.derive_lapse_rate(prof)
    assert source == "derived"
    assert 0.0089 < rate < 0.0091


# ------------------------------------------------------------ entitlements

def test_trial_token_is_pro_and_short_lived():
    token = ent.issue_trial()
    e = ent.verify_token(token)
    assert e.is_pro
    assert e.source == "trial"
    assert e.hours == ent.TRIAL_HOURS


def test_plan_payload_exposes_the_trial_to_the_ui():
    p = ent.plan_payload()
    assert p["trial_days"] == p["pricing"]["trial"]["days"] == 2
    assert p["trial_hours"] == ent.TRIAL_HOURS


def test_tampered_token_is_not_pro():
    token = ent.issue_token("free", "passcode")
    payload, sig = token.split(".")
    flipped = "A" if sig[0] != "A" else "B"
    e = ent.verify_token(payload + "." + flipped + sig[1:])
    assert not e.is_pro


def test_yearly_discount_matches_the_two_prices():
    p = ent.PRICING
    expected = round((1 - p["yearly"]["price"] / (12 * p["monthly"]["price"])) * 100, 1)
    assert ent.yearly_discount_percent() == expected


# ------------------------------------------------------- GFS cache keying

def test_cache_key_changes_when_the_variable_set_changes():
    original = wx.GFS_SFC_VARS
    before = wx._varsig()
    try:
        wx.GFS_SFC_VARS = original + ("PWAT",)
        assert wx._varsig() != before
    finally:
        wx.GFS_SFC_VARS = original
    assert wx._varsig() == before


def test_gfs_steps_follow_the_published_cadence():
    assert wx.gfs_steps(48) == list(range(1, 49))
    steps = wx.gfs_steps(240)
    assert steps[:120] == list(range(1, 121))
    # beyond f120 GFS is 3-hourly: f123, f126, ... and must not request f121
    assert steps[120] == 123
    assert all(s % 3 == 0 for s in steps[120:])
    assert steps[-1] == 240
