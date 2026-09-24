"""Tests for the production-hardening layer added in PHASE A.

Grouped by the guarantee each one protects, and each test states the failure it
is guarding against. The expensive ones (cache fill, single-flight) use a
temporary cache directory so they never touch a real one.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analytics
import cachestore
import config
import entitlements as ent
import promo
import ratelimit


# ============================================================ config / coordinate

def test_coord_bounds_reject_the_values_that_used_to_download_the_planet():
    """`lat=999` was accepted and fetched a whole-global GRIB field.

    NOMADS clamps an out-of-range sub-region to the full grid, so a bogus
    coordinate was not an error there — it was a 419 MB cache file. The bounds
    have to reject it before any request is made.
    """
    assert config.coord_error(37.98, 23.72) is None
    assert config.coord_error(999, 23.72) is not None
    assert config.coord_error(37.98, 999) is not None
    assert config.coord_error(float("nan"), 23.72) is not None
    assert config.coord_error(float("inf"), 23.72) is not None
    assert config.coord_error(90.0, 180.0) is None
    assert config.coord_error(-90.0, -180.0) is None


def test_longitude_outside_the_conventional_range_is_still_refused():
    """The per-point path wraps lon, but the API contract does not offer that."""
    assert config.coord_error(37.0, 180.1) is not None
    assert config.coord_error(37.0, -180.1) is not None


def test_hours_bounds_leave_both_tiers_untouched():
    """The request bound is a sanity limit, not a tier change."""
    assert config.HOURS_MAX == ent.PRO_HOURS == 240
    assert ent.FREE_HOURS == 72


def test_env_file_values_actually_reach_the_readers(monkeypatch):
    """The ordering bug: `.env` was loaded after the settings were read.

    `WX_SECRET` in `.env` reached `os.environ` but the module kept the public
    development default, so a file-configured deploy minted forgeable tokens.
    """
    monkeypatch.setenv("WX_SECRET", "a-real-environment-secret")
    assert ent.secret() == "a-real-environment-secret"


def test_production_refuses_to_sign_with_the_development_default(monkeypatch):
    """The dev default is in the source; using it in prod makes tokens forgeable."""
    monkeypatch.delenv("WX_SECRET", raising=False)
    monkeypatch.setenv("WX_ENV", "production")
    with pytest.raises(config.ConfigError):
        config.signing_secret()


def test_development_falls_back_so_a_laptop_and_pytest_still_run(monkeypatch):
    monkeypatch.delenv("WX_SECRET", raising=False)
    monkeypatch.setenv("WX_ENV", "dev")
    assert config.signing_secret() == config.DEV_SECRET


def test_validate_runtime_names_missing_stripe_configuration(monkeypatch):
    monkeypatch.setenv("WX_SECRET", "x")
    monkeypatch.setenv("WX_STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.delenv("WX_STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("WX_PUBLIC_BASE_URL", raising=False)
    problems = config.validate_runtime()
    assert any("WX_PUBLIC_BASE_URL" in p for p in problems)
    assert any("WX_STRIPE_WEBHOOK_SECRET" in p for p in problems)


# ============================================================ cache

@pytest.fixture()
def tmp_cache(monkeypatch):
    d = tempfile.mkdtemp(prefix="wx-cache-test-")
    monkeypatch.setenv("WX_CACHE_DIR", d)
    monkeypatch.delenv("WX_CACHE_MAX_MB", raising=False)
    yield d


def test_cache_round_trips_and_expires(tmp_cache):
    cachestore.put("k1", b"payload-bytes")
    assert cachestore.get("k1", ttl=60) == b"payload-bytes"
    assert cachestore.get("k1", ttl=0) is None


def test_a_truncated_entry_is_discarded_rather_than_served(tmp_cache):
    """A short file is a killed write, not data.

    Serving it would surface as `xr.open_dataset` failing, which reads as "the
    model is down" and sends the operator looking in the wrong place.
    """
    cachestore.put("k2", b"a-real-payload-long-enough")
    path = cachestore._path("k2")
    with open(path, "wb") as f:
        f.write(b"x")
    assert cachestore.get("k2", ttl=60) is None
    assert not os.path.exists(path), "the damaged entry is removed, not kept"


def test_missing_file_is_a_miss_not_an_error(tmp_cache):
    assert cachestore.get("never-written", ttl=60) is None


def test_eviction_removes_oldest_first_and_respects_the_cap(tmp_cache, monkeypatch):
    """Without eviction the cache grew until the disk filled."""
    for i in range(10):
        cachestore.put(f"e{i}", b"y" * 4096)
        # Force distinct mtimes so oldest-first is deterministic.
        os.utime(cachestore._path(f"e{i}"), (1000 + i, 1000 + i))
    before = cachestore.total_bytes()
    assert before > 30000
    result = cachestore.evict(target=8000)
    assert result["evicted"] > 0
    assert cachestore.total_bytes() <= 8000
    # The newest entries survive; the oldest went first.
    assert os.path.exists(cachestore._path("e9"))
    assert not os.path.exists(cachestore._path("e0"))


def test_eviction_is_a_no_op_below_the_cap(tmp_cache):
    cachestore.put("small", b"z" * 100)
    assert cachestore.evict(target=10_000_000)["evicted"] == 0


def test_a_disabled_cap_keeps_everything(tmp_cache):
    cachestore.put("keep", b"z" * 100)
    assert cachestore.evict(target=0)["evicted"] == 0


def test_put_is_atomic_so_a_reader_never_sees_a_partial_file(tmp_cache):
    """The write goes to a temp file and is renamed into place.

    Checked by asserting no stray temp files are left and the final file is the
    full payload — `os.replace` is the operation that makes it atomic.
    """
    cachestore.put("atomic", b"complete-payload")
    leftovers = [f for f in os.listdir(tmp_cache) if f.startswith(".tmp-")]
    assert leftovers == []
    assert cachestore.get("atomic", ttl=60) == b"complete-payload"


def test_the_configured_cap_converts_to_bytes(monkeypatch):
    monkeypatch.setenv("WX_CACHE_MAX_MB", "64")
    assert config.cache_max_bytes() == 64 * 1024 * 1024
    monkeypatch.setenv("WX_CACHE_MAX_MB", "0")
    assert config.cache_max_bytes() == 0
    monkeypatch.setenv("WX_CACHE_MAX_MB", "not-a-number")
    assert config.cache_max_bytes() == config.DEFAULT_CACHE_MAX_MB * 1024 * 1024


def test_stats_reports_size_and_cap(tmp_cache, monkeypatch):
    monkeypatch.setenv("WX_CACHE_MAX_MB", "1")
    cachestore.put("s1", b"x" * 500)
    st = cachestore.stats()
    assert st["files"] == 1 and st["bytes"] >= 500
    assert st["limit_bytes"] == 1024 * 1024


# ============================================================ rate limiting

def test_token_bucket_allows_the_burst_then_refuses():
    lim = ratelimit.Limiter()
    limit = ratelimit.Limits(rate=1.0, burst=3)
    for _ in range(3):
        assert lim.allow("k", limit)[0]
    allowed, retry = lim.allow("k", limit)
    assert not allowed and retry > 0


def test_refills_over_time_rather_than_per_window():
    """A fixed window lets a client double-spend at a minute boundary."""
    lim = ratelimit.Limiter()
    limit = ratelimit.Limits(rate=1.0, burst=2)
    now = 1000.0
    assert lim.allow("k", limit, now=now)[0]
    assert lim.allow("k", limit, now=now)[0]
    assert not lim.allow("k", limit, now=now)[0]
    # One token back after one second, not a whole new allowance.
    assert lim.allow("k", limit, now=now + 1.0)[0]
    assert not lim.allow("k", limit, now=now + 1.0)[0]


def test_buckets_are_independent_per_client():
    lim = ratelimit.Limiter()
    limit = ratelimit.Limits(rate=1.0, burst=1)
    assert lim.allow("a", limit)[0]
    assert not lim.allow("a", limit)[0]
    assert lim.allow("b", limit)[0]


def test_stale_buckets_are_swept_so_the_map_does_not_grow_forever():
    lim = ratelimit.Limiter()
    limit = ratelimit.Limits(rate=1.0, burst=1)
    lim.allow("old", limit, now=0.0)
    assert len(lim._buckets) == 1
    # A later call triggers the sweep, and the untouched bucket is dropped.
    lim.allow("new", limit, now=10_000.0)
    assert all(k[0] != "old" for k in lim._buckets)


def test_proxy_headers_are_only_trusted_when_configured(monkeypatch):
    """A directly-exposed server that trusts XFF is trivially evaded."""
    class Req:
        headers = {"x-forwarded-for": "1.2.3.4"}
        query_params: dict = {}
        client = type("C", (), {"host": "9.9.9.9"})()
    assert ratelimit.client_key(Req(), trust_proxy=True) == "ip:1.2.3.4"
    assert ratelimit.client_key(Req(), trust_proxy=False) == "ip:9.9.9.9"


def test_an_invalid_forwarded_address_falls_back_to_the_socket_peer():
    class Req:
        headers = {"x-forwarded-for": "not-an-ip"}
        query_params: dict = {}
        client = type("C", (), {"host": "9.9.9.9"})()
    assert ratelimit.client_key(Req(), trust_proxy=True) == "ip:9.9.9.9"


def test_a_token_is_hashed_before_it_becomes_a_bucket_key():
    """The limiter must not hold a credential in memory."""
    class Req:
        headers = {"x-wx-token": "super-secret-token-value"}
        query_params: dict = {}
        client = None
    key = ratelimit.client_key(Req(), trust_proxy=False)
    assert key.startswith("tok:")
    assert "super-secret" not in key


# ============================================================ promo codes

@pytest.fixture()
def promo_db(monkeypatch):
    d = tempfile.mkdtemp(prefix="wx-promo-test-")
    monkeypatch.setenv("WX_DB", os.path.join(d, "test.db"))
    promo.init_db()
    analytics.init_db()
    promo._SALT = None
    analytics._SALT = None
    yield d


def test_a_code_grants_the_advertised_duration(promo_db):
    promo.create_code("FRIEND5", 5, max_redemptions=10)
    r = promo.redeem("FRIEND5", "device-a")
    assert r["days"] == 5
    until = promo.active_until("device-a")
    assert until is not None
    # Roughly five days out, not five seconds and not a year.
    assert 4.9 * 86400 < until - time.time() < 5.1 * 86400


def test_the_same_device_cannot_redeem_the_same_code_twice(promo_db):
    promo.create_code("FRIEND5", 5)
    promo.redeem("FRIEND5", "device-a")
    with pytest.raises(promo.RedemptionError) as e:
        promo.redeem("FRIEND5", "device-a")
    assert e.value.code == "already_used"


def test_a_different_device_can_use_the_code(promo_db):
    promo.create_code("FRIEND5", 5, max_redemptions=2)
    promo.redeem("FRIEND5", "device-a")
    promo.redeem("FRIEND5", "device-b")
    until = promo.active_until("device-b")
    assert until is not None


def test_max_redemptions_is_enforced_for_later_devices(promo_db):
    promo.create_code("ONEUSE", 1, max_redemptions=1)
    promo.redeem("ONEUSE", "device-a")
    with pytest.raises(promo.RedemptionError) as e:
        promo.redeem("ONEUSE", "device-b")
    assert e.value.code == "exhausted"


def test_an_inactive_code_is_refused(promo_db):
    promo.create_code("DEAD", 5)
    promo.set_active("DEAD", False)
    with pytest.raises(promo.RedemptionError) as e:
        promo.redeem("DEAD", "device-a")
    assert e.value.code == "inactive"


def test_revoking_a_code_does_not_retroactively_grant_or_deny(promo_db):
    """Revocation stops new redemptions; existing windows are left in place."""
    promo.create_code("FRIEND5", 5)
    promo.redeem("FRIEND5", "device-a")
    promo.set_active("FRIEND5", False)
    # The window already granted stays: it is a separate record.
    assert promo.active_until("device-a") is not None
    with pytest.raises(promo.RedemptionError):
        promo.redeem("FRIEND5", "device-b")


def test_an_expired_code_is_refused(promo_db):
    promo.create_code("OLD", 5, expires_at="2020-01-01")
    with pytest.raises(promo.RedemptionError) as e:
        promo.redeem("OLD", "device-a")
    assert e.value.code == "expired"


def test_a_not_yet_started_code_is_refused(promo_db):
    promo.create_code("SOON", 5, starts_at="2090-01-01")
    with pytest.raises(promo.RedemptionError) as e:
        promo.redeem("SOON", "device-a")
    assert e.value.code == "not_started"


def test_an_unknown_code_is_refused(promo_db):
    with pytest.raises(promo.RedemptionError) as e:
        promo.redeem("NOSUCHCODE", "device-a")
    assert e.value.code == "unknown"


def test_a_malformed_code_is_refused_before_any_lookup(promo_db):
    with pytest.raises(promo.RedemptionError) as e:
        promo.redeem("!!", "device-a")
    assert e.value.code == "malformed"


def test_a_personal_code_only_works_for_its_named_subject(promo_db):
    """The JACKFRIEND5 case: a gift for one specific person."""
    promo.create_code("JACKFRIEND5", 5, restricted_to="device-jack")
    with pytest.raises(promo.RedemptionError) as e:
        promo.redeem("JACKFRIEND5", "someone-else")
    assert e.value.code == "not_you"
    r = promo.redeem("JACKFRIEND5", "device-jack")
    assert r["days"] == 5


def test_a_personal_code_cannot_be_used_without_an_identity(promo_db):
    promo.create_code("JACKFRIEND5", 5, restricted_to="device-jack")
    with pytest.raises(promo.RedemptionError) as e:
        promo.redeem("JACKFRIEND5", None)
    assert e.value.code == "identify"


def test_hyphens_and_spaces_are_ignored_so_a_screenshot_still_works(promo_db):
    promo.create_code("FRIEND5", 5)
    assert promo.redeem(" friend-5 ", "device-a")["code"] == "FRIEND5"


def test_an_expired_window_no_longer_counts_as_active(promo_db):
    """`active_until` is a live comparison, not a stored flag."""
    promo.create_code("SHORT", 1)
    promo.redeem("SHORT", "device-a")
    assert promo.active_until("device-a") is not None
    with sqlite3.connect(os.environ["WX_DB"]) as con:
        con.execute("UPDATE promo_redemptions SET pro_until=? WHERE subject=?",
                    ("2020-01-01T00:00:00Z", "device-a"))
    assert promo.active_until("device-a") is None


def test_creation_input_is_validated(promo_db):
    for code, days in (("X", 5), ("GOODCODE", 0), ("GOODCODE", 99999), ("!", 5)):
        with pytest.raises(ValueError):
            promo.create_code(code, days)


def test_a_negative_or_zero_max_redemption_is_refused(promo_db):
    with pytest.raises(ValueError):
        promo.create_code("GOODCODE", 5, max_redemptions=0)
    with pytest.raises(ValueError):
        promo.create_code("GOODCODE", 5, max_redemptions=-3)


def test_a_bad_date_is_refused(promo_db):
    with pytest.raises(ValueError):
        promo.create_code("GOODCODE", 5, expires_at="not-a-date")
    with pytest.raises(ValueError):
        promo.create_code("GOODCODE", 5, starts_at="2090-01-02", expires_at="2090-01-01")


def test_only_the_last_use_can_be_taken_when_two_devices_race(promo_db):
    """The conditional UPDATE is what makes the limit race-free.

    Both `redeem` calls run in sequence here, but the second sees a count that is
    already at the ceiling and is refused by the same SQL predicate that would
    refuse it under a real race.
    """
    promo.create_code("LAST", 1, max_redemptions=1)
    promo.redeem("LAST", "device-a")
    with pytest.raises(promo.RedemptionError) as e:
        promo.redeem("LAST", "device-b")
    assert e.value.code == "exhausted"
    assert promo.get_code("LAST")["redemption_count"] == 1


def test_generated_codes_avoid_ambiguous_characters():
    """Only the random body is drawn from the safe alphabet."""
    for _ in range(50):
        c = promo.generate_code(length=8)
        assert not (set(c) & set("O0I1L"))


def test_a_prefix_is_kept_verbatim_because_the_operator_writes_it_down():
    assert promo.generate_code(prefix="GIFT", length=8).startswith("GIFT")


def test_the_redemption_record_records_who_and_when(promo_db):
    promo.create_code("FRIEND5", 5)
    promo.redeem("FRIEND5", "device-a", source="web")
    rows = promo.redemptions("FRIEND5")
    assert len(rows) == 1
    assert rows[0]["subject"] == "device-a"
    assert rows[0]["source"] == "web"
    assert rows[0]["pro_until"]


def test_a_bare_database_filename_does_not_crash(monkeypatch, tmp_path):
    """`os.path.dirname("")` is "", and `makedirs("")` raises."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WX_DB", "station.db")
    promo.init_db()
    assert promo.stats()["available"]


