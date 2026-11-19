"""Tailscale watchdog: the widget, watch shortcut and Funnel hub all ride on the Mac mini staying
signed in as one node. Reads `tailscale status --json` (runner injected, so no test shells out)."""

from __future__ import annotations

import json
import subprocess
import types
from datetime import datetime, timedelta

from franklinwh_scraper import alerts, tailscale_watch
from franklinwh_scraper.tailscale_watch import TailscaleStatus

GOOD = {"BackendState": "Running", "Health": [],
        "Self": {"HostName": "Eric's Mini", "Online": True, "KeyExpiry": "2027-05-17T08:18:13Z"}}


def _runner(payload=None, returncode=0, raises=None, text=None):
    def run(cmd, **kw):
        if raises:
            raise raises
        return types.SimpleNamespace(returncode=returncode, stdout=text if text is not None else json.dumps(payload),
                                     stderr="")
    return run


def _read(**kw):
    return tailscale_watch.read_status(run=_runner(**kw), which=lambda _n: "/usr/bin/tailscale")


NOW = datetime(2026, 11, 18, 22, 0)


def _st(name="Eric's Mini", online=True, backend="Running", expiry=None):
    return TailscaleStatus(host_name=name, online=online, backend_state=backend, key_expiry=expiry, health=[])


# ---------------------------------------------------------------- reading

def test_read_status_parses_the_real_shape():
    s = _read(payload=GOOD)
    assert s.host_name == "Eric's Mini" and s.online is True and s.backend_state == "Running"
    assert s.key_expiry == datetime.fromisoformat("2027-05-17T08:18:13+00:00")


def test_disabled_key_expiry_is_none():
    for exp in (None, "0001-01-01T00:00:00Z"):
        p = json.loads(json.dumps(GOOD))
        p["Self"]["KeyExpiry"] = exp
        assert _read(payload=p).key_expiry is None


def test_no_binary_means_not_applicable():
    assert tailscale_watch.read_status(run=_runner(payload=GOOD), which=lambda _n: None, candidates=()) is None


def test_a_failing_or_garbled_status_is_unreachable_not_silent():
    for kw in ({"raises": subprocess.TimeoutExpired("tailscale", 5)}, {"returncode": 1, "payload": {}},
               {"text": "not json"}, {"payload": {"BackendState": "Running"}}):   # no Self
        s = _read(**kw)
        assert s.backend_state == "Unreachable" and s.online is False


# ---------------------------------------------------------------- health alert

def _tick(state, status, minutes):
    return alerts._alert_tailscale_health(state, "d", NOW + timedelta(minutes=minutes), status)


def test_healthy_node_is_quiet_and_remembers_its_name():
    state: dict = {}
    assert _tick(state, _st(), 0) is None
    assert state["tailscale_hostname"] == "Eric's Mini"


def test_not_applicable_is_quiet():
    assert alerts._alert_tailscale_health({}, "d", NOW, None) is None


def test_needs_two_bad_reads_before_alerting_then_only_once_per_incident():
    state = {"tailscale_hostname": "Eric's Mini"}
    down = _st(backend="Stopped", online=False)
    assert _tick(state, down, 0) is None                 # one blip: wait
    body = _tick(state, down, 5)
    assert body and "Stopped" in body and state["tailscale_health_force_loud"] is True
    assert _tick(state, down, 10) is None                # same incident: no repeat


def test_recovery_sends_one_quiet_note_and_rearms():
    state = {"tailscale_hostname": "Eric's Mini"}
    down = _st(online=False)
    _tick(state, down, 0)
    _tick(state, down, 5)
    state.pop("tailscale_health_force_loud", None)
    note = _tick(state, _st(), 10)
    assert note and "back" in note.lower() and not state.get("tailscale_health_force_loud")
    assert _tick(state, _st(), 15) is None
    _tick(state, down, 20)
    assert _tick(state, down, 25)                        # a new incident alerts again


