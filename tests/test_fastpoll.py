"""Adaptive fast polling: breaker, live-file overlay and the wait loop. No network, no real sleeps."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import requests

from franklinwh_scraper import fastpoll
from franklinwh_scraper.account import Current, Stats, Totals
from franklinwh_scraper.config import Config

T0 = datetime(2026, 11, 11, 17, 0, 0)


def _stats(load=5.0, soc=60.0, grid=0.0):
    return Stats(
        timestamp="x", gateway_id="gw",
        current=Current(solar_production_kw=0.0, generator_production_kw=0.0, generator_enabled=False,
                        battery_use_kw=load - grid, grid_use_kw=grid, home_load_kw=load,
                        battery_soc_pct=soc, grid_status="normal"),
        totals=Totals(0, 0, 0, 0, 0, 1.5, 0),
    )


def _cfg(**kw):
    base = dict(fast_poll=True, fast_poll_seconds=120, fast_poll_load_kw=3.0, fast_poll_max_per_hour=36)
    base.update(kw)
    return Config(**base)


class Clock:
    """Fake time: sleep() moves it, nothing really waits."""
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self): return self.t
    def now(self): return T0 + timedelta(seconds=self.t)

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class FakeClient:
    def __init__(self, clock, readings=None, errors=None):
        self.clock, self.readings, self.errors = clock, list(readings or []), list(errors or [])
        self.calls: list[dict] = []

    def get_stats(self, gateway, **kw):
        self.calls.append({"at": self.clock.t, "gateway": gateway, **kw})
        if self.errors:
            err = self.errors.pop(0)
            if err is not None:
                raise err
        return self.readings.pop(0) if self.readings else _stats()


def _run(tmp_path, clock, client, cfg=None, last=None, deadline=300.0, guard=None, alerts=None):
    cfg = cfg or _cfg()
    guard = guard or fastpoll.FastPollGuard.for_config(cfg)
    alerts = alerts if alerts is not None else []
    fastpoll.wait_with_fast_polls(
        client=client, gateway="gw", outdir=tmp_path, cfg=cfg,
        last_stats=last if last is not None else _stats(), deadline=deadline, guard=guard,
        alert=lambda msg, urgent=False: alerts.append((msg, urgent)),
        sleep=clock.sleep, monotonic=clock.monotonic, now=clock.now,
    )
    return guard, alerts


# ── "hot" test and config clamps ─────────────────────────────────────────────────────────────────

def test_is_hot_at_and_above_the_threshold_only():
    assert fastpoll.is_hot(_stats(load=3.0), 3.0)
    assert fastpoll.is_hot(_stats(load=7.6), 3.0)
    assert not fastpoll.is_hot(_stats(load=2.99), 3.0)
    assert not fastpoll.is_hot(None, 3.0)


def test_interval_never_drops_below_the_60s_floor():
    assert fastpoll.effective_interval_s(_cfg(fast_poll_seconds=10)) == 60
    assert fastpoll.effective_interval_s(_cfg(fast_poll_seconds=120)) == 120
    assert fastpoll.effective_interval_s(_cfg(fast_poll_seconds="junk")) == 120     # hand-edited config can't crash the loop


def test_fast_poll_is_off_by_default():
    assert Config().fast_poll is False


# ── breaker ──────────────────────────────────────────────────────────────────────────────────────

def _guard(**kw):
    return fastpoll.FastPollGuard(min_interval_s=kw.pop("min_interval_s", 120), max_per_hour=kw.pop("max_per_hour", 36), **kw)


def test_guard_spaces_polls_and_caps_the_hourly_budget():
    g = _guard(max_per_hour=3, min_interval_s=60)
    assert g.allow(T0)
    g.note_attempt(T0)
    assert not g.allow(T0 + timedelta(seconds=30))            # too soon
    for i in (1, 2):
        g.note_attempt(T0 + timedelta(seconds=60 * i))
    assert not g.allow(T0 + timedelta(seconds=180))           # 3 in the last hour: budget spent
    assert g.allow(T0 + timedelta(seconds=3601))              # window slid past the first


def test_two_errors_in_a_row_trip_the_breaker_with_growing_cooldowns():
    g = _guard()
    assert g.fail(T0, RuntimeError("boom")) is None           # one blip is tolerated
    msg = g.fail(T0 + timedelta(minutes=2), RuntimeError("boom"))
    assert msg and "paused" in msg.lower()
    assert not g.allow(T0 + timedelta(minutes=2, seconds=5))
    assert g.blocked_until == T0 + timedelta(minutes=2) + timedelta(minutes=10)
    later = T0 + timedelta(minutes=13)
    assert g.allow(later)
    g.fail(later, RuntimeError("boom"))
    assert g.fail(later + timedelta(minutes=2), RuntimeError("boom")) is None      # re-trips quietly: only the first is announced
    assert g.blocked_until == later + timedelta(minutes=2) + timedelta(minutes=20)  # cooldown doubled


def test_cooldown_is_capped_at_two_hours():
    g = _guard()
    t = T0
    waits = []
    for _ in range(10):
        g.fail(t, RuntimeError("x"))
        g.fail(t, RuntimeError("x"))
        waits.append(g.blocked_until - t)
        t = g.blocked_until + timedelta(seconds=1)
    assert waits[:4] == [timedelta(minutes=m) for m in (10, 20, 40, 80)]
    assert max(waits) == timedelta(hours=2)


def test_a_success_clears_the_error_streak_and_the_cooldown_ladder():
    g = _guard()
    g.fail(T0, RuntimeError("x"))
    g.ok(T0 + timedelta(minutes=2))
    assert g.fail(T0 + timedelta(minutes=4), RuntimeError("x")) is None   # streak restarted: still just one error
    assert g.errors == 1 and g.blocked_until is None


@pytest.mark.parametrize("exc", [
    requests.HTTPError("429 Client Error", response=SimpleNamespace(status_code=429)),
    RuntimeError("Login failed, code 429: Too many requests"),
    RuntimeError("API said: rate limit exceeded"),
])
def test_throttling_trips_at_once_for_an_hour_and_says_so(exc):
    g = _guard()
    msg = g.fail(T0, exc)
    assert msg and "throttl" in msg.lower()
    assert g.blocked_until == T0 + timedelta(hours=1)
    assert not g.stopped                                              # recoverable, just backed off


def test_throttle_cooldown_doubles_each_time_up_to_six_hours():
    g = _guard()
    t = T0
    waits = []
    for _ in range(5):
        g.fail(t, RuntimeError("429 too many requests"))
        waits.append(g.blocked_until - t)
        t = g.blocked_until + timedelta(seconds=1)
    assert waits[:3] == [timedelta(hours=1), timedelta(hours=2), timedelta(hours=4)]
    assert max(waits) == timedelta(hours=6)


def test_account_locked_stops_fast_polling_until_someone_clears_it():
    g = _guard()
    msg = g.fail(T0, ValueError("Account locked: too many attempts"))
    assert g.stopped and msg and "locked" in msg.lower()
    assert not g.allow(T0 + timedelta(days=30))                       # no timer ever re-enables it


def test_guard_state_survives_a_restart(tmp_path):
    g = _guard()
    g.fail(T0, RuntimeError("429 too many requests"))
    fastpoll.save_state(tmp_path, g)
    g2 = fastpoll.load_state(tmp_path, _guard())
    assert g2.blocked_until == g.blocked_until and g2.trips == 1
    s = _guard()
    s.fail(T0, ValueError("Account locked"))
    fastpoll.save_state(tmp_path, s)
    assert fastpoll.load_state(tmp_path, _guard()).stopped is True


def test_corrupt_state_file_falls_back_to_a_clean_guard(tmp_path):
    (tmp_path / fastpoll.STATE_FILE).write_text("{not json")
    g = fastpoll.load_state(tmp_path, _guard())
    assert g.allow(T0) and not g.stopped


# ── live file + overlay ──────────────────────────────────────────────────────────────────────────

def test_live_file_roundtrip_and_staleness(tmp_path):
    fastpoll.write_live(tmp_path, _stats(load=6.2, soc=55.5), at=T0, next_at=T0 + timedelta(seconds=120))
    live = fastpoll.read_live(tmp_path, now=T0 + timedelta(seconds=30))
    assert live["home_load_kw"] == 6.2 and live["battery_soc"] == 55.5
    assert live["timestamp"] == T0.isoformat() and live["next_poll"] == (T0 + timedelta(seconds=120)).isoformat()
    assert fastpoll.read_live(tmp_path, now=T0 + timedelta(minutes=11)) is None     # older than 10 min: ignored


def test_live_file_missing_or_corrupt_is_none(tmp_path):
    assert fastpoll.read_live(tmp_path, now=T0) is None
    (tmp_path / fastpoll.LIVE_FILE).write_text("garbage")
    assert fastpoll.read_live(tmp_path, now=T0) is None


def test_live_file_never_carries_nan(tmp_path):
    fastpoll.write_live(tmp_path, _stats(load=float("nan")), at=T0, next_at=T0)
    raw = (tmp_path / fastpoll.LIVE_FILE).read_text()
    assert "NaN" not in raw and json.loads(raw)["home_load_kw"] is None


def test_overlay_prefers_the_newer_live_reading_and_keeps_totals():
    r = {"timestamp": (T0 - timedelta(minutes=3)).isoformat(), "home_load_kw": 3.9, "solar_kw": 0.0,
         "battery_soc": 60.0, "grid_use_kw": 0.0, "grid_status": "normal", "battery_use_kw": 3.9,
         "solar_total_kwh": 9.0}
    live = {"timestamp": T0.isoformat(), "home_load_kw": 5.6, "solar_kw": 0.2, "battery_soc": 59.0,
            "grid_use_kw": 0.0, "grid_status": "normal", "battery_use_kw": 5.4, "solar_total_kwh": 9.1,
            "next_poll": (T0 + timedelta(seconds=120)).isoformat()}
    merged, used = fastpoll.overlay(r, live)
    assert used and merged["home_load_kw"] == 5.6 and merged["battery_soc"] == 59.0
    assert merged["timestamp"] == T0.isoformat() and merged["next_poll"] == live["next_poll"]
    assert r["home_load_kw"] == 3.9                                   # input not mutated


def test_overlay_ignores_an_older_live_reading_and_missing_inputs():
    r = {"timestamp": T0.isoformat(), "home_load_kw": 3.9}
    live = {"timestamp": (T0 - timedelta(minutes=1)).isoformat(), "home_load_kw": 9.9}
    assert fastpoll.overlay(r, live) == (r, False)
    assert fastpoll.overlay(r, None) == (r, False)
    assert fastpoll.overlay(None, live) == (None, False)


def test_overlay_skips_null_fields():
    r = {"timestamp": (T0 - timedelta(minutes=3)).isoformat(), "home_load_kw": 3.9, "battery_soc": 60.0}
    live = {"timestamp": T0.isoformat(), "home_load_kw": None, "battery_soc": 58.0}
    merged, used = fastpoll.overlay(r, live)
    assert used and merged["home_load_kw"] == 3.9 and merged["battery_soc"] == 58.0


# ── the wait loop ────────────────────────────────────────────────────────────────────────────────

def test_disabled_waits_the_whole_interval_and_never_polls(tmp_path):
    clock = Clock()
    client = FakeClient(clock)
    _run(tmp_path, clock, client, cfg=_cfg(fast_poll=False))
    assert client.calls == [] and clock.sleeps == [300.0]
    assert not (tmp_path / fastpoll.LIVE_FILE).exists()


def test_kill_file_stops_it_without_a_restart(tmp_path):
    (tmp_path / fastpoll.KILL_FILE).write_text("")
    clock = Clock()
    client = FakeClient(clock)
    _run(tmp_path, clock, client)
    assert client.calls == [] and clock.sleeps == [300.0]


def test_quiet_house_waits_the_whole_interval(tmp_path):
    clock = Clock()
    client = FakeClient(clock)
    _run(tmp_path, clock, client, last=_stats(load=0.6))
    assert client.calls == [] and clock.sleeps == [300.0]


def test_high_load_polls_every_two_minutes_inside_the_five_minute_cycle(tmp_path):
    clock = Clock()
    client = FakeClient(clock, readings=[_stats(load=6.0), _stats(load=6.5)])
    _run(tmp_path, clock, client)
    assert [c["at"] for c in client.calls] == [120.0, 240.0]          # the normal poll owns t=300
    assert all(c.get("_empty_retries") == 0 for c in client.calls)    # no hidden retry storm on a failing call
    assert clock.t == 300.0
    live = json.loads((tmp_path / fastpoll.LIVE_FILE).read_text())
    assert live["home_load_kw"] == 6.5


def test_it_returns_to_normal_pace_when_the_load_drops(tmp_path):
    clock = Clock()
    client = FakeClient(clock, readings=[_stats(load=0.5)])
    _run(tmp_path, clock, client)
    assert len(client.calls) == 1 and clock.t == 300.0                # one fast poll saw it drop, then quiet


def test_live_file_tells_the_dashboard_when_the_next_poll_is_due(tmp_path):
    clock = Clock()
    client = FakeClient(clock, readings=[_stats(load=0.5)])
    _run(tmp_path, clock, client)
    live = json.loads((tmp_path / fastpoll.LIVE_FILE).read_text())
    assert live["next_poll"] == (T0 + timedelta(seconds=300)).isoformat()       # quiet now: next is the normal poll


def test_two_errors_pause_fast_polling_and_alert_once(tmp_path):
    clock = Clock()
    client = FakeClient(clock, errors=[RuntimeError("boom"), RuntimeError("boom")])
    guard, alerts = _run(tmp_path, clock, client, deadline=700.0)
    assert len(client.calls) == 2 and len(alerts) == 1 and alerts[0][1] is False
    assert clock.t == 700.0 and guard.blocked_until is not None
    assert json.loads((tmp_path / fastpoll.STATE_FILE).read_text())["blocked_until"]


def test_a_lock_alerts_urgently_and_stops_for_good(tmp_path):
    clock = Clock()
    client = FakeClient(clock, errors=[ValueError("Account locked: x")])
    guard, alerts = _run(tmp_path, clock, client, deadline=900.0)
    assert len(client.calls) == 1 and guard.stopped and alerts[0][1] is True
    _, alerts2 = _run(tmp_path, Clock(), FakeClient(Clock()), guard=guard, deadline=900.0)
    assert alerts2 == []                                              # and stays quiet and idle afterwards


def test_hourly_budget_stops_polling_but_the_cycle_still_waits_it_out(tmp_path):
    clock = Clock()
    cfg = _cfg(fast_poll_max_per_hour=1)
    client = FakeClient(clock)
    _run(tmp_path, clock, client, cfg=cfg, deadline=900.0)
    assert len(client.calls) == 1 and clock.t == 900.0


def test_exact_interval_even_when_a_poll_takes_time(tmp_path):
    clock = Clock()

    class Slow(FakeClient):
        def get_stats(self, gateway, **kw):
            r = super().get_stats(gateway, **kw)
            clock.t += 7                                              # the call itself took 7 s
            return r

    _run(tmp_path, clock, Slow(clock), deadline=300.0)
    assert clock.t == 300.0                                           # never overshoots the normal poll


def test_keyboard_interrupt_is_not_swallowed(tmp_path):
    clock = Clock()
    client = FakeClient(clock, errors=[KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, clock, client)


# ── dashboard endpoints use the live overlay ─────────────────────────────────────────────────────

def _api(tmp_path, monkeypatch, load=3.9):
    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi
    from franklinwh_scraper.history import HistoryStore
    with HistoryStore(tmp_path / "history.db") as db:
        db.record(_stats(load=load, soc=60.0))
    monkeypatch.setattr(webapi, "_OUT", tmp_path, raising=False)
    monkeypatch.setattr(webapi, "_cfg", Config(dashboard_token=""), raising=False)
    return TestClient(webapi.app)


def test_current_is_unchanged_without_a_live_file(tmp_path, monkeypatch):
    body = _api(tmp_path, monkeypatch).get("/api/current").json()
    assert body["ok"] and body["home_load_kw"] == 3.9 and body["live"] is False


def test_current_shows_the_newer_live_reading_and_its_next_poll(tmp_path, monkeypatch):
    c = _api(tmp_path, monkeypatch)
    at = datetime.now() + timedelta(seconds=5)
    fastpoll.write_live(tmp_path, _stats(load=5.6, soc=58.0), at=at, next_at=at + timedelta(seconds=120))
    body = c.get("/api/current").json()
    assert body["live"] is True and body["home_load_kw"] == 5.6 and body["soc_pct"] == 58.0
    assert body["last_poll"] == at.isoformat() and body["next_poll"] == (at + timedelta(seconds=120)).isoformat()


def test_current_ignores_a_live_reading_older_than_the_stored_one(tmp_path, monkeypatch):
    c = _api(tmp_path, monkeypatch)
    old = datetime.now() - timedelta(minutes=2)
    fastpoll.write_live(tmp_path, _stats(load=9.9), at=old, next_at=old)
    body = c.get("/api/current").json()
    assert body["live"] is False and body["home_load_kw"] == 3.9


def test_glance_widget_uses_the_live_reading_too(tmp_path, monkeypatch):
    c = _api(tmp_path, monkeypatch)
    at = datetime.now() + timedelta(seconds=5)
    fastpoll.write_live(tmp_path, _stats(load=5.6), at=at, next_at=at)
    body = c.get("/api/glance").json()
    assert body["load_kw"] == 5.6 and "5.6kW" in body["text"]
