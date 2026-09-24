"""Security tests for the camera registry and its public/private boundary.

The camera module is the one place where the server holds a credential it must
never publish and an address it must never fetch on a caller's behalf. These
tests pin both properties, following the acceptance list in the brief:

credentials out of responses / page / logs; arbitrary URLs and SSRF refused;
unknown and disabled ids refused; public metadata still served; a missing secret
failing safely; the existing FREE/PRO gate not bypassed; the public YouTube id
served without the private config; the private source never returned; the
provider not client-selectable; a disabled provider yielding no live endpoint;
and the video-only policy.

They exercise the module directly and the HTTP surface through the real app,
because "the whitelist drops it on the way out" is only meaningful if the same
holds over HTTP.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cameras as cams  # noqa: E402


# Credentials that must never surface. Long, distinctive values so a substring
# search in a response or a log line is unambiguous.
SECRET_PASS = "S3cr3t-RTSP-Pass-d3adb33f"
RTSP_URL = "rtsp://cam-lan.internal:554/Streaming/Channels/101"
INTERNAL_HOST = "cam-lan.internal"


@pytest.fixture()
def env(monkeypatch):
    """Reload the module per test so env-driven config is read cleanly."""
    def _apply(cameras=None, sources=None, allowed=None, **extra):
        for k, v in extra.items():
            monkeypatch.setenv(k, v)
        if cameras is None:
            monkeypatch.delenv("WX_CAMERAS", raising=False)
        else:
            monkeypatch.setenv("WX_CAMERAS", cameras)
        if sources is None:
            monkeypatch.delenv("WX_CAMERA_SOURCES", raising=False)
        else:
            monkeypatch.setenv("WX_CAMERA_SOURCES", sources)
        if allowed is None:
            monkeypatch.delenv("WX_CAMERA_ALLOWED_HOSTS", raising=False)
        else:
            monkeypatch.setenv("WX_CAMERA_ALLOWED_HOSTS", allowed)
        importlib.reload(cams)
    yield _apply
    importlib.reload(cams)


def _public_cam(**over):
    cam = {"id": "ilioupoli", "name": "Ilioupoli Sky", "region": "Αττική",
           "lat": 37.9333, "lon": 23.75,
           "snapshot": "https://cam.example/latest.jpg"}
    cam.update(over)
    return cam


def _sources(**over):
    src = {"id": "ilioupoli", "url": RTSP_URL,
           "username": "viewer", "password": SECRET_PASS,
           "secret_ref": "WX_CAM_TEST_PASS"}
    src.update(over)
    return json.dumps([src])


# ------------------------------------------------- (1)(8)(11)(12) no leakage

def test_credentials_never_appear_in_the_public_payload(env):
    env(cameras=json.dumps([_public_cam(live_enabled=True, live_provider="youtube",
                                        youtube_live_id="AbCdEf12345")]),
        sources=_sources(), allowed=INTERNAL_HOST,
        WX_CAM_TEST_PASS=SECRET_PASS)
    blob = json.dumps(cams.camera_payload())
    assert SECRET_PASS not in blob
    assert RTSP_URL not in blob and INTERNAL_HOST not in blob
    assert "viewer" not in blob
    assert "secret_ref" not in blob and "WX_CAM_TEST_PASS" not in blob


def test_private_source_is_never_returned_by_a_public_call(env):
    """The private source exists and is reachable by the pipeline API only; the
    public accessors never carry it."""
    env(cameras=json.dumps([_public_cam()]), sources=_sources(),
        allowed=INTERNAL_HOST, WX_CAM_TEST_PASS=SECRET_PASS)
    detail = cams.find_camera("ilioupoli")
    assert detail is not None
    blob = json.dumps(detail)
    assert RTSP_URL not in blob and SECRET_PASS not in blob
    # ...and the pipeline door does have it, which is what makes the test real.
    src = cams.source_for("ilioupoli")
    assert src and src["url"] == RTSP_URL
    assert cams.resolve_secret(src) == SECRET_PASS


def test_credentials_never_appear_in_the_rendered_page(env):
    """No secret may reach the served HTML, even with a fully configured camera."""
    env(cameras=json.dumps([_public_cam(live_enabled=True, live_provider="youtube",
                                        youtube_live_id="AbCdEf12345")]),
        sources=_sources(), allowed=INTERNAL_HOST,
        WX_CAM_TEST_PASS=SECRET_PASS)
    import app as app_module
    html = TestClient(app_module.app).get("/").text
    assert SECRET_PASS not in html
    assert RTSP_URL not in html and INTERNAL_HOST not in html
    assert "WX_CAM_TEST_PASS" not in html


def test_a_private_key_added_to_camera_config_cannot_ride_along(env):
    """The whitelist is structural: an unknown key in WX_CAMERAS is dropped, not
    spread into the response."""
    env(cameras=json.dumps([_public_cam(url=RTSP_URL, password=SECRET_PASS)]))
    cam = cams.camera_payload()["cameras"][0]
    assert "url" not in cam and "password" not in cam
    assert SECRET_PASS not in json.dumps(cam)


def test_youtube_id_is_public_while_the_source_stays_private(env):
    """The public embed id is published; the connection config is not. This is
    the line the brief draws, so it gets its own test."""
    env(cameras=json.dumps([_public_cam(live_enabled=True, live_provider="youtube",
                                        youtube_live_id="AbCdEf12345")]),
        sources=_sources(), allowed=INTERNAL_HOST, WX_CAM_TEST_PASS=SECRET_PASS)
    cam = cams.camera_payload()["cameras"][0]
    assert cam["live"] == {"provider": "youtube", "video_id": "AbCdEf12345",
                           "privacy_enhanced": True}
    assert RTSP_URL not in json.dumps(cam)


# ------------------------------------------------------- (3) logging safety

def test_a_rejected_source_logs_no_credential(env, caplog):
    """A source with credentials embedded in its URL is rejected, and the log
    line carries a redacted host, never the userinfo or the URL."""
    caplog.set_level(logging.WARNING, logger="wx.cameras")
    a_url = f"rtsp://user:{SECRET_PASS}@cam-lan.internal:554/s1"
    env(sources=json.dumps([{"id": "ilioupoli", "url": a_url}]))
    cams._load_sources()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET_PASS not in text and "user:" not in text
    assert "/s1" not in text


def test_an_allowlist_rejection_logs_only_a_redacted_host(env, caplog):
    caplog.set_level(logging.WARNING, logger="wx.cameras")
    env(sources=_sources(), allowed="only-this-host.example")
    cams._load_sources()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET_PASS not in text and RTSP_URL not in text


# ------------------------------------------------------------- (4) bad input

def test_a_non_source_scheme_is_refused(env):
    env(sources=json.dumps([{"id": "ilioupoli", "url": "ftp://cam-lan.internal/x"}]))
    assert cams.source_for("ilioupoli") is None


def test_credentials_embedded_in_the_source_url_are_refused(env):
    """Userinfo in the URL is rejected so a credential cannot travel in a string
    that logging or an error might echo."""
    env(sources=json.dumps([{"id": "ilioupoli",
                             "url": f"rtsp://viewer:{SECRET_PASS}@cam-lan.internal/1"}]),
        allowed=INTERNAL_HOST)
    assert cams.source_for("ilioupoli") is None


def test_a_host_outside_the_allowlist_is_refused(env):
    env(sources=_sources(), allowed="other.example")
    assert cams.source_for("ilioupoli") is None


# ---------------------------------------------------- (15) video-only policy

def test_a_source_with_audio_is_refused(env):
    """Audio is not muted downstream; the source is rejected, so no audio track
    enters the pipeline at all."""
    env(sources=_sources(audio=True), allowed=INTERNAL_HOST, WX_CAM_TEST_PASS=SECRET_PASS)
    assert cams.source_for("ilioupoli") is None


def test_an_accepted_source_is_marked_video_only(env):
    env(sources=_sources(), allowed=INTERNAL_HOST, WX_CAM_TEST_PASS=SECRET_PASS)
    src = cams.source_for("ilioupoli")
    assert src and src["audio"] is False


# ------------------------------------------- (5) SSRF / no arbitrary fetching

def test_no_endpoint_accepts_a_url_or_can_be_asked_to_fetch(env):
    """There is no URL parameter to abuse: an arbitrary query string is ignored
    and the response is still the server-side configuration."""
    env(cameras=json.dumps([_public_cam()]))
    import app as app_module
    c = TestClient(app_module.app)
    r = c.get("/api/cameras", params={"url": "http://127.0.0.1:8000/"})
    assert r.status_code == 200
    assert "127.0.0.1" not in r.text
    # A camera detail endpoint exists, but its path segment is an id looked up in
    # config, never used to build a fetch target.
    r2 = c.get("/api/cameras/http://169.254.169.254/latest/meta-data/")
    assert r2.status_code == 404
    assert "169.254" not in r2.text


def test_the_source_store_is_not_reachable_from_any_route(env):
    """No route returns a private source, whatever id is asked for."""
    env(cameras=json.dumps([_public_cam()]), sources=_sources(),
        allowed=INTERNAL_HOST, WX_CAM_TEST_PASS=SECRET_PASS)
    import app as app_module
    c = TestClient(app_module.app)
    for path in ("/api/cameras", "/api/cameras/ilioupoli"):
        body = c.get(path).text
        assert SECRET_PASS not in body and RTSP_URL not in body


# ------------------------------------------------ (6)(7) id authorization

def test_an_unknown_camera_id_is_refused(env):
    env(cameras=json.dumps([_public_cam()]))
    import app as app_module
    c = TestClient(app_module.app)
    assert c.get("/api/cameras/nope").status_code == 404
    assert cams.find_camera("nope") is None


def test_a_disabled_camera_is_absent_from_the_list_and_the_detail(env):
    env(cameras=json.dumps([_public_cam(), _public_cam(id="off", enabled=False)]))
    import app as app_module
    c = TestClient(app_module.app)
    ids = {x["id"] for x in c.get("/api/cameras").json()["cameras"]}
    assert ids == {"ilioupoli"}
    assert c.get("/api/cameras/off").status_code == 404
    # and it cannot be used to reach a private source either
    assert cams.source_for("off") is None


def test_a_disabled_camera_source_is_not_reachable(env):
    """Even with a private source defined, a disabled camera exposes nothing."""
    env(cameras=json.dumps([_public_cam(enabled=False)]), sources=_sources(),
        allowed=INTERNAL_HOST, WX_CAM_TEST_PASS=SECRET_PASS)
    assert cams.find_camera("ilioupoli") is None
    assert cams.source_for("ilioupoli") is None


# ------------------------------------------- (9) missing config fails safe

def test_a_missing_secret_resolves_to_none_not_an_error(env):
    env(sources=_sources(secret_ref="WX_CAM_ABSENT_VAR"), allowed=INTERNAL_HOST)
    src = cams.source_for("ilioupoli")
    assert src is not None           # the source itself is valid
    assert cams.resolve_secret(src) is None  # the value is simply absent


def test_a_malformed_source_blob_yields_no_sources_not_an_exception(env):
    env(sources="{not json", allowed=INTERNAL_HOST)
    assert cams.source_for("ilioupoli") is None


def test_a_malformed_camera_blob_falls_back_to_defaults(env):
    env(cameras="{not json")
    payload = cams.camera_payload()
    assert payload["configured_count"] == 0
    assert {c["id"] for c in payload["cameras"]} == {"ilioupoli", "glinado"}


# ------------------------------------- (10) existing FREE/PRO gate intact

def test_the_camera_list_did_not_become_a_pro_endpoint(env):
    """Cameras stay available to FREE as before; the change must not have moved
    them behind the PRO gate, and equally must not have opened a PRO route."""
    env(cameras=json.dumps([_public_cam()]))
    import app as app_module
    c = TestClient(app_module.app)
    assert c.get("/api/cameras").status_code == 200          # still public
    assert c.get("/api/expert", params={"lat": 37.98, "lon": 23.72}).status_code == 403


def test_a_forged_token_still_grants_nothing(env):
    """Sanity: the entitlement model is untouched, so a forged token is still FREE."""
    env(cameras=json.dumps([_public_cam()]))
    import app as app_module
    c = TestClient(app_module.app)
    me = c.get("/api/me", headers={"X-WX-Token": "eyJ0aWVyIjoicHJvIn0.deadbeef"}).json()
    assert me["is_pro"] is False


# -------------------------------- (13)(14) provider cannot be chosen by client

def test_an_unsupported_provider_yields_no_live_block(env):
    env(cameras=json.dumps([_public_cam(live_enabled=True, live_provider="rtmp",
                                        youtube_live_id="AbCdEf12345")]))
    assert cams.camera_payload()["cameras"][0]["live"] is None


def test_live_enabled_without_an_id_yields_no_live_block(env):
    env(cameras=json.dumps([_public_cam(live_enabled=True, live_provider="youtube")]))
    assert cams.camera_payload()["cameras"][0]["live"] is None


def test_a_client_cannot_turn_live_on_for_a_camera_that_has_it_off(env):
    """There is no query flag, body or header that adds a live block; the only
    source of truth is the server-side configuration."""
    env(cameras=json.dumps([_public_cam()]))  # no live configured
    import app as app_module
    c = TestClient(app_module.app)
    for params in ({"live": "1"}, {"provider": "youtube"}, {"live_provider": "youtube"}):
        cam = c.get("/api/cameras/ilioupoli", params=params).json()
        assert cam["live"] is None


def test_a_malformed_youtube_id_yields_no_live_block(env):
    env(cameras=json.dumps([_public_cam(live_enabled=True, live_provider="youtube",
                                        youtube_live_id="https://youtu.be/x")]))
    assert cams.camera_payload()["cameras"][0]["live"] is None


# ------------------------------------------------- snapshot interval bounds

def test_snapshot_interval_is_whitelisted(env):
    env(cameras=json.dumps([_public_cam(snapshot_interval_min=7)]))
    assert cams.camera_payload()["cameras"][0]["snapshot_interval_min"] == 5
    env(cameras=json.dumps([_public_cam(snapshot_interval_min=15)]))
    assert cams.camera_payload()["cameras"][0]["snapshot_interval_min"] == 15


# --------------------------------------------------- health reports counts only

def test_health_camera_block_carries_counts_and_no_credentials(env):
    env(cameras=json.dumps([_public_cam(live_enabled=True, live_provider="youtube",
                                        youtube_live_id="AbCdEf12345")]),
        sources=_sources(), allowed=INTERNAL_HOST, WX_CAM_TEST_PASS=SECRET_PASS)
    h = cams.health()
    assert h["configured"] == 1 and h["snapshots"] == 1 and h["live"] == 1
    assert h["sources"] == 1 and h["allowed_hosts"] is True
    blob = json.dumps(h)
    assert SECRET_PASS not in blob and RTSP_URL not in blob and INTERNAL_HOST not in blob


def test_a_malformed_source_blob_is_reported_as_zero(env):
    env(cameras=json.dumps([_public_cam()]), sources="{not json")
    assert cams.health()["sources"] == 0


# ------------------------------ M1: a bad port must not crash redaction/health
#
# `_redact_url` runs on config that is *already being rejected*, either to build
# the log line or on the way out of `_load_sources`. If a field access inside it
# can raise, a config typo turns into a 500 on /api/health — the one endpoint a
# deploy check relies on. These pin the fail-safe behaviour.

def test_redact_url_survives_a_non_numeric_port():
    """A non-numeric port must yield a safe redacted string, not raise."""
    out = cams._redact_url("rtsp://viewer:" + SECRET_PASS + "@cam-lan.internal:bad/x")
    assert isinstance(out, str)
    assert SECRET_PASS not in out and "viewer" not in out and "/x" not in out


def test_redact_url_survives_an_out_of_range_port():
    out = cams._redact_url("rtsp://cam-lan.internal:99999/x")
    assert isinstance(out, str)
    assert "/x" not in out


def test_a_rejected_source_with_a_bad_port_leaks_nothing_to_the_log(env, caplog):
    """The rejection path is exactly where redaction runs; it must not raise and
    must not write the URL or the userinfo."""
    caplog.set_level(logging.WARNING, logger="wx.cameras")
    bad = f"rtsp://viewer:{SECRET_PASS}@cam-lan.internal:bad/x"
    env(sources=json.dumps([{"id": "ilioupoli", "url": bad}]), allowed=INTERNAL_HOST)
    assert cams.source_for("ilioupoli") is None          # rejected, no exception
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET_PASS not in text and "viewer" not in text and "/x" not in text


def test_health_stays_200_when_a_source_has_a_malformed_port(env):
    """End-to-end: a malformed source must degrade to counts, not a 500."""
    env(cameras=json.dumps([_public_cam()]),
        sources=json.dumps([{"id": "ilioupoli", "url": "rtsp://h:bad/x"}]),
        allowed=INTERNAL_HOST)
    import app as app_module
    r = TestClient(app_module.app).get("/api/health")
    assert r.status_code == 200
    body = r.text
    assert SECRET_PASS not in body and "rtsp://" not in body and ":bad" not in body

