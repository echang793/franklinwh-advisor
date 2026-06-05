"""Claude-powered Telegram chatbot for FranklinWH energy queries."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_MAX_TURNS = 10   # conversation turns kept per chat
_MODEL     = "claude-haiku-4-5-20251001"

_SYSTEM_PROMPT = """\
You are an energy assistant for a home with a FranklinWH battery, solar panels, \
and an SDG&E EV-TOU-5 electricity plan. Help the owner understand their solar \
production, battery state, and electricity costs, and give practical advice on \
charging schedules, mode switches, and load timing.

EV-TOU-5 weekday rates:
  12am–6am   Super Off-Peak  $0.117/kWh (winter) / $0.124 (summer)
  6am–10am   Off-Peak        $0.473 / $0.502
  10am–2pm   Super Off-Peak  $0.117 / $0.124  ← cheapest window
  2pm–4pm    Off-Peak        $0.473 / $0.502
  4pm–9pm    On-Peak         $0.529 / $0.800  ← most expensive
  9pm–12am   Off-Peak        $0.473 / $0.502
Weekends: Super Off-Peak until 2pm, then same as weekday.
Summer = June–October.

Battery modes:
  Self-Consumption  – solar first, then battery, grid as backup
  Emergency Backup  – charges battery from grid (good before on-peak)
  Time-of-Use       – charges off-peak, discharges on-peak

