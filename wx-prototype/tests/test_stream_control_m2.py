"""M1 lifecycle + M2 stream control plane: onboarding, state, limits, security.

One test per promise, so an erosion of any promise fails here:

* lifecycle is derived from config and reports only closed-vocabulary reasons;
* a disabled camera is visible to the operator but not startable;
* a camera is startable only at ``enabled`` (tested + live + valid id);
* the control plane separates desired from observed and never calls a start
  "live" before the worker says so;
* start/stop are idempotent, a second caller cannot create a second worker, and
  the max-active cap is enforced;
* a stale ``starting`` worker is decayed to ``error`` and stops being public-live;
* the public payload gains only a coarse boolean, and the admin surface leaks
  no URL, host, credential, command line or raw worker text.

Everything runs against the offline mock worker. No camera, RTSP URL, secret,
FFmpeg or network is involved anywhere in this file.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import camera_lifecycle as lifecycle
import cameras as cams
import stream_control as sc

PUBLIC_HTTPS = "https://cam.example/latest.jpg"
SECRET_PASS = "unit-test-pass-123"
INTERNAL_HOST = "cam-lan.internal"
SOURCE_URL = "rtsp://cam-lan.internal:554/Streaming/Channels/101"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point WX_DB at a fresh file and drive camera config like the other suites.

    Only the keys passed on each call are touched, so a test can call ``env`` a
    second time to change one env var (say the mock mode) without wiping the
    camera config it set up on the first call. Passing a key as ``None`` clears it.
    """
    monkeypatch.setenv("WX_DB", str(tmp_path / "stream-test.db"))
    _ALIASES = {"cameras": "WX_CAMERAS", "sources": "WX_CAMERA_SOURCES",
                "allowed": "WX_CAMERA_ALLOWED_HOSTS"}

    def _apply(**vars):
        for k, v in vars.items():
            name = _ALIASES.get(k, k)
            if v is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, v)
        importlib.reload(cams)
        importlib.reload(lifecycle)
        importlib.reload(sc)
        sc.init_db()

    yield _apply
    importlib.reload(cams)
    importlib.reload(lifecycle)
    importlib.reload(sc)


def _cam(**over):
    cam = {"id": "ilioupoli", "name": "Ilioupoli Sky", "region": "Αττική",
           "lat": 37.9333, "lon": 23.75, "snapshot": PUBLIC_HTTPS}
    cam.update(over)
    return cam


def _ready_env(env, **cam_over):
    """A camera that should reach lifecycle `enabled`."""
    cam = _cam(live_enabled=True, live_provider="youtube",
               youtube_live_id="MOCKPUBLICID")
    cam.update(cam_over)
    env(cameras=json.dumps([cam]),
        sources=json.dumps([{"id": "ilioupoli", "url": SOURCE_URL,
                             "username": "viewer",
                             "secret_ref": "WX_CAM_TEST_PASS"}]),
        allowed=INTERNAL_HOST, WX_CAM_TEST_PASS=SECRET_PASS,
        WX_STREAM_ENABLED="1")


# ------------------------------------------------------------------ M1 lifecycle

def test_lifecycle_disabled_before_anything_else(env):
    env(cameras=json.dumps([_cam(enabled=False)]))
    life = lifecycle.lifecycle_of("ilioupoli")
    assert life["state"] == "disabled"
    # Disabled short-circuits: no checks, no reason -- nothing to "fix".
    assert life["checks"] == [] and life["reason"] is None
    # And a disabled camera is still visible to the operator (unlike the public list).
    assert life is not None


def test_lifecycle_configured_when_no_source_is_attached(env):
    env(cameras=json.dumps([_cam()]))
    life = lifecycle.lifecycle_of("ilioupoli")
    assert life["state"] == "configured"
    assert life["reason"] == "source_absent"


def test_lifecycle_configured_when_a_check_fails(env):
    # Host is not on the allowlist -> fails closed, still `configured`.
    env(cameras=json.dumps([_cam()]),
        sources=json.dumps([{"id": "ilioupoli", "url": SOURCE_URL}]),
        allowed="other-host.internal")
    life = lifecycle.lifecycle_of("ilioupoli")
    assert life["state"] == "configured"
    assert life["reason"] == "host_not_allowlisted"


