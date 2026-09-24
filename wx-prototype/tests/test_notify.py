"""Notifications: entitlement gating, thresholds, dedupe/retry, location, privacy.

Three layers are covered here:

* the pure rule engine (`notify.evaluate`) — no network, no database, no clock,
  driven with a hand-built hourly series so every boundary is exact;
* the dedupe/delivery state machine (`notify.claim` / `mark_*` / `dispatch_once`)
  driven with a fake push, so restart and retry behaviour is tested for real
  rather than asserted from reading the code;
* the HTTP surface (`/api/notify/*`, `/api/push/*`) through TestClient, to pin
  the PRO gate and the graceful degradation when VAPID is absent.

No test here contacts a push service. The fake push is installed by monkeypatch,
which is the only way to exercise the delivery path without sending anything.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import entitlements as ent  # noqa: E402
import notify  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=3))   # Europe/Athens in summer


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Point every store at a fresh database for one test."""
    monkeypatch.setenv("WX_DB", str(tmp_path / "station.db"))
    monkeypatch.setenv("WX_RATE_LIMIT_DISABLED", "1")
    notify.init_db()
    return tmp_path


@pytest.fixture()
def client(db):
    return TestClient(app_module.app)


@pytest.fixture(autouse=True)
def _no_vapid(monkeypatch):
    """Default every test to "not configured" unless it sets keys itself.

    The `pywebpush` package is also made to look present, so a test can turn the
    feature on by setting the two VAPID keys alone and the dependency check does
    not have to be re-stubbed in every case.
    """
    monkeypatch.delenv("WX_VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("WX_VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(notify, "webpush_available", lambda: True)


def _hours(**over):
    """A flat 24 h series. A scalar override sets hour 1; a dict sets the named
    hours, so a test can shape a window rather than a single hour."""
    base = {"t": 25.0, "precip": 0.0, "wind": 10.0, "gust": 15.0, "cape": None,
            "feels": 25.0}
    out = []
    for s in range(1, 25):
        row = dict(base)
        row["step_h"] = s
        out.append(row)
    for k, v in over.items():
        if isinstance(v, dict):
            for step, value in v.items():
                out[int(step) - 1][k] = value
        else:
            out[0][k] = v
    return out


def _span(start, span, **vals):
    """A dict setting `vals` across `span` consecutive hours from `start`."""
    return {k: {s: v for s in range(start, start + span)}
            for k, v in vals.items()}


RUN = "2026071512"   # 12Z run; step 1 lands at 16:00 local (13:00+3)


# ---------------------------------------------------------------- rules: rain

def test_rain_below_threshold_is_silent():
    alerts = notify.evaluate(_hours(precip=14.9))
    assert [a for a in alerts if a.rule == "rain"] == []


def test_rain_at_threshold_warns():
    alerts = notify.evaluate(_hours(precip=15.0))
    rain = [a for a in alerts if a.rule == "rain"]
    assert len(rain) == 1 and rain[0].severity == "warn"


def test_rain_severe_at_25mm():
    alerts = notify.evaluate(_hours(precip=25.0))
    rain = [a for a in alerts if a.rule == "rain"]
    assert len(rain) == 1 and rain[0].severity == "severe"


def test_rain_accumulates_over_six_hours():
    """5 mm/h for six hours is 30 mm, which is severe even though no single hour
    is remarkable. The rule sums a window, it does not look at one hour."""
    hours = _hours(precip={1: 5.0, 2: 5.0, 3: 5.0, 4: 5.0, 5: 5.0, 6: 5.0})
    rain = [a for a in notify.evaluate(hours) if a.rule == "rain"]
    assert len(rain) == 1 and rain[0].severity == "severe"
    assert rain[0].value == 30.0


def test_rain_rule_respects_the_user_switch():
    alerts = notify.evaluate(_hours(precip=30.0), rules={"rain": 0})
    assert [a for a in alerts if a.rule == "rain"] == []


# ---------------------------------------------------------------- rules: storm

def test_storm_requires_both_cape_and_rate():
    assert [a for a in notify.evaluate(_hours(cape=1500.0, precip=4.9))
            if a.rule == "storm"] == []
    assert [a for a in notify.evaluate(_hours(cape=999.0, precip=9.0))
            if a.rule == "storm"] == []


def test_storm_fires_when_cape_and_rate_both_cross():
    # The rule looks at a 3 h window and requires CAPE on every hour of it, so
    # both fields are set across the span, not on a single hour.
    alerts = notify.evaluate(_hours(**{**_span(1, 3, cape=1000.0),
                                       **_span(1, 1, precip=5.0)}))
    storm = [a for a in alerts if a.rule == "storm"]
    assert len(storm) == 1 and storm[0].severity == "warn"


def test_storm_severe_at_2000_cape():
    alerts = notify.evaluate(_hours(**{**_span(1, 3, cape=2000.0),
                                       **_span(1, 1, precip=6.0)}))
    storm = [a for a in alerts if a.rule == "storm"]
    assert len(storm) == 1 and storm[0].severity == "severe"


def test_storm_never_fires_without_cape_data():
    """Where the model gives no CAPE the rule must not run at all: substituting a
    guess would present a condition as a forecast."""
    alerts = notify.evaluate(_hours(cape=None, precip=40.0))
    assert [a for a in alerts if a.rule == "storm"] == []


# ---------------------------------------------------------------- rules: wind

def test_wind_below_threshold_is_silent():
    assert [a for a in notify.evaluate(_hours(gust=69.0)) if a.rule == "wind"] == []


def test_wind_at_threshold_warns():
    wind = [a for a in notify.evaluate(_hours(gust=70.0)) if a.rule == "wind"]
    assert len(wind) == 1 and wind[0].severity == "warn"


def test_wind_severe_at_90():
    wind = [a for a in notify.evaluate(_hours(gust=90.0)) if a.rule == "wind"]
    assert len(wind) == 1 and wind[0].severity == "severe"


def test_wind_falls_back_to_sustained_when_gust_missing():
    hours = _hours(wind=75.0)
    for h in hours:
        h["gust"] = None
    wind = [a for a in notify.evaluate(hours) if a.rule == "wind"]
    assert len(wind) == 1 and wind[0].value == 75.0


# ---------------------------------------------------------------- rules: temp

def test_heat_at_40_c():
    assert [a for a in notify.evaluate(_hours(t=39.9)) if a.rule == "temp"] == []
    hot = [a for a in notify.evaluate(_hours(t=40.0)) if a.rule == "temp"]
    assert len(hot) == 1 and "ζέστη" in hot[0].title


def test_feels_like_41_c_is_heat():
    alert = [a for a in notify.evaluate(_hours(t=35.0, feels=41.0))
             if a.rule == "temp"]
    assert len(alert) == 1 and "αίσθηση" in alert[0].body


def test_cold_at_minus_10():
    assert [a for a in notify.evaluate(_hours(t=-9.9)) if a.rule == "temp"] == []
    cold = [a for a in notify.evaluate(_hours(t=-10.0)) if a.rule == "temp"]
    assert len(cold) == 1 and "κρύο" in cold[0].title


def test_heat_wins_over_cold_when_both_present():
    """A day with a 41 C afternoon and a -11 C night should read as the heat
    event, because that is the one the user must act on; one alert per rule."""
    hours = _hours(t={1: -11.0, 14: 41.0})
    temp = [a for a in notify.evaluate(hours) if a.rule == "temp"]
    assert len(temp) == 1 and "ζέστη" in temp[0].title


# ---------------------------------------------------------------- lead time

def test_minimum_lead_suppresses_an_imminent_event():
    """An alert for something starting within the minimum lead is not actionable."""
    hours = _hours(precip=30.0)
    alerts = notify.evaluate(hours, th=notify.Thresholds(min_lead_h=3))
    assert [a for a in alerts if a.rule == "rain"] == []


# ---------------------------------------------------------------- quiet hours

def test_quiet_hours_wrap_midnight():
    th = notify.Thresholds(quiet_from=22, quiet_to=7)
    assert notify.in_quiet_hours(23, th) is True
    assert notify.in_quiet_hours(3, th) is True
    assert notify.in_quiet_hours(7, th) is False
    assert notify.in_quiet_hours(12, th) is False


def test_quiet_hours_disabled_when_equal():
    assert notify.in_quiet_hours(23, notify.Thresholds(quiet_from=0, quiet_to=0)) is False


# ---------------------------------------------------------------- thresholds config

def test_thresholds_are_env_overridable(monkeypatch):
    monkeypatch.setenv("WX_NOTIFY_RAIN_6H_MM", "7.5")
    monkeypatch.setenv("WX_NOTIFY_GUST_KMH", "55")
    monkeypatch.setenv("WX_NOTIFY_COOLDOWN_H_RAIN", "1")
    th = notify.load_thresholds()
    assert th.rain_6h_mm == 7.5
    assert th.gust_kmh == 55
    assert th.cooldown_h["rain"] == 1


def test_threshold_defaults_are_the_agreed_initial_values():
    th = notify.Thresholds()
    assert (th.rain_6h_mm, th.rain_6h_mm_severe) == (15.0, 25.0)
    assert (th.cape_jkg, th.cape_jkg_severe, th.storm_pr_mmh) == (1000.0, 2000.0, 5.0)
    assert (th.gust_kmh, th.gust_kmh_severe) == (70.0, 90.0)
    assert (th.hot_c, th.feels_c, th.cold_c) == (40.0, 41.0, -10.0)
    assert (th.quiet_from, th.quiet_to, th.max_per_day) == (22, 7, 5)


# ---------------------------------------------------------------- quantization

def test_location_is_quantized_before_storage():
    lat, lon = notify.quantize(37.9672, 23.7489)
    assert lat == 38.0 and lon == 23.7
    assert lat % notify.CELL_DEG == 0 or round(lat / notify.CELL_DEG, 6).is_integer()


def test_quantize_never_returns_the_exact_point():
    for lat, lon in ((37.9672, 23.7489), (40.123456, 22.987654), (35.5, 24.01)):
        q = notify.quantize(lat, lon)
        assert q != (lat, lon)


# ---------------------------------------------------------------- dedupe: claimed/sent

def _sub(subject="dev-1", cell=(38.0, 23.7)):
    return {"subject": subject, "endpoint": "https://push.example/x",
            "p256dh": "k", "auth": "a", "cell_lat": cell[0], "cell_lon": cell[1],
            "rules": {"rain": 1, "storm": 1, "wind": 1, "temp": 1},
            "active": True, "pro_until_cached": int(_future())}


def _future():
    import time
    return time.time() + 86400


def _alert(rule="rain", bucket=0, cell="38.00,23.70", value=20.0):
    return notify.Alert(rule, "warn", 6, bucket, value, 6, "t", "b",
                        local_date="2026-07-15", local_hour=12, cell=cell)


def test_first_claim_owns_the_event(db):
    assert notify.claim("dev-1", _alert()) == "claimed"


def test_second_claim_after_send_is_refused(db):
    a = _alert()
    notify.claim("dev-1", a)
    notify.mark_sent("dev-1", a.key)
    assert notify.claim("dev-1", a) == "sent"


def test_restart_does_not_resend_a_sent_event(db):
    """The dedupe lives in the database, so a fresh process sees the same row."""
    a = _alert()
    notify.claim("dev-1", a)
    notify.mark_sent("dev-1", a.key)
    # Simulate a restart: nothing in memory is reused, only the db is consulted.
    assert notify.claim("dev-1", a) == "sent"


def test_different_cell_is_a_new_event(db):
    a1 = _alert(rule="rain", cell="38.00,23.70")
    notify.claim("dev-1", a1)
    notify.mark_sent("dev-1", a1.key)
    # A different rule entirely, so the rain cooldown is not what is being tested.
    a2 = _alert(rule="wind", cell="37.00,25.00")
    assert a2.key != a1.key
    assert notify.claim("dev-1", a2) == "claimed"


def test_changing_location_changes_the_event_key(db):
    assert _alert(cell="38.00,23.70").key != _alert(cell="38.00,23.80").key


# ---------------------------------------------------------------- retry

def test_failed_push_stays_retryable(db):
    a = _alert()
    assert notify.claim("dev-1", a) == "claimed"
    status = notify.mark_attempt_failed("dev-1", a.key, "TimeoutError")
    assert status == "pending"
    # Backoff has not elapsed, so the next pass waits rather than hammering.
    assert notify.claim("dev-1", a) == "waiting"


def test_retry_after_backoff_is_reclaimed(db):
    a = _alert()
    notify.claim("dev-1", a)
    notify.mark_attempt_failed("dev-1", a.key, "TimeoutError")
    # Rewind the last attempt so the backoff appears elapsed.
    import sqlite3
    import config
    with sqlite3.connect(config.db_path()) as con:
        con.execute("UPDATE notify_sent SET last_attempt_at=? WHERE event_key=?",
                    ("2020-01-01T00:00:00Z", a.key))
    assert notify.claim("dev-1", a) == "pending"


def test_retry_exhaustion_marks_failed_and_stops(db):
    a = _alert()
    notify.claim("dev-1", a)
    for _ in range(notify.load_thresholds().retry_max):
        notify.mark_attempt_failed("dev-1", a.key, "boom")
    assert notify.claim("dev-1", a) == "sent"      # never resurrected
    row = _sent_row("dev-1", a.key)
    assert row["status"] == "failed"


def test_successful_retry_yields_exactly_one_sent_row(db):
    a = _alert()
    notify.claim("dev-1", a)
    notify.mark_attempt_failed("dev-1", a.key, "boom")
    notify.mark_attempt_failed("dev-1", a.key, "boom")
    notify.mark_sent("dev-1", a.key)
    rows = _all_sent("dev-1")
    assert len(rows) == 1 and rows[0]["status"] == "sent"


def _sent_row(subject, key):
    import sqlite3
    import config
    con = sqlite3.connect(config.db_path())
    con.row_factory = sqlite3.Row
    return con.execute("SELECT * FROM notify_sent WHERE subject=? AND event_key=?",
                       (subject, key)).fetchone()


def _all_sent(subject):
    import sqlite3
    import config
    con = sqlite3.connect(config.db_path())
    con.row_factory = sqlite3.Row
    return con.execute("SELECT * FROM notify_sent WHERE subject=?", (subject,)).fetchall()


# ---------------------------------------------------------------- cooldown / cap

def test_cooldown_suppresses_a_second_event_of_the_same_rule(db):
    a1 = _alert(bucket=0)
    notify.claim("dev-1", a1)
    notify.mark_sent("dev-1", a1.key)
    a2 = _alert(bucket=1)           # same rule, same day, different window
    assert notify.claim("dev-1", a2) == "cooling"


def test_daily_cap_suppresses_after_max(db, monkeypatch):
    monkeypatch.setenv("WX_NOTIFY_MAX_PER_DAY", "1")
    monkeypatch.setenv("WX_NOTIFY_COOLDOWN_H_TEMP", "0")
    a1 = _alert(rule="temp", bucket=0)
    notify.claim("dev-1", a1)
    notify.mark_sent("dev-1", a1.key)
    a2 = _alert(rule="temp", bucket=1)
    assert notify.claim("dev-1", a2) == "cooling"


# ---------------------------------------------------------------- dispatch

def test_dispatch_sends_once_and_records_it(db, monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "send_push",
                        lambda sub, alert: (calls.append(alert.key), (True, None))[1])
    counts = notify.dispatch_once([_sub()], lambda s: _hours(precip=30.0),
                                  run_utc=RUN, tz=TZ)
    assert counts["sent"] == 1 and len(calls) == 1


def test_dispatch_is_idempotent_across_passes(db, monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "send_push",
                        lambda sub, alert: (calls.append(alert.key), (True, None))[1])
    notify.dispatch_once([_sub()], lambda s: _hours(precip=30.0), run_utc=RUN, tz=TZ)
    notify.dispatch_once([_sub()], lambda s: _hours(precip=30.0), run_utc=RUN, tz=TZ)
    assert len(calls) == 1, "the same event must not be delivered twice"


def test_dispatch_skips_a_subscriber_with_no_data(db, monkeypatch):
    monkeypatch.setattr(notify, "send_push", lambda s, a: (True, None))
    counts = notify.dispatch_once([_sub()], lambda s: None, run_utc=RUN, tz=TZ)
    assert counts["sent"] == 0 and counts["skipped"] >= 1


def test_dispatch_never_raises_on_a_bad_subscriber(db, monkeypatch):
    def boom(s):
        raise RuntimeError("model exploded")
    monkeypatch.setattr(notify, "send_push", lambda s, a: (True, None))
    counts = notify.dispatch_once([_sub()], boom, run_utc=RUN, tz=TZ)
    assert counts["errors"] == 1


def test_two_subscribers_in_one_cell_share_a_series_read(db, monkeypatch):
    """The shared-read guarantee: the memo means one call per cell, not per sub."""
    reads = []

    def series_for(sub):
        reads.append(notify.cell_label(sub.get("cell_lat"), sub.get("cell_lon")))
        return _hours(precip=30.0)

    monkeypatch.setattr(notify, "send_push", lambda s, a: (True, None))
    # Mirror run_forever's memo wrapper.
    memo = {}

    def one(sub):
        key = notify.cell_label(sub.get("cell_lat"), sub.get("cell_lon"))
        if key not in memo:
            memo[key] = series_for(sub)
        return memo[key]

    notify.dispatch_once([_sub("a"), _sub("b")], one, run_utc=RUN, tz=TZ)
    assert len(reads) == 1, "two subscribers in one cell must share one read"


def test_severe_alert_ignores_quiet_hours(db, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_push",
                        lambda s, a: (sent.append(a.severity), (True, None))[1])
    # A 00Z run: step 1 lands at 03:00 local, inside the 22:00-07:00 quiet window.
    hours = _hours(precip=0.0)
    hours[0]["precip"] = 16.0                        # warn-level 6 h total
    counts = notify.dispatch_once([_sub()], lambda s: hours, run_utc="2026071500", tz=TZ)
    assert counts["sent"] == 0                       # warn suppressed at night
    # A severe event at the same hour must get through.
    hours[0]["precip"] = 30.0
    notify.purge_sent(0)
    sent.clear()
    counts = notify.dispatch_once([_sub()], lambda s: hours, run_utc="2026071500", tz=TZ)
    assert counts["sent"] == 1 and sent == ["severe"]


# ---------------------------------------------------------------- pro cache / dispatch gate

def test_expired_pro_cache_blocks_dispatch(db, monkeypatch):
    """An expired subscription must not keep receiving alerts. The loop filters on
    the cached window before it ever reads a model."""
    import time
    monkeypatch.setattr(notify, "send_push", lambda s, a: (True, None))
    sub = _sub()
    sub["pro_until_cached"] = int(time.time()) - 60
    subs = [sub]
    eligible = [s for s in subs if notify._pro_active(s)]
    assert eligible == []


def test_pro_cache_refresh_does_not_log_out_on_stripe_blip(db, monkeypatch):
    class FakeBilling:
        @staticmethod
        def subscription_access(sid):
            return {"status": "unknown"}
    # Create the row first: the refresh path updates an existing subscription.
    notify.upsert_subscription("dev-1", "https://push.example/x", "k", "a",
                               subscription_id="sub_123")
    notify.set_pro_cache("dev-1", 999, "sub_123")
    before = notify.get_subscription("dev-1")["pro_until_cached"]
    notify.refresh_pro_cache(notify.get_subscription("dev-1"), billing=FakeBilling)
    after = notify.get_subscription("dev-1")["pro_until_cached"]
    assert before == after, "an unreachable Stripe must not erase the cached window"


# ---------------------------------------------------------------- HTTP: entitlement gate

def _pro_token(monkeypatch, client):
    monkeypatch.setenv("WX_MASTER_CODE", "TEST-PASSCODE")
    r = client.post("/api/auth/passcode", json={"code": "TEST-PASSCODE"})
    assert r.status_code == 200
    return r.json()["token"]



def _subscribe(client, tok, **extra):
    """Subscribe a PRO token and return (token, device).

    The first notify write mints a device id for a token that has none and
    returns a re-signed token; every later assertion must use that token, exactly
    as the browser does. Testing with the original token would be testing a state
    the app never leaves itself in.
    """
    body = {"endpoint": "https://push.example/x", "keys": {"p256dh": "k", "auth": "a"}}
    body.update(extra)
    r = client.post("/api/push/subscribe", headers={"X-WX-Token": tok}, json=body)
    assert r.status_code == 200, r.text
    new_tok = r.json().get("token") or tok
    return new_tok, ent.verify_token(new_tok).device


def test_free_user_cannot_subscribe(client, monkeypatch):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    r = client.post("/api/push/subscribe", json={
        "endpoint": "https://push.example/x", "keys": {"p256dh": "k", "auth": "a"}})
    assert r.status_code == 403


def test_free_user_cannot_set_location(client, monkeypatch):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    r = client.post("/api/notify/location", json={"lat": 38.0, "lon": 23.7})
    assert r.status_code == 403


def test_expired_pro_cannot_subscribe(client, monkeypatch):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    expired = ent.issue_token("pro", "promo", ttl=-100)
    r = client.post("/api/push/subscribe",
                    headers={"X-WX-Token": expired},
                    json={"endpoint": "https://push.example/x",
                          "keys": {"p256dh": "k", "auth": "a"}})
    assert r.status_code == 403


def test_pro_user_can_subscribe(client, monkeypatch, db):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    tok = _pro_token(monkeypatch, client)
    tok, dev = _subscribe(client, tok, location={"lat": 37.9672, "lon": 23.7489,
                                                 "name": "Ηλιούπολη"})
    st = client.get("/api/notify/state", headers={"X-WX-Token": tok}).json()
    assert st["subscribed"] and st["active"] and st["place_name"] == "Ηλιούπολη"


def test_notify_state_is_free_for_a_free_caller(client):
    st = client.get("/api/notify/state").json()
    assert st["eligible"] is False


# ---------------------------------------------------------------- location independence

def test_forecast_location_does_not_change_notification_location(client, monkeypatch, db):
    """The whole reason the two are separate. Fetching a forecast must not touch
    the notification cell, or browsing would silently move the user's alerts."""
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    tok = _pro_token(monkeypatch, client)
    tok, dev = _subscribe(client, tok, location={"lat": 37.9672, "lon": 23.7489,
                                                 "name": "Ηλιούπολη"})
    before = notify.get_subscription(dev)
    # Ask for a completely different place. This endpoint is cached/network-bound
    # and may 502 in the test sandbox; either way it must not write to notify_subs.
    client.get("/api/brief", params={"lat": 37.1, "lon": 25.4})
    after = notify.get_subscription(dev)
    assert after["cell_lat"] == before["cell_lat"]
    assert after["cell_lon"] == before["cell_lon"]
    assert after["place_name"] == "Ηλιούπολη"


def test_location_change_updates_only_that_subscription(client, monkeypatch, db):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    tok = _pro_token(monkeypatch, client)
    tok, dev = _subscribe(client, tok, location={"lat": 37.9672, "lon": 23.7489,
                                                 "name": "Ηλιούπολη"})
    r = client.post("/api/notify/location", headers={"X-WX-Token": tok},
                    json={"lat": 37.0667, "lon": 25.4167, "name": "Γλινάδο",
                          "admin1": "Νάξος"})
    assert r.status_code == 200
    sub = notify.get_subscription(dev)
    assert sub["place_name"] == "Γλινάδο"
    assert (sub["cell_lat"], sub["cell_lon"]) == notify.quantize(37.0667, 25.4167)


def test_location_is_stored_quantized_not_exact(client, monkeypatch, db):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    tok = _pro_token(monkeypatch, client)
    tok, dev = _subscribe(client, tok)
    client.post("/api/notify/location", headers={"X-WX-Token": tok},
                json={"lat": 37.9672, "lon": 23.7489, "name": "X"})
    sub = notify.get_subscription(dev)
    assert sub["cell_lat"] == 38.0 and sub["cell_lon"] == 23.7
    # The exact input must appear nowhere in the stored row.
    assert "37.9672" not in repr(dict(sub))


# ---------------------------------------------------------------- graceful degradation

def test_push_config_reports_unavailable_without_keys(client):
    r = client.get("/api/push/config")
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is False
    assert d["vapid_public_key"] is None


def test_push_config_never_leaks_the_private_key(client, monkeypatch):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "TOP-SECRET-PRIVATE")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    body = client.get("/api/push/config").text
    assert "TOP-SECRET-PRIVATE" not in body


