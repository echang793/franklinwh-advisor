"""Find the battery reserve SoC the owner set, from the data.

When the reserve is raised before a night grid charge (or simply left at its default),
the battery stops discharging at that level and holds it for hours while the grid
carries the house. So a long stretch of one SoC, with the battery idle and the grid
supplying the load, is the reserve setting. Nothing reads the setting from the system;
this reads it back from what the battery did.
"""

import statistics
from datetime import datetime

MIN_HOLD_MINUTES = 90        # an hour of flat SoC happens by chance; a night does not
MAX_SOC_SPAN = 1.0           # SoC is reported in whole points: allow one point of rounding
MAX_BATT_KW = 0.15           # idle: neither charging nor discharging
MIN_GRID_KW = 0.3            # the grid, not the battery, is carrying the house
MAX_GAP_MINUTES = 20         # an outage or missed polls end a hold rather than bridge it


def _minutes(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60.0


def detect_reserve_hold(rows: list[tuple[str, float, float, float]],
                        min_minutes: float = MIN_HOLD_MINUTES) -> dict | None:
    """The longest reserve hold in `rows` (timestamp, soc, battery_use_kw, grid_use_kw),
    oldest first: {"level": whole-percent SoC, "start", "end", "minutes"}, or None."""
    best = None
    i, n = 0, len(rows)
    while i < n:
        lo = hi = rows[i][1]
        j = i
        while j + 1 < n and _minutes(rows[j][0], rows[j + 1][0]) <= MAX_GAP_MINUTES:
            nlo, nhi = min(lo, rows[j + 1][1]), max(hi, rows[j + 1][1])
            if nhi - nlo > MAX_SOC_SPAN:
                break
            lo, hi, j = nlo, nhi, j + 1
        seg = rows[i:j + 1]
        minutes = _minutes(seg[0][0], seg[-1][0])
        if (minutes >= min_minutes
                and statistics.median(abs(r[2]) for r in seg) <= MAX_BATT_KW
                and statistics.median(r[3] for r in seg) >= MIN_GRID_KW
                and (best is None or minutes > best["minutes"])):
            best = {"level": int(round(statistics.median(r[1] for r in seg))),
                    "start": seg[0][0], "end": seg[-1][0], "minutes": minutes}
        i = j + 1
    return best
