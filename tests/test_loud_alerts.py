"""Loud-alert control: which Telegram alerts may vibrate the phone and watch.

Everything is silent except urgent alerts; the user can promote others with /loud.
The list lives in .peak_alert_state.json (same lock and file as /mute and /snooze), with an
optional static `loud_alerts` in the config.
"""

from __future__ import annotations

from franklinwh_scraper import alerts
from franklinwh_scraper.alerts import _load_peak_state
from franklinwh_scraper.chatbot import TelegramChatBot
from franklinwh_scraper.config import Config


def test_is_loud_truth_table():
    cfg = Config(loud_alerts=["car_on_peak"])
    assert alerts._is_loud("grid_down", Config())                       # urgent is always loud
    assert alerts._is_loud("car_on_peak", cfg)                          # promoted in config
    assert alerts._is_loud("evening_short", Config(), {"loud_alerts": ["evening_short"]})  # promoted in state
    assert not alerts._is_loud("evening_short", Config(), {})
    assert not alerts._is_loud("evening_short", Config(), {"loud_alerts": "garbage"})  # malformed state ignored


def test_set_loud_validates_names_and_toggles():
    state: dict = {}
    ok, msg = alerts.set_loud(state, "Low-SoC-1pm", True)
    assert ok and state["loud_alerts"] == ["low_soc_1pm"] and "low_soc_1pm" in msg
    ok, _ = alerts.set_loud(state, "low_soc_1pm", True)        # idempotent, no duplicate
    assert ok and state["loud_alerts"] == ["low_soc_1pm"]
    ok, msg = alerts.set_loud(state, "not_a_real_alert", True)
    assert not ok and "Unknown alert" in msg
    ok, msg = alerts.set_loud(state, "grid_down", True)        # already always loud
    assert ok and "always" in msg.lower() and state["loud_alerts"] == ["low_soc_1pm"]
    ok, _ = alerts.set_loud(state, "low_soc_1pm", False)
    assert ok and "loud_alerts" not in state


def test_send_alert_loud_overrides_silent_without_being_urgent(monkeypatch):
    seen = []
    monkeypatch.setattr(alerts, "notify_telegram",
                        lambda body, tok, chat, reply_markup=None, silent=False: seen.append(silent))
    monkeypatch.setattr(alerts, "_log_alert", lambda *a, **k: None)
    cfg = Config(telegram_bot_token="t", telegram_chat_id="1")
    alerts._send_alert("a", cfg, alert_name="x")                  # routine: silent
    alerts._send_alert("b", cfg, alert_name="x", loud=True)       # promoted: buzzes
    alerts._send_alert("c", cfg, urgent=True, alert_name="x")     # urgent: buzzes
    alerts._send_alert("d", cfg, urgent=True, alert_name="x", loud=False)  # explicit wins
    assert seen == [True, False, False, True]


def test_dispatcher_buzzes_only_for_loud_alerts(tmp_path, monkeypatch):
    """_check_peak_alerts must hand the promoted alert to _send_alert as loud."""
    import types
    sent = []
    monkeypatch.setattr(alerts, "_send_alert",
                        lambda body, cfg, urgent=False, alert_name=None, loud=None, **_k:
                        sent.append((alert_name, urgent, loud)))
    c = types.SimpleNamespace(
        battery_soc_pct=80.0, home_load_kw=1.0, solar_production_kw=0.0,
        battery_use_kw=1.0, grid_use_kw=0.0, grid_status="down",
        generator_production_kw=0.0, generator_enabled=False,
    )
    totals = types.SimpleNamespace(
        solar_kwh=0.0, battery_charge_kwh=0.0, battery_discharge_kwh=0.0, grid_load_kwh=0.0,
        grid_export_kwh=0.0, home_use_kwh=0.0, grid_import_kwh=0.0, generator_kwh=0.0,
        battery_load_kwh=0.0, solar_load_kwh=0.0)
    stats = types.SimpleNamespace(current=c, totals=totals)
    cfg = Config(telegram_bot_token="t", telegram_chat_id="c", loud_alerts=["grid_restored"])
    alerts._check_peak_alerts(stats, cfg, tmp_path)
    by_name = {n: (u, loud) for n, u, loud in sent}
    assert by_name["grid_down"] == (True, True)  # urgent alerts are loud


def test_bot_loud_command_roundtrip(tmp_path):
    bot = TelegramChatBot(Config(output_dir=str(tmp_path)), api_key="x")
    assert "low_soc_1pm" in bot._loud_command("/loud low_soc_1pm")
    assert _load_peak_state(tmp_path)["loud_alerts"] == ["low_soc_1pm"]
    listing = bot._loud_command("/loud")
    assert "Buzzing: low_soc_1pm" in listing
    assert "no longer" in bot._loud_command("/quiet low_soc_1pm").lower()
    assert "loud_alerts" not in _load_peak_state(tmp_path)
    assert "Unknown alert" in bot._loud_command("/loud nonsense_name")


def test_bot_loud_buttons_toggle_via_callback(tmp_path):
    bot = TelegramChatBot(Config(output_dir=str(tmp_path)), api_key="x")
    kb = bot._loud_keyboard()
    datas = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert "loud:low_soc_1pm:1" in datas
    ok, _ = bot._set_loud("low_soc_1pm", True)
    assert ok
    kb = bot._loud_keyboard()
    datas = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert "loud:low_soc_1pm:0" in datas  # a loud alert now offers the way back
