"""A half-installed environment must be visible from one request.

`ephem` and several other packages are optional on purpose: the forecast works
without them and the affected card degrades. The cost of that design is that a
missing package is easy to miss, because nothing fails - so the deploy check has
to report what actually got installed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app.app)


def test_health_reports_optional_extras(client):
    h = client.get("/api/health").json()
    assert "optional" in h
    assert "astro_ephem" in h["optional"]
    assert isinstance(h["optional"]["astro_ephem"], bool)


def test_health_astro_flag_matches_the_real_import(client):
    """The flag must reflect the running interpreter, not a hardcoded value."""
    import astro
    h = client.get("/api/health").json()
    assert h["optional"]["astro_ephem"] is astro._HAVE_EPHEM


def test_health_never_leaks_credentials(client):
    h = client.get("/api/health").json()
    blob = str(h)
    for token in ("ghp_", "ghu_", "Bearer", "password"):
        assert token not in blob


def test_the_endpoint_actually_serves_the_card_in_this_install(client):
    """The astro unit tests cover the maths; this covers the deployment. If ephem
    is importable only by a different interpreter than the one running the app,
    every maths test still passes while the card is blank in the browser — which
    is exactly how this broke. No monkeypatching: the real route does the work.

    Deliberately not in test_astro.py, whose module-level skip would silence it
    in precisely the broken install it exists to catch."""
    r = client.get("/api/sky", params={"lat": 37.98, "lon": 23.73, "elev": 70})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True, (
        f"card not served by the running interpreter: {body.get('reason')} "
        f"(interpreter {body.get('interpreter')})")
    assert body["sun"]["rise"]["label"]
    assert body["sun"]["twilight"]["civil"]["dawn"]["label"]
    assert body["moon"]["illumination_pct"] is not None


def test_health_and_sky_agree_about_the_card(client):
    """A green /api/health must mean the card really works. They drifted apart in
    the bug: health read a boot-time flag while the card did the work."""
    h = client.get("/api/health").json()["optional"]
    s = client.get("/api/sky", params={"lat": 37.98, "lon": 23.73}).json()
    assert h["astro_ephem"] == s["available"]
    if not h["astro_ephem"]:
        assert "astro_fix" in h


def test_health_reports_the_import_error_when_the_wheel_is_broken(client, monkeypatch):
    """'Installed but will not load' and 'not installed' need different fixes, and
    health has to tell them apart instead of reporting both as missing."""
    import astro
    monkeypatch.setattr(astro, "_HAVE_EPHEM", False)
    monkeypatch.setattr(astro, "_IMPORT_ERROR", "ImportError: libgfortran.so.5")
    h = client.get("/api/health").json()["optional"]
    assert h["astro_import_error"] == "ImportError: libgfortran.so.5"
    monkeypatch.setattr(astro, "_IMPORT_ERROR", None)
    h2 = client.get("/api/health").json()["optional"]
    assert "astro_import_error" not in h2


# ------------------------------------------------------------- RAM grid status

def test_health_reports_ram_grid_state(client):
    """The RAM path runs in the background, so its failures are invisible unless
    health surfaces them. An operator must be able to see whether the flag is on,
    which runs are loaded, and whether the last refresh failed."""
    h = client.get("/api/health").json()
    assert "ram_grids" in h
    assert isinstance(h["ram_grids"]["enabled"], bool)
    assert isinstance(h["ram_grids"]["models"], dict)


def test_health_ram_flag_matches_the_environment(client, monkeypatch):
    import grids
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "1")
    assert client.get("/api/health").json()["ram_grids"]["enabled"] is True
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "0")
    assert client.get("/api/health").json()["ram_grids"]["enabled"] is False


def test_health_ram_status_surfaces_a_never_loaded_model(client, monkeypatch):
    """A model that only ever failed must still appear, with its error, so a
    silently broken scheduler is not mistaken for a slow one."""
    import grids
    fresh = grids.GridStore()
    fresh.mark_failure("gfs", "RuntimeError: NOMADS is down")
    monkeypatch.setattr(grids, "STORE", fresh)
    h = client.get("/api/health").json()["ram_grids"]["models"]
    assert "gfs" in h
    assert "NOMADS is down" in h["gfs"]["last_error"]


def test_health_surfaces_grid_persistence_state(client, monkeypatch, tmp_path):
    """Retention working must be visible, not inferred from the disk.

    `disk_runs` bounded to keep_runs is the operator's evidence that the archive
    set is not growing without limit, and `disk_bytes` answers "how much is this
    costing me" without an ssh session.
    """
    import grids
    monkeypatch.setenv("WX_CACHE_DIR", str(tmp_path / "cache"))
    for run in ("2026010100", "2026010106", "2026010112"):
        grids.save_grid(grids.synthetic_grid(run=run), scope="greece")
    grids.prune_grids(keep=2)

    persist = client.get("/api/health").json()["ram_grids"]["models"]["persist"]
    assert persist["keep_runs"] == 2
    assert persist["disk_runs"]["gfs|greece"] == 2
    assert persist["disk_bytes"] > 0
    assert persist["dir"].endswith("grids")

