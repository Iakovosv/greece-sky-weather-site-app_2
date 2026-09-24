"""`app` must load `.env` *before* the modules that read a setting at import.

The bug these pin down: `app.py` imported `grids`, `wx`, `scheduler`, `billing`
and `astro` before calling `envfile.load()`. Those modules resolve seven settings
at import time, so a deploy configured through `.env` - the documented way, and
the only way on a plain VPS - silently ran with the development defaults:

    WX_GRID_KEEP_RUNS   grids.GRID_KEEP_RUNS
    WX_RUN_LOOKUP_TTL_S wx.RUN_LOOKUP_TTL_S
    WX_RAM_GFS_HOURS    scheduler.GFS_MAX_HOURS
    WX_RAM_REFRESH_S    scheduler.REFRESH_INTERVAL_S
    WX_RAM_RETRY_S      scheduler.RETRY_INTERVAL_S
    WX_SUB_STATE_TTL_S  billing.STATE_TTL_S
    WX_ASTRO_TZ         astro.TZ_NAME

The property under test is the observable behaviour, not the import order: an
`.env` next to the code must set these seven values, real environment variables
must still win, and with no `.env` the defaults must be exactly what they were.

Everything runs in a subprocess against a throwaway copy of the tree - a fresh
interpreter is the only way to re-create import order, and the copy guarantees
no real `.env`, cache dir or database is touched.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]

# (env var, module, attribute, .env value, expected type)
IMPORT_TIME_SETTINGS = [
    ("WX_GRID_KEEP_RUNS", "grids", "GRID_KEEP_RUNS", "7", int),
    ("WX_RUN_LOOKUP_TTL_S", "wx", "RUN_LOOKUP_TTL_S", "42", float),
    ("WX_RAM_GFS_HOURS", "scheduler", "GFS_MAX_HOURS", "180", int),
    ("WX_RAM_REFRESH_S", "scheduler", "REFRESH_INTERVAL_S", "1234", int),
    ("WX_RAM_RETRY_S", "scheduler", "RETRY_INTERVAL_S", "99", int),
    ("WX_SUB_STATE_TTL_S", "billing", "STATE_TTL_S", "77", int),
    ("WX_ASTRO_TZ", "astro", "TZ_NAME", "Europe/London", str),
]

# Every variable any of the tests sets. Stripped from the child environment so a
# value already exported on the developer's machine cannot mask a regression.
ALL_KEYS = [k for k, *_ in IMPORT_TIME_SETTINGS] + ["WX_CACHE_DIR", "WX_ENV",
                                                    "WX_SECRET", "WX_MASTER_CODE"]

_READER = """
import importlib, os, sys
import app  # noqa: F401 - the import order under test lives here
import grids, wx, scheduler, billing, astro
print(grids.GRID_KEEP_RUNS)
print(wx.RUN_LOOKUP_TTL_S)
print(scheduler.GFS_MAX_HOURS)
print(scheduler.REFRESH_INTERVAL_S)
print(scheduler.RETRY_INTERVAL_S)
print(billing.STATE_TTL_S)
print(astro.TZ_NAME)
"""


def _clean_env_dir() -> str:
    """A copy of the code with no `.env`, so nothing leaks in from the repo."""
    d = tempfile.mkdtemp(prefix="wx-import-order-")
    for src in PROJECT.glob("*.py"):
        shutil.copy2(src, os.path.join(d, src.name))
    return d


def _run(workdir: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ALL_KEYS}
    env["PYTHONPATH"] = workdir
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, "-c", _READER], capture_output=True,
                          text=True, cwd=workdir, env=env, timeout=120)


@pytest.fixture()
def workdir():
    d = _clean_env_dir()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_env_file_reaches_every_import_time_setting(workdir):
    """The regression: each setting must take its `.env` value, not the default."""
    lines = [f"{key}={value}" for key, _mod, _attr, value, _t in IMPORT_TIME_SETTINGS]
    lines.append(f"WX_CACHE_DIR={os.path.join(workdir, 'cache')}")
    with open(os.path.join(workdir, ".env"), "w") as f:
        f.write("\n".join(lines) + "\n")

    out = _run(workdir)
    assert out.returncode == 0, out.stderr
    resolved = out.stdout.strip().splitlines()
    assert len(resolved) == len(IMPORT_TIME_SETTINGS), resolved

    misses = []
    for (key, mod, attr, want, cast), got in zip(IMPORT_TIME_SETTINGS, resolved):
        if cast(got) != cast(want):
            misses.append(f"{key} ({mod}.{attr}) = {got!r}, expected {want!r}")
    assert not misses, ".env was ignored for: " + "; ".join(misses)


def test_without_a_env_file_the_defaults_are_unchanged(workdir):
    """No `.env` must mean exactly the old defaults - this change adds nothing."""
    out = _run(workdir)
    assert out.returncode == 0, out.stderr
    got = out.stdout.strip().splitlines()
    expected = ["2", "300.0", "240", "21600", "1200", "600", "Europe/Athens"]
    assert got == expected, got


def test_real_environment_variables_still_win_over_the_env_file(workdir):
    """The systemd `Environment=` deploy: the file must not quietly override it."""
    with open(os.path.join(workdir, ".env"), "w") as f:
        f.write("WX_GRID_KEEP_RUNS=7\nWX_ASTRO_TZ=Europe/London\n")
    out = _run(workdir, {"WX_GRID_KEEP_RUNS": "5", "WX_ASTRO_TZ": "Europe/Paris"})
    assert out.returncode == 0, out.stderr
    got = out.stdout.strip().splitlines()
    assert got[0] == "5", "the real environment lost to .env"
    assert got[6] == "Europe/Paris", "the real environment lost to .env"
