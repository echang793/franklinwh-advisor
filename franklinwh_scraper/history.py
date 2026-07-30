"""SQLite-backed store for historical energy readings and load profile building."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .account import Stats

logger = logging.getLogger(__name__)

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
    battery_use_kw   REAL    NOT NULL DEFAULT 0,
    -- Cumulative daily counters (like solar_total_kwh, they reset at midnight)
    -- for the three paths that serve home load. See Totals in account.py.
    battery_load_kwh REAL    NOT NULL DEFAULT 0,
    solar_load_kwh   REAL    NOT NULL DEFAULT 0,
    grid_load_kwh    REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_slot ON readings(day_of_week, hour_of_day);
CREATE INDEX IF NOT EXISTS idx_timestamp ON readings(timestamp);
"""

# Per-column migrations for pre-existing DBs. Each runs in its own try/except
# so an already-applied column doesn't block later ones.
_MIGRATIONS = [
    "ALTER TABLE readings ADD COLUMN solar_total_kwh REAL NOT NULL DEFAULT 0;",
    "ALTER TABLE readings ADD COLUMN battery_use_kw REAL NOT NULL DEFAULT 0;",
    "ALTER TABLE readings ADD COLUMN battery_load_kwh REAL NOT NULL DEFAULT 0;",
    "ALTER TABLE readings ADD COLUMN solar_load_kwh REAL NOT NULL DEFAULT 0;",
    "ALTER TABLE readings ADD COLUMN grid_load_kwh REAL NOT NULL DEFAULT 0;",
]


# (day_of_week, hour_of_day) → average kW
LoadProfile = dict[tuple[int, int], float]

# Polls target ~15 min but drift to 1-2 h (or longer during daemon downtime).
# Trapezoidal integration over real timestamps replaces the old fixed-0.25 h
# assumption, which undercounted energy by ~1.5x when polls were sparse.
# Gaps longer than this cap are clamped so multi-hour outages aren't integrated
# as continuous power.
_MAX_INTEGRATION_GAP_H = 1.0


