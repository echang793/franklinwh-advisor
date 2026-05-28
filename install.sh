#!/bin/bash
# FranklinWH Advisor — one-command installer
# Works on macOS and Linux (Ubuntu/Debian/Oracle Cloud free tier)
set -e

echo ""
echo "  FranklinWH Advisor Installer"
echo "  ──────────────────────────────"

# ── Python check ─────────────────────────────────────────────────────
PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON="$cmd"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "  ERROR: Python 3.9+ is required but not found."
    echo "  Install it: https://python.org/downloads"
    exit 1
fi

echo "  Using: $($PYTHON --version)"

# ── Install dependencies ─────────────────────────────────────────────
echo "  Installing dependencies..."
$PYTHON -m pip install --quiet requests click beautifulsoup4 anthropic

# ── Cron setup (Linux only) ──────────────────────────────────────────
if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    CRON_CMD="*/15 7-23 * * * cd $SCRIPT_DIR && $PYTHON scrape.py account advise >> $SCRIPT_DIR/output/advisor.log 2>&1"

    # Add to crontab if not already there
    if ! crontab -l 2>/dev/null | grep -q "scrape.py account advise"; then
        (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
        echo "  Cron job installed (every 15 min, 7am–11pm)"
    else
        echo "  Cron job already exists — skipping"
    fi
fi

echo ""
echo "  Done! Run the setup wizard next:"
echo ""
echo "      $PYTHON scrape.py setup"
echo ""
echo "  Then start the advisor:"
echo ""
echo "      $PYTHON scrape.py start"
echo ""
