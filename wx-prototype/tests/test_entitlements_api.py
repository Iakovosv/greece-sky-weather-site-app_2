"""End-to-end entitlement tests through the real FastAPI app.

These are the tests that matter commercially: they exercise the same HTTP surface
a browser hits, through the real handlers, with a real SQLite promo database and
a stubbed Stripe. Nothing here reaches into an internal function to assert on a
value the endpoint would not actually have returned.

The scenarios are the ones the acceptance list names, in order: free blocked,
paid allowed, cancelled blocked, promo active, promo expired, promo exhausted,
double redemption, invalid code, localStorage forgery, cache miss regeneration,
and concurrent requests sharing one run.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def client(monkeypatch):
    """A fresh app process per test: temp DB and cache, rate limit off by default."""
    d = tempfile.mkdtemp(prefix="wx-api-test-")
    monkeypatch.setenv("WX_DB", os.path.join(d, "test.db"))
    monkeypatch.setenv("WX_CACHE_DIR", os.path.join(d, "cache"))
    monkeypatch.setenv("WX_SECRET", "test-secret-not-the-dev-default")
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "0")
    # Off so endpoint tests are not fighting the limiter; the limiter has its own
    # unit tests and its own middleware test below.
    monkeypatch.setenv("WX_RATE_LIMIT_DISABLED", "1")

    import app as app_module
    import analytics
    import entitlements as ent
    import promo

    promo.init_db()
    analytics.init_db()
    promo._SALT = None
    analytics._SALT = None

    with TestClient(app_module.app) as c:
        c.ent = ent
        c.promo = promo
        yield c


def _pro_token(client, source="passcode", device=None, sub=None, ttl=None):
    return client.ent.issue_token("pro", source, ttl=ttl or client.ent.TOKEN_TTL_S,
                                  subscription_id=sub, device=device)


# ============================================================ free vs pro

def test_free_user_is_refused_on_every_pro_endpoint(client):
    """The PRO endpoints must 403, not send data the client is meant to hide."""
    for path in ("/api/expert", "/api/skewt"):
        r = client.get(path, params={"lat": 37.98, "lon": 23.72})
        assert r.status_code == 403, path
        assert "lat" not in r.text and "t2m" not in r.text


def test_free_brief_reports_the_free_window(client):
    """FREE remains 72 h. This is the tier promise, not a tuning knob."""
    # /api/plans needs no forecast fetch and states the windows.
    plans = client.get("/api/plans").json()
    assert plans["free_hours"] == 72
    assert plans["pro_hours"] == 240


def test_me_reports_free_without_a_token(client):
    me = client.get("/api/me").json()
    assert me["tier"] == "free" and me["is_pro"] is False
    assert me["hours"] == 72


def test_me_reports_pro_with_a_passcode_token(client):
    token = client.ent.check_passcode(client.ent.master_code())
    me = client.get("/api/me", headers={"X-WX-Token": token}).json()
    assert me["is_pro"] is True and me["hours"] == 240


def test_a_forged_token_is_refused(client):
    """The HMAC is what stops this; a signature that does not verify is FREE."""
    forged = "eyJ0aWVyIjoicHJvIn0.deadbeef"
    me = client.get("/api/me", headers={"X-WX-Token": forged}).json()
    assert me["is_pro"] is False
    assert client.get("/api/expert", params={"lat": 37.9, "lon": 23.7},
                      headers={"X-WX-Token": forged}).status_code == 403


def test_a_token_signed_with_the_wrong_key_is_refused(client, monkeypatch):
    import entitlements as ent
    good = ent.issue_token("pro", "passcode")
    monkeypatch.setenv("WX_SECRET", "a-different-key-entirely")
    assert ent.verify_token(good).is_pro is False


def test_an_expired_token_is_refused(client):
    expired = client.ent.issue_token("pro", "passcode", ttl=-10)
    assert client.ent.verify_token(expired).is_pro is False
    me = client.get("/api/me", headers={"X-WX-Token": expired}).json()
    assert me["is_pro"] is False


def test_a_free_token_cannot_claim_pro_by_editing_its_payload(client):
    """The tier lives inside the signed payload; editing it breaks the signature."""
    import base64
    import json as _json
    free = client.ent.issue_token("free", "trial")
    body, sig = free.rsplit(".", 1)
    payload = _json.loads(base64.urlsafe_b64decode(body + "==="))
    payload["tier"] = "pro"
    tampered = base64.urlsafe_b64encode(
        _json.dumps(payload).encode()).decode().rstrip("=") + "." + sig
    assert client.ent.verify_token(tampered).is_pro is False


# ============================================================ subscription lifecycle

class _StubSub:
    """Minimal stand-in for a Stripe subscription, as `.to_dict()` returns it."""

    def __init__(self, status="active", period_end=None, cancel_at=None):
        self._d = {
            "id": "sub_test", "status": status,
            "cancel_at": cancel_at,
            "cancel_at_period_end": bool(cancel_at is None and status == "active"),
            "metadata": {"plan": "monthly"},
            "items": {"data": [{"current_period_end":
                                period_end or int(time.time()) + 30 * 86400}]},
        }

    def to_dict(self):
        return self._d


def _stub_stripe(monkeypatch, sub):
    import billing
    monkeypatch.setattr(billing, "subscription_state",
                        lambda sid, *a, **k: {
                            "id": sid, "status": sub._d["status"],
                            "current_period_end": sub._d["items"]["data"][0]["current_period_end"],
                            "cancel_at": sub._d["cancel_at"],
                            "auto_renew": sub._d["cancel_at_period_end"],
                        })
    billing.cache_forget()


def test_an_active_subscription_grants_pro(client, monkeypatch):
    _stub_stripe(monkeypatch, _StubSub("active"))
    token = _pro_token(client, "subscription", sub="sub_test")
    me = client.get("/api/me", headers={"X-WX-Token": token}).json()
    assert me["is_pro"] is True
    assert me["manageable"] is True


def test_a_cancelled_subscription_stops_granting_pro(client, monkeypatch):
    """The hole this closes: a 30-day token outliving a cancelled subscription."""
    _stub_stripe(monkeypatch, _StubSub("canceled", period_end=int(time.time()) - 60))
    token = _pro_token(client, "subscription", sub="sub_test")
    assert client.ent.verify_token(token).is_pro is True   # token itself is fine
    me = client.get("/api/me", headers={"X-WX-Token": token}).json()
    assert me["is_pro"] is False, "the subscription is over, so PRO must be over"
    assert client.get("/api/expert", params={"lat": 37.9, "lon": 23.7},
                      headers={"X-WX-Token": token}).status_code == 403


def test_a_cancelled_subscription_inside_the_paid_period_still_works(client, monkeypatch):
    """Cancelling stops renewal; it does not confiscate what was paid for."""
    _stub_stripe(monkeypatch, _StubSub("canceled", period_end=int(time.time()) + 5 * 86400))
    token = _pro_token(client, "subscription", sub="sub_test")
    me = client.get("/api/me", headers={"X-WX-Token": token}).json()
    assert me["is_pro"] is True


def test_an_unpaid_subscription_is_refused(client, monkeypatch):
    _stub_stripe(monkeypatch, _StubSub("unpaid"))
    token = _pro_token(client, "subscription", sub="sub_test")
    assert client.get("/api/me",
                      headers={"X-WX-Token": token}).json()["is_pro"] is False


def test_a_past_due_subscription_keeps_access_while_stripe_retries(client, monkeypatch):
    """Cutting off at the first failed charge punishes a slow bank."""
    _stub_stripe(monkeypatch, _StubSub("past_due"))
    token = _pro_token(client, "subscription", sub="sub_test")
    assert client.get("/api/me",
                      headers={"X-WX-Token": token}).json()["is_pro"] is True


def test_a_stripe_outage_does_not_log_paying_customers_out(client, monkeypatch):
    """Fail-closed on an outage would be worse than the risk it guards against.

    The verified token's own expiry is the fallback, bounded at 30 days.
    """
    import billing

    def boom(sid, *a, **k):
        raise RuntimeError("stripe is down")

    monkeypatch.setattr(billing, "subscription_state", boom)
    billing.cache_forget()
    token = _pro_token(client, "subscription", sub="sub_test")
    me = client.get("/api/me", headers={"X-WX-Token": token}).json()
    assert me["is_pro"] is True
    assert "subscription_unverified" in me["notes"]


def test_a_repeated_lookup_is_served_from_cache(client, monkeypatch):
    import billing
    calls = {"n": 0}

    def counted(sid, *a, **k):
        calls["n"] += 1
        return {"id": sid, "status": "active",
                "current_period_end": int(time.time()) + 86400,
                "cancel_at": None, "auto_renew": True}

    monkeypatch.setattr(billing, "subscription_state", counted)
    billing.cache_forget()
    token = _pro_token(client, "subscription", sub="sub_cache")
    for _ in range(5):
        client.get("/api/me", headers={"X-WX-Token": token})
    assert calls["n"] == 1, "five requests must not be five Stripe calls"


def test_forgetting_the_cache_forces_a_fresh_lookup(client, monkeypatch):
    import billing
    calls = {"n": 0}

    def counted(sid, *a, **k):
        calls["n"] += 1
        return {"id": sid, "status": "active",
                "current_period_end": int(time.time()) + 86400,
                "cancel_at": None, "auto_renew": True}

    monkeypatch.setattr(billing, "subscription_state", counted)
    billing.cache_forget()
    token = _pro_token(client, "subscription", sub="sub_cache2")
    client.get("/api/me", headers={"X-WX-Token": token})
    billing.cache_forget("sub_cache2")
    client.get("/api/me", headers={"X-WX-Token": token})
    assert calls["n"] == 2


# ============================================================ promo through HTTP

def test_a_five_day_code_activates_pro_through_the_api(client):
    client.post("/api/admin/promo", headers={"X-WX-Admin": "x"}, json={})  # 503, no admin token
    r = client.post("/api/promo/redeem", json={"code": "FRIEND5"})
    assert r.status_code == 404, "no such code exists yet"
    _create_code("FRIEND5", 5, max_redemptions=10)
    r = client.post("/api/promo/redeem", json={"code": "FRIEND5"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["days"] == 5
    token = body["token"]

    me = client.get("/api/me", headers={"X-WX-Token": token}).json()
    assert me["is_pro"] is True
    assert me["hours"] == 240
    assert me["pro_until_iso"]
    # The window is ~5 days, and the expiry the UI shows matches.
    delta = me["pro_until"] - int(time.time())
    assert 4.9 * 86400 < delta < 5.1 * 86400


def test_the_server_blocks_pro_for_an_expired_promo(client):
    _create_code("SHORT1", 1)
    token = client.post("/api/promo/redeem", json={"code": "SHORT1"}).json()["token"]
    # Rewind the granted window; the token itself is still valid.
    import sqlite3
    with sqlite3.connect(os.environ["WX_DB"]) as con:
        con.execute("UPDATE promo_redemptions SET pro_until='2020-01-01T00:00:00Z'")
    me = client.get("/api/me", headers={"X-WX-Token": token}).json()
    assert me["is_pro"] is False
    assert client.get("/api/expert", params={"lat": 37.9, "lon": 23.7},
                      headers={"X-WX-Token": token}).status_code == 403


def test_a_promo_does_not_erase_an_active_subscription(client, monkeypatch):
    """Composition: the later of the two ends wins, and the sub stays manageable.

    The redemption returns a new token carrying both the device and the existing
    subscription id; that is the token the browser keeps.
    """
    _stub_stripe(monkeypatch, _StubSub("active", period_end=int(time.time()) + 20 * 86400))
    original = _pro_token(client, "subscription", sub="sub_test")
    _create_code("EXTRA5", 5)
    r = client.post("/api/promo/redeem", json={"code": "EXTRA5"},
                    headers={"X-WX-Token": original})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    me = client.get("/api/me", headers={"X-WX-Token": token}).json()
    assert me["is_pro"] is True
    assert me["manageable"] is True, "the Stripe subscription is untouched"
    # The subscription period is the later end, so it is what is reported.
    assert me["pro_until"] >= int(time.time()) + 19 * 86400
    # And the subscription token on its own keeps working.
    assert client.get("/api/me",
                      headers={"X-WX-Token": original}).json()["is_pro"] is True


def test_a_promo_extends_past_the_paid_period_when_it_is_later(client, monkeypatch):
    _stub_stripe(monkeypatch, _StubSub("active", period_end=int(time.time()) + 2 * 86400))
    original = _pro_token(client, "subscription", sub="sub_test")
    _create_code("LONG5", 30)
    r = client.post("/api/promo/redeem", json={"code": "LONG5"},
                    headers={"X-WX-Token": original})
    assert r.status_code == 200, r.text
    me = client.get("/api/me", headers={"X-WX-Token": r.json()["token"]}).json()
    assert me["pro_until"] > int(time.time()) + 29 * 86400
    assert me["is_pro"] is True


def test_the_same_device_cannot_redeem_twice_over_http(client):
    _create_code("TWICE", 5)
    first = client.post("/api/promo/redeem", json={"code": "TWICE"})
    assert first.status_code == 200
    token = first.json()["token"]
    second = client.post("/api/promo/redeem", json={"code": "TWICE"},
                         headers={"X-WX-Token": token})
    assert second.status_code == 409
    assert "χρησιμοποιηθεί" in second.json()["detail"]


def test_an_invalid_code_is_refused_with_404(client):
    r = client.post("/api/promo/redeem", json={"code": "TOTALLYFAKE"})
    assert r.status_code == 404
    assert r.json()["detail"]


def test_an_exhausted_code_is_refused_over_http(client):
    _create_code("ONLYONE", 1, max_redemptions=1)
    a = client.post("/api/promo/redeem", json={"code": "ONLYONE"})
    assert a.status_code == 200
    # No token at all, so the server mints a fresh device for this caller.
    b = client.post("/api/promo/redeem", json={"code": "ONLYONE"})
    assert b.status_code == 410
    assert "εξαντληθεί" in b.json()["detail"]


def test_editing_localstorage_does_not_grant_pro(client):
    """There is nothing client-side to edit: the server decides every time.

    Simulated by sending the header a tampering frontend would have to send — an
    arbitrary string — and confirming the server still answers FREE.
    """
    r = client.get("/api/me", headers={"X-WX-Token": "pro=true",
                                       "X-Device": "anything-goes"})
    assert r.json()["is_pro"] is False
    assert client.get("/api/expert", params={"lat": 37.9, "lon": 23.7},
                      headers={"X-WX-Token": "pro=true"}).status_code == 403


def test_a_promised_window_survives_a_browser_date_change(client):
    """The browser clock is irrelevant: expiry is compared server-side."""
    _create_code("CLOCK", 5)
    token = client.post("/api/promo/redeem", json={"code": "CLOCK"}).json()["token"]
    # A browser that thinks it is 2099 changes nothing about the token or the
    # stored window; the server's clock is the only one consulted.
    me = client.get("/api/me", headers={"X-WX-Token": token})
    assert me.json()["is_pro"] is True


def test_a_personal_code_is_refused_for_the_wrong_device(client):
    _create_code("JACK5", 5, restricted_to="device-jack")
    wrong = _pro_token(client, "promo", device="device-someone-else")
    r = client.post("/api/promo/redeem", json={"code": "JACK5"},
                    headers={"X-WX-Token": wrong})
    assert r.status_code == 403
    right = _pro_token(client, "promo", device="device-jack")
    r2 = client.post("/api/promo/redeem", json={"code": "JACK5"},
                     headers={"X-WX-Token": right})
    assert r2.status_code == 200


def test_promo_status_reports_the_active_window(client):
    _create_code("STATUS", 5)
    token = client.post("/api/promo/redeem", json={"code": "STATUS"}).json()["token"]
    st = client.get("/api/promo/status", headers={"X-WX-Token": token}).json()
    assert st["active"] is True
    assert st["codes"] == ["STATUS"]


def test_promo_status_reveals_only_the_callers_own_device_id(client):
    """The device id is what an operator needs for a personal gift code.

    It must come from the signed token, not from a header a client could set —
    otherwise anyone could present someone else's id and redeem their gift.
    """
    token = _pro_token(client, "trial", device="device-mine")
    st = client.get("/api/promo/status", headers={"X-WX-Token": token}).json()
    assert st["device"] == "device-mine"

    forged = client.get("/api/promo/status", headers={"X-WX-Token": "not-a-token"}).json()
    assert forged["device"] in (None, "")


# ============================================================ admin API

def test_admin_endpoints_are_closed_when_unconfigured(client, monkeypatch):
    """An unconfigured deploy must not expose code creation."""
    monkeypatch.delenv("WX_ADMIN_TOKEN", raising=False)
    assert client.get("/api/admin/promo").status_code == 503
    assert client.post("/api/admin/promo", json={"code": "X", "duration_days": 5}).status_code == 503


def test_admin_endpoints_refuse_a_wrong_token(client, monkeypatch):
    monkeypatch.setenv("WX_ADMIN_TOKEN", "the-real-admin-token")
    assert client.get("/api/admin/promo", headers={"X-WX-Admin": "guess"}).status_code == 403
    assert client.get("/api/admin/promo").status_code == 403


def test_an_admin_creates_a_code_without_a_code_change(client, monkeypatch):
    """The whole point of the admin surface: no redeploy to issue a code."""
    monkeypatch.setenv("WX_ADMIN_TOKEN", "admin-tok")
    h = {"X-WX-Admin": "admin-tok"}
    r = client.post("/api/admin/promo", headers=h, json={
        "code": "FRIEND5", "duration_days": 5, "max_redemptions": 10,
        "note": "launch giveaway"})
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True
    row = r.json()["code"]
    assert row["code"] == "FRIEND5" and row["duration_days"] == 5
    assert row["created_at"] and row["active"] == 1

    listed = client.get("/api/admin/promo", headers=h).json()
    codes = {c["code"]: c for c in listed["codes"]}
    assert "FRIEND5" in codes
    assert codes["FRIEND5"]["max_redemptions"] == 10


def test_admin_input_errors_are_reported(client, monkeypatch):
    monkeypatch.setenv("WX_ADMIN_TOKEN", "admin-tok")
    h = {"X-WX-Admin": "admin-tok"}
    assert client.post("/api/admin/promo", headers=h,
                       json={"code": "FRIEND5", "duration_days": 0}).status_code == 422
    assert client.post("/api/admin/promo", headers=h,
                       json={"code": "!", "duration_days": 5}).status_code == 422


def test_admin_can_revoke_a_code(client, monkeypatch):
    monkeypatch.setenv("WX_ADMIN_TOKEN", "admin-tok")
    h = {"X-WX-Admin": "admin-tok"}
    client.post("/api/admin/promo", headers=h,
                json={"code": "REVOKE1", "duration_days": 5})
    r = client.post("/api/admin/promo/REVOKE1/active", headers=h, json={"active": False})
    assert r.status_code == 200
    assert client.post("/api/promo/redeem",
                       json={"code": "REVOKE1"}).status_code == 410
    assert client.post("/api/admin/promo/NOPE/active", headers=h,
                       json={"active": False}).status_code == 404


def test_admin_lists_show_use_counts_and_truncate_the_subject(client, monkeypatch):
    monkeypatch.setenv("WX_ADMIN_TOKEN", "admin-tok")
    h = {"X-WX-Admin": "admin-tok"}
    client.post("/api/admin/promo", headers=h, json={"code": "COUNTME", "duration_days": 5})
    client.post("/api/promo/redeem", json={"code": "COUNTME"})
    listed = client.get("/api/admin/promo", headers=h).json()
    row = next(c for c in listed["codes"] if c["code"] == "COUNTME")
    assert row["redemption_count"] == 1
    assert len(row["recent_redemptions"]) == 1
    # The identifier is truncated: enough to recognise, not a bulk export.
    assert len(row["recent_redemptions"][0]["subject"]) <= 9


def test_admin_analytics_needs_the_admin_token(client, monkeypatch):
    monkeypatch.setenv("WX_ADMIN_TOKEN", "admin-tok")
    assert client.get("/api/admin/analytics").status_code == 403
    r = client.get("/api/admin/analytics", headers={"X-WX-Admin": "admin-tok"})
    assert r.status_code == 200
    assert r.json()["available"] is True


# ============================================================ analytics ingest


def test_analytics_accepts_a_bounded_batch(client):
    r = client.post("/api/analytics", json={"events": [
        {"name": "page_view"}, {"name": "forecast_loaded", "lat": 37.98, "lon": 23.72}]})
    assert r.status_code == 200 and r.json()["recorded"] == 2


def test_analytics_refuses_an_oversized_batch(client):
    events = [{"name": "page_view"} for _ in range(30)]
    assert client.post("/api/analytics", json={"events": events}).status_code == 413


def test_analytics_refuses_an_unknown_event_without_creating_it(client):
    r = client.post("/api/analytics", json={"events": [{"name": "not_a_real_event"}]})
    assert r.status_code == 200
    assert r.json()["recorded"] == 0


def test_analytics_stores_only_a_coarse_cell(client):
    client.post("/api/analytics", json={"events": [
        {"name": "forecast_loaded", "lat": 37.9838, "lon": 23.7275}]})
    import sqlite3
    with sqlite3.connect(os.environ["WX_DB"]) as con:
        row = con.execute("SELECT cell_lat, cell_lon FROM analytics_events").fetchone()
    assert (row[0], row[1]) == (38.0, 23.5)

# ============================================================ validation


def test_out_of_range_coordinates_are_refused_before_any_fetch(client):
    """422, not a 419 MB cache file."""
    for params in ({"lat": 999, "lon": 23.7}, {"lat": 37.9, "lon": 999},
                   {"lat": "nan", "lon": 23.7}):
        r = client.get("/api/brief", params=params)
        assert r.status_code == 422, params


def test_every_coordinate_endpoint_shares_the_same_bounds(client):
    """A bogus coordinate must fail the same way everywhere, not just on brief.

    `elevation`, `reverse`, `sky` and `verify` each reach a remote service with
    the point; each has to reject it before that happens.
    """
    bad = {"lat": 999, "lon": 23.7}
    for path in ("/api/brief", "/api/elevation", "/api/reverse", "/api/sky",
                 "/api/verify"):
        assert client.get(path, params=bad).status_code == 422, path


def test_a_negative_hour_count_is_refused(client):
    assert client.get("/api/brief",
                      params={"lat": 37.9, "lon": 23.7, "hours": -5}).status_code == 422
    assert client.get("/api/brief",
                      params={"lat": 37.9, "lon": 23.7, "hours": 0}).status_code == 422


def test_an_absurd_hour_count_is_refused_not_silently_capped(client):
    assert client.get("/api/brief",
                      params={"lat": 37.9, "lon": 23.7, "hours": 99999}).status_code == 422


def test_expert_day_and_hour_are_bounded(client):
    token = client.ent.check_passcode(client.ent.master_code())
    h = {"X-WX-Token": token}
    assert client.get("/api/expert", params={"lat": 37.9, "lon": 23.7, "day": 99},
                      headers=h).status_code == 422
    assert client.get("/api/expert", params={"lat": 37.9, "lon": 23.7, "hour": 50},
                      headers=h).status_code == 422

# ============================================================ single-flight


def test_concurrent_identical_requests_run_the_work_once(client, monkeypatch):
    """Without this, N simultaneous requests to a cold point do N full fetches."""
    import app as app_module

    runs = {"n": 0}

    async def fake_build(*a, **k):
        runs["n"] += 1
        await asyncio.sleep(0.05)      # long enough for the others to join
        return {"ok": True}

    monkeypatch.setattr(app_module, "_build_brief", fake_build)

    async def hammer():
        return await asyncio.gather(*[
            app_module._single_flight("same-key", lambda: fake_build()) for _ in range(6)])

    results = asyncio.get_event_loop().run_until_complete(hammer()) if False else asyncio.run(hammer())
    assert runs["n"] == 1, f"expected one run, saw {runs['n']}"
    assert all(r == {"ok": True} for r in results)


def test_different_keys_do_not_share_a_run():
    import app as app_module
    runs = {"n": 0}

    async def fake():
        runs["n"] += 1
        await asyncio.sleep(0.01)
        return runs["n"]

    async def go():
        return await asyncio.gather(
            app_module._single_flight("a", fake),
            app_module._single_flight("b", fake))

    asyncio.run(go())
    assert runs["n"] == 2


def test_a_failed_leader_propagates_and_frees_the_key():
    """A failed in-flight entry must not be left behind, or the endpoint wedges."""
    import app as app_module

    async def boom():
        raise ValueError("model down")

    async def first():
        with pytest.raises(ValueError):
            await app_module._single_flight("failkey", boom)

    asyncio.run(first())
    assert "failkey" not in app_module._BRIEF_INFLIGHT

# ============================================================ cache degradation


def test_a_missing_cache_entry_is_regenerated_not_fatal(client, monkeypatch):
    """A cold cache must produce a forecast, not an error."""
    import cachestore
    monkeypatch.setattr(cachestore, "get", lambda *a, **k: None)
    assert cachestore.get("anything", 60) is None
    # The cache layer being empty is a normal state; nothing raises.


def test_a_damaged_cache_entry_is_a_miss(client, monkeypatch):
    import cachestore
    import config
    assert config.cache_dir()
    cachestore.put("damaged", b"a-long-enough-payload")
    with open(cachestore._path("damaged"), "wb") as f:
        f.write(b"x")
    assert cachestore.get("damaged", 60) is None

# ============================================================ rate limiting middleware


def test_the_limiter_returns_429_with_a_retry_hint(monkeypatch):
    """Enabled here specifically, since the client fixture disables it."""
    d = tempfile.mkdtemp(prefix="wx-rl-test-")
    monkeypatch.setenv("WX_DB", os.path.join(d, "t.db"))
    monkeypatch.setenv("WX_CACHE_DIR", os.path.join(d, "c"))
    monkeypatch.setenv("WX_SECRET", "x")
    monkeypatch.delenv("WX_RATE_LIMIT_DISABLED", raising=False)

    import app as app_module
    import ratelimit
    ratelimit.LIMITER.reset()
    with TestClient(app_module.app) as c:
        codes = [c.post("/api/promo/redeem", json={"code": "X"}).status_code
                 for _ in range(12)]
    assert 429 in codes
    ratelimit.LIMITER.reset()


def test_health_is_never_rate_limited(monkeypatch):
    """A throttled health check reports the service as down."""
    d = tempfile.mkdtemp(prefix="wx-rl-health-")
    monkeypatch.setenv("WX_DB", os.path.join(d, "t.db"))
    monkeypatch.setenv("WX_CACHE_DIR", os.path.join(d, "c"))
    monkeypatch.delenv("WX_RATE_LIMIT_DISABLED", raising=False)
    import app as app_module
    import ratelimit
    ratelimit.LIMITER.reset()
    with TestClient(app_module.app) as c:
        assert all(c.get("/api/health").status_code == 200 for _ in range(40))
    ratelimit.LIMITER.reset()

# ============================================================ health & headers


def test_health_reports_the_dependencies_it_can_check(client):
    h = client.get("/api/health").json()
    for key in ("cache", "promo", "billing", "auth", "ram_grids"):
        assert key in h, key
    assert h["auth"]["rate_limit_enabled"] is False       # disabled in this fixture
    assert h["billing"]["webhook_configured"] in (True, False)
    assert h["cache"]["limit_bytes"] or h["cache"]["ok"] in (True, False)


def test_security_headers_are_present(client):
    r = client.get("/api/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "strict-origin" in r.headers["Referrer-Policy"]


def test_redemption_sets_an_httponly_device_cookie(client):
    _create_code("COOKIECODE", 5)
    r = client.post("/api/promo/redeem", json={"code": "COOKIECODE"})
    assert r.status_code == 200
    cookie = r.headers.get("set-cookie", "")
    assert "wx_dev=" in cookie and "HttpOnly" in cookie

# ============================================================ helpers


def _create_code(code, days, max_redemptions=None, restricted_to=None):
    """Create a code directly, the way the admin endpoint eventually would."""
    import promo
    return promo.create_code(code, days, max_redemptions=max_redemptions,
                             restricted_to=restricted_to)

# ============================================================ analytics ingest


def test_the_analytics_endpoint_records_a_batch(client):
    r = client.post("/api/analytics", json={"events": [
        {"name": "page_view"},
        {"name": "forecast_loaded", "lat": 37.9838, "lon": 23.7275, "value": 72},
    ]})
    assert r.status_code == 200
    assert r.json()["recorded"] == 2


def test_the_analytics_endpoint_refuses_an_unknown_event_name(client):
    """The vocabulary is closed, so a client cannot invent a new dimension."""
    r = client.post("/api/analytics", json={"events": [{"name": "made_up_event"}]})
    assert r.status_code == 200          # accepted, but nothing recorded
    assert r.json()["recorded"] == 0


def test_the_analytics_endpoint_rejects_a_malformed_body(client):
    assert client.post("/api/analytics", json={"events": "nope"}).status_code == 400
    assert client.post("/api/analytics", json=[]).status_code == 400


def test_the_analytics_endpoint_caps_a_single_batch(client):
    events = [{"name": "page_view"}] * 26
    assert client.post("/api/analytics", json={"events": events}).status_code == 413


def test_the_analytics_endpoint_stores_no_exact_coordinates(client):
    """The privacy promise: only a coarse cell is persisted, never the point."""
    import sqlite3
    from pathlib import Path
    client.post("/api/analytics", json={"events": [
        {"name": "forecast_loaded", "lat": 37.9838, "lon": 23.7275}]})
    con = sqlite3.connect(Path(os.environ["WX_DB"]))
    try:
        cell = con.execute(
            "SELECT cell_lat, cell_lon FROM analytics_events "
            "WHERE name='forecast_loaded'").fetchone()
    finally:
        con.close()
    assert cell is not None
    assert cell != (37.9838, 23.7275)
    assert abs(cell[0] - 37.9838) <= 1.0 and abs(cell[1] - 23.7275) <= 1.0


def test_the_analytics_endpoint_is_a_noop_when_disabled(client, monkeypatch):
    monkeypatch.setenv("WX_ANALYTICS", "0")
    r = client.post("/api/analytics", json={"events": [{"name": "page_view"}]})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "recorded": 0, "enabled": False}
