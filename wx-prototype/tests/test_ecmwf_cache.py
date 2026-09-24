"""Regression tests for the ECMWF disk cache in `wx.ecmwf_point`.

The bug these pin down: the function re-fetched its `.index` and its five
per-parameter `.grib2` Range slices on every call, and nothing touched
`cachestore`. A warm, identical forecast therefore cost six ECMWF GETs, which
both doubled request latency and produced the 429s that silently drop ECMWF from
the model comparison.

The identity of what must not change is as important as the caching itself:
the point is selected after decode, so the cache key is the run/step (not the
coordinate) and one download is shared by every user asking for that run.

No network is used here. A fake client records every GET and `xr.open_dataset`
is replaced by a decoder over the exact bytes that were handed to disk, so a
cache hit is proven by the absence of a GET rather than by a stub that would
return the same value either way.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wx  # noqa: E402

PARAMS = ("2t", "10u", "10v", "msl", "tp")
VALUES = {"2t": 300.0, "10u": 4.0, "10v": -2.0, "msl": 101300.0, "tp": 0.002}


def _index() -> bytes:
    """A minimal `.index`: one sfc row per parameter plus rows that must be skipped."""
    lines = [f'{{"param":"{p}","levtype":"sfc","_offset":{100 + i * 10},"_length":{8 + i}}}'
             for i, p in enumerate(PARAMS)]
    # A non-surface level and an unused parameter: both are filtered out by the
    # function and must not produce a fetch.
    lines.append('{"param":"z","levtype":"pl","_offset":900,"_length":40}')
    lines.append('{"param":"ssrd","levtype":"sfc","_offset":950,"_length":40}')
    return ("\n".join(lines) + "\n").encode()


def _grib_for(param: str) -> bytes:
    return b"GRIB" + param.encode() + b"." * 12


class _Resp:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Records GETs; serves the index and maps each Range header to one param."""

    def __init__(self, fail_grib: bool = False, fail_index: bool = False):
        self.calls: list[str] = []
        self.ranges: list[str] = []
        self.fail_grib = fail_grib
        self.fail_index = fail_index
        self._by_range = {}
        for i, p in enumerate(PARAMS):
            start = 100 + i * 10
            length = 8 + i
            self._by_range[f"bytes={start}-{start + length - 1}"] = _grib_for(p)

    async def get(self, url, **kwargs):
        self.calls.append(url)
        if url.endswith(".index"):
            if self.fail_index:
                raise RuntimeError("index unavailable")
            return _Resp(_index())
        if self.fail_grib:
            raise RuntimeError("grib unavailable")
        rng = kwargs["headers"]["Range"]
        self.ranges.append(rng)
        return _Resp(self._by_range[rng])


class _FakeDS:
    def __init__(self, value):
        self._value = value
        self.data_vars = ["x"]

    def __getitem__(self, _k):
        return self

    def sel(self, **_k):
        return self

    @property
    def values(self):
        return self._value

    def close(self):
        pass


@pytest.fixture()
def cache_dir(monkeypatch):
    d = tempfile.mkdtemp(prefix="wx-ecmwf-test-")
    monkeypatch.setenv("WX_CACHE_DIR", os.path.join(d, "cache"))
    return d


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """The 0.4 s politeness sleep is real but pointless in a unit test."""
    async def _nosleep(*_a, **_k):
        return None

    monkeypatch.setattr(wx.asyncio, "sleep", _nosleep)


@pytest.fixture(autouse=True)
def fake_decode(monkeypatch):
    """Decode the bytes on disk, keyed by the shortName the function asks for."""
    def _open(path, **kwargs):
        want = kwargs["backend_kwargs"]["filter_by_keys"]["shortName"]
        raw = Path(path).read_bytes()
        assert raw == _grib_for(want), "decoder was handed the wrong blob"
        return _FakeDS(VALUES[want])

    monkeypatch.setattr(wx.xr, "open_dataset", _open)


def _run(client, step: int = 24, hh: str = "00"):
    return asyncio.run(wx.ecmwf_point(client, 37.98, 23.73, step=step, hh=hh))


# ============================================================ cold / warm

def test_cold_request_fetches_index_and_one_slice_per_parameter(cache_dir):
    client = FakeClient()
    out = _run(client)

    assert out == pytest.approx(VALUES)
    # One index plus exactly one Range GET per cached parameter.
    assert sum(u.endswith(".index") for u in client.calls) == 1
    assert sum(u.endswith(".grib2") for u in client.calls) == len(PARAMS)
    assert len(client.calls) == 1 + len(PARAMS)


