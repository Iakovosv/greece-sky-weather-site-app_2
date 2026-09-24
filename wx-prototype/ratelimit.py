"""In-process token-bucket rate limiting, keyed by client.

Why a token bucket and not a fixed window
-----------------------------------------
A fixed window (N requests per minute, counter reset on the minute) lets a client
spend the full allowance at 12:00:59 and again at 12:01:00 — twice the intended
rate at a boundary, which is exactly when a burst hurts. A token bucket refills
smoothly, so the limit is a rate rather than a per-window quota.

Why in-process
--------------
One uvicorn process is the deployment (the RAM grids are shared process-wide and
would be duplicated per worker). A single dict of buckets is therefore correct
and needs no Redis. If the service ever runs multiple workers, each keeps its own
bucket and the effective limit multiplies by the worker count — noted here so the
trade-off is explicit rather than discovered.

What is limited, and by how much
--------------------------------
The numbers differ per endpoint because the *cost* differs by three orders of
magnitude. A `/api/brief` from RAM is arithmetic; from cold cache it is a hundred
NOMADS fetches. `/api/verify` is an ERA5 chunk per hour plus a range request per
lead. Registration and passcode attempts are cheap to serve and expensive to get
wrong, so they are limited hardest.
"""
from __future__ import annotations

import ipaddress
import logging
import threading
import time

log = logging.getLogger("wx.ratelimit")


class Limits:
    """Tokens per second, and the largest burst allowed. Tuned per endpoint."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.burst = burst


# A normal forecast: generous, because a returning user re-renders on every
# location change and the whole point is that it is cheap when cached.
FORECAST = Limits(rate=1.0, burst=30)
# Verification is the expensive one: one ERA5 chunk per hour in the window.
VERIFY = Limits(rate=1.0 / 20.0, burst=3)
# Promo redemption and passcode entry: a human types these. Limited hard enough
# that guessing a code is impractical, loose enough that a typo costs one retry.
REDEEM = Limits(rate=1.0 / 15.0, burst=5)
# Geocoding: each miss is an outbound call to Photon, a free shared service.
GEOCODE = Limits(rate=0.5, burst=10)
# Station ingest: an Ecowitt gateway pushes every 60 s; a station is one client.
STATION = Limits(rate=1.0, burst=30)
# Analytics ingest: a page sends one batch on unload, so a human produces a
# handful per session. Generous enough that a fast navigation is not throttled,
# tight enough that the endpoint cannot be used to write rows without bound —
# which is the one thing here an anonymous caller can grow.
ANALYTICS = Limits(rate=0.5, burst=20)
# Everything else, including the free astronomy card.
DEFAULT = Limits(rate=2.0, burst=60)


class Bucket:
    __slots__ = ("tokens", "last")

    def __init__(self, tokens: float) -> None:
        self.tokens = tokens
        self.last = time.monotonic()


class Limiter:
    """Thread-safe token buckets, one per (key, class)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], Bucket] = {}
        self._last_sweep = time.monotonic()

    def allow(self, key: str, limit: Limits, cost: float = 1.0,
              now: float | None = None) -> tuple[bool, float]:
        """Consume `cost` tokens. Returns (allowed, seconds_until_next_token).

        The retry hint is what turns a 429 into something a client can act on:
        it is included in the `Retry-After` header and in the JSON body.
        """
        now = now if now is not None else time.monotonic()
        bkey = (key, f"{limit.rate}:{limit.burst}")
        with self._lock:
            self._maybe_sweep(now)
            b = self._buckets.get(bkey)
            if b is None:
                b = Bucket(float(limit.burst))
                # The clock is `now`, not the moment the object was constructed:
                # a bucket created a microsecond later than `now` would compute a
                # negative elapsed time and refill to just under the burst, so the
                # very first request would be refused.
                b.last = now
                self._buckets[bkey] = b
            b.tokens = min(float(limit.burst), b.tokens + (now - b.last) * limit.rate)
            b.last = now
            if b.tokens >= cost:
                b.tokens -= cost
                return True, 0.0
            deficit = cost - b.tokens
            return False, deficit / limit.rate if limit.rate > 0 else 1.0

    def _maybe_sweep(self, now: float) -> None:
        """Drop buckets that have not been touched for a while.

        Without this the dict grows with the number of distinct client keys seen
        since boot — a slow leak on a long-running process, and an easy one to
        trigger if a client can invent keys (which it can, when proxy headers are
        trusted). A bucket that is full and idle carries no state worth keeping.
        """
        if now - self._last_sweep < 600:
            return
        self._last_sweep = now
        stale = [k for k, b in self._buckets.items() if (now - b.last) > 3600]
        for k in stale:
            self._buckets.pop(k, None)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


LIMITER = Limiter()


def client_key(request, trust_proxy: bool) -> str:
    """A stable identifier for the caller.

    Prefers the entitlement token when one is presented: it is a signed identity,
    so a caller cannot mint a fresh one per request to get a fresh bucket. Falls
    back to the socket peer, and only trusts `X-Forwarded-For` when the deployment
    says a proxy sets it — a server that trusts the header unconditionally lets a
    client evade every IP-based limit by inventing a new address per request.
    """
    token = request.headers.get("x-wx-token") or request.query_params.get("token")
    if token:
        # The token is a credential; hash it so the limiter never holds the value.
        import hashlib
        return "tok:" + hashlib.sha256(token.encode()).hexdigest()[:16]
    if trust_proxy:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            first = fwd.split(",")[0].strip()
            if _valid_ip(first):
                return "ip:" + first
    peer = request.client.host if request.client else "unknown"
    return "ip:" + peer


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False
