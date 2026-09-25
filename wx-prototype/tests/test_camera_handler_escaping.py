"""Regression tests for camera inline-handler escaping (audit finding H-1).

Camera `id`/`name` come from `WX_CAMERAS`, and `renderCameras()` builds inline
HTML handlers (`onload="snapshotLoaded('...')"`) by interpolating those values.
An inline handler argument is a *JavaScript string literal*, not HTML text, so
`esc()` alone is not enough: the browser decodes `&#39;` back to `'` before the
JS parser runs, which ends the literal and either breaks the handler or lets an
injected expression execute.

These tests do not read the template and look for a substring. They take the
real `esc`, `jsq`, `camSnapshotSrc`, `pluralMin` and `renderCameras` functions out
of the served page, run them in Node against hostile configuration values, and
then parse the handlers of the markup the real code produced -- the same two
steps a browser performs (HTML entity decode, then JS parse/eval). Node is a
standard dev tool here; the suite is skipped if it is absent rather than faked.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required for JS handler checks")

MOCK_SNAPSHOT = "https://picsum.photos/seed/cam/800/450"

# Values a careless or hostile camera config could carry. Every one of them must
# survive as data and never change the structure of the handler.
HOSTILE = [
    "Nikos' Beach",                 # apostrophe ends a JS literal
    "back\\slash",                  # backslash escapes the closing quote
    "two\nlines",                   # newline breaks a JS literal
    "</script><script>alert(1)</script>",   # HTML-like / script-closing
    "');console.log('INJECTED');//",        # direct injection attempt
    '`;console.log("TEMPLATE");//',         # backtick + quote
    "\u2028line-sep",               # JS line separator
]


def _served_script() -> str:
    html = TestClient(app_module.app).get("/").text
    m = re.search(r"<script>(.*?)</script></body></html>", html, re.S)
    assert m, "no main <script> block in the served page"
    return m.group(1)


def _fn_src(js: str, name: str) -> str:
    """Source of `function name(...) { ... }` with brace matching, so nested
    braces in the body do not truncate it (the regex helper used elsewhere
    stops at the next `function` and would cut a nested arrow function)."""
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", js)
    assert m, f"function {name} not found in served page"
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


def _run_node(tmp_path, cameras: list[dict]) -> dict:
    """Execute the real render functions in Node and return the analysis.

    Node builds the card markup through the *served* code, then for every inline
    handler it decodes HTML entities and compiles the result as JavaScript with a
    recording stub. The returned dict reports, per handler, whether it parsed and
    which values the stub received.
    """
    js = _served_script()
    pieces = "\n".join(_fn_src(js, n) for n in
                       ("esc", "jsq", "camSnapshotSrc", "pluralMin", "renderCameras"))

    harness = f"""
const __fn = {json.dumps(pieces)};
eval(__fn);

// Minimal DOM: capture the markup renderCameras emits.
const BOX = {{innerHTML: '', textContent: ''}};
const SUB = {{textContent: ''}};
globalThis.document = {{
  getElementById: (id) => (id === 'cam-sub' ? SUB : BOX),
  createElement: () => ({{}}),
}};
globalThis.CAMS = {json.dumps({"cameras": cameras, "stamp": 42, "note": None,
                               "refresh_seconds": 60, "configured_count": 1})};

renderCameras();
const html = BOX.innerHTML;

