"""Pure decision logic for closed-loop EV solar charging.

Zero I/O and zero network: everything here is a function of its inputs, so
the control policy is unit-testable without a Tesla, a FranklinWH gateway,
or a clock. The controller (ev_controller.py) gathers inputs, calls
decide(), and executes the returned action via tesla.py.

Behavior (user-confirmed):
- Daytime: charge speed tracks the solar surplus left over after the house
  and (until it reaches a healthy SoC) the FranklinWH battery are served.
- Overnight super-off-peak: charge at max amps until the car's own charge
  limit — the car is always ready by morning.
- On-peak 16-21: never charge.
- Everything else (evening off-peak, manual sessions we didn't start):
  hands off.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .tou import TouPeriod

_EV_VOLTS = 240.0        # ChargePoint Home Flex on a 240 V circuit
_MIN_AMPS = 5            # Tesla API floor (~1.2 kW); below this we STOP instead
# 1 A quantization tracks the surplus tightly (a 5 A step would strand up to
# 1.2 kW of solar); flap protection comes from the delta threshold + dwell
# time below, not from coarse steps.
_MIN_AMP_DELTA = 3       # A — don't command changes smaller than this
_START_SURPLUS_KW = 1.7  # ~7 A: restart threshold, deliberately above stop
_STOP_SURPLUS_KW = 1.0   # below the sustainable 5 A minimum
_START_TICKS = 2         # sustained ticks (10 min @ 5-min polls) before START
_STOP_TICKS = 3          # one ~10-min cloud transit shouldn't kill a session
_MIN_COMMAND_INTERVAL_MIN = 10.0  # >= 2 ticks between amp commands
_RESERVE_KW = 0.25       # headroom so quantization rounding never imports
_OVERNIGHT_END_HOUR = 6  # weekday super-off-peak ends 06:00; the 10-14
                         # midday SOP block stays surplus-tracked instead
_SOLAR_MIN_KW = 0.1      # below this there is no "daytime" to track


class EvAction(str, Enum):
    NONE = "none"
    SET_AMPS = "set_amps"
    START = "start"          # charge_start + set amps
    STOP = "stop"            # charge_stop + restore max amps
    RESTORE_MAX = "restore_max"


@dataclass(frozen=True)
class VehicleChargeState:
    """Snapshot of the car's charge state, mapped from a Fleet API poll."""
    plugged_in: bool         # charging_state != "Disconnected"
    charging: bool           # charging_state == "Charging"
    requested_amps: int      # charge_current_request
    actual_current_a: float  # charger_actual_current
    charger_voltage: float   # charger_voltage (0 when not charging)
    vehicle_soc_pct: float   # battery_level
    charge_limit_pct: float  # charge_limit_soc
    charger_max_amps: int    # charge_current_request_max
    fast_charger: bool       # fast_charger_present -> definitely not home
    fetched_at: datetime


@dataclass(frozen=True)
class EvParams:
    """Config-derived knobs; trivial to construct in tests."""
    max_amps: int = 32
    battery_first_soc: float = 80.0  # FWH battery tops up before EV gets surplus
    reserve_kw: float = _RESERVE_KW


@dataclass(frozen=True)
class EvInputs:
    now: datetime
    tou_period: TouPeriod
    solar_kw: float
    home_load_kw: float          # INCLUDES the EV's own draw
    fwh_battery_soc: float
    fwh_battery_kw: float        # negative = charging
    vehicle: VehicleChargeState | None  # None = not polled / asleep this tick
    at_home: bool
    session_active: bool         # a session WE started is in progress
    last_commanded_amps: int | None
    minutes_since_last_command: float
    low_surplus_ticks: int       # consecutive ticks below _STOP_SURPLUS_KW
    high_surplus_ticks: int      # consecutive ticks above _START_SURPLUS_KW
    override_active: bool


@dataclass(frozen=True)
class EvDecision:
    action: EvAction
    amps: int | None = None
    reason: str = ""


def ev_draw_kw(vehicle: VehicleChargeState | None,
               last_commanded_amps: int | None) -> float:
    """The EV's own current draw in kW.

    Prefer the Tesla-reported actual current x voltage; between vehicle
    polls fall back to what we last commanded (at nominal 240 V). Zero when
    we have no evidence the car is drawing anything.
    """
    if vehicle is not None and vehicle.charging:
        volts = vehicle.charger_voltage or _EV_VOLTS
        return vehicle.actual_current_a * volts / 1000.0
    if vehicle is None and last_commanded_amps:
        return last_commanded_amps * _EV_VOLTS / 1000.0
    return 0.0


