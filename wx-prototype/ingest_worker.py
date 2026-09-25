"""The real stream worker (M3-A): supervise one FFmpeg-class process per camera.

This is the concrete :class:`stream_control.StreamWorker` for production. It
launches a real child process (through :mod:`ingest_process`, which the tests
replace with a fake), keeps it alive with heartbeats, and restarts it with bounded
exponential backoff when it dies. It writes *observed* state and nothing else: the
control plane owns ``desired``, so a worker can never start or stop itself.

What it deliberately does not do
--------------------------------
* It never builds an RTSP URL. That comes from :mod:`ingest_command`, which reads
  the trusted registry; no client input reaches this file.
* It never parses a process's stderr. A media tool's stderr is free text and
  parsing it is how a URL or a credential ends up in a log. The process is either
  running or gone, and "gone" maps to a closed-vocabulary reason.
* It never records. The only output is the RTMPS destination the builder set.
* It never touches ``desired``.

Concurrency model
-----------------
One :class:`_Job` per camera id, held in a dict guarded by an :class:`asyncio.Lock`.
That lock is the per-camera mutex (B4): two concurrent ``start`` calls for one
camera produce exactly one process, and a ``stop`` racing a ``start`` cannot leave
an unsupervised child behind. Because the web process and the worker process are
different processes, this in-process mutex is *not* the only guard -- the control
plane's ``BEGIN IMMEDIATE`` and the ``desired`` short-circuit remain the
cross-process arbitration. Both are needed and neither replaces the other.

Timing is injected
------------------
``sleep``, ``clock`` and ``rand`` are constructor arguments. The supervisor's
backoff and heartbeat cadence are therefore exercised in milliseconds in tests
without patching the event loop, and the backoff a test observes is the backoff
production would use.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time

import ingest_command
import ingest_process

log = logging.getLogger("wx.ingest_worker")

# ---- backoff and circuit breaker (constants, not magic numbers in the loop) ----

# First wait after a crash. Small: a transient RTSP blip should recover quickly.
BACKOFF_INITIAL_S = 1.0
# Each successive failure waits this much longer...
BACKOFF_FACTOR = 2.0
# ...up to this ceiling, so a camera that is off overnight is retried rarely, not
# hammered: 1, 2, 4, 8, 16, 32, 60, 60, ...
BACKOFF_MAX_S = 60.0
# Fraction of the delay added or removed at random. Without jitter, N cameras
# restarting after a shared upstream outage would retry in lockstep forever.
BACKOFF_JITTER = 0.25
# Give up after this many consecutive failures *within* the window below. The
# window resets the counter when a process has been healthy long enough, so a
# camera that streams for an hour then blips is not near the limit.
MAX_CONSECUTIVE_RESTARTS = 5
CIRCUIT_RESET_AFTER_S = 300.0

# How long to wait for a clean SIGTERM exit before escalating to SIGKILL, and how
# long to wait for the kill to take effect before giving up on the process (the
# state row is corrected regardless -- see :meth:`RealStreamWorker.stop`).
STOP_GRACE_S = 5.0
KILL_GRACE_S = 2.0

# A process that exits within this many seconds of launch is treated as a failed
# launch rather than a stream that ended, which is what distinguishes "wrong
# credentials" from "the stream stopped".
LAUNCH_GRACE_S = 2.0


def backoff_delay(attempt: int, *, rand: float | None = None) -> float:
    """The delay before restart ``attempt`` (1-based), with jitter applied.

    Exposed as a function so a test can assert the growth curve and the jitter
    bound directly, without running the supervisor.
    """
    base = min(BACKOFF_INITIAL_S * (BACKOFF_FACTOR ** max(0, attempt - 1)),
               BACKOFF_MAX_S)
    r = random.random() if rand is None else rand
    factor = 1.0 + BACKOFF_JITTER * (2.0 * r - 1.0)     # [1-j, 1+j]
    return max(0.0, min(base * factor, BACKOFF_MAX_S * (1.0 + BACKOFF_JITTER)))


class _Job:
    """One camera's supervised process: the handle, its task, and retry state."""

    __slots__ = ("camera_id", "command", "handle", "task", "stopping",
                 "failures", "healthy_since")

    def __init__(self, camera_id: str, command: ingest_command.IngestCommand,
                 handle: ingest_process.ProcessHandle) -> None:
        self.camera_id = camera_id
        self.command = command
        self.handle = handle
        self.task: asyncio.Task | None = None
        self.stopping = False
        self.failures = 0
        self.healthy_since: float | None = None


