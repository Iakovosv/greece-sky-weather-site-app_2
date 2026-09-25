"""Server-side stream control plane: authoritative state, start/stop, limits.

This module owns the *decision* to run a camera stream. It does not run one. The
actual media worker (FFmpeg/MediaMTX/systemd) is a separate milestone and, when
it arrives, will be a separate process on the same host; the contract here is
designed so that swap needs no change to the state model or the callers.

Three separations carry the design
----------------------------------
**desired vs observed.** The control plane records what it *wants* (``desired``:
``stopped`` | ``running``) and the worker records what is *true* (``observed``:
``stopped`` | ``starting`` | ``live`` | ``stopping`` | ``error``). Only
``observed`` decides what a visitor is told, so a start request that has not yet
borne fruit is never advertised as live.

**control plane vs worker.** ``request_start``/``request_stop`` never touch RTSP,
a credential, or a subprocess. They consult policy, then call a
:class:`StreamWorker`. In M2 that is :class:`MockStreamWorker`, which touches no
network and needs no secret; M3 supplies a real implementation behind the same
interface.

**web process vs worker process.** The state lives in the existing SQLite
database (``WX_DB``), not in a process-local dict, precisely so the M3 worker --
a different process -- reads and writes the *same* rows through the same
functions. That is why the schema carries ``worker`` and timestamp columns now,
before there is a second process to need them.

Liveness: why ``observed='live'`` is not enough
-----------------------------------------------
If ``live`` were taken at face value forever, a worker that died would leave the
UI saying LIVE indefinitely -- with no writer left to correct the row. So
``live`` is treated as *time-bounded evidence*, not a permanent fact:

* the worker refreshes ``heartbeat_at`` while it believes it is streaming
  (:func:`heartbeat`); ``start`` also seeds it;
* :func:`public_running` reports true only while that heartbeat is *fresh*, so a
  dead worker stops being advertised the moment its last beat ages out, with no
  writer required;
* :func:`reconcile` *narrows* a stale ``live`` row to ``error``/``stale`` so an
  operator sees why, and so the max-active slot is released.

The heartbeat is written by the worker process, never by a browser: no client
activity, header or request can extend a camera's liveness. The cadence is a
documented contract (see :data:`HEARTBEAT_INTERVAL_S`), not a guess about M3.

Security posture
----------------
No function here accepts a URL, host, credential or command from a caller: a
camera is named only by its server-side id, and the worker resolves its own
material. Error reasons are a closed vocabulary, so nothing a worker reports can
reach an operator (or a log) as free text. The heartbeat is likewise never part
of the public payload: the browser sees only ``{running: bool}``.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time

import config

log = logging.getLogger("wx.stream_control")

# Observed states, in lifecycle order. ``desired`` is the two-valued intent.
OBSERVED_STATES = ("stopped", "starting", "live", "stopping", "error")
DESIRED_STATES = ("stopped", "running")
ACTIVE_OBSERVED = ("starting", "live", "stopping")

# A worker that has been "starting"/"stopping" for longer than this is presumed
# gone rather than slow. It is reported as stale rather than as live, so a crashed
# worker cannot leave the UI claiming a stream that is not running.
STALE_AFTER_S = 90

# The heartbeat contract between the worker and this control plane.
#
# How the limit was chosen (deliberately, not guessed):
#   * It must be comfortably longer than the worst-case gap between two beats on a
#     loaded host, so a busy-but-alive worker is never declared dead. A camera
#     encode may stall for a second or two under CPU contention; minutes of slack
#     remove that false positive entirely.
#   * It must be short enough that a visitor is not shown a dead stream for long.
#     Two minutes of grace is already over-generous for a UI badge that a page
#     refresh will re-ask about.
#   * It is a *constant*, not yet an env knob, on purpose: exposing it invites
#     tuning it to a value the real worker does not actually honour, which would
#     be worse than a fixed contract. M3 is expected to beat every
#     HEARTBEAT_INTERVAL_S while streaming, giving a 3x margin before the
#     freshness window closes.
HEARTBEAT_INTERVAL_S = 30
LIVE_FRESH_S = 3 * HEARTBEAT_INTERVAL_S

# Freshness of an observed state is judged against different columns: a finished
# state (live) is only fresh while its heartbeat is, whereas a transient state
# (starting/stopping) is fresh while its last transition was recent enough.
_LIVE_LIKE = ("live",)

# Closed vocabulary for the admin-facing failure reason. A worker's own message is
# never stored or shown; it is mapped to one of these.
ERROR_REASONS = frozenset({"launch_failed", "worker_unavailable", "timeout",
                           "stale", "unknown",
                           # Refusals the ingest worker maps from a command that
                           # could not be built. They are here, in the one
                           # vocabulary, so the admin view never has to render a
                           # worker's own text.
                           "source_absent", "credential_missing", "audio_not_allowed",
                           "no_output", "host_not_allowlisted", "source_scheme",
                           "not_startable", "backend_unavailable",
                           # M3-B: the private secret directory could not be used,
                           # so the credential could not be kept out of argv. Fail
                           # closed rather than fall back to a command line.
                           "secret_store_unavailable"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stream_state (
    camera_id        TEXT PRIMARY KEY,
    desired          TEXT NOT NULL DEFAULT 'stopped',
    observed         TEXT NOT NULL DEFAULT 'stopped',
    error_reason     TEXT,               -- one of ERROR_REASONS, or NULL
    worker           TEXT,               -- worker kind, e.g. 'mock'; no cmdline
    started_at       TEXT,               -- when observed last became live
    heartbeat_at     TEXT,               -- last worker liveness beat while live
    updated_at       TEXT NOT NULL,      -- last write by control plane or worker
    transition_at    TEXT NOT NULL,      -- last observed-state change
    restart_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_stream_observed ON stream_state (observed);
"""