// What a browser does to an attribute value before the JS engine sees it.
function decodeEntities(s) {{
  return s.replace(/&#39;/g, "'").replace(/&quot;/g, '"')
          .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}}

const ATTRIBUTES = ['onload', 'onerror', 'onclick'];
const results = [];
const attrRe = new RegExp('(' + ATTRIBUTES.join('|') + ')="([^"]*)"', 'g');
let m;
while ((m = attrRe.exec(html)) !== null) {{
  const name = m[1];
  const raw = m[2];
  const src = decodeEntities(raw);
  const calls = [];
  let parseError = null;
  let injected = false;
  try {{
    const sideEffects = [];
    const stub = (...a) => calls.push(a);
    const fakeConsole = {{ log: (...a) => sideEffects.push(['log', a]) }};
    // Compile the decoded handler exactly as the browser would.
    const f = new Function('snapshotLoaded', 'snapshotFailed', 'openCamLive',
                           'gotoPoint', 'console', src);
    f(stub, stub, stub, stub, fakeConsole);
    injected = sideEffects.length > 0;
  }} catch (e) {{
    parseError = String(e && e.message || e);
  }}
  results.push({{name, raw, src, calls, parseError, injected}});
}}

process.stdout.write(JSON.stringify({{html, results}}));
"""

    script = tmp_path / "harness.js"
    script.write_text(harness, encoding="utf-8")
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _cameras_with(value: str, *, field: str = "id") -> list[dict]:
    cam = {"id": "ilioupoli", "name": "Ilioupoli Sky", "region": "Attiki",
           "lat": 37.9333, "lon": 23.75, "snapshot": MOCK_SNAPSHOT,
           "snapshot_via": "direct", "status": "live",
           "snapshot_interval_min": 5,
           "live": {"provider": "youtube", "video_id": "MOCKPUBLICID"}}
    cam[field] = value
    return [cam]


# ---------------------------------------------------------------- H-1

@pytest.mark.parametrize("value", HOSTILE)
def test_every_inline_handler_still_parses(tmp_path, value):
    """A hostile camera id must not produce a handler the JS parser rejects."""
    out = _run_node(tmp_path, _cameras_with(value, field="id"))
    assert out["results"], "renderCameras produced no inline handlers"
    broken = [(r["name"], r["parseError"]) for r in out["results"] if r["parseError"]]
    assert not broken, f"handler(s) did not parse for id={value!r}: {broken}"


@pytest.mark.parametrize("value", HOSTILE)
def test_no_injected_code_executes_via_the_id(tmp_path, value):
    out = _run_node(tmp_path, _cameras_with(value, field="id"))
    assert not any(r["injected"] for r in out["results"]), \
        f"injected code ran for id={value!r}"


@pytest.mark.parametrize("value", HOSTILE)
def test_no_injected_code_executes_via_the_name(tmp_path, value):
    """The name reaches `gotoPoint(...)` as an inline argument too."""
    out = _run_node(tmp_path, _cameras_with(value, field="name"))
    assert not any(r["injected"] for r in out["results"]), \
        f"injected code ran for name={value!r}"
    broken = [(r["name"], r["parseError"]) for r in out["results"] if r["parseError"]]
    assert not broken, f"handler(s) did not parse for name={value!r}: {broken}"


def test_the_real_camera_id_is_preserved_exactly(tmp_path):
    """Escaping must not mangle the value: the handler receives the original id."""
    out = _run_node(tmp_path, _cameras_with("beach'1", field="id"))
    # Each handler that carries the id as its argument, keyed by its callee name.
    by_callee = {}
    for r in out["results"]:
        assert r["parseError"] is None, r
        callee = r["src"].split("(", 1)[0]
        by_callee[callee] = r["calls"]
    for callee in ("snapshotLoaded", "snapshotFailed", "openCamLive"):
        assert by_callee.get(callee) == [["beach'1"]], f"{callee}: {by_callee.get(callee)!r}"


def test_a_newline_in_the_name_is_preserved_and_does_not_split_the_handler(tmp_path):
    out = _run_node(tmp_path, _cameras_with("A\nB", field="name"))
    goto = [r for r in out["results"] if r["src"].startswith("gotoPoint(")]
    assert goto, "expected the gotoPoint handler"
    assert all(r["parseError"] is None for r in goto), goto
    # `gotoPoint(lat, lon, name)` — the name is the third argument.
    assert goto[0]["calls"] == [[37.9333, 23.75, "A\nB"]], goto[0]["calls"]


def test_script_closing_input_cannot_break_out_of_the_script_block(tmp_path):
    """Even a config value containing `</script>` must not close the document's
    script element, because `<` is escaped inside the JS literal."""
    out = _run_node(tmp_path, _cameras_with("</script><script>alert(1)</script>",
                                            field="name"))
    assert "</script><script>" not in out["html"]
    assert not any(r["injected"] for r in out["results"])


# ------------------------------------------------- structural guard

def test_inline_handlers_route_camera_values_through_jsq():
    """The fix must stay applied: a value interpolated into an inline handler is
    wrapped in `esc(jsq(...))`. A bare `esc(` there is the H-1 bug returning."""
    tpl = TestClient(app_module.app).get("/").text
    body = _fn_src(_served_script(), "renderCameras")
    assert "esc(jsq(c.id))" in body, "camera id must be escaped for a JS literal"
    assert "esc(jsq(c.name))" in body, "camera name must be escaped for a JS literal"
    # The raw-esc-only form of the bug is gone.
    assert "snapshotLoaded(\\''+esc(c.id)" not in body
    assert "openCamLive(\\''+esc(c.id)" not in body
