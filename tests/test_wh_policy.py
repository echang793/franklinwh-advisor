"""Unit tests for wh_policy — pure functions only, no network, no EcoNet."""

from __future__ import annotations

from datetime import datetime

from franklinwh_scraper.tou import TouPeriod
from franklinwh_scraper.wh_policy import (
    MAX_SETPOINT_F,
    MIN_SAFE_SETPOINT_F,
    WaterHeaterState,
    WhAction,
    WhInputs,
    WhMode,
    WhParams,
    clamp_setpoint,
    compute_wh_surplus_kw,
    decide,
)


def _heater(**kw) -> WaterHeaterState:
    defaults = dict(
        mode=WhMode.HEAT_PUMP, setpoint_f=120, running=False,
        fetched_at=datetime(2026, 7, 15, 12, 0),
    )
    defaults.update(kw)
    return WaterHeaterState(**defaults)


def _inputs(**kw) -> WhInputs:
    defaults = dict(
        now=datetime(2026, 7, 15, 12, 0),          # Wed noon, July
        tou_period=TouPeriod.OFF_PEAK,
        solar_kw=0.0, home_load_kw=1.0,
        wh_draw_kw=0.0, ev_draw_kw=0.0,
        fwh_battery_soc=90.0,
        heater=_heater(),
        away=False, override_active=False,
        minutes_since_last_command=120.0,
        high_surplus_ticks=0, low_surplus_ticks=0,
    )
    defaults.update(kw)
    return WhInputs(**defaults)


P = WhParams()


# ---------------------------------------------------------------- safety floor

def test_clamp_never_below_safe_floor():
    assert clamp_setpoint(100) == MIN_SAFE_SETPOINT_F
    assert clamp_setpoint(MIN_SAFE_SETPOINT_F) == MIN_SAFE_SETPOINT_F


def test_clamp_never_above_scald_ceiling():
    assert clamp_setpoint(160) == MAX_SETPOINT_F


def test_misconfigured_peak_setpoint_still_floored():
    # A config typo (110 F) must never reach the heater: Legionella risk.
    d = decide(_inputs(now=datetime(2026, 7, 15, 17, 0), tou_period=TouPeriod.ON_PEAK,
                       heater=_heater(mode=WhMode.ENERGY_SAVER, setpoint_f=130)),
               WhParams(peak_setpoint_f=110))
    assert d.action == WhAction.SET
    assert d.setpoint_f == MIN_SAFE_SETPOINT_F


def test_every_set_decision_respects_floor_across_a_day():
    params = WhParams(peak_setpoint_f=100, normal_setpoint_f=105, pre_peak_setpoint_f=90,
                      bank_setpoint_f=95)
    for hour in range(24):
        period = TouPeriod.ON_PEAK if 16 <= hour < 21 else (
            TouPeriod.SUPER_OFF_PEAK if hour < 6 else TouPeriod.OFF_PEAK)
        d = decide(_inputs(now=datetime(2026, 7, 15, hour, 0), tou_period=period,
                           solar_kw=6.0, high_surplus_ticks=5,
                           heater=_heater(setpoint_f=140, mode=WhMode.ELECTRIC)), params)
        if d.action == WhAction.SET and d.setpoint_f is not None:
            assert d.setpoint_f >= MIN_SAFE_SETPOINT_F, hour


# ---------------------------------------------------------------- heat-pump-only default

def test_default_normal_setpoint_is_rheem_factory_120():
    # 120 F is the Rheem factory default and the user's own setting.
    assert WhParams().normal_setpoint_f == 120


def test_default_normal_mode_is_heat_pump():
    # Heat pump only is the most efficient mode and the user's own choice;
    # Energy Saver would let the resistance element back in off-peak.
    assert WhParams().normal_mode == WhMode.HEAT_PUMP


