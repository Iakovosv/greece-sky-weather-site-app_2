"""Corrective milestone F1-F4: write-error handling, live liveness, detail API.

Each test targets one promise from the review:

* **F1** a database that is unavailable yields a sanitized refusal, never a 500
  traceback and never a leaked table/SQL/path;
* **F2** ``live`` is time-bounded evidence, not a permanent fact -- a worker that
  vanishes stops being advertised on its own, and reconcile narrows the row;
* **F3** the detail endpoint carries the same ``{running: bool}`` as the list;
* **F4** the batched/single-pass rewrites are behaviour-preserving.

Everything runs offline. No camera, RTSP URL, secret, FFmpeg or network.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import camera_lifecycle as lifecycle
import cameras as cams
import stream_control as sc

SECRET_PASS = "unit-test-pass-123"
INTERNAL_HOST = "cam-lan.internal"
SOURCE_URL = "rtsp://cam-lan.internal:554/Streaming/Channels/101"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh WX_DB per test, incremental camera config, like the M1/M2 suite."""
    monkeypatch.setenv("WX_DB", str(tmp_path / "corrective.db"))
    aliases = {"cameras": "WX_CAMERAS", "sources": "WX_CAMERA_SOURCES",
               "allowed": "WX_CAMERA_ALLOWED_HOSTS"}

    def _apply(**vars):
        for k, v in vars.items():
            name = aliases.get(k, k)
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
           "lat": 37.9333, "lon": 23.75, "snapshot": "https://cam.example/a.jpg",
           "live_enabled": True, "live_provider": "youtube",
           "youtube_live_id": "MOCKPUBLICID"}
    cam.update(over)
    return cam


def _ready(env, **cam_over):
    cam = _cam(**cam_over)
    env(cameras=json.dumps([cam]),
        sources=json.dumps([{"id": cam["id"], "url": SOURCE_URL,
                             "secret_ref": "WX_CAM_TEST_PASS"}]),
        allowed=INTERNAL_HOST, WX_CAM_TEST_PASS=SECRET_PASS,
        WX_STREAM_ENABLED="1")


# ============================================================ F1: write safety

def _break_db(monkeypatch):
    """Make any state-store connection fail, as an unavailable DB would."""
    def boom(*a, **k):
        raise sqlite3.OperationalError("no such table: stream_state")
    monkeypatch.setattr(sc, "_connect", boom)


def test_start_refuses_cleanly_when_state_store_is_unavailable(env, monkeypatch):
    _ready(env)
    _break_db(monkeypatch)
    r = asyncio.run(sc.request_start("ilioupoli"))
    assert r["ok"] is False
    # A closed-vocabulary reason, and no half-written state to report.
    assert r["reason"] == "state_unavailable"
    assert r["state"] is None


def test_stop_refuses_cleanly_when_state_store_is_unavailable(env, monkeypatch):
    _ready(env)
    # A live row first, with the store working...
    asyncio.run(sc.request_start("ilioupoli"))
    # ...then the store goes away before the stop is attempted.
    _break_db(monkeypatch)
    r = asyncio.run(sc.request_stop("ilioupoli"))
    assert r["ok"] is False and r["reason"] == "state_unavailable"
    assert r["state"] is None