def test_lifecycle_tested_when_source_is_valid_but_live_is_off(env):
    env(cameras=json.dumps([_cam()]),
        sources=json.dumps([{"id": "ilioupoli", "url": SOURCE_URL,
                             "secret_ref": "WX_CAM_TEST_PASS"}]),
        allowed=INTERNAL_HOST, WX_CAM_TEST_PASS=SECRET_PASS)
    life = lifecycle.lifecycle_of("ilioupoli")
    assert life["state"] == "tested"
    assert all(c["ok"] for c in life["checks"])


def test_lifecycle_enabled_when_tested_and_live_is_configured(env):
    _ready_env(env)
    assert lifecycle.lifecycle_of("ilioupoli")["state"] == "enabled"


def test_unresolvable_credential_blocks_tested(env):
    env(cameras=json.dumps([_cam()]),
        sources=json.dumps([{"id": "ilioupoli", "url": SOURCE_URL,
                             "secret_ref": "WX_CAM_MISSING_PASS"}]),
        allowed=INTERNAL_HOST)
    life = lifecycle.lifecycle_of("ilioupoli")
    assert life["state"] == "configured"
    assert life["reason"] == "credential_missing"


def test_live_enabled_without_a_valid_id_fails_closed(env):
    """A half-configured live block must not pass as `tested`.

    Fail-safe: rather than calling the source "tested" and separately noting a bad
    live id, the camera stays at `configured` with the live reason, so it can
    never be startable on the strength of a broken live config.
    """
    _ready_env(env, youtube_live_id="not a valid id")
    life = lifecycle.lifecycle_of("ilioupoli")
    assert life["state"] == "configured"
    assert life["reason"] == "live_provider_invalid"


def test_lifecycle_reason_is_always_from_the_closed_vocabulary(env):
    """A reason can never be free text, however the config is malformed."""
    env(cameras=json.dumps([_cam()]),
        sources=json.dumps([{"id": "ilioupoli", "url": "ftp://x/y"}]))
    life = lifecycle.lifecycle_of("ilioupoli")
    assert life["reason"] in lifecycle.REASONS
    for c in life["checks"]:
        assert c["reason"] is None or c["reason"] in lifecycle.REASONS


def test_lifecycle_summary_counts_without_naming(env):
    env(cameras=json.dumps([_cam(enabled=False),
                            {"id": "glinado", "name": "G", "snapshot": PUBLIC_HTTPS}]))
    summary = lifecycle.summary()
    assert summary["disabled"] == 1
    assert set(summary) == set(lifecycle.STATES)


def test_lifecycle_of_an_unknown_id_is_none(env):
    env(cameras=json.dumps([_cam()]))
    assert lifecycle.lifecycle_of("nope") is None
    assert lifecycle.lifecycle_of("../../etc/passwd") is None


def test_is_startable_is_false_below_enabled(env):
    env(cameras=json.dumps([_cam()]))          # configured, not enabled
    ok, why = lifecycle.is_startable("ilioupoli")
    assert ok is False and why == "not_ready"


# ------------------------------------------------------- M2 state model & start

def test_start_moves_desired_and_observed_to_live(env):
    _ready_env(env)
    r = asyncio.run(sc.request_start("ilioupoli"))
    assert r["ok"] is True and r["reason"] == "started"
    assert r["state"]["desired"] == "running"
    assert r["state"]["observed"] == "live"
    assert sc.public_running("ilioupoli") is True


def test_a_start_request_is_not_live_until_the_worker_confirms(env):
    """The mandate: 'start requested' must never be reported as live."""
    _ready_env(env)
    # `deferred` keeps the worker in `starting` (accepted, not confirmed).
    env(WX_STREAM_MOCK="deferred")
    r = asyncio.run(sc.request_start("ilioupoli"))
    assert r["ok"] is True
    assert r["state"]["observed"] == "starting"
    assert sc.public_running("ilioupoli") is False


def test_start_is_refused_below_enabled_lifecycle(env):
    env(cameras=json.dumps([_cam()]), WX_STREAM_ENABLED="1")
    r = asyncio.run(sc.request_start("ilioupoli"))
    assert r["ok"] is False and r["reason"] == "not_ready"
    assert sc.public_running("ilioupoli") is False


