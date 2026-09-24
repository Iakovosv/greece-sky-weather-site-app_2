"""Live sky cameras.

The cameras are the one part of this service that is genuinely ours: no public
model or reanalysis can say what the sky looks like *right now* over Ilioupoli.
Everything else here can be reproduced from open data; this cannot.

Configuration
-------------
Cameras are configured, never hardcoded to a guessable URL. Point them at your
own feeds with ``WX_CAMERAS``. Two forms are accepted — the short one is usually
enough:

    WX_CAMERAS='{"ilioupoli":"https://cam.example/ilioupoli/latest.jpg",
                 "glinado":"https://cam.example/glinado/latest.jpg"}'

    WX_CAMERAS='[
      {"id":"ilioupoli","name":"Ilioupoli Sky","lat":37.95,"lon":23.75,
       "snapshot":"https://cam.example/ilioupoli/latest.jpg",
       "timelapse":"https://cam.example/ilioupoli/today.mp4"},
      {"id":"glinado","name":"Glinado Sky","lat":37.07,"lon":25.42,
       "snapshot":"https://cam.example/glinado/latest.jpg"}
    ]'

In the short form the key selects one of the default sites, so name, region and
coordinates come from there; a key that matches no default still becomes a camera,
with only an id and a URL.

A camera with no ``snapshot`` is reported as ``not_configured`` and the UI shows
an explicit placeholder. That is deliberate: an ``<img>`` pointing at a URL that
does not exist renders as a broken icon, which reads as "our service is down"
rather than "this feed is not set up yet".

The browser loads the images directly from the feed, so there is no server-side
fetch of a user-influenced URL and therefore no SSRF surface. Only camera
metadata crosses the server boundary.
"""
from __future__ import annotations

import json
import os
import time

# The two sites the product was built around. Coordinates are the camera
# locations, not the town centres, because the sky shown is the sky overhead.
DEFAULT_CAMERAS: list[dict] = [
    {"id": "ilioupoli", "name": "Ilioupoli Sky", "region": "Αττική",
     "lat": 37.9333, "lon": 23.7500, "snapshot": None, "timelapse": None},
    {"id": "glinado", "name": "Glinado Sky", "region": "Νάξος",
     "lat": 37.0667, "lon": 25.4167, "snapshot": None, "timelapse": None},
]

_KEEP = ("id", "name", "region", "lat", "lon", "snapshot", "timelapse", "note")


def _sanitize(raw: dict, fallback_id: str) -> dict | None:
    """Keep only known keys, with types the UI can rely on.

    Config arrives from the environment, so a typo there must not produce a card
    with a missing name or a string latitude that breaks the map link.
    """
    if not isinstance(raw, dict):
        return None
    out = {k: raw.get(k) for k in _KEEP}
    cid = str(out.get("id") or fallback_id)
    if not cid:
        return None
    out["id"] = cid
    out["name"] = str(out.get("name") or cid)
    out["region"] = str(out.get("region") or "")
    try:
        out["lat"] = float(out["lat"]) if out.get("lat") is not None else None
    except (TypeError, ValueError):
        out["lat"] = None
    try:
        out["lon"] = float(out["lon"]) if out.get("lon") is not None else None
    except (TypeError, ValueError):
        out["lon"] = None
    for k in ("snapshot", "timelapse", "note"):
        v = out.get(k)
        out[k] = str(v) if v else None
    return out


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
    """Configured cameras, or the two default sites with no feeds attached."""
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


def camera_payload(now: float | None = None) -> dict:
    """Camera list plus the freshness stamp the UI uses to bust image caches.

    ``stamp`` is the current minute: a live snapshot URL is reloaded with
    ``?t=<stamp>`` so the browser fetches a new frame instead of reusing the
    cached one, while a paused view keeps showing the frame it already has.
    """
    now = now if now is not None else time.time()
    stamp = int(now // 60)
    cams = []
    for c in load_config():
        configured = bool(c.get("snapshot"))
        cams.append({
            **c,
            "status": "live" if configured else "not_configured",
            "has_timelapse": bool(c.get("timelapse")),
        })
    return {
        "cameras": cams,
        "configured_count": sum(1 for c in cams if c["status"] == "live"),
        "refresh_seconds": 60,
        "stamp": stamp,
        "note": ("Οι ζωντανές ροές δεν είναι ακόμη συνδεδεμένες."
                 if not any(c["status"] == "live" for c in cams) else None),
    }
