"""Stateful orchestrator for closed-loop EV solar charging.

Called once per advisor tick (cli.py watch loop, 5-min cadence) with the
fresh FranklinWH stats. Decides whether to spend a billable Tesla poll,
builds EvInputs, asks ev_policy.decide(), and executes the action. Every
Tesla failure degrades to "do nothing this tick" — the advisor itself must
never notice.

State lives in output/.ev_controller_state.json (own file — no lock
contention with the alert engine), written atomically. Decisions append to
output/ev_controller.jsonl for the dry-run soak and `account ev-status`.
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from . import ev_policy, tou
from .config import Config
from .config import save as save_config
from .ev_policy import (EvAction, EvDecision, EvInputs, EvParams,
                        VehicleChargeState)
from .notifier import notify_telegram
from .tesla import NotAuthorized, TeslaClient, TeslaError, VehicleAsleep

logger = logging.getLogger(__name__)

_STATE_FILE = ".ev_controller_state.json"
_LOG_FILE = "ev_controller.jsonl"

# Adaptive vehicle-poll cadences (minutes). The fast control loop rides the
# free FranklinWH data; billable Tesla polls only verify plug/SoC/override.
_POLL_ACTIVE_MIN = 15      # managed session in progress
_POLL_DETECT_MIN = 30      # idle but surplus (or overnight window) says "maybe"
_SNAPSHOT_MAX_AGE_MIN = 20  # older cached vehicle snapshot -> treat as None

_EV_API_ERROR_LIMIT = 5    # Telegram note after this many consecutive failures
# Budget guard against the $10/month Fleet API credit (rates verified
# July 2026: data $0.002, command $0.001, wake $0.02).
_COST = {"data": 0.002, "cmd": 0.001, "wake": 0.02}
_BUDGET_DEGRADE_USD = 8.0   # projection past this -> double poll intervals
_BUDGET_HALT_USD = 9.50     # spend past this -> no new sessions this month

# Auto-calibrate cfg.ev_charging_kw from real charging sessions instead of
# leaving it at whatever the setup wizard guessed — same rolling-median idea
# as the solar system-peak-kW calibration (_get_system_peak_kw in alerts.py).
_EV_KW_MIN_SAMPLES = 5        # real charging readings needed before trusting it
_EV_KW_SAMPLE_CAP = 60        # rolling window — a handful of real sessions
_EV_KW_UPDATE_THRESHOLD = 0.10  # only write cfg on a >=10% swing from current
_EV_KW_UPDATE_COOLDOWN_H = 24   # at most one auto-tune write per day


def _now_iso(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


class EvController:
    def __init__(self, cfg: Config, outdir: Path):
        self.cfg = cfg
        self.outdir = Path(outdir)
        self.state_path = self.outdir / _STATE_FILE
        self.log_path = self.outdir / _LOG_FILE
        self.state = self._load_state()
        self.client = TeslaClient(
            vin=cfg.tesla_vin,
            client_id=cfg.tesla_client_id,
            on_spend=self._record_spend,
        )
        # Crash recovery: if a previous run died mid-managed-session, the car
        # may be stuck at throttled amps. Restore full speed once, up front.
        if self.state.get("session") in ("solar", "overnight"):
            self._restore_after_orphan()

    # ------------------------------------------------------------ state I/O

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.state_path)

    def _log(self, now: datetime, decision: EvDecision, **extra) -> None:
        entry = {"timestamp": _now_iso(now), "action": decision.action.value,
                 "amps": decision.amps, "reason": decision.reason,
                 "dry_run": self.cfg.ev_dry_run, **extra}
        try:
            with self.log_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            logger.exception("ev controller log write failed")

    def _notify(self, body: str) -> None:
        if self.cfg.telegram_bot_token and self.cfg.telegram_chat_id:
            try:
                notify_telegram(body, self.cfg.telegram_bot_token,
                                self.cfg.telegram_chat_id)
            except Exception:
                logger.exception("ev telegram notify failed")

    # ------------------------------------------------------------ spend meter

    def _record_spend(self, kind: str) -> None:
        month = datetime.now().strftime("%Y-%m")
        spend = self.state.setdefault("spend", {})
        spend.setdefault(month, {"data": 0, "cmd": 0, "wake": 0})
        spend[month][kind] = spend[month].get(kind, 0) + 1
        # Prune months older than the previous one.
        for k in [k for k in spend if k < (datetime.now() - timedelta(days=62)).strftime("%Y-%m")]:
            del spend[k]

    def month_spend_usd(self, now: datetime | None = None) -> float:
        month = (now or datetime.now()).strftime("%Y-%m")
        counts = self.state.get("spend", {}).get(month, {})
        return sum(_COST[k] * counts.get(k, 0) for k in _COST)

    def _budget_state(self, now: datetime) -> str:
        """'ok' | 'degrade' | 'halt' based on month-to-date spend projection."""
        spent = self.month_spend_usd(now)
        if spent >= _BUDGET_HALT_USD:
            return "halt"
        frac = max(now.day / 30.0, 1 / 30.0)
        if spent / frac > _BUDGET_DEGRADE_USD:
            return "degrade"
        return "ok"

    # ------------------------------------------------------------ vehicle poll

    def _cached_vehicle(self, now: datetime, max_age_min: float) -> VehicleChargeState | None:
        raw = self.state.get("last_vehicle")
        if not raw:
            return None
        try:
            fetched = datetime.fromisoformat(raw["fetched_at"])
            if (now - fetched) > timedelta(minutes=max_age_min):
                return None
            return VehicleChargeState(**{**raw, "fetched_at": fetched})
        except (KeyError, TypeError, ValueError):
            return None

    def _poll_vehicle(self, now: datetime) -> VehicleChargeState | None:
        try:
            v = self.client.get_charge_state()
        except VehicleAsleep:
            self.state["consec_tesla_errors"] = 0
            return None  # asleep car is not charging; nothing to control
        except (NotAuthorized, TeslaError) as e:
            self._bump_error(now, str(e))
            return None
        self._clear_errors(now)
        self.state["last_vehicle"] = {**asdict(v),
                                      "fetched_at": _now_iso(v.fetched_at)}
        self._record_ev_kw_sample(v, now)
        return v

    def _record_ev_kw_sample(self, v: VehicleChargeState, now: datetime) -> None:
        """Feed a real charging reading into the ev_charging_kw calibration.

        Only called from a genuinely fresh Tesla poll (never a cached
        snapshot) — a stale reading resampled every tick between polls
        would bias the median toward whatever amps happened to be set the
        last time we actually asked.
        """
        if not v.charging or v.actual_current_a <= 0:
            return
        kw = v.actual_current_a * (v.charger_voltage or 240.0) / 1000.0
        samples = self.state.get("ev_draw_samples", [])
        samples.append(round(kw, 2))
        self.state["ev_draw_samples"] = samples[-_EV_KW_SAMPLE_CAP:]
        self._maybe_auto_tune_ev_kw(now)

    def _maybe_auto_tune_ev_kw(self, now: datetime) -> None:
        samples = self.state.get("ev_draw_samples", [])
        if len(samples) < _EV_KW_MIN_SAMPLES or self.cfg.ev_charging_kw <= 0:
            return
        calibrated = statistics.median(samples)
        current = self.cfg.ev_charging_kw
        if abs(calibrated - current) / current < _EV_KW_UPDATE_THRESHOLD:
            return
        last_tuned = self.state.get("ev_kw_last_tuned_iso")
        if last_tuned:
            try:
                if (now - datetime.fromisoformat(last_tuned)
                        < timedelta(hours=_EV_KW_UPDATE_COOLDOWN_H)):
                    return
            except ValueError:
                pass
        old = current
        self.cfg.ev_charging_kw = round(calibrated, 1)
        try:
            save_config(self.cfg)
        except OSError:
            logger.exception("ev: failed to persist auto-tuned ev_charging_kw")
            self.cfg.ev_charging_kw = old  # don't claim a tune that didn't save
            return
        self.state["ev_kw_last_tuned_iso"] = _now_iso(now)
        self._notify(
            f"🔌 EV charging draw estimate auto-tuned: {old:.1f} kW → "
            f"{self.cfg.ev_charging_kw:.1f} kW, from {len(samples)} real "
            f"charging readings. The digest's 'with EV charging' line will "
            f"use the new number.")

    def _bump_error(self, now: datetime, msg: str) -> None:
        n = self.state.get("consec_tesla_errors", 0) + 1
        self.state["consec_tesla_errors"] = n
        self.state["last_error"] = f"{_now_iso(now)} {msg[:200]}"
        if n == _EV_API_ERROR_LIMIT:
            self._notify(f"🚗 <b>EV control: {n} consecutive Tesla API errors</b>\n"
                         f"{msg[:200]}\nStanding down until the API recovers.")

    def _clear_errors(self, now: datetime) -> None:
        if self.state.get("consec_tesla_errors", 0) >= _EV_API_ERROR_LIMIT:
            self._notify("🚗 EV control: Tesla API recovered.")
        self.state["consec_tesla_errors"] = 0

    def _should_poll(self, now: datetime, surplus_hint_kw: float,
                     budget: str) -> bool:
        """Spend a $0.002 vehicle poll this tick?"""
        session = self.state.get("session", "none")
        period = tou.period_at(now)
        overnight = (period == tou.TouPeriod.SUPER_OFF_PEAK
                     and now.hour < ev_policy._OVERNIGHT_END_HOUR)
        interval = None
        if session in ("solar", "overnight"):
            interval = _POLL_ACTIVE_MIN
        elif overnight or surplus_hint_kw >= ev_policy._START_SURPLUS_KW:
            interval = _POLL_DETECT_MIN
        if interval is None:
            return False
        if budget == "degrade":
            interval *= 2
        last = self.state.get("last_poll_iso")
        if not last:
            return True
        try:
            return (now - datetime.fromisoformat(last)) >= timedelta(minutes=interval)
        except ValueError:
            return True

    # ------------------------------------------------------------ helpers

    def _at_home(self, v: VehicleChargeState, home_load_kw: float) -> bool:
        """The car's reported AC draw must actually appear in home_load_kw.

        FranklinWH can't see which outlet the car is on; but if the Tesla
        says it's pulling 7 kW AC and the whole house is drawing 2 kW, it is
        charging somewhere else. Idle cars are provisionally 'home' — the
        START verification (home_load must jump) catches that case.
        """
        if v.fast_charger:
            return False
        if self.state.get("away_until_unplug") and v.plugged_in:
            return False
        if not v.charging:
            return True
        draw = v.actual_current_a * (v.charger_voltage or 240.0) / 1000.0
        if draw < 0.5:
            return True
        return home_load_kw + ev_policy._HOME_MATCH_TOL_KW >= draw

    def _detect_override(self, now: datetime, v: VehicleChargeState) -> None:
        if not v.plugged_in:
            # Unplug clears standdown and away-flag: new plug = fresh consent.
            self.state.pop("override_until_iso", None)
            self.state.pop("away_until_unplug", None)
            if self.state.get("session") != "none":
                self._end_session(now, "unplugged")
            return
        last_cmd = self.state.get("last_commanded_amps")
        if (self.state.get("session") in ("solar", "overnight")
                and last_cmd is not None
                and v.requested_amps
                and v.requested_amps != last_cmd):
            until = now + timedelta(hours=ev_policy._OVERRIDE_BACKOFF_HOURS)
            self.state["override_until_iso"] = _now_iso(until)
            self.state["session"] = "none"
            self._notify("🚗 EV control: detected a manual change in the Tesla "
                         f"app ({v.requested_amps} A) — standing down until "
                         f"{until.strftime('%H:%M')}.")

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

    def _end_session(self, now: datetime, why: str) -> None:
        kind = self.state.get("session", "none")
        self.state["session"] = "none"
        self.state["low_surplus_ticks"] = 0
        self.state["high_surplus_ticks"] = 0
        if kind in ("solar", "overnight"):
            self._notify(f"🚗 EV {kind} charging session ended — {why}.")

    def _restore_after_orphan(self) -> None:
        """Startup after a crash mid-session: car may be stuck throttled."""
        self.state["session"] = "none"
        if self.cfg.ev_dry_run:
            return
        try:
            self.client.set_charging_amps(self.cfg.ev_max_amps)
            logger.info("ev: restored max amps after orphaned session")
        except (TeslaError, NotAuthorized):
            logger.warning("ev: could not restore amps after orphaned session")
        self._save_state()

    # ------------------------------------------------------------ execution

    def _execute(self, now: datetime, d: EvDecision) -> None:
        if self.cfg.ev_dry_run or d.action is EvAction.NONE:
            return
        if d.action is EvAction.SET_AMPS:
            self.client.set_charging_amps(d.amps)
            self.state["last_commanded_amps"] = d.amps
            self.state["last_command_iso"] = _now_iso(now)
        elif d.action is EvAction.START:
            self.client.set_charging_amps(d.amps)
            self.client.charge_start()
            self.state["last_commanded_amps"] = d.amps
            self.state["last_command_iso"] = _now_iso(now)
        elif d.action in (EvAction.STOP, EvAction.RESTORE_MAX):
            if d.action is EvAction.STOP:
                self.client.charge_stop()
            # Once charge_stop() succeeds, charging IS stopped — clear state
            # now, before the amps restore below, rather than after both
            # calls. If set_charging_amps then fails, the exception still
            # propagates to tick()'s handler (error gets counted), but
            # last_commanded_amps no longer sits stale at the pre-stop
            # value, which would otherwise corrupt the next tick's deadband
            # math (decide() comparing a fresh target against amps that
            # haven't actually applied to a stopped session in a while).
            self.state["last_commanded_amps"] = None
            self.state["last_command_iso"] = _now_iso(now)
            # Always restore max so a dead advisor can never leave the car
            # throttled — the in-car scheduled-charging backstop then works
            # at full speed.
            self.client.set_charging_amps(self.cfg.ev_max_amps)

    # ------------------------------------------------------------ main tick

    def tick(self, stats, now: datetime) -> None:
        c = stats.current
        period = tou.period_at(now)

        # Free surplus hint from FranklinWH data alone (assume any cached EV
        # draw) — gates whether we spend a Tesla poll at all.
        cached = self._cached_vehicle(now, _SNAPSHOT_MAX_AGE_MIN)
        ev_kw = ev_policy.ev_draw_kw(cached, self.state.get("last_commanded_amps"))
        surplus = ev_policy.compute_surplus_kw(
            c.solar_production_kw, c.home_load_kw, ev_kw, self.cfg.ev_reserve_kw)

        # Hysteresis tick counters run every tick on free data.
        if surplus <= ev_policy._STOP_SURPLUS_KW:
            self.state["low_surplus_ticks"] = self.state.get("low_surplus_ticks", 0) + 1
        else:
            self.state["low_surplus_ticks"] = 0
        if surplus >= ev_policy._START_SURPLUS_KW:
            self.state["high_surplus_ticks"] = self.state.get("high_surplus_ticks", 0) + 1
        else:
            self.state["high_surplus_ticks"] = 0

        budget = self._budget_state(now)

        vehicle = cached
        if self._should_poll(now, surplus, budget):
            self.state["last_poll_iso"] = _now_iso(now)
            vehicle = self._poll_vehicle(now) or cached

        if vehicle is not None:
            self._detect_override(now, vehicle)

        # START verification: last tick we started a session; home_load must
        # now contain the car's draw, else it's charging somewhere else.
        if (self.state.pop("verify_start", None)
                and vehicle is not None and vehicle.charging
                and not self._at_home(vehicle, c.home_load_kw)):
            self.state["away_until_unplug"] = True
            self._end_session(now, "charging away from home")
            if not self.cfg.ev_dry_run:
                try:
                    self.client.charge_stop()
                    self.client.set_charging_amps(self.cfg.ev_max_amps)
                except (TeslaError, NotAuthorized):
                    pass

        inp = EvInputs(
            now=now, tou_period=period,
            solar_kw=c.solar_production_kw, home_load_kw=c.home_load_kw,
            fwh_battery_soc=c.battery_soc_pct, fwh_battery_kw=c.battery_use_kw,
            vehicle=vehicle,
            at_home=vehicle is not None and self._at_home(vehicle, c.home_load_kw),
            session_active=self.state.get("session", "none") != "none",
            last_commanded_amps=self.state.get("last_commanded_amps"),
            minutes_since_last_command=self._minutes_since_command(now),
            low_surplus_ticks=self.state.get("low_surplus_ticks", 0),
            high_surplus_ticks=self.state.get("high_surplus_ticks", 0),
            override_active=self._override_active(now),
        )
        params = EvParams(max_amps=self.cfg.ev_max_amps,
                          battery_first_soc=self.cfg.ev_battery_first_soc,
                          reserve_kw=self.cfg.ev_reserve_kw)
        d = ev_policy.decide(inp, params)

        if d.action is EvAction.START and budget == "halt":
            d = EvDecision(EvAction.NONE, reason="monthly API budget exhausted")
            if not self.state.get("budget_halt_notified"):
                self.state["budget_halt_notified"] = now.strftime("%Y-%m")
                self._notify("🚗 EV control: monthly Tesla API budget reached — "
                             "no new sessions until next month.")

        try:
            self._execute(now, d)
        except (TeslaError, NotAuthorized) as e:
            self._bump_error(now, str(e))
            d = EvDecision(EvAction.NONE, reason=f"command failed: {e}")

        # Session bookkeeping.
        if d.action is EvAction.START and not self.cfg.ev_dry_run:
            kind = ("overnight" if "overnight" in d.reason else "solar")
            self.state["session"] = kind
            self.state["verify_start"] = True
            self._notify(f"🚗 EV {kind} charging started at {d.amps} A "
                         f"({d.reason}).")
        elif d.action is EvAction.STOP:
            self._end_session(now, d.reason)

        if d.action is not EvAction.NONE or vehicle is not None:
            self._log(now, d, surplus_kw=round(surplus, 2),
                      ev_kw=round(ev_kw, 2), soc=c.battery_soc_pct,
                      period=period.value,
                      session=self.state.get("session", "none"))
        self._save_state()

    def _minutes_since_command(self, now: datetime) -> float:
        raw = self.state.get("last_command_iso")
        if not raw:
            return 1e9
        try:
            return (now - datetime.fromisoformat(raw)).total_seconds() / 60.0
        except ValueError:
            return 1e9
