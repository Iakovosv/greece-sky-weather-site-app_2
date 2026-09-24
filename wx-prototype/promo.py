"""Promo / gift codes granting temporary PRO.

The rule this file exists to enforce
------------------------------------
A code must never be a frontend flag. It grants a server-side entitlement that is
evaluated exactly like a paid subscription: the endpoint that serves PRO data
asks the same question ("is there an active PRO entitlement for this caller?")
regardless of whether the answer came from Stripe or from a code, and it is the
server that answers. Deleting `localStorage`, editing a date in devtools, or
setting a JS variable changes nothing, because none of those reach the decision.

Storage
-------
SQLite, in the database `bias.py` already uses (`WX_DB`). Adding a second store
would mean a second backup target and a second failure mode for no benefit; the
code, the redemption record and the station telemetry are all small, and the
existing `_db()` context manager already sets WAL and a busy timeout.

Model
-----
`promo_codes` is the definition; `promo_redemptions` is the append-only record.
The redemption table is what makes "already used by this device" and
"max_redemptions reached" answerable, and it is also the audit trail an operator
needs when a code is disputed. It stores an opaque device id and the code — the
same device id lives in the caller's signed token — and deliberately **not** an
IP address, a user agent or a location.

Expiry semantics
----------------
The window a code grants starts when it is *redeemed*, not when it is created.
A "5 day" code handed to a friend who activates it next week gives five days from
activation. The definition can additionally carry a `starts_at`/`expires_at`
window after which redemption is refused.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import secrets
import sqlite3
import string
import threading
import time

import config

log = logging.getLogger("wx.promo")

# Ambiguous characters (0/O, 1/I/L) are excluded: these codes are read aloud and
# typed by hand, and a support ticket about "is that a one or an ell" is avoidable.
CODE_ALPHABET = string.ascii_uppercase + string.digits
CODE_ALPHABET = "".join(c for c in CODE_ALPHABET if c not in "O0I1L")

MIN_DAYS, MAX_DAYS = 1, 3650          # ten years is the ceiling; beyond is a typo
MAX_REDEMPTIONS_CEILING = 1_000_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS promo_codes (
    code            TEXT PRIMARY KEY,
    duration_days   INTEGER NOT NULL,
    created_at      TEXT    NOT NULL,
    created_by      TEXT,
    note            TEXT,
    max_redemptions INTEGER,            -- NULL = unlimited
    redemption_count INTEGER NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1,
    starts_at       TEXT,               -- NULL = valid immediately
    expires_at      TEXT,               -- NULL = no global expiry
    restricted_to   TEXT,               -- NULL = anyone; else a device/user id
    is_gift         INTEGER NOT NULL DEFAULT 0,
    -- The bearer a personal code is restricted to, when known at creation time. A
    -- device id is minted by the server on first contact and carried in the signed
    -- token, so an operator can issue a code for a specific device without an
    -- accounts table existing.
    intended_subject TEXT
);
CREATE TABLE IF NOT EXISTS promo_redemptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    subject     TEXT NOT NULL,          -- opaque device id from the signed token
    redeemed_at TEXT NOT NULL,
    pro_until   TEXT NOT NULL,          -- when the granted entitlement ends
    source      TEXT,                   -- where it was redeemed from, e.g. "web"
    UNIQUE (code, subject)              -- one redemption per code per subject
);
CREATE INDEX IF NOT EXISTS idx_promo_red_subject ON promo_redemptions (subject);
CREATE INDEX IF NOT EXISTS idx_promo_red_code    ON promo_redemptions (code);
"""

_LOCAL = threading.local()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime | None = None) -> str:
    return (t or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def _connect() -> sqlite3.Connection:
    path = config.db_path()
    parent = os.path.dirname(path)
    # `os.path.dirname("")` is "", and `makedirs("")` raises. The old bias._db did
    # exactly that, so a bare `WX_DB=station.db` crashed the app on first ingest.
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db() -> None:
    with _connect() as con:
        con.executescript(_SCHEMA)


# ---------------------------------------------------------------- code handling

def normalize(code: str | None) -> str:
    """Upper-case, trim, and drop the separators people add when copying.

    `  friend-5 ` and `FRIEND5` are the same code: a hyphen or a space typed from
    a screenshot should not read as "invalid code".
    """
    if not code:
        return ""
    return "".join(ch for ch in code.strip().upper() if ch not in " -_")


def looks_valid(code: str) -> bool:
    return 3 <= len(code) <= 64 and all(ch.isalnum() for ch in code)


def generate_code(prefix: str = "", length: int = 8) -> str:
    """A random code body. The prefix is the operator's own label, passed through.

    Only the random body is drawn from CODE_ALPHABET. A prefix like "GIFT" is used
    verbatim — sanitising it would silently rename a code the operator is about to
    write down.
    """
    body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(max(4, length)))
    return normalize(prefix) + body