def test_defaults_never_leave_heat_pump_over_a_day():
    # Walk a sunny day at 5-min ticks, applying every SET, from the user's
    # real starting point: heat pump only at 120 F.
    heater = _heater()
    high = low = 0
    for i in range(24 * 12):
        now = datetime(2026, 7, 15, 0, 0) + i * (datetime(2026, 1, 1, 0, 5) - datetime(2026, 1, 1))
        h = now.hour
        period = TouPeriod.ON_PEAK if 16 <= h < 21 else (
            TouPeriod.SUPER_OFF_PEAK if h < 6 else TouPeriod.OFF_PEAK)
        solar = 5.0 if 9 <= h < 16 else 0.0
        sunny = solar - 1.0 - 0.25 >= P.bank_start_kw
        high, low = (high + 1, 0) if sunny else (0, low + 1)
        d = decide(_inputs(now=now, tou_period=period, solar_kw=solar, home_load_kw=1.0,
                           heater=heater, high_surplus_ticks=high, low_surplus_ticks=low), P)
        if d.action == WhAction.SET:
            assert d.mode == WhMode.HEAT_PUMP, (now, d)
            heater = _heater(mode=d.mode, setpoint_f=d.setpoint_f)


def test_energy_saver_heater_returns_to_heat_pump_off_peak():
    d = decide(_inputs(now=datetime(2026, 7, 15, 22, 0),
                       heater=_heater(mode=WhMode.ENERGY_SAVER)), P)
    assert d.action == WhAction.SET and d.mode == WhMode.HEAT_PUMP


# ---------------------------------------------------------------- hard gates

def test_override_stands_down():
    d = decide(_inputs(override_active=True, tou_period=TouPeriod.ON_PEAK), P)
    assert d.action == WhAction.NONE


def test_no_heater_data_does_nothing():
    d = decide(_inputs(heater=None, tou_period=TouPeriod.ON_PEAK), P)
    assert d.action == WhAction.NONE


def test_away_sets_vacation():
    d = decide(_inputs(away=True), P)
    assert d.action == WhAction.SET
    assert d.mode == WhMode.VACATION


def test_away_already_vacation_is_noop():
    d = decide(_inputs(away=True, heater=_heater(mode=WhMode.VACATION)), P)
    assert d.action == WhAction.NONE


def test_back_from_away_restores_normal():
    d = decide(_inputs(heater=_heater(mode=WhMode.VACATION)), P)
    assert d.action == WhAction.SET
    assert d.mode == P.normal_mode
    assert d.setpoint_f == P.normal_setpoint_f


# ---------------------------------------------------------------- on-peak

def test_on_peak_forces_heat_pump_only():
    # Resistance element (~4.5 kW) must never fire 4-9 PM.
    d = decide(_inputs(now=datetime(2026, 7, 15, 16, 5), tou_period=TouPeriod.ON_PEAK,
                       heater=_heater(mode=WhMode.ENERGY_SAVER)), P)
    assert d.action == WhAction.SET
    assert d.mode == WhMode.HEAT_PUMP
    assert d.setpoint_f == P.peak_setpoint_f


def test_on_peak_overrides_manual_electric_mode():
    d = decide(_inputs(now=datetime(2026, 7, 15, 18, 0), tou_period=TouPeriod.ON_PEAK,
                       heater=_heater(mode=WhMode.ELECTRIC, setpoint_f=130)), P)
    assert d.mode == WhMode.HEAT_PUMP


def test_on_peak_ignores_command_rate_limit():
    d = decide(_inputs(now=datetime(2026, 7, 15, 16, 0), tou_period=TouPeriod.ON_PEAK,
                       minutes_since_last_command=1.0,
                       heater=_heater(mode=WhMode.ENERGY_SAVER)), P)
    assert d.action == WhAction.SET


def test_on_peak_already_safe_is_noop():
    d = decide(_inputs(now=datetime(2026, 7, 15, 17, 0), tou_period=TouPeriod.ON_PEAK,
                       heater=_heater(mode=WhMode.HEAT_PUMP, setpoint_f=P.peak_setpoint_f)), P)
    assert d.action == WhAction.NONE


# ---------------------------------------------------------------- pre-peak top-up

