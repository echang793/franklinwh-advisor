"""Read this Mac's Tailscale state (`tailscale status --json`) for the watchdog alerts.

The dashboard, the phone widget, the watch shortcut and the Funnel hub all reach the Mac mini over
one Tailscale node, so the advisor watches that node: whether it is running and online, whether its
name is still the one it had (another Mac signing in as the same node renames it), and when its key
expires. The runner is injectable so tests never shell out.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# launchd gives the advisor a bare PATH, so look in the usual CLI install spots too. Never the
# Tailscale.app bundle binary: that is the GUI and refuses `status --json`.
_CANDIDATES = ("/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale")
_TIMEOUT_S = 5
_CACHE_TTL_S = 60.0
_cache: dict = {}


@dataclass(frozen=True)
class TailscaleStatus:
    host_name: str | None
    online: bool
    backend_state: str                 # "Running", "Stopped", "NeedsLogin", ... or "Unreachable"
    key_expiry: datetime | None        # timezone-aware; None when key expiry is disabled
    health: list[str] = field(default_factory=list)


_UNREACHABLE = TailscaleStatus(host_name=None, online=False, backend_state="Unreachable", key_expiry=None)


def _parse_expiry(raw) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.year > 2000 else None      # "0001-01-01T00:00:00Z" means no expiry


def read_status(run=subprocess.run, which=shutil.which, candidates=_CANDIDATES) -> TailscaleStatus | None:
    """This node's status. None when Tailscale isn't installed here (nothing to watch); an
    "Unreachable" status when it is installed but the daemon doesn't answer or the output is junk."""
    binary = which("tailscale") or next((p for p in candidates if os.path.exists(p)), None)
    if not binary:
        return None
    try:
        out = run([binary, "status", "--json"], capture_output=True, text=True, timeout=_TIMEOUT_S)
        if out.returncode != 0:
            logger.debug("tailscale status exited %s", out.returncode)
            return _UNREACHABLE
        data = json.loads(out.stdout)
        me = data["Self"]
        return TailscaleStatus(
            host_name=me.get("HostName"), online=bool(me.get("Online")),
            backend_state=str(data.get("BackendState", "Unknown")),
            key_expiry=_parse_expiry(me.get("KeyExpiry")),
            health=[str(h) for h in (data.get("Health") or [])])
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError, AttributeError):
        logger.debug("tailscale status unreadable", exc_info=True)
        return _UNREACHABLE


def cached_status(now: float | None = None) -> TailscaleStatus | None:
    """`read_status` at most once a minute, so the two alerts that use it share one read per cycle."""
    t = time.monotonic() if now is None else now
    if _cache and t - _cache["at"] < _CACHE_TTL_S:
        return _cache["val"]
    val = read_status()
    _cache.update(at=t, val=val)
    return val
