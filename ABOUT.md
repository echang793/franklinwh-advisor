# FranklinWH Energy Advisor

This tool monitors your FranklinWH home battery system and sends you alerts
(iMessage on macOS, or Telegram on any device) when it recommends changing your
battery mode.

---

## What It Does

Every 15 minutes (7am to 11pm daily) it automatically:

1. Logs into your FranklinWH account and reads live data:
   - Battery state of charge (SoC %)
   - Solar production (kW)
   - Home load (kW)
   - Grid import/export (kW)
   - Grid status (normal / down / off)

2. Fetches a 48-hour solar irradiance forecast for your location using the Open-Meteo weather API (free, no account needed).

3. Analyzes your historical usage patterns (stored locally in `output/history.db`) to predict your energy demand over the next 12 hours.

4. Combines all three sources to recommend a battery mode:
   - **Self-Consumption** — maximize solar use, minimize grid import
   - **Emergency Backup** — charge reserves before bad weather or high demand
   - **No change needed** — current mode is appropriate

5. Sends you an alert (iMessage or Telegram) if the recommendation changes or a critical condition is detected (grid down, battery critically low).

---

## When You Get a Text

### Mode Change Alerts
Sends only when action is needed. "Battery OK" messages are suppressed — you only hear about it when something requires attention. Each mode alert fires at most once per day.

- Good solar day ahead → switch to Self-Consumption
- Bad weather or high demand → switch to Emergency Backup
- Grid down or battery critically low → immediate alert

### Daily Scheduled Alerts

| Time | Alert |
|------|-------|
| 8:00–8:30 am | **Morning preview** — current SoC + predicted solar kWh for the day |
| 9:30–10:30 am | **Low solar morning** — solar < 0.5 kW, cloudy day expected |
| 11:00 am–3:00 pm | **Solar stopped mid-day** — possible inverter issue |
| 11:30 am–12:30 pm | **Battery low at noon** — SoC < 30% despite available solar |
| 1:00–2:00 pm | **Low battery before peak** — SoC < 40%, grid import risk during 4–9 pm |
| 1:40–2:10 pm | **Emergency Backup ready** — SoC ≥ 80%, battery charged before 4 pm peak |
| 4:00–9:00 pm | **Grid import during peak** — grid draw > 0.3 kW |
| 9:00–9:59 pm | **End-of-day digest** — solar generated, grid consumed/exported, home used, final SoC, estimated backup duration |

### Immediate Alerts
Fire as soon as conditions are met; most limited to once per day.

- **Grid down** — running on battery only; includes estimated backup hours at current load
- **Battery fully charged** — SoC reaches 99% (once per charge cycle; resets after SoC drops below 90%)
- **Battery no longer full** — SoC drops below 90% during 3–7 pm
- **Battery draining fast** — SoC dropping > 8%/hr while below 35%
- **Battery not charging** — solar > 1.5 kW but battery idle and SoC < 80% between 10am–2pm

---

## Files

```
scrape.py                  Main entry point
franklinwh_scraper/        Python package with all the logic
output/
  history.db               Your usage history (improves predictions over time)
  advisor_log.jsonl        Every recommendation ever made, with full details
  advisor.log              Raw output from each cron run
  poll_log.csv             Raw energy readings (if you use the poll command)

~/.franklinwh.json         Your saved credentials and settings (private, chmod 600)
```

---

## Commands

```bash
python3 scrape.py setup                  # First-time configuration wizard
python3 scrape.py start                  # Run the advisor using saved config
python3 scrape.py install-service        # Install macOS LaunchAgent (auto-start on login)
python3 scrape.py account advise --watch # Run continuously (checks every N min)
python3 scrape.py account stats          # Live energy snapshot
python3 scrape.py account poll           # Log stats to CSV continuously
python3 scrape.py account history        # Show your usage profile by hour
python3 scrape.py account gateways       # List gateways on your account
python3 scrape.py scrape products        # Scrape product specs from franklinwh.com
python3 scrape.py scrape faq             # Scrape FAQ articles
python3 scrape.py scrape all             # Scrape all public website data
```

---

## Notifications

Two channels are supported — configure either or both:

**iMessage (macOS only)**
Alerts sent from your Mac's Messages app to any phone number.
Requires: macOS with Messages signed in, phone number configured in setup.

**Telegram (cross-platform)**
Free Telegram bot messages to any chat.
Setup: create a bot via @BotFather → get your token → message your bot → run setup wizard and it auto-detects your chat ID.

Both channels send the same alerts simultaneously if both are configured.

---

## Notes

- Predictions improve over time. After 3 days of data the advisor incorporates your usage patterns. After 7 days confidence is high.

- Solar generation estimates improve as the system self-calibrates. On sunny days it records your panel output vs. irradiance and builds a measured system peak kW. After 3 sunny readings, the morning preview and 12h solar forecasts switch from historical averages to weather-adjusted estimates.

- Only one advisor watcher can run at a time. A second instance will refuse and point you to the PID file at `~/.franklinwh.pid`. Delete that file if the process died unexpectedly.

- Run `python3 scrape.py install-service` to install a macOS LaunchAgent that starts the advisor automatically at login and restarts it on crash.

- The tool never stores your password in logs or output files. Credentials are only saved in `~/.franklinwh.json` (owner-read-only).

- Weather forecast is cached for 30 minutes. A brief network outage will not kill the advisory — the last good forecast is reused until a fresh fetch succeeds.

- Estimated backup duration uses your configured battery capacity (set during setup). Default is 13.6 kWh — update via `python3 scrape.py setup` if your system is different (aPower 10 = 10 kWh, aPower 15 = 15 kWh, stacked = sum of usable kWh).

- Weather data is from [Open-Meteo](https://open-meteo.com) (free, no API key required).

- Energy API is from energy.franklinwh.com (the same backend used by the FranklinWH mobile app).
