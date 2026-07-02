"""Shared formatting helpers used by the CLI alerts and the Telegram chatbot."""

from __future__ import annotations


def soc_bar(pct: float) -> str:
    """10-block SoC progress bar with color indicator: 🟢 ████████░░ 74%"""
    filled    = round(max(0.0, min(100.0, pct)) / 10)
    indicator = "🟢" if pct >= 60 else ("🟡" if pct >= 30 else "🔴")
    return f"{indicator} {'█' * filled}{'░' * (10 - filled)} {pct:.0f}%"


def time_to_pct(
    current_soc: float, target_pct: float,
    cap_kwh: float, batt_kw: float,
) -> float | None:
    """Hours to reach target_pct at current charge/discharge rate.

    batt_kw > 0 = discharging; batt_kw < 0 = charging.
    Returns None when idle or target already passed.
    """
    if abs(batt_kw) < 0.1:
        return None
    delta_kwh      = (target_pct - current_soc) / 100.0 * cap_kwh
    rate_kwh_per_h = -batt_kw  # positive when charging (SoC rising)
    if rate_kwh_per_h == 0:
        return None
    hours = delta_kwh / rate_kwh_per_h
    return hours if hours > 0 else None


def fmt_hours(hours: float) -> str:
    """'2h 14m' or '34m' from a fractional hour count."""
    h = int(hours)
    m = round((hours - h) * 60)
    if m == 60:
        h += 1
        m = 0
    return f"{h}h {m}m" if h > 0 else f"{m}m"
