"""The map is gone, and favourites took its place.

Removing Leaflet is a licensing decision as much as a UI one: with no raster
tiles there is no tile provider to license for commercial use. These tests pin
that removal down, so a later change cannot quietly reintroduce a third-party
tile fetch, and they check that the location paths the map used to own - picking
a point, naming it, getting its elevation - still work without it.
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


# ------------------------------------------------------------------ map removal

def test_no_map_library_is_loaded(client):
    html = _page(client)
    assert "leaflet.js" not in html
    assert "leaflet.css" not in html
    assert "<script src=\"/static/chart.umd.min.js\"></script>" in html


def test_no_tile_or_map_code_remains(client):
    html = _page(client)
    for token in ("TILE_URL", "TILE_ATTRIB", "tileLayer", "L.map(", "toggleMap",
                  "initMap", "mapwrap", "WX_TILE"):
        assert token not in html, token


def test_leaflet_assets_are_no_longer_served(client):
    assert client.get("/static/leaflet.js").status_code == 404
    assert client.get("/static/leaflet.css").status_code == 404
    assert client.get("/static/chart.umd.min.js").status_code == 200


def test_health_reports_no_base_map_and_still_no_non_commercial_sources(client):
    body = client.get("/api/health").json()
    assert body["base_map"] is None
    assert "tiles" not in body
    assert body["non_commercial_sources_used"] is False


# ------------------------------------------------------------------ favourites

def test_favourites_live_in_localstorage_only(client):
    html = _page(client)
    assert "localStorage.setItem(FAV_KEY" in html
    assert "localStorage.getItem(FAV_KEY)" in html
    # no server endpoint is used to store or read favourites
    assert "/api/fav" not in html
    assert client.get("/api/favs").status_code == 404


def test_favourite_controls_exist(client):
    html = _page(client)
    assert 'id="favaddbtn"' in html
    assert 'id="favlist"' in html
    assert "function favAddCurrent" in html
    assert "function favRemove" in html
    assert "function favPick" in html
    assert "function renderFavourites" in html


def test_favourite_labels_are_escaped_before_injection(client):
    """A label comes from a remote geocoder, so it is untrusted markup."""
    html = _page(client)
    m = re.search(r"function renderFavourites\(\)\{(.*?)\n\}", html, re.S)
    assert m, "renderFavourites not found"
    body = m.group(1)
    assert "esc(f.label)" in body
    assert "+f.label+" not in body, "a raw label is interpolated into HTML"


def test_corrupt_favourites_cannot_break_the_page(client):
    """JSON.parse is wrapped, and the result is filtered, so bad storage is inert."""
    html = _page(client)
    m = re.search(r"function favsLoad\(\)\{(.*?)\n\}", html, re.S)
    assert m
    body = m.group(1)
    assert "try{" in body and "catch" in body
    assert "isFinite(f.lat)" in body and "isFinite(f.lon)" in body


# ------------------------------------------------------------------ location paths

def test_manual_coordinates_replace_the_map_drag(client):
    """The map was the only way to reach an unnamed point; the inputs restore it."""
    html = _page(client)
    assert 'id="inp-lat"' in html
    assert 'id="inp-lon"' in html
    assert "function applyManualCoords" in html
    # and it validates before spending a request
    m = re.search(r"function applyManualCoords\(\)\{(.*?)\n\}", html, re.S)
    body = m.group(1)
    assert "−90…90" in body or "-90" in body


def test_elevation_is_resolved_for_every_location_path(client):
    """With no map drag, load() must fetch the DEM itself or the correction breaks."""
    html = _page(client)
    assert "async function resolvePoint" in html
    m = re.search(r"async function load\(lat,lon,label,demPayload,manualElev\)\{(.*?)\n  // Every path",
                  html, re.S)
    assert m, "load() not found"
    body = m.group(1)
    assert "await resolvePoint(lat,lon)" in body
    # a manual value must win over the DEM, and must be passed in rather than
    # inherited from the previous location
    assert "manualElev!=null?manualElev:demElev" in body


def test_search_still_returns_coordinates(client, monkeypatch):
    """The simple text search is the primary replacement for the map."""
    async def fake_geocode(client_, q, lat, lon, *a, **k):
        return [{"name": "Αθήνα", "latitude": 37.9838, "longitude": 23.7275,
                 "countrycode": "GR", "admin1": "Αττική"}]

    monkeypatch.setattr(app.wx, "geocode", fake_geocode)
    r = client.get("/api/resolve", params={"q": "Αθήνα", "lat": 39.0, "lon": 22.0})
    assert r.status_code == 200
    got = r.json()
    assert got[0]["latitude"] == pytest.approx(37.9838)
    assert got[0]["longitude"] == pytest.approx(23.7275)
