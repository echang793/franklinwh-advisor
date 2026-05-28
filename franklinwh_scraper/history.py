"""SQLite-backed store for historical energy readings and load profile building."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .account import Stats

DEFAULT_DB_PATH = Path("output/history.db")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS readings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT    NOT NULL,
    day_of_week      INTEGER NOT NULL,
    hour_of_day      INTEGER NOT NULL,
    home_load_kw     REAL    NOT NULL,
    solar_kw         REAL    NOT NULL,
    battery_soc      REAL    NOT NULL,
    grid_use_kw      REAL    NOT NULL,
    grid_status      TEXT    NOT NULL,
    solar_total_kwh  REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_slot ON readings(day_of_week, hour_of_day);
"""

_MIGRATE_SQL = """
ALTER TABLE readings ADD COLUMN solar_total_kwh REAL NOT NULL DEFAULT 0;
"""


# (day_of_week, hour_of_day) → average kW
LoadProfile = dict[tuple[int, int], float]


@dataclass
class MonthlyTotals:
    year_month: str       # "2026-05"
    solar_kwh: float      # sum of MAX(solar_total_kwh) per day (API running total)
    grid_import_kwh: float
    grid_export_kwh: float
    home_load_kwh: float
    days_with_data: int


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
        try:
            self._conn.executescript(_MIGRATE_SQL)
        except sqlite3.OperationalError:
            pass  # column already exists
        self._conn.commit()

    # ---------------------------------------------------------------- #

    def record(self, stats: Stats) -> None:
        now = datetime.now()
        self._conn.execute(
            """
            INSERT INTO readings
              (timestamp, day_of_week, hour_of_day,
               home_load_kw, solar_kw, battery_soc, grid_use_kw, grid_status,
               solar_total_kwh)
            VALUES (?,?,?,?,?,?,?,?,?)
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
                stats.totals.solar_kwh,
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

    def daily_solar_kwh(self, date_str: str, interval_hours: float = 0.25) -> float:
        """Sum actual solar production for a calendar date.

        Assumes readings are taken every `interval_hours` (default 15 min = 0.25 h).
        Returns 0.0 if no readings exist for that date.
        """
        rows = self._conn.execute(
            "SELECT solar_kw FROM readings WHERE substr(timestamp,1,10)=?",
            (date_str,),
        ).fetchall()
        return round(sum(r[0] for r in rows) * interval_hours, 2)

    def daily_solar_kwh_api(self, date_str: str) -> float:
        """Return actual daily solar kWh from the API's own running total.

        Uses MAX(solar_total_kwh) for the date — the API counter resets at midnight
        and peaks at end-of-day, so MAX gives the true daily production regardless
        of how many polls were missed.  Returns 0.0 if no rows or column missing.
        """
        row = self._conn.execute(
            "SELECT MAX(solar_total_kwh) FROM readings WHERE substr(timestamp,1,10)=?",
            (date_str,),
        ).fetchone()
        return round(float(row[0]), 2) if row and row[0] is not None else 0.0

    def monthly_totals(self, year_month: str, interval_hours: float = 0.25) -> MonthlyTotals:
        """Aggregate energy totals for a calendar month (YYYY-MM).

        Solar uses MAX(solar_total_kwh) per day — the API running counter peaks at
        end-of-day, so summing daily maxima gives true monthly generation regardless
        of poll gaps.  Grid and load are integrated from instantaneous kW readings.
        """
        prefix = year_month + "-"

        # Solar: sum of each day's MAX API counter
        solar_rows = self._conn.execute(
            """
            SELECT substr(timestamp,1,10), MAX(solar_total_kwh)
            FROM readings
            WHERE timestamp LIKE ?
            GROUP BY substr(timestamp,1,10)
            """,
            (prefix + "%",),
        ).fetchall()
        solar_kwh = round(sum(r[1] for r in solar_rows if r[1] is not None), 1)
        days_with_data = len(solar_rows)

        # Grid and load: integrate instantaneous kW over poll interval
        kw_rows = self._conn.execute(
            "SELECT grid_use_kw, home_load_kw FROM readings WHERE timestamp LIKE ?",
            (prefix + "%",),
        ).fetchall()
        grid_import_kwh = round(sum(r[0] for r in kw_rows if r[0] > 0) * interval_hours, 1)
        grid_export_kwh = round(sum(-r[0] for r in kw_rows if r[0] < 0) * interval_hours, 1)
        home_load_kwh   = round(sum(r[1] for r in kw_rows) * interval_hours, 1)

        return MonthlyTotals(
            year_month=year_month,
            solar_kwh=solar_kwh,
            grid_import_kwh=grid_import_kwh,
            grid_export_kwh=grid_export_kwh,
            home_load_kwh=home_load_kwh,
            days_with_data=days_with_data,
        )

    def recent_avg_load(self, hours: int = 2) -> float | None:
        """Average home load over the last N hours of recorded data."""
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        row = self._conn.execute(
            "SELECT AVG(home_load_kw) FROM readings WHERE timestamp >= ?",
            (cutoff,),
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    # ── Seasonal profiles ──────────────────────────────────────────── #

    @staticmethod
    def _season_months(season: str) -> tuple[int, ...]:
        return {
            "spring": (3, 4, 5),
            "summer": (6, 7, 8),
            "fall":   (9, 10, 11),
            "winter": (12, 1, 2),
        }[season]

    def days_in_season(self, season: str) -> int:
        """Distinct calendar days with data that fall in the given season."""
        months = self._season_months(season)
        placeholders = ",".join("?" * len(months))
        row = self._conn.execute(
            f"""
            SELECT COUNT(DISTINCT substr(timestamp,1,10))
            FROM readings
            WHERE CAST(substr(timestamp,6,2) AS INTEGER) IN ({placeholders})
            """,
            months,
        ).fetchone()
        return row[0] if row else 0

    def seasonal_load_profile(self, season: str) -> LoadProfile:
        """Average home load kW keyed by (day_of_week, hour_of_day) for one season."""
        months = self._season_months(season)
        placeholders = ",".join("?" * len(months))
        rows = self._conn.execute(
            f"""
            SELECT day_of_week, hour_of_day, AVG(home_load_kw)
            FROM readings
            WHERE CAST(substr(timestamp,6,2) AS INTEGER) IN ({placeholders})
            GROUP BY day_of_week, hour_of_day
            """,
            months,
        ).fetchall()
        return {(int(r[0]), int(r[1])): float(r[2]) for r in rows}

    def seasonal_solar_profile(self, season: str) -> LoadProfile:
        """Average solar production kW keyed by (day_of_week, hour_of_day) for one season."""
        months = self._season_months(season)
        placeholders = ",".join("?" * len(months))
        rows = self._conn.execute(
            f"""
            SELECT day_of_week, hour_of_day, AVG(solar_kw)
            FROM readings
            WHERE CAST(substr(timestamp,6,2) AS INTEGER) IN ({placeholders})
            GROUP BY day_of_week, hour_of_day
            """,
            months,
        ).fetchall()
        return {(int(r[0]), int(r[1])): float(r[2]) for r in rows}

    def period_totals(self, start_date: str, end_date: str, interval_hours: float = 0.25) -> MonthlyTotals:
        """Aggregate energy totals for an arbitrary date range (inclusive YYYY-MM-DD).

        Useful for billing-cycle summaries that don't align with calendar months.
        Solar uses MAX(solar_total_kwh) per day; grid/load integrated from instantaneous kW.
        """
        solar_rows = self._conn.execute(
            """
            SELECT substr(timestamp,1,10), MAX(solar_total_kwh)
            FROM readings
            WHERE substr(timestamp,1,10) >= ? AND substr(timestamp,1,10) <= ?
            GROUP BY substr(timestamp,1,10)
            """,
            (start_date, end_date),
        ).fetchall()
        solar_kwh      = round(sum(r[1] for r in solar_rows if r[1] is not None), 1)
        days_with_data = len(solar_rows)

        kw_rows = self._conn.execute(
            "SELECT grid_use_kw, home_load_kw FROM readings "
            "WHERE substr(timestamp,1,10) >= ? AND substr(timestamp,1,10) <= ?",
            (start_date, end_date),
        ).fetchall()
        grid_import_kwh = round(sum(r[0] for r in kw_rows if r[0] > 0) * interval_hours, 1)
        grid_export_kwh = round(sum(-r[0] for r in kw_rows if r[0] < 0) * interval_hours, 1)
        home_load_kwh   = round(sum(r[1] for r in kw_rows) * interval_hours, 1)

        return MonthlyTotals(
            year_month=f"{start_date}:{end_date}",
            solar_kwh=solar_kwh,
            grid_import_kwh=grid_import_kwh,
            grid_export_kwh=grid_export_kwh,
            home_load_kwh=home_load_kwh,
            days_with_data=days_with_data,
        )

    def weekly_readings(
        self, start_date: str, end_date: str
    ) -> list[tuple[str, float, float, float]]:
        """Return (timestamp, grid_use_kw, home_load_kw, solar_kw) for a date range."""
        rows = self._conn.execute(
            "SELECT timestamp, grid_use_kw, home_load_kw, solar_kw FROM readings "
            "WHERE substr(timestamp,1,10) >= ? AND substr(timestamp,1,10) <= ? "
            "ORDER BY timestamp",
            (start_date, end_date),
        ).fetchall()
        return [(r[0], float(r[1]), float(r[2]), float(r[3])) for r in rows]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
