"""Unit tests for ev_policy — pure functions only, no network, no Tesla."""

from __future__ import annotations

from datetime import datetime

from franklinwh_scraper.ev_policy import (
    EvAction,
    EvDecision,
    EvInputs,
    EvParams,
    VehicleChargeState,
    amps_for_surplus,
    compute_surplus_kw,
    decide,
    ev_draw_kw,
)
from franklinwh_scraper.tou import TouPeriod


def _vehicle(**kw) -> VehicleChargeState:
    defaults = dict(
        plugged_in=True, charging=True, requested_amps=15,
        actual_current_a=15.0, charger_voltage=240.0,
        vehicle_soc_pct=60.0, charge_limit_pct=80.0,
        charger_max_amps=32, fast_charger=False,
        fetched_at=datetime(2026, 7, 15, 12, 0),
    )
    defaults.update(kw)
    return VehicleChargeState(**defaults)


def _inputs(**kw) -> EvInputs:
    defaults = dict(
        now=datetime(2026, 7, 15, 12, 0),          # Wed noon, July
        tou_period=TouPeriod.OFF_PEAK,
        solar_kw=6.0, home_load_kw=4.6,
        fwh_battery_soc=90.0, fwh_battery_kw=-0.5,
        vehicle=_vehicle(),
        at_home=True, session_active=True,
        last_commanded_amps=15, minutes_since_last_command=15.0,
        low_surplus_ticks=0, high_surplus_ticks=3,
        override_active=False,
    )
    defaults.update(kw)
    return EvInputs(**defaults)


# ---------------------------------------------------------------- surplus math

def test_surplus_adds_back_ev_draw():
    # solar 5, home_load 4.6 of which the EV itself is 3.6 -> real surplus
    # ~= 5 - 1.0 - 0.25 = 3.75, NOT 5 - 4.6 = 0.4 (or negative with reserve).
    assert abs(compute_surplus_kw(5.0, 4.6, 3.6, 0.25) - 3.75) < 1e-9


def test_surplus_stable_across_amp_raise():
    # Raising amps raises home_load by the same amount; computed surplus must
    # not move — this is the anti-oscillation property.
    before = compute_surplus_kw(6.0, 3.0, 1.2, 0.25)   # charging at 5 A
    after = compute_surplus_kw(6.0, 4.8, 3.0, 0.25)    # raised to 12.5 A
    assert abs(before - after) < 1e-9


def test_ev_draw_prefers_reported_then_fallback():
    v = _vehicle(actual_current_a=10.0, charger_voltage=238.0)
    assert abs(ev_draw_kw(v, 32) - 2.38) < 1e-9        # reported wins
    assert abs(ev_draw_kw(None, 10) - 2.4) < 1e-9      # commanded fallback
    idle = _vehicle(charging=False, actual_current_a=0.0)
    assert ev_draw_kw(idle, 10) == 0.0                 # idle car draws nothing


def test_amps_quantize_floor_and_clamp():
    assert amps_for_surplus(3.1, 32) == 12   # floor(12.9)
    assert amps_for_surplus(0.9, 32) == 0    # below 5 A minimum
    assert amps_for_surplus(20.0, 32) == 32  # clamped to max
    assert amps_for_surplus(1.2, 32) == 5    # exactly the floor


# ---------------------------------------------------------------- hard gates

def test_override_blocks_all_commands():
    d = decide(_inputs(override_active=True), EvParams())
    assert d.action is EvAction.NONE
    assert "override" in d.reason


def test_no_vehicle_data_none():
    assert decide(_inputs(vehicle=None), EvParams()).action is EvAction.NONE


def test_away_from_home_none():
    assert decide(_inputs(at_home=False), EvParams()).action is EvAction.NONE
    fast = _inputs(vehicle=_vehicle(fast_charger=True))
    assert decide(fast, EvParams()).action is EvAction.NONE


# ---------------------------------------------------------------- on-peak

def test_on_peak_stops_managed_charging():
    d = decide(_inputs(tou_period=TouPeriod.ON_PEAK,
                       now=datetime(2026, 7, 15, 17, 0)), EvParams())
    assert d.action is EvAction.STOP


def test_on_peak_leaves_manual_session_alone():
    d = decide(_inputs(tou_period=TouPeriod.ON_PEAK, session_active=False,
                       now=datetime(2026, 7, 15, 17, 0)), EvParams())
    assert d.action is EvAction.NONE