Be concise and practical. Use the system data block at the start of each message.
"""


def build_context(stats, history, outlook, cfg) -> str:
    """Snapshot of current system state, injected as user-message context."""
    from .tou import period_at, rate_at, on_peak_window

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
        lines += [
            f"  Battery SoC:   {c.battery_soc_pct:.0f}%",
            f"  Solar now:     {c.solar_production_kw:.2f} kW",
            f"  Home load:     {c.home_load_kw:.2f} kW",
            f"  Grid:          {grid_str}",
            f"  Grid status:   {c.grid_status}",
            f"  Today solar:   {t.solar_kwh:.1f} kWh",
        ]

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
                from .tou import rate_at as _r
                interval = 0.25
                imp = exp = 0.0
                for ts, grid_kw, _hk, _sk in readings:
                    try:
                        dt = datetime.fromisoformat(ts)
                    except Exception:
                        continue
                    r = _r(dt)
                    if grid_kw > 0:
                        imp += grid_kw * interval * r
                    elif grid_kw < 0:
                        exp += abs(grid_kw) * interval * r
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

    capacity = getattr(cfg, "battery_capacity_kwh", 13.6)
    location = getattr(cfg, "location_name", "")
    loc_str  = f", {location}" if location else ""
    lines.append(f"  System:        FranklinWH {capacity:.0f} kWh battery{loc_str}")

    return "\n".join(lines)


class TelegramChatBot:
    """Long-poll Telegram bot backed by Claude Haiku for energy Q&A."""

    def __init__(self, cfg, api_key: str):
        self._cfg             = cfg
        self._api_key         = api_key
        self._offset          = 0
        self._convos: dict[str, list[dict]] = {}
        self._lock            = threading.Lock()
        self._stats           = None
        self._hist_store      = None
        self._outlook         = None
        self._system_peak_kw: float | None = None
        self._perf_ratio: float = 1.0

    def update_state(self, stats, history_store, outlook,
                     system_peak_kw: float | None = None,
                     perf_ratio: float = 1.0) -> None:
        with self._lock:
            self._stats          = stats
            self._hist_store     = history_store
            self._outlook        = outlook
            self._system_peak_kw = system_peak_kw
            self._perf_ratio     = perf_ratio

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
                    msg  = upd.get("message") or upd.get("edited_message")
                    if not msg:
                        continue
                    text    = (msg.get("text") or "").strip()
                    chat_id = str(msg["chat"]["id"])
                    if not text:
                        continue
                    if text.lower() in ("/start", "/help"):
                        self._send(chat_id,
                            "FranklinWH AI assistant\n\n"
                            "Ask me anything about your solar, battery, or energy costs.\n"
                            "Example: \"Should I charge the car now?\" or \"Why is my battery low?\"\n\n"
                            "/status   — current snapshot\n"
                            "/forecast — solar & weather outlook\n"
                            "/history  — 7-day energy summary\n"
                            "/bill     — current billing cycle cost + projection\n"
                            "/tip      — best action right now\n"
                            "/clear    — reset conversation history"
                        )
                        continue
                    if text.lower() == "/clear":
                        self._convos.pop(chat_id, None)
                        self._send(chat_id, "Conversation cleared.")
                        continue
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
        with self._lock:
            stats   = self._stats
            store   = self._hist_store
            outlook = self._outlook
        if stats is None:
            self._send(chat_id, "No data yet — advisor hasn't completed its first check.")
            return
        self._send(chat_id, build_context(stats, store, outlook, self._cfg))

    def _send_forecast(self, chat_id: str) -> None:
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

    def _send_history(self, chat_id: str) -> None:
        with self._lock:
            store = self._hist_store
        if store is None:
            self._send(chat_id, "No history data yet — start the advisor first.")
            return
        from .tou import rate_at
        now        = datetime.now()
        week_end   = now.date()
        week_start = week_end - timedelta(days=6)
        readings   = store.weekly_readings(
            week_start.strftime("%Y-%m-%d"),
            week_end.strftime("%Y-%m-%d"),
        )
        if not readings:
            self._send(chat_id, "No history data for the past 7 days yet.")
            return
        interval      = 0.25
        import_cost   = 0.0
        export_credit = 0.0
        solar_kwh     = 0.0
        for ts, grid_kw, _home_kw, s_kw in readings:
            try:
                dt = datetime.fromisoformat(ts)
            except Exception:
                continue
            r = rate_at(dt)
            if grid_kw > 0:
                import_cost   += grid_kw * interval * r
            elif grid_kw < 0:
                export_credit += abs(grid_kw) * interval * r
            solar_kwh += s_kw * interval
        net        = import_cost - export_credit
        week_label = f"{week_start.strftime('%b %-d')}–{week_end.strftime('%b %-d')}"
        self._send(chat_id,
            f"📊 7-Day Energy — {week_label}\n"
            f"Solar generated:     {solar_kwh:.1f} kWh\n"
            f"Grid import cost:    ${import_cost:.2f}\n"
            f"Grid export credit:  ${export_credit:.2f}\n"
            f"Net cost:            ${net:.2f}"
        )

    def _send_bill(self, chat_id: str) -> None:
        with self._lock:
            store = self._hist_store
        if store is None:
            self._send(chat_id, "No history data yet — start the advisor first.")
            return
        from .tou import rate_at
        now         = datetime.now()
        today       = now.strftime("%Y-%m-%d")
        cycle_start = (now.date().replace(day=1) - timedelta(days=1)).replace(day=20)
        days_so_far = (now.date() - cycle_start).days
        if days_so_far < 1:
            self._send(chat_id, "Billing cycle just started — not enough data yet.")
            return
        readings = store.weekly_readings(cycle_start.strftime("%Y-%m-%d"), today)
        if not readings:
            self._send(chat_id, "No readings for current billing cycle yet.")
            return
        interval      = 0.25
        import_cost   = 0.0
        export_credit = 0.0
        for ts, grid_kw, _home_kw, _solar_kw in readings:
            try:
                dt = datetime.fromisoformat(ts)
            except Exception:
                continue
            r = rate_at(dt)
            if grid_kw > 0:
                import_cost   += grid_kw * interval * r
            elif grid_kw < 0:
                export_credit += abs(grid_kw) * interval * r
        net_actual    = import_cost - export_credit
        daily_net     = net_actual / days_so_far
        projected_net = daily_net * 30
        projected_imp = import_cost / days_so_far * 30
        projected_exp = export_credit / days_so_far * 30
        cycle_label   = f"{cycle_start.strftime('%b %-d')} – {now.date().strftime('%b %-d')}"
        self._send(chat_id,
            f"💡 Billing Cycle — {cycle_label} ({days_so_far} days)\n"
            f"Grid import:  ${import_cost:.2f}\n"
            f"Grid export:  ${export_credit:.2f}\n"
            f"Net so far:   ${net_actual:.2f}\n\n"
            f"Projected full cycle (~30 days):\n"
            f"  Import:  ${projected_imp:.2f}\n"
            f"  Export:  ${projected_exp:.2f}\n"
            f"  Net:     ${projected_net:.2f}  (${daily_net:.2f}/day avg)"
        )

    def _send_tip(self, chat_id: str) -> None:
        with self._lock:
            stats   = self._stats
            outlook = self._outlook
        if stats is None:
            self._send(chat_id, "No data yet — advisor hasn't completed its first check.")
            return
        from .tou import TouPeriod, period_at, on_peak_window, rate_at
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
            msg = (f"⚠️ Importing {grid:.1f} kW during on-peak (${rate:.3f}/kWh).\n"
                   f"Battery at {soc:.0f}% — reduce non-essential loads if possible.")
        elif period == TouPeriod.ON_PEAK and soc < 20:
            msg = (f"🔴 Battery critical ({soc:.0f}%) during peak.\n"
                   f"Shed non-essential loads to extend backup duration.")
        elif period == TouPeriod.ON_PEAK and soc >= 80:
            msg = (f"🟢 Well positioned for on-peak — {soc:.0f}% SoC, "
                   f"solar {solar:.1f} kW. No action needed.")
        elif period in (TouPeriod.OFF_PEAK, TouPeriod.SUPER_OFF_PEAK) and soc < 30 and 0 < mins_to_peak < 120:
            msg = (f"🟡 Battery at {soc:.0f}% with on-peak in {mins_to_peak // 60}h {mins_to_peak % 60}m.\n"
                   f"Switch to Emergency Backup now to charge before 4 pm.")
        elif period == TouPeriod.SUPER_OFF_PEAK and soc < 50 and solar < 1.0:
            msg = (f"💡 Super off-peak now (cheapest rate: ${rate:.3f}/kWh).\n"
                   f"Battery at {soc:.0f}%, solar low — good time for Emergency Backup.")
        elif solar > 3.0 and soc >= 95:
            msg = (f"🌞 Solar producing {solar:.1f} kW and battery full ({soc:.0f}%).\n"
                   f"Self-Consumption mode is optimal — excess going to grid.")
        else:
            msg = (f"✅ All looks good — {soc:.0f}% SoC, {solar:.1f} kW solar, "
                   f"grid {grid:+.1f} kW ({period.value.replace('_', ' ')}).")
        self._send(chat_id, msg)

    def _handle(self, chat_id: str, text: str) -> None:
        try:
            with self._lock:
                stats   = self._stats
                store   = self._hist_store
                outlook = self._outlook
            ctx    = build_context(stats, store, outlook, self._cfg)
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
        import anthropic
        client  = anthropic.Anthropic(api_key=self._api_key)
        history = list(self._convos.get(chat_id, []))

        history.append({"role": "user", "content": f"{context}\n\nQuestion: {question}"})
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=history,
        )
        reply = resp.content[0].text
        history.append({"role": "assistant", "content": reply})
        self._convos[chat_id] = history[-(_MAX_TURNS * 2):]
        return reply

    def _call_ollama(self, chat_id: str, question: str, context: str) -> str:
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

    def _send(self, chat_id: str, text: str) -> None:
        url  = f"https://api.telegram.org/bot{self._cfg.telegram_bot_token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": text}).encode()
        req  = Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            urlopen(req, timeout=10)
        except Exception as e:
            logger.warning("Chatbot send error: %s", e)