# ============================================================ analytics

def test_only_coarse_cells_are_stored(promo_db):
    """The point is never written; only its 0.5° cell is.

    Verified by reading the stored coordinate back: it must equal the snapped
    cell, not the input.
    """
    analytics.track("forecast_loaded", device="device-a", lat=37.9838, lon=23.7275)
    with sqlite3.connect(os.environ["WX_DB"]) as con:
        row = con.execute("SELECT cell_lat, cell_lon FROM analytics_events").fetchone()
    assert row[0] == pytest.approx(38.0)
    assert row[1] == pytest.approx(23.5)
    # The exact coordinate appears nowhere in the row.
    assert 37.9838 not in row
    assert 23.7275 not in row


def test_a_few_metres_apart_land_in_the_same_cell(promo_db):
    """This is the property that makes the cell non-identifying."""
    a = analytics.coarse_cell(37.9838, 23.7275)
    b = analytics.coarse_cell(37.9850, 23.7290)
    assert a == b


def test_no_ip_or_full_referrer_is_stored(promo_db):
    class Req:
        url = type("U", (), {"path": "/api/brief"})()
        headers = {"referer": "https://news.example.gr/story?id=personal-123",
                   "accept-language": "el-GR,el;q=0.9", "user-agent": "Mozilla Firefox"}
        client = type("C", (), {"host": "203.0.113.9"})()
    analytics.track("page_view", request=Req(), device="device-a")
    with sqlite3.connect(os.environ["WX_DB"]) as con:
        row = dict(zip(
            [d[0] for d in con.execute("SELECT * FROM analytics_events LIMIT 1").description],
            con.execute("SELECT * FROM analytics_events LIMIT 1").fetchone()))
    assert row["referrer_host"] == "news.example.gr"
    assert row["language"] == "el-GR"
    assert row["browser"] == "Firefox"
    assert "personal-123" not in str(row)
    assert "203.0.113.9" not in str(row)


