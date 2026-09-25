"""Greece Sky and Weather — dual-view weather service.

Run:  uvicorn app:app --host 0.0.0.0 --port 12000

Data sources are all commercially licensable (see LICENSES.md):
GFS (public domain), ICON-EU (DWD, CC BY 4.0), ECMWF open data (CC BY 4.0,
best-effort), Photon geocoding (OSM).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import html
import io
import json
import logging
import math
import os
import secrets
import time

import httpx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import xarray as xr
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.requests import ClientDisconnect

import envfile

# Loaded before the local imports on purpose: `wx`, `grids`, `scheduler`,
# `billing` and `astro` resolve a setting at import time, so reading `.env`
# afterwards left those seven values frozen at their development defaults and
# made a file-configured deploy silently ignore the file.
# Real environment variables win (override=False): a systemd unit or container
# -e flag is a deployment decision, a stray .env in the cwd is not.
envfile.load()

import analytics  # noqa: E402 - must follow envfile.load() above
import astro  # noqa: E402
import bias  # noqa: E402
import billing as bill  # noqa: E402
import cameras as cams  # noqa: E402
import config  # noqa: E402
import entitlements as ent  # noqa: E402
import grids  # noqa: E402
import legal  # noqa: E402
import logging_setup  # noqa: E402
import notify  # noqa: E402
import promo  # noqa: E402
import ratelimit  # noqa: E402
import scheduler  # noqa: E402
import snapshots  # noqa: E402
import verify as vfy  # noqa: E402
import wx  # noqa: E402

# Logging is configured immediately after the environment is loaded, so the
# level and format can come from `.env`, and so every import-time message from
# this point on is actually visible. Before this, the root logger had no handler
# and `log.info` went nowhere.
logging_setup.configure()

log = logging.getLogger("wx")

app = FastAPI(title="Greece Sky and Weather")

# Endpoints whose rate limit differs from the default. The middleware looks the
# path up here; anything absent uses DEFAULT.
_LIMITS = {
    "/api/brief": ratelimit.FORECAST,
    "/api/expert": ratelimit.FORECAST,
    "/api/skewt": ratelimit.FORECAST,
    "/api/verify": ratelimit.VERIFY,
    "/api/promo/redeem": ratelimit.REDEEM,
    "/api/auth/passcode": ratelimit.REDEEM,
    "/api/auth/trial": ratelimit.REDEEM,
    "/api/resolve": ratelimit.GEOCODE,
    "/api/reverse": ratelimit.GEOCODE,
    "/api/elevation": ratelimit.GEOCODE,
    "/api/station/ecowitt": ratelimit.STATION,
    "/api/station/register": ratelimit.STATION,
    "/api/analytics": ratelimit.ANALYTICS,
    "/api/notify/test": ratelimit.NOTIFY_TEST,
    "/api/push/subscribe": ratelimit.NOTIFY_WRITE,
    "/api/push/unsubscribe": ratelimit.NOTIFY_WRITE,
    "/api/notify/location": ratelimit.NOTIFY_WRITE,
    "/api/notify/prefs": ratelimit.NOTIFY_WRITE,
}
# Paths that must never be throttled: a rate-limited health check reports the
# service as down, and the legal pages are read by Stripe's crawler.
_LIMIT_EXEMPT = ("/api/health", "/terms", "/privacy", "/refunds", "/licenses",
                 "/static/")


@app.middleware("http")
async def _rate_limit(request: Request, call_next):
    """Body-size ceiling, token-bucket throttle, and the security headers.

    Ordering note: the limiter runs before the route, so an abusive request never
    reaches a handler that would do a network fetch. That is the point of putting
    it here rather than inside each handler. The body check runs first of all, so
    an oversized payload is refused before any parsing or throttling work.
    """
    too_large = _declared_body_too_large(request)
    if too_large is not None:
        return _with_security_headers(too_large)

    path = request.url.path
    limit = _LIMITS.get(path)
    if config.rate_limit_enabled() and limit is not None and _is_throttled_endpoint(path):
        key = ratelimit.client_key(request, config.trust_proxy_headers())
        allowed, retry = ratelimit.LIMITER.allow(key, limit)
        if not allowed:
            wait = max(1, int(retry + 0.999))
            log.warning("rate limit hit: path=%s key=%s retry_after=%ds", path, key[:8], wait)
            return JSONResponse(
                {"detail": "Πολλά αιτήματα σε σύντομο χρόνο. Δοκίμασε ξανά σε λίγο.",
                 "retry_after": wait},
                status_code=429, headers={"Retry-After": str(wait)})

    # Only a body that does not declare its size needs the streaming guard; one
    # with a `Content-Length` was already fully checked above.
    if request.headers.get("content-length") is None:
        overflow = _install_body_counter(request)
        try:
            response = await call_next(request)
        except ClientDisconnect:
            # Raised while the handler read a body that the counter cut off. That
            # only happens once the cap was passed, so it is the oversized case,
            # not a real client disconnect.
            if overflow["exceeded"]:
                log.warning("request body too large (streamed): path=%s cap=%s",
                            path, overflow["cap"])
                return _with_security_headers(JSONResponse(
                    {"detail": "Το αίτημα είναι πολύ μεγάλο."}, status_code=413))
            raise
        if overflow["exceeded"]:
            log.warning("request body too large (streamed): path=%s cap=%s",
                        path, overflow["cap"])
            return _with_security_headers(JSONResponse(
                {"detail": "Το αίτημα είναι πολύ μεγάλο."}, status_code=413))
        return _with_security_headers(response)

    return _with_security_headers(await call_next(request))


def _declared_body_too_large(request: Request) -> JSONResponse | None:
    """413/400 when a declared `Content-Length` is over the cap or malformed.

    Only the cheap, up-front case: a body that announces its size can be refused
    before a single byte is read. A body with no `Content-Length` (chunked) is
    covered by `_install_body_counter`. GET/HEAD/OPTIONS carry no body.
    """
    cap = config.max_body_bytes()
    if cap <= 0 or request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    declared = request.headers.get("content-length")
    if declared is None:
        return None
    try:
        size = int(declared)
    except ValueError:
        return JSONResponse({"detail": "Μη έγκυρο Content-Length."}, status_code=400)
    if size > cap:
        log.warning("request body too large: path=%s declared=%s cap=%s",
                    request.url.path, declared, cap)
        return JSONResponse({"detail": "Το αίτημα είναι πολύ μεγάλο."}, status_code=413)
    return None


def _install_body_counter(request: Request) -> dict:
    """Wrap the ASGI receive channel so a body without `Content-Length` is capped.

    A chunked request is not trusted to be small: the wrapper counts the bytes
    actually delivered to the handler and, once the cap is passed, ends the stream
    (`http.disconnect`) so the server stops reading instead of buffering without
    bound. The middleware turns the `exceeded` flag into a 413 afterwards. This
    request object is the same one handed to `call_next`, so the wrapper is in
    force for the handler's body read.

    Returns a mutable record the caller inspects once the response is produced.
    """
    record = {"exceeded": False, "cap": config.max_body_bytes()}
    cap = record["cap"]
    if cap <= 0 or request.method in ("GET", "HEAD", "OPTIONS"):
        return record
    original_receive = request._receive
    seen = 0

    async def _counting_receive():
        nonlocal seen
        message = await original_receive()
        if message.get("type") == "http.request":
            seen += len(message.get("body", b""))
            if seen > cap:
                record["exceeded"] = True
                return {"type": "http.disconnect"}
        return message

    request._receive = _counting_receive
    return record


def _is_throttled_endpoint(path: str) -> bool:
    return not any(path.startswith(p) for p in _LIMIT_EXEMPT)


def _with_security_headers(response: Response) -> Response:
    """Headers that cost nothing and close a class of browser-side issue.

    No Content-Security-Policy yet: the page inlines its own script and style, so
    a CSP without a nonce would break it, and a nonce needs the response to be
    assembled differently. Recorded here as deliberate, not forgotten.
    """
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


@app.on_event("startup")
async def _report_optional_deps():
    """Say once, at boot, which optional extras are missing and how to fix them.

    A missing ephem only shows up as a blank card later, in a browser, which is
    the slowest possible way to learn that `pip` and `uvicorn` are different
    Pythons. The log line names the interpreter, so the fix is one command.
    """
    log.info("starting: env=%s log_level=%s rate_limit=%s cache_max_mb=%s",
             config.env_name(), os.environ.get("WX_LOG_LEVEL", "INFO"),
             "on" if config.rate_limit_enabled() else "off",
             round(config.cache_max_bytes() / 1e6) if config.cache_max_bytes() else "none")
    for problem in config.validate_runtime():
        log.warning("configuration: %s", problem)
    # Fatal preconditions last, so the warnings above are already in the log when
    # this raises. In production a missing entitlement secret must stop the boot
    # rather than quietly serving a different configuration than was intended.
    config.assert_production_ready()
    try:
        promo.init_db()
    except Exception as e:
        log.error("promo database unavailable: %s", e)
    if not astro._HAVE_EPHEM:
        err = astro.import_error()
        if err and astro.is_missing():
            log.warning("Το ephem λείπει από τον interpreter %s (Python %s). Η κάρτα "
                        "'Ο Ουρανός Τώρα' θα δείχνει μη διαθέσιμη. Εγκατάσταση: %s",
                        astro.interpreter(), astro._py_version(), astro.install_hint())
        elif err:
            log.error("Το ephem υπάρχει αλλά απέτυχε να φορτώσει στον interpreter %s "
                      "(Python %s): %s. Η κάρτα 'Ο Ουρανός Τώρα' θα δείχνει μη "
                      "διαθέσιμη.", astro.interpreter(), astro._py_version(), err)
        else:
            log.warning("Το ephem λείπει / δεν φορτώθηκε στον interpreter %s "
                        "(Python %s), χωρίς καταγεγραμμένο σφάλμα. Η κάρτα "
                        "'Ο Ουρανός Τώρα' θα δείχνει μη διαθέσιμη.",
                        astro.interpreter(), astro._py_version())
    if not bill.checkout_available():
        log.info("Οι πληρωμές είναι ανενεργές (λείπει: %s). Το passcode και το trial "
                 "λειτουργούν κανονικά.", ", ".join(bill.missing_config()))
    # RAM grids are opt-in: a deploy that has not watched the refresher run keeps
    # the proven per-point path. The task handle is kept so shutdown can cancel it.
    global _ram_task
    _ram_task = scheduler.start(grids.STORE)
    if _ram_task is not None:
        log.info("RAM grids ενεργά (WX_USE_RAM_GRIDS): ο scheduler ξεκίνησε στο background.")
    global _notify_task
    _notify_task = notify.start(prime=_notify_prime)
    if _notify_task is not None:
        log.info("Οι ειδοποιήσεις είναι ενεργές: ο loop ξεκίνησε στο background.")
    elif notify.interval_s() > 0:
        log.info("Οι ειδοποιήσεις είναι ανενεργές (λείπει: %s).",
                 notify.unavailable_reason() or "disabled")


_ram_task: asyncio.Task | None = None
_notify_task: asyncio.Task | None = None


async def _notify_prime(subs: list[dict]) -> dict:
    """Build one hourly series per distinct cell for the whole batch.

    This is the shared-read point: ten subscribers in Ilioupoli cost one set of
    model reads, not ten. It uses the RAM grids when they are loaded and falls
    back to the same per-point path the forecast uses otherwise, so notifications
    work with `WX_USE_RAM_GRIDS` off as well.
    """
    out: dict = {}
    for sub in subs:
        key = notify.cell_label(sub.get("cell_lat"), sub.get("cell_lon"))
        if key is None or key in out:
            continue
        try:
            rows = await _notify_series(sub["cell_lat"], sub["cell_lon"])
        except Exception as e:
            log.warning("notify: series for cell %s failed: %s: %s",
                        key, type(e).__name__, e)
            rows = None
        out[key] = rows
    return out


def _normalize_hours(rows: list[dict]) -> list[dict]:
    """Shapes either source's rows into what `notify.evaluate` reads.

    Both sources already hand over `t2m_c` in **Celsius**: the per-point path
    converts at decode time (`wx.gfs_surface_step`) and the shared grid does the
    same when it is built (`scheduler.build_gfs`). Subtracting 273.15 here a
    second time turned an ordinary 25 °C afternoon into -248 °C, which tripped
    the cold rule and left the heat rule unreachable. The value is therefore
    passed through unchanged. `feels` is computed with the same `apparent_temp`
    the UI uses, which also takes Celsius, so an alert and the forecast cannot
    disagree about the number.
    """
    out = []
    for r in rows:
        step = r.get("step", r.get("step_h"))
        if step is None:
            continue
        raw_t = r.get("t2m_c")
        t = None if raw_t is None else raw_t
        wind = r.get("wind_kmh")
        rh = r.get("rh2_pct")
        out.append({
            "step_h": int(step),
            "t": t,
            "rh": rh,
            "precip": r.get("precip_mm"),
            "wind": wind,
            "gust": r.get("gust_kmh"),
            "cape": r.get("cape"),
            "feels": apparent_temp(t, rh, wind) if t is not None else None,
        })
    return out


async def _notify_series(lat: float, lon: float) -> list[dict] | None:
    """Hourly series for one cell, shaped for `notify.evaluate`.

    Prefers the in-memory grids (no network, no GRIB decode) when the flag is on
    and the point is covered; otherwise falls back to `wx.gfs_surface_series`,
    which is cached and works continent-wide. CAPE is attached only where the
    model provides it, because the storm rule must not be run on a guess.
    """
    hours = 24
    # `ensure_loaded` matters here as much as in `/api/brief`: after a restart the
    # persisted run is restored on first use instead of waiting for the refresher's
    # first network pass, so alerts and the forecast read the same grid from the
    # first request on.
    ram = (grids.STORE.ensure_loaded("gfs", grids.gfs_scope())
           if grids.flag_enabled() else None)
    if ram is not None and not grids.covers(ram, lat, lon):
        ram = None
    if ram is not None:
        steps = [s for s in wx.gfs_steps(hours) if ram.step_index(s) is not None]
        if steps:
            return _normalize_hours(grids.surface_rows(ram, lat, lon, steps))
    rows = await wx.gfs_surface_series(lat, lon, hours=hours)
    return _normalize_hours(rows)


@app.on_event("shutdown")
async def _stop_ram_scheduler():
    if _notify_task is not None and not _notify_task.done():
        _notify_task.cancel()
    if _ram_task is not None and not _ram_task.done():
        _ram_task.cancel()

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

PAGE = r"""<!doctype html><html lang="el"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Greece Sky and Weather</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#070b14">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Greece Sky">
<script src="/static/chart.umd.min.js"></script>
<style>
/* Dark glass, iOS-style. --card is translucent on purpose: the blur in
   .glass below has nothing to blur without something behind it, so the page
   gradients on body::before are load-bearing, not decoration. */
:root{color-scheme:dark;
      --bg:#070b14;--bg2:#0b1220;
      --card:rgba(18,24,38,.75);--card2:rgba(18,24,38,.88);
      --line:rgba(255,255,255,.08);--line2:rgba(255,255,255,.18);
      --ink:#eef2f8;--dim:#9aa7bd;--accent:#4da3ff;--accent2:#0f8fd6;
      --good:#34d399;--warn:#fbbf24;--bad:#f87171;
      --blur:16px;--shadow:0 10px 30px 0 rgba(0,0,0,.4)}
*{box-sizing:border-box}
html{background:var(--bg)}
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:0;
  background:transparent;color:var(--ink)}
/* A fixed wash behind everything. Kept on a pseudo-element rather than on body
   so it stays put while scrolling without background-attachment:fixed, which
   stutters on iOS. html carries the flat colour, so this sits below content. */
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
  background:
    radial-gradient(1100px 700px at 8% -10%,rgba(77,163,255,.22),transparent 62%),
    radial-gradient(900px 650px at 104% 4%,rgba(139,92,246,.20),transparent 62%),
    radial-gradient(1000px 700px at 50% 116%,rgba(16,185,129,.15),transparent 62%),
    linear-gradient(180deg,#070b14,#0b1220 55%,#070b14)}
header{background:rgba(12,17,28,.62);border-bottom:1px solid var(--line);padding:14px 18px;
  backdrop-filter:blur(var(--blur)) saturate(160%);
  -webkit-backdrop-filter:blur(var(--blur)) saturate(160%)}
h1{margin:0 0 10px;font-size:19px;font-weight:650}
h3{font-size:15px;margin:22px 0 10px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input,button{font-size:15px;padding:8px 12px;border-radius:8px;border:1px solid var(--line);
  background:var(--card2);color:var(--ink);font-family:inherit}
input::placeholder{color:var(--dim)}
input:focus-visible,button:focus-visible{outline:2px solid var(--accent);
  outline-offset:2px}
button{cursor:pointer;font-weight:550;transition:background .15s ease,border-color .15s ease}
button.primary{background:linear-gradient(180deg,#5eb0ff,var(--accent));color:#04121f;
  border-color:transparent;font-weight:650}
main{padding:18px;max-width:1040px;margin:0 auto}
#where{color:var(--dim);font-size:14px;margin:12px 0 0}
.tabs{display:flex;gap:4px;margin:18px 0 0;border-bottom:1px solid var(--line);flex-wrap:wrap}
.tabs button{border:0;border-bottom:2px solid transparent;border-radius:0;background:none;
  padding:10px 14px;color:var(--dim)}
.tabs button.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:650}
.panel{display:none;padding-top:18px}
.panel.active{display:block}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
/* --- the glass surface ---
   iOS reads as glass because light passes through it: a translucent fill, a
   blur of whatever is behind, a 1px highlight where the edge catches the light,
   and a wide soft shadow for lift. All four are needed; with a near-opaque fill
   the blur is invisible and the card just looks flat grey.

   -webkit- is kept alongside the standard property because Safari still needs it,
   and Safari is the browser this is meant to evoke. Where backdrop-filter is
   unsupported (older Firefox, some Android WebViews) @supports below raises the
   fill opacity so text keeps its contrast - a glass card over a bright gradient
   with no blur is a legibility bug, not a graceful degradation. */
.glass,.card,.fcard,.cta,.cam,.chartbox,.veri,.verdict,.plan,.opt,
details.geo,.tierbar,.toggle,.sheet,table{
  background:var(--card);
  border:1px solid var(--line);
  backdrop-filter:blur(var(--blur)) saturate(160%);
  -webkit-backdrop-filter:blur(var(--blur)) saturate(160%);
  box-shadow:var(--shadow);
}
/* the edge highlight: a 1px top inner line, the way a glass pane catches light */
.glass,.card,.fcard,.cta,.cam,.chartbox,.veri,.verdict,.plan,.opt,.sheet{
  box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.07)}
.card{border-radius:12px;padding:14px}
.card .k{font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.03em}
.card .v{font-size:26px;font-weight:650;margin-top:4px;letter-spacing:-.02em}
.card .s{font-size:12px;color:var(--dim);margin-top:3px;line-height:1.45}
.verdict{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);
  border-radius:10px;padding:14px 16px;margin:16px 0}
.verdict h3{margin:0 0 6px;font-size:15px}
.verdict ul{margin:6px 0 0;padding-left:20px;font-size:14px;line-height:1.65}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
  border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:7px 9px;text-align:right;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th{background:rgba(255,255,255,.06);font-weight:600;font-size:12px;color:var(--dim)}
tbody tr:last-child td{border-bottom:0}
img{max-width:100%;height:auto;border-radius:10px;border:1px solid var(--line);background:#0d1420}
.chartbox{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-top:8px}
.note{font-size:12px;color:var(--dim);margin-top:8px;line-height:1.6}
.err{background:rgba(248,113,113,.12);border-color:rgba(248,113,113,.35);color:#fca5a5}
.badge{display:inline-block;font-size:12px;padding:3px 9px;border-radius:20px;
  background:rgba(77,163,255,.16);color:var(--accent);font-weight:600;
  border:1px solid rgba(77,163,255,.22)}
.badge.good{background:rgba(52,211,153,.15);color:var(--good);border-color:rgba(52,211,153,.28)}
.badge.warn{background:rgba(251,191,36,.15);color:var(--warn);border-color:rgba(251,191,36,.28)}
.badge.bad{background:rgba(248,113,113,.15);color:var(--bad);border-color:rgba(248,113,113,.28)}
.bar{height:8px;border-radius:4px;background:rgba(255,255,255,.09);overflow:hidden;margin:8px 0 4px}
.bar > i{display:block;height:100%;border-radius:4px}
.bar.good > i{width:100%;background:var(--good)}
.bar.warn > i{width:60%;background:var(--warn)}
.bar.bad > i{width:30%;background:var(--bad)}
.bar.unknown > i{width:10%;background:var(--dim)}
.spin{color:var(--dim);font-size:14px;padding:20px 0}
footer{max-width:1040px;margin:0 auto;padding:18px;color:var(--dim);font-size:11.5px;line-height:1.7}
footer a{color:var(--accent)}
/* --- site footer ---
   Server-rendered, so contacts and legal links survive a failed forecast load.
   Stripe's reviewers are not a browser running our JS either. */
#site{max-width:1040px;margin:22px auto 0;padding:20px 18px 30px;border-top:1px solid var(--line);
  display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:18px}
#site .fcol{display:flex;flex-direction:column;gap:3px;align-items:flex-start}
#site .fhead{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  color:var(--dim);margin-bottom:5px}
#site a{color:var(--ink);text-decoration:none;font-size:13px;padding:2px 0}
#site a:hover{color:var(--accent);text-decoration:underline}
#site .fbot{grid-column:1/-1;border-top:1px solid var(--line);padding-top:14px;margin-top:4px;
  color:var(--dim);font-size:12px}
.consent{font-size:11.5px;line-height:1.65;color:var(--dim);margin:12px 0 0;text-align:left}
.consent a{color:var(--accent)}
/* --- favorites (localStorage, no server round trip) --- */
#favbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
#favbar .favadd{padding:7px 12px;font-size:13px}
#favlist{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px}
.favwrap{position:relative}
.favchip{display:flex;align-items:stretch;border:1px solid var(--line);border-radius:999px;
  background:var(--card);overflow:hidden;font-size:13px}
.favchip button{border:0;background:transparent;color:var(--ink);padding:7px 10px;cursor:pointer;
  font-size:13px;line-height:1}
.favchip button.pick:hover{background:rgba(255,255,255,.07)}
.favchip button.del{color:var(--dim);border-left:1px solid var(--line);padding:7px 11px}
.favchip button.del:hover{color:#ff9f9f;background:rgba(255,80,80,.10)}
#favempty{font-size:12.5px;color:var(--dim)}
details.geo{border:1px solid var(--line);background:var(--card);border-radius:10px;margin:14px 0;padding:10px 14px}
details.geo summary{cursor:pointer;font-weight:600;font-size:14px}
details.geo .body{padding-top:10px;font-size:13px}
details.geo label{display:block;margin:8px 0 3px;color:var(--dim);font-size:12px}
details.geo input{width:100%;max-width:280px}
.warnbox{background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.30);color:var(--warn);border-radius:8px;
  padding:9px 11px;font-size:12px;line-height:1.55;margin-top:8px}
.recolist{font-size:13px;line-height:1.7;margin:6px 0 0;padding-left:18px}
/* --- free/pro gating --- */
.locked{position:relative;overflow:hidden}
.locked .blurred{filter:blur(8px);pointer-events:none;user-select:none;opacity:.75}
.lockover{position:absolute;inset:0;display:flex;flex-direction:column;gap:8px;
  align-items:center;justify-content:center;text-align:center;padding:18px;
  background:linear-gradient(180deg,rgba(7,11,20,.72),rgba(7,11,20,.90))}
.lockover .lk{font-size:26px;line-height:1}
.lockover h4{margin:0;font-size:15px}
.lockover p{margin:0;font-size:12.5px;color:var(--dim);max-width:440px;line-height:1.55}
.tierbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:12.5px;
  border:1px solid var(--line);background:var(--card);border-radius:10px;padding:9px 12px;margin:14px 0}
.pill{font-size:11px;font-weight:700;letter-spacing:.04em;padding:3px 9px;border-radius:20px;
  background:rgba(255,255,255,.09);color:var(--dim)}
.pill.pro{background:var(--accent);color:#04121f}
.pill.free{background:rgba(52,211,153,.16);color:var(--good)}
.modal{position:fixed;inset:0;background:rgba(18,22,28,.55);display:none;
  align-items:center;justify-content:center;padding:18px;z-index:9000}
.modal.open{display:flex}
.sheet{background:var(--card);border-radius:16px;max-width:460px;width:100%;
  padding:22px;box-shadow:0 18px 50px rgba(0,0,0,.3);max-height:92vh;overflow:auto}
.sheet h3{margin:0 0 4px;font-size:19px}
.sheet .sub{color:var(--dim);font-size:13px;margin-bottom:16px}
.plan{border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:10px;cursor:pointer;
  background:var(--card);position:relative}
.plan.sel{border-color:var(--accent);box-shadow:var(--shadow),0 0 0 2px rgba(77,163,255,.28)}
.plan .prow{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.plan .amt{font-size:24px;font-weight:700}
.plan .amt span{font-size:13px;font-weight:400;color:var(--dim)}
.plan .m{font-size:12.5px;color:var(--dim);margin-top:4px}
.plan .best{position:absolute;top:-9px;right:12px;background:var(--good);color:#fff;
  font-size:10.5px;font-weight:700;padding:3px 9px;border-radius:20px;letter-spacing:.03em;color:#04121f}
.sheet ul.ul{margin:8px 0 0;padding-left:20px;font-size:13.5px;line-height:1.7}
.sheet button.wide{width:100%;margin-top:12px;padding:12px}
.codebox{margin-top:14px;border-top:1px solid var(--line);padding-top:14px}
.codebox .row{margin-top:8px}
.msg{font-size:13px;margin-top:10px}
.msg.ok{color:var(--good)}
.msg.err{color:var(--bad)}
.note2{font-size:11.5px;color:var(--dim);margin-top:12px;line-height:1.6}
/* Recurring-billing disclosure. Sits with the consent text, same weight: the
   fact that the charge repeats is a term of sale, not a footnote to hide. */
.autorenew{font-size:11.5px;line-height:1.65;color:var(--dim);margin:10px 0 0;text-align:left}
.autorenew b{color:var(--ink);font-weight:600}
/* Subscription management. Quiet by design: no accent border, no call to
   action, and hidden entirely unless the visitor has a subscription to manage. */
.manage{margin-top:14px;border-top:1px solid var(--line);padding-top:12px;font-size:12.5px}
.manage .mrow{display:flex;justify-content:space-between;align-items:baseline;gap:10px;
  color:var(--dim);padding:3px 0}
.manage .mrow b{color:var(--ink);font-weight:600}
.manage .mtoggle{display:flex;align-items:center;gap:8px;margin-top:9px;color:var(--dim)}
.manage .mtoggle input{margin:0;width:auto}
.manage button{margin-top:9px}
.manage .mlabel{font-size:11px;color:var(--dim);flex:1}
/* placeholder cells in locked previews: a soft bar, never a plausible number */
td.ph{background:linear-gradient(90deg,rgba(255,255,255,.08),rgba(255,255,255,.13));border-radius:5px;height:13px;
  min-width:52px;display:inline-block;margin:3px 0}
.phchart{height:200px;border-radius:10px;
  background:repeating-linear-gradient(45deg,rgba(255,255,255,.07),rgba(255,255,255,.07) 9px,rgba(255,255,255,.04) 9px,rgba(255,255,255,.04) 18px)}
/* --- the "now" hero: the first screen for the everyday user ---
   This is the only thing most visitors read. Everything technical lives below it
   behind the Εξειδικευμένα tab, so this block has to stand on its own: icon,
   temperature, feels-like, today's range, and one plain sentence. */
.nowhero{position:relative;overflow:hidden;border-radius:20px;padding:24px 26px;
  background:var(--card);border:1px solid var(--line);
  backdrop-filter:blur(var(--blur)) saturate(160%);
  -webkit-backdrop-filter:blur(var(--blur)) saturate(160%);
  box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.07);
  display:grid;grid-template-columns:auto 1fr;gap:22px;align-items:center}
/* the wash is what the glass samples; without it the card is flat grey */
.nowhero:before{content:"";position:absolute;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(560px 320px at 6% 0%,rgba(77,163,255,.20),transparent 68%),
    radial-gradient(480px 300px at 100% 100%,rgba(139,92,246,.16),transparent 68%)}
.nowhero > *{position:relative;z-index:1}
.nowhero .glyph{font-size:62px;line-height:1;text-align:center;min-width:104px;
  filter:drop-shadow(0 6px 18px rgba(0,0,0,.45))}
.nowhero .cond{font-size:15px;font-weight:600;color:var(--dim);text-align:center;
  margin-top:8px;letter-spacing:.01em}
.nowhero .big{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.nowhero .temp{font-size:66px;font-weight:700;letter-spacing:-.035em;line-height:1;
  font-variant-numeric:tabular-nums}
.nowhero .temp sup{font-size:26px;font-weight:300;vertical-align:super;
  letter-spacing:0;color:var(--dim)}
/* units are deliberately lighter than the number: the value is the message */
.nowhero .unit{font-size:17px;font-weight:300;color:var(--dim)}
.nowhero .feels{font-size:14.5px;color:var(--dim);margin-left:2px}
.nowhero .feels b{font-weight:650;color:var(--ink);font-variant-numeric:tabular-nums}
.nowhero .range{display:flex;gap:16px;flex-wrap:wrap;margin-top:12px;font-size:14px;color:var(--dim)}
.nowhero .range b{color:var(--ink);font-weight:650;font-variant-numeric:tabular-nums}
.nowhero .line{margin:13px 0 0;font-size:16px;line-height:1.55;font-weight:450;
  max-width:56ch}
.nowhero .chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.nowhero.severe{border-color:rgba(248,113,113,.42);
  box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.07),0 0 0 1px rgba(248,113,113,.18)}

/* --- colour-coded chips: temperature, wind, reliability ---
   Tone classes are produced by temp_tone()/wind_tone() on the server so the
   thresholds live in one place and are unit-testable. */
.chip{display:inline-flex;align-items:center;gap:7px;font-size:13px;font-weight:600;
  padding:5px 12px;border-radius:20px;border:1px solid transparent;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.08);white-space:nowrap}
.chip .lb{font-weight:400;opacity:.75}
.chip.freezing{background:rgba(96,165,250,.18);color:#93c5fd;border-color:rgba(96,165,250,.30)}
.chip.cold{background:rgba(56,189,248,.16);color:#7dd3fc;border-color:rgba(56,189,248,.28)}
.chip.mild{background:rgba(52,211,153,.15);color:var(--good);border-color:rgba(52,211,153,.28)}
.chip.hot{background:rgba(251,146,60,.17);color:#fdba74;border-color:rgba(251,146,60,.30)}
.chip.extreme{background:rgba(248,113,113,.18);color:#fca5a5;border-color:rgba(248,113,113,.32)}
.chip.calm{background:rgba(52,211,153,.15);color:var(--good);border-color:rgba(52,211,153,.28)}
.chip.breezy{background:rgba(77,163,255,.16);color:var(--accent);border-color:rgba(77,163,255,.28)}
.chip.strong{background:rgba(251,191,36,.16);color:var(--warn);border-color:rgba(251,191,36,.30)}
.chip.dangerous{background:rgba(248,113,113,.18);color:#fca5a5;border-color:rgba(248,113,113,.34)}
.chip.unknown{background:rgba(255,255,255,.09);color:var(--dim);border-color:var(--line)}

/* --- collapsible expert sections ---
   The advanced data is dense by request: a pro wants the numbers packed and
   aligned, not spaced out. Each section is a <details> so the simple user never
   scrolls through CAPE and helicity to reach tomorrow's forecast. */
.xsec{border:1px solid var(--line);border-radius:14px;margin:12px 0;overflow:hidden;
  background:var(--card);backdrop-filter:blur(var(--blur)) saturate(160%);
  -webkit-backdrop-filter:blur(var(--blur)) saturate(160%);box-shadow:var(--shadow)}
.xsec > summary{cursor:pointer;padding:13px 16px;font-size:14.5px;font-weight:650;
  display:flex;align-items:center;gap:10px;list-style:none;user-select:none}
.xsec > summary::-webkit-details-marker{display:none}
.xsec > summary::after{content:"▸";margin-left:auto;color:var(--dim);font-size:13px;
  transition:transform .18s ease}
.xsec[open] > summary::after{transform:rotate(90deg)}
.xsec > summary:hover{background:rgba(255,255,255,.04)}
.xsec > summary:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.xsec .sumhint{margin-left:8px;font-size:12px;font-weight:400;color:var(--dim)}
.xsec .xbody{padding:2px 16px 16px}
.xsec[open] > summary{border-bottom:1px solid var(--line)}
.xsec .xbody > h3:first-child{margin-top:8px}
/* dense grids for the pro view: tighter than the simple cards on purpose */
.xgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.xgrid .xcell{background:rgba(11,18,32,.72);padding:9px 11px}
.xgrid .xk{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim)}
.xgrid .xv{font-size:18px;font-weight:650;margin-top:2px;font-variant-numeric:tabular-nums;
  letter-spacing:-.01em}
.xgrid .xu{font-size:10.5px;font-weight:300;color:var(--dim);margin-left:3px}
.xgrid .xt{font-size:10.5px;margin-top:2px}
.xgrid .xt.good{color:var(--good)}
.xgrid .xt.warn{color:var(--warn)}
.xgrid .xt.bad{color:var(--bad)}
.xgrid .xt.dim{color:var(--dim)}
/* pro tables: same numerals, no wasted height.
   These live inside an already-glassy .xsec, so they carry no fill or border of
   their own: a second translucent pane stacked on the first both muddies the
   text and has nothing new to blur. */
.dense{width:100%;border-collapse:collapse;font-size:12.5px;background:none;
  border:0;font-variant-numeric:tabular-nums}
.dense th,.dense td{padding:5px 9px;text-align:right;border-bottom:1px solid var(--line)}
.dense th:first-child,.dense td:first-child{text-align:left}
.dense thead th{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--dim);font-weight:600;position:sticky;top:0;background:#0e1524}
.dense tbody tr:last-child td{border-bottom:0}
.dense tbody tr:hover td{background:rgba(255,255,255,.035)}
.dense .mm{color:var(--dim);font-weight:300}
/* the Skew-T is an image: kill the default inline-image gap so it sits flush */
.xsec img.skewt{display:block;width:100%;border-radius:12px;border:1px solid var(--line);
  background:#131e30;margin:0}

/* --- time selector for the Εξειδικευμένα tab --- */
.xpick{margin:0 0 12px}
.xpick .xh{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);
  font-weight:600;display:flex;align-items:center;gap:8px}
.xpick .xrow{display:flex;gap:10px;flex-wrap:wrap;margin-top:8px}
.xpick label{display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--dim);
  font-weight:600;text-transform:uppercase;letter-spacing:.05em;flex:1;min-width:120px}
.xpick select{appearance:none;width:100%;padding:9px 11px;border-radius:10px;
  border:1px solid var(--line);background:rgba(255,255,255,.05);color:var(--ink);
  font:inherit;font-size:13.5px;font-variant-numeric:tabular-nums;cursor:pointer}
.xpick select:disabled{opacity:.55;cursor:default}
.xpick select:focus{outline:2px solid var(--accent);outline-offset:1px}
.xpick .xspin{font-size:10.5px;color:var(--accent);text-transform:none;letter-spacing:0}
.xpick .xspin::before{content:'●';margin-right:4px;animation:pulse 1s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.3}50%{opacity:1}}

/* --- "Ο Ουρανός Τώρα": sun / moon / twilight ---
   An astronomy card on a weather site earns its place by being genuinely local:
   every value is computed for the selected point, and the altitude is passed in
   because a village at 600 m really does see the Sun earlier than the coast. */
.astro{--astro-ink:#eef2f8;margin:14px 0}
.astro .ahead{display:flex;justify-content:space-between;align-items:flex-start;
  gap:14px;flex-wrap:wrap;margin-bottom:14px}
.astro .atitle{font-size:15px;font-weight:650;display:flex;align-items:center;gap:8px}
.astro .atitle .ic{font-size:19px}
.astro .awhen{font-size:12.5px;color:var(--dim);line-height:1.6;text-align:right}
.astro .awhen b{color:var(--ink);font-weight:600}
.astro .agrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(268px,1fr));gap:14px}
/* each body sits in its own inner glass pane so the two read as parallel */
.astro .apanel{position:relative;overflow:hidden;border:1px solid var(--line);
  border-radius:14px;padding:14px 15px;
  background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.012))}
