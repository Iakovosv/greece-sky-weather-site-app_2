"""Station telemetry storage and local bias correction.

The point of this module is to measure how wrong the model is *at one specific
place*, using a station that actually sits there. A 25 km grid cell cannot know
that a village sits in a valley; a station can.

Method
------
1. Record every station observation (Ecowitt push) with its UTC timestamp.
2. Record every model forecast value we serve, tagged with the run that produced it.
3. For each observation, find the forecast that was *operationally available* at
   the time of the observation (latest run issued before the observation, matching
   the same valid hour) and take `obs - forecast`. That is a true out-of-sample
   error, not a comparison against the same run that produced it.
4. Average the last N errors into an offset, and apply that offset to the first
   few hours of the next forecast.

Guards (all of them matter in production)
----------------------------------------
* Minimum sample count before any correction is applied.
* Reject observations that are physically implausible for the site.
* Reject the whole offset if it is larger than `MAX_OFFSET_C` - that usually means
  a broken sensor, a shadowed thermometer, or a station that has moved.
* Clamp the applied correction, so a bad batch of observations cannot silently
  push the forecast many degrees off.
* Report the offset and sample count in the API output, so the correction is never
  hidden from the user or from whoever is debugging it.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("WX_DB", "/tmp/wx-cache/station.db")

# --- tuning knobs, all deliberate ---
MIN_PAIRS = 4          # need at least this many obs/forecast pairs
MAX_OFFSET_C = 5.0     # larger than this means the sensor or site is suspect
CLAMP_C = 3.0          # never shift the forecast more than this
CORRECTION_HOURS = 6   # only the first N hours get corrected
IMPLAUSIBLE_C = (-60.0, 60.0)


@contextmanager
def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with _db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS station_obs (
            station_id TEXT NOT NULL,
            ts_utc     TEXT NOT NULL,
            temp_c     REAL,
            humidity   REAL,
            wind_kmh   REAL,
            gust_kmh   REAL,
            pressure   REAL,
            rain_mm    REAL,
            PRIMARY KEY (station_id, ts_utc)
        );
        CREATE TABLE IF NOT EXISTS model_fcst (
            run_utc    TEXT NOT NULL,
            valid_utc  TEXT NOT NULL,
            lat        REAL NOT NULL,
            lon        REAL NOT NULL,
            source     TEXT NOT NULL,
            temp_c     REAL,
            PRIMARY KEY (run_utc, valid_utc, lat, lon, source)
        );
        CREATE INDEX IF NOT EXISTS idx_fcst_match ON model_fcst (lat, lon, valid_utc);
        CREATE TABLE IF NOT EXISTS stations (
            station_id TEXT PRIMARY KEY,
            passkey    TEXT,
            name       TEXT,
            lat        REAL,
            lon        REAL,
            elevation_m REAL,
            active     INTEGER DEFAULT 1
        );
        """)


# ---------------------------------------------------------------- ingest

def record_obs(station_id: str, ts_utc: str, temp_c: float | None, humidity: float | None,
               wind_kmh: float | None, gust_kmh: float | None, pressure: float | None,
               rain_mm: float | None) -> bool:
    """Store one observation. Returns False if it was rejected as implausible."""
    if temp_c is not None and not (IMPLAUSIBLE_C[0] <= temp_c <= IMPLUSIBLE_C[1]):
        return False
    with _db() as con:
        con.execute(
            "INSERT OR REPLACE INTO station_obs VALUES (?,?,?,?,?,?,?,?)",
            (station_id, ts_utc, temp_c, humidity, wind_kmh, gust_kmh, pressure, rain_mm))
    return True


def record_forecasts(run_utc: str, lat: float, lon: float, source: str, rows: list[dict]) -> None:
    """Store served forecast values so future observations can be verified against them."""
    now = dt.datetime.now(dt.timezone.utc)
    with _db() as con:
        for r in rows:
            if r.get("t2m_c") is None:
                continue
            valid = (now + dt.timedelta(hours=int(r["step"]))).replace(
                minute=0, second=0, microsecond=0)
            con.execute(
                "INSERT OR REPLACE INTO model_fcst VALUES (?,?,?,?,?,?)",
                (run_utc, valid.strftime("%Y-%m-%dT%H:%M"), round(lat, 2), round(lon, 2),
                 source, float(r["t2m_c"])))


def register_station(station_id: str, passkey: str | None, name: str | None,
                     lat: float, lon: float, elevation_m: float | None = None) -> None:
    with _db() as con:
        con.execute("INSERT OR REPLACE INTO stations VALUES (?,?,?,?,?,?,1)",
                    (station_id, passkey, name, lat, lon, elevation_m))