def create_code(code: str, duration_days: int, *, created_by: str | None = None,
                note: str | None = None, max_redemptions: int | None = None,
                active: bool = True, starts_at: str | None = None,
                expires_at: str | None = None, restricted_to: str | None = None,
                is_gift: bool = False) -> dict:
    """Create or replace a code definition. Raises ValueError on bad input.

    Validation is here rather than in the endpoint so it cannot be bypassed by
    calling the function directly, and so an admin creating a code gets the same
    rules the public redemption path depends on.
    """
    code = normalize(code)
    if not looks_valid(code):
        raise ValueError("code must be 3-64 alphanumeric characters (no spaces)")
    try:
        days = int(duration_days)
    except (TypeError, ValueError):
        raise ValueError("duration_days must be an integer")
    if not (MIN_DAYS <= days <= MAX_DAYS):
        raise ValueError(f"duration_days must be between {MIN_DAYS} and {MAX_DAYS}")
    if max_redemptions is not None:
        try:
            max_redemptions = int(max_redemptions)
        except (TypeError, ValueError):
            raise ValueError("max_redemptions must be an integer or null")
        if max_redemptions < 1 or max_redemptions > MAX_REDEMPTIONS_CEILING:
            raise ValueError(f"max_redemptions must be between 1 and {MAX_REDEMPTIONS_CEILING}")
    if starts_at and not parse_ts(starts_at):
        raise ValueError("starts_at must be an ISO date/datetime")
    if expires_at and not parse_ts(expires_at):
        raise ValueError("expires_at must be an ISO date/datetime")
    if starts_at and expires_at and parse_ts(starts_at) > parse_ts(expires_at):
        raise ValueError("starts_at is after expires_at")
    restricted_to = (restricted_to or "").strip() or None

    with _connect() as con:
        con.execute(
            """INSERT INTO promo_codes
                   (code, duration_days, created_at, created_by, note, max_redemptions,
                    redemption_count, active, starts_at, expires_at, restricted_to, is_gift)
               VALUES (?,?,?,?,?,?,0,?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                    duration_days=excluded.duration_days,
                    created_by=excluded.created_by,
                    note=excluded.note,
                    max_redemptions=excluded.max_redemptions,
                    active=excluded.active,
                    starts_at=excluded.starts_at,
                    expires_at=excluded.expires_at,
                    restricted_to=excluded.restricted_to,
                    is_gift=excluded.is_gift""",
            (code, days, _iso(), created_by, note, max_redemptions,
             1 if active else 0, starts_at, expires_at, restricted_to, 1 if is_gift else 0))
    log.info("promo code created: code=%s days=%d max=%s active=%s",
             code, days, max_redemptions, active)
    return get_code(code) or {}


def get_code(code: str) -> dict | None:
    with _connect() as con:
        row = con.execute("SELECT * FROM promo_codes WHERE code=?",
                          (normalize(code),)).fetchone()
    return dict(row) if row else None


def set_active(code: str, active: bool) -> bool:
    """Deactivate (revoke) or reactivate a code. Returns whether it existed."""
    with _connect() as con:
        cur = con.execute("UPDATE promo_codes SET active=? WHERE code=?",
                          (1 if active else 0, normalize(code)))
    if cur.rowcount:
        log.info("promo code %s: active=%s", normalize(code), active)
    return bool(cur.rowcount)


def list_codes(include_inactive: bool = True) -> list[dict]:
    q = "SELECT * FROM promo_codes"
    if not include_inactive:
        q += " WHERE active=1"
    q += " ORDER BY created_at DESC"
    with _connect() as con:
        return [dict(r) for r in con.execute(q).fetchall()]


