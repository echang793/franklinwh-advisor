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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# iCloud Desktop/Documents sync locks files and has crashed the advisor twice.
case "$SCRIPT_DIR" in
    "$HOME/Desktop"/*|"$HOME/Documents"/*)
        echo "  WARNING: $SCRIPT_DIR is in an iCloud-synced folder."
        echo "  Move the repo to ~/Projects first (see RUNBOOK.md)." ;;
esac

# ── Install dependencies ─────────────────────────────────────────────
echo "  Installing dependencies..."
PIP_FLAGS="--quiet"
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS Homebrew Python is externally managed — must pass this flag
    PIP_FLAGS="$PIP_FLAGS --break-system-packages"
fi
# Editable install of the package itself so every dependency in pyproject.toml
# (fastapi, uvicorn, ...) is present — the old hand-typed list missed some.
$PYTHON -m pip install $PIP_FLAGS -e "$SCRIPT_DIR"

# ── Cron setup (Linux only) ──────────────────────────────────────────
if [[ "$OSTYPE" == "linux-gnu"* ]]; then
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
echo "  Running on more than one machine? Set run_on_host in ~/.franklinwh.json and"
echo "  keep the service on ONE machine only (RUNBOOK.md: \"Adding or moving to another machine\")."
echo ""
