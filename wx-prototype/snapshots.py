"""Secure server-side snapshot runtime.

Why this module exists
----------------------
The camera UI used to refresh an image by pointing the browser at a configured
snapshot URL. That is fine for a public URL an operator pasted, but it does not
survive contact with a real camera: a Hikvision/NVR still is only reachable
behind a server-side credential and an internal address, and neither may reach the
browser. So the fetch moves to the server, and this module is the only place that
is allowed to make it.

The security model, stated plainly
----------------------------------
* **No client-supplied URL, ever.** A request names a camera *id*; the id is
  looked up in the trusted registry (``cameras.source_for``) and the URL comes
  from there. There is no parameter anywhere on the public surface that a caller
  can use to name a host.
* **No redirects.** A redirect is reported, never followed. Following one would
  re-open SSRF: the first hop passes every check, and the second hop -- chosen by
  the upstream -- is a fresh URL that the allowlist never saw.
* **SSRF defence pins the address it validated.** A hostname check alone is not a
  defence: `evil.example` is a fine hostname that resolves to `127.0.0.1`. So the
  host is resolved once, every answer must be public unicast, and the first
  validated address is written into the request URL as a literal before the
  connection is made. httpx is handed an IP, not a name, so it performs no second
  lookup and there is no window in which a rebinding answer could swap a private
  address in between the check and the dial. This holds for both the public URL
  and the private-camera source; the only difference between them is whether the
  host must also be on ``WX_CAMERA_ALLOWED_HOSTS``.

Failure is generic on purpose. Every failure mode collapses to one of three
public errors (not found, unavailable, no image), so a client cannot use the
response to learn whether a particular internal host exists, is reachable, or is
slow.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx

import cameras as cams
import config
import ratelimit

log = logging.getLogger("wx.snapshots")

# ------------------------------------------------------------------ limits

# A connection that never answers must not hold a worker: connect is short, read
# is the one image transfer. Both are explicit rather than httpx's default of
# "wait forever".
CONNECT_TIMEOUT_S = 4.0
READ_TIMEOUT_S = 8.0
# Response ceiling. A JPEG still is tens of KB; 2 MB is far above any legitimate
# frame and far below what would let an upstream exhaust memory by streaming.
MAX_BYTES = 2 * 1024 * 1024
# Accept only what a browser can render as a still. Anything else -- HTML from a
# login page, JSON from an API, an error document -- is a failure, not an image.
ALLOWED_CONTENT_TYPES = ("image/jpeg", "image/jpg", "image/png", "image/webp")

# How long a fetched frame is reused before another upstream hit, when the camera
# does not name its own cadence. Bounded below by MIN_TTL_S: a source that says
# "1 second" would otherwise be honoured and hammered by every viewer.
DEFAULT_TTL_S = 15
MIN_TTL_S = 5


# ------------------------------------------------------------------ errors

class SnapshotError(Exception):
    """Base for every failure. Public handlers must not leak ``str(self)``.

    The message is for the log. A handler collapses the whole hierarchy to one
    generic client answer, which is what stops an attacker distinguishing
    "host refused" from "host timed out" from "no such camera".
    """


class UnknownCamera(SnapshotError):
    """No enabled camera with that id (unknown and disabled are the same answer)."""


class NoSource(SnapshotError):
    """The camera is real but has no snapshot source configured yet."""


class SourceRejected(SnapshotError):
    """The configured source itself is unsafe or malformed.

    A rejection of *configuration*, not of a request: it means the operator set a
    private address, embedded credentials, a bad scheme, or a bad mock mode.
    Never surfaced to a client.
    """


class FetchFailed(SnapshotError):
    """The upstream could not be reached, answered non-2xx, redirected, or was
    too slow / too large / not an image. One class for all of them on purpose."""


class FetchThrottled(SnapshotError):
    """This caller asked for a *new* upstream fetch too often for one camera.

    Raised only on the cache-miss path (see :meth:`SnapshotCache.get_or_fetch`),
    never for a cache hit -- a viewer reading the shared frame is not throttled.
    It is a separate class from :class:`FetchFailed` so the endpoint can answer
    with the app's existing rate-limit contract (429 + ``Retry-After``) rather
    than the generic "unavailable" (503), which would invite an immediate retry
    and so defeat the point of the limit.
    """

    def __init__(self, retry_after: float) -> None:
        super().__init__("snapshot fetch throttled")
        self.retry_after = retry_after


# ------------------------------------------------------------------ results

@dataclass(frozen=True)
class Snapshot:
    """A fetched frame, ready to serve. Immutable so the cache hands out copies."""

    data: bytes
    content_type: str
    fetched_at: float
    source_kind: str            # "http" | "https" | "fetch" | "mock"


# ------------------------------------------------------------------ addresses

def _as_ip(value: str):
    """The parsed address, or None when `value` is not a literal IP.

    Distinguishing "not an IP" from "an IP that is not public" matters: a
    hostname must fall through to resolution, while a literal address is decided
    on the spot.
    """
    try:
        return ipaddress.ip_address((value or "").strip().strip("[]"))
    except ValueError:
        return None


def _is_public_address(ip: str) -> bool:
    """True only for a global unicast address.

    Written as an allow-list rather than a list of blocked ranges: a new
    special-purpose range (there are many) is then rejected by default instead of
    silently allowed. Loopback, link-local, private, reserved, multicast and
    unspecified all fall outside ``is_global``.
    """
    addr = _as_ip(ip)
    if addr is None:
        return False
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_global


def _default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def resolve_public(host: str, *, resolver=None) -> list[str] | None:
    """The addresses for `host` **if every one is public unicast**, else None.

    The single decision point for "may we dial this name", used by both HTTP
    layers so the rule exists in exactly one place. Returning the addresses (not
    a bool) is the point: the caller pins the *same* addresses it just validated,
    so nothing re-resolves between the check and the connection -- that is what
    closes the DNS-rebinding window a separate check-then-dial would leave open.

    Fail closed: an empty resolution, a resolver error, a literal non-public
    address, or *any* non-public answer in a split response all return None. A
    name that answers with one public and one private address is refused, because
    dialling the second is the attack.

    ``resolver`` defaults to the real one and is injectable so tests drive DNS
    exactly, with no network.
    """
    resolver = resolver or _default_resolver
    text = (host or "").strip().strip("[]")
    if not text:
        return None
    literal = _as_ip(text)
    if literal is not None:
        # A literal address needs no resolution; public or refused on the spot.
        return [text] if _is_public_address(text) else None
    try:
        addresses = resolver(text)
    except (OSError, UnicodeError, ValueError):
        return None
    if not addresses:
        return None
    if not all(_is_public_address(a) for a in addresses):
        return None
    return addresses


# ------------------------------------------------------------------ mock

# A real, decodable 1x1 PNG. Kept literal so the mock never needs an image
# library or a generator at import time.
_ONE_PIXEL_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

# Deterministic offline outcomes, keyed by mock mode name. Every branch of the
# fetch path -- success, a too-large body, a wrong content type, an upstream
# error, a hang -- can be driven with no network at all.
MOCK_MODES = ("ok", "timeout", "error", "wrongtype", "oversize", "empty")


def _mock_outcome(mode: str) -> tuple[str, bytes, str, int | None]:
    """(kind, body, content_type, status) for a mode. `kind` is image/timeout/status."""
    if mode == "timeout":
        return ("timeout", b"", "", None)
    if mode == "error":
        return ("status", b"", "", 502)
    if mode == "wrongtype":
        return ("image", b"<html>not an image</html>", "text/html", None)
    if mode == "oversize":
        return ("image", b"\x00" * (MAX_BYTES + 1), "image/jpeg", None)
    if mode == "empty":
        return ("image", b"", "image/png", None)
    return ("image", _ONE_PIXEL_PNG, "image/png", None)


def _mock_mode() -> str | None:
    """The configured mock mode, or None when mock mode is off.

    ``WX_CAMERA_SNAPSHOT_MOCK`` turns the offline source on for all cameras. It is
    a development/test switch and never a production one, so three things are true:
    it is refused outright when ``WX_ENV=production``, an unknown value is rejected
    rather than treated as "no mock" (a typo cannot silently point a camera at a
    real fetch), and the value must be one of :data:`MOCK_MODES`.
    """
    raw = (os.environ.get("WX_CAMERA_SNAPSHOT_MOCK") or "").strip().lower()
    if not raw:
        return None
    if config.is_production():
        raise SourceRejected("mock mode is not allowed in production")
    if raw not in MOCK_MODES:
        raise SourceRejected("unknown mock mode")
    return raw


# ------------------------------------------------------------------ sources

@dataclass
class SnapshotSource:
    """A validated snapshot source for one camera.

    Built only from the trusted private store, never from a request. The subclass
    decides *how* bytes are obtained and owns the SSRF rules for its scheme.
    """

    url: str
    camera_id: str
    kind: str
    interval_min: int = 5

    @property
    def ttl_s(self) -> int:
        minutes = self.interval_min or (DEFAULT_TTL_S / 60)
        return max(MIN_TTL_S, int(minutes * 60))

    async def fetch(self) -> Snapshot:      # pragma: no cover - interface
        raise NotImplementedError


def _reject(reason: str, url: str) -> SourceRejected:
    # `_redact_url` drops the query and any userinfo, so a credential in a bad
    # URL cannot escape through this log line.
    log.warning("snapshot source rejected (%s): %s", reason, cams._redact_url(url))
    return SourceRejected(reason)


class HttpSnapshotSource(SnapshotSource):
    """The one HTTP(S) fetcher. A `require_allowlist` switch, not a second code path.

    Why there is one fetcher and not two
    ------------------------------------
    The public path and the private-camera path differ in exactly one thing: who
    is allowed to name the host. Everything after that -- resolve, validate, pin,
    connect, read under a cap -- is identical, and duplicating it would mean two
    copies of the SSRF rules to keep in step. So the policy is a class attribute
    and the networking lives here once:

    * ``require_allowlist = False`` -- a public snapshot URL an operator pasted.
      Any host, but it must resolve to public unicast addresses only.
    * ``require_allowlist = True`` -- a private camera source. The host must *also*
      be on ``WX_CAMERA_ALLOWED_HOSTS``, an explicit operator decision. This is
      :class:`FetchSnapshotSource` below.

    IP pinning (the TOCTOU fix)
    ---------------------------
    :func:`resolve_public` returns the very addresses it validated, and the first
    is written into the URL as a literal before the request is sent. httpx is
    therefore handed ``https://93.184.216.34/path``, not ``https://name/path``:
    it has no name to resolve, so there is no window in which a second lookup
    could return a different (private) address than the one that was checked.
    A fresh ``AsyncClient`` is opened and closed per fetch, so there is no pooled
    connection from an earlier DNS answer to be reused either.

    TLS and the Host header
    -----------------------
    Pinning an address must not break certificate verification, so two things are
    preserved while the literal address is dialled:

    * ``Host: <name>`` -- the origin server still sees the name it expects.
    * ``sni_hostname: <name>`` -- httpcore uses this for the TLS handshake *and*
      for the certificate hostname check (its default context has
      ``check_hostname=True``); verification stays against the name, never the IP.

    A redirect is never followed: the next hop would be a URL httpx picks, which
    the allowlist never saw. It is reported as a failure instead.
    """

    require_allowlist = False

    async def fetch(self) -> Snapshot:
        parts = urlsplit(self.url)
        if parts.scheme not in ("http", "https"):
            raise SourceRejected("scheme not http(s)")
        if parts.username or parts.password:
            raise SourceRejected("credentials in url")
        try:
            host = parts.hostname or ""
        except ValueError:
            raise SourceRejected("invalid host")
        if not host:
            raise SourceRejected("no host")
        if self.require_allowlist:
            allowed = cams._allowed_hosts()
            if not allowed:
                raise SourceRejected("no allowlist configured")
            if host.lower() not in allowed:
                raise SourceRejected("host not allowlisted")
        addresses = resolve_public(host)
        if addresses is None:
            raise SourceRejected("host is not a public address")
        # ZoneID (some IPv6 answers) would make the URL invalid; drop it.
        pinned = addresses[0].split("%")[0]
        target = _with_host(self.url, pinned)
        headers = {"Host": host}
        extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
        return await _fetch_http(target, self.kind, headers=headers,
                                 extensions=extensions)


class FetchSnapshotSource(HttpSnapshotSource):
    """The private-camera policy: an allowlisted host, fetched with a pinned IP.

    Identical networking to :class:`HttpSnapshotSource`; the only difference is
    that the host must be on ``WX_CAMERA_ALLOWED_HOSTS``. It exists as a named
    class because ``source_for`` and the tests read better selecting a policy by
    name than by a boolean.
    """

    require_allowlist = True


class MockSnapshotSource(SnapshotSource):
    """Layer 3: deterministic, offline, no network. For tests and development.

    ``url`` carries the mode as its host (``mock://ok.example.invalid``) so the
    source reads the same way as the others and the well-known ``.invalid`` TLD
    guarantees it can never be a real host.
    """

    async def fetch(self) -> Snapshot:
        host = (urlsplit(self.url).hostname or "").lower()
        mode = host.split(".")[0] if host else ""
        if mode not in MOCK_MODES:
            raise SourceRejected("unknown mock mode")
        kind, body, ctype, status = _mock_outcome(mode)
        if kind == "timeout":
            raise FetchFailed("mock timeout")
        if kind == "status":
            raise FetchFailed(f"mock upstream status {status}")
        if len(body) > MAX_BYTES:
            raise FetchFailed("mock body over the size cap")
        ctype = _normalise_content_type(ctype)
        if ctype not in ALLOWED_CONTENT_TYPES:
            raise FetchFailed("mock content type is not an image")
        if not body:
            raise FetchFailed("mock empty body")
        return Snapshot(data=body, content_type=ctype, fetched_at=time.time(),
                        source_kind="mock")


def _with_host(url: str, host: str) -> str:
    """Return `url` with its host replaced by `host`, keeping path/query/port.

    Only used to pin an address that has already been validated; the pieces go
    through ``urlunsplit`` so no re-parsing of attacker-controlled data happens
    here. IPv6 literals are bracketed.

    A port that ``urlsplit`` cannot parse (out of range, non-numeric) raises
    ``ValueError`` on access, which would otherwise surface as a 500. It is an
    unusable source, so it fails closed as a rejection and the endpoint answers
    with the same generic "unavailable" as any other bad config.
    """
    p = urlsplit(url)
    try:
        port = p.port
    except ValueError:
        raise SourceRejected("invalid port")
    literal = f"[{host}]" if ":" in host and not host.startswith("[") else host
    netloc = literal if port is None else f"{literal}:{port}"
    return urlunsplit((p.scheme, netloc, p.path, p.query, ""))


def _normalise_content_type(value: str) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def _is_image_content_type(value: str) -> bool:
    """Whether a (possibly parameterised) Content-Type is a still we may serve.

    One place decides "is this an image", so the streaming fetch and the mock
    source cannot drift: both normalise the same way and consult the same
    allowlist. A login page's ``text/html``, an API's ``application/json`` and an
    error document are all refused here rather than being handed to a browser.
    """
    return _normalise_content_type(value) in ALLOWED_CONTENT_TYPES


async def _fetch_http(url: str, kind: str, *, headers: dict | None,
                      extensions: dict | None) -> Snapshot:
    """One GET under all the limits, streamed so the byte cap is a real ceiling."""
    timeout = httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
    try:
        async with httpx.AsyncClient(timeout=timeout,
                                     follow_redirects=False) as client:
            async with client.stream("GET", url, headers=headers,
                                     extensions=extensions) as response:
                if 300 <= response.status_code < 400:
                    # Reported, never followed. See the module docstring.
                    raise FetchFailed("upstream redirected")
                if response.status_code != 200:
                    raise FetchFailed(f"upstream status {response.status_code}")
                ctype = _normalise_content_type(
                    response.headers.get("content-type", ""))
                if not _is_image_content_type(ctype):
                    raise FetchFailed("content type is not an image")
                # A declared length over the cap is refused before a byte is read,
                # so a hostile upstream cannot make us buffer up to the cap first.
                # A missing or unparseable length is not trusted: the streamed
                # counter below is the real ceiling either way.
                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        if int(declared) > MAX_BYTES:
                            raise FetchFailed("declared body over the size cap")
                    except ValueError:
                        pass
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise FetchFailed("body over the size cap")
                if not body:
                    raise FetchFailed("empty body")
                return Snapshot(data=bytes(body), content_type=ctype,
                                fetched_at=time.time(), source_kind=kind)
    except httpx.HTTPError as e:
        raise FetchFailed(f"transport error: {type(e).__name__}") from e


# ------------------------------------------------------------------ builder

def source_for(camera_id: str) -> SnapshotSource | None:
    """Build a validated source for a camera, or None when it has none.

    Mock mode, when enabled, wins: it is an explicit operator/test switch and it
    touches no network. Otherwise the URL comes from ``cameras.source_for`` -- the
    single door to the private store, which already applies scheme, host-allowlist
    and credential checks. This function adds the snapshot-specific policy (which
    schemes this runtime will fetch, and whether the host is on the private
    allowlist) and never consults a request.
    """
    cam = cams.find_camera(camera_id)
    interval = (cam or {}).get("snapshot_interval_min") or 5

    mode = _mock_mode()
    if mode is not None:
        return MockSnapshotSource(url=f"mock://{mode}.example.invalid",
                                  camera_id=camera_id, kind="mock",
                                  interval_min=interval)

    src = cams.source_for(camera_id)
    if not src:
        return None
    url = (src.get("url") or "").strip()
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        raise _reject("unparseable url", url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        # rtsp/rtsps: a real stream source, not a snapshot one. That pipeline is
        # a separate milestone; here it is simply "no snapshot yet".
        raise _reject("scheme is not a snapshot scheme", url)
    if parts.username or parts.password:
        raise _reject("credentials embedded in url", url)
    host = (parts.hostname or "").lower()
    if host and host in cams._allowed_hosts():
        return FetchSnapshotSource(url=url, camera_id=camera_id, kind=scheme,
                                   interval_min=interval)
    return HttpSnapshotSource(url=url, camera_id=camera_id, kind=scheme,
                              interval_min=interval)


# ------------------------------------------------------------------ cache

@dataclass
class _Entry:
    snapshot: Snapshot | None = None
    fetched_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SnapshotCache:
    """Per-camera single-flight cache. In-process, bounded, no persistence.

    The load model it exists for: many browsers ask for the same camera's frame
    at roughly the same time, and without this each request would be an upstream
    camera hit. Two properties close that:

    * **Single-flight.** A per-camera ``asyncio.Lock`` means concurrent misses
      wait for the one fetch in progress and then share its result, so N
      simultaneous viewers cost one upstream request.
    * **TTL.** A frame is reused for the camera's snapshot interval (bounded to a
      sane minimum) before another upstream hit.

    An in-process dict is correct because the deployment is one uvicorn process
    (the RAM grids are shared process-wide and would be duplicated per worker).
    If that ever changes, each worker caches independently -- noted as a
    trade-off, not hidden. Nothing here writes to disk or to a database.
    """

    def __init__(self, max_entries: int = 64) -> None:
        self._entries: dict[str, _Entry] = {}
        self._max = max_entries

    def get_fresh(self, camera_id: str, ttl_s: int,
                  now: float | None = None) -> Snapshot | None:
        """A cached frame younger than `ttl_s`, or None (a miss or an expired one).

        Freshness is measured against the entry's own timestamp, not the frame's:
        the cache is what decides "recent enough", and it can be driven by an
        injected clock, which is what makes the TTL testable without sleeping.
        """
        entry = self._entries.get(camera_id)
        if entry is None or entry.snapshot is None:
            return None
        age = (now if now is not None else time.time()) - entry.fetched_at
        if age >= ttl_s:
            return None
        return entry.snapshot

    def _entry(self, camera_id: str) -> _Entry:
        entry = self._entries.get(camera_id)
        if entry is None:
            if len(self._entries) >= self._max:
                # Evict the oldest fetch; the map stays small and a camera that is
                # wanted again simply re-fetches.
                oldest = min(self._entries,
                             key=lambda k: self._entries[k].fetched_at)
                self._entries.pop(oldest, None)
            entry = _Entry()
            self._entries[camera_id] = entry
        return entry

    async def get_or_fetch(self, camera_id: str, source: SnapshotSource,
                           now: float | None = None,
                           client_key: str | None = None) -> Snapshot:
        """Return a fresh frame, fetching at most once for concurrent callers.

        The freshness re-check happens *inside* the lock: a caller that waited
        must see the frame the first caller just fetched, not fetch again.

        The upstream-fetch throttle is charged here too, and only here: after the
        in-lock freshness re-check, i.e. on the cache-miss path that is actually
        about to reach the upstream. That placement is what keeps the three
        concerns from fighting each other:

        * a cache hit returns above this line and spends no token, so viewers
          reading the shared frame are never throttled by each other;
        * under a concurrent cold miss, the single-flight winner charges one
          token and fetches, and every waiter that then sees the frame fresh
          returns without charging -- so a burst of N viewers is one token, not
          N, and cannot become an upstream request storm;
        * a caller whose own bucket is exhausted is refused before any dial, so
          the upstream is protected without the reader being penalised.

        ``client_key`` is the per-caller identity (already hashed by
        :func:`ratelimit.client_key`). When it is None the throttle is skipped --
        a direct, in-process call that is not on behalf of a client.
        """
        entry = self._entry(camera_id)
        async with entry.lock:
            fresh = self.get_fresh(camera_id, source.ttl_s, now)
            if fresh is not None:
                return fresh
            _charge_fetch(camera_id, client_key)
            snap = await source.fetch()
            entry.snapshot = snap
            # Stamp with the caller's clock when one was injected, so a driven
            # test clock and the real one never disagree about freshness.
            entry.fetched_at = now if now is not None else snap.fetched_at
            return snap

    def put(self, camera_id: str, snapshot: Snapshot) -> None:
        entry = self._entry(camera_id)
        entry.snapshot = snapshot
        entry.fetched_at = snapshot.fetched_at

    def clear(self) -> None:
        self._entries.clear()


# Module-level cache. Reset between tests via `reset_cache()`.
_CACHE = SnapshotCache()


def reset_cache() -> None:
    _CACHE.clear()


def _charge_fetch(camera_id: str, client_key: str | None) -> None:
    """Spend one upstream-fetch token for (camera, caller), or raise.

    The bucket key pairs the camera with the caller so one caller hammering one
    camera cannot starve another camera, and one camera's load does not spend a
    different camera's budget. A caller with no identity (an in-process call) is
    not throttled: there is no client to protect the upstream from there.
    """
    if client_key is None:
        return
    if not config.rate_limit_enabled():
        # Honor the same switch the middleware does, so one `WX_RATE_LIMIT_DISABLED`
        # disables throttling everywhere rather than leaving this path on.
        return
    allowed, retry = ratelimit.LIMITER.allow(
        f"camsnap:{camera_id}:{client_key}", ratelimit.CAMERA_SNAPSHOT)
    if not allowed:
        raise FetchThrottled(retry)


async def get_snapshot(camera_id: str, *, now: float | None = None,
                       cache: SnapshotCache | None = None,
                       client_key: str | None = None) -> Snapshot:
    """The public entry point: a fresh frame for a camera id, or raise.

    Raises a :class:`SnapshotError` subclass; the caller (the endpoint) is
    responsible for collapsing it to a generic client response. ``cache`` is
    injectable so a test can isolate state. ``client_key`` (from
    :func:`ratelimit.client_key`) is used only to charge the upstream-fetch
    throttle on a miss; it is never stored or logged here.
    """
    cache = cache or _CACHE
    cam = cams.find_camera(camera_id)
    if cam is None:
        raise UnknownCamera("unknown or disabled camera")
    if not cam.get("snapshot"):
        raise NoSource("camera has no snapshot configured")
    source = source_for(camera_id)
    if source is None:
        raise NoSource("camera has no snapshot source")
    fresh = cache.get_fresh(camera_id, source.ttl_s, now)
    if fresh is not None:
        return fresh
    return await cache.get_or_fetch(camera_id, source, now, client_key)


# ------------------------------------------------------------------ health

def snapshot_via(camera_id: str) -> str | None:
    """How the browser should obtain this camera's still: "server" or "direct".

    Additive presentation metadata, computed here rather than carried in the
    public camera schema, so adding a server-side snapshot path needs no change to
    that schema. The rule is deliberately conservative:

    * ``"server"`` when a private source is configured (the private path, which
      only the server may fetch) or when mock mode is on;
    * ``"direct"`` otherwise -- a public URL the browser already loads today, so
      the default deployment keeps its existing behaviour byte for byte.

    Returns None when the camera has no snapshot at all (``not_configured``).
    """
    cam = cams.find_camera(camera_id)
    if cam is None or not cam.get("snapshot"):
        return None
    try:
        mode = _mock_mode()
        has_source = cams.source_for(camera_id) is not None
    except SnapshotError:
        return "direct"
    if mode is not None or has_source:
        return "server"
    return "direct"


def cache_state(cache: SnapshotCache | None = None,
                now: float | None = None) -> dict:
    """Per-camera cache age and TTL, keyed by camera id. No frame bytes, no URL.

    Operator-facing: tells an operator *which* camera is stale and how long its
    TTL is, which the counts-only `health()` cannot. The id is public (it is in
    the URL), and the entry's age/ttl are numbers; nothing here is a host, a
    source URL or a credential, so it is safe to show behind an operator gate.
    """
    cache = cache or _CACHE
    stamp = now if now is not None else time.time()
    state: dict[str, dict] = {}
    for camera_id, entry in cache._entries.items():
        if entry.snapshot is None:
            continue
        state[camera_id] = {
            "age_s": round(max(0.0, stamp - entry.fetched_at), 1),
            "content_type": entry.snapshot.content_type,
            "kind": entry.snapshot.source_kind,
            "bytes": len(entry.snapshot.data),
        }
    return state


def diagnostics(cache: SnapshotCache | None = None,
                now: float | None = None) -> dict:
    """Operator-facing per-camera snapshot state, sanitized.

    Combines what the public payload knows (id, whether a still is configured,
    the interval, how the browser will fetch it) with the cache's own view (age,
    size). It deliberately reuses :func:`snapshot_via` for the transport decision
    rather than re-deriving it, so an operator sees exactly what the browser is
    told.

    Never contains a source URL, a host, a credential or frame bytes. Served only
    behind the operator gate (`GET /api/admin/cameras`).
    """
    cache = cache or _CACHE
    stamp = now if now is not None else time.time()
    cache_state_ = cache_state(cache, stamp)
    out = []
    for cam in cams.load_config():
        cid = cam.get("id", "")
        try:
            source = source_for(cid)
            src_kind = source.kind if source is not None else None
            ttl = source.ttl_s if source is not None else None
        except SnapshotError:
            src_kind, ttl = "rejected", None
        cached = cache_state_.get(cid)
        out.append({
            "id": cid,
            "enabled": bool(cam.get("enabled", True)),
            "configured": bool(cam.get("snapshot")),
            "via": snapshot_via(cid),
            "interval_min": cam.get("snapshot_interval_min"),
            "source_kind": src_kind,
            "ttl_s": ttl,
            "cached": cached is not None,
            "cache_age_s": cached["age_s"] if cached else None,
            "cache_bytes": cached["bytes"] if cached else None,
        })
    return {"cameras": out}


def health() -> dict:
    """Counts only for /api/health: never a URL, host or credential.

    ``mock_sources`` is surfaced so an operator can see that a camera is still
    pointed at the offline test source rather than a real one.
    """
    kinds: dict[str, int] = {}
    mock = 0
    for cam in cams.load_config():
        if not cam.get("enabled", True) or not cam.get("snapshot"):
            continue
        try:
            source = source_for(cam["id"])
        except SnapshotError:
            continue
        if source is None:
            continue
        kinds[source.kind] = kinds.get(source.kind, 0) + 1
        if source.kind == "mock":
            mock += 1
    return {"sources": sum(kinds.values()), "by_kind": kinds, "mock_sources": mock}