def get_station(station_id: str) -> dict | None:
    """Full station row, including the passkey. Internal callers only.

    Anything that reaches an HTTP response must go through `public_station()`
    instead, or the device credential will be served to an anonymous caller.
    """
    with _db() as con:
        row = con.execute("SELECT * FROM stations WHERE station_id=? AND active=1",
                          (station_id,)).fetchone()
    return dict(row) if row else None


# The fields safe to return over an unauthenticated response. `passkey` is the
# device credential and is deliberately absent: it authenticates the Ecowitt
# push, so publishing it would let anyone post fabricated observations.
PUBLIC_STATION_FIELDS = ("station_id", "name", "lat", "lon", "elevation_m", "active")


def public_station(station: dict | None) -> dict | None:
    """A station row reduced to its non-secret fields, or None."""
    if not station:
        return None
    return {k: station.get(k) for k in PUBLIC_STATION_FIELDS}


# ---------------------------------------------------------------- correction

def compute_bias(station_id: str, lat: float, lon: float, source: str = "gfs",
                 hours: int = CORRECTION_HOURS) -> dict:
    """Average station-minus-forecast temperature error over recent hours.

    Matches each observation to the forecast from the newest run that was already
    issued when the observation was taken, so the error is genuinely out-of-sample.
    """
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")
    with _db() as con:
        obs = con.execute(
            "SELECT ts_utc, temp_c FROM station_obs "
            "WHERE station_id=? AND ts_utc>=? AND temp_c IS NOT NULL ORDER BY ts_utc",
            (station_id, since)).fetchall()

        pairs: list[tuple[str, float, float]] = []
        for o in obs:
            f = con.execute(
                "SELECT run_utc, temp_c FROM model_fcst "
                "WHERE lat=? AND lon=? AND source=? AND valid_utc=? "
                "AND run_utc <= ? ORDER BY run_utc DESC LIMIT 1",
                (round(lat, 2), round(lon, 2), source, o["ts_utc"], o["ts_utc"])).fetchone()
            if f and f["temp_c"] is not None:
                pairs.append((o["ts_utc"], float(o["temp_c"]), float(f["temp_c"])))

    n = len(pairs)
    if n < MIN_PAIRS:
        return {"applied": False, "reason": f"only {n} matched observations (need {MIN_PAIRS})",
                "pairs": n, "offset_c": None, "raw_offset_c": None}

    errors = [o - f for _, o, f in pairs]
    raw = sum(errors) / n
    # median absolute deviation as a cheap outlier sanity check
    med = sorted(errors)[n // 2]
    mad = sum(abs(e - med) for e in errors) / n

    if abs(raw) > MAX_OFFSET_C:
        return {"applied": False,
                "reason": f"offset {raw:+.1f}C exceeds {MAX_OFFSET_C}C - suspect sensor or site change",
                "pairs": n, "offset_c": None, "raw_offset_c": round(raw, 2), "mad_c": round(mad, 2)}

    applied = max(-CLAMP_C, min(CLAMP_C, raw))
    return {"applied": True, "reason": "ok", "pairs": n,
            "offset_c": round(applied, 2), "raw_offset_c": round(raw, 2), "mad_c": round(mad, 2),
            "clamped": abs(applied - raw) > 0.01}


def apply_correction(rows: list[dict], bias: dict, field: str = "t2m_c") -> list[dict]:
    """Apply the offset to the first CORRECTION_HOURS of the series.

    The correction fades out rather than stopping abruptly, so the handover to the
    raw model is not a visible jump. Returns new dicts; the raw value is kept
    on each row under `<field>_raw` so nothing is hidden.
    """
    if not bias.get("applied"):
        return rows
    off = bias["offset_c"]
    out = []
    for r in rows:
        r = dict(r)
        step = int(r.get("step", 0))
        if step <= CORRECTION_HOURS and r.get(field) is not None:
            weight = 1.0 - (step - 1) / CORRECTION_HOURS  # 1.0 at step 1 -> ~0 at step 6
            weight = max(0.0, min(1.0, weight))
            r[f"{field}_raw"] = r[field]
            r[field] = r[field] + off * weight
            r["bias_c"] = round(off * weight, 2)
        out.append(r)
    return out


def recent_obs(station_id: str, limit: int = 24) -> list[dict]:
    with _db() as con:
        rows = con.execute(
            "SELECT * FROM station_obs WHERE station_id=? ORDER BY ts_utc DESC LIMIT ?",
            (station_id, limit)).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print("station db initialised at", DB_PATH)