.astro .apanel.sun:before,.astro .apanel.moon:before{
  content:"";position:absolute;right:-56px;top:-56px;width:150px;height:150px;
  border-radius:50%;pointer-events:none}
.astro .apanel.sun:before{background:radial-gradient(circle,rgba(251,191,36,.26),transparent 68%)}
.astro .apanel.moon:before{background:radial-gradient(circle,rgba(148,163,255,.20),transparent 68%)}
.astro .apanel > *{position:relative}
.astro .aphead{display:flex;align-items:center;gap:9px;margin-bottom:11px}
.astro .aphead .big{font-size:27px;line-height:1}
.astro .aphead .nm{font-size:14.5px;font-weight:650}
.astro .aphead .st{font-size:11.5px;color:var(--dim);margin-top:1px}
.astro .aphead .now{margin-left:auto;text-align:right;font-size:11.5px;color:var(--dim);
  line-height:1.45}
.astro .aphead .now b{display:block;font-size:15px;color:var(--ink);
  font-variant-numeric:tabular-nums}
/* three-column stat strip: rise / transit / set, aligned on the numerals */
.astro .aevents{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden;
  margin-bottom:11px}
.astro .aevents .ev{background:rgba(11,18,32,.66);padding:8px 9px;text-align:center}
.astro .aevents .ek{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim)}
.astro .aevents .evv{font-size:15.5px;font-weight:650;margin-top:2px;
  font-variant-numeric:tabular-nums}
.astro .aevents .ev.miss .evv{color:var(--dim);font-weight:400}
.astro .evsub{font-size:10px;color:var(--dim);margin-top:1px}
.astro .arow{display:flex;justify-content:space-between;gap:10px;font-size:12.5px;
  padding:5px 0;border-bottom:1px solid var(--line)}
.astro .arow:last-child{border-bottom:0}
.astro .arow .k{color:var(--dim)}
.astro .arow .v{font-weight:600;font-variant-numeric:tabular-nums;text-align:right}
/* phase bar: illumination as a filled fraction, with the age underneath */
.astro .phasebar{height:7px;border-radius:4px;background:rgba(255,255,255,.10);
  overflow:hidden;margin:9px 0 4px}
