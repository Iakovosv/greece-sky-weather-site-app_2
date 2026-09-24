"""The lead-time selector for the Εξειδικευμένα tab.

Two things can go wrong here and both are silent: a step the GFS run does not
publish (a 404 the UI does not explain), and a day count that reaches past what
the tier pays for. The tests below pin the published-step rule and the clamp.
"""
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import wx  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app.app)


def test_gfs_is_hourly_then_three_hourly():
    """The published cadence: hourly to +120 h, 3-hourly after. Step 0 is the
    analysis, which the series fetch never requests, so the list starts at +1."""
    steps = wx.gfs_steps(240)
    assert [s for s in steps if s <= 120] == list(range(1, 121))
    assert 123 in steps and 126 in steps
    assert 121 not in steps and 122 not in steps and 0 not in steps


@pytest.mark.parametrize("want,expected", [
    (1, 1), (12, 12), (120, 120),
    (121, 120),      # 121 is not published; nearest is 120
    (122, 123),      # 122 is a tie in distance; the later step wins
    (241, 240),      # beyond the horizon clamps, never rounds up to a missing step
    (999, 240),
])
def test_nearest_step_snaps_to_a_published_step(want, expected):
    assert app.nearest_gfs_step(want, 240) == expected


def test_nearest_step_respects_the_free_horizon():
    """A free caller asking for day 5 must be kept inside 72 h."""
    assert app.nearest_gfs_step(120, 72) == 72


def test_nearest_step_output_is_always_published():
    """Whatever the input, the result is a step the run genuinely has — that is
    the whole point, because a missing step is a 404 with no explanation."""
    for want in range(0, 300):
        step = app.nearest_gfs_step(want, 240)
        assert step in wx.gfs_steps(240), f"{want} -> {step} missing"


def test_step_to_utc_adds_hours_to_the_run():
    assert wx.step_to_utc("2026092300", 0) == "2026-09-23T00:00Z"
    assert wx.step_to_utc("2026092300", 240) == "2026-10-03T00:00Z"


def test_expert_endpoint_is_pro_gated(client):
    """Gated server-side, before any download: a free caller gets 403 and no
    numbers at all, not a page that hides them in the browser."""
    r = client.get("/api/expert", params={"lat": 37.98, "lon": 23.73, "day": 1, "hour": 6})
    assert r.status_code == 403


def test_picker_only_offers_published_hours():
    """The UI's xSteps and xDays must agree with the server's published-step
    rule, or the selector offers an hour that then fails."""
    src = Path(app.__file__).read_text()
    m = re.search(r"function xSteps\(day, upto\)\{(.*?)\n\}", src, re.S)
    assert m, "xSteps not found"
    assert "abs<=120 || abs%3===0" in m.group(1)
    d = re.search(r"function xDays\(upto\)\{(.*?)\n\}", src, re.S)
    assert d, "xDays not found"
    assert "abs<=120 || abs%3===0" in d.group(1)
