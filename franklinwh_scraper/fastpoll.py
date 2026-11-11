"""Adaptive faster polling while the house is drawing a lot.

The advisor reads the gateway every `watch_interval` minutes (5). That is right for
forecasting and learning, but a dryer or a car charger can start and stop inside one
interval, so the dashboard can show a number up to five minutes old. While home load is
high, this module re-reads the gateway every `fast_poll_seconds` (default 2 minutes) in the
gaps between normal polls.

Design rules, all of them there so that turning it off or being throttled costs nothing:

- Fast readings are NOT stored in history. They go to a small live file the dashboard
  overlays on its latest reading, so forecasts, learned loads and alerts only ever see the
  normal 5-minute cadence, and switching this off leaves no data to clean up.
- The normal poll keeps its own deadline. Fast polls only fill the wait, and stop short of
  it, so the two never run back to back.
- A circuit breaker (`FastPollGuard`) protects the account. FranklinWH publishes no rate
  limits for this API, and a locked account would lock the phone app too:
    * two errors in a row   -> pause 10 min, doubling each time, capped at 2 h
    * throttling (429 etc.) -> pause 1 h, doubling each time, capped at 6 h
    * "Account locked"      -> stop for good (persisted; delete the state file to re-arm)
  plus a hard floor of 60 s between polls and a cap of N polls per hour.
- Off by default; `touch output/.fastpoll_off` stops it without a restart.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LIVE_FILE = ".live_stats.json"
STATE_FILE = ".fastpoll_state.json"
KILL_FILE = ".fastpoll_off"

FLOOR_S = 60                       # never poll faster than this, whatever the config says
_DEFAULT_S = 120
_DEFAULT_LOAD_KW = 3.0
_DEFAULT_PER_HOUR = 36
_MARGIN_S = 20                     # don't start a fast poll this close to the normal one
_LIVE_MAX_AGE = timedelta(minutes=10)

_ERROR_PAUSE_MIN, _ERROR_PAUSE_MAX = 10, 120
_THROTTLE_PAUSE_MIN, _THROTTLE_PAUSE_MAX = 60, 360

_OVERLAY_KEYS = ("home_load_kw", "solar_kw", "battery_soc", "grid_use_kw",
                 "grid_status", "battery_use_kw", "solar_total_kwh")

_THROTTLE_RE = re.compile(r"\b429\b|too many|rate[ -]?limit|throttl", re.IGNORECASE)


# ── settings ─────────────────────────────────────────────────────────────────────────────────────

def effective_interval_s(cfg: Any) -> int:
    try:
        v = int(getattr(cfg, "fast_poll_seconds", _DEFAULT_S))
    except (TypeError, ValueError):
        v = _DEFAULT_S
    return max(FLOOR_S, v)


def load_threshold_kw(cfg: Any) -> float:
    try:
        v = float(getattr(cfg, "fast_poll_load_kw", _DEFAULT_LOAD_KW))
    except (TypeError, ValueError):
        return _DEFAULT_LOAD_KW
    return v if math.isfinite(v) else _DEFAULT_LOAD_KW


def _max_per_hour(cfg: Any) -> int:
    try:
        return max(1, int(getattr(cfg, "fast_poll_max_per_hour", _DEFAULT_PER_HOUR)))
    except (TypeError, ValueError):
        return _DEFAULT_PER_HOUR


def is_enabled(cfg: Any, outdir: Path) -> bool:
    return bool(getattr(cfg, "fast_poll", False)) and not (Path(outdir) / KILL_FILE).exists()


def is_hot(stats: Any, load_kw: float) -> bool:
    """True when the house is drawing enough that a 5-minute-old number can mislead."""
    try:
        return stats is not None and float(stats.current.home_load_kw) >= load_kw
    except (AttributeError, TypeError, ValueError):
        return False


# ── circuit breaker ──────────────────────────────────────────────────────────────────────────────

def classify(exc: BaseException) -> str:
    """'locked' | 'throttled' | 'error' — from what account.py raises."""
    text = str(exc)
    if isinstance(exc, ValueError):
        low = text.lower()
        if "locked" in low or "invalid credentials" in low:
            return "locked"
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429 or _THROTTLE_RE.search(text):
        return "throttled"
    return "error"


@dataclass
class FastPollGuard:
    min_interval_s: int = _DEFAULT_S
    max_per_hour: int = _DEFAULT_PER_HOUR
    errors: int = 0                          # consecutive failures since the last success
    trips: int = 0                           # breaker openings since the last success (the cooldown ladder)
    blocked_until: datetime | None = None
    stopped: bool = False                    # account locked: no timer re-enables it
    attempts: list[datetime] = field(default_factory=list)

    @classmethod
    def for_config(cls, cfg: Any) -> FastPollGuard:
        return cls(min_interval_s=effective_interval_s(cfg), max_per_hour=_max_per_hour(cfg))

    def paused(self, now: datetime) -> bool:
        return self.stopped or (self.blocked_until is not None and now < self.blocked_until)

    def allow(self, now: datetime) -> bool:
        if self.paused(now):
            return False
        self.attempts = [a for a in self.attempts if now - a < timedelta(hours=1)]
        if len(self.attempts) >= self.max_per_hour:
            return False
        if self.attempts and (now - self.attempts[-1]).total_seconds() < self.min_interval_s:
            return False
        return True

    def note_attempt(self, now: datetime) -> None:
        self.attempts.append(now)

    def ok(self, now: datetime) -> bool:
        """A good reading. Returns True if this cleared an error streak or cooldown ladder."""
        changed = bool(self.errors or self.trips or self.blocked_until)
        self.errors = 0
        self.trips = 0
        self.blocked_until = None
        return changed

    def _trip(self, now: datetime, start_min: int, cap_min: int) -> int:
        minutes = min(start_min * 2 ** self.trips, cap_min)
        self.blocked_until = now + timedelta(minutes=minutes)
        self.trips += 1
        self.errors = 0
        return minutes

    def fail(self, now: datetime, exc: BaseException) -> str | None:
        """Record a failed fast poll. Returns a message to send the user when one is due."""
        kind = classify(exc)
        detail = f"{type(exc).__name__}: {str(exc)[:100]}"
        if kind == "locked":
            self.stopped = True
            return ("🔒 FranklinWH says the account is locked or the login was rejected "
                    f"({detail}). Fast polling is stopped for good; the normal advisor poll is "
                    "separate. Check the FranklinWH app, and once it works delete "
                    f"output/{STATE_FILE} to allow fast polling again.")
        if kind == "throttled":
            minutes = self._trip(now, _THROTTLE_PAUSE_MIN, _THROTTLE_PAUSE_MAX)
            return (f"⏸ FranklinWH looks to be throttling requests ({detail}). Fast polling "
                    f"paused for {minutes} min; normal 5-minute polling is unaffected.")
        self.errors += 1
        if self.errors < 2:
            return None
        minutes = self._trip(now, _ERROR_PAUSE_MIN, _ERROR_PAUSE_MAX)
        if self.trips == 1:                  # announce the first pause of a streak, not every re-trip
            return (f"⏸ Fast polling paused for {minutes} min after repeated errors ({detail}). "
                    "Normal 5-minute polling is unaffected.")
        return None


def save_state(outdir: Path, guard: FastPollGuard) -> None:
    """Persist the breaker so a restart can't forget a throttle or a lock. Never raises."""
    try:
        target = Path(outdir) / STATE_FILE
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "errors": guard.errors, "trips": guard.trips, "stopped": guard.stopped,
            "blocked_until": guard.blocked_until.isoformat() if guard.blocked_until else None,
        }))
        tmp.replace(target)
    except OSError:
        logger.debug("Couldn't save fast-poll state", exc_info=True)


