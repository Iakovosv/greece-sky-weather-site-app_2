"""Billing: auto-renewal default, the opt-out, and honest degradation.

The `set_auto_renew` and `create_checkout` paths call the Stripe SDK, which needs
a network and an account. Those two tests inject a minimal stub SDK into
sys.modules so the *real* code in billing.py runs (argument building, the
cancel_at choice, state derivation) without a live account. Everything else here
exercises real functions directly on plain dicts, which is how Stripe returns
data from `.to_dict()` anyway.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import billing  # noqa: E402
import entitlements as ent  # noqa: E402


# --------------------------------------------------------------- pure helpers

def test_auto_renew_is_declared_default():
    assert billing.AUTO_RENEW_BY_DEFAULT is True


def test_not_configured_by_default(monkeypatch):
    for k in ("WX_STRIPE_SECRET_KEY", "WX_STRIPE_PRICE_MONTHLY",
              "WX_STRIPE_PRICE_YEARLY", "WX_PUBLIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    assert billing.checkout_available() is False
    assert set(billing.missing_config()) == {
        "WX_STRIPE_SECRET_KEY", "WX_PUBLIC_BASE_URL",
        "WX_STRIPE_PRICE_MONTHLY", "WX_STRIPE_PRICE_YEARLY"}


def test_configured_when_all_keys_present(monkeypatch):
    monkeypatch.setenv("WX_STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("WX_STRIPE_PRICE_MONTHLY", "price_m")
    monkeypatch.setenv("WX_STRIPE_PRICE_YEARLY", "price_y")
    monkeypatch.setenv("WX_PUBLIC_BASE_URL", "https://example.gr/")
    assert billing.missing_config() == []
    assert billing.base_url() == "https://example.gr"   # trailing slash trimmed


# --------------------------------------------------- renewal state derivation

def _sub(**over):
    base = {
        "id": "sub_1", "status": "active", "cancel_at": None,
        "cancel_at_period_end": False, "metadata": {"plan": "yearly"},
        "items": {"data": [{"current_period_end": 2_000_000_000}]},
    }
    base.update(over)
    return base


def test_active_subscription_renews():
    st = billing._state_from_subscription(_sub())
    assert st["auto_renew"] is True
    assert st["cancel_at"] is None


def test_scheduled_cancel_reports_will_not_renew():
    """`cancel_at` set means the subscription stops: this is the opt-out state."""
    st = billing._state_from_subscription(_sub(cancel_at=1_900_000_000))
    assert st["auto_renew"] is False
    assert st["cancel_at"] == 1_900_000_000


def test_legacy_cancel_at_period_end_is_understood():
    """Old subscriptions still carry the deprecated field; it must read the same."""
    st = billing._state_from_subscription(_sub(cancel_at_period_end=True))
    assert st["auto_renew"] is False
    assert st["cancel_at"] == 2_000_000_000    # falls back to the period end


def test_canceled_status_does_not_claim_renewal():
    st = billing._state_from_subscription(_sub(status="canceled"))
    assert st["auto_renew"] is False


def test_object_like_subscription_is_supported():
    """Stripe returns attribute-style objects, not only dicts."""
    class O:
        id, status, cancel_at, cancel_at_period_end = "sub_2", "active", None, False
        metadata = {"plan": "monthly"}
        items = types.SimpleNamespace(data=[{"current_period_end": 1_800_000_000}])
    st = billing._state_from_subscription(O())
    assert st["auto_renew"] is True
    assert st["plan"] == "monthly"


# --------------------------------------------------------- the SDK call paths

@pytest.fixture
def fake_stripe(monkeypatch):
    """A stub stripe module that records what billing.py sent it.

    Justified because the alternative is either no test of the checkout
    arguments (including the deprecated field we must not send) or a live
    network call to Stripe from the test suite.
    """
    calls = {"create": [], "modify": [], "retrieved": None}
    sub = _sub()

    class Session:
        @staticmethod
        def create(**kw):
            calls["create"].append(kw)
            return types.SimpleNamespace(id="cs_1", url="https://checkout.stripe.test/x")

    class Subscription:
        @staticmethod
        def retrieve(sid):
            calls["retrieved"] = sid
            return sub

        @staticmethod
        def modify(sid, **kw):
            calls["modify"].append((sid, kw))
            if kw.get("cancel_at") == "":
                sub["cancel_at"] = None
                sub["cancel_at_period_end"] = False
            else:
                sub["cancel_at"] = kw["cancel_at"]
            return sub

    stub = types.SimpleNamespace(
        checkout=types.SimpleNamespace(Session=Session),
        Subscription=Subscription, api_key=None)
    monkeypatch.setitem(sys.modules, "stripe", stub)
    monkeypatch.setenv("WX_STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("WX_STRIPE_PRICE_MONTHLY", "price_m")
    monkeypatch.setenv("WX_STRIPE_PRICE_YEARLY", "price_y")
    monkeypatch.setenv("WX_PUBLIC_BASE_URL", "https://example.gr")
    return calls, stub


def test_checkout_does_not_send_deprecated_cancel_field(fake_stripe):
    """The core of the ticket: renewal must not be disabled at checkout.

    `cancel_at_period_end` is deprecated and not a Checkout parameter, so it must
    be absent. And nothing else may set a cancellation: renewal stays the default.
    """
    calls, _ = fake_stripe
    billing.create_checkout("monthly")
    kw = calls["create"][0]
    assert kw["mode"] == "subscription"
    assert "cancel_at_period_end" not in kw
    assert "cancel_at_period_end" not in kw.get("subscription_data", {})
    assert "cancel_at" not in kw.get("subscription_data", {})
    assert kw["subscription_data"]["metadata"]["plan"] == "monthly"


def test_checkout_carries_the_token_for_activation(fake_stripe):
    calls, _ = fake_stripe
    billing.create_checkout("yearly", token="tok.abc")
    kw = calls["create"][0]
    assert kw["subscription_data"]["metadata"]["wx_token"] == "tok.abc"
    assert "session_id={CHECKOUT_SESSION_ID}" in kw["success_url"]


def test_unknown_plan_is_rejected(fake_stripe):
    with pytest.raises(ValueError):
        billing.create_checkout("lifetime")   # not a real plan


def test_opt_out_schedules_cancel_at_period_end(fake_stripe):
    """Turning renewal off keeps the paid period: cancel_at is the period end."""
    calls, _ = fake_stripe
    st = billing.set_auto_renew("sub_1", enabled=False)
    sid, kw = calls["modify"][0]
    assert kw["cancel_at"] == 2_000_000_000
    assert st["auto_renew"] is False


def test_re_enable_clears_the_scheduled_cancel(fake_stripe):
    calls, _ = fake_stripe
    billing.set_auto_renew("sub_1", enabled=False)
    st = billing.set_auto_renew("sub_1", enabled=True)
    assert calls["modify"][-1][1] == {"cancel_at": ""}
    assert st["auto_renew"] is True


def test_set_auto_renew_without_config_raises(monkeypatch):
    monkeypatch.delenv("WX_STRIPE_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        billing.set_auto_renew("sub_1", enabled=False)


# ----------------------------------------------------- token carries the sub id

def test_token_round_trips_the_subscription_id():
    tok = ent.issue_token("pro", "subscription", subscription_id="sub_9")
    e = ent.verify_token(tok)
    assert e.is_pro and e.subscription_id == "sub_9" and e.source == "subscription"


def test_passcode_token_has_no_subscription_id():
    """A free/passcode token must not claim a manageable subscription."""
    tok = ent.check_passcode(ent.MASTER_CODE)
    assert ent.verify_token(tok).subscription_id is None


def test_token_without_sub_id_is_unchanged_format():
    tok = ent.issue_token("pro", "trial")
    assert ent.verify_token(tok).subscription_id is None
