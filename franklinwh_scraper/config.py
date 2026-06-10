"""Persistent configuration stored at ~/.franklinwh.json."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

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

    # Per-alert opt-outs.  Empty = all alerts enabled.
    # Values are alert-name strings (function suffix after _alert_).
    disabled_alerts: list[str] = field(default_factory=list)

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
        except (json.JSONDecodeError, OSError):
            pass

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
        try: cfg.lat = float(overrides["lat"])
        except ValueError: pass
    if overrides["lon"]:
        try: cfg.lon = float(overrides["lon"])
        except ValueError: pass

    return cfg


def save(cfg: Config) -> None:
    """Save config to ~/.franklinwh.json with restricted permissions."""
    CONFIG_PATH.write_text(json.dumps(cfg.to_dict(), indent=2))
    CONFIG_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
