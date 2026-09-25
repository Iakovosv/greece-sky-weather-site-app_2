"""M3-A: the real ingest worker, its process abstraction, and the command builder.

Everything here runs offline: the process factory is a fake, so no FFmpeg exists,
no camera is contacted, no secret is real and no stream is published. The fakes are
the point -- they let every branch (exit, crash, hang, SIGTERM ignored, kill) be
driven deterministically, and they let a test assert on the exact argv the
production path would execute.

The security tests are falsifiability tests: each one fails if a specific
protection is removed (the `-an` flag, the host allowlist, the per-camera mutex,
the heartbeat, the backoff, the redaction). A test that would pass with the
protection deleted is not a test of the protection.

Worker lifecycle is exercised through `stream_control.request_start` /
`request_stop` rather than by calling the worker directly, because that is the real
contract: the control plane creates the state row before the worker runs, and the
worker only ever writes observed onto it.
"""
import asyncio
import importlib
import inspect
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import camera_lifecycle as lifecycle  # noqa: E402
import cameras as cams          # noqa: E402
import ingest_command as ic     # noqa: E402
import ingest_process as ip     # noqa: E402
import ingest_worker as iw      # noqa: E402
import stream_control as sc     # noqa: E402

CAM_HOST = "cam-lan.internal"
CAM_PASS = "sup3r-secret-pass"
STREAM_KEY = "yt-stream-key-abc123"
INGEST_URL = f"rtmps://ingest.example.com/live2/{STREAM_KEY}"
RTSP = f"rtsp://{CAM_HOST}:554/Streaming/Channels/101"
PUBLIC_ID = "PUBLICID01"


def _cam(**over):
    cam = {"id": "ilioupoli", "name": "A", "region": "Attiki", "lat": 37.9,
           "lon": 23.7, "snapshot": "https://pub.example/a.jpg",
           "live_enabled": True, "live_provider": "youtube",
           "youtube_live_id": PUBLIC_ID}
    cam.update(over)
    return cam


def _source(**over):
    src = {"id": "ilioupoli", "url": RTSP, "username": "viewer",
           "secret_ref": "WX_CAMERA_ILIOUPOLI_PASS"}
    src.update(over)
    return src


