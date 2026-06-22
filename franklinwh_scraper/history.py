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
    solar_total_kwh  REAL    NOT NULL DEFAULT 0,
    battery_use_kw   REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_slot ON readings(day_of_week, hour_of_day);
"""

# Per-column migrations for pre-existing DBs. Each runs in its own try/except
# so an already-applied column doesn't block later ones.
_MIGRATIONS = [
    "ALTER TABLE readings ADD COLUMN solar_total_kwh REAL NOT NULL DEFAULT 0;",
    "ALTER TABLE readings ADD COLUMN battery_use_kw REAL NOT NULL DEFAULT 0;",
]


# (day_of_week, hour_of_day) → average kW
LoadProfile = dict[tuple[int, int], float]

# Polls target ~15 min but drift to 1-2 h (or longer during daemon downtime).
# Trapezoidal integration over real timestamps replaces the old fixed-0.25 h
# assumption, which undercounted energy by ~1.5x when polls were sparse.
# Gaps longer than this cap are clamped so multi-hour outages aren't integrated
# as continuous power.
_MAX_INTEGRATION_GAP_H = 1.0


def integrate_intervals(
    readings: list[tuple],
) -> list[tuple[datetime, float, float, float, float]]:
    """Trapezoidal pairing of consecutive kW readings over actual elapsed time.

    Input rows: (timestamp_iso, grid_kw, home_kw, solar_kw) in any order.
    Yields one tuple per interval: (interval_start_dt, hours, grid_kw_avg,
    home_kw_avg, solar_kw_avg). Each interval's hours is the real gap to the
    next reading, clamped to _MAX_INTEGRATION_GAP_H. Multiply a kW_avg by hours
    for that interval's kWh; apply rate_at(interval_start_dt) for TOU cost.
    """
    parsed: list[tuple[datetime, float, float, float]] = []
    for r in readings:
        try:
            dt = datetime.fromisoformat(r[0])
        except (ValueError, TypeError):
            continue
        parsed.append((dt, float(r[1]), float(r[2]), float(r[3])))
    parsed.sort(key=lambda x: x[0])

    out: list[tuple[datetime, float, float, float, float]] = []
    for (d0, g0, h0, s0), (d1, g1, h1, s1) in zip(parsed, parsed[1:]):
        hours = min(_MAX_INTEGRATION_GAP_H, (d1 - d0).total_seconds() / 3600)
        if hours <= 0:
            continue
        out.append((d0, hours, (g0 + g1) / 2, (h0 + h1) / 2, (s0 + s1) / 2))
    return out


@dataclass
class MonthlyTotals:
    year_month: str       # "2026-05"
    solar_kwh: float      # sum of MAX(solar_total_kwh) per day (API running total)
    grid_import_kwh: float
    grid_export_kwh: float
    home_load_kwh: float
    days_with_data: int


class HistoryStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.executescript(_CREATE_SQL)
        for migration in _MIGRATIONS:
            try:
                self._conn.execute(migration)
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
               solar_total_kwh, battery_use_kw)
            VALUES (?,?,?,?,?,?,?,?,?,?)
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
                stats.current.battery_use_kw,
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

    def daily_solar_kwh(self, date_str: str) -> float:
        """Integrate actual solar production for a calendar date (trapezoidal).

        Fallback for daily_solar_kwh_api when the API running counter is absent.
        Returns 0.0 if no readings exist for that date.
        """
        rows = self._conn.execute(
            "SELECT timestamp, grid_use_kw, home_load_kw, solar_kw FROM readings "
            "WHERE substr(timestamp,1,10)=? ORDER BY timestamp",
            (date_str,),
        ).fetchall()
        return round(sum(s * hours for _dt, hours, _g, _h, s in integrate_intervals(rows)), 2)

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

    def period_totals(self, start_date: str, end_date: str) -> MonthlyTotals:
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
            "SELECT timestamp, grid_use_kw, home_load_kw, solar_kw FROM readings "
            "WHERE substr(timestamp,1,10) >= ? AND substr(timestamp,1,10) <= ? "
            "ORDER BY timestamp",
            (start_date, end_date),
        ).fetchall()
        grid_import_kwh = grid_export_kwh = home_load_kwh = 0.0
        for _dt, hours, grid_kw, home_kw, _solar in integrate_intervals(kw_rows):
            if grid_kw > 0:
                grid_import_kwh += grid_kw * hours
            elif grid_kw < 0:
                grid_export_kwh += -grid_kw * hours
            home_load_kwh += home_kw * hours
        grid_import_kwh = round(grid_import_kwh, 1)
        grid_export_kwh = round(grid_export_kwh, 1)
        home_load_kwh   = round(home_load_kwh, 1)

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

    def capacity_samples(
        self, start_date: str, end_date: str, min_soc_drop: float = 30.0,
    ) -> list[float]:
        """Effective usable-capacity estimates (kWh) from clean battery-only discharge runs.

        A run = consecutive readings where the battery is discharging
        (battery_use_kw < 0) and the home is not importing from grid
        (grid_use_kw <= ~0). For each run whose SoC declines by at least
        `min_soc_drop` percent, effective capacity = discharge_kWh / (soc_drop/100).
        Aggregating over a run (not per-sample) suppresses meter noise.
        Returns one capacity estimate per qualifying run.
        """
        rows = self._conn.execute(
            "SELECT timestamp, battery_use_kw, grid_use_kw, battery_soc FROM readings "
            "WHERE substr(timestamp,1,10) >= ? AND substr(timestamp,1,10) <= ? "
            "ORDER BY timestamp",
            (start_date, end_date),
        ).fetchall()

        samples: list[float] = []
        run_kwh = 0.0
        run_soc_start: float | None = None
        prev: tuple[datetime, float, float, float] | None = None

        def _flush(soc_end: float) -> None:
            nonlocal run_kwh, run_soc_start
            if run_soc_start is not None:
                drop = run_soc_start - soc_end
                if drop >= min_soc_drop and run_kwh > 0:
                    samples.append(run_kwh / (drop / 100.0))
            run_kwh = 0.0
            run_soc_start = None

        for ts, batt_kw, grid_kw, soc in rows:
            try:
                dt = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                continue
            batt_kw, grid_kw, soc = float(batt_kw), float(grid_kw), float(soc)
            discharging = batt_kw < -0.05 and grid_kw <= 0.05 and soc < (prev[3] if prev else soc) + 0.01
            if prev is not None and discharging:
                hours = min(_MAX_INTEGRATION_GAP_H, (dt - prev[0]).total_seconds() / 3600)
                if hours > 0:
                    if run_soc_start is None:
                        run_soc_start = prev[3]
                    run_kwh += (-(prev[1] + batt_kw) / 2) * hours  # avg discharge kW × hours
            else:
                _flush(prev[3] if prev else soc)
            prev = (dt, batt_kw, grid_kw, soc)
        if prev is not None:
            _flush(prev[3])
        return samples

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
