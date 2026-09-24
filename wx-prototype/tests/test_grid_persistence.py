"""The shared-grid caching guarantees, end to end.

These pin down the commercial property that motivated the work: the number of
GFS upstream downloads must depend on how often the *model publishes a new run*,
not on how many users or locations ask. The failure they are written against is
the old shape, where every distinct point paid its own NOMADS fetch.

Three behaviours are covered, and each is checked against a counter of the real
network entry points rather than a stub that would return the same answer either
way:

  * **Run-identity short-circuit** - a refresh pass that finds the published run
    unchanged performs no download.
  * **Persistence across restart** - a process that has just started serves the
    last run off disk instead of re-downloading it.
  * **A failed new run keeps the last valid one** - on disk as well as in RAM.

Everything here is hermetic: `WX_CACHE_DIR` is a tmp dir, the builders are
fakes, and the flag is set explicitly per test so the tests cannot depend on the
ambient environment.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import grids    # noqa: E402
import scheduler  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    """Every test gets its own cache dir and starts from a clean store."""
    monkeypatch.setenv("WX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("WX_RAM_ICON_SCOPE", raising=False)
    # Tests share the module-level STORE; clear it so a leftover grid from a
    # previous test cannot satisfy a short-circuit this test is meant to observe.
    grids.STORE._current.clear()
    grids.STORE._previous.clear()
    grids.STORE.stats.clear()
    yield
    grids.STORE._current.clear()
    grids.STORE.stats.clear()


def _counting_builder(run: str, calls: list, fail: bool = False):
    """A builder that records each invocation by run id."""
    async def build(client):
        calls.append(run)
        if fail:
            raise RuntimeError("NOMADS is down")
        return grids.synthetic_grid(model="gfs", run=run, steps=[0, 1, 2])
    return build


def _target(run: str):
    return ("gfs", (lambda: run, grids.gfs_scope))


# ------------------------------------------------------- run-identity short-circuit

def test_same_run_second_pass_does_no_download():
    """The core promise: an unchanged cycle costs nothing.

    The refresh loop wakes every few hours. When the published run has not moved,
    the pass must decide that *before* touching the network - otherwise each wake
    re-downloads 160 GFS steps for data already in RAM.
    """
    store = grids.GridStore()
    calls: list = []
    build = _counting_builder("2026010100", calls)

    first = asyncio.run(scheduler.refresh_once(
        store, builders={"gfs": build}, targets=dict([_target("2026010100")])))
    assert first["gfs"].startswith("ok run=2026010100")
    assert calls == ["2026010100"]

    second = asyncio.run(scheduler.refresh_once(
        store, builders={"gfs": build}, targets=dict([_target("2026010100")])))
    assert second["gfs"] == "ok unchanged run=2026010100"
    assert calls == ["2026010100"], "an unchanged run triggered a second download"


def test_a_genuinely_new_run_does_download():
    """The short-circuit must not overshoot: a new cycle is fetched."""
    store = grids.GridStore()
    calls: list = []

    asyncio.run(scheduler.refresh_once(
        store, builders={"gfs": _counting_builder("2026010100", calls)},
        targets=dict([_target("2026010100")])))
    result = asyncio.run(scheduler.refresh_once(
        store, builders={"gfs": _counting_builder("2026010106", calls)},
        targets=dict([_target("2026010106")])))

    assert result["gfs"].startswith("ok run=2026010106")
    assert calls == ["2026010100", "2026010106"]
    assert store.get("gfs").run == "2026010106"


def test_a_run_lookup_failure_does_not_download_and_keeps_the_grid():
    """If availability cannot be established, the safe answer is to do nothing.

    A probe that raises (NOMADS unreachable) must not be read as "the run is
    unknown, fetch something anyway": that would turn a monitoring blip into a
    burst of downloads. The loaded grid stays and the failure is recorded.
    """
    store = grids.GridStore()
    calls: list = []
    asyncio.run(scheduler.refresh_once(
        store, builders={"gfs": _counting_builder("2026010100", calls)},
        targets=dict([_target("2026010100")])))

    def boom():
        raise RuntimeError("NOMADS unreachable")

    result = asyncio.run(scheduler.refresh_once(
        store, builders={"gfs": _counting_builder("2026010199", calls)},
        targets={"gfs": (boom, grids.gfs_scope)}))

    assert result["gfs"].startswith("failed: run lookup:")
    assert calls == ["2026010100"], "a lookup failure caused a download"
    assert store.get("gfs").run == "2026010100"


def test_a_scope_change_forces_a_rebuild_at_the_same_run():
    """Changing the ICON box changes what the grid must contain.

    Without this guard, an operator switching `WX_RAM_ICON_SCOPE` to `europe`
    would keep the old Greece grid until the run id happened to change - serving
    the narrow box while the config says the wide one. The run id alone is not
    enough to decide a match.
    """
    store = grids.GridStore()
    calls: list = []
    asyncio.run(scheduler.refresh_once(
        store, builders={"gfs": _counting_builder("2026010100", calls)},
        targets={"gfs": (lambda: "2026010100", lambda: "greece")}))

    result = asyncio.run(scheduler.refresh_once(
        store, builders={"gfs": _counting_builder("2026010100", calls)},
        targets={"gfs": (lambda: "2026010100", lambda: "europe")}))

    assert result["gfs"].startswith("ok run="), "a scope change was masked by the run id"
    assert calls == ["2026010100", "2026010100"]


def test_custom_builders_keep_the_build_every_time_behaviour():
    """Backwards compatibility for callers that pass their own builders.

    No targets means no run identity to compare, so the pass must build - that is
    what every pre-existing scheduler test and any alternate source relies on.
    """
    store = grids.GridStore()
    calls: list = []
    build = _counting_builder("2026010100", calls)

    asyncio.run(scheduler.refresh_once(store, builders={"gfs": build}))
    asyncio.run(scheduler.refresh_once(store, builders={"gfs": build}))

    assert calls == ["2026010100", "2026010100"]


# ------------------------------------------------------- restart / persistence

def test_restart_serves_the_persisted_run_without_downloading():
    """A fresh process must not re-download a run it already has on disk.

    This is the difference between a deploy costing one disk read and costing the
    full 160-step download. The new store starts empty, so the short-circuit has
    to consult the archive before deciding to build.
    """
    first_store = grids.GridStore()
    calls: list = []
    asyncio.run(scheduler.refresh_once(
        first_store, builders={"gfs": _counting_builder("2026010100", calls)},
        targets=dict([_target("2026010100")])))
    assert calls == ["2026010100"]
    assert grids._disk_run_counts().get("gfs|greece") == 1

    # Simulate a restart: same disk, brand-new in-memory store.
    fresh = grids.GridStore()
    result = asyncio.run(scheduler.refresh_once(
        fresh, builders={"gfs": _counting_builder("2026010100", calls)},
        targets=dict([_target("2026010100")])))

    assert result["gfs"] == "ok unchanged run=2026010100"
    assert calls == ["2026010100"], "restart re-downloaded a run already on disk"
    assert fresh.get("gfs").run == "2026010100"


def test_a_failed_new_run_keeps_the_last_valid_persisted_run():
    """A broken new cycle must fall back to the previous good run, across restart.

    The new run is *available* (so no short-circuit) but its download fails. The
    in-memory grid and the disk archive must both still hold the old run, so a
    restart in that state serves the old one rather than nothing.
    """
    store = grids.GridStore()
    calls: list = []
    asyncio.run(scheduler.refresh_once(
        store, builders={"gfs": _counting_builder("2026010100", calls)},
        targets=dict([_target("2026010100")])))

    failed = asyncio.run(scheduler.refresh_once(
        store, builders={"gfs": _counting_builder("2026010106", calls, fail=True)},
        targets=dict([_target("2026010106")])))

    assert failed["gfs"].startswith("failed: RuntimeError")
    assert calls[-1] == "2026010106", "the new run was not attempted"
    assert store.get("gfs").run == "2026010100", "the good run was dropped"
    assert store.health()["gfs"]["stale"] is True

    # And it survives a restart: the disk still has the last valid run.
    fresh = grids.GridStore()
    restored = fresh.ensure_loaded("gfs", grids.gfs_scope())
    assert restored is not None
    assert restored.run == "2026010100"


def test_ensure_loaded_restores_and_is_a_noop_when_already_live():
    """Lazy restore reads the archive once; a live grid short-circuits it."""
    grids.save_grid(grids.synthetic_grid(run="2026010100"), scope="greece")
    store = grids.GridStore()

    got = store.ensure_loaded("gfs", "greece")
    assert got is not None and got.run == "2026010100"
    assert store.get("gfs") is got

    # Second call must return the live object untouched (no second disk read).
    again = store.ensure_loaded("gfs", "greece")
    assert again is got


def test_ensure_loaded_returns_none_when_nothing_is_persisted():
    """A truly cold start has no archive; the caller must fall back, not crash."""
    store = grids.GridStore()
    assert store.ensure_loaded("gfs", "greece") is None


def test_persisted_grid_round_trips_values_and_axes():
    """What is stored is the decoded grid, so the numbers must come back intact.

    A transposed axis or a float32/float64 mix-up would still load and still
    interpolate to something plausible; tying the values to their coordinates is
    what makes that visible.
    """
    g = grids.synthetic_grid(run="2026010100", steps=[0, 1, 2])
    g.meta["orog"] = np.full((g.lat.size, g.lon.size), 123.0, dtype=np.float32)
    grids.save_grid(g, scope="greece")

    back = grids.load_grid("gfs", "2026010100", "greece")
    assert back is not None
    np.testing.assert_allclose(back.lat, g.lat)
    np.testing.assert_allclose(back.lon, g.lon)
    assert back.steps == g.steps
    for name in g.vars:
        np.testing.assert_allclose(back.vars[name], g.vars[name])
    np.testing.assert_allclose(back.meta["orog"], g.meta["orog"])
    # The interpolation itself must agree, not just the arrays.
    assert grids.bilinear(back, "t2m_c", 1, 37.0, 23.0) == pytest.approx(
        grids.bilinear(g, "t2m_c", 1, 37.0, 23.0))


def test_a_truncated_archive_is_treated_as_a_miss_and_cleaned_up():
    """A save killed mid-flight must not be served as if it were a grid."""
    grids.save_grid(grids.synthetic_grid(run="2026010100"), scope="greece")
    npz, meta_p = grids._grid_paths("gfs", "2026010100", "greece")
    with open(npz, "r+b") as f:
        f.truncate(12)

    assert grids.load_grid("gfs", "2026010100", "greece") is None
    assert not os.path.exists(npz), "the damaged archive was left behind"
    assert not os.path.exists(meta_p)


# ------------------------------------------------------- retention

def test_retention_keeps_only_the_newest_runs_per_model():
    """Disk use is bounded by construction, not by eviction pressure."""
    for run in ("2026010100", "2026010106", "2026010112", "2026010118"):
        grids.save_grid(grids.synthetic_grid(run=run), scope="greece")

    summary = grids.prune_grids(keep=2)

    assert summary["kept"]["gfs|greece"] == 4   # inspected
    assert summary["removed"] == 4              # two runs removed, npz+json
    assert grids._disk_run_counts()["gfs|greece"] == 2
    # The two newest survive; the older two are gone.
    assert grids.load_grid("gfs", "2026010118", "greece") is not None
    assert grids.load_grid("gfs", "2026010112", "greece") is not None
    assert grids.load_grid("gfs", "2026010106", "greece") is None
    assert grids.load_grid("gfs", "2026010100", "greece") is None


def test_retention_keeps_the_newest_two_not_merely_two():
    """A bug that deleted the newest files would also leave 'two' behind."""
    for run in ("2026010100", "2026010106", "2026010112"):
        grids.save_grid(grids.synthetic_grid(run=run), scope="greece")
        time.sleep(0.01)

    grids.prune_grids(keep=2)

    runs = {m["run"] for m in grids._grid_metas()}
    assert runs == {"2026010112", "2026010106"}


def test_retention_is_per_model_and_scope():
    """GFS and ICON are separate families; one must not evict the other's runs."""
    for run in ("2026010100", "2026010106", "2026010112"):
        grids.save_grid(grids.synthetic_grid(model="gfs", run=run), scope="greece")
    grids.save_grid(grids.synthetic_grid(model="icon", run="2026010100"), scope="greece")

    grids.prune_grids(keep=2)

    counts = grids._disk_run_counts()
    assert counts["gfs|greece"] == 2
    assert counts["icon|greece"] == 1


