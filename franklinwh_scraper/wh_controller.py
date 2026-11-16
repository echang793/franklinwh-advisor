"""Stateful orchestrator for the Rheem heat pump water heater.

Called once per advisor tick (cli.py watch loop, 5-min cadence) with the
fresh FranklinWH stats, like ev_controller.py. Polls EcoNet, builds
WhInputs, asks wh_policy.decide(), and (when not in dry run) applies the
result. Every EcoNet failure degrades to "do nothing this tick".

State lives in output/.wh_controller_state.json, written atomically.
Decisions append to output/wh_controller.jsonl for the dry-run soak: one
line when anything changes (decision, heater mode/setpoint, running state,
error), plus an hourly heartbeat. The running_state text is the ground
truth for whether the resistance element ran on-peak.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from . import tou
from .config import Config
from .econet import EcoNetClient, EcoNetError, HeaterReading
from .notifier import notify_telegram
from .wh_policy import (WaterHeaterState, WhAction, WhDecision, WhInputs, WhMode,
                        WhParams, compute_wh_surplus_kw, decide)

logger = logging.getLogger(__name__)

_STATE_FILE = ".wh_controller_state.json"
_LOG_FILE = "wh_controller.jsonl"
_EV_STATE_FILE = ".ev_controller_state.json"
_ALERT_STATE_FILE = ".peak_alert_state.json"

_HEAT_PUMP_KW = 0.5          # compressor draw
_ELEMENT_KW = 4.5            # resistance element draw
_EV_VOLTS = 240.0
_OVERRIDE_HOURS = 4          # stand down this long after a change made in the app
_PROPAGATION_MIN = 10.0      # a command can take a poll or two to show up in EcoNet
_ERROR_ALERT_TICKS = 6       # ~30 min of failed polls before one Telegram alert
_HEARTBEAT_MIN = 60.0
_NEVER = 1e9


def estimate_wh_kw(h: WaterHeaterState | None) -> float:
    """Heater draw from mode + running flag. Unsure cases assume the heat
    pump: overestimating the element would inflate the computed surplus."""
    if h is None or not h.running:
        return 0.0
    return _ELEMENT_KW if h.mode == WhMode.ELECTRIC else _HEAT_PUMP_KW


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def ev_draw_from_state(outdir: Path) -> float:
    """EV draw from the EV controller's own state file (read-only)."""
    s = _read_json(Path(outdir) / _EV_STATE_FILE)
    amps = s.get("last_commanded_amps")
    if s.get("session") in ("solar", "overnight") and isinstance(amps, (int, float)):
        return amps * _EV_VOLTS / 1000.0
    return 0.0


def read_away(outdir: Path) -> bool:
    """alerts.py's away_mode flag (read-only)."""
    return bool(_read_json(Path(outdir) / _ALERT_STATE_FILE).get("away_since"))


def params_from_config(cfg: Config) -> WhParams:
    d = WhParams()
    try:
        normal = WhMode(getattr(cfg, "wh_normal_mode", d.normal_mode.value))
    except ValueError:
        logger.warning("unknown wh_normal_mode %r — using %s", cfg.wh_normal_mode, d.normal_mode.value)
        normal = d.normal_mode
    return WhParams(
        normal_mode=normal,
        normal_setpoint_f=getattr(cfg, "wh_normal_setpoint_f", d.normal_setpoint_f),
        peak_setpoint_f=getattr(cfg, "wh_peak_setpoint_f", d.peak_setpoint_f),
        pre_peak_setpoint_f=getattr(cfg, "wh_pre_peak_setpoint_f", d.pre_peak_setpoint_f),
        bank_setpoint_f=getattr(cfg, "wh_bank_setpoint_f", d.bank_setpoint_f),
        battery_first_soc=getattr(cfg, "wh_battery_first_soc", d.battery_first_soc),
    )