def test_unknown_event_names_are_refused(promo_db):
    """A typo must not create a new dimension no dashboard knows about."""
    assert analytics.track("forecast_loaed_typo", device="d") is False
    assert analytics.summary(1)["totals"]["events"] == 0


def test_analytics_can_be_disabled_entirely(monkeypatch, promo_db):
    monkeypatch.setenv("WX_ANALYTICS", "0")
    assert analytics.enabled() is False
    assert analytics.track("page_view", device="d") is False


def test_analytics_failure_never_breaks_the_caller(promo_db, monkeypatch):
    """A write failure is logged, not raised: the forecast must still render."""
    monkeypatch.setenv("WX_DB", "/proc/definitely/not/writable.db")
    assert analytics.track("page_view", device="d") is False


def test_the_visitor_hash_is_salted_and_not_reversible(promo_db):
    h = analytics.hash_visitor("device-a")
    assert h and len(h) == 20
    assert "device-a" not in h


def test_summary_counts_visitors_sessions_and_events(promo_db):
    analytics.track("page_view", device="a")
    analytics.track("forecast_loaded", device="a")
    analytics.track("page_view", device="b")
    s = analytics.summary(days=7)
    assert s["totals"]["visitors"] == 2
    assert s["totals"]["events"] == 3
    names = {r["name"]: r["n"] for r in s["events"]}
    assert names["page_view"] == 2


