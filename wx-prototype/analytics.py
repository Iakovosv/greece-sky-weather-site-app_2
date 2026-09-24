"""First-party, privacy-conscious event analytics.

Design constraints, and why each choice was made
------------------------------------------------
*Self-hosted, no monthly cost.* The events go into the same SQLite file the rest
of the app already uses. No third-party script is loaded by the page, which also
means no data processor to disclose in the privacy policy and no cookie banner
for a tracking cookie that does not exist.

*No precise location, ever.* This is the one that matters for a weather app,
because the interesting event — `forecast_loaded` — naturally has a coordinate
attached to it. The coordinate is reduced to a coarse cell (0.5° ≈ 55 km) and
then, before it is stored, *only the cell* is kept. A 0.5° cell over Attica has
thousands of residents; it cannot identify a household, and it is still enough to
answer "which parts of the country use this". The exact point is never written.

*No IP address, no user agent string, no fingerprint.* The visitor id is the same
opaque device id the entitlement token carries, hashed with a per-install salt so
it cannot be correlated with anything outside this database. Referrer is reduced
to its host (`news.example.gr`), not the full URL, because a full URL can carry a
query string that identifies a person.

*Country/region without IP geolocation.* Taken from the browser's own
`Accept-Language` heading, which is already sent and is approximately right. It
is a genuine compromise: `el-GR` is not proof of location. Stated as such in the
privacy page rather than dressed up as geolocation.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
from urllib.parse import urlparse

import config

log = logging.getLogger("wx.analytics")

# Event names are a closed vocabulary. A typo in a `track()` call would otherwise
# create a new row dimension that no dashboard knows about; validating here makes
# it a visible log line instead.
EVENTS = frozenset({
    "page_view",
    "forecast_loaded",
    "location_searched",
    "map_location_selected",
    "forecast_72h_viewed",
    "forecast_240h_viewed",
    "model_comparison_opened",
    "expert_opened",
    "skewt_opened",
    "expert_time_changed",
    "pro_paywall_viewed",
    "checkout_started",
    "subscription_created",
    "subscription_cancelled",
    "promo_code_opened",
    "promo_code_redeemed",
    "sky_camera_opened",
    "verification_viewed",
})

# Coarse cell size in degrees. 0.5° is ~55 km of latitude and, at Greek
# latitudes, ~43 km of longitude. Chosen to be clearly non-identifying while
# still separating Crete from Macedonia from the Ionian.
CELL_DEG = 0.5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analytics_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    day         TEXT NOT NULL,           -- denormalised for cheap daily counts
    name        TEXT NOT NULL,
    visitor     TEXT,                    -- salted hash of the device id
    session     TEXT,                    -- hash of (visitor, session start)
    path        TEXT,
    referrer_host TEXT,
    language    TEXT,                    -- Accept-Language heading, e.g. "el"
    device      TEXT,                    -- "phone" | "tablet" | "desktop"
    browser     TEXT,                    -- "firefox" | "chrome" | ...
    cell_lat    REAL,                    -- coarse only, NULL for most events
    cell_lon    REAL,
    value_num   REAL,                    -- e.g. requested hours, model count
    meta        TEXT                     -- small JSON blob, no PII
);
CREATE INDEX IF NOT EXISTS idx_an_day  ON analytics_events (day);
CREATE INDEX IF NOT EXISTS idx_an_name ON analytics_events (name, day);
CREATE INDEX IF NOT EXISTS idx_an_vis  ON analytics_events (visitor);
"""

_LOCAL = threading.local()


def _connect() -> sqlite3.Connection:
    path = config.db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db() -> None:
    with _connect() as con:
        con.executescript(_SCHEMA)


