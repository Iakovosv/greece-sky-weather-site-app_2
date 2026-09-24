"""Tests for the .env loader, URL redaction and camera short form.

These cover the pieces where a mistake is invisible until it is expensive: a
credential leaking into a response body, or a camera mapping that silently drops
a site. The map and its tile configuration were removed from the UI, so the tile
tests that used to live here are gone with them.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import envfile


# ------------------------------------------------------------------ .env parsing

def test_parse_ignores_comments_and_blank_lines():
    assert envfile.parse("# a comment\n\nKEY=value\n") == {"KEY": "value"}


def test_parse_strips_optional_quotes():
    assert envfile.parse('A="x y"\nB=\'z\'\n') == {"A": "x y", "B": "z"}


def test_parse_drops_an_unquoted_trailing_comment():
    assert envfile.parse("KEY=value  # why\n") == {"KEY": "value"}


def test_parse_keeps_a_hash_inside_quotes():
    """A quoted value is data: truncating at '#' would corrupt a URL."""
    assert envfile.parse('U="https://x/{z}/{y}.png?a=b#frag"\n') == {
        "U": "https://x/{z}/{y}.png?a=b#frag"}


def test_parse_keeps_a_hash_that_is_part_of_the_value():
    # No whitespace before '#', so it cannot be a comment.
    assert envfile.parse("U=https://x/y#frag\n") == {"U": "https://x/y#frag"}


def test_parse_tolerates_a_line_without_equals_separator_value():
    # "KEY=" is a valid (empty) assignment; a bare word is not and is skipped.
    assert envfile.parse("KEY=\njustaword\n") == {"KEY": ""}


def test_parse_accepts_export_prefix():
    assert envfile.parse("export KEY=value\n") == {"KEY": "value"}


def test_parse_of_garbage_returns_a_mapping_never_raises():
    assert envfile.parse("===bad\n=noname\n") == {}


# ------------------------------------------------------------------ .env loading

def test_load_does_not_override_the_real_environment(tmp_path, monkeypatch):
    """A deployed systemd/container variable is a decision; a file is not."""
    f = tmp_path / ".env"
    f.write_text("WX_TEST_KEY=from_file\n")
    monkeypatch.setenv("WX_TEST_KEY", "from_real_env")
    applied = envfile.load(f)
    assert "WX_TEST_KEY" not in applied
    assert os.environ["WX_TEST_KEY"] == "from_real_env"


def test_load_sets_variables_that_are_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("WX_TEST_ABSENT", raising=False)
    f = tmp_path / ".env"
    f.write_text("WX_TEST_ABSENT=hello\n")
    assert envfile.load(f) == ["WX_TEST_ABSENT"]
    assert os.environ["WX_TEST_ABSENT"] == "hello"


def test_load_takes_all_values_without_echoing_them(tmp_path, monkeypatch, capsys):
    """Secrets must not reach stdout. load() returns key names only, never values."""
    monkeypatch.delenv("WX_CAM_SECRET", raising=False)
    f = tmp_path / ".env"
    f.write_text("WX_CAM_SECRET=super-secret-value\n")
    applied = envfile.load(f)
    assert "super-secret-value" not in repr(applied)
    assert "super-secret-value" not in capsys.readouterr().out


def test_load_of_a_missing_file_is_a_noop(tmp_path):
    assert envfile.load(tmp_path / "nope.env") == []


# ------------------------------------------------------------------ redaction

def test_redact_url_hides_a_key_but_keeps_the_rest():
    import app
    got = app.redact_url("https://x/{z}/{y}.png?api_key=SECRET&style=light")
    assert "SECRET" not in got
    assert "api_key=<redacted>" in got
    assert "style=light" in got


def test_redact_url_leaves_a_keyless_url_untouched():
    import app
    url = "https://tile.example/{z}/{x}/{y}.png"
    assert app.redact_url(url) == url


@pytest.mark.parametrize("name", ["api_key", "apikey", "access_token", "secret", "password"])
def test_redact_url_catches_the_usual_credential_names(name):
    import app
    assert "LEAK" not in app.redact_url(f"https://x/t.png?{name}=LEAK")


# ------------------------------------------------------------------ camera short form

def test_camera_short_form_maps_urls_onto_the_builtin_sites(monkeypatch):
    import cameras as cams
    monkeypatch.setenv("WX_CAMERAS", json.dumps({
        "ilioupoli": "https://cam.example/1.jpg",
        "glinado": "https://cam.example/2.jpg"}))
    importlib.reload(cams)
    payload = cams.camera_payload(now=1000.0)
    assert payload["configured_count"] == 2
    by_id = {c["id"]: c for c in payload["cameras"]}
    # name/region/coords come from the built-in entry, only the URL is new
    assert by_id["ilioupoli"]["name"] == "Ilioupoli Sky"
    assert by_id["ilioupoli"]["region"] == "Αττική"
    assert by_id["ilioupoli"]["lat"] == pytest.approx(37.9333)
    assert by_id["ilioupoli"]["snapshot"] == "https://cam.example/1.jpg"


def test_camera_short_form_accepts_an_unknown_id(monkeypatch):
    import cameras as cams
    monkeypatch.setenv("WX_CAMERAS", json.dumps({"meteora": "https://cam.example/m.jpg"}))
    importlib.reload(cams)
    cam = cams.camera_payload()["cameras"][0]
    assert cam["id"] == "meteora" and cam["snapshot"] == "https://cam.example/m.jpg"
    assert cam["lat"] is None       # no built-in site to borrow coordinates from


def test_camera_long_form_still_works(monkeypatch):
    import cameras as cams
    monkeypatch.setenv("WX_CAMERAS", json.dumps([
        {"id": "custom", "name": "Custom", "lat": 38.0, "lon": 23.0,
         "snapshot": "https://cam.example/c.jpg", "timelapse": "https://cam.example/c.mp4"}]))
    importlib.reload(cams)
    cam = cams.camera_payload()["cameras"][0]
    assert cam["name"] == "Custom" and cam["has_timelapse"] is True


def test_camera_empty_mapping_falls_back_to_defaults(monkeypatch):
    import cameras as cams
    monkeypatch.setenv("WX_CAMERAS", "{}")
    importlib.reload(cams)
    assert {c["id"] for c in cams.camera_payload()["cameras"]} == {"ilioupoli", "glinado"}


# ------------------------------------------------------------------ Skew-T theme

def _synthetic_sounding():
    import numpy as np
    import xarray as xr
    p = np.array([1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100], dtype=float)
    n = len(p)
    return xr.Dataset(
        {"t": ("isobaricInhPa", 300 - np.arange(n) * 6.0),
         "r": ("isobaricInhPa", np.linspace(70, 20, n)),
         "u": ("isobaricInhPa", np.linspace(2, 30, n)),
         "v": ("isobaricInhPa", np.linspace(-2, 12, n))},
        coords={"isobaricInhPa": p,
                "latitude": ("latitude", [37.98]), "longitude": ("longitude", [23.73])})


def test_skewt_is_themed_for_the_dark_card():
    """A white chart in a dark card reads as a pasted-in image.

    savefig's bbox_inches="tight" leaves its margin in the figure's own colour, so
    the facecolor has to be set in both places; asserting on the corners catches a
    regression in either one.
    """
    import app
    from PIL import Image
    import io as _io

    png = app.skewt_png(_synthetic_sounding(), 37.98, 23.73, 12)
    im = Image.open(_io.BytesIO(png)).convert("RGB")
    w, h = im.size
    corners = [im.getpixel((2, 2)), im.getpixel((w - 3, 2)),
               im.getpixel((2, h - 3)), im.getpixel((w - 3, h - 3))]
    # The panel is the glass card composited over the page wash. Read the value
    # from the theme rather than hard-coding it here, so the two cannot drift.
    panel = app.SKEWT_PANEL_RGB
    assert all(all(abs(a - b) <= 14 for a, b in zip(c, panel)) for c in corners), corners
    assert not any(min(c) > 240 for c in corners), "a white margin came back"


def test_skewt_panel_matches_the_composited_glass_card():
    """The PNG has to blend into its card, not sit on it as a lighter block.

    card rgba(18,24,38,.70) over the brightest part of the wash is #131e30; a
    deviation here means the chart will read as a pasted-in rectangle again.
    """
    import app

    def over(fg, bg):
        r, g, b, a = fg
        return tuple(round(f * a + x * (1 - a)) for f, x in zip((r, g, b), bg))

    wash = over((77, 163, 255, 0.22), (7, 11, 20))
    card = over((18, 24, 38, 0.70), wash)
    assert all(abs(a - b) <= 4 for a, b in zip(app.SKEWT_PANEL_RGB, card)), (
        app.SKEWT_PANEL_RGB, card)
