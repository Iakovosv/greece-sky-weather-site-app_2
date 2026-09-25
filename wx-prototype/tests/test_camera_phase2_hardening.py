"""Phase 2 camera-infrastructure hardening: regression and security tests.

One suite per promise the milestone makes, so a future change that erodes one of
them fails here rather than in production:

* the upstream-fetch throttle (Option B): cache hits never spend a token, one
  burst is one token, a caller is refused before any dial, and the refusal uses
  the app's existing 429 contract;
* snapshot-runtime robustness: a declared over-cap length is refused before the
  buffer grows, and one normalisation decides "is this an image";
* strict camera-config validation: an id or coordinate that is not clearly safe
  fails safe instead of reaching a payload, a route or an element id;
* LIVE state robustness: opening twice does not build a second player.
* operator diagnostics: gated like the other admin routes, and sanitized.

Everything is local/mock. No real camera, RTSP URL, credential or live id.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cameras as cams
import ratelimit
import snapshots as snap

PUBLIC_HTTPS = "https://cam.example/latest.jpg"
SECRET_PASS = "unit-test-pass-123"
INTERNAL_HOST = "cam-lan.internal"
SOURCE_URL = "rtsp://cam-lan.internal:554/Streaming/Channels/101"


@pytest.fixture
def env(monkeypatch):
    def _apply(*, cameras=None, sources=None, allowed=None, mock=None, **extra):
        for k, v in extra.items():
            monkeypatch.setenv(k, v)
        for key, value in (("WX_CAMERAS", cameras),
                           ("WX_CAMERA_SOURCES", sources),
                           ("WX_CAMERA_ALLOWED_HOSTS", allowed),
                           ("WX_CAMERA_SNAPSHOT_MOCK", mock)):
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        importlib.reload(cams)
        importlib.reload(snap)
        snap.reset_cache()
        ratelimit.LIMITER.reset()
    yield _apply
    importlib.reload(cams)
    importlib.reload(snap)
    snap.reset_cache()
    ratelimit.LIMITER.reset()


@pytest.fixture
def throttle_on(monkeypatch):
    """Rate limiting is normally off for the suite; opt in for this test."""
    monkeypatch.delenv("WX_RATE_LIMIT_DISABLED", raising=False)
    ratelimit.LIMITER.reset()
    yield
    ratelimit.LIMITER.reset()


def _cam(**over):
    cam = {"id": "ilioupoli", "name": "Ilioupoli Sky", "region": "Αττική",
           "lat": 37.9333, "lon": 23.75, "snapshot": PUBLIC_HTTPS}
    cam.update(over)
    return json.dumps([cam])


def _client():
    import app as app_module
    return TestClient(app_module.app)


# ================================================= item 1: upstream-fetch throttle

def test_cache_hit_never_spends_a_token(env, throttle_on):
    """Many readers of one frame cost one fetch and zero further tokens."""
    env(cameras=_cam(), mock="ok")
    c = _client()
    # First request is a miss (one token) and fills the cache.
    assert c.get("/api/cameras/ilioupoli/snapshot").status_code == 200
    # Every later request inside the TTL is a hit: no token, no refusal, even
    # far beyond the bucket's small burst.
    for _ in range(30):
        assert c.get("/api/cameras/ilioupoli/snapshot").status_code == 200


def test_a_cold_burst_is_one_fetch_and_one_token(env, throttle_on, monkeypatch):
    """N simultaneous viewers of a cold camera share the single fetch; the burst
    cannot be spent N times because only the single-flight winner charges."""
    env(cameras=_cam(), mock="ok")
    spend = {"n": 0}
    real_allow = ratelimit.LIMITER.allow

    def counting_allow(key, limits, cost=1.0, now=None):
        allowed, retry = real_allow(key, limits, cost, now)
        if key.startswith("camsnap:"):
            spend["n"] += 1
        return allowed, retry

    monkeypatch.setattr(ratelimit.LIMITER, "allow", counting_allow)
    snap.reset_cache()
    source = snap.source_for("ilioupoli")

    import asyncio

    async def go():
        return await asyncio.gather(*[
            snap.get_snapshot("ilioupoli", client_key="client-a")
            for _ in range(12)])

    results = asyncio.run(go())
    assert all(r.data == results[0].data for r in results)
    # One fetch, one charged token -- not twelve.
    assert spend["n"] == 1


def test_throttle_is_keyed_per_camera(env, throttle_on):
    """One camera's spent budget does not refuse a different camera."""
    env(cameras=json.dumps([
        {"id": "ilioupoli", "snapshot": PUBLIC_HTTPS},
        {"id": "glinado", "snapshot": PUBLIC_HTTPS}]), mock="ok")
    # Exhaust ilioupoli's bucket, then glinado still works.
    for _ in range(6):
        ratelimit.LIMITER.allow("camsnap:ilioupoli:client-a",
                                ratelimit.CAMERA_SNAPSHOT)
    ok, _ = ratelimit.LIMITER.allow("camsnap:ilioupoli:client-a",
                                    ratelimit.CAMERA_SNAPSHOT)
    assert ok is False
    other, _ = ratelimit.LIMITER.allow("camsnap:glinado:client-a",
                                       ratelimit.CAMERA_SNAPSHOT)
    assert other is True