# Columns added after the table first shipped. Applied with ALTER TABLE on boot so
# an existing WX_DB keeps working without a manual migration; SQLite has no
# "ADD COLUMN IF NOT EXISTS", so the presence check is explicit and idempotent.
_ADDED_COLUMNS = (("heartbeat_at", "TEXT"),)


# ---------------------------------------------------------------- worker seam

class StreamWorkerError(Exception):
    """A worker could not start/stop. ``reason`` is laundered through the set."""

    def __init__(self, reason: str = "unknown") -> None:
        super().__init__("stream worker failure")
        self.reason = reason if reason in ERROR_REASONS else "unknown"


class StreamWorker:
    """What the control plane needs from a media worker. M3 implements this.

    ``start`` is called only after the control plane has committed ``desired =
    running`` and ``observed = starting``; ``stop`` only after ``desired =
    stopped``. An implementation must be idempotent for a camera that is already
    in the requested condition, because a restart of the control plane replays a
    request it already granted.

    Return value of ``start``: the observed state the worker reached, or ``None``
    for "confirmed live". Returning ``"starting"`` is the honest answer for a real
    worker that has *accepted* the job but not yet produced a stream -- it is a
    separate process on M3 and will write ``live`` itself once the stream is up
    (through :func:`_set_observed`). The control plane never guesses this: a job
    that is merely accepted is not a stream, so the public status stays false.
    """

    name = "abstract"

    async def start(self, camera_id: str) -> str | None:  # pragma: no cover - interface
        raise NotImplementedError

    async def stop(self, camera_id: str) -> None:       # pragma: no cover - interface
        raise NotImplementedError


class MockStreamWorker(StreamWorker):
    """Deterministic, offline worker for tests. No network, no secret, no process.

    Its behaviour is driven by ``WX_STREAM_MOCK`` so every branch -- a clean
    start, a failure to launch, a start that is accepted but never confirms, a
    clean stop -- can be exercised with no camera and no FFmpeg.
    """

    name = "mock"

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode if mode in ("ok", "fail", "deferred", "stopfail") else "ok"

    async def start(self, camera_id: str) -> str | None:
        if self.mode == "fail":
            raise StreamWorkerError("launch_failed")
        if self.mode == "deferred":
            # Accepted, not confirmed: exactly what a slow real worker looks like
            # from here. The staleness rule is what eventually cleans it up.
            return "starting"
        return None          # confirmed live

    async def stop(self, camera_id: str) -> None:
        if self.mode == "stopfail":
            raise StreamWorkerError("worker_unavailable")