def compute_surplus_kw(solar_kw: float, home_load_kw: float,
                       ev_kw: float, reserve_kw: float) -> float:
    """Solar left over for the EV after the rest of the house.

    home_load_kw INCLUDES the EV's own draw, so it must be added back —
    otherwise raising amps raises home_load, which lowers the apparent
    surplus next tick, and the loop oscillates against itself.
    """
    other_load_kw = max(0.0, home_load_kw - ev_kw)
    return solar_kw - other_load_kw - reserve_kw


def amps_for_surplus(surplus_kw: float, max_amps: int) -> int:
    """Quantize a surplus to whole amps: floor, clamp [MIN, max]; 0 below MIN."""
    amps = int(surplus_kw * 1000.0 / _EV_VOLTS)
    if amps < _MIN_AMPS:
        return 0
    return min(amps, max_amps)


def decide(inp: EvInputs, p: EvParams) -> EvDecision:  # noqa: C901
    """One control decision per advisor tick. Pure — no I/O, no clock reads."""
    v = inp.vehicle

    # 1. Hard gates: respect the human, never act blind or away from home.
    if inp.override_active:
        return EvDecision(EvAction.NONE, reason="manual override standdown")
    if v is None:
        return EvDecision(EvAction.NONE, reason="no vehicle data this tick")
    if not inp.at_home or v.fast_charger:
        return EvDecision(EvAction.NONE, reason="vehicle not charging at home")

    max_amps = min(p.max_amps, v.charger_max_amps or p.max_amps)

    # 2. On-peak (16-21): never charge. Stop only sessions we own — a manual
    #    on-peak charge is the user's deliberate choice.
    if inp.tou_period == TouPeriod.ON_PEAK:
        if v.charging and inp.session_active:
            return EvDecision(EvAction.STOP, reason="on-peak — stop charging")
        return EvDecision(EvAction.NONE, reason="on-peak — hands off")

    # 3. Overnight fallback: guaranteed full-speed charge in the cheap window.
    #    hour < 6 excludes the 10-14 midday super-off-peak block, which stays
    #    surplus-tracked (the car already got its guaranteed charge overnight).
    if (inp.tou_period == TouPeriod.SUPER_OFF_PEAK
            and inp.now.hour < _OVERNIGHT_END_HOUR):
        if v.plugged_in and v.vehicle_soc_pct < v.charge_limit_pct:
            if not v.charging:
                return EvDecision(EvAction.START, amps=max_amps,
                                  reason="overnight super-off-peak fallback")
            if v.requested_amps < max_amps:
                return EvDecision(EvAction.SET_AMPS, amps=max_amps,
                                  reason="overnight — restore full speed")
        return EvDecision(EvAction.NONE, reason="overnight — at limit or unplugged")

    # 4. Daytime surplus tracking.
    if inp.solar_kw > _SOLAR_MIN_KW:
        if inp.fwh_battery_soc < p.battery_first_soc:
            if v.charging and inp.session_active:
                return EvDecision(
                    EvAction.STOP,
                    reason=f"battery first — FWH at {inp.fwh_battery_soc:.0f}%"
                           f" < {p.battery_first_soc:.0f}%")
            return EvDecision(EvAction.NONE, reason="battery first — EV waits")

        ev_kw = ev_draw_kw(v, inp.last_commanded_amps)
        surplus = compute_surplus_kw(inp.solar_kw, inp.home_load_kw,
                                     ev_kw, p.reserve_kw)
        target = amps_for_surplus(surplus, max_amps)

        if not v.charging:
            if (v.plugged_in and target > 0
                    and inp.high_surplus_ticks >= _START_TICKS):
                return EvDecision(EvAction.START, amps=target,
                                  reason=f"solar surplus {surplus:.1f} kW sustained")
            return EvDecision(EvAction.NONE, reason="waiting for sustained surplus")

        if not inp.session_active:
            return EvDecision(EvAction.NONE, reason="manual session — hands off")

        if inp.low_surplus_ticks >= _STOP_TICKS:
            return EvDecision(EvAction.STOP,
                              reason=f"surplus gone ({surplus:.1f} kW sustained low)")

        current = (inp.last_commanded_amps
                   if inp.last_commanded_amps is not None else v.requested_amps)
        if (target >= _MIN_AMPS
                and abs(target - current) >= _MIN_AMP_DELTA
                and inp.minutes_since_last_command >= _MIN_COMMAND_INTERVAL_MIN):
            return EvDecision(EvAction.SET_AMPS, amps=target,
                              reason=f"track surplus {surplus:.1f} kW -> {target} A")
        return EvDecision(EvAction.NONE, reason="within deadband")

    # 5. Evening / night outside the overnight window: hands off entirely.
    return EvDecision(EvAction.NONE, reason="outside control windows")
