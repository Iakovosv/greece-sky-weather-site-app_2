"""Sun, moon and twilight for one point, for the "Ο Ουρανός Τώρα" card.

Positions come from PyEphem, which is MIT-licensed and therefore fine to ship in
a commercial product. The alternative - hand-rolling Meeus' lunar series - would
be a few hundred lines of polynomial that nobody would ever verify, for a result
that drifts by minutes. A maintained library that is checked against the
almanacs is the better engineering trade.

Conventions, because they are the difference between a right and a wrong answer:

* Rise and set use the standard almanac definition: the *upper limb* of the body
  at the horizon, which for the Sun is a centre altitude of -0.833 degrees
  (0.567 deg of mean refraction plus the 0.267 deg semi-diameter). PyEphem's own
  default (pressure=1010, horizon=0) approximates the same thing, but the
  explicit -0.833 is the published convention and does not depend on a pressure
  that nobody measured.
* Twilight needs the Sun's *centre* at the depression angle, so it uses
  pressure=0 with horizon -6/-12/-18. Mixing the two conventions is the classic
  bug that makes civil dawn wrong by several minutes.
* An observer at altitude sees the horizon dip, so sunrise is earlier and sunset
  later. PyEphem applies that dip only when `horizon` is left unset; setting
  `horizon` explicitly cancels it, so the dip is added here instead.
* All displayed times are local (Europe/Athens), which is UTC+3 in summer and
  UTC+2 in winter. The offset is computed rather than assumed, and the card
  labels it, so a winter card does not claim to be UTC+3.
"""
from __future__ import annotations

import datetime as dt
import math
import os
from zoneinfo import ZoneInfo

# The card is a plain-language feature, so it degrades to "unavailable" rather
# than taking the forecast page down with it if the dependency is missing.
#
# Three different failures used to look identical here: a wheel that was never
# installed, a wheel that is present but whose C extension will not load (wrong
# ABI, missing lib, permissions), and a present wheel with a missing dependency.
# All were reported the same way, so the operator reinstalled forever while the
# real error stayed hidden. The import error is kept *and classified* — telling
# "not installed" from "installed but broken" needs different fixes — and the
# import is retried on demand so a package installed after boot shows up without
# a restart.
ephem = None
_HAVE_EPHEM = False
_IMPORT_ERROR: str | None = None
# True only when the failure was `ephem` itself being absent, i.e. the case that
# `pip install ephem` actually fixes. A ModuleNotFoundError naming some *other*
# module means ephem is present but one of its dependencies is not.
_MISSING = False


def _try_import() -> bool:
    global ephem, _HAVE_EPHEM, _IMPORT_ERROR, _MISSING
    try:
        import ephem as _e
    except ModuleNotFoundError as e:
        # `import ephem` raising for anything other than ephem itself means the
        # package is present and a dependency is missing, which reinstalling
        # ephem will not fix.
        missing_ephem = getattr(e, "name", None) in (None, "ephem")
        ephem = None
        _HAVE_EPHEM = False
        _IMPORT_ERROR = f"{type(e).__name__}: {e}"
        _MISSING = missing_ephem
        return False
    except Exception as e:  # pragma: no cover - exercised in a broken install
        ephem = None
        _HAVE_EPHEM = False
        _IMPORT_ERROR = f"{type(e).__name__}: {e}"
        _MISSING = False
        return False
    ephem = _e
    _HAVE_EPHEM = True
    _IMPORT_ERROR = None
    _MISSING = False
    return True


_try_import()


def reload_ephem() -> bool:
    """Re-attempt the import. Called when a request finds ephem unavailable, so
    installing it into the running interpreter is enough — no restart needed."""
    return _HAVE_EPHEM or _try_import()


def import_error() -> str | None:
    """The failure text when the import failed, else None.

    Not a missing/not-missing signal: ephem absent also produces an error string
    (the ModuleNotFoundError). Use `is_missing()` for that, and this only to show
    the operator the underlying exception."""
    return _IMPORT_ERROR


def is_missing() -> bool:
    """Whether the failure is "not installed" rather than "installed but broken".

    This is what the card's wording keys off. Reporting a plain
    ModuleNotFoundError as "υπάρχει αλλά απέτυχε να φορτώσει" is a contradiction
    that sends the reader looking for the wrong problem.
    """
    return _MISSING


def interpreter() -> str:
    """The Python that is actually running this code.

    The common broken install is a wheel installed into a *different* interpreter
    than the one serving the app (`pip` and `uvicorn` on different Pythons, or a
    system pip under a virtualenv uvicorn). Reporting this path is what turns
    "η βιβλιοθήκη δεν είναι διαθέσιμη" into a one-command fix.
    """
    import sys
    return sys.executable


