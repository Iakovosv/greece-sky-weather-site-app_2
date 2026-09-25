"""Local/mock end-to-end tests for the camera UI flow.

These exercise the whole card lifecycle that the browser drives -- snapshot ->
LIVE -> YouTube player -> close LIVE -> back to the last snapshot -- without a
real camera, a real RTSP source or a streaming pipeline. Everything here is the
documented mock surface from `.env.example` ("κάμερες: local/mock states").

The checks fall into two groups:

* server truth: what the payload and the served page may contain, and what they
  must never contain (private source, credentials, internal hosts).
* UI contract: the structural guarantees the front end relies on so that no
  custom control ever sits over the official YouTube iframe, and that each
  configuration state maps to exactly one card state.
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

SECRET_PASS = "unit-test-pass-123"
SOURCE_URL = "rtsp://cam-lan.internal:554/Streaming/Channels/101"
INTERNAL_HOST = "cam-lan.internal"

MOCK_SNAPSHOT = "https://picsum.photos/seed/ilioupoli/800/450"
MOCK_YT_ID = "MOCKPUBLICID"


@pytest.fixture
def env(monkeypatch):
    """Reload the module per test, the same contract the other camera suites use."""
    def _apply(cameras=None, *, sources=None, allowed=None, **extra):
        for k, v in extra.items():
            monkeypatch.setenv(k, v)
        for key, value in (("WX_CAMERAS", cameras),
                           ("WX_CAMERA_SOURCES", sources),
                           ("WX_CAMERA_ALLOWED_HOSTS", allowed)):
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        importlib.reload(cams)
    yield _apply
    importlib.reload(cams)


def _cam(**over):
    cam = {"id": "ilioupoli", "name": "Ilioupoli Sky", "region": "Αττική",
           "lat": 37.9333, "lon": 23.75, "snapshot": MOCK_SNAPSHOT}
    cam.update(over)
    return json.dumps([cam])


@pytest.fixture
def tpl():
    """The served page. The card markup is built client-side from /api/cameras,
    so this template is configuration-independent -- the same bytes for every
    state. State-specific behaviour is asserted against the payload instead."""
    import app as app_module
    return TestClient(app_module.app).get("/").text


# ------------------------------------------------- (a) enabled + snapshot

def test_state_a_enabled_snapshot_is_live_and_public_safe(env):
    env(cameras=_cam())
    payload = cams.camera_payload(now=1000.0)
    assert payload["configured_count"] == 1
    assert payload["note"] is None
    cam = payload["cameras"][0]
    assert cam["status"] == "live"
    assert cam["snapshot"] == MOCK_SNAPSHOT
    assert cam["live"] is None                      # no live configured -> no player
    assert set(cam) <= {"id", "name", "region", "lat", "lon", "snapshot",
                        "timelapse", "note", "snapshot_interval_min", "status",
                        "has_timelapse", "live"}


def test_state_a_has_no_live_block_so_no_player_is_offered(env):
    env(cameras=_cam())
    cam = cams.camera_payload()["cameras"][0]
    # The LIVE button is gated on this server field, so None means no gate opens.
    assert cam["live"] is None
    assert "openCamLive('ilioupoli')" not in json.dumps(cam)


# ------------------------------------------------- (b) enabled, no snapshot

def test_state_b_missing_snapshot_is_not_configured(env):
    env(cameras=_cam(snapshot=None))
    payload = cams.camera_payload()
    assert payload["configured_count"] == 0
    cam = payload["cameras"][0]
    assert cam["status"] == "not_configured"
    assert cam["snapshot"] is None
    assert payload["note"] is not None


# ------------------------------------------------- (c) disabled

def test_state_c_disabled_camera_is_absent_and_404s(env, monkeypatch):
    env(cameras=json.dumps([{"id": "ilioupoli", "enabled": False,
                             "snapshot": MOCK_SNAPSHOT}]))
    import app as app_module
    client = TestClient(app_module.app)
    assert cams.camera_payload()["configured_count"] == 0
    assert cams.find_camera("ilioupoli") is None
    assert client.get("/api/cameras/ilioupoli").status_code == 404
    assert "ilioupoli" not in json.dumps(cams.camera_payload())


# ------------------------------------------------- (d) LIVE enabled + id

def test_state_d_live_block_appears_only_with_a_valid_id(env):
    env(cameras=_cam(live_enabled=True, live_provider="youtube",
                     youtube_live_id=MOCK_YT_ID))
    live = cams.camera_payload()["cameras"][0]["live"]
    assert live == {"provider": "youtube", "video_id": MOCK_YT_ID,
                    "privacy_enhanced": True}


def test_state_d_payload_publishes_the_live_block_the_button_gate_reads(env, tpl):
    env(cameras=_cam(live_enabled=True, live_provider="youtube",
                     youtube_live_id=MOCK_YT_ID))
    cam = cams.camera_payload()["cameras"][0]
    assert cam["live"] == {"provider": "youtube", "video_id": MOCK_YT_ID,
                           "privacy_enhanced": True}
    # The render template only emits the button when this block is present.
    assert "const golive = (live && c.live)" in tpl


# ------------------------------------------------- (e) LIVE unavailable provider

def test_state_e_unsupported_provider_yields_no_live_block(env):
    env(cameras=_cam(live_enabled=True, live_provider="rtsp"))
    assert cams.camera_payload()["cameras"][0]["live"] is None


# ------------------------------------------------- (f) LIVE enabled, bad id

@pytest.mark.parametrize("bad_id", [None, "", "not a valid id",
                                    "https://youtu.be/MOCKPUBLICID", "x"])
def test_state_f_missing_or_invalid_youtube_id_yields_no_live_block(env, bad_id):
    extra = {} if bad_id is None else {"youtube_live_id": bad_id}
    env(cameras=_cam(live_enabled=True, live_provider="youtube", **extra))
    assert cams.camera_payload()["cameras"][0]["live"] is None


# ------------------------------------------------- unknown / audio / private

def test_unknown_camera_is_rejected(env, monkeypatch):
    env(cameras=_cam())
    import app as app_module
    assert cams.find_camera("nope") is None
    assert cams.find_camera("") is None
    assert TestClient(app_module.app).get("/api/cameras/nope").status_code == 404


def test_audio_enabled_private_source_is_still_refused(env):
    env(sources=json.dumps([{"id": "ilioupoli", "url": SOURCE_URL,
                             "audio": True}]))
    assert cams.source_for("ilioupoli") is None


def test_private_source_never_reaches_payload_or_page(env, monkeypatch):
    env(cameras=_cam(live_enabled=True, live_provider="youtube",
                     youtube_live_id=MOCK_YT_ID),
        sources=json.dumps([{"id": "ilioupoli", "url": SOURCE_URL,
                             "username": "viewer", "password": SECRET_PASS,
                             "secret_ref": "WX_CAM_TEST_PASS"}]),
        allowed=INTERNAL_HOST, WX_CAM_TEST_PASS=SECRET_PASS)
    blob = json.dumps(cams.camera_payload())
    for leak in (SECRET_PASS, SOURCE_URL, INTERNAL_HOST, "rtsp://"):
        assert leak not in blob
    import app as app_module
    html = TestClient(app_module.app).get("/").text
    for leak in (SECRET_PASS, SOURCE_URL, INTERNAL_HOST, "rtsp://",
                 "WX_CAM_TEST_PASS"):
        assert leak not in html


def test_rendered_page_never_emits_the_mock_private_source(env, monkeypatch):
    """The mock states add snapshot URLs, never a source. This is the boundary
    the milestone must not erode: a configured source stays server-side."""
    env(cameras=_cam(live_enabled=True, live_provider="youtube",
                     youtube_live_id=MOCK_YT_ID),
        sources=json.dumps([{"id": "ilioupoli", "url": SOURCE_URL,
                             "username": "viewer", "password": SECRET_PASS}]),
        allowed=INTERNAL_HOST)
    import app as app_module
    html = TestClient(app_module.app).get("/").text
    assert SOURCE_URL not in html and SECRET_PASS not in html


# ------------------------------------ UI contract: no overlay over the iframe

def test_player_is_its_own_region_not_the_stage(tpl):
    assert ".cam .camplayer .camframe{position:absolute" in tpl
    assert ".cam .stage .camframe" not in tpl
    assert "stage.appendChild(f)" not in tpl


def test_stage_is_hidden_while_live_is_open(tpl):
    assert ".cam .stage.playing{display:none}" in tpl


def test_close_control_lives_in_the_bar_below_the_player(tpl):
    # The close button is appended to the bar (`bar`), never to the stage/player.
    assert "bar.appendChild(close)" in tpl
    assert "stage.appendChild(close)" not in tpl
    assert "player.appendChild(close)" not in tpl


def test_hidden_control_bar_cannot_leave_an_empty_strip(tpl):
    # Without this rule the author display:flex beats [hidden] and every card
    # would show an empty bar even when live is closed.
    assert ".cam .camctl[hidden]{display:none}" in tpl


def test_live_open_and_close_are_wired_to_the_same_card(tpl):
    for fn in ("function openCamLive(id)", "function closeCamLive(id)",
               "function youtubeEmbedUrl(videoId)", "function showLiveFallback(player,msg)"):
        assert fn in tpl
    # Close returns to the last snapshot: the stage loses `playing` and the image
    # is refreshed through the shared resolver (which picks direct vs server).
    assert "stage.classList.remove('playing')" in tpl
    assert "img.src=camSnapshotSrc(c, CAMS.stamp)" in tpl


# ------------------------------------------------- YouTube embed policy

def test_youtube_embed_url_is_privacy_enhanced_and_inline(tpl):
    assert "https://www.youtube-nocookie.com/embed/" in tpl
    assert "playsinline:'1'" in tpl
    assert "autoplay:'1'" in tpl
    # The iframe is only ever created by the explicit user action.
    assert "function openCamLive(id){" in tpl
    assert 'onclick="openCamLive(' in tpl


def test_no_audio_pipeline_is_introduced(tpl):
    """Audio stays prohibited server-side. The UI must not add a mic/recording
    surface to compensate on the client.

    The assertions target actual code constructs (API calls and media elements),
    not prose: the camera comments legitimately *name* RTSP/WebRTC while saying
    they are not used, and that documentation must not be rewritten to pass a
    test.
    """
    lowered = tpl.lower()
    for banned in ("getusermedia(", "mediarecorder(", "getdisplaymedia(",
                   "<audio", "new audio(", "mediastreamsource(", "webrtc://",
                   "rtsp://", "rtmp://", ".m3u8", "<video"):
        assert banned not in lowered


# ------------------------------------------------- responsive

def test_mobile_viewport_stacks_the_camera_grid_and_bar(tpl):
    assert "@media (max-width:640px)" in tpl
    assert ".camgrid{grid-template-columns:1fr" in tpl
    assert ".cam .camctl{justify-content:flex-start}" in tpl
