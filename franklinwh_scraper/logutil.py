"""Size-based rotation for the LaunchAgent log files."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_MAX_BYTES = 5_000_000
_DEFAULT_KEEP = 3


def rotate_log(path: Path, max_bytes: int = _DEFAULT_MAX_BYTES, keep: int = _DEFAULT_KEEP) -> bool:
    """Rotate `path` once it exceeds `max_bytes`, keeping `keep` old copies
    (path.1 newest ... path.<keep> oldest). Returns whether it rotated.

    launchd holds these files open for append, so they can't be renamed out
    from under the running process: the current file is copied to path.1 and
    then truncated in place. Append mode makes the writer's next line land at
    the new start of the file (checked against a real launchd job — no NUL
    hole). A line or two written between the copy and the truncate can be
    lost; that's the price of not restarting the service. Never raises — log
    housekeeping must not be able to take the advisor down.
    """
    try:
        if not path.is_file() or path.stat().st_size <= max_bytes:
            return False
        for n in range(keep, 1, -1):
            older = path.with_name(f"{path.name}.{n - 1}")
            if older.exists():
                older.replace(path.with_name(f"{path.name}.{n}"))
        shutil.copyfile(path, path.with_name(f"{path.name}.1"))
        with open(path, "r+b") as f:
            f.truncate(0)
        return True
    except OSError as e:
        logger.warning("Log rotation failed for %s: %s", path, e)
        return False


def rotate_known_logs(output_dir: Path, home: Path | None = None,
                      max_bytes: int = _DEFAULT_MAX_BYTES, keep: int = _DEFAULT_KEEP) -> list[Path]:
    """Rotate this project's own logs: <output_dir>/{advisor,dashboard}.log
    (what `install-service` writes) and ~/Library/Logs/franklinwh-*.log
    (where the live agents' stdout has been redirected). Returns what rotated."""
    home = home or Path.home()
    candidates = [output_dir / "advisor.log", output_dir / "dashboard.log"]
    lib_logs = home / "Library" / "Logs"
    if lib_logs.is_dir():
        candidates += sorted(p for p in lib_logs.glob("franklinwh-*.log") if not p.name.endswith((".1", ".2", ".3")))
    return [p for p in candidates if rotate_log(p, max_bytes, keep)]
