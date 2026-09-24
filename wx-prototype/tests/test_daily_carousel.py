"""The 10-day daily carousel and its PRO gate.

Two properties matter here and a later edit can quietly break either:

  * the first cards a visitor sees are real days - the free tier must actually
    carry three days of data, or the "free" cards are empty shells;
  * the locked cards contain no numbers. A blurred placeholder that reads like a
    forecast invites a screenshot and a wrong decision, which is the failure the
    whole gating design exists to prevent. These tests read the rendered markup,
    so blur-as-presentation cannot hide a real value underneath.

The rendered/interactive checks (scroll-snap, actual blur, overflow) live in the
browser probes; these run without a browser.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import entitlements as ent  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app.app)


def _page(client) -> str:
    return client.get("/").text


# --- structure ---------------------------------------------------------------

def test_carousel_markup_exists(client):
    html = _page(client)
    assert "function dailyCarousel" in html
    assert "function dayCard" in html
    assert "function lockedDayCard" in html
    assert 'class="dstrip"' in html


def test_carousel_scrolls_and_snaps(client):
    """A plain overflow row drifts; snap is what makes it feel like cards. The
    snap must be on the strip, and horizontal only."""
    html = _page(client)
    m = re.search(r"\.dstrip\{([^}]*)\}", html)
    assert m, "no .dstrip rule"
    css = m.group(1)
    assert "overflow-x:auto" in css
    assert "scroll-snap-type:x mandatory" in css
    assert "overscroll-behavior-x:contain" in css


def test_cards_snap_to_start(client):
    html = _page(client)
    m = re.search(r"\.dcard\{([^}]*)\}", html)
    assert m and "scroll-snap-align:start" in m.group(1)


def test_cards_have_a_fixed_width_so_a_row_can_scroll(client):
    """Cards must not be flex-grow, or the row fits the viewport and never
    scrolls - which silently removes days 4-10 from a phone."""
    html = _page(client)
    m = re.search(r"\.dcard\{([^}]*)\}", html)
    css = m.group(1)
    assert "flex:0 0 auto" in css
    assert "width:" in css


# --- the locked cards carry no data -----------------------------------------

def test_locked_card_markup_is_placeholder_only():
    """Build a locked card and assert no numeric value is in it."""
    html = _page(TestClient(app.app))
    m = re.search(r"function lockedDayCard\(day, label\)\{(.*?)\n\}\n", html, re.S)
    assert m, "lockedDayCard not found"
    body = m.group(1)
    # the only digits allowed are loop counters and inline geometry
    for bad in ("tmax", "tmin", "rain_mm", "wind_max", "feels", "bft"):
        assert bad not in body, f"locked card renders {bad}"
    assert "\u2014" in body or "dph" in body


def test_blur_is_presentation_and_the_css_says_so(client):
    html = _page(client)
    m = re.search(r"\.dcard\.locked \.blurred\{([^}]*)\}", html)
    assert m and "filter:blur" in m.group(1)
    # comment must state that the server gate, not the blur, is the control
    assert "never reach the browser" in html or "never receives them" in html


def test_locked_cards_show_a_pro_badge(client):
    html = _page(client)
    m = re.search(r"\.dcard \.probadge\{([^}]*)\}", html)
    assert m, "no probadge rule"
    assert "color:" in m.group(1)
    # the card itself must emit the badge, not just define the class
    lm = re.search(r"function lockedDayCard\(day, label\)\{(.*?)\n\}\n", html, re.S)
    assert 'class="probadge"' in lm.group(1)
    assert "🔒" in lm.group(1)


# --- the free tier really carries three days --------------------------------

def test_free_tier_is_three_days():
    assert ent.FREE_HOURS == 72
    assert ent.FREE_HOURS // 24 == 3
    assert ent.PRO_HOURS // 24 == 10


def test_plan_copy_matches_the_real_window():
    p = ent.plan_payload()
    assert p["free_hours"] == 72
    assert "3" in p["free_display"]
    assert p["pro_locked_days"] == "Ημέρες 4–10"


def test_daily_summary_labels_are_real_dates():
    """Each bucket gets a weekday and a date, derived from the run. A card that
    says 'day 4' makes the reader count; a date does not."""
    hours = [{"step_h": s, "t": 20.0, "feels": 19.0, "precip": 0.0,
              "wind": 10.0, "gust": 15.0, "cloud_pct": 10}
             for s in range(1, 73)]
    daily = app.daily_summary(hours, "2026092306")
    assert [d["day"] for d in daily] == [1, 2, 3]
    for d in daily:
        assert d["weekday"] in app.astro.GREEK_DAYS
        assert re.fullmatch(r"\d{1,2}/\d{1,2}", d["date_label"])
    assert daily[0]["date_label"] == "23/9"
    assert daily[1]["date_label"] == "24/9"


def test_day_labels_track_the_run_hour_not_just_the_date():
    """A late run shifts the first local day; the label must follow the timestamp,
    not assume run_date + N."""
    # 23:00Z in summer is already 02:00 local on the 24th at +3
    lab = app._day_label("2026092323", 1, 0)
    assert lab["date_label"] == "24/9"


def test_daily_summary_without_a_run_still_works():
    """The label is decorative; a missing run id must not drop the numbers."""
    hours = [{"step_h": s, "t": 20.0, "feels": 19.0, "precip": 0.0,
              "wind": 10.0, "gust": 15.0, "cloud_pct": 10}
             for s in range(1, 25)]
    daily = app.daily_summary(hours)
    assert len(daily) == 1
    assert daily[0]["tmin"] == 20.0
    assert daily[0]["date_label"] is None


def test_day_card_icon_comes_from_the_sky_condition():
    """The card icon must reuse the hero's sky_condition, so the glyph and the
    wording cannot disagree about the same day."""
    hours = [{"step_h": s, "t": 20.0, "feels": 19.0, "precip": 0.0,
              "wind": 10.0, "gust": 15.0, "cloud_pct": 5} for s in range(1, 25)]
    d = app.daily_summary(hours)[0]
    expected = app.sky_condition(d["cloud_pct"], d["rain_max_h"], d["bft_max"])
    assert d["icon"] == expected["icon"]
    assert d["condition"] == expected["text"]


def test_wet_day_icon_reflects_the_rainaiest_hour_not_the_mean():
    """An overnight storm must not be hidden by a dry daylight mean."""
    hours = [{"step_h": s, "t": 20.0, "feels": 19.0,
              "precip": (8.0 if s in (3, 4) else 0.0),
              "wind": 10.0, "gust": 15.0, "cloud_pct": 20} for s in range(1, 25)]
    d = app.daily_summary(hours)[0]
    assert d["rain_max_h"] == 8.0
    assert d["icon"] in ("🌧️",)


# --- upsell ------------------------------------------------------------------

def test_upsell_replaces_the_old_skeleton_table(client):
    html = _page(client)
    assert "function proUpsell" in html
    # the old days-3..10 skeleton must be gone; it duplicated the carousel
    assert "function lockedDaysBlock" not in html
    assert "Ημέρες 3–10" not in html


def test_upsell_is_hidden_for_pro():
    """proUpsell returns '' when the tier is pro; guard the early return."""
    html = _page(TestClient(app.app))
    m = re.search(r"function proUpsell\(d\)\{(.*?)\n\}", html, re.S)
    assert m and "if(t.is_pro) return ''" in m.group(1)
