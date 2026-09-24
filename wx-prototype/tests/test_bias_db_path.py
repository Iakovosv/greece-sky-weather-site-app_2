"""`bias` must resolve the station DB lazily, exactly like every other module.

The bug these pin down: `bias` read `WX_DB` into a module constant at import
time, but `app.py` imports `bias` (line 36) *before* `envfile.load()` (line 55).
A deploy configured by `.env` therefore had `bias` writing to a different
`station.db` than `promo` and `analytics`, so the backup of `WX_DB` silently
missed forecast history and observations.

The important property is that the two answers agree, under all three ways a
process can be configured: a `.env` loaded after import, only `WX_CACHE_DIR`
after import, and the real environment set before import. Nothing here asserts
*which* path wins - only that `bias` and `config` never disagree.

No network, no real /tmp/wx-cache: `WX_CACHE_DIR` is always a tmp dir, and a
test that would create the legacy path fails.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
LEGACY = "/tmp/wx-cache/station.db"

sys.path.insert(0, str(PROJECT))

import bias    # noqa: E402
import config  # noqa: E402


def _same(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _legacy_snapshot() -> tuple[bool, float]:
    """(exists, mtime) of the legacy path.

    Asserted unchanged rather than absent: on a machine that ever ran the old
    code the file legitimately exists, and the property under test is that *this*
    code no longer writes to it, not that a stale file was never there.
    """
    try:
        return True, os.stat(LEGACY).st_mtime_ns
    except FileNotFoundError:
        return False, 0.0


@pytest.fixture()
def clean_env(monkeypatch):
    """No cache/db tell-tales, and a tmp dir nothing may escape."""
    for name in ("WX_DB", "WX_CACHE_DIR"):
        monkeypatch.delenv(name, raising=False)
    with tempfile.TemporaryDirectory(prefix="wx-bias-") as d:
        yield d


# ------------------------------------------------------- same source of truth

def test_bias_delegates_to_config(clean_env):
    """The one thing that must hold unconditionally: a single answer."""
    monkeypatch_dir = os.path.join(clean_env, "cache")
    os.environ["WX_CACHE_DIR"] = monkeypatch_dir
    try:
        assert _same(bias.db_path(), config.db_path())
    finally:
        del os.environ["WX_CACHE_DIR"]


def test_only_wx_cache_dir_set_puts_both_in_the_same_db(clean_env):
    os.environ["WX_CACHE_DIR"] = os.path.join(clean_env, "cache")
    try:
        assert bias.db_path() == os.path.join(clean_env, "cache", "station.db")
        assert _same(bias.db_path(), config.db_path())
    finally:
        del os.environ["WX_CACHE_DIR"]


def test_wx_db_wins_over_the_cache_dir_for_both(clean_env):
    os.environ["WX_CACHE_DIR"] = os.path.join(clean_env, "cache")
    os.environ["WX_DB"] = os.path.join(clean_env, "data", "station.db")
    try:
        assert _same(bias.db_path(), config.db_path())
        assert bias.db_path() == os.path.join(clean_env, "data", "station.db")
    finally:
        del os.environ["WX_DB"]
        del os.environ["WX_CACHE_DIR"]


def test_a_change_after_import_is_honoured(clean_env):
    """Resolution happens per call, so a later change is not frozen out.

    This is the regression: with a module constant, the first value read at
    import time stuck for the life of the process.
    """
    first = os.path.join(clean_env, "cache-a")
    second = os.path.join(clean_env, "cache-b")
    os.environ["WX_CACHE_DIR"] = first
    try:
        assert bias.db_path() == os.path.join(first, "station.db")
        os.environ["WX_CACHE_DIR"] = second
        assert bias.db_path() == os.path.join(second, "station.db")
        assert bias.db_path() != os.path.join(first, "station.db")
    finally:
        del os.environ["WX_CACHE_DIR"]


# ------------------------------------------- the .env-after-import scenario

def test_env_loaded_after_import_lands_on_one_db(clean_env):
    """Reproduce app.py's ordering in a child process, .env style.

    Done in a subprocess because the point is the import order itself: `bias`
    must be imported before `.env` is read, and that cannot be re-created once
    the module is already imported in this interpreter.
    """
    before = _legacy_snapshot()
    env_file = os.path.join(clean_env, ".env")
    db = os.path.join(clean_env, "srv", "station.db")
    with open(env_file, "w") as f:
        f.write(f"WX_DB={db}\n")

    code = f"""