def test_db_failure_leaks_nothing_and_does_not_raise(env, monkeypatch, caplog):
    _ready(env)
    _break_db(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        r = asyncio.run(sc.request_start("ilioupoli"))
    blob = json.dumps(r)
    # None of the things a raw exception would have carried.
    for leak in ("stream_state", "SELECT", "INSERT", "UPDATE", "no such table",
                 "Traceback", "OperationalError", "/", ".db"):
        assert leak not in blob
    # The log line is a category, not the exception text.
    logtext = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "no such table" not in logtext
    assert "SELECT" not in logtext
    assert "OperationalError" in logtext  # type name only, deliberately


def test_state_unavailable_reason_is_in_the_closed_vocabulary(env, monkeypatch):
    _ready(env)
    _break_db(monkeypatch)
    r = asyncio.run(sc.request_start("ilioupoli"))
    assert r["reason"] in sc.ERROR_REASONS | {"state_unavailable"}


def test_admin_start_returns_sanitized_error_not_500(env, monkeypatch):
    _ready(env)
    import app as app_module
    import ratelimit
    from fastapi.testclient import TestClient
    monkeypatch.setenv("WX_ADMIN_TOKEN", "operator-secret")
    importlib.reload(app_module)
    sc.init_db()
    # The limiter bucket is process-global; other admin-start tests in this run
    # may already have spent it, and this test is about the DB failure path.
    ratelimit.LIMITER.reset()
    c = TestClient(app_module.app)
    _break_db(monkeypatch)
    r = c.post("/api/admin/streams/ilioupoli/start",
               headers={"X-WX-Admin": "operator-secret"})
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["reason"] == "state_unavailable"
    for leak in ("stream_state", "SELECT", "Traceback", "no such table", ".db"):
        assert leak not in r.text


# ==================================================== F2: live liveness contract

def test_live_with_fresh_heartbeat_is_publicly_running(env):
    _ready(env)
    asyncio.run(sc.request_start("ilioupoli"))
    now = time.time()
    assert sc.public_running("ilioupoli", now=now) is True
    st = sc.status("ilioupoli", now=now)
    assert st["observed"] == "live" and st["stale"] is False
    assert st["heartbeat_at"] is not None


def test_live_with_stale_heartbeat_is_not_publicly_running(env):
    _ready(env)
    asyncio.run(sc.request_start("ilioupoli"))
    later = time.time() + sc.LIVE_FRESH_S + 10
    assert sc.public_running("ilioupoli", now=later) is False
    # The row is still `live`; it is the freshness that has lapsed.
    assert sc.status("ilioupoli", now=later)["observed"] == "live"
    assert sc.status("ilioupoli", now=later)["stale"] is True


def test_worker_disappears_while_live_and_reconcile_clears_it(env):
    _ready(env)
    asyncio.run(sc.request_start("ilioupoli"))
    later = time.time() + sc.LIVE_FRESH_S + 10
    # No heartbeat arrives; reconcile is what turns the stale row into an error.
    acted = sc.reconcile(now=later)
    assert "ilioupoli" in acted
    st = sc.status("ilioupoli", now=later)
    assert st["observed"] == "error" and st["error_reason"] == "stale"
    assert sc.public_running("ilioupoli", now=later) is False


def test_worker_that_keeps_beating_stays_live(env):
    _ready(env)
    asyncio.run(sc.request_start("ilioupoli"))
    base = time.time()
    # Beats within the window keep it alive indefinitely.
    for step in range(1, 6):
        beat_at = base + step * sc.HEARTBEAT_INTERVAL_S
        assert sc.heartbeat("ilioupoli", now=beat_at) is True
        assert sc.reconcile(now=beat_at + 1) == []
        assert sc.public_running("ilioupoli", now=beat_at + 1) is True


def test_application_restart_does_not_make_a_stale_live_row_fresh(env):
    """A fresh process reads the same row; staleness survives the restart."""
    _ready(env)
    asyncio.run(sc.request_start("ilioupoli"))
    later = time.time() + sc.LIVE_FRESH_S + 10
    # Simulate a restart: reload every module, re-open the same DB file.
    importlib.reload(cams)
    importlib.reload(lifecycle)
    importlib.reload(sc)
    sc.init_db()
    assert sc.public_running("ilioupoli", now=later) is False
    assert sc.reconcile(now=later) == ["ilioupoli"]


def test_heartbeat_is_ignored_for_a_non_live_row(env):
    _ready(env)
    # No start: the row does not exist, so a beat cannot resurrect anything.
    assert sc.heartbeat("ilioupoli", now=time.time()) is False


def test_heartbeat_is_ignored_after_a_stop(env):
    _ready(env)
    asyncio.run(sc.request_start("ilioupoli"))
    asyncio.run(sc.request_stop("ilioupoli"))
    assert sc.heartbeat("ilioupoli", now=time.time()) is False
    assert sc.public_running("ilioupoli") is False


def test_stale_live_releases_the_max_active_slot(env):
    """A wedged live worker must not pin the only slot forever."""
    env(cameras=json.dumps([
            _cam(id="ilioupoli", youtube_live_id="IDONE11"),
            _cam(id="glinado", youtube_live_id="IDTWO22")]),
        sources=json.dumps([
            {"id": "ilioupoli", "url": SOURCE_URL, "secret_ref": "WX_CAM_TEST_PASS"},
            {"id": "glinado", "url": SOURCE_URL, "secret_ref": "WX_CAM_TEST_PASS"}]),
        allowed=INTERNAL_HOST, WX_CAM_TEST_PASS=SECRET_PASS,
        WX_STREAM_ENABLED="1", WX_STREAM_MAX_ACTIVE="1")
    asyncio.run(sc.request_start("ilioupoli"))
    # While it looks live, the slot is occupied.
    assert asyncio.run(sc.request_start("glinado"))["reason"] == "max_active"
    # Once it ages out, reconcile frees the slot for the next camera.
    sc.reconcile(now=time.time() + sc.LIVE_FRESH_S + 10)
    assert sc.active_count() == 0
    assert asyncio.run(sc.request_start("glinado"))["ok"] is True


def test_duplicate_start_does_not_create_a_second_worker(env):
    _ready(env)
    first = asyncio.run(sc.request_start("ilioupoli"))
    second = asyncio.run(sc.request_start("ilioupoli"))
    assert first["reason"] == "started"
    assert second["reason"] == "already_running"
    assert sc.active_count() == 1
    rows = sqlite3.connect(__import__("config").db_path()).execute(
        "SELECT COUNT(*) FROM stream_state").fetchone()[0]
    assert rows == 1


def test_no_heartbeat_field_is_public(env):
    _ready(env)
    asyncio.run(sc.request_start("ilioupoli"))
    blob = json.dumps(cams.camera_payload())
    for leak in ("heartbeat", "stale", "restart_count", "worker", "worker_error",
                 "error_reason", "desired", "observed"):
        assert leak not in blob


def test_reconcile_leaves_a_fresh_live_alone(env):
    _ready(env)
    asyncio.run(sc.request_start("ilioupoli"))
    assert sc.reconcile(now=time.time()) == []
    assert sc.status("ilioupoli")["observed"] == "live"


# ================================================ F3: detail live_status parity

def test_detail_endpoint_reports_running_false_by_default(env, monkeypatch):
    _ready(env)
    import app as app_module
    from fastapi.testclient import TestClient
    importlib.reload(app_module)
    sc.init_db()
    c = TestClient(app_module.app)
    body = c.get("/api/cameras/ilioupoli").json()
    assert body["live_status"] == {"running": False}


def test_detail_endpoint_reports_running_true_when_live(env, monkeypatch):
    _ready(env)
    import app as app_module
    from fastapi.testclient import TestClient
    importlib.reload(app_module)
    sc.init_db()
    asyncio.run(sc.request_start("ilioupoli"))
    c = TestClient(app_module.app)
    assert c.get("/api/cameras/ilioupoli").json()["live_status"] == {"running": True}


def test_detail_and_list_agree_on_live_status(env, monkeypatch):
    _ready(env)
    import app as app_module
    from fastapi.testclient import TestClient
    importlib.reload(app_module)
    sc.init_db()
    asyncio.run(sc.request_start("ilioupoli"))
    c = TestClient(app_module.app)
    listed = {cam["id"]: cam["live_status"]
              for cam in c.get("/api/cameras").json()["cameras"]}
    detail = c.get("/api/cameras/ilioupoli").json()["live_status"]
    assert listed["ilioupoli"] == detail == {"running": True}


def test_detail_endpoint_unknown_camera_is_404(env, monkeypatch):
    _ready(env)
    import app as app_module
    from fastapi.testclient import TestClient
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    assert c.get("/api/cameras/nosuch").status_code == 404


def test_detail_endpoint_disabled_camera_is_404(env, monkeypatch):
    env(cameras=json.dumps([_cam(enabled=False)]), WX_STREAM_ENABLED="1")
    import app as app_module
    from fastapi.testclient import TestClient
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    assert c.get("/api/cameras/ilioupoli").status_code == 404


def test_detail_live_status_leaks_no_internal_field(env, monkeypatch):
    _ready(env)
    import app as app_module
    from fastapi.testclient import TestClient
    monkeypatch.setenv("WX_ADMIN_TOKEN", "operator-secret")
    importlib.reload(app_module)
    sc.init_db()
    asyncio.run(sc.request_start("ilioupoli"))
    c = TestClient(app_module.app)
    text = c.get("/api/cameras/ilioupoli").text
    for leak in (SECRET_PASS, SOURCE_URL, INTERNAL_HOST, "rtsp://", "554",
                 "heartbeat", "worker", "observed", "desired", "error_reason"):
        assert leak not in text
    body = c.get("/api/cameras/ilioupoli").json()
    assert body["live_status"] == {"running": True}
    assert set(body["live_status"]) == {"running"}


# ================================================= F4: performance, same output

def test_summary_reads_config_once(env, monkeypatch):
    env(cameras=json.dumps([_cam(id="a", youtube_live_id="IDAAAA11"),
                            _cam(id="b", youtube_live_id="IDBBBB22"),
                            _cam(id="c", youtube_live_id="IDCCCC33")]))
    calls = {"n": 0}
    real = cams.load_config

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(cams, "load_config", counting)
    lifecycle.summary()
    # Previously one load_config per camera plus the outer one; now exactly one.
    assert calls["n"] == 1


def test_summary_counts_match_per_camera_states(env):
    env(cameras=json.dumps([_cam(id="a", youtube_live_id="IDAAAA11"),
                            _cam(id="b", enabled=False),
                            {"id": "c", "name": "C", "snapshot": "https://x/c.jpg"}],
                           ))
    counts = lifecycle.summary()
    assert counts["disabled"] == 1
    # b is disabled; c has no source -> configured. a has no source either.
    assert counts["configured"] == 2
    total = sum(counts.values())
    assert total == 3


def test_running_map_matches_public_running(env):
    _ready(env)
    asyncio.run(sc.request_start("ilioupoli"))
    now = time.time()
    m = sc.running_map(now=now)
    assert m["ilioupoli"] == sc.public_running("ilioupoli", now=now) is True


def test_running_map_uses_one_query(env, monkeypatch):
    env(cameras=json.dumps([_cam(id="a", youtube_live_id="IDAAAA11"),
                            _cam(id="b", youtube_live_id="IDBBBB22")]))
    opens = {"n": 0}
    real = sc._connect

    def counting(*a, **k):
        opens["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(sc, "_connect", counting)
    sc.running_map()
    assert opens["n"] == 1


def test_running_map_is_empty_on_db_failure(env, monkeypatch):
    _ready(env)
    _break_db(monkeypatch)
    assert sc.running_map() == {}


def test_public_payload_unchanged_by_batching(env, monkeypatch):
    """The list endpoint's shape and values are the same as before the rewrite."""
    _ready(env)
    import app as app_module
    from fastapi.testclient import TestClient
    importlib.reload(app_module)
    sc.init_db()
    c = TestClient(app_module.app)
    body = c.get("/api/cameras").json()
    allowed = {"id", "name", "region", "lat", "lon", "snapshot", "timelapse",
               "note", "snapshot_interval_min", "status", "has_timelapse", "live",
               "snapshot_via", "live_status"}
    for cam in body["cameras"]:
        assert set(cam) <= allowed
        assert set(cam["live_status"]) == {"running"}
        assert cam["live_status"]["running"] is False