def enabled() -> bool:
    """Analytics can be switched off entirely; the default is on."""
    return (os.environ.get("WX_ANALYTICS", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def retention_days() -> int:
    """How long raw events are kept. 0 disables pruning (not recommended).

    Raw events are per-visitor rows, so an unpruned table grows without bound on
    a public site: it is the one table here that an anonymous caller can add rows
    to. A year is long enough for a seasonal comparison and short enough that the
    file stays small enough to back up by copying.
    """
    raw = (os.environ.get("WX_ANALYTICS_RETENTION_DAYS") or "").strip()
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return 365
    return max(0, days)


def prune(retention: int | None = None) -> dict:
    """Delete events older than the retention window. Safe to call repeatedly.

    Run from the scheduler and opportunistically after a write burst, so a
    long-running process does not depend on an external cron to stay bounded.
    """
    days = retention_days() if retention is None else retention
    if days <= 0:
        return {"pruned": 0, "retention_days": days}
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        with _connect() as con:
            cur = con.execute("DELETE FROM analytics_events WHERE day < ?", (cutoff,))
            removed = cur.rowcount or 0
    except sqlite3.Error as e:
        log.warning("analytics prune failed: %s", e)
        return {"pruned": 0, "retention_days": days, "error": str(e)[:120]}
    if removed:
        log.info("analytics prune: removed %d event(s) older than %d days",
                 removed, days)
    return {"pruned": removed, "retention_days": days}


_SALT: str | None = None

# When the last prune ran, so a write-heavy minute does not run one per request.
_LAST_PRUNE = 0.0
_PRUNE_INTERVAL_S = 3600


def maybe_prune(now: float | None = None) -> dict | None:
    """Prune at most once an hour, from whatever request happens to arrive.

    A background task would be tidier, but this table is only touched by the
    ingest endpoint; hanging the periodic tidy off the same traffic that grows it
    means a deployment needs no cron and an idle deployment does no work.
    """
    global _LAST_PRUNE
    now = time.time() if now is None else now
    if now - _LAST_PRUNE < _PRUNE_INTERVAL_S:
        return None
    _LAST_PRUNE = now
    return prune()


def _salt() -> str:
    """Per-install salt for hashing the visitor id.

    Held in the database rather than in the environment so a backup of the
    database plus a `.env` are not enough to recompute the hashes — the salt file
    would have to leak too. Generated on first use and never rotated (rotating it
    would split every returning visitor into two, which would silently corrupt
    the retention numbers).
    """
    global _SALT
    if _SALT is not None:
        return _SALT
    with _connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS analytics_salt (v TEXT)")
        row = con.execute("SELECT v FROM analytics_salt LIMIT 1").fetchone()
        if row:
            _SALT = row["v"]
            return _SALT
        import secrets
        _SALT = secrets.token_hex(32)
        con.execute("INSERT INTO analytics_salt (v) VALUES (?)", (_SALT,))
    return _SALT


def hash_visitor(device: str | None) -> str | None:
    if not device:
        return None
    return hashlib.sha256((_salt() + device).encode()).hexdigest()[:20]


def coarse_cell(lat: float | None, lon: float | None) -> tuple[float | None, float | None]:
    """Snap a coordinate to its coarse cell, or (None, None) if not real."""
    if lat is None or lon is None:
        return None, None
    if not (config.finite_lat(lat) and config.finite_lon(lon)):
        return None, None
    return (round(lat / CELL_DEG) * CELL_DEG, round(lon / CELL_DEG) * CELL_DEG)


_BROWSER_PATTERNS = (
    ("edge", "Edge"), ("edg/", "Edge"), ("opr/", "Opera"), ("opera", "Opera"),
    ("firefox", "Firefox"), ("fxios", "Firefox"), ("chrome", "Chrome"),
    ("crios", "Chrome"), ("safari", "Safari"),
)


def describe_ua(ua: str | None) -> tuple[str, str]:
    """(device class, browser family) from a user agent.

    Only the family is kept, never the version or the full string: a version
    narrows the population, and the full string is close to a fingerprint. The
    result is one of a handful of values, which is all a usage chart needs.
    """
    if not ua:
        return "unknown", "unknown"
    low = ua.lower()
    if "ipad" in low or ("android" in low and "mobile" not in low) or "tablet" in low:
        device = "tablet"
    elif "mobi" in low or "iphone" in low or "android" in low:
        device = "phone"
    else:
        device = "desktop"
    browser = "other"
    for needle, name in _BROWSER_PATTERNS:
        if needle in low:
            browser = name
            break
    return device, browser


def _language(header: str | None) -> str | None:
    """The primary language tag, e.g. `el-GR` from `el-GR,el;q=0.9,en;q=0.8`.

    The region subtag is kept when present: it is the only country-level signal
    available without IP geolocation, which is exactly what the usage breakdown
    wants. It is a weak signal — a Greek speaker abroad browsing in English is
    counted as English — and the privacy page says so rather than presenting it
    as a location.
    """
    if not header:
        return None
    first = header.split(",")[0].strip()
    tag = first.split(";")[0].strip()
    return tag[:12] or None


def _referrer_host(value: str | None) -> str | None:
    """Just the host. A full referrer can carry an identifying query string."""
    if not value:
        return None
    try:
        host = urlparse(value).hostname
    except ValueError:
        return None
    if not host:
        return None
    # Drop a `www.` prefix so the same site is one row.
    return re.sub(r"^www\.", "", host)[:80]


def track(name: str, *, request=None, device: str | None = None,
          lat: float | None = None, lon: float | None = None,
          value: float | None = None, meta: dict | None = None,
          session: str | None = None) -> bool:
    """Record one event. Never raises into the request path.

    Analytics is a side concern: a failure to write a row must not fail the
    forecast. Everything is caught and logged at warning level.
    """
    if not enabled():
        return False
    if name not in EVENTS:
        log.warning("analytics: unknown event %r ignored", name)
        return False
    try:
        path = referrer = lang = ua = None
        if request is not None:
            path = request.url.path[:120]
            referrer = request.headers.get("referer")
            lang = _language(request.headers.get("accept-language"))
            ua = request.headers.get("user-agent")
        device_class, browser = describe_ua(ua)
        cell_lat, cell_lon = coarse_cell(lat, lon)
        now = dt.datetime.now(dt.timezone.utc)
        import json as _json
        with _connect() as con:
            con.execute(
                """INSERT INTO analytics_events
                       (ts, day, name, visitor, session, path, referrer_host, language,
                        device, browser, cell_lat, cell_lon, value_num, meta)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (now.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%d"), name,
                 hash_visitor(device), session, path, _referrer_host(referrer), lang,
                 device_class, browser, cell_lat, cell_lon, value,
                 _json.dumps(meta, ensure_ascii=False)[:400] if meta else None))
        return True
    except Exception as e:
        log.warning("analytics write failed for %s: %s", name, e)
        return False


def summary(days: int = 30) -> dict:
    """Aggregates for the admin view. Small enough to compute on read."""
    days = max(1, min(int(days), 365))
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        with _connect() as con:
            totals = con.execute(
                """SELECT COUNT(*) events, COUNT(DISTINCT visitor) visitors,
                          COUNT(DISTINCT session) sessions
                     FROM analytics_events WHERE day >= ?""", (cutoff,)).fetchone()
            by_name = con.execute(
                """SELECT name, COUNT(*) n, COUNT(DISTINCT visitor) uniq
                     FROM analytics_events WHERE day >= ?
                    GROUP BY name ORDER BY n DESC""", (cutoff,)).fetchall()
            by_day = con.execute(
                """SELECT day, COUNT(*) n, COUNT(DISTINCT visitor) visitors
                     FROM analytics_events WHERE day >= ?
                    GROUP BY day ORDER BY day DESC""", (cutoff,)).fetchall()
            refs = con.execute(
                """SELECT COALESCE(referrer_host, '(direct)') host, COUNT(*) n
                     FROM analytics_events WHERE day >= ? AND name='page_view'
                    GROUP BY host ORDER BY n DESC LIMIT 15""", (cutoff,)).fetchall()
            devices = con.execute(
                """SELECT device, browser, COUNT(DISTINCT visitor) visitors
                     FROM analytics_events WHERE day >= ?
                    GROUP BY device, browser ORDER BY visitors DESC""", (cutoff,)).fetchall()
            langs = con.execute(
                """SELECT COALESCE(language,'(unknown)') language,
                          COUNT(DISTINCT visitor) visitors
                     FROM analytics_events WHERE day >= ?
                    GROUP BY language ORDER BY visitors DESC LIMIT 15""", (cutoff,)).fetchall()
            cells = con.execute(
                """SELECT cell_lat, cell_lon, COUNT(*) n
                     FROM analytics_events
                    WHERE day >= ? AND cell_lat IS NOT NULL
                    GROUP BY cell_lat, cell_lon ORDER BY n DESC LIMIT 25""", (cutoff,)).fetchall()
    except sqlite3.Error as e:
        return {"available": False, "error": str(e)[:120]}
    return {
        "available": True,
        "window_days": days,
        "totals": dict(totals) if totals else {},
        "events": [dict(r) for r in by_name],
        "daily": [dict(r) for r in by_day],
        "referrers": [dict(r) for r in refs],
        "devices": [dict(r) for r in devices],
        "languages": [dict(r) for r in langs],
        # Coarse cells, not points. Documented in the privacy page.
        "coarse_cells": [dict(r) for r in cells],
        "note": "Τοποθεσία μόνο σε κύτταρα 0.5° (~50 km). Χωρίς IP, χωρίς coords ακριβείας.",
    }