# The real backend supervises child processes, so it must be a single object for
# the whole process: its per-camera job table and mutex are what stop a second
# request from spawning a second FFmpeg. The mock is stateless and cheap, so it is
# built per call as before.
_REAL_WORKER = None


def worker_for_backend() -> StreamWorker:
    """The worker the configured backend names. Dispatches on ``WX_STREAM_BACKEND``.

    ``mock`` (the default) returns a stateless :class:`MockStreamWorker`.
    ``real`` returns the process-wide :class:`RealStreamWorker` singleton, because
    its per-camera state is the only thing preventing a duplicate process.

    An unknown backend **fails closed**: it returns a worker that refuses every
    start with a sanitized ``backend_unavailable`` reason. It must never fall back
    to the mock, because a deploy that asked for real ingest and silently got a
    fake "live" is the one failure mode this whole layer exists to prevent.
    """
    global _REAL_WORKER
    backend = (config.stream_backend() or "mock").strip().lower()
    if backend == "mock":
        mode = (os.environ.get("WX_STREAM_MOCK") or "ok").strip().lower()
        return MockStreamWorker(mode)
    if backend == "real":
        if _REAL_WORKER is None:
            import ingest_worker
            _REAL_WORKER = ingest_worker.RealStreamWorker()
        return _REAL_WORKER
    log.error("unknown stream backend %r; stream starts will be refused", backend)
    return _UnavailableWorker(backend)


def activation_ready() -> dict:
    """Deploy-readiness probe for the real ingest backend. Delegates to the worker.

    Kept here so ``/api/health`` and the admin surface have one import, and so the
    control plane stays the only module the app talks to for stream state.
    """
    if (config.stream_backend() or "mock").strip().lower() != "real":
        return {"ready": False, "reason": "backend_not_real"}
    import ingest_worker
    return ingest_worker.activation_ready()


def current_real_worker():
    """The real worker if one has been built this process, else None.

    Used by the app's shutdown hook: it must stop child processes without
    *creating* a worker, because building one during shutdown would spawn nothing
    and only add work. Never returns the mock -- a mock has nothing to stop.
    """
    return _REAL_WORKER


class _UnavailableWorker(StreamWorker):
    """A backend this build does not ship. Refuses, and never pretends to work."""

    def __init__(self, backend: str) -> None:
        self.name = f"unavailable:{backend}"[:40]

    async def start(self, camera_id: str) -> str | None:
        raise StreamWorkerError("backend_unavailable")

    async def stop(self, camera_id: str) -> None:
        raise StreamWorkerError("backend_unavailable")


# ---------------------------------------------------------------- storage

def _connect() -> sqlite3.Connection:
    path = config.db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db() -> None:
    with _connect() as con:
        con.executescript(_SCHEMA)
        existing = {r["name"] for r in con.execute("PRAGMA table_info(stream_state)")}
        for name, decl in _ADDED_COLUMNS:
            if name not in existing:
                con.execute(f"ALTER TABLE stream_state ADD COLUMN {name} {decl}")


def _now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts or _now(), dt.timezone.utc)\
        .strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- reads

def status(camera_id: str, now: float | None = None) -> dict | None:
    """The admin view of one camera's stream, or None when it has no row.

    Pure read; no side effects. ``stale`` is computed here rather than stored, so
    a wedged worker is reported as stale without needing a writer to notice.
    """
    try:
        with _connect() as con:
            row = con.execute("SELECT * FROM stream_state WHERE camera_id=?",
                              (str(camera_id),)).fetchone()
    except sqlite3.Error as e:
        log.warning("stream state read failed: %s", type(e).__name__)
        return None
    if row is None:
        return None
    return _row_view(row, now)


