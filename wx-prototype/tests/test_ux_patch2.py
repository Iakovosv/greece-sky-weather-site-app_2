"""UX patch 2: FREE discovery, wording consistency, modal title, sign-out, errors.

Same approach as `test_ux_patch.py`: the purchase UI is server-rendered
JavaScript, so these read the served HTML for the exact strings and branches a
future edit could silently undo. They are regression anchors for findings B5,
B7, B8, B10, B11, B12 and B13.

Nothing here asserts on Stripe, entitlement or promo server logic; those have
their own tests, and this patch changed no behaviour - only wording and the
surface an error is shown on.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import entitlements as ent  # noqa: E402


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
    m = re.search(
        r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{(.*?)\n(?=(?:async\s+)?function\s|\Z)",
        js, re.S)
    assert m, f"function {name} not found"
    return m.group(1)


# --------------------------------------------------------------------- B5

def test_free_cta_offers_a_quiet_way_to_a_code(html):
    """A FREE visitor must be able to learn that a promo/gift code exists,
    without a second billing flow: the hint only opens the existing modal."""
    js = _script(html)
    cta = _function(js, "renderCta")
    assert "δωροκάρτα" in cta, "the FREE CTA must mention a code/gift option"
    assert "openModal()" in cta, "discovery must reuse the existing modal, not a new flow"


def test_free_discovery_does_not_add_a_new_endpoint(html):
    """The hint is wording only; it must not introduce a checkout/auth call."""
    js = _script(html)
    cta = _function(js, "renderCta")
    assert "/api/promo/redeem" not in cta
    assert "/api/checkout" not in cta


def test_the_pro_code_field_still_exists(html):
    """Discovery points at the field that already existed (B3), it does not replace it."""
    body = html.split("<script>", 1)[0]
    assert "Έχεις κωδικό PRO;" in body
    assert 'id="pm-promo"' in body


# --------------------------------------------------------------------- B7

def test_upgrade_flow_uses_one_grammatical_person(html):
    """The purchase flow addresses the visitor in the singular throughout; the
    leftover plural forms in the same flow are gone."""
    body = html.split("<script>", 1)[0]
    for plural in ("επιλέξετε", "σημείο σας", "το βλέπετε", "δικό σας"):
        assert plural not in html, f"plural form {plural!r} left in the upgrade flow"
    assert "σημείο σου" in body


def test_elevation_panel_is_singular(html):
    js = _script(html)
    fn = _function(js, "renderSimple")
    assert "βλέπεις" in fn and "υψόμετρο" in fn
    assert "βλέπετε" not in fn
    assert "δικό σου" in fn


# --------------------------------------------------------------------- B8

def test_activation_is_not_conflated_with_access_source(html):
    """"Πηγή ενεργοποίησης" muddled two meanings. The tier bar now says where
    access came from, leaving "ενεργοποίηση" to the act of unlocking."""
    js = _script(html)
    bar = _function(js, "renderTierBar")
    assert "Πηγή πρόσβασης" in bar
    assert "Πηγή ενεργοποίησης" not in bar


def test_activation_keeps_its_unlock_meaning_elsewhere(html):
    """Removing the ambiguous use must not remove the word from the flows where
    it correctly means "unlock now"."""
    js = _script(html)
    assert "Ενεργοποίηση…" in js                      # admin red­eem button
    assert "Η ενεργοποίηση με κωδικό λειτουργεί" in js  # code note


# --------------------------------------------------------------------- B10

def test_modal_subtitle_states_what_free_and_pro_include(html):
    """10 days is the PRO window; the FREE window is 3 of them. The subtitle may
    not read as though FREE simply includes 10 days."""
    js = _script(html)
    sub = _function(js, "fillPlans")
    assert "πλήρη" not in sub, "the old 'full window' framing must be replaced"
    assert "δωρεάν οι πρώτες" in sub, "the FREE share of the window must be explicit"


def test_plan_unlock_copy_names_the_free_share():
    p = ent.plan_payload()
    first = p["unlocks"][0].lower()
    assert "10 ημερών" in first
    assert "δωρεάν" in first, "the first unlock must state the FREE share"


def test_carousel_header_is_tier_aware(html):
    js = _script(html)
    fn = _function(js, "dailyCarousel")
    assert "δωρεάν οι πρώτες" in fn, "a FREE visitor must see the real free share"
    assert "isPro" in fn and "Πρόγνωση 10 ημερών" in fn


# --------------------------------------------------------------------- B11

def test_modal_title_depends_on_tier(html):
    """An existing PRO must not be shown 'Αναβάθμιση σε PRO'."""
    js = _script(html)
    fn = _function(js, "fillPlans")
    assert "pm-title" in fn
    assert "'Διαχείριση PRO'" in fn and "'Αναβάθμιση σε PRO'" in fn


def test_static_title_is_only_the_pre_fetch_default(html):
    """The rendered title is driven by fillPlans; the markup value is a placeholder."""
    body = html.split("<script>", 1)[0]
    m = re.search(r'<h3 id="pm-title">([^<]*)</h3>', body)
    assert m and m.group(1) == "Αναβάθμιση σε PRO"


# --------------------------------------------------------------------- B12

def test_sign_out_wording_is_not_a_logout(html):
    """"Έξοδος από PRO" read like a subscription cancellation. The control and
    its confirmation now say what they actually do - and nothing more."""
    js = _script(html)
    bar = _function(js, "renderTierBar")
    assert "Έξοδος από PRO" not in bar
    assert "Αφαίρεση PRO από τη συσκευή" in bar
    body = _function(js, "signOut")
    assert "ΔΕΝ ακυρώνεται" in body
    assert "δεν σταματά η" in body and "χρέωση" in body


def test_sign_out_still_does_not_touch_the_subscription(html):
    """B12 is wording only: no new endpoint, no cancellation call."""
    js = _script(html)
    body = _function(js, "signOut")
    assert "fetch(" not in body
    assert "localStorage.removeItem('wx_token')" in body


# --------------------------------------------------------------------- B13

def test_trial_error_is_shown_in_the_page(html):
    js = _script(html)
    fn = _function(js, "startTrial")
    assert "alert(" not in fn, "a trial failure must not be a browser alert"
    assert "uiMsg(" in fn


def test_claim_error_is_shown_in_the_modal(html):
    js = _script(html)
    fn = _function(js, "claimCheckout")
    assert "alert(" not in fn
    assert "uiMsg(" in fn
    assert "openModal()" in fn, "the error must land on the surface the user returns to"


def test_the_inline_message_helper_targets_existing_lines(html):
    js = _script(html)
    fn = _function(js, "uiMsg")
    assert "'pm-msg'" in fn and "'cta-msg'" in fn
    assert "textContent" in fn


def test_cta_message_placeholder_exists_for_the_trial_flow(html):
    """startTrial can be triggered from the why-tab, where the modal is not open;
    the CTA carries its own line so the message has somewhere to land."""
    js = _script(html)
    assert 'id="cta-msg"' in js


def test_no_alert_left_in_the_pro_or_promo_flow(html):
    """The only alerts left are the geo/search ones, which are not part of the
    trial/claim/promo surfaces this finding covers."""
    js = _script(html)
    alerts = re.findall(r"alert\(([^)]*)", js)
    assert alerts, "expected the geo/search alerts to still exist"
    for a in alerts:
        assert "γεωγραφ" not in a
        assert "θέση" in a or "τοποθεσία" in a or "geolocation" in a, \
            f"unexpected alert outside the geo/search flow: {a!r}"
