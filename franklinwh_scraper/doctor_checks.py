"""System-level checks for `franklinwh doctor` — failure modes that took the
advisor down for hours and were only found by hand (2026-09)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


def icloud_path_warning(path: Path, home: Path | None = None) -> str | None:
    """Warning text if `path` sits in a folder iCloud may sync, else None.

    iCloud Desktop & Documents sync briefly locks files (OSError EDEADLK on
    a marker write crash-looped the advisor) and moving the folder broke
    fresh-process path resolution — twice in 2026-09. ~/Projects is not synced.
    """
    home = home or Path.home()
    for synced in (home / "Desktop", home / "Documents", home / "Library" / "Mobile Documents"):
        try:
            path.relative_to(synced)
        except ValueError:
            continue
        return (f"under ~/{synced.relative_to(home)} — iCloud sync can lock files and has "
                f"crashed the advisor; keep it in ~/Projects")
    return None


def parse_launchd_print(text: str) -> tuple[str | None, int | None]:
    """(service state, pid) from `launchctl print` output — the first
    `state =` / `pid =` lines are the service's own; later ones are nested."""
    state = re.search(r"^\s*state = (.+?)\s*$", text, re.M)
    pid = re.search(r"^\s*pid = (\d+)\s*$", text, re.M)
    return (state.group(1) if state else None, int(pid.group(1)) if pid else None)


def _run_launchctl_print(label: str) -> tuple[int, str]:
    proc = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                          capture_output=True, text=True, timeout=10)
    return proc.returncode, proc.stdout + proc.stderr


def launchd_health(label: str, run=_run_launchctl_print) -> tuple[str, str]:
    """Classify a LaunchAgent: (kind, detail), kind one of running / wedged /
    stopped / not_loaded / unknown.

    "wedged" is `state = spawn scheduled` with no PID — launchd believes it
    will spawn the job but never does (bootout/bootstrap/kickstart all "succeed";
    only removing and recreating the plist cleared it, 2026-09-17). It can
    also appear for a moment right after a start, so re-run before acting.
    """
    try:
        rc, out = run(label)
    except (OSError, subprocess.SubprocessError) as e:
        return "unknown", str(e)
    if rc == 113 or "Could not find service" in out:
        return "not_loaded", "not loaded on this machine"
    if rc != 0:
        return "unknown", out.strip().splitlines()[0] if out.strip() else f"launchctl exit {rc}"
    state, pid = parse_launchd_print(out)
    if pid is not None:
        return "running", f"running (pid {pid})"
    if state == "spawn scheduled":
        return "wedged", "state=spawn scheduled, no PID — launchd may be wedged (see RUNBOOK)"
    return "stopped", f"not running (state={state or 'unknown'})"