def test_second_start_does_not_create_a_second_worker(env):
    _ready_env(env)
    first = asyncio.run(sc.request_start("ilioupoli"))
    second = asyncio.run(sc.request_start("ilioupoli"))
    assert first["reason"] == "started"
    assert second["ok"] is True and second["reason"] == "already_running"
    assert sc.active_count() == 1


def test_stop_is_idempotent(env):
    _ready_env(env)
    asyncio.run(sc.request_start("ilioupoli"))
    first = asyncio.run(sc.request_stop("ilioupoli"))
    second = asyncio.run(sc.request_stop("ilioupoli"))
    assert first["ok"] is True and first["state"]["observed"] == "stopped"
    assert second["reason"] == "already_stopped"
    assert sc.public_running("ilioupoli") is False


def test_worker_start_failure_records_error_and_reason(env):
    _ready_env(env)
    env(WX_STREAM_MOCK="fail")
    r = asyncio.run(sc.request_start("ilioupoli"))
    assert r["ok"] is False and r["reason"] == "worker_error"
    assert r["state"]["observed"] == "error"
    assert r["state"]["error_reason"] == "launch_failed"
    assert sc.public_running("ilioupoli") is False


def test_max_active_cap_refuses_a_second_camera(env):
    """A hard cap of 1: the second camera cannot occupy a worker slot."""
    env(cameras=json.dumps([
            _cam(id="ilioupoli", live_enabled=True, live_provider="youtube",
                 youtube_live_id="IDONE1"),
            dict(_cam(id="glinado", live_enabled=True, live_provider="youtube",
                      youtube_live_id="IDTWO1"))]),
        sources=json.dumps([
            {"id": "ilioupoli", "url": SOURCE_URL, "secret_ref": "WX_CAM_TEST_PASS"},
            {"id": "glinado", "url": SOURCE_URL, "secret_ref": "WX_CAM_TEST_PASS"}]),
        allowed=INTERNAL_HOST, WX_CAM_TEST_PASS=SECRET_PASS,
        WX_STREAM_ENABLED="1", WX_STREAM_MAX_ACTIVE="1")
    a = asyncio.run(sc.request_start("ilioupoli"))
    b = asyncio.run(sc.request_start("glinado"))
    assert a["ok"] is True
    assert b["ok"] is False and b["reason"] == "max_active"
    assert sc.active_count() == 1


def test_max_active_floor_of_one(env):
    """A nonsense value cannot open the cap."""
    env(cameras=json.dumps([_cam()]), WX_STREAM_MAX_ACTIVE="0")
    import config
    importlib.reload(config)
    assert config.stream_max_active() == 1
    env(WX_STREAM_MAX_ACTIVE="not-a-number")
    importlib.reload(config)
    assert config.stream_max_active() == 1


def test_stale_starting_decays_to_error_and_stops_being_public_live(env):
    """A wedged worker must not keep the UI claiming LIVE."""
    import time
    _ready_env(env)
    env(WX_STREAM_MOCK="deferred")
    asyncio.run(sc.request_start("ilioupoli"))
    # Drive the clock past the staleness window (real "now" plus a margin).
    later = time.time() + sc.STALE_AFTER_S + 10
    st = sc.status("ilioupoli", now=later)
    assert st["stale"] is True
    assert sc.public_running("ilioupoli", now=later) is False
    acted = sc.reconcile(now=later)
    assert "ilioupoli" in acted
    assert sc.status("ilioupoli")["observed"] == "error"
    assert sc.status("ilioupoli")["error_reason"] == "stale"


def test_sqlite_state_is_shared_not_process_local(env):
    """The state must be readable through a second connection (the M3 worker)."""
    _ready_env(env)
    asyncio.run(sc.request_start("ilioupoli"))
    import config
    con = sqlite3.connect(config.db_path())
    try:
        row = con.execute("SELECT desired, observed FROM stream_state").fetchone()
    finally:
        con.close()
    assert row == ("running", "live")


# ----------------------------------------------------- M2 API + security surface

def test_public_payload_gains_only_a_coarse_boolean(env, monkeypatch):
    """The public camera payload grows exactly one coarse key, nothing more."""
    _ready_env(env)
    import app as app_module
    from fastapi.testclient import TestClient
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    body = c.get("/api/cameras").json()
    allowed = {"id", "name", "region", "lat", "lon", "snapshot", "timelapse",
               "note", "snapshot_interval_min", "status", "has_timelapse", "live",
               "snapshot_via", "live_status"}
    for cam in body["cameras"]:
        assert set(cam) <= allowed
        assert set(cam["live_status"]) == {"running"}
        assert isinstance(cam["live_status"]["running"], bool)


