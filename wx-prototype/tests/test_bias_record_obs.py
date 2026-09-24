"""`record_obs` must reject implausible temperatures without crashing.

The bug this pins down: the upper bound of the plausibility check was spelled
`IMPLUSIBLE_C` (no `A`) and never defined, so every Ecowitt push that carried a
temperature raised `NameError` before the row was written. The lower bound used
the correct name, which is why only the upper half of the comparison failed.

A plain stored-value round trip would have caught it, so that is exactly what
this does: no mocks, a real SQLite file, the real function.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bias    # noqa: E402


@pytest.fixture()
def store(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="wx-obs-") as d:
        monkeypatch.setenv("WX_CACHE_DIR", os.path.join(d, "cache"))
        monkeypatch.delenv("WX_DB", raising=False)
        bias.init_db()
        yield


def test_a_valid_temperature_is_stored_without_a_nameerror(store):
    """The regression: this call raised NameError on the previous code."""
    ok = bias.record_obs("st-1", "2026-01-01T00:00:00Z", 18.5, 55.0, 10.0, 12.0, 1013.0, 0.0)
    assert ok is True

    rows = bias.recent_obs("st-1", limit=5)
    assert len(rows) == 1
    assert rows[0]["temp_c"] == 18.5
    assert rows[0]["humidity"] == 55.0


def test_the_upper_bound_still_rejects_implausible_values(store):
    """Both ends of the range must work, not just the lower one."""
    assert bias.record_obs("st-1", "2026-01-01T00:00:00Z", 100.0, None, None, None, None, None) is False
    assert bias.record_obs("st-1", "2026-01-01T01:00:00Z", -100.0, None, None, None, None, None) is False
    assert bias.recent_obs("st-1", limit=5) == []


def test_the_boundary_values_are_accepted(store):
    low, high = bias.IMPLAUSIBLE_C
    assert bias.record_obs("st-1", "2026-01-01T00:00:00Z", low, None, None, None, None, None) is True
    assert bias.record_obs("st-1", "2026-01-01T01:00:00Z", high, None, None, None, None, None) is True
    assert len(bias.recent_obs("st-1", limit=5)) == 2


def test_a_missing_temperature_is_allowed(store):
    """A rain-only push has no temperature and must not be rejected."""
    assert bias.record_obs("st-1", "2026-01-01T00:00:00Z", None, None, None, None, None, 3.5) is True
    assert bias.recent_obs("st-1", limit=5)[0]["temp_c"] is None
