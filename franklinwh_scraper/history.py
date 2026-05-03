"""SQLite-backed store for historical energy readings and load profile building."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .account import Stats

DEFAULT_DB_PATH = Path("output/history.db")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS readings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT    NOT NULL,
    day_of_week   INTEGER NOT NULL,  -- 0=Mon … 6=Sun
    hour_of_day   INTEGER NOT NULL,  -- 0–23
    home_load_kw  REAL    NOT NULL,
    solar_kw      REAL    NOT NULL,
    battery_soc   REAL    NOT NULL,
    grid_use_kw   REAL    NOT NULL,
    grid_status   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slot ON readings(day_of_week, hour_of_day);
"""


# (day_of_week, hour_of_day) → average kW
LoadProfile = dict[tuple[int, int], float]


@dataclass
class SlotStats:
    day_of_week: int
    hour_of_day: int
    avg_load_kw: float
    avg_solar_kw: float
    sample_count: int


class HistoryStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.executescript(_CREATE_SQL)
        self._conn.commit()

    # ---------------------------------------------------------------- #

    def record(self, stats: Stats) -> None:
        now = datetime.now()
        self._conn.execute(
            """
            INSERT INTO readings
              (timestamp, day_of_week, hour_of_day,
               home_load_kw, solar_kw, battery_soc, grid_use_kw, grid_status)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                now.isoformat(),
                now.weekday(),
                now.hour,
                stats.current.home_load_kw,
                stats.current.solar_production_kw,
                stats.current.battery_soc_pct,
                stats.current.grid_use_kw,
                stats.current.grid_status,
            ),
        )
        self._conn.commit()

    # ---------------------------------------------------------------- #

    def reading_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM readings").fetchone()
        return row[0]

    def distinct_days(self) -> int:
        """Number of distinct calendar days with data."""
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT substr(timestamp,1,10)) FROM readings"
        ).fetchone()
        return row[0]

    def has_enough_data(self, min_days: int = 3) -> bool:
        return self.distinct_days() >= min_days

    # ---------------------------------------------------------------- #

    def load_profile(self) -> LoadProfile:
        """Return average home load kW keyed by (day_of_week, hour_of_day)."""
        rows = self._conn.execute(
            """
            SELECT day_of_week, hour_of_day, AVG(home_load_kw)
            FROM readings
            GROUP BY day_of_week, hour_of_day
            """
        ).fetchall()
        return {(int(r[0]), int(r[1])): float(r[2]) for r in rows}

    def solar_profile(self) -> LoadProfile:
        """Return average solar production kW keyed by (day_of_week, hour_of_day)."""
        rows = self._conn.execute(
            """
            SELECT day_of_week, hour_of_day, AVG(solar_kw)
            FROM readings
            GROUP BY day_of_week, hour_of_day
            """
        ).fetchall()
        return {(int(r[0]), int(r[1])): float(r[2]) for r in rows}

    def slot_detail(self, day_of_week: int, hour_of_day: int) -> SlotStats | None:
        row = self._conn.execute(
            """
            SELECT AVG(home_load_kw), AVG(solar_kw), COUNT(*)
            FROM readings
            WHERE day_of_week=? AND hour_of_day=?
            """,
            (day_of_week, hour_of_day),
        ).fetchone()
        if not row or row[2] == 0:
            return None
        return SlotStats(
            day_of_week=day_of_week,
            hour_of_day=hour_of_day,
            avg_load_kw=float(row[0]),
            avg_solar_kw=float(row[1]),
            sample_count=int(row[2]),
        )

    def recent_avg_load(self, hours: int = 2) -> float | None:
        """Average home load over the last N hours of recorded data."""
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        row = self._conn.execute(
            "SELECT AVG(home_load_kw) FROM readings WHERE timestamp >= ?",
            (cutoff,),
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