def _parse_ts(value) -> float:
    """An ISO-8601 UTC stamp as an epoch, or 0.0 when it is absent/unreadable."""
    from datetime import datetime, timezone
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")\
            .replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _is_stale(observed: str, updated_at, heartbeat_at, stamp: float) -> bool:
    """Whether an observed state has aged out of freshness, by state kind.

    ``live`` is judged on its *heartbeat* (a worker may be alive for hours but
    must keep beating); ``starting``/``stopping`` are judged on their last
    transition, because no beat is expected until the worker reaches ``live``.
    Other states (``stopped``/``error``) are never stale -- they are terminal.
    """
    if observed in _LIVE_LIKE:
        return stamp - _parse_ts(heartbeat_at) > LIVE_FRESH_S
    if observed in ("starting", "stopping"):
        return stamp - _parse_ts(updated_at) > STALE_AFTER_S
    return False


def _row_view(row: sqlite3.Row, now: float | None) -> dict:
    stamp = now if now is not None else _now()
    keys = row.keys()
    heartbeat = row["heartbeat_at"] if "heartbeat_at" in keys else None
    age = max(0.0, stamp - _parse_ts(row["updated_at"]))
    beat_age = (max(0.0, stamp - _parse_ts(heartbeat))
                if heartbeat is not None else None)
    return {
        "id": row["camera_id"],
        "desired": row["desired"],
        "observed": row["observed"],
        "error_reason": row["error_reason"],
        "worker": row["worker"],
        "started_at": row["started_at"],
        # Admin-only liveness evidence. Never part of a public payload.
        "heartbeat_at": heartbeat,
        "heartbeat_age_s": None if beat_age is None else round(beat_age, 1),
        "updated_at": row["updated_at"],
        "restart_count": row["restart_count"],
        "age_s": round(age, 1),
        "stale": _is_stale(row["observed"], row["updated_at"], heartbeat, stamp),
    }


def all_status(now: float | None = None) -> list[dict]:
    """Every camera's stream row. Admin-only; sanitised by :func:`_row_view`."""
    try:
        with _connect() as con:
            rows = con.execute("SELECT * FROM stream_state ORDER BY camera_id").fetchall()
    except sqlite3.Error as e:
        log.warning("stream state list failed: %s", type(e).__name__)
        return []
    return [_row_view(r, now) for r in rows]


def active_count(now: float | None = None) -> int:
    """How many cameras are believed to be running right now.

    Counts ``starting``/``live``/``stopping`` -- anything that is occupying a
    worker slot -- so a camera mid-shutdown is not double-counted against the cap
    by a racing start.
    """
    try:
        with _connect() as con:
            row = con.execute(
                "SELECT COUNT(*) c FROM stream_state WHERE observed IN (?,?,?)",
                ACTIVE_OBSERVED).fetchone()
    except sqlite3.Error:
        return 0
    return int(row["c"] or 0)


def _running_of(row: sqlite3.Row, now: float | None) -> bool:
    """The public truth for one loaded row: live *and* still fresh."""
    view = _row_view(row, now)
    return view["observed"] == "live" and not view["stale"]


def running_map(now: float | None = None) -> dict[str, bool]:
    """Public liveness for every camera, resolved in a single query.

    The list endpoint needs the answer for every camera; asking per camera meant
    one connection and one row lookup each. One read is equivalent and cheaper.
    Only camera ids that actually have a row appear; the caller treats a missing
    id as false. No worker detail is returned -- just the boolean a caller needs.
    """
    try:
        with _connect() as con:
            rows = con.execute("SELECT * FROM stream_state").fetchall()
    except sqlite3.Error as e:
        log.warning("stream state read failed: %s", type(e).__name__)
        return {}
    return {r["camera_id"]: _running_of(r, now) for r in rows}