.astro .phasebar > i{display:block;height:100%;border-radius:4px;
  background:linear-gradient(90deg,#8ea2ff,#e7ecff)}
/* the 24 h track: pure SVG, no chart library, so it costs nothing to render */
.astro .track{margin-top:10px}
.astro .track svg{display:block;width:100%;height:74px;overflow:visible}
.astro .track .ttl{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--dim);margin-bottom:3px}
.astro .track .axis{stroke:rgba(255,255,255,.16);stroke-width:1;stroke-dasharray:3 4}
.astro .track .curve{fill:none;stroke-width:2}
.astro .track.sun .curve{stroke:#fbbf24}
.astro .track.moon .curve{stroke:#a5b4fc}
.astro .track .area{opacity:.16}
.astro .track.sun .area{fill:#fbbf24}
.astro .track.moon .area{fill:#a5b4fc}
.astro .track .hr{stroke:rgba(255,255,255,.10);stroke-width:1}
.astro .track .lbl{fill:#9aa7bd;font-size:9px}
.astro .track .nowdot{fill:#fff;stroke:#0b1220;stroke-width:1.5}
.astro .anote{font-size:11.5px;color:var(--dim);line-height:1.65;margin-top:12px}
.astro .awarn{font-size:11px;color:var(--warn);margin-top:6px}
/* Horizontal scroll for wide numeric tables (hourly, daily, model comparison).
   Overscroll is contained so a swipe at the edge does not page the whole body,
   and the scrollbar is slim so it does not eat a row of type. */
.tscroll{overflow-x:auto;overscroll-behavior-x:contain;-webkit-overflow-scrolling:touch;
  max-width:100%}
.tscroll table{margin-bottom:0}
.tscroll::-webkit-scrollbar{height:6px}
.tscroll::-webkit-scrollbar-thumb{background:rgba(255,255,255,.18);border-radius:3px}

/* --- 10-day daily carousel ---
   Ten day-cards is more than a phone can show, so the strip scrolls. Scroll-snap
   is what makes it feel like cards rather than a cut-off table: without it a swipe
   stops mid-card and the reader loses which day they are on. The strip is a flex
   row with a fixed card width, so 2 cards fit a phone and 5 fit a desktop, and the
   overflow is the only thing that moves - never the page. */
.dstrip{display:flex;gap:10px;overflow-x:auto;overscroll-behavior-x:contain;
  -webkit-overflow-scrolling:touch;scroll-snap-type:x mandatory;
  padding:2px 2px 10px;margin:0 -2px}
.dstrip::-webkit-scrollbar{height:6px}
.dstrip::-webkit-scrollbar-thumb{background:rgba(255,255,255,.18);border-radius:3px}
.dstrip::-webkit-scrollbar-track{background:transparent}
.dcard{flex:0 0 auto;width:150px;scroll-snap-align:start;
  background:rgba(11,18,32,.55);border:1px solid var(--line);border-radius:14px;
  padding:12px 13px;display:flex;flex-direction:column;gap:7px}
.dcard .dhead{display:flex;align-items:baseline;justify-content:space-between;gap:6px}
.dcard .dname{font-size:13.5px;font-weight:650}
.dcard .ddate{font-size:11px;color:var(--dim);font-variant-numeric:tabular-nums}
.dcard .dicon{font-size:27px;line-height:1.1}
.dcard .dtemps{font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.dcard .dtmax{font-size:20px;font-weight:650}
.dcard .dtmin{font-size:14px;font-weight:400;color:var(--dim);margin-left:5px}
.dcard .drow{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--dim)}
.dcard .drow b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.dcard .dtoday{font-size:10px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--accent);font-weight:600}
/* Locked days are placeholders with no numbers, blurred under a lock. The real
   values never reach the browser on the free tier, so the blur is presentation
   and the gate is server-side. */
.dcard.locked{position:relative;overflow:hidden;padding:0}
.dcard.locked .blurred{filter:blur(7px);opacity:.55;pointer-events:none;user-select:none;
  padding:12px 13px;display:flex;flex-direction:column;gap:7px}
.dcard.locked .lockover{position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:5px;text-align:center;padding:8px}
.dcard.locked .lockover .lk{font-size:22px;line-height:1}
.dcard .probadge{font-size:9.5px;font-weight:700;letter-spacing:.07em;
  background:rgba(251,191,36,.16);color:#fbbf24;border:1px solid rgba(251,191,36,.34);
  border-radius:5px;padding:1px 6px}
.dcard .dph{height:11px;border-radius:4px;
  background:linear-gradient(90deg,rgba(255,255,255,.08),rgba(255,255,255,.13))}
/* Wireframe bars rather than shaped numbers: a blurred strip that looks like a
   forecast invites someone to screenshot it and believe it. */
.dcard .dphrow{display:flex;gap:4px;align-items:flex-end;height:26px}
.dcard .dphrow i{flex:1;border-radius:3px;background:rgba(255,255,255,.10)}
/* The standalone upsell banner: same lock treatment, but in normal flow rather
   than absolutely positioned over a blurred placeholder. */
.locked.upsell .lockover{position:static;inset:auto}
@media (max-width:640px){
  .dcard{width:142px}
}
@media (max-width:640px){
  .astro .agrid{grid-template-columns:1fr;gap:11px}
  .astro .awhen{text-align:left}
  .astro .apanel{padding:12px 13px}
  /* On a phone the table is allowed to be wider than the card; the wrapper
     scrolls. Without the min-width the cells squeeze and wrap every value. */
  .tscroll table{min-width:480px}
  .astro .aevents .evv{font-size:14px}
  .astro .track svg{height:64px}
}

/* --- why-gsw hero + feature cards --- */
.hero{background:linear-gradient(160deg,#0d2b4b,#0b6bcb 62%,#0f8fd6);
  border-radius:16px;padding:30px 26px;color:#fff;position:relative;overflow:hidden}
.hero:before{content:"";position:absolute;right:-70px;top:-70px;width:230px;height:230px;
  border-radius:50%;background:rgba(255,255,255,.09)}
.hero:after{content:"";position:absolute;right:30px;bottom:-90px;width:170px;height:170px;
  border-radius:50%;background:rgba(255,255,255,.06)}
.hero h2{margin:0 0 8px;font-size:25px;font-weight:700;letter-spacing:-.01em;position:relative}
.hero p{margin:0;font-size:14.5px;line-height:1.65;max-width:640px;
  color:rgba(255,255,255,.92);position:relative}
.hero .eyebrow{font-size:11.5px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  color:rgba(255,255,255,.75);margin-bottom:10px;position:relative}
.feat{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px;margin:16px 0}
.fcard{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;
  display:flex;flex-direction:column;gap:9px;position:relative;overflow:hidden}
.fcard:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent)}
.fcard .ic{font-size:23px;line-height:1}
.fcard h3{margin:0;font-size:15px;font-weight:650}
.fcard p{margin:0;font-size:13px;line-height:1.62;color:var(--dim)}
.fcard .tag{align-self:flex-start;font-size:11px;font-weight:700;letter-spacing:.03em;
  padding:3px 9px;border-radius:20px;background:rgba(77,163,255,.16);color:var(--accent);margin-top:auto}
/* --- PRO CTA banner --- */
.cta{background:var(--card);border:1px solid var(--line);border-radius:16px;
  padding:24px;margin:20px 0;box-shadow:0 6px 22px rgba(13,43,75,.07)}
.cta h3{margin:0 0 4px;font-size:19px}
.cta .lead{margin:0 0 4px;font-size:14px;color:var(--ink)}
.cta .leadsub{margin:0 0 16px;font-size:13px;color:var(--dim)}
.cta .opts{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.opt{border:1px solid var(--line);border-radius:12px;padding:16px;text-align:left;
  background:var(--card);cursor:pointer;position:relative;font-family:inherit}
.opt:hover{border-color:var(--line2)}
.opt.sel{border-color:var(--accent);box-shadow:var(--shadow),0 0 0 2px rgba(77,163,255,.28)}
.opt .amt{font-size:23px;font-weight:700;display:block;margin:6px 0 2px}
.opt .amt small{font-size:12.5px;font-weight:400;color:var(--dim)}
.opt .nm{font-size:12px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:var(--dim)}
.opt .ds{font-size:12px;color:var(--dim);line-height:1.5}
.opt .best{position:absolute;top:-9px;right:12px;background:var(--good);color:#fff;
  font-size:10.5px;font-weight:700;padding:3px 9px;border-radius:20px;letter-spacing:.03em}
.opt.free{border-style:dashed}
.cta .go{margin-top:14px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.cta .fineprint{font-size:11.5px;color:var(--dim);margin-top:12px;line-height:1.6}
/* --- live sky cameras --- */
.camgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin:14px 0}
.cam{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;
  display:flex;flex-direction:column}
.cam .stage{position:relative;aspect-ratio:16/9;background:#0e1116;display:flex;
  align-items:center;justify-content:center;overflow:hidden}
.cam .stage img{width:100%;height:100%;object-fit:cover;display:block}
.cam .stage .off{color:var(--dim);font-size:12.5px;text-align:center;padding:16px;line-height:1.6}
.cam .stage .camload{position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;color:var(--dim);font-size:12.5px}
.cam .live{position:absolute;top:9px;left:9px;display:flex;align-items:center;gap:6px;
  background:rgba(17,17,17,.72);color:#fff;font-size:10.5px;font-weight:700;
  letter-spacing:.06em;padding:4px 9px;border-radius:20px;backdrop-filter:blur(4px)}
.cam .live i{width:7px;height:7px;border-radius:50%;background:#ff3b30;display:block;
  animation:pulse 1.6s ease-in-out infinite}
.cam .live.off i{background:var(--dim);animation:none}
/* The LIVE badge starts hidden and is revealed only when a frame actually
   arrives. `[hidden]` is honoured explicitly because the author `display:flex`
   above would otherwise beat the user-agent rule and show it anyway. */
.cam .live[hidden]{display:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.cam .meta{padding:13px 15px 15px}
.cam .meta .nm{font-size:14.5px;font-weight:650;margin:0 0 2px}
.cam .meta .rg{font-size:12px;color:var(--dim)}
.cam .actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:11px}
.cam .actions button{padding:7px 11px;font-size:12.5px}
.cam .actions button[disabled]{opacity:.45;cursor:not-allowed}
.cam .notebox{font-size:11.5px;color:var(--warn);background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.30);
  border-radius:7px;padding:7px 9px;margin-top:10px;line-height:1.55}
.cam .cad{font-size:11.5px;color:var(--dim);margin-top:6px}
.cam .upd{font-size:11.5px;color:var(--dim);margin-top:2px;min-height:14px}
.cam .stage .golive{position:absolute;bottom:9px;right:9px;background:rgba(17,17,17,.78);color:#fff;
  border:1px solid rgba(255,255,255,.18);font-size:11.5px;font-weight:700;letter-spacing:.04em;
  padding:6px 12px;border-radius:20px;cursor:pointer;backdrop-filter:blur(4px)}
.cam .stage .golive:hover{background:rgba(255,59,48,.85)}
.cam .stage.playing{display:none}
/* When a live player is open its own region takes the stage's place in the flex
   column and the stage (snapshot + its overlay badges/button) is hidden
   entirely, so no custom control can sit over the YouTube player. The iframe is
   plain in-flow content -- official YouTube embed, untouched. */
.cam .camplayer{display:none;position:relative;aspect-ratio:16/9;background:#000;overflow:hidden}
.cam .camplayer.playing{display:block}
.cam .camplayer .camframe{position:absolute;inset:0;width:100%;height:100%;border:0;display:block;background:#000}
/* The control bar is the live flow's home: an in-flow status line and the close
   control, always *below* the player region, never over the iframe. `[hidden]`
   is honoured explicitly because the author `display:flex` would otherwise beat
   the user-agent `[hidden]{display:none}` and leave an empty strip in every card. */
.cam .camctl{display:flex;align-items:center;justify-content:space-between;gap:10px;
  flex-wrap:wrap;padding:8px 12px 0}
.cam .camctl[hidden]{display:none}
.cam .camctl .camstate{display:inline-flex;align-items:center;gap:10px;flex-wrap:wrap}
.cam .camctl .livestate{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;
  font-weight:700;letter-spacing:.05em;color:#fca5a5}
.cam .camctl .livestate i{width:7px;height:7px;border-radius:50%;background:#ff3b30;display:block;
  animation:pulse 1.6s ease-in-out infinite}
.cam .camctl .livehint{font-size:11px;color:var(--dim)}
.cam .camctl .closecam{background:rgba(17,17,17,.78);
  color:#fff;border:1px solid rgba(255,255,255,.18);font-size:11.5px;padding:5px 11px;
  border-radius:18px;cursor:pointer;font-family:inherit}
.cam .camplayer .livefb{position:absolute;inset:auto 12px 12px 12px;z-index:2;background:rgba(17,17,17,.85);
  color:var(--dim);font-size:12px;padding:9px 11px;border-radius:8px;text-align:center}
.camhead{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.toggle{display:inline-flex;align-items:center;gap:9px;font-size:13px;cursor:pointer;
  border:1px solid var(--line);background:var(--card);border-radius:22px;padding:7px 14px;
  font-family:inherit;color:var(--ink)}
.toggle .dot{width:9px;height:9px;border-radius:50%;background:var(--dim);display:block}
.toggle.on{border-color:rgba(248,113,113,.55);background:rgba(248,113,113,.14);color:#fca5a5}
.toggle.on .dot{background:#ff3b30;animation:pulse 1.6s ease-in-out infinite}
/* --- verification card --- */
.veri{margin:14px 0}
.veri table{width:100%;border-collapse:collapse;font-size:13px}
.veri th,.veri td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right}
.veri th:first-child,.veri td:first-child{text-align:left}
.veri thead th{font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim)}
.veri .lead{font-weight:600}
.veri .src{font-size:11.5px;color:var(--dim);line-height:1.65;margin-top:10px}
.veri .big{display:flex;gap:20px;flex-wrap:wrap;margin:6px 0 16px}
.veri .big .b .k{font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim)}
.veri .big .b .v{font-size:26px;font-weight:700}
.veri .big .b .v small{font-size:13px;font-weight:400;color:var(--dim)}
/* --- premium surface layer ---
   The glass base is defined once, near .card. This layer only adds what is
   specific: a hover lift, the accent hairline, and the gradient tints on the
   tints that sit on top of glass (badges, table headers). */
.fcard,.cta,.cam,.chartbox{transition:box-shadow .18s ease,transform .18s ease}
.fcard:hover,.cam:hover,.chartbox:hover,.card:hover{
  box-shadow:0 12px 40px rgba(0,0,0,.48),inset 0 1px 0 rgba(255,255,255,.10);
  transform:translateY(-2px)}
/* the accent edge becomes a gradient so it reads as designed, not as a border */
.fcard:before{background:linear-gradient(180deg,var(--accent),var(--accent2) 55%,transparent)}
.veri{
  background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:16px 18px;margin:14px 0}
.veri table{border:0;background:none}
.badge,.pill,.fcard .tag{box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}
.badge{background:linear-gradient(180deg,rgba(77,163,255,.20),rgba(77,163,255,.12))}
th{background:linear-gradient(180deg,rgba(255,255,255,.09),rgba(255,255,255,.04))}
/* tab underline reads as an active indicator rather than plain colour */
.tabs button{transition:color .15s ease,border-color .15s ease}
.tabs button:hover{color:var(--ink)}
button.primary{box-shadow:0 2px 14px rgba(77,163,255,.35)}
button.primary:hover{filter:brightness(1.08)}
/* the hero is the first thing seen; give it a diagonal sheen */
.hero{box-shadow:0 20px 50px rgba(0,0,0,.5)}
.hero:before{background:radial-gradient(circle at 30% 30%,rgba(255,255,255,.16),transparent 70%)}
/* A glass card over a bright wash can still lose text contrast. If the browser
   cannot blur the backdrop, the translucency buys nothing, so make the fill
   opaque enough to carry the text on its own. */
@supports not ((backdrop-filter:blur(2px)) or (-webkit-backdrop-filter:blur(2px))){
  :root{--card:rgba(17,23,36,.96);--card2:rgba(17,23,36,.98)}
  header{background:rgba(12,17,28,.96)}
}
/* Respect a stated preference for less motion rather than animating anyway. */
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms !important;animation-iteration-count:1 !important;
    transition-duration:.01ms !important}
  .fcard:hover,.cam:hover,.chartbox:hover,.card:hover{transform:none}
}
/* --- small screens ---
   The cards are the product; on a phone the hero, feature grid and the two
   verification numbers have to survive without horizontal scroll. */
@media (max-width:640px){
  header{padding:12px 14px}
  h1{font-size:17px}
  .row input{flex:1 1 100%}
  .hero{padding:22px 18px;border-radius:13px}
  .hero h2{font-size:20px}
  .hero p{font-size:13.5px}
  .feat{grid-template-columns:1fr;gap:11px}
  .fcard{padding:16px}
  .cta{padding:18px}
  .cta h3{font-size:17px}
  .cta .opts{grid-template-columns:1fr}
  .camgrid{grid-template-columns:1fr;gap:11px}
  .cam .camctl{justify-content:flex-start}
  .sheet{padding:18px;border-radius:13px}
  .veri .big{gap:14px}
  .veri .big .b .v{font-size:21px}
  /* a four-column numeric table only fits if the type shrinks and the padding goes */
  .veri th,.veri td{padding:7px 5px;font-size:12px}
  .veri thead th{font-size:10px;letter-spacing:0}
  .camhead{flex-direction:column;align-items:flex-start}
  /* The hero stacks: the icon on the left of a 66px number leaves no room for a
     sentence on a phone, and the number is what the visitor came for. */
  .nowhero{grid-template-columns:1fr;gap:12px;padding:18px 16px;border-radius:16px;
    text-align:left}
  .nowhero .glyph{font-size:46px;min-width:0;text-align:left}
  .nowhero .cond{text-align:left;font-size:13.5px}
  .nowhero .temp{font-size:52px}
  .nowhero .temp sup{font-size:20px}
  .nowhero .line{font-size:14.5px}
  .nowhero .range{gap:12px;font-size:13px}
  .xsec > summary{padding:12px 13px;font-size:13.5px}
  .xsec .sumhint{display:none}
  .xsec .xbody{padding:2px 13px 13px}
  .xgrid{grid-template-columns:repeat(auto-fit,minmax(90px,1fr))}
  .xgrid .xv{font-size:15.5px}
  .dense{font-size:11.5px}
  .dense th,.dense td{padding:5px 5px}
}
</style></head><body>
<header>
  <h1>Greece Sky and Weather</h1>
  <div class="row">
    <button class="primary" onclick="geo()">Η θέση μου</button>
    <input id="q" placeholder="γράψε πόλη ή τοποθεσία" onkeydown="if(event.key==='Enter')search()">
    <button onclick="search()">Αναζήτηση</button>
  </div>
  <div id="where"></div>

  <div id="favbar">
    <button class="favadd" id="favaddbtn" onclick="favAddCurrent()" style="display:none">
      ☆ Προσθήκη στα Αγαπημένα</button>
    <span id="favempty">Τα αγαπημένα σου θα εμφανιστούν εδώ.</span>
  </div>
  <div id="favlist"></div>
</header>
<main>
  <div class="tabs">
    <button id="tab-simple" class="active" onclick="showTab('simple')">Απλή πρόγνωση</button>
    <button id="tab-expert" onclick="showTab('expert')">Εξειδικευμένα</button>
    <button id="tab-why" onclick="showTab('why')">🎯 Αρχιτεκτονική &amp; Ακρίβεια</button>
  </div>
  <section id="panel-simple" class="panel active"><div id="simple"><p class="spin">Διάλεξε τοποθεσία.</p></div></section>
  <section id="panel-expert" class="panel"><div id="expert"><p class="spin">Διάλεξε τοποθεσία.</p></div></section>
  <section id="panel-why" class="panel">
    <div class="hero">
      <div class="eyebrow">Greece Sky and Weather</div>
      <h2>Γιατί να διαλέξεις το Greece Sky and Weather;</h2>
      <p>Οι κοινές υπηρεσίες δίνουν μία μέση τιμή για ολόκληρη την περιοχή. Εμείς
        υπολογίζουμε την πρόγνωση ειδικά για το σημείο σου — με το πραγματικό του
        υψόμετρο, με τη σύγκριση τριών μοντέλων και με πλήρη ραδιοβόλιση για την
        πρόγνωση έντονων φαινομένων.</p>
    </div>

    <div class="feat">
      <div class="fcard">
        <div class="ic">⛰️</div>
        <h3>Διόρθωση θερμοκρασίας με πραγματικό υψόμετρο</h3>
        <p>Δυναμική προσαρμογή με βαθμίδα θερμοκρασίας υπολογισμένη από το προφίλ
          του μοντέλου και το ακριβές υψόμετρο του σημείου σου — από GPS,
          συντεταγμένες ή χειροκίνητα.</p>
        <span class="tag">Lapse rate από το sounding</span>
      </div>
      <div class="fcard">
        <div class="ic">🛰️</div>
        <h3>Συμφωνία 3 μοντέλων (GFS, ECMWF, ICON)</h3>
        <p>Σύγκριση των κορυφαίων μοντέλων δίπλα-δίπλα. Όταν συμφωνούν, υπάρχει
          ομοφωνία μεταξύ τους· όταν αποκλίνουν, το βλέπεις αμέσως. Είναι ένδειξη
          συμφωνίας, όχι εγγύηση ότι η πρόγνωση θα επαληθευτεί.</p>
        <span class="tag">Ένδειξη συμφωνίας μοντέλων</span>
      </div>
      <div class="fcard">
        <div class="ic">🌩️</div>
        <h3>Ραδιοβόλιση έντονων φαινομένων</h3>
        <p>Εντοπισμός αστάθειας και δυναμικής με διαδραστικά κατακόρυφα προφίλ:
          SBCAPE, MLCAPE, MUCAPE, shear, SRH, ύψος LCL και σημείο πήξης.</p>
        <span class="tag">Διαδραστικό Skew-T</span>
      </div>
      <div class="fcard">
        <div class="ic">📹</div>
        <h3>Ζωντανές κάμερες ουρανού</h3>
        <p>Πραγματική εικόνα από το σημείο, όχι μόνο μοντέλο. Σύγκρινε την
          πρόγνωση με τον ουρανό που βλέπεις τώρα.</p>
        <span class="tag">Ilioupoli &amp; Glinado</span>
      </div>
      <div class="fcard">
        <div class="ic">📊</div>
        <h3>Επαλήθευση έναντι ERA5</h3>
        <p>Δεν ζητάμε να μας πιστέψεις. Κάθε πρόγνωση συγκρίνεται με το
          reanalysis της Copernicus και το σφάλμα είναι δημόσιο.</p>
        <span class="tag">Μετρήσιμη ακρίβεια</span>
      </div>
    </div>

    <h3>Ζωντανές κάμερες</h3>
    <div class="camhead">
      <div class="sub" id="cam-sub">Ζωντανή εικόνα από τα σημεία μας.</div>
      <button class="toggle" id="cam-toggle" onclick="toggleLive()">
        <span class="dot"></span><span id="cam-toggle-label">🔴 LIVE COVERAGE</span>
      </button>
    </div>
    <div class="camgrid" id="cams"><p class="spin">Φόρτωση καμερών…</p></div>

    <h3>Ακρίβεια έναντι ERA5</h3>
    <div id="veri"><p class="spin">Διάλεξε τοποθεσία για να υπολογιστεί το σφάλμα έναντι ERA5.</p></div>

    <div id="why-cta"></div>
  </section>
</main>
<footer id="attr"></footer>
<footer id="site">
  <div class="fcol">
    <div class="fhead">Επικοινωνία</div>
    <a href="__YOUTUBE__" target="_blank" rel="noopener noreferrer">🔴 YouTube: Greece Sky and Weather</a>
    <a href="__FACEBOOK__" target="_blank" rel="noopener noreferrer">🔵 Facebook: Greece Sky and Weather</a>
    <a href="mailto:__EMAIL__">✉️ __EMAIL__</a>
  </div>
  <div class="fcol">
    <div class="fhead">Νομικά</div>
    <a href="/terms">Όροι Χρήσης</a>
    <a href="/privacy">Πολιτική Απορρήτου</a>
    <a href="/refunds">Πολιτική Επιστροφών / Ακυρώσεων</a>
    <a href="/licenses">Άδειες δεδομένων</a>
  </div>
  <div class="fbot">&copy; 2026 Greece Sky and Weather</div>
</footer>

<div class="modal" id="promodal" onclick="if(event.target===this)closeModal()">
  <div class="sheet" role="dialog" aria-modal="true" aria-labelledby="pm-title">
    <h3 id="pm-title">Αναβάθμιση σε PRO</h3>
    <div class="sub" id="pm-sub">Ξεκλείδωσε πρόγνωση 10 ημερών — δωρεάν οι πρώτες 3 — με Skew-T, δείκτες αστάθειας και σύγκριση 3 μοντέλων.</div>

    <div class="plan sel" id="plan-yearly" onclick="pickPlan('yearly')">
      <div class="best">Best Value — Έκπτωση <span id="disc">44.3</span>%</div>
      <div class="prow"><span id="y-label">Ετήσιο</span><span class="amt">€<span id="y-price">19.99</span><span>/έτος</span></span></div>
      <div class="m">Ισοδυναμεί με €<span id="y-eq">1.67</span>/μήνα — χρέωση μία φορά τον χρόνο.</div>
    </div>

    <div class="plan" id="plan-monthly" onclick="pickPlan('monthly')">
      <div class="prow"><span id="m-label">Μηνιαίο</span><span class="amt">€<span id="m-price">2.99</span><span>/μήνα</span></span></div>
      <div class="m">Ακύρωση όποτε θέλεις, χωρίς δέσμευση.</div>
    </div>

    <ul class="ul" id="pm-unlocks"></ul>

    <button class="wide primary" id="pm-cta" onclick="checkout()">Συνέχεια στην πληρωμή</button>
    <button class="wide" id="pm-trial" onclick="startTrial()" style="margin-top:8px">Ξεκίνα δωρεάν δοκιμή <span id="pm-trial-days">2</span> ημερών</button>
    <!-- Recurring-billing disclosure at the point of sale. Not fine print by
         accident: the customer must know the charge repeats before paying. -->
    <p class="autorenew" id="pm-autorenew"></p>
    <!-- Placed at the point of sale, not only in the footer: this is where the
         customer forms the expectation the refund policy has to answer. -->
    <p class="consent">Συνεχίζοντας αποδέχεσαι τους
      <a href="/terms" target="_blank" rel="noopener">Όρους Χρήσης</a>, την
      <a href="/privacy" target="_blank" rel="noopener">Πολιτική Απορρήτου</a> και την
      <a href="/refunds" target="_blank" rel="noopener">Πολιτική Επιστροφών</a>.
      Η πρόγνωση είναι εκτίμηση με σφάλμα, όχι εγγύηση.</p>
    <div class="msg" id="pm-msg"></div>

    <!-- Subscription management. Deliberately quiet: a bordered box that only
         appears for a visitor who actually has a subscription. -->
    <div class="manage" id="pm-manage" hidden></div>

    <!-- Promo/gift window, shown only when the caller holds one. Filled from the
         existing /api/promo/status; the device id it also returns is operator
         data and is deliberately never rendered here. -->
    <div class="manage" id="pm-promoline" hidden></div>

    <!-- Notifications (PRO). Kept compact on purpose: a badge, the notification
         area, and the alert types. The browser/OS permission state and the
         app-level on/off state are shown as two separate things, because they
         fail independently and only the browser knows the first. -->
    <div class="manage" id="pm-notify" hidden>
      <div class="mrow">
        <b>🔔 Ειδοποιήσεις</b>
        <span id="pm-notify-badge" class="mlabel">—</span>
      </div>
      <div id="pm-notify-body"></div>
      <div class="msg" id="pm-notify-msg"></div>
    </div>

    <div class="codebox">
      <div class="row" style="justify-content:space-between">
        <b style="font-size:13px">Έχεις κωδικό PRO;</b>
      </div>
      <div class="row">
        <input id="pm-promo" placeholder="Κωδικός PRO" autocomplete="off"
               onkeydown="if(event.key==='Enter')redeemPromo()">
        <button onclick="redeemPromo()">Εξαργύρωση</button>
      </div>
      <div class="msg" id="pm-promo-msg"></div>
    </div>

    <!-- Operator/admin passcode. Deliberately quieter and collapsed: this is the
         operator's own key, not something a customer is expected to have, so it
         must not be the field a gift-code holder reaches for first. -->
    <details class="codebox" id="pm-admin-wrap">
      <summary style="font-size:13px">Κωδικός διαχειριστή</summary>
      <div class="row">
        <input id="pm-code" placeholder="Κωδικός διαχειριστή" autocomplete="off"
               onkeydown="if(event.key==='Enter')redeem()">
        <button onclick="redeem()">Ενεργοποίηση</button>
      </div>
      <div class="msg" id="pm-code-msg"></div>
    </details>

    <div class="note2" id="pm-note"></div>
  </div>
</div>
<script>
/* No map library is loaded. Location comes from text search, the browser's
   geolocation, or a favourite, and elevation is resolved server-side. That drops
   Leaflet and the tile provider entirely, which removes the {z}/{x}/{y} raster
   licence question rather than merely documenting it. */
let CHART=null, CUR=null;
let TOKEN=localStorage.getItem('wx_token')||null;
let TIER={tier:'free',is_pro:false};      // filled from the first /api/brief response
let PLANS=null;
let SELECTED_PLAN='yearly';
let VERI_POINT=null, VERI_DONE=null;

/* Camera names, regions and feed URLs all come from configuration, so they are
   untrusted text as far as the DOM is concerned. */
function esc(s){
  return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',
    '"':'&quot;',"'":'&#39;'}[c]));
}
/* For a value placed inside an inline handler *argument*, which is a JavaScript
   string literal, not HTML text. `esc()` alone is the wrong tool there: the
   browser decodes `&#39;` back to `'` before the JS parser runs, so an
   apostrophe would end the literal and break — or inject into — the handler.
   Escape for the JS literal first (backslash, quote, control characters, and
   `<` so no `</script>` can form), then let `esc()` handle the attribute
   quoting. Always use as `esc(jsq(value))` inside an inline handler. */
function jsq(s){
  return String(s==null?'':s)
    .replace(/\\/g,'\\\\')
    .replace(/'/g,"\\'")
    .replace(/\r/g,'\\r').replace(/\n/g,'\\n')
    .replace(/</g,'\\x3c');
}

const FREE_HOURS=72, PRO_HOURS=240;

/* ---------- plan / modal ---------- */
/* ---------------------------------------------------------------- analytics
   First-party and deliberately small. Events are queued and flushed as one
   batched POST so a visitor who clicks around does not generate a request per
   click. Coordinates are sent only for forecast events, and the server stores
   them as a ~50 km cell, never as the point. There is no third-party script and
   no cookie: turning off `WX_ANALYTICS` on the server makes this endpoint a
   no-op, and nothing here is needed for the page to work. */
const WX_EVENTS=[];
let wxFlushTimer=null;
function track(name,opts){
  try{
    const ev={name};
    if(opts){
      if(typeof opts.value==='number') ev.value=opts.value;
      if(typeof opts.lat==='number'&&isFinite(opts.lat)) ev.lat=opts.lat;
      if(typeof opts.lon==='number'&&isFinite(opts.lon)) ev.lon=opts.lon;
      if(opts.meta&&typeof opts.meta==='object') ev.meta=opts.meta;
    }
    WX_EVENTS.push(ev);
    if(WX_EVENTS.length>=20){ flushEvents(); return; }
    if(wxFlushTimer) return;
    wxFlushTimer=setTimeout(flushEvents,4000);   // trailing batch
  }catch(e){}   // analytics must never break the page
}
function flushEvents(){
  if(wxFlushTimer){ clearTimeout(wxFlushTimer); wxFlushTimer=null; }
  if(!WX_EVENTS.length) return;
  const batch=WX_EVENTS.splice(0,WX_EVENTS.length);
  try{
    const payload=JSON.stringify({events:batch});
    // sendBeacon survives the page unload that a normal fetch would lose.
    if(navigator.sendBeacon){
      navigator.sendBeacon('/api/analytics',new Blob([payload],{type:'application/json'}));
      return;
    }
    fetch('/api/analytics',{method:'POST',headers:{'Content-Type':'application/json'},
      body:payload,keepalive:true}).catch(()=>{});
  }catch(e){}
}
addEventListener('visibilitychange',()=>{ if(document.visibilityState==='hidden') flushEvents(); });
addEventListener('pagehide',flushEvents);

/* Opening an expert section is a <details> toggle, not a function call, so the
   event is read from the DOM once per open rather than instrumenting each
   renderer. `toggle` fires on every details in the page; the id decides. */
document.addEventListener('toggle',e=>{
  const el=e.target;
  if(!el || !el.open || !el.id) return;
  if(el.id==='x-skewt') track('skewt_opened');
  else if(el.id==='x-models') track('model_comparison_opened');
},true);

function openModal(){
  track('pro_paywall_viewed');
  track('promo_code_opened');   // the PRO-code field is visible on every open now
  document.getElementById('promodal').classList.add('open');
  document.getElementById('pm-admin-wrap').open=false;
  // A returning visitor can open the modal straight from the hero, before any
  // forecast has fetched /api/plans; fill it in rather than showing a blank card.
  if(PLANS){ fillPlans(); return; }
  fetch('/api/plans').then(r=>r.json()).then(p=>{ PLANS=p; fillPlans(); }).catch(()=>{});
}
function closeModal(){ document.getElementById('promodal').classList.remove('open'); }

/* The in-page CTA reuses the same PLANS payload as the modal, so a price change
   in entitlements.py shows up in both places and cannot half-apply. */
function renderCta(){
  const el=document.getElementById('why-cta'); if(!el) return;
  // The tab can be opened before any forecast is loaded, so pull the plans
  // payload here; otherwise the banner would sit empty until a location is chosen.
  if(!PLANS){
    el.innerHTML='<div class="cta"><p class="spin">Φόρτωση πλάνων…</p></div>';
    fetch('/api/plans').then(r=>r.json()).then(p=>{
      PLANS=p; renderCta();
    }).catch(()=>{ el.innerHTML='<div class="cta"><p class="spin">Τα πλάνα δεν φορτώθηκαν.</p></div>'; });
    return;
  }
  const P=PLANS.pricing, d=PLANS.yearly_discount_percent, pro=TIER.is_pro;
  const locked=PLANS.pro_hours-PLANS.free_hours;
  const dayFrom=PLANS.free_hours/24+1, dayTo=PLANS.pro_hours/24;
  el.innerHTML='<div class="cta">'
    +'<h3>'+(pro?'Το PRO είναι ενεργό':'Και οι υπόλοιπες '+(dayTo-dayFrom+1)+' ημέρες;')+'</h3>'
    +'<p class="lead">'+(pro
      ? 'Έχεις πλήρη πρόσβαση σε '+PLANS.pro_display+' πρόγνωση, Skew-T, δείκτες αστάθειας και σύγκριση 3 μοντέλων.'
      : 'Οι ημέρες '+dayFrom+'–'+dayTo+' είναι θολές. Με το PRO βλέπεις ολόκληρη την πρόγνωση.')+'</p>'
    +'<p class="leadsub">Δωρεάν: '+PLANS.free_display+' · PRO: '+PLANS.pro_display
      +' · Κλειδωμένες ώρες: '+locked+'</p>'
    +(pro?'<div class="go"><button class="primary" onclick="openModal()">Διαχείριση συνδρομής</button></div>'
    :'<div class="opts">'
      +'<button class="opt free sel" id="opt-trial" onclick="pickOpt(\'trial\')">'
        +'<span class="nm">Δωρεάν δοκιμή</span>'
        +'<span class="amt">€0 <small>/'+PLANS.trial_days+' ημέρες</small></span>'
        +'<span class="ds">Ξεκλείδωμα όλων των λειτουργιών PRO. Χωρίς κάρτα, με πραγματικό κωδικό.</span></button>'
      +'<button class="opt" id="opt-monthly" onclick="pickOpt(\'monthly\')">'
        +'<span class="nm">Μηνιαίο</span>'
        +'<span class="amt">€'+P.monthly.price.toFixed(2)+' <small>/μήνα</small></span>'
        +'<span class="ds">Χρέωση κάθε μήνα. Ακύρωση όποτε θέλεις.</span></button>'
      +'<button class="opt" id="opt-yearly" onclick="pickOpt(\'yearly\')">'
        +'<span class="best">Καλύτερη τιμή — '+d+'% έκπτωση</span>'
        +'<span class="nm">Ετήσιο</span>'
        +'<span class="amt">€'+P.yearly.price.toFixed(2)+' <small>/έτος</small></span>'
        +'<span class="ds">€'+P.yearly.monthly_equivalent.toFixed(2)+' τον μήνα, με μία χρέωση.</span></button>'
      +'</div>'
      +'<div class="go"><button class="primary" onclick="ctaGo()" id="cta-go">Ξεκίνα δωρεάν δοκιμή '
        +PLANS.trial_days+' ημερών</button>'
        +'<button onclick="openModal()">Δες όλα τα πλάνα</button></div>'
      +'<p class="leadsub">Έχεις κωδικό PRO ή δωροκάρτα; '
        +'<a href="#" onclick="openModal();return false">Εξαργύρωσέ τον εδώ</a></p>'
      +'<div class="msg" id="cta-msg"></div>')
    +'<div class="fineprint">'+(PLANS.checkout_available
      ? 'Η πληρωμή γίνεται με ασφάλεια μέσω Stripe. Δεν βλέπουμε στοιχεία κάρτας.'
      : 'Η πληρωμή δεν είναι διαθέσιμη αυτή τη στιγμή.')
      +'</div>'
    +'</div>';
  SELECTED_PLAN='trial';
}
function pickOpt(p){
  SELECTED_PLAN=p;
  for(const k of ['trial','monthly','yearly']){
    const b=document.getElementById('opt-'+k); if(b) b.classList.toggle('sel',k===p);
  }
  const go=document.getElementById('cta-go'); if(!go) return;
  go.textContent = p==='trial' ? 'Ξεκίνα δωρεάν δοκιμή '+PLANS.trial_days+' ημερών'
    : p==='yearly' ? 'Συνέχεια με €'+PLANS.pricing.yearly.price.toFixed(2)+' τον χρόνο'
    : 'Συνέχεια με €'+PLANS.pricing.monthly.price.toFixed(2)+' τον μήνα';
}
function ctaGo(){
  if(SELECTED_PLAN!=='trial'){ openModal(); pickPlan(SELECTED_PLAN); return; }
  startTrial();
}
async function refreshTier(){
  try{
    const h={}; if(TOKEN) h['X-WX-Token']=TOKEN;
    const me=await (await fetch('/api/me',{headers:h})).json();
    TIER={tier:me.tier,is_pro:me.is_pro,source:me.source,
          pro_hours:PLANS?PLANS.pro_hours:240,
          free_hours:PLANS?PLANS.free_hours:72,
          locked_hours:PLANS?PLANS.pro_hours:240};
  }catch(e){}
}
async function startTrial(){
  const go=document.getElementById('cta-go');
  if(go){ go.disabled=true; go.textContent='Ενεργοποίηση…'; }
  try{
    const r=await fetch('/api/auth/trial',{method:'POST'});
    const d=await r.json();
    if(!r.ok) throw new Error(d.detail||'Αποτυχία ενεργοποίησης');
    TOKEN=d.token; localStorage.setItem('wx_token',TOKEN);
    PLANS=null;                                   // refetch, so the tier bar is truthful
    await refreshTier();                          // and so is the CTA's PRO wording
    if(CUR) await load(CUR.lat,CUR.lon,CUR.label,{elevation_m:CUR.elev});
    else { renderCta(); }
  }catch(e){
    if(go){ go.disabled=false; go.textContent='Ξεκίνα δωρεάν δοκιμή'; }
    uiMsg('Σφάλμα: '+e.message,false);   // in place, never a browser alert
  }
}
/* Inline message for the trial/checkout flow. Written to the modal's own line and
   to the why-tab CTA line, so the failure lands inside whichever surface the
   visitor is looking at instead of a browser alert that steals the whole tab. */
function uiMsg(text, ok){
  for(const id of ['pm-msg','cta-msg']){
    const m=document.getElementById(id); if(!m) continue;
    m.className='msg '+(ok?'ok':'err'); m.textContent=text;
  }
}
function pickPlan(p){
  SELECTED_PLAN=p;
  document.getElementById('plan-yearly').classList.toggle('sel',p==='yearly');
  document.getElementById('plan-monthly').classList.toggle('sel',p==='monthly');
  renderAutoRenew();
  renderAutoRenewNote();
}
/* The bottom note must match whether payment actually works. Claiming "the
   button does not charge" after wiring Stripe, or the reverse, is a lie either
   way; the server's checkout_available is the source of truth. */
function renderAutoRenewNote(){
  const el=document.getElementById('pm-note'); if(!el) return;
  if(PLANS && PLANS.checkout_available){
    el.textContent='Η πληρωμή εκτελείται από τη Stripe. Δεν βλέπουμε στοιχεία κάρτας. '
      +'Η ενεργοποίηση με κωδικό λειτουργεί επίσης.';
    return;
  }
  el.textContent='Η πληρωμή δεν είναι διαθέσιμη αυτή τη στιγμή.'
    +' Η ενεργοποίηση με κωδικό λειτουργεί πραγματικά.';
}
function fillPlans(){
  const P=PLANS.pricing;
  const d=PLANS.yearly_discount_percent;
  document.getElementById('disc').textContent=d;
  // state the real percentage, not a rounded-up marketing figure
  document.getElementById('y-price').textContent=P.yearly.price.toFixed(2);
  document.getElementById('m-price').textContent=P.monthly.price.toFixed(2);
  document.getElementById('y-eq').textContent=P.yearly.monthly_equivalent.toFixed(2);
  document.getElementById('pm-unlocks').innerHTML=
    PLANS.unlocks.map(u=>'<li>'+u+'</li>').join('');
  document.getElementById('pm-sub').textContent=
    'Ξεκλείδωσε πρόγνωση '+PLANS.pro_hours/24+' ημερών — δωρεάν οι πρώτες '
    +PLANS.free_hours/24+' — με Skew-T, δείκτες αστάθειας και σύγκριση 3 μοντέλων.';
  // The modal title must describe what the visitor can actually do here. A PRO
  // visitor opening it from "Διαχείριση συνδρομής" must not be told to upgrade.
  document.getElementById('pm-title').textContent=
    TIER.is_pro?'Διαχείριση PRO':'Αναβάθμιση σε PRO';
  document.getElementById('pm-trial-days').textContent=PLANS.trial_days;
  // already PRO: a trial button would just be noise
  document.getElementById('pm-trial').style.display=TIER.is_pro?'none':'';
  document.getElementById('pm-cta').textContent=
    TIER.is_pro?'Άλλαξε πλάνο':'Συνέχεια στην πληρωμή';
  const cta=document.getElementById('pm-cta');
  // Disabling the button when Stripe is unconfigured is honest; a button that
  // looks live but cannot charge is the worst outcome for a paid product.
  if(PLANS.checkout_available){
    cta.disabled=false; cta.title='';
  }else{
    cta.disabled=true; cta.title='Η πληρωμή δεν είναι ρυθμισμένη σε αυτή την εγκατάσταση.';
  }
  renderAutoRenew(); renderAutoRenewNote(); loadSubscription(); loadPromoLine();
  renderNotify();
}
async function redeem(){
  const code=document.getElementById('pm-code').value.trim();
  const msg=document.getElementById('pm-code-msg');
  if(!code){ msg.className='msg err'; msg.textContent='Γράψε τον κωδικό.'; return; }
  try{
    const r=await fetch('/api/auth/passcode',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});
    const d=await r.json();
    if(!r.ok){ msg.className='msg err'; msg.textContent=d.detail||'Λανθασμένος κωδικός.'; return; }
    TOKEN=d.token; localStorage.setItem('wx_token',TOKEN);
    msg.className='msg ok';
    msg.textContent='Ενεργοποιήθηκε το PRO για '+d.expires_days+' ημέρες.';
    setTimeout(()=>{ closeModal(); if(CUR) reload(); },700);
  }catch(e){ msg.className='msg err'; msg.textContent='Σφάλμα: '+e.message; }
}
async function redeemPromo(){
  const code=document.getElementById('pm-promo').value.trim();
  const msg=document.getElementById('pm-promo-msg');
  if(!code){ msg.className='msg err'; msg.textContent='Γράψε τον κωδικό.'; return; }
  msg.className='msg'; msg.textContent='Έλεγχος…';
  try{
    const r=await fetch('/api/promo/redeem',{method:'POST',
      headers:{'Content-Type':'application/json',...(TOKEN?{'X-WX-Token':TOKEN}:{})},
      body:JSON.stringify({code})});
    const d=await r.json();
    if(!r.ok){
      // The server sends the reason; the wording is deliberately one line.
      msg.className='msg err';
      msg.textContent = r.status===409 ? 'Ο κωδικός έχει ήδη χρησιμοποιηθεί.'
        : r.status===410 ? 'Ο κωδικός έχει λήξει ή εξαντληθεί.'
        : r.status===404 ? 'Μη έγκυρος ή ληγμένος κωδικός.'
        : (d.detail || 'Ο κωδικός δεν έγινε δεκτός.');
      return;
    }
    TOKEN=d.token; localStorage.setItem('wx_token',TOKEN);
    msg.className='msg ok';
    msg.textContent='Το PRO ενεργοποιήθηκε έως '+d.pro_until_iso+'.';
    loadPromoLine();                              // show the window it just granted
    renderNotify();                               // notifications just became available
    setTimeout(()=>{ closeModal(); if(CUR) reload(); },900);
  }catch(e){ msg.className='msg err'; msg.textContent='Σφάλμα: '+e.message; }
}
async function checkout(){
  const m=document.getElementById('pm-msg');
  m.className='msg err';
  if(SELECTED_PLAN==='trial'){ startTrial(); return; }
  m.className='msg';
  m.textContent='Μετάβαση στην ασφαλή πληρωμή…';
  try{
    const r=await fetch('/api/checkout',{method:'POST',
      headers:{'Content-Type':'application/json',
               ...(TOKEN?{'X-WX-Token':TOKEN}:{})},
      body:JSON.stringify({plan:SELECTED_PLAN})});
    const d=await r.json();
    // A 503 detail names the missing server settings; that is operator data, so
    // the user gets a neutral line instead. Other errors are already user-facing.
    if(!r.ok){ m.className='msg err';
      m.textContent = r.status===503 ? 'Η πληρωμή δεν είναι διαθέσιμη αυτή τη στιγμή.'
        : (d.detail||'Η πληρωμή δεν ξεκίνησε.'); return; }
    track('checkout_started',{meta:{plan:SELECTED_PLAN}});
    // Stripe's hosted page handles the card. Nothing card-related touches this app.
    location.href=d.url;
  }catch(e){ m.className='msg err'; m.textContent='Σφάλμα: '+e.message; }
}
/* The recurring-billing term, shown before payment and driven by the server so
   it cannot claim something the backend does not do. */
function renderAutoRenew(){
  const el=document.getElementById('pm-autorenew'); if(!el) return;
  const on = PLANS ? PLANS.auto_renew_default : true;
  if(!on){ el.textContent=''; return; }
  const per = SELECTED_PLAN==='monthly' ? 'μήνα' : 'χρόνο';
  el.innerHTML='Η συνδρομή <b>ανανεώνεται αυτόματα κάθε '+per+'</b> μέχρι να την '
    +'ακυρώσεις. Η ακύρωση γίνεται από τη «Διαχείριση συνδρομής» και ισχύει από '
    +'την επόμενη περίοδο.';
}
/* Entitlement source is not proof of a subscription: the id lives in the signed
   token, so ask the server what it actually knows before showing controls. */
let SUB=null;
async function loadSubscription(){
  const box=document.getElementById('pm-manage'); if(!box) return;
  if(!TOKEN){ box.hidden=true; return; }
  let me=null;
  try{ me=await (await fetch('/api/me',{headers:{'X-WX-Token':TOKEN}})).json(); }
  catch(e){ box.hidden=true; return; }
  if(!me || !me.manageable){ box.hidden=true; return; }
  try{
    const r=await fetch('/api/subscription',{headers:{'X-WX-Token':TOKEN}});
    SUB=await r.json();
    if(!r.ok) throw new Error(SUB.detail||'Σφάλμα');
  }catch(e){
    box.hidden=false;
    box.innerHTML='<div class="mlabel">Δεν ήταν δυνατή η ανάκτηση της συνδρομής: '
      +esc(e.message)+'</div>';
    return;
  }
  renderManage();
}
function fmtDate(ts){
  if(!ts) return '—';
  const d=new Date(ts*1000);
  return d.toLocaleDateString('el-GR',{day:'numeric',month:'long',year:'numeric'});
}
/* The promo/gift window this caller holds, if any. Read from the existing
   /api/promo/status; only the human-readable end date is shown — the endpoint
   also returns the caller's device id for an operator, and that never reaches
   the page. A caller with no token has no device yet, so this is skipped. */
async function loadPromoLine(){
  const box=document.getElementById('pm-promoline'); if(!box) return;
  if(!TOKEN){ box.hidden=true; return; }
  try{
    const r=await fetch('/api/promo/status',{headers:{'X-WX-Token':TOKEN}});
    const d=await r.json();
    if(!r.ok || !d.active){ box.hidden=true; return; }
    box.hidden=false;
    box.innerHTML='<div class="mrow"><span>Κωδικός PRO</span><b>ενεργό έως '
      +esc(d.pro_until_iso||fmtDate(d.pro_until))+'</b></div>';
  }catch(e){ box.hidden=true; }
}

/* ---------- notifications (PRO) ----------
   The server owns eligibility and delivery. This layer only (a) asks the browser
   for permission and a subscription, (b) hands that subscription and the chosen
   notification area to the server, and (c) renders what /api/notify/state says.
   It never decides who is PRO and never contains a PRO feature by itself. */
let NOTIFY=null;                 // last /api/notify/state payload
let NOTIFY_CFG=null;             // /api/push/config
const NOTIFY_LABELS={rain:'Βροχή',storm:'Καταιγίδα',wind:'Άνεμος',temp:'Θερμοκρασία'};

function isIOS(){
  return /iP(hone|ad|od)/.test(navigator.platform) ||
    (navigator.userAgent.includes('Mac') && 'ontouchend' in document);
}
function isStandalone(){
  return window.navigator.standalone===true ||
    (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);
}
function notifySupported(){
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

async function loadNotifyConfig(){
  if(NOTIFY_CFG) return NOTIFY_CFG;
  try{ NOTIFY_CFG=await (await fetch('/api/push/config')).json(); }catch(e){ NOTIFY_CFG=null; }
  return NOTIFY_CFG;
}

async function renderNotify(){
  const box=document.getElementById('pm-notify'); if(!box) return;
  if(!TIER.is_pro){ box.hidden=true; return; }        // FREE sees nothing here
  box.hidden=false;
  const badge=document.getElementById('pm-notify-badge');
  const body=document.getElementById('pm-notify-body');

  const cfg=await loadNotifyConfig();
  if(!cfg || !cfg.available){
    badge.textContent='—';
    body.innerHTML='<div class="mlabel" style="margin-top:8px">Οι ειδοποιήσεις δεν είναι '
      +'διαθέσιμες αυτή τη στιγμή.</div>';
    return;
  }
  try{
    NOTIFY=await (await fetch('/api/notify/state',
      {headers:TOKEN?{'X-WX-Token':TOKEN}:{}})).json();
  }catch(e){ NOTIFY=null; }
  if(!NOTIFY){ badge.textContent='—'; body.innerHTML=''; return; }
  paintNotify();
}

function paintNotify(){
  const badge=document.getElementById('pm-notify-badge');
  const body=document.getElementById('pm-notify-body');
  const n=NOTIFY||{};
  const perm=('Notification' in window)?Notification.permission:'default';

  // Two independent states, shown as two independent lines. "Blocked in the
  // browser" is not the same as "off in the app", and the user can only fix the
  // first one in their browser settings.
  if(!n.subscribed){
    badge.textContent='Ανενεργές';
    badge.style.color='var(--dim)';
    let h='';
    if(isIOS() && !isStandalone()){
      h+='<div class="mlabel" style="margin-top:8px">Στο iPhone οι ειδοποιήσεις '
        +'λειτουργούν μόνο όταν η εφαρμογή είναι στην οθόνη αφετηρίας. Πάτησε '
        +'«Κοινή χρήση» → «Προσθήκη στην οθόνη αφετηρίας» και άνοιξέ την από εκεί.</div>';
    }else if(!notifySupported()){
      h+='<div class="mlabel" style="margin-top:8px">Ο browser δεν υποστηρίζει ειδοποιήσεις.</div>';
    }else{
      h+='<div class="mlabel" style="margin-top:8px">Δεν έχει οριστεί περιοχή ειδοποιήσεων.</div>'
        +'<button onclick="notifyEnable()">📍 Χρησιμοποίηση της τρέχουσας τοποθεσίας μου</button>'
        +'<button onclick="notifyAskSearch()">Αναζήτηση περιοχής</button>';
    }
    h+='<div id="pm-notify-search"></div>';
    body.innerHTML=h;
    return;
  }

  // Subscribed: badge reflects the app-level switch, not the permission.
  badge.textContent = n.active?'Ενεργές':'Ανενεργές';
  badge.style.color = n.active?'var(--good)':'var(--dim)';
  let h='';
  if(perm==='denied'){
    h+='<div class="mlabel" style="margin-top:8px">Ο browser έχει μπλοκάρει τις '
      +'ειδοποιήσεις. Άνοιξέ τις από τις ρυθμίσεις του site.</div>';
  }
  const place=n.place_name?esc(n.place_name)+(n.place_admin1?' — '+esc(n.place_admin1):'')
    :'<span style="color:var(--bad)">δεν έχει οριστεί</span>';
  h+='<div class="mrow" style="margin-top:8px"><span class="mlabel">Περιοχή ειδοποιήσεων</span></div>'
    +'<div class="mrow"><b>📍 '+place+'</b></div>'
    +'<button onclick="notifyAskSearch()">Αλλαγή περιοχής</button>';

  if(n.rules){
    h+='<div class="mrow" style="margin-top:10px"><span class="mlabel">Τύποι ειδοποιήσεων</span></div>';
    for(const k of ['rain','storm','wind','temp']){
      const on=n.rules[k]?'checked':'';
      h+='<label class="mtoggle"><input type="checkbox" '+on+' onchange="notifyToggle(\''+k+'\',this.checked)">'
        +'<span class="mlabel">'+NOTIFY_LABELS[k]+'</span></label>';
    }
  }
  h+='<div class="mrow" style="margin-top:8px">'
    +'<button onclick="notifyToggleActive('+(n.active?'false':'true')+')">'
    +(n.active?'Απενεργοποίηση':'Ενεργοποίηση')+'</button>'
    +'<button onclick="notifyTest()">Δοκιμαστική ειδοποίηση</button></div>'
    +'<div id="pm-notify-search"></div>';
  body.innerHTML=h;
}

function notifyMsg(text, ok){
  const m=document.getElementById('pm-notify-msg'); if(!m) return;
  m.className='msg '+(ok?'ok':'err'); m.textContent=text;
}

/* Ask for permission, get one fix, reverse-geocode it, and register it. The
   coordinates are read exactly once, on this click; there is no watcher. */
async function notifyEnable(){
  if(!notifySupported()){ notifyMsg('Ο browser δεν υποστηρίζει ειδοποιήσεις.'); return; }
  if(isIOS() && !isStandalone()){
    notifyMsg('Στο iPhone χρειάζεται πρώτα «Προσθήκη στην οθόνη αφετηρίας».'); return;
  }
  try{
    const perm=await Notification.requestPermission();
    if(perm!=='granted'){ notifyMsg('Ο browser δεν έδωσε άδεια. Μπορείς να ορίσεις περιοχή χειροκίνητα.',false); return; }
  }catch(e){ notifyMsg('Σφάλμα άδειας: '+e.message,false); return; }

  notifyMsg('Λήψη τοποθεσίας…');
  let loc=null;
  if(navigator.geolocation){
    loc=await new Promise(res=>{
      navigator.geolocation.getCurrentPosition(
        p=>res({lat:p.coords.latitude,lon:p.coords.longitude}),
        ()=>res(null),{enableHighAccuracy:false,timeout:10000,maximumAge:600000});
    });
  }
  if(!loc){
    notifyMsg('Δεν πήραμε τοποθεσία. Αναζήτησε την περιοχή χειροκίνητα.',false);
    notifyAskSearch();
    return;
  }
  // Reverse geocode for a human label. A failure here is fine: the cell is what
  // actually drives the alerts, the label is only for display.
  let name=null, admin1=null;
  try{
    const r=await (await fetch('/api/reverse?lat='+loc.lat+'&lon='+loc.lon)).json();
    name=r.name||null; admin1=r.admin1||r.state||null;
  }catch(e){}
  await notifyRegister(loc.lat,loc.lon,name,admin1);
}

async function notifyRegister(lat,lon,name,admin1){
  try{
    notifyMsg('Ενεργοποίηση…');
    const reg=await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;
    const cfg=await loadNotifyConfig();
    if(!cfg||!cfg.vapid_public_key){ notifyMsg('Οι ειδοποιήσεις δεν είναι ρυθμισμένες.',false); return; }
    let sub=await reg.pushManager.getSubscription();
    if(!sub){
      sub=await reg.pushManager.subscribe({
        userVisibleOnly:true,
        applicationServerKey:urlBase64ToUint8Array(cfg.vapid_public_key)});
    }
    const payload={subscription:sub.toJSON(),ios_standalone:isStandalone(),
                   location:{lat:lat,lon:lon,name:name,admin1:admin1}};
    const r=await fetch('/api/push/subscribe',{method:'POST',
      headers:{'Content-Type':'application/json',...(TOKEN?{'X-WX-Token':TOKEN}:{})},
      body:JSON.stringify(payload)});
    const d=await r.json();
    if(!r.ok){ notifyMsg(d.detail||'Η ενεργοποίηση απέτυχε.',false); return; }
    // A passcode/subscription token carries no device id. The server mints one on
    // the first notify write and returns a re-signed token carrying it; without
    // storing that here the very next write would mint a different id and the
    // freshly created subscription would look like it belonged to someone else.
    if(d.token){ TOKEN=d.token; localStorage.setItem('wx_token',TOKEN); }
    track('notify_enabled');
    notifyMsg('Οι ειδοποιήσεις ενεργοποιήθηκαν.',true);
    await renderNotify();
  }catch(e){ notifyMsg('Σφάλμα: '+e.message,false); }
}

function notifyAskSearch(){
  const box=document.getElementById('pm-notify-search'); if(!box) return;
  box.innerHTML='<div class="row" style="margin-top:8px">'
    +'<input id="pm-notify-q" placeholder="Πόλη ή χωριό" autocomplete="off"'
    +' onkeydown="if(event.key===\'Enter\')notifySearch()">'
    +'<button onclick="notifySearch()">Αναζήτηση</button></div>';
  const q=document.getElementById('pm-notify-q'); if(q) q.focus();
}

async function notifySearch(){
  const q=document.getElementById('pm-notify-q');
  if(!q) return;
  const s=q.value.trim(); if(!s) return;
  try{
    const r=await (await fetch('/api/resolve?q='+encodeURIComponent(s)+'&lat=39.0&lon=22.0')).json();
    if(!r.length){ notifyMsg('Δεν βρέθηκε τοποθεσία.',false); return; }
    const g=r.find(x=>x.countrycode==='GR')||r[0];
    await notifySetLocation(g.latitude,g.longitude,
      g.name+(g.admin1?' — '+g.admin1:''),g.admin1||null);
  }catch(e){ notifyMsg('Σφάλμα αναζήτησης: '+e.message,false); }
}

async function notifySetLocation(lat,lon,name,admin1){
  try{
    const r=await fetch('/api/notify/location',{method:'POST',
      headers:{'Content-Type':'application/json',...(TOKEN?{'X-WX-Token':TOKEN}:{})},
      body:JSON.stringify({lat:lat,lon:lon,name:name,admin1:admin1})});
    const d=await r.json();
    if(!r.ok){ notifyMsg(d.detail||'Η αλλαγή περιοχής απέτυχε.',false); return; }
    track('notify_location_set');
    notifyMsg('Η περιοχή ειδοποιήσεων ενημερώθηκε.',true);
    await renderNotify();
  }catch(e){ notifyMsg('Σφάλμα: '+e.message,false); }
}

async function notifyToggle(rule,on){
  const rules=Object.assign({},(NOTIFY&&NOTIFY.rules)||{});
  rules[rule]=on?1:0;
  try{
    const r=await fetch('/api/notify/prefs',{method:'POST',
      headers:{'Content-Type':'application/json','X-WX-Token':TOKEN},
      body:JSON.stringify({rules:rules})});
    const d=await r.json();
    if(!r.ok){ notifyMsg(d.detail||'Η αποθήκευση απέτυχε.',false); return; }
    NOTIFY.rules=d.rules; notifyMsg('Αποθηκεύτηκε.',true);
  }catch(e){ notifyMsg('Σφάλμα: '+e.message,false); }
}

async function notifyToggleActive(active){
  try{
    const r=await fetch('/api/notify/prefs',{method:'POST',
      headers:{'Content-Type':'application/json','X-WX-Token':TOKEN},
      body:JSON.stringify({active:active})});
    const d=await r.json();
    if(!r.ok){ notifyMsg(d.detail||'Η αποθήκευση απέτυχε.',false); return; }
    track(active?'notify_enabled':'notify_disabled');
    notifyMsg(active?'Ενεργοποιήθηκαν.':'Απενεργοποιήθηκαν.',true);
    await renderNotify();
  }catch(e){ notifyMsg('Σφάλμα: '+e.message,false); }
}

async function notifyTest(){
  notifyMsg('Αποστολή…');
  try{
    const r=await fetch('/api/notify/test',{method:'POST',
      headers:{'X-WX-Token':TOKEN}});
    const d=await r.json();
    if(!r.ok){ notifyMsg(d.detail||'Η αποστολή απέτυχε.',false); return; }
    track('notify_test_sent');
    notifyMsg('Στάλθηκε. Δες τις ειδοποιήσεις της συσκευής.',true);
  }catch(e){ notifyMsg('Σφάλμα: '+e.message,false); }
}

function urlBase64ToUint8Array(base64){
  const pad='='.repeat((4-base64.length%4)%4);
  const b64=(base64+pad).replace(/-/g,'+').replace(/_/g,'/');
  const raw=atob(b64); const out=new Uint8Array(raw.length);
  for(let i=0;i<raw.length;i++) out[i]=raw.charCodeAt(i);
  return out;
}

/* A browser can rotate a subscription on its own. The worker tells us; we simply
   re-register with the server using the identity we already hold. */
if('serviceWorker' in navigator){
  navigator.serviceWorker.addEventListener('message',ev=>{
    if(ev.data && ev.data.type==='pushsubscriptionchange' && TOKEN) notifyRenderSafe();
  });
}
function notifyRenderSafe(){ if(TIER.is_pro) renderNotify(); }

function renderManage(){
  const box=document.getElementById('pm-manage'); if(!box||!SUB) return;
  box.hidden=false;
  const renews=SUB.auto_renew;
  box.innerHTML=
     '<div class="mrow"><span>Κατάσταση</span><b>'+esc(SUB.status||'—')+'</b></div>'
    +'<div class="mrow"><span>Αυτόματη ανανέωση</span><b>'+(renews?'ενεργή':'ανενεργή')+'</b></div>'
    +'<div class="mrow"><span>'+(renews?'Επόμενη χρέωση':'Λήξη πρόσβασης')+'</span><b>'
      +fmtDate(SUB.cancel_at||SUB.current_period_end)+'</b></div>'
    +(renews
      ? '<label class="mtoggle"><input type="checkbox" id="ar-off" onchange="setAutoRenew(false)">'
        +'<span class="mlabel">Απενεργοποίηση αυτόματης ανανέωσης. Η πρόσβαση συνεχίζεται '
        +'μέχρι το τέλος της περιόδου που έχεις πληρώσει.</span></label>'
      : '<label class="mtoggle"><input type="checkbox" id="ar-on" checked onchange="setAutoRenew(true)">'
        +'<span class="mlabel">Επανενεργοποίηση αυτόματης ανανέωσης.</span></label>');
}
async function setAutoRenew(enabled){
  const box=document.getElementById('pm-manage');
  const m=document.getElementById('pm-msg');
  m.className='msg'; m.textContent='Αποθήκευση…';
  try{
    const r=await fetch('/api/subscription/auto-renew',{method:'POST',
      headers:{'Content-Type':'application/json','X-WX-Token':TOKEN},
      body:JSON.stringify({enabled:enabled})});
    const d=await r.json();
    if(!r.ok) throw new Error(d.detail||'Σφάλμα');
    SUB=d; renderManage();
    if(!enabled) track('subscription_cancelled');
    m.className='msg ok';
    m.textContent = enabled ? 'Η αυτόματη ανανέωση ενεργοποιήθηκε.'
      : 'Η αυτόματη ανανέωση απενεργοποιήθηκε. Δεν θα χρεωθείς ξανά.';
  }catch(e){
    m.className='msg err'; m.textContent='Σφάλμα: '+e.message;
    renderManage();   // put the checkbox back to the state the server still holds
  }
}

function signOut(){
  // The token is the only thing that grants PRO on this device, and a promo code
  // cannot be redeemed twice, so this is not a reversible "log out". Say so
  // plainly rather than letting a single click drop paid-for access.
  const msg='Η αφαίρεση βγάζει το PRO από αυτή τη συσκευή: θα ξαναδείς μόνο τη '
    +'δωρεάν πρόγνωση και θα χρειαστεί νέος κωδικός ή νέα ενεργοποίηση για να το '
    +'πάρεις πίσω. Η συνδρομή σου στη Stripe ΔΕΝ ακυρώνεται και δεν σταματά η '
    +'χρέωση. Θέλεις να συνεχίσεις;';
  if(!window.confirm(msg)) return;
  TOKEN=null; localStorage.removeItem('wx_token');
  const pl=document.getElementById('pm-promoline'); if(pl) pl.hidden=true;
  const pn=document.getElementById('pm-notify'); if(pn) pn.hidden=true;
  TIER={tier:'free',is_pro:false,source:'free',
        free_hours:PLANS?PLANS.free_hours:72,
        pro_hours:PLANS?PLANS.pro_hours:240,
        locked_hours:PLANS?PLANS.pro_hours:240};
  renderCta();
  if(CUR) reload();
}
async function reload(){ if(CUR) load(CUR.lat,CUR.lon,CUR.label,{elevation_m:CUR.elev}); }

/* ---------- tier bar ---------- */
function renderTierBar(t, containerId){
  const el=document.getElementById(containerId); if(!el) return;
  const pro=t.is_pro;
  const src=pro?({passcode:'κωδικός πρόσβασης',subscription:'συνδρομή Stripe',trial:'δωρεάν δοκιμή',free:''}[t.source]||t.source):'';
  el.innerHTML='<span class="pill '+(pro?'pro':'free')+'">'+(pro?'PRO':'FREE')+'</span>'
    +'<span>'+(pro
        ? 'Ξεκλειδωμένες '+PLANS_HOURS(t.pro_hours)+'. Πηγή πρόσβασης: '+src+'.'
        : 'Δωρεάν πρόγνωση '+PLANS_HOURS(t.free_hours)+'. Κλειδωμένες οι επόμενες '
          +PLANS_HOURS(t.locked_hours)+'.')
    +'</span>';
  if(!pro) el.innerHTML+='<button onclick="openModal()" style="margin-left:auto">Αναβάθμιση σε PRO</button>';
  // A subscription token cannot be re-issued from the browser: the only way back
  // is a fresh Checkout Session, so there is no sign-out button for it. Removing
  // one's own PRO by accident must not be a single click away.
  else if(t.source!=='subscription') el.innerHTML+='<button onclick="signOut()" style="margin-left:auto">Αφαίρεση PRO από τη συσκευή</button>';
}
function PLANS_HOURS(hours){ return hours>=24 ? (hours/24)+' ημέρες ('+hours+' ώρες)' : hours+' ώρες'; }

/* ---------- locked blocks ---------- */
/* A distinct placeholder skeleton per locked section, so four locked blocks do not
   all render the identical chart. `kind` picks what the blurred layer looks like. */
function lockedBlock(kind,title,desc){
  let inner='';
  const ph=n=>{let s='';for(let i=0;i<n;i++) s+='<td class="ph"></td>';return s;};

  if(kind==='indices'){
    let rows='';const names=['SBCAPE','MLCAPE','MUCAPE','SBCIN','Shear 0–1 km','Shear 0–6 km','SRH 0–3 km','LCL'];
    for(const nm of names) rows+='<tr><td>'+nm+'</td>'+ph(2)+'</tr>';
    inner='<table><thead><tr><th>Δείκτης</th><th>Τιμή</th><th>Μονάδα</th></tr></thead>'
      +'<tbody>'+rows+'</tbody></table>';
  }else if(kind==='skewt'){
    inner='<div class="phchart"></div>';
  }else if(kind==='models'){
    let rows='';for(const m of ['GFS (NOAA) 0.25°','ICON-EU (DWD) 0.0625°','ECMWF IFS 0.25°','Απόκλιση']){
      rows+='<tr><td>'+m+'</td>'+ph(3)+'</tr>';}
    inner='<table><thead><tr><th>Μοντέλο</th><th>Τώρα</th><th>+24h</th><th>+48h</th></tr></thead>'
      +'<tbody>'+rows+'</tbody></table>';
  }else{
    let rows='';for(const p of ['850 hPa','700 hPa','500 hPa','300 hPa','200 hPa']){
      rows+='<tr><td>'+p+'</td>'+ph(5)+'</tr>';}
    inner='<table><thead><tr><th>Επίπεδο</th><th>Ύψος</th><th>Θερμ.</th><th>Δρόσος</th><th>Υγρ.</th><th>Άνεμος</th></tr></thead>'
      +'<tbody>'+rows+'</tbody></table>';
  }

  return '<div class="locked"><div class="blurred">'+inner+'</div>'
    +'<div class="lockover"><div class="lk">🔒</div><h4>'+title+'</h4><p>'+desc+'</p>'
    +'<button class="primary" onclick="openModal()">Αναβάθμιση σε PRO</button></div></div>';
}

function showTab(t){
  for(const n of ['simple','expert','why']){
    document.getElementById('panel-'+n).classList.toggle('active', n===t);
    document.getElementById('tab-'+n).classList.toggle('active', n===t);
  }
  if(t==='expert') track('expert_opened');
  if(t==='why'){ track('verification_viewed'); renderCta(); maybeVerify(); }
  if(CHART) CHART.resize();
}
/* ---------------------------------------------------------------- favourites
   Kept in localStorage, so there is no favourites table, no account, and no
   request: adding or removing a place is instant and works offline. The stored
   shape is deliberately just {label,lat,lon} - enough to re-run load() later. */
const FAV_KEY='wx_favs';
function favsLoad(){
  try{
    const raw=localStorage.getItem(FAV_KEY);
    const arr=raw?JSON.parse(raw):[];
    return Array.isArray(arr)?arr.filter(f=>f&&isFinite(f.lat)&&isFinite(f.lon)):[];
  }catch(e){ return []; }   // corrupt entry must not break the whole page
}
function favsSave(list){
  try{ localStorage.setItem(FAV_KEY,JSON.stringify(list.slice(0,40))); }
  catch(e){}                // private mode / quota: favourites are best-effort
}
function favKeyOf(lat,lon){ return lat.toFixed(3)+','+lon.toFixed(3); }
function favIsSaved(lat,lon){
  const k=favKeyOf(lat,lon);
  return favsLoad().some(f=>favKeyOf(f.lat,f.lon)===k);
}
function favAddCurrent(){
  if(!CUR) return;
  const list=favsLoad();
  if(list.some(f=>favKeyOf(f.lat,f.lon)===favKeyOf(CUR.lat,CUR.lon))){ renderFavourites(); return; }
  list.push({label:CUR.label||'Τοποθεσία',lat:CUR.lat,lon:CUR.lon});
  favsSave(list); renderFavourites();
}
function favRemove(lat,lon){
  const k=favKeyOf(lat,lon);
  favsSave(favsLoad().filter(f=>favKeyOf(f.lat,f.lon)!==k));
  renderFavourites();
}
function favPick(i){
  const f=favsLoad()[i];
  if(f) load(f.lat,f.lon,f.label);
}
/* The list is the only place favourites are shown, so it re-renders whenever
   one changes. Text is escaped: a place name comes from a remote geocoder and
   is not trusted to be free of markup. */
function renderFavourites(){
  const list=favsLoad();
  const box=document.getElementById('favlist');
  const empty=document.getElementById('favempty');
  if(box){
    box.innerHTML=list.map((f,i)=>
      '<span class="favchip"><button class="pick" onclick="favPick('+i+')" title="'
      +esc(f.label)+'">'+esc(f.label)+'</button>'
      +'<button class="del" onclick="favRemove('+f.lat+','+f.lon+')" '
      +'title="Διαγραφή από τα αγαπημένα" aria-label="Διαγραφή">✕</button></span>').join('');
  }
  if(empty) empty.style.display=list.length?'none':'inline';
  const btn=document.getElementById('favaddbtn');
  if(btn){
    const has=CUR&&CUR.lat!=null;
    btn.style.display=has?'inline-block':'none';
    if(has){
      const saved=favIsSaved(CUR.lat,CUR.lon);
      btn.disabled=saved;
      btn.textContent=saved?'★ Στα Αγαπημένα':'☆ Προσθήκη στα Αγαπημένα';
    }
  }
}

/* Elevation for a point, resolved server-side. The map used to do this on drag;
   without a map every location path (search, geolocation, favourite) runs it
   once here instead, so the temperature correction still gets a real elevation.
   The raw payload is kept (not just the number) because the elevation panel
   renders point_elevation_m / model_elevation_m / dataset from it. */
async function resolvePoint(lat,lon){
  try{
    return await (await fetch(`/api/elevation?lat=${lat}&lon=${lon}`)).json();
  }catch(e){ return {}; }
}
function persistElevation(){
  const inp=document.getElementById('inp-elev');
  if(!inp||!CUR) return;
  const raw=inp.value.trim();
  let manual=null;
  if(raw===''){
    document.getElementById('elev-src').textContent='κενό — δεν εφαρμόζεται διόρθωση';
  }else{
    const v=Number(raw);
    if(!isFinite(v)||v<-50||v>3000){
      document.getElementById('elev-src').textContent='μη έγκυρη τιμή (δέξου -50…3000 m)';
      return;
    }
    manual=v;
    document.getElementById('elev-src').textContent='χειροκίνητη τιμή — θα χρησιμοποιηθεί στη διόρθωση';
  }
  // CUR.dem is the DEM payload already fetched for this point; reusing it avoids
  // a second /api/elevation call just because the elevation was edited.
  load(CUR.lat,CUR.lon,CUR.label,CUR.dem,manual);
}

async function geo(){
  if(!navigator.geolocation){alert('Ο browser δεν υποστηρίζει geolocation');return}
  navigator.geolocation.getCurrentPosition(
    async p=>{
      let label='Η θέση μου';
      try{
        const r=await (await fetch(`/api/reverse?lat=${p.coords.latitude}&lon=${p.coords.longitude}`)).json();
        if(r.name) label=r.name;
      }catch(e){}
      load(p.coords.latitude,p.coords.longitude,label);
    },
    e=>alert('Αδυναμία λήψης θέσης: '+e.message),
    {enableHighAccuracy:true,timeout:10000,maximumAge:60000});
}
async function search(){
  const q=document.getElementById('q').value.trim(); if(!q)return;
  // bias to Greece so "Ηλιούπολη" does not resolve to a bus stop in Cyprus
  const r=await (await fetch('/api/resolve?q='+encodeURIComponent(q)+'&lat=39.0&lon=22.0')).json();
  if(!r.length){alert('Δεν βρέθηκε τοποθεσία');return}
  const greek=r.find(x=>x.countrycode==='GR')||r[0];
  track('location_searched');
  load(greek.latitude,greek.longitude,greek.name+(greek.admin1?' — '+greek.admin1:''));
}
/* Manual coordinates, the map's replacement for named-less points. Validated
   here so a typo shows a message next to the fields rather than firing a request
   for a point in the Gulf of Guinea. */
async function applyManualCoords(){
  const out=document.getElementById('coord-src');
  const la=Number(document.getElementById('inp-lat').value);
  const lo=Number(document.getElementById('inp-lon').value);
  if(!document.getElementById('inp-lat').value.trim()||
     !document.getElementById('inp-lon').value.trim()||!isFinite(la)||!isFinite(lo)){
    out.textContent='Συμπλήρωσε και τις δύο τιμές.'; return;
  }
  if(la<-90||la>90||lo<-180||lo>180){
    out.textContent='Το πλάτος είναι −90…90 και το μήκος −180…180.'; return;
  }
  out.textContent='Αναζήτηση ονόματος…';
  let label=la.toFixed(4)+', '+lo.toFixed(4);
  try{
    const r=await (await fetch(`/api/reverse?lat=${la}&lon=${lo}`)).json();
    if(r.name) label=r.name+(r.city&&r.city!==r.name?' — '+r.city:'')
      +(r.state?' ('+r.state+')':'');
  }catch(e){}
  out.textContent='Φορτώθηκε: '+label;
  track('map_location_selected');
  load(la,lo,label);
}
async function load(lat,lon,label,demPayload,manualElev){
  // One entry point for every way of choosing a point (search, geolocation,
  // favourite, manual elevation). Elevation is resolved here when the caller
  // does not already have it, so the temperature correction is not dependent on
  // a map being dragged.
  // `demPayload` is the raw /api/elevation response; the whole payload is kept
  // so the elevation panel can still show the DEM details after a manual edit.
  // `manualElev` is an operator-typed elevation, which wins over the DEM, and is
  // passed explicitly so it never leaks from the previously viewed location.
  const dem=demPayload||await resolvePoint(lat,lon);
  const demElev=dem.point_elevation_m!=null?dem.point_elevation_m:null;
  const elevM=manualElev!=null?manualElev:demElev;
  CUR={lat:lat,lon:lon,label:label,elev:elevM,dem:dem};
  // Every path that picks a point ends up here, so this is where the accuracy
  // tab learns which point to verify. Done before the forecast fetch so
  // switching to that tab immediately is already correct.
  VERI_POINT={lat:lat,lon:lon,label:label};
  VERI_DONE=null;
  if(document.getElementById('panel-why').classList.contains('active')) maybeVerify();
  document.getElementById('where').textContent='Σημείο: '+label+'  ['+lat.toFixed(3)+', '+lon.toFixed(3)+']';
  document.getElementById('simple').innerHTML='<p class="spin">Φόρτωση δεδομένων…</p>';
  document.getElementById('expert').innerHTML='<p class="spin">Φόρτωση δεδομένων…</p>';
  const qs=new URLSearchParams({lat:lat,lon:lon});
  if(CUR.elev!=null) qs.set('elevation_m',CUR.elev);
  const hdrs={};
  if(TOKEN) hdrs['X-WX-Token']=TOKEN;
  let d;
  try{ d=await (await fetch('/api/brief?'+qs,{headers:hdrs})).json(); }
  catch(e){ document.getElementById('simple').innerHTML='<div class="card err">Σφάλμα: '+e.message+'</div>'; return; }
  if(d.error){ document.getElementById('simple').innerHTML='<div class="card err">'+d.error+'</div>'; return; }
  // The panel needs two things: the DEM details for the point (from /api/elevation)
  // and the correction that was actually applied (from meta.elevation). Merging
  // them here keeps the panel in one place instead of reading both.
  d._elev=Object.assign({},dem||{},(d.meta&&d.meta.elevation)||{});
  if(manualElev!=null) d._elev.manual_elevation=manualElev;
  if(d.tier){ TIER=d.tier; PLANS=d.tier.plans; }
  if(!PLANS){ try{ PLANS=await (await fetch('/api/plans')).json(); }catch(e){} }
  track('forecast_loaded',{lat:lat,lon:lon,value:(d.tier&&d.tier.hours)||undefined});
  if(d.tier){
    if(d.tier.hours>=72) track('forecast_72h_viewed');
    if(d.tier.hours>=240) track('forecast_240h_viewed');
  }
  renderSimple(d); renderExpert(d); renderAttribution(d);
  renderFavourites();
  // Astro runs after the forecast is on screen, so a slow sky calculation never
  // delays the weather itself. It is a separate request that can fail alone.
  loadSkyNow(lat,lon,CUR.elev);
}
const n1=v=>(v==null||isNaN(v))?'—':(+v).toFixed(1);
const n0=v=>(v==null||isNaN(v))?'—':Math.round(v);

function renderSimple(d){
  const s=d.simple, st=s.stats, now=s.now;
  const bftBadge = v=>v==null?'—':v+' Bft';
  const el=d._elev||{};
  const modelEl = d.meta.model_elevation_m;

  // --- manual elevation control -------------------------------------------
  let elevPanel = '<details class="geo"><summary>⛰️ Υψόμετρο &amp; Τοπική Θερμοκρασία</summary>'
    +'<div class="body">'
    +'<p>Η θερμοκρασία που βλέπεις προσαρμόζεται στο ακριβές υψόμετρο της τοποθεσίας '
    +'σου. Σε αντίθεση με τα κοινά sites που δίνουν μια γενική μέση τιμή για όλη την '
    +'περιοχή, το Greece Sky and Weather υπολογίζει τη θερμοκρασία, την αίσθηση και τη '
    +'βάση των νεφών ειδικά για το δικό σου υψόμετρο.</p>'
    +'<label>Υψόμετρο σημείου (m) — επεξεργάσιμο</label>'
    +'<input id="inp-elev" type="number" step="1" min="-50" max="3000" '
      +'value="'+((el.manual_elevation!=null)?el.manual_elevation
                 :(el.point_elevation_m!=null?el.point_elevation_m:''))+'" '
      +'oninput="this.dataset.touched=\'1\'" onchange="persistElevation()">'
    +'<div id="elev-src" style="font-size:12px;color:#67707d;margin-top:4px">'
      +(el.manual_elevation!=null
          ? 'χειροκίνητη τιμή — υπερισχύει του DEM'
          : (el.point_elevation_m!=null
             ? 'DEM: '+(el.dataset_note||el.dataset||'')+' — μπορείς να το αλλάξεις'
             : 'δεν βρέθηκε υψόμετρο για το σημείο — γράψε τιμή για διόρθωση'))
    +'</div>';
  // --- manual coordinates -------------------------------------------------
  // Without the map this is the only way to reach a point that has no searchable
  // name (a peak, a field, a bay). It replaces exactly the capability the map
  // drag provided, so removing the map does not remove a way to pick a spot.
  elevPanel+='<details class="geo" style="margin-top:10px"><summary>📍 Συντεταγμένες (χειροκίνητα)</summary>'
    +'<div class="body"><p>Για σημεία χωρίς όνομα στην αναζήτηση — κορυφές, αγροτεμάχια, '
    +'όρμοι. Δέχεται δεκαδικές μοίρες (π.χ. 37.9838, 23.7275).</p>'
    +'<label>Πλάτος (lat)</label>'
    +'<input id="inp-lat" type="number" step="0.0001" min="-90" max="90" '
      +'value="'+(CUR&&CUR.lat!=null?CUR.lat.toFixed(4):'')+'">'
    +'<label>Μήκος (lon)</label>'
    +'<input id="inp-lon" type="number" step="0.0001" min="-180" max="180" '
      +'value="'+(CUR&&CUR.lon!=null?CUR.lon.toFixed(4):'')+'">'
    +'<div id="coord-src" style="font-size:12px;color:#67707d;margin-top:4px">'
    +'Η τοποθεσία θα ονομαστεί από την υπηρεσία γεωκωδικοποίησης.</div>'
    +'<button onclick="applyManualCoords()">Μετάβαση στις συντεταγμένες</button></div></details>';

  if(el.model_elevation_m!=null)
    elevPanel+='<p class="note">Μέσο υψόμετρο κελιού GFS: <b>'+el.model_elevation_m+' m</b>.'
      +(d.note_elevation?' '+d.note_elevation:'')
      +'</p>';
  if(el.model_agl_m!=null){
    elevPanel+='<p class="note">Διαφορά σημείου − κελιού: <b>'+el.model_agl_m+' m</b>. '
      +(Math.abs(el.model_agl_m)>200
        ? 'Η διαφορά είναι μεγάλη, οπότε η διόρθωση έχει σημασία για αυτό το σημείο.'
        : 'Η διαφορά είναι μικρή, οπότε η επίδραση είναι περιορισμένη.')
      +'</p>';
  }
  if(el.applied_c!=null){
    const src=el.lapse_rate_source==='derived'?'από το προφίλ του μοντέλου':'τυπική βαθμίδα';
    elevPanel+='<p class="note">Βαθμίδα που χρησιμοποιήθηκε: <b>'
      +n1(el.lapse_rate_c_per_km)+'°C/km</b> ('+src+').</p>';
  }
  elevPanel+='<button onclick="persistElevation()">Εφαρμογή</button></div></details>';

  let h='<div class="tierbar" id="tierbar-simple"></div>';
  h+=nowHero(s.hero);
  h+='<div id="skynow"></div>';
  h+='<div class="cards">'
    +'<div class="card"><div class="k">Σήμερα</div><div class="v">'+n0(st.tmin)+'–'+n0(st.tmax)+'°</div>'
    +'<div class="s">ελάχιστη – μέγιστη</div></div>'
    +'<div class="card"><div class="k">Βροχή 24ω</div><div class="v">'+n1(st.precip24_mm)+' mm</div>'
    +'<div class="s">'+(st.precip24_mm<0.2?'στεγνά':'βροχερό')+'</div></div>'
    +'<div class="card"><div class="k">Άνεμος</div><div class="v">'+n0(st.wind_max_kmh)+' km/h</div>'
    +'<div class="s">'+bftBadge(st.bft_max)+' · ριπές έως '+n0(st.gust_max_kmh)+' km/h</div></div>'
    +'<div class="card"><div class="k">Βάση νεφών</div>'
    +   '<div class="v">'+(now.cloud_base
        ? n0(now.cloud_base.base_agl_m)+' m'
        : '—')+'</div>'
    +'<div class="s">'+(now.cloud_base
        ? 'πάνω από το έδαφος · διαφορά δρόσου '+n1(now.cloud_base.spread_c)+'°'
        : 'μη διαθέσιμη')+'</div></div>'
    +'</div>';

  if(el.applied_m!=null && el.applied_c!=null){
    h+='<p class="note">Διόρθωση υψομέτρου: το σημείο είναι '
      +(el.model_agl_m>0?'ψηλότερα κατά '+el.model_agl_m+' m':'χαμηλότερα κατά '+Math.abs(el.model_agl_m)+' m')
      +' από το κελί του μοντέλου, οπότε η θερμοκρασία προσαρμόστηκε κατά <b>'
      +el.applied_c+'°C</b>.</p>';
  }

  h+='<div class="verdict"><h3>Τι να περιμένεις</h3><ul>';
  /* The first summary line is the rain outlook, which the hero headline already
     states in one sentence. Repeating it here would make the page look padded,
     so the list starts from the detail the hero does not carry. */
  const lines = s.summary.slice(s.hero ? 1 : 0);
  for(const line of lines) h+='<li>'+line+'</li>';
  h+='</ul>';
  const a=d.expert&&d.expert.agreement;
  if(a){
    h+='<div class="bar '+(a.available?a.class:'unknown')+'"><i></i></div>';
    h+='<p class="note"><b>Συμφωνία μοντέλων: '+a.text+'</b> — '+(a.detail||'μη επαρκή δεδομένα συμφωνίας')+
       ' Δεν είναι βεβαιότητα πρόγνωσης, μόνο ένδειξη του πόσο συμφωνούν τα μοντέλα μεταξύ τους.</p>';
  }
  if(d.meta.bias&&d.meta.bias.applied){
    h+='<p class="note">Εφαρμόστηκε διόρθωση τοπικού σταθμού: '
      +d.meta.bias.offset_c+'°C στις πρώτες 6 ώρες.</p>';
  }
  h+='</div>';

  h+='<h3>Μετεόγραμμα '+(d.tier&&d.tier.is_pro?'10 ημερών':'72 ωρών')+'</h3><div class="chartbox"><canvas id="mg" height="150"></canvas>'
    +'<p class="note">Αριστερός άξονας: θερμοκρασία και αίσθηση. Δεξιός άξονας: βροχή (mm/h).</p></div>'
    +elevPanel;

  h+='<h3>Ωριαία ανάλυση</h3><table><thead><tr><th>Ώρα</th><th>Θερμ.</th><th>Αίσθηση</th>'
    +'<th>Βροχή</th><th>Άνεμος</th><th>Bft</th><th>Ριπές</th></tr></thead><tbody>';
  for(const r of s.hours){
    h+='<tr><td>+'+r.step_h+'h</td><td>'+n1(r.t)+'°</td><td>'+n1(r.feels)+'°</td>'
      +'<td>'+n1(r.precip)+' mm</td><td>'+n0(r.wind)+' km/h</td><td>'+n0(r.bft)+'</td>'
      +'<td>'+n0(r.gust)+' km/h</td></tr>';
  }
  h+='</tbody></table>';
  h+=dailyCarousel(s.daily);
  h+=dailyTable(s.daily);
  h+=proUpsell(d);
  document.getElementById('simple').innerHTML=h;
  wrapTables(document.getElementById('simple'));
  renderTierBar(d.tier||TIER,'tierbar-simple');
  drawMeteogram(s.hours, (d.tier||TIER).is_pro, d.tier);
}

/* Daily rows for the days the tier actually allows. */
/* Wide numeric tables cannot fit a phone. Wrapping each in a scroll container
   keeps the columns aligned (a display:block table would not) and keeps the
   page itself from scrolling sideways. */
function wrapTables(root){
  if(!root) return;
  for(const t of root.querySelectorAll('table')){
    if(t.parentElement && t.parentElement.classList.contains('tscroll')) continue;
    const w=document.createElement('div');
    w.className='tscroll';
    t.parentNode.insertBefore(w,t);
    w.appendChild(t);
  }
}

/* --- 10-day daily carousel -------------------------------------------------
   Replaces the daily table as the primary daily view. The table still exists
   below it, compacted, because the carousel is for scanning and the table is for
   comparing exact numbers - and because a carousel is unusable with a screen
   reader, while a table is fine. Both read the same payload, so they cannot
   disagree. */

/* The day card for a day the tier actually covers. The icon, condition and every
   number come from the server, so the card cannot disagree with the hero or the
   table about the same day. */
function dayCard(r){
  const wet=r.rain_mm>=0.2;
  let h='<article class="dcard">'
    +'<div class="dhead"><span class="dname">'
      +(r.is_today?'<span class="dtoday">Σήμερα</span>':(r.weekday||('Ημέρα '+r.day)))
      +'</span><span class="ddate">'+(r.date_label||('+'+r.from_h+'h'))+'</span></div>'
    +'<div class="dicon" aria-hidden="true">'+(r.icon||'🌡️')+'</div>'
    +'<div class="dtemps"><span class="dtmax">'+n0(r.tmax)+'°</span>'
      +'<span class="dtmin">'+n0(r.tmin)+'°</span></div>'
    +'<div class="drow">🌡️ αίσθηση <b>'+n0(r.feels_min)+'°</b></div>'
    +'<div class="drow">💧 <b>'+n1(r.rain_mm)+' mm</b>'
      +(wet?' <span style="color:#4da3ff">βροχερή</span>':' <span>στεγνή</span>')+'</div>'
    +'<div class="drow">💨 <b>'+n0(r.wind_max)+' km/h</b>'
      +(r.bft_max!=null?' · <b>'+r.bft_max+'</b> Bft':'')+'</div>'
    +'</article>';
  return h;
}

/* A locked day. Carries no numbers at all - the free tier never receives them.
   The bars are a wireframe and the values are em dashes on purpose: a blurred
   strip that looks like a real forecast invites a screenshot and a wrong decision. */
function lockedDayCard(day, label){
  const labels=['Κυριακή','Δευτέρα','Τρίτη','Τετάρτη','Πέμπτη','Παρασκευή','Σάββατο'];
  let bars='<div class="dphrow">';
  for(let i=0;i<7;i++) bars+='<i style="height:'+(35+((i*37)%55))+'%"></i>';
  bars+='</div>';
  return '<article class="dcard locked">'
    +'<div class="blurred">'
      +'<div class="dhead"><span class="dname">'+(label||('Ημέρα '+day))+'</span>'
        +'<span class="ddate">—</span></div>'
      +'<div class="dph" style="width:34px;height:24px"></div>'
      +bars
      +'<div class="dph" style="width:72%"></div>'
      +'<div class="dph" style="width:58%"></div>'
    +'</div>'
    +'<div class="lockover"><div class="lk">🔒</div>'
      +'<span class="probadge">PRO</span></div>'
    +'</article>';
}

/* The strip. Real cards for the days the tier includes, then placeholders for the
   rest. `t.free_days` comes from the server, so the number of real cards matches
   what the server actually sent. */
function dailyCarousel(daily){
  const isPro=(TIER&&TIER.is_pro)||false;
  if(!daily||!daily.length) return '';
  let h='<h3>'+(isPro?'Πρόγνωση 10 ημερών'
      :'Πρόγνωση 10 ημερών — δωρεάν οι πρώτες '+daily.length)+'</h3>'
    +'<div class="dstrip" id="dstrip" role="list" aria-label="Ημερήσια πρόγνωση">';
  for(const r of daily) h+=dayCard(r);
  if(!isPro){
    for(let day=daily.length+1; day<=10; day++) h+=lockedDayCard(day);
  }
  h+='</div>';

  // A hint only when there is something to scroll to and something locked.
  if(!isPro && daily.length<10){
    h+='<p class="note">Οι πρώτες '+(daily.length)+' ημέρες είναι δωρεάν. '
      +'Σύρε οριζόντια για να δεις τις υπόλοιπες — οι ημέρες '
      +(daily.length+1)+'–10 είναι στο PRO.</p>';
  }else{
    h+='<p class="note">Σύρε οριζόντια για τις επόμενες ημέρες.</p>';
  }
  return h;
}

function dailyTable(daily){
  if(!daily||!daily.length) return '';
  let h='<details class="geo"><summary>Αναλυτικός πίνακας ανά ημέρα</summary>'
    +'<div class="tscroll"><table><thead><tr><th>Ημέρα</th><th>Κάλυψη</th>'
    +'<th>Ελάχ.</th><th>Μέγ.</th><th>Αίσθηση</th><th>Βροχή</th><th>Άνεμος</th></tr></thead><tbody>';
  for(const r of daily){
    // show real coverage, because after +120h the model is 3-hourly, not hourly
    const cov=r.coverage_h&&r.coverage_h<24?(r.coverage_h+'h από '+r.samples+' δείγματα'):'24h';
    const nm=r.weekday?r.weekday+' '+(r.date_label||''):('Ημέρα '+r.day);
    h+='<tr><td>'+nm.trim()+'</td><td>+'+r.from_h+'…+'+r.to_h+'h <span style="color:#67707d">('+cov+')</span></td>'
      +'<td>'+n1(r.tmin)+'°</td><td>'+n1(r.tmax)+'°</td><td>'+n1(r.feels_min)+'°</td>'
      +'<td>'+n1(r.rain_mm)+' mm</td><td>'+n0(r.wind_max)+' km/h'+(r.bft_max!=null?' ('+r.bft_max+' Bft)':'')+'</td></tr>';
  }
  h+='</tbody></table></div>';
  h+='<p class="note">Μετά το +120h το GFS δίνει δεδομένα κάθε 3 ώρες, όχι κάθε ώρα, '
    +'γι\' αυτό οι τελευταίες ημέρες έχουν λιγότερα δείγματα.</p></details>';
  return h;
}

/* Upsell under the carousel. The blurred cards show *that* days are locked; this
   says what unlocking them includes and gives one clear way to do it. It replaces
   the old days-3-10 skeleton table, whose placeholders duplicated the carousel. */
function proUpsell(d){
  const t=(d&&d.tier)||TIER||{};
  if(t.is_pro) return '';
  const free=(t.free_hours||FREE_HOURS)/24;
  const locked=t.locked_hours!=null?t.locked_hours/24:(PRO_HOURS-FREE_HOURS)/24;
  return '<div class="locked upsell"><div class="lockover">'
    +'<div class="lk">🔒</div>'
    +'<h4>Οι επόμενες '+locked+' ημέρες είναι διαθέσιμες στο PRO</h4>'
    +'<p>Η δωρεάν πρόγνωση καλύπτει '+free+' ημέρες. Με το PRO ξεκλειδώνεις '
    +(free+locked)+' ημέρες συνολικά ('+((t.pro_hours||PRO_HOURS))+' ώρες), μαζί με '
    +'Skew-T, δείκτες αστάθειας και σύγκριση 3 μοντέλων.</p>'
    +'<button class="primary" onclick="openModal()">Αναβάθμιση σε PRO — €'
    +(PLANS?PLANS.pricing.yearly.price.toFixed(2):'19.99')+'/έτος</button>'
    +'</div></div>';
}

function drawMeteogram(hours, isPro, tier){
  const el=document.getElementById('mg'); if(!el||typeof Chart==='undefined')return;
  if(CHART){CHART.destroy();CHART=null;}
  const labels=hours.map(r=>'+'+r.step_h+'h');

  // Shade the hours the free tier does not include, so the chart does not imply
  // coverage the user does not have.
  const freeH=(tier&&tier.free_hours)||FREE_HOURS;
  const shade={
    id:'shade',
    beforeDatasetsDraw(chart){
      if(isPro) return;
      const {ctx,chartArea:ca,scales:{x}}=chart;
      const at=x.getPixelForValue(freeH-0.5);
      if(!isFinite(at)) return;
      ctx.save();
      ctx.fillStyle='rgba(154,167,189,.20)';
      ctx.fillRect(at,ca.top,ca.right-at,ca.bottom-ca.top);
      ctx.fillStyle='#9aa7bd';
      ctx.font='500 11px system-ui';
      ctx.fillText('🔒 PRO',at+8,ca.top+14);
      ctx.restore();
    }
  };

  CHART=new Chart(el,{
    type:'line',
    data:{labels,datasets:[
      {label:'Θερμοκρασία °C',data:hours.map(r=>r.t),borderColor:'#f87171',
       backgroundColor:'rgba(248,113,113,.14)',yAxisID:'y',tension:.3,borderWidth:2.4,pointRadius:0,fill:true},
      {label:'Αίσθηση °C',data:hours.map(r=>r.feels),borderColor:'#c084fc',
       yAxisID:'y',tension:.3,borderWidth:1.8,borderDash:[5,4],pointRadius:0},
      {type:'bar',label:'Βροχή mm/h',data:hours.map(r=>r.precip),backgroundColor:'#4da3ff',
       yAxisID:'y1',borderRadius:3,barPercentage:.7}
    ]},
    plugins:[shade],
    options:{responsive:true,maintainAspectRatio:true,interaction:{mode:'index',intersect:false},
      plugins:{legend:{labels:{boxWidth:12,font:{size:11},color:'#eef2f8'}}},
      scales:{
        y:{position:'left',title:{display:true,text:'°C',font:{size:11},color:'#9aa7bd'},
           grid:{color:'rgba(255,255,255,.08)'},ticks:{color:'#9aa7bd'}},
        y1:{position:'right',title:{display:true,text:'mm/h',font:{size:11},color:'#9aa7bd'},
            grid:{drawOnChartArea:false},beginAtZero:true,
            ticks:{color:'#9aa7bd',callback:v=>v.toFixed(1)}},
        x:{grid:{display:false},ticks:{maxTicksLimit:14,font:{size:10},color:'#9aa7bd'}}
      }}
  });
}

function capeTone(v){
  if(v==null) return 'dim';
  if(v>=2500) return 'bad';
  if(v>=1000) return 'warn';
  if(v>=300) return 'good';
  return 'dim';
}
function shearTone(v){
  if(v==null) return 'dim';
  if(v>=40) return 'bad';
  if(v>=30) return 'warn';
  if(v>=15) return 'good';
  return 'dim';
}
function srhTone(v){
  if(v==null) return 'dim';
  if(v>=250) return 'bad';
  if(v>=100) return 'warn';
  return 'dim';
}
function cinTone(v){
  if(v==null) return 'dim';
  if(v<=-200) return 'good';
  if(v<=-50) return 'warn';
  return 'dim';
}
function toneWord(t){
  if(t==='bad') return 'ακραίο';
  if(t==='warn') return 'αυξημένο';
  if(t==='good') return 'μέτριο';
  return 'ήρεμο';
}

/* One dense cell of the pro grid. The number carries the weight and the unit is
   deliberately light, so a forecaster scanning the grid reads values, not units. */
function xcell(k,v,u,tone,note){
  return '<div class="xcell"><div class="xk">'+k+'</div>'
    +'<div class="xv">'+(v==null?'—':v)+(u?'<span class="xu">'+u+'</span>':'')+'</div>'
    +(note?'<div class="xt '+(tone||'dim')+'">'+note+'</div>':'')+'</div>';
}

/* A collapsible expert section. Everything scientific goes in one of these, so a
   non-specialist never scrolls past CAPE and helicity to reach tomorrow's weather. */
function xsec(id,title,hint,body,open){
  return '<details class="xsec" id="'+id+'"'+(open?' open':'')+'>'
    +'<summary>'+title+(hint?'<span class="sumhint">'+hint+'</span>':'')+'</summary>'
    +'<div class="xbody">'+body+'</div></details>';
}

/* Selected lead time for the Εξειδικευμένα tab. Module state, not DOM: a
   re-render after a new location or a tier change keeps the expert's choice. */
let XSEL={day:0,hour:0};
let XD=null;              // last /api/brief payload: lat/lon, tier, expert data
let XBUSY=false;

function xPro(){ return !!(XD && XD.tier && XD.tier.is_pro); }
function xUpto(){ return (XD && XD.tier && XD.tier.pro_hours) || PRO_HOURS; }

/* Hours the GFS run actually publishes for the chosen day. GFS is hourly to
   +120 h and 3-hourly after, so the control offers only real steps and never
   has to explain a 404. Step 0 is the analysis, which has no forecast content,
   so the list starts at +1 h. Same rule as gfs_steps on the server. */
function xSteps(day, upto){
  const out=[];
  for(let h=0; h<24; h++){
    const abs=day*24+h;
    if(abs>upto) break;
    if(abs>0 && (abs<=120 || abs%3===0)) out.push(h);
  }
  return out;
}

/* Days that actually contain a published step, so a 240 h horizon yields days
   1..10 and the lone +240 step reads as "+240 h" rather than a phantom day 11. */
function xDays(upto){
  const days=[];
  for(let abs=1; abs<=upto; abs++){
    if(abs<=120 || abs%3===0) days.push(Math.floor(abs/24));
  }
  return days.filter((v,i,a)=>a.indexOf(v)===i);
}

/* The picker. Days are bounded by the tier so an expert who only reaches 72 h
   sees 3 days, not 10 with a failure at the end. */
function renderExpertPicker(){
  if(!xPro()) return '';
  const upto=xUpto();
  const days=xDays(upto);
  if(!days.includes(XSEL.day)) XSEL.day = days[0] ?? 0;
  const steps=xSteps(XSEL.day, upto);
  if(!steps.includes(XSEL.hour)) XSEL.hour = steps.length ? steps[0] : 0;

  let dayOpts='';
  for(const i of days){
    const from=i*24, to=Math.min(i*24+23, upto);
    dayOpts+='<option value="'+i+'"'+(i===XSEL.day?' selected':'')+'>Ημέρα '+(i+1)
      +' (+'+from+'–'+to+' h)</option>';
  }
  let hourOpts='';
  for(const h of steps){
    hourOpts+='<option value="'+h+'"'+(h===XSEL.hour?' selected':'')+'>'
      +String(h).padStart(2,'0')+':00</option>';
  }
  const e=(XD&&XD.expert)||{};
  return '<div class="xpick card" id="xpick-live">'
    +'<div class="xh">Χρόνος πρόγνωσης <span class="xspin" id="xspin" hidden></span></div>'
    +'<div class="xrow">'
    +'<label>Ημέρα<select id="xp-day" onchange="expertPick()">'+dayOpts+'</select></label>'
    +'<label>Ώρα<select id="xp-hour" onchange="expertPick()">'+hourOpts+'</select></label>'
    +'</div>'
    +(e.valid_label?'<div class="note" id="xp-valid">Έγκυρο: <b>'+e.valid_label
      +'</b> · +'+e.step+' h από το run '+(e.run||'')+'.</div>':'<div class="note" id="xp-valid"></div>')
    +'</div>';
}

function renderExpertShell(){
  return '<div class="tierbar" id="tierbar-expert"></div>'
    + renderExpertPicker()
    + '<div id="xbody-live"></div>';
}

function expertPick(){
  const dayEl=document.getElementById('xp-day'), hourEl=document.getElementById('xp-hour');
  if(dayEl) XSEL.day=parseInt(dayEl.value,10)||0;
  if(hourEl) XSEL.hour=parseInt(hourEl.value,10)||0;
  loadExpert();
}

/* Fetch the profile for the selected time and repaint the body in place. The
   picker is rebuilt (the day change alters the hour list) but the body's <details>
   are not, so the section the expert opened stays open across a reload. */
async function loadExpert(){
  const d=XD; if(!d || !xPro()) return;
  XBUSY=true;
  const spin=document.getElementById('xspin'); if(spin) spin.hidden=false;
  const dayEl=document.getElementById('xp-day'), hourEl=document.getElementById('xp-hour');
  if(dayEl) dayEl.disabled=true; if(hourEl) hourEl.disabled=true;

  const hdrs={}; if(TOKEN) hdrs['X-WX-Token']=TOKEN;
  const qs='lat='+d.meta.lat+'&lon='+d.meta.lon+'&day='+XSEL.day+'&hour='+XSEL.hour;
  try{
    const r=await fetch('/api/expert?'+qs,{headers:hdrs});
    if(r.status===403){ d.expert={locked:true}; }
    else { d.expert=Object.assign({}, d.expert, await r.json()); }
  }catch(err){
    d.expert=Object.assign({}, d.expert, {error:'Δεν ήταν δυνατή η φόρτωση των δεδομένων.'});
  }
  XBUSY=false;

  const root=document.getElementById('expert'); if(!root) return;
  if(d.expert.locked){
    renderExpert(d);            // tier dropped: let the locked path take over
    return;
  }
  root.innerHTML=renderExpertShell();
  renderTierBar(d.tier||TIER,'tierbar-expert');
  expertBody(d);
  // Tracked after the body is painted, so an event never precedes the view.
  track('expert_time_changed',{value:XSEL.day*24+XSEL.hour});
}

function renderExpert(d){
  XD=d;
  const e=d.expert||{};
  let h='<div class="tierbar" id="tierbar-expert"></div>';

  // Free tier: the server sent no expert data. Everything below is a placeholder.
  if(e.locked){
    h+=xsec('x-indices','Δείκτες αστάθειας και κινηματικής','PRO',
      lockedBlock('indices','Οι δείκτες είναι διαθέσιμοι στο PRO',
        'SBCAPE, MLCAPE, MUCAPE, CIN, shear 0–1 km και 0–6 km, SRH 0–3 km, LCL και '
        +'ισόθερμο 0°C — υπολογισμένοι από εμάς πάνω στα GFS pressure levels.'));
    h+=xsec('x-skewt','Ραδιοβόλιση / Skew-T','PRO',
      lockedBlock('skewt','Η ραδιοβόλιση είναι διαθέσιμη στο PRO',
        'Διάγραμμα Skew-T με καμπύλες θερμοκρασίας και σημείου δρόσου, barb ανέμου ανά '
        +'επίπεδο και αδιαβατικές γραμμές.'));
    h+=xsec('x-models','Σύγκριση 3 μοντέλων','PRO',
      lockedBlock('models','Η σύγκριση 3 μοντέλων είναι διαθέσιμη στο PRO',
        'GFS 0.25°, ICON-EU 7 km και ECMWF IFS δίπλα-δίπλα, με τη μεταξύ τους απόκλιση '
        +'ως ένδειξη συμφωνίας των μοντέλων.'));
    h+=xsec('x-levels','Κατακόρυφη δομή','PRO',
      lockedBlock('levels','Η κατακόρυφη δομή είναι διαθέσιμη στο PRO',
        'Πίεση, ύψος, θερμοκρασία, σημείο δρόσου, σχετική υγρασία και άνεμος για κάθε '
        +'επίπεδο πίεσης.'));
    document.getElementById('expert').innerHTML=h;
    wrapTables(document.getElementById('expert'));
    renderTierBar(d.tier||TIER,'tierbar-expert');
    return;
  }

  if(e.error){document.getElementById('expert').innerHTML='<div class="card err">'+e.error+'</div>';return}

  h+=renderExpertShell();

  document.getElementById('expert').innerHTML=h;
  renderTierBar(d.tier||TIER,'tierbar-expert');
  expertBody(d);
  XSEL.day=0; XSEL.hour=0;      // a new location starts at "now"
  loadExpert();
}

/* Everything below the selector. Split out so changing the hour re-renders only
   this part: rebuilding the whole tab would collapse the <details> the expert
   just opened. */
function expertBody(d){
  const e=d.expert||{};
  const host=document.getElementById('xbody-live'); if(!host) return;
  let h='';
  const g=e.derived||{};

  /* The interpretation sits outside the collapsible sections on purpose: it is
     the conclusion a forecaster needs in order to decide whether to open the
     numbers at all, so burying it behind a click would invert the hierarchy. */
  if(!g.error){
    h+='<div class="verdict"><h3>Σύνοψη έντονων φαινομένων</h3><ul>';
    for(const line of (e.interpretation||[])) h+='<li>'+line+'</li>';
    h+='</ul><p class="note">Κριτήριο προειδοποίησης περιστροφής: SRH ≥ 100 m²/s² '
      +'<b>και</b> shear 0–6 km ≥ 30 kt <b>και</b> CAPE &gt; 300 J/kg — απαιτούνται και τα τρία.</p></div>';

    const rows=[['SBCAPE',n0(g.sbcape_j_kg),'J/kg',capeTone(g.sbcape_j_kg)],
                ['MLCAPE',n0(g.mlcape_j_kg),'J/kg',capeTone(g.mlcape_j_kg)],
                ['MUCAPE',n0(g.mucape_j_kg),'J/kg',capeTone(g.mucape_j_kg)],
                ['SBCIN',n0(g.sbcin_j_kg),'J/kg',cinTone(g.sbcin_j_kg)],
                ['Shear 0–1 km',n1(g.shear_0_1km_kt),'kt',shearTone(g.shear_0_1km_kt)],
                ['Shear 0–6 km',n1(g.shear_0_6km_kt),'kt',shearTone(g.shear_0_6km_kt)],
                ['SRH 0–3 km',n0(g.srh_0_3km_m2s2),'m²/s²',srhTone(g.srh_0_3km_m2s2)],
                ['LCL',n0(g.lcl_m_agl),'m AGL','dim'],
                ['Ισόθερμο 0°C',n0(g.freezing_level_m),'m','dim']];
    let grid='<div class="xgrid">';
    for(const [k,v,u,t] of rows) grid+=xcell(k,v,u,t,toneWord(t));
    grid+='</div>';
    h+=xsec('x-indices','Δείκτες αστάθειας και κινηματικής','CAPE · shear · SRH · LCL',grid,true);
  }

  h+=xsec('x-skewt','Ραδιοβόλιση / Skew-T','T · Td · άνεμος ανά επίπεδο',
    '<img class="skewt" src="/api/skewt?lat='+d.meta.lat+'&lon='+d.meta.lon
      +'&step='+(e.step!=null?e.step:12)+'&t='+Date.now()
      +(TOKEN?'&token='+encodeURIComponent(TOKEN):'')+'" alt="Skew-T" loading="lazy">'
    +'<p class="note">Κόκκινη: θερμοκρασία αέρα. Πράσινη: σημείο δρόσου. Όταν συγκλίνουν, ο αέρας '
    +'κορέσσεται. Τα barb δείχνουν άνεμο ανά επίπεδο. Διαγώνιες γραμμές: ξηρές/υγρές αδιαβατικές '
    +'και γραμμές ανάμιξης. Σημειωμένα επίπεδα: 850, 700, 500, 300 hPa.'
    +(e.valid_label?' Το διάγραμμα είναι για <b>'+e.valid_label+'</b>, το ίδιο βήμα με τους δείκτες.':'')
    +'</p>');

  if(e.agreement){
    h+='<div class="verdict"><h3>Συμφωνία μοντέλων</h3>'
      +'<div class="bar '+(e.agreement.available?e.agreement.class:'unknown')+'"><i></i></div>'
      +'<p><span class="badge '+(e.agreement.available?e.agreement.class:'')+'">'
      +e.agreement.text+'</span> '+(e.agreement.detail||'')+'</p>'
      +'<p class="note">Εκτίμηση από τη σύγκλιση μοντέλων, όχι από ιστορικό σφάλμα. Αν όλα τα '
      +'μοντέλα κάνουν το ίδιο λάθος, η τιμή θα φαίνεται υψηλή.</p></div>';
  }

  if(e.model_grid){
    let t='<table class="dense"><thead><tr><th>Μοντέλο</th><th>Ανάλυση</th>'
      +'<th>Άδεια</th><th>Θερμ. τώρα</th><th>Θερμ. +24h</th><th>Υετός 24h</th></tr></thead><tbody>';
    for(const m of e.model_grid){
      t+='<tr><td>'+m.model+'</td><td class="mm">'+m.res+'</td><td class="mm">'+m.license+'</td>'
        +'<td>'+n1(m.t_now)+'°</td><td>'+n1(m.t_24h)+'°</td>'
        +'<td>'+(m.precip_24h==null?'—':n1(m.precip_24h)+' mm')+'</td></tr>';
    }
    t+='</tbody></table><p class="note">Το GFS είναι ωριαίο. Το ICON-EU δημοσιεύει ανά ώρα έως +24h '
      +'και κάθε 3 ώρες μετά, επομένως η στήλη +24h χρησιμοποιεί το διαθέσιμο βήμα.</p>';
    h+=xsec('x-models','Σύγκριση μοντέλων','GFS · ICON · ECMWF',t);
  }

  if(e.levels){
    let t='<table class="dense"><thead><tr><th>Πίεση hPa</th><th>Ύψος m</th><th>T °C</th>'
      +'<th>Td °C</th><th>RH %</th><th>Άνεμος kt</th></tr></thead><tbody>';
    for(const l of e.levels){
      t+='<tr><td>'+l.p+'</td><td>'+n0(l.z)+'</td><td>'+n1(l.t)+'</td><td>'+n1(l.td)+'</td>'
        +'<td>'+n0(l.rh)+'</td><td>'+n1(l.wind)+'</td></tr>';
    }
    t+='</tbody></table>';
    h+=xsec('x-levels','Κατακόρυφη δομή',e.levels.length+' επίπεδα πίεσης',t);
  }

  host.innerHTML=h;
  wrapTables(host);
}
/* ============================ "Ο Ουρανός Τώρα" ============================
   Sun, moon and twilight for the selected point. Everything is computed on the
   server for that latitude/longitude/elevation, so Ηλιούπολη and Γλινάδο Νάξου
   genuinely differ. Mounted from renderSimple as an async placeholder: a slow or
   failed astro call must never hold up the forecast, so it renders its own error.
   ======================================================================== */

function skyEventCell(key,label,ev,sub){
  const miss = !ev;
  return '<div class="ev'+(miss?' miss':'')+'"><div class="ek">'+label+'</div>'
    +'<div class="evv">'+(miss?'—':ev.label)+'</div>'
    +(sub?'<div class="evsub">'+sub+'</div>':'')+'</div>';
}

/* A 24 h altitude track drawn as inline SVG. Deliberately not Chart.js: this is
   one smooth arc, and a 300 kB chart library for it would be absurd. The y axis
   spans -90..90 so the horizon line sits at a truthful position, and the zero
   line marks the geometric horizon. */
function skyTrack(track,cls,horizonDeg,nowH){
  if(!track||!track.length) return '';
  const W=300,H=74,pad=3;
  const x=h=>pad+(h/24)*(W-2*pad);
  const y=alt=>H-pad-((alt+90)/180)*(H-2*pad);
  let pts=[],area=[];
  for(const p of track){ pts.push(x(p.h).toFixed(1)+','+y(p.alt).toFixed(1)); }
  const zeroY=y(0);
  const hzY=y(horizonDeg);
  // Gridlines sit on whole local clock hours (the track starts at local midnight),
  // so the labels are 00/06/12/18 rather than an offset from the current minute.
  let grid='';
  for(let h=0;h<=24;h+=6){
    grid+='<line class="hr" x1="'+x(h).toFixed(1)+'" y1="'+pad+'" x2="'+x(h).toFixed(1)
      +'" y2="'+(H-pad)+'"/>';
    grid+='<text class="lbl" x="'+x(h).toFixed(1)+'" y="'+(H+1)+'" text-anchor="middle">'
      +String(h%24).padStart(2,'0')+'</text>';
  }
  const d='M'+pts.join(' L');
  const areaD=d+' L'+x(24).toFixed(1)+','+zeroY.toFixed(1)+' L'+x(0).toFixed(1)+','+zeroY.toFixed(1)+' Z';
  // dot at the sample nearest the current moment, so the arc reads as "where we are"
  const nearest=track.reduce((a,b)=>Math.abs(b.h-nowH)<Math.abs(a.h-nowH)?b:a,track[0]);
  return '<div class="track '+cls+'"><div class="ttl">Τροχιά 24ώρου · ύψος</div>'
    +'<svg viewBox="0 0 '+W+' '+(H+11)+'" preserveAspectRatio="none" role="img">'
    +grid
    +'<line class="axis" x1="'+pad+'" y1="'+hzY.toFixed(1)+'" x2="'+(W-pad)+'" y2="'+hzY.toFixed(1)+'"/>'
    +'<path class="area" d="'+areaD+'"/>'
    +'<path class="curve" d="'+d+'"/>'
    +'<circle class="nowdot" cx="'+x(nearest.h).toFixed(1)+'" cy="'+y(nearest.alt).toFixed(1)+'" r="3.4"/>'
    +'</svg></div>';
}

function renderSkyNow(a){
  if(!a) return '';
  if(!a.available){
    // The reason plus the exact command is deliberate: this card is the only
    // place an operator notices the dependency is missing, so it must be
    // actionable there rather than only in the server log.
    let h='<h3>Ο Ουρανός Τώρα</h3><div class="card"><p class="note">'
      +esc(a.reason||'Τα αστρονομικά δεδομένα δεν είναι διαθέσιμα αυτή τη στιγμή.');
    if(a.interpreter) h+='<br>Interpreter: <code>'+esc(a.interpreter)+'</code>'
      +' (Python '+esc(a.python_version||'?')+')';
    if(a.import_error) h+='<br>Σφάλμα φόρτωσης: <code>'+esc(a.import_error)+'</code>';
    if(a.install_hint) h+='<br>Διόρθωση: <code>'+esc(a.install_hint)+'</code>.';
    h+='</p></div>';
    return h;
  }
  const s=a.sun||{}, m=a.moon||{}, n=a.now||{};
  const nowLocalH = n.clock ? (parseInt(n.clock.slice(0,2),10)+parseInt(n.clock.slice(3,5),10)/60) : 12;

  let h='<div class="astro card">';
  h+='<div class="ahead"><div class="atitle"><span class="ic">🔭</span>Ο Ουρανός Τώρα</div>'
    +'<div class="awhen"><b>'+(n.label||'')+'</b><br>'
    +'Τοπική ώρα <b>'+(n.clock||'')+'</b> ('+a.utc_offset+') · UTC '+(n.utc||'')
    +(a.elevation_m!=null?' · υψόμετρο '+a.elevation_m+' m':'')+'</div></div>';

  h+='<div class="agrid">';

  /* ---------------- Sun ---------------- */
  h+='<div class="apanel sun"><div class="aphead"><span class="big">'+(s.is_up?'☀️':'🌙')+'</span>'
    +'<div><div class="nm">Ήλιος</div><div class="st">'
    +(s.is_up?'πάνω από τον ορίζοντα':'κάτω από τον ορίζοντα')+'</div></div>'
    +'<div class="now"><b>'+(s.altitude==null?'—':s.altitude+'°')+'</b>ύψος · '
    +(s.azimuth_compass||'')+' '+(s.azimuth==null?'':s.azimuth+'°')+'</div></div>';

  h+='<div class="aevents">'
    +skyEventCell('rise','Ανατολή',s.rise)
    +skyEventCell('tra','Μεσουράνηση',s.transit)
    +skyEventCell('set','Δύση',s.set)
    +'</div>';

  h+='<div class="arow"><span class="k">Διάρκεια ημέρας</span><span class="v">'
    +(s.day_length_h==null?'—':s.day_length_h+' ώρες')+'</span></div>';
  h+=skyTrack(s.track,'sun',s.horizon_deg||0,nowLocalH);

  const tw=s.twilight||{};
  const order=['civil','nautical','astro'];
  let twRows='';
  for(const k of order){
    const t=tw[k]; if(!t) continue;
    twRows+='<div class="arow"><span class="k">'+t.label+' λυκαυγές/λυκόφως (−'+t.degrees+'°)</span>'
      +'<span class="v">'+((t.dawn&&t.dawn.label)||'—')+' / '+((t.dusk&&t.dusk.label)||'—')
      +'</span></div>';
  }
  if(twRows) h+='<div class="arow" style="border-bottom:0;padding-bottom:0"><span class="k">'
    +'Λυκόφως</span><span class="v"></span></div>'+twRows;
  h+='</div>';

  /* ---------------- Moon ---------------- */
  const wax=m.waxing?'αύξουσα':'φθίνουσα';
  h+='<div class="apanel moon"><div class="aphead"><span class="big">'+moonGlyph(m.illumination_pct)+'</span>'
    +'<div><div class="nm">Σελήνη</div><div class="st">'+(m.phase_name||'')+' · '+wax+'</div></div>'
    +'<div class="now"><b>'+(m.altitude==null?'—':m.altitude+'°')+'</b>ύψος · '
    +(m.azimuth_compass||'')+' '+(m.azimuth==null?'':m.azimuth+'°')+'</div></div>';

  h+='<div class="aevents">'
    +skyEventCell('rise','Ανατολή',m.rise)
    +skyEventCell('tra','Μεσουράνηση',m.transit)
    +skyEventCell('set','Δύση',m.set)
    +'</div>';

  h+='<div class="arow"><span class="k">Φωτισμός</span><span class="v">'
    +n0(m.illumination_pct)+'%</span></div>';
  h+='<div class="phasebar"><i style="width:'+Math.max(0,Math.min(100,m.illumination_pct||0))+'%"></i></div>';
  h+='<div class="arow"><span class="k">Ηλικία</span><span class="v">'+n1(m.age_days)+' ημέρες</span></div>';
  h+=skyTrack(m.track,'moon',m.horizon_deg||0,nowLocalH);
  h+='<div class="arow" style="margin-top:8px"><span class="k">Επόμενη νέα σελήνη</span>'
    +'<span class="v">'+(m.next_new_moon||'—')+'</span></div>';
  h+='<div class="arow"><span class="k">Επόμενη πανσέληνος</span><span class="v">'
    +(m.next_full_moon||'—')+'</span></div>';
  h+='</div>';

  h+='</div>'; // agrid

  h+='<p class="anote">Ώρες σε τοπική ζώνη '+a.utc_offset+'. Οι ώρες ανατολής/δύσης '
    +'χρησιμοποιούν τον ορίζοντα του τόπου σου: σε υψόμετρο ο ορίζοντας «κατεβαίνει», '
    +'οπότε ο Ήλιος ανατέλλει ελαφρώς νωρίτερα από ό,τι στη θάλασσα. Ο '
    +'λύκος/λυκαυγές υπολογίζεται με το κέντρο του ηλιακού δίσκου στις −6°, −12° και '
    +'−18°, όπως ορίζεται για αστροφωτογραφία. Υπολογισμοί με PyEphem (MIT), για το '
    +'συγκεκριμένο σημείο ('+n1(a.lat)+', '+n1(a.lon)+').</p>';
  h+='</div>';
  return h;
}

/* Moon glyph chosen from the illumination and whether it is waxing, so the icon
   is not a fixed picture that contradicts the percentage next to it. */
function moonGlyph(pct){
  const p=(pct==null?100:pct);
  if(p<3) return '🌑';
  if(p<40) return '🌒';
  if(p<60) return '🌓';
  if(p<90) return '🌔';
  if(p<97) return '🌕';
  return '🌕';
}

let SKY_REQ=0;
async function loadSkyNow(lat,lon,elev){
  const host=document.getElementById('skynow');
  if(!host) return;
  const mine=++SKY_REQ;   // ignore a response that arrives after a newer pick
  host.innerHTML='<p class="spin">Υπολογισμός ουρανού…</p>';
  const qs=new URLSearchParams({lat:lat,lon:lon});
  if(elev!=null) qs.set('elev',elev);
  let a=null;
  try{ a=await (await fetch('/api/sky?'+qs)).json(); }
  catch(e){ a={available:false,reason:e.message}; }
  if(mine!==SKY_REQ) return;
  host.innerHTML=renderSkyNow(a);
}

function card(k,v,s){return '<div class="card"><div class="k">'+k+'</div><div class="v">'+v
  +'</div><div class="s">'+s+'</div></div>';}

/* The first screen. Kept to what a non-specialist needs: an icon, the current
   temperature, feels-like, today's range, and one plain sentence. Anything that
   needs explanation lives in the Εξειδικευμένα tab instead of here.

   Missing values render as an em dash rather than 0 or NaN - a hero that shows a
   confident wrong number is worse than one that admits it has none. */
function nowHero(hr){
  if(!hr) return '';
  const deg = v => v==null ? '—' : n0(v);
  const chips = [];

  if(hr.temp_tone && hr.temp_tone!=='unknown' && hr.feels!=null)
    chips.push('<span class="chip '+hr.temp_tone+'"><span class="lb">αίσθηση</span> '
      +n1(hr.feels)+'°</span>');

  if(hr.wind_tone && hr.wind_tone!=='unknown' && hr.bft!=null)
    chips.push('<span class="chip '+hr.wind_tone+'"><span class="lb">άνεμος</span> '
      +hr.bft+' Bft</span>');

  let h='<div class="nowhero'+(hr.severe?' severe':'')+'">';
  h+='<div><div class="glyph">'+(hr.icon||'🌡️')+'</div>'
    +'<div class="cond">'+(hr.condition||'')+'</div></div>';
  h+='<div><div class="big">'
    +'<span class="temp">'+deg(hr.t)+'<sup>°C</sup></span>'
    +'<span class="feels">αίσθηση <b>'+(hr.feels==null?'—':n1(hr.feels)+'°')+'</b></span>'
    +'</div>';
  h+='<div class="range"><span>Σήμερα <b>'+deg(hr.tmin)+'° – '+deg(hr.tmax)+'°</b></span>'
    +(hr.bft!=null?'<span>Άνεμος έως <b>'+hr.bft+' Bft</b></span>':'')
    +'</div>';
  h+='<p class="line">'+(hr.headline||'')+'</p>';
  if(chips.length) h+='<div class="chips">'+chips.join('')+'</div>';
  h+='</div></div>';
  return h;
}

function renderAttribution(d){
  const a=d.attribution||{};
  /* This block is only the data-licence text; contacts and legal links live in
     the server-rendered #site footer, so a failed forecast cannot hide them. */
  document.getElementById('attr').innerHTML =
    '<b>Πηγές δεδομένων και άδειες.</b><br>'
    +'GFS: '+a.gfs+'<br>'
    +'ICON-EU: '+(a.icon||'')+'<br>'
    +'ECMWF: '+(a.ecmwf||'')+'<br>'
    +(a.geocoding||'')+'<br>'
    +'Οι δείκτες (CAPE, shear, SRH, LCL) και τα διαγράμματα υπολογίζονται από αυτή την υπηρεσία '
    +'με MetPy πάνω στα ανοιχτά grid δεδομένα.<br>'
    +'<a href="/licenses">Αναλυτικός κατάλογος αδειών</a>';
}
document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeModal(); });

/* ---------- live sky cameras ----------
   Cameras are the only thing here that is not reproducible from open data, so
   they get shown as-is. When a feed is not wired up yet the card says so plainly
   instead of showing a broken image. */
let CAMS=null, LIVE_ON=false, CAM_TIMER=null, CAM_LAST={};

async function loadCameras(){
  const box=document.getElementById('cams');
  // One deliberate retry, then stop: a transient blip should not leave the
  // section dead, but a persistent failure must not become a retry loop.
  for(let attempt=0; attempt<2; attempt++){
    try{
      const r=await fetch('/api/cameras');
      if(!r.ok) throw new Error('http '+r.status);
      CAMS=await r.json();
      track('sky_camera_opened');
      renderCameras();
      return;
    }catch(e){
      if(attempt===0){ await new Promise(res=>setTimeout(res,1500)); continue; }
      box.innerHTML='<div class="card err" role="status">'
        +'Οι κάμερες δεν φόρτωσαν. Δοκίμασε ξανά σε λίγο.</div>';
    }
  }
}
function renderCameras(){
  const box=document.getElementById('cams');
  if(!CAMS||!CAMS.cameras||!CAMS.cameras.length){
    box.innerHTML='<div class="card">Δεν έχουν ρυθμιστεί κάμερες.</div>'; return;
  }
  if(CAMS.note!=null) document.getElementById('cam-sub').textContent=CAMS.note;
  box.innerHTML=CAMS.cameras.map(c=>{
    const live=c.status==='live';
    let stage, badge;
    if(live){
      // The frame starts hidden behind a loading line. It is revealed on load
      // and swapped for an offline line on error, so the card never shows a
      // broken-image icon and never claims LIVE for a frame it does not have.
      stage='<span class="camload">Φόρτωση εικόνας…</span>'
        +'<img id="camimg-'+esc(c.id)+'" src="'+esc(camSnapshotSrc(c, CAMS.stamp))+'"'
        +' alt="'+esc(c.name)+' — ζωντανή εικόνα" loading="lazy"'
        +' onload="snapshotLoaded(\''+esc(jsq(c.id))+'\')"'
        +' onerror="snapshotFailed(\''+esc(jsq(c.id))+'\')">';
      badge='<div class="live" id="cambadge-'+esc(c.id)+'" hidden><i></i>LIVE</div>';
    }else{
      stage='<div class="off">Η εικόνα δεν είναι διαθέσιμη<br><b>'+esc(c.name)+'</b></div>';
      badge='<div class="live off"><i></i>OFFLINE</div>';
    }
    const golive = (live && c.live)
      ? '<button class="golive" onclick="openCamLive(\''+esc(jsq(c.id))+'\')"'
        +' aria-label="Άνοιγμα ζωντανής ροής: '+esc(c.name)+'">🔴 LIVE</button>'
      : '';
    const tl = c.timelapse
      ? '<a href="'+esc(c.timelapse)+'" target="_blank" rel="noopener"><button>Timelapse</button></a>'
      : '<button disabled title="Δεν έχει ρυθμιστεί timelapse">Timelapse</button>';
    // Visitor-facing and deliberately uninformative: a missing feed must not
    // report *why* it is missing, which would expose server configuration.
    const note = live? '' :
      '<div class="notebox">Η εικόνα αυτής της κάμερας δεν είναι ακόμη διαθέσιμη.</div>';
    const mapq=c.lat!=null&&c.lon!=null? ' onclick="gotoPoint('+c.lat+','+c.lon+',\''+esc(jsq(c.name))+'\')"' : '';
    const cadence = live
      ? '<div class="cad">Αυτόματη εικόνα: κάθε '+c.snapshot_interval_min+' '+pluralMin(c.snapshot_interval_min)+'</div>'
      : '';
    const upd = live
      ? '<div class="upd" id="camupd-'+esc(c.id)+'" aria-live="polite"></div>'
      : '';
    return '<div class="cam" id="camcard-'+esc(c.id)+'" role="group"'
      +' aria-label="'+esc(c.name)+'">'
      +'<div class="stage" id="camstage-'+esc(c.id)+'">'+stage+badge+golive+'</div>'
      +'<div class="camplayer" id="camplayer-'+esc(c.id)+'"></div>'
      +'<div class="camctl" id="camctl-'+esc(c.id)+'" hidden></div>'
      +'<div class="meta"><div class="nm">'+esc(c.name)+'</div>'
      +'<div class="rg">'+esc(c.region||'')+(c.lat!=null?' · '+c.lat.toFixed(2)+', '+c.lon.toFixed(2):'')+'</div>'
      +cadence
      +'<div class="actions">'+(c.lat!=null?'<button'+mapq+'>Στην πρόγνωση</button>':'')+tl+'</div>'
      +upd+note+'</div></div>';
  }).join('');
}
function pluralMin(n){ return n===1? 'λεπτό':'λεπτά'; }
/* Every state transition goes through these two helpers, so the loading line,
   the LIVE badge and the timestamp can never disagree about what the card is
   showing. `camBeginLoad` is used on the first render and on every refresh;
   `snapshotLoaded` / `snapshotFailed` are the only two ways it resolves. */
function camBeginLoad(id){
  const stage=document.getElementById('camstage-'+id);
  const img=document.getElementById('camimg-'+id);
  const badge=document.getElementById('cambadge-'+id);
  if(badge){ badge.hidden=true; badge.classList.remove('off'); } // reset any prior error
  if(img) img.style.display='';
  if(stage){
    const stale=stage.querySelector('.off');
    if(stale) stale.remove();
    if(!stage.querySelector('.camload')){
      const l=document.createElement('span');
      l.className='camload'; l.textContent='Φόρτωση εικόνας…';
      stage.insertBefore(l, stage.firstChild);
    }
  }
  camUpdate(id, '');
}
function camUpdate(id, text){
  const el=document.getElementById('camupd-'+id);
  if(el) el.textContent=text;
}
function snapshotLoaded(id){
  const stage=document.getElementById('camstage-'+id);
  const load=stage && stage.querySelector('.camload');
  if(load) load.remove();
  const badge=document.getElementById('cambadge-'+id);
  if(badge) badge.hidden=false;
  camUpdate(id, 'Τελευταία ενημέρωση: '+clockTime());
}
function clockTime(){
  const d=new Date();
  return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');
}
/* Where a still comes from. Today that is normally the feed's own public URL
   ("direct"), which the browser loads exactly as it always has. A camera whose
   source is private is marked snapshot_via==="server": its URL must never reach
   the browser, so the image is proxied through /api/cameras/<id>/snapshot. The
   server decides which, per camera, so a private source can be added later
   without touching this card. */
function camSnapshotSrc(c, stamp){
  const sep=c.snapshot.includes('?')?'&':'?';
  if(c.snapshot_via==='server'){
    return '/api/cameras/'+encodeURIComponent(c.id)+'/snapshot?t='+stamp;
  }
  return c.snapshot+sep+'t='+stamp;
}
/* A still that failed to load must not render as a broken icon or keep claiming
   LIVE: the server-side path fails generically (503/504) and the card says so,
   the same as a feed that is not wired up. The badge goes back to a neutral
   OFFLINE, so "LIVE" is only ever shown next to a frame that actually arrived. */
function snapshotFailed(id){
  const c=CAMS&&CAMS.cameras.find(x=>x.id===id);
  const stage=document.getElementById('camstage-'+id);
  const img=document.getElementById('camimg-'+id);
  if(img) img.style.display='none';
  const load=stage && stage.querySelector('.camload');
  if(load) load.remove();
  const badge=document.getElementById('cambadge-'+id);
  if(badge){ badge.classList.add('off'); badge.hidden=false; }
  if(stage && !stage.querySelector('.off')){
    const d=document.createElement('div');
    d.className='off';
    d.innerHTML='Η εικόνα δεν είναι διαθέσιμη<br><b>'+esc((c&&c.name)||'')+'</b>';
    stage.appendChild(d);
  }
  camUpdate(id, '');
}
/* Live playback is a *provider* concern, not a WebRTC one. Today the only
   provider is YouTube, embedded through the official player with the public
   video id the server publishes. The camera's own address never reaches the
   browser, so there is no RTSP URL to leak. Opening is a user action (so autoplay
   is allowed), and closing restores the last snapshot the card already holds.
   The player occupies its own region (#camplayer-*), never the snapshot stage:
   the snapshot stage is hidden while live is open, so no custom control (badge,
   LIVE button, close button) can ever overlay the iframe. */
function youtubeEmbedUrl(videoId){
  // Privacy-enhanced mode, per YouTube's documentation. `mute=1` is a default,
  // not the audio guarantee: audio is kept out of the stream pipeline itself.
  const q=new URLSearchParams({autoplay:'1',mute:'1',playsinline:'1',rel:'0'});
  return 'https://www.youtube-nocookie.com/embed/'+encodeURIComponent(videoId)
    +'?'+q.toString();
}
function openCamLive(id){
  if(!CAMS) return;
  const c=CAMS.cameras.find(x=>x.id===id);
  if(!c||!c.live) return;
  const stage=document.getElementById('camstage-'+id);
  const player=document.getElementById('camplayer-'+id);
  const bar=document.getElementById('camctl-'+id);
  if(!stage||!player||!bar) return;
  track('sky_camera_live_opened');
  // Hide the snapshot stage (with its overlays) and give the player its own
  // region. The two are never visible at once, so nothing can sit over the embed.
  stage.classList.add('playing');
  player.classList.add('playing');
  player.innerHTML='';
  if(c.live.provider==='youtube'){
    const f=document.createElement('iframe');
    f.className='camframe';
    f.title=c.name+' — LIVE';
    f.setAttribute('allow','autoplay; encrypted-media; picture-in-picture; fullscreen');
    f.setAttribute('allowfullscreen','');
    f.src=youtubeEmbedUrl(c.live.video_id);
    f.onerror=()=>{ showLiveFallback(player,'Ο player δεν φόρτωσε. Δοκίμασε ξανά.'); };
    player.appendChild(f);
    // Autoplay can be refused; the official player then shows its own play
    // control, which is the correct fallback. Never assume it started.
  } else {
    showLiveFallback(player,'Ο πάροχος ζωντανής ροής δεν υποστηρίζεται.');
  }
  // The close control lives in flow, in its own bar below the player -- outside
  // the iframe's surface, never over the official YouTube controls.
  const state=document.createElement('span');
  state.className='camstate';
  state.innerHTML='<span class="livestate"><i></i>LIVE</span>'
    +'<span class="livehint">Αν δεν ξεκινήσει, πάτησε play στο player.</span>';
  const close=document.createElement('button');
  close.className='closecam'; close.textContent='Κλείσιμο LIVE';
  close.setAttribute('aria-label','Κλείσιμο ζωντανής ροής: '+c.name);
  close.onclick=()=>closeCamLive(id);
  bar.innerHTML=''; bar.appendChild(state); bar.appendChild(close); bar.hidden=false;
}
function closeCamLive(id){
  const stage=document.getElementById('camstage-'+id);
  const player=document.getElementById('camplayer-'+id);
  const bar=document.getElementById('camctl-'+id);
  if(stage) stage.classList.remove('playing');
  if(player){ player.classList.remove('playing'); player.innerHTML=''; }
  if(bar){ bar.hidden=true; bar.innerHTML=''; }
  const c=CAMS&&CAMS.cameras.find(x=>x.id===id);
  const img=document.getElementById('camimg-'+id);
  if(c&&img){ // refresh to the newest frame rather than the stale one
    CAMS.stamp=Math.floor(Date.now()/60000);
    camBeginLoad(id);
    img.src=camSnapshotSrc(c, CAMS.stamp);
  }
}
function showLiveFallback(player,msg){
  const d=document.createElement('div');
  d.className='livefb'; d.textContent=msg;
  player.appendChild(d);
}
function gotoPoint(lat,lon,name){
  // Straight to the forecast for the camera's own coordinates, rather than
  // routing through a location entry that would leave the panels on the old city.
  const q=document.getElementById('q'); if(q) q.value=name;
  load(lat,lon,name+' (κάμερα)');
  window.scrollTo({top:0,behavior:'smooth'});
}
/* Refresh only the pixels, never the whole card: re-rendering on every tick would
   drop focus and make the grid flicker.

   The polling window is `snapshot_interval_min` — the same number the card shows
   the visitor — so the still is re-fetched no more often than the feed it claims
   to come from. It drives a single fast ticker and gates each camera on its own
   cadence, because a per-camera interval would multiply `CAM_TIMER` by the number
   of configured feeds. `refresh_seconds` is therefore the *tick granularity*,
   never a competing cadence: the tick never exceeds a camera's own interval. */
function refreshTickMs(){
  const tick=(CAMS&&CAMS.refresh_seconds||60)*1000;
  const cams=(CAMS&&CAMS.cameras)||[];
  let smallest=null;
  for(const c of cams){
    if(c.status!=='live'||!c.snapshot_interval_min) continue;
    if(smallest===null||c.snapshot_interval_min<smallest) smallest=c.snapshot_interval_min;
  }
  return Math.min(tick, (smallest===null?1:smallest)*60000);
}
function tickCameras(){
  if(!LIVE_ON||!CAMS) return;
  const now=Date.now();
  for(const c of CAMS.cameras){
    if(c.status!=='live') continue;
    const img=document.getElementById('camimg-'+c.id);
    if(!img) continue;
    const due=(c.snapshot_interval_min||1)*60000;
    if(CAM_LAST[c.id]&&now-CAM_LAST[c.id]<due) continue;  // not yet due
    CAM_LAST[c.id]=now;
    CAMS.stamp=Math.floor(now/60000);
    camBeginLoad(c.id);
    img.src=camSnapshotSrc(c, CAMS.stamp);
  }
}
function toggleLive(){
  LIVE_ON=!LIVE_ON;
  const b=document.getElementById('cam-toggle');
  b.classList.toggle('on',LIVE_ON);
  document.getElementById('cam-toggle-label').textContent =
    LIVE_ON? 'LIVE COVERAGE · παύση':'🔴 LIVE COVERAGE';
  if(CAM_TIMER){ clearInterval(CAM_TIMER); CAM_TIMER=null; }
  if(LIVE_ON){ CAM_LAST={}; tickCameras(); CAM_TIMER=setInterval(tickCameras, refreshTickMs()); }
}

/* ---------- ERA5 verification ---------- */
/* Runs at most once per selected point: each call costs a GFS range request per
   lead time plus an ERA5 chunk per hour, so re-running on every tab switch would
   burn the sources' goodwill for no new information. */
function maybeVerify(){
  if(!VERI_POINT) return;
  const key=VERI_POINT.lat.toFixed(3)+','+VERI_POINT.lon.toFixed(3);
  if(VERI_DONE===key) return;
  VERI_DONE=key;
  loadVerification(VERI_POINT.lat,VERI_POINT.lon);
}
async function loadVerification(lat,lon){
  const box=document.getElementById('veri');
  box.innerHTML='<p class="spin">Υπολογισμός σφάλματος έναντι ERA5…</p>';
  try{
    const r=await fetch('/api/verify?lat='+lat+'&lon='+lon+'&days=4');
    const d=await r.json();
    if(!d.ok){ box.innerHTML='<div class="card err">Η επαλήθευση δεν είναι διαθέσιμη: '+esc(d.error||'')+'</div>'; VERI_DONE=null; return; }
    renderVerification(d);
  }catch(e){
    box.innerHTML='<div class="card err">Η επαλήθευση δεν φόρτωσε.</div>';
    VERI_DONE=null;   // a failed run must not block the next attempt
  }
}
function signed(v,unit){ if(v==null) return '—'; return (v>0?'+':'')+v+' '+unit; }
function renderVerification(d){
  const box=document.getElementById('veri');
  const t=d.overall_temperature||{}, w=d.overall_wind||{};
  let h='<div class="veri">';
  h+='<div class="big">'
    +'<div class="b"><div class="k">Σφάλμα θερμοκρασίας (MAE)</div>'
      +'<div class="v">'+(t.mae!=null?t.mae:'—')+'<small> °C</small></div></div>'
    +'<div class="b"><div class="k">Μεροληψία</div>'
      +'<div class="v">'+(t.bias!=null?(t.bias>0?'+':'')+t.bias:'—')+'<small> °C</small></div></div>'
    +'<div class="b"><div class="k">Δείγματα</div>'
      +'<div class="v">'+(t.n||0)+'<small> σημεία</small></div></div>'
    +'</div>';
  h+='<table><thead><tr><th>Ορίζοντας</th><th>Σφάλμα θερμ. (MAE)</th>'
    +'<th>Μεροληψία θερμ.</th><th>Σφάλμα ανέμου (MAE)</th></tr></thead><tbody>';
  for(const b of (d.by_lead||[])){
    const bt=b.temperature||{}, bw=b.wind||{};
    h+='<tr><td class="lead">'+b.lead_h+' h</td>'
      +'<td>'+(bt.mae!=null?bt.mae+' °C':'—')+'</td>'
      +'<td>'+signed(bt.bias,'°C')+'</td>'
      +'<td>'+(bw.mae!=null?bw.mae+' km/h':'—')+'</td></tr>';
  }
  h+='</tbody></table>';
  const win=d.window||{};
  h+='<div class="src">Αληθές μέτρο: <b>'+esc(d.truth||'')+'</b>. Πρόγνωση: <b>'
    +esc(d.forecast||'')+'</b>. Μέση τιμή σε πλαίσιο ±'+d.box_deg+'°, ώστε η σύγκριση '
    +'να αφορά την αέρια μάζα και όχι τη διαφορά των πλεγμάτων. Παράθυρο '
    +esc(String(win.first_valid||''))+' → '+esc(String(win.last_valid||''))
    +' (cycles: '+esc((win.run_dates||[]).join(', '))+').'
    +' Τα νούμερα είναι δείγμα για τα σημεία και τις ημερομηνίες του παραθύρου, όχι '
    +'μέτρηση γενικής υπεροχής της πρόγνωσης· διάβασέ τα μαζί με τον αριθμό δειγμάτων.</div>';
  h+='</div>';
  box.innerHTML=h;
}

/* Returning from Stripe: exchange the paid session for a token. The session id
   is in the URL, so it is cleaned out of history immediately afterwards. */
async function claimCheckout(sessionId){
  try{
    const r=await fetch('/api/checkout/claim',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sessionId})});
    const d=await r.json();
    if(!r.ok) throw new Error(d.detail||'Η ενεργοποίηση απέτυχε.');
    TOKEN=d.token; localStorage.setItem('wx_token',TOKEN);
    history.replaceState({},'',location.pathname);   // drop session_id from the URL
    track('subscription_created');
    await refreshTier();
    if(CUR) await load(CUR.lat,CUR.lon,CUR.label,{elevation_m:CUR.elev});
    else renderCta();
    openModal();
  }catch(e){
    history.replaceState({},'',location.pathname);
    // Error inside the modal the caller is about to land on, not a browser alert.
    openModal();
    uiMsg('Η πληρωμή ολοκληρώθηκε, αλλά η ενεργοποίηση απέτυχε: '+e.message
      +' Στείλε το αναγνωριστικό συναλλαγής στην υποστήριξη.',false);
  }
}

