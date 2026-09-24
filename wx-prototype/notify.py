"""Weather push notifications for Greek locations (PRO feature).

The rule this file exists to enforce
-----------------------------------
Whether a caller receives notifications is answered by the same server-side PRO
entitlement every other PRO surface uses. The subscribe endpoint asks
`effective_entitlement()`; nothing here is a browser flag, and no new
entitlement type is introduced.

What is stored, and why
-----------------------
SQLite, in the database the rest of the app already uses (`WX_DB`), so there is
one backup target rather than two.

`notify_subs` holds what a push needs: the subscription endpoint and keys the
browser's PushManager produced, plus the user's chosen notification location and
alert preferences. The location is stored **quantized to 0.1 deg** (`CELL_DEG`)
rather than as the exact point the browser reported. That is a deliberate
reduction, not a claim that quantized location is anonymous: 0.1 deg is about
11 km, which is coarser than the forecast grid the app already serves and is not
a street address, but it is still location data and is documented as such in the
privacy policy. There is no continuous tracking - the coordinates are read once,
with an explicit user action, and then only the quantized cell is kept.

`notify_sent` is the deduplication and delivery record. One row per
(subject, event_key), where `event_key` names the rule, the local date, the
forecast window and the cell. A unique index makes "has this already been sent?"
atomic in SQLite, which is what keeps a restart from sending the same alert
twice without any in-process lock.

Delivery is **at-least-once**, not exactly-once: the row goes to `pending`, the
push is attempted, then the row becomes `sent` or `failed`. A crash in the small
window between a successful push and the status update can re-send one alert on
the next cycle. That direction is chosen on purpose - the alternative (mark sent
before sending) silently loses a real alert on any transient push failure, which
is the worse outcome for a weather warning. The OS notification `tag` is the
event key, so even a re-send replaces the previous notification rather than
stacking a second one.

Thresholds
----------
`THRESHOLDS` holds the initial product defaults, all overridable by environment
variable. They are reasonable starting values tuned for Greece, **not**
scientifically validated limits, and they are expected to be adjusted once real
notification outcomes can be reviewed. Every threshold lives in this one place;
no rule hardcodes a number.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field, replace

import config

log = logging.getLogger("wx.notify")

# Quantization of the stored notification location. 0.1 deg is ~11 km of latitude
# and, at Greek latitudes, ~8-9 km of longitude. Coarser than the 0.25 deg GFS
# grid the free forecast already serves, so no alert can reveal more about a
# point than the forecast the user can already fetch. It is still location data
# and is treated as such in the privacy policy - not as anonymised.
CELL_DEG = 0.1

RULES = ("rain", "storm", "wind", "temp")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notify_subs (
    subject          TEXT PRIMARY KEY,   -- opaque device id from the signed token
    endpoint         TEXT NOT NULL,      -- PushSubscription.endpoint
    p256dh           TEXT NOT NULL,      -- PushSubscription keys
    auth             TEXT NOT NULL,
    ua_class         TEXT,               -- phone | tablet | desktop
    browser          TEXT,
    active           INTEGER NOT NULL DEFAULT 1,
    ios_standalone   INTEGER NOT NULL DEFAULT 0,
    -- Notification location. Quantized to CELL_DEG; never the exact point.
    place_name       TEXT,
    place_admin1     TEXT,
    cell_lat         REAL,
    cell_lon         REAL,
    rules            TEXT,               -- JSON {"rain":1,"storm":1,"wind":1,"temp":1}
    quiet_from       INTEGER,            -- local hour the quiet window starts
    quiet_to         INTEGER,            -- local hour it ends
    -- PRO state cache used by the background pass, which has no request to ask.
    -- Refreshed from Stripe for subscriptions; see refresh_pro_cache().
    pro_until_cached INTEGER,
    subscription_id  TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    last_seen_at     TEXT,
    last_error       TEXT,
    fail_count       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS notify_sent (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject         TEXT NOT NULL,
    event_key       TEXT NOT NULL,
    rule            TEXT NOT NULL,
    severity        TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    lead_h          INTEGER,
    fired_at        TEXT NOT NULL,
    last_attempt_at TEXT,
    sent_at         TEXT,
    last_error      TEXT,
    UNIQUE (subject, event_key)
);
CREATE INDEX IF NOT EXISTS idx_nsent_sub_day ON notify_sent (subject, fired_at);
CREATE INDEX IF NOT EXISTS idx_nsent_status  ON notify_sent (status);
CREATE TABLE IF NOT EXISTS notify_runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    evaluated INTEGER NOT NULL DEFAULT 0,
    sent      INTEGER NOT NULL DEFAULT 0,
    skipped   INTEGER NOT NULL DEFAULT 0,
    errors    INTEGER NOT NULL DEFAULT 0,
    detail    TEXT
);
"""