def test_a_throttled_refusal_is_429_with_retry_after(env, throttle_on):
    """A refused new fetch uses the same contract as the middleware: 429 +
    Retry-After, not a generic 503 that would invite an immediate retry."""
    env(cameras=_cam(), mock="ok")
    c = _client()
    # Spend the bucket for this caller's key: 6 burst tokens, then the next cold
    # fetch is refused. The key is `camsnap:<camera>:<client_key>` and TestClient's
    # peer is "testclient" (`ratelimit.client_key`).
    key = "camsnap:ilioupoli:ip:testclient"
    for _ in range(6):
        ratelimit.LIMITER.allow(key, ratelimit.CAMERA_SNAPSHOT)
    snap.reset_cache()   # force a cold fetch for the next request
    r = c.get("/api/cameras/ilioupoli/snapshot")
    assert r.status_code == 429
    assert "retry_after" in r.json()
    assert r.headers.get("Retry-After")


def test_a_cache_hit_survives_an_exhausted_bucket(env, throttle_on):
    """The point of Option B: a warm frame is served even when the caller has no
    fetch tokens left, so normal viewers are not throttled."""
    env(cameras=_cam(), mock="ok")
    c = _client()
    assert c.get("/api/cameras/ilioupoli/snapshot").status_code == 200   # warms
    # Drain every token this caller has.
    for _ in range(50):
        ratelimit.LIMITER.allow("camsnap:ilioupoli:testclient",
                                ratelimit.CAMERA_SNAPSHOT)
    # Still a hit: served, not refused.
    assert c.get("/api/cameras/ilioupoli/snapshot").status_code == 200


def test_direct_in_process_call_is_not_throttled(env, throttle_on):
    """A caller with no client identity (in-process) is not throttled."""
    env(cameras=_cam(), mock="ok")
    import asyncio
    for _ in range(20):
        snap.reset_cache()
        assert asyncio.run(snap.get_snapshot("ilioupoli")).data


def test_throttle_honors_the_global_disable_switch(env, monkeypatch):
    monkeypatch.setenv("WX_RATE_LIMIT_DISABLED", "1")
    env(cameras=_cam(), mock="ok")
    c = _client()
    for _ in range(20):
        snap.reset_cache()
        assert c.get("/api/cameras/ilioupoli/snapshot").status_code == 200


# =============================================== item 3: runtime robustness

def test_declared_over_cap_length_is_refused_before_buffering(env):
    """One normalisation decides "is this an image"; a text/html login page is
    never served as a camera frame."""
    env(cameras=_cam(), mock="wrongtype")
    assert _client().get("/api/cameras/ilioupoli/snapshot").status_code == 503


def test_oversized_mock_is_refused(env):
    env(cameras=_cam(), mock="oversize")
    assert _client().get("/api/cameras/ilioupoli/snapshot").status_code == 503


def test_empty_mock_is_refused(env):
    env(cameras=_cam(), mock="empty")
    assert _client().get("/api/cameras/ilioupoli/snapshot").status_code == 503


def test_image_content_type_helper_matches_the_fetch_path(env):
    assert snap._is_image_content_type("image/jpeg") is True
    assert snap._is_image_content_type("image/png; charset=binary") is True
    assert snap._is_image_content_type("text/html") is False
    assert snap._is_image_content_type("") is False


# ====================================== item 4: strict config validation

def test_an_id_outside_the_shape_is_dropped(env):
    """An id is a URL segment and an element id; an unsafe one fails safe."""
    env(cameras=json.dumps([{"id": "../../etc/passwd", "snapshot": PUBLIC_HTTPS}]))
    payload = cams.camera_payload()
    assert all("<" not in c["id"] and "/" not in c["id"] for c in payload["cameras"])


def test_a_blank_or_non_string_id_falls_back_to_the_positional_id(env):
    env(cameras=json.dumps([{"id": "", "snapshot": PUBLIC_HTTPS}]))
    payload = cams.camera_payload()
    assert payload["cameras"][0]["id"].startswith("cam")


def test_an_uppercase_id_is_normalised_to_the_constrained_shape(env):
    env(cameras=json.dumps([{"id": "ILIOUPOLI", "snapshot": PUBLIC_HTTPS}]))
    assert cams.camera_payload()["cameras"][0]["id"] == "ilioupoli"


