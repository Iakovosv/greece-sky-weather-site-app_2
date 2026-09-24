"""The dark glass theme.

A stylesheet cannot be tested by asserting its own text - that only proves the
rule was written. These guard the two properties that actually matter and that a
later edit can silently undo:

  * the palette is dark, and nothing paints a light background on top of it;
  * every surface that carries text is translucent *and* blurred, because a
    translucent fill without a blur puts text on a background the element is not
    sampling, which is a legibility bug rather than a graceful degradation.

The rendered check lives in the browser probes; these run without a browser.
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


def _style(client) -> str:
    r = client.get("/")
    assert r.status_code == 200
    m = re.search(r"<style>(.*?)</style>", r.text, re.S)
    assert m, "no <style> block in the page"
    return m.group(1)


# ------------------------------------------------------------------ palette

def test_palette_is_dark(client):
    css = _style(client)
    root = re.search(r":root\{(.*?)\}", css, re.S).group(1)
    assert "color-scheme:dark" in root
    assert "--bg:#070b14" in root
    # The card must stay translucent or the blur has nothing to show through.
    card = re.search(r"--card:(rgba\([^)]*\))", root).group(1)
    alpha = float(re.search(r"([\d.]+)\)", card).group(1))
    assert 0.3 < alpha < 0.95, card
    assert "--ink:#eef2f8" in root


def test_the_card_token_is_translucent_not_white(client):
    css = _style(client)
    assert "--card:#fff" not in css
    assert "--card:#ffffff" not in css


# ------------------------------------------------------- no light leftovers

def test_no_light_background_survives_in_the_page_css(client):
    """A single missed background is what makes a dark theme look unfinished."""
    css = _style(client)
    light = []
    for m in re.finditer(r"background:\s*(#[0-9a-fA-F]{6}|rgba?\([^)]*\))", css):
        v = m.group(1)
        if v.startswith("#"):
            r, g, b = (int(v[i:i + 2], 16) for i in (1, 3, 5))
            a = 1.0
        else:
            nums = re.findall(r"[\d.]+", v)
            r, g, b = (float(x) for x in nums[:3])
            a = float(nums[3]) if len(nums) > 3 else 1.0
        if a > 0.4 and (r + g + b) / 3 > 170:
            light.append(v)
    assert not light, f"light backgrounds still painted: {light}"


def test_no_light_text_colour_survives(client):
    """Dark text on a dark card is invisible; these are the old palette values."""
    css = _style(client)
    for stale in ("color:#161a20", "color:#2b323c", "color:#67707d"):
        assert stale not in css, stale


# ------------------------------------------------------------------- glass

def test_every_text_surface_is_translucent_and_blurred(client):
    css = _style(client)
    m = re.search(r"\.glass,([^{]+)\{([^}]*)\}", css)
    assert m, "the glass surface rule is missing"
    body = m.group(2)
    assert "backdrop-filter:blur(var(--blur))" in body
    assert "-webkit-backdrop-filter" in body, "Safari needs the prefixed property"
    group = m.group(1)
    for sel in (".card", ".fcard", ".cta", ".cam", ".chartbox", ".veri",
                ".verdict", ".plan", ".opt", "table", "details.geo"):
        assert sel in group, f"{sel} is not a glass surface"


def test_the_requested_glass_values_are_present(client):
    """The glass recipe the brief specifies, spelled out.

    These are pinned rather than derived so that an edit to any one of the four
    values is a deliberate change with a failing test, not a silent drift away
    from the look that was asked for.
    """
    css = _style(client)
    root = re.search(r":root\{(.*?)\}", css, re.S).group(1)
    assert "rgba(18,24,38,.75)" in root             # translucent dark fill
    assert "--blur:16px" in root                    # backdrop blur
    assert "--line:rgba(255,255,255,.08)" in root   # 1px light border
    assert "0 10px 30px 0 rgba(0,0,0,.4)" in root   # soft shadow


def test_blur_has_something_to_blur(client):
    """Without a wash behind them, translucent cards read as flat grey."""
    css = _style(client)
    # Match the rule, not the comment above it that also names body::before.
    m = re.search(r"body::before\{([^}]*)\}", css)
    assert m, "the page wash rule is missing"
    wash = m.group(1)
    assert "radial-gradient" in wash
    assert "linear-gradient" in wash


# ------------------------------------------------- unsupported backdrop-filter

def test_opaque_fallback_when_blur_is_unsupported(client):
    """No blur means the translucency buys nothing, so the fill must carry text."""
    css = _style(client)
    assert "@supports not" in css
    block = css.split("@supports not", 1)[1]
    assert "backdrop-filter" in block
    assert "--card:rgba(17,23,36,.9" in block


def test_reduced_motion_is_respected(client):
    css = _style(client)
    assert "prefers-reduced-motion" in css


def test_layout_critical_css_is_not_weakened(client):
    """The glass work must not drop the responsive and focus rules."""
    css = _style(client)
    assert "max-width:640px" in css
    assert ":focus-visible" in css


# ----------------------------------------------------------- legal / licenses

@pytest.mark.parametrize("path", ["/terms", "/privacy", "/refunds"])
def test_legal_pages_use_the_same_dark_palette(client, path):
    """A legal link must not drop the customer onto a white page."""
    html = client.get(path).text
    assert "background:#070b14" in html
    assert "background:#f6f8fb" not in html
    assert "color-scheme:dark" in html


def test_licenses_page_uses_the_same_dark_palette(client):
    html = client.get("/licenses").text
    assert "background:#070b14" in html
    assert "background:#fff" not in html


# ------------------------------------------------------- dual-layer UX structure

def test_hero_is_glass_and_has_no_white_margins(client):
    """The hero is the first screen, so it follows the same glass recipe as the
    rest rather than being a flat coloured block."""
    css = _style(client)
    m = re.search(r"\.nowhero\{([^}]*)\}", css)
    assert m, "the .nowhero rule is missing"
    rule = m.group(1)
    assert "backdrop-filter" in rule
    assert "var(--card)" in rule
    assert "border-radius" in rule


def test_hero_wash_gives_the_glass_something_to_sample(client):
    css = _style(client)
    m = re.search(r"\.nowhero:before\{([^}]*)\}", css)
    assert m, "the hero needs its own wash or it renders flat grey"
    assert "radial-gradient" in m.group(1)


def test_severe_hero_is_visually_distinct(client):
    """A severe-weather state must be findable in the stylesheet, not only in JS."""
    css = _style(client)
    assert ".nowhero.severe" in css


def test_chips_cover_every_tone_the_server_can_emit(client):
    """temp_tone()/wind_tone() return these names; a missing class means an
    unstyled chip on the page."""
    import app
    css = _style(client)
    tones = set()
    for t in (-5, 5, 18, 30, 40):
        tones.add(app.temp_tone(t))
    for b in (0, 3, 5, 9):
        tones.add(app.wind_tone(b))
    tones.discard("unknown")
    for tone in tones:
        assert f".chip.{tone}" in css, f"no styling for chip tone {tone}"


def test_expert_data_lives_in_collapsible_sections(client):
    """The dual-layer contract: the pro data is behind a disclosure so a simple
    user never scrolls through it, but the stylesheet must still style it."""
    css = _style(client)
    assert ".xsec" in css
    assert ".xsec > summary" in css
    assert ".xgrid" in css
    assert ".dense" in css


def test_pro_tables_are_dense_not_spacious(client):
    """The brief asked for compact advanced tables; a large row height would be
    a regression against that."""
    css = _style(client)
    m = re.search(r"\.dense th,\.dense td\{([^}]*)\}", css)
    assert m, "the dense table padding rule is missing"
    m2 = re.search(r"padding:(\d+)px", m.group(1))
    assert m2 and int(m2.group(1)) <= 6, m.group(1)


def test_skewt_image_is_flush_in_its_section(client):
    """No white margin: the Skew-T must fill its card and sit on the card colour."""
    css = _style(client)
    m = re.search(r"\.xsec img\.skewt\{([^}]*)\}", css)
    assert m, "the Skew-T section image rule is missing"
    rule = m.group(1)
    assert "width:100%" in rule
    assert "display:block" in rule


# --------------------------------------------------------- astro card surface

def test_astro_card_is_styled(client):
    css = _style(client)
    assert ".astro" in css
    assert ".astro .aevents" in css
    assert ".astro .phasebar" in css
    assert ".astro .track" in css


def test_astro_card_mount_point_exists_in_the_page(client):
    """The card is loaded after the forecast, so the container must be in the
    simple view markup or the fetch has nowhere to land."""
    html = client.get("/").text
    assert 'id="skynow"' in html
    assert "loadSkyNow" in html
    assert "renderSkyNow" in html


def test_astro_endpoint_is_free_and_returns_data(client):
    """It is deliberately not PRO-gated: it is a pure calculation with no quota."""
    r = client.get("/api/sky?lat=37.9838&lon=23.7275&elev=150")
    assert r.status_code == 200
    j = r.json()
    assert j["available"] is True
    assert j["sun"]["rise"]["label"]
    assert 0 <= j["moon"]["illumination_pct"] <= 100


def test_astro_endpoint_is_location_specific(client):
    """The whole point of the card is that it differs by place."""
    a = client.get("/api/sky?lat=37.9838&lon=23.7275").json()
    b = client.get("/api/sky?lat=40.6401&lon=22.9444").json()
    assert a["sun"]["rise"]["label"] != b["sun"]["rise"]["label"]
