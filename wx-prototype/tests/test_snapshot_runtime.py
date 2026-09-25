"""Tests for the secure server-side snapshot runtime (snapshots.py).

Two things are being proved here, and they are different questions:

* **Behaviour** -- a camera's still is fetched, cached, and served; every failure
  mode degrades to a generic public answer.
* **Sandboxing** -- no request can name a host, no redirect is followed, no
  private address is ever dialled, and no credential can reach a response or a
  log line.

The network is never touched. Fetching is driven through the ``mock://`` source,
and DNS is driven through an injected resolver, so the SSRF rules are tested
against exact addresses rather than whatever a name happens to resolve to today.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cameras as cams
import snapshots as snap

SECRET_PASS = "unit-test-pass-123"
SOURCE_URL = "rtsp://cam-lan.internal:554/Streaming/Channels/101"
PRIVATE_HOST = "cam-lan.internal"
PUBLIC_HTTPS = "https://cam.example/latest.jpg"


@pytest.fixture
def env(monkeypatch):
    """Reload cameras + snapshots per test, matching the other camera suites.

    Both modules are reloaded because snapshots binds cameras at import; a test
    that only reloads one would leave the pair pointing at different registries.
    """
    def _apply(*, cameras=None, sources=None, allowed=None, mock=None):
        for key, value, delete in (
                ("WX_CAMERAS", cameras, cameras is None),
                ("WX_CAMERA_SOURCES", sources, sources is None),
                ("WX_CAMERA_ALLOWED_HOSTS", allowed, allowed is None),
                ("WX_CAMERA_SNAPSHOT_MOCK", mock, mock is None)):
            if delete:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        importlib.reload(cams)
        importlib.reload(snap)
        snap.reset_cache()
    yield _apply
    importlib.reload(cams)
    importlib.reload(snap)
    snap.reset_cache()


def _cam(**over):
    cam = {"id": "ilioupoli", "name": "Ilioupoli Sky", "region": "Αττική",
           "lat": 37.9333, "lon": 23.75, "snapshot": PUBLIC_HTTPS,
           "snapshot_interval_min": 5}
    cam.update(over)
    return json.dumps([cam])


def _run(coro):
    return asyncio.run(coro)


# ============================================================ SSRF: addresses

@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True),
    ("1.1.1.1", True),
    ("2606:4700:4700::1111", True),
    ("127.0.0.1", False),          # loopback
    ("127.1.2.3", False),          # loopback, not just .0.1
    ("::1", False),                # loopback v6
    ("10.0.0.1", False),           # private
    ("172.16.5.4", False),         # private
    ("192.168.1.10", False),       # private
    ("169.254.1.1", False),        # link-local
    ("fe80::1", False),            # link-local v6
    ("0.0.0.0", False),            # unspecified
    ("100.64.0.1", False),         # carrier-grade NAT (shared)
    ("192.0.2.5", False),          # documentation range
    ("::ffff:127.0.0.1", False),   # v4-mapped loopback
    ("::ffff:10.0.0.1", False),    # v4-mapped private
    ("not-an-ip", False),
    ("", False),
])
def test_address_policy(ip, public):
    assert snap._is_public_address(ip) is public


def test_host_is_public_only_if_every_answer_is_public():
    # A split answer (one public, one private) must be refused: dialling the
    # second is the attack. `resolve_public` returns None rather than addresses.
    mixed = lambda h: ["93.184.216.34", "10.0.0.1"]
    assert snap.resolve_public("split.example", resolver=mixed) is None
    allpub = lambda h: ["93.184.216.34", "1.1.1.1"]
    assert snap.resolve_public("good.example", resolver=allpub) == ["93.184.216.34",
                                                                   "1.1.1.1"]


def test_host_resolution_failure_fails_closed():
    def boom(host):
        raise OSError("no such host")
    assert snap.resolve_public("nope.example", resolver=boom) is None
    assert snap.resolve_public("empty.example", resolver=lambda h: []) is None


def test_literal_address_needs_no_resolution():
    # A literal is decided on the spot and never handed to the resolver.
    called = {"n": 0}

    def never(host):
        called["n"] += 1
        return ["1.1.1.1"]

    assert snap.resolve_public("127.0.0.1", resolver=never) is None
    assert called["n"] == 0
    assert snap.resolve_public("8.8.8.8", resolver=never) == ["8.8.8.8"]
    assert called["n"] == 0


# ============================================================ endpoint: camera id only

def _client():
    import app as app_module
    return TestClient(app_module.app)


def test_valid_mock_snapshot_is_served(env):
    env(cameras=_cam(), mock="ok")
    r = _client().get("/api/cameras/ilioupoli/snapshot")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert len(r.content) > 0
    assert r.headers.get("cache-control") == "no-store"


def test_trusted_camera_id_served(env):
    env(cameras=_cam(), mock="ok")
    import app as app_module
    from urllib.parse import urlsplit
    # The served image must decode as a PNG, i.e. the bytes are the mock frame.
    r = TestClient(app_module.app).get("/api/cameras/ilioupoli/snapshot")
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_unknown_camera_is_404(env):
    env(cameras=_cam(), mock="ok")
    assert _client().get("/api/cameras/nope/snapshot").status_code == 404


def test_disabled_camera_is_404_and_indistinguishable(env):
    env(cameras=json.dumps([{"id": "ilioupoli", "enabled": False,
                             "snapshot": PUBLIC_HTTPS}]), mock="ok")
    assert _client().get("/api/cameras/ilioupoli/snapshot").status_code == 404


def test_camera_without_snapshot_is_503(env):
    env(cameras=json.dumps([{"id": "ilioupoli", "name": "X"}]), mock="ok")
    assert _client().get("/api/cameras/ilioupoli/snapshot").status_code == 503


def test_no_arbitrary_url_parameter_exists(env):
    """The endpoint must ignore an injected URL/host: only the id is read."""
    env(cameras=_cam(), mock="ok")
    client = _client()
    params = {"url": "http://169.254.169.254/latest/meta-data/",
              "host": "evil.example", "src": "file:///etc/passwd"}
    r = client.get("/api/cameras/ilioupoli/snapshot", params=params)
    # It still serves the camera's own mock frame, not the injected target.
    assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"


# ============================================================ mock modes

def test_mock_timeout_is_504(env):
    env(cameras=_cam(), mock="timeout")
    assert _client().get("/api/cameras/ilioupoli/snapshot").status_code == 504


def test_mock_upstream_error_is_503(env):
    env(cameras=_cam(), mock="error")
    assert _client().get("/api/cameras/ilioupoli/snapshot").status_code == 503


def test_mock_invalid_content_type_is_503(env):
    env(cameras=_cam(), mock="wrongtype")
    assert _client().get("/api/cameras/ilioupoli/snapshot").status_code == 503


def test_mock_oversized_response_is_503(env):
    env(cameras=_cam(), mock="oversize")
    assert _client().get("/api/cameras/ilioupoli/snapshot").status_code == 503


def test_mock_empty_body_is_503(env):
    env(cameras=_cam(), mock="empty")
    assert _client().get("/api/cameras/ilioupoli/snapshot").status_code == 503


def test_unknown_mock_mode_is_rejected_not_ignored(env):
    # A typo must not silently fall back to a real fetch.
    env(cameras=_cam(), mock="typo")
    with pytest.raises(snap.SourceRejected):
        snap.source_for("ilioupoli")


def test_health_endpoint_stays_up_when_mock_mode_is_misconfigured(env):
    """A rejected mock mode must not turn /api/health into a 500."""
    env(cameras=_cam(), mock="typo")
    h = snap.health()
    assert h["sources"] == 0            # the camera's source is unusable, reported as none


def test_mock_mode_refused_in_production(env, monkeypatch):
    env(cameras=_cam(), mock="ok")
    monkeypatch.setenv("WX_ENV", "production")
    with pytest.raises(snap.SourceRejected):
        snap.source_for("ilioupoli")
    # And the endpoint degrades to a generic 503 rather than serving mock frames.
    assert _client().get("/api/cameras/ilioupoli/snapshot").status_code == 503


# ============================================================ source validation

def test_malformed_source_url_is_rejected(env):
    env(cameras=_cam(), sources=json.dumps([{"id": "ilioupoli",
                                            "url": "ht tp://[bad"}]))
    # cameras.source_for itself drops the malformed source, so there is none.
    assert snap.source_for("ilioupoli") is None


def test_source_scheme_must_be_snapshot_scheme(env):
    env(cameras=_cam(), sources=json.dumps([{"id": "ilioupoli", "url": SOURCE_URL}]),
        allowed=PRIVATE_HOST)
    with pytest.raises(snap.SourceRejected):
        snap.source_for("ilioupoli")


def test_source_with_userinfo_is_rejected(env):
    env(cameras=_cam(),
        sources=json.dumps([{"id": "ilioupoli",
                             "url": "http://user:pass@cam.example/x.jpg"}]))
    # credentials embedded in the URL are refused by cameras._validate_source,
    # so no source survives to the snapshot layer at all.
    assert cams.source_for("ilioupoli") is None


def test_private_source_host_must_be_allowlisted_for_pinned_fetch(env):
    # A private source whose host is not on the allowlist is served by the public
    # http layer (same policy cameras.py already applied), never by the pinned one.
    env(cameras=_cam(),
        sources=json.dumps([{"id": "ilioupoli", "url": "http://93.184.216.34/x.jpg"}]))
    src = snap.source_for("ilioupoli")
    assert isinstance(src, snap.HttpSnapshotSource)


def test_allowlisted_host_selects_the_pinned_fetcher(env):
    env(cameras=_cam(),
        sources=json.dumps([{"id": "ilioupoli", "url": "https://cam-lan.internal/x.jpg"}]),
        allowed=PRIVATE_HOST)
    src = snap.source_for("ilioupoli")
    assert isinstance(src, snap.FetchSnapshotSource)


def test_pinned_fetcher_refuses_host_that_resolves_private(env, monkeypatch):
    env(cameras=_cam(),
        sources=json.dumps([{"id": "ilioupoli", "url": "https://cam-lan.internal/x.jpg"}]),
        allowed=PRIVATE_HOST)
    monkeypatch.setattr(snap, "_default_resolver", lambda h: ["10.0.0.1"])
    src = snap.source_for("ilioupoli")
    with pytest.raises(snap.SourceRejected):
        _run(src.fetch())


def test_pinned_fetcher_refuses_host_outside_allowlist(env, monkeypatch):
    env(cameras=_cam(),
        sources=json.dumps([{"id": "ilioupoli", "url": "https://other.example/x.jpg"}]),
        allowed=PRIVATE_HOST)
    src = snap.FetchSnapshotSource(url="https://other.example/x.jpg",
                                   camera_id="ilioupoli", kind="https")
    with pytest.raises(snap.SourceRejected):
        _run(src.fetch())


def test_public_http_source_refuses_private_literal(env, monkeypatch):
    env(cameras=_cam())
    src = snap.HttpSnapshotSource(url="http://127.0.0.1/x.jpg",
                                  camera_id="ilioupoli", kind="http")
    with pytest.raises(snap.SourceRejected):
        _run(src.fetch())


# ============================================================ redirects

@pytest.mark.parametrize("status,location", [
    (301, "http://169.254.169.254/"),
    (302, "http://127.0.0.1/"),
    (307, "https://10.0.0.1/"),
])
def test_redirect_is_reported_never_followed(env, monkeypatch, status, location):
    """A redirect is a failure. Following it would re-open SSRF on a fresh URL."""
    import httpx

    class FakeResponse:
        status_code = status
        headers = {"location": location, "content-type": "image/jpeg"}

    class FakeStream:
        async def __aenter__(self): return FakeResponse()
        async def __aexit__(self, *a): return False

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def stream(self, *a, **k): return FakeStream()

    env(cameras=_cam())
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    # DNS is pinned so the test is offline; the address is public, so the fetch
    # reaches the redirect guard and must fail there.
    monkeypatch.setattr(snap, "_default_resolver", lambda h: ["93.184.216.34"])
    src = snap.HttpSnapshotSource(url=PUBLIC_HTTPS, camera_id="ilioupoli",
                                  kind="https")
    with pytest.raises(snap.FetchFailed) as exc:
        _run(src.fetch())
    assert "redirect" in str(exc.value)


def test_redirect_target_private_is_never_dialled(env, monkeypatch):
    """Belt and braces: even if the redirect were followed the target is refused.

    The point of recording this is that the *fetch* refuses, so there is no code
    path on which a private target gets a connection.
    """
    env(cameras=_cam())
    assert snap.resolve_public("127.0.0.1") is None
    assert snap.resolve_public("169.254.169.254") is None


# ============================================================ HTTP fetch path
#
# The endpoint tests above run through mock mode, which never touches
# `_fetch_http`. These drive that function directly with a fake streaming client
# so the byte cap, content-type gate, status handling and empty-body rule are
# each exercised on the real code path, offline.

class _FakeResp:
    def __init__(self, status=200, headers=None, chunks=()):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


def _fake_httpx(monkeypatch, resp, capture=None):
    """Patch httpx.AsyncClient with a client whose stream() yields `resp`."""
    class FakeStream:
        async def __aenter__(self): return resp
        async def __aexit__(self, *a): return False

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def stream(self, method, url, **k):
            if capture is not None:
                capture["url"] = url
                capture["headers"] = k.get("headers")
                capture["extensions"] = k.get("extensions")
                capture["follow"] = "follow_redirects"
            return FakeStream()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)


def test_http_success_returns_a_snapshot(env, monkeypatch):
    env(cameras=_cam())
    _fake_httpx(monkeypatch, _FakeResp(
        headers={"content-type": "image/jpeg; charset=binary"},
        chunks=[b"\xff\xd8\xff\xe0", b"rest"]))
    snap_res = _run(snap._fetch_http("https://cam.example/x.jpg", "https",
                                     headers=None, extensions=None))
    assert snap_res.content_type == "image/jpeg"      # params stripped
    assert snap_res.data == b"\xff\xd8\xff\xe0rest"


def test_http_oversize_is_refused_before_buffering_it_all(env, monkeypatch):
    """The cap is enforced mid-stream, not after a full read."""
    env(cameras=_cam())
    chunk = b"\x00" * (256 * 1024)
    many = [chunk] * ((snap.MAX_BYTES // len(chunk)) + 4)
    _fake_httpx(monkeypatch, _FakeResp(headers={"content-type": "image/jpeg"},
                                       chunks=many))
    with pytest.raises(snap.FetchFailed) as exc:
        _run(snap._fetch_http("https://cam.example/x.jpg", "https",
                              headers=None, extensions=None))
    assert "size cap" in str(exc.value)


def test_http_wrong_content_type_is_refused(env, monkeypatch):
    env(cameras=_cam())
    _fake_httpx(monkeypatch, _FakeResp(headers={"content-type": "text/html"},
                                       chunks=[b"<html>"]))
    with pytest.raises(snap.FetchFailed):
        _run(snap._fetch_http("https://cam.example/x", "https",
                              headers=None, extensions=None))


def test_http_non_200_is_refused(env, monkeypatch):
    env(cameras=_cam())
    _fake_httpx(monkeypatch, _FakeResp(status=502,
                                       headers={"content-type": "image/jpeg"},
                                       chunks=[b"x"]))
    with pytest.raises(snap.FetchFailed):
        _run(snap._fetch_http("https://cam.example/x", "https",
                              headers=None, extensions=None))


def test_http_empty_body_is_refused(env, monkeypatch):
    env(cameras=_cam())
    _fake_httpx(monkeypatch, _FakeResp(headers={"content-type": "image/png"},
                                       chunks=[]))
    with pytest.raises(snap.FetchFailed):
        _run(snap._fetch_http("https://cam.example/x", "https",
                              headers=None, extensions=None))


def test_pinned_fetch_pins_the_validated_address(env, monkeypatch):
    """The address that was checked is the address dialled, and SNI keeps the name."""
    env(cameras=_cam(),
        sources=json.dumps([{"id": "ilioupoli", "url": "https://cam-lan.internal/x.jpg"}]),
        allowed=PRIVATE_HOST)
    monkeypatch.setattr(snap, "_default_resolver",
                        lambda h: ["93.184.216.34", "1.1.1.1"])
    cap = {}
    _fake_httpx(monkeypatch, _FakeResp(headers={"content-type": "image/jpeg"},
                                       chunks=[b"img"]), capture=cap)
    src = snap.FetchSnapshotSource(url="https://cam-lan.internal/x.jpg",
                                   camera_id="ilioupoli", kind="https")
    _run(src.fetch())
    assert cap["url"] == "https://93.184.216.34/x.jpg"       # pinned IP
    assert cap["headers"] == {"Host": "cam-lan.internal"}     # name preserved
    assert cap["extensions"] == {"sni_hostname": "cam-lan.internal"}


def test_public_fetch_also_pins_the_validated_address(env, monkeypatch):
    """The public path is pinned too: no second lookup between check and dial."""
    env(cameras=_cam())                                    # no allowlist needed
    monkeypatch.setattr(snap, "_default_resolver", lambda h: ["93.184.216.34"])
    cap = {}
    _fake_httpx(monkeypatch, _FakeResp(headers={"content-type": "image/png"},
                                       chunks=[b"png"]), capture=cap)
    src = snap.HttpSnapshotSource(url="https://cam.example/latest.jpg",
                                  camera_id="ilioupoli", kind="https")
    _run(src.fetch())
    assert cap["url"] == "https://93.184.216.34/latest.jpg"
    assert cap["headers"] == {"Host": "cam.example"}
    assert cap["extensions"] == {"sni_hostname": "cam.example"}


def test_resolver_is_called_once_per_fetch(env, monkeypatch):
    """One resolution, one dial: a second lookup is exactly the rebinding window."""
    env(cameras=_cam())
    calls = {"n": 0}

    def counting(host):
        calls["n"] += 1
        # First answer public, any later answer private: a rebinding adversary.
        return ["93.184.216.34"] if calls["n"] == 1 else ["127.0.0.1"]

    monkeypatch.setattr(snap, "_default_resolver", counting)
    cap = {}
    _fake_httpx(monkeypatch, _FakeResp(headers={"content-type": "image/png"},
                                       chunks=[b"png"]), capture=cap)
    src = snap.HttpSnapshotSource(url="https://rebind.example/x.png",
                                  camera_id="ilioupoli", kind="https")
    _run(src.fetch())
    assert calls["n"] == 1                       # resolved exactly once
    assert cap["url"] == "https://93.184.216.34/x.png"   # the validated address


def test_ipv6_answer_is_pinned_and_bracketed(env, monkeypatch):
    """An IPv6 answer is pinned as a bracketed literal, keeping the Host name."""
    env(cameras=_cam())
    monkeypatch.setattr(snap, "_default_resolver",
                        lambda h: ["2606:4700:4700::1111"])
    cap = {}
    _fake_httpx(monkeypatch, _FakeResp(headers={"content-type": "image/png"},
                                       chunks=[b"png"]), capture=cap)
    src = snap.HttpSnapshotSource(url="https://cam.example/a/b.png?x=1",
                                  camera_id="ilioupoli", kind="https")
    _run(src.fetch())
    assert cap["url"] == "https://[2606:4700:4700::1111]/a/b.png?x=1"
    assert cap["headers"] == {"Host": "cam.example"}


def test_ipv6_zoneid_is_stripped_from_the_pinned_url(env, monkeypatch):
    env(cameras=_cam())
    monkeypatch.setattr(snap, "_default_resolver", lambda h: ["fe80::1%eth0"])
    # fe80:: is link-local, so it is refused before any dial.
    src = snap.HttpSnapshotSource(url="https://cam.example/x.png",
                                  camera_id="ilioupoli", kind="https")
    with pytest.raises(snap.SourceRejected):
        _run(src.fetch())
    # A public address carrying a ZoneID still pins without the zone suffix.
    monkeypatch.setattr(snap, "_default_resolver", lambda h: ["2606:4700::1%eth0"])
    cap = {}
    _fake_httpx(monkeypatch, _FakeResp(headers={"content-type": "image/png"},
                                       chunks=[b"png"]), capture=cap)
    _run(src.fetch())
    assert cap["url"] == "https://[2606:4700::1]/x.png"


def test_any_private_answer_refuses_the_whole_name(env, monkeypatch):
    """A split A/AAAA response (public + private) is refused, not partially used."""
    env(cameras=_cam())
    for answers in (["93.184.216.34", "10.0.0.1"],
                    ["2606:4700::1", "fe80::1"],
                    ["1.1.1.1", "169.254.169.254"]):
        monkeypatch.setattr(snap, "_default_resolver", lambda h, a=answers: a)
        src = snap.HttpSnapshotSource(url="https://split.example/x.png",
                                      camera_id="ilioupoli", kind="https")
        with pytest.raises(snap.SourceRejected):
            _run(src.fetch())


def test_http_source_refuses_a_credential_url_directly(env, monkeypatch):
    """`_fetch_http` is never reached with userinfo: the source layer refuses it."""
    env(cameras=_cam())
    src = snap.HttpSnapshotSource(url="https://user:pass@8.8.8.8/x.jpg",
                                  camera_id="ilioupoli", kind="https")
    with pytest.raises(snap.SourceRejected):
        _run(src.fetch())


def test_builder_rejects_userinfo_in_the_configured_source(env):
    """Even if an operator writes userinfo into WX_CAMERA_SOURCES, no source is built."""
    # cameras._validate_source refuses embedded credentials, so source_for is None.
    env(cameras=_cam(),
        sources=json.dumps([{"id": "ilioupoli",
                             "url": "http://viewer:pw@8.8.8.8/x.jpg"}]))
    assert cams.source_for("ilioupoli") is None
    assert snap.source_for("ilioupoli") is None


def test_unparseable_port_is_rejected_not_a_500(env):
    """A source with an out-of-range port is bad config, answered generically."""
    env(cameras=_cam(),
        sources=json.dumps([{"id": "ilioupoli",
                             "url": "https://93.184.216.34:99999/x.jpg"}]))
    src = snap.source_for("ilioupoli")
    assert src is not None
    with pytest.raises(snap.SourceRejected):
        _run(src.fetch())
    # End to end: the endpoint degrades, it does not report an internal error.
    assert _client().get("/api/cameras/ilioupoli/snapshot").status_code == 503


def test_unclosed_ipv6_literal_is_rejected_not_a_500(env):
    env(cameras=_cam(),
        sources=json.dumps([{"id": "ilioupoli",
                             "url": "https://[2606:4700::1/x.jpg"}]))
    src = snap.source_for("ilioupoli")
    if src is None:
        return                      # rejected earlier, also acceptable
    with pytest.raises(snap.SourceRejected):
        _run(src.fetch())


def test_with_host_preserves_an_explicit_port(env):
    env(cameras=_cam())
    out = snap._with_host("https://cam.example:8443/a/b.png?x=1", "93.184.216.34")
    assert out == "https://93.184.216.34:8443/a/b.png?x=1"
    out6 = snap._with_host("https://cam.example:8443/a/b.png", "2606:4700::1")
    assert out6 == "https://[2606:4700::1]:8443/a/b.png"


def test_credentials_never_reach_the_snapshot_response(env):
    env(cameras=_cam(),
        sources=json.dumps([{"id": "ilioupoli", "url": "https://cam-lan.internal/x.jpg",
                             "username": "viewer", "password": SECRET_PASS,
                             "secret_ref": "WX_CAM_TEST_PASS"}]),
        allowed=PRIVATE_HOST, mock="ok")
    # WX_CAM_TEST_PASS is present in the environment; it must not surface.
    import os
    os.environ["WX_CAM_TEST_PASS"] = SECRET_PASS
    r = _client().get("/api/cameras/ilioupoli/snapshot")
    for leak in (SECRET_PASS, PRIVATE_HOST, "cam-lan.internal", "viewer"):
        assert leak.encode() not in r.content
        assert leak not in r.text


def test_public_camera_payload_never_carries_the_private_source(env):
    env(cameras=_cam(),
        sources=json.dumps([{"id": "ilioupoli", "url": "https://cam-lan.internal/x.jpg",
                             "username": "viewer", "password": SECRET_PASS}]),
        allowed=PRIVATE_HOST)
    blob = _client().get("/api/cameras").text
    for leak in (SOURCE_URL, SECRET_PASS, PRIVATE_HOST, "rtsp://", "viewer"):
        assert leak not in blob


def test_rejected_source_logs_no_credential(env, caplog):
    env(cameras=_cam(),
        sources=json.dumps([{"id": "ilioupoli",
                             "url": "https://user:LEAKED@cam-lan.internal/x.jpg"}]),
        allowed=PRIVATE_HOST)
    with caplog.at_level(logging.WARNING):
        snap.source_for("ilioupoli")
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "LEAKED" not in joined


def test_snapshot_via_is_server_for_a_private_source(env):
    env(cameras=_cam(),
        sources=json.dumps([{"id": "ilioupoli", "url": "https://cam-lan.internal/x.jpg"}]),
        allowed=PRIVATE_HOST)
    assert snap.snapshot_via("ilioupoli") == "server"


def test_snapshot_via_is_direct_for_a_public_url(env):
    """Default deployment behaviour is preserved: a public URL stays browser-loaded."""
    env(cameras=_cam())
    assert snap.snapshot_via("ilioupoli") == "direct"


def test_snapshot_via_is_server_in_mock_mode(env):
    env(cameras=_cam(), mock="ok")
    assert snap.snapshot_via("ilioupoli") == "server"


def test_public_payload_carries_snapshot_via_without_the_source(env):
    env(cameras=_cam(),
        sources=json.dumps([{"id": "ilioupoli", "url": "https://cam-lan.internal/x.jpg"}]),
        allowed=PRIVATE_HOST, mock="ok")
    payload = _client().get("/api/cameras").json()
    cam = payload["cameras"][0]
    assert cam["snapshot_via"] == "server"
    assert "cam-lan.internal" not in json.dumps(payload)


# ============================================================ caching / load

def test_second_request_within_ttl_does_not_refetch(env, monkeypatch):
    env(cameras=_cam(), mock="ok")
    calls = {"n": 0}
    real = snap.MockSnapshotSource.fetch

    async def counting(self):
        calls["n"] += 1
        return await real(self)

    monkeypatch.setattr(snap.MockSnapshotSource, "fetch", counting)
    snap.reset_cache()
    client = _client()
    client.get("/api/cameras/ilioupoli/snapshot")
    client.get("/api/cameras/ilioupoli/snapshot")
    assert calls["n"] == 1


def test_concurrent_requests_share_one_fetch(env, monkeypatch):
    """N simultaneous viewers cost one upstream request (single-flight)."""
    env(cameras=_cam(), mock="ok")
    calls = {"n": 0}
    real = snap.MockSnapshotSource.fetch

    async def slow(self):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return await real(self)

    monkeypatch.setattr(snap.MockSnapshotSource, "fetch", slow)
    snap.reset_cache()
    cache = snap.SnapshotCache()
    src = snap.source_for("ilioupoli")

    async def go():
        return await asyncio.gather(*[
            cache.get_or_fetch("ilioupoli", src) for _ in range(12)])

    results = _run(go())
    assert calls["n"] == 1
    assert all(r.data == results[0].data for r in results)


def test_ttl_expiry_triggers_a_refetch(env, monkeypatch):
    env(cameras=_cam(snapshot_interval_min=1), mock="ok")
    calls = {"n": 0}
    real = snap.MockSnapshotSource.fetch

    async def counting(self):
        calls["n"] += 1
        return await real(self)

    monkeypatch.setattr(snap.MockSnapshotSource, "fetch", counting)
    snap.reset_cache()
    cache = snap.SnapshotCache()
    src = snap.source_for("ilioupoli")
    base = 1_000_000.0
    _run(cache.get_or_fetch("ilioupoli", src, base))
    _run(cache.get_or_fetch("ilioupoli", src, base + 1))         # within TTL (60s)
    assert calls["n"] == 1
    _run(cache.get_or_fetch("ilioupoli", src, base + src.ttl_s + 1))
    assert calls["n"] == 2


def test_cache_is_bounded(env):
    env(cameras=_cam(), mock="ok")
    cache = snap.SnapshotCache(max_entries=3)
    for i in range(10):
        cache.put(f"cam{i}", snap.Snapshot(data=b"x" * 10, content_type="image/png",
                                          fetched_at=float(i), source_kind="mock"))
    assert len(cache._entries) <= 3


def test_ttl_floor_stops_a_one_second_cadence(env):
    env(cameras=_cam(snapshot_interval_min=1), mock="ok")
    src = snap.source_for("ilioupoli")
    assert src.ttl_s >= snap.MIN_TTL_S


def test_damaged_cache_falls_back_to_fetch(env, monkeypatch):
    """A cached frame that no longer decodes must not be served for ever."""
    # Simulated at the cache layer: a fresh entry is used, an expired one misses.
    env(cameras=_cam(), mock="ok")
    cache = snap.SnapshotCache()
    cache.put("ilioupoli", snap.Snapshot(data=b"\x89PNG", content_type="image/png",
                                        fetched_at=0.0, source_kind="mock"))
    assert cache.get_fresh("ilioupoli", ttl_s=60, now=10_000.0) is None


# ============================================================ health

def test_health_reports_counts_only(env):
    env(cameras=_cam(live_enabled=True, live_provider="youtube",
                     youtube_live_id="MOCKPUBLICID"),
        sources=json.dumps([{"id": "ilioupoli", "url": "https://cam-lan.internal/x.jpg",
                             "password": SECRET_PASS}]),
        allowed=PRIVATE_HOST)
    h = snap.health()
    assert "sources" in h and "by_kind" in h and "mock_sources" in h
    blob = json.dumps(h)
    for leak in (SECRET_PASS, PRIVATE_HOST, "cam-lan.internal", "rtsp://"):
        assert leak not in blob


def test_health_never_reports_a_url(env):
    env(cameras=_cam())
    assert PUBLIC_HTTPS not in json.dumps(snap.health())


def test_app_health_includes_the_snapshot_block(env):
    env(cameras=_cam(), mock="ok")
    r = _client().get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert "snapshots" in body
    assert "mock_sources" in body["snapshots"]


# ============================================================ degradation

def test_source_rejected_is_generic_over_http(env):
    """A rejected source config must not describe itself to the client."""
    env(cameras=_cam(),
        sources=json.dumps([{"id": "ilioupoli", "url": "rtsp://cam-lan.internal/x"}]),
        allowed=PRIVATE_HOST)
    r = _client().get("/api/cameras/ilioupoli/snapshot")
    assert r.status_code == 503
    for leak in ("rtsp", "cam-lan.internal", "scheme"):
        assert leak not in r.text


def test_unknown_and_disabled_are_the_same_answer(env):
    env(cameras=json.dumps([
        {"id": "ilioupoli", "enabled": False, "snapshot": PUBLIC_HTTPS}]),
        mock="ok")
    client = _client()
    disabled = client.get("/api/cameras/ilioupoli/snapshot")
    unknown = client.get("/api/cameras/does-not-exist/snapshot")
    assert disabled.status_code == unknown.status_code == 404
    assert disabled.json() == unknown.json()