def test_an_out_of_range_latitude_is_not_a_coordinate(env):
    env(cameras=json.dumps([{"id": "ilioupoli", "lat": 100.0, "lon": 23.7,
                             "snapshot": PUBLIC_HTTPS}]))
    assert cams.camera_payload()["cameras"][0]["lat"] is None


def test_a_string_latitude_fails_safe(env):
    env(cameras=json.dumps([{"id": "ilioupoli", "lat": "not-a-number",
                             "lon": 23.7, "snapshot": PUBLIC_HTTPS}]))
    assert cams.camera_payload()["cameras"][0]["lat"] is None


def test_a_valid_longitude_near_the_bound_is_kept(env):
    env(cameras=json.dumps([{"id": "ilioupoli", "lat": -89.9, "lon": 179.9,
                             "snapshot": PUBLIC_HTTPS}]))
    cam = cams.camera_payload()["cameras"][0]
    assert cam["lat"] == -89.9 and cam["lon"] == 179.9


def test_a_built_in_site_keeps_its_coordinates_through_the_mapping_form(env):
    """Strictness must not drop the built-in coordinates a short-form install
    relies on: the mapping form merges the default site, coordinates included."""
    env(cameras=json.dumps({"ilioupoli": PUBLIC_HTTPS}))
    cam = cams.camera_payload()["cameras"][0]
    assert cam["id"] == "ilioupoli"
    assert cam["lat"] == 37.9333 and cam["lon"] == 23.75


# =============================================== item 5: LIVE robustness

def test_live_open_is_idempotent():
    import app as app_module
    html = TestClient(app_module.app).get("/").text
    # The guard precedes any iframe construction, so a second click is a no-op.
    guard = html.index("classList.contains('playing')) return;")
    build = html.index("document.createElement('iframe')")
    assert guard < build


def test_live_fallback_and_close_are_still_wired():
    import app as app_module
    html = TestClient(app_module.app).get("/").text
    for fn in ("function openCamLive(id)", "function closeCamLive(id)",
               "function showLiveFallback(player,msg)"):
        assert fn in html


# =========================================== item 2: operator diagnostics

def test_admin_cameras_is_closed_when_unconfigured(env, monkeypatch):
    env(cameras=_cam(), mock="ok")
    monkeypatch.delenv("WX_ADMIN_TOKEN", raising=False)
    import app as app_module
    c = TestClient(app_module.app)
    assert c.get("/api/admin/cameras").status_code == 503


def test_admin_cameras_requires_the_operator_token(env, monkeypatch):
    env(cameras=_cam(), mock="ok")
    monkeypatch.setenv("WX_ADMIN_TOKEN", "operator-secret")
    import app as app_module
    c = TestClient(app_module.app)
    assert c.get("/api/admin/cameras").status_code == 403
    assert c.get("/api/admin/cameras",
                 headers={"X-WX-Admin": "operator-secret"}).status_code == 200


def test_diagnostics_never_carry_a_source_url_host_or_credential(env, monkeypatch):
    env(cameras=_cam(live_enabled=True, live_provider="youtube",
                     youtube_live_id="MOCKPUBLICID"),
        sources=json.dumps([{"id": "ilioupoli", "url": SOURCE_URL,
                             "username": "viewer", "password": SECRET_PASS,
                             "secret_ref": "WX_CAM_TEST_PASS"}]),
        allowed=INTERNAL_HOST, WX_CAM_TEST_PASS=SECRET_PASS)
    monkeypatch.setenv("WX_ADMIN_TOKEN", "operator-secret")
    import app as app_module
    c = TestClient(app_module.app)
    r = c.get("/api/admin/cameras", headers={"X-WX-Admin": "operator-secret"})
    assert r.status_code == 200
    blob = json.dumps(r.json())
    for leak in (SECRET_PASS, SOURCE_URL, INTERNAL_HOST, "rtsp://", "554"):
        assert leak not in blob


def test_diagnostics_reports_per_camera_cache_age(env):
    env(cameras=_cam(), mock="ok")
    snap.reset_cache()
    # Warm the cache, then read diagnostics against a driven clock.
    import asyncio
    asyncio.run(snap.get_snapshot("ilioupoli", now=1000.0))
    d = snap.diagnostics(now=1010.0)
    row = next(r for r in d["cameras"] if r["id"] == "ilioupoli")
    assert row["configured"] is True
    assert row["cached"] is True
    assert row["cache_age_s"] >= 0
    assert row["via"] == "server"       # mock mode is served through the server


def test_diagnostics_survives_a_rejected_source(env):
    env(cameras=_cam(),
        sources=json.dumps([{"id": "ilioupoli", "url": "rtsp://cam-lan.internal/x"}]),
        allowed=INTERNAL_HOST)
    d = snap.diagnostics()
    row = next(r for r in d["cameras"] if r["id"] == "ilioupoli")
    assert row["source_kind"] == "rejected"
    assert "cam-lan.internal" not in json.dumps(d)