def test_admin_streams_requires_the_operator_token(env, monkeypatch):
    _ready_env(env)
    import app as app_module
    from fastapi.testclient import TestClient
    monkeypatch.delenv("WX_ADMIN_TOKEN", raising=False)
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    assert c.get("/api/admin/streams").status_code == 503
    monkeypatch.setenv("WX_ADMIN_TOKEN", "operator-secret")
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    assert c.get("/api/admin/streams").status_code == 403
    assert c.get("/api/admin/streams",
                 headers={"X-WX-Admin": "operator-secret"}).status_code == 200


def test_admin_start_and_stop_via_the_operator_surface(env, monkeypatch):
    _ready_env(env)
    import app as app_module
    from fastapi.testclient import TestClient
    monkeypatch.setenv("WX_ADMIN_TOKEN", "operator-secret")
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    h = {"X-WX-Admin": "operator-secret"}
    r = c.post("/api/admin/streams/ilioupoli/start", headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True
    r = c.post("/api/admin/streams/ilioupoli/stop", headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True


def test_admin_stream_diagnostics_are_sanitized(env, monkeypatch):
    _ready_env(env)
    import app as app_module
    from fastapi.testclient import TestClient
    monkeypatch.setenv("WX_ADMIN_TOKEN", "operator-secret")
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    h = {"X-WX-Admin": "operator-secret"}
    c.post("/api/admin/streams/ilioupoli/start", headers=h)
    blob = json.dumps(c.get("/api/admin/streams", headers=h).json())
    for leak in (SECRET_PASS, SOURCE_URL, INTERNAL_HOST, "rtsp://", "554",
                 "ffmpeg", "/usr/", "pid"):
        assert leak not in blob.lower()


def test_admin_cameras_carries_lifecycle_but_no_secret(env, monkeypatch):
    _ready_env(env)
    import app as app_module
    from fastapi.testclient import TestClient
    monkeypatch.setenv("WX_ADMIN_TOKEN", "operator-secret")
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    r = c.get("/api/admin/cameras", headers={"X-WX-Admin": "operator-secret"})
    assert r.status_code == 200
    blob = json.dumps(r.json())
    assert '"lifecycle"' in blob and '"enabled"' in blob
    for leak in (SECRET_PASS, SOURCE_URL, INTERNAL_HOST):
        assert leak not in blob


def test_no_public_start_or_stop_endpoint_exists(env, monkeypatch):
    """The browser must have no direct control endpoint."""
    _ready_env(env)
    import app as app_module
    from fastapi.testclient import TestClient
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    assert c.post("/api/cameras/ilioupoli/live/start").status_code == 404
    assert c.post("/api/cameras/ilioupoli/live/stop").status_code == 404


def test_health_reports_streams_and_lifecycle_counts(env, monkeypatch):
    _ready_env(env)
    import app as app_module
    from fastapi.testclient import TestClient
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    j = c.get("/api/health").json()
    assert "streams" in j and "camera_lifecycle" in j
    assert j["streams"]["max_active"] >= 1
    blob = json.dumps(j["streams"])
    assert "ilioupoli" not in blob


def test_entitlement_flags_are_untouched_by_the_stream_plane(env):
    """The stream plane must not change FREE/PRO behaviour."""
    import entitlements as ent
    assert ent.FREE_HOURS == 72 and ent.PRO_HOURS == 240


def test_mock_worker_needs_no_network_or_secret(env, monkeypatch):
    """A start with NO private secret set still works under the mock worker."""
    env(cameras=json.dumps([_cam(live_enabled=True, live_provider="youtube",
                                 youtube_live_id="IDONLY1")]),
        sources=json.dumps([{"id": "ilioupoli", "url": "rtsp://cam-lan.internal/x"}]),
        allowed=INTERNAL_HOST, WX_STREAM_ENABLED="1")
    # lifecycle is `enabled` despite no resolvable credential (no secret_ref).
    assert lifecycle.lifecycle_of("ilioupoli")["state"] == "enabled"
    r = asyncio.run(sc.request_start("ilioupoli", worker=sc.MockStreamWorker("ok")))
    assert r["ok"] is True