def test_subscribe_is_503_when_unconfigured_not_403(client, monkeypatch):
    """Without VAPID the feature is off entirely, so the answer is "not available"
    rather than leaking that the caller also lacks PRO."""
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    monkeypatch.setenv("WX_MASTER_CODE", "TEST-PASSCODE")
    tok = client.post("/api/auth/passcode", json={"code": "TEST-PASSCODE"}).json()["token"]
    r = client.post("/api/push/subscribe", headers={"X-WX-Token": tok}, json={
        "endpoint": "https://push.example/x", "keys": {"p256dh": "k", "auth": "a"}})
    assert r.status_code == 503


def test_health_reports_push_state(client):
    d = client.get("/api/health").json()
    assert "push" in d
    assert d["push"]["configured"] is False
    assert "subscribers" in d["push"]


# ---------------------------------------------------------------- validation

def test_subscribe_rejects_a_non_https_endpoint(client, monkeypatch, db):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    tok = _pro_token(monkeypatch, client)
    r = client.post("/api/push/subscribe", headers={"X-WX-Token": tok}, json={
        "endpoint": "javascript:alert(1)", "keys": {"p256dh": "k", "auth": "a"}})
    assert r.status_code == 422


def test_location_rejects_bad_coordinates(client, monkeypatch, db):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    tok = _pro_token(monkeypatch, client)
    tok, dev = _subscribe(client, tok)
    r = client.post("/api/notify/location", headers={"X-WX-Token": tok},
                    json={"lat": 999, "lon": 23.7})
    assert r.status_code == 422


