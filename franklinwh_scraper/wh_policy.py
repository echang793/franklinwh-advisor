"""Pure decision logic for the Rheem heat pump water heater (EcoNet).

Zero I/O and zero network, same shape as ev_policy.py: the controller
(wh_controller.py, to come) gathers inputs once per advisor tick, calls
decide(), and executes the returned action via the EcoNet client.

Why: a heat pump water heater in Energy Saver (hybrid) mode can fire its
~4.5 kW resistance element whenever demand is high, including 4-9 PM
on-peak, the most expensive power on EV-TOU-5. The heat pump alone draws
~0.5 kW. The tank is a thermal battery, so heat is cheapest to bank from
midday solar and before the peak window starts.

Behavior:
- On-peak 16-21: heat-pump-only at a lower setpoint. The resistance element
  never fires. Applied at once (no command rate limit).
- Pre-peak hour (15:00): top up in heat-pump-only mode so the tank enters
  peak hot. A higher banked setpoint is kept, not lowered.
- Daytime solar surplus (battery served first): heat-pump-only at a raised
  setpoint, banking free heat. Held through brief clouds, ended on
  sustained low surplus.
- Away (away.py): vacation mode.
- Everything else: the normal mode and setpoint. The normal mode defaults to
  heat-pump-only, so with default settings the heater never leaves heat
  pump only except for vacation; only the setpoint moves.
- Setpoint never below MIN_SAFE_SETPOINT_F (Legionella) or above
  MAX_SETPOINT_F (scald), whatever the config says.

EV interaction: the water heater outranks the EV for solar. ev_policy
already counts the heater's draw as house load, so the EV yields on its own;
compute_wh_surplus_kw adds the EV's draw back so the heater sees it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .tou import TouPeriod

MIN_SAFE_SETPOINT_F = 120   # below this Legionella can grow in the tank
MAX_SETPOINT_F = 140        # Rheem heat pump ceiling; scald risk above
_START_TICKS = 2            # sustained ticks (10 min @ 5-min polls) before banking
_STOP_TICKS = 3             # one ~10-min cloud transit shouldn't end banking
_MIN_COMMAND_INTERVAL_MIN = 30.0  # EcoNet is a cloud API; don't chatter
_SOLAR_MIN_KW = 0.1         # below this there is no "daytime"
_PRE_PEAK_HOUR = 15         # the hour before on-peak starts at 16:00


class WhMode(str, Enum):
    """EcoNet operating modes for a Rheem heat pump water heater."""
    OFF = "off"
    HEAT_PUMP = "heat_pump"          # compressor only, ~0.5 kW
    ENERGY_SAVER = "energy_saver"    # hybrid: compressor, element when demand is high
    HIGH_DEMAND = "high_demand"      # compressor + element more eagerly
    ELECTRIC = "electric"            # element only, ~4.5 kW
    VACATION = "vacation"


class WhAction(str, Enum):
    NONE = "none"
    SET = "set"      # set mode and (if not None) setpoint


@dataclass(frozen=True)
class WaterHeaterState:
    """Snapshot of the heater, mapped from an EcoNet poll."""
    mode: WhMode
    setpoint_f: int
    running: bool
    fetched_at: datetime


@dataclass(frozen=True)
class WhParams:
    """Config-derived knobs; trivial to construct in tests."""
    normal_mode: WhMode = WhMode.HEAT_PUMP   # most efficient; Energy Saver lets the element in
    normal_setpoint_f: int = 125
    peak_setpoint_f: int = 120
    pre_peak_setpoint_f: int = 130
    bank_setpoint_f: int = 135
    battery_first_soc: float = 80.0   # FWH battery tops up before the tank banks heat
    bank_start_kw: float = 0.8        # surplus that covers the compressor plus margin
    reserve_kw: float = 0.25


@dataclass(frozen=True)
class WhInputs:
    now: datetime
    tou_period: TouPeriod
    solar_kw: float
    home_load_kw: float          # INCLUDES the heater's and the EV's draw
    wh_draw_kw: float
    ev_draw_kw: float
    fwh_battery_soc: float
    heater: WaterHeaterState | None  # None = poll failed this tick
    away: bool
    override_active: bool
    minutes_since_last_command: float
    high_surplus_ticks: int      # consecutive ticks with surplus >= bank_start_kw
    low_surplus_ticks: int       # consecutive ticks with surplus < bank_start_kw


@dataclass(frozen=True)
class WhDecision:
    action: WhAction
    mode: WhMode | None = None
    setpoint_f: int | None = None
    reason: str = ""


def clamp_setpoint(setpoint_f: float) -> int:
    """Hold any setpoint inside [MIN_SAFE_SETPOINT_F, MAX_SETPOINT_F]."""
    return int(min(MAX_SETPOINT_F, max(MIN_SAFE_SETPOINT_F, round(setpoint_f))))


def compute_wh_surplus_kw(solar_kw: float, home_load_kw: float, wh_kw: float,
                          ev_kw: float, reserve_kw: float) -> float:
    """Solar left for the water heater after the rest of the house.

    Both the heater's own draw and the EV's are added back: the heater's so
    banking doesn't cancel itself next tick, the EV's because the heater
    outranks it for solar.
    """
    other_load_kw = max(0.0, home_load_kw - wh_kw - ev_kw)
    return solar_kw - other_load_kw - reserve_kw


def _at(heater: WaterHeaterState, mode: WhMode, setpoint_f: int | None) -> bool:
    return heater.mode == mode and (setpoint_f is None or heater.setpoint_f == setpoint_f)


def _move(inp: WhInputs, mode: WhMode, setpoint_f: int | None, why: str,
          urgent: bool = False) -> WhDecision:
    """SET toward a target unless already there or (non-urgent) rate limited."""
    if _at(inp.heater, mode, setpoint_f):
        return WhDecision(WhAction.NONE, reason=f"{why} — already set")
    if not urgent and inp.minutes_since_last_command < _MIN_COMMAND_INTERVAL_MIN:
        return WhDecision(WhAction.NONE, reason=f"{why} — rate limited")
    return WhDecision(WhAction.SET, mode=mode, setpoint_f=setpoint_f, reason=why)


def decide(inp: WhInputs, p: WhParams) -> WhDecision:
    """One control decision per advisor tick. Pure — no I/O, no clock reads."""
    h = inp.heater

    # 1. Hard gates: respect the human, never act blind.
    if inp.override_active:
        return WhDecision(WhAction.NONE, reason="manual override standdown")
    if h is None:
        return WhDecision(WhAction.NONE, reason="no heater data this tick")
    if inp.away:
        return _move(inp, WhMode.VACATION, None, "away — vacation mode", urgent=True)

    # 2. On-peak: heat pump only, never the resistance element.
    if inp.tou_period == TouPeriod.ON_PEAK:
        return _move(inp, WhMode.HEAT_PUMP, clamp_setpoint(p.peak_setpoint_f),
                     "on-peak — heat pump only", urgent=True)

    # 3. Pre-peak hour: enter the peak with a hot tank.
    pre_peak = clamp_setpoint(p.pre_peak_setpoint_f)
    if inp.now.hour == _PRE_PEAK_HOUR:
        if h.mode == WhMode.HEAT_PUMP and h.setpoint_f >= pre_peak:
            return WhDecision(WhAction.NONE, reason="pre-peak — tank already banked")
        return _move(inp, WhMode.HEAT_PUMP, pre_peak, "pre-peak top-up")

    # 4. Daytime solar: bank heat once the FWH battery is served.
    bank = clamp_setpoint(p.bank_setpoint_f)
    banking = h.mode == WhMode.HEAT_PUMP and h.setpoint_f >= bank
    if inp.solar_kw > _SOLAR_MIN_KW:
        if banking and inp.low_surplus_ticks < _STOP_TICKS:
            return WhDecision(WhAction.NONE, reason="banking solar heat — holding")
        if not banking and inp.fwh_battery_soc >= p.battery_first_soc:
            surplus = compute_wh_surplus_kw(inp.solar_kw, inp.home_load_kw,
                                            inp.wh_draw_kw, inp.ev_draw_kw, p.reserve_kw)
            if surplus >= p.bank_start_kw and inp.high_surplus_ticks >= _START_TICKS:
                return _move(inp, WhMode.HEAT_PUMP, bank,
                             f"solar surplus {surplus:.1f} kW sustained — bank heat")

    # 5. Everything else: normal.
    return _move(inp, p.normal_mode, clamp_setpoint(p.normal_setpoint_f), "normal")
