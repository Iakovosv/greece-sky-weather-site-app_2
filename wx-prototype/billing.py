"""Subscription lifecycle: Stripe Checkout, auto-renewal, cancellation.

Why this file exists rather than a `stripe.checkout.Session.create` call inline
-----------------------------------------------------------------------------
Two facts drive the design, and both are easy to get wrong:

1. **Auto-renewal is the default for a Stripe subscription.** A subscription
   recurs until it is cancelled; there is no "enable auto-renewal" switch to
   flip. So the requirement "auto-renewal on by default, with a discreet
   opt-out" is really two requirements: do not accidentally disable it at
   checkout, and provide a working, discoverable way to turn it off later.

2. **The obvious field is deprecated.** `cancel_at_period_end` (on both
   Subscription and Update) was deprecated in the Basil API (2025-05-28) in
   favour of `cancel_at`. It is also *not* a documented parameter of Checkout's
   `subscription_data`, so setting it at checkout to "force" auto-renewal is
   both deprecated and unsupported. Passing an unknown parameter would fail the
   request outright, which is worse than doing nothing: since renewal is already
   the default, the correct action at checkout is to not disable it.

The cancellation therefore happens on the Subscription, via `cancel_at`, after
checkout — which is also where the user's opt-out belongs.

Degradation
-----------
`stripe` and the environment keys are optional. Without them the site runs
exactly as before: passcode and trial still work, and `checkout_available()` is
False so the UI says so instead of offering a button that cannot work. Importing
this module must never be fatal.

Environment
-----------
WX_STRIPE_SECRET_KEY   Stripe secret key (sk_test_... / sk_live_...). Unset = off.
WX_STRIPE_PRICE_MONTHLY  price id for the monthly plan
WX_STRIPE_PRICE_YEARLY   price id for the yearly plan
WX_STRIPE_WEBHOOK_SECRET signing secret for the webhook (optional but required
                        to verify webhook signatures)
WX_PUBLIC_BASE_URL     absolute base URL of this site, for the redirect URLs
"""
from __future__ import annotations

import os

# Auto-renewal is Stripe's default for subscriptions. Nothing here turns it off;
# `cancel_at` on the subscription is what later opts a user out, preserving the
# period they already paid for.
AUTO_RENEW_BY_DEFAULT = True

_PLAN_ENV = {"monthly": "WX_STRIPE_PRICE_MONTHLY", "yearly": "WX_STRIPE_PRICE_YEARLY"}


def secret_key() -> str | None:
    return (os.environ.get("WX_STRIPE_SECRET_KEY") or "").strip() or None


def webhook_secret() -> str | None:
    return (os.environ.get("WX_STRIPE_WEBHOOK_SECRET") or "").strip() or None


def base_url() -> str:
    return (os.environ.get("WX_PUBLIC_BASE_URL") or "").strip().rstrip("/")


def price_id(plan: str) -> str | None:
    env = _PLAN_ENV.get(plan)
    if not env:
        return None
    return (os.environ.get(env) or "").strip() or None


def _stripe():
    """Return the stripe module, or None when it is not installed.

    Imported lazily so a missing dependency or a bad key cannot stop the app
    from importing — the forecast has no dependency on payments.
    """
    try:
        import stripe
    except Exception:
        return None
    key = secret_key()
    if not key:
        return None
    stripe.api_key = key
    return stripe


def missing_config() -> list[str]:
    """Names of the settings that are absent, for an honest UI and a health check.

    Returned rather than logged-and-hidden: a payment button that does nothing is
    the single most damaging failure for a paid product, so the UI is told which
    pieces are missing and refuses to imply it can take money.
    """
    need = []
    if not secret_key():
        need.append("WX_STRIPE_SECRET_KEY")
    if not base_url():
        need.append("WX_PUBLIC_BASE_URL")
    if not price_id("monthly"):
        need.append("WX_STRIPE_PRICE_MONTHLY")
    if not price_id("yearly"):
        need.append("WX_STRIPE_PRICE_YEARLY")
    return need


