"""Claude-powered Telegram chatbot for FranklinWH energy queries."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from urllib.error import URLError
from urllib.request import Request, urlopen

from .format_utils import fmt_hours, soc_bar, time_to_pct
from .tou import _RATES, TouPeriod, on_peak_window, period_at, rate_at

logger = logging.getLogger(__name__)

_MAX_TURNS   = 10   # conversation turns kept per chat
_MODEL       = "claude-haiku-4-5-20251001"
_DAILY_CALL_CAP = 50  # max chatbot API calls per day (single-user home app)


_soc_bar     = soc_bar
_time_to_pct = time_to_pct
_fmt_hours   = fmt_hours


def _rate_row(hours: str, period: TouPeriod, label: str, note: str = "") -> str:
    """Format one EV-TOU-5 rate row from tou._RATES, so the prompt can't drift from the live rate table."""
    winter = _RATES["winter"][period]
    summer = _RATES["summer"][period]
    return f"  {hours:<10} {label:<15} ${winter:.3f}/kWh (winter) / ${summer:.3f} (summer){note}"


_RATE_TABLE = "\n".join([
    _rate_row("12am–6am", TouPeriod.SUPER_OFF_PEAK, "Super Off-Peak"),
    _rate_row("6am–10am", TouPeriod.OFF_PEAK, "Off-Peak"),
    _rate_row("10am–2pm", TouPeriod.SUPER_OFF_PEAK, "Super Off-Peak", "  ← cheapest window"),
    _rate_row("2pm–4pm", TouPeriod.OFF_PEAK, "Off-Peak"),
    _rate_row("4pm–9pm", TouPeriod.ON_PEAK, "On-Peak", "  ← most expensive"),
    _rate_row("9pm–12am", TouPeriod.OFF_PEAK, "Off-Peak"),
])


_SYSTEM_PROMPT = f"""\
You are an energy assistant for a home with a FranklinWH battery, solar panels, \
and an SDG&E EV-TOU-5 electricity plan. Help the owner understand their solar \
production, battery state, and electricity costs, and give practical advice on \
charging schedules, mode switches, and load timing.

EV-TOU-5 weekday rates:
{_RATE_TABLE}
Weekends: Super Off-Peak until 2pm, then same as weekday.
Summer = June–October.

Battery modes:
  Self-Consumption  – solar first, then battery, grid as backup
  Emergency Backup  – charges battery from grid (good before on-peak)
  Time-of-Use       – charges off-peak, discharges on-peak

Answer in 1-3 short sentences, Telegram-message length. No preamble, no restating \
the question, no bullet lists unless the user asks for a breakdown. Give the direct \
answer first. Use the system data block at the start of each message.
"""


