"""FranklinWH scraper — polished CLI."""

from __future__ import annotations

import atexit
import json
import logging
import os
import stat
import time
from datetime import datetime, timedelta
from pathlib import Path


import click

logger = logging.getLogger(__name__)

from .account import AccountClient
from .advisor import Mode, recommend
from .alerts import (
    _BATTERY_CAPACITY_KWH,
    _GHI_CLOUDY_THRESHOLD,
    _SOLAR_CAL_LOG_FILE,
    _check_peak_alerts,
    _get_hourly_bias,
    _get_performance_ratio,
    _get_system_peak_kw,
    _load_peak_state,
    _ping_healthcheck,
    _save_peak_state,
    _state_lock,
)
from .chatbot import TelegramChatBot
from .client import FranklinWHClient
from .config import Config, load as load_config, save as save_config
from .exporters import export_csv, export_json

from .history import HistoryStore
from .license import ENFORCE_LICENSE, check_license
from .notifier import (notify_email, notify_imessage, notify_log,
                       notify_macos, notify_telegram, notify_webhook,
                       fetch_telegram_chat_id, rec_to_text)
from .predictor import predict
from .scrapers import FAQScraper, ProductsScraper, SupportScraper
from .tou import cycle_bounds
from .weather import fetch_solar_outlook_cached as _fetch_outlook_cached, geocode


# ── Helpers ──────────────────────────────────────────────────────────

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def _ok(msg: str)   -> None: click.echo(click.style(f"  ✓  {msg}", fg="green"))
def _warn(msg: str) -> None: click.echo(click.style(f"  ⚠  {msg}", fg="yellow"))
def _err(msg: str)  -> None: click.echo(click.style(f"  ✗  {msg}", fg="red"))
def _info(msg: str) -> None: click.echo(f"     {msg}")
def _hr()           -> None: click.echo(click.style("─" * 60, dim=True))
def _header(title: str) -> None:
    click.echo()
    click.echo(click.style(f"  {title}", bold=True))
    _hr()


_PID_FILE = Path.home() / ".franklinwh.pid"


def _acquire_pid_lock() -> bool:
    """Return True if no other watcher is running; register cleanup on exit."""
    if _PID_FILE.exists():
        try:
            pid = int(_PID_FILE.read_text().strip())
            os.kill(pid, 0)
            return False  # process alive
        except (ValueError, ProcessLookupError, PermissionError):
            _PID_FILE.unlink(missing_ok=True)  # stale — remove before atomic create
    # Exclusive create is atomic — fails with FileExistsError if another process wins the race
    try:
        with open(_PID_FILE, "x") as f:
            f.write(str(os.getpid()))
        atexit.register(_release_pid_lock)
        return True
    except FileExistsError:
        return False


def _release_pid_lock() -> None:
    try:
        _PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _load_last_mode(out: Path) -> str | None:
    """Read the last recommended mode from disk (persists across cron runs)."""
    p = out / ".last_recommendation.json"
    try:
        return json.loads(p.read_text()).get("mode")
    except (OSError, json.JSONDecodeError):
        return None


def _save_last_mode(out: Path, mode: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / ".last_recommendation.json").write_text(json.dumps({"mode": mode}))


def _write_health_marker(out: Path, consec_errors: int, last_error: str | None) -> None:
    """Persist the watch loop's own error-streak tracking so webapi.py's
    dashboard can show FranklinWH API outage status without needing to
    check advisor.log — the CLI already detects and reports this over
    Telegram, this just makes the same signal visible on the dashboard."""
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / ".health.json").write_text(json.dumps({
            "consec_errors": consec_errors,
            "last_error": last_error,
            "updated": datetime.now().isoformat(),
        }))
    except OSError:
        pass


def _read_consec_errors(out: Path) -> int:
    """Load the error streak persisted by the last process, so a cron-based
    (non --watch) install — where every invocation is a fresh short-lived
    process — can still accumulate a multi-cycle streak instead of the
    counter silently resetting to 0 on every single run."""
    try:
        data = json.loads((out / ".health.json").read_text())
        return int(data.get("consec_errors", 0))
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return 0



def _dispatch_notifications(rec, cfg: Config, notify_flag: bool, last_mode: str | None, outdir: Path | None = None) -> None:
    """Send macOS + iMessage notifications when the recommendation changes or is critical."""
    changed  = rec.mode.value != last_mode
    critical = rec.urgency == "critical"

    if not (changed or critical):
        return

    # Never notify for NO_CHANGE — "Battery OK" messages are noise.
    if not rec.needs_action and not critical:
        return

    # Self-Consumption is the user's assumed baseline mode at all times, so
    # "switch to Self-Consumption" is never actionable news — it's either
    # reverting from a rare Emergency-Backup excursion or confirming a
    # default the user never left. Emergency Backup (the only mode the user
    # actually switches into, and only for super-off-peak charging) still
    # notifies normally below.
    if rec.mode == Mode.SELF_CONSUMPTION:
        return

    # Suppress info-level alerts during quiet hours (midnight–7am).
    if rec.urgency == "info" and not critical and datetime.now().hour < 7:
        logger.debug("Suppressing info alert during quiet hours: %s", rec.mode.value)
        return

    # Mode-change alerts fire at most once per day per mode to stop oscillation noise.
    if outdir is not None:
        with _state_lock(outdir):
            state = _load_peak_state(outdir)
            today = datetime.now().strftime("%Y-%m-%d")
            key   = f"alerted_{rec.mode.value}_date"
            if state.get(key) == today:
                return
            state[key] = today
            _save_peak_state(outdir, state)

    if notify_flag:
        notify_macos(rec)

    if cfg.imessage_phone:
        notify_imessage(rec, cfg.imessage_phone)

    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        notify_telegram(rec_to_text(rec), cfg.telegram_bot_token, cfg.telegram_chat_id)

    if cfg.smtp_host and cfg.email_to:
        notify_email(rec_to_text(rec), cfg)

    if cfg.webhook_url:
        notify_webhook(rec_to_text(rec), critical, cfg)


def _resolve_gateway(client: AccountClient, gateway: str) -> str:
    if gateway:
        return gateway
    gateways = client.get_gateways()
    if not gateways:
        raise click.ClickException("No gateways found on this account.")
    gw_obj = gateways[0]
    gid = gw_obj.get("gatewayId") or gw_obj.get("id", "")
    _info(f"Gateway: {gid}")
    return gid


def _require_config(cfg: Config) -> None:
    if not cfg.is_complete():
        raise click.ClickException(
            "Setup not complete. Run:  python3.13 scrape.py setup"
        )


# ── Root group ───────────────────────────────────────────────────────