class WhController:
    def __init__(self, cfg: Config, outdir: Path, client: EcoNetClient | None = None,
                 notify: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.outdir = Path(outdir)
        self.state_path = self.outdir / _STATE_FILE
        self.log_path = self.outdir / _LOG_FILE
        self.client = client or EcoNetClient()
        self._notify_fn = notify
        self.params = params_from_config(cfg)
        self.state = _read_json(self.state_path)

    @property
    def dry_run(self) -> bool:
        return bool(getattr(self.cfg, "wh_dry_run", True))

    # ------------------------------------------------------------ state I/O

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.state_path)

    def _notify(self, body: str) -> None:
        try:
            if self._notify_fn is not None:
                self._notify_fn(body)
            elif self.cfg.telegram_bot_token and self.cfg.telegram_chat_id:
                notify_telegram(body, self.cfg.telegram_bot_token, self.cfg.telegram_chat_id)
        except Exception:
            logger.exception("water heater notify failed")

    def _log(self, now: datetime, d: WhDecision, reading: HeaterReading | None,
             extra: dict) -> None:
        h = reading.state if reading else None
        entry = {"action": d.action.value, "mode": d.mode.value if d.mode else None,
                 "setpoint_f": d.setpoint_f, "reason": d.reason,
                 "heater_mode": h.mode.value if h else None,
                 "heater_setpoint_f": h.setpoint_f if h else None,
                 "running": h.running if h else None,
                 "running_state": reading.running_state if reading else None,
                 "tank_pct": reading.tank_pct if reading else None,
                 "error": extra.get("error")}
        key = json.dumps(entry, sort_keys=True)
        last_at = self.state.get("last_log_iso")
        stale = (last_at is None
                 or (now - datetime.fromisoformat(last_at)) >= timedelta(minutes=_HEARTBEAT_MIN))
        if key == self.state.get("last_log_key") and not stale:
            return
        self.state["last_log_key"] = key
        self.state["last_log_iso"] = now.isoformat(timespec="seconds")
        line = {"timestamp": now.isoformat(timespec="seconds"), **entry,
                "dry_run": self.dry_run, **{k: v for k, v in extra.items() if k != "error"}}
        try:
            with self.log_path.open("a") as f:
                f.write(json.dumps(line) + "\n")
        except OSError:
            logger.exception("water heater log write failed")

    # ------------------------------------------------------------ helpers

    def _minutes_since_command(self, now: datetime) -> float:
        raw = self.state.get("last_command_at")
        if not raw:
            return _NEVER
        try:
            return (now - datetime.fromisoformat(raw)).total_seconds() / 60.0
        except ValueError:
            return _NEVER

    def _override_active(self, now: datetime) -> bool:
        raw = self.state.get("override_until_iso")
        if not raw:
            return False
        try:
            if now < datetime.fromisoformat(raw):
                return True
        except ValueError:
            pass
        self.state.pop("override_until_iso", None)
        return False

    def _detect_override(self, now: datetime, h: WaterHeaterState) -> None:
        """A heater that no longer matches our last command, once the command
        has had time to land, was changed by a person: stand down."""
        cmd = self.state.get("last_command")
        if not cmd or self._minutes_since_command(now) < _PROPAGATION_MIN:
            return
        if h.mode.value == cmd["mode"] and (cmd["setpoint"] is None or h.setpoint_f == cmd["setpoint"]):
            return
        until = now + timedelta(hours=_OVERRIDE_HOURS)
        self.state["override_until_iso"] = until.isoformat(timespec="seconds")
        self.state.pop("last_command", None)
        self._notify(f"🚿 Water heater changed in the app ({h.mode.value}, {h.setpoint_f}°F) — "
                     f"advisor hands off until {until:%-I:%M %p}.")

    def _poll(self, now: datetime) -> HeaterReading | None:
        try:
            reading = self.client.read_state(now)
        except EcoNetError as e:
            n = self.state.get("consec_errors", 0) + 1
            self.state["consec_errors"] = n
            self.state["last_error"] = str(e)
            if n >= _ERROR_ALERT_TICKS and not self.state.get("error_alerted"):
                self.state["error_alerted"] = True
                self._notify(f"🚿 Water heater: EcoNet unreachable for ~{n * 5} min ({e}). "
                             f"Advisor is leaving the heater alone until it's back.")
            return None
        self.state["consec_errors"] = 0
        self.state.pop("error_alerted", None)
        return reading

    def _bump_ticks(self, surplus: float) -> None:
        high = surplus >= self.params.bank_start_kw
        self.state["high_surplus_ticks"] = self.state.get("high_surplus_ticks", 0) + 1 if high else 0
        self.state["low_surplus_ticks"] = 0 if high else self.state.get("low_surplus_ticks", 0) + 1

    # ------------------------------------------------------------ tick

    def tick(self, stats, now: datetime) -> WhDecision:
        c = stats.current
        reading = self._poll(now)
        heater = reading.state if reading else None
        if heater is not None and not self.dry_run:
            self._detect_override(now, heater)

        wh_kw = estimate_wh_kw(heater)
        ev_kw = ev_draw_from_state(self.outdir)
        surplus = compute_wh_surplus_kw(c.solar_production_kw, c.home_load_kw, wh_kw, ev_kw,
                                        self.params.reserve_kw)
        self._bump_ticks(surplus)

        inp = WhInputs(
            now=now, tou_period=tou.period_at(now),
            solar_kw=c.solar_production_kw, home_load_kw=c.home_load_kw,
            wh_draw_kw=wh_kw, ev_draw_kw=ev_kw, fwh_battery_soc=c.battery_soc_pct,
            heater=heater, away=read_away(self.outdir),
            override_active=self._override_active(now),
            minutes_since_last_command=self._minutes_since_command(now),
            high_surplus_ticks=self.state.get("high_surplus_ticks", 0),
            low_surplus_ticks=self.state.get("low_surplus_ticks", 0),
        )
        d = decide(inp, self.params)

        extra: dict = {"surplus_kw": round(surplus, 2), "solar_kw": c.solar_production_kw,
                       "home_load_kw": c.home_load_kw, "soc": c.battery_soc_pct,
                       "period": inp.tou_period.value}
        if heater is None and self.state.get("last_error"):
            extra["error"] = self.state["last_error"]
        if d.action == WhAction.SET and not self.dry_run:
            try:
                self.client.apply(d.mode, d.setpoint_f)
                self.state["last_command"] = {"mode": d.mode.value, "setpoint": d.setpoint_f}
                self.state["last_command_at"] = now.isoformat(timespec="seconds")
            except EcoNetError as e:
                logger.warning("water heater command failed: %s", e)
                extra["error"] = f"apply failed: {e}"

        self._log(now, d, reading, extra)
        self._save_state()
        return d
