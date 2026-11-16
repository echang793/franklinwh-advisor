"""Unit tests for wh_controller — a fake EcoNet client, tmp output dir."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from franklinwh_scraper.config import Config
from franklinwh_scraper.econet import EcoNetError, EcoNetRateLimited, HeaterReading
from franklinwh_scraper.wh_controller import (
    WhController,
    ev_draw_from_state,
    estimate_wh_kw,
    params_from_config,
    read_away,
)
from franklinwh_scraper.wh_policy import WaterHeaterState, WhAction, WhMode


@dataclass
class _Current:
    solar_production_kw: float = 0.0
    home_load_kw: float = 1.0
    battery_soc_pct: float = 90.0


@dataclass
class _Stats:
    current: _Current


class FakeClient:
    def __init__(self, mode=WhMode.HEAT_PUMP, setpoint=125, running=False,
                 running_state="", fail: Exception | None = None):
        self.mode, self.setpoint, self.running = mode, setpoint, running
        self.running_state = running_state
        self.fail = fail
        self.applied: list[tuple] = []
        self.apply_fail: Exception | None = None

    reads = 0

    def read_state(self, now):
        self.reads += 1
        if self.fail:
            raise self.fail
        return HeaterReading(
            state=WaterHeaterState(mode=self.mode, setpoint_f=self.setpoint,
                                   running=self.running, fetched_at=now),
            running_state=self.running_state, tank_pct=100)

    def apply(self, mode, setpoint):
        if self.apply_fail:
            raise self.apply_fail
        self.applied.append((mode, setpoint))
        self.mode = mode
        if setpoint is not None:
            self.setpoint = setpoint


def _cfg(**kw) -> Config:
    c = Config()
    c.wh_control_enabled = True
    c.wh_dry_run = True
    for k, v in kw.items():
        setattr(c, k, v)
    return c


PEAK = datetime(2026, 11, 16, 17, 0)      # Monday 5 PM
NIGHT = datetime(2026, 11, 16, 2, 0)


def _stats(**kw):
    return _Stats(_Current(**kw))


def _log(tmp_path):
    p = tmp_path / "wh_controller.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ---------------------------------------------------------------- helpers

def test_estimate_draw():
    st = lambda m, r: WaterHeaterState(mode=m, setpoint_f=125, running=r, fetched_at=NIGHT)  # noqa: E731
    assert estimate_wh_kw(None) == 0.0
    assert estimate_wh_kw(st(WhMode.HEAT_PUMP, False)) == 0.0
    assert estimate_wh_kw(st(WhMode.HEAT_PUMP, True)) == 0.5
    assert estimate_wh_kw(st(WhMode.ELECTRIC, True)) == 4.5


def test_ev_draw_from_state(tmp_path):
    assert ev_draw_from_state(tmp_path) == 0.0
    (tmp_path / ".ev_controller_state.json").write_text(
        json.dumps({"session": "solar", "last_commanded_amps": 10}))
    assert abs(ev_draw_from_state(tmp_path) - 2.4) < 1e-9
    (tmp_path / ".ev_controller_state.json").write_text(
        json.dumps({"session": "none", "last_commanded_amps": 10}))
    assert ev_draw_from_state(tmp_path) == 0.0


def test_read_away(tmp_path):
    assert read_away(tmp_path) is False
    (tmp_path / ".peak_alert_state.json").write_text(json.dumps({"away_since": "2026-11-10"}))
    assert read_away(tmp_path) is True
    (tmp_path / ".peak_alert_state.json").write_text("{corrupt")
    assert read_away(tmp_path) is False


def test_params_from_config():
    p = params_from_config(_cfg(wh_peak_setpoint_f=122, wh_normal_mode="energy_saver"))
    assert p.peak_setpoint_f == 122
    assert p.normal_mode == WhMode.ENERGY_SAVER      # explicit opt-in still honored


def test_config_default_is_heat_pump():
    assert Config().wh_normal_mode == "heat_pump"
    assert params_from_config(Config()).normal_mode == WhMode.HEAT_PUMP
    assert Config().wh_normal_setpoint_f == 120
    assert params_from_config(Config()).normal_setpoint_f == 120


def test_params_bad_mode_falls_back():
    assert params_from_config(_cfg(wh_normal_mode="turbo")).normal_mode == WhMode.HEAT_PUMP


# ---------------------------------------------------------------- dry run

def test_dry_run_never_applies(tmp_path):
    fc = FakeClient()
    ctl = WhController(_cfg(), tmp_path, client=fc)
    d = ctl.tick(_stats(), PEAK)
    assert d.action == WhAction.SET and d.mode == WhMode.HEAT_PUMP
    assert fc.applied == []
    entry = _log(tmp_path)[-1]
    assert entry["dry_run"] is True and entry["action"] == "set"
    assert entry["heater_mode"] == "heat_pump"


def test_dry_run_log_deduplicates_repeat_decisions(tmp_path):
    ctl = WhController(_cfg(), tmp_path, client=FakeClient())
    for i in range(5):
        ctl.tick(_stats(), PEAK + timedelta(minutes=5 * i))
    assert len(_log(tmp_path)) == 1


def test_log_records_running_state_changes(tmp_path):
    fc = FakeClient()
    ctl = WhController(_cfg(), tmp_path, client=fc)
    ctl.tick(_stats(), PEAK)
    fc.running, fc.running_state = True, "Electric Running"
    ctl.tick(_stats(), PEAK + timedelta(minutes=5))
    log = _log(tmp_path)
    assert len(log) == 2 and log[-1]["running_state"] == "Electric Running"


def test_log_heartbeat_hourly(tmp_path):
    ctl = WhController(_cfg(), tmp_path, client=FakeClient())
    for i in range(13):
        ctl.tick(_stats(), NIGHT + timedelta(minutes=5 * i))
    assert len(_log(tmp_path)) == 2


# ---------------------------------------------------------------- live

def test_live_applies_and_rate_limits(tmp_path):
    fc = FakeClient()
    ctl = WhController(_cfg(wh_dry_run=False), tmp_path, client=fc)
    ctl.tick(_stats(), PEAK)
    assert fc.applied == [(WhMode.HEAT_PUMP, 120)]
    ctl.tick(_stats(), PEAK + timedelta(minutes=5))
    assert len(fc.applied) == 1          # already at target


def test_state_survives_restart(tmp_path):
    fc = FakeClient()
    WhController(_cfg(wh_dry_run=False), tmp_path, client=fc).tick(_stats(), PEAK)
    ctl2 = WhController(_cfg(wh_dry_run=False), tmp_path, client=fc)
    assert ctl2.state["last_command"]["mode"] == "heat_pump"


def test_manual_change_starts_override(tmp_path):
    fc = FakeClient()
    ctl = WhController(_cfg(wh_dry_run=False), tmp_path, client=fc)
    ctl.tick(_stats(), PEAK)
    fc.mode, fc.setpoint = WhMode.HIGH_DEMAND, 140           # user changed it in the app
    d = ctl.tick(_stats(), PEAK + timedelta(minutes=15))
    assert d.action == WhAction.NONE and "override" in d.reason
    assert len(fc.applied) == 1
    # Override expires after the stand-down window.
    d = ctl.tick(_stats(), PEAK + timedelta(hours=5))
    assert d.action == WhAction.SET or "override" not in d.reason


def test_no_override_while_command_propagates(tmp_path):
    fc = FakeClient()
    ctl = WhController(_cfg(wh_dry_run=False), tmp_path, client=fc)
    ctl.tick(_stats(), PEAK)
    fc.setpoint = 125                          # cloud hasn't caught up yet
    ctl.tick(_stats(), PEAK + timedelta(minutes=5))
    assert "override_until_iso" not in ctl.state


def test_apply_failure_degrades_quietly(tmp_path):
    fc = FakeClient()
    fc.apply_fail = EcoNetError("mqtt down")
    ctl = WhController(_cfg(wh_dry_run=False), tmp_path, client=fc)
    ctl.tick(_stats(), PEAK)                 # must not raise
    assert "last_command" not in ctl.state
    assert _log(tmp_path)[-1]["error"]


# ---------------------------------------------------------------- failure modes

def test_poll_failure_is_no_action(tmp_path):
    fc = FakeClient(fail=EcoNetError("cloud down"))
    ctl = WhController(_cfg(wh_dry_run=False), tmp_path, client=fc)
    d = ctl.tick(_stats(), PEAK)
    assert d.action == WhAction.NONE
    assert fc.applied == []


def test_sustained_poll_failure_alerts_once(tmp_path):
    sent = []
    fc = FakeClient(fail=EcoNetError("cloud down"))
    ctl = WhController(_cfg(), tmp_path, client=fc, notify=sent.append)
    for i in range(10):
        ctl.tick(_stats(), PEAK + timedelta(minutes=5 * i))
    assert len(sent) == 1
    fc.fail = None
    ctl.tick(_stats(), PEAK + timedelta(minutes=55))
    assert ctl.state.get("consec_errors", 0) == 0


def test_surplus_ticks_drive_banking(tmp_path):
    noon = datetime(2026, 11, 16, 12, 0)
    fc = FakeClient(setpoint=120)                  # already at the normal setpoint
    ctl = WhController(_cfg(wh_dry_run=False), tmp_path, client=fc)
    sunny = _stats(solar_production_kw=4.0, home_load_kw=1.0)
    ctl.tick(sunny, noon)
    assert fc.applied == []                  # one tick isn't sustained
    ctl.tick(sunny, noon + timedelta(minutes=5))
    assert fc.applied == [(WhMode.HEAT_PUMP, 135)]


def test_away_flag_reaches_policy(tmp_path):
    (tmp_path / ".peak_alert_state.json").write_text(json.dumps({"away_since": "2026-11-10"}))
    fc = FakeClient()
    ctl = WhController(_cfg(wh_dry_run=False), tmp_path, client=fc)
    ctl.tick(_stats(), NIGHT)
    assert fc.applied == [(WhMode.VACATION, None)]


# ---------------------------------------------------------------- rate limits (HTTP 429)

def test_rate_limit_pauses_polling_then_retries(tmp_path):
    fc = FakeClient(fail=EcoNetRateLimited("login"))
    ctl = WhController(_cfg(), tmp_path, client=fc)
    d = ctl.tick(_stats(), PEAK)
    assert fc.reads == 1 and d.action == WhAction.NONE
    for m in (5, 10, 25):                        # inside the 30-min backoff: no EcoNet calls
        ctl.tick(_stats(), PEAK + timedelta(minutes=m))
    assert fc.reads == 1
    ctl.tick(_stats(), PEAK + timedelta(minutes=31))
    assert fc.reads == 2


def test_repeated_rate_limit_doubles_backoff_up_to_cap(tmp_path):
    fc = FakeClient(fail=EcoNetRateLimited("login"))
    ctl = WhController(_cfg(), tmp_path, client=fc)
    t = PEAK
    waits = []
    for _ in range(5):
        ctl.tick(_stats(), t)
        until = datetime.fromisoformat(ctl.state["backoff_until_iso"])
        waits.append(round((until - t).total_seconds() / 60))
        t = until + timedelta(minutes=1)
    assert waits == [30, 60, 120, 240, 240]


def test_success_clears_backoff(tmp_path):
    fc = FakeClient(fail=EcoNetRateLimited("login"))
    ctl = WhController(_cfg(), tmp_path, client=fc)
    ctl.tick(_stats(), PEAK)
    fc.fail = None
    ctl.tick(_stats(), PEAK + timedelta(minutes=31))
    assert "backoff_until_iso" not in ctl.state and "backoff_min" not in ctl.state


def test_backoff_is_logged(tmp_path):
    ctl = WhController(_cfg(), tmp_path, client=FakeClient(fail=EcoNetRateLimited("login")))
    ctl.tick(_stats(), PEAK)
    assert "429" in _log(tmp_path)[-1]["error"] or "rate" in _log(tmp_path)[-1]["error"].lower()


# ---------------------------------------------------------------- daily energy collection

class EnergyClient(FakeClient):
    def __init__(self, energy=None, energy_fail=None, **kw):
        super().__init__(**kw)
        self.energy = {14: 0.5} if energy is None else energy
        self.energy_fail = energy_fail
        self.energy_calls: list = []

    def energy_for_day(self, day):
        self.energy_calls.append(day)
        if self.energy_fail:
            raise self.energy_fail
        return self.energy


def _energy(tmp_path):
    from franklinwh_scraper.wh_whatif import load_energy
    return load_energy(tmp_path)


def test_collects_yesterday_first_then_backfills_one_per_tick(tmp_path):
    from datetime import date
    fc = EnergyClient()
    ctl = WhController(_cfg(), tmp_path, client=fc)
    t = datetime(2026, 11, 16, 2, 0)
    ctl.tick(_stats(), t)
    ctl.tick(_stats(), t + timedelta(minutes=5))
    assert fc.energy_calls == [date(2026, 11, 15), date(2026, 11, 14)]
    assert set(_energy(tmp_path)) == {"2026-11-15", "2026-11-14"}


def test_backfill_stops_at_lookback(tmp_path):
    fc = EnergyClient()
    ctl = WhController(_cfg(), tmp_path, client=fc)
    t = datetime(2026, 11, 16, 2, 0)
    for i in range(50):
        ctl.tick(_stats(), t + timedelta(minutes=5 * i))
    assert len(fc.energy_calls) == 35


def test_no_collection_before_1am(tmp_path):
    fc = EnergyClient()
    ctl = WhController(_cfg(), tmp_path, client=fc)
    ctl.tick(_stats(), datetime(2026, 11, 16, 0, 30))
    assert fc.energy_calls == []


def test_unsupported_day_not_retried(tmp_path):
    from datetime import date
    fc = EnergyClient()
    fc.energy = None
    ctl = WhController(_cfg(), tmp_path, client=fc)
    t = datetime(2026, 11, 16, 2, 0)
    ctl.tick(_stats(), t)
    ctl.tick(_stats(), t + timedelta(minutes=5))
    assert fc.energy_calls == [date(2026, 11, 15), date(2026, 11, 14)]


def test_energy_error_retried_later_and_tick_survives(tmp_path):
    from datetime import date
    fc = EnergyClient(energy_fail=EcoNetError("boom"))
    ctl = WhController(_cfg(), tmp_path, client=fc)
    t = datetime(2026, 11, 16, 2, 0)
    ctl.tick(_stats(), t)
    fc.energy_fail = None
    ctl.tick(_stats(), t + timedelta(minutes=5))
    assert fc.energy_calls == [date(2026, 11, 15), date(2026, 11, 15)]


def test_energy_rate_limit_starts_backoff(tmp_path):
    fc = EnergyClient(energy_fail=EcoNetRateLimited("energy report"))
    ctl = WhController(_cfg(), tmp_path, client=fc)
    t = datetime(2026, 11, 16, 2, 0)
    ctl.tick(_stats(), t)
    ctl.tick(_stats(), t + timedelta(minutes=5))
    assert len(fc.energy_calls) == 1 and fc.reads == 1
    assert "backoff_until_iso" in ctl.state
