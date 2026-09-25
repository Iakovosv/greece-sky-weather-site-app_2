"""Camera refresh-cadence consistency (audit finding M-3).

The card tells the visitor "Αυτόματη εικόνα: κάθε N λεπτά" from
`snapshot_interval_min`, while the LIVE refresh was driven by a fixed
`refresh_seconds` (60 s) ticker. Two independent numbers, so the UI could claim a
5-minute feed and poll it every minute.

The fix makes `snapshot_interval_min` the single cadence and keeps
`refresh_seconds` as tick granularity that can never exceed it. These tests read
the served functions and assert the relationship, so the two numbers cannot drift
apart again without failing here.

`refreshTickMs()` and `tickCameras()` are also executed in Node against real
payload shapes to prove the gate actually suppresses an early refresh.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")


def _served_script() -> str:
    html = TestClient(app_module.app).get("/").text
    m = re.search(r"<script>(.*?)</script></body></html>", html, re.S)
    assert m, "no main <script> block"
    return m.group(1)


def _fn_src(js: str, name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", js)
    assert m, f"{name} not found"
    start = m.start()
    i = js.index("{", m.start())
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[start:j + 1]
    raise AssertionError(f"unbalanced braces for {name}")


# ------------------------------------------------- cadence is single-sourced

def test_the_displayed_cadence_is_snapshot_interval_min():
    body = _fn_src(_served_script(), "renderCameras")
    assert "c.snapshot_interval_min" in body
    # The label must be built from that same value, not a hardcoded minute count.
    assert "pluralMin(c.snapshot_interval_min)" in body


def test_the_ticker_uses_the_shared_cadence_helper_not_a_bare_refresh_seconds():
    body = _fn_src(_served_script(), "toggleLive")
    assert "refreshTickMs()" in body, \
        "the interval must come from the single cadence helper"
    # The old form: the tick was refresh_seconds itself, unrelated to the feed.
    assert "setInterval(tickCameras, (CAMS&&CAMS.refresh_seconds||60)*1000)" not in body


# ------------------------------------------------- the gate suppresses early refresh

def _run_tick(tmp_path, *, interval_min: int, elapsed_ms: int) -> dict:
    """Run the real tickCameras()/refreshTickMs() in Node with a recording DOM.

    Returns how many snapshot refreshes the tick performed, so the test can assert
    that a camera is not re-fetched before its own cadence has elapsed.
    """
    js = _served_script()
    pieces = "\n".join(_fn_src(js, n) for n in
                       ("camSnapshotSrc", "camBeginLoad", "camUpdate", "refreshTickMs",
                        "tickCameras"))
    payload = {
        "cameras": [{"id": "ilioupoli", "name": "Ilioupoli Sky", "region": "Attiki",
                     "lat": 37.9333, "lon": 23.75,
                     "snapshot": "https://picsum.photos/seed/cam/800/450",
                     "snapshot_via": "direct", "status": "live",
                     "snapshot_interval_min": interval_min,
                     "live": {"provider": "youtube", "video_id": "MOCK"}}],
        "refresh_seconds": 60, "stamp": 100, "configured_count": 1, "note": None,
    }
    harness = f"""
const code = {json.dumps(pieces)};
eval(code);

let REFRESHES = 0;
const imgs = {{}};
function mkEl() {{
  return {{
    _src: '',
    set src(v) {{ this._src = v; if (v) REFRESHES++; }},
    get src() {{ return this._src; }},
    style: {{}}, hidden: false,
    classList: {{ add() {{}}, remove() {{}}, toggle() {{}} }},
    querySelector() {{ return null; }},
    appendChild() {{}}, remove() {{}},
    insertBefore() {{}}, firstChild: null,
  }};
}}
const store = {{}};
globalThis.document = {{
  getElementById: (id) => (store[id] || (store[id] = mkEl())),
  createElement: () => mkEl(),
}};
globalThis.CAMS = {json.dumps(payload)};
globalThis.LIVE_ON = true;
globalThis.CAM_LAST = {{}};

// Prime: the very first tick always refreshes.
tickCameras();
const afterFirst = REFRESHES;
REFRESHES = 0;

// Simulate time passing without a real wait: the gate compares Date.now() to
// CAM_LAST, so move CAM_LAST forward by `elapsed` relative to now.
CAM_LAST['ilioupoli'] = Date.now() - {elapsed_ms};
tickCameras();
const afterSecond = REFRESHES;

process.stdout.write(JSON.stringify({{afterFirst, afterSecond,
  tickMs: refreshTickMs()}}));
"""
    script = tmp_path / "tick.js"
    script.write_text(harness, encoding="utf-8")
    p = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_a_camera_is_not_refreshed_before_its_own_interval(tmp_path):
    """cadence 5 min, 60 s elapsed -> the second tick must not re-fetch."""
    out = _run_tick(tmp_path, interval_min=5, elapsed_ms=60_000)
    assert out["afterFirst"] == 1, f"first tick should refresh once: {out}"
    assert out["afterSecond"] == 0, f"early tick must be gated: {out}"


def test_a_camera_refreshes_once_its_interval_has_elapsed(tmp_path):
    out = _run_tick(tmp_path, interval_min=5, elapsed_ms=5 * 60_000 + 1000)
    assert out["afterFirst"] == 1
    assert out["afterSecond"] == 1, f"a due camera must refresh: {out}"


def test_the_tick_never_exceeds_the_smallest_camera_interval(tmp_path):
    """With a 1-minute feed configured, the ticker must run every 60 s, not 300 s,
    or the same 5-minute number would again disagree with reality."""
    out = _run_tick(tmp_path, interval_min=1, elapsed_ms=0)
    assert out["tickMs"] == 60_000, out


def test_the_tick_does_not_run_faster_than_refresh_seconds(tmp_path):
    """A camera whose interval is longer than the tick granularity keeps the
    ticker at refresh_seconds; it is the per-camera gate, not the timer, that
    decides when the image is re-fetched."""
    out = _run_tick(tmp_path, interval_min=30, elapsed_ms=0)
    assert out["tickMs"] == 60_000, out