def test_pre_peak_tops_up_in_heat_pump_mode():
    d = decide(_inputs(now=datetime(2026, 7, 15, 15, 0)), P)
    assert d.action == WhAction.SET
    assert d.mode == WhMode.HEAT_PUMP
    assert d.setpoint_f == P.pre_peak_setpoint_f


def test_pre_peak_keeps_higher_banked_setpoint():
    d = decide(_inputs(now=datetime(2026, 7, 15, 15, 30),
                       heater=_heater(mode=WhMode.HEAT_PUMP, setpoint_f=P.bank_setpoint_f)), P)
    assert d.action == WhAction.NONE


# ---------------------------------------------------------------- solar banking

def test_surplus_adds_back_heater_and_ev_draw():
    # solar 5, house 4.0 of which heater 0.5 and EV 2.5 -> other 1.0
    # surplus = 5 - 1.0 - 0.25 = 3.75: heater outranks EV for solar.
    assert abs(compute_wh_surplus_kw(5.0, 4.0, 0.5, 2.5, 0.25) - 3.75) < 1e-9


def test_banks_heat_on_sustained_surplus():
    d = decide(_inputs(solar_kw=4.0, home_load_kw=1.0, high_surplus_ticks=2), P)
    assert d.action == WhAction.SET
    assert d.mode == WhMode.HEAT_PUMP
    assert d.setpoint_f == P.bank_setpoint_f


def test_no_bank_without_sustained_ticks():
    d = decide(_inputs(solar_kw=4.0, home_load_kw=1.0, high_surplus_ticks=1,
                       heater=_heater(mode=P.normal_mode, setpoint_f=P.normal_setpoint_f)), P)
    assert d.action == WhAction.NONE


def test_battery_first_blocks_banking():
    d = decide(_inputs(solar_kw=4.0, home_load_kw=1.0, high_surplus_ticks=5,
                       fwh_battery_soc=50.0), P)
    assert d.action == WhAction.NONE or d.setpoint_f != P.bank_setpoint_f


def test_banking_holds_through_brief_cloud():
    d = decide(_inputs(solar_kw=0.5, home_load_kw=1.0, low_surplus_ticks=1,
                       heater=_heater(mode=WhMode.HEAT_PUMP, setpoint_f=P.bank_setpoint_f)), P)
    assert d.action == WhAction.NONE


def test_banking_ends_on_sustained_low_surplus():
    d = decide(_inputs(solar_kw=0.5, home_load_kw=1.0, low_surplus_ticks=3,
                       heater=_heater(mode=WhMode.HEAT_PUMP, setpoint_f=P.bank_setpoint_f)), P)
    assert d.action == WhAction.SET
    assert d.mode == P.normal_mode
    assert d.setpoint_f == P.normal_setpoint_f


# ---------------------------------------------------------------- normal / flap control

def test_off_peak_restores_normal_from_raised_setpoint():
    d = decide(_inputs(now=datetime(2026, 7, 15, 21, 5),
                       heater=_heater(mode=WhMode.HEAT_PUMP, setpoint_f=P.pre_peak_setpoint_f)), P)
    assert d.action == WhAction.SET
    assert d.mode == P.normal_mode
    assert d.setpoint_f == P.normal_setpoint_f


def test_at_normal_is_noop():
    d = decide(_inputs(now=datetime(2026, 7, 15, 2, 0), tou_period=TouPeriod.SUPER_OFF_PEAK), P)
    assert d.action == WhAction.NONE


def test_non_peak_change_is_rate_limited():
    d = decide(_inputs(now=datetime(2026, 7, 15, 21, 5), minutes_since_last_command=5.0,
                       heater=_heater(mode=WhMode.HEAT_PUMP, setpoint_f=P.pre_peak_setpoint_f)), P)
    assert d.action == WhAction.NONE and "rate limited" in d.reason


def test_every_decision_has_a_reason():
    for kw in (dict(), dict(override_active=True), dict(heater=None), dict(away=True),
               dict(tou_period=TouPeriod.ON_PEAK, now=datetime(2026, 7, 15, 17, 0))):
        assert decide(_inputs(**kw), P).reason