def load_state(outdir: Path, guard: FastPollGuard) -> FastPollGuard:
    try:
        d = json.loads((Path(outdir) / STATE_FILE).read_text())
        guard.errors = int(d.get("errors", 0))
        guard.trips = int(d.get("trips", 0))
        guard.stopped = bool(d.get("stopped", False))
        b = d.get("blocked_until")
        guard.blocked_until = datetime.fromisoformat(b) if b else None
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return guard


# ── live file the dashboard overlays ─────────────────────────────────────────────────────────────

def _finite(x: Any) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def write_live(outdir: Path, stats: Any, at: datetime, next_at: datetime) -> None:
    """The newest reading plus when the next poll is due. Never raises, never writes NaN."""
    try:
        c, t = stats.current, stats.totals
        payload = {
            "timestamp": at.isoformat(),
            "next_poll": next_at.isoformat(),
            "home_load_kw": _finite(c.home_load_kw),
            "solar_kw": _finite(c.solar_production_kw),
            "battery_soc": _finite(c.battery_soc_pct),
            "grid_use_kw": _finite(c.grid_use_kw),
            "battery_use_kw": _finite(c.battery_use_kw),
            "grid_status": c.grid_status,
            "solar_total_kwh": _finite(t.solar_kwh),
        }
        target = Path(outdir) / LIVE_FILE
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(target)
    except (OSError, AttributeError, TypeError, ValueError):
        logger.debug("Couldn't write live stats", exc_info=True)


