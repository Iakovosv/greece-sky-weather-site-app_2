"""PWA / iOS web push surface: manifest, service worker, icons, page wiring.

These are served-asset tests, not browser tests: they pin the facts a browser or
an OS checks — the manifest parses and has the icons and the `standalone`
display iOS requires, the worker is reachable from the site root, and the page
carries the apple-touch metadata that decides whether "Add to Home Screen" is
offered and whether web push is available at all on iOS.

Nothing here asserts the notification rules; those are in test_notify.py.
"""
from __future__ import annotations

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "static")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WX_DB", str(tmp_path / "station.db"))
    monkeypatch.setenv("WX_RATE_LIMIT_DISABLED", "1")
    return TestClient(app_module.app)


@pytest.fixture()
def html(client) -> str:
    r = client.get("/")
    assert r.status_code == 200
    return r.text


# ---------------------------------------------------------------- manifest

def test_manifest_is_served_with_the_right_content_type(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert "manifest" in r.headers["content-type"]


def test_manifest_is_valid_json_with_required_fields(client):
    d = client.get("/manifest.webmanifest").json()
    assert d["name"] and d["short_name"]
    # `display: standalone` is what makes iOS install it as a web app and, in
    # turn, what makes web push available there at all.
    assert d["display"] == "standalone"
    assert d["start_url"] == "/" and d["scope"] == "/"


def test_manifest_declares_both_icon_purposes(client):
    d = client.get("/manifest.webmanifest").json()
    purposes = {i.get("purpose") for i in d["icons"]}
    assert "any" in purposes and "maskable" in purposes
    sizes = {i["sizes"] for i in d["icons"]}
    assert "192x192" in sizes and "512x512" in sizes


def test_manifest_icons_actually_exist_and_are_pngs(client):
    d = client.get("/manifest.webmanifest").json()
    for icon in d["icons"]:
        path = os.path.join(STATIC, os.path.basename(icon["src"]))
        assert os.path.exists(path), f"{icon['src']} is referenced but missing"
        with open(path, "rb") as f:
            assert f.read(8) == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"


# ---------------------------------------------------------------- service worker

def test_service_worker_is_served_from_the_site_root(client):
    """Scope is capped at the worker's directory. Served from /static/ it would
    only control /static/*, so the notification click could not open the app."""
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers.get("service-worker-allowed") == "/"


def test_service_worker_is_not_cached(client):
    r = client.get("/sw.js")
    assert "no-cache" in r.headers.get("cache-control", "")


def test_service_worker_handles_push_and_click(client):
    js = client.get("/sw.js").text
    assert "addEventListener('push'" in js
    assert "showNotification" in js
    assert "addEventListener('notificationclick'" in js


def test_service_worker_has_no_fetch_handler(client):
    """No offline cache on purpose: a stale forecast page is worse than a network
    error. Pinning the absence keeps a well-meaning future edit from adding one."""
    js = client.get("/sw.js").text
    assert "addEventListener('fetch'" not in js


def test_service_worker_uses_the_server_tag_for_dedupe(client):
    js = client.get("/sw.js").text
    assert "data.tag" in js or "tag:" in js


# ---------------------------------------------------------------- static allow-list

def test_icons_are_served_from_static(client):
    for name in ("icon-192.png", "icon-512.png", "icon-maskable-512.png",
                 "apple-touch-icon.png"):
        r = client.get("/static/" + name)
        assert r.status_code == 200, name
        assert "image/png" in r.headers["content-type"]


def test_static_allow_list_rejects_path_traversal(client):
    r = client.get("/static/../app.py")
    assert r.status_code in (404, 400)


def test_static_allow_list_rejects_an_unknown_name(client):
    assert client.get("/static/secrets.txt").status_code == 404


# ---------------------------------------------------------------- page head

def test_page_links_the_manifest(html):
    assert 'rel="manifest" href="/manifest.webmanifest"' in html


def test_page_declares_apple_touch_icon(html):
    assert 'rel="apple-touch-icon"' in html
    assert "apple-touch-icon.png" in html


def test_page_declares_apple_mobile_web_app_capable(html):
    """Without this meta tag iOS never treats the site as an installed web app,
    and web push is unavailable no matter what the server does."""
    assert 'name="apple-mobile-web-app-capable" content="yes"' in html


def test_page_declares_a_theme_color(html):
    assert 'name="theme-color"' in html


# ---------------------------------------------------------------- frontend wiring

def _script(html: str) -> str:
    m = re.search(r"<script>(.*?)</script></body></html>", html, re.S)
    assert m, "no main script block"
    return m.group(1)


def test_frontend_registers_the_worker_and_subscribes(html):
    js = _script(html)
    assert "serviceWorker.register('/sw.js')" in js
    assert "pushManager.subscribe" in js
    assert "urlBase64ToUint8Array" in js


def test_frontend_sends_the_expected_subscribe_payload(html):
    js = _script(html)
    for field in ("subscription:sub.toJSON()", "ios_standalone", "location:",
                  "applicationServerKey"):
        assert field in js, field


def test_frontend_shows_ios_add_to_home_screen_guidance(html):
    js = _script(html)
    assert "οθόνη αφετηρίας" in js
    assert "isStandalone" in js


def test_frontend_never_sends_the_location_as_pro_secret(html):
    """The page asks for a location and a subscription; it must not be the thing
    that decides PRO. There is no client-side entitlement flag to flip."""
    js = _script(html)
    assert "is_pro=true" not in js.replace(" ", "")
    assert "localStorage.setItem('wx_pro'" not in js
    # The tier always comes from the server, never from storage alone.
    assert "/api/me" in js


def test_frontend_renders_the_server_state_not_its_own(html):
    js = _script(html)
    assert "/api/notify/state" in js