/* Restore the tier on load, so a stored trial or passcode token is reflected
   in the CTA and the tier bar before any location is chosen. */
(async function boot(){
  track('page_view');
  renderFavourites();      // paint saved places before any location is chosen
  loadCameras();
  const q=new URLSearchParams(location.search);
  if(q.get('checkout')==='success' && q.get('session_id')){
    await claimCheckout(q.get('session_id'));
    return;
  }
  if(q.get('checkout')==='cancel'){
    history.replaceState({},'',location.pathname);
    openModal();
    const m=document.getElementById('pm-msg');
    m.className='msg err'; m.textContent='Η πληρωμή ακυρώθηκε. Δεν έγινε καμία χρέωση.';
  }
  if(!TOKEN) return;
  await refreshTier();
  loadPromoLine();          // a returning promo holder sees their window on load
  notifyRenderSafe();       // and a returning PRO holder sees their alert settings
  if(TIER.is_pro) renderCta();
})();
</script></body></html>"""


# ---------------------------------------------------------------- helpers

def fmt_run(date: str, hh: str) -> str:
    return f"{date[6:8]}/{date[4:6]} {hh}:00 UTC"


def nearest_gfs_step(want: int, limit: int) -> int:
    """Snap a wanted lead time to a step the GFS run actually publishes.

    GFS is hourly to +120 h and 3-hourly after, so f019 does not exist on a
    240-hour run. Rounding down would silently show an earlier hour than
    requested, so this picks the genuinely closest published step instead. The
    result is also clamped to the tier's horizon.
    """
    steps = wx.gfs_steps(limit)
    if not steps:
        return 0
    want = max(0, min(int(want), steps[-1]))
    # Closest published step; on a tie prefer the later one.
    return min(steps, key=lambda s: (abs(s - want), -s))


def fmt_valid(iso: str) -> str:
    """'YYYY-MM-DDTHH:MMZ' as a UTC label, matching the run label's clock."""
    t = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)
    return fmt_run(t.strftime("%Y%m%d"), t.strftime("%H"))


