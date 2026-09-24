"""Runtime configuration: request bounds, lazy settings, and startup validation.

Why this module exists
----------------------
Two problems it solves, both of which were real bugs rather than tidiness:

1. **Settings were read at import time, before ``.env`` was loaded.**
   ``app.py`` calls ``envfile.load()`` after its imports, but ``wx``, ``bias`` and
   ``entitlements`` read their settings at *their* import time — which happens
   first. The result was that a ``WX_SECRET`` (or ``WX_CACHE_DIR``, or ``WX_DB``)
   set in ``.env`` landed in ``os.environ`` and was silently ignored, so a deploy
   that configured everything through the file still ran with the public
   development defaults. The readers below resolve from the environment on every
   call, so the order no longer matters.

2. **Request parameters had no bounds.** ``/api/brief?lat=999`` returned HTTP 200
   with plausible-looking numbers and fetched a whole global GRIB field, because
   NOMADS clamps an out-of-range sub-region to the full grid. One anonymous
   request produced a 419 MB cache file. The bounds here are the single source of
   truth for every endpoint that takes a coordinate.

The bounds are deliberately the *geographic* limits, not "Greece only": this
service is served continent-wide when RAM grids are in Europe scope, and the
per-point path subsets server-side anywhere in the world. Constraining to a
country box would remove a working capability.
"""
from __future__ import annotations

import logging
import math
import os

log = logging.getLogger("wx.config")


class ConfigError(RuntimeError):
    """A configuration that is unsafe to serve with."""


# ---------------------------------------------------------------- request bounds

LAT_MIN, LAT_MAX = -90.0, 90.0
LON_MIN, LON_MAX = -180.0, 180.0
# Longitude is not rejected when it falls outside +/-180: a client that sends
# 190 has named a real meridian 10 degrees east of the antimeridian, and the
# per-point path's `rightlon`/`leftlon` parameters accept values outside the
# conventional range. FastAPI's `le=180` would have refused it. Latitude has no
# such wrap, so it is bounded at the poles.

# Forecast hours. The upper bound is the PRO window: it is a sanity limit on the
# request, not a change to either tier. FREE and PRO remain 72 h and 240 h in
# entitlements.py, and the endpoint clamps to the caller's entitlement below this.
HOURS_MIN, HOURS_MAX = 1, 240

# Guard rails for the disk cache. Sized so a normal PRO run never evicts itself:
# one 240 h GFS surface series for a point is a few MB of decoded fields, and the
# 3 h TTL means the working set is bounded by the number of *distinct points*
# served in that window. 2 GiB is roughly a thousand such points. The cap exists
# to stop the unbounded growth the lat=999 bug produced, not to police normal use.
DEFAULT_CACHE_MAX_MB = 2048
CACHE_TTL_S = 3 * 3600

# Request body ceiling. Every POST here carries a small JSON/form payload: a
# promo code, a Stripe session id, one analytics batch, one Ecowitt reading. 1 MB
# is far above any legitimate one and far below what would let an anonymous
# caller force a large allocation or a slow parse per request.
DEFAULT_MAX_BODY_MB = 1


def max_body_bytes() -> int:
    """Hard ceiling for an incoming request body, in bytes. 0 disables the cap."""
    raw = _env("WX_MAX_BODY_MB")
    try:
        mb = int(raw)
    except (TypeError, ValueError):
        mb = DEFAULT_MAX_BODY_MB
    if mb <= 0:
        return 0
    return mb * 1024 * 1024


def finite_lat(value: float) -> bool:
    """True when `value` is a real latitude. NaN and infinity are not."""
    try:
        return math.isfinite(value) and LAT_MIN <= value <= LAT_MAX
    except (TypeError, ValueError):
        return False


def finite_lon(value: float) -> bool:
    """True when `value` is a real longitude (the full +/-180 range)."""
    try:
        return math.isfinite(value) and LON_MIN <= value <= LON_MAX
    except (TypeError, ValueError):
        return False


def coord_error(lat: float, lon: float) -> str | None:
    """A human-readable reason the coordinate is unusable, or None when it is fine."""
    if not finite_lat(lat):
        return f"lat must be a finite number between {LAT_MIN:.0f} and {LAT_MAX:.0f}"
    if not finite_lon(lon):
        return f"lon must be a finite number between {LON_MIN:.0f} and {LON_MAX:.0f}"
    return None


# ---------------------------------------------------------------- lazy settings

def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def cache_dir() -> str:
    """Directory for GRIB/NetCDF/result cache files.

    Resolved on every call so ``envfile.load()`` in ``app.py`` actually reaches
    it, and so a test can point it at a temporary directory.
    """
    return _env("WX_CACHE_DIR") or "/tmp/wx-cache"


def db_path() -> str:
    """SQLite file for station telemetry, forecast rows and (later) promo codes."""
    raw = _env("WX_DB") or os.path.join(cache_dir(), "station.db")
    return raw


def cache_max_bytes() -> int:
    """Hard ceiling for the cache directory. 0 or negative disables the cap."""
    raw = _env("WX_CACHE_MAX_MB")
    try:
        mb = int(raw)
    except (TypeError, ValueError):
        mb = DEFAULT_CACHE_MAX_MB
    if mb <= 0:
        return 0
    return mb * 1024 * 1024


def signing_secret() -> str:
    """The HMAC key for entitlement tokens.

    Raises rather than falling back to a default in production. The old behaviour
    — a public development string — meant that a forgotten ``WX_SECRET`` let any
    visitor mint a PRO token, because the key was in the source.
    """
    value = _env("WX_SECRET")
    if value:
        return value
    if is_production():
        raise ConfigError(
            "WX_SECRET must be set to a random value when WX_ENV=production. "
            "Generate one with: python3 -c \"import secrets;print(secrets.token_urlsafe(48))\"")
    return DEV_SECRET