def _next_day(date_str: str) -> str:
    """'2026-07-02' -> '2026-07-03' — half-open upper bound for a timestamp range.

    ISO8601 timestamp strings sort lexicographically, so `timestamp >= start
    AND timestamp < _next_day(end)` lets SQLite use a plain index on the
    timestamp column, unlike `substr(timestamp,1,10) = ?` which can't.
    """
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


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
        # check_same_thread=False: webapi.py opens its own read-only connections
        # to this same file from a different thread/process; WAL mode + a
        # busy_timeout let concurrent readers wait out a writer's commit
        # instead of raising "database is locked" on the first collision.
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(_CREATE_SQL)
            for migration in _MIGRATIONS:
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass  # column already exists
            conn.commit()
        except Exception:
            conn.close()
            raise
        self._conn = conn

    # ---------------------------------------------------------------- #

    def record(self, stats: Stats) -> None:
        now = datetime.now()
        self._conn.execute(
            """
            INSERT INTO readings
              (timestamp, day_of_week, hour_of_day,
               home_load_kw, solar_kw, battery_soc, grid_use_kw, grid_status,
               solar_total_kwh, battery_use_kw,
               battery_load_kwh, solar_load_kwh, grid_load_kwh)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                stats.totals.battery_load_kwh,
                stats.totals.solar_load_kwh,
                stats.totals.grid_load_kwh,
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

    def slot_counts(self) -> dict[tuple[int, int], int]:
        """Number of readings per (day_of_week, hour_of_day) slot."""
        rows = self._conn.execute(
            "SELECT day_of_week, hour_of_day, COUNT(*) FROM readings "
            "GROUP BY day_of_week, hour_of_day"
        ).fetchall()
        return {(int(r[0]), int(r[1])): int(r[2]) for r in rows}

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
            "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
            (date_str, _next_day(date_str)),
        ).fetchall()
        return round(sum(s * hours for _dt, hours, _g, _h, s in integrate_intervals(rows)), 2)

    _RESET_TOLERANCE_KWH = 0.05  # float noise floor when checking monotonicity

    def daily_solar_kwh_api(self, date_str: str) -> float:
        """Return actual daily solar kWh from the API's own running total.

        MAX(solar_total_kwh) for the date normally gives the true daily
        production regardless of how many polls were missed, since the API
        counter resets at midnight and peaks at end-of-day. But a gateway
        reboot or MQTT session reset can also reset the counter *mid-day* —
        if production after that reset stays below the pre-reset peak, a
        naive MAX() would silently under-report the day. Detect a mid-day
        drop and fall back to the trapezoidal method (daily_solar_kwh) when
        the series isn't monotonically non-decreasing.
        """
        rows = self._conn.execute(
            "SELECT solar_total_kwh FROM readings WHERE timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp",
            (date_str, _next_day(date_str)),
        ).fetchall()
        values = [r[0] for r in rows if r[0] is not None]
        if not values:
            return 0.0
        for prev, cur in zip(values, values[1:]):
            if cur < prev - self._RESET_TOLERANCE_KWH:
                logger.info("Mid-day solar counter reset detected for %s — using trapezoidal fallback", date_str)
                return self.daily_solar_kwh(date_str)
        return round(max(values), 2)

    def quiet_hour_loads(self, start_date: str, end_date: str) -> dict[str, list[float]]:
        """Home load readings during the overnight quiet window, by date.

        Midnight-5am approximates the always-on draw: no solar, and the
        household is asleep, so what's left is the fridge, networking gear,
        standby loads and anything accidentally left running. Grouped in
        Python rather than aggregated in SQL so the caller picks the
        statistic (see _alert_baseline_load_drift, which uses a percentile
        rather than a min).
        """
        rows = self._conn.execute(
            "SELECT substr(timestamp,1,10) AS d, home_load_kw FROM readings "
            "WHERE timestamp >= ? AND timestamp < ? AND hour_of_day IN (0,1,2,3,4) "
            "ORDER BY timestamp",
            (start_date, _next_day(end_date)),
        ).fetchall()
        out: dict[str, list[float]] = {}
        for d, kw in rows:
            if kw is None:
                continue
            out.setdefault(d, []).append(float(kw))
        return out

    def grid_charge_days(self, start_date: str, end_date: str) -> set[str]:
        """Dates where the battery was charged from the grid.

        Grid import while the battery charges and solar is essentially absent
        is the observable signature that Emergency Backup (or a TOU charge
        window) was actually engaged — there's no API field reporting the
        configured mode. Requires 2+ such readings in a day so a single
        ambiguous sample doesn't count.
        """
        rows = self._conn.execute(
            "SELECT substr(timestamp,1,10) AS d, COUNT(*) FROM readings "
            "WHERE timestamp >= ? AND timestamp < ? "
            "  AND grid_use_kw > 0.3 AND battery_use_kw < -0.3 AND solar_kw < 0.5 "
            "GROUP BY d HAVING COUNT(*) >= 2",
            (start_date, _next_day(end_date)),
        ).fetchall()
        return {r[0] for r in rows}

    def daily_attribution(self, date_str: str) -> tuple[float, float, float] | None:
        """Return (battery→home, solar→home, grid→home) kWh for a calendar date.

        These are cumulative daily counters that reset at midnight, same as
        solar_total_kwh, so the day's total is normally just the final value.
        A gateway reboot can also reset them mid-day, which a naive MAX()
        would silently under-report.

        Unlike daily_solar_kwh_api there is no instantaneous-kW series to fall
        back on for these paths, so on a detected reset we sum the peak of
        each monotonic run instead. Monotonicity is checked on the *sum* of
        the three, since they reset together.

        Returns None when there are no readings for that date.
        """
        rows = self._conn.execute(
            "SELECT battery_load_kwh, solar_load_kwh, grid_load_kwh FROM readings "
            "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
            (date_str, _next_day(date_str)),
        ).fetchall()
        triples = [
            (float(r[0] or 0.0), float(r[1] or 0.0), float(r[2] or 0.0))
            for r in rows
        ]
        if not triples:
            return None

        segments: list[list[tuple[float, float, float]]] = [[]]
        prev_total = None
        for t in triples:
            total = sum(t)
            if prev_total is not None and total < prev_total - self._RESET_TOLERANCE_KWH:
                logger.info("Mid-day attribution counter reset detected for %s", date_str)
                segments.append([])
            segments[-1].append(t)
            prev_total = total

        batt = sum(max((s[0] for s in seg), default=0.0) for seg in segments if seg)
        sol  = sum(max((s[1] for s in seg), default=0.0) for seg in segments if seg)
        grid = sum(max((s[2] for s in seg), default=0.0) for seg in segments if seg)
        return round(batt, 2), round(sol, 2), round(grid, 2)

    def daily_battery_kwh(self, date_str: str) -> tuple[float, float]:
        """Return (charge_kwh, discharge_kwh) for a calendar date via trapezoidal integration.

        battery_use_kw > 0 = discharging, < 0 = charging.
        Returns (0.0, 0.0) if no readings exist for that date.
        """
        rows = self._conn.execute(
            "SELECT timestamp, battery_use_kw FROM readings "
            "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
            (date_str, _next_day(date_str)),
        ).fetchall()
        chg = dis = 0.0
        for i in range(1, len(rows)):
            t1, b1 = rows[i - 1]
            t2, b2 = rows[i]
            try:
                # Clamped like every sibling integrator (integrate_intervals,
                # capacity_samples) — an unclamped gap (daemon down for hours)
                # would otherwise integrate that whole span as continuous
                # charge/discharge power, wildly inflating the day's total.
                dt_h = min(
                    _MAX_INTEGRATION_GAP_H,
                    (datetime.fromisoformat(t2) - datetime.fromisoformat(t1)).total_seconds() / 3600,
                )
            except (ValueError, TypeError):
                # TypeError: a NULL timestamp (fromisoformat(None)) — every
                # sibling integration function (integrate_intervals,
                # capacity_samples, total_discharge_kwh) already catches
                # both; this one only caught ValueError and would crash
                # instead of skipping the bad row.
                logger.warning("Skipping interval with bad timestamp: %r → %r", t1, t2)
                continue
            avg = (b1 + b2) / 2
            if avg > 0:
                dis += avg * dt_h
            else:
                chg += -avg * dt_h
        return round(chg, 2), round(dis, 2)

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
        months = {
            "spring": (3, 4, 5),
            "summer": (6, 7, 8),
            "fall":   (9, 10, 11),
            "winter": (12, 1, 2),
        }.get(season.lower())
        if months is None:
            raise ValueError(f"Unknown season {season!r} — expected spring/summer/fall/winter")
        return months

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

    def seasonal_slot_counts(self, season: str) -> dict[tuple[int, int], int]:
        """Number of readings per (day_of_week, hour_of_day) slot, within one
        season only — the season-scoped counterpart to slot_counts(), needed
        so forecast confidence reflects the sample size actually backing a
        seasonal-profile value instead of the unrelated all-time count for
        that weekday/hour (which can be large purely from other seasons)."""
        months = self._season_months(season)
        placeholders = ",".join("?" * len(months))
        rows = self._conn.execute(
            f"""
            SELECT day_of_week, hour_of_day, COUNT(*)
            FROM readings
            WHERE CAST(substr(timestamp,6,2) AS INTEGER) IN ({placeholders})
            GROUP BY day_of_week, hour_of_day
            """,
            months,
        ).fetchall()
        return {(int(r[0]), int(r[1])): int(r[2]) for r in rows}

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

    def recent_load_profile(self, days: int) -> LoadProfile:
        """Average home load kW keyed by (day_of_week, hour_of_day) over the trailing N days.

        Weights a sustained recent change (new EV, HVAC swap) far more heavily
        than an all-time or seasonal average would, at the cost of more noise
        per slot — callers should blend with a longer baseline, not use alone.
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            """
            SELECT day_of_week, hour_of_day, AVG(home_load_kw)
            FROM readings
            WHERE timestamp >= ?
            GROUP BY day_of_week, hour_of_day
            """,
            (cutoff,),
        ).fetchall()
        return {(int(r[0]), int(r[1])): float(r[2]) for r in rows}

    def recent_solar_profile(self, days: int) -> LoadProfile:
        """Average solar production kW keyed by (day_of_week, hour_of_day) over the trailing N days."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            """
            SELECT day_of_week, hour_of_day, AVG(solar_kw)
            FROM readings
            WHERE timestamp >= ?
            GROUP BY day_of_week, hour_of_day
            """,
            (cutoff,),
        ).fetchall()
        return {(int(r[0]), int(r[1])): float(r[2]) for r in rows}

    def recent_slot_counts(self, days: int) -> dict[tuple[int, int], int]:
        """Number of readings per (day_of_week, hour_of_day) slot in the trailing N days."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            "SELECT day_of_week, hour_of_day, COUNT(*) FROM readings "
            "WHERE timestamp >= ? GROUP BY day_of_week, hour_of_day",
            (cutoff,),
        ).fetchall()
        return {(int(r[0]), int(r[1])): int(r[2]) for r in rows}

    def period_totals(self, start_date: str, end_date: str) -> MonthlyTotals:
        """Aggregate energy totals for an arbitrary date range (inclusive YYYY-MM-DD).

        Useful for billing-cycle summaries that don't align with calendar months.
        Solar uses MAX(solar_total_kwh) per day; grid/load integrated from instantaneous kW.
        """
        end_exclusive = _next_day(end_date)
        solar_rows = self._conn.execute(
            """
            SELECT substr(timestamp,1,10), MAX(solar_total_kwh)
            FROM readings
            WHERE timestamp >= ? AND timestamp < ?
            GROUP BY substr(timestamp,1,10)
            """,
            (start_date, end_exclusive),
        ).fetchall()
        solar_kwh      = round(sum(r[1] for r in solar_rows if r[1] is not None), 1)
        days_with_data = len(solar_rows)

        kw_rows = self._conn.execute(
            "SELECT timestamp, grid_use_kw, home_load_kw, solar_kw FROM readings "
            "WHERE timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp",
            (start_date, end_exclusive),
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
            "WHERE timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp",
            (start_date, _next_day(end_date)),
        ).fetchall()
        return [(r[0], float(r[1]), float(r[2]), float(r[3])) for r in rows]

    def soc_near(self, timestamp: str) -> float | None:
        """Battery SoC from the reading closest to `timestamp` (ISO string),
        searching a +/-30 min window. None if no reading is that close."""
        row = self._conn.execute(
            "SELECT battery_soc, timestamp FROM readings "
            "WHERE timestamp >= ? AND timestamp <= ? "
            "ORDER BY ABS(strftime('%s', timestamp) - strftime('%s', ?)) LIMIT 1",
            (
                (datetime.fromisoformat(timestamp) - timedelta(minutes=30)).isoformat(),
                (datetime.fromisoformat(timestamp) + timedelta(minutes=30)).isoformat(),
                timestamp,
            ),
        ).fetchone()
        return float(row[0]) if row else None

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
            "WHERE timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp",
            (start_date, _next_day(end_date)),
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
            # battery_use_kw > 0 = discharging, < 0 = charging (account.py's
            # documented sign convention, same one daily_battery_kwh uses) —
            # this was inverted, which meant the gate could never match real
            # discharge data and capacity_samples() always returned [].
            discharging = batt_kw > 0.05 and grid_kw <= 0.05 and soc < (prev[3] if prev else soc) + 0.01
            if prev is not None and discharging:
                hours = min(_MAX_INTEGRATION_GAP_H, (dt - prev[0]).total_seconds() / 3600)
                if hours > 0:
                    if run_soc_start is None:
                        run_soc_start = prev[3]
                    run_kwh += ((prev[1] + batt_kw) / 2) * hours  # avg discharge kW × hours
            else:
                _flush(prev[3] if prev else soc)
            prev = (dt, batt_kw, grid_kw, soc)
        if prev is not None:
            _flush(prev[3])
        return samples

    def total_discharge_kwh(self, start_date: str | None = None, end_date: str | None = None) -> float:
        """Integrate total battery discharge kWh via trapezoidal rule over stored readings.

        Returns energy drawn from the battery (positive value).  Caps time gaps
        at 1 hour to avoid counting idle periods as discharge.
        """
        where = "WHERE battery_use_kw IS NOT NULL"
        params: list[str] = []
        if start_date:
            where += " AND timestamp >= ?"
            params.append(start_date)
        if end_date:
            where += " AND timestamp < ?"
            params.append(_next_day(end_date))
        rows = self._conn.execute(
            f"SELECT timestamp, battery_use_kw FROM readings {where} ORDER BY timestamp",
            params,
        ).fetchall()
        total = 0.0
        for i in range(1, len(rows)):
            try:
                t0 = datetime.fromisoformat(rows[i - 1][0])
                t1 = datetime.fromisoformat(rows[i][0])
            except (ValueError, TypeError):
                continue
            hours = min(1.0, (t1 - t0).total_seconds() / 3600)
            avg_kw = (rows[i - 1][1] + rows[i][1]) / 2
            if avg_kw < 0:
                total += -avg_kw * hours
        return round(total, 1)

    def first_reading_date(self) -> str | None:
        """Return the earliest recorded timestamp, or None if no data."""
        row = self._conn.execute("SELECT MIN(timestamp) FROM readings").fetchone()
        return row[0][:10] if row and row[0] else None

    def rollup_old_readings(self, older_than_days: int = 180) -> int:
        """Downsample readings older than N days from ~5-min to one row per
        (date, hour), cutting old row count ~12x. readings.db otherwise
        grows unbounded forever at a 5-min poll cadence.

        Collapsing to one row per *day* (like a typical retention rollup)
        would destroy the (day_of_week, hour_of_day) slot granularity the
        predictor's load/solar profiles depend on — one row per hour keeps
        every slot the predictor actually buckets by, just with far fewer
        near-duplicate samples per slot for old history. Recent data (the
        window that matters most for accuracy) is left untouched.

        Returns the number of rows removed. Call periodically (e.g. weekly),
        not every poll — it's a full historical scan.
        """
        cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat()
        buckets = self._conn.execute(
            """
            SELECT substr(timestamp,1,13) AS bucket,
                   day_of_week, hour_of_day,
                   AVG(home_load_kw), AVG(solar_kw), AVG(battery_soc),
                   AVG(grid_use_kw), MAX(solar_total_kwh), AVG(battery_use_kw),
                   MAX(battery_load_kwh), MAX(solar_load_kwh), MAX(grid_load_kwh),
                   COUNT(*), MIN(timestamp)
            FROM readings
            WHERE timestamp < ?
            GROUP BY bucket
            HAVING COUNT(*) > 1
            """,
            (cutoff,),
        ).fetchall()
        removed = 0
        # Every column must be listed in the INSERT below — anything omitted
        # is silently reset to its DEFAULT 0 when a bucket is rolled up.
        # MAX (not AVG) for the cumulative daily counters, matching
        # solar_total_kwh.
        for (bucket, dow, hod, load, solar, soc, grid, total, batt,
             batt_load, solar_load, grid_load, cnt, first_ts) in buckets:
            self._conn.execute("DELETE FROM readings WHERE substr(timestamp,1,13) = ?", (bucket,))
            self._conn.execute(
                "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
                "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw,"
                "battery_load_kwh,solar_load_kwh,grid_load_kwh) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (first_ts, dow, hod, load, solar, soc, grid, "normal", total, batt,
                 batt_load, solar_load, grid_load),
            )
            removed += cnt - 1
        if buckets:
            self._conn.commit()
            logger.info("Rolled up %d old readings into %d hourly buckets (%d rows removed)",
                       sum(b[12] for b in buckets), len(buckets), removed)
        return removed

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