def test_refresh_persists_and_prunes_as_it_goes():
    """The loop applies retention itself; a deploy needs no separate cron."""
    store = grids.GridStore()
    calls: list = []
    for run in ("2026010100", "2026010106", "2026010112"):
        asyncio.run(scheduler.refresh_once(
            store, builders={"gfs": _counting_builder(run, calls)},
            targets=dict([_target(run)])))

    assert grids._disk_run_counts()["gfs|greece"] == 2, "retention did not run"
    assert calls == ["2026010100", "2026010106", "2026010112"]


def test_persistence_can_be_disabled_by_a_caller():
    """`persist=False` must leave no archive behind (used by non-default sources)."""
    store = grids.GridStore()
    asyncio.run(scheduler.refresh_once(
        store, builders={"gfs": _counting_builder("2026010100", [])},
        targets=dict([_target("2026010100")]), persist=False))

    assert grids.disk_bytes() == 0


# ------------------------------------------------------- concurrency

def test_many_concurrent_restores_read_the_archive_once():
    """A burst of first requests after a restart must not fan out into disk reads.

    The store is empty and the archive is present, which is exactly the state a
    freshly started process is in. The guard has to collapse the burst.
    """
    grids.save_grid(grids.synthetic_grid(run="2026010100"), scope="greece")
    store = grids.GridStore()

    real_load = grids.load_grid
    loads = {"n": 0}

    def counting_load(*a, **k):
        loads["n"] += 1
        return real_load(*a, **k)

    grids.load_grid = counting_load
    try:
        results: list = []
        start = threading.Barrier(8)

        def worker():
            start.wait()
            results.append(store.ensure_loaded("gfs", "greece"))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        grids.load_grid = real_load

    assert all(r is not None for r in results)
    assert loads["n"] == 1, f"the archive was read {loads['n']} times, expected 1"


def test_two_models_are_restored_independently():
    grids.save_grid(grids.synthetic_grid(model="gfs", run="2026010100"), scope="greece")
    grids.save_grid(grids.synthetic_grid(model="icon", run="2026010100"), scope="greece")
    store = grids.GridStore()

    assert store.ensure_loaded("gfs", "greece").model == "gfs"
    assert store.ensure_loaded("icon", "greece").model == "icon"


# ------------------------------------------------------- disk footprint

def test_disk_footprint_is_bounded_by_retention():
    """Four runs in, two kept: the directory must shrink, and stay small."""
    for run in ("2026010100", "2026010106", "2026010112", "2026010118"):
        grids.save_grid(grids.synthetic_grid(run=run), scope="greece")

    full = grids.disk_bytes()
    grids.prune_grids(keep=2)
    trimmed = grids.disk_bytes()

    assert trimmed < full
    assert grids._disk_run_counts()["gfs|greece"] == 2
