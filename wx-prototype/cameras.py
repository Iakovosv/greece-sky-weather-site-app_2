"""Live sky cameras: registry, public metadata, and the private source store.

The cameras are the one part of this service that is genuinely ours: no public
model or reanalysis can say what the sky looks like *right now* over Ilioupoli.
Everything else here can be reproduced from open data; this cannot.

Two-layer model
---------------
Camera configuration has two clearly separated halves, and keeping them apart is
the whole point of this module:

* **Public metadata** -- id, title, region, coordinates, snapshot/timelapse URLs,
  the snapshot interval, and (when configured) the *public* YouTube live
  identifier. This is what ``/api/cameras`` serves and what the browser may see.
* **Private source** -- the RTSP/HTTP URL, username, password and secret
  reference used later by a server-side stream pipeline. It lives in a separate
  environment variable (``WX_CAMERA_SOURCES``), is parsed separately, and is
  reachable only through :func:`source_for`. It is never merged into the camera
  dict and never appears in a payload.

That separation is enforced structurally rather than by remembering to strip
keys: :func:`camera_payload` builds each camera from an explicit whitelist, so a
new private key added to the environment cannot leak by being carried along in a
``{**cam}`` spread.

Configuration
-------------
Public metadata comes from ``WX_CAMERAS``. Two forms are accepted -- the short
one is usually enough::

    WX_CAMERAS='{"ilioupoli":"https://cam.example/ilioupoli/latest.jpg",
                 "glinado":"https://cam.example/glinado/latest.jpg"}'

    WX_CAMERAS='[
      {"id":"ilioupoli","name":"Ilioupoli Sky","lat":37.95,"lon":23.75,
       "snapshot":"https://cam.example/ilioupoli/latest.jpg",
       "snapshot_interval_min":5,
       "live_enabled":true,"live_provider":"youtube","youtube_live_id":"XXXXXXXXXXX"},
      {"id":"glinado","name":"Glinado Sky","lat":37.07,"lon":25.42,
       "snapshot":"https://cam.example/glinado/latest.jpg"}
    ]'

In the short form the key selects one of the default sites, so name, region and
coordinates come from there; a key that matches no default still becomes a camera,
with only an id and a URL.

The private source comes from a *different* variable, so credentials are never
carried in the same JSON that the short form makes people write::

    WX_CAMERA_SOURCES='[
      {"id":"ilioupoli","url":"rtsp://cam-lan.local:554/Streaming/Channels/101",
       "username":"viewer","secret_ref":"WX_CAMERA_ILIOUPOLI_PASS"}
    ]'

Optional ``WX_CAMERA_ALLOWED_HOSTS`` (comma-separated) restricts which hosts a
private source may name.

Snapshot mode vs live mode
--------------------------
Snapshot mode is the default, low-resource mode: the browser shows the last
image the feed publishes and reloads it on a configurable interval. No stream and
no video ever crosses our server.

Live mode is modelled as a *provider*, not as WebRTC. The provider now is
YouTube: ``live_provider="youtube"`` plus a public ``youtube_live_id``, which the
future frontend embeds through the official player. The private source never
reaches the browser -- an embed URL is ``youtube-nocookie.com`` with a public
video id, not an RTSP URL. Adding ``webrtc`` or ``hls`` later is a new entry in
:data:`LIVE_PROVIDERS`, not a redesign.

Video-only policy: camera streams carry no audio. A source that declares
``audio: true`` is rejected rather than muted downstream, so no audio track is
present for a future pipeline to forward.

A camera with no ``snapshot`` is reported as ``not_configured`` and the UI shows
an explicit placeholder. That is deliberate: an ``<img>`` pointing at a URL that
does not exist renders as a broken icon, which reads as "our service is down"
rather than "this feed is not set up yet".

The browser loads snapshot images directly from the feed, so there is no
server-side fetch of a user-influenced URL and therefore no SSRF surface. Only
camera metadata crosses the server boundary.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from urllib.parse import urlsplit

import config

log = logging.getLogger("wx.cameras")

# The two sites the product was built around. Coordinates are the camera
# locations, not the town centres, because the sky shown is the sky overhead.
DEFAULT_CAMERAS: list[dict] = [
    {"id": "ilioupoli", "name": "Ilioupoli Sky", "region": "Αττική",
     "lat": 37.9333, "lon": 23.7500, "snapshot": None, "timelapse": None},
    {"id": "glinado", "name": "Glinado Sky", "region": "Νάξος",
     "lat": 37.0667, "lon": 25.4167, "snapshot": None, "timelapse": None},
]

# Only these keys ever leave the server. A private key added to the environment
# is ignored here rather than carried along by a dict spread -- this whitelist is
# the security boundary, not a formatting convenience.
_PUBLIC_KEYS = ("id", "name", "region", "lat", "lon", "snapshot", "timelapse", "note")

# Live distribution providers we are prepared to hand a public embed for. A
# provider not listed here is treated as "no live", whatever the config says.
LIVE_PROVIDERS = ("youtube",)

# Snapshot cadence is chosen from a fixed menu so a typo cannot produce a camera
# that hammers a feed every second. Minutes; snapshot mode is the low-resource
# default and 5 is the value the UI copy names.
ALLOWED_INTERVALS = (1, 2, 5, 10, 15, 30, 60)
DEFAULT_INTERVAL = 5

_ALLOWED_SNAPSHOT_SCHEMES = ("http", "https")
_SOURCE_SCHEMES = ("rtsp", "rtsps", "http", "https")

# A camera id is an opaque public identifier that ends up in a URL path
# (`/api/cameras/<id>/snapshot`) and in an HTML element id, so it is constrained
# to a conservative shape rather than accepted as free text. Lowercase letters,
# digits, hyphen and underscore; 1-64 chars; must start with a letter or digit.
# This keeps an id stable and URL-safe, and stops a config typo from producing an
# id that a route, an element id, or a log line would have to quote defensively.
_CAMERA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


# --------------------------------------------------------------- URL safety

def _redact_url(url: str | None) -> str:
    """A URL safe to put in a log line: host kept, credentials and query dropped.

    Logs are the easiest place for a source URL to escape, so nothing that looks
    like a credential is ever formatted into one.
    """
    if not url:
        return ""
    try:
        p = urlsplit(str(url))
        # `.scheme`, `.hostname` and `.port` all raise ValueError on some inputs
        # (an out-of-range or non-numeric port, an unclosed IPv6 literal), so the
        # field access belongs inside the guard: `_redact_url` runs on rejected
        # config, and a rejected config must not be able to 500 the health check.
        scheme, hostname = p.scheme, p.hostname
        port = f":{p.port}" if p.port else ""
    except ValueError:
        return "<unparseable>"
    if not scheme or not hostname:
        return "<redacted>"
    return f"{scheme}://{hostname}{port}/…"


def _valid_snapshot_url(value) -> str | None:
    """A snapshot URL the browser may load, or None when it is unusable."""
    if not value:
        return None
    text = str(value).strip()
    try:
        p = urlsplit(text)
    except ValueError:
        return None
    if p.scheme.lower() not in _ALLOWED_SNAPSHOT_SCHEMES or not p.hostname:
        return None
    return text


# --------------------------------------------------------------- public model

def _valid_camera_id(value) -> str | None:
    """An id matching the constrained public shape, or None.

    Deliberately strict: the id is the one piece of config that becomes a URL
    segment and an element id, so a value that is not clearly safe is dropped
    rather than escaped at every use site.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if _CAMERA_ID_RE.match(text) else None