def test_hostname_change_is_the_identity_mixup():
    """2026-11-18: the mini came back named erics-macbook-air after the Air signed in."""
    state = {"tailscale_hostname": "Eric's Mini"}
    flipped = _st(name="erics-macbook-air")
    assert _tick(state, flipped, 0) is None
    body = _tick(state, flipped, 5)
    assert body and "erics-macbook-air" in body and "Eric's Mini" in body
    assert state["tailscale_hostname"] == "Eric's Mini"  # baseline kept while it is wrong


def test_a_deliberate_rename_is_adopted_after_a_day():
    state = {"tailscale_hostname": "Eric's Mini"}
    renamed = _st(name="erics-mini")
    _tick(state, renamed, 0)
    _tick(state, renamed, 5)
    assert _tick(state, renamed, 24 * 60 + 10) is None   # stable for a day: new baseline, no alert
    assert state["tailscale_hostname"] == "erics-mini"


# ---------------------------------------------------------------- key expiry

EXPIRY = (NOW + timedelta(days=45)).astimezone()      # one fixed key; the clock moves toward it


def _key_tick(state, days_left):
    now = EXPIRY.replace(tzinfo=None) - timedelta(days=days_left)
    return alerts._alert_tailscale_key_expiry(state, "d", now, _st(expiry=EXPIRY))


def test_key_expiry_reminds_at_30_7_and_1_days_once_each():
    state: dict = {}
    assert _key_tick(state, 45) is None
    body = _key_tick(state, 29.5)
    assert body and "in 29 days" in body and "Disable key expiry" in body
    assert not state.get("tailscale_key_expiry_force_loud")            # the 30-day reminder is silent
    assert _key_tick(state, 28.5) is None
    assert _key_tick(state, 6.5) and state["tailscale_key_expiry_force_loud"] is True   # 7 days and closer buzz
    assert _key_tick(state, 6.0) is None
    assert "under a day" in _key_tick(state, 0.5)


def test_key_expiry_first_seen_close_sends_only_the_nearest_reminder():
    state: dict = {}
    assert _key_tick(state, 5)
    assert _key_tick(state, 4) is None and _key_tick(state, 0.9)       # 30 and 7 are spent; the 1-day one is still due


def test_renewed_key_starts_over_and_disabled_expiry_is_silent():
    state: dict = {}
    _key_tick(state, 5)
    renewed = _st(expiry=EXPIRY + timedelta(days=90))
    assert alerts._alert_tailscale_key_expiry(state, "d", EXPIRY.replace(tzinfo=None) - timedelta(days=5), renewed) is None
    assert state["tailscale_key_alerts"]["sent"] == []      # the renewed key's reminders start from scratch
    assert alerts._alert_tailscale_key_expiry(state, "d", NOW, _st(expiry=None)) is None
    assert alerts._alert_tailscale_key_expiry(state, "d", NOW, None) is None


def test_both_alerts_are_registered_candidates():
    names = alerts.known_alert_names()
    assert "tailscale_health" in names and "tailscale_key_expiry" in names


def test_cached_status_reads_once_per_window(monkeypatch):
    calls = []
    monkeypatch.setattr(tailscale_watch, "read_status", lambda **k: calls.append(1) or _st())
    tailscale_watch._cache.clear()
    tailscale_watch.cached_status(now=100.0)
    tailscale_watch.cached_status(now=130.0)
    tailscale_watch.cached_status(now=200.0)
    assert len(calls) == 2


def test_gui_app_bundle_is_never_used_as_the_cli():
    """Regression: under launchd's bare PATH the watchdog fell back to the Tailscale.app GUI binary,
    which refuses `status --json` ("GUI failed to start"), so every read looked Unreachable."""
    assert not any("Tailscale.app" in p for p in tailscale_watch._CANDIDATES)
    s = tailscale_watch.read_status(run=_runner(payload=GOOD), which=lambda _n: None,
                                    candidates=("/definitely/not/here",))
    assert s is None                                    # no real CLI found: not applicable, not an outage