def test_summary_windows_out_old_rows(promo_db):
    analytics.track("page_view", device="a")
    with sqlite3.connect(os.environ["WX_DB"]) as con:
        con.execute("UPDATE analytics_events SET day='2000-01-01', ts='2000-01-01T00:00:00Z'")
    assert analytics.summary(days=7)["totals"]["events"] == 0


def test_retention_prunes_rows_past_the_window(promo_db, monkeypatch):
    """Raw events are the one table an anonymous caller can grow, so old rows
    must actually leave the file rather than merely fall out of the summary."""
    monkeypatch.setenv("WX_ANALYTICS_RETENTION_DAYS", "30")
    analytics.track("page_view", device="a")
    with sqlite3.connect(os.environ["WX_DB"]) as con:
        con.execute("UPDATE analytics_events SET day='2000-01-01', ts='2000-01-01T00:00:00Z'")
    out = analytics.prune()
    assert out["pruned"] == 1
    with sqlite3.connect(os.environ["WX_DB"]) as con:
        remaining = con.execute("SELECT COUNT(*) FROM analytics_events").fetchone()[0]
    assert remaining == 0


def test_retention_keeps_rows_inside_the_window(promo_db, monkeypatch):
    monkeypatch.setenv("WX_ANALYTICS_RETENTION_DAYS", "30")
    analytics.track("page_view", device="a")
    assert analytics.prune()["pruned"] == 0
    assert analytics.summary(days=7)["totals"]["events"] == 1


