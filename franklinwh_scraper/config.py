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
    gateway: str = ""       # empty = auto-detect first gateway
    output_dir: str = "output"
    watch_interval: int = 30   # minutes
    imessage_phone: str = ""         # e.g. "+19255884276" (macOS only)
    telegram_bot_token: str = ""     # from @BotFather
    telegram_chat_id: str = ""       # auto-detected on setup
    battery_capacity_kwh: float = 13.6  # usable kWh — aPower 10=10, aPower 15=15, stacked=multiple

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

    # Env vars override saved config (allows CI / temporary overrides)
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
    # chmod 600 — owner read/write only (password is stored here)
    CONFIG_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