def _finite_coord(key: str, value) -> float | None:
    """A coordinate in range for its axis, or None.

    A string latitude is a typo, not a number, and an out-of-range value is not a
    coordinate. Both become None so a later map link or forecast call never has to
    re-validate what config already handed it. The axis decides the bound: a
    latitude of 100 is refused by `finite_lat` even though 100 is a valid longitude.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    check = config.finite_lat if key == "lat" else config.finite_lon
    return number if check(number) else None


def _sanitize(raw: dict, fallback_id: str) -> dict | None:
    """Keep only known keys, with types the UI can rely on.

    Config arrives from the environment, so a typo there must not produce a card
    with a missing name or a string latitude that breaks the map link. Crucially,
    only public-safe keys survive: private source material is not read here at
    all, so it cannot ride along in the returned dict.

    Invalid configuration fails safe: an id that does not match the constrained
    shape, or a coordinate that is not a finite in-range number, is dropped rather
    than carried into a payload, a route or an element id. A camera whose id is
    unusable is skipped entirely (``None``), so it cannot become an ambiguous
    public entry.
    """
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    cid = _valid_camera_id(raw.get("id")) or _valid_camera_id(fallback_id)
    if not cid:
        return None
    out["id"] = cid
    out["name"] = str(raw.get("name") or cid)
    out["region"] = str(raw.get("region") or "")
    for key in ("lat", "lon"):
        # A missing coordinate is fine (None); a present-but-unusable one is not a
        # coordinate, so it becomes None rather than an out-of-range float that a
        # map link or a forecast call would later have to re-validate.
        out[key] = _finite_coord(key, raw.get(key)) if raw.get(key) is not None else None
    # A snapshot that is not a public http(s) URL is treated as "not set", so the
    # card says "not configured" instead of failing to load a bad address.
    out["snapshot"] = _valid_snapshot_url(raw.get("snapshot"))
    out["timelapse"] = _valid_snapshot_url(raw.get("timelapse"))
    out["note"] = str(raw["note"]) if raw.get("note") else None

    out["enabled"] = raw.get("enabled") is not False
    out["snapshot_interval_min"] = _interval(raw.get("snapshot_interval_min"))
    out["live_enabled"] = bool(raw.get("live_enabled"))
    out["live_provider"] = _provider(raw.get("live_provider"))
    out["youtube_live_id"] = _youtube_id(raw.get("youtube_live_id"))
    return out


def _interval(value) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    return minutes if minutes in ALLOWED_INTERVALS else DEFAULT_INTERVAL


def _provider(value) -> str | None:
    """A supported live provider name, or None when absent/unsupported.

    Whitelisted rather than free text: the value reaches the browser and decides
    which player is embedded, so an unknown string must not be forwarded.
    """
    if not value:
        return None
    name = str(value).strip().lower()
    return name if name in LIVE_PROVIDERS else None


def _youtube_id(value) -> str | None:
    """A syntactically valid public YouTube video/live id, or None.

    The id is public information, but validating its shape keeps a paste error
    (a full URL, a channel handle, a stray space) from becoming an embed that
    silently renders nothing.
    """
    if not value:
        return None
    text = str(value).strip()
    if not (6 <= len(text) <= 24):
        return None
    if any(not (c.isalnum() or c in "_-") for c in text):
        return None
    return text


def _from_mapping(parsed: dict) -> list[dict]:
    """Simplified form: ``{"ilioupoli": "https://.../latest.jpg"}``.

    This is the shape people reach for first, and it is enough for the common
    case, so accept it rather than making a two-entry install write out full
    objects. The default site list supplies name, region and coordinates; the
    value is the snapshot URL. A key that matches no default becomes a camera
    with only an id and a URL.
    """
    by_id = {c["id"]: dict(c) for c in DEFAULT_CAMERAS}
    out = []
    for i, (cid, url) in enumerate(parsed.items()):
        cid = str(cid)
        out.append(_sanitize({**by_id.get(cid, {"id": cid}), "snapshot": url}, f"cam{i}"))
    return [c for c in out if c]


def load_config() -> list[dict]:
    """Configured cameras, or the two default sites with no feeds attached.

    Returns public-safe camera dicts only. Private connection material is not
    read here -- see :func:`source_for`.
    """
    raw = os.environ.get("WX_CAMERAS")
    if not raw:
        return [dict(c) for c in DEFAULT_CAMERAS]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return [dict(c) for c in DEFAULT_CAMERAS]
    if isinstance(parsed, dict):
        return _from_mapping(parsed) or [dict(c) for c in DEFAULT_CAMERAS]
    if not isinstance(parsed, list):
        return [dict(c) for c in DEFAULT_CAMERAS]
    out = []
    for i, item in enumerate(parsed):
        clean = _sanitize(item, f"cam{i}")
        if clean:
            out.append(clean)
    return out or [dict(c) for c in DEFAULT_CAMERAS]


def _public_live(cam: dict) -> dict | None:
    """The public live block for a camera, or None when live is not available.

    Only a supported provider with a valid public id yields a block, so a config
    that is half-filled ("live_enabled" but no id) produces no live affordance
    rather than a button that opens nothing. Private connection material is not
    consulted here at all.
    """
    if not cam.get("live_enabled"):
        return None
    provider = cam.get("live_provider")
    if provider not in LIVE_PROVIDERS:
        return None
    if provider == "youtube":
        video_id = cam.get("youtube_live_id")
        if not video_id:
            return None
        return {"provider": "youtube", "video_id": video_id,
                "privacy_enhanced": True}
    return None


def public_camera(cam: dict) -> dict:
    """One camera as it is safe to publish, built from the public whitelist.

    Explicit key selection is the point: nothing that is not named here can
    appear in a response, however the environment is configured.
    """
    out = {k: cam.get(k) for k in _PUBLIC_KEYS}
    out["snapshot_interval_min"] = _interval(cam.get("snapshot_interval_min"))
    configured = bool(cam.get("snapshot"))
    out["status"] = "live" if configured else "not_configured"
    out["has_timelapse"] = bool(cam.get("timelapse"))
    out["live"] = _public_live(cam)
    return out


def camera_payload(now: float | None = None) -> dict:
    """Public camera list plus the freshness stamp the UI uses to bust image caches.

    ``stamp`` is the current minute: a live snapshot URL is reloaded with
    ``?t=<stamp>`` so the browser fetches a new frame instead of reusing the
    cached one, while a paused view keeps showing the frame it already has.

    Disabled cameras are omitted entirely: an operator switching a feed off must
    not leave its metadata reachable.
    """
    now = now if now is not None else time.time()
    stamp = int(now // 60)
    cams = [public_camera(c) for c in load_config() if c.get("enabled", True)]
    return {
        "cameras": cams,
        "configured_count": sum(1 for c in cams if c["status"] == "live"),
        "refresh_seconds": 60,
        "stamp": stamp,
        "note": ("Οι ζωντανές ροές δεν είναι ακόμη συνδεδεμένες."
                 if not any(c["status"] == "live" for c in cams) else None),
    }


def find_camera(camera_id: str, now: float | None = None) -> dict | None:
    """Public metadata for one camera, or None when unknown or disabled.

    Unknown and disabled are the same answer on purpose: a caller must not be
    able to tell a camera that was switched off from one that never existed, and
    neither may an arbitrary id reach the private source store.
    """
    wanted = str(camera_id or "").strip()
    if not wanted:
        return None
    for cam in load_config():
        if cam.get("id") == wanted and cam.get("enabled", True):
            return public_camera(cam)
    return None


# --------------------------------------------------------------- private store
#
# Everything below is server-side only. It is deliberately a separate store from
# the camera dicts above, so there is no code path on which private material can
# be spread into a response.

def _allowed_hosts() -> set[str]:
    raw = os.environ.get("WX_CAMERA_ALLOWED_HOSTS") or ""
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _validate_source(raw: dict) -> dict | None:
    """Validate one private source definition, or None when it is unusable.

    Checks, in order: it is an object; the URL carries an allowed scheme and a
    host; credentials are not embedded in the URL itself (they belong in the
    named fields, so they cannot leak through URL logging); the host is on the
    allowlist when one is configured; and the source is video-only. Every
    rejection returns None and logs at most a redacted host, never the URL or a
    credential.
    """
    if not isinstance(raw, dict):
        return None
    url = str(raw.get("url") or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        log.warning("camera source rejected: unparseable url")
        return None
    if parts.scheme.lower() not in _SOURCE_SCHEMES or not parts.hostname:
        log.warning("camera source rejected: bad scheme or host (%s)",
                    _redact_url(url))
        return None
    if parts.username or parts.password:
        log.warning("camera source rejected: credentials embedded in url (%s)",
                    _redact_url(url))
        return None
    allowed = _allowed_hosts()
    if allowed and parts.hostname.lower() not in allowed:
        log.warning("camera source rejected: host not allowlisted (%s)",
                    _redact_url(url))
        return None
    if raw.get("audio"):
        log.warning("camera source rejected: audio is not permitted (video-only)")
        return None
    return {
        "id": str(raw.get("id") or ""),
        "url": url,
        "username": str(raw.get("username") or "") or None,
        "password": str(raw.get("password") or "") or None,
        "secret_ref": str(raw.get("secret_ref") or "") or None,
        "audio": False,
    }


def _parse_sources_raw() -> dict[str, dict]:
    """The declared private sources by id, **before** any validation.

    Split out so the lifecycle view can explain *why* a source was refused
    without re-deriving it: the declared entry and the validated entry are the
    same input seen at two stages. Never merged into a payload.
    """
    raw = os.environ.get("WX_CAMERA_SOURCES")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("WX_CAMERA_SOURCES is not valid JSON; no private sources loaded")
        return {}
    if isinstance(parsed, dict):
        parsed = [{"id": k, **(v if isinstance(v, dict) else {"url": v})}
                  for k, v in parsed.items()]
    if not isinstance(parsed, list):
        return {}
    return {str(i["id"]): i for i in parsed
            if isinstance(i, dict) and i.get("id")}


def _load_sources() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in _parse_sources_raw().values():
        clean = _validate_source(item)
        if clean and clean["id"]:
            out[clean["id"]] = clean
    return out


def declared_source(camera_id: str) -> dict | None:
    """The raw, unvalidated private source declared for an id, or None.

    Server-side only, and only so an operator-facing lifecycle view can name the
    failing rule instead of reporting a source as simply "absent". The returned
    dict is never embedded in a response or a log line.
    """
    return declared_sources().get(str(camera_id or "").strip() or "\x00")


def declared_sources() -> dict[str, dict]:
    """Every declared private source by id, before validation.

    A batch form of :func:`declared_source` for callers that need them all (the
    lifecycle summary); returns the same entries without re-parsing per id.
    Server-side only, never merged into a payload.
    """
    return _parse_sources_raw()


def all_sources() -> dict[str, dict]:
    """Every validated private source by id.

    Batch form of :func:`source_for`. Server-side only: the dict holds URLs and
    credential references, so it must never reach a payload or a log.
    """
    return _load_sources()


def source_for(camera_id: str) -> dict | None:
    """Private source for a camera, for a future server-side stream pipeline.

    The only door to the private store. It is not called from any request
    handler: no endpoint may reach a camera source from a web request, which is
    what keeps a client from naming a URL for the server to fetch. Returns None
    for an unknown or disabled camera, so an untrusted id cannot probe the store.
    """
    cam = find_camera(camera_id)
    if cam is None:
        return None
    return _load_sources().get(cam["id"])


def resolve_secret(source: dict) -> str | None:
    """The credential value named by a source's ``secret_ref``, or None.

    Read only here, never returned in a payload and never logged. The reference
    (an environment variable name) is safe to store; its value is not.
    """
    ref = (source or {}).get("secret_ref")
    if not ref:
        return None
    value = os.environ.get(ref)
    return value or None


def health() -> dict:
    """Counts for /api/health. Never a URL, host or credential.

    A malformed ``WX_CAMERA_SOURCES`` shows here as ``sources: 0`` rather than
    printing what it contained, which is the point of reporting counts only.
    """
    cams = load_config()
    return {
        "configured": len(cams),
        "snapshots": sum(1 for c in cams if c.get("snapshot")),
        "live": sum(1 for c in cams if _public_live(c)),
        "sources": len(_load_sources()),
        "allowed_hosts": bool(_allowed_hosts()),
    }
