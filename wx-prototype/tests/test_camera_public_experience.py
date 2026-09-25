"""End-to-end camera experience, driven entirely from local/mock configuration.

One test per scenario the milestone names, each proving the full path
``configuration -> /api/cameras -> /api/cameras/{id}/snapshot -> served page``
rather than a single layer in isolation. Everything is deterministic and offline:
the snapshot runtime runs in mock modes and no DNS, socket or real camera is
touched. No real camera IP, RTSP URL, credential or YouTube live id appears here.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cameras as cams
import ratelimit
import snapshots as snap

PUBLIC_HTTPS = "https://cam.example/latest.jpg"
MOCK_YT_ID = "MOCKPUBLICID"


@pytest.fixture(autouse=True)
def _isolate_limiter(monkeypatch):
    """Rate limiting is off for the suite, and the fetch bucket is process-global.

    Both are pinned per test so one scenario's misses cannot spend the next
    scenario's tokens, and so the throttle is only consulted where a test asks
    for it.
    """
    monkeypatch.setenv("WX_RATE_LIMIT_DISABLED", "1")
    ratelimit.LIMITER.reset()
    yield
    ratelimit.LIMITER.reset()


@pytest.fixture
def env(monkeypatch):
    """Reload the pair per test so cameras/snapshots share one registry."""
    def _apply(*, cameras=None, sources=None, allowed=None, mock=None):
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
    yield _apply
    importlib.reload(cams)
    importlib.reload(snap)
    snap.reset_cache()


def _cam(**over):
    cam = {"id": "ilioupoli", "name": "Ilioupoli Sky", "region": "Αττική",
           "lat": 37.9333, "lon": 23.75, "snapshot": PUBLIC_HTTPS}
    cam.update(over)
    return json.dumps([cam])


def _client(monkeypatch) -> TestClient:
    import app as app_module
    importlib.reload(app_module)
    return TestClient(app_module.app)


def _page(monkeypatch) -> str:
    r = _client(monkeypatch).get("/")
    assert r.status_code == 200
    return r.text


# ============================================================ 1. success

def test_scenario_1_successful_snapshot_end_to_end(env, monkeypatch):
    env(cameras=_cam(), mock="ok")
    c = _client(monkeypatch)
    meta = c.get("/api/cameras").json()
    assert meta["cameras"][0]["status"] == "live"
    assert meta["cameras"][0]["snapshot_via"] == "server"
    r = c.get("/api/cameras/ilioupoli/snapshot")
    assert r.status_code == 200
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"      # a real, decodable image
    assert r.headers["content-type"].startswith("image/")
    assert r.headers["cache-control"] == "no-store"


def test_scenario_1_served_page_uses_the_server_path_not_the_source_url(env, monkeypatch):
    env(cameras=_cam(), mock="ok")
    html = _page(monkeypatch)
    js = re.search(r"<script>(.*?)</script></body></html>", html, re.S).group(1)
    # The resolver switches to the server route only for snapshot_via==="server".
    assert "c.snapshot_via==='server'" in js
    assert "/api/cameras/'+encodeURIComponent(c.id)+'/snapshot" in js


# ============================================================ 2. not configured

def test_scenario_2_not_configured_shows_a_clean_state(env, monkeypatch):
    env(cameras=json.dumps([{"id": "glinado", "name": "Glinado Sky"}]))
    c = _client(monkeypatch)
    cam = c.get("/api/cameras").json()["cameras"][0]
    assert cam["status"] == "not_configured"
    assert cam["snapshot"] is None
    # The snapshot endpoint reports "unavailable", never a broken fetch.
    assert c.get("/api/cameras/glinado/snapshot").status_code == 503
    # And the page never names a configuration variable to the visitor.
    assert "WX_CAMERAS" not in _page(monkeypatch)


# ============================================================ 3. timeout

def test_scenario_3_timeout_is_504_and_generic(env, monkeypatch):
    env(cameras=_cam(), mock="timeout")
    c = _client(monkeypatch)
    assert c.get("/api/cameras/ilioupoli/snapshot").status_code == 504
    body = c.get("/api/cameras/ilioupoli/snapshot").text
    for leak in ("timeout", "Timeout", PUBLIC_HTTPS, "cam.example"):
        assert leak not in body


# ============================================================ 4. error

def test_scenario_4_upstream_error_is_503_and_carries_no_detail(env, monkeypatch):
    env(cameras=_cam(), mock="error")
    c = _client(monkeypatch)
    r = c.get("/api/cameras/ilioupoli/snapshot")
    assert r.status_code == 503
    assert r.json()["detail"] == "Η εικόνα δεν είναι διαθέσιμη αυτή τη στιγμή."


def test_scenario_4_ui_error_state_is_graceful(env, monkeypatch):
    env(cameras=_cam(), mock="error")
    html = _page(monkeypatch)
    js = re.search(r"<script>(.*?)</script></body></html>", html, re.S).group(1)
    m = re.search(r"function snapshotFailed\(id\)\{(.*?)\n(?:async )?function", js, re.S)
    body = m.group(1)
    assert "img.style.display='none'" in body        # no broken-image icon
    assert "badge.classList.add('off')" in body      # no misleading LIVE
    for leak in ("stack", "trace", "err.", "http://", "https://"):
        assert leak not in body


# ============================================================ 5. wrong type

def test_scenario_5_wrong_content_type_is_refused(env, monkeypatch):
    env(cameras=_cam(), mock="wrongtype")
    c = _client(monkeypatch)
    r = c.get("/api/cameras/ilioupoli/snapshot")
    assert r.status_code == 503
    assert b"<html>" not in r.content                 # the HTML body is not relayed


# ============================================================ 6. oversized

def test_scenario_6_oversized_response_is_refused(env, monkeypatch):
    env(cameras=_cam(), mock="oversize")
    c = _client(monkeypatch)
    r = c.get("/api/cameras/ilioupoli/snapshot")
    assert r.status_code == 503
    assert len(r.content) < snap.MAX_BYTES            # the cap is enforced upstream


# ============================================================ 7. disabled

def test_scenario_7_disabled_camera_is_absent_and_indistinguishable(env, monkeypatch):
    env(cameras=json.dumps([{"id": "ilioupoli", "enabled": False,
                             "snapshot": PUBLIC_HTTPS}]), mock="ok")
    c = _client(monkeypatch)
    assert c.get("/api/cameras").json()["cameras"] == []
    assert c.get("/api/cameras/ilioupoli").status_code == 404
    assert c.get("/api/cameras/ilioupoli/snapshot").status_code == 404
    # Unknown and disabled give the same answer, so neither reveals the other.
    assert c.get("/api/cameras/never-existed").status_code == 404


# ============================================================ 8/9. LIVE config

def test_scenario_8_valid_youtube_live_configuration_offers_the_player(env, monkeypatch):
    env(cameras=_cam(live_enabled=True, live_provider="youtube",
                     youtube_live_id=MOCK_YT_ID))
    cam = _client(monkeypatch).get("/api/cameras").json()["cameras"][0]
    assert cam["live"] == {"provider": "youtube", "video_id": MOCK_YT_ID,
                           "privacy_enhanced": True}


def test_scenario_9_no_youtube_live_configuration_offers_nothing(env, monkeypatch):
    env(cameras=_cam())
    cam = _client(monkeypatch).get("/api/cameras").json()["cameras"][0]
    assert cam["live"] is None
    html = _page(monkeypatch)
    js = re.search(r"<script>(.*?)</script></body></html>", html, re.S).group(1)
    # The button gate reads the server field, so a missing block renders nothing.
    assert "const golive = (live && c.live)" in js


def test_scenario_9b_partial_live_configuration_offers_nothing(env, monkeypatch):
    env(cameras=_cam(live_enabled=True, live_provider="youtube"))
    assert _client(monkeypatch).get("/api/cameras").json()["cameras"][0]["live"] is None


# ============================================== 10. close LIVE -> snapshot

def test_scenario_10_closing_live_returns_to_the_last_snapshot(env, monkeypatch):
    env(cameras=_cam(live_enabled=True, live_provider="youtube",
                     youtube_live_id=MOCK_YT_ID))
    html = _page(monkeypatch)
    js = re.search(r"<script>(.*?)</script></body></html>", html, re.S).group(1)
    m = re.search(r"function closeCamLive\(id\)\{(.*?)\n(?:async )?function", js, re.S)
    body = m.group(1)
    assert "stage.classList.remove('playing')" in body
    assert "player.classList.remove('playing')" in body
    assert "bar.hidden=true" in body
    # The snapshot is refreshed through the shared resolver, not a raw URL.
    assert "camBeginLoad(id)" in body
    assert "img.src=camSnapshotSrc(c, CAMS.stamp)" in body


def test_scenario_10_live_opens_only_on_an_explicit_click(env, monkeypatch):
    env(cameras=_cam(live_enabled=True, live_provider="youtube",
                     youtube_live_id=MOCK_YT_ID))
    html = _page(monkeypatch)
    # The iframe is created inside openCamLive and nowhere on page load.
    assert "function openCamLive(id){" in html
    assert 'onclick="openCamLive(' in html
    assert "youtube-nocookie.com/embed/" in html


# ============================================== security: no leakage anywhere

def test_private_source_never_reaches_any_camera_surface(env, monkeypatch):
    secret = "unit-test-pass-123"
    rtsp = "rtsp://cam-lan.internal:554/Streaming/Channels/101"
    env(cameras=_cam(), sources=json.dumps([{"id": "ilioupoli", "url": rtsp,
                                            "username": "viewer",
                                            "password": secret}]),
        allowed="cam-lan.internal", mock="ok")
    c = _client(monkeypatch)
    for path in ("/api/cameras", "/api/cameras/ilioupoli",
                 "/api/cameras/ilioupoli/snapshot", "/api/health"):
        body = c.get(path).text
        for leak in (secret, rtsp, "cam-lan.internal", "rtsp://"):
            assert leak not in body, f"{leak!r} leaked via {path}"


def test_no_audio_pipeline_exists_on_the_public_surface(env, monkeypatch):
    env(cameras=_cam(live_enabled=True, live_provider="youtube",
                     youtube_live_id=MOCK_YT_ID))
    html = _page(monkeypatch).lower()
    for banned in ("getusermedia(", "mediarecorder(", "<audio", "<video",
                   "rtsp://", "rtmp://", ".m3u8"):
        assert banned not in html