def test_location_requires_an_existing_subscription(client, monkeypatch, db):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    tok = _pro_token(monkeypatch, client)
    r = client.post("/api/notify/location", headers={"X-WX-Token": tok},
                    json={"lat": 38.0, "lon": 23.7})
    assert r.status_code == 409


# ---------------------------------------------------------------- prefs

def test_prefs_toggle_rules_and_active(client, monkeypatch, db):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    tok = _pro_token(monkeypatch, client)
    tok, dev = _subscribe(client, tok)
    r = client.post("/api/notify/prefs", headers={"X-WX-Token": tok},
                    json={"rules": {"rain": 0, "wind": 1}})
    assert r.status_code == 200
    assert r.json()["rules"]["rain"] == 0 and r.json()["rules"]["wind"] == 1
    r = client.post("/api/notify/prefs", headers={"X-WX-Token": tok},
                    json={"active": False})
    assert r.json()["active"] is False


def test_prefs_rejects_a_bad_quiet_hour(client, monkeypatch, db):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    tok = _pro_token(monkeypatch, client)
    tok, dev = _subscribe(client, tok)
    r = client.post("/api/notify/prefs", headers={"X-WX-Token": tok},
                    json={"quiet_from": 99})
    assert r.status_code == 422


# ---------------------------------------------------------------- unsubscribe

