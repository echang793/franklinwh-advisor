"""`franklinwh waterheater ...` — set up, check, and go live with water heater control.

Kept out of cli.py (already very long); cli.py registers grp_wh.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import click

from .config import save as save_config
from .econet import CREDS_PATH, EcoNetClient, EcoNetError, EcoNetRateLimited, write_creds
from .tou import cycle_bounds
from .wh_whatif import cycle_report, describe_report

_RESTART = ("     launchctl stop com.franklinwh.advisor && "
            "launchctl start com.franklinwh.advisor")


@click.group("waterheater")
def grp_wh() -> None:
    """Rheem heat pump water heater control (EcoNet)."""


@grp_wh.command("setup")
@click.pass_context
def wh_setup(ctx: click.Context) -> None:
    """Save your EcoNet login, test it, and enable control in dry run."""
    cfg = ctx.obj["config"]
    email = click.prompt("  EcoNet email")
    password = click.prompt("  EcoNet password", hide_input=True)
    write_creds({"email": email, "password": password}, CREDS_PATH)
    try:
        r = EcoNetClient(creds_path=CREDS_PATH).read_state(datetime.now())
    except EcoNetRateLimited as e:
        raise click.ClickException(
            f"EcoNet is rate-limiting this account ({e}). Your login is saved. "
            "Please wait 30 minutes, then run setup once; retrying sooner restarts the block.")
    except EcoNetError as e:
        raise click.ClickException(f"Could not read the water heater: {e}")
    s = r.state
    click.echo(f"  ✓ Found heater: {s.mode.value}, {s.setpoint_f}°F, "
               f"{r.running_state or ('running' if s.running else 'idle')}, "
               f"tank {r.tank_pct if r.tank_pct is not None else '?'}%")
    cfg.wh_control_enabled = True
    cfg.wh_dry_run = True
    save_config(cfg)
    click.echo("  ✓ Water heater control enabled in DRY RUN (logs decisions, sends nothing).\n"
               "  Restart the advisor to pick it up:\n" + _RESTART + "\n"
               "  After a week, review with `franklinwh waterheater status`, then "
               "`franklinwh waterheater go-live`.")


@grp_wh.command("go-live")
@click.pass_context
def wh_go_live(ctx: click.Context) -> None:
    """Turn off dry run — the controller starts changing the heater."""
    cfg = ctx.obj["config"]
    if not getattr(cfg, "wh_control_enabled", False):
        raise click.ClickException("Water heater control isn't set up — run "
                                   "`franklinwh waterheater setup`.")
    if not cfg.wh_dry_run:
        click.echo("  Already live.")
        return
    if not click.confirm("  The advisor will now change your water heater's mode and "
                         "temperature automatically. Continue?"):
        return
    cfg.wh_dry_run = False
    save_config(cfg)
    click.echo("  ✓ Water heater control is LIVE. Restart the advisor:\n" + _RESTART)


def _is_element(running_state: str | None) -> bool:
    t = (running_state or "").lower()
    return "electric" in t or "element" in t


@grp_wh.command("status")
@click.option("--last", default=10, show_default=True, help="Recent log lines to show.")
@click.pass_context
def wh_status(ctx: click.Context, last: int) -> None:
    """Mode, recent decisions, and how often the element ran on-peak."""
    cfg = ctx.obj["config"]
    enabled = getattr(cfg, "wh_control_enabled", False)
    mode = "off" if not enabled else ("DRY RUN" if cfg.wh_dry_run else "LIVE")
    click.echo(f"  Water heater control: {mode}")
    path = Path(cfg.output_dir) / "wh_controller.jsonl"
    entries = []
    try:
        for line in path.read_text().splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        click.echo("  No decisions logged yet.")
        return
    peak = [e for e in entries if e.get("period") == "on_peak" and e.get("running")]
    elem = [e for e in peak if _is_element(e.get("running_state"))]
    click.echo(f"  Log entries: {len(entries)}; on-peak running: {len(peak)}; "
               f"on-peak element runs: {len(elem)} (from EcoNet's running text)")
    for e in entries[-last:]:
        click.echo(f"  {e.get('timestamp', '?')}  {e.get('action', '?'):4}  "
                   f"{e.get('reason', '')}  [{e.get('heater_mode')}, {e.get('heater_setpoint_f')}°F, "
                   f"{e.get('running_state') or '-'}]{'  ERROR ' + e['error'] if e.get('error') else ''}")


def _open_store(cfg):
    """The advisor's history store, or None before it has one."""
    from .history import HistoryStore
    path = Path(cfg.output_dir) / "history.db"
    return HistoryStore(path) if path.exists() else None


@grp_wh.command("compare")
@click.option("--days", type=int, default=None, help="The last N days instead of this billing cycle so far.")
@click.pass_context
def wh_compare(ctx: click.Context, days: int | None) -> None:
    """What Energy Saver, High Demand or Electric would have cost vs heat pump only."""
    cfg = ctx.obj["config"]
    end = date.today() - timedelta(days=1)
    if days:
        start = end - timedelta(days=days - 1)
    else:
        start_day = getattr(cfg, "billing_cycle_start_day", 20)
        start, _ = cycle_bounds(date.today(), start_day)
        if start > end:                       # today is the cycle's first day: show the last one
            start, _ = cycle_bounds(end, start_day)
    store = _open_store(cfg)
    try:
        rep = cycle_report(store, Path(cfg.output_dir), cfg, start, end)
    finally:
        if store is not None and hasattr(store, "close"):
            store.close()
    if rep is None:
        click.echo(f"  No water heater energy data yet for {start:%b %-d} – {end:%b %-d}. The advisor collects "
                   "it daily (backfilling the last 35 days) while water heater control is set up.")
        return
    click.echo(describe_report(rep))
