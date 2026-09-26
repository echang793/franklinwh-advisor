#!/usr/bin/env python3
"""Rebuild franklinwh_scraper/data/export_prices_legacy2024.json from SDG&E's file.

    1. Download "Legacy 2024 Export Pricing" from
       https://www.sdge.com/solar/solar-billing-plan/export-pricing
       (LY2024 NBT Pricing Upload MIDAS.zip) and unzip it.
    2. python scripts/build_export_prices.py "LY2024 NBT Pricing Upload MIDAS.csv"

Use the file matching your bill's "Export Pricing:" line (Legacy 2023/2024/
2025/2026); each contains the NBT vintage that applies to it.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from franklinwh_scraper.exportprices import DEFAULT_PATH, build_from_csv  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    data = build_from_csv(sys.argv[1])
    DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_PATH.write_text(json.dumps(data, separators=(",", ":")))
    n = sum(len(v) for y in data["years"].values() for v in y.values())
    print(f"wrote {DEFAULT_PATH} ({DEFAULT_PATH.stat().st_size // 1024} KB, "
          f"{len(data['years'])} years, {n} month-blocks)")


if __name__ == "__main__":
    main()
