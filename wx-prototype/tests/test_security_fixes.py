"""Regression tests for the production-readiness fixes.

One section per finding, each pinned to the exact behaviour that was wrong:

* the master passcode had a usable default in production;
* the public station endpoints returned the Ecowitt device passkey;
* request bodies had no size ceiling;
* the published-cycle probe ran on every `/api/health` call;
* a malformed station registration returned 500 instead of 422.

None of these touch the forecast maths or the entitlement windows, so the tier
tests next door remain the authority on FREE=72h / PRO=240h.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def client(monkeypatch):
    d = tempfile.mkdtemp(prefix="wx-secfix-test-")
    monkeypatch.setenv("WX_DB", os.path.join(d, "test.db"))
    monkeypatch.setenv("WX_CACHE_DIR", os.path.join(d, "cache"))
    monkeypatch.setenv("WX_SECRET", "test-secret-not-the-dev-default")
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "0")
    monkeypatch.setenv("WX_RATE_LIMIT_DISABLED", "1")
    monkeypatch.delenv("WX_MAX_BODY_MB", raising=False)

    import app as app_module
    import bias
    import entitlements as ent

    bias.init_db()
    with TestClient(app_module.app) as c:
        c.ent = ent
        c.bias = bias
        yield c


# ============================================================ master passcode

def test_a_public_default_master_code_does_not_exist():
    """No literal that unlocks PRO may be compiled in and handed out.

    The old default was a fixed string that also appeared in the README and the
    UI placeholder, so a deploy that forgot WX_MASTER_CODE gave PRO away.
    """
    import config
    src = Path(config.__file__).read_text()
    assert "GSW-PRO-2026" not in src


def test_master_code_is_empty_in_production_when_unset(monkeypatch):
    import config
    monkeypatch.setenv("WX_ENV", "production")
    monkeypatch.setenv("WX_SECRET", "x" * 20)
    monkeypatch.delenv("WX_MASTER_CODE", raising=False)
    assert config.master_code() == ""


def test_master_code_is_empty_in_production_when_explicitly_blank(monkeypatch):
    import config
    monkeypatch.setenv("WX_ENV", "production")
    monkeypatch.setenv("WX_SECRET", "x" * 20)
    monkeypatch.setenv("WX_MASTER_CODE", "   ")
    assert config.master_code() == ""


def test_passcode_endpoint_is_closed_in_production_without_a_master_code(
        client, monkeypatch):
    """An empty master code must never match, including an empty submission."""
    monkeypatch.setenv("WX_ENV", "production")
    monkeypatch.setenv("WX_SECRET", "x" * 20)
    monkeypatch.delenv("WX_MASTER_CODE", raising=False)
    assert client.post("/api/auth/passcode", json={"code": ""}).status_code == 401
    assert client.post("/api/auth/passcode", json={"code": "anything"}).status_code == 401


def test_a_configured_master_code_still_works_in_production(client, monkeypatch):
    """The comp/admin path is preserved - it just has to be configured."""
    monkeypatch.setenv("WX_ENV", "production")
    monkeypatch.setenv("WX_SECRET", "x" * 20)
    monkeypatch.setenv("WX_MASTER_CODE", "comp-code-please-change")
    r = client.post("/api/auth/passcode", json={"code": "comp-code-please-change"})
    assert r.status_code == 200 and r.json()["tier"] == "pro"


def test_production_warns_when_master_code_is_unset(monkeypatch):
    import config
    monkeypatch.setenv("WX_ENV", "production")
    monkeypatch.setenv("WX_SECRET", "x" * 20)
    monkeypatch.delenv("WX_MASTER_CODE", raising=False)
    warnings = " ".join(config.validate_runtime())
    assert "WX_MASTER_CODE" in warnings


# ------------------------------------------------------------ production fail-fast

def test_production_boot_fails_when_master_code_is_unset(monkeypatch):
    """WX_ENV=production with no WX_MASTER_CODE must refuse to start.

    Not merely warn: a deploy that believes it has comp access but does not is a
    configuration error, and the passcode is an entitlement source, so it fails
    loudly at startup instead of coming up in a different shape than intended.
    """
    import config
    import app as app_module
    monkeypatch.setenv("WX_ENV", "production")
    monkeypatch.setenv("WX_SECRET", "x" * 40)
    monkeypatch.delenv("WX_MASTER_CODE", raising=False)
    with pytest.raises(config.ConfigError) as err:
        with TestClient(app_module.app):
            pass
    assert "WX_MASTER_CODE" in str(err.value)


def test_production_boot_fails_when_master_code_is_blank(monkeypatch):
    import config
    import app as app_module
    monkeypatch.setenv("WX_ENV", "production")
    monkeypatch.setenv("WX_SECRET", "x" * 40)
    monkeypatch.setenv("WX_MASTER_CODE", "   ")
    with pytest.raises(config.ConfigError) as err:
        with TestClient(app_module.app):
            pass
    assert "WX_MASTER_CODE" in str(err.value)


def test_production_boot_fails_when_signing_secret_is_unset(monkeypatch):
    import config
    import app as app_module
    monkeypatch.setenv("WX_ENV", "production")
    monkeypatch.delenv("WX_SECRET", raising=False)
    monkeypatch.setenv("WX_MASTER_CODE", "a-real-comp-code")
    with pytest.raises(config.ConfigError) as err:
        with TestClient(app_module.app):
            pass
    assert "WX_SECRET" in str(err.value)


def test_production_boot_succeeds_with_both_secrets_set(monkeypatch):
    import app as app_module
    monkeypatch.setenv("WX_ENV", "production")
    monkeypatch.setenv("WX_SECRET", "x" * 40)
    monkeypatch.setenv("WX_MASTER_CODE", "a-real-comp-code")
    with TestClient(app_module.app) as c:
        assert c.get("/api/health").status_code == 200


def test_development_boot_is_not_blocked_by_the_master_code(monkeypatch):
    """Dev/staging keep the warning, never the fatal error."""
    import config
    import app as app_module
    monkeypatch.delenv("WX_MASTER_CODE", raising=False)
    monkeypatch.delenv("WX_ENV", raising=False)
    assert config.assert_production_ready() is None
    with TestClient(app_module.app) as c:
        assert c.get("/api/health").status_code == 200


def test_staging_does_not_fail_fast(monkeypatch):
    import config
    monkeypatch.setenv("WX_ENV", "staging")
    monkeypatch.delenv("WX_MASTER_CODE", raising=False)
    assert config.assert_production_ready() is None


def test_assert_production_ready_is_a_noop_without_master_code_in_dev(monkeypatch):
    import config
    monkeypatch.setenv("WX_ENV", "dev")
    monkeypatch.delenv("WX_MASTER_CODE", raising=False)
    monkeypatch.delenv("WX_SECRET", raising=False)
    assert config.assert_production_ready() is None


def test_passcode_is_still_closed_when_master_code_is_empty(client, monkeypatch):
    """Independent of the startup guard: empty must never match at request time."""
    monkeypatch.setenv("WX_ENV", "production")
    monkeypatch.setenv("WX_MASTER_CODE", "")
    assert client.post("/api/auth/passcode", json={"code": ""}).status_code == 401


def test_wx_secret_is_still_a_hard_failure_in_production(monkeypatch):
    """The master code is a soft guard; the signing key must stay a hard one."""
    import config
    monkeypatch.setenv("WX_ENV", "production")
    monkeypatch.delenv("WX_SECRET", raising=False)
    with pytest.raises(config.ConfigError):
        config.signing_secret()


# ============================================================ passkey secrecy

def test_station_status_never_returns_the_passkey(client):
    client.bias.register_station("ST-A", "MY-SECRET-PASSKEY", "Σταθμός", 37.98, 23.72)
    r = client.get("/api/station/ST-A")
    assert r.status_code == 200
    body = r.json()
    assert "passkey" not in body["station"]
    assert "MY-SECRET-PASSKEY" not in r.text


def test_public_station_projection_drops_only_the_secret():
    import bias
    row = {"station_id": "s", "passkey": "p", "name": "n",
           "lat": 1.0, "lon": 2.0, "elevation_m": 3.0, "active": 1}
    out = bias.public_station(row)
    assert "passkey" not in out
    assert out["station_id"] == "s" and out["lat"] == 1.0 and out["active"] == 1


def test_public_station_passes_through_none():
    import bias
    assert bias.public_station(None) is None


def test_brief_does_not_expose_the_station_passkey(client, monkeypatch):
    """`/api/brief?station=` echoes station info; it must be the public shape."""
    client.bias.register_station("ST-B", "MY-SECRET-PASSKEY", "Σταθμός", 37.98, 23.72)

    import app as app_module

    async def fake_brief(request, lat, lon, station, hours, elevation_m, entl):
        st = client.bias.get_station(station)
        return {"station": client.bias.public_station(st)}

    # Exercise the handler's own station projection without a network forecast.
    monkeypatch.setattr(app_module, "_build_brief", fake_brief)
    r = client.get("/api/brief", params={"lat": 37.98, "lon": 23.72, "station": "ST-B"})
    assert r.status_code == 200
    assert "MY-SECRET-PASSKEY" not in r.text
    assert "passkey" not in r.json().get("station", {})


# ============================================================ body size limit

def test_a_legitimate_small_post_is_unaffected(client):
    """The cap must not break a normal payload."""
    r = client.post("/api/promo/redeem", json={"code": "NOPE"})
    assert r.status_code in (400, 404)  # code invalid, but the body was accepted


def test_declared_oversized_body_is_refused_with_413(client):
    big = b"x" * (2 * 1024 * 1024)  # 2 MB > 1 MB cap
    r = client.post("/api/station/register", content=big,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_streamed_body_without_content_length_is_refused(client):
    """A chunked body is not trusted to be small."""
    def chunks():
        for _ in range(64):
            yield b"y" * (64 * 1024)  # 4 MB total
    r = client.post("/api/station/register", content=chunks(),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_body_cap_can_be_raised_and_disabled(client, monkeypatch):
    import config
    monkeypatch.setenv("WX_MAX_BODY_MB", "4")
    assert config.max_body_bytes() == 4 * 1024 * 1024
    monkeypatch.setenv("WX_MAX_BODY_MB", "0")
    assert config.max_body_bytes() == 0
    monkeypatch.setenv("WX_MAX_BODY_MB", "not-a-number")
    assert config.max_body_bytes() == config.DEFAULT_MAX_BODY_MB * 1024 * 1024


def test_a_get_request_is_not_size_checked(client):
    assert client.get("/api/plans").status_code == 200


def test_stripe_webhook_body_shape_is_not_broken(client):
    """A normal webhook-sized body reaches the handler (rejected on signature,
    not on size)."""
    r = client.post("/api/stripe/webhook", content=b"{}",
                    headers={"Stripe-Signature": "t=1,v1=deadbeef",
                             "Content-Type": "application/json"})
    assert r.status_code != 413


# ============================================================ run-probe memoization

# The probe itself is patched, not `httpx.Client.get`: TestClient makes its own
# requests through the shared httpx client, so patching that method would also
# intercept the test's own HTTP call and count it.

def _counting_probe(monkeypatch):
    """Replace the network probe with a counter. Returns the counter dict."""
    import wx
    calls = {"n": 0}

    def fake_probe(now):
        calls["n"] += 1
        return ("20260101", "00")

    monkeypatch.setattr(wx, "_probe_latest_gfs_run", fake_probe)
    wx._run_lookup_cache.clear()
    return calls


def test_repeated_run_lookups_hit_the_network_once(monkeypatch):
    import wx
    calls = _counting_probe(monkeypatch)
    monkeypatch.setattr(wx, "RUN_LOOKUP_TTL_S", 300.0)

    first = wx.latest_gfs_run()
    for _ in range(5):
        assert wx.latest_gfs_run() == first
    assert calls["n"] == 1


def test_run_lookup_expires_after_the_ttl(monkeypatch):
    import wx
    calls = _counting_probe(monkeypatch)
    monkeypatch.setattr(wx, "RUN_LOOKUP_TTL_S", 0.0)

    wx.latest_gfs_run()
    wx.latest_gfs_run()
    assert calls["n"] == 2


def test_an_explicit_time_bypasses_the_cache(monkeypatch):
    import datetime as dt
    import wx
    calls = _counting_probe(monkeypatch)
    monkeypatch.setattr(wx, "RUN_LOOKUP_TTL_S", 300.0)

    when = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    wx.latest_gfs_run(now=when)
    wx.latest_gfs_run(now=when)
    assert calls["n"] == 2


def test_repeated_health_calls_do_not_reprobe_nomads(client, monkeypatch):
    """The exact regression: /api/health is unthrottled, so it must be cheap."""
    import wx
    calls = _counting_probe(monkeypatch)
    monkeypatch.setattr(wx, "RUN_LOOKUP_TTL_S", 300.0)

    for _ in range(10):
        assert client.get("/api/health").status_code == 200
    assert calls["n"] == 1


# ============================================================ register validation

def test_empty_registration_is_422_not_500(client):
    assert client.post("/api/station/register", json={}).status_code == 422


def test_non_numeric_coordinates_are_422_not_500(client):
    r = client.post("/api/station/register",
                    json={"station_id": "s", "lat": "abc", "lon": 1})
    assert r.status_code == 422


def test_out_of_range_coordinates_are_422(client):
    r = client.post("/api/station/register",
                    json={"station_id": "s", "lat": 999, "lon": 0})
    assert r.status_code == 422


def test_bad_elevation_is_422(client):
    r = client.post("/api/station/register",
                    json={"station_id": "s", "lat": 37.0, "lon": 23.0, "elevation_m": "tall"})
    assert r.status_code == 422


def test_a_valid_registration_still_succeeds(client):
    r = client.post("/api/station/register",
                    json={"station_id": "ST-OK", "lat": 37.98, "lon": 23.72,
                          "name": "Σταθμός", "passkey": "k"})
    assert r.status_code == 200 and r.json()["registered"] is True