def test_retention_zero_disables_pruning(promo_db, monkeypatch):
    """An operator who sets it to 0 gets the old unbounded behaviour, not a wipe."""
    monkeypatch.setenv("WX_ANALYTICS_RETENTION_DAYS", "0")
    analytics.track("page_view", device="a")
    with sqlite3.connect(os.environ["WX_DB"]) as con:
        con.execute("UPDATE analytics_events SET day='2000-01-01', ts='2000-01-01T00:00:00Z'")
    assert analytics.prune()["pruned"] == 0


def test_maybe_prune_runs_at_most_once_an_hour(promo_db, monkeypatch):
    monkeypatch.setenv("WX_ANALYTICS_RETENTION_DAYS", "30")
    analytics._LAST_PRUNE = 0.0
    t = time.time()
    assert analytics.maybe_prune(now=t) is not None
    assert analytics.maybe_prune(now=t + 60) is None
    assert analytics.maybe_prune(now=t + 3601) is not None
    analytics._LAST_PRUNE = 0.0


def test_device_and_browser_classification():
    """Real user-agent strings, and the ordering that makes Safari correct.

    Every iOS browser puts "Safari" in its UA, so the Safari rule must be matched
    last or Chrome-on-iOS is miscounted. That is what the CriOS case pins down.
    """
    iphone_safari = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                     "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                     "Mobile/15E148 Safari/604.1")
    iphone_chrome = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                     "AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/120.0 "
                     "Mobile/15E148 Safari/604.1")
    ipad = ("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
    android = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like "
               "Gecko) Chrome/120.0 Mobile Safari/537.36")
    desktop = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like "
               "Gecko) Chrome/120.0 Safari/537.36")

    assert analytics.describe_ua(iphone_safari) == ("phone", "Safari")
    assert analytics.describe_ua(iphone_chrome) == ("phone", "Chrome")
    assert analytics.describe_ua(ipad) == ("tablet", "Safari")
    assert analytics.describe_ua(android) == ("phone", "Chrome")
    assert analytics.describe_ua(desktop) == ("desktop", "Chrome")
    assert analytics.describe_ua(None) == ("unknown", "unknown")


def test_analytics_ingest_is_not_exempt_from_rate_limiting():
    """The one endpoint an anonymous caller can grow must stay throttled.

    Regression guard: `/api/analytics` was on the exempt list, which let a
    client write rows without bound. Importing the app is what the other suites
    already do, so this is the same cost as any end-to-end test.
    """
    import app as app_module
    assert "/api/analytics" in app_module._LIMITS
    assert "/api/analytics" not in app_module._LIMIT_EXEMPT
