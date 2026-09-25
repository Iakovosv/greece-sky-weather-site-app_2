"""Camera UI state tests: the card must reflect the runtime, never guess.

The camera card is built client-side from ``/api/cameras``, so most of what can go
wrong is *how a state is rendered*, not which state the server reports. These
tests pin the rendering contract for the four states an operator can produce with
no real hardware -- configured, not configured, upstream error, disabled -- plus
the LIVE open/close cycle.

They read the served page. The card markup lives in one string built at request
time, so a structural assertion on the template is the honest way to test it
without a browser; the payload assertions live in ``test_camera_ui_flow.py`` and
``test_snapshot_runtime.py``.
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
def html() -> str:
    r = TestClient(app_module.app).get("/")
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


# --------------------------------------------- B (not configured) is neutral

def test_not_configured_card_does_not_name_an_environment_variable(html):
    """The empty-state copy is visitor-facing. Naming WX_CAMERAS is operator
    detail: it tells a visitor the deployment is unfinished and leaks a server
    configuration key into user-facing HTML."""
    assert "WX_CAMERAS" not in html
    assert "WX_CAMERA" not in html


def test_not_configured_card_does_not_explain_why_the_feed_is_missing(html):
    """A missing feed must not report *why* it is missing."""
    fn = _function(_script(html), "renderCameras")
    assert "WX_CAMERAS" not in fn
    assert "notebox" in fn
    # A short, non-technical line rather than a configuration instruction.
    assert "δεν είναι ακόμη διαθέσιμη" in fn


# ------------------------------------------- A/D loading and LIVE badge gate

def test_configured_card_starts_with_a_loading_line_and_lazy_image(html):
    fn = _function(_script(html), "renderCameras")
    assert 'class="camload"' in fn          # the loading line is part of the card
    assert 'loading="lazy"' in fn
    assert "onload=" in fn and "onerror=" in fn


def test_live_badge_is_hidden_until_a_frame_actually_arrives(html):
    """The bug this pins: the badge said LIVE even when the image failed. It is
    now rendered hidden and revealed only from the image's own onload."""
    fn = _function(_script(html), "renderCameras")
    assert 'id="cambadge-'+"" in fn or "id=\"cambadge-" in fn
    badge = re.search(r"badge='<div class=\"live\"[^>]*hidden", fn)
    assert badge, "the LIVE badge must start hidden"
    loaded = _function(_script(html), "snapshotLoaded")
    assert "badge.hidden=false" in loaded


def test_hidden_live_badge_is_hidden_by_css_not_just_the_attribute(html):
    # Author display:flex would beat the user-agent [hidden] rule without this.
    assert ".cam .live[hidden]{display:none}" in html


# --------------------------------------------- C (error) is graceful, neutral

def test_failed_frame_does_not_keep_claiming_live(html):
    fn = _function(_script(html), "snapshotFailed")
    # The badge is switched to the neutral OFFLINE variant...
    assert "badge.classList.add('off')" in fn
    # ...the loading line is cleared...
    assert "camload" in fn
    # ...and the image is hidden rather than left as a broken icon.
    assert "img.style.display='none'" in fn


def test_error_state_carries_no_exception_or_url_detail(html):
    fn = _function(_script(html), "snapshotFailed")
    for leak in ("stack", "message", "err.", "trace", "http://", "https://"):
        assert leak not in fn, f"error card must not render {leak!r}"


# --------------------------------------------- last-updated indication

def test_last_updated_line_exists_and_is_announced(html):
    fn = _function(_script(html), "renderCameras")
    assert 'class="upd"' in fn
    assert 'aria-live="polite"' in fn
    loaded = _function(_script(html), "snapshotLoaded")
    assert "Τελευταία ενημέρωση" in loaded
    assert "clockTime()" in loaded


def test_updated_line_has_a_reserved_height_so_the_card_does_not_jump(html):
    assert ".cam .upd{font-size:11.5px;color:var(--dim);margin-top:2px;min-height:14px}" in html


def test_stage_keeps_a_fixed_ratio_so_the_grid_does_not_reflow(html):
    assert ".cam .stage{position:relative;aspect-ratio:16/9" in html


# --------------------------------------------- no retry loop

def test_camera_load_retries_once_then_stops(html):
    fn = _function(_script(html), "loadCameras")
    assert "attempt<2" in fn
    assert "setTimeout" in fn
    # Bounded, not a while(true)/recursion.
    assert "while" not in fn
    assert "loadCameras(" not in fn.replace("async function loadCameras(", "")


def test_camera_load_timeout_budget_is_bounded(html):
    fn = _function(_script(html), "loadCameras")
    delays = [int(x) for x in re.findall(r"setTimeout\(res,(\d+)\)", fn)]
    assert delays and sum(delays) <= 5000


# --------------------------------------------- LIVE: still correct after pass

def test_live_affordance_is_absent_without_a_server_live_block(html):
    fn = _function(_script(html), "renderCameras")
    assert "const golive = (live && c.live)" in fn
    assert "openCamLive" in fn


def test_close_live_still_returns_to_the_snapshot_through_the_state_machine(html):
    fn = _function(_script(html), "closeCamLive")
    assert "stage.classList.remove('playing')" in fn
    assert "camBeginLoad(id)" in fn
    assert "img.src=camSnapshotSrc(c, CAMS.stamp)" in fn


def test_refresh_reuses_the_same_state_machine(html):
    fn = _function(_script(html), "tickCameras")
    assert "camBeginLoad(c.id)" in fn
    # Re-rendering the whole card on every tick would drop focus and flicker.
    assert "renderCameras()" not in fn


def test_a_later_successful_frame_clears_a_previous_error(html):
    """Without this, one failed refresh would leave the card permanently grey and
    the loading line would never return on the next tick."""
    fn = _function(_script(html), "camBeginLoad")
    assert "badge.classList.remove('off')" in fn
    assert "stage.querySelector('.off')" in fn


# --------------------------------------------- accessibility

def test_camera_card_is_a_labelled_group(html):
    fn = _function(_script(html), "renderCameras")
    assert 'role="group"' in fn
    assert "aria-label=" in fn


def test_frame_has_alt_text_and_live_button_is_labelled(html):
    fn = _function(_script(html), "renderCameras")
    assert 'alt="' in fn
    assert "aria-label=\"Άνοιγμα ζωντανής ροής" in fn


def test_focus_visible_style_still_applies_to_buttons(html):
    # The camera controls are ordinary buttons/links; the global rule must hold.
    assert "button:focus-visible{outline:2px solid var(--accent)" in html
