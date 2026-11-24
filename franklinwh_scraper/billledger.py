"""Permanent bill ledger: one row per billing cycle, kept past the 30-day state pruning.

`actual_bill_*` entries in the peak-alert state expire after 30 days, so without this the only trace
of how far the estimate was from a real bill is the alert log. A row pairs the real bill with the
estimate the app had for the cycle, so accuracy can be tracked and the projection corrected for a
consistent bias. Pure file I/O and arithmetic: nothing here talks to the network or the history DB.
"""

from __future__ import annotations

import fcntl
import json
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

LEDGER_FILE = "bill_ledger.json"
MATCH_DAYS = 3                  # SDG&E's read date drifts a day or two; same tolerance as _find_actual_bill
MIN_SAMPLES = 3                 # fewer cycles than this is an anecdote, not a bias
MIN_ESTIMATE = 5.0              # $: a percent-off figure against a tiny estimate is noise
FIRST_CALIBRATED_END = date(2026, 9, 17)    # the first cycle whose bill taught the model its rates


def _path(out: Path) -> Path:
    return Path(out) / LEDGER_FILE


def _day(s) -> date | None:
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").date()
    except ValueError:
        return None


def load(out: Path) -> list[dict]:
    """All rows, oldest cycle first. A missing or damaged file reads as empty, never raises."""
    try:
        data = json.loads(_path(out).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    rows = [r for r in data if isinstance(r, dict) and _day(r.get("end")) and isinstance(r.get("actual"), (int, float))]
    return sorted(rows, key=lambda r: r["end"])


@contextmanager
def _locked(out: Path):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / ".bill_ledger.lock", "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _write(out: Path, rows: list[dict]) -> None:
    target = _path(out)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(rows, key=lambda r: r["end"]), indent=1))
    tmp.replace(target)             # atomic: no half-written ledger


def _same_cycle(a: dict, b: dict) -> bool:
    da, db = _day(a["end"]), _day(b["end"])
    return bool(da and db and abs((da - db).days) <= MATCH_DAYS)


def upsert(out: Path, entry: dict) -> dict:
    """Add a cycle, or merge into the row for the same cycle. A later write replaces values it
    carries and never erases one it doesn't (a bare reply must not wipe a parsed bill's detail)."""
    with _locked(out):
        rows = load(out)
        merged = dict(entry)
        for i, row in enumerate(rows):
            if _same_cycle(row, entry):
                merged = {**row, **{k: v for k, v in entry.items() if v is not None}}
                rows.pop(i)
                break
        rows.append(merged)
        _write(out, rows)
    return merged


def _usable(r: dict) -> bool:
    est = r.get("estimate")
    return isinstance(est, (int, float)) and abs(est) >= MIN_ESTIMATE and not r.get("exclude")


def accuracy(rows: list[dict], n: int = 6) -> dict | None:
    """How the estimate has run against real bills, over the last `n` usable cycles (None: too few)."""
    use = sorted((r for r in rows if _usable(r)), key=lambda r: r["end"])[-n:]
    if len(use) < MIN_SAMPLES:
        return None
    pcts = [(r["actual"] - r["estimate"]) / abs(r["estimate"]) * 100 for r in use]
    errs = [r["actual"] - r["estimate"] for r in use]
    return {"n": len(use), "bias_pct": sum(pcts) / len(pcts), "low_pct": min(pcts), "high_pct": max(pcts),
            "mean_abs_err": sum(abs(e) for e in errs) / len(errs), "mean_err": sum(errs) / len(errs)}


def expected(projected: float, stats: dict | None) -> dict | None:
    """What a projected bill probably comes to once the estimate's habitual miss is applied.
    Moves by a percent of |projected|, so a credit month shrinks its credit when the model runs low."""
    if not stats:
        return None
    step = abs(projected) / 100.0
    return {"point": round(projected + step * stats["bias_pct"], 2),
            "low": round(projected + step * stats["low_pct"], 2),
            "high": round(projected + step * stats["high_pct"], 2),
            "n": stats.get("n", 0), "bias_pct": stats["bias_pct"]}


_LOG_LINE = re.compile(
    r"Cycle ending ([A-Z][a-z]{2} \d{1,2}): actual (-?\$?-?\d+(?:\.\d+)?) vs app estimate (-?\$?-?\d+(?:\.\d+)?)")


def _money(tok: str) -> float:
    neg = "-" in tok
    return -abs(float(tok.replace("$", "").replace("-", ""))) if neg else float(tok.replace("$", ""))