def beaufort(kmh: float | None) -> int | None:
    """Beaufort number from km/h. Thresholds are the standard upper bounds."""
    if kmh is None:
        return None
    bounds = [(1, 0), (5, 1), (11, 2), (19, 3), (28, 4), (38, 5), (49, 6),
              (61, 7), (74, 8), (88, 9), (102, 10), (117, 11)]
    for upper, bft in bounds:
        if kmh <= upper:
            return bft
    return 12


# Sky condition for the simple view. Cloud cover comes from GFS TCDC, so the
# icon and wording describe the model's sky, not an inference from humidity.
# Order matters: precipitation outranks cover, because "βροχή" is what a user
# needs to know even when the sky is also overcast.
SKY_BUCKETS = (
    (0, 15, "clear", "Καθαρός ουρανός", "☀️"),
    (15, 40, "mostly-clear", "Λίγα σύννεφα", "🌤️"),
    (40, 70, "partly-cloudy", "Αρκετά σύννεφα", "⛅"),
    (70, 90, "mostly-cloudy", "Συννεφιά", "🌥️"),
    (90, 101, "overcast", "Πλήρης συννεφιά", "☁️"),
)


def sky_condition(cloud_pct: float | None, precip_mm: float | None,
                  bft: int | None = None, is_day: bool = True) -> dict:
    """Plain-language sky state for the hero, plus a stable icon key.

    The icon is returned as an opaque key so the front-end owns the glyph and the
    wording stays testable. ``precip_mm`` is the hour's total: anything measurable
    means the customer cares about rain more than about cloud amount.
    """
    p = precip_mm or 0.0
    if p >= 4.0:
        return {"key": "rain", "text": "Δυνατή βροχή", "icon": "🌧️", "severe": True}
    if p >= 1.0:
        return {"key": "rain", "text": "Βροχή", "icon": "🌧️", "severe": False}
    if p >= 0.1:
        return {"key": "drizzle", "text": "Ψιχάλες", "icon": "🌦️", "severe": False}

    c = 0.0 if cloud_pct is None else cloud_pct
    key, text, icon = "unknown", "Καιρός μη διαθέσιμος", "🌡️"
    for lo, hi, k, t, i in SKY_BUCKETS:
        if lo <= c < hi:
            key, text, icon = k, t, i
            break

    if not is_day and key in ("clear", "mostly-clear"):
        return {"key": "clear-night", "text": "Καθαρός νυχτερινός ουρανός", "icon": "🌙",
                "severe": False}
    return {"key": key, "text": text, "icon": icon, "severe": False}


