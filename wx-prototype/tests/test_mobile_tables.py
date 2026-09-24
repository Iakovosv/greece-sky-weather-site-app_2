"""The wide numeric tables must not push the page sideways on a phone.

The hourly table is seven numeric columns plus a time column. On a 390 px screen
it is the one thing on the page that cannot fit, and before the scroll wrapper it
made the whole document 460 px wide, which lets the user pan the entire page.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app.app)


def _page(client) -> str:
    return client.get("/").text


def test_wide_tables_are_wrapped_in_a_scroll_container(client):
    """Every table is wrapped by wrapTables() after render, so the helper and the
    container style must both exist; otherwise the wrapper is a plain div."""
    html = _page(client)
    assert "function wrapTables" in html
    assert ".tscroll{" in html
    # the wrapper must actually be given the scroll behaviour, not just aligned
    m = re.search(r"\.tscroll\{([^}]*)\}", html)
    assert "overflow-x:auto" in m.group(1)


def test_scroll_wrapper_is_applied_on_every_render_path(client):
    """Simple, expert-locked and expert-full each mount their own HTML; missing
    one leaves that view overflowing. Expert-full wraps its live host because it
    re-renders in place on a time change."""
    html = _page(client)
    assert "wrapTables(document.getElementById('simple'))" in html
    assert "wrapTables(document.getElementById('expert'))" in html
    assert "wrapTables(host)" in html


def test_mobile_table_has_a_min_width_so_cells_do_not_squeeze(client):
    html = _page(client)
    assert re.search(r"\.tscroll table\{min-width:\d+px\}", html)


def test_scroll_wrapper_contains_overscroll(client):
    """Without containment a horizontal swipe at the edge scrolls the page body,
    which reads as the layout breaking."""
    html = _page(client)
    assert "overscroll-behavior-x:contain" in html