def build_context(stats, history, outlook, cfg, *, rec=None, forecast=None,
                  outdir=None) -> str:
    """Snapshot of current system state, injected as user-message context.

    The extras are keyword-only with defaults so existing callers keep
    working. They exist because the model previously couldn't see the
    advisor's own recommendation, the forecast, or any recent alert — so it
    could confidently contradict the alert the user had just received.
    """

    now   = datetime.now()
    lines = [f"[System snapshot — {now.strftime('%-I:%M %p')}]"]

    if stats:
        c = stats.current
        t = stats.totals
        grid_str = (
            f"+{c.grid_use_kw:.2f} kW (importing)" if c.grid_use_kw > 0 else
            f"{c.grid_use_kw:.2f} kW (exporting)"  if c.grid_use_kw < 0 else
            "0.00 kW"
        )
        cap = getattr(cfg, "battery_capacity_kwh", 13.6)
        tte = _time_to_pct(c.battery_soc_pct, 0.0, cap, c.battery_use_kw)
        ttf = _time_to_pct(c.battery_soc_pct, 100.0, cap, c.battery_use_kw)
        batt_rate = (f"charging at {abs(c.battery_use_kw):.1f} kW" if c.battery_use_kw < -0.1
                     else f"discharging at {c.battery_use_kw:.1f} kW" if c.battery_use_kw > 0.1
                     else "idle")
        lines += [
            f"  Battery SoC:   {_soc_bar(c.battery_soc_pct)} ({batt_rate})",
            f"  Solar now:     {c.solar_production_kw:.2f} kW",
            f"  Home load:     {c.home_load_kw:.2f} kW",
            f"  Grid:          {grid_str}",
            f"  Grid status:   {c.grid_status}",
            f"  Today solar:   {t.solar_kwh:.1f} kWh",
        ]
        if tte is not None:
            lines.append(f"  Time to empty: ~{_fmt_hours(tte)}")
        if ttf is not None:
            lines.append(f"  Time to full:  ~{_fmt_hours(ttf)}")

    period     = period_at(now)
    rate       = rate_at(now)
    peak_start, peak_end = on_peak_window(now)
    lines.append(f"  TOU period:    {period.value.replace('_', ' ')} (${rate:.3f}/kWh)")
    if now < peak_start:
        mins = int((peak_start - now).total_seconds() / 60)
        lines.append(f"  On-peak in:    {mins // 60}h {mins % 60}m (4pm–9pm)")
    elif peak_start <= now < peak_end:
        mins = int((peak_end - now).total_seconds() / 60)
        lines.append(f"  On-peak NOW — ends in {mins // 60}h {mins % 60}m")

    if outlook:
        lines.append(f"  Solar (6h avg): {outlook.avg_ghi(6):.0f} W/m²")

    if history:
        try:
            today_str  = now.strftime("%Y-%m-%d")
            week_start = (now.date() - timedelta(days=6)).strftime("%Y-%m-%d")
            readings   = history.weekly_readings(week_start, today_str)
            if readings:
                from .history import integrate_intervals
                imp = exp = 0.0
                for dt, hours, grid_kw, _hk, _sk in integrate_intervals(readings):
                    r = rate_at(dt)
                    if grid_kw > 0:
                        imp += grid_kw * hours * r
                    elif grid_kw < 0:
                        exp += -grid_kw * hours * r
                solar_7d = sum(
                    history.daily_solar_kwh_api(
                        (now.date() - timedelta(days=i)).strftime("%Y-%m-%d")
                    )
                    for i in range(7)
                )
                lines += [
                    f"  7-day solar:   {solar_7d:.1f} kWh",
                    f"  7-day import:  ${imp:.2f}",
                    f"  7-day export:  ${exp:.2f} credit",
                ]
        except Exception:
            pass

    # The advisor's own current call. Highest-value addition: without it the
    # bot answers "should I switch modes?" from raw telemetry and can
    # contradict the recommendation the user was just alerted about.
    if rec is not None:
        try:
            mode = getattr(getattr(rec, "mode", None), "value", "")
            if mode:
                lines.append(f"  Advisor says:  {mode} ({rec.urgency})")
                reason = (rec.reason or "").split("\n")[0].strip()
                if reason:
                    lines.append(f"  Because:       {reason[:110]}")
            d = getattr(rec, "details", None) or {}
            soc4 = d.get("projected_soc_4pm_pct")
            draw = d.get("projected_peak_draw_kwh")
            if soc4 is not None and draw is not None:
                lines.append(f"  Projected 4pm: {soc4:.0f}% SoC · peak draw ~{draw:.1f} kWh")
        except Exception:
            pass

    # Aggregated, never enumerated — 24 hourly rows would dominate the
    # context window for very little added signal.
    if forecast is not None and getattr(forecast, "hours", None):
        try:
            nxt = [h for h in forecast.hours if h.dt > now][:6]
            if nxt:
                s6 = sum(h.predicted_solar_kw for h in nxt)
                l6 = sum(h.predicted_load_kw for h in nxt)
                lines.append(
                    f"  Next 6h:       solar ~{s6:.1f} kWh, load ~{l6:.1f} kWh "
                    f"(net {s6 - l6:+.1f}) [{forecast.confidence}, {forecast.data_days}d]"
                )
            rest = [h for h in forecast.hours if h.dt > now and h.dt.date() == now.date()]
            if rest:
                lines.append(f"  Rest of today: solar ~{sum(h.predicted_solar_kw for h in rest):.1f} kWh")
        except Exception:
            pass

    # Recent alerts, so the bot knows what the user was just told. Tail the
    # log rather than reading it whole — same seek-from-end trick webapi uses.
    if outdir is not None:
        try:
            from pathlib import Path
            p = Path(outdir) / "alerts_log.jsonl"
            with open(p, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 4096))
                tail = f.read().decode(errors="replace").strip().splitlines()
            recent = []
            for ln in tail[-3:]:
                try:
                    a = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                body = re.sub(r"<[^>]+>", "", a.get("body", "")).split("\n")[0].strip()
                if body:
                    recent.append(body[:60])
            if recent:
                lines.append("  Recent alerts:")
                lines += [f"    · {b}" for b in recent]
        except (OSError, ValueError):
            pass

    capacity = getattr(cfg, "battery_capacity_kwh", 13.6)
    location = getattr(cfg, "location_name", "")
    loc_str  = f", {location}" if location else ""
    lines.append(f"  System:        FranklinWH {capacity:.0f} kWh battery{loc_str}")

    return "\n".join(lines)