def _py_version() -> str:
    import platform
    return platform.python_version()


def install_hint() -> str:
    return f'"{interpreter()}" -m pip install "ephem>=4.1"'


TZ_NAME = os.environ.get("WX_ASTRO_TZ", "Europe/Athens")

# Standard almanac horizon for the Sun: upper limb at the horizon.
SUN_HORIZON_DEG = -0.833

# Moon has no atmosphere to refract through, so its upper limb is at -0.267,
# which is just its semi-diameter.
MOON_HORIZON_DEG = -0.267

DEPRESSIONS = {"civil": 6.0, "nautical": 12.0, "astro": 18.0}

GREEK_DAYS = ["Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή"]
GREEK_MONTHS = ["Ιανουαρίου", "Φεβρουαρίου", "Μαρτίου", "Απριλίου", "Μαΐου", "Ιουνίου",
                "Ιουλίου", "Αυγούστου", "Σεπτεμβρίου", "Οκτωβρίου", "Νοεμβρίου", "Δεκεμβρίου"]

# Illumination fraction bands, by age in days. The names are the standard Greek
# ones for the eight principal phases.
PHASE_NAMES = [
    (1.0, "Νέα Σελήνη"),
    (6.5, "Αύξων μηνίσκος"),
    (8.0, "Πρώτο τέταρτο"),
    (13.5, "Αύξων αμφίκυρτος"),
    (16.5, "Πανσέληνος"),
    (21.5, "Φθίνων αμφίκυρτος"),
    (23.5, "Τελευταίο τέταρτο"),
    (29.0, "Φθίνων μηνίσκος"),
]

SYNODIC_MONTH_D = 29.530588853


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(TZ_NAME)
    except Exception:
        return ZoneInfo("UTC")


def _ephem_date(when: dt.datetime):
    """PyEphem treats a naive datetime as UTC; be explicit so an aware one is
    not silently misread as local."""
    if when.tzinfo is not None:
        when = when.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return ephem.Date(when)


def _local(edate, tz: ZoneInfo) -> dt.datetime:
    return (ephem.Date(edate).datetime().replace(tzinfo=dt.timezone.utc)
            .astimezone(tz))


def _observer(lat: float, lon: float, elevation_m: float | None,
              horizon_deg: float | None) -> "ephem.Observer":
    o = ephem.Observer()
    # PyEphem takes longitude positive to the east, the opposite of the
    # convention weather APIs often use. The app stores east-positive, so this
    # is a direct assignment.
    o.lat = str(lat)
    o.lon = str(lon)
    o.elevation = float(elevation_m or 0)

    if horizon_deg is None:
        # Let PyEphem apply its pressure model and its elevation dip.
        o.pressure = 1010.0
    else:
        # Refraction is already folded into the requested horizon angle, so the
        # pressure model must be off or it would be applied twice. The horizon
        # dip is added by _horizon_with_dip instead, because setting `horizon`
        # explicitly suppresses the one PyEphem would otherwise apply.
        o.pressure = 0.0
        o.horizon = str(horizon_deg)
    return o


def _horizon_with_dip(horizon_deg: float, elevation_m: float | None) -> float:
    """Depress the horizon for an observer above sea level.

    The horizon drops by about 0.0347 * sqrt(height in metres) degrees, so at
    1200 m the Sun rises roughly six minutes earlier than the almanac time for
    sea level. Small, but it is the kind of detail this card exists to get right.
    """
    if not elevation_m or elevation_m <= 0:
        return horizon_deg
    return horizon_deg - 0.0347 * math.sqrt(float(elevation_m))


def _find_event(lat: float, lon: float, elevation_m: float | None,
                horizon_deg: float | None, body: str, method: str,
                start: dt.datetime, end: dt.datetime, tz: ZoneInfo):
    """First rise/set/transit strictly after `start`, or None if not before `end`.

    Returning None is a real outcome, not an error: the Moon rises about 50
    minutes later each day, so roughly once a month there is a calendar day with
    no moonrise at all. The card must say so rather than invent a time.
    """
    o = _observer(lat, lon, elevation_m, horizon_deg)
    o.date = _ephem_date(start)
    try:
        e = getattr(o, method)(getattr(ephem, body)())
    except (ephem.AlwaysUpError, ephem.NeverUpError, ValueError):
        # Polar day/night: inside the Arctic/Antarctic circle the Sun may not
        # cross the horizon at all, and PyEphem raises instead of returning None.
        # That is the same "no event today" outcome as a moonless night, so it is
        # reported as None rather than propagated. ValueError covers a NaN date.
        return None
    if e is None or e >= _ephem_date(end):
        return None
    return _local(e, tz)