def temp_tone(t: float | None) -> str:
    """Colour band for a temperature, for the hero badge.

    Bands are about what a person should do, not meteorology: freezing, cold,
    comfortable, hot, extreme. The front-end maps the band to a colour.
    """
    if t is None:
        return "unknown"
    if t < 0:
        return "freezing"
    if t < 10:
        return "cold"
    if t < 26:
        return "mild"
    if t < 34:
        return "hot"
    return "extreme"


def wind_tone(bft: int | None) -> str:
    """Colour band for a Beaufort number: calm, breezy, strong, dangerous."""
    if bft is None:
        return "unknown"
    if bft <= 2:
        return "calm"
    if bft <= 4:
        return "breezy"
    if bft <= 6:
        return "strong"
    return "dangerous"


def _is_daytime(hour: dict) -> bool:
    """Whether the first forecast hour falls in local daylight.

    GFS steps are UTC. Greece is UTC+2 in winter and UTC+3 in summer, so a fixed
    offset would put sunrise two hours off; the offset is taken from the clock
    rather than hardcoded. Used only to choose a night icon, so an approximation
    at the DST edges is acceptable.
    """
    step = hour.get("step_h")
    if step is None:
        return True
    try:
        from zoneinfo import ZoneInfo
        now_local = dt.datetime.now(ZoneInfo("Europe/Athens"))
        local_h = (now_local.hour + step) % 24
    except Exception:
        local_h = (dt.datetime.now(dt.timezone.utc).hour + step + 2) % 24
    return 7 <= local_h < 20


WIND_NAME = {0: "άπνοια", 1: "σχεδόν άπνοια", 2: "ασθενής", 3: "ασθενής",
             4: "μέτριος", 5: "μέτριος", 6: "ισχυρός", 7: "ισχυρός",
             8: "θυελλώδης", 9: "θυελλώδης", 10: "πολύ θυελλώδης",
             11: "σφοδρή θύελλα", 12: "τυφώνας"}
DIR_NAME = ["Β", "ΒΑ", "Α", "ΝΑ", "Ν", "ΝΔ", "Δ", "ΒΔ"]


def dir_name(deg: float | None) -> str:
    if deg is None:
        return ""
    return DIR_NAME[int(round(deg / 45)) % 8]


def wind_phrase(kmh: float | None, deg: float | None) -> str:
    bft = beaufort(kmh)
    if bft is None:
        return "Άνεμος μη διαθέσιμος"
    name = WIND_NAME.get(bft, "")
    d = dir_name(deg)
    base = f"{name} άνεμος" + (f" από {d}" if d else "")
    # Build the sentence case here rather than with .capitalize(), which lowercases
    # the rest of the string and would mangle "Β" (North) and "Bft".
    return f"{base[0].upper()}{base[1:]} — {kmh:.0f} km/h ({bft} Bft)"


def apparent_temp(t_c: float, rh: float | None, wind_kmh: float | None) -> float:
    """Australian apparent temperature: heat index when warm, wind chill when cold.

    Needed because the commercially-clean sources (GFS/ICON/ECMWF) do not publish
    an apparent-temperature field, unlike the Open-Meteo free tier we dropped.
    """
    if wind_kmh is None:
        wind_kmh = 0.0
    if rh is None:
        rh = 50.0
    e = (rh / 100.0) * 6.105 * math.exp(17.27 * t_c / (237.7 + t_c))
    at = t_c + 0.33 * e - 0.70 * (wind_kmh / 3.6) - 4.0
    if t_c <= 10 and wind_kmh > 4.8:
        v = (wind_kmh / 3.6) ** 0.16
        at = 13.12 + 0.6215 * t_c - 11.37 * v + 0.3965 * t_c * v
    return at


def dewpoint(t_c: float, rh: float) -> float:
    a, b = 17.27, 237.7
    alpha = (a * t_c) / (b + t_c) + math.log(max(rh, 1) / 100.0)
    return (b * alpha) / (a - alpha)


def cloud_base_m(t_c: float | None, rh: float | None,
                 elevation_m: float | None = None) -> dict | None:
    """Convective cloud base from the 2 m dewpoint depression.

    The lifted condensation level rises about 125 m per °C of spread between
    temperature and dewpoint, which is the same relationship the Skew-T shows.
    It is a fair-weather estimate: it assumes a well-mixed surface layer, so it
    describes cumulus bases, not the base of a stratiform or frontal deck.
    """
    if t_c is None or rh is None or rh <= 0:
        return None
    td = dewpoint(t_c, rh)
    agl = 125.0 * (t_c - td)
    if agl < 0:
        return None
    out = {"base_agl_m": round(agl), "base_msl_m": None, "spread_c": round(t_c - td, 1)}
    if elevation_m is not None:
        out["base_msl_m"] = round(elevation_m + agl)
    return out


def agreement(series: dict[str, list[float]]) -> dict:
    """Hour-by-hour spread between models. Single source of truth for reliability."""
    models = [m for m, s in series.items() if s]
    if len(models) < 2:
        return {"available": False, "class": "warn", "text": "άγνωστη", "detail": ""}
    n = min(len(series[m]) for m in models)
    spreads = []
    for i in range(n):
        vals = [series[m][i] for m in models if series[m][i] is not None]
        if len(vals) >= 2:
            spreads.append(max(vals) - min(vals))
    if not spreads:
        return {"available": False, "class": "warn", "text": "άγνωστη", "detail": ""}
    avg, mx = float(np.mean(spreads)), float(np.max(spreads))
    cls, txt = (("good", "υψηλή") if avg < 1.5 else
                ("warn", "μέτρια") if avg < 3.0 else ("bad", "χαμηλή"))
    return {"available": True, "class": cls, "text": txt, "avg": round(avg, 2),
            "max": round(mx, 2), "models": len(models), "points": len(spreads),
            "detail": f"Τα {len(models)} μοντέλα αποκλίνουν κατά {avg:.1f}°C μέσο όρο "
                      f"(έως {mx:.1f}°C) στις επόμενες {len(spreads)} ώρες."}


SEVERE_CAPE_MIN = 300.0
SEVERE_SRH_MIN = 100.0
SEVERE_SHEAR_MIN = 30.0

LAPSE_RATE_C_PER_M = 0.0065      # ISA average, the fallback when no profile is usable
LAPSE_MIN_C_PER_M = 0.0040       # near-isothermal air mass
LAPSE_MAX_C_PER_M = 0.0098       # dry adiabatic limit
MAX_ELEV_CORRECTION_C = 12.0     # cap, so a bad elevation cannot wreck the forecast
ELEVATION_LIMIT_M = 3000.0


def derive_lapse_rate(prof) -> tuple[float, str]:
    """Environmental lapse rate read from the model's own sounding.

    6.5 °C/km is the ISA average, but on any given day over Greece the rate in the
    lowest 3 km runs from roughly 4 (near-isothermal) to 9.8 °C/km (dry adiabatic).
    Taking it from the profile makes the elevation correction follow the air mass
    that is actually overhead instead of a year-round constant.

    Clamped to that physical range so a noisy fit cannot produce an absurd offset,
    and it falls back to the standard rate whenever the profile is unusable.
    """
    try:
        z = np.asarray(prof["gh"].values, dtype=float)
        t = np.asarray(prof["t"].values, dtype=float) - 273.15
        order = np.argsort(z)
        z, t = z[order], t[order]
        # Only the layer the correction applies to, so upper-air details do not skew it.
        keep = (z >= z[0]) & (z <= z[0] + 3000)
        if int(keep.sum()) >= 3:
            rate = -float(np.polyfit(z[keep], t[keep], 1)[0])
            if LAPSE_MIN_C_PER_M <= rate <= LAPSE_MAX_C_PER_M:
                return rate, "derived"
    except Exception:
        pass
    return LAPSE_RATE_C_PER_M, "standard"


def severe_text(d: dict) -> list[str]:
    """Strict interpretation. Rotation wording requires all three thresholds, per spec."""
    out = []
    cape = d.get("sbcape_j_kg") or 0
    ml = d.get("mlcape_j_kg") or 0
    mu = d.get("mucape_j_kg") or 0
    shear06 = d.get("shear_0_6km_kt") or 0
    shear01 = d.get("shear_0_1km_kt") or 0
    cin = abs(d.get("sbcin_j_kg") or 0)
    srh = d.get("srh_0_3km_m2s2")
    best = max(cape, ml, mu)

    if best < 100:
        out.append("Πολύ χαμηλή αστάθεια — δεν αναμένεται συναγωγή.")
    elif best < 500:
        out.append(f"Οριακή αστάθεια (CAPE {best:.0f} J/kg) — μεμονωμένες μπόρες αν σπάσει τοπικά το CIN.")
    elif best < 1500:
        out.append(f"Μέτρια αστάθεια (CAPE {best:.0f} J/kg) — πιθανές καταιγίδες, συνήθως παροδικές.")
    elif best < 3000:
        out.append(f"Υψηλή αστάθεια (CAPE {best:.0f} J/kg) — ισχυρές καταιγίδες πιθανές.")
    else:
        out.append(f"Πολύ υψηλή αστάθεια (CAPE {best:.0f} J/kg) — δυνατότητα βίαιων καταιγίδων.")

    if cin > 150:
        out.append(f"CIN {cin:.0f} J/kg — «καπάκι» που συγκρατεί την ανάπτυξη· αν σπάσει απότομα, "
                   "οι καταιγίδες μπορεί να εκδηλωθούν εκρηκτικά.")
    elif cin > 25:
        out.append(f"Μέτριο CIN ({cin:.0f} J/kg) — χρειάζεται θέρμανση ή σύγκλιση για έναρξη συναγωγής.")

    if shear06 >= 40:
        out.append(f"Ισχυρό shear 0–6 km ({shear06:.0f} kt) — ευνοείται οργάνωση σε γραμμές λαίλαπας.")
    elif shear06 >= 25:
        out.append(f"Μέτριο shear 0–6 km ({shear06:.0f} kt) — οι καταιγίδες μπορούν να οργανωθούν.")
    elif best >= 500:
        out.append(f"Χαμηλό shear 0–6 km ({shear06:.0f} kt) — οι καταιγίδες θα είναι απομονωμένες.")

    if shear01:
        out.append(f"Shear 0–1 km: {shear01:.1f} kt"
                   + (" — σημαντικό για περιστροφή σε χαμηλά επίπεδα." if shear01 >= 20 else "."))

    # Supercell / rotation wording: ALL THREE conditions required.
    if srh is not None and srh >= SEVERE_SRH_MIN and shear06 >= SEVERE_SHEAR_MIN and best > SEVERE_CAPE_MIN:
        out.append(f"⚠ Συνθήκες περιστρεφόμενων καταιγίδων: SRH 0–3 km {srh:.0f} m²/s², "
                   f"shear 0–6 km {shear06:.0f} kt, CAPE {best:.0f} J/kg — και οι τρεις δείκτες "
                   "ξεπερνούν τα κατώφλια. Να παρακολουθείς ραντάρ και επόμενες ενημερώσεις.")
    elif srh is not None and srh >= 50:
        out.append(f"SRH 0–3 km {srh:.0f} m²/s² — μέτρια στροβιλότητα, αλλά δεν πληρούνται "
                   "όλα τα κριτήρια για προειδοποίηση περιστροφής.")
    return out


# ---------------------------------------------------------------- GFS-derived indices

def expert_indices(ds: xr.Dataset, lat: float, lon: float) -> dict:
    import metpy.calc as mpcalc
    from metpy.units import units

    pt = ds.sel(latitude=lat, longitude=lon, method="nearest")
    p, T, rh = pt.isobaricInhPa.values, pt["t"].values, pt["r"].values
    u, v = pt["u"].values, pt["v"].values
    z = pt["gh"].values  # GFS HGT on pressure levels is geopotential height in metres

    T_k = T * units.kelvin
    Td = mpcalc.dewpoint_from_relative_humidity(T_k, np.clip(rh / 100, 0.001, 1))

    levels = [{"p": int(p[i]), "z": round(float(z[i])), "t": round(float(T[i]) - 273.15, 1),
               "td": round(float(Td[i].to("degC").m), 1), "rh": round(float(rh[i])),
               "wind": round(float(np.hypot(u[i], v[i])) * 1.94384, 1)}
              for i in range(len(p))]

    out: dict = {"levels": levels, "gridpoint": [float(pt.latitude), float(pt.longitude)]}
    try:
        p_u, u_u, v_u = p * units.hPa, u * units("m/s"), v * units("m/s")
        hgt = z * units.meter
        sbcape, sbcin = mpcalc.surface_based_cape_cin(p_u, T_k, Td)
        mlcape, mlcin = mpcalc.mixed_layer_cape_cin(p_u, T_k, Td)
        mucape, _ = mpcalc.most_unstable_cape_cin(p_u, T_k, Td)
        lcl_p, lcl_t = mpcalc.lcl(p_u[0], T_k[0], Td[0])

        def shear(depth_m: float) -> float:
            if float(z[-1] - z[0]) < depth_m:
                return float("nan")
            s = mpcalc.bulk_shear(p_u, u_u, v_u, height=hgt, depth=depth_m * units.m)
            return float(np.hypot(s[0].m, s[1].m) * 1.94384)

        shear06 = shear(6000)
        shear01 = shear(1000)

        # LCL height in metres above ground. pressure_to_height_std returns a
        # scalar Quantity, not an array, so indexing [0] would raise.
        try:
            lcl_z = float(mpcalc.pressure_to_height_std(lcl_p).to("m").m)
            z0 = float(z[0])
            lcl_agl = round(lcl_z - z0)
        except Exception:
            lcl_agl = None

        tc = T - 273.15
        fz = None
        for i in range(len(tc) - 1):
            if tc[i] >= 0 > tc[i + 1]:
                frac = tc[i] / (tc[i] - tc[i + 1])
                fz = float(z[i] + frac * (z[i + 1] - z[i]))
                break

        srh = None
        try:
            srh = float(mpcalc.storm_relative_helicity(
                hgt, u_u, v_u, depth=3000 * units.m, bottom=hgt[0])[0].m)
        except Exception:
            pass

        out["derived"] = {
            "sbcape_j_kg": round(float(sbcape.m)), "sbcin_j_kg": round(float(sbcin.m)),
            "mlcape_j_kg": round(float(mlcape.m)), "mlcin_j_kg": round(float(mlcin.m)),
            "mucape_j_kg": round(float(mucape.m)),
            "lcl_hpa": round(float(lcl_p.m)), "lcl_m_agl": lcl_agl,
            "shear_0_1km_kt": None if np.isnan(shear01) else round(shear01, 1),
            "shear_0_6km_kt": None if np.isnan(shear06) else round(shear06, 1),
            "srh_0_3km_m2s2": round(srh) if srh is not None else None,
            "freezing_level_m": round(fz) if fz else None,
        }
        out["interpretation"] = severe_text(out["derived"])
    except Exception as e:
        out["derived"] = {"error": f"{type(e).__name__}: {str(e)[:150]}"}
        out["interpretation"] = []
    return out


# ---------------------------------------------------------------- charts

# The glass card composited over the page wash: rgba(18,24,38,.70) over the
# brightest point of body::before (#070b14 plus the blue radial). Kept as a named
# value so the chart and the CSS cannot drift apart.
SKEWT_PANEL_HEX = "#131e30"
SKEWT_PANEL_RGB = (0x13, 0x1e, 0x30)


def skewt_png(ds: xr.Dataset, lat: float, lon: float, step: int) -> bytes:
    from metpy.plots import SkewT
    from metpy.units import units
    import metpy.calc as mpcalc

    pt = ds.sel(latitude=lat, longitude=lon, method="nearest")
    p, T, rh = pt.isobaricInhPa.values, pt["t"].values, pt["r"].values
    u, v = pt["u"].values, pt["v"].values
    Td = mpcalc.dewpoint_from_relative_humidity(T * units.kelvin, np.clip(rh / 100, 0.001, 1))
    p_u = p * units.hPa

    # The chart sits inside a dark glass card. Left at matplotlib's default white
    # it reads as a pasted-in image rather than part of the page, so the axes are
    # themed to match: a slate panel, light text, muted gridlines. The panel is the
    # glass card composited over the page wash (rgba(18,24,38,.70) over #070b14),
    # so the PNG blends into the card instead of sitting on it as a lighter block.
    panel, ink, grid = SKEWT_PANEL_HEX, "#e8eef5", "#5c6b7d"
    fig = plt.figure(figsize=(6.6, 6.6), dpi=115)
    fig.patch.set_facecolor(panel)
    skew = SkewT(fig, rotation=42)
    skew.ax.set_facecolor(panel)
    skew.plot(p_u, (T * units.kelvin).to("degC"), "#ff5f56", linewidth=2.4, label="Θερμοκρασία (T)")
    skew.plot(p_u, Td.to("degC"), "#4ade80", linewidth=2.4, label="Σημείο δρόσου (Td)")
    skew.plot_dry_adiabats(alpha=0.22, linewidth=0.6, color="#7d8b9c")
    skew.plot_moist_adiabats(alpha=0.22, linewidth=0.6, color="#7d8b9c")
    skew.plot_mixing_lines(alpha=0.22, linewidth=0.6, color="#7d8b9c")
    skew.plot_barbs(p_u[::2], u[::2] * units("m/s"), v[::2] * units("m/s"), length=5.5)
    for lvl in (850, 700, 500, 300):
        skew.ax.axhline(lvl, color=grid, alpha=0.5, linewidth=0.5)
        skew.ax.text(-34, lvl + 6, f"{lvl}", fontsize=6.5, color="#9fb0c2")
    skew.ax.set_xlim(-35, 40)
    skew.ax.set_ylim(1000, 100)
    skew.ax.tick_params(colors=ink, labelsize=7.5)
    for spine in skew.ax.spines.values():
        spine.set_color(grid)
    skew.ax.xaxis.label.set_color(ink)
    skew.ax.yaxis.label.set_color(ink)
    leg = skew.ax.legend(loc="upper right", fontsize=8.5, framealpha=0.92,
                         facecolor=panel, edgecolor=grid)
    for txt in leg.get_texts():
        txt.set_color(ink)
    skew.ax.set_title(f"Ραδιοβόλιση GFS 0.25°  ·  {lat:.3f}, {lon:.3f}  ·  ώρα f{step:03d}\n"
                      "προσαρμοσμένο από NOAA GFS (public domain)",
                      fontsize=8.5, color=ink)
    buf = io.BytesIO()
    # bbox_inches="tight" crops to the artists; without a matching facecolor the
    # margin it leaves behind is still the figure default, i.e. a white border.
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=panel)
    plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------- identity & entitlement
#
# Three things live here, and they answer one question: what is this caller
# allowed to see? The answer is computed on the server from three independent
# sources — the signed token, the live Stripe subscription behind it, and any
# promo window the device holds — and the frontend never decides it.

DEVICE_COOKIE = "wx_dev"
DEVICE_MAX_AGE_S = 60 * 60 * 24 * 365 * 2


def device_id(request: Request) -> str | None:
    """The caller's opaque device id, if the token carries one.

    Read from the signature-verified payload, never from a header a client could
    set: a spoofable device id would make a personal promo code redeemable by
    anyone who guessed the id. A caller with no token has no identity yet, which
    is fine — redeeming a code mints one (see `with_device`).
    """
    token = request.headers.get("X-WX-Token") or request.query_params.get("token")
    return ent.verify_token(token).device


def with_device(response: Response, device: str | None) -> Response:
    """Attach the device id to a response that minted one."""
    if device:
        response.set_cookie(DEVICE_COOKIE, device, max_age=DEVICE_MAX_AGE_S,
                            httponly=True, samesite="lax",
                            secure=config.is_production())
    return response


def bearer_token(request: Request) -> str | None:
    return request.headers.get("X-WX-Token") or request.query_params.get("token")


def effective_entitlement(request: Request, check_subscription: bool = True) -> ent.Entitlement:
    """The entitlement actually in force, from every source that grants PRO.

    Composition, in one place so the two PRO endpoints cannot drift apart:

    1. Verify the signed token. A forged or expired token yields `free` here; no
       other branch can resurrect it.
    2. If it names a subscription, ask Stripe (through a short cache) whether that
       subscription is still live. This is what makes a cancelled or unpaid
       subscription stop granting access before the 30-day token expires — the
       hole that let an old HMAC token keep working after cancellation.
    3. If the caller holds a promo window for their device id, extend PRO to the
       later of the two ends.

    Failure policy: a Stripe lookup that cannot complete does **not** grant access
    on its own, and does **not** revoke it either. It falls back to the token's own
    expiry, which is bounded at 30 days and was issued only after a verified
    payment. Fail-closed here would mean a five-minute Stripe outage logs every
    paying customer out; fail-open on *expiry* would never happen, because the
    token still expires.
    """
    e = ent.verify_token(bearer_token(request))
    if not e.is_pro:
        return e

    # What the token itself guarantees, before the subscription is consulted.
    #
    # For a passcode or a trial the token *is* the grant, so its expiry is the whole
    # window. For a promo token it is not: the token carries a 30-day lifetime so it
    # can be presented on later requests, while the window it unlocks is the
    # redemption's `pro_until` in the database. Treating the token lifetime as the
    # grant is how a 5-day code would have quietly granted 30 days.
    #
    # A subscription token is worse than either: its 30-day lifetime must never
    # outlive the subscription, or cancelling would leave PRO running. So for a
    # subscription token nothing is taken from the token itself — only from Stripe.
    promo_source = e.source == "promo"
    ends: list[int] = []
    if not promo_source and not e.subscription_id:
        if e.pro_until:
            ends.append(int(e.pro_until))
        if e.expires_at:
            ends.append(int(e.expires_at))
    notes: list[str] = []

    if e.subscription_id and check_subscription:
        access = bill.subscription_access(e.subscription_id)
        status = access.get("status")
        if access.get("status") == "unknown":
            # Stripe was unreachable. Fall back to the verified token's own expiry,
            # which is bounded at 30 days, and label the response so an operator can
            # see that the check did not happen.
            notes.append("subscription_unverified")
            if e.expires_at:
                ends.append(int(e.expires_at))
            log.warning("subscription lookup failed for %s...: %s",
                        str(e.subscription_id)[:12], access.get("error"))
        elif access.get("active"):
            if access.get("until"):
                ends.append(int(access["until"]))
            notes.append(f"subscription:{status}")
        else:
            # Stripe says this subscription grants nothing. Nothing is added. A promo
            # may still be in force for the same device, so that is checked below
            # rather than returning FREE immediately.
            log.info("subscription %s... no longer grants PRO (%s)",
                     str(e.subscription_id)[:12], access.get("reason"))
            notes.append("subscription_inactive")

    promo_until = None
    try:
        promo_until = promo.active_until(e.device)
    except Exception as ex:
        log.warning("promo lookup failed: %s", ex)
    if promo_until:
        ends.append(int(promo_until))
        notes.append("promo")

    if not ends:
        # A promo token whose window has lapsed, or one with no device and no
        # record behind it. Either way there is nothing keeping PRO alive.
        return ent.Entitlement("free", ent.FREE_HOURS, None, "free",
                               device=e.device,
                               notes=tuple(notes) + ("no_active_grant",))

    pro_until = max(ends)
    if pro_until <= int(time.time()):
        return ent.Entitlement("free", ent.FREE_HOURS, None, "free",
                               device=e.device, notes=tuple(notes) + ("expired",))

    source = e.source
    if promo_until and promo_until >= pro_until and not promo_source:
        # The promo is what is keeping PRO alive for a token that would otherwise
        # have lapsed; say so, so the UI can name the right end date.
        source = "promo"

    return ent.Entitlement("pro", ent.PRO_HOURS, e.expires_at, source,
                           e.subscription_id, e.device,
                           pro_until=pro_until, notes=tuple(notes))


def pro_hours_for(e: ent.Entitlement) -> int:
    """Forecast window this entitlement unlocks. FREE=72, PRO=240, unchanged."""
    return ent.PRO_HOURS if e.is_pro else ent.FREE_HOURS


def require_pro(request: Request) -> ent.Entitlement:
    """Raise 403 unless the caller has an active PRO entitlement."""
    e = effective_entitlement(request)
    if not e.is_pro:
        raise HTTPException(403, "Η λειτουργία είναι διαθέσιμη στη συνδρομή PRO.")
    return e


def entitlement_payload(e: ent.Entitlement) -> dict:
    """The `tier` block the UI reads, with the effective window made explicit."""
    return {
        "tier": e.tier, "is_pro": e.is_pro, "source": e.source,
        "hours": pro_hours_for(e),
        "free_hours": ent.FREE_HOURS, "pro_hours": ent.PRO_HOURS,
        "expires_at": e.expires_at,
        "pro_until": e.pro_until,
        "pro_until_iso": (dt.datetime.fromtimestamp(e.pro_until, dt.timezone.utc)
                          .strftime("%Y-%m-%d") if e.pro_until else None),
        "notes": list(e.notes),
        "manageable": bool(e.subscription_id),
        "locked_hours": 0 if e.is_pro else ent.PRO_HOURS - pro_hours_for(e),
    }


# ---------------------------------------------------------------- single-flight
#
# Two users (or the same user's double click) asking for the same cold forecast
# at the same moment used to run the whole GFS fetch twice: five parallel
# requests to one new point took 13/27/39/51/64 s, serialized, because each did
# its own ~160 NOMADS calls. This collapses them onto one.

_BRIEF_INFLIGHT: dict[str, asyncio.Future] = {}
_BRIEF_LOCK = asyncio.Lock()


async def _single_flight(key: str, factory):
    """Run `factory()` once per `key`; concurrent callers await the same result.

    The result is *not* cached here — the underlying GRIB step cache already does
    that, and caching the fully-rendered payload would serve stale numbers across
    a model run boundary. This only deduplicates work that is in flight.
    """
    async with _BRIEF_LOCK:
        fut = _BRIEF_INFLIGHT.get(key)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            _BRIEF_INFLIGHT[key] = fut
            leader = True
        else:
            leader = False
    if not leader:
        return await asyncio.shield(fut)
    try:
        result = await factory()
        if not fut.done():
            fut.set_result(result)
        return result
    except BaseException as e:
        if not fut.done():
            fut.set_exception(e)
        raise
    finally:
        async with _BRIEF_LOCK:
            _BRIEF_INFLIGHT.pop(key, None)