# ---------------------------------------------------------------- overnight

def test_overnight_fallback_starts_max_amps():
    d = decide(_inputs(
        tou_period=TouPeriod.SUPER_OFF_PEAK, solar_kw=0.0,
        now=datetime(2026, 7, 15, 0, 30),
        vehicle=_vehicle(charging=False, vehicle_soc_pct=60.0),
        session_active=False,
    ), EvParams(max_amps=32))
    assert d.action is EvAction.START
    assert d.amps == 32


def test_overnight_at_limit_none():
    d = decide(_inputs(
        tou_period=TouPeriod.SUPER_OFF_PEAK, solar_kw=0.0,
        now=datetime(2026, 7, 15, 1, 0),
        vehicle=_vehicle(charging=False, vehicle_soc_pct=80.0),
        session_active=False,
    ), EvParams())
    assert d.action is EvAction.NONE


def test_overnight_restores_full_speed_if_throttled():
    d = decide(_inputs(
        tou_period=TouPeriod.SUPER_OFF_PEAK, solar_kw=0.0,
        now=datetime(2026, 7, 15, 2, 0),
        vehicle=_vehicle(charging=True, requested_amps=5),
    ), EvParams(max_amps=32))
    assert d.action is EvAction.SET_AMPS
    assert d.amps == 32


def test_overnight_respects_charger_max():
    d = decide(_inputs(
        tou_period=TouPeriod.SUPER_OFF_PEAK, solar_kw=0.0,
        now=datetime(2026, 7, 15, 2, 0),
        vehicle=_vehicle(charging=False, charger_max_amps=24),
        session_active=False,
    ), EvParams(max_amps=32))
    assert d.amps == 24


def test_midday_super_off_peak_not_force_charged():
    # Weekday 10-14 is also SUPER_OFF_PEAK; must surplus-track, not max-charge.
    d = decide(_inputs(
        tou_period=TouPeriod.SUPER_OFF_PEAK,
        now=datetime(2026, 7, 15, 11, 0),
        solar_kw=6.0, home_load_kw=4.6,
    ), EvParams())
    assert d.action in (EvAction.NONE, EvAction.SET_AMPS)
    assert "overnight" not in d.reason


# ---------------------------------------------------------------- battery-first

def test_battery_first_blocks_surplus():
    d = decide(_inputs(fwh_battery_soc=70.0,
                       vehicle=_vehicle(charging=False), session_active=False),
               EvParams(battery_first_soc=80.0))
    assert d.action is EvAction.NONE
    assert "battery first" in d.reason


def test_battery_first_stops_managed_session():
    d = decide(_inputs(fwh_battery_soc=70.0), EvParams(battery_first_soc=80.0))
    assert d.action is EvAction.STOP


# ---------------------------------------------------------------- tracking

def test_start_requires_sustained_surplus():
    idle = _inputs(vehicle=_vehicle(charging=False, actual_current_a=0.0),
                   session_active=False, last_commanded_amps=None,
                   home_load_kw=1.0)
    low = decide(
        _inputs(vehicle=_vehicle(charging=False, actual_current_a=0.0),
                session_active=False, last_commanded_amps=None,
                home_load_kw=1.0, high_surplus_ticks=1),
        EvParams())
    assert low.action is EvAction.NONE
    ok = decide(idle, EvParams())
    assert ok.action is EvAction.START
    assert ok.amps == amps_for_surplus(
        compute_surplus_kw(6.0, 1.0, 0.0, 0.25), 32)


def test_stop_requires_sustained_low_surplus():
    two = decide(_inputs(solar_kw=1.0, low_surplus_ticks=2), EvParams())
    assert two.action is not EvAction.STOP
    three = decide(_inputs(solar_kw=1.0, low_surplus_ticks=3), EvParams())
    assert three.action is EvAction.STOP


def test_restart_threshold_above_stop():
    # After a stop, 1.3 kW surplus (below start's 1.7) must not restart even
    # though it is above the stop threshold — hysteresis dead zone.
    d = decide(_inputs(
        solar_kw=2.5, home_load_kw=1.2,   # surplus ~1.05 -> 4 A -> target 0
        vehicle=_vehicle(charging=False, actual_current_a=0.0),
        session_active=False, last_commanded_amps=None,
        high_surplus_ticks=0,
    ), EvParams())
    assert d.action is EvAction.NONE


