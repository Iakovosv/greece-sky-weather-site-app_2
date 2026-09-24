"""Footer contacts and legal pages.

The footer is server-rendered on purpose. Stripe's reviewers, and any link
checker, fetch the HTML without running the JavaScript that fills in the
forecast block, so contacts and legal links have to be in the initial response.
These tests read the raw HTML for exactly that reason.
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


def _index(client) -> str:
    r = client.get("/")
    assert r.status_code == 200
    return r.text


# ------------------------------------------------------------------ footer

def test_site_footer_is_in_the_initial_html(client):
    """No JS needed: this is the whole point of rendering it on the server."""
    html = _index(client)
    assert '<footer id="site">' in html
    assert '&copy; 2026 Greece Sky and Weather' in html


def test_contacts_are_present(client):
    html = _index(client)
    assert "youtube.com" in html and "facebook.com" in html
    assert "mailto:greekskyweather@gmail.com" in html


def test_legal_links_are_present(client):
    html = _index(client)
    for path in ("/terms", "/privacy", "/refunds", "/licenses"):
        assert f'href="{path}"' in html, path


def test_consent_links_sit_next_to_the_checkout_button(client):
    """Stripe expects terms to be reachable from the point of sale, not just the footer."""
    html = _index(client)
    assert 'class="consent"' in html
    block = html.split('class="consent"')[1].split("</p>")[0]
    for path in ("/terms", "/privacy", "/refunds"):
        assert f'href="{path}"' in block, path


def test_social_links_open_in_a_new_tab(client):
    html = _index(client)
    social = re.findall(r'<a href="https://www\.(?:youtube|facebook)[^"]*"[^>]*>', html)
    assert len(social) == 2, social
    for tag in social:
        assert 'target="_blank"' in tag, tag
        # Without noopener the opened page gets a handle on window.opener.
        assert 'rel="noopener' in tag, tag


def test_footer_survives_a_failed_forecast(client):
    """The forecast block is filled by JS and can fail; contacts must not.

    Asserted structurally: #site and #attr are siblings, so nothing that renders
    into #attr can remove or hide the contacts.
    """
    html = _index(client)
    assert html.index('id="attr"') < html.index('id="site"')
    assert "</main>" in html.split('<footer id="site">')[0]


def test_no_unsubstituted_placeholders(client):
    html = _index(client)
    for token in ("__YOUTUBE__", "__FACEBOOK__", "__EMAIL__"):
        assert token not in html, token


# ------------------------------------------------------------------ legal pages

@pytest.mark.parametrize("path,needle", [
    ("/terms", "Όροι Χρήσης"),
    ("/privacy", "Πολιτική Απορρήτου"),
    ("/refunds", "Πολιτική Επιστροφών"),
])
def test_legal_pages_render(client, path, needle):
    r = client.get(path)
    assert r.status_code == 200
    assert needle in r.text
    assert "greekskyweather@gmail.com" in r.text


def test_legal_pages_are_public(client):
    """A buyer has to be able to read the terms before paying, so no auth."""
    for path in ("/terms", "/privacy", "/refunds", "/licenses"):
        r = client.get(path)
        assert r.status_code == 200, path


@pytest.mark.parametrize("path", ["/terms", "/refunds"])
def test_pages_that_touch_expectations_disclaim_certainty(client, path):
    """Where a customer forms an expectation, the pages say a forecast is an estimate.

    Not asserted on /privacy: that page is about data handling, and padding it with
    a weather disclaimer would be noise.
    """
    r = client.get(path)
    assert "σφάλμα" in r.text or "αβεβαιότητα" in r.text


def test_terms_say_a_lawyer_has_not_reviewed_them(client):
    """Better a visible caveat than a template pretending to be vetted text."""
    assert "δεν έχει ελεγχθεί από νομικό" in client.get("/terms").text


# ------------------------------------------------------------------ config

def test_contacts_come_from_the_environment(monkeypatch, client):
    monkeypatch.setenv("WX_YOUTUBE_URL", "https://youtube.com/@SomeOtherHandle")
    monkeypatch.setenv("WX_EMAIL", "hello@example.gr")
    html = _index(client)
    assert "https://youtube.com/@SomeOtherHandle" in html
    assert "mailto:hello@example.gr" in html


def test_a_quoted_url_cannot_break_out_of_the_attribute(monkeypatch, client):
    """Contacts are operator config, but they are still untrusted as HTML.

    The quote has to arrive as the entity &quot; so the href attribute still ends
    where the template says it does. If it were inserted raw, the text after it
    would become new attributes - an onmouseover that fires on hover.
    """
    monkeypatch.setenv("WX_YOUTUBE_URL", 'https://x.test/?a="onmouseover=alert(1)')
    html = _index(client)
    assert 'a=&quot;onmouseover=alert(1)' in html
    # The raw quote must not appear right after the injected text, which is the
    # signature of an attribute break-out.
    assert 'a="onmouseover=alert(1)"' not in html


def test_empty_contact_env_falls_back_to_the_default(monkeypatch):
    import legal
    monkeypatch.setenv("WX_YOUTUBE_URL", "")
    assert legal.contacts()["youtube"] == legal._DEFAULTS["youtube"]
