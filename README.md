# FranklinWH Energy Advisor

Monitors your [FranklinWH](https://franklinwh.com) home battery system and sends smart alerts via **Telegram** (any device) or **iMessage** (macOS) when it recommends changing your battery mode.

Works on macOS and Linux (including Oracle Cloud free tier, Raspberry Pi, etc.).

---

## What it does

Every 15 minutes (7 am – 11 pm daily) it:

1. Reads live data from your FranklinWH account — battery SoC, solar, grid, load
2. Fetches a 48-hour solar irradiance forecast from [Open-Meteo](https://open-meteo.com) (free, no key)
3. Analyzes your historical usage patterns to predict demand over the next 12 hours
4. Recommends a battery mode and sends an alert if action is needed

### Alerts you get

**Mode change alerts** (only when action is needed):
- Good solar day ahead → switch to Self-Consumption
- Bad weather or high demand → switch to Emergency Backup

**Scheduled daily alerts:**
- 8:00 am — Morning preview: SoC + predicted solar generation for the day
- 9:30 am — Low solar warning if cloudy morning detected
- 11 am–3 pm — Solar dropped mid-day (possible inverter issue)
- 11:30 am–12:30 pm — Battery still low at noon despite available solar
- 1–2 pm — Battery under 40% before peak hours
- 1:40–2:10 pm — Battery at 80%+ before 4 pm peak
- 4–9 pm — Grid import during peak hours
- 9 pm — End-of-day digest with daily totals and estimated backup duration

**Immediate alerts:**
- Grid down — with estimated battery backup hours at current load
- Battery fully charged (SoC ≥ 99%)
- Battery dropping below 90% during 3–7 pm
- Battery draining fast (> 8%/hr below 35% SoC)
- Battery not charging despite strong solar (possible inverter/mode issue)

---

## Requirements

- Python 3.9+
- A FranklinWH account (the same login used by the mobile app)
- Telegram bot (free, takes 2 minutes to set up) **or** macOS with Messages

---

## Install

```bash
git clone https://github.com/echang793/franklinwh-advisor
cd franklinwh-advisor
bash install.sh
```

Then run the setup wizard:

```bash
python3 scrape.py setup
```

The wizard will ask for:
- Your FranklinWH email and password
- Your home location (for solar forecast)
- Telegram bot token (create one via [@BotFather](https://t.me/BotFather)) or iMessage phone number
- Your battery's usable capacity in kWh (check your FranklinWH app)

### Start monitoring

```bash
python3 scrape.py start
```

### Auto-start on login (macOS)

```bash
python3 scrape.py install-service
```

This installs a LaunchAgent that starts the advisor at login and restarts it if it crashes.

### Auto-start on Linux (cron)

The `install.sh` script sets this up automatically. To do it manually:

```
*/15 7-23 * * * cd /path/to/franklinwh-advisor && python3 scrape.py account advise >> output/advisor.log 2>&1
```

---

## Configuration

All settings are stored at `~/.franklinwh.json` (chmod 600, never committed to git).

To update any setting:

```bash
python3 scrape.py setup
```

### Environment variable overrides

Useful for Docker or CI:

```
FRANKLINWH_EMAIL=you@example.com
FRANKLINWH_PASSWORD=yourpassword
FRANKLINWH_LAT=37.7749
FRANKLINWH_LON=-122.4194
FRANKLINWH_GATEWAY=your-gateway-id   # optional, auto-detected if omitted
```

---

## Commands

```
python3 scrape.py setup              First-time setup wizard
python3 scrape.py start              Start the advisor (uses saved config)
python3 scrape.py install-service    Install macOS LaunchAgent
python3 scrape.py account advise     Run one advisory check (no loop)
python3 scrape.py account advise --watch   Run continuously
python3 scrape.py account stats      Live energy snapshot
python3 scrape.py account poll       Log readings to CSV
python3 scrape.py account history    Show hourly usage profile
python3 scrape.py account gateways   List gateways on your account
```

---

## Battery capacity

The backup duration estimate in alerts uses the usable capacity you set during setup. Common values:

| Model | Usable kWh |
|-------|-----------|
| aPower 10 | 10.0 |
| aPower 15 | 15.0 |
| 2× aPower 15 | 30.0 |

Update it any time with `python3 scrape.py setup`.

---

## How predictions improve over time

- **Day 1–2**: Alerts fire but predictions use rough estimates
- **Day 3+**: Usage-pattern-based load forecasting activates
- **3+ sunny days**: Solar forecast switches from historical averages to weather-adjusted estimates using your measured panel output (GHI × calibrated system peak kW)

---

## Privacy

- Credentials are stored only in `~/.franklinwh.json` (owner read-only, never logged)
- Weather data comes from [Open-Meteo](https://open-meteo.com) — free, no account, no tracking
- Energy data comes from `energy.franklinwh.com` (the same API used by the FranklinWH app)
- Nothing is sent anywhere except your configured Telegram bot or iMessage

---

## Files

```
scrape.py                 Entry point
franklinwh_scraper/       Python package
  account.py              FranklinWH API client
  advisor.py              Mode recommendation logic
  weather.py              Open-Meteo solar forecast
  history.py              SQLite usage history
  predictor.py            Load + solar forecasting
  notifier.py             Telegram / iMessage dispatchers
  cli.py                  All commands and alert logic
  config.py               Configuration persistence
output/
  history.db              Your usage history (improves predictions over time)
  advisor_log.jsonl       Every recommendation, with full context
  advisor.log             Raw cron output
~/.franklinwh.json        Your private config (never share this file)
```