def redemptions(code: str | None = None, limit: int = 200) -> list[dict]:
    with _connect() as con:
        if code:
            rows = con.execute(
                "SELECT * FROM promo_redemptions WHERE code=? "
                "ORDER BY redeemed_at DESC LIMIT ?", (normalize(code), limit)).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM promo_redemptions ORDER BY redeemed_at DESC LIMIT ?",
                (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- redemption

class RedemptionError(Exception):
    """A refusal the caller should show verbatim, with a machine-readable code."""

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _evaluate(row: dict, subject: str | None, now: dt.datetime) -> None:
    """Raise RedemptionError unless this definition may be redeemed by `subject`."""
    if not row.get("active"):
        raise RedemptionError("inactive", "Ο κωδικός δεν είναι ενεργός.", 410)
    starts = parse_ts(row.get("starts_at"))
    if starts and now < starts:
        raise RedemptionError("not_started", "Ο κωδικός δεν έχει ενεργοποιηθεί ακόμη.", 409)
    expires = parse_ts(row.get("expires_at"))
    if expires and now >= expires:
        raise RedemptionError("expired", "Ο κωδικός έχει λήξει.", 410)
    maxr = row.get("max_redemptions")
    if maxr is not None and int(row.get("redemption_count") or 0) >= int(maxr):
        raise RedemptionError("exhausted", "Ο κωδικός έχει εξαντληθεί.", 410)
    restricted = (row.get("restricted_to") or "").strip()
    if restricted:
        if not subject:
            raise RedemptionError("identify", "Ο κωδικός είναι προσωπικός και δεν "
                                  "μπορεί να ταυτοποιηθεί αυτή η συσκευή.", 403)
        if subject != restricted:
            raise RedemptionError("not_you", "Ο κωδικός είναι προσωπικός και δεν "
                                  "προορίζεται για αυτή τη συσκευή.", 403)


def redeem(code: str, subject: str | None, source: str | None = None) -> dict:
    """Redeem a code for `subject`. Returns the granted window.

    The whole check-and-write runs in one transaction with the code row locked by
    the write, so two simultaneous redemptions of the last remaining use cannot
    both succeed. That race is the reason the counting column is updated with a
    conditional `WHERE redemption_count < max_redemptions` rather than a
    read-then-write.
    """
    code = normalize(code)
    if not code or not looks_valid(code):
        raise RedemptionError("malformed", "Μη έγκυρος κωδικός.", 400)

    now = _now()
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")   # take the write lock before reading
        row = con.execute("SELECT * FROM promo_codes WHERE code=?", (code,)).fetchone()
        if row is None:
            raise RedemptionError("unknown", "Μη έγκυρος ή ληγμένος κωδικός.", 404)
        row = dict(row)
        _evaluate(row, subject, now)

        if subject:
            dup = con.execute(
                "SELECT pro_until FROM promo_redemptions WHERE code=? AND subject=?",
                (code, subject)).fetchone()
            if dup:
                # A second attempt by the same device is refused, but the window it
                # already holds is returned so the UI can say "already active until".
                raise RedemptionError(
                    "already_used",
                    "Ο κωδικός έχει ήδη χρησιμοποιηθεί σε αυτή τη συσκευή.", 409)
        else:
            dup = None

        # The conditional update is the atomic "did I get the last use?" test.
        cur = con.execute(
            """UPDATE promo_codes
                  SET redemption_count = redemption_count + 1
                WHERE code=?
                  AND active=1
                  AND (max_redemptions IS NULL OR redemption_count < max_redemptions)""",
            (code,))
        if cur.rowcount == 0:
            raise RedemptionError("exhausted", "Ο κωδικός έχει εξαντληθεί.", 410)

        days = int(row["duration_days"])
        until = now + dt.timedelta(days=days)
        con.execute(
            """INSERT INTO promo_redemptions (code, subject, redeemed_at, pro_until, source)
               VALUES (?,?,?,?,?)""",
            (code, subject or "anonymous", _iso(now), _iso(until), source))
        con.commit()
    except RedemptionError:
        con.rollback()
        raise
    except sqlite3.IntegrityError:
        # The UNIQUE(code, subject) constraint fired between our check and insert.
        con.rollback()
        raise RedemptionError("already_used",
                              "Ο κωδικός έχει ήδη χρησιμοποιηθεί σε αυτή τη συσκευή.", 409)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    log.info("promo redeemed: code=%s days=%d until=%s", code, days, _iso(until))
    return {"code": code, "days": days, "pro_until": int(until.timestamp()),
            "pro_until_iso": _iso(until), "subject": subject}


def active_until(subject: str | None) -> int | None:
    """Latest `pro_until` this subject still holds, or None.

    This is the only query the entitlement path makes, and it is intentionally a
    timestamp comparison in Python rather than a `WHERE pro_until > now` in SQL:
    the stored format is the same ISO used everywhere else, and keeping the
    comparison in one place avoids a second date parser.
    """
    if not subject:
        return None
    try:
        with _connect() as con:
            rows = con.execute(
                "SELECT pro_until FROM promo_redemptions WHERE subject=?", (subject,)).fetchall()
    except sqlite3.Error as e:
        log.warning("promo lookup failed for subject: %s", e)
        return None
    now = _now()
    best: int | None = None
    for r in rows:
        t = parse_ts(r["pro_until"])
        if t and t > now:
            ts = int(t.timestamp())
            best = ts if best is None else max(best, ts)
    return best


def subject_redemptions(subject: str) -> list[dict]:
    """What one subject has redeemed — used to render "active until" honestly."""
    if not subject:
        return []
    with _connect() as con:
        rows = con.execute(
            "SELECT code, redeemed_at, pro_until FROM promo_redemptions "
            "WHERE subject=? ORDER BY redeemed_at DESC", (subject,)).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    """Counters for the admin view and /api/health."""
    try:
        with _connect() as con:
            codes = con.execute("SELECT COUNT(*) c FROM promo_codes").fetchone()["c"]
            active = con.execute(
                "SELECT COUNT(*) c FROM promo_codes WHERE active=1").fetchone()["c"]
            reds = con.execute("SELECT COUNT(*) c FROM promo_redemptions").fetchone()["c"]
    except sqlite3.Error:
        return {"available": False}
    return {"available": True, "codes": codes, "active_codes": active,
            "redemptions": reds}