def seed_from_alert_log(out: Path) -> int:
    """Recover past cycles from the "Bill reconciliation" messages in alerts_log.jsonl (the only
    place they survive). Later messages win for the same cycle; cycles the ledger already has are
    left alone; the cycle before the model had learned from any bill is kept but excluded from
    accuracy. Returns how many rows were added."""
    try:
        lines = (Path(out) / "alerts_log.jsonl").read_text().splitlines()
    except OSError:
        return 0
    found: list[dict] = []
    for line in lines:
        try:
            rec = json.loads(line)
            body, ts = rec.get("body", ""), datetime.fromisoformat(rec["ts"])
        except (json.JSONDecodeError, KeyError, ValueError, AttributeError):
            continue
        m = _LOG_LINE.search(body) if "Bill reconciliation" in body else None
        if not m:
            continue
        try:
            end = datetime.strptime(f"{m.group(1)} {ts.year}", "%b %d %Y").date()
        except ValueError:
            continue
        if end > ts.date() + timedelta(days=2):          # "Dec 19" logged in January belongs to last year
            end = end.replace(year=end.year - 1)
        row = {"end": end.isoformat(), "actual": _money(m.group(2)), "estimate": _money(m.group(3)),
               "source": "alert-log", "recorded": ts.date().isoformat()}
        if end < FIRST_CALIBRATED_END:
            row["exclude"] = True
        found = [f for f in found if not _same_cycle(f, row)] + [row]
    existing = load(out)
    added = 0
    for row in found:
        if any(_same_cycle(e, row) for e in existing):
            continue
        upsert(out, row)
        existing.append(row)
        added += 1
    return added


def _usd(x: float, signed: bool = False) -> str:
    sign = ("+" if signed else "") if x >= 0 else "-"
    return f"{sign}${abs(x):.2f}"


def format_table(rows: list[dict], n: int = 8) -> str:
    """One line per cycle (newest last): the real bill, the estimate and the miss."""
    if not rows:
        return "No bills recorded yet."
    out = []
    for r in sorted(rows, key=lambda r: r["end"])[-n:]:
        end = _day(r["end"])
        est = r.get("estimate")
        line = f"{end.strftime('%b %-d'):<7} {_usd(r['actual']):>8}"
        line += (f"  est {_usd(est)} ({_usd(r['actual'] - est, signed=True)})" if isinstance(est, (int, float))
                 else "  est n/a")
        if isinstance(r.get("due"), (int, float)):
            line += f"  due {_usd(r['due'])}"
        if r.get("exclude"):
            line += " *"
        out.append(line)
    text = "<code>" + "\n".join(out) + "</code>"
    return text + ("\n* before the model had learned from any bill; not counted in accuracy"
                   if any(r.get("exclude") for r in rows[-n:]) else "")


def format_summary(rows: list[dict], n: int = 8) -> str:
    """The table plus one line on how the estimate has been running against real bills."""
    if not rows:
        return "No bills recorded yet."
    text = format_table(rows, n)
    bank = next((r for r in sorted(rows, key=lambda r: r["end"], reverse=True)
                 if isinstance(r.get("sbp_balance"), (int, float))), None)
    if bank is not None:
        added = (f" (+{_usd(bank['sbp_credited'])} that cycle)"
                 if isinstance(bank.get("sbp_credited"), (int, float)) else "")
        text += f"\nSolar Billing Plan credit bank: {_usd(bank['sbp_balance'])} after the {_day(bank['end']).strftime('%b %-d')} bill{added}."
    stats = accuracy(rows)
    if stats is None:
        have = sum(1 for r in rows if _usable(r))
        return text + f"\nNeed {MIN_SAMPLES} bills with an estimate to say how accurate the app is (have {have})."
    bias = stats["bias_pct"]
    return (text + f"\nThe app has run {abs(bias):.0f}% {'low' if bias > 0 else 'high'} over the last "
            f"{stats['n']} bills (range {stats['low_pct']:+.0f}% to {stats['high_pct']:+.0f}%, "
            f"${stats['mean_abs_err']:.2f} off on average).")


def expected_line(rows: list[dict], projected: float) -> str:
    """"Expect on the real bill: ~$52.64 (about $52.16 to $53.29)..." for a projected cycle total,
    or "" until the ledger has enough cycles to say how the estimate runs."""
    stats = accuracy(rows)
    ex = expected(projected, stats)
    if ex is None:
        return ""
    bias = stats["bias_pct"]
    return (f"Expect on the real bill: ~{_usd(ex['point'])} (about {_usd(ex['low'])} to {_usd(ex['high'])}). "
            f"The app has run {abs(bias):.0f}% {'low' if bias > 0 else 'high'} over the last {stats['n']} bills.")