@click.group()
@click.option("--verbose", "-v", is_flag=True)
@click.option("--delay", default=1.5, hidden=True)
@click.pass_context
def cli(ctx: click.Context, verbose: bool, delay: float) -> None:
    """FranklinWH energy scraper & battery advisor."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["delay"]  = delay
    ctx.obj["config"] = load_config()


# ── Setup wizard ─────────────────────────────────────────────────────

@cli.command()
@click.option("--quick", is_flag=True, default=False,
              help="On a re-run, skip the AI Chatbot / EV charging / Tesla "
                   "/ Uptime monitoring sections and leave them exactly as "
                   "configured — for touching up one thing (credentials, "
                   "location, alert toggles) without wading through the "
                   "whole wizard again. Has no effect on first-time setup.")
def setup(quick: bool) -> None:
    """Interactive setup — saves your credentials and location once."""
    cfg = load_config()
    # Captured before any prompts run: whether this is a genuine re-run
    # (--quick only skips sections when there's something configured to
    # skip — a first-time setup always gets the full wizard).
    quick = quick and cfg.is_complete()

    click.echo()
    click.echo(click.style("  FranklinWH Setup Wizard", bold=True, fg="cyan"))
    _hr()
    click.echo("  Credentials are saved to ~/.franklinwh.json (chmod 600).")
    click.echo("  Press Enter to keep the current value shown in [brackets].")
    if quick:
        click.echo(click.style(
            "  --quick: skipping Chatbot / EV / Tesla / Uptime sections "
            "(already configured).", dim=True))
    click.echo()

    # ── Credentials ──────────────────────────────────────────────────
    click.echo(click.style("  Account", bold=True))
    cfg.email    = click.prompt("  Email",    default=cfg.email or "")
    cfg.password = click.prompt("  Password", default=cfg.password or "",
                                hide_input=True, confirmation_prompt=not cfg.password)

    # Test login
    click.echo()
    click.echo("  Testing login…", nl=False)
    try:
        with AccountClient(cfg.email, cfg.password) as client:
            client.login()
            gateways = client.get_gateways()
        click.echo(click.style(" OK", fg="green"))
        if gateways:
            gw_obj  = gateways[0]
            cfg.gateway = gw_obj.get("gatewayId") or gw_obj.get("id", "")
            _ok(f"Gateway detected: {cfg.gateway}")
        else:
            _warn("No gateways found — you can add one later.")
    except Exception as e:
        # Was `except ValueError` only — login()/get_gateways() can also
        # raise RuntimeError (non-standard API response code) or a raw
        # requests/JSON exception (network blip, API 5xx, maintenance
        # window), none of which are ValueError. Any of those used to crash
        # the wizard with a raw traceback before save_config() ever ran,
        # discarding every answer already entered — broadened to match the
        # same graceful "FAILED, continue anyway?" recovery the ValueError
        # case already had.
        click.echo(click.style(" FAILED", fg="red"))
        _err(str(e))
        if not click.confirm("  Continue saving anyway?", default=False):
            raise SystemExit(1)

    # ── Location ─────────────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  Location  (for solar forecast)", bold=True))

    if cfg.location_name:
        click.echo(f"  Current: {cfg.location_name} ({cfg.lat:.4f}, {cfg.lon:.4f})")

    while True:
        city = click.prompt(
            "  City or town",
            default=cfg.location_name or "",
        )
        click.echo(f'  Looking up "{city}"…', nl=False)
        loc = geocode(city)
        if loc:
            click.echo(click.style(f" Found: {loc.name}, {loc.country} "
                                   f"({loc.lat:.4f}, {loc.lon:.4f})", fg="green"))
            cfg.lat           = loc.lat
            cfg.lon           = loc.lon
            cfg.location_name = f"{loc.name}, {loc.country}"
            break
        else:
            click.echo(click.style(" Not found.", fg="red"))
            click.echo("  Try a larger nearby city, or enter coordinates manually.")
            if click.confirm("  Enter coordinates manually?", default=False):
                cfg.lat = click.prompt("  Latitude",  type=float, default=cfg.lat)
                cfg.lon = click.prompt("  Longitude", type=float, default=cfg.lon)
                cfg.location_name = f"{cfg.lat:.4f}, {cfg.lon:.4f}"
                break

    # ── Notifications ────────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  Notifications", bold=True))
    click.echo("  Choose how you want to receive alerts. You can enable multiple channels.")
    click.echo()

    # ── Telegram ─────────────────────────────────────────────────────
    click.echo(click.style("  Telegram", bold=True) + "  — free, cross-platform, recommended")
    click.echo("    1. Message @BotFather on Telegram → /newbot → copy the token")
    click.echo("    2. Send any message to your new bot")
    click.echo("    3. Paste the token below (chat ID is auto-detected)")
    click.echo()
    tg_token = click.prompt(
        "  Bot token (leave blank to skip)",
        default=cfg.telegram_bot_token or "",
        hide_input=True,
    ).strip()
    if tg_token:
        cfg.telegram_bot_token = tg_token
        click.echo("  Send any message to your bot in Telegram now, then wait…")
        click.echo("  Detecting your Telegram chat ID (up to ~9 seconds)…", nl=False)
        chat_id = fetch_telegram_chat_id(tg_token)
        if chat_id:
            cfg.telegram_chat_id = chat_id
            click.echo(click.style(f" Found (chat ID: {chat_id})", fg="green"))
            _ok("Telegram alerts configured")
        else:
            click.echo(click.style(" Not found", fg="yellow"))
            _warn("Could not detect chat ID automatically.")
            cfg.telegram_chat_id = click.prompt(
                "  Enter chat ID manually (visit t.me/userinfobot to find yours)",
                default=cfg.telegram_chat_id or "",
            ).strip()
    else:
        cfg.telegram_bot_token = ""
        cfg.telegram_chat_id   = ""

    # ── Email ─────────────────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  Email", bold=True) + "  — SMTP (Gmail, Outlook, any provider)")
    click.echo("    Gmail tip: use an App Password (myaccount.google.com → Security → App Passwords)")
    click.echo()
    email_to = click.prompt(
        "  Recipient email (leave blank to skip)",
        default=cfg.email_to or "",
    ).strip()
    if email_to:
        cfg.email_to   = email_to
        cfg.smtp_host  = click.prompt("  SMTP host",     default=cfg.smtp_host  or "smtp.gmail.com").strip()
        cfg.smtp_port  = click.prompt("  SMTP port",     default=cfg.smtp_port  or 587, type=int)
        cfg.smtp_user  = click.prompt("  SMTP username", default=cfg.smtp_user  or email_to).strip()
        cfg.smtp_password = click.prompt(
            "  SMTP password / app password", default=cfg.smtp_password or "",
            hide_input=True,
        ).strip()
        cfg.email_from = click.prompt(
            "  From address", default=cfg.email_from or email_to,
        ).strip()
        # Test
        click.echo("  Sending test email…", nl=False)
        try:
            if notify_email("FranklinWH advisor connected ✓\nThis is your test message.", cfg):
                click.echo(click.style(" Sent!", fg="green"))
                _ok("Email alerts configured")
            else:
                click.echo(click.style(" Failed", fg="red"))
                _warn("Email saved but test failed — double-check your credentials (see advisor.log for details).")
        except Exception as e:
            click.echo(click.style(f" Failed: {e}", fg="red"))
            _warn("Email saved but test failed — double-check your credentials.")
    else:
        cfg.email_to = cfg.email_from = cfg.smtp_host = cfg.smtp_user = cfg.smtp_password = ""

    # ── Webhook ───────────────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  Webhook", bold=True) + "  — POST JSON to Slack, Discord, or any custom URL")
    click.echo("    Payload: {\"alert\": \"...\", \"urgent\": bool, \"timestamp\": \"ISO8601\"}")
    click.echo()
    wh = click.prompt(
        "  Webhook URL (leave blank to skip)",
        default=cfg.webhook_url or "",
    ).strip()
    if wh:
        cfg.webhook_url = wh
        click.echo("  Sending test webhook…", nl=False)
        try:
            if notify_webhook("FranklinWH advisor connected ✓  This is your test message.", False, cfg):
                click.echo(click.style(" Sent!", fg="green"))
                _ok("Webhook configured")
            else:
                click.echo(click.style(" Failed", fg="red"))
                _warn("Webhook saved but test failed — check the URL (see advisor.log for details).")
        except Exception as e:
            click.echo(click.style(f" Failed: {e}", fg="red"))
            _warn("Webhook saved but test failed — check the URL.")
    else:
        cfg.webhook_url = ""

    # ── iMessage ─────────────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  iMessage", bold=True) + "  — macOS only")
    click.echo()
    phone = click.prompt(
        "  Phone number (e.g. +19255884276, leave blank to skip)",
        default=cfg.imessage_phone or "",
    ).strip()
    cfg.imessage_phone = phone if phone else ""
    if cfg.imessage_phone:
        import sys as _sys
        if _sys.platform != "darwin":
            _warn("iMessage only works on macOS — saved but won't send on this OS.")
        else:
            _ok(f"iMessage alerts will be sent to {cfg.imessage_phone}")

    if not any([cfg.telegram_chat_id, cfg.email_to, cfg.webhook_url, cfg.imessage_phone]):
        _warn("No notification channels configured — you won't receive any alerts.")

    # ── AI Chatbot ────────────────────────────────────────────────────
    if cfg.telegram_bot_token and cfg.telegram_chat_id and not quick:
        click.echo()
        click.echo(click.style("  AI Chatbot (optional)", bold=True))
        click.echo('  Answer questions like "How much did I save this week?" in Telegram.')
        click.echo()
        backend = click.prompt(
            "  Chat backend",
            type=click.Choice(["anthropic", "ollama", "none"]),
            default=cfg.chat_backend if cfg.chat_backend in ("anthropic", "ollama") else "none",
        )
        if backend == "anthropic":
            click.echo("  Get an API key at console.anthropic.com → API Keys (free credits available)")
            ak = click.prompt(
                "  Anthropic API key",
                default=cfg.anthropic_api_key or "",
                hide_input=True,
            ).strip()
            cfg.anthropic_api_key = ak
            cfg.chat_backend = "anthropic"
            _ok("Anthropic chatbot enabled")
        elif backend == "ollama":
            cfg.chat_backend = "ollama"
            cfg.ollama_model = click.prompt("  Ollama model", default=cfg.ollama_model or "llama3.1:8b").strip()
            cfg.ollama_url   = click.prompt("  Ollama URL",   default=cfg.ollama_url or "http://localhost:11434").strip()
            _ok(f"Ollama chatbot enabled (model: {cfg.ollama_model})")
            _info("Make sure Ollama is running: ollama serve")
        else:
            cfg.chat_backend = "none"
            _info("Chatbot disabled")

    # ── Alert preferences ─────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  Alert preferences", bold=True))
    click.echo("  Safety alerts (grid outage, fast battery drain) are always on.")
    click.echo("  Toggle the optional groups below — you can change these later in ~/.franklinwh.json.")
    click.echo()

    _ALERT_GROUPS: list[tuple[str, str, list[str]]] = [
        (
            "Morning briefing",
            "Daily preview with today's solar forecast, pre-charge advice, and peak solar window",
            ["morning_preview"],
        ),
        (
            "Peak-hour monitoring",
            "Alerts during 4–9 pm: grid import, low SoC, battery not charging, export opportunity, EV charging window",
            ["grid_import", "eb_ready", "low_soc_1pm", "low_noon_soc",
             "low_morning_solar", "solar_stopped", "not_charging", "export_arbitrage",
             "ev_charge_window"],
        ),
        (
            "Daily / weekly reports",
            "End-of-day digest, weekly TOU cost summary, monthly billing cycle, bill projection",
            ["eod_digest", "weekly_summary", "monthly_summary", "bill_projection"],
        ),
        (
            "Battery health",
            "Capacity fade, solar degradation, always-on load drift, heat wave & storm prep, surplus export, 2-day cloudy warning",
            ["solar_degradation", "solar_back_to_baseline",
             "capacity_fade", "baseline_load_drift", "peak_streak", "heat_wave_prep", "storm_prep",
             "multiday_cloudy_precharge", "solar_surplus_overflow"],
        ),
    ]

    cfg.disabled_alerts = list(cfg.disabled_alerts or [])
    for group_name, group_desc, members in _ALERT_GROUPS:
        currently_on = not any(m in cfg.disabled_alerts for m in members)
        click.echo(f"  {click.style(group_name, bold=True)}")
        click.echo(f"    {group_desc}")
        enabled = click.confirm("    Enable?", default=currently_on)
        if enabled:
            for m in members:
                if m in cfg.disabled_alerts:
                    cfg.disabled_alerts.remove(m)
            if click.confirm("    Customize individual alerts in this group?", default=False):
                for m in members:
                    on = click.confirm(f"      {m}?", default=m not in cfg.disabled_alerts)
                    if on and m in cfg.disabled_alerts:
                        cfg.disabled_alerts.remove(m)
                    elif not on and m not in cfg.disabled_alerts:
                        cfg.disabled_alerts.append(m)
        else:
            for m in members:
                if m not in cfg.disabled_alerts:
                    cfg.disabled_alerts.append(m)
        click.echo()

    if cfg.disabled_alerts:
        _info(f"Disabled alerts: {', '.join(sorted(cfg.disabled_alerts))}")
    else:
        _info("All optional alerts enabled")

    # ── Battery & system ──────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  Battery & system", bold=True))
    _BATTERY_MODELS = [
        ("aPower 10",       10.0),
        ("aPower 15",       15.0),
        ("2× aPower 10",    20.0),
        ("2× aPower 15",    30.0),
        ("Enter manually",  None),
    ]
    click.echo()
    click.echo("  Battery model:")
    for i, (name, kwh) in enumerate(_BATTERY_MODELS, 1):
        kwh_str = f"  ({kwh} kWh)" if kwh else ""
        click.echo(f"    {i}. {name}{kwh_str}")
    model_choice = click.prompt(
        "  Select model",
        type=click.IntRange(1, len(_BATTERY_MODELS)),
        default=next(
            (i for i, (_, k) in enumerate(_BATTERY_MODELS, 1) if k == cfg.battery_capacity_kwh),
            len(_BATTERY_MODELS),
        ),
    )
    chosen_kwh = _BATTERY_MODELS[model_choice - 1][1]
    if chosen_kwh is None:
        cfg.battery_capacity_kwh = click.prompt(
            "  Battery usable capacity (kWh)", type=float,
            default=cfg.battery_capacity_kwh,
        )
    else:
        cfg.battery_capacity_kwh = chosen_kwh
        _ok(f"Battery set to {cfg.battery_capacity_kwh} kWh")

    click.echo()
    while True:
        _inst = click.prompt(
            "  System install date (YYYY-MM-DD, blank if unsure)",
            default=cfg.install_date or "", show_default=False,
        ).strip()
        if not _inst:
            cfg.install_date = ""
            _info("Skipped — lifetime cycle counts will be reported since tracking start.")
            break
        try:
            datetime.strptime(_inst, "%Y-%m-%d")
            cfg.install_date = _inst
            break
        except ValueError:
            _warn("Use YYYY-MM-DD, or leave blank.")

    click.echo()
    click.echo("  Your utility bills on a per-meter read date, not a fixed")
    click.echo("  company-wide day — check the 'service period' on your bill.")
    while True:
        _day = click.prompt(
            "  Day of month your billing cycle starts", type=int,
            default=cfg.billing_cycle_start_day or 20,
        )
        if 1 <= _day <= 31:
            cfg.billing_cycle_start_day = _day
            _cs, _ce = cycle_bounds(datetime.now().date(), _day)
            _ok(f"Current cycle: {_cs:%b %-d} – {_ce:%b %-d} ({(_ce - _cs).days + 1} days)")
            break
        _warn("Enter a day between 1 and 31.")

    cfg.output_dir = click.prompt("  Output directory", default=cfg.output_dir)

    # ── EV charging ───────────────────────────────────────────────────
    if not quick:
        click.echo()
        cfg.ev_charging = click.confirm(
            "  Do you charge an EV at home? (enables off-peak charging advice)",
            default=cfg.ev_charging,
        )
    if cfg.ev_charging and not quick:
        cfg.ev_kwh_per_session = click.prompt(
            "  Typical kWh per charge (0 if unsure)", type=float,
            default=cfg.ev_kwh_per_session or 0.0,
        )
        cfg.ev_charging_kw = click.prompt(
            "  Typical charging draw in kW (shown as a 'with EV charging'\n"
            "  SoC estimate in the evening digest)",
            type=float, default=cfg.ev_charging_kw,
        )
        click.echo()
        cfg.ev_control_enabled = click.confirm(
            "  Tesla owners: automatically adjust charging speed to track "
            "solar surplus?\n  (one-time Tesla developer setup required — "
            "see docs/TESLA_SETUP.md)",
            default=cfg.ev_control_enabled,
        )
        if cfg.ev_control_enabled:
            cfg.tesla_vin = click.prompt(
                "  Tesla VIN", default=cfg.tesla_vin or "").strip()
            cfg.tesla_client_id = click.prompt(
                "  Tesla developer app client ID",
                default=cfg.tesla_client_id or "").strip()
            cfg.ev_max_amps = click.prompt(
                "  Max charging amps (check your charger's breaker)",
                type=int, default=cfg.ev_max_amps)
            cfg.ev_battery_first_soc = click.prompt(
                "  Home battery SoC before EV gets surplus (%)",
                type=float, default=cfg.ev_battery_first_soc)
            from .tesla import TESLA_TOKEN_PATH
            if not TESLA_TOKEN_PATH.exists():
                _warn("Not yet authorized with Tesla — after finishing "
                      "docs/TESLA_SETUP.md, run: franklinwh tesla auth")
            _warn("Starting in DRY-RUN mode: decisions are logged but no "
                  "commands are sent. Disable with: franklinwh tesla go-live")

    # ── Uptime monitoring ─────────────────────────────────────────────
    if not quick:
        click.echo()
        click.echo(click.style("  Uptime monitoring (optional)", bold=True))
        click.echo("  Get a free ping URL at healthchecks.io — you'll be notified if the advisor stops running.")
        cfg.healthcheck_url = click.prompt(
            "  Healthcheck ping URL (leave blank to skip)",
            default=cfg.healthcheck_url or "",
        ).strip()

    # ── Save ─────────────────────────────────────────────────────────
    save_config(cfg)
    click.echo()
    _ok("Configuration saved to ~/.franklinwh.json")
    click.echo()

    import sys as _sys
    if _sys.platform == "darwin":
        click.echo("  Start the advisor:")
        click.echo(click.style("      franklinwh install-service", fg="cyan", bold=True))
        click.echo("  (installs a LaunchAgent that runs automatically on login)")
    else:
        click.echo("  Add a cron job to run the advisor every 5 minutes (7am–11pm):")
        click.echo(click.style(
            "      (crontab -l; echo '*/5 7-23 * * * franklinwh account advise >> ~/franklinwh.log 2>&1') | crontab -",
            fg="cyan", bold=True,
        ))
    click.echo()


# ── Doctor ───────────────────────────────────────────────────────────

@cli.command()
def doctor() -> None:
    """Check your configuration and connectivity."""
    from franklinwh_scraper.config import CONFIG_PATH
    import pathlib

    click.echo()
    click.echo(click.style("  FranklinWH Doctor", bold=True, fg="cyan"))
    _hr()

    def _check(label: str, ok: bool, detail: str = "") -> None:
        mark  = click.style("✓", fg="green") if ok else click.style("✗", fg="red")
        extra = f"  {detail}" if detail else ""
        click.echo(f"  {mark}  {label}{extra}")

    cfg = load_config()

    _check("Config file exists",   CONFIG_PATH.exists(),       str(CONFIG_PATH))
    _check("Email configured",     bool(cfg.email))
    _check("Password set",         bool(cfg.password))
    _check("Location set",         bool(cfg.lat and cfg.lon),  f"{cfg.lat:.4f}, {cfg.lon:.4f}" if cfg.lat else "")
    _bcs, _bce = cycle_bounds(datetime.now().date(), cfg.billing_cycle_start_day)
    _check("Billing cycle", True,
           f"day {cfg.billing_cycle_start_day} — current: {_bcs:%b %-d} – {_bce:%b %-d}")
    _check("Install date", bool(cfg.install_date),
           cfg.install_date or "not set — cycle counts shown since tracking start")

    # At least one notification channel
    has_channel = bool(
        cfg.imessage_phone or (cfg.telegram_bot_token and cfg.telegram_chat_id)
        or (cfg.smtp_host and cfg.email_to) or cfg.webhook_url
    )
    _check("Notification channel", has_channel)
    _check("Uptime monitoring",    bool(cfg.healthcheck_url),
           "configured" if cfg.healthcheck_url else "optional — set up at healthchecks.io")

    # EV draw estimate — feeds the digest's "with EV charging" SoC line
    # whenever cfg.ev_charging is set, independent of Tesla control below.
    if getattr(cfg, "ev_charging", False):
        _ev_kw = getattr(cfg, "ev_charging_kw", 7.6)
        _check("EV charging draw estimate", _ev_kw != 7.6,
               f"{_ev_kw:.1f} kW"
               if _ev_kw != 7.6 else
               "still the 7.6 kW default guess — run `franklinwh setup` "
               "and set it to your actual charging amperage for an "
               "accurate 'with EV' digest line")

    # EV charge control (optional feature — only checked when enabled)
    if getattr(cfg, "ev_control_enabled", False):
        from .tesla import TESLA_KEY_PATH, TESLA_TOKEN_PATH
        _check("EV control: VIN + client ID",
               bool(cfg.tesla_vin and cfg.tesla_client_id))
        _check("EV control: Tesla tokens", TESLA_TOKEN_PATH.exists(),
               str(TESLA_TOKEN_PATH) if TESLA_TOKEN_PATH.exists()
               else "run: franklinwh tesla auth")
        _check("EV control: command signing key", TESLA_KEY_PATH.exists(),
               str(TESLA_KEY_PATH) if TESLA_KEY_PATH.exists()
               else "see docs/TESLA_SETUP.md")
        _check("EV control: mode", True,
               "DRY RUN (logging only)" if cfg.ev_dry_run else "LIVE")
        _ev_state_path = pathlib.Path(cfg.output_dir) / ".ev_controller_state.json"
        try:
            _ev_state = json.loads(_ev_state_path.read_text())
            _last_err = _ev_state.get("last_error", "")
            _check("EV control: last error", not _last_err,
                   _last_err or "none")
        except (OSError, json.JSONDecodeError):
            _check("EV control: state", True, "no state yet (starts on first run)")

    # Output dir writable
    out = pathlib.Path(cfg.output_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / ".doctor_tmp").touch()
        (out / ".doctor_tmp").unlink()
        _check("Output directory writable", True, str(out.resolve()))
    except Exception as e:
        _check("Output directory writable", False, str(e))

    # History DB
    db_path = out / "history.db"
    _check("History database exists", db_path.exists(), str(db_path) if db_path.exists() else "(will be created on first run)")

    # API login
    if cfg.email and cfg.password:
        click.echo("  Checking FranklinWH API login…", nl=False)
        try:
            with AccountClient(cfg.email, cfg.password) as client:
                client.login()
                gateways = client.get_gateways()
            click.echo(click.style(" OK", fg="green"))
            _check("API login", True, f"{len(gateways)} gateway(s) found")
        except Exception as e:
            click.echo(click.style(f" FAILED: {e}", fg="red"))
            _check("API login", False)
    else:
        _check("API login", False, "credentials not set — run: franklinwh setup")

    click.echo()


# ── Tesla (EV charge control) ────────────────────────────────────────

@cli.group("tesla")
def grp_tesla() -> None:
    """Tesla Fleet API setup for EV charge control."""


@grp_tesla.command("keygen")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite an existing key pair.")
@click.option("--out", "-o", default=None,
              help="Where to write the public key for hosting (default: output/tesla-public-key.pem).")
@click.pass_context
def tesla_keygen(ctx: click.Context, force: bool, out: str | None) -> None:
    """Generate the EC key pair newer vehicles require for signed commands.

    Turns docs/TESLA_SETUP.md step 2's two raw openssl commands into one —
    writes the private key to ~/.franklinwh_tesla_key.pem (0600) and the
    public key to output/tesla-public-key.pem, then prints exactly what to
    host and where, matching the Vehicle Command protocol's required path.
    """
    from .tesla import TESLA_KEY_PATH

    cfg = ctx.obj["config"]

    if TESLA_KEY_PATH.exists() and not force:
        raise click.ClickException(
            f"{TESLA_KEY_PATH} already exists — pairing a new key with your "
            f"vehicle requires redoing the phone-app pairing step too, so "
            f"this won't overwrite silently. Re-run with --force if you "
            f"mean to replace it.")

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError as e:
        raise click.ClickException(
            "cryptography not installed — "
            "pip3 install 'franklinwh-scraper[ev]' or pip3 install cryptography") from e

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    fd = os.open(TESLA_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                 stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(private_pem)
    finally:
        TESLA_KEY_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)

    pub_path = Path(out) if out else Path(cfg.output_dir) / "tesla-public-key.pem"
    pub_path.parent.mkdir(parents=True, exist_ok=True)
    pub_path.write_bytes(public_pem)

    _ok(f"Private key written to {TESLA_KEY_PATH} (0600)")
    _ok(f"Public key written to {pub_path}")
    click.echo()
    click.echo("  Host the public key at exactly:")
    click.echo("  https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem")
    click.echo()
    click.echo("  Caddy route (add to your vantage-hub Caddyfile):")
    click.echo(click.style(
        f"  handle /.well-known/appspecific/com.tesla.3p.public-key.pem {{\n"
        f"      root * {pub_path.parent.resolve()}\n"
        f"      rewrite * /tesla-public-key.pem\n"
        f"      file_server\n"
        f"  }}", dim=True,
    ))
    click.echo()
    click.echo("  Then verify with:")
    click.echo("  curl -s https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem")
    click.echo()
    _ok("Next: docs/TESLA_SETUP.md steps 3-4 (partner registration, phone pairing), "
        "then `franklinwh tesla auth`.")


@grp_tesla.command("auth")
@click.pass_context
def tesla_auth(ctx: click.Context) -> None:
    """One-time OAuth: authorize this app with your Tesla account."""
    cfg = ctx.obj["config"]
    if not cfg.tesla_client_id:
        raise click.ClickException(
            "tesla_client_id not set — run `franklinwh setup` first "
            "(and see docs/TESLA_SETUP.md for the developer registration).")

    from .tesla import TESLA_TOKEN_PATH, build_login_url, exchange_code

    client_secret = click.prompt("  Tesla app client secret", hide_input=True)
    redirect_uri = click.prompt(
        "  Redirect URI (exactly as registered in the developer portal)")

    url = build_login_url(cfg.tesla_client_id, redirect_uri)
    click.echo()
    click.echo("  Open this URL in a browser, sign in, and approve:")
    click.echo(f"\n  {url}\n")
    click.echo("  After approving you land on the redirect URI — copy the "
               "full URL from the address bar (it contains ?code=...).")
    pasted = click.prompt("  Paste the full redirect URL (or just the code)")

    code = pasted.strip()
    if "code=" in code:
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(code).query)
        code = (qs.get("code") or [""])[0]
    if not code:
        raise click.ClickException("No authorization code found in the input.")

    exchange_code(cfg.tesla_client_id, client_secret, redirect_uri, code)
    _ok(f"Authorized — tokens saved to {TESLA_TOKEN_PATH}")
    _ok("Next: dry-run soak. Leave ev_dry_run on for a sunny day or two, "
        "then run: franklinwh tesla go-live")


@grp_tesla.command("go-live")
@click.pass_context
def tesla_go_live(ctx: click.Context) -> None:
    """Turn off dry-run mode — the controller starts sending real commands."""
    cfg = ctx.obj["config"]
    if not cfg.ev_control_enabled:
        raise click.ClickException("EV control is not enabled — run `franklinwh setup`.")
    if not cfg.ev_dry_run:
        _ok("Already live.")
        return
    if not click.confirm(
            "  The advisor will now adjust your Tesla's charging speed "
            "automatically. Continue?"):
        return
    cfg.ev_dry_run = False
    save_config(cfg)
    _ok("EV control is LIVE. Restart the advisor to pick it up:\n"
        "     launchctl stop com.franklinwh.advisor && "
        "launchctl start com.franklinwh.advisor")


# ── Dashboard ────────────────────────────────────────────────────────

@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8093, show_default=True, type=int)
@click.option("--reload", is_flag=True, default=False, hidden=True)
@click.pass_context
def dashboard(ctx: click.Context, host: str, port: int, reload: bool) -> None:
    """Serve the live web dashboard."""
    cfg = ctx.obj["config"]
    _require_config(cfg)

    try:
        import uvicorn
    except ImportError:
        raise click.ClickException(
            "uvicorn is not installed. Run:  pip install 'fastapi' 'uvicorn[standard]'"
        )

    # The /api/* routes have no auth unless dashboard_token is set (see
    # webapi.py's module docstring), and they expose live home-occupancy and
    # billing data. Refuse to put that on a LAN address unguarded.
    _loopback = host in ("127.0.0.1", "localhost", "::1")
    if not _loopback and not getattr(cfg, "dashboard_token", ""):
        raise click.ClickException(
            f"Refusing to bind {host} without a dashboard_token.\n"
            "  The /api/* routes expose live load, battery and billing data.\n"
            '  Set "dashboard_token" in ~/.franklinwh.json first, or bind 127.0.0.1.'
        )

    _header("FranklinWH Dashboard")
    _info(f"Serving  http://{host}:{port}")
    _info(f"Static   {_dashboard_static_dir()}")
    if not _loopback:
        _warn("Bound to a non-loopback address — token auth is active.")
    click.echo()
    uvicorn.run("franklinwh_scraper.webapi:app", host=host, port=port, reload=reload)


def _dashboard_static_dir() -> Path:
    from .webapi import _STATIC
    return _STATIC


# ── Start (one-command entry point) ──────────────────────────────────

@cli.command()
@click.pass_context
def start(ctx: click.Context) -> None:
    """Start the battery advisor using your saved configuration.

    Equivalent to:  account advise --watch
    """
    cfg = ctx.obj["config"]
    _require_config(cfg)

    click.echo()
    click.echo(click.style("  FranklinWH Battery Advisor", bold=True, fg="cyan"))
    _hr()
    _info(f"Account:  {cfg.email}")
    _info(f"Location: {cfg.location_name} ({cfg.lat:.4f}, {cfg.lon:.4f})")
    _info(f"Checking every {cfg.watch_interval} minutes")
    _info(f"Output:   {cfg.output_dir}/")
    click.echo()

    ctx.invoke(
        cmd_advise,
        email=cfg.email,
        password=cfg.password,
        gateway=cfg.gateway or None,
        lat=cfg.lat,
        lon=cfg.lon,
        notify=True,
        out=cfg.output_dir,
        watch=True,
        interval=cfg.watch_interval,
    )


# ── Install macOS LaunchAgent ─────────────────────────────────────────

@cli.command("install-service")
@click.option("--dashboard", "with_dashboard", is_flag=True, default=False,
              help="Also install a LaunchAgent for the web dashboard.")
@click.option("--port", default=8093, show_default=True, type=int,
              help="Port for the dashboard agent (with --dashboard).")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite an existing plist (a .bak copy is kept).")
@click.pass_context
def cmd_install_service(ctx: click.Context, with_dashboard: bool, port: int,
                        force: bool) -> None:
    """Install a macOS LaunchAgent so the advisor starts automatically on login."""
    import sys

    if sys.platform != "darwin":
        _err("install-service is only supported on macOS. On Linux, set up a cron job or systemd timer manually.")
        sys.exit(1)

    cfg       = ctx.obj["config"]
    python    = sys.executable
    script    = (Path(__file__).parent.parent / "scrape.py").resolve()
    log_dir   = Path(cfg.output_dir).resolve()
    label     = "com.franklinwh.advisor"
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / f"{label}.plist"

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{script}</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>{log_dir}/advisor.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/advisor.log</string>
</dict>
</plist>"""

    plist_dir.mkdir(parents=True, exist_ok=True)

    def _write_plist(path: Path, content: str, name: str) -> bool:
        """Write, refusing to clobber an existing agent without --force.

        A hand-written plist may differ in interpreter, working directory or
        log paths; silently replacing it would move the logs out from under
        the user with no warning.
        """
        if path.exists() and not force:
            _warn(f"{path.name} already exists — not overwriting.")
            _info("Re-run with --force to replace it (a .bak copy is kept).")
            return False
        if path.exists():
            backup = path.with_suffix(".plist.bak")
            backup.write_text(path.read_text())
            _info(f"Backed up existing {name} agent to {backup.name}")
        path.write_text(content)
        _ok(f"Written: {path}")
        return True

    _header("LaunchAgent Installed")
    wrote_advisor = _write_plist(plist_path, plist, "advisor")

    if with_dashboard:
        dash_label = "com.franklinwh.dashboard"
        dash_path  = plist_dir / f"{dash_label}.plist"
        dash_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{dash_label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{script}</string>
        <string>dashboard</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>{port}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{script.parent}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>{log_dir}/dashboard.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/dashboard.log</string>
</dict>
</plist>"""
        if _write_plist(dash_path, dash_plist, "dashboard"):
            _info(f"Dashboard will serve http://127.0.0.1:{port}")

    click.echo()
    if wrote_advisor:
        _info(f"Load now:    launchctl load {plist_path}")
        _info(f"Start now:   launchctl start {label}")
        _info(f"Uninstall:   launchctl unload {plist_path} && rm {plist_path}")
    click.echo()


# ── Public website scrapers ───────────────────────────────────────────

@cli.group("scrape")
def grp_scrape() -> None:
    """Scrape public FranklinWH product / support data (no login needed)."""


@grp_scrape.command("products")
@click.option("--out", "-o", default="output", show_default=True)
@click.option("--format", "fmt", type=click.Choice(["json", "csv", "both"]), default="both")
@click.pass_context
def cmd_products(ctx: click.Context, out: str, fmt: str) -> None:
    """Scrape product pages — specs, features, images."""
    _header("Scraping Products")
    with FranklinWHClient(delay=ctx.obj["delay"]) as client:
        data = ProductsScraper(client).scrape_all()
    _ok(f"Scraped {len(data)} products")
    _write(data, Path(out) / "products", fmt)


@grp_scrape.command("faq")
@click.option("--out", "-o", default="output", show_default=True)
@click.option("--format", "fmt", type=click.Choice(["json", "csv", "both"]), default="both")
@click.option("--max-pages", default=5, show_default=True)
@click.pass_context
def cmd_faq(ctx: click.Context, out: str, fmt: str, max_pages: int) -> None:
    """Scrape FAQ articles for homeowners and installers."""
    _header("Scraping FAQs")
    with FranklinWHClient(delay=ctx.obj["delay"]) as client:
        data = FAQScraper(client, max_pages=max_pages).scrape_all()
    _ok(f"Scraped {len(data)} FAQ items")
    _write(data, Path(out) / "faq", fmt)


@grp_scrape.command("support")
@click.option("--out", "-o", default="output", show_default=True)
@click.option("--format", "fmt", type=click.Choice(["json", "csv", "both"]), default="both")
@click.pass_context
def cmd_support(ctx: click.Context, out: str, fmt: str) -> None:
    """Scrape knowledge base and support articles."""
    _header("Scraping Support Articles")
    with FranklinWHClient(delay=ctx.obj["delay"]) as client:
        data = SupportScraper(client).scrape_all()
    _ok(f"Scraped {len(data)} articles")
    _write(data, Path(out) / "support", fmt)


@grp_scrape.command("all")
@click.option("--out", "-o", default="output", show_default=True)
@click.option("--format", "fmt", type=click.Choice(["json", "csv", "both"]), default="both")
@click.option("--max-pages", default=5, show_default=True, hidden=True)
@click.pass_context
def cmd_scrape_all(ctx: click.Context, out: str, fmt: str, max_pages: int) -> None:
    """Scrape everything: products, FAQs, and support articles."""
    outdir = Path(out)
    delay  = ctx.obj["delay"]
    _header("Scraping All Public Data")

    with FranklinWHClient(delay=delay) as client:
        products = ProductsScraper(client).scrape_all()
        _ok(f"Products: {len(products)}")
        _write(products, outdir / "products", fmt)

        faqs = FAQScraper(client, max_pages=max_pages).scrape_all()
        _ok(f"FAQs: {len(faqs)}")
        _write(faqs, outdir / "faq", fmt)

        support = SupportScraper(client).scrape_all()
        _ok(f"Support articles: {len(support)}")
        _write(support, outdir / "support", fmt)

    export_json(
        {"products": len(products), "faqs": len(faqs), "support": len(support)},
        outdir / "summary.json",
    )
    click.echo()
    _ok(f"Done — output saved to {outdir.resolve()}/")


# ── Account commands ──────────────────────────────────────────────────

@cli.group("account")
def grp_account() -> None:
    """Live account data — requires your FranklinWH login."""


@grp_account.command("gateways")
@click.option("--email",    envvar="FRANKLINWH_EMAIL",    default=None)
@click.option("--password", envvar="FRANKLINWH_PASSWORD", default=None, hide_input=True)
@click.pass_context
def cmd_gateways(ctx: click.Context, email: str | None, password: str | None) -> None:
    """List all aGates on your account."""
    cfg = ctx.obj["config"]
    email    = email    or cfg.email
    password = password or cfg.password
    if not email or not password:
        raise click.ClickException("Run 'setup' first, or set FRANKLINWH_EMAIL / FRANKLINWH_PASSWORD.")

    _header("Your Gateways")
    with AccountClient(email, password) as client:
        gateways = client.get_gateways()

    if not gateways:
        _warn("No gateways found on this account.")
        return

    for gw in gateways:
        gid  = gw.get("gatewayId") or gw.get("id", "?")
        loc  = gw.get("address") or gw.get("location", "")
        _ok(f"{gid}  {click.style(loc, dim=True)}")


@grp_account.command("stats")
@click.option("--email",    envvar="FRANKLINWH_EMAIL",    default=None)
@click.option("--password", envvar="FRANKLINWH_PASSWORD", default=None, hide_input=True)
@click.option("--gateway",  envvar="FRANKLINWH_GATEWAY",  default=None)
@click.option("--out", "-o", default="output", show_default=True)
@click.option("--format", "fmt", type=click.Choice(["json", "csv", "both"]), default="both")
@click.pass_context
def cmd_stats(ctx: click.Context, email: str | None, password: str | None,
              gateway: str | None, out: str, fmt: str) -> None:
    """Fetch a live energy snapshot from your system."""
    cfg = ctx.obj["config"]
    email    = email    or cfg.email
    password = password or cfg.password
    gateway  = gateway  or cfg.gateway or None
    if not email or not password:
        raise click.ClickException("Run 'setup' first, or set FRANKLINWH_EMAIL / FRANKLINWH_PASSWORD.")

    _header("Live Energy Snapshot")
    with AccountClient(email, password) as client:
        gateway = _resolve_gateway(client, gateway)
        stats   = client.get_stats(gateway)

    c = stats.current
    t = stats.totals

    click.echo(f"  {'Solar':12} {c.solar_production_kw:>6.2f} kW")
    click.echo(f"  {'Battery':12} {c.battery_use_kw:>+6.2f} kW  "
               f"{click.style(f'SoC {c.battery_soc_pct:.0f}%', bold=True)}")
    click.echo(f"  {'Grid':12} {c.grid_use_kw:>+6.2f} kW  "
               f"[{c.grid_status}]")
    click.echo(f"  {'Home load':12} {c.home_load_kw:>6.2f} kW")
    _hr()
    click.echo(click.style("  Today's totals", bold=True))
    click.echo(f"  {'Solar':20} {t.solar_kwh:>7.2f} kWh")
    click.echo(f"  {'Grid consumed':20} {t.grid_load_kwh:>7.2f} kWh")
    click.echo(f"  {'Grid exported':20} {t.grid_export_kwh:>7.2f} kWh")
    click.echo(f"  {'Grid meter import':20} {t.grid_import_kwh:>7.2f} kWh  (incl. battery charging)")
    click.echo(f"  {'Home use':20} {t.home_use_kwh:>7.2f} kWh")
    click.echo(f"  {'Battery charged':20} {t.battery_charge_kwh:>7.2f} kWh")
    click.echo(f"  {'Battery discharged':20} {t.battery_discharge_kwh:>7.2f} kWh")

    _write([stats.to_flat_dict()], Path(out) / "stats", fmt)


@grp_account.command("probe-switches", hidden=True)
@click.option("--email",    envvar="FRANKLINWH_EMAIL",    default=None)
@click.option("--password", envvar="FRANKLINWH_PASSWORD", default=None, hide_input=True)
@click.option("--gateway",  envvar="FRANKLINWH_GATEWAY",  default=None)
@click.option("--out", "-o", default="output", show_default=True)
@click.option("--dry-run", is_flag=True, default=False,
              help="Print the MQTT envelope that would be sent and exit, without sending.")
@click.pass_context
def cmd_probe_switches(ctx: click.Context, email: str | None, password: str | None,
                       gateway: str | None, out: str, dry_run: bool) -> None:
    """Diagnostic: dump the smart-circuit (MQTT cmd 353) response shape.

    Read-only. cmd 353 with opt:0 is a query — it ran on every get_stats()
    poll in production until it was removed purely as a cost optimization
    (the result was never read). No write command exists in this client.

    Used to determine whether this account has smart circuits installed and,
    if so, what the undocumented response looks like — the full response is
    written to output/ (gitignored) because circuit names are personal data.
    """
    cfg = ctx.obj["config"]
    email    = email    or cfg.email
    password = password or cfg.password
    gateway  = gateway  or cfg.gateway or None
    if not email or not password:
        raise click.ClickException("Run 'setup' first, or set FRANKLINWH_EMAIL / FRANKLINWH_PASSWORD.")

    _header("Smart-circuit probe (MQTT cmd 353)")
    with AccountClient(email, password) as client:
        gateway = _resolve_gateway(client, gateway)

        if dry_run:
            envelope = client._build_mqtt_payload(353, {"opt": 0, "order": gateway}, gateway)
            click.echo("  Would POST this envelope (nothing sent):")
            click.echo()
            click.echo(f"  {envelope}")
            return

        try:
            result = client.get_switch_usage(gateway)
        except TimeoutError as e:
            _warn(f"Device timeout: {e}")
            _info("Gateway was busy or asleep — INCONCLUSIVE, not evidence that")
            _info("smart circuits are absent. Retry while the app shows it online.")
            return
        except ConnectionError as e:
            _err(f"Gateway offline: {e}")
            return
        except RuntimeError as e:
            _err(str(e))
            _info("An 'unsupported command' code here is the real signal that")
            _info("this gateway has no smart circuits.")
            return

    click.echo(f"  Python type : {type(result).__name__}")
    if isinstance(result, dict):
        click.echo(f"  Top-level keys: {sorted(result.keys()) or '(none)'}")
        for k, v in result.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                click.echo(f"  First element of {k!r}: {sorted(v[0].keys())}  ({len(v)} entries)")
    elif isinstance(result, list):
        click.echo(f"  List length : {len(result)}")
        if result and isinstance(result[0], dict):
            click.echo(f"  First element keys: {sorted(result[0].keys())}")
    _hr()

    if not result:
        _warn("Empty response — the gateway answered but reported nothing.")
        _info("Confirm on a second, separate day before concluding that no")
        _info("smart circuits are installed.")
        return

    click.echo(json.dumps(result, indent=2)[:4000])
    outdir = Path(out)
    outdir.mkdir(parents=True, exist_ok=True)
    dest = outdir / f"switch_probe_{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    dest.write_text(json.dumps(result, indent=2))
    _hr()
    _ok(f"Full response written to {dest}")
    _warn("Contains circuit names (personal data) — do not commit it.")


@grp_account.command("poll")
@click.option("--email",    envvar="FRANKLINWH_EMAIL",    default=None)
@click.option("--password", envvar="FRANKLINWH_PASSWORD", default=None, hide_input=True)
@click.option("--gateway",  envvar="FRANKLINWH_GATEWAY",  default=None)
@click.option("--interval", "-i", default=30,  show_default=True, help="Seconds between readings")
@click.option("--count",    "-n", default=0,   show_default=True, help="Readings to take (0 = infinite)")
@click.option("--out", "-o", default="output", show_default=True)
@click.pass_context
def cmd_poll(ctx: click.Context, email: str | None, password: str | None,
             gateway: str | None, interval: int, count: int, out: str) -> None:
    """Continuously log live stats to a CSV file."""
    cfg = ctx.obj["config"]
    email    = email    or cfg.email
    password = password or cfg.password
    gateway  = gateway  or cfg.gateway or None
    if not email or not password:
        raise click.ClickException("Run 'setup' first.")

    outdir   = Path(out)
    log_path = outdir / "poll_log.csv"

    _header("Live Polling")
    with AccountClient(email, password) as client:
        gateway = _resolve_gateway(client, gateway)
        _info(f"Logging to {log_path}  (Ctrl+C to stop)")
        click.echo()

        iteration = 0
        try:
            while count == 0 or iteration < count:
                try:
                    stats = client.get_stats(gateway)
                    export_csv([stats.to_flat_dict()], log_path, append=True)
                    c = stats.current
                    click.echo(
                        f"  {stats.timestamp}  "
                        f"Solar {c.solar_production_kw:.2f}kW  "
                        f"Grid {c.grid_use_kw:+.2f}kW  "
                        f"Batt {c.battery_use_kw:+.2f}kW @ "
                        f"{click.style(f'{c.battery_soc_pct:.0f}%', bold=True)}  "
                        f"Home {c.home_load_kw:.2f}kW"
                    )
                    iteration += 1
                    if count == 0 or iteration < count:
                        time.sleep(interval)
                except (TimeoutError, ConnectionError) as e:
                    _warn(f"{e} — retrying in {interval}s")
                    time.sleep(interval)
        except KeyboardInterrupt:
            click.echo()
            _ok(f"Stopped after {iteration} reading(s). Log saved to {log_path}")


@grp_account.command("advise")
@click.option("--email",    envvar="FRANKLINWH_EMAIL",    default=None)
@click.option("--password", envvar="FRANKLINWH_PASSWORD", default=None, hide_input=True)
@click.option("--gateway",  envvar="FRANKLINWH_GATEWAY",  default=None)
@click.option("--lat",  envvar="FRANKLINWH_LAT",  default=None, type=float)
@click.option("--lon",  envvar="FRANKLINWH_LON",  default=None, type=float)
@click.option("--notify/--no-notify", default=True, show_default=True)
@click.option("--out", "-o", default=None)
@click.option("--watch", is_flag=True, default=False,
              help="Keep running and re-check on --interval")
@click.option("--interval", default=None, type=int,
              help="Minutes between checks (default: from setup)")
@click.pass_context
def cmd_advise(
    ctx: click.Context,
    email: str | None, password: str | None, gateway: str | None,
    lat: float | None, lon: float | None,
    notify: bool, out: str | None,
    watch: bool, interval: int | None,
) -> None:
    """Recommend a battery mode based on live stats, weather, and usage patterns.

    Tip: run 'setup' once so you never need to pass credentials here.
    """
    cfg = ctx.obj["config"]
    email    = email    or cfg.email
    password = password or cfg.password
    gateway  = gateway  or cfg.gateway or None
    lat      = lat      or (cfg.lat  if cfg.lat  else None)
    lon      = lon      or (cfg.lon  if cfg.lon  else None)
    out      = out      or cfg.output_dir
    interval = interval or cfg.watch_interval

    if not email or not password:
        raise click.ClickException("Run 'setup' first.")
    if not lat or not lon:
        raise click.ClickException("Location not set. Run 'setup' to configure it.")

    outdir    = Path(out)
    log_path  = outdir / "advisor_log.jsonl"
    db_path   = outdir / "history.db"
    last_mode = _load_last_mode(outdir)   # persists across cron runs

    if watch and not _acquire_pid_lock():
        raise click.ClickException(
            "Another instance is already running. "
            f"Stop it first, or delete {_PID_FILE} if it's stale."
        )

    with AccountClient(email, password) as client, HistoryStore(db_path) as history:
        gateway = _resolve_gateway(client, gateway)

        days     = history.distinct_days()
        readings = history.reading_count()

        _header("Battery Advisor")
        _info(f"Location:  {cfg.location_name or f'{lat:.4f}, {lon:.4f}'}")
        if days == 0:
            _info("Usage history: none yet — collecting now, predictions activate after 3 days")
        else:
            status = "predictions active" if days >= 3 else f"predictions in {3-days} more day(s)"
            _info(f"Usage history: {readings} readings across {days} day(s) — {status}")
        click.echo()

        # Start Telegram AI chatbot thread if configured
        _chatbot: TelegramChatBot | None = None
        _backend = getattr(cfg, "chat_backend", "anthropic")
        _bot_ready = (
            cfg.telegram_bot_token and _backend != "none" and (
                (_backend == "anthropic" and getattr(cfg, "anthropic_api_key", "")) or
                (_backend == "ollama")
            )
        )
        if _bot_ready and _backend == "anthropic":
            try:
                import anthropic as _anthropic_check  # noqa: F401
            except ImportError:
                _warn("anthropic package not installed — chatbot disabled. Run: pip install anthropic")
                _bot_ready = False
        if _bot_ready:
            import threading as _threading
            _chatbot = TelegramChatBot(cfg, getattr(cfg, "anthropic_api_key", ""), outdir)
            _bot_thread = _threading.Thread(target=_chatbot.run, daemon=True, name="tg-chatbot")
            _bot_thread.start()
            _info("Telegram AI chatbot started — message the bot to ask energy questions")
            click.echo()

        if ENFORCE_LICENSE:
            _lic = check_license(gateway or "")
            if _lic.state == "invalid":
                raise click.ClickException(f"License check failed: {_lic.message}")
            if _lic.state == "grace":
                _warn(_lic.message)

        # Loaded from the persisted health marker (not reset to 0) so a
        # cron-based install — a fresh process per invocation, no --watch —
        # can still accumulate a multi-cycle failure streak instead of the
        # "N poll errors in a row" alert being structurally unreachable.
        _consec_errors   = _read_consec_errors(outdir)
        _ERROR_THRESHOLD = 8
        _last_stats      = None  # cached for time-gated alerts during API outages
        _lic_warn_date   = ""    # one grace-period Telegram warning per day

        # Closed-loop EV charging control (opt-in). Constructor failures
        # (missing token file, bad VIN) must degrade to advisory-only, never
        # kill the advisor.
        _ev = None
        if getattr(cfg, "ev_control_enabled", False):
            try:
                from .ev_controller import EvController
                _ev = EvController(cfg, outdir)
                _ok(f"EV charge control active"
                    f"{' (dry run)' if cfg.ev_dry_run else ''}")
            except Exception as e:
                logger.exception("EV controller init failed")
                _err(f"EV control disabled this run: {e}")

        while True:
            # Re-check each cycle so expiry mid-run is caught; file read + Ed25519
            # verify is microseconds, negligible at a 5-min poll cadence.
            if ENFORCE_LICENSE:
                _lic = check_license(gateway or "")
                if _lic.state == "invalid":
                    _err(f"License check failed: {_lic.message}")
                    _lic_msg = f"🔒 FranklinWH Advisor stopped: {_lic.message}"
                    if cfg.telegram_bot_token and cfg.telegram_chat_id:
                        notify_telegram(_lic_msg, cfg.telegram_bot_token, cfg.telegram_chat_id)
                    if cfg.smtp_host and cfg.email_to:
                        notify_email(_lic_msg, cfg)
                    if cfg.webhook_url:
                        notify_webhook(_lic_msg, True, cfg)
                    break
                if _lic.state == "grace":
                    _today_str = datetime.now().strftime("%Y-%m-%d")
                    if _lic_warn_date != _today_str:
                        _lic_warn_date = _today_str
                        _warn(_lic.message)
                        _lic_msg = f"🔒 {_lic.message}"
                        # License grace warnings previously only reached Telegram,
                        # despite email/webhook being fully supported channels
                        # elsewhere (same gap as the mode-change notification fix).
                        if cfg.telegram_bot_token and cfg.telegram_chat_id:
                            notify_telegram(_lic_msg, cfg.telegram_bot_token, cfg.telegram_chat_id)
                        if cfg.smtp_host and cfg.email_to:
                            notify_email(_lic_msg, cfg)
                        if cfg.webhook_url:
                            notify_webhook(_lic_msg, False, cfg)
            try:
                stats = client.get_stats(gateway)
                _last_stats = stats
                history.record(stats)

                # Weekly readings.db rollup — downsamples data older than 180
                # days from ~5-min to hourly granularity. A simple marker file
                # (not the alerts state, to avoid lock contention with its own
                # file-locked read/save cycle) caps this to once every 7 days;
                # it's a full historical table scan, not a per-poll operation.
                _rollup_marker = outdir / ".last_rollup"
                _today_iso = datetime.now().strftime("%Y-%m-%d")
                try:
                    _last_rollup = _rollup_marker.read_text().strip()
                    datetime.strptime(_last_rollup, "%Y-%m-%d")  # validate format
                except (OSError, ValueError):
                    # A missing file is normal (first run). A malformed one
                    # (e.g. truncated by a crash mid-write) used to raise
                    # ValueError uncaught here, which the outer handler then
                    # miscounted as a FranklinWH API poll error — and since
                    # the marker was never corrected, every cycle after that
                    # repeated the same false alert forever. Treating it the
                    # same as "no marker yet" self-heals on the next write.
                    _last_rollup = ""
                if not _last_rollup or (datetime.now() - datetime.strptime(_last_rollup, "%Y-%m-%d")).days >= 7:
                    _removed = history.rollup_old_readings()
                    if _removed:
                        _info(f"Rolled up {_removed} old readings")
                    outdir.mkdir(parents=True, exist_ok=True)
                    _rollup_marker.write_text(_today_iso)

                outlook        = _fetch_outlook_cached(lat, lon)
                _peak_state    = _load_peak_state(outdir)
                system_peak_kw = _get_system_peak_kw(_peak_state)
                cloudy_now     = (
                    outlook.avg_ghi(12) < _GHI_CLOUDY_THRESHOLD
                    if outlook else False
                )
                perf_ratio     = _get_performance_ratio(_peak_state, cloudy=cloudy_now)
                avg_temp_c     = outlook.avg_temp_c(24) if outlook else 22.0
                hourly_bias    = _get_hourly_bias(_peak_state)
                # No current_load_kw here — this forecast feeds recommend()'s
                # Emergency-Backup decision and the chatbot, and a single
                # noisy poll shouldn't ripple into those. The digest builds
                # its own live-anchored forecast internally (alerts.py).
                usage_forecast = (
                    predict(history, 24, outlook=outlook, system_peak_kw=system_peak_kw,
                            perf_ratio=perf_ratio, avg_temp_c=avg_temp_c,
                            hourly_bias=hourly_bias)
                    if history.has_enough_data() else None
                )
                rec = recommend(
                    stats, outlook, usage_forecast,
                    battery_capacity_kwh=getattr(cfg, "battery_capacity_kwh", _BATTERY_CAPACITY_KWH),
                )

                if _chatbot is not None:
                    # rec too: without it the bot answers "should I switch
                    # modes?" from raw telemetry and can contradict the
                    # recommendation the user was just alerted about.
                    _chatbot.update_state(stats, history, outlook, system_peak_kw, perf_ratio,
                                          usage_forecast=usage_forecast, rec=rec)

                # Home Assistant webhook state push
                if getattr(cfg, "ha_webhook_url", ""):
                    from .notifier import notify_ha_webhook as _ha_push
                    from .tou import period_at as _pat, rate_at as _rat
                    _now = datetime.now()
                    _ha_push(cfg.ha_webhook_url, {
                        "soc_pct":          stats.current.battery_soc_pct,
                        "solar_kw":         stats.current.solar_production_kw,
                        "home_load_kw":     stats.current.home_load_kw,
                        "grid_kw":          stats.current.grid_use_kw,
                        "battery_kw":       stats.current.battery_use_kw,
                        "grid_status":      stats.current.grid_status,
                        "solar_today_kwh":  stats.totals.solar_kwh,
                        "tou_period":       _pat(_now).value,
                        "tou_rate":         _rat(_now),
                        "timestamp":        _now.isoformat(),
                    })

                # First-run welcome message
                if history.reading_count() == 1 and cfg.telegram_bot_token and cfg.telegram_chat_id:
                    notify_telegram(
                        "✅ FranklinWH Advisor is running!\n\n"
                        f"Monitoring your battery at {cfg.location_name or 'your location'}.\n"
                        "Collecting usage data — full predictions and alerts activate after 3 days.\n\n"
                        "You'll get a morning preview each day at 7:30 am and alerts whenever action is needed."
                        + ("\n\nTip: message this bot to ask energy questions." if _chatbot is not None else ""),
                        cfg.telegram_bot_token, cfg.telegram_chat_id,
                    )

                _print_recommendation(rec, stats, usage_forecast, cfg.location_name)
                notify_log(rec, log_path)
                _dispatch_notifications(rec, cfg, notify, last_mode, outdir)
                _check_peak_alerts(stats, cfg, outdir, outlook=outlook, usage_forecast=usage_forecast, store=history)

                # EV controller runs on its own try/except: a Tesla failure
                # must never inflate _consec_errors (that path alerts about
                # FranklinWH poll outages, which this is not). On a FWH
                # outage tick this is skipped entirely — no fresh stats, no
                # blind amp changes.
                if _ev is not None:
                    try:
                        _ev.tick(stats, datetime.now())
                    except Exception:
                        logger.exception("EV controller error — advisor unaffected")

                last_mode = rec.mode.value
                _save_last_mode(outdir, last_mode)

                if _consec_errors >= _ERROR_THRESHOLD and cfg.telegram_bot_token and cfg.telegram_chat_id:
                    notify_telegram(
                        "✅ FranklinWH Advisor: poll errors resolved — alerts resuming.",
                        cfg.telegram_bot_token, cfg.telegram_chat_id,
                    )
                _consec_errors = 0
                _ping_healthcheck(cfg)  # signal a healthy completed cycle
                _write_health_marker(outdir, 0, None)

            except Exception as e:
                _consec_errors += 1
                logger.exception("Watch loop error")
                _err(str(e))
                _write_health_marker(outdir, _consec_errors, str(e))
                # During API outages, still fire time-gated alerts (morning preview,
                # EOD digest) using the last known stats so sleep/connectivity blips
                # don't silently swallow them.
                _now_h = datetime.now().hour
                if _last_stats is not None and _now_h in (7, 8, 21, 22):
                    try:
                        _check_peak_alerts(
                            _last_stats, cfg, outdir,
                            outlook=None, usage_forecast=None, store=history,
                        )
                    except Exception:
                        pass
                if _consec_errors == _ERROR_THRESHOLD and cfg.telegram_bot_token and cfg.telegram_chat_id:
                    notify_telegram(
                        f"⚠️ FranklinWH Advisor: {_ERROR_THRESHOLD} poll errors in a row\n"
                        f"Error: {e}\n"
                        f"Alerts paused until fixed. Check advisor log for details.",
                        cfg.telegram_bot_token, cfg.telegram_chat_id,
                    )

            if not watch:
                break

            click.echo(click.style(
                f"  Next check in {interval} min — Ctrl+C to stop",
                dim=True,
            ))
            try:
                time.sleep(interval * 60)
                click.echo()
            except KeyboardInterrupt:
                click.echo()
                _ok("Advisor stopped.")
                break


@grp_account.command("history")
@click.option("--out", "-o", default=None)
@click.pass_context
def cmd_history(ctx: click.Context, out: str | None) -> None:
    """Show your recorded usage history and hourly load profile."""
    cfg     = ctx.obj["config"]
    out     = out or cfg.output_dir
    db_path = Path(out) / "history.db"

    if not db_path.exists():
        raise click.ClickException(
            "No history yet. Run 'start' or 'account advise --watch' to begin collecting."
        )

    _header("Usage History")
    with HistoryStore(db_path) as history:
        days     = history.distinct_days()
        readings = history.reading_count()

        _info(f"{readings} readings across {days} day(s)")
        status = "active" if days >= 3 else f"need {3-days} more day(s)"
        _info(f"Predictions: {status}")

        recent = history.recent_avg_load(2)
        if recent is not None:
            _info(f"Recent avg load (last 2h): {recent:.2f} kW")

        profile = history.load_profile()
        if not profile:
            return

        click.echo()
        click.echo(click.style("  Avg home load by hour", bold=True))
        _hr()

        by_hour: dict[int, list[float]] = {}
        for (_, hr), kw in profile.items():
            by_hour.setdefault(hr, []).append(kw)

        peak = max(
            sum(v) / len(v) for v in by_hour.values() if v
        ) if by_hour else 1.0

        for hr in range(24):
            vals = by_hour.get(hr, [])
            avg  = sum(vals) / len(vals) if vals else 0.0
            bar_len = int((avg / peak) * 30) if peak else 0
            bar  = click.style("█" * bar_len, fg="cyan") + click.style("░" * (30 - bar_len), dim=True)
            label = f"{hr:02d}:00"
            click.echo(f"  {label}  {bar}  {avg:.2f} kW")


@grp_account.command("savings")
@click.option("--days", "-d", default=30, show_default=True, type=int)
@click.option("--out", "-o", default=None)
@click.pass_context
def cmd_savings(ctx: click.Context, days: int, out: str | None) -> None:
    """What the battery + solar have actually saved you."""
    from . import savings as _savings
    from .history import integrate_intervals as _ii

    cfg    = ctx.obj["config"]
    outdir = Path(out or cfg.output_dir)
    db     = outdir / "history.db"
    if not db.exists():
        raise click.ClickException(f"No history database at {db}. Has the advisor run?")

    end   = datetime.now().date()
    start = end - timedelta(days=days - 1)
    with HistoryStore(db) as store:
        rows = store.weekly_readings(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        charge_days = store.grid_charge_days(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    if not rows:
        raise click.ClickException("No readings in that window.")

    sv = _savings.compute(_ii(rows), start.isoformat(), end.isoformat())

    _header(f"Savings — last {sv.days} days ({sv.start} → {sv.end})")
    click.echo(f"  {'Home used':22} {sv.home_kwh:>8.1f} kWh")
    click.echo(f"  {'Served by battery/solar':22} {sv.self_use_kwh:>8.1f} kWh")
    click.echo(f"  {'Grid imported':22} {sv.import_kwh:>8.1f} kWh")
    click.echo(f"  {'Grid exported':22} {sv.export_kwh:>8.1f} kWh")
    _hr()
    click.echo(click.style("  Actual energy cost (excl. base service)", bold=True))
    click.echo(f"  {'Import':22} ${sv.actual_import_cost:>8.2f}")
    click.echo(f"  {'Export credit':22} ${sv.actual_export_credit:>8.2f}")
    click.echo(f"  {'Net':22} ${sv.actual_net_energy_cost:>8.2f}")
    _hr()
    click.echo(click.style("  Saved vs. no battery / no solar", bold=True))
    click.echo(f"  {'Would have paid':22} ${sv.grid_only_cost:>8.2f}")
    click.echo(f"  {'Actually paid':22} ${sv.actual_net_energy_cost:>8.2f}")
    click.echo(click.style(f"  {'SAVED':22} ${sv.saved_vs_grid_only:>8.2f}", fg="green", bold=True))
    click.echo()
    click.echo(f"  {'  on-peak (4–9pm)':22} ${sv.saved_on_peak:>8.2f}")
    click.echo(f"  {'  off-peak':22} ${sv.saved_off_peak:>8.2f}")
    click.echo(f"  {'  super off-peak':22} ${sv.saved_super_off_peak:>8.2f}")
    click.echo(f"  {'  export credit':22} ${sv.actual_export_credit:>8.2f}")
    _hr()
    click.echo(click.style("  Battery's own contribution (estimate)", bold=True))
    click.echo(f"  {'Solar-only would cost':22} ${sv.solar_only_net_cost:>8.2f}")
    click.echo(f"  {'Battery saved':22} ${sv.saved_vs_solar_only:>8.2f}")

    state = _load_peak_state(outdir)
    cum = state.get("savings_cumulative")
    if isinstance(cum, dict) and cum.get("days"):
        _hr()
        click.echo(click.style("  Running total (since tracking began)", bold=True))
        click.echo(f"  ${cum.get('vs_grid', 0.0):.2f} over {cum['days']} days "
                   f"(~${cum.get('vs_grid', 0.0) / max(1, cum['days']):.2f}/day)")

    audit = _savings.followed_advice_audit(outdir / "advisor_log.jsonl", charge_days, days)
    if audit.get("available"):
        _hr()
        click.echo(f"  EB recommended on {audit['eb_recommended_days']} of the last {days} days "
                   f"· grid charging observed on {audit['grid_charge_days']}")

    _hr()
    _info(f"Priced at rates effective {sv.priced_at}; base service charge excluded")
    _info("(it is incurred either way, so counting it would inflate savings).")
    if sv.export_days_at_assumed_rate:
        _info(f"{sv.export_days_at_assumed_rate} export day(s) priced at the assumed "
              f"NBT floor rate — SDG&E publishes hourly export rates only for Aug/Sep.")


@grp_account.command("ev-status")
@click.option("--out", "-o", default=None)
@click.option("--live", is_flag=True, default=False,
              help="Do one billable Tesla poll ($0.002) for fresh vehicle state.")
@click.option("--days", type=int, default=None,
              help="Show the full decision history over this many days "
                   "(default: just the last 5 entries) — useful for "
                   "judging the controller during a dry-run soak before "
                   "`tesla go-live`.")
@click.pass_context
def cmd_ev_status(ctx: click.Context, out: str | None, live: bool,
                  days: int | None) -> None:
    """EV charge controller status: session, last decision, spend."""
    cfg    = ctx.obj["config"]
    outdir = Path(out or cfg.output_dir)

    if not getattr(cfg, "ev_control_enabled", False):
        raise click.ClickException(
            "EV control is not enabled — run `franklinwh setup`.")

    state_path = outdir / ".ev_controller_state.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}

    _header("EV Charge Control")
    mode = "DRY RUN (logging only)" if cfg.ev_dry_run else "LIVE"
    click.echo(f"  {'Mode':24} {mode}")
    click.echo(f"  {'Session':24} {state.get('session', 'none')}")
    click.echo(f"  {'Commanded amps':24} {state.get('last_commanded_amps') or '—'}")
    click.echo(f"  {'Override standdown':24} "
               f"{state.get('override_until_iso') or 'no'}")
    click.echo(f"  {'Consecutive API errors':24} "
               f"{state.get('consec_tesla_errors', 0)}")
    if state.get("last_error"):
        click.echo(f"  {'Last error':24} {state['last_error']}")

    n_samples = len(state.get("ev_draw_samples", []))
    if n_samples:
        click.echo(f"  {'EV draw calibration':24} "
                   f"{n_samples} real reading(s), current estimate "
                   f"{cfg.ev_charging_kw:.1f} kW"
                   + (f" (last tuned {state['ev_kw_last_tuned_iso']})"
                      if state.get("ev_kw_last_tuned_iso") else ""))

    month = datetime.now().strftime("%Y-%m")
    counts = state.get("spend", {}).get(month, {})
    spend = (counts.get("data", 0) * 0.002 + counts.get("cmd", 0) * 0.001
             + counts.get("wake", 0) * 0.02)
    click.echo(f"  {'API spend ({})'.format(month):24} "
               f"${spend:.2f} of $10 credit "
               f"(data {counts.get('data', 0)} · cmd {counts.get('cmd', 0)}"
               f" · wake {counts.get('wake', 0)})")

    log_path = outdir / "ev_controller.jsonl"
    if log_path.exists():
        _hr()
        all_lines = log_path.read_text().splitlines()
        if days is not None:
            cutoff = datetime.now() - timedelta(days=days)
            entries = []
            for line in all_lines:
                try:
                    e = json.loads(line)
                    if datetime.fromisoformat(e["timestamp"]) >= cutoff:
                        entries.append(e)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
            actions = [e for e in entries if e.get("action") != "none"]
            click.echo(click.style(
                f"  Decisions — last {days} day(s)", bold=True))
            click.echo(f"  {len(entries)} logged, {len(actions)} action(s) taken")
            for e in actions:
                click.echo(f"  {e['timestamp']}  {e['action']:<10} "
                           f"{e.get('amps') or '':>3}  {e['reason']}")
        else:
            click.echo(click.style("  Recent decisions", bold=True))
            for line in all_lines[-5:]:
                try:
                    e = json.loads(line)
                    click.echo(f"  {e['timestamp']}  {e['action']:<10} "
                               f"{e.get('amps') or '':>3}  {e['reason']}")
                except (json.JSONDecodeError, KeyError):
                    continue

    if live:
        _hr()
        click.echo("  Polling vehicle…", nl=False)
        try:
            from .tesla import TeslaClient
            v = TeslaClient(cfg.tesla_vin, cfg.tesla_client_id).get_charge_state()
            click.echo()
            click.echo(f"  {'Plugged in':24} {'yes' if v.plugged_in else 'no'}")
            click.echo(f"  {'Charging':24} {'yes' if v.charging else 'no'}")
            click.echo(f"  {'Vehicle SoC':24} {v.vehicle_soc_pct:.0f}% "
                       f"(limit {v.charge_limit_pct:.0f}%)")
            click.echo(f"  {'Amps':24} {v.requested_amps} requested · "
                       f"{v.actual_current_a:.0f} actual · "
                       f"{v.charger_max_amps} max")
        except Exception as e:
            click.echo(click.style(f" FAILED: {e}", fg="red"))
    click.echo()


@grp_account.command("accuracy")
@click.option("--out", "-o", default=None)
@click.pass_context
def cmd_accuracy(ctx: click.Context, out: str | None) -> None:
    """Show solar-forecast accuracy trend by week (how close predicted kWh was to actual)."""
    cfg     = ctx.obj["config"]
    out     = out or cfg.output_dir
    outdir  = Path(out)
    state   = _load_peak_state(outdir)

    daily_pr = {
        k[len("daily_pr_"):]: v
        for k, v in state.items()
        if k.startswith("daily_pr_") and v is not None
    }
    if not daily_pr:
        raise click.ClickException(
            "No accuracy data yet — it accumulates once mornings preview against "
            "the prior day's actual solar output."
        )

    _header("Solar Forecast Accuracy")

    by_week: dict[str, list[float]] = {}
    for date_str, ratio in daily_pr.items():
        try:
            iso_year, iso_week, _ = datetime.strptime(date_str, "%Y-%m-%d").isocalendar()
        except ValueError:
            continue
        by_week.setdefault(f"{iso_year}-W{iso_week:02d}", []).append(ratio)

    weeks = sorted(by_week)
    click.echo(click.style("  Week        Days   Mean err%   Trend", bold=True))
    _hr()
    prior_errs: list[float] = []
    for wk in weeks:
        ratios   = by_week[wk]
        errs     = [abs(1.0 - r) * 100 for r in ratios]
        mean_err = sum(errs) / len(errs)
        trend    = ""
        if prior_errs:
            prior_mean = sum(prior_errs) / len(prior_errs)
            delta = mean_err - prior_mean
            if abs(delta) >= 1.0:
                trend = f"({'+' if delta > 0 else ''}{delta:.1f} vs prior 4wk avg)"
        click.echo(f"  {wk}   {len(ratios):>4}   {mean_err:>7.1f}%   {trend}")
        prior_errs = (prior_errs + errs)[-4 * 7:]  # rolling ~4-week window of daily samples

    all_errs = [abs(1.0 - r) * 100 for r in daily_pr.values()]
    within_5 = sum(1 for e in all_errs if e <= 5.0)
    click.echo()
    _info(f"Overall: {len(all_errs)} day(s), mean error {sum(all_errs)/len(all_errs):.1f}%, "
          f"{within_5}/{len(all_errs)} within 5%")


@grp_account.command("solar-audit")
@click.option("--days", type=int, default=14, show_default=True)
@click.option("--out", "-o", default=None)
@click.pass_context
def cmd_solar_audit(ctx: click.Context, days: int, out: str | None) -> None:
    """Per-day solar calibration inputs vs actual — diagnose outlier days.

    Complements `accuracy` (weekly error trend) with the exact inputs behind
    each day's prediction — system_peak_kw, perf_ratio, cloudy_day, avg_ghi —
    read from output/solar_calibration_log.jsonl. Days with >15% error are
    flagged and grouped by cloudy_day so a recurring clear-day vs cloudy-day
    skew is visible at a glance, instead of another one-off grep/jq session.
    """
    cfg      = ctx.obj["config"]
    outdir   = Path(out or cfg.output_dir)
    log_path = outdir / _SOLAR_CAL_LOG_FILE
    db_path  = outdir / "history.db"

    if not log_path.exists():
        raise click.ClickException(
            f"No calibration log yet at {log_path} — it's written once/day "
            "by the morning preview alert. Check back after it's run a few days."
        )

    cutoff = datetime.now() - timedelta(days=days)
    entries = []
    for line in log_path.read_text().splitlines():
        try:
            e = json.loads(line)
            if datetime.fromisoformat(e["timestamp"]) >= cutoff:
                entries.append(e)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    entries.sort(key=lambda e: e["date"], reverse=True)

    if not entries:
        raise click.ClickException(f"No calibration log entries in the last {days} day(s).")

    _header(f"Solar Calibration Audit — last {days} day(s)")
    click.echo(click.style(
        "  Date         Predicted  Actual   Err%   Cloudy  PeakKW  PR", bold=True))
    _hr()

    outliers: list[tuple[dict, float]] = []
    with HistoryStore(db_path) as store:
        for e in entries:
            actual = store.daily_solar_kwh(e["date"])
            predicted = e["predicted_kwh"]
            err_pct = ((predicted - actual) / actual * 100) if actual > 0 else 0.0
            flag = " ⚠" if abs(err_pct) > 15 else ""
            click.echo(
                f"  {e['date']:10}   {predicted:>7.1f}   {actual:>6.1f}  "
                f"{err_pct:>+6.1f}%  {'yes' if e['cloudy_day'] else 'no':<6}  "
                f"{e['system_peak_kw']:>5.1f}  {e['perf_ratio']:.2f}{flag}"
            )
            if abs(err_pct) > 15:
                outliers.append((e, err_pct))

    if outliers:
        _hr()
        clear_out = [e for e, _ in outliers if not e["cloudy_day"]]
        cloudy_out = [e for e, _ in outliers if e["cloudy_day"]]
        click.echo(click.style(f"  {len(outliers)} day(s) over 15% error", bold=True))
        click.echo(f"  Clear-day outliers:  {len(clear_out)}"
                    + (f" ({', '.join(e['date'] for e in clear_out)})" if clear_out else ""))
        click.echo(f"  Cloudy-day outliers: {len(cloudy_out)}"
                    + (f" ({', '.join(e['date'] for e in cloudy_out)})" if cloudy_out else ""))
    click.echo()


# ── Shared helpers ────────────────────────────────────────────────────

def _print_recommendation(rec, stats, usage_forecast=None, location="") -> None:
    urgency_color = {"info": "green", "warning": "yellow", "critical": "red"}
    urgency_label = {"info": "INFO", "warning": "WARN", "critical": "CRIT"}
    emoji         = {"info": "🟢",  "warning": "🟡",   "critical": "🔴"}

    color  = urgency_color.get(rec.urgency, "white")
    action = (
        f"→ Switch to {rec.mode.value.replace('_', ' ').upper()}"
        if rec.needs_action else "No mode change needed"
    )

    click.echo(
        f"  {emoji.get(rec.urgency, '⚪')} "
        + click.style(f"[{urgency_label.get(rec.urgency)}]  {action}", fg=color, bold=True)
    )
    click.echo(f"     {rec.reason}")
    click.echo()

    c = stats.current
    click.echo(
        f"  {'Now':10}  "
        f"Solar {c.solar_production_kw:.1f}kW  "
        f"Grid {c.grid_use_kw:+.1f}kW  "
        f"Battery {c.battery_use_kw:+.1f}kW @ "
        + click.style(f"{c.battery_soc_pct:.0f}%", bold=True)
        + f"  Home {c.home_load_kw:.1f}kW"
    )
    d = rec.details
    click.echo(
        f"  {'Weather':10}  "
        f"next 6h {d['ghi_next_6h_wm2']:.0f} W/m²  "
        f"next 24h {d['ghi_next_24h_wm2']:.0f} W/m²  "
        f"cloud {d['cloud_cover_6h_pct']:.0f}%"
        + (f"  [{location}]" if location else "")
    )
    if usage_forecast and usage_forecast.confidence != "none":
        net_color = "green" if usage_forecast.net_kwh >= 0 else "yellow"
        click.echo(
            f"  {'Patterns':10}  "
            f"12h load {usage_forecast.total_load_kwh:.1f} kWh  "
            f"solar {usage_forecast.total_solar_kwh:.1f} kWh  "
            f"net "
            + click.style(f"{usage_forecast.net_kwh:+.1f} kWh", fg=net_color)
            + f"  [{usage_forecast.confidence} confidence, {usage_forecast.data_days}d data]"
        )
    _hr()


def _write(data: list, base: Path, fmt: str) -> None:
    if fmt in ("json", "both"):
        export_json(data, base.with_suffix(".json"))
    if fmt in ("csv", "both"):
        export_csv(data, base.with_suffix(".csv"))


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