import sys
sys.path.insert(0, {str(PROJECT)!r})
import bias, config, envfile
applied = envfile.load({env_file!r})
print(applied)
print(bias.db_path())
print(config.db_path())
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={k: v for k, v in os.environ.items()
                              if k not in ("WX_DB", "WX_CACHE_DIR")},
                         cwd=clean_env)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert "WX_DB" in lines[0], f".env was not applied: {lines[0]!r}"
    assert _same(lines[1], lines[2]), f"bias and config disagree: {lines[1:]}"
    assert os.path.realpath(lines[1]) == os.path.realpath(db)
    assert _legacy_snapshot() == before, "the legacy path was written to"


def test_env_loaded_after_import_with_only_the_cache_dir(clean_env):
    """The same ordering, but the more common deploy: only WX_CACHE_DIR is set."""
    before = _legacy_snapshot()
    env_file = os.path.join(clean_env, ".env")
    cache = os.path.join(clean_env, "srv", "cache")
    with open(env_file, "w") as f:
        f.write(f"WX_CACHE_DIR={cache}\n")

    code = f"""
import sys
sys.path.insert(0, {str(PROJECT)!r})
import bias, config, envfile
print(envfile.load({env_file!r}))
print(bias.db_path())
print(config.db_path())
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={k: v for k, v in os.environ.items()
                              if k not in ("WX_DB", "WX_CACHE_DIR")},
                         cwd=clean_env)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert _same(lines[1], lines[2]), f"bias and config disagree: {lines[1:]}"
    assert os.path.realpath(lines[1]) == os.path.realpath(os.path.join(cache, "station.db"))
    assert _legacy_snapshot() == before, "the legacy path was written to"


def test_environment_set_before_import_wins(clean_env):
    """The systemd `EnvironmentFile=` deployment: real env vars exist already."""
    db = os.path.join(clean_env, "srv", "station.db")
    code = f"""
import sys
sys.path.insert(0, {str(PROJECT)!r})
import bias, config
print(bias.db_path())
print(config.db_path())
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "WX_DB": db, "WX_CACHE_DIR": ""},
                         cwd=clean_env)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert os.path.realpath(lines[0]) == os.path.realpath(db)
    assert _same(lines[0], lines[1])


# ------------------------------------------------------- operations still work

def test_db_operations_write_to_the_resolved_path(clean_env):
    """The whole point of the path: a real write must land where config says."""
    before = _legacy_snapshot()
    cache = os.path.join(clean_env, "cache")
    os.environ["WX_CACHE_DIR"] = cache
    try:
        bias.init_db()
        bias.record_forecasts("2026-01-01T00:00:00Z", 37.98, 23.73, "gfs",
                              [{"step": 1, "t2m_c": 20.0}])
        expected = os.path.join(cache, "station.db")
        assert os.path.exists(expected), "nothing was written to the resolved path"
        assert _same(bias.db_path(), expected)
        with bias._db() as con:
            n = con.execute("SELECT COUNT(*) FROM model_fcst").fetchone()[0]
        assert n >= 1
        assert _legacy_snapshot() == before, "a second DB was created at the legacy path"
    finally:
        del os.environ["WX_CACHE_DIR"]


def test_recent_obs_and_station_helpers_still_work(clean_env):
    before = _legacy_snapshot()
    os.environ["WX_CACHE_DIR"] = os.path.join(clean_env, "cache")
    try:
        bias.init_db()
        bias.register_station("st-1", "k", "Test", 37.98, 23.73, 100.0)
        row = bias.get_station("st-1")
        assert row["name"] == "Test"
        # public_station must stay the only passkey-free projection.
        pub = bias.public_station(row)
        assert "passkey" not in pub
        assert bias.recent_obs("st-1", limit=5) == []
        assert _legacy_snapshot() == before
    finally:
        del os.environ["WX_CACHE_DIR"]