def checkout_available() -> bool:
    return _stripe() is not None and not missing_config()


def create_checkout(plan: str, customer_email: str | None = None,
                    token: str | None = None) -> dict:
    """Create a Checkout Session for a recurring plan. Returns its client secret/url.

    `subscription_data` carries only fields that Checkout actually documents
    (`metadata`). The client's entitlement token is stashed there so the webhook
    can hand the same visitor their PRO token after payment, without an accounts
    table.
    """
    if plan not in _PLAN_ENV:
        raise ValueError(f"unknown plan: {plan!r}")
    if not checkout_available():
        raise RuntimeError("stripe is not configured: " + ", ".join(missing_config()))
    stripe = _stripe()
    price = price_id(plan)
    base = base_url()
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price, "quantity": 1}],
        customer_email=customer_email or None,
        # Auto-renewal is already the default here; we deliberately do not send
        # cancel_at_period_end (deprecated, and not a Checkout parameter).
        subscription_data={"metadata": {"plan": plan, **(  {"wx_token": token} if token else {})}},
        success_url=f"{base}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base}/?checkout=cancel",
        allow_promotion_codes=False,
    )
    return {"id": session.id, "url": session.url, "plan": plan}


def subscription_state(subscription_id: str) -> dict:
    """Renewal state for one subscription, in the vocabulary the UI shows.

    `renews_at` is derived from `cancel_at` when present (the new field) and
    from `cancel_at_period_end` otherwise (legacy data), so both spellings of
    "will not renew" are understood.
    """
    stripe = _stripe()
    if stripe is None:
        raise RuntimeError("stripe is not configured")
    sub = stripe.Subscription.retrieve(subscription_id)
    return _state_from_subscription(sub)


def _get(obj, key, default=None):
    """Stripe objects behave like dicts but are not dicts; support both."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def cancel_at_timestamp(sub) -> int | None:
    """The moment this subscription stops, or None if it will keep renewing.

    Prefers `cancel_at` (current) and falls back to `cancel_at_period_end` +
    current period end (deprecated but still returned for older subscriptions).
    """
    cancel_at = _get(sub, "cancel_at")
    if cancel_at:
        return int(cancel_at)
    if _get(sub, "cancel_at_period_end"):
        items = _get(_get(sub, "items", {}), "data", []) or []
        ends = [_get(i, "current_period_end") for i in items]
        ends = [e for e in ends if e]
        if ends:
            return int(min(ends))
        # no item period available: cancellation is scheduled but undated
        return 0
    return None


def _state_from_subscription(sub) -> dict:
    end = cancel_at_timestamp(sub)
    will_renew = end is None and _get(sub, "status") in ("active", "trialing", "past_due")
    return {
        "subscription_id": _get(sub, "id"),
        "status": _get(sub, "status"),
        "auto_renew": will_renew,
        "cancel_at": end,
        "plan": (_get(_get(sub, "metadata", {}), "plan")),
        "current_period_end": _current_period_end(sub),
    }


def _current_period_end(sub) -> int | None:
    items = _get(_get(sub, "items", {}), "data", []) or []
    ends = [e for e in (_get(i, "current_period_end") for i in items) if e]
    return int(min(ends)) if ends else None


def set_auto_renew(subscription_id: str, enabled: bool) -> dict:
    """Turn recurring billing on or off. This is the opt-out at the heart of the ticket.

    enabled=False  -> cancel at the end of the paid period (access is kept until
                      then, which is what the refund policy promises).
    enabled=True   -> clear the schedule, so the subscription renews again.
    """
    stripe = _stripe()
    if stripe is None:
        raise RuntimeError("stripe is not configured")
    sub = stripe.Subscription.retrieve(subscription_id)
    if enabled:
        # Cancel any scheduled end, keep the subscription running.
        stripe.Subscription.modify(subscription_id, cancel_at="")
    else:
        end = _current_period_end(sub)
        if not end:
            raise RuntimeError("subscription has no billing period to end")
        stripe.Subscription.modify(subscription_id, cancel_at=end)
    return subscription_state(subscription_id)
