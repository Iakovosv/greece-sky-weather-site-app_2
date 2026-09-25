"""Billing endpoint hardening (audit findings M-1 and L-2).

Two behaviours, both about what the *client* sees when Stripe is configured but
the request or the SDK misbehaves:

* M-1: a malformed or non-object body on `POST /api/checkout` and
  `POST /api/subscription/auto-renew` is a client mistake. It must read as 400,
  not surface as an unhandled 500 from `request.json()`.
* L-2: when the Stripe SDK raises, the response is neutral. The exception type
  and message are dependency detail an operator reads in the log; they must not
  be echoed to the caller.

These go through the real FastAPI routes. `billing` is driven with a stub Stripe
SDK (the same justified pattern `test_billing.py` uses) so the real handler code
runs without a live account or network.
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
    """A fresh app process with Stripe configured, so the 503 gate is passed."""
    d = tempfile.mkdtemp(prefix="wx-billing-test-")
    monkeypatch.setenv("WX_DB", os.path.join(d, "test.db"))
    monkeypatch.setenv("WX_CACHE_DIR", os.path.join(d, "cache"))
    monkeypatch.setenv("WX_SECRET", "test-secret-not-the-dev-default")
    monkeypatch.setenv("WX_USE_RAM_GRIDS", "0")
    monkeypatch.setenv("WX_RATE_LIMIT_DISABLED", "1")
    monkeypatch.setenv("WX_STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("WX_STRIPE_PRICE_MONTHLY", "price_m")
    monkeypatch.setenv("WX_STRIPE_PRICE_YEARLY", "price_y")
    monkeypatch.setenv("WX_PUBLIC_BASE_URL", "https://example.gr")

    import app as app_module
    import billing
    import entitlements as ent
    import promo

    promo.init_db()

    with TestClient(app_module.app) as c:
        c.billing = billing
        c.ent = ent
        yield c


def _pro_token_with_sub(client, sub="sub_test_1"):
    return client.ent.issue_token("pro", "subscription",
                                  subscription_id=sub,
                                  ttl=client.ent.TOKEN_TTL_S)


# ---------------------------------------------------------------- M-1

def test_checkout_rejects_a_non_json_body_with_400(client):
    r = client.post("/api/checkout", content=b"not json at all",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400, r.text
    assert "500" not in r.text


def test_checkout_rejects_a_json_array_body_with_400(client):
    """Valid JSON that is not an object is still a client mistake, not a crash."""
    r = client.post("/api/checkout", json=[1, 2, 3])
    assert r.status_code == 400, r.text


def test_checkout_rejects_a_json_scalar_body_with_400(client):
    r = client.post("/api/checkout", json="monthly")
    assert r.status_code == 400, r.text


def test_auto_renew_rejects_a_non_json_body_with_400(client):
    token = _pro_token_with_sub(client)
    r = client.post("/api/subscription/auto-renew", content=b"not json",
                    headers={"X-WX-Token": token, "Content-Type": "application/json"})
    assert r.status_code == 400, r.text


def test_auto_renew_rejects_a_json_array_body_with_400(client):
    token = _pro_token_with_sub(client)
    r = client.post("/api/subscription/auto-renew", headers={"X-WX-Token": token},
                    json=[True])
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------- L-2

def test_checkout_does_not_leak_the_stripe_exception_text(client, monkeypatch):
    """A raising SDK must produce a neutral 502, not a copy of the exception."""
    secret_detail = "sk_live_LEAKY_internal_detail"

    def boom(*a, **kw):
        raise RuntimeError(secret_detail)

    monkeypatch.setattr(client.billing, "create_checkout", boom)
    r = client.post("/api/checkout", json={"plan": "monthly"})
    assert r.status_code == 502, r.text
    body = r.text
    assert secret_detail not in body
    assert "RuntimeError" not in body
    assert "Traceback" not in body


def test_checkout_dependency_type_is_not_named_in_the_response(client, monkeypatch):
    """The old shape echoed `Stripe: AttributeError: 'NoneType' ...`. None of that
    is client-facing: the caller gets a plain retry message."""
    def boom(*a, **kw):
        raise AttributeError("'NoneType' object has no attribute 'checkout'")

    monkeypatch.setattr(client.billing, "create_checkout", boom)
    r = client.post("/api/checkout", json={"plan": "yearly"})
    assert r.status_code == 502, r.text
    assert "AttributeError" not in r.text
    assert "NoneType" not in r.text
    assert "'checkout'" not in r.text


def test_checkout_error_is_still_logged_server_side(client, monkeypatch, caplog):
    """Diagnostics are kept, just not on the wire."""
    import logging

    def boom(*a, **kw):
        raise RuntimeError("backend-unavailable")

    monkeypatch.setattr(client.billing, "create_checkout", boom)
    with caplog.at_level(logging.WARNING, logger="wx"):
        r = client.post("/api/checkout", json={"plan": "monthly"})
    assert r.status_code == 502
    assert any("checkout failed" in rec.getMessage().lower() or
               "backend-unavailable" in rec.getMessage()
               for rec in caplog.records), [rec.getMessage() for rec in caplog.records]


def test_auto_renew_does_not_leak_the_stripe_exception_text(client, monkeypatch):
    token = _pro_token_with_sub(client)
    secret_detail = "internal-subscription-detail-xyz"

    def boom(*a, **kw):
        raise RuntimeError(secret_detail)

    monkeypatch.setattr(client.billing, "set_auto_renew", boom)
    r = client.post("/api/subscription/auto-renew", headers={"X-WX-Token": token},
                    json={"enabled": False})
    assert r.status_code == 502, r.text
    assert secret_detail not in r.text
    assert "RuntimeError" not in r.text