def _collect_coord(lat: float, lon: float) -> None:
    """422 on a coordinate no model can answer for.

    FastAPI's `Query(ge=,le=)` already rejects most of this, but the bounds are
    restated here for calls that reach the handler with a NaN smuggled through a
    float coercion, and so the error text is the same on every endpoint.
    """
    reason = config.coord_error(lat, lon)
    if reason:
        raise HTTPException(422, reason)


# ---------------------------------------------------------------- routes

def redact_url(url: str) -> str:
    """Strip credential-looking query parameters from a URL.

    Used for the external APIs whose URLs (and any key in them) are reported by
    ``/api/health``. Health is the endpoint an operator curls during a deploy, and
    its output ends up in logs and tickets, so a key must not be echoed back.
    """
    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    keep = []
    for part in query.split("&"):
        name = part.split("=", 1)[0].lower()
        if any(t in name for t in ("key", "token", "secret", "pass")):
            keep.append(f"{part.split('=', 1)[0]}=<redacted>")
        else:
            keep.append(part)
    return base + "?" + "&".join(keep)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    # The footer's hrefs are HTML, so they use html.escape rather than raw
    # interpolation: a contact value containing a quote must not close the attribute.
    c = legal.contacts()
    return (PAGE
            .replace("__YOUTUBE__", html.escape(c["youtube"], quote=True))
            .replace("__FACEBOOK__", html.escape(c["facebook"], quote=True))
            .replace("__EMAIL__", html.escape(c["email"], quote=True)))


@app.get("/api/health")
async def health():
    """Liveness plus the current model run, so a deploy can be verified quickly."""
    try:
        date, hh = wx.latest_gfs_run()
        gfs = {"run": [date, hh], "ok": True}
    except Exception as e:
        gfs = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    return {
        "ok": gfs["ok"],
        "gfs_run": gfs.get("run"),
        "gfs_error": gfs.get("error"),
        # Optional extras whose absence degrades a card instead of the forecast.
        # Reported here so a deploy check catches a half-installed environment;
        # otherwise a missing package only shows up as a broken card in the UI.
        # `install_hint` names the interpreter that failed the import, which is
        # what distinguishes a missing wheel from a wheel in the wrong Python.
        # `astro_missing` says whether installing would actually help: a wheel
        # that is present but broken (or whose dependency is absent) is not fixed
        # by re-running pip, so offering the command would mislead.
        "optional": {"astro_ephem": astro._HAVE_EPHEM,
                     "interpreter": astro.interpreter(),
                     "python_version": astro._py_version(),
                     **({} if astro._HAVE_EPHEM else
                        {"astro_missing": astro.is_missing(),
                         **({"astro_fix": astro.install_hint()}
                            if astro.is_missing() else {}),
                         **({"astro_import_error": astro.import_error()}
                            if astro.import_error() else {})})},
        # RAM grids: `enabled` is the feature flag, `models` carries each grid's
        # run id and age. A model absent from `models` never loaded, and `stale`
        # means the last refresh failed and a previous run is being served - the
        # two states an operator needs to tell apart when a card looks wrong.
        "ram_grids": {"enabled": grids.flag_enabled(),
                      "icon_scope": "europe" if grids.icon_bbox() is grids.EUROPE_BBOX
                                    else "greek",
                      "models": grids.STORE.health()},
        # Payments: `checkout_available` must be true before the UI may offer a
        # payment button. `missing_config` names what is absent, without values.
        # `webhook_configured` is reported separately from checkout availability
        # because they fail independently: checkout works with no webhook secret,
        # and a deploy that only sets the price ids would activate nothing.
        "billing": {"checkout_available": bill.checkout_available(),
                    "missing_config": bill.missing_config(),
                    "webhook_configured": bool(bill.webhook_secret()),
                    "auto_renew_default": bill.AUTO_RENEW_BY_DEFAULT},
        # Cache: size against the configured cap, and a count of damaged entries
        # rejected on read since boot. `damaged > 0` is not an error — it means a
        # truncated write was detected and regenerated rather than served.
        "cache": _cache_health(),
        # Promo codes: the tables are created lazily, so a failure here is a
        # broken database rather than a missing feature.
        "promo": promo.stats(),
        # Entitlement signing: whether a real WX_SECRET is in force. Reported as a
        # boolean only, never the value.
        "auth": {"secret_configured": bool(os.environ.get("WX_SECRET")),
                 "rate_limit_enabled": config.rate_limit_enabled(),
                 "trust_proxy": config.trust_proxy_headers()},
        # Analytics: on/off and the retention window. Reported so an operator can
        # confirm at a glance that raw events are bounded, not accumulating.
        "analytics": {"enabled": analytics.enabled(),
                      "retention_days": analytics.retention_days()},
        # Cameras: counts only, never a source URL or credential. `sources` tells
        # an operator whether the private store parsed; a malformed blob shows
        # here as 0 without printing what it contained.
        "cameras": cams.health(),
        # Snapshot runtime: counts of resolved sources by kind, and how many are
        # still the offline mock. Never a URL, host or credential.
        "snapshots": snapshots.health(),
        "data_sources": ["GFS (public domain)", "ICON-EU DWD (CC BY 4.0)",
                         "ECMWF open data (CC BY 4.0, best-effort)",
                         "Photon geocoding (OSM)", "OpenTopoData DEM"],
        "non_commercial_sources_used": False,
        # No basemap tiles are fetched: the UI has no map, so there is no raster
        # tile provider to license. The geocoder and DEM are still third-party
        # free services and remain the open commercial item.
        "base_map": None,
        # Notifications: `configured` is whether VAPID keys and the push package
        # are present. `subscribers` counts stored subscriptions, so an operator
        # can tell "no subscribers yet" from "the loop is not running".
        "push": {"configured": notify.push_available(),
                 "reason": notify.unavailable_reason(),
                 "interval_s": notify.interval_s(),
                 "subscribers": notify.stats()},
    }


def _cache_health() -> dict:
    try:
        st = wx.cache_stats()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:100]}"}
    limit = st.get("limit_bytes")
    over = bool(limit and st["bytes"] > limit)
    return {"ok": not over, "bytes": st["bytes"], "limit_bytes": limit,
            "files": st["files"], "oldest_age_s": st["oldest_age_s"],
            "near_limit": bool(limit and st["bytes"] > 0.9 * limit)}


@app.get("/api/elevation")
async def elevation(lat: float, lon: float):
    """Point elevation (DEM) plus the model cell's mean elevation.

    The two differ, and the difference is exactly what the temperature correction
    is for. Returning both means the user can see how much the grid cell is
    misrepresenting their location.
    """
    _collect_coord(lat, lon)
    date, hh = wx.latest_gfs_run()
    async with httpx.AsyncClient(headers=wx.UA, timeout=40) as c:
        dem, orog = await asyncio.gather(
            wx.dem_elevation(c, lat, lon),
            wx.gfs_orography(c, lat, lon, date, hh),
            return_exceptions=True)
    dem = dem if isinstance(dem, dict) else {"elevation_m": None}
    orog = None if isinstance(orog, Exception) else orog

    agl = None
    if dem.get("elevation_m") is not None and orog is not None:
        agl = round(dem["elevation_m"] - orog)

    return {"lat": lat, "lon": lon,
            "point_elevation_m": dem.get("elevation_m"),
            "model_elevation_m": None if orog is None else round(orog),
            "model_agl_m": agl,
            "dataset": dem.get("dataset"), "dataset_note": dem.get("dataset_note"),
            "source": dem.get("source"), "model_source": "NOAA GFS surface geopotential",
            "error": dem.get("error")}


@app.get("/terms", response_class=HTMLResponse)
async def terms_page() -> str:
    """Όροι Χρήσης. Public, no auth: a buyer must be able to read these first."""
    return legal.terms_page()


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page() -> str:
    """Πολιτική Απορρήτου. Public, no auth."""
    return legal.privacy_page()


@app.get("/refunds", response_class=HTMLResponse)
async def refunds_page() -> str:
    """Πολιτική Επιστροφών. Public, no auth: Stripe expects it reachable."""
    return legal.refunds_page()


@app.get("/licenses", response_class=HTMLResponse)
async def licenses_page() -> str:
    """Serve the licence audit so attribution is always reachable from the footer."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "LICENSES.md")
    if not os.path.exists(p):
        raise HTTPException(404, "LICENSES.md not found")
    text = open(p, encoding="utf-8").read()
    esc = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return (f"<!doctype html><html lang='el'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Άδειες χρήσης</title><style>"
            f":root{{color-scheme:dark}}"
            f"body{{font-family:system-ui,sans-serif;max-width:860px;margin:0 auto;padding:24px;"
            f"background:#070b14;color:#eef2f8}}"
            f"pre{{white-space:pre-wrap;background:rgba(18,24,38,.70);"
            f"border:1px solid rgba(255,255,255,.10);border-radius:10px;"
            f"padding:20px;font-size:13px;line-height:1.6;font-family:ui-monospace,monospace;"
            f"backdrop-filter:blur(14px) saturate(160%);"
            f"-webkit-backdrop-filter:blur(14px) saturate(160%);"
            f"box-shadow:0 8px 32px 0 rgba(0,0,0,.37);color:#c7d2e2}}"
            f"a{{color:#4da3ff}}</style></head><body>"
            f"<p><a href='/'>&larr; Πίσω στην πρόγνωση</a></p><pre>{esc}</pre></body></html>")


@app.get("/sw.js")
async def service_worker() -> Response:
    """The push service worker, at the root so its scope covers the whole site.

    Served from a route rather than `/static/` because a worker's scope is capped
    at its own directory: `/static/sw.js` would only control `/static/*`. The
    `Service-Worker-Allowed` header is belt-and-braces for proxies that rewrite
    the path. `no-cache` so a worker update is picked up on the next load.
    """
    p = os.path.join(STATIC_DIR, "sw.js")
    if not os.path.exists(p):
        raise HTTPException(404, "sw.js not vendored")
    return Response(open(p, "rb").read(), media_type="application/javascript",
                    headers={"Cache-Control": "no-cache",
                             "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
async def manifest() -> Response:
    """The web app manifest. Required for iOS web push to be offered at all."""
    p = os.path.join(STATIC_DIR, "manifest.webmanifest")
    if not os.path.exists(p):
        raise HTTPException(404, "manifest not vendored")
    return Response(open(p, "rb").read(), media_type="application/manifest+json")


@app.get("/static/{name}")
async def static_file(name: str) -> Response:
    """Serve the vendored front-end assets.

    Only an allow-list, by basename: a path parameter that reaches the filesystem
    needs this so that `../` cannot walk out of static/.
    """
    allowed = {"chart.umd.min.js": "application/javascript",
               "sw.js": "application/javascript",
               "manifest.webmanifest": "application/manifest+json",
               "icon-192.png": "image/png",
               "icon-512.png": "image/png",
               "icon-maskable-512.png": "image/png",
               "apple-touch-icon.png": "image/png"}
    media = allowed.get(name)
    if media is None:
        raise HTTPException(404, "not found")
    p = os.path.join(STATIC_DIR, name)
    if not os.path.exists(p):
        raise HTTPException(404, f"{name} not vendored")
    return Response(open(p, "rb").read(), media_type=media)


@app.get("/api/resolve")
async def resolve(q: str = Query(min_length=2, max_length=80),
                  lat: float | None = None, lon: float | None = None):
    async with httpx.AsyncClient(headers=wx.UA, timeout=25) as c:
        return await wx.geocode(c, q, lat, lon)


@app.get("/api/reverse")
async def reverse(lat: float, lon: float):
    _collect_coord(lat, lon)
    async with httpx.AsyncClient(headers=wx.UA, timeout=25) as c:
        return await wx.reverse_geocode(c, lat, lon)


async def _resolved(value):
    """Wrap an already-computed value so it can be gathered alongside coroutines.

    `asyncio.gather(*, return_exceptions=True)` is used at the call site, so the
    RAM branch has to return something awaitable to keep the shape identical.
    """
    return value


def _icon_from_grid(grid: grids.GridSpec, lat: float, lon: float,
                    steps: list[int]) -> dict:
    """ICON-EU comparison fields from RAM, keyed exactly like the per-point path.

    The keys (`t2m_24`, `precip_24`, `cape_ml_0`, ...) are what the model_grid and
    agreement code downstream reads, so they must match byte for byte.

    Units must match too, and temperature does not: the grid stores `t2m_c` in
    Celsius, while the per-point path returns raw Kelvin from GRIB and every
    consumer subtracts 273.15 itself. Returning Celsius here would render the
    ICON row as about -256 C while GFS sits next to it in Celsius - a wrong
    number shown as a real one, which is exactly what the comparison grid is
    meant to expose. So the conversion happens here, once, at the boundary.
    """
    out: dict = {}
    for var, key in (("t2m_c", "t2m"), ("precip_mm", "precip"), ("cape", "cape_ml")):
        for s in steps:
            v = grids.bilinear(grid, var, s, lat, lon)
            if v is not None:
                out[f"{key}_{s}"] = v + 273.15 if key == "t2m" else v
    return out


@app.get("/api/brief")
async def brief(request: Request, lat: float, lon: float, station: str | None = None,
                hours: int | None = None, elevation_m: float | None = None):
    """Simple + expert view in one response.

    Tier gating happens here, on the server: a free response contains only 72 hours
    and no expert payload. The client-side blur is presentation on top of data that
    is genuinely absent.

    Primary series is GFS (public domain, hourly, reliable). ICON-EU (7 km) is
    sampled at a few steps for the model-comparison grid, since each ICON-EU
    variable is a ~1 MB whole-of-Europe download. ECMWF is attempted but is
    allowed to fail.

    Validation order matters. The coordinate is checked *first*, before any
    entitlement work and long before any fetch: `lat=999` used to reach NOMADS,
    where an out-of-range sub-region is clamped to the whole planet, and a single
    such request left a 419 MB file in the cache.
    """
    _collect_coord(lat, lon)
    if elevation_m is not None and not (-50 <= elevation_m <= ELEVATION_LIMIT_M):
        raise HTTPException(422, f"elevation_m must be between -50 and {ELEVATION_LIMIT_M:.0f} m")
    if hours is not None and not (config.HOURS_MIN <= hours <= config.HOURS_MAX):
        raise HTTPException(422, f"hours must be between {config.HOURS_MIN} and {config.HOURS_MAX}")
    if station is not None and not (1 <= len(station) <= 64):
        raise HTTPException(422, "station must be 1-64 characters")

    entl = effective_entitlement(request)
    # PRO gets 10 days, free is capped at 72 h regardless of what was asked for.
    # `hours` was already bounded above by HOURS_MAX, and is bounded here by the
    # entitlement, so an oversized request can never exceed the caller's tier.
    allowed_hours = pro_hours_for(entl)
    hours = min(hours or allowed_hours, allowed_hours)

    # Identical concurrent requests share one run. The key deliberately excludes
    # the token: two free users asking for the same point want the same numbers,
    # and the tier is already folded into `hours`.
    flight_key = (f"brief|{lat:.3f},{lon:.3f}|{hours}|{elevation_m}|{station}")
    return await _single_flight(
        flight_key, lambda: _build_brief(request, lat, lon, station, hours, elevation_m, entl))


async def _build_brief(request: Request, lat: float, lon: float, station: str | None,
                       hours: int, elevation_m: float | None, entl: ent.Entitlement):
    """Everything `/api/brief` does after validation. Split out for single-flight."""
    icon_steps = [0, 6, 12, 18, 24, 48] if entl.is_pro else []

    # When the RAM path is enabled and a grid is loaded, the GFS series and the
    # model orography come from memory: no network, no GRIB decode inside the
    # request. `ram` is falsy if the flag is off, nothing has loaded yet, or the
    # point is outside the regional grid, and the per-point path runs unchanged in
    # that case. That last condition matters: the RAM grid covers Greece only,
    # while the per-point path subsets server-side and works continent-wide, so
    # without the coverage check a point in Berlin would get a 502 exactly when the
    # flag was switched on.
    ram = (grids.STORE.ensure_loaded("gfs", grids.gfs_scope())
           if grids.flag_enabled() else None)
    if ram is not None and not grids.covers(ram, lat, lon):
        ram = None
    ram_steps = ([s for s in wx.gfs_steps(hours) if ram.step_index(s) is not None]
                 if ram is not None else [])

    if ram is not None and ram_steps:
        # The run id comes from the grid, not from a probe: resolving it here
        # would reintroduce the very network call the RAM path exists to remove,
        # and could even name a newer cycle than the one actually in memory.
        run_utc = ram.run
        date, hh = run_utc[:8], run_utc[8:10]
    else:
        date, hh = wx.latest_gfs_run()
        run_utc = f"{date}{hh}"

    async with httpx.AsyncClient(headers=wx.UA, timeout=120) as c:
        if ram is not None and ram_steps:
            gfs_rows = grids.surface_rows(ram, lat, lon, ram_steps)
            rows_task = _resolved(gfs_rows)
            orog_val = grids.model_orography(ram, lat, lon)
            orog_task = _resolved(orog_val)
        else:
            rows_task = wx.gfs_surface_series(lat, lon, hours=hours)
            orog_task = wx.gfs_orography(c, lat, lon, date, hh)
        prof_task = wx.gfs_profile_dataset(c, lat, lon, step=12)

        async def icon_task():
            # The ICON grid is trimmed to the Greece box, so a point outside it
            # must use the per-point path rather than come back empty. The lazy
            # restore mirrors GFS: after a restart the persisted ICON run is used
            # instead of the per-point path until the first refresh completes.
            if grids.flag_enabled():
                icon_grid = grids.STORE.ensure_loaded("icon", grids.icon_scope())
                if icon_grid is not None and grids.covers(icon_grid, lat, lon):
                    return _icon_from_grid(icon_grid, lat, lon, icon_steps)
            out = {}
            for var in ("t2m", "precip", "cape_ml"):
                for s in icon_steps:
                    try:
                        out[f"{var}_{s}"] = await wx.icon_eu_point(c, lat, lon, var, s)
                    except Exception:
                        pass
            return out

        async def ecmwf_task():
            for st in (24, 48):
                try:
                    return await wx.ecmwf_point(c, lat, lon, step=st)
                except Exception:
                    continue
            return {}

        gfs_rows, prof, icon, ec, orog = await asyncio.gather(
            rows_task, prof_task, icon_task(), ecmwf_task(), orog_task,
            return_exceptions=True)

    if isinstance(gfs_rows, Exception) or not gfs_rows:
        return JSONResponse({"error": f"GFS unavailable: {gfs_rows}"}, status_code=502)

    model_elev = None if isinstance(orog, Exception) else orog

    # --- terrain correction: the model's 2 m temperature is valid at the cell's
    # mean height, not at the user's actual height. The rate is read from the
    # sounding when possible, else the ISA standard, and the offset is capped. ---
    lapse_rate, lapse_src = (
        derive_lapse_rate(prof) if not isinstance(prof, Exception)
        else (LAPSE_RATE_C_PER_M, "standard"))
    elev_info: dict = {"model_elevation_m": None if model_elev is None else round(model_elev),
                       "lapse_rate_c_per_km": round(lapse_rate * 1000, 2),
                       "lapse_rate_source": lapse_src}
    if model_elev is not None and elevation_m is not None:
        agl = elevation_m - model_elev
        applied_c = -lapse_rate * agl  # warmer lower down, colder higher up
        applied_c = max(-MAX_ELEV_CORRECTION_C, min(MAX_ELEV_CORRECTION_C, applied_c))
        elev_info |= {"applied_m": round(agl), "applied_c": round(applied_c, 2),
                      "model_agl_m": round(agl), "point_elevation_m": round(elevation_m, 1)}
        for r in gfs_rows:
            if r.get("t2m_c") is not None:
                r["t2m_c_cell"] = r["t2m_c"]
                r["t2m_c"] = r["t2m_c"] + applied_c
                r["elevation_c"] = round(applied_c, 2)
        rate_txt = (f"{lapse_rate * 1000:.1f}°C/km"
                    + (" από το προφίλ" if lapse_src == "derived" else " (τυπική βαθμίδα)"))
        elev_info["note"] = (f"Θερμοκρασία προσαρμοσμένη για διαφορά υψομέτρου {round(agl)} m "
                             f"με βαθμίδα {rate_txt} (όριο ±{MAX_ELEV_CORRECTION_C}°C).")
    elif elevation_m is not None and model_elev is None:
        elev_info["note"] = "Δεν ήταν δυνατή η ανάκτηση του υψομέτρου του κελιού, δεν εφαρμόστηκε διόρθωση."

    # --- bias correction from a local station, if one is configured ---
    bias_info = {"applied": False, "reason": "no station configured"}
    station_info = None
    if station:
        st = bias.get_station(station)
        if st:
            # Public payload field: strip the passkey (see public_station).
            station_info = bias.public_station(st)
            bias_info = bias.compute_bias(station, st["lat"] or lat, st["lon"] or lon, "gfs")
            gfs_rows = bias.apply_correction(gfs_rows, bias_info)
        else:
            bias_info = {"applied": False, "reason": f"station '{station}' not registered"}
    try:
        bias.record_forecasts(run_utc, lat, lon, "gfs", gfs_rows)
    except Exception:
        pass

    out: dict = {
        "meta": {"lat": lat, "lon": lon, "gfs_run": run_utc, "gfs_run_label": fmt_run(date, hh),
                 "hours": hours, "station": station_info, "bias": bias_info,
                 "model_elevation_m": elev_info.get("model_elevation_m"),
                 "elevation": elev_info,
                 "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")},
        "simple": simple_view(gfs_rows, bias_info, elevation_m, run_utc),
        "attribution": attribution_block(),
    }

    expert: dict = {}
    if entl.is_pro:
        if isinstance(prof, Exception):
            expert["error"] = f"GFS profile: {type(prof).__name__}: {str(prof)[:150]}"
        else:
            expert |= expert_indices(prof, lat, lon)
            expert["run"] = run_utc

        # model comparison grid
        grid = [{"model": "GFS (NOAA)", "res": "0.25° ~25 km", "license": "Public domain"}]
        if isinstance(icon, dict) and icon.get("t2m_0") is not None:
            grid.append({"model": "ICON-EU (DWD)", "res": "0.0625° ~7 km", "license": "CC BY 4.0",
                         "t_now": round(icon["t2m_0"] - 273.15, 1),
                         "t_24h": round(icon["t2m_24"] - 273.15, 1) if icon.get("t2m_24") is not None else None,
                         "precip_24h": round(icon["precip_24"], 1) if icon.get("precip_24") is not None else None,
                         "cape_now": round(icon["cape_ml_0"]) if icon.get("cape_ml_0") is not None else None})
        if isinstance(ec, dict) and ec.get("2t") is not None:
            grid.append({"model": "ECMWF IFS", "res": "0.25° ~25 km", "license": "CC BY 4.0",
                         "t_now": round(ec["2t"] - 273.15, 1),
                         "msl_hpa": round(ec["msl"] / 100, 1) if ec.get("msl") else None})
        expert["model_grid"] = grid
        expert["agreement"] = agreement(temperature_series_for_agreement(gfs_rows, icon, ec))
        if isinstance(gfs_rows[0], dict):
            expert["gfs_now"] = {k: (round(v, 2) if isinstance(v, float) else v)
                                 for k, v in gfs_rows[0].items()}
    else:
        # The free tier gets no expert data at all - not blurred data. The blur in
        # the UI sits on top of a placeholder, and the real values never leave here.
        expert = {"locked": True, "unlocked_hours": ent.PRO_HOURS,
                  "unlocks": ent.plan_payload()["unlocks"]}

    out["expert"] = expert
    tier_block = entitlement_payload(entl)
    tier_block["hours"] = hours
    tier_block["plans"] = ent.plan_payload()
    # Short-lived: the payload depends on the caller's entitlement and on the
    # model run, neither of which should be pinned for a browser cache.
    out["tier"] = tier_block
    return JSONResponse(out, headers={"Cache-Control": "private, max-age=300"})


def temperature_series_for_agreement(gfs_rows: list[dict], icon: dict, ec: dict) -> dict[str, list]:
    """GFS hourly, plus ICON-EU interpolated onto the same hourly index.

    ICON-EU is only sampled at a few steps here (each variable is a ~1 MB
    whole-of-Europe download), so its series is linearly interpolated. That is
    honest for a spread estimate as long as the sparse sampling is remembered -
    it understates true disagreement between sampling points.
    """
    series: dict[str, list] = {"GFS": [r.get("t2m_c") for r in gfs_rows]}
    n = len(gfs_rows)

    if icon:
        keymap = {s: icon[f"t2m_{s}"] - 273.15
                  for s in (0, 6, 12, 18, 24, 48) if icon.get(f"t2m_{s}") is not None}
        steps = sorted(keymap)
        if len(steps) >= 2:
            vals: list[float] = []
            for i in range(n):
                step = i + 1
                if step <= steps[0]:
                    vals.append(keymap[steps[0]])
                elif step >= steps[-1]:
                    vals.append(keymap[steps[-1]])
                else:
                    lo = max(s for s in steps if s <= step)
                    hi = min(s for s in steps if s >= step)
                    frac = 0.0 if hi == lo else (step - lo) / (hi - lo)
                    vals.append(keymap[lo] + frac * (keymap[hi] - keymap[lo]))
            series["ICON-EU"] = vals
    return series


def simple_view(rows: list[dict], bias_info: dict,
                elevation_m: float | None = None,
                run_utc: str | None = None) -> dict:
    """The everyday-user view: plain language, Beaufort, feels-like in front.

    Uses every hour the tier allows, so the meteogram covers the full window
    (72 h free, 240 h PRO) rather than silently truncating.
    """
    hours = []
    for r in rows:
        t = r.get("t2m_c")
        rh = r.get("rh2_pct")
        a = apparent_temp(t, rh, r.get("wind_kmh")) if t is not None else None
        hours.append({
            "step_h": r["step"],
            "t": None if t is None else round(t, 1),
            "feels": None if a is None else round(a, 1),
            "rh": None if rh is None else round(rh),
            "cloud_pct": None if r.get("cloud_pct") is None else round(r["cloud_pct"]),
            "cloud_base": cloud_base_m(t, rh, elevation_m),
            "precip": round(r.get("precip_mm") or 0.0, 1),
            "wind": None if r.get("wind_kmh") is None else round(r["wind_kmh"]),
            "wind_dir": r.get("wind_dir"),
            "gust": None if r.get("gust_kmh") is None else round(r["gust_kmh"]),
            "bft": beaufort(r.get("wind_kmh")),
            "bias_c": r.get("bias_c"),
        })

    first24 = hours[:24]
    temps = [h["t"] for h in first24 if h["t"] is not None]
    precs = [h["precip"] for h in first24]
    winds = [h["wind"] for h in first24 if h["wind"] is not None]
    gusts = [h["gust"] for h in first24 if h["gust"] is not None]
    total_p = float(sum(precs))
    max_w = max(winds) if winds else None
    max_g = max(gusts) if gusts else None
    wdir_max = next((h["wind_dir"] for h in first24 if h["wind"] == max_w), None) if winds else None

    summary: list[str] = []
    if total_p < 0.2:
        summary.append("Δεν αναμένεται βροχή τις επόμενες 24 ώρες.")
    elif total_p < 2:
        summary.append(f"Μικρή πιθανότητα ασθενούς βροχής (~{total_p:.1f} mm συνολικά).")
    elif total_p < 10:
        summary.append(f"Αναμένεται βροχή ~{total_p:.0f} mm. Χρήσιμη ομπρέλα.")
    else:
        summary.append(f"Ισχυρές βροχές, σύνολο ~{total_p:.0f} mm. Πιθανές τοπικές πλημμύρες.")

    if temps:
        feels = [h["feels"] for h in first24 if h["feels"] is not None]
        extra = f", με αίσθηση έως {min(feels):.0f}°" if feels else ""
        summary.append(f"Θερμοκρασία {min(temps):.0f}°–{max(temps):.0f}°{extra}.")
    if max_w is not None:
        summary.append(wind_phrase(max_w, wdir_max) +
                       (f", με ριπές έως {max_g:.0f} km/h ({beaufort(max_g)} Bft)." if max_g else "."))
        if max_g and max_g >= 50:
            summary.append("Οι ριπές ξεπερνούν τα 50 km/h — προσοχή σε ελαφριές κατασκευές και μικρά σκάφη.")

    if bias_info.get("applied"):
        summary.append(f"Οι πρώτες {bias.CORRECTION_HOURS} ώρες είναι διορθωμένες με τον τοπικό σταθμό "
                       f"({bias_info['offset_c']:+.1f}°C από {bias_info['pairs']} μετρήσεις).")

    # ---------------------------------------------------------------- hero
    # One plain sentence and one icon for the first screen. Everything below it
    # in the page is optional detail, so this has to stand alone.
    now = hours[0] if hours else {}
    bft_now = now.get("bft")
    sky = sky_condition(now.get("cloud_pct"), now.get("precip"), bft_now,
                        is_day=_is_daytime(now))

    if total_p < 0.2:
        rain_clause = "Δεν αναμένεται βροχή"
    elif total_p < 2:
        rain_clause = "Πιθανές ψιχάλες"
    elif total_p < 10:
        rain_clause = f"Βροχή ~{total_p:.0f} mm"
    else:
        rain_clause = f"Ισχυρές βροχές ~{total_p:.0f} mm"

    wind_clause = ""
    if bft_now is not None:
        wind_clause = f", {WIND_NAME.get(bft_now, 'άνεμος')} άνεμος {bft_now} Bft"
    hero = {
        "icon": sky["icon"],
        "condition": sky["text"],
        "sky_key": sky["key"],
        "headline": f"{rain_clause}{wind_clause}.",
        "t": now.get("t"),
        "feels": now.get("feels"),
        "tmin": min(temps) if temps else None,
        "tmax": max(temps) if temps else None,
        "bft": bft_now,
        "temp_tone": temp_tone(now.get("feels") if now.get("feels") is not None else now.get("t")),
        "wind_tone": wind_tone(bft_now),
        "severe": sky["severe"] or (max_g is not None and max_g >= 70),
    }

    return {
        "now": now,
        "hero": hero,
        "hours": hours,
        "daily": daily_summary(hours, run_utc),
        "stats": {"tmin": min(temps) if temps else None, "tmax": max(temps) if temps else None,
                  "precip24_mm": round(total_p, 1), "wind_max_kmh": max_w,
                  "wind_dir": wdir_max, "gust_max_kmh": max_g,
                  "bft_max": beaufort(max_w)},
        "summary": summary,
    }


def daily_summary(hours: list[dict], run_utc: str | None = None) -> list[dict]:
    """Aggregate by 24-hour windows measured in forecast hours.

    Bucketing by row position would be wrong: GFS is hourly to f120 and 3-hourly
    after, so seven rows can be four hours and seven rows can be eighteen. The
    bucket key is (step_h - 1) // 24, which is a real 24-hour day regardless of
    output cadence.

    Each bucket is also given a real calendar label (weekday + date) derived from
    the run time and its own first step. The carousel shows "Παρασκευή 25/9" rather
    than "Ημέρα 4": a card that says day 4 makes the reader count, and the count is
    the only way to know which day it lands on.
    """
    buckets: dict[int, list[dict]] = {}
    for h in hours:
        buckets.setdefault((int(h["step_h"]) - 1) // 24, []).append(h)

    out = []
    for day_idx in sorted(buckets):
        chunk = buckets[day_idx]
        ts = [h["t"] for h in chunk if h["t"] is not None]
        ws = [h["wind"] for h in chunk if h["wind"] is not None]
        gs = [h["gust"] for h in chunk if h["gust"] is not None]
        fs = [h["feels"] for h in chunk if h["feels"] is not None]
        if not ts:
            continue
        row = {
            "day": day_idx + 1,
            "from_h": chunk[0]["step_h"], "to_h": chunk[-1]["step_h"],
            "coverage_h": chunk[-1]["step_h"] - chunk[0]["step_h"] + 1,
            "samples": len(chunk),
            "tmin": round(min(ts), 1), "tmax": round(max(ts), 1),
            "feels_min": round(min(fs), 1) if fs else None,
            "rain_mm": round(float(sum(h["precip"] for h in chunk)), 1),
            "wind_max": max(ws) if ws else None, "gust_max": max(gs) if gs else None,
            "bft_max": beaufort(max(ws)) if ws else None,
        }
        # The day icon has to come from the whole day, not one hour: a daylight
        # hour would hide an overnight storm, and an overnight hour would describe
        # a day the user is awake for. Mean cover plus the day's heaviest hour is
        # the honest summary, and it reuses sky_condition so the glyph cannot drift
        # from the hero's.
        cl = [h["cloud_pct"] for h in chunk if h.get("cloud_pct") is not None]
        pr = [h["precip"] or 0.0 for h in chunk]
        row["cloud_pct"] = round(sum(cl) / len(cl)) if cl else None
        row["rain_max_h"] = round(max(pr), 1) if pr else 0.0
        sky = sky_condition(row["cloud_pct"], row["rain_max_h"], row["bft_max"])
        row["icon"] = sky["icon"]
        row["condition"] = sky["text"]
        row |= _day_label(run_utc, chunk[0]["step_h"], day_idx)
        out.append(row)
    return out


def _day_label(run_utc: str | None, first_step_h: int, day_idx: int) -> dict:
    """Calendar label for one daily bucket, in Europe/Athens.

    A forecast hour is a lead time from the *run*, which is published in UTC, so
    the local date is not run_date + N. Summer is UTC+3: a 06Z run's f24 lands at
    09:00 the next local morning, but a 12Z run's f24 lands at 15:00 while a 18Z
    run's f24 has already rolled past local midnight into the day after. Computing
    it from the timestamp rather than assuming keeps the card honest across both
    the DST switch and the four daily runs.
    """
    if not run_utc or len(run_utc) < 8:
        return {"date_label": None, "weekday": None, "is_today": day_idx == 0}
    try:
        base = dt.datetime.strptime(run_utc[:8], "%Y%m%d").replace(tzinfo=dt.timezone.utc)
        if len(run_utc) >= 10 and run_utc[8:10].isdigit():
            base += dt.timedelta(hours=int(run_utc[8:10]))
    except ValueError:
        return {"date_label": None, "weekday": None, "is_today": day_idx == 0}
    local = (base + dt.timedelta(hours=max(0, first_step_h - 1))).astimezone(astro._tz())
    today = dt.datetime.now(astro._tz()).date()
    return {
        "date_label": f"{local.day}/{local.month}",
        "weekday": astro.GREEK_DAYS[local.weekday()],
        # "Σήμερα" means the card's date is the local today, not "the first row".
        # The first bucket starts a few hours after the run, so it can already be
        # tomorrow when the run is late in the UTC day.
        "is_today": local.date() == today,
    }


def attribution_block() -> dict:
    return {
        "gfs": "Data source: NOAA/NWS GFS, public domain. No endorsement by NOAA implied.",
        "icon": "Source: Deutscher Wetterdienst (DWD), CC BY 4.0.",
        "ecmwf": ("This service is based on data and products of the European Centre for "
                  "Medium-Range Weather Forecasts (ECMWF). Source www.ecmwf.int. "
                  "Licensed CC BY 4.0. Data have been modified: indices were computed "
                  "and charts rendered by this service. ECMWF accepts no liability for "
                  "any error or omission in the data."),
        "geocoding": "Geocoding: Photon / OpenStreetMap contributors (ODbL).",
        "era5": ("Verification against ERA5 reanalysis. Generated using Copernicus "
                 "Climate Change Service information [2026]. Neither the European "
                 "Commission nor ECMWF is responsible for any use of this information."),
    }


@app.get("/api/cameras")
async def cameras_endpoint():
    """Camera metadata for the UI. Public: it is a marketing surface, not data.

    Only public metadata crosses the boundary. Private source material (RTSP
    URL, credentials) lives in a separate store that no endpoint reads; the
    payload is built from an explicit whitelist in cameras.py, so a new private
    key cannot leak by being carried along.

    ``snapshot_via`` is added here, not in the camera schema in cameras.py, so the
    server-side snapshot runtime is presentation metadata grafted onto the payload
    rather than a change to what a camera *is*. It tells the browser whether to
    load the still itself ("direct", today's behaviour) or through the server
    ("server", for a private source the browser must never see).
    """
    payload = cams.camera_payload()
    for cam in payload.get("cameras", []):
        via = snapshots.snapshot_via(cam.get("id", ""))
        if via:
            cam["snapshot_via"] = via
    return payload


@app.get("/api/cameras/{camera_id}")
async def camera_endpoint(camera_id: str):
    """One camera's public metadata, by id. Unknown and disabled both 404.

    The id is looked up in server-side configuration and never used to build a
    URL, so an arbitrary id cannot reach any feed or private source. A camera
    that an operator disabled is indistinguishable from one that never existed.
    """
    cam = cams.find_camera(camera_id)
    if cam is None:
        raise HTTPException(404, "Η κάμερα δεν υπάρχει.")
    return cam


@app.get("/api/cameras/{camera_id}/snapshot")
async def camera_snapshot_endpoint(camera_id: str):
    """The latest still for a camera, fetched server-side from the trusted source.

    The request names a camera *id* only. The URL to fetch comes from the private
    camera configuration, never from the caller, so no client input can name a
    host. The response is an image or a generic error; it never carries the source
    URL, a host, or a credential.

    Every failure collapses to one of three answers on purpose, and unknown vs
    disabled vs unreachable are deliberately indistinguishable:

    * ``404`` no enabled camera with that id (also covers disabled);
    * ``503`` the camera exists but a frame is not available right now (no source
      configured, rejected config, or the upstream failed);
    * ``504`` the upstream did not answer within the read timeout.

    A caller cannot learn from the response whether a particular internal host
    exists, is reachable, or is slow.
    """
    try:
        snap = await snapshots.get_snapshot(camera_id)
    except snapshots.UnknownCamera:
        raise HTTPException(404, "Η κάμερα δεν υπάρχει.")
    except (snapshots.NoSource, snapshots.SourceRejected):
        log.warning("snapshot unavailable: camera=%s reason=%s",
                    camera_id, "no source")
        raise HTTPException(503, "Η εικόνα δεν είναι διαθέσιμη αυτή τη στιγμή.")
    except snapshots.FetchFailed as e:
        is_timeout = "timeout" in str(e).lower()
        log.warning("snapshot fetch failed: camera=%s reason=%s",
                    camera_id, "timeout" if is_timeout else "upstream")
        raise HTTPException(504 if is_timeout else 503,
                            "Η εικόνα δεν είναι διαθέσιμη αυτή τη στιγμή.")
    return Response(content=snap.data, media_type=snap.content_type,
                    headers={"Cache-Control": "no-store"})


@app.get("/api/verify")
async def verify_endpoint(lat: float, lon: float, days: int = Query(4, ge=1, le=10)):
    """Archived-forecast skill against ERA5, for a point.

    Public on purpose: a verification number the customer cannot check is
    marketing, and the whole reason for building this was to have one that holds
    up. It is also expensive (one ERA5 chunk per hour plus one GFS range request
    per lead), so it is cached aggressively for a day.
    """
    _collect_coord(lat, lon)
    key = f"verify-api|{lat:.2f},{lon:.2f}|{days}"
    blob = wx.cache_get(key, ttl=24 * 3600)
    if blob is not None:
        return JSONResponse(json.loads(blob))
    try:
        result = await vfy.verify(lat, lon, days=days)
    except Exception as e:
        raise HTTPException(502, f"Verification unavailable: {type(e).__name__}: {str(e)[:120]}")
    if result.get("ok"):
        wx.cache_put(key, json.dumps(result).encode())
    return JSONResponse(result)


@app.get("/api/sky")
async def sky(lat: float, lon: float, elev: float | None = None):
    """Sun, moon and twilight for one point — free, no token.

    This is deliberately not PRO-gated: it is the card that makes the free tier
    worth returning to, and it depends on nothing we pay for per call. It is a
    pure calculation, so a burst of traffic costs CPU and no quota.
    """
    _collect_coord(lat, lon)
    try:
        return astro.sky_now(lat, lon, elev)
    except Exception as e:
        return JSONResponse({"available": False,
                             "reason": f"{type(e).__name__}: {str(e)[:160]}"},
                            status_code=200)


@app.get("/api/expert")
async def expert(request: Request, lat: float, lon: float, day: int = 0, hour: int = 0):
    """Instability indices and the vertical profile for a chosen lead time.

    PRO-only, and gated server-side before any download happens: a free caller
    gets 403 and no numbers at all. `day`/`hour` are the selector in the
    Εξειδικευμένα tab, so the expert can walk the forecast instead of only
    seeing now.

    The step is expressed in whole hours from the latest run and snapped to a
    step the GFS run genuinely publishes (hourly to +120 h, then every 3 h).
    Requesting f019 on a run that only has f018 is the bug this avoids.
    """
    _collect_coord(lat, lon)
    e = require_pro(request)

    # `day` and `hour` come from select controls, but a hand-rolled request can
    # send anything. Bounding them here keeps the step arithmetic in range.
    if not (0 <= day <= 11):
        raise HTTPException(422, "day must be between 0 and 11")
    if not (0 <= hour <= 23):
        raise HTTPException(422, "hour must be between 0 and 23")

    raw = day * 24 + hour
    step = nearest_gfs_step(raw, e.hours)

    async with httpx.AsyncClient(headers=wx.UA, timeout=120) as c:
        try:
            prof, run_utc = await wx.gfs_profile_dataset(
                c, lat, lon, step, with_run_time=True)
        except Exception as ex:
            raise HTTPException(502, f"GFS: {type(ex).__name__}: {str(ex)[:160]}")

    out = expert_indices(prof, lat, lon)
    out["requested_step"] = raw
    out["step"] = step
    out["run"] = run_utc
    out["valid_utc"] = wx.step_to_utc(run_utc, step)
    out["valid_label"] = fmt_valid(out["valid_utc"])
    out["lock_note"] = ("Το PRO φτάνει τα +240 h· ο επιλεγμένος χρόνος "
                        f"περιορίστηκε στο +{step} h.")
    return out


@app.get("/api/skewt")
async def skewt(request: Request, lat: float, lon: float, step: int = 12):
    _collect_coord(lat, lon)
    e = require_pro(request)
    if not (0 <= step <= ent.PRO_HOURS):
        raise HTTPException(422, f"step must be between 0 and {ent.PRO_HOURS}")
    step = nearest_gfs_step(step, e.hours)
    async with httpx.AsyncClient(headers=wx.UA, timeout=120) as c:
        try:
            ds = await wx.gfs_profile_dataset(c, lat, lon, step)
        except Exception as e:
            raise HTTPException(502, f"GFS: {e}")
    return Response(skewt_png(ds, lat, lon, step), media_type="image/png")


# ---------------------------------------------------------------- plans & auth

@app.get("/api/plans")
async def plans():
    p = ent.plan_payload()
    # The UI needs to know whether it can honestly offer payment, and whether
    # recurring billing is on by default, rather than discovering it on click.
    p["checkout_available"] = bill.checkout_available()
    p["auto_renew_default"] = bill.AUTO_RENEW_BY_DEFAULT
    if not bill.checkout_available():
        p["checkout_missing"] = bill.missing_config()
    return p


@app.post("/api/auth/passcode")
async def auth_passcode(payload: dict):
    """Exchange the master passcode for a signed PRO token (testing / comps).

    The token is HMAC-signed, so it cannot be forged, and it carries an expiry.
    """
    token = ent.check_passcode(str(payload.get("code", "")))
    if not token:
        raise HTTPException(401, "Λανθασμένος κωδικός.")
    return {"token": token, "tier": "pro", "source": "passcode",
            "expires_days": ent.TOKEN_TTL_S // 86400}


@app.post("/api/auth/trial")
async def auth_trial():
    """Start the 2-day trial. Issues a real, expiring PRO token.

    The trial is genuine access, not a teaser: it opens the full 10-day window,
    including Skew-T and the indices, because those are what the trial is meant
    to demonstrate.
    """
    return {"token": ent.issue_trial(), "tier": "pro", "source": "trial",
            "hours": ent.TRIAL_HOURS, "expires_days": ent.TRIAL_TTL_S // 86400}


@app.get("/api/me")
async def me(request: Request):
    """What this caller is entitled to right now.

    Uses the composed entitlement, not the bare token: a token whose subscription
    was cancelled must report FREE here, because this endpoint is what the UI
    trusts to decide whether to show the PRO badge.
    """
    e = effective_entitlement(request)
    payload = entitlement_payload(e)
    payload["redemptions"] = promo_redemptions_for(e.device)
    return payload


def promo_redemptions_for(device: str | None) -> list[dict]:
    """The caller's own promo windows, for "PRO activated until <date>"."""
    if not device:
        return []
    try:
        return [{"code": r["code"], "pro_until": r["pro_until"]}
                for r in promo.subject_redemptions(device)]
    except Exception:
        return []


# ---------------------------------------------------------------- billing (Stripe)

@app.post("/api/checkout")
async def checkout(request: Request):
    """Create a Stripe Checkout Session for the chosen plan.

    Refuses with 503 when Stripe is not configured, instead of returning a
    session-less success the UI would have to pretend to use.
    """
    if not bill.checkout_available():
        raise HTTPException(503, "Η πληρωμή δεν είναι διαθέσιμη: " + ", ".join(bill.missing_config()))
    # Same body guard as /api/promo/redeem: a malformed body is a client mistake
    # and must read as 400, not surface as an unhandled 500.
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Μη έγκυρο σώμα αιτήματος.")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Μη έγκυρο σώμα αιτήματος.")
    plan = str(payload.get("plan", "")).strip()
    if plan in ("trial", ""):
        raise HTTPException(400, "Διάλεξε μηνιαίο ή ετήσιο πλάνο.")
    # Pass the caller's own token through so the webhook can hand it back after
    # payment; it is signed, so it proves nothing to Stripe and carries no PII.
    token = request.headers.get("X-WX-Token") or None
    email = (payload.get("email") or "").strip() or None
    try:
        return bill.create_checkout(plan, customer_email=email, token=token)
    except Exception as e:
        # The exception type/text is dependency detail that must not reach the
        # client. Keep it server-side for the operator; answer neutrally.
        log.warning("checkout failed: plan=%s err=%s: %s",
                    plan, type(e).__name__, str(e)[:200])
        raise HTTPException(502, "Η πληρωμή δεν ξεκίνησε. Δοκίμασε ξανά σε λίγο.")