def test_deadband_blocks_small_changes():
    # EV at 15 A (3.6 kW) + house 3.6 kW. solar 7.75 -> surplus
    # 7.75 - 3.6 - 0.25 = 3.9 -> 16 A target; |delta| 1 < 3 -> NONE.
    d = decide(_inputs(solar_kw=7.75, home_load_kw=7.2), EvParams())
    assert d.action is EvAction.NONE
    assert "deadband" in d.reason


def test_dwell_blocks_rapid_changes():
    d = decide(_inputs(solar_kw=9.0, minutes_since_last_command=4.0),
               EvParams())
    assert d.action is EvAction.NONE


def test_tracks_surplus_when_delta_and_dwell_ok():
    # EV at 15 A (3.6 kW). solar 9, home 4.6 -> surplus 9-1.0-0.25=7.75 -> 32 A.
    d = decide(_inputs(solar_kw=9.0), EvParams())
    assert d.action is EvAction.SET_AMPS
    assert d.amps == 32


def test_manual_daytime_session_untouched():
    d = decide(_inputs(session_active=False), EvParams())
    assert d.action is EvAction.NONE
    assert "manual" in d.reason


def test_evening_hands_off():
    d = decide(_inputs(
        tou_period=TouPeriod.OFF_PEAK, solar_kw=0.0,
        now=datetime(2026, 7, 15, 22, 0),
    ), EvParams())
    assert d.action is EvAction.NONE
    assert "outside" in d.reason


def test_controller_dry_run_tick_end_to_end(tmp_path, monkeypatch):
    """Full tick() with a stubbed Tesla client: no commands in dry run,
    state file and decision log written, nothing raises."""
    from types import SimpleNamespace

    from franklinwh_scraper import ev_controller as evc
    from franklinwh_scraper.config import Config

    calls = []

    class StubClient:
        def __init__(self, *a, **kw):
            pass

        def get_charge_state(self):
            calls.append("poll")
            return _vehicle(charging=False, actual_current_a=0.0)

        def set_charging_amps(self, amps):
            calls.append(("amps", amps))

        def charge_start(self):
            calls.append("start")

        def charge_stop(self):
            calls.append("stop")

    monkeypatch.setattr(evc, "TeslaClient", StubClient)
    cfg = Config(ev_control_enabled=True, ev_dry_run=True,
                 tesla_vin="5YJ3TEST", tesla_client_id="cid")
    ctl = evc.EvController(cfg, tmp_path)

    stats = SimpleNamespace(current=SimpleNamespace(
        solar_production_kw=6.0, home_load_kw=1.5, grid_use_kw=-2.0,
        battery_use_kw=-0.5, battery_soc_pct=90.0, grid_status="normal"))
    now = datetime(2026, 7, 15, 12, 0)
    ctl.tick(stats, now)
    ctl.tick(stats, datetime(2026, 7, 15, 12, 5))

    # Dry run: polls are allowed (metered), commands never sent.
    assert all(c == "poll" for c in calls)
    assert (tmp_path / ".ev_controller_state.json").exists()
    state = ctl.state
    assert state["high_surplus_ticks"] >= 2      # surplus ~4.25 kW sustained
    assert state.get("last_commanded_amps") is None


def test_controller_spend_meter_prices_calls(tmp_path, monkeypatch):
    from franklinwh_scraper import ev_controller as evc
    from franklinwh_scraper.config import Config

    monkeypatch.setattr(evc, "TeslaClient",
                        lambda *a, **kw: SimpleNamespaceClient())

    class SimpleNamespaceClient:
        pass

    cfg = Config(ev_control_enabled=True, tesla_vin="v", tesla_client_id="c")
    ctl = evc.EvController(cfg, tmp_path)
    for _ in range(10):
        ctl._record_spend("data")
    ctl._record_spend("wake")
    # 10 x $0.002 + 1 x $0.02 = $0.04
    assert abs(ctl.month_spend_usd() - 0.04) < 1e-9


def test_decision_is_frozen_dataclass():
    d = EvDecision(EvAction.NONE)
    try:
        d.amps = 5
        raised = False
    except AttributeError:
        raised = True
    assert raised
