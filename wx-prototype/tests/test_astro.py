"""The astro card's numbers.

An astronomy card is only worth shipping if its times are right, and a wrong
sunrise is not a cosmetic bug. These tests pin the properties that are
independently checkable without an almanac at hand:

  * the equinox (~12 h day) and the solstices, which constrain the whole model;
  * that the two refraction conventions are not mixed up, since using the
    almanac horizon for twilight is the classic multi-minute bug;
  * that altitude moves sunrise in the right direction;
  * that a missing moonrise is reported as missing rather than fabricated.

The expected values were cross-checked against published Athens almanac times
before being written down here.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import astro  # noqa: E402

pytestmark = pytest.mark.skipif(not astro._HAVE_EPHEM,
                                reason="ephem not installed")

ATHENS = (37.9838, 23.7275)
NAXOS = (37.0965, 25.4094)


def minutes(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def day_length(a: dict) -> float:
    return a["sun"]["day_length_h"]


def at(y, mo, d, h=12, latlon=ATHENS, elev=0):
    lat, lon = latlon
    return astro.sky_now(lat, lon, elev, dt.datetime(y, mo, d, h, 0, tzinfo=dt.timezone.utc))


def test_equinox_day_is_twelve_hours():
    """On the equinox every latitude gets ~12 h. A model that gets refraction or
    the semi-diameter wrong fails this by a visible margin."""
    for latlon in (ATHENS, NAXOS, (40.85, 25.87), (35.24, 25.16)):
        a = at(2026, 3, 20, latlon=latlon)
        assert abs(day_length(a) - 12.05) < 0.25, (latlon, day_length(a))


def test_summer_solstice_is_longer_than_winter():
    summer = day_length(at(2026, 6, 21))
    winter = day_length(at(2026, 12, 21))
    assert summer > 14.0
    assert winter < 10.0
    assert summer - winter > 4.0


def test_athens_solstice_times_match_the_almanac():
    """Published Athens times are about 06:06/20:47 in late June and roughly
    07:36/17:09 in late December. Allowing a couple of minutes covers the
    year-to-year drift and the choice of horizon convention."""
    summer = at(2026, 6, 21)
    assert abs(minutes(summer["sun"]["rise"]["label"]) - minutes("06:06")) <= 6
    assert abs(minutes(summer["sun"]["set"]["label"]) - minutes("20:47")) <= 6

    winter = at(2026, 12, 21)
    assert abs(minutes(winter["sun"]["rise"]["label"]) - minutes("07:36")) <= 6
    assert abs(minutes(winter["sun"]["set"]["label"]) - minutes("17:09")) <= 6


def test_sunrise_precedes_transit_precedes_sunset():
    a = at(2026, 9, 23)
    r = minutes(a["sun"]["rise"]["label"])
    t = minutes(a["sun"]["transit"]["label"])
    s = minutes(a["sun"]["set"]["label"])
    assert r < t < s


def test_transit_is_near_local_noon():
    """Solar noon in Athens is within a few minutes of 13:20 summer time, since
    the meridian is 23.7E against a 30E zone centre, offset by the equation of
    time. The loose bound catches a longitude sign error, which would move it
    hours rather than minutes."""
    a = at(2026, 9, 23)
    assert abs(minutes(a["sun"]["transit"]["label"]) - minutes("13:19")) <= 20


def test_twilight_bands_are_ordered_and_nested():
    """Dawn at -18 is earliest, then -12, then -6, then sunrise; dusk mirrors it.
    If the twilight code accidentally used the sunrise horizon, the bands would
    collapse onto the sunrise time and this ordering would fail."""
    a = at(2026, 9, 23)
    tw = a["sun"]["twilight"]
    astro_dawn = minutes(tw["astro"]["dawn"]["label"])
    naut_dawn = minutes(tw["nautical"]["dawn"]["label"])
    civil_dawn = minutes(tw["civil"]["dawn"]["label"])
    sunrise = minutes(a["sun"]["rise"]["label"])
    assert astro_dawn < naut_dawn < civil_dawn < sunrise

    sunset = minutes(a["sun"]["set"]["label"])
    civil_dusk = minutes(tw["civil"]["dusk"]["label"])
    naut_dusk = minutes(tw["nautical"]["dusk"]["label"])
    astro_dusk = minutes(tw["astro"]["dusk"]["label"])
    assert sunset < civil_dusk < naut_dusk < astro_dusk


def test_twilight_uses_centre_convention_not_sunrise_horizon():
    """Civil dawn must be strictly later than astronomical dawn by a real gap.
    On this date the -18 to -6 span is about an hour; a mixed-convention bug
    would compress it to near zero."""
    a = at(2026, 9, 23)
    gap = (minutes(a["sun"]["twilight"]["civil"]["dawn"]["label"])
           - minutes(a["sun"]["twilight"]["astro"]["dawn"]["label"]))
    assert 40 <= gap <= 80, gap


def test_altitude_makes_sunrise_earlier_and_sunset_later():
    """The horizon dips as you climb, so the Sun appears earlier and leaves
    later. Only the sign is asserted, but that sign is the whole point."""
    sea = at(2026, 3, 20, elev=0)
    high = at(2026, 3, 20, elev=1500)
    assert minutes(high["sun"]["rise"]["label"]) < minutes(sea["sun"]["rise"]["label"])
    assert minutes(high["sun"]["set"]["label"]) > minutes(sea["sun"]["set"]["label"])
    assert day_length(high) > day_length(sea)


def test_altitude_dip_is_the_standard_formula():
    assert astro._horizon_with_dip(-0.833, 0) == pytest.approx(-0.833)
    assert astro._horizon_with_dip(-0.833, None) == pytest.approx(-0.833)
    assert astro._horizon_with_dip(-0.833, 400) == pytest.approx(
        -0.833 - 0.0347 * math.sqrt(400), abs=1e-6)


def test_longitude_changes_sunrise_between_two_greek_points():
    """The card exists to be local. Athens and Naxos are 1.7 degrees of
    longitude apart, so Naxos must see the Sun earlier."""
    a = at(2026, 9, 23, latlon=ATHENS)
    n = at(2026, 9, 23, latlon=NAXOS)
    assert minutes(n["sun"]["rise"]["label"]) < minutes(a["sun"]["rise"]["label"])


def test_moon_illumination_matches_the_synodic_cycle():
    """Illumination tracks age within a wide envelope: near new moon it is dark,
    near full it is bright. The exact value is ephem's business, but a
    phase/age inconsistency in our own code would break this."""
    full = at(2026, 9, 26)          # this month's full moon
    new = at(2026, 10, 10)          # and the next new moon
    assert full["moon"]["illumination_pct"] > 92
    assert new["moon"]["illumination_pct"] < 8
    assert full["moon"]["age_days"] > 13
    assert new["moon"]["age_days"] < 1.5 or new["moon"]["age_days"] > 28


def test_moon_phase_name_is_consistent_with_illumination():
    a = at(2026, 9, 23)
    m = a["moon"]
    assert 0 <= m["illumination_pct"] <= 100
    if m["illumination_pct"] > 97:
        assert "Πανσέληνος" in m["phase_name"]
    if m["illumination_pct"] < 2:
        assert "Νέα" in m["phase_name"]


def test_moon_age_is_within_the_synodic_month():
    for d in range(1, 29, 3):
        a = at(2026, 9, d)
        assert 0 <= a["moon"]["age_days"] < astro.SYNODIC_MONTH_D


def test_a_day_with_no_moonrise_reports_none_not_a_fabricated_time():
    """Roughly once a month the Moon never rises inside a calendar day. The card
    must be able to say so; a substituted time would be a silent lie."""
    a = at(2026, 1, 9)
    assert a["moon"]["rise"] is None
    assert a["moon"]["set"] is not None


def test_tracks_span_the_day_and_reach_both_hemispheres():
    a = at(2026, 3, 20, elev=0)
    for body in ("sun", "moon"):
        tr = a[body]["track"]
        assert len(tr) == 25
        assert [p["h"] for p in tr] == list(range(25))
        alts = [p["alt"] for p in tr]
        # over a full day the altitude must go both above and below the horizon
        assert max(alts) > 0 > min(alts)
        assert all(-90 <= p["alt"] <= 90 for p in tr)
        assert all(0 <= p["az"] <= 360 for p in tr)


def test_local_time_offset_follows_daylight_saving():
    """The brief said UTC+3; that is only true in summer. The card reports the
    real offset so a winter date is not mislabelled."""
    summer = at(2026, 6, 21)
    winter = at(2026, 12, 21)
    assert summer["utc_offset"] == "UTC+03:00"
    assert winter["utc_offset"] == "UTC+02:00"


def test_local_clock_is_not_the_utc_clock():
    a = at(2026, 9, 23, h=12)
    assert a["now"]["utc"] == "12:00"
    assert a["now"]["clock"] == "15:00"
    assert "Τετάρτη" in a["now"]["day_name"]


def test_payload_is_json_serialisable():
    """It is served straight from FastAPI, so nothing exotic may leak in."""
    import json
    a = at(2026, 9, 23)
    assert json.loads(json.dumps(a))["available"] is True


def test_missing_ephem_degrades_instead_of_raising(monkeypatch):
    """The forecast page must survive the dependency being absent."""
    monkeypatch.setattr(astro, "reload_ephem", lambda: False)
    monkeypatch.setattr(astro, "_IMPORT_ERROR", None)
    a = astro.sky_now(*ATHENS)
    assert a["available"] is False
    assert a["reason"]


def test_missing_ephem_message_says_how_to_fix_it(monkeypatch):
    """The message is read by whoever runs the server, not by a weather user, so
    it has to name the real fix. A bare 'not installed' sends them to Google.

    The fix is a bare interpreter path now, not 'requirements.txt': the common
    failure is ephem installed into a different Python than uvicorn runs, and
    'pip install -r requirements.txt' would repeat that mistake."""
    monkeypatch.setattr(astro, "reload_ephem", lambda: False)
    monkeypatch.setattr(astro, "_IMPORT_ERROR", "ModuleNotFoundError: "
                                                "No module named 'ephem'")
    monkeypatch.setattr(astro, "_MISSING", True)
    payload = astro.sky_now(*ATHENS)
    assert "ephem" in payload["reason"]
    assert "-m pip install" in payload["install_hint"]
    assert payload["interpreter"] == astro.interpreter()
    assert payload["missing"] is True


def test_a_broken_wheel_is_not_offered_a_reinstall(monkeypatch):
    """The install command only appears when installing is the cure. Printing it
    for a broken wheel is what keeps the operator in the reinstall loop."""
    monkeypatch.setattr(astro, "reload_ephem", lambda: False)
    monkeypatch.setattr(astro, "_IMPORT_ERROR",
                        "ImportError: libgfortran.so.5")
    monkeypatch.setattr(astro, "_MISSING", False)
    payload = astro.sky_now(*ATHENS)
    assert payload["missing"] is False
    assert "install_hint" not in payload


def test_a_broken_wheel_is_not_reported_as_missing(monkeypatch):
    """ephem present but failing to import (wrong ABI, missing lib) must not be
    described as 'not installed', or the operator reinstalls forever while the
    real error stays hidden. This is the bug the card actually hit."""
    monkeypatch.setattr(astro, "reload_ephem", lambda: False)
    monkeypatch.setattr(astro, "_IMPORT_ERROR", "ImportError: libgfortran.so.5")
    monkeypatch.setattr(astro, "_MISSING", False)
    payload = astro.sky_now(*ATHENS)
    assert payload["available"] is False
    assert "libgfortran" in payload["reason"]
    assert payload["import_error"] == "ImportError: libgfortran.so.5"
    assert "δεν είναι εγκατεστημένη" not in payload["reason"]


def test_the_import_is_retried_so_it_can_recover_without_a_restart(monkeypatch):
    """Installing ephem into the running interpreter should be enough. If the
    check only ever read a flag frozen at boot, the fix would appear not to work
    until the server was restarted."""
    calls = []
    monkeypatch.setattr(astro, "_HAVE_EPHEM", False)

    def fake_try():
        calls.append(1)
        return True

    monkeypatch.setattr(astro, "_try_import", fake_try)
    assert astro.reload_ephem() is True
    assert calls, "reload_ephem did not re-attempt the import"


def test_polar_night_is_no_event_not_a_crash():
    """Above the Arctic circle the Sun may not cross the horizon at all. PyEphem
    raises AlwaysUpError/NeverUpError instead of returning None, and that used to
    escape as an exception and blank the whole card."""
    a = astro.sky_now(89.9, 0.0, 0, dt.datetime(2026, 12, 21, 12, tzinfo=dt.timezone.utc))
    assert a["available"] is True      # the card still draws
    assert a["sun"]["rise"] is None
    assert a["sun"]["set"] is None
    assert a["sun"]["day_length_h"] is None


def test_polar_day_is_no_event_not_a_crash():
    a = astro.sky_now(89.9, 0.0, 0, dt.datetime(2026, 6, 21, 12, tzinfo=dt.timezone.utc))
    assert a["available"] is True
    assert a["sun"]["set"] is None


def test_ephem_is_a_declared_dependency():
    """The card is data-bearing, so ephem must ship with the app rather than be
    an accident of the local environment."""
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    assert re.search(r"^ephem", req, re.M), "ephem missing from requirements.txt"