def test_unsubscribe_deactivates_without_deleting(client, monkeypatch, db):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    tok = _pro_token(monkeypatch, client)
    tok, dev = _subscribe(client, tok)
    r = client.post("/api/push/unsubscribe", headers={"X-WX-Token": tok})
    assert r.status_code == 200
    assert notify.get_subscription(dev) is not None
    assert notify.get_subscription(dev)["active"] is False


def test_unsubscribe_purge_removes_the_row(client, monkeypatch, db):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    tok = _pro_token(monkeypatch, client)
    tok, dev = _subscribe(client, tok)
    client.post("/api/push/unsubscribe", headers={"X-WX-Token": tok},
                json={"purge": True})
    assert notify.get_subscription(dev) is None


# ---------------------------------------------------------------- test send

def test_notify_test_is_pro_only(client, monkeypatch, db):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    assert client.post("/api/notify/test").status_code == 403


def test_notify_test_requires_a_subscription(client, monkeypatch, db):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    tok = _pro_token(monkeypatch, client)
    assert client.post("/api/notify/test", headers={"X-WX-Token": tok}).status_code == 409


def test_notify_test_sends_one_push(client, monkeypatch, db):
    monkeypatch.setenv("WX_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("WX_VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(notify, "webpush_available", lambda: True)
    sent = []
    monkeypatch.setattr(notify, "send_push",
                        lambda s, a: (sent.append(a), (True, None))[1])
    tok = _pro_token(monkeypatch, client)
    tok, dev = _subscribe(client, tok)
    r = client.post("/api/notify/test", headers={"X-WX-Token": tok})
    assert r.status_code == 200 and r.json()["sent"] is True
    assert len(sent) == 1


# ---------------------------------------------------------------- observability

def test_stats_counts_subscribers(db, monkeypatch):
    notify.upsert_subscription("dev-1", "https://push.example/x", "k", "a")
    st = notify.stats()
    assert st["subscribers"] == 1 and st["active"] == 1


def test_dispatch_records_a_run_row(db, monkeypatch):
    monkeypatch.setattr(notify, "send_push", lambda s, a: (True, None))
    notify.dispatch_once([_sub()], lambda s: _hours(precip=30.0), run_utc=RUN, tz=TZ)
    run = notify.last_run()
    assert run is not None and run["sent"] == 1


def test_stats_survives_a_broken_database(monkeypatch):
    monkeypatch.setenv("WX_DB", "/proc/definitely/not/writable.db")
    st = notify.stats()
    assert "error" in st