def public_running(camera_id: str, now: float | None = None) -> bool:
    """The coarse public truth: is this camera *actually* live right now.

    ``True`` only while ``observed == 'live'`` *and* its heartbeat is fresh. Not
    ``desired``, not ``starting`` -- a request that has not produced a stream is
    not a stream. And a ``live`` row whose worker stopped beating decays to
    ``False`` on its own (see :data:`LIVE_FRESH_S`), so a worker that vanishes
    cannot keep the UI claiming LIVE even before :func:`reconcile` notices.
    """
    st = status(camera_id, now)
    if st is None or st["stale"]:
        return False
    return st["observed"] == "live"


def heartbeat(camera_id: str, now: float | None = None) -> bool:
    """Record that a worker still believes it is streaming. Worker-side only.

    Called periodically by the media worker (M3) while a stream is up; it is the
    evidence that keeps a ``live`` row fresh. Written only for a row that is
    actually ``live`` and still desired running -- beating for a camera that was
    stopped meanwhile, or that is not live, would resurrect stale state, so those
    cases are ignored. Returns whether the beat was accepted.
    """
    stamp = _iso(now)
    try:
        with _connect() as con:
            cur = con.execute(
                "UPDATE stream_state SET heartbeat_at=?, updated_at=?"
                " WHERE camera_id=? AND observed='live' AND desired='running'",
                (stamp, stamp, str(camera_id)))
            return cur.rowcount > 0
    except sqlite3.Error as e:
        log.warning("stream heartbeat write failed: camera=%s type=%s",
                    camera_id, type(e).__name__)
        return False


# ---------------------------------------------------------------- writes

async def request_start(camera_id: str, *, worker: StreamWorker | None = None,
                        now: float | None = None) -> dict:
    """Ask for a camera's stream to run. Idempotent; enforces the caps.

    Order of operations, all of which matter:

    1. The camera must be startable (lifecycle ``enabled``). A camera that is not
       fully configured is refused before any worker is consulted.
    2. A single transaction commits ``desired=running, observed=starting`` and
       enforces max-active *inside* the transaction, so two concurrent requests
       cannot both pass the cap -- the same reasoning as the snapshot
       single-flight, but the arbitration is the database because the M3 worker is
       a separate process.
    3. Only then is the worker invoked. On success ``observed=live``; on failure
       ``observed=error`` with a laundered reason.

    Already ``running`` (desired) is a no-op returning the current state: a second
    click is not a second stream.
    """
    import camera_lifecycle as lifecycle

    worker = worker or worker_for_backend()
    cam_id = str(camera_id)
    ok, why = lifecycle.is_startable(cam_id)
    if not ok:
        return {"ok": False, "reason": why, "state": status(cam_id, now)}

    cap = config.stream_max_active()
    try:
        with _connect() as con:
            con.execute("BEGIN IMMEDIATE")      # serialise across processes
            row = con.execute("SELECT * FROM stream_state WHERE camera_id=?",
                              (cam_id,)).fetchone()
            if row is not None and row["desired"] == "running" \
                    and row["observed"] in ("starting", "live"):
                view = _row_view(row, now)
                con.rollback()
                return {"ok": True, "reason": "already_running", "state": view}
            if row is None or row["observed"] not in ACTIVE_OBSERVED:
                active = con.execute(
                    "SELECT COUNT(*) c FROM stream_state WHERE observed IN (?,?,?)",
                    ACTIVE_OBSERVED).fetchone()["c"]
                if active >= cap:
                    con.rollback()
                    return {"ok": False, "reason": "max_active",
                            "state": status(cam_id, now)}
            # Commit the intent before any work: if this process dies mid-start, the
            # row still records that a worker was asked, which is what makes the
            # staleness rule able to clean it up.
            con.execute(
                "INSERT INTO stream_state (camera_id, desired, observed, error_reason,"
                " worker, updated_at, transition_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(camera_id) DO UPDATE SET desired='running',"
                " observed='starting', error_reason=NULL, worker=?, updated_at=?,"
                " transition_at=?",
                (cam_id, "running", "starting", None, worker.name, _iso(), _iso(),
                 worker.name, _iso(), _iso()))
            con.commit()
    except sqlite3.Error as e:
        # The state store is the authority; without it we must not touch a worker.
        # Nothing about the failure (table, SQL, path, traceback) leaves this line.
        log.warning("stream start refused: state store unavailable: camera=%s type=%s",
                    cam_id, type(e).__name__)
        return {"ok": False, "reason": "state_unavailable", "state": None}

    try:
        observed = await worker.start(cam_id)
    except StreamWorkerError as e:
        _set_observed(cam_id, "error", error_reason=e.reason, worker=worker.name)
        log.warning("stream start failed: camera=%s reason=%s", cam_id, e.reason)
        return {"ok": False, "reason": "worker_error", "state": status(cam_id)}
    except Exception as e:  # a worker must never crash the control plane
        _set_observed(cam_id, "error", error_reason="unknown", worker=worker.name)
        log.warning("stream start crashed: camera=%s type=%s", cam_id, type(e).__name__)
        return {"ok": False, "reason": "worker_error", "state": status(cam_id)}

    # A worker that reports it is still starting is honoured: the job is
    # accepted, not confirmed, and the staleness rule will clean it up if the
    # worker never writes `live` itself. Only a confirmed worker is recorded live.
    if observed == "starting":
        return {"ok": True, "reason": "starting", "state": status(cam_id)}
    # Seed the heartbeat with the same write that marks it live: liveness is
    # evidence the worker must keep producing, and this is the first beat.
    _set_observed(cam_id, "live", worker=worker.name, started=True, beat=True)
    return {"ok": True, "reason": "started", "state": status(cam_id)}