def _reload():
    for mod in (cams, lifecycle, sc, ic, iw):
        importlib.reload(mod)
    sc.init_db()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A startable camera with a private source and a resolvable credential."""
    monkeypatch.setenv("WX_DB", str(tmp_path / "wx.db"))
    monkeypatch.setenv("WX_STREAM_ENABLED", "1")
    monkeypatch.setenv("WX_STREAM_MAX_ACTIVE", "1")
    monkeypatch.setenv("WX_CAMERA_ALLOWED_HOSTS", CAM_HOST)
    monkeypatch.setenv("WX_CAMERA_ILIOUPOLI_PASS", CAM_PASS)
    monkeypatch.setenv("WX_CAMERA_INGEST_REF", "WX_CAMERA_INGEST_URL")
    monkeypatch.setenv("WX_CAMERA_INGEST_URL", INGEST_URL)
    monkeypatch.setenv("WX_STREAM_SECRET_DIR", str(tmp_path / "stream-secrets"))
    monkeypatch.setenv("WX_CAMERAS", json.dumps([_cam()]))
    monkeypatch.setenv("WX_CAMERA_SOURCES", json.dumps([_source()]))
    _reload()
    yield
    _reload()


# ------------------------------------------------------------- fake processes

class FakeHandle(ip.ProcessHandle):
    """A process the test drives. ``behaviour`` decides how it ends."""

    def __init__(self, pid, behaviour="stays", exit_code=0):
        self.pid = pid
        self.behaviour = behaviour
        self._exit_code = exit_code
        self._ended = asyncio.Event()
        self.term = 0
        self.killed = 0
        self._dead = False
        if behaviour in ("exit_now", "crash_now"):
            self._dead = True
            self._exit_code = exit_code if behaviour == "exit_now" else -11
            self._ended.set()

    async def wait(self):
        if self.behaviour in ("exit_now", "crash_now"):
            return self._exit_code
        if self.behaviour == "hang":
            await asyncio.Event().wait()      # never returns
        # stays / graceful / ignores_term: alive until signalled. `graceful` ends
        # on terminate, `ignores_term` only on kill.
        await self._ended.wait()
        return self._exit_code

    def terminate(self):
        self.term += 1
        if self.behaviour == "graceful":
            self._dead = True
            self._ended.set()
        # "ignores_term": do nothing -- SIGKILL must follow

    def kill(self):
        self.killed += 1
        self._dead = True
        self._ended.set()

    def is_running(self):
        return not self._dead

    def returncode(self):
        return None if not self._dead else self._exit_code

    def stderr_tail(self):
        return ""


class FakeFactory(ip.ProcessFactory):
    """Hands out FakeHandles and records every spawn (argv included)."""

    def __init__(self, behaviours=None, default="stays"):
        self.behaviours = list(behaviours or [])
        self.default = default
        self.spawned = []
        self.handles = []

    async def spawn(self, argv, env):
        self.spawned.append(list(argv))
        behaviour = self.behaviours.pop(0) if self.behaviours else self.default
        if behaviour == "spawn_fail":
            raise ip.ProcessSpawnError("launch_failed")
        h = FakeHandle(pid=1000 + len(self.spawned), behaviour=behaviour)
        self.handles.append(h)
        return h


def _worker(factory, **kw):
    # A long beat interval by default: the supervisor marks `live` (with the seed
    # heartbeat) as soon as the process is up, so renewal is not needed for most
    # tests -- and keeping it rare means a shared `sleep` fake counts only backoff
    # waits, not beats. The cadence test sets a small one explicitly.
    kw.setdefault("heartbeat_interval_s", 100.0)
    # A short stop grace: the fake "stays" process never honours SIGTERM, so the
    # production default (seconds) would make every teardown wait it out. Tests
    # that assert on the escalation pass their own values.
    kw.setdefault("stop_grace_s", 0.05)
    return iw.RealStreamWorker(factory=factory, **kw)


async def _wait_until(pred, ticks=300):
    for _ in range(ticks):
        if pred():
            return True
        await asyncio.sleep(0)
    return pred()


# ================================================================ 1. dispatch

def test_backend_dispatch_mock(env, monkeypatch):
    monkeypatch.setenv("WX_STREAM_BACKEND", "mock")
    importlib.reload(sc)
    assert isinstance(sc.worker_for_backend(), sc.MockStreamWorker)


def test_backend_dispatch_real_returns_singleton(env, monkeypatch):
    monkeypatch.setenv("WX_STREAM_BACKEND", "real")
    importlib.reload(sc)
    a = sc.worker_for_backend()
    b = sc.worker_for_backend()
    assert isinstance(a, iw.RealStreamWorker)
    # The singleton is the per-camera mutex's home; two objects would mean two
    # processes for one camera.
    assert a is b


def test_backend_default_is_mock(env, monkeypatch):
    monkeypatch.delenv("WX_STREAM_BACKEND", raising=False)
    importlib.reload(sc)
    assert isinstance(sc.worker_for_backend(), sc.MockStreamWorker)


def test_unknown_backend_fails_closed_never_falls_back_to_mock(env, monkeypatch):
    async def go():
        monkeypatch.setenv("WX_STREAM_BACKEND", "totally-unknown")
        importlib.reload(sc)
        w = sc.worker_for_backend()
        # Mutation test: a silent fallback to mock would make this identity true
        # and hide a "real" deploy that is actually fake.
        assert not isinstance(w, sc.MockStreamWorker)
        with pytest.raises(sc.StreamWorkerError) as e:
            await w.start("ilioupoli")
        assert e.value.reason == "backend_unavailable"
    asyncio.run(go())


def test_unknown_backend_refused_through_request_start(env, monkeypatch):
    async def go():
        monkeypatch.setenv("WX_STREAM_BACKEND", "nope")
        importlib.reload(sc)
        sc.init_db()
        r = await sc.request_start("ilioupoli")
        assert r["ok"] is False and r["reason"] == "worker_error"
    asyncio.run(go())


# =========================================================== 2. command builder

def _store():
    return ic.SecretFileStore(os.environ["WX_STREAM_SECRET_DIR"])


def _cmd(**kw):
    return ic.build_command("ilioupoli", ic.resolved_source("ilioupoli"),
                            ingest_url=INGEST_URL, ffmpeg_bin="ffmpeg",
                            secret_store=_store(), **kw)


def test_command_is_video_only(env):
    cmd = _cmd()
    # Mutation test: remove `-an` or the explicit map and this fails.
    assert "-an" in cmd.argv
    assert cmd.argv[cmd.argv.index("-map") + 1] == "0:v:0"
    joined = " ".join(cmd.argv)
    assert "-acodec" not in joined and "-c:a" not in joined


def test_command_keeps_credentials_out_of_argv(env):
    """M3-B / L3: the RTSP credential and the stream key are not command-line args.

    Mutation test: reverting to `-i <url with userinfo>` (or a positional ingest
    URL carrying the key) puts them back in argv -- which is what any local user
    reads from /proc/<pid>/cmdline -- and this fails.
    """
    cmd = _cmd()
    joined = " ".join(cmd.argv)
    assert CAM_PASS not in joined
    assert "viewer@" not in joined and "viewer:" not in joined
    assert STREAM_KEY not in joined
    # The input is read from a file via FFmpeg's "argument from file" form.
    assert "-i" not in cmd.argv
    assert "-/i" in cmd.argv
    # ...and the destination is the bare host; app and key are options.
    assert cmd.argv[-1] == "rtmps://ingest.example.com"
    assert cmd.argv[cmd.argv.index("-rtmp_app") + 1] == "live2"
    assert "-/rtmp_playpath" in cmd.argv


def test_secret_files_hold_the_real_values_0600(env):
    import stat
    cmd = _cmd()
    assert cmd.secret_file is not None
    with open(cmd.secret_file, encoding="utf-8") as fh:
        assert fh.read() == f"rtsp://viewer:{CAM_PASS}@{CAM_HOST}:554/Streaming/Channels/101"
    mode = stat.S_IMODE(os.stat(cmd.secret_file).st_mode)
    assert mode == 0o600
    mode_dir = stat.S_IMODE(os.stat(os.path.dirname(cmd.secret_file)).st_mode)
    assert mode_dir == 0o700
    key_file = _store().path_for("ilioupoli", "key")
    with open(key_file, encoding="utf-8") as fh:
        assert fh.read() == STREAM_KEY


def test_command_has_no_recording_output(env):
    cmd = _cmd()
    assert cmd.argv[-1] == "rtmps://ingest.example.com"   # one output: the ingest
    assert cmd.argv.count("-f") == 1
    assert cmd.argv[cmd.argv.index("-f") + 1] == "flv"
    joined = " ".join(cmd.argv)
    assert "segment" not in joined and "tee" not in joined


def test_command_applies_host_allowlist_at_build_time(env, monkeypatch):
    monkeypatch.setenv("WX_CAMERA_ALLOWED_HOSTS", "some-other-host")
    importlib.reload(cams)
    # Mutation test: drop the allowlist check and this build succeeds.
    with pytest.raises(ic.CommandBuildError) as e:
        ic.build_command("ilioupoli", _source(), ingest_url=INGEST_URL)
    assert e.value.reason == "host_not_allowlisted"


def test_command_rejects_credentials_in_url(env, monkeypatch):
    # Allowlist the host so the credential check -- not the host check -- is the
    # one under test.
    monkeypatch.setenv("WX_CAMERA_ALLOWED_HOSTS", "host")
    importlib.reload(cams)
    with pytest.raises(ic.CommandBuildError) as e:
        ic.build_command("ilioupoli", {"url": "rtsp://user:pw@host/stream"},
                         ingest_url=INGEST_URL, secret_store=_store())
    assert e.value.reason == "source_scheme"


def test_command_rejects_audio_source(env):
    with pytest.raises(ic.CommandBuildError) as e:
        ic.build_command("ilioupoli", {"url": RTSP, "audio": True},
                         ingest_url=INGEST_URL, secret_store=_store())
    assert e.value.reason == "audio_not_allowed"


def test_command_requires_output(env):
    with pytest.raises(ic.CommandBuildError) as e:
        ic.build_command("ilioupoli", ic.resolved_source("ilioupoli"),
                         ingest_url=None, secret_store=_store())
    assert e.value.reason == "no_output"


def test_command_without_secret_store_fails_closed(env):
    # Mutation test: allowing a None store would mean building an `-i <url>`
    # command line with the credential in it.
    with pytest.raises(ic.CommandBuildError) as e:
        ic.build_command("ilioupoli", ic.resolved_source("ilioupoli"),
                         ingest_url=INGEST_URL)
    assert e.value.reason == "secret_store_unavailable"


def test_command_missing_credential_fails_closed(env, monkeypatch):
    monkeypatch.delenv("WX_CAMERA_ILIOUPOLI_PASS", raising=False)
    importlib.reload(cams)
    with pytest.raises(ic.CommandBuildError) as e:
        ic.build_command("ilioupoli", _source(), ingest_url=INGEST_URL,
                         secret_store=_store())
    assert e.value.reason == "credential_missing"


def test_resolved_source_absent(env):
    with pytest.raises(ic.CommandBuildError) as e:
        ic.resolved_source("no-such-camera")
    assert e.value.reason == "source_absent"


def test_injected_credential_is_percent_encoded(env):
    cmd = ic.build_command(
        "ilioupoli", {"url": RTSP, "username": "user@x", "password": "p:w/d"},
        ingest_url=INGEST_URL, secret_store=_store())
    # The encoding now lands in the secret file (FFmpeg reads it verbatim) rather
    # than in argv.
    assert "-/i" in cmd.argv
    with open(cmd.secret_file, encoding="utf-8") as fh:
        assert "user%40x:p%3Aw%2Fd@" in fh.read()


def test_summary_redacts_url_path_and_credentials(env):
    cmd = _cmd()
    # A path can be a token, so it is dropped rather than masked.
    assert "/Streaming/Channels" not in cmd.summary
    assert CAM_PASS not in cmd.summary
    assert STREAM_KEY not in cmd.summary
    assert cmd.summary == (
        f"ffmpeg in=rtsp://{CAM_HOST}:554 out=rtmps://ingest.example.com video-only")


def test_safe_argv_redacts_every_secret(env):
    cmd = _cmd()
    safe = " ".join(cmd.safe_argv())
    assert CAM_PASS not in safe
    assert STREAM_KEY not in safe
    # The real argv does not carry them either any more (M3-B) -- they live only
    # in the 0600 secret files -- so refusal-by-absence and redaction agree.
    real = " ".join(cmd.argv)
    assert CAM_PASS not in real and STREAM_KEY not in real


def test_scrub_removes_secrets_from_stderr_text(env):
    cmd = _cmd()
    text = f"Connection to {RTSP} failed; key {STREAM_KEY}; pw {CAM_PASS}"
    scrubbed = cmd.scrub(text)
    assert CAM_PASS not in scrubbed and STREAM_KEY not in scrubbed
    assert "<redacted>" in scrubbed


def test_repr_never_leaks_argv(env):
    cmd = _cmd()
    assert CAM_PASS not in repr(cmd) and STREAM_KEY not in repr(cmd)


def test_redact_url_drops_userinfo_path_query():
    assert ic.redact_url("rtsp://u:p@h:554/a/b?tok=1") == "rtsp://h:554"
    assert ic.redact_url("not a url") in ("<redacted>", "<unparseable>")


def test_child_env_withholds_project_secrets(env, monkeypatch):
    # Mutation test: `env = dict(os.environ)` would pass every one of these to the
    # child, giving a compromised media process the web app's own credentials.
    monkeypatch.setenv("WX_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_should_not_leak")
    monkeypatch.setenv("WX_CAMERA_INGEST_URL", INGEST_URL)
    cmd = _cmd()
    assert "WX_ADMIN_TOKEN" not in cmd.env
    assert "STRIPE_SECRET_KEY" not in cmd.env
    # And no value, under any key, equals one of them.
    assert "admin-secret" not in cmd.env.values()
    assert "sk_live_should_not_leak" not in cmd.env.values()
    # The handful of variables a media tool needs are still there.
    assert set(cmd.env) <= set(ic._ENV_PASSTHROUGH)


# ==================================================== 2b. M3-B output lock-down

def test_output_app_and_key_are_split_not_arbitrary(env):
    """The destination the builder emits is derived only from the resolved URL.

    Mutation test: emit the ingest URL as a positional argument (as M3-A did) and
    the stream key is back in argv; accept an arbitrary destination and this test
    cannot pin the shape.
    """
    cmd = _cmd()
    base, app, key = ic._split_output(INGEST_URL)
    assert base == "rtmps://ingest.example.com"
    assert app == "live2" and key == STREAM_KEY
    assert cmd.argv[-1] == base
    # No argument anywhere is an arbitrary absolute URL from a caller: the only
    # URL-shaped positional is the host the server itself resolved.
    assert [a for a in cmd.argv if a.startswith("rtmps://")] == [base]


def test_secret_store_refuses_symlinked_directory(env, tmp_path):
    # A symlinked directory would redirect the credential to wherever it points.
    real = tmp_path / "real"; real.mkdir()
    link = tmp_path / "link"; os.symlink(real, link)
    store = ic.SecretFileStore(str(link))
    with pytest.raises(OSError):
        store.write("ilioupoli", "secret")
    assert not any(real.iterdir())


def test_secret_store_replaces_a_planted_symlink_without_following_it(env, tmp_path):
    # A symlink at the *file* path must not let an attacker make FFmpeg read a
    # world-readable file: os.replace swaps the link for the real 0600 file, and
    # the victim keeps its original contents either way.
    store = ic.SecretFileStore(str(tmp_path / "d"))
    os.makedirs(store.directory, mode=0o700, exist_ok=True)
    victim = tmp_path / "victim.txt"; victim.write_text("original")
    link = store.path_for("ilioupoli")
    os.symlink(victim, link)
    store.write("ilioupoli", "secret")
    assert victim.read_text() == "original"
    assert not os.path.islink(store.path_for("ilioupoli"))


def test_secret_store_filename_is_not_a_path(env):
    store = ic.SecretFileStore("/tmp/does-not-matter")
    p = store.path_for("../../etc/passwd")
    assert os.path.basename(p) == "etcpasswd-in.url"
    assert ".." not in p and os.sep not in os.path.basename(p)


def test_stop_removes_the_secret_files(env):
    async def go():
        f = FakeFactory(default="stays")
        w = _worker(f)
        await sc.request_start("ilioupoli", worker=w)
        store = w.secret_store
        assert os.path.exists(store.path_for("ilioupoli"))
        await sc.request_stop("ilioupoli", worker=w)
        # The credential file must not outlive the stream.
        assert not os.path.exists(store.path_for("ilioupoli"))
        assert not os.path.exists(store.path_for("ilioupoli", "key"))
    asyncio.run(go())


def test_spawn_failure_leaves_no_secret_file(env):
    """A failed spawn must not strand the credential file the builder wrote."""
    async def go():
        f = FakeFactory(behaviours=["spawn_fail"])
        w = _worker(f)
        r = await sc.request_start("ilioupoli", worker=w)
        assert r["ok"] is False
        store = w.secret_store
        assert not os.path.exists(store.path_for("ilioupoli"))
        assert not os.path.exists(store.path_for("ilioupoli", "key"))
    asyncio.run(go())


def test_supervisor_exit_removes_the_secret_files_on_crash_loop(env, monkeypatch):
    async def go():
        monkeypatch.setattr(iw, "MAX_CONSECUTIVE_RESTARTS", 1)
        f = FakeFactory(default="crash_now")
        w = _worker(f, launch_grace_s=0.0,
                    sleep=lambda s: asyncio.sleep(0))
        await sc.request_start("ilioupoli", worker=w)
        store = w.secret_store
        # The supervisor's terminal path (here: crash-loop exhausted) must remove
        # the credential files. Wait for the removal itself -- the property under
        # test -- rather than for a state write that precedes the cleanup.
        await _wait_until(lambda: not os.path.exists(store.path_for("ilioupoli")))
        assert not os.path.exists(store.path_for("ilioupoli"))
        assert not os.path.exists(store.path_for("ilioupoli", "key"))
    asyncio.run(go())


def test_real_worker_activation_off_means_no_process(env, monkeypatch):
    """WX_STREAM_ENABLED=0 refuses at the endpoint before any worker runs.

    Mutation test: drop the `stream_enabled` guard in the start endpoint and a
    real backend would be reached with the feature off.
    """
    monkeypatch.setenv("WX_STREAM_ENABLED", "0")
    monkeypatch.setenv("WX_ADMIN_TOKEN", "operator-secret")
    import app as app_module
    from fastapi.testclient import TestClient
    c = TestClient(app_module.app)
    h = {"X-WX-Admin": "operator-secret"}
    r = c.post("/api/admin/streams/ilioupoli/start", headers=h)
    # The endpoint refuses with 503 when the control plane is off, so no process
    # is ever spawned and live_status stays false.
    assert r.status_code == 503
    assert c.get("/api/cameras/ilioupoli").json()["live_status"]["running"] is False


# ==================================================== 2c. deploy readiness probe

def test_activation_ready_reports_mock_backend_without_touching_disk(env, monkeypatch):
    monkeypatch.setenv("WX_STREAM_BACKEND", "mock")
    importlib.reload(sc)
    assert sc.activation_ready() == {"ready": False, "reason": "backend_not_real"}


def test_activation_ready_needs_ffmpeg_and_a_destination(env, monkeypatch):
    monkeypatch.setenv("WX_STREAM_BACKEND", "real")
    monkeypatch.setenv("WX_CAMERA_INGEST_REF", "WX_CAMERA_INGEST_URL")
    monkeypatch.delenv("WX_CAMERA_INGEST_URL", raising=False)
    importlib.reload(sc)
    # The fixture has a startable camera but no real ingest destination, so a
    # start would be refused: `ready` is false and the camera is counted, not
    # named.
    r = sc.activation_ready()
    assert r["ready"] is False
    assert "ilioupoli" not in json.dumps(r)
    # Give it a destination and, if FFmpeg happens to be present, it is ready.
    monkeypatch.setenv("WX_CAMERA_INGEST_URL", INGEST_URL)
    importlib.reload(sc)
    r2 = sc.activation_ready()
    assert set(r2) <= {"ready", "reason", "unconfigured_cameras"}
    assert INGEST_URL not in json.dumps(r2) and STREAM_KEY not in json.dumps(r2)


def test_health_streams_block_exposes_readiness_but_no_secret(env, monkeypatch):
    monkeypatch.setenv("WX_STREAM_BACKEND", "real")
    monkeypatch.setenv("WX_STREAM_ENABLED", "1")
    monkeypatch.setenv("WX_ADMIN_TOKEN", "operator-secret")
    import app as app_module
    from fastapi.testclient import TestClient
    c = TestClient(app_module.app)
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()["streams"]
    assert "ready" in body
    assert INGEST_URL not in r.text and STREAM_KEY not in r.text and CAM_PASS not in r.text


# ======================================================== 3. worker start/stop
def test_real_worker_start_returns_starting(env):
    async def go():
        f = FakeFactory(default="stays")
        w = _worker(f)
        r = await sc.request_start("ilioupoli", worker=w)
        assert r["ok"] is True and r["reason"] == "starting"
        assert len(f.spawned) == 1 and w.active_jobs() == 1
        await w.shutdown()
    asyncio.run(go())


def test_real_worker_start_writes_live_and_heartbeat(env):
    async def go():
        f = FakeFactory(default="stays")
        w = _worker(f, heartbeat_interval_s=0.02)
        await sc.request_start("ilioupoli", worker=w)
        await asyncio.sleep(0.08)
        st = sc.status("ilioupoli")
        assert st["observed"] == "live"
        assert sc.public_running("ilioupoli") is True
        await w.shutdown()
    asyncio.run(go())


def test_real_worker_stop_marks_stopped_and_is_idempotent(env):
    async def go():
        f = FakeFactory(default="graceful")
        w = _worker(f)
        await sc.request_start("ilioupoli", worker=w)
        await sc.request_stop("ilioupoli", worker=w)
        await sc.request_stop("ilioupoli", worker=w)   # no error, no new spawn
        assert w.active_jobs() == 0
        assert len(f.spawned) == 1
        assert sc.status("ilioupoli")["observed"] == "stopped"
        assert sc.public_running("ilioupoli") is False
    asyncio.run(go())


def test_real_worker_second_start_is_idempotent_no_second_process(env):
    async def go():
        f = FakeFactory(default="stays")
        w = _worker(f)
        await sc.request_start("ilioupoli", worker=w)
        r = await sc.request_start("ilioupoli", worker=w)
        assert r["reason"] == "already_running"
        assert len(f.spawned) == 1              # mutation: a weak mutex gives 2
        await w.shutdown()
    asyncio.run(go())


def test_concurrent_same_camera_start_spawns_one_process(env):
    async def go():
        f = FakeFactory(default="stays")
        w = _worker(f)
        await asyncio.gather(w.start("ilioupoli"), w.start("ilioupoli"),
                             w.start("ilioupoli"))
        # The per-camera mutex: three concurrent starts, exactly one process.
        assert len(f.spawned) == 1
        await w.shutdown()
    asyncio.run(go())


def test_stop_during_start_leaves_no_orphan(env):
    async def go():
        f = FakeFactory(default="graceful")
        w = _worker(f)
        await w.start("ilioupoli")
        await w.stop("ilioupoli")
        assert w.active_jobs() == 0 and len(f.spawned) == 1
        # Every handle the factory made was terminated, not abandoned.
        assert all(h.term >= 1 or h.killed >= 1 or not h.is_running()
                   for h in f.handles)
    asyncio.run(go())


def test_start_refuses_non_startable_camera(env, monkeypatch):
    async def go():
        monkeypatch.setenv("WX_CAMERAS", json.dumps([_cam(enabled=False)]))
        importlib.reload(cams)
        importlib.reload(lifecycle)
        f = FakeFactory(default="stays")
        w = _worker(f)
        with pytest.raises(sc.StreamWorkerError) as e:
            await w.start("ilioupoli")
        assert e.value.reason == "not_startable"
        assert f.spawned == []
    asyncio.run(go())


def test_start_without_source_is_refused_and_sanitized(env, monkeypatch):
    async def go():
        monkeypatch.delenv("WX_CAMERA_SOURCES", raising=False)
        importlib.reload(cams)
        importlib.reload(lifecycle)
        f = FakeFactory(default="stays")
        w = _worker(f)
        # Without a source the camera is not `enabled`, so the lifecycle gate
        # refuses before the worker -- a sanitized reason, not free text.
        with pytest.raises(sc.StreamWorkerError) as e:
            await w.start("ilioupoli")
        assert e.value.reason in sc.ERROR_REASONS
        assert " " not in e.value.reason
        assert f.spawned == []
    asyncio.run(go())


def test_spawn_failure_is_launch_failed(env):
    async def go():
        f = FakeFactory(behaviours=["spawn_fail"])
        w = _worker(f)
        with pytest.raises(sc.StreamWorkerError) as e:
            await w.start("ilioupoli")
        assert e.value.reason == "launch_failed"
        assert w.active_jobs() == 0
    asyncio.run(go())


def test_ingest_ref_absent_refuses_with_no_output(env, monkeypatch):
    async def go():
        monkeypatch.delenv("WX_CAMERA_INGEST_REF", raising=False)
        importlib.reload(iw)
        f = FakeFactory(default="stays")
        w = _worker(f)
        with pytest.raises(sc.StreamWorkerError) as e:
            await w.start("ilioupoli")
        assert e.value.reason == "no_output"
        assert f.spawned == []
    asyncio.run(go())


# ================================================= 4. process lifecycle/terminate

def test_sigterm_clean_exit(env):
    async def go():
        f = FakeFactory(default="graceful")
        w = _worker(f, stop_grace_s=0.5, kill_grace_s=0.5)
        await sc.request_start("ilioupoli", worker=w)
        await sc.request_stop("ilioupoli", worker=w)
        assert f.handles[0].term == 1 and f.handles[0].killed == 0
        assert sc.status("ilioupoli")["observed"] == "stopped"
    asyncio.run(go())


def test_sigkill_fallback_when_sigterm_ignored(env):
    async def go():
        f = FakeFactory(default="ignores_term")
        w = _worker(f, stop_grace_s=0.02, kill_grace_s=0.5)
        await sc.request_start("ilioupoli", worker=w)
        await sc.request_stop("ilioupoli", worker=w)
        # SIGTERM was sent, ignored, then SIGKILL: the row is stopped even though
        # the process refused to die politely.
        assert f.handles[0].term == 1 and f.handles[0].killed == 1
        assert sc.status("ilioupoli")["observed"] == "stopped"
        assert w.active_jobs() == 0
    asyncio.run(go())


def test_hung_process_stop_escalates(env):
    async def go():
        f = FakeFactory(default="hang")
        w = _worker(f, stop_grace_s=0.02, kill_grace_s=0.5)
        await sc.request_start("ilioupoli", worker=w)
        await sc.request_stop("ilioupoli", worker=w)
        assert w.active_jobs() == 0
        assert sc.status("ilioupoli")["observed"] == "stopped"
    asyncio.run(go())


def test_graceful_shutdown_stops_all(env):
    async def go():
        f = FakeFactory(default="graceful")
        w = _worker(f)
        await sc.request_start("ilioupoli", worker=w)
        await w.shutdown()
        assert w.active_jobs() == 0
        assert sc.status("ilioupoli")["observed"] == "stopped"
    asyncio.run(go())


# ============================================================ 5. crash + backoff

def test_process_crash_then_restart_respawns(env):
    async def go():
        f = FakeFactory(behaviours=["crash_now"], default="stays")
        w = _worker(f, launch_grace_s=0.0, sleep=lambda s: asyncio.sleep(0))
        await sc.request_start("ilioupoli", worker=w)
        await _wait_until(lambda: len(f.spawned) >= 2)
        assert len(f.spawned) >= 2          # one crash produced one respawn
        await w.shutdown()
    asyncio.run(go())


def test_rapid_crashes_do_not_tight_loop(env):
    async def go():
        delays = []

        async def fake_sleep(s):
            delays.append(s)

        f = FakeFactory(default="crash_now")
        w = _worker(f, sleep=fake_sleep, rand=0.5, launch_grace_s=0.0)
        await w.start("ilioupoli")
        await _wait_until(lambda: len(delays) >= 4)
        await w.shutdown()
        # Mutation test: remove the backoff sleep and these stay near zero.
        assert len(delays) >= 3
        assert delays[0] < delays[1] < delays[2]     # growth, not a tight loop
    asyncio.run(go())


def test_circuit_breaker_bounds_retries(env):
    async def go():
        async def fake_sleep(s):
            await asyncio.sleep(0)

        f = FakeFactory(default="crash_now")
        w = _worker(f, sleep=fake_sleep, launch_grace_s=999.0)
        # Start through the control plane so the state row exists for the worker
        # to write its observed outcome onto.
        await sc.request_start("ilioupoli", worker=w)
        await _wait_until(
            lambda: w.active_jobs() == 0
            and sc.status("ilioupoli") and sc.status("ilioupoli")["observed"] == "error")
        # Bounded: the breaker opens instead of retrying forever.
        assert len(f.spawned) == iw.MAX_CONSECUTIVE_RESTARTS + 1
        assert sc.status("ilioupoli")["error_reason"] == "worker_unavailable"
        await w.shutdown()
    asyncio.run(go())


def test_backoff_growth_and_jitter_bounds():
    lo = [iw.backoff_delay(i, rand=0.0) for i in range(1, 10)]
    hi = [iw.backoff_delay(i, rand=1.0) for i in range(1, 10)]
    assert lo[0] == pytest.approx(iw.BACKOFF_INITIAL_S * (1 - iw.BACKOFF_JITTER))
    # Non-decreasing to the cap, and never above cap+jitter.
    for i in range(1, len(lo)):
        assert lo[i] >= lo[i - 1] - 1e-9 or lo[i] == pytest.approx(
            iw.BACKOFF_MAX_S * (1 - iw.BACKOFF_JITTER))
    assert max(hi) <= iw.BACKOFF_MAX_S * (1 + iw.BACKOFF_JITTER) + 1e-9
    # Jitter actually varies the delay for a fixed attempt (mutation: remove it).
    assert iw.backoff_delay(3, rand=0.0) != iw.backoff_delay(3, rand=1.0)


def test_live_without_heartbeat_goes_stale(env):
    async def go():
        # A row that reached live but whose worker stopped beating (it died while
        # looking healthy) must decay: public_running goes false and reconcile
        # narrows it to error/stale, releasing the max-active slot.
        await sc.request_start("ilioupoli", worker=sc.MockStreamWorker("ok"))
        assert sc.public_running("ilioupoli") is True
        far = time.time() + sc.LIVE_FRESH_S + 5
        assert sc.public_running("ilioupoli", now=far) is False
        assert sc.status("ilioupoli", now=far)["stale"] is True
        assert "ilioupoli" in sc.reconcile(now=far)
        assert sc.status("ilioupoli")["observed"] == "error"
        assert sc.status("ilioupoli")["error_reason"] == "stale"
    asyncio.run(go())


def test_heartbeat_cadence_repeats_while_live(env, monkeypatch):
    async def go():
        beats = []
        real = sc.heartbeat
        monkeypatch.setattr(sc, "heartbeat",
                            lambda cam, now=None: beats.append(cam) or real(cam, now))
        f = FakeFactory(default="stays")
        w = _worker(f, heartbeat_interval_s=0.01)
        await sc.request_start("ilioupoli", worker=w)
        await asyncio.sleep(0.12)
        # Mutation test: without the beat loop only the seed beat exists, so the
        # count would be 0 here (the seed is written by the control plane).
        assert len(beats) >= 2
        assert set(beats) == {"ilioupoli"}
        assert sc.status("ilioupoli")["observed"] == "live"
        await w.shutdown()
    asyncio.run(go())


# ============================================================ 6. B5 disable live

def test_supervisor_does_not_restart_disabled_camera(env, monkeypatch):
    async def go():
        f = FakeFactory(default="crash_now")
        w = _worker(f, launch_grace_s=0.0, sleep=lambda s: asyncio.sleep(0))
        calls = {"n": 0}

        def flaky(cam_id):
            calls["n"] += 1
            # The first check (inside start) passes; every check after the crash
            # reports the camera was switched off.
            return calls["n"] == 1

        monkeypatch.setattr(iw, "_still_startable", flaky)
        # Through the control plane, so the row the worker writes onto exists.
        await sc.request_start("ilioupoli", worker=w)
        await _wait_until(
            lambda: sc.status("ilioupoli") and sc.status("ilioupoli")["observed"] == "error")
        # Mutation test: without the lifecycle re-check the crash path respawns.
        assert len(f.spawned) == 1
        assert calls["n"] >= 2
        assert sc.status("ilioupoli")["error_reason"] == "not_startable"
        await w.shutdown()
    asyncio.run(go())


def test_drain_disabled_stops_running_camera(env, monkeypatch):
    async def go():
        await sc.request_start("ilioupoli", worker=sc.MockStreamWorker("ok"))
        assert sc.public_running("ilioupoli") is True
        monkeypatch.setenv("WX_CAMERAS", json.dumps([_cam(enabled=False)]))
        importlib.reload(cams)
        importlib.reload(lifecycle)
        importlib.reload(sc)
        acted = sc.reconcile()
        assert "ilioupoli" in acted
        assert sc.status("ilioupoli")["observed"] == "stopped"
        assert sc.public_running("ilioupoli") is False
        assert sc.active_count() == 0        # slot released for reuse
    asyncio.run(go())


# ===================================================== 7. max-active integration

def test_max_active_enforced_with_real_worker(env, monkeypatch):
    async def go():
        monkeypatch.setenv("WX_CAMERAS", json.dumps([
            _cam(), _cam(id="glinado", name="B", youtube_live_id="PUBLICID02")]))
        monkeypatch.setenv("WX_CAMERA_SOURCES", json.dumps([
            _source(), _source(id="glinado")]))
        importlib.reload(cams)
        importlib.reload(lifecycle)
        importlib.reload(sc)
        sc.init_db()
        f = FakeFactory(default="stays")
        w = _worker(f)
        r1 = await sc.request_start("ilioupoli", worker=w)
        r2 = await sc.request_start("glinado", worker=w)
        assert r1["ok"] is True
        assert r2["ok"] is False and r2["reason"] == "max_active"
        assert len(f.spawned) == 1
        await w.shutdown()
    asyncio.run(go())


# ======================================================= 8. no secret anywhere

def test_no_secret_in_logs_when_start_fails(env, caplog):
    async def go():
        with caplog.at_level("DEBUG"):
            f = FakeFactory(behaviours=["spawn_fail"])
            w = _worker(f)
            with pytest.raises(sc.StreamWorkerError):
                await w.start("ilioupoli")
    asyncio.run(go())
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert CAM_PASS not in blob and STREAM_KEY not in blob and RTSP not in blob


def test_no_secret_in_admin_payload(env, monkeypatch):
    import app as app_module
    from fastapi.testclient import TestClient
    monkeypatch.setenv("WX_ADMIN_TOKEN", "operator-secret")
    importlib.reload(app_module)
    sc.init_db()
    c = TestClient(app_module.app)
    r = c.get("/api/admin/streams", headers={"X-WX-Admin": "operator-secret"})
    assert r.status_code == 200
    assert CAM_PASS not in r.text and STREAM_KEY not in r.text
    assert RTSP not in r.text and CAM_HOST not in r.text


def test_no_secret_in_public_api(env):
    import app as app_module
    from fastapi.testclient import TestClient
    c = TestClient(app_module.app)
    r = c.get("/api/cameras")
    assert r.status_code == 200
    assert CAM_PASS not in r.text and STREAM_KEY not in r.text
    assert CAM_HOST not in r.text


def test_ingest_url_not_in_health(env):
    import app as app_module
    from fastapi.testclient import TestClient
    c = TestClient(app_module.app)
    r = c.get("/api/health")
    assert r.status_code == 200
    assert STREAM_KEY not in r.text and INGEST_URL not in r.text


# ============================================================ 9. error vocabulary

def test_every_build_reason_is_in_closed_vocabulary():
    # A builder reason the control plane cannot launder would drop to "unknown",
    # losing the operator's only clue.
    assert ic.BUILD_REASONS <= sc.ERROR_REASONS


def test_no_reason_is_free_text():
    for reason in sc.ERROR_REASONS:
        assert " " not in reason and reason == reason.lower()


# ====================================================== 10. existing behaviour

def test_free_pro_untouched():
    import entitlements
    assert entitlements.FREE_HOURS == 72
    assert entitlements.PRO_HOURS == 240


def test_public_camera_schema_unchanged(env):
    import app as app_module
    from fastapi.testclient import TestClient
    c = TestClient(app_module.app)
    d = c.get("/api/cameras/ilioupoli").json()
    assert "live_status" in d
    assert set(d["live_status"]) == {"running"}
    assert d["live_status"]["running"] is False        # nothing started


def test_worker_never_calls_asyncio_subprocess_directly():
    # The abstraction is what makes these tests possible; a direct call in the
    # worker would be untestable here and would bypass the secret handling.
    src = inspect.getsource(iw)
    assert "create_subprocess_exec" not in src
    assert "asyncio.subprocess" not in src