@app.post("/api/checkout/claim")
async def checkout_claim(payload: dict):
    """Exchange a paid Checkout Session for a PRO token.

    The browser cannot receive the webhook, so this is how the buyer gets their
    token back on return. The session id is not a secret the client can forge a
    payment with: it is verified against Stripe and must be paid. A session that
    is unpaid, unknown, or not a subscription is refused, so this cannot be used
    to mint PRO without paying.
    """
    if not bill.checkout_available():
        raise HTTPException(503, "Η πληρωμή δεν είναι διαθέσιμη.")
    sid = str(payload.get("session_id", "")).strip()
    if not sid.startswith("cs_"):
        raise HTTPException(400, "Μη έγκυρο αναγνωριστικό συνεδρίας.")
    stripe = bill._stripe()
    try:
        session = stripe.checkout.Session.retrieve(sid, expand=["subscription"])
    except Exception as e:
        raise HTTPException(502, f"Stripe: {type(e).__name__}: {str(e)[:160]}")
    if session.get("payment_status") != "paid" and session.get("status") != "complete":
        raise HTTPException(402, "Η πληρωμή δεν έχει ολοκληρωθεί.")
    sub = session.get("subscription")
    sub_id = sub.get("id") if isinstance(sub, dict) else sub
    if not sub_id:
        raise HTTPException(402, "Η συνεδρία δεν περιέχει συνδρομή.")
    return {"token": ent.issue_token("pro", "subscription", subscription_id=sub_id),
            "tier": "pro", "source": "subscription", "subscription_id": sub_id}


@app.get("/api/subscription")
async def subscription(request: Request):
    """Renewal state for the caller's own subscription.

    Identified by the subscription id inside the signed token, not by a
    guessable query parameter, so one visitor cannot read another's state.
    """
    token = request.headers.get("X-WX-Token") or request.query_params.get("token")
    e = ent.verify_token(token)
    if not e.subscription_id:
        raise HTTPException(404, "Δεν υπάρχει συνδρομή σε αυτό το token.")
    try:
        return bill.subscription_state(e.subscription_id)
    except Exception as ex:
        raise HTTPException(502, f"Stripe: {type(ex).__name__}: {str(ex)[:160]}")


@app.post("/api/subscription/auto-renew")
async def set_auto_renew(request: Request):
    """The discreet opt-out. enabled=false keeps access until the paid period ends."""
    token = request.headers.get("X-WX-Token") or None
    e = ent.verify_token(token)
    if not e.subscription_id:
        raise HTTPException(404, "Δεν υπάρχει συνδρομή σε αυτό το token.")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Μη έγκυρο σώμα αιτήματος.")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Μη έγκυρο σώμα αιτήματος.")
    enabled = bool(payload.get("enabled"))
    try:
        result = bill.set_auto_renew(e.subscription_id, enabled)
    except Exception as ex:
        log.warning("auto-renew change failed: sub=%s enabled=%s err=%s: %s",
                    e.subscription_id[:12], enabled, type(ex).__name__, str(ex)[:200])
        raise HTTPException(502, "Η αλλαγή δεν ολοκληρώθηκε. Δοκίμασε ξανά σε λίγο.")
    # The cached state is now wrong by construction; the user just changed it.
    bill.cache_forget(e.subscription_id)
    return result


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    """Activate PRO on a completed subscription payment.

    Verifies the signature when a webhook secret is configured; without it the
    endpoint is refused rather than trusted, because an unverified webhook lets
    anyone mint themselves a subscription.
    """
    secret = bill.webhook_secret()
    if not secret:
        raise HTTPException(503, "Ο webhook δεν είναι ρυθμισμένος.")
    stripe = bill._stripe()
    body = await request.body()
    sig = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(body, sig, secret)
    except Exception:
        raise HTTPException(400, "Μη έγκυρη υπογραφή webhook.")

    etype = event["type"]
    obj = event["data"]["object"]
    log.info("stripe webhook: type=%s id=%s", etype, obj.get("id"))

    # Every lifecycle event invalidates whatever we cached for that subscription.
    # The webhook is Stripe telling us the state changed, so continuing to serve a
    # cached "active" after `customer.subscription.deleted` would be a bug we were
    # explicitly warned about.
    sub_id = obj.get("id") if etype.startswith("customer.subscription") else obj.get("subscription")
    if sub_id:
        bill.cache_forget(sub_id)

    if etype == "checkout.session.completed":
        # The buyer needs their token on return; the webhook is the server-to-server
        # signal that the payment landed. Attach the subscription id so it is manageable.
        sub = obj.get("subscription")
        sub = sub.get("id") if isinstance(sub, dict) else sub
        meta = obj.get("metadata") or {}
        if sub and meta.get("wx_token"):
            return {"ok": True, "token": ent.issue_token("pro", "subscription",
                                                         subscription_id=sub)}
        return {"ok": True, "note": "payment recorded; claim endpoint issues the token"}

    if etype == "invoice.payment_failed":
        # Access is deliberately *not* revoked here. Stripe retries for several
        # days and `customer.subscription.updated` will carry `past_due`, which
        # `subscription_access` still admits. What this does is make the failure
        # visible and drop the cache so the next request sees the retry state.
        log.warning("invoice payment failed: customer=%s subscription=%s",
                    obj.get("customer"), obj.get("subscription"))

    if etype == "customer.subscription.deleted":
        log.info("subscription deleted: %s", sub_id)

    return {"ok": True, "ignored": etype}


# ---------------------------------------------------------------- Ecowitt station ingest

@app.post("/api/station/ecowitt")
async def ecowitt_ingest(request: Request):
    """Receive an Ecowitt "custom upload" push.

    Ecowitt gateways post form-encoded WU-style fields (tempf, windspeedmph, ...)
    with a PASSKEY identifying the device. The gateway cannot POST to an HTTPS
    URL on older firmwares, so this endpoint is also reachable over plain HTTP if
    the deployment chooses - but never expose the passkey in logs.
    """
    form = dict(await request.form())
    passkey = form.get("PASSKEY") or form.get("passkey")
    station_id = form.get("stationtype") and (form.get("PASSKEY") or "unknown")
    station_id = passkey or "unknown"

    def f(name: str, conv=lambda x: x):
        raw = form.get(name)
        if raw in (None, "", "--"):
            return None
        try:
            return conv(float(raw))
        except (TypeError, ValueError):
            return None

    temp_c = f("tempf", lambda x: (x - 32) * 5 / 9)
    if temp_c is None:
        temp_c = f("temp_c", lambda x: x)
    windspeed = f("windspeedmph", lambda x: x * 1.609344)
    gust = f("windgustmph", lambda x: x * 1.609344)
    humidity = f("humidity")
    pressure = f("baromrelin", lambda x: x * 33.8639)  # inHg -> hPa
    rain = f("dailyrainin", lambda x: x * 25.4)
    dateutc = form.get("dateutc")

    if dateutc and " " in dateutc:
        ts = dt.datetime.strptime(dateutc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    else:
        ts = dt.datetime.now(dt.timezone.utc)
    ts_iso = ts.strftime("%Y-%m-%dT%H:%M")

    ok = bias.record_obs(station_id, ts_iso, temp_c, humidity, windspeed, gust, pressure, rain)
    if not ok:
        return JSONResponse({"stored": False, "reason": "temperature out of plausible range"}, 422)
    return {"stored": True, "station_id": station_id[:8] + "…", "ts_utc": ts_iso,
            "temp_c": None if temp_c is None else round(temp_c, 2)}


@app.post("/api/station/register")
async def register_station(payload: dict):
    """Register or update a station. Validated here so bad input is a 422.

    A missing key used to be a KeyError and a non-numeric `lat` a ValueError,
    both of which surfaced as HTTP 500 from an unhandled exception - a public
    endpoint reporting a server fault for what is a client mistake.
    """
    station_id = payload.get("station_id")
    if not isinstance(station_id, str) or not (1 <= len(station_id) <= 64):
        raise HTTPException(422, "station_id is required (1-64 characters)")
    try:
        lat = float(payload["lat"])
        lon = float(payload["lon"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "lat and lon are required and must be numbers")
    if config.coord_error(lat, lon):
        raise HTTPException(422, config.coord_error(lat, lon))
    elevation_m = payload.get("elevation_m")
    if elevation_m is not None:
        try:
            elevation_m = float(elevation_m)
        except (TypeError, ValueError):
            raise HTTPException(422, "elevation_m must be a number")
        if not (-500 <= elevation_m <= 9000):
            raise HTTPException(422, "elevation_m is out of range")
    passkey = payload.get("passkey")
    name = payload.get("name")
    bias.init_db()
    bias.register_station(station_id=station_id, passkey=passkey,
                          name=name if isinstance(name, str) else None,
                          lat=lat, lon=lon, elevation_m=elevation_m)
    return {"registered": True}


@app.get("/api/station/{station_id}")
async def station_status(station_id: str):
    st = bias.get_station(station_id)
    if not st:
        raise HTTPException(404, "not registered")
    obs = bias.recent_obs(station_id, 12)
    b = bias.compute_bias(station_id, st["lat"], st["lon"], "gfs")
    # Public response: the passkey authenticates the Ecowitt push, so it must
    # never leave the server. `public_station` is the single choke point.
    return {"station": bias.public_station(st), "recent_obs": obs, "bias": b}


bias.init_db()


# ---------------------------------------------------------------- promo codes (public)

@app.post("/api/promo/redeem")
async def promo_redeem(request: Request):
    """Redeem a code for the calling device.

    The identity is the opaque device id carried in the caller's signed token.
    A caller with no token is issued one here — that is the moment a device
    becomes identifiable to this service, and it is why the response sets an
    httpOnly cookie as well as returning a token.

    Everything that decides whether the code is acceptable happens in
    `promo.redeem`, inside one SQLite transaction: a second request racing for
    the last remaining use cannot also succeed. Nothing about "is this caller
    PRO" is decided in the browser.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Μη έγκυρο σώμα αιτήματος.")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Μη έγκυρο σώμα αιτήματος.")
    code = str(payload.get("code", ""))
    caller = ent.verify_token(bearer_token(request))
    dev = caller.device
    minted = False
    if not dev:
        dev = secrets.token_hex(16)
        minted = True
    try:
        result = promo.redeem(code, dev, source="web")
    except promo.RedemptionError as e:
        # A refused attempt is still a redemption outcome worth counting; the
        # reason stays in meta so failures are separable from successes.
        analytics.track("promo_code_redeemed", request=request, device=dev,
                        meta={"ok": False, "reason": e.code})
        log.info("promo redeem refused: reason=%s", e.code)
        raise HTTPException(e.http_status, e.message)

    # A fresh PRO token carrying the device id, so the entitlement is checked
    # server-side on every later request rather than trusted from the client.
    #
    # A subscription id already in the caller's token is carried forward, so a
    # subscriber who redeems a gift code gets one token that unlocks both. Without
    # this the redemption would bind to a device the subscription token does not
    # have, and the paid subscriber would see no promo at all.
    token = ent.issue_token("pro", "promo", ttl=ent.TOKEN_TTL_S, device=dev,
                            subscription_id=caller.subscription_id)
    analytics.track("promo_code_redeemed", request=request, device=dev,
                    value=result["days"], meta={"ok": True, "code": result["code"][:16]})
    log.info("promo redeemed: code=%s days=%d", result["code"], result["days"])
    body = {
        "ok": True,
        "token": token,
        "code": result["code"],
        "days": result["days"],
        "pro_until": result["pro_until"],
        "pro_until_iso": result["pro_until_iso"],
        "message": f"Το PRO ενεργοποιήθηκε έως {result['pro_until_iso'][:10]}.",
    }
    resp = JSONResponse(body)
    if minted:
        with_device(resp, dev)
    return resp


@app.get("/api/promo/status")
async def promo_status(request: Request):
    """Whether this caller holds a promo window, for the small UI line.

    Also returns the caller's own device id. It is the identifier an operator
    needs to issue a personal gift code (`restricted_to`), and it is only ever
    the caller's own value — it is never looked up from anyone else's request.
    """
    dev = device_id(request)
    until = None
    try:
        until = promo.active_until(dev)
    except Exception:
        pass
    reds = promo_redemptions_for(dev)
    return {"active": bool(until),
            "pro_until": until,
            "pro_until_iso": (dt.datetime.fromtimestamp(until, dt.timezone.utc)
                              .strftime("%Y-%m-%d") if until else None),
            "device": dev,
            "codes": sorted({r["code"] for r in reds})}


# ---------------------------------------------------------------- promo admin

ADMIN_HEADER = "X-WX-Admin"


def require_admin(request: Request) -> None:
    """Gate the admin endpoints on a separate operator token.

    Deliberately a *different* secret from `WX_SECRET`: the signing key is used on
    every request path, while this one is only compared here. If it is unset the
    admin surface is closed rather than open — an unconfigured deploy must not
    expose code creation to the internet.

    Compared with `hmac.compare_digest`, and never logged.
    """
    configured = (os.environ.get("WX_ADMIN_TOKEN") or "").strip()
    if not configured:
        raise HTTPException(503, "Το admin API δεν είναι ρυθμισμένο (WX_ADMIN_TOKEN).")
    presented = request.headers.get(ADMIN_HEADER) or ""
    if not hmac.compare_digest(presented.encode(), configured.encode()):
        log.warning("admin auth failed from %s",
                    ratelimit.client_key(request, config.trust_proxy_headers())[:12])
        raise HTTPException(403, "Μη εξουσιοδοτημένη πρόσβαση.")


@app.post("/api/admin/promo")
async def admin_promo_create(request: Request):
    """Create or update a code. Nothing here needs a code change or a redeploy."""
    require_admin(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Μη έγκυρο σώμα αιτήματος.")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Μη έγκυρο σώμα αιτήματος.")
    try:
        row = promo.create_code(
            code=str(payload.get("code", "")),
            duration_days=payload.get("duration_days"),
            created_by=str(payload.get("created_by") or "admin")[:64],
            note=str(payload.get("note") or "")[:280] or None,
            max_redemptions=payload.get("max_redemptions"),
            active=bool(payload.get("active", True)),
            starts_at=payload.get("starts_at"),
            expires_at=payload.get("expires_at"),
            restricted_to=payload.get("restricted_to") or payload.get("intended_subject"),
            is_gift=bool(payload.get("is_gift", False)),
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"created": True, "code": row}


@app.get("/api/admin/promo")
async def admin_promo_list(request: Request, include_inactive: bool = True):
    """Codes with their use counts. No personal data beyond a truncated subject."""
    require_admin(request)
    codes = promo.list_codes(include_inactive=include_inactive)
    for c in codes:
        rs = promo.redemptions(c["code"], limit=50)
        c["recent_redemptions"] = [
            {"subject": (r["subject"] or "")[:8] + ("…" if r["subject"] else ""),
             "redeemed_at": r["redeemed_at"], "pro_until": r["pro_until"]}
            for r in rs]
    return {"codes": codes, "stats": promo.stats()}


@app.post("/api/admin/promo/{code}/active")
async def admin_promo_set_active(code: str, request: Request):
    """Revoke or restore a code."""
    require_admin(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Μη έγκυρο σώμα αιτήματος.")
    ok = promo.set_active(code, bool(payload.get("active")))
    if not ok:
        raise HTTPException(404, "Ο κωδικός δεν υπάρχει.")
    return {"code": promo.normalize(code), "active": bool(payload.get("active"))}


@app.get("/api/admin/analytics")
async def admin_analytics(request: Request, days: int = Query(30, ge=1, le=365)):
    require_admin(request)
    return analytics.summary(days=days)


# ---------------------------------------------------------------- notifications (PRO)

def _notify_enabled_or_503() -> None:
    if not notify.push_available():
        # Same shape as the billing 503: neutral message, no env names.
        raise HTTPException(503, "Οι ειδοποιήσεις δεν είναι διαθέσιμες αυτή τη στιγμή.")


def _notify_device(request: Request, response: Response,
                   e: ent.Entitlement) -> tuple[str, str | None]:
    """The caller's device id, minting one if their token predates it.

    Passcode and Stripe tokens carry no device id, so a legitimate PRO holder
    arriving from those flows has no identity for a subscription to key on. The
    first notify write mints one, exactly as the promo path does, and hands back
    a re-signed token carrying it. Returns (device, new_token_or_None).
    """
    if e.device:
        return e.device, None
    device = secrets.token_hex(16)
    token = ent.issue_token(e.tier or "pro", e.source or "passcode",
                            device=device, subscription_id=e.subscription_id)
    with_device(response, device)
    return device, token


@app.get("/api/push/config")
async def push_config():
    """Whether push can work here, and the public key the browser needs.

    The private key is never part of this response and never leaves the server.
    """
    available = notify.push_available()
    return {"available": available,
            "reason": notify.unavailable_reason(),
            "vapid_public_key": notify.vapid_public_key() if available else None}


@app.get("/api/notify/state")
async def notify_state(request: Request):
    """The app's view of this caller's notification settings.

    Deliberately separate from the browser's own `Notification.permission`: the UI
    must be able to show "blocked in the browser" and "off in the app" as two
    different states, and only the browser knows the first.
    """
    e = effective_entitlement(request)
    sub = notify.get_subscription(e.device) if e.device else None
    return {
        "eligible": bool(e.is_pro),
        "tier": e.tier,
        "source": e.source,
        "available": notify.push_available(),
        "reason": notify.unavailable_reason(),
        "subscribed": bool(sub),
        "active": bool(sub and sub.get("active")),
        "has_location": bool(sub and sub.get("has_location")),
        "place_name": sub.get("place_name") if sub else None,
        "place_admin1": sub.get("place_admin1") if sub else None,
        "rules": sub.get("rules") if sub else None,
        "quiet_from": sub.get("quiet_from") if sub else None,
        "quiet_to": sub.get("quiet_to") if sub else None,
        "ios_standalone": bool(sub and sub.get("ios_standalone")),
    }


@app.post("/api/notify/location")
async def notify_location(request: Request, response: Response, payload: dict):
    """Set or change the notification location.

    Only this endpoint changes it. Viewing a forecast for another place never
    touches it, which is the whole point of keeping the notification area apart
    from the browsing one. The exact point is quantized before storage.
    """
    _notify_enabled_or_503()
    e = require_pro(request)
    device, _ = _notify_device(request, response, e)
    sub = notify.get_subscription(device)
    if not sub:
        raise HTTPException(409, "Ενεργοποίησε πρώτα τις ειδοποιήσεις.")
    try:
        lat = float(payload.get("lat"))
        lon = float(payload.get("lon"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Το lat και το lon πρέπει να είναι αριθμοί.")
    err = config.coord_error(lat, lon)
    if err:
        raise HTTPException(422, err)
    name = payload.get("name")
    admin1 = payload.get("admin1")
    updated = notify.set_location(
        device,
        place_name=name[:80] if isinstance(name, str) else None,
        place_admin1=admin1[:80] if isinstance(admin1, str) else None,
        lat=lat, lon=lon)
    if updated is None:
        raise HTTPException(404, "Δεν βρέθηκε η συνδρομή.")
    analytics.track("notify_location_set", request=request, device=device, lat=lat, lon=lon)
    return {"ok": True, "place_name": updated.get("place_name"),
            "place_admin1": updated.get("place_admin1"),
            "cell": notify.cell_label(updated.get("cell_lat"), updated.get("cell_lon"))}


@app.post("/api/notify/prefs")
async def notify_prefs(request: Request, response: Response, payload: dict):
    """Enable/disable the app-level subscription and choose alert types."""
    _notify_enabled_or_503()
    e = require_pro(request)
    device, _ = _notify_device(request, response, e)
    sub = notify.get_subscription(device)
    if not sub:
        raise HTTPException(409, "Ενεργοποίησε πρώτα τις ειδοποιήσεις.")
    if "active" in payload:
        notify.set_active(device, bool(payload.get("active")))
    if isinstance(payload.get("rules"), dict) or "quiet_from" in payload or "quiet_to" in payload:
        qf = payload.get("quiet_from")
        qt = payload.get("quiet_to")
        for v, label in ((qf, "quiet_from"), (qt, "quiet_to")):
            if v is not None and not (isinstance(v, int) and 0 <= v <= 23):
                raise HTTPException(422, f"Το {label} πρέπει να είναι ώρα 0-23.")
        notify.set_rules(device,
                         payload.get("rules") if isinstance(payload.get("rules"), dict) else {},
                         quiet_from=qf if isinstance(qf, int) else None,
                         quiet_to=qt if isinstance(qt, int) else None)
    sub = notify.get_subscription(device)
    return {"ok": True, "subscribed": True, "active": bool(sub.get("active")),
            "rules": sub.get("rules")}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request, response: Response, payload: dict):
    """Store this device's push subscription. PRO only, like every PRO surface."""
    _notify_enabled_or_503()
    e = require_pro(request)
    device, new_token = _notify_device(request, response, e)

    subinfo = payload.get("subscription") if isinstance(payload.get("subscription"), dict) else payload
    endpoint = subinfo.get("endpoint")
    keys = subinfo.get("keys") if isinstance(subinfo.get("keys"), dict) else {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not (isinstance(endpoint, str) and endpoint.startswith("https://")
            and isinstance(p256dh, str) and isinstance(auth, str)):
        raise HTTPException(422, "Μη έγκυρη συνδρομή push.")

    ua_class, browser = analytics.describe_ua(request.headers.get("user-agent"))
    loc = payload.get("location") if isinstance(payload.get("location"), dict) else None
    cell = None
    place_name = place_admin1 = None
    if loc:
        try:
            lat, lon = float(loc.get("lat")), float(loc.get("lon"))
            if not config.coord_error(lat, lon):
                cell = notify.quantize(lat, lon)
                place_name = loc.get("name")[:80] if isinstance(loc.get("name"), str) else None
                place_admin1 = loc.get("admin1")[:80] if isinstance(loc.get("admin1"), str) else None
        except (TypeError, ValueError):
            cell = None

    notify.upsert_subscription(
        device, endpoint, p256dh, auth,
        ua_class=ua_class, browser=browser,
        ios_standalone=bool(payload.get("ios_standalone")),
        place_name=place_name, place_admin1=place_admin1, cell=cell,
        rules=payload.get("rules"),
        quiet_from=payload.get("quiet_from") if isinstance(payload.get("quiet_from"), int) else None,
        quiet_to=payload.get("quiet_to") if isinstance(payload.get("quiet_to"), int) else None,
        pro_until=int(e.pro_until or e.expires_at or 0) or None,
        subscription_id=e.subscription_id)
    analytics.track("notify_enabled", request=request, device=device,
                    lat=cell[0] if cell else None, lon=cell[1] if cell else None)
    out = {"ok": True, "subscribed": True}
    if new_token:
        out["token"] = new_token
    return out


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request, payload: dict | None = None):
    """Deactivate the app-level subscription. Optionally erase the stored data.

    A purge is offered because the privacy policy promises removal on request;
    the default only flips `active`, which keeps the dedupe history intact.
    """
    e = effective_entitlement(request)
    if not e.device:
        return {"ok": True, "unsubscribed": False}
    purge = bool((payload or {}).get("purge"))
    if purge:
        notify.purge(e.device)
        analytics.track("notify_disabled", request=request, device=e.device,
                        meta={"purge": True})
        return {"ok": True, "unsubscribed": True, "purged": True}
    changed = notify.set_active(e.device, False)
    analytics.track("notify_disabled", request=request, device=e.device)
    return {"ok": True, "unsubscribed": changed}


@app.post("/api/notify/test")
async def notify_test(request: Request):
    """Send one real notification to this device. PRO only; tightly rate-limited."""
    _notify_enabled_or_503()
    e = require_pro(request)
    sub = notify.get_subscription(e.device) if e.device else None
    if not sub or not sub.get("active"):
        raise HTTPException(409, "Ενεργοποίησε πρώτα τις ειδοποιήσεις.")
    alert = notify.Alert(
        rule="test", severity="warn", lead_h=0, bucket=0, value=0.0, window_h=0,
        title="🔔 Δοκιμαστική ειδοποίηση",
        body="Οι ειδοποιήσεις λειτουργούν σε αυτή τη συσκευή.",
        cell=notify.cell_label(sub.get("cell_lat"), sub.get("cell_lon")))
    ok, err = notify.send_push(sub, alert)
    if not ok:
        log.warning("notify: test push failed for %s: %s", str(e.device)[:12], err)
        raise HTTPException(502, "Η αποστολή δοκιμαστικής ειδοποίησης απέτυχε.")
    return {"ok": True, "sent": True}


# ---------------------------------------------------------------- analytics ingest

@app.post("/api/analytics")
async def analytics_ingest(request: Request):
    """Record a batch of events from the page.

    Bounded on both axes: at most 25 events per call, and each event name must be
    in the closed vocabulary. A visitor cannot create a new event dimension, and
    a client cannot use this as a way to write unbounded rows. Coordinates, when
    present, are reduced to a coarse cell inside `analytics.track` and the exact
    point is never stored.
    """
    if not analytics.enabled():
        return {"ok": True, "recorded": 0, "enabled": False}
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Μη έγκυρο σώμα αιτήματος.")
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list):
        raise HTTPException(400, "Το πεδίο 'events' πρέπει να είναι λίστα.")
    if len(events) > 25:
        raise HTTPException(413, "Πολλά events σε ένα αίτημα (όριο 25).")
    dev = device_id(request)
    recorded = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        name = str(ev.get("name", ""))
        lat = ev.get("lat") if isinstance(ev.get("lat"), (int, float)) else None
        lon = ev.get("lon") if isinstance(ev.get("lon"), (int, float)) else None
        meta = ev.get("meta") if isinstance(ev.get("meta"), dict) else None
        value = ev.get("value") if isinstance(ev.get("value"), (int, float)) else None
        if analytics.track(name, request=request, device=dev, lat=lat, lon=lon,
                           value=value, meta=meta):
            recorded += 1
    if recorded:
        # Opportunistic tidy, throttled to once an hour inside `maybe_prune`.
        analytics.maybe_prune()
    # The response is deliberately empty of detail: this endpoint is not a way to
    # probe which event names exist.
    return {"ok": True, "recorded": recorded}


analytics.init_db()