async def request_stop(camera_id: str, *, worker: StreamWorker | None = None,
                       now: float | None = None) -> dict:
    """Ask for a camera's stream to stop. Idempotent.

    A camera that is already stopped is a no-op, not an error. ``observed``
    passes through ``stopping`` while the worker unwinds, then ``stopped``.
    """
    worker = worker or worker_for_backend()
    cam_id = str(camera_id)
    try:
        with _connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM stream_state WHERE camera_id=?",
                              (cam_id,)).fetchone()
            if row is None or row["observed"] == "stopped":
                view = _row_view(row, now) if row is not None else None
                con.rollback()
                return {"ok": True, "reason": "already_stopped", "state": view}
            con.execute(
                "UPDATE stream_state SET desired='stopped', observed='stopping',"
                " error_reason=NULL, worker=?, updated_at=?, transition_at=?"
                " WHERE camera_id=?",
                (worker.name, _iso(), _iso(), cam_id))
            con.commit()
    except sqlite3.Error as e:
        log.warning("stream stop refused: state store unavailable: camera=%s type=%s",
                    cam_id, type(e).__name__)
        return {"ok": False, "reason": "state_unavailable", "state": None}

    try:
        await worker.stop(cam_id)
    except StreamWorkerError as e:
        _set_observed(cam_id, "error", error_reason=e.reason, worker=worker.name)
        log.warning("stream stop failed: camera=%s reason=%s", cam_id, e.reason)
        return {"ok": False, "reason": "worker_error", "state": status(cam_id)}
    except Exception as e:
        _set_observed(cam_id, "error", error_reason="unknown", worker=worker.name)
        log.warning("stream stop crashed: camera=%s type=%s", cam_id, type(e).__name__)
        return {"ok": False, "reason": "worker_error", "state": status(cam_id)}

    _set_observed(cam_id, "stopped", worker=worker.name)
    return {"ok": True, "reason": "stopped", "state": status(cam_id)}