# The development value is intentionally not secret-looking, and `signing_secret`
# refuses to use it in production. It exists so `pytest` and a laptop run work.
DEV_SECRET = "dev-only-insecure-secret-change-me"


def master_code() -> str:
    """The comp/test passcode that unlocks PRO, or "" when there is none.

    Unlike `WX_SECRET`, there is deliberately no usable default in production: a
    literal compiled into the source and printed in the README would let anyone
    mint a PRO token by copying it. Production must set the variable;
    `assert_production_ready()` refuses to start without it, and this returns ""
    so the passcode endpoint can never match in the meantime. The development
    value below exists only so `pytest` and a laptop run work.
    """
    value = _env("WX_MASTER_CODE")
    if value:
        return value
    if is_production():
        return ""
    return DEV_MASTER_CODE


# Development-only fallback. `master_code()` refuses to return it in production.
DEV_MASTER_CODE = "GSW-DEV-ONLY-CODE"


def env_name(fallback: str = "dev") -> str:
    return (_env("WX_ENV") or fallback).lower()


def is_production() -> bool:
    """Resolved on every call.

    Not read from the module-level `ENV` snapshot: that snapshot is taken while
    this module is imported, which happens before `app.py` calls ``envfile.load()``,
    so a `WX_ENV=production` in `.env` would not be seen by a decision that has to
    be right. The snapshot is kept only for logging.
    """
    return env_name() in ("prod", "production")


def trust_proxy_headers() -> bool:
    """Whether to believe X-Forwarded-For when identifying a client.

    Off by default: a directly-exposed server that trusts a client-supplied
    header would let anyone evade an IP-based rate limit by inventing a new
    address on every request. Turned on only when a reverse proxy is known to
    strip and set the header.
    """
    return _env("WX_TRUST_PROXY").lower() in ("1", "true", "yes", "on")


def rate_limit_enabled() -> bool:
    return _env("WX_RATE_LIMIT_DISABLED").lower() not in ("1", "true", "yes", "on")


# These are read once, at import. That is correct for `ENV` because
# `envfile.load()` runs before this module is imported (app.py imports it first),
# and because a "which environment am I" decision must not change mid-process.
ENV = env_name()


# ---------------------------------------------------------------- validation

def validate_runtime() -> list[str]:
    """Warnings about a configuration that is wrong but not fatal.

    Called once at startup. Returned (and logged by the caller) rather than
    raised, because each of these degrades one feature and the rest of the
    service must still come up: a missing webhook secret breaks activation, not
    the forecast.
    """
    problems: list[str] = []
    if not _env("WX_SECRET"):
        if is_production():
            # signing_secret() raises here; surface it as the startup warning too.
            problems.append("WX_SECRET is unset")
        else:
            problems.append("WX_SECRET is unset (using the development default)")
    if not _env("WX_MASTER_CODE") and is_production():
        # Non-fatal warning only outside production; in production
        # `assert_production_ready()` raises on the same condition, so this line
        # exists to name the problem in logs before that check runs.
        problems.append("WX_MASTER_CODE is unset: the passcode endpoint is disabled "
                        "in production (set it to enable comp/test access)")
    if not _env("WX_PUBLIC_BASE_URL") and _env("WX_STRIPE_SECRET_KEY"):
        problems.append("WX_PUBLIC_BASE_URL is unset while Stripe is configured; "
                        "checkout redirects will not work")
    if _env("WX_STRIPE_SECRET_KEY") and not _env("WX_STRIPE_WEBHOOK_SECRET"):
        problems.append("WX_STRIPE_WEBHOOK_SECRET is unset: subscriptions cannot be "
                        "activated through the webhook (the claim endpoint still works)")
    return problems


def assert_production_ready() -> None:
    """Fatal production preconditions. Raises `ConfigError` on the first failure.

    Separate from `validate_runtime()` because the two have different contracts:
    that one returns warnings for things that degrade a feature but let the
    service come up, this one refuses to start. A misconfiguration that would
    hand out PRO or allow forged tokens is not something to boot through.

    Called once from the app's startup hook, which runs after `envfile.load()`,
    so a value in `.env` is honoured. Outside production this is a no-op: the
    development defaults exist precisely so a laptop and `pytest` work.

    WX_MASTER_CODE is required in production for the same reason as WX_SECRET:
    the passcode is an entitlement source. Leaving it unset does not merely
    disable comp access, it also means the deploy silently differs from what the
    operator believed was configured, so it fails loudly instead.
    """
    if not is_production():
        return
    if not _env("WX_SECRET"):
        raise ConfigError(
            "WX_SECRET must be set when WX_ENV=production. Generate one with: "
            "python3 -c \"import secrets;print(secrets.token_urlsafe(48))\"")
    if not _env("WX_MASTER_CODE"):
        raise ConfigError(
            "WX_MASTER_CODE must be set when WX_ENV=production: it is the passcode "
            "that unlocks PRO. Set a random value, or unset WX_ENV if this is not "
            "a production deployment. There is no usable default.")


# ---------------------------------------------------------------- push (VAPID)

def vapid_public_key() -> str:
    """The VAPID public key handed to the browser. Safe to serve."""
    return _env("WX_VAPID_PUBLIC_KEY")


def vapid_private_key() -> str:
    """The VAPID private key. Never logged and never returned to a client.

    Read from the environment only, like the Stripe secret: there is no file
    fallback and no default, so a deploy that has not set it cannot accidentally
    sign with a public value.
    """
    return _env("WX_VAPID_PRIVATE_KEY")


def vapid_configured() -> bool:
    return bool(vapid_public_key() and vapid_private_key())
