"""Persistent configuration stored at ~/.franklinwh.json."""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".franklinwh.json"


@dataclass
class Config:
    email: str = ""
    password: str = ""
    lat: float = 0.0
    lon: float = 0.0
    location_name: str = ""
    gateway: str = ""
    output_dir: str = "output"
    watch_interval: int = 30
    imessage_phone: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    battery_capacity_kwh: float = 13.6
    # Day of month the utility billing cycle starts. SDG&E bills on a
    # per-meter read date, not a utility-wide constant — check your bill.
    billing_cycle_start_day: int = 20
    # YYYY-MM-DD. Blank is honest and supported: without it the weekly
    # summary reports cycles "since tracking start" rather than inventing a
    # lifetime figure by extrapolating from a guessed install date.
    install_date: str = ""
    anthropic_api_key: str = ""
    chat_backend: str = "none"      # "anthropic" | "ollama" | "none"
    ollama_model: str = "llama3.1:8b"
    ollama_url: str = "http://localhost:11434"
    ha_webhook_url: str = ""

    # Email (SMTP) notifications
    email_to: str = ""
    email_from: str = ""            # defaults to email_to when blank
    smtp_host: str = ""             # e.g. smtp.gmail.com
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    # Generic webhook (POST JSON to Slack, Discord, custom URL, etc.)
    webhook_url: str = ""

    # Uptime monitoring — ping this URL each successful run (e.g. healthchecks.io).
    # If pings stop, the service notifies you the advisor has gone down.
    healthcheck_url: str = ""

    # EV charging — enables the off-peak EV charge-window advisor.
    ev_charging: bool = False
    ev_kwh_per_session: float = 0.0   # 0 = unknown; if set, shows $ savings estimate

    # Closed-loop EV charging control (Tesla Fleet API). See docs/TESLA_SETUP.md.
    # Tokens live in ~/.franklinwh_tesla.json (they rotate; keeping them out of
    # this file means a wizard save can never clobber a rotated refresh token).
    ev_control_enabled: bool = False  # master gate; ev_charging stays advisory-only
    ev_dry_run: bool = True           # log decisions, send no commands (safe rollout)
    tesla_vin: str = ""
    tesla_client_id: str = ""
    ev_max_amps: int = 32             # confirm against the charger's circuit breaker
    ev_battery_first_soc: float = 80.0  # FWH battery tops up before EV gets surplus
    ev_reserve_kw: float = 0.25       # headroom so rounding never imports from grid

    # Per-alert opt-outs.  Empty = all alerts enabled.
    # Values are alert-name strings (function suffix after _alert_).
    disabled_alerts: list[str] = field(default_factory=list)

    # Optional shared-secret for the web dashboard (webapi.py). Empty = no
    # auth required (fine for the default 127.0.0.1-only LaunchAgent binding;
    # set this if the dashboard is ever reachable beyond localhost).
    dashboard_token: str = ""

    def is_complete(self) -> bool:
        return bool(self.email and self.password and self.lat and self.lon)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load() -> Config:
    """Load config from ~/.franklinwh.json, falling back to env vars."""
    cfg = Config()

    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        except (json.JSONDecodeError, OSError) as e:
            # Previously silent — a corrupt config file meant credentials
            # appeared to have "vanished" with zero trace in the logs.
            logger.warning("Failed to read %s (%s) — falling back to defaults", CONFIG_PATH, e)

    overrides = {
        "email":    os.environ.get("FRANKLINWH_EMAIL", ""),
        "password": os.environ.get("FRANKLINWH_PASSWORD", ""),
        "lat":      os.environ.get("FRANKLINWH_LAT", ""),
        "lon":      os.environ.get("FRANKLINWH_LON", ""),
        "gateway":  os.environ.get("FRANKLINWH_GATEWAY", ""),
    }
    if overrides["email"]:    cfg.email    = overrides["email"]
    if overrides["password"]: cfg.password = overrides["password"]
    if overrides["gateway"]:  cfg.gateway  = overrides["gateway"]
    if overrides["lat"]:
        try:
            cfg.lat = float(overrides["lat"])
        except ValueError:
            logging.getLogger(__name__).warning(
                "Ignoring invalid FRANKLINWH_LAT=%r", overrides["lat"])
    if overrides["lon"]:
        try:
            cfg.lon = float(overrides["lon"])
        except ValueError:
            logging.getLogger(__name__).warning(
                "Ignoring invalid FRANKLINWH_LON=%r", overrides["lon"])

    return cfg


def save(cfg: Config) -> None:
    """Save config to ~/.franklinwh.json with restricted permissions.

    Creates the file with 0600 already applied via os.open's mode arg,
    instead of write_text() (creates under the process umask, often 644)
    followed by a separate chmod — that left a brief window where
    credentials (password, smtp_password, anthropic_api_key) were
    world/group-readable, and left them that way permanently if chmod
    itself ever failed.
    """
    data = json.dumps(cfg.to_dict(), indent=2)
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
    finally:
        CONFIG_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)  # in case the file pre-existed with looser perms