def _set_observed(camera_id: str, observed: str, *, worker: str | None = None,
                  error_reason: str | None = None, started: bool = False,
                  restart: bool = False, beat: bool = False) -> None:
    """Record the worker's observed outcome. Worker-side writers call this too.

    ``beat`` stamps ``heartbeat_at`` as part of the same write. It is used when a
    worker *confirms* it is live (the first beat); subsequent beats come through
    :func:`heartbeat` directly. It is never set for an error or a stop, so a
    non-live state cannot carry a misleading fresh heartbeat.
    """
    now = _iso()
    reason = error_reason if error_reason in ERROR_REASONS else None
    try:
        with _connect() as con:
            con.execute(
                "UPDATE stream_state SET observed=?, error_reason=?,"
                " worker=COALESCE(?, worker),"
                " started_at=CASE WHEN ? THEN ? ELSE started_at END,"
                " heartbeat_at=CASE WHEN ? THEN ? ELSE heartbeat_at END,"
                " restart_count=restart_count+?, updated_at=?, transition_at=?"
                " WHERE camera_id=?",
                (observed, reason, worker, 1 if started else 0, now,
                 1 if beat else 0, now,
                 1 if restart else 0, now, now, str(camera_id)))
    except sqlite3.Error as e:
        log.warning("stream state write failed: camera=%s type=%s",
                    camera_id, type(e).__name__)


def reconcile(now: float | None = None) -> list[str]:
    """Narrow aged-out state to ``error``/``stale``. Returns the ids it acted on.

    Called from the app's periodic loop (and by the M3 worker on boot). Two kinds
    of state age out:

    * ``starting``/``stopping`` past :data:`STALE_AFTER_S` -- a worker that never
      reached a terminal state;
    * ``live`` whose heartbeat has gone past :data:`LIVE_FRESH_S` -- a worker that
      died while it looked healthy (case C of the review).

    Both become ``error``/``stale``, so ``public_running`` stops reporting them
    and an operator sees why. It never *starts* anything -- reconciliation only
    ever narrows. It is also the only place that releases a stale ``live`` row's
    max-active slot, so a wedged worker cannot pin a slot forever.
    """
    stamp = now if now is not None else _now()
    acted: list[str] = []
    try:
        with _connect() as con:
            rows = con.execute(
                "SELECT camera_id, observed, updated_at, heartbeat_at FROM"
                " stream_state WHERE observed IN ('starting','stopping','live')"
            ).fetchall()
    except sqlite3.Error:
        return []
    for row in rows:
        if _is_stale(row["observed"], row["updated_at"],
                     row["heartbeat_at"] if "heartbeat_at" in row.keys() else None,
                     stamp):
            _set_observed(row["camera_id"], "error", error_reason="stale")
            acted.append(row["camera_id"])
    if acted:
        log.warning("stream reconcile: %d stale worker(s) marked errored", len(acted))
    acted.extend(_drain_disabled())
    return acted


def _drain_disabled(now: float | None = None) -> list[str]:
    """Stop a running camera whose lifecycle is no longer startable (B5).

    ``enabled=false`` on a live camera must mean "stop", not "keep streaming until
    something else notices". This is the control-plane half: it drives the row to
    a stopped state and releases the max-active slot, so the public status stops
    claiming LIVE and the slot is reusable even if the worker's own process is
    still winding down.

    It deliberately does *not* call the worker. Reconcile stays a pure state
    narrowing pass with no side effects beyond the database; the worker notices
    the same lifecycle change on its own (its supervisor refuses to restart a
    non-startable camera) and unwinds. Two independent checks, one shared rule.

    ``enabled`` keeps its existing meaning -- lifecycle activation, not liveness.
    """
    try:
        import camera_lifecycle as lifecycle
    except Exception:  # pragma: no cover - import guard
        return []
    try:
        with _connect() as con:
            rows = con.execute(
                "SELECT camera_id FROM stream_state WHERE observed IN ('starting','live')"
            ).fetchall()
    except sqlite3.Error:
        return []
    stopped: list[str] = []
    for row in rows:
        cam_id = row["camera_id"]
        try:
            ok, _ = lifecycle.is_startable(cam_id)
        except Exception:
            # An unanswerable gate must not read as "keep streaming".
            ok = False
        if ok:
            continue
        _set_observed(cam_id, "stopped")
        stopped.append(cam_id)
    if stopped:
        log.warning("stream reconcile: %d camera(s) stopped after lifecycle change",
                    len(stopped))
    return stopped
