"""Send a test iMessage for each of the 8 FranklinWH alerts."""

import json
import subprocess
import sys
from pathlib import Path

# Load phone number directly from config file — avoids importing the full
# package which uses Python 3.10+ union type syntax (|).
config_path = Path.home() / ".franklinwh.json"
if not config_path.exists():
    print("ERROR: ~/.franklinwh.json not found. Run 'setup' first.")
    sys.exit(1)

phone = json.loads(config_path.read_text()).get("imessage_phone", "")
if not phone:
    print("ERROR: No iMessage phone number in config. Run 'setup' first.")
    sys.exit(1)


def notify_imessage_text(body: str, phone: str) -> None:
    def esc(s):
        return s.replace('"', '\\"')
    script = (
        f'tell application "Messages"\n'
        f'  set targetService to 1st service whose service type = iMessage\n'
        f'  set targetBuddy to buddy "{phone}" of targetService\n'
        f'  send "{esc(body)}" to targetBuddy\n'
        f'end tell'
    )
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
print(f"Sending 8 test alerts to {phone}...")

alerts = [
    (
        "1/8 — Grid Import (Peak Hours)",
        "⚠️ FranklinWH: Pulling from grid during peak hours (4–9 pm)\n"
        "SoC 45%  |  Grid +1.20 kW  |  Solar 0.00 kW  |  Load 1.20 kW\n"
        "Time: 5:30 PM\n[TEST ALERT]"
    ),
    (
        "2/8 — Low Battery at 1 pm",
        "🟡 FranklinWH: Battery under 40% at 1:15 PM\n"
        "SoC 35% — grid import risk during 4–9 pm peak\n"
        "Solar 2.10 kW  |  Load 0.90 kW\n"
        "Consider switching to Emergency Backup to charge before peak.\n[TEST ALERT]"
    ),
    (
        "3/8 — Emergency Backup Ready",
        "🟢 FranklinWH: Battery at 82% — Emergency Backup target reached\n"
        "Time: 1:52 PM — battery ready before 4 pm peak\n"
        "Solar 3.40 kW  |  Load 1.10 kW\n"
        "You can now switch modes if needed.\n[TEST ALERT]"
    ),
    (
        "4/8 — Low Solar (Cloudy Morning)",
        "☁️ FranklinWH: Low solar at 10:05 AM — cloudy day ahead\n"
        "Solar 0.18 kW  |  SoC 72%  |  Load 1.30 kW\n"
        "Consider conserving battery early — less solar charging expected today.\n[TEST ALERT]"
    ),
    (
        "5/8 — Solar Stopped Mid-Day",
        "🔴 FranklinWH: Solar dropped mid-day — possible issue\n"
        "Was 3.20 kW → now 0.05 kW at 1:10 PM\n"
        "SoC 68%  |  Check inverter or cloud cover.\n[TEST ALERT]"
    ),
    (
        "6/8 — Battery Low at Noon",
        "🟡 FranklinWH: Battery still low at noon — only 27% SoC\n"
        "Solar 2.80 kW available but battery hasn't recovered\n"
        "Time: 12:05 PM  |  Load 1.40 kW\n"
        "Check battery mode — may need manual intervention.\n[TEST ALERT]"
    ),
    (
        "7/8 — End-of-Day Digest",
        "📊 FranklinWH Daily Summary — Sat Apr 26\n"
        "Solar generated:  18.4 kWh\n"
        "Grid imported:     0.8 kWh\n"
        "Grid exported:     4.2 kWh\n"
        "Home used:        14.6 kWh\n"
        "Battery SoC now:  61%\n[TEST ALERT]"
    ),
    (
        "8/8 — Grid Down",
        "🔴 FranklinWH: GRID DOWN at 2:45 PM\n"
        "Running on battery — SoC 74%  |  Load 1.80 kW\n"
        "Solar 2.10 kW\n[TEST ALERT]"
    ),
]

for label, body in alerts:
    print(f"  Sending {label}...", end=" ", flush=True)
    notify_imessage_text(body, phone)
    print("sent")

print(f"\nDone — check your messages on {phone}")
