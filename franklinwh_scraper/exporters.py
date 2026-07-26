"""Export scraped data to JSON and CSV."""

import csv
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def export_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("Wrote JSON -> %s (%d bytes)", path, path.stat().st_size)


def export_csv(records: list[dict[str, Any]], path: Path, append: bool = False) -> None:
    if not records:
        logger.warning("No records to write to %s", path)
        return

    path.parent.mkdir(parents=True, exist_ok=True)

    flat_records = [_flatten(r) for r in records]
    fieldnames = list({k for r in flat_records for k in r})
    fieldnames.sort()

    write_header = not (append and path.exists())
    mode = "a" if append else "w"

    if append and path.exists():
        # Only checked file existence before, never whether the on-disk
        # header still matches the current field set — a schema change
        # (a field added/removed/renamed) resuming an append against a
        # pre-existing file silently shifted every subsequent row's values
        # under the wrong column header. Reuse the file's own header for
        # column order so alignment is always preserved; anything the old
        # header lacks is dropped (ignore) or left blank (restval), and a
        # mismatch is logged so it isn't silently invisible.
        try:
            with open(path, newline="", encoding="utf-8") as f:
                existing_header = next(csv.reader(f), None)
        except OSError:
            existing_header = None
        if existing_header and set(existing_header) != set(fieldnames):
            logger.warning(
                "CSV schema drift on %s — on-disk header has %s, current fields are %s; "
                "keeping the on-disk column order (mismatched fields ignored/blank)",
                path, existing_header, fieldnames,
            )
        if existing_header:
            fieldnames = existing_header

    with open(path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", restval="")
        if write_header:
            writer.writeheader()
        writer.writerows(flat_records)

    logger.info("%s CSV -> %s (%d rows)", "Appended" if append else "Wrote", path, len(flat_records))


def _flatten(record: dict[str, Any]) -> dict[str, str]:
    flat: dict[str, str] = {}
    for k, v in record.items():
        if isinstance(v, (dict, list)):
            flat[k] = json.dumps(v, ensure_ascii=False)
        elif v is None:
            flat[k] = ""
        else:
            flat[k] = str(v)
    return flat
