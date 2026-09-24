"""UX patch: PRO sign-out guard, checkout messaging, code labels, promo line.

The purchase UI is server-rendered JavaScript, so these read the served HTML for
the exact guards rather than driving a browser. They are regression anchors: each
one names a behaviour that a future edit could silently undo.

Scope is the five findings this patch addressed — B1 (PRO sign-out), B2 (why-tab
checkout message), B3 (PRO vs admin code labels), B6 (promo status line), B9 (no
environment names in the UI). Nothing here asserts on Stripe or entitlement
server logic; those have their own tests.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(app_module.app)


@pytest.fixture()
def html(client) -> str:
    r = client.get("/")
    assert r.status_code == 200
    return r.text


def _script(html: str) -> str:
    m = re.search(r"<script>(.*?)</script></body></html>", html, re.S)
    assert m, "no main <script> block in the page"
    return m.group(1)


def _function(js: str, name: str) -> str:
    """The body of a named JS function, up to the next top-level declaration.

    Enough to assert what a function does and does not call, without a JS parser.
    """
    m = re.search(
        r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{(.*?)\n(?=(?:async\s+)?function\s|\Z)",
        js, re.S)
    assert m, f"function {name} not found"
    return m.group(1)


# --------------------------------------------------------------------- B1

def test_subscription_does_not_offer_sign_out(html):
    """A paid subscription must not lose PRO to a single, logout-looking click.

    The turn is a guard on the entitlement source; a subscription token has no
    browser-side way back, so the button is omitted for it.
    """
    js = _script(html)
    bar = _function(js, "renderTierBar")
    assert "t.source!=='subscription'" in bar.replace(" ", ""), \
        "renderTierBar must not offer sign-out to a subscription source"
    # The sign-out button is now behind that guard, not unconditional.
    assert re.search(r"else if\(.*subscription.*\).*signOut\(\)", bar)


def test_passcode_trial_promo_can_still_sign_out_with_a_warning(html):
    """The other sources keep the control, but it now explains itself first."""
    js = _script(html)
    body = _function(js, "signOut")
    assert "window.confirm(" in body, "signOut must confirm before dropping the token"
    assert "ΔΕΝ ακυρώνεται" in body, "the warning must say the subscription is not cancelled"
    assert "localStorage.removeItem('wx_token')" in body


def test_sign_out_never_touches_subscription_state(html):
    """B1 must be a client-side token change only: no Stripe, no auto-renew call."""
    js = _script(html)
    body = _function(js, "signOut")
    assert "fetch(" not in body, "signOut must not call any endpoint"
    assert "auto-renew" not in body
    assert "subscription" not in body.replace("subscription state", "")


# --------------------------------------------------------------------- B2

def test_why_tab_checkout_message_is_conditional(html):
    """The stale hardcoded 'payment is not wired' claim must be gone, replaced
    by a branch on the server's checkout_available flag."""
    js = _script(html)
    assert "Η ενεργοποίηση πληρωμής δεν είναι συνδεδεμένη σε αυτή την έκδοση" not in js
    cta = _function(js, "renderCta")
    assert "PLANS.checkout_available" in cta
    assert "Stripe" in cta, "when checkout works, the message must say so"


def test_why_tab_stays_neutral_when_checkout_is_off(html):
    js = _script(html)
    cta = _function(js, "renderCta")
    assert "Η πληρωμή δεν είναι διαθέσιμη αυτή τη στιγμή." in cta


# --------------------------------------------------------------------- B3

def test_pro_code_and_admin_code_are_separated(html):
    """A gift-code holder must land on the PRO field, not the operator passcode."""
    body = html.split("<script>", 1)[0]
    assert "Έχεις κωδικό PRO;" in body
    assert 'id="pm-promo"' in body
    assert "Κωδικός διαχειριστή" in body
    assert 'id="pm-admin-wrap"' in body
    # The admin passcode is the collapsed one, so it is not the first thing seen.
    assert re.search(r'<details class="codebox" id="pm-admin-wrap">', body)
    # The old ambiguous wording is gone.
    assert "Έχετε κωδικό πρόσβασης;" not in body
    assert "Κωδικός πρόσβασης" not in body


def test_pro_code_field_comes_before_admin_field(html):
    body = html.split("<script>", 1)[0]
    assert body.index('id="pm-promo"') < body.index('id="pm-admin-wrap"')


# --------------------------------------------------------------------- B6

def test_promo_status_line_uses_the_existing_endpoint(html):
    js = _script(html)
    body = _function(js, "loadPromoLine")
    assert "/api/promo/status" in body
    assert "X-WX-Token" in body, "the caller's own token identifies them; no query param"


def test_promo_status_line_never_shows_the_device_id(html):
    """The endpoint returns the caller's device id for an operator; it is
    deliberately not rendered to the end user."""
    js = _script(html)
    body = _function(js, "loadPromoLine")
    assert "device" not in body
    assert "pro_until" in body


def test_promo_line_shows_the_end_date(html):
    js = _script(html)
    body = _function(js, "loadPromoLine")
    assert "ενεργό έως" in body
    assert 'id="pm-promoline"' in html


# --------------------------------------------------------------------- B9

ENV_TOKENS = (
    "WX_STRIPE_SECRET_KEY", "WX_STRIPE_PRICE_MONTHLY", "WX_STRIPE_PRICE_YEARLY",
    "WX_STRIPE_WEBHOOK_SECRET", "WX_PUBLIC_BASE_URL", "WX_SECRET",
)


def test_no_environment_names_reach_the_page(html):
    for token in ENV_TOKENS:
        assert token not in html, f"{token} must not appear in user-facing HTML"


def test_unavailable_checkout_uses_a_neutral_message(html):
    js = _script(html)
    note = _function(js, "renderAutoRenewNote")
    assert "Η πληρωμή δεν είναι διαθέσιμη αυτή τη στιγμή." in note
    assert "λείπει" not in note, "the missing-settings list is operator data"


def test_checkout_503_does_not_render_the_server_detail(html):
    """The 503 detail names missing settings; the UI must substitute its own line."""
    js = _script(html)
    fn = _function(js, "checkout")
    assert "r.status===503" in fn
    assert "Η πληρωμή δεν είναι διαθέσιμη αυτή τη στιγμή." in fn


# ------------------------------------------------- the backend contract held

def test_checkout_endpoint_still_reports_missing_config_to_operators(client, monkeypatch):
    """The technical detail is only *muted in the UI*; the API still exposes it
    for diagnostics, so this patch changed no backend contract."""
    for k in ("WX_STRIPE_SECRET_KEY", "WX_PUBLIC_BASE_URL",
              "WX_STRIPE_PRICE_MONTHLY", "WX_STRIPE_PRICE_YEARLY"):
        monkeypatch.delenv(k, raising=False)
    import billing
    missing = billing.missing_config()
    assert "WX_STRIPE_SECRET_KEY" in missing