def test_repeat_request_is_served_from_cache_with_zero_upstream_gets(cache_dir):
    first = FakeClient()
    _run(first)
    assert len(first.calls) == 1 + len(PARAMS)

    second = FakeClient()
    out = _run(second)
    assert out == pytest.approx(VALUES)
    assert second.calls == [], "a warm request must not touch ECMWF"


def test_cache_survives_a_restart(cache_dir):
    """A restart is a process that shares nothing but the cache directory.

    This is modelled by a fresh client and no module-level memoisation, which is
    exactly the state a new worker starts in; the on-disk entry is the only thing
    carried over.
    """
    _run(FakeClient())
    before = sorted(os.listdir(os.path.join(cache_dir, "cache")))
    assert before, "nothing was written to the cache directory"

    restarted = FakeClient()
    out = _run(restarted)
    assert out == pytest.approx(VALUES)
    assert restarted.calls == [], "cache did not survive a restart"


# ============================================================ key isolation

def test_a_different_step_does_not_reuse_the_other_run(cache_dir):
    _run(FakeClient(), step=24)
    other = FakeClient()
    _run(other, step=48)
    assert len(other.calls) == 1 + len(PARAMS), "step 48 wrongly reused step 24"


def test_a_different_cycle_does_not_reuse_the_other_run(cache_dir):
    _run(FakeClient(), hh="00")
    other = FakeClient()
    _run(other, hh="12")
    assert len(other.calls) == 1 + len(PARAMS), "cycle 12 wrongly reused cycle 00"


def test_each_parameter_keeps_its_own_entry(cache_dir):
    """The slices share one URL, so isolation can only come from the Range header.

    A key that ignored the parameter would make the first slice win and the rest
    silently decode as the wrong field.
    """
    client = FakeClient()
    _run(client)
    assert len(client.ranges) == len(PARAMS)
    assert sorted(client.ranges) == sorted(set(client.ranges)), "a parameter reused a slice"


# ============================================================ expiry / damage

def test_an_expired_entry_triggers_a_refetch(cache_dir):
    _run(FakeClient())
    folder = os.path.join(cache_dir, "cache")
    old = time.time() - wx.ECMWF_TTL_S - 60
    for name in os.listdir(folder):
        os.utime(os.path.join(folder, name), (old, old))

    again = FakeClient()
    _run(again)
    assert len(again.calls) == 1 + len(PARAMS), "an expired entry was served anyway"


def test_a_truncated_entry_is_discarded_and_refetched(cache_dir):
    """cachestore drops entries below `MIN_REAL_BYTES`; the caller must refetch."""
    _run(FakeClient())
    folder = os.path.join(cache_dir, "cache")
    for name in os.listdir(folder):
        p = os.path.join(folder, name)
        with open(p, "wb") as f:
            f.write(b"x")  # damaged

    again = FakeClient()
    out = _run(again)
    assert out == pytest.approx(VALUES)
    assert len(again.calls) == 1 + len(PARAMS), "a damaged entry was served anyway"


# ============================================================ degradation

def test_a_grib_failure_raises_so_the_caller_can_degrade(cache_dir):
    """Failure must surface as an exception, never as partial cached rubbish."""
    client = FakeClient(fail_grib=True)
    with pytest.raises(RuntimeError):
        _run(client)


def test_an_index_failure_raises_and_poisons_nothing(cache_dir):
    with pytest.raises(RuntimeError):
        _run(FakeClient(fail_index=True))

    # A later, healthy call must succeed: a failed fetch wrote no half entry.
    ok = FakeClient()
    assert _run(ok) == pytest.approx(VALUES)
    assert len(ok.calls) == 1 + len(PARAMS)


def test_a_failed_fetch_leaves_no_cache_entry_behind(cache_dir):
    with pytest.raises(RuntimeError):
        _run(FakeClient(fail_grib=True))
    folder = os.path.join(cache_dir, "cache")
    left = os.listdir(folder) if os.path.isdir(folder) else []
    # The index is cached before the gribs are attempted; that is intentional and
    # harmless, but no *parameter* slice may exist.
    assert not any(n.startswith("ec-") for n in left)