def _track(lat: float, lon: float, elevation_m: float | None, body: str,
           start: dt.datetime, hours: int = 24, step_h: int = 1) -> list[dict]:
    """Altitude and azimuth sampled across the day, for the 24 h path drawing.

    Geometric altitude (pressure=0) so the arc is a smooth curve; the horizon
    line the card draws is the same -0.833 convention as the rise/set times.
    """
    o = _observer(lat, lon, elevation_m, None)
    o.pressure = 0.0
    out = []
    for i in range(0, hours + 1, step_h):
        t = start + dt.timedelta(hours=i)
        o.date = _ephem_date(t)
        b = getattr(ephem, body)(o)
        out.append({"h": i,
                    "alt": round(math.degrees(float(b.alt)), 1),
                    "az": round(math.degrees(float(b.az)), 1)})
    return out


def _phase_name(age: float) -> str:
    for limit, name in PHASE_NAMES:
        if age < limit:
            return name
    return PHASE_NAMES[-1][1]


def _fmt(t: dt.datetime | None, with_seconds: bool = False) -> dict | None:
    if t is None:
        return None
    return {"label": t.strftime("%H:%M:%S" if with_seconds else "%H:%M"),
            "iso": t.isoformat(timespec="seconds")}


def _length_h(a: dt.datetime | None, b: dt.datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return round((b - a).total_seconds() / 3600.0, 2)


def sky_now(lat: float, lon: float, elevation_m: float | None = None,
            when: dt.datetime | None = None) -> dict:
    """Everything the "Ο Ουρανός Τώρα" card shows, for one point and moment.

    Scoped to a single local calendar day beginning at local midnight, so every
    time returned belongs to the date in `now`.
    """
    if not reload_ephem():
        err = import_error()
        # Three cases, three different fixes. Wording the "not installed" case as
        # "present but failed to load" contradicts the ModuleNotFoundError shown
        # right next to it, so the reader hunts a broken wheel that does not exist.
        if is_missing():
            reason = ("Η βιβλιοθήκη αστρονομίας (ephem) δεν είναι εγκατεστημένη στο "
                      "περιβάλλον του server.")
        elif err:
            reason = ("Η βιβλιοθήκη ephem υπάρχει αλλά απέτυχε να φορτώσει: " + err)
        else:  # pragma: no cover - import_error() is set whenever the import fails
            reason = ("Η βιβλιοθήκη αστρονομίας (ephem) δεν είναι διαθέσιμη.")
        missing = is_missing()
        payload = {"available": False,
                   "reason": reason,
                   # Enough to fix it without guessing: the exact interpreter that
                   # failed to import, and the command that installs into it.
                   "interpreter": interpreter(),
                   "python_version": _py_version(),
                   "import_error": err,
                   "missing": missing}
        # The install command is offered only when installing is the fix. Showing
        # it for a broken wheel is how the operator ends up reinstalling forever,
        # which is the bug this whole branch exists to stop.
        if missing:
            payload["install_hint"] = install_hint()
        return payload

    tz = _tz()
    now_utc = (when or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    now_local = now_utc.astimezone(tz)

    day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_local = day_start_local + dt.timedelta(days=1)

    # --- sun ---------------------------------------------------------------
    sun_horizon = _horizon_with_dip(SUN_HORIZON_DEG, elevation_m)
    moon_horizon = _horizon_with_dip(MOON_HORIZON_DEG, elevation_m)

    sun_rise = _find_event(lat, lon, elevation_m, sun_horizon, "Sun", "next_rising",
                           day_start_local, day_end_local, tz)
    sun_set = _find_event(lat, lon, elevation_m, sun_horizon, "Sun", "next_setting",
                          day_start_local, day_end_local, tz)
    sun_transit = _find_event(lat, lon, elevation_m, None, "Sun", "next_transit",
                              day_start_local, day_end_local, tz)

    # Twilight uses the Sun's centre, hence the different convention.
    twilight = {}
    for name, dep in DEPRESSIONS.items():
        dawn = _find_event(lat, lon, elevation_m, -dep, "Sun", "next_rising",
                           day_start_local, day_end_local, tz)
        dusk = _find_event(lat, lon, elevation_m, -dep, "Sun", "next_setting",
                           day_start_local, day_end_local, tz)
        twilight[name] = {"dawn": _fmt(dawn), "dusk": _fmt(dusk),
                          "label": {"civil": "Πολιτικό", "nautical": "Ναυτικό",
                                    "astro": "Αστρονομικό"}[name],
                          "degrees": dep}

    # --- moon --------------------------------------------------------------
    moon = ephem.Moon(_ephem_date(now_utc))
    illum = round(float(moon.phase), 1)
    prev_new = ephem.previous_new_moon(_ephem_date(now_utc))
    age = (ephem.Date(_ephem_date(now_utc)) - ephem.Date(prev_new)) % SYNODIC_MONTH_D

    moon_rise = _find_event(lat, lon, elevation_m, moon_horizon, "Moon", "next_rising",
                            day_start_local, day_end_local, tz)
    moon_set = _find_event(lat, lon, elevation_m, moon_horizon, "Moon", "next_setting",
                           day_start_local, day_end_local, tz)
    moon_transit = _find_event(lat, lon, elevation_m, None, "Moon", "next_transit",
                               day_start_local, day_end_local, tz)

    # --- position right now ------------------------------------------------
    o = _observer(lat, lon, elevation_m, None)
    o.pressure = 0.0
    o.date = _ephem_date(now_utc)
    sun_now = ephem.Sun(o)
    sun_alt = round(math.degrees(float(sun_now.alt)), 1)
    moon_now = ephem.Moon(o)
    moon_alt = round(math.degrees(float(moon_now.alt)), 1)
    moon_az = round(math.degrees(float(moon_now.az)), 1)

    def azimuth_compass(deg: float) -> str:
        dirs = ["Β", "ΒΑ", "Α", "ΝΑ", "Ν", "ΝΔ", "Δ", "ΒΔ"]
        return dirs[int((deg + 22.5) % 360 // 45)]

    offset = now_local.utcoffset() or dt.timedelta(0)
    total_min = int(offset.total_seconds() // 60)
    offset_label = f"UTC{'+' if total_min >= 0 else '-'}{abs(total_min)//60:02d}:{abs(total_min)%60:02d}"

    return {
        "available": True,
        "tz": TZ_NAME,
        "utc_offset": offset_label,
        "lat": round(lat, 4), "lon": round(lon, 4),
        "elevation_m": None if elevation_m is None else round(elevation_m),
        "now": {
            "iso": now_local.isoformat(timespec="seconds"),
            "clock": now_local.strftime("%H:%M"),
            "utc": now_utc.strftime("%H:%M"),
            "date": now_local.strftime("%d/%m/%Y"),
            "day_name": GREEK_DAYS[now_local.weekday()],
            "month_name": GREEK_MONTHS[now_local.month - 1],
            "label": (f"{GREEK_DAYS[now_local.weekday()]} "
                      f"{now_local.day} {GREEK_MONTHS[now_local.month - 1]} "
                      f"{now_local.year}"),
        },
        "sun": {
            "rise": _fmt(sun_rise), "set": _fmt(sun_set),
            "transit": _fmt(sun_transit),
            "day_length_h": _length_h(sun_rise, sun_set),
            "altitude": sun_alt,
            "azimuth": round(math.degrees(float(sun_now.az)), 1),
            "azimuth_compass": azimuth_compass(math.degrees(float(sun_now.az))),
            "is_up": sun_alt > sun_horizon,
            "horizon_deg": round(sun_horizon, 3),
            "track": _track(lat, lon, elevation_m, "Sun", day_start_local),
            "twilight": twilight,
        },
        "moon": {
            "illumination_pct": illum,
            "age_days": round(age, 1),
            "phase_name": _phase_name(age),
            "waxing": age < SYNODIC_MONTH_D / 2,
            "rise": _fmt(moon_rise), "set": _fmt(moon_set),
            "transit": _fmt(moon_transit),
            "altitude": moon_alt,
            "azimuth": moon_az,
            "azimuth_compass": azimuth_compass(moon_az),
            "is_up": moon_alt > moon_horizon,
            "horizon_deg": round(moon_horizon, 3),
            "track": _track(lat, lon, elevation_m, "Moon", day_start_local),
            "next_new_moon": ephem.Date(
                ephem.next_new_moon(_ephem_date(now_utc))).datetime().strftime("%d/%m/%Y"),
            "next_full_moon": ephem.Date(
                ephem.next_full_moon(_ephem_date(now_utc))).datetime().strftime("%d/%m/%Y"),
        },
    }