class RealStreamWorker:
    """Supervises one child process per camera. See the module docstring."""

    name = "real"

    def __init__(self, *, factory: ingest_process.ProcessFactory | None = None,
                 builder=None, sleep=asyncio.sleep, clock=time.time,
                 rand=None, heartbeat_interval_s: float | None = None,
                 stop_grace_s: float = STOP_GRACE_S,
                 kill_grace_s: float = KILL_GRACE_S,
                 launch_grace_s: float = LAUNCH_GRACE_S) -> None:
        self._factory = factory or ingest_process.AsyncioProcessFactory()
        self._build = builder or ingest_command.build_command
        self._sleep = sleep
        self._clock = clock
        self._rand = rand
        self._heartbeat_interval_s = (heartbeat_interval_s
                                      if heartbeat_interval_s is not None
                                      else _heartbeat_interval())
        self._stop_grace_s = stop_grace_s
        self._kill_grace_s = kill_grace_s
        self._launch_grace_s = launch_grace_s
        self._jobs: dict[str, _Job] = {}
        self._mutex = asyncio.Lock()

    # ------------------------------------------------------------- public API

    async def start(self, camera_id: str) -> str | None:
        """Launch the ingest process for a camera, or raise ``StreamWorkerError``.

        Returns ``"starting"``: the job is *accepted*, not confirmed. The process
        is up, but "producing a stream" is the supervisor's observation to make and
        to write as ``live`` -- the control plane must not advertise a stream on
        the strength of a successful ``fork``.
        """
        cam_id = str(camera_id)
        # Defence in depth: the control plane already refuses a non-startable
        # camera, but the worker must not depend on its caller having checked.
        # Fail closed if configuration no longer allows this camera to run.
        if not _still_startable(cam_id):
            raise _worker_error("not_startable")
        async with self._mutex:
            existing = self._jobs.get(cam_id)
            if existing is not None and existing.handle.is_running() \
                    and not existing.stopping:
                # Idempotent: a second start for a camera already running is the
                # same job, never a second process.
                return "starting"
            command = self._command_for(cam_id)
            handle = await self._spawn(command)
            job = _Job(cam_id, command, handle)
            self._jobs[cam_id] = job
            job.task = asyncio.create_task(self._supervise(job))
        return "starting"

    async def stop(self, camera_id: str) -> None:
        """Stop a camera's process. Idempotent, and never leaves a child behind.

        Termination is graceful first (SIGTERM), then unconditional (SIGKILL). The
        row is moved to ``stopped`` even if the process will not die, because a
        stop that *reported* failure while leaving the UI claiming LIVE would be
        worse than a stop that reports success and leaves an orphan for the OS to
        reap -- and the control plane's own ``reconcile`` is the backstop.
        """
        cam_id = str(camera_id)
        async with self._mutex:
            job = self._jobs.pop(cam_id, None)
            if job is None:
                return
            job.stopping = True
            task = job.task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._terminate(job.handle)
        self._set(cam_id, "stopped")

    async def shutdown(self) -> None:
        """Stop every supervised process. Called on application shutdown."""
        async with self._mutex:
            cam_ids = list(self._jobs)
        for cam_id in cam_ids:
            try:
                await self.stop(cam_id)
            except Exception as e:  # shutdown must not raise
                log.warning("ingest shutdown: stop failed: camera=%s type=%s",
                            cam_id, type(e).__name__)

    def active_jobs(self) -> int:
        """How many cameras this worker currently supervises. Admin-side only."""
        return len(self._jobs)

    # ---------------------------------------------------------------- internals

    def _command_for(self, cam_id: str) -> ingest_command.IngestCommand:
        """Build the command from trusted state, mapping refusals to reasons."""
        try:
            source = ingest_command.resolved_source(cam_id)
            return self._build(cam_id, source,
                               ingest_url=_resolve_ingest_url(cam_id))
        except ingest_command.CommandBuildError as e:
            raise _worker_error(e.reason)
        except Exception as e:  # a builder must never crash the control plane
            log.warning("ingest command build crashed: camera=%s type=%s",
                        cam_id, type(e).__name__)
            raise _worker_error("unknown")

    async def _spawn(self, command: ingest_command.IngestCommand):
        try:
            return await self._factory.spawn(command.argv, command.env)
        except ingest_process.ProcessSpawnError as e:
            raise _worker_error(e.reason)

    async def _supervise(self, job: _Job) -> None:
        """Keep a camera's process alive, or give up in a bounded, sanitized way."""
        cam_id = job.camera_id
        try:
            while not job.stopping:
                started = self._clock()
                handle = job.handle
                # The process exists: this is the first liveness evidence. Mark
                # live and begin beating while we watch it.
                self._set(cam_id, "live", started=True, beat=True)
                beat = asyncio.create_task(self._beat(job))
                try:
                    code = await handle.wait()
                finally:
                    beat.cancel()
                if job.stopping:
                    return

                uptime = self._clock() - started
                if uptime >= CIRCUIT_RESET_AFTER_S:
                    job.failures = 0
                job.failures += 1

                # B5: a camera disabled while its process was running (or dying)
                # must not be restarted. The lifecycle gate is re-consulted here,
                # before a new process is spent, so "enabled=false" means stop.
                if not _still_startable(cam_id):
                    log.info("ingest not restarting disabled camera: camera=%s", cam_id)
                    self._set(cam_id, "error", error_reason="not_startable")
                    return

                if job.failures > MAX_CONSECUTIVE_RESTARTS:
                    # Circuit breaker open: stop retrying so a broken camera does
                    # not spawn a process every minute forever. The row is an
                    # error, which also releases the max-active slot.
                    log.warning("ingest gave up: camera=%s failures=%d exit=%s",
                                cam_id, job.failures, _code_label(code))
                    self._set(cam_id, "error", error_reason="worker_unavailable")
                    return

                reason = ("launch_failed" if uptime < self._launch_grace_s
                          else "worker_unavailable")
                log.warning("ingest process exited: camera=%s exit=%s uptime=%.1fs",
                            cam_id, _code_label(code), uptime)
                self._set(cam_id, "error", error_reason=reason)

                delay = backoff_delay(job.failures, rand=self._rand)
                log.info("ingest restart in %.1fs: camera=%s attempt=%d",
                         delay, cam_id, job.failures)
                await self._sleep(delay)
                if job.stopping:
                    return
                try:
                    # The command is reused verbatim: a restart must not rebuild it,
                    # because a rebuild would re-read config and could quietly pick
                    # up a *different* source mid-stream. _spawn maps a spawn
                    # failure to a worker error, which the outer handler records.
                    job.handle = await self._spawn(job.command)
                except _WorkerError:
                    raise
                except Exception as e:
                    log.warning("ingest respawn failed: camera=%s type=%s",
                                cam_id, type(e).__name__)
                    self._set(cam_id, "error", error_reason="launch_failed")
                    return
        except asyncio.CancelledError:
            # Stop or shutdown: terminate quietly and let the caller set state.
            await self._terminate(job.handle)
            raise
        except _WorkerError as e:
            self._set(cam_id, "error", error_reason=e.reason)
        except Exception as e:  # a supervisor must never die silently
            log.warning("ingest supervisor crashed: camera=%s type=%s",
                        cam_id, type(e).__name__)
            self._set(cam_id, "error", error_reason="unknown")

    async def _beat(self, job: _Job) -> None:
        """Heartbeat on the worker's cadence while the process is supervised."""
        import stream_control as streams
        try:
            while True:
                await self._sleep(self._heartbeat_interval_s)
                streams.heartbeat(job.camera_id)
        except asyncio.CancelledError:
            return

    async def _terminate(self, handle) -> None:
        """SIGTERM, then SIGKILL if it will not go. Never raises."""
        try:
            if not handle.is_running():
                return
            handle.terminate()
            if await self._wait_for_exit(handle, self._stop_grace_s):
                return
            log.warning("ingest process ignored SIGTERM; sending SIGKILL")
            handle.kill()
            await self._wait_for_exit(handle, self._kill_grace_s)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("ingest terminate failed: type=%s", type(e).__name__)

    async def _wait_for_exit(self, handle, timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(handle.wait(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

    def _set(self, cam_id: str, observed: str, *, error_reason: str | None = None,
             started: bool = False, beat: bool = False) -> None:
        import stream_control as streams
        streams._set_observed(cam_id, observed, worker=self.name,
                              error_reason=error_reason, started=started, beat=beat)


class _WorkerError(Exception):
    """Internal carrier so a raise inside the supervisor becomes a state write."""

    def __init__(self, reason: str) -> None:
        super().__init__("ingest worker error")
        self.reason = reason


def _worker_error(reason: str):
    """A ``StreamWorkerError`` with the reason laundered by the control plane."""
    import stream_control as streams
    return streams.StreamWorkerError(reason)


def _code_label(code) -> str:
    """A sanitized label for an exit code: a number, or a signal name."""
    if isinstance(code, int) and code < 0:
        return f"signal{-code}"
    return str(code)


def _heartbeat_interval() -> float:
    import stream_control as streams
    return float(streams.HEARTBEAT_INTERVAL_S)


def _still_startable(camera_id: str) -> bool:
    """Whether the lifecycle gate still lets this camera run.

    Fails *closed*: if the lifecycle module cannot answer (an import error, a
    malformed config), a restart is refused rather than attempted. The gate exists
    to stop exactly the case where configuration says "no" and a retry loop says
    "again", so an unanswerable gate must not be read as "yes".
    """
    try:
        import camera_lifecycle as lifecycle
        ok, _ = lifecycle.is_startable(camera_id)
        return ok
    except Exception as e:
        log.warning("ingest startability check failed: camera=%s type=%s",
                    camera_id, type(e).__name__)
        return False


def _resolve_ingest_url(camera_id: str) -> str | None:
    """The server-side RTMPS destination for a camera, or None.

    The stream key is resolved here, from the same private store that holds the
    RTSP material, and is returned only as part of a URL the builder places in
    argv. It is never returned to a caller, logged, or stored on a command
    attribute. Until the VPS secrets milestone wires a real value, this returns
    None and the builder refuses the command -- so no stream key exists anywhere
    in this build.
    """
    import cameras as cams
    ref = os.environ.get("WX_CAMERA_INGEST_REF")
    if not ref:
        return None
    value = cams.resolve_secret({"secret_ref": ref})
    return value or None