_LOCAL = threading.local()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime | None = None) -> str:
    return (t or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect() -> sqlite3.Connection:
    path = config.db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db() -> None:
    with _connect() as con:
        con.executescript(_SCHEMA)


# ---------------------------------------------------------------- thresholds

def _envf(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default


def _envi(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Thresholds:
    """Initial product defaults, all env-overridable, all in one place.

    These are starting values chosen to match Greek weather practice (the 40 C
    heat figure, Beaufort 9 for wind) and the model fields already served. They
    are not scientifically validated warning limits and are expected to move once
    real outcomes can be reviewed.
    """
    # Rain: total over a rolling 6 h window within the next day.
    rain_6h_mm: float = 15.0
    rain_6h_mm_severe: float = 25.0
    # Storm: instability AND a triggering hourly rate, both required. CAPE alone
    # is a condition, not a forecast of a storm, so it is never sufficient alone.
    cape_jkg: float = 1000.0
    cape_jkg_severe: float = 2000.0
    storm_pr_mmh: float = 5.0
    # Wind: gust where the model has it, else sustained wind (Beaufort 9/10).
    gust_kmh: float = 70.0
    gust_kmh_severe: float = 90.0
    # Temperature: heat uses air temp or feels-like; cold uses air temp.
    hot_c: float = 40.0
    feels_c: float = 41.0
    cold_c: float = -10.0
    # Anti-spam.
    cooldown_h: dict = field(default_factory=lambda: {
        "rain": 6, "storm": 3, "wind": 6, "temp": 12})
    quiet_from: int = 22
    quiet_to: int = 7
    max_per_day: int = 5
    min_lead_h: int = 1
    # Delivery retry. Bounded so a dead subscription cannot retry forever.
    retry_max: int = 3
    retry_backoff_min: int = 15

    @property
    def window_h(self) -> int:
        return 24


def load_thresholds() -> Thresholds:
    """Resolved on every call, so envfile.load() and tests both reach it."""
    base = Thresholds()
    cooldowns = dict(base.cooldown_h)
    for rule in RULES:
        cooldowns[rule] = _envi(f"WX_NOTIFY_COOLDOWN_H_{rule.upper()}",
                                cooldowns[rule])
    return replace(
        base,
        rain_6h_mm=_envf("WX_NOTIFY_RAIN_6H_MM", base.rain_6h_mm),
        rain_6h_mm_severe=_envf("WX_NOTIFY_RAIN_6H_MM_SEVERE", base.rain_6h_mm_severe),
        cape_jkg=_envf("WX_NOTIFY_CAPE_JKG", base.cape_jkg),
        cape_jkg_severe=_envf("WX_NOTIFY_CAPE_JKG_SEVERE", base.cape_jkg_severe),
        storm_pr_mmh=_envf("WX_NOTIFY_STORM_PR_MMH", base.storm_pr_mmh),
        gust_kmh=_envf("WX_NOTIFY_GUST_KMH", base.gust_kmh),
        gust_kmh_severe=_envf("WX_NOTIFY_GUST_KMH_SEVERE", base.gust_kmh_severe),
        hot_c=_envf("WX_NOTIFY_HOT_C", base.hot_c),
        feels_c=_envf("WX_NOTIFY_FEELS_C", base.feels_c),
        cold_c=_envf("WX_NOTIFY_COLD_C", base.cold_c),
        cooldown_h=cooldowns,
        quiet_from=_envi("WX_NOTIFY_QUIET_FROM", base.quiet_from),
        quiet_to=_envi("WX_NOTIFY_QUIET_TO", base.quiet_to),
        max_per_day=_envi("WX_NOTIFY_MAX_PER_DAY", base.max_per_day),
        min_lead_h=_envi("WX_NOTIFY_MIN_LEAD_H", base.min_lead_h),
        retry_max=_envi("WX_NOTIFY_RETRY_MAX", base.retry_max or 3) or 3,
        retry_backoff_min=_envi("WX_NOTIFY_RETRY_BACKOFF_MIN", base.retry_backoff_min),
    )


# ---------------------------------------------------------------- VAPID

def vapid_public_key() -> str:
    return config.vapid_public_key()


def vapid_private_key() -> str:
    """Never logged, never returned to a client. Environment only."""
    return config.vapid_private_key()


def vapid_subject() -> str:
    return (os.environ.get("WX_VAPID_SUBJECT") or "").strip() or "mailto:support@example.com"


def webpush_module():
    """The pywebpush entry points, or None when the package is absent.

    Imported lazily, like `stripe` in billing.py: without it the forecast, the
    passcode and the trial all still work and notification enablement disables
    itself rather than failing at import time.
    """
    try:
        import pywebpush  # noqa: F401
        return pywebpush
    except Exception:
        return None


def webpush_available() -> bool:
    return webpush_module() is not None


def push_available() -> bool:
    """Whether real dispatch can happen at all. Keys are the auth to the push service."""
    return webpush_available() and bool(vapid_public_key() and vapid_private_key())


def unavailable_reason() -> str | None:
    if not webpush_available():
        return "package_missing"
    if not vapid_public_key() or not vapid_private_key():
        return "not_configured"
    return None


def print_vapid_keys() -> None:
    """Generate a VAPID keypair and print it in the form `.env` expects.

    A one-off setup helper, not used at runtime. The private key is printed to
    stdout for the operator to paste into `.env`; it is never logged by the app.
    """
    import base64
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from py_vapid import Vapid01
    v = Vapid01()
    v.generate_keys()
    raw = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    pub = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    # py_vapid's `from_string` base64url-decodes and expects exactly 32 bytes,
    # so the private key is stored base64url too — not hex.
    priv_bytes = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    priv = base64.urlsafe_b64encode(priv_bytes).rstrip(b"=").decode()
    print(f"WX_VAPID_PUBLIC_KEY={pub}")
    print(f"WX_VAPID_PRIVATE_KEY={priv}")


# ---------------------------------------------------------------- subscriptions

def normalize_rules(rules) -> dict:
    """Only the known rule names, coerced to 0/1. An unknown key is dropped."""
    out = {}
    src = rules if isinstance(rules, dict) else {}
    for rule in RULES:
        try:
            out[rule] = 1 if int(src.get(rule, 1)) else 0
        except (TypeError, ValueError):
            out[rule] = 1
    return out


def quantize(lat: float, lon: float) -> tuple[float, float]:
    """Snap a point to the CELL_DEG grid. Stored value, never the exact input."""
    return (round(round(lat / CELL_DEG) * CELL_DEG, 2),
            round(round(lon / CELL_DEG) * CELL_DEG, 2))


def cell_label(cell_lat: float | None, cell_lon: float | None) -> str | None:
    if cell_lat is None or cell_lon is None:
        return None
    return f"{cell_lat:.2f},{cell_lon:.2f}"


def upsert_subscription(subject: str, endpoint: str, p256dh: str, auth: str, *,
                        ua_class: str | None = None, browser: str | None = None,
                        ios_standalone: bool = False,
                        place_name: str | None = None, place_admin1: str | None = None,
                        cell: tuple[float, float] | None = None,
                        rules=None, quiet_from: int | None = None,
                        quiet_to: int | None = None,
                        pro_until: int | None = None,
                        subscription_id: str | None = None) -> dict:
    """Create or replace a device's subscription. Raises ValueError on bad input."""
    if not subject:
        raise ValueError("subject is required")
    if not endpoint or not p256dh or not auth:
        raise ValueError("endpoint, p256dh and auth are required")
    if len(endpoint) > 2000:
        raise ValueError("endpoint is too long")
    init_db()
    now = _iso()
    cell_lat, cell_lon = (cell if cell else (None, None))
    with _connect() as con:
        con.execute(
            """INSERT INTO notify_subs
                   (subject, endpoint, p256dh, auth, ua_class, browser, active,
                    ios_standalone, place_name, place_admin1, cell_lat, cell_lon,
                    rules, quiet_from, quiet_to, pro_until_cached, subscription_id,
                    created_at, updated_at, last_seen_at, fail_count)
               VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
               ON CONFLICT(subject) DO UPDATE SET
                   endpoint=excluded.endpoint, p256dh=excluded.p256dh,
                   auth=excluded.auth, ua_class=excluded.ua_class,
                   browser=excluded.browser, active=1,
                   ios_standalone=excluded.ios_standalone,
                   -- location and rules are only overwritten when supplied, so a
                   -- re-subscribe from a new browser does not wipe the user's
                   -- chosen notification location.
                   place_name=COALESCE(excluded.place_name, notify_subs.place_name),
                   place_admin1=COALESCE(excluded.place_admin1, notify_subs.place_admin1),
                   cell_lat=COALESCE(excluded.cell_lat, notify_subs.cell_lat),
                   cell_lon=COALESCE(excluded.cell_lon, notify_subs.cell_lon),
                   rules=COALESCE(excluded.rules, notify_subs.rules),
                   quiet_from=COALESCE(excluded.quiet_from, notify_subs.quiet_from),
                   quiet_to=COALESCE(excluded.quiet_to, notify_subs.quiet_to),
                   pro_until_cached=COALESCE(excluded.pro_until_cached, notify_subs.pro_until_cached),
                   subscription_id=COALESCE(excluded.subscription_id, notify_subs.subscription_id),
                   updated_at=excluded.updated_at, last_seen_at=excluded.last_seen_at,
                   fail_count=0, last_error=NULL""",
            (subject, endpoint, p256dh, auth, ua_class, browser,
             1 if ios_standalone else 0, place_name, place_admin1, cell_lat, cell_lon,
             json.dumps(normalize_rules(rules)), quiet_from, quiet_to,
             pro_until, subscription_id, now, now, now))
    return get_subscription(subject) or {}


def set_active(subject: str, active: bool) -> bool:
    init_db()
    with _connect() as con:
        cur = con.execute("UPDATE notify_subs SET active=?, updated_at=? WHERE subject=?",
                          (1 if active else 0, _iso(), subject))
    return cur.rowcount > 0


def purge(subject: str) -> bool:
    """Remove the subscription row. The dedupe history is kept (see purge_sent)."""
    init_db()
    with _connect() as con:
        cur = con.execute("DELETE FROM notify_subs WHERE subject=?", (subject,))
    return cur.rowcount > 0


def set_location(subject: str, *, place_name: str | None, place_admin1: str | None,
                 lat: float, lon: float) -> dict | None:
    """Set the notification location. Only the quantized cell is written."""
    cell_lat, cell_lon = quantize(lat, lon)
    init_db()
    with _connect() as con:
        cur = con.execute(
            """UPDATE notify_subs SET place_name=?, place_admin1=?, cell_lat=?,
                   cell_lon=?, updated_at=? WHERE subject=?""",
            (place_name, place_admin1, cell_lat, cell_lon, _iso(), subject))
    return get_subscription(subject) if cur.rowcount else None


def set_rules(subject: str, rules, *, quiet_from: int | None = None,
              quiet_to: int | None = None) -> dict | None:
    init_db()
    with _connect() as con:
        cur = con.execute(
            """UPDATE notify_subs SET rules=?, quiet_from=COALESCE(?, quiet_from),
                   quiet_to=COALESCE(?, quiet_to), updated_at=? WHERE subject=?""",
            (json.dumps(normalize_rules(rules)), quiet_from, quiet_to, _iso(), subject))
    return get_subscription(subject) if cur.rowcount else None


def set_pro_cache(subject: str, pro_until: int | None,
                  subscription_id: str | None = None) -> None:
    init_db()
    with _connect() as con:
        con.execute(
            """UPDATE notify_subs SET pro_until_cached=?, subscription_id=?,
                   updated_at=? WHERE subject=?""",
            (pro_until, subscription_id, _iso(), subject))


def get_subscription(subject: str) -> dict | None:
    init_db()
    with _connect() as con:
        row = con.execute("SELECT * FROM notify_subs WHERE subject=?", (subject,)).fetchone()
    return _row_to_sub(row) if row else None


def _row_to_sub(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["rules"] = json.loads(d.get("rules") or "{}")
    except (TypeError, ValueError):
        d["rules"] = {}
    d["rules"] = normalize_rules(d["rules"])
    d["active"] = bool(d.get("active"))
    d["ios_standalone"] = bool(d.get("ios_standalone"))
    d["has_location"] = d.get("cell_lat") is not None and d.get("cell_lon") is not None
    return d


def active_subscriptions(limit: int = 500) -> list[dict]:
    init_db()
    with _connect() as con:
        rows = con.execute(
            """SELECT * FROM notify_subs WHERE active=1
               ORDER BY COALESCE(last_seen_at, created_at) DESC LIMIT ?""",
            (int(limit),)).fetchall()
    return [_row_to_sub(r) for r in rows]


def stats() -> dict:
    """Small counts for /api/health. Never raises."""
    try:
        init_db()
        with _connect() as con:
            subs = con.execute(
                "SELECT COUNT(*) c, SUM(active) a FROM notify_subs").fetchone()
            sent = con.execute(
                "SELECT COUNT(*) c FROM notify_sent WHERE status='sent'").fetchone()
            pending = con.execute(
                "SELECT COUNT(*) c FROM notify_sent WHERE status='pending'").fetchone()
            failed = con.execute(
                "SELECT COUNT(*) c FROM notify_sent WHERE status='failed'").fetchone()
        return {"subscribers": int(subs["c"] or 0),
                "active": int(subs["a"] or 0),
                "sent": int(sent["c"] or 0),
                "pending": int(pending["c"] or 0),
                "failed": int(failed["c"] or 0)}
    except Exception as e:  # a broken db must not break /api/health
        return {"error": f"{type(e).__name__}"}


# ---------------------------------------------------------------- evaluation

@dataclass(frozen=True)
class Alert:
    rule: str
    severity: str          # "warn" | "severe"
    lead_h: int
    bucket: int
    value: float
    window_h: int
    title: str
    body: str
    local_date: str = "unknown"
    local_hour: int | None = None
    cell: str | None = None

    @property
    def key(self) -> str:
        """One identity per alert occurrence.

        Rule + local date + window bucket + cell. The cell is part of the key so
        that changing the notification location produces a genuinely new event
        rather than being suppressed by the previous location's history.
        """
        return f"{self.rule}|{self.local_date}|{self.bucket}|{self.cell or 'none'}"


def _consecutive_run(hours: list[dict], start: int, span: int) -> list[dict] | None:
    """`span` consecutive hourly entries from `start`, or None if not contiguous."""
    chunk = hours[start:start + span]
    if len(chunk) < span:
        return None
    for a, b in zip(chunk, chunk[1:]):
        if b["step_h"] - a["step_h"] != 1:
            return None
    return chunk


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def evaluate(hours: list[dict], *, run_utc: str | None = None,
             tz: dt.tzinfo | None = None,
             rules: dict | None = None,
             cell: str | None = None,
             th: Thresholds | None = None) -> list[Alert]:
    """Evaluate the four rules over the next `th.window_h` hours.

    Pure: no network, no database, no clock. `hours` is the hourly series the
    forecast path already produces (`step_h`, `t`, `feels`, `precip`, `wind`,
    `gust`, and `cape` where the model provides it). `run_utc` ("YYYYMMDDHH") is
    used for local-date labelling of the event key and the quiet-hours check.
    """
    th = th or load_thresholds()
    rules = normalize_rules(rules)
    horizon = th.window_h
    series = [h for h in hours if isinstance(h, dict)
              and _num(h.get("step_h")) is not None
              and 0 <= int(h["step_h"]) <= horizon]
    if not series:
        return []

    def _local_time(step_h: int) -> tuple[str, int | None]:
        """(local date, local hour) for a lead time, or ('unknown', None)."""
        if not run_utc or len(run_utc) < 10:
            return "unknown", None
        try:
            base = dt.datetime.strptime(run_utc, "%Y%m%d%H").replace(
                tzinfo=dt.timezone.utc)
        except ValueError:
            return "unknown", None
        local = (base + dt.timedelta(hours=int(step_h))).astimezone(
            tz or dt.timezone.utc)
        return local.strftime("%Y-%m-%d"), local.hour

    def mk(rule: str, severity: str, lead: int, bucket: int, value: float,
           window_h: int, title: str, body: str) -> Alert:
        date, hour = _local_time(lead)
        return Alert(rule, severity, lead, bucket, value, window_h, title, body,
                     local_date=date, local_hour=hour, cell=cell)

    out: list[Alert] = []

    # ---- rain: a 6 h total
    if rules["rain"]:
        best: Alert | None = None
        for i in range(len(series)):
            chunk = _consecutive_run(series, i, 6)
            if not chunk:
                continue
            vals = [_num(h.get("precip")) for h in chunk]
            if any(v is None for v in vals):
                continue
            total = sum(vals)
            if total < th.rain_6h_mm:
                continue
            lead = int(chunk[0]["step_h"])
            if lead < th.min_lead_h:
                continue
            sev = "severe" if total >= th.rain_6h_mm_severe else "warn"
            cand = mk("rain", sev, lead, i, round(total, 1), 6,
                      "🌧️ Σημαντική βροχή",
                      f"Έως {total:.0f} mm βροχής σε 6 ώρες, σε περίπου {lead} ώρες.")
            if best is None or cand.value > best.value:
                best = cand
        if best:
            out.append(best)

    # ---- storm: CAPE and a triggering hourly rate, both required.
    # Where the model gives no CAPE the rule does not run at all. Substituting a
    # guess would present a condition as a forecast, which this app does not do.
    if rules["storm"]:
        best = None
        for i in range(len(series)):
            chunk = _consecutive_run(series, i, 3)
            if not chunk:
                continue
            capes = [_num(h.get("cape")) for h in chunk]
            if any(v is None for v in capes):
                continue
            cape = max(capes)
            if cape < th.cape_jkg:
                continue
            prs = [_num(h.get("precip")) or 0.0 for h in chunk]
            if max(prs) < th.storm_pr_mmh:
                continue
            lead = int(chunk[0]["step_h"])
            if lead < th.min_lead_h:
                continue
            sev = "severe" if cape >= th.cape_jkg_severe else "warn"
            cand = mk("storm", sev, lead, i, round(cape), 3,
                      "⛈️ Πιθανή καταιγίδα",
                      f"Ασταθής ατμόσφαιρα (CAPE {cape:.0f} J/kg) με έντονη βροχή "
                      f"σε περίπου {lead} ώρες. Είναι ένδειξη συνθηκών, όχι βεβαιότητα.")
            if best is None or cand.value > best.value:
                best = cand
        if best:
            out.append(best)

    # ---- wind: gust where available, else sustained wind
    if rules["wind"]:
        best = None
        for h in series:
            g = _num(h.get("gust"))
            w = _num(h.get("wind"))
            v = g if g is not None else w
            if v is None or v < th.gust_kmh:
                continue
            lead = int(h["step_h"])
            if lead < th.min_lead_h:
                continue
            sev = "severe" if v >= th.gust_kmh_severe else "warn"
            what = "ριπές" if g is not None else "άνεμος"
            cand = mk("wind", sev, lead, lead, round(v), 1,
                      "💨 Ισχυρός άνεμος",
                      f"Ισχυρός {what} έως {v:.0f} km/h σε περίπου {lead} ώρες.")
            if best is None or cand.value > best.value:
                best = cand
        if best:
            out.append(best)

    # ---- temperature: the day's extremes
    if rules["temp"]:
        temps = [(_num(h.get("t")), int(h["step_h"])) for h in series]
        feels = [(_num(h.get("feels")), int(h["step_h"])) for h in series]
        hot = [(v, s) for v, s in temps if v is not None and v >= th.hot_c]
        feels_hot = [(v, s) for v, s in feels if v is not None and v >= th.feels_c]
        cold = [(v, s) for v, s in temps if v is not None and v <= th.cold_c]
        pick = None
        if hot:
            pick = ("heat", max(hot), f"θερμοκρασία {max(hot)[0]:.0f}°C")
        if feels_hot and (pick is None or max(feels_hot)[0] > pick[1][0]):
            pick = ("heat", max(feels_hot), f"αίσθηση {max(feels_hot)[0]:.0f}°C")
        if cold and pick is None:
            pick = ("cold", min(cold), f"θερμοκρασία {min(cold)[0]:.0f}°C")
        if pick:
            kind, (val, step), desc = pick
            if step >= th.min_lead_h:
                title = "🌡️ Ακραία ζέστη" if kind == "heat" else "🌡️ Ακραίο κρύο"
                out.append(mk("temp", "warn", step, step, round(val), 24, title,
                              f"Αναμένεται {desc} σε περίπου {step} ώρες."))

    return out


def in_quiet_hours(hour: int | None, th: Thresholds) -> bool:
    if hour is None:
        return False
    frm, to = th.quiet_from, th.quiet_to
    if frm == to:
        return False
    if frm < to:
        return frm <= hour < to
    return hour >= frm or hour < to


# ---------------------------------------------------------------- dedupe + delivery

def claim(subject: str, alert: Alert) -> str:
    """Try to take ownership of one alert occurrence.

    Returns one of:
      "claimed"  - this cycle owns it and must attempt a push (row is `pending`)
      "pending"  - a previous attempt is owed a retry, and the backoff has elapsed
      "waiting"  - admitted but still inside the retry backoff; try next cycle
      "sent"     - already delivered, or retries exhausted; never send again
      "cooling"  - inside the rule's cooldown, or over the daily cap

    The pending/sent split is what makes delivery at-least-once without an
    unbounded retry: a row that is `pending` is retried after a backoff, up to
    `retry_max` attempts, then marked `failed` and left alone.
    """
    th = load_thresholds()
    init_db()
    now = _now()
    with _connect() as con:
        row = con.execute(
            "SELECT status, attempts, last_attempt_at FROM notify_sent "
            "WHERE subject=? AND event_key=?", (subject, alert.key)).fetchone()
        if row is None:
            # Cooldown / daily cap are checked before the insert so a suppressed
            # alert does not occupy the event key (a later cycle within the same
            # window should still be able to fire once the cooldown clears).
            if _in_cooldown(con, subject, alert, th, now):
                return "cooling"
            if _over_daily_cap(con, subject, th, now):
                return "cooling"
            con.execute(
                """INSERT INTO notify_sent
                       (subject, event_key, rule, severity, status, attempts,
                        lead_h, fired_at)
                   VALUES (?,?,?,?,'pending',0,?,?)""",
                (subject, alert.key, alert.rule, alert.severity, alert.lead_h, _iso(now)))
            return "claimed"

        if row["status"] == "sent":
            return "sent"
        if row["status"] == "failed":
            return "sent"          # exhausted; do not resurrect
        # pending: retry only after the backoff, and only while attempts remain.
        if int(row["attempts"] or 0) >= th.retry_max:
            con.execute("UPDATE notify_sent SET status='failed' WHERE subject=? AND event_key=?",
                        (subject, alert.key))
            return "sent"
        last = _parse_iso(row["last_attempt_at"])
        if last is not None:
            waited = (now - last).total_seconds() / 60.0
            if waited < th.retry_backoff_min:
                return "waiting"
        return "pending"


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _in_cooldown(con, subject: str, alert: Alert, th: Thresholds,
                 now: dt.datetime) -> bool:
    hours = th.cooldown_h.get(alert.rule, 0)
    if hours <= 0:
        return False
    cutoff = _iso(now - dt.timedelta(hours=hours))
    row = con.execute(
        "SELECT 1 FROM notify_sent WHERE subject=? AND rule=? AND status='sent' "
        "AND fired_at >= ? LIMIT 1", (subject, alert.rule, cutoff)).fetchone()
    return row is not None


def _over_daily_cap(con, subject: str, th: Thresholds, now: dt.datetime) -> bool:
    if th.max_per_day <= 0:
        return False
    day = now.strftime("%Y-%m-%d")
    row = con.execute(
        "SELECT COUNT(*) c FROM notify_sent WHERE subject=? AND status='sent' "
        "AND fired_at >= ?", (subject, day + "T00:00:00Z")).fetchone()
    return int(row["c"] or 0) >= th.max_per_day


def mark_sent(subject: str, event_key: str) -> None:
    with _connect() as con:
        con.execute(
            """UPDATE notify_sent SET status='sent', sent_at=?, attempts=attempts+1,
                   last_attempt_at=?, last_error=NULL
               WHERE subject=? AND event_key=?""",
            (_iso(), _iso(), subject, event_key))


def mark_attempt_failed(subject: str, event_key: str, error: str) -> str:
    """Record a failed attempt. Returns the resulting status."""
    th = load_thresholds()
    with _connect() as con:
        row = con.execute(
            "SELECT attempts FROM notify_sent WHERE subject=? AND event_key=?",
            (subject, event_key)).fetchone()
        attempts = int(row["attempts"] or 0) + 1 if row else 1
        status = "failed" if attempts >= th.retry_max else "pending"
        con.execute(
            """UPDATE notify_sent SET status=?, attempts=?, last_attempt_at=?,
                   last_error=? WHERE subject=? AND event_key=?""",
            (status, attempts, _iso(), (error or "")[:200], subject, event_key))
    return status


def drop_dead_subscription(subject: str, error: str) -> None:
    """A push service answering 404/410 means the subscription no longer exists."""
    with _connect() as con:
        con.execute(
            """UPDATE notify_subs SET active=0, last_error=?, fail_count=fail_count+1,
                   updated_at=? WHERE subject=?""",
            ((error or "")[:200], _iso(), subject))


def record_run(evaluated: int, sent: int, skipped: int, errors: int,
               detail: dict | None = None) -> None:
    try:
        init_db()
        with _connect() as con:
            con.execute(
                "INSERT INTO notify_runs (ts, evaluated, sent, skipped, errors, detail) "
                "VALUES (?,?,?,?,?,?)",
                (_iso(), evaluated, sent, skipped, errors,
                 json.dumps(detail) if detail else None))
    except Exception as e:
        log.warning("notify: could not record run: %s", type(e).__name__)


def last_run() -> dict | None:
    try:
        init_db()
        with _connect() as con:
            row = con.execute(
                "SELECT * FROM notify_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def purge_sent(days: int = 30) -> int:
    """Drop dedupe rows older than `days`. Idempotent; called from the loop."""
    if days <= 0:
        return 0
    cutoff = _iso(_now() - dt.timedelta(days=days))
    with _connect() as con:
        cur = con.execute("DELETE FROM notify_sent WHERE fired_at < ?", (cutoff,))
    return cur.rowcount


def send_push(sub: dict, alert: Alert) -> tuple[bool, str | None]:
    """Deliver one alert. Returns (ok, error-or-None).

    The OS notification `tag` is the event key, so even the at-least-once window
    cannot stack two visible notifications for one event - the second replaces
    the first.
    """
    mod = webpush_module()
    if mod is None:
        return False, "package_missing"
    payload = {
        "title": alert.title,
        "body": alert.body,
        "tag": alert.key,
        "url": sub.get("place_name") or None,
        "severity": alert.severity,
    }
    try:
        mod.webpush(
            subscription_info={"endpoint": sub["endpoint"],
                               "keys": {"p256dh": sub["p256dh"],
                                        "auth": sub["auth"]}},
            data=json.dumps(payload),
            vapid_private_key=vapid_private_key(),
            vapid_claims={"sub": vapid_subject()},
            timeout=10,
        )
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"


def dispatch_once(subs: list[dict], series_for, *,
                  run_utc: str | None = None, tz: dt.tzinfo | None = None,
                  th: Thresholds | None = None) -> dict:
    """One pass over `subs`. `series_for(sub)` returns the hourly series.

    Split from the loop so it can be tested with a fake `series_for` and a fake
    push: no network, no clock, no real subscription.
    """
    th = th or load_thresholds()
    counts = {"evaluated": 0, "sent": 0, "skipped": 0, "errors": 0}
    for sub in subs:
        counts["evaluated"] += 1
        try:
            hours = series_for(sub)
            if not hours:
                counts["skipped"] += 1
                continue
            for alert in evaluate(hours, run_utc=run_utc, tz=tz,
                                  rules=sub.get("rules"), th=th,
                                  cell=cell_label(sub.get("cell_lat"), sub.get("cell_lon"))):
                # Quiet hours is checked before the claim, so a suppressed alert
                # leaves no row behind. If the same event later escalates to
                # severe it can then be claimed and sent, instead of being
                # blocked forever by a stale "already handled" record.
                if alert.severity != "severe" and in_quiet_hours(alert.local_hour, th):
                    counts["skipped"] += 1
                    continue
                state = claim(sub["subject"], alert)
                if state in ("sent", "cooling", "waiting"):
                    counts["skipped"] += 1
                    continue
                # "claimed" or "pending": a push is owed this cycle.
                ok, err = send_push(sub, alert)
                if ok:
                    mark_sent(sub["subject"], alert.key)
                    counts["sent"] += 1
                else:
                    status = mark_attempt_failed(sub["subject"], alert.key, err or "")
                    if status == "failed":
                        drop_dead_subscription(sub["subject"], err or "send failed")
                    counts["errors"] += 1
        except Exception as e:  # one bad subscriber must not stop the pass
            counts["errors"] += 1
            log.warning("notify: subscriber %s failed: %s: %s",
                        str(sub.get("subject"))[:12], type(e).__name__, e)
    record_run(counts["evaluated"], counts["sent"], counts["skipped"],
               counts["errors"])
    return counts


# ---------------------------------------------------------------- pro cache

def refresh_pro_cache(sub, *, now: float | None = None, billing=None) -> bool:
    """Refresh the cached PRO window for one subscription holder.

    The background pass has no HTTP request to ask `effective_entitlement()`
    about, so it works from `pro_until_cached`. For a subscription holder the
    window is re-checked against Stripe on a schedule; a promo or passcode window
    is longer-lived than the token, so the cached value is trusted until it
    passes. A subscription whose lookup fails keeps its previous cache rather
    than losing access on a five-minute Stripe blip - matching the fail policy in
    `effective_entitlement`.
    """
    if billing is None:
        import billing as billing  # local import: billing is optional
    interval = _envi("WX_NOTIFY_PRO_REFRESH_S", 6 * 3600)
    now = now if now is not None else time.time()
    sub_id = sub.get("subscription_id")
    if not sub_id:
        return False
    if sub.get("pro_until_cached") and sub.get("updated_at"):
        upd = _parse_iso(sub.get("updated_at"))
        if upd is not None and (now - upd.timestamp()) < interval:
            return False
    try:
        access = billing.subscription_access(sub_id)
    except Exception as e:
        log.warning("notify: pro cache refresh failed for %s: %s: %s",
                    str(sub.get("subject"))[:12], type(e).__name__, e)
        return False
    if access.get("status") == "unknown":
        return False
    until = int(access.get("until") or 0) if access.get("active") else 0
    set_pro_cache(sub["subject"], until or None, sub_id)
    return True


def _pro_active(sub) -> bool:
    until = sub.get("pro_until_cached")
    if not until:
        return False
    return int(until) > time.time()


# ---------------------------------------------------------------- loop

INTERVAL_ENV = "WX_NOTIFY_INTERVAL_S"
BATCH_ENV = "WX_NOTIFY_BATCH"
RETENTION_ENV = "WX_NOTIFY_RETENTION_DAYS"


def interval_s() -> int:
    """Seconds between passes. 0 disables the loop entirely (kill switch)."""
    return _envi(INTERVAL_ENV, 1800)


def batch_size() -> int:
    return max(1, _envi(BATCH_ENV, 500))


def retention_days() -> int:
    return _envi(RETENTION_ENV, 30)


async def run_forever(interval: int | None = None, *, prime=None,
                      series_for=None, billing=None) -> None:
    """Background pass. Never dies; records a run row and logs each cycle.

    `prime` is an async callable supplied by app.py: given the eligible
    subscriptions it returns ``{cell: hourly_series}``, so one set of model reads
    serves every subscriber in the same cell. A missing entry means "no data",
    and that subscriber is skipped rather than alerted on nothing. `series_for`
    remains available as a plain sync fallback, which is what the tests use.
    """
    interval = interval if interval is not None else interval_s()
    while True:
        try:
            subs = active_subscriptions(batch_size())
            for sub in subs:
                refresh_pro_cache(sub, billing=billing)
            eligible = [s for s in subs if _pro_active(s)]
            th = load_thresholds()
            primed: dict = {}
            if eligible and prime is not None:
                try:
                    primed = await prime(eligible) or {}
                except Exception as e:  # a data outage must not kill the loop
                    log.warning("notify: series build failed: %s: %s",
                                type(e).__name__, e)
                    primed = {}
            if eligible and (primed or series_for is not None):
                def one(sub, _p=primed):
                    key = cell_label(sub.get("cell_lat"), sub.get("cell_lon"))
                    if key in _p:
                        return _p[key]
                    if series_for is not None:
                        return series_for(sub)
                    return None

                counts = dispatch_once(eligible, one, tz=_tz(), th=th)
                log.info("notify: pass evaluated=%d sent=%d skipped=%d errors=%d",
                         counts["evaluated"], counts["sent"], counts["skipped"],
                         counts["errors"])
            else:
                record_run(len(subs), 0, len(subs), 0,
                           {"reason": "no_eligible_subs" if subs else "no_subs"})
            purge_sent(retention_days())
            _prune_dead()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # the loop must survive anything
            log.exception("notify: pass crashed: %s", e)
        await asyncio.sleep(interval)


def _tz():
    try:
        import astro
        return astro._tz()
    except Exception:
        return dt.timezone.utc


def _prune_dead(older_days: int = 30) -> int:
    cutoff = _iso(_now() - dt.timedelta(days=older_days))
    try:
        with _connect() as con:
            cur = con.execute(
                "DELETE FROM notify_subs WHERE active=0 AND updated_at < ?", (cutoff,))
        return cur.rowcount
    except Exception:
        return 0


def start(*, prime=None, series_for=None, billing=None) -> asyncio.Task | None:
    """Start the background pass, unless push is unconfigured or disabled.

    Returns the task so shutdown can cancel it, and None when the feature is off
    - so importing this module never starts network traffic by itself, matching
    `scheduler.start()`.
    """
    if interval_s() <= 0:
        log.info("%s=0; notification loop not started", INTERVAL_ENV)
        return None
    if not push_available():
        log.info("push is not configured (%s); notification loop not started",
                 unavailable_reason())
        return None
    return asyncio.create_task(run_forever(prime=prime, series_for=series_for,
                                           billing=billing))
