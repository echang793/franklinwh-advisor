#!/bin/bash
# FranklinWH Advisor — pull latest code and restart the service
set -e

echo "  Pulling latest updates..."
git pull origin main

if [[ "$OSTYPE" == "darwin"* ]]; then
    PLIST="$HOME/Library/LaunchAgents/com.franklinwh.advisor.plist"
    if [ -f "$PLIST" ]; then
        echo "  Restarting LaunchAgent..."
        launchctl unload "$PLIST" 2>/dev/null || true
        launchctl load "$PLIST"
        echo "  Done. Advisor restarted with latest code."
    else
        echo "  No LaunchAgent found — run 'python3 scrape.py install-service' first."
    fi
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    echo "  Cron job will pick up changes on next run (no restart needed)."
    echo "  Done."
fi
