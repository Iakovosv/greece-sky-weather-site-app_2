"""Process abstraction for the M3 stream worker: spawn, monitor, terminate.

The point of this module is to keep ``asyncio.create_subprocess_exec`` out of the
worker's logic. The worker asks a *factory* for a process handle and only ever
speaks to that handle: ``wait``, ``terminate``, ``kill``, ``is_running``. Tests
supply a fake factory and drive every branch -- immediate exit, crash, hang,
SIGTERM ignored, SIGKILL fallback, stderr noise -- with no FFmpeg installed and
no camera present.

Two pieces, deliberately small
------------------------------
* :class:`ProcessHandle` -- the protocol the worker depends on. A fake is a few
  lines, which is the whole reason the worker never calls ``asyncio`` directly.
* :class:`ProcessFactory` -- how a handle is obtained. The production
  implementation is :class:`AsyncioProcessFactory`; it is the only place in the
  codebase that starts a real child process.

What a handle must guarantee
----------------------------
``pid`` is for the worker's own bookkeeping and never leaves the admin side.
``terminate``/``kill`` are signal *senders*: they do not wait, and they never
raise for an already-dead process, so a stop path can call them defensively.
``wait`` returns the exit code once and is safe to call from several tasks.
``stderr_tail`` returns a bounded, last-N-bytes view a human can read; the worker
never parses it (parsing a media tool's stderr is how free text reaches a log),
it only decides "the process is gone" and records a closed-vocabulary reason.

No secrets cross this boundary. The environment a process is given comes from
:mod:`ingest_command`, is passed straight through to ``execve``, and is never
read back, logged or stored.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque

log = logging.getLogger("wx.ingest_process")

# How much stderr is retained for an operator to read. Bounded on purpose: a
# chatty process must not be able to grow a buffer without limit.
STDERR_TAIL_BYTES = 4096


class ProcessSpawnError(Exception):
    """A child process could not be started. Carries a sanitized reason only.

    The underlying exception (``FileNotFoundError`` for a missing binary, an
    ``OSError`` for a resource limit) is deliberately *not* forwarded: its text
    can name a path or a filesystem, and the worker only needs to know the launch
    failed and why, from a closed vocabulary.
    """

    def __init__(self, reason: str = "launch_failed") -> None:
        super().__init__("process spawn failed")
        self.reason = reason


class ProcessHandle:
    """What the worker needs from a running child process. Implemented by fakes."""

    pid: int | None = None

    async def wait(self) -> int:  # pragma: no cover - interface
        """Return the exit code, once the process has ended."""
        raise NotImplementedError

    def terminate(self) -> None:  # pragma: no cover - interface
        """Ask the process to stop (SIGTERM). Must not raise if it already died."""
        raise NotImplementedError

    def kill(self) -> None:  # pragma: no cover - interface
        """Stop the process unconditionally (SIGKILL). Idempotent."""
        raise NotImplementedError

    def is_running(self) -> bool:  # pragma: no cover - interface
        """Whether the process is still alive, without blocking."""
        raise NotImplementedError

    def returncode(self) -> int | None:  # pragma: no cover - interface
        """The exit code if it has ended, else None."""
        raise NotImplementedError

    def stderr_tail(self) -> str:  # pragma: no cover - interface
        """The last bytes the process wrote to stderr, for a human. Never parsed."""
        return ""


class ProcessFactory:
    """How the worker obtains a :class:`ProcessHandle`. Swapped out in tests."""

    async def spawn(self, argv: list[str], env: dict[str, str]) -> ProcessHandle:  # pragma: no cover
        raise NotImplementedError


class AsyncioProcessHandle(ProcessHandle):
    """A handle over ``asyncio.subprocess.Process``.

    ``stdout`` is discarded: nothing here consumes a media stream's stdout, and a
    pipe nobody reads is a deadlock. ``stderr`` is drained by a background task
    into a bounded deque, so a process that floods stderr cannot block on a full
    pipe and cannot grow memory without limit.
    """

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self.pid = proc.pid
        self._tail: deque[bytes] = deque()
        self._tail_bytes = 0
        self._pump: asyncio.Task | None = None
        if proc.stderr is not None:
            self._pump = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        try:
            while True:
                chunk = await self._proc.stderr.read(1024)
                if not chunk:
                    return
                self._tail.append(chunk)
                self._tail_bytes += len(chunk)
                while self._tail_bytes > STDERR_TAIL_BYTES and len(self._tail) > 1:
                    self._tail_bytes -= len(self._tail.popleft())
        except (asyncio.CancelledError, Exception):
            return

    async def wait(self) -> int:
        return await self._proc.wait()

    def terminate(self) -> None:
        try:
            if self._proc.returncode is None:
                self._proc.terminate()
        except ProcessLookupError:
            pass

    def kill(self) -> None:
        try:
            if self._proc.returncode is None:
                self._proc.kill()
        except ProcessLookupError:
            pass

    def is_running(self) -> bool:
        return self._proc.returncode is None

    def returncode(self) -> int | None:
        return self._proc.returncode

    def stderr_tail(self) -> str:
        try:
            return b"".join(self._tail).decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive decode guard
            return ""


class AsyncioProcessFactory(ProcessFactory):
    """The one production spawner. The only place a real child process starts.

    ``start_new_session=True`` puts the child in its own process group: a stop
    must not be able to signal the web process, and the child's own children
    (a demuxer, a muxer) are not left behind in our group. The environment is
    passed as given -- this class never inspects it, so it cannot log a secret.
    """

    def __init__(self, *, stderr_pipe: bool = True) -> None:
        self._stderr_pipe = stderr_pipe

    async def spawn(self, argv: list[str], env: dict[str, str]) -> ProcessHandle:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=(asyncio.subprocess.PIPE if self._stderr_pipe
                        else asyncio.subprocess.DEVNULL),
                start_new_session=True,
            )
        except FileNotFoundError:
            # The binary is not installed. Named as a closed reason, not as a path.
            raise ProcessSpawnError("launch_failed")
        except (OSError, ValueError):
            raise ProcessSpawnError("launch_failed")
        return AsyncioProcessHandle(proc)