def read_live(outdir: Path, now: datetime | None = None) -> dict | None:
    """The live reading, or None if missing, malformed, stale (> 10 min) or from the future."""
    now = now or datetime.now()
    try:
        d = json.loads((Path(outdir) / LIVE_FILE).read_text())
        age = now - datetime.fromisoformat(d["timestamp"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(d, dict) or age > _LIVE_MAX_AGE or age < timedelta(seconds=-60):
        return None
    return d


def overlay(reading: dict | None, live: dict | None) -> tuple[dict | None, bool]:
    """`reading` (the newest stored row) with the live fields on top when live is newer."""
    if reading is None or live is None:
        return reading, False
    try:
        if datetime.fromisoformat(live["timestamp"]) <= datetime.fromisoformat(reading["timestamp"]):
            return reading, False
    except (KeyError, ValueError, TypeError):
        return reading, False
    merged = dict(reading)
    for k in _OVERLAY_KEYS:
        if live.get(k) is not None:
            merged[k] = live[k]
    merged["timestamp"] = live["timestamp"]
    if live.get("next_poll"):
        merged["next_poll"] = live["next_poll"]
    return merged, True


# ── the wait between normal polls ────────────────────────────────────────────────────────────────

def wait_with_fast_polls(
    *,
    client: Any,
    gateway: str,
    outdir: Path,
    cfg: Any,
    last_stats: Any,
    deadline: float,
    guard: FastPollGuard,
    alert: Callable[..., None],
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = datetime.now,
) -> None:
    """Replace `sleep(until the next normal poll)`: with fast polling off, or a quiet house,
    this is exactly one sleep. While load is high it wakes every few minutes to re-read the
    gateway, then returns at `deadline` (a `monotonic()` time) so the normal poll stays on time."""
    outdir = Path(outdir)
    interval = effective_interval_s(cfg)
    threshold = load_threshold_kw(cfg)
    stats = last_stats
    announced = False

    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        if not is_enabled(cfg, outdir) or guard.stopped or not is_hot(stats, threshold):
            sleep(remaining)
            return
        if not announced:                    # tell the dashboard the next reading is sooner than 5 min
            announced = True
            if not guard.paused(now()):
                write_live(outdir, stats, now(), now() + timedelta(seconds=min(interval, remaining)))
        if remaining <= interval + _MARGIN_S:
            sleep(remaining)                 # the normal poll is due; don't crowd it
            return
        sleep(interval)
        if not is_enabled(cfg, outdir):
            continue
        t = now()
        if not guard.allow(t):
            continue                         # paused, or over budget: keep waiting in interval-sized steps
        guard.note_attempt(t)
        t0 = monotonic()
        try:
            new = client.get_stats(gateway, _empty_retries=0)    # no hidden retries on a call we're rationing
        except Exception as e:               # noqa: BLE001  (KeyboardInterrupt still propagates)
            logger.warning("Fast poll failed after %.1fs (%s: %s)", monotonic() - t0, type(e).__name__, e)
            msg = guard.fail(now(), e)
            save_state(outdir, guard)
            if msg:
                try:
                    alert(msg, urgent=guard.stopped)
                except Exception:            # noqa: BLE001  (a failed notification must not break the wait)
                    logger.exception("Fast-poll alert failed")
            continue
        logger.info("Fast poll ok in %.1fs (home load %.1f kW)", monotonic() - t0, new.current.home_load_kw)
        if guard.ok(now()):
            save_state(outdir, guard)
        stats = new
        after = deadline - monotonic()
        soon = now() + timedelta(seconds=max(0.0, min(after, interval)) if is_hot(stats, threshold) else max(0.0, after))
        write_live(outdir, stats, now(), soon)