class TelegramChatBot:
    """Long-poll Telegram bot backed by Claude Haiku for energy Q&A."""

    def __init__(self, cfg, api_key: str, outdir=None):
        self._cfg             = cfg
        self._api_key         = api_key
        # The main watch loop resolves its actual output dir from `--out`
        # (which can override cfg.output_dir) — without this, /sundown's
        # state write independently re-derived from cfg alone and could
        # silently split alert state across two directories when --out was
        # set, so the EOD digest would never find the chatbot's prediction.
        self._outdir           = outdir
        self._offset          = 0
        self._convos: dict[str, list[dict]] = {}
        self._lock            = threading.Lock()
        # Separate from self._lock, which the main watch loop's update_state()
        # also takes every ~5 min to refresh stats/outlook — sharing one lock
        # meant a slow Claude/Ollama call (up to the 120s Ollama timeout)
        # could delay that update and, downstream, time-sensitive alerts.
        self._convo_lock      = threading.Lock()
        self._stats           = None
        self._hist_store      = None
        self._outlook         = None
        self._system_peak_kw: float | None = None
        self._perf_ratio: float = 1.0
        self._usage_forecast = None
        self._rec            = None
        self._call_count_date: str = ""
        self._call_count: int = 0

    def _is_authorized(self, chat_id: str) -> bool:
        """True if chat_id is the configured owner, or no owner is configured."""
        return not self._cfg.telegram_chat_id or chat_id == self._cfg.telegram_chat_id

    def _under_daily_cap(self) -> bool:
        """True and increments the counter if today's call budget isn't exhausted.

        Locked: each incoming message spawns its own worker thread, and an
        unlocked check-then-increment here let two near-simultaneous
        messages race past the check before either incremented, bypassing
        the _DAILY_CALL_CAP spend guard it exists to enforce.
        """
        with self._lock:
            today = datetime.now().strftime("%Y-%m-%d")
            if today != self._call_count_date:
                self._call_count_date = today
                self._call_count = 0
            if self._call_count >= _DAILY_CALL_CAP:
                return False
            self._call_count += 1
            return True

    def update_state(self, stats, history_store, outlook,
                     system_peak_kw: float | None = None,
                     perf_ratio: float = 1.0,
                     usage_forecast=None, rec=None) -> None:
        with self._lock:
            self._stats          = stats
            self._hist_store     = history_store
            self._outlook        = outlook
            self._system_peak_kw = system_peak_kw
            self._perf_ratio     = perf_ratio
            self._usage_forecast = usage_forecast
            self._rec            = rec

    def run(self) -> None:
        logger.info("Telegram chatbot started")
        base = f"https://api.telegram.org/bot{self._cfg.telegram_bot_token}"
        while True:
            try:
                url = f"{base}/getUpdates?offset={self._offset}&timeout=30"
                with urlopen(url, timeout=35) as resp:
                    data = json.loads(resp.read())
                for upd in data.get("result", []):
                    self._offset = upd["update_id"] + 1
                    cq = upd.get("callback_query")
                    if cq:
                        self._handle_callback_query(cq)
                        continue
                    msg  = upd.get("message") or upd.get("edited_message")
                    if not msg:
                        continue
                    text    = (msg.get("text") or "").strip()
                    chat_id = str(msg["chat"]["id"])
                    if not self._is_authorized(chat_id):
                        # Drop silently — replying would confirm the bot exists
                        # to a stranger who isn't the configured owner.
                        logger.debug("Ignoring message from unauthorized chat_id %s", chat_id)
                        continue
                    if not text:
                        continue
                    if text.lower() in ("/start", "/help"):
                        self._send(chat_id,
                            "FranklinWH AI assistant\n\n"
                            "Ask me anything about your solar, battery, or energy costs.\n"
                            'Example: "Should I charge the car now?" or "Why is my battery low?"\n\n'
                            "/status   — current snapshot\n"
                            "/forecast — solar &amp; weather outlook\n"
                            "/history  — 7-day energy summary\n"
                            "/bill     — current billing cycle cost + projection\n"
                            "/tip      — best action right now\n"
                            "/modes    — explain battery modes\n"
                            "/until N  — time to reach N% SoC at current rate\n"
                            "/sundown  — projected SoC when today's solar is done\n"
                            "/sundown H — projected SoC in H hours (1-24)\n"
                            "/mute     — snooze non-safety alerts (2h or 8h)\n"
                            "/unmute   — cancel an active mute\n"
                            "/clear    — reset conversation history"
                        )
                        continue
                    if text.lower() == "/modes":
                        self._send(chat_id,
                            "🔋 <b>FranklinWH Battery Modes</b>\n\n"
                            "<b>Self-Consumption</b> (default)\n"
                            "Solar charges battery first. Grid used only when battery is empty. Best for sunny days.\n\n"
                            "<b>Emergency Backup</b>\n"
                            "Battery charges from grid. Keeps battery full for outage protection or pre-peak charging.\n\n"
                            "<b>Time-of-Use</b>\n"
                            "Charges during cheap off-peak hours, discharges during expensive 4–9 pm on-peak.\n\n"
                            "💡 Switch to Emergency Backup before 4 pm if SoC is below ~50%."
                        )
                        continue
                    if text.lower() == "/clear":
                        with self._convo_lock:
                            self._convos.pop(chat_id, None)
                        self._send(chat_id, "Conversation cleared.")
                        continue
                    # daemon=True below (all handler threads): matches the
                    # parent bot thread's own daemon flag, which exists
                    # specifically so process shutdown isn't blocked — a
                    # non-daemon handler stuck behind a slow Ollama call
                    # (up to the 120s urlopen timeout) used to defeat that.
                    if text.lower() == "/status":
                        threading.Thread(
                            target=self._send_status,
                            args=(chat_id,),
                            daemon=True,
                        ).start()
                        continue
                    if text.lower() == "/forecast":
                        threading.Thread(
                            target=self._send_forecast,
                            args=(chat_id,),
                            daemon=True,
                        ).start()
                        continue
                    if text.lower() == "/history":
                        threading.Thread(
                            target=self._send_history,
                            args=(chat_id,),
                            daemon=True,
                        ).start()
                        continue
                    if text.lower() == "/bill":
                        threading.Thread(
                            target=self._send_bill,
                            args=(chat_id,),
                            daemon=True,
                        ).start()
                        continue
                    if text.lower() == "/tip":
                        threading.Thread(
                            target=self._send_tip,
                            args=(chat_id,),
                            daemon=True,
                        ).start()
                        continue
                    if text.lower() == "/summary":
                        threading.Thread(
                            target=self._send_summary,
                            args=(chat_id,),
                            daemon=True,
                        ).start()
                        continue
                    # /mute — buttons for the two durations, matching the
                    # CMR News bot's mute UX. /mute N (hours) skips the
                    # round-trip for anyone who already knows the duration.
                    if text.lower() == "/mute":
                        kb = {"inline_keyboard": [[
                            {"text": "2 hours", "callback_data": "mute:2"},
                            {"text": "8 hours", "callback_data": "mute:8"},
                        ]]}
                        self._send(chat_id,
                            "Mute alerts for how long?\n"
                            "Safety alerts (grid outage, fast drain, area "
                            "outage) are never muted.", reply_markup=kb)
                        continue
                    _mm = re.search(r'/mute\s+(\d+(?:\.\d+)?)', text.lower())
                    if _mm:
                        self._send(chat_id, self._set_mute(float(_mm.group(1))))
                        continue
                    if text.lower() == "/unmute":
                        self._send(chat_id, self._set_mute(0))
                        continue
                    # /until <N[%]> or natural language "until/time to/reach N%"
                    _um = re.search(
                        r'/until\s+(\d+)'          # /until 20
                        r'|until\s+(\d+)\s*%'      # until 20%
                        r'|time\s+to\s+(\d+)\s*%'  # time to 80%
                        r'|reach\s+(\d+)\s*%',     # reach 50%
                        text.lower()
                    )
                    if _um:
                        _tgt = int(next(g for g in _um.groups() if g is not None))
                        threading.Thread(
                            target=self._send_until,
                            args=(chat_id, _tgt),
                            daemon=True,
                        ).start()
                        continue
                    # /sundown [H] — no argument: projected SoC once today's
                    # solar is done. With an hour count, it's just a friendlier
                    # name for /willmake H (same projection math) — the two
                    # otherwise-near-duplicate commands stayed separate, so a
                    # user reaching for /sundown to ask "in 3 hours" shouldn't
                    # have to remember /willmake exists instead.
                    _sd_hrs = re.search(r'/sundown\s+(\d+)', text.lower())
                    if _sd_hrs:
                        threading.Thread(
                            target=self._send_projection,
                            args=(chat_id, int(_sd_hrs.group(1))),
                            daemon=True,
                        ).start()
                        continue
                    if text.lower() == "/sundown" or re.search(
                        r'sun\s*(down|set)|end\s+of\s+(the\s+)?day|solar.{0,15}(over|done|finish)',
                        text.lower()
                    ):
                        threading.Thread(
                            target=self._send_sundown,
                            args=(chat_id,),
                            daemon=True,
                        ).start()
                        continue
                    # /willmake <H> — will the battery make it H hours without grid import?
                    _wm = re.search(r'/willmake\s+(\d+)|will\s+i\s+make\s+it\s+(\d+)\s*h', text.lower())
                    if _wm:
                        _hrs = int(next(g for g in _wm.groups() if g is not None))
                        threading.Thread(
                            target=self._send_projection,
                            args=(chat_id, _hrs),
                            daemon=True,
                        ).start()
                        continue
                    threading.Thread(
                        target=self._handle,
                        args=(chat_id, text),
                        daemon=True,
                    ).start()
            except URLError:
                time.sleep(5)
            except Exception as e:
                logger.warning("Chatbot poll error: %s", e)
                time.sleep(5)

    def _send_status(self, chat_id: str) -> None:
        try:
            with self._lock:
                stats    = self._stats
                store    = self._hist_store
                outlook  = self._outlook
                rec      = self._rec
                forecast = self._usage_forecast
            if stats is None:
                self._send(chat_id, "No data yet — advisor hasn't completed its first check.")
                return
            text = build_context(stats, store, outlook, self._cfg,
                                 rec=rec, forecast=forecast, outdir=self._outdir)
            mute_note = self._mute_status_line()
            if mute_note:
                text += f"\n\n{mute_note}"
            self._send(chat_id, text)
        except Exception as e:
            logger.warning("_send_status error: %s", e)
            self._send(chat_id, f"Error fetching status: {e}")

    def _send_forecast(self, chat_id: str) -> None:
        try:
            with self._lock:
                outlook     = self._outlook
                stats       = self._stats
                system_peak = self._system_peak_kw
                perf_ratio  = self._perf_ratio
            if outlook is None:
                self._send(chat_id, "No weather data yet — try again in a moment.")
                return
            now  = datetime.now()

            def _sky(ghi: float) -> str:
                return "Sunny" if ghi >= 400 else ("Partly cloudy" if ghi >= 300 else "Cloudy")

            def _bar(ghi: float) -> str:
                if ghi < 50:  return " "
                if ghi < 150: return "▁"
                if ghi < 250: return "▃"
                if ghi < 380: return "▅"
                if ghi < 530: return "▇"
                return "█"

            today    = now.date()
            tomorrow = (now + timedelta(days=1)).date()

            today_hrs = [h for h in outlook.hours if h.time.date() == today    and 6 <= h.time.hour <= 19]
            tmrw_hrs  = [h for h in outlook.hours if h.time.date() == tomorrow and 6 <= h.time.hour <= 19]

            today_ghi = outlook.avg_ghi(12)
            tmrw_ghi  = outlook.tomorrow_avg_ghi()

            lines = [f"🌤️ Solar Forecast — {now.strftime('%a %b %-d')}"]

            lines.append(f"\nToday: {_sky(today_ghi)} ({today_ghi:.0f} W/m²)")
            if today_hrs:
                lines.append(f"6a {''.join(_bar(h.ghi_wm2) for h in today_hrs)} 7p")
            if system_peak:
                today_kwh = round(outlook.today_generation_kwh(system_peak) * perf_ratio, 1)
                lines.append(f"~{today_kwh:.1f} kWh predicted")

            lines.append(f"\nTomorrow: {_sky(tmrw_ghi)} ({tmrw_ghi:.0f} W/m²)")
            if tmrw_hrs:
                lines.append(f"6a {''.join(_bar(h.ghi_wm2) for h in tmrw_hrs)} 7p")
            if system_peak:
                tmrw_kwh = outlook.tomorrow_generation_kwh(system_peak, perf_ratio)
                lines.append(f"~{tmrw_kwh:.1f} kWh predicted")
                if tmrw_ghi < 250:
                    lines.append("⚡ Dim tomorrow — consider Emergency Backup tonight.")

            if stats:
                c = stats.current
                lines.append(f"\nNow: Solar {c.solar_production_kw:.2f} kW  |  SoC {c.battery_soc_pct:.0f}%")
            self._send(chat_id, "\n".join(lines))
        except Exception as e:
            logger.warning("_send_forecast error: %s", e)
            self._send(chat_id, f"Error fetching forecast: {e}")

    def _send_history(self, chat_id: str) -> None:
        try:
            from pathlib import Path
            from .history import HistoryStore, integrate_intervals
            from .tou import rate_at, base_service_cost
            # self._outdir (the main loop's resolved --out) before cfg — see
            # the note in __init__; re-deriving from cfg alone reads a
            # different history.db than the advisor writes under --out.
            db_path = (self._outdir or Path(getattr(self._cfg, "output_dir", "output"))) / "history.db"
            if not db_path.exists():
                self._send(chat_id, "No history database yet — has the advisor run at least once?")
                return
            now        = datetime.now()
            week_end   = now.date()
            week_start = week_end - timedelta(days=6)
            with HistoryStore(db_path) as store:
                readings = store.weekly_readings(
                    week_start.strftime("%Y-%m-%d"),
                    week_end.strftime("%Y-%m-%d"),
                )
                if not readings:
                    self._send(chat_id, "No history data for the past 7 days yet.")
                    return
                import_cost   = 0.0
                export_credit = 0.0
                solar_kwh     = 0.0
                for dt, hours, grid_kw, _home_kw, s_kw in integrate_intervals(readings):
                    r = rate_at(dt)
                    if grid_kw > 0:
                        import_cost   += grid_kw * hours * r
                    elif grid_kw < 0:
                        export_credit += -grid_kw * hours * r
                    solar_kwh += s_kw * hours
            base_fee   = base_service_cost(7)
            net        = import_cost - export_credit + base_fee
            week_label = f"{week_start.strftime('%b %-d')}–{week_end.strftime('%b %-d')}"
            self._send(chat_id,
                f"📊 <b>7-Day Energy</b> — {week_label}\n"
                f"Solar generated:     <b>{solar_kwh:.1f} kWh</b>\n"
                f"Grid import cost:    <b>${import_cost:.2f}</b>\n"
                f"Grid export credit:  <b>${export_credit:.2f}</b>\n"
                f"Base service:        ${base_fee:.2f}\n"
                f"Net cost:            <b>${net:.2f}</b>"
            )
        except Exception as e:
            logger.warning("_send_history error: %s", e)
            self._send(chat_id, f"Error fetching history: {e}")

    def _send_bill(self, chat_id: str) -> None:
        try:
            from pathlib import Path
            from .history import HistoryStore, integrate_intervals
            from .tou import rate_at, base_service_cost, cycle_bounds, export_rate_at
            db_path = (self._outdir or Path(getattr(self._cfg, "output_dir", "output"))) / "history.db"
            if not db_path.exists():
                self._send(chat_id, "No history database yet — has the advisor run at least once?")
                return
            now         = datetime.now()
            today       = now.strftime("%Y-%m-%d")
            # Shared cycle math — this used to compute "the 20th of the prior
            # month" unconditionally, which past the 20th put the window a
            # full month in the past (on Jul 30 it reported a Jun 20 start).
            cycle_start, cycle_end = cycle_bounds(
                now.date(), getattr(self._cfg, "billing_cycle_start_day", 20)
            )
            # Inclusive of today, matching /api/bill's day_n — base service is
            # billed per calendar day in the period, and an off-by-one here
            # made the two surfaces disagree by exactly one day's base fee.
            days_so_far = (now.date() - cycle_start).days + 1
            cycle_days  = (cycle_end - cycle_start).days + 1
            if days_so_far < 1:
                self._send(chat_id, "Billing cycle just started — not enough data yet.")
                return
            with HistoryStore(db_path) as store:
                readings = store.weekly_readings(cycle_start.strftime("%Y-%m-%d"), today)
            if not readings:
                self._send(chat_id, "No readings for current billing cycle yet.")
                return
            import_cost   = 0.0
            export_credit = 0.0
            for dt, hours, grid_kw, _home_kw, _solar_kw in integrate_intervals(readings):
                if grid_kw > 0:
                    import_cost   += grid_kw * hours * rate_at(dt)
                elif grid_kw < 0:
                    # export_rate_at, not rate_at — exports earn the NEM 3.0
                    # credit, not the retail import rate. /api/bill has always
                    # priced it this way; this side was inflating the credit.
                    export_credit += -grid_kw * hours * export_rate_at(dt)
            base_actual   = base_service_cost(days_so_far)
            net_actual    = import_cost - export_credit + base_actual
            daily_net     = net_actual / days_so_far
            projected_net = daily_net * cycle_days
            projected_imp = import_cost / days_so_far * cycle_days
            projected_exp = export_credit / days_so_far * cycle_days
            projected_base = base_service_cost(cycle_days)
            cycle_label   = f"{cycle_start.strftime('%b %-d')} – {now.date().strftime('%b %-d')}"
            self._send(chat_id,
                f"💡 <b>Billing Cycle</b> — {cycle_label} ({days_so_far} days)\n"
                f"Grid import:  ${import_cost:.2f}\n"
                f"Grid export:  ${export_credit:.2f}\n"
                f"Base service: ${base_actual:.2f}\n"
                f"Net so far:   <b>${net_actual:.2f}</b>\n\n"
                f"Projected full cycle ({cycle_days} days):\n"
                f"  Import:  ${projected_imp:.2f}\n"
                f"  Export:  ${projected_exp:.2f}\n"
                f"  Base:    ${projected_base:.2f}\n"
                f"  Net:     <b>${projected_net:.2f}</b>  (${daily_net:.2f}/day avg)"
            )
        except Exception as e:
            logger.warning("_send_bill error: %s", e)
            self._send(chat_id, f"Error fetching billing data: {e}")

    def _send_summary(self, chat_id: str) -> None:
        """'How am I doing today' in one command — the tip, forecast, and
        bill pieces already exist independently; this just runs all three
        instead of requiring the user to know three separate commands."""
        self._send_tip(chat_id)
        self._send_forecast(chat_id)
        self._send_bill(chat_id)

    def _send_tip(self, chat_id: str) -> None:
        try:
            with self._lock:
                stats   = self._stats
                outlook = self._outlook
            if stats is None:
                self._send(chat_id, "No data yet — advisor hasn't completed its first check.")
                return
            from .tou import TouPeriod
            now    = datetime.now()
            c      = stats.current
            soc    = c.battery_soc_pct
            solar  = c.solar_production_kw
            grid   = c.grid_use_kw
            period = period_at(now)
            rate   = rate_at(now)
            peak_start, _ = on_peak_window(now)
            secs_to_peak  = (peak_start - now).total_seconds()
            mins_to_peak  = int(secs_to_peak / 60) if secs_to_peak > 0 else 0

            if period == TouPeriod.ON_PEAK and grid > 0.5:
                msg = (f"⚠️ Importing <b>{grid:.1f} kW</b> during on-peak (${rate:.3f}/kWh).\n"
                       f"Battery at <b>{soc:.0f}%</b> — reduce non-essential loads if possible.")
            elif period == TouPeriod.ON_PEAK and soc < 20:
                msg = (f"🔴 Battery critical (<b>{soc:.0f}%</b>) during peak.\n"
                       f"Shed non-essential loads to extend backup duration.")
            elif period == TouPeriod.ON_PEAK and soc >= 80:
                msg = (f"🟢 Well positioned for on-peak — <b>{soc:.0f}% SoC</b>, "
                       f"solar {solar:.1f} kW. No action needed.")
            elif period in (TouPeriod.OFF_PEAK, TouPeriod.SUPER_OFF_PEAK) and soc < 30 and 0 < mins_to_peak < 120:
                msg = (f"🟡 Battery at <b>{soc:.0f}%</b> with on-peak in {mins_to_peak // 60}h {mins_to_peak % 60}m.\n"
                       f"Switch to Emergency Backup now to charge before 4 pm.")
            elif period == TouPeriod.SUPER_OFF_PEAK and soc < 50 and solar < 1.0:
                msg = (f"💡 Super off-peak now (cheapest rate: ${rate:.3f}/kWh).\n"
                       f"Battery at <b>{soc:.0f}%</b>, solar low — good time for Emergency Backup.")
            elif solar > 3.0 and soc >= 95:
                msg = (f"🌞 Solar producing <b>{solar:.1f} kW</b> and battery full ({soc:.0f}%).\n"
                       f"Self-Consumption mode is optimal — excess going to grid.")
            else:
                msg = (f"✅ All looks good — <b>{soc:.0f}% SoC</b>, {solar:.1f} kW solar, "
                       f"grid {grid:+.1f} kW ({period.value.replace('_', ' ')}).")
            self._send(chat_id, msg)
        except Exception as e:
            logger.warning("_send_tip error: %s", e)
            self._send(chat_id, f"Error generating tip: {e}")

    def _handle(self, chat_id: str, text: str) -> None:
        try:
            with self._lock:
                stats    = self._stats
                store    = self._hist_store
                outlook  = self._outlook
                rec      = self._rec
                forecast = self._usage_forecast
            if not self._under_daily_cap():
                self._send(chat_id, f"Daily question limit ({_DAILY_CALL_CAP}) reached — try again tomorrow.")
                return
            ctx    = build_context(stats, store, outlook, self._cfg,
                                   rec=rec, forecast=forecast, outdir=self._outdir)
            backend = getattr(self._cfg, "chat_backend", "anthropic")
            if backend == "ollama":
                reply = self._call_ollama(chat_id, text, ctx)
            else:
                reply = self._call_claude(chat_id, text, ctx)
            self._send(chat_id, reply)
        except Exception as e:
            logger.warning("Chatbot handle error: %s", e)
            self._send(chat_id, f"Error: {e}")

    def _call_claude(self, chat_id: str, question: str, context: str) -> str:
        # Locked read-modify-write: each incoming message runs on its own
        # thread, and an unlocked snapshot-then-write-back here let two
        # near-simultaneous messages for the same chat both read the same
        # starting history and then last-writer-wins on the save, silently
        # dropping one message's turn from the conversation.
        import anthropic
        client = anthropic.Anthropic(api_key=self._api_key)
        with self._convo_lock:
            history = list(self._convos.get(chat_id, []))

            history.append({"role": "user", "content": f"{context}\n\nQuestion: {question}"})
            resp = client.messages.create(
                model=_MODEL,
                max_tokens=200,
                system=_SYSTEM_PROMPT,
                messages=history,
            )
            reply = resp.content[0].text
            history.append({"role": "assistant", "content": reply})
            self._convos[chat_id] = history[-(_MAX_TURNS * 2):]
        return reply

    def _call_ollama(self, chat_id: str, question: str, context: str) -> str:
        with self._convo_lock:
            history = list(self._convos.get(chat_id, []))
            history.append({"role": "user", "content": f"{context}\n\nQuestion: {question}"})

            messages = [{"role": "system", "content": _SYSTEM_PROMPT}] + history
            model    = getattr(self._cfg, "ollama_model", "llama3.1:8b")
            base_url = getattr(self._cfg, "ollama_url", "http://localhost:11434")
            payload  = json.dumps({
                "model":    model,
                "messages": messages,
                "stream":   False,
            }).encode()
            req  = Request(
                f"{base_url.rstrip('/')}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urlopen(req, timeout=120) as resp:
                data  = json.loads(resp.read())
            reply = (data.get("message") or {}).get("content") or data.get("response", "")
            if not reply:
                raise ValueError(f"Unexpected Ollama response: {data}")
            history.append({"role": "assistant", "content": reply})
            self._convos[chat_id] = history[-(_MAX_TURNS * 2):]
        return reply

    def _send_until(self, chat_id: str, target_pct: int) -> None:
        """Respond to /until N — estimated time to reach target SoC at current rate."""
        try:
            with self._lock:
                stats = self._stats
            if stats is None:
                self._send(chat_id, "No data yet — advisor hasn't completed its first check.")
                return
            c   = stats.current
            cap = getattr(self._cfg, "battery_capacity_kwh", 13.6)
            soc = c.battery_soc_pct
            batt_kw = c.battery_use_kw
            if not (0 <= target_pct <= 100):
                self._send(chat_id, "Target must be 0–100%.")
                return
            hours = _time_to_pct(soc, float(target_pct), cap, batt_kw)
            if hours is None:
                if abs(batt_kw) < 0.1:
                    self._send(chat_id,
                        f"🔋 Battery is idle ({soc:.0f}% SoC, {batt_kw:+.2f} kW) — can't estimate time to {target_pct}%.")
                else:
                    direction = "charging" if batt_kw < 0 else "discharging"
                    self._send(chat_id,
                        f"🔋 Battery is {direction} ({soc:.0f}% → {batt_kw:+.1f} kW) — "
                        f"{target_pct}% is in the wrong direction.")
                return
            eta     = datetime.now() + timedelta(hours=hours)
            eta_str = eta.strftime("%-I:%M %p")
            rate_str = (f"+{abs(batt_kw):.1f} kW charging" if batt_kw < 0
                        else f"{batt_kw:.1f} kW discharging")
            direction = "up to" if target_pct > soc else "down to"
            self._send(chat_id,
                f"🔋 Battery at <b>{soc:.0f}%</b> — {direction} <b>{target_pct}%</b>\n"
                f"Rate: {rate_str}  ·  Cap: {cap:.0f} kWh\n"
                f"Est: <b>{_fmt_hours(hours)}</b> (~{eta_str})\n"
                f"<i>Rate based on current load — solar and load changes will affect actual time.</i>"
            )
        except Exception as e:
            logger.warning("_send_until error: %s", e)
            self._send(chat_id, f"Error: {e}")

    def _send_projection(self, chat_id: str, hours_ahead: int) -> None:
        """Respond to /willmake H — project SoC forward using the solar+load
        forecast (not just the current battery rate) and report whether the
        battery stays above 0% without grid import through that window."""
        try:
            with self._lock:
                stats    = self._stats
                forecast = self._usage_forecast
            if stats is None:
                self._send(chat_id, "No data yet — advisor hasn't completed its first check.")
                return
            if forecast is None or forecast.confidence == "none":
                self._send(chat_id, "Not enough usage history yet for a forecast-based projection.")
                return
            if not (1 <= hours_ahead <= 24):
                self._send(chat_id, "Projection window must be 1-24 hours.")
                return

            cap = getattr(self._cfg, "battery_capacity_kwh", 13.6)
            c   = stats.current
            soc = c.battery_soc_pct
            kwh = soc / 100.0 * cap

            now     = datetime.now()
            horizon = now + timedelta(hours=hours_ahead)
            min_soc = soc
            min_at  = now
            for h in forecast.hours:
                if h.dt <= now or h.dt > horizon:
                    continue
                kwh = max(0.0, min(cap, kwh + h.predicted_solar_kw - h.predicted_load_kw))
                pct = kwh / cap * 100.0
                if pct < min_soc:
                    min_soc, min_at = pct, h.dt

            end_pct = kwh / cap * 100.0
            will_make_it = min_soc > 0.0
            verdict = (
                f"✅ Should make it — projected SoC stays above <b>{min_soc:.0f}%</b>"
                if will_make_it else
                f"⚠️ Projected to hit <b>0%</b> around {min_at.strftime('%-I:%M %p')} — grid import likely"
            )
            self._send(chat_id,
                f"🔮 Projection: next {hours_ahead}h (using solar+load forecast)\n"
                f"{verdict}\n"
                f"Now: <b>{soc:.0f}%</b>  →  {horizon.strftime('%-I:%M %p')}: ~<b>{end_pct:.0f}%</b>\n"
                f"<i>{forecast.confidence.title()} confidence, {forecast.data_days}d data — actual weather/load will vary.</i>"
            )
        except Exception as e:
            # Was log-only — every other handler sends the user something on
            # failure; this one left /willmake looking indistinguishable
            # from the bot being dead if the projection math ever raised.
            logger.warning("_send_projection error: %s", e)
            self._send(chat_id, "Couldn't run that projection right now — try again in a bit.")

    def _send_sundown(self, chat_id: str) -> None:
        """Respond to /sundown — project SoC forward from now to the last hour
        today's forecast still expects meaningful solar, i.e. the SoC once
        today's generation is done for the day."""
        try:
            with self._lock:
                stats    = self._stats
                forecast = self._usage_forecast
                outlook  = self._outlook
                store    = self._hist_store
            if stats is None:
                self._send(chat_id, "No data yet — advisor hasn't completed its first check.")
                return
            if forecast is None or forecast.confidence == "none":
                self._send(chat_id, "Not enough usage history yet for a forecast-based projection.")
                return

            cap = getattr(self._cfg, "battery_capacity_kwh", 13.6)
            c   = stats.current
            soc = c.battery_soc_pct
            kwh = soc / 100.0 * cap
            now = datetime.now()

            from pathlib import Path

            from .alerts import (_GHI_CLOUDY_THRESHOLD, _get_hourly_bias,
                                 _get_performance_ratio, _get_sundown_bias,
                                 _get_system_peak_kw, _load_peak_state,
                                 _save_peak_state, _state_lock)
            from .predictor import predict

            out = self._outdir or Path(getattr(self._cfg, "output_dir", "output"))
            state = _load_peak_state(out)

            # Live-anchor to what's actually happening right now, same as
            # the EOD digest's own recompute (alerts.py) — the shared
            # `self._usage_forecast` deliberately isn't live-anchored (it
            # also drives recommend()'s Emergency-Backup decision, where a
            # single noisy poll shouldn't ripple in), but /sundown is asked
            # in the moment and should reflect the moment. Falls back to
            # the shared forecast on any failure — never let this take the
            # whole command down.
            live_forecast = forecast
            if store is not None:
                try:
                    cloudy = outlook.avg_ghi(12) < _GHI_CLOUDY_THRESHOLD if outlook else False
                    live_forecast = predict(
                        store, 24, outlook=outlook,
                        system_peak_kw=_get_system_peak_kw(state),
                        perf_ratio=_get_performance_ratio(state, cloudy=cloudy),
                        hourly_bias=_get_hourly_bias(state),
                        current_load_kw=c.home_load_kw,
                    )
                except Exception:
                    logger.exception("/sundown: live-anchored forecast failed")
                    live_forecast = forecast

            # Last forecast hour today still expecting real solar — mirrors the
            # sunrise-detection approach in alerts._alert_eod_digest, just for
            # the trailing edge of the day instead of the leading edge.
            today_sun_hours = [
                h for h in live_forecast.hours
                if h.dt > now and h.dt.date() == now.date() and h.predicted_solar_kw > 0.1
            ]
            if not today_sun_hours:
                self._send(chat_id, "☀️ Looks like solar generation for today is already done (or not enough forecast data left today).")
                return
            sundown_dt = today_sun_hours[-1].dt

            for h in live_forecast.hours:
                if h.dt <= now or h.dt > sundown_dt:
                    continue
                kwh = max(0.0, min(cap, kwh + h.predicted_solar_kw - h.predicted_load_kw))

            raw_pct = kwh / cap * 100.0
            # Learned additive correction from how past /sundown calls
            # actually did (state["sundown_bias_samples"], recorded by the
            # EOD digest's accuracy check) — 0.0 until >=3 graded samples
            # exist, so this is a no-op until there's real signal.
            bias = _get_sundown_bias(state)
            end_pct = max(0.0, min(100.0, raw_pct + bias))

            # Persist the prediction so the EOD digest can report how it did —
            # only meaningful if the user actually asked, so this is opt-in
            # per-day rather than something the advisor predicts on its own.
            with _state_lock(out):
                state = _load_peak_state(out)  # re-load under the lock — avoid clobbering a concurrent writer
                state[f"sundown_pred_{now.strftime('%Y-%m-%d')}"] = {
                    "pct": round(end_pct, 1),
                    "raw_pct": round(raw_pct, 1),
                    "dt": sundown_dt.isoformat(),
                    "requested_at": now.isoformat(),
                }
                _save_peak_state(out, state)

            # The EOD digest fires once at 9-10pm and won't re-check a
            # sundown_pred_ recorded after it already ran (it only fires
            # once per day) — only reachable on long summer days when
            # sundown falls near 9pm, but worth a heads-up rather than the
            # accuracy bullet just silently never appearing.
            followup = (
                "\n<i>Too late in the day for tonight's summary to grade this — no accuracy follow-up.</i>"
                if now.hour >= 21 else
                "\n<i>I'll check how this did in tonight's ~9pm summary.</i>"
            )
            self._send(chat_id,
                f"🌇 Projected SoC at sundown (~{sundown_dt.strftime('%-I:%M %p')}, using solar+load forecast)\n"
                f"Now: <b>{soc:.0f}%</b>  →  Sundown: ~<b>{end_pct:.0f}%</b>\n"
                f"<i>{live_forecast.confidence.title()} confidence, {live_forecast.data_days}d data — actual weather/load will vary.</i>"
                f"{followup}"
            )
        except Exception as e:
            logger.warning("_send_sundown error: %s", e)
            self._send(chat_id, f"Error: {e}")

    def _handle_callback_query(self, cq: dict) -> None:
        """Inline-keyboard button tap — currently only the /mute buttons."""
        cq_id   = cq.get("id", "")
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        if not chat_id or not self._is_authorized(chat_id):
            self._answer_callback_query(cq_id)
            return
        data = cq.get("data", "")
        if data.startswith("mute:"):
            try:
                hours = float(data.split(":", 1)[1])
            except ValueError:
                hours = 0.0
            reply = self._set_mute(hours)
            self._answer_callback_query(cq_id, "Muted" if hours > 0 else "Unmuted")
            self._send(chat_id, reply)
            return
        self._answer_callback_query(cq_id)

    def _set_mute(self, hours: float) -> str:
        """Mute (hours > 0) or clear (hours <= 0) the alert snooze.

        Writes alerts_muted_until to the same .peak_alert_state.json the
        watch loop's dispatcher reads (alerts._alert_enabled), under the
        same lock — mirrors the /sundown prediction-persist pattern above.
        Safety alerts are structurally exempt regardless of this flag; see
        alerts._ALWAYS_ON_ALERTS.
        """
        from pathlib import Path

        from .alerts import _load_peak_state, _save_peak_state, _state_lock
        out = self._outdir or Path(getattr(self._cfg, "output_dir", "output"))
        now = datetime.now()
        until = now + timedelta(hours=hours) if hours > 0 else None
        with _state_lock(out):
            state = _load_peak_state(out)
            if until:
                state["alerts_muted_until"] = until.isoformat()
            else:
                state.pop("alerts_muted_until", None)
            _save_peak_state(out, state)
        if until:
            return (f"🔕 Alerts muted until {until.strftime('%-I:%M %p')} "
                    f"({hours:g}h). Safety alerts (grid outage, fast drain, "
                    f"area outage) are never muted.")
        return "🔔 Alerts unmuted."

    def _mute_status_line(self) -> str:
        """One-line mute status for /status, or '' if not muted. Read-only
        peek at state — no lock needed for an informational display."""
        from pathlib import Path

        from .alerts import _load_peak_state
        out = self._outdir or Path(getattr(self._cfg, "output_dir", "output"))
        until_raw = _load_peak_state(out).get("alerts_muted_until")
        if not until_raw:
            return ""
        try:
            until = datetime.fromisoformat(until_raw)
        except ValueError:
            return ""
        if datetime.now() >= until:
            return ""
        return f"🔕 Alerts muted until {until.strftime('%-I:%M %p')}."

    def _send(self, chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        url = f"https://api.telegram.org/bot{self._cfg.telegram_bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        data = json.dumps(payload).encode()
        req  = Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            urlopen(req, timeout=10)
        except Exception as e:
            logger.warning("Chatbot send error: %s", e)

    def _answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        """Dismiss an inline-keyboard button's loading spinner. Telegram
        expects this within a few seconds of any callback_query, tap or not."""
        url = f"https://api.telegram.org/bot{self._cfg.telegram_bot_token}/answerCallbackQuery"
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        data = json.dumps(payload).encode()
        req  = Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            urlopen(req, timeout=10)
        except Exception as e:
            logger.warning("Chatbot answerCallbackQuery error: %s", e)
