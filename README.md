# FranklinWH Energy Advisor

Monitors your [FranklinWH](https://franklinwh.com) home battery system and sends smart alerts via **Telegram** (any device) or **iMessage** (macOS) when it recommends changing your battery mode.

Works on macOS and Linux (including Oracle Cloud free tier, Raspberry Pi, etc.).

---

## What it does

Every 15 minutes (7 am – 11 pm daily) it:

1. Reads live data from your FranklinWH account — battery SoC, solar, grid, load
2. Fetches a 48-hour solar irradiance forecast from [Open-Meteo](https://open-meteo.com) (free, no key)
3. Analyzes your historical usage patterns to predict demand over the next 24 hours
4. Recommends a battery mode using TOU-aware scheduling and sends an alert if action is needed

### Alerts you get

**TOU-aware mode recommendations** (SDG&E EV-TOU-5 — adapt rates in `tou.py` for your utility):
- Projects battery SoC at 4 pm on-peak start using usage + solar forecast
- Recommends Emergency Backup with exact charge duration if battery will fall short
- Switches to Self-Consumption when solar surplus predicted

**Scheduled daily alerts:**
- 8:00 am — Morning preview: SoC + predicted solar generation + yesterday's accuracy
- 9:30 am — Low solar warning if cloudy morning detected
- 11 am–3 pm — Solar dropped mid-day (possible inverter issue)
- 11:30 am–12:30 pm — Battery still low at noon despite available solar
- 1–2 pm — Battery under 40% before peak hours
- 1:40–2:10 pm — Battery at 80%+ before 4 pm peak (Emergency Backup target reached)
- 4–9 pm — Grid import during peak hours
- 9–10 pm — End-of-day digest: daily totals, TOU grid cost, peak coverage %, predicted 6 am SoC

**Weekly & monthly:**
- Sunday 9–10 pm — Weekly TOU cost summary: import cost, export credit, peak savings
- 5th of month — Projected full billing cycle cost based on actual data so far
- 19th of month — Billing cycle summary (20th–19th) vs prior cycle

**Long-term health:**
- Solar degradation alert — fires if 7-day performance ratio drops >5% vs 30-day baseline
- Peak coverage streak — fires if battery runs short (< 50%) at peak 3 days in a row

**Immediate alerts:**
- Grid down — with estimated battery backup hours at current load
- Grid restored — outage duration and kWh used from battery
- Battery fully charged (SoC ≥ 99%)
- Battery dropping below 90% during 3–7 pm
- Battery draining fast (> 8%/hr below 35% SoC)
- Battery not charging despite strong solar (possible inverter/mode issue)
- Area power outage bridged from [CMR News](https://github.com/echang793/cmr-news) — fires when that bot detects a nearby outage

### AI chatbot (optional)

Message your Telegram bot natural-language questions about your system:

> "Should I charge the car now or wait?"
> "Why is my battery at 30% already?"
> "How much am I spending on electricity this week?"

Powered by **Claude Haiku** (cloud, ~$0 at home usage) or **Ollama** (fully local, private).
Set up during `python3 scrape.py setup`.

Built-in commands (no AI token used):

| Command | Response |
|---------|----------|
| `/status` | Live snapshot: SoC, solar, grid, TOU period |
| `/forecast` | Today + tomorrow GHI-based sky condition and solar outlook |
| `/history` | 7-day TOU-weighted solar generated, grid import/export cost |
| `/clear` | Reset conversation memory |

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
- AI chatbot backend: `anthropic` (get a free key at [console.anthropic.com](https://console.anthropic.com)), `ollama` (local — run `ollama pull llama3.1:8b` first), or `none`

> **Privacy:** every user runs their own copy with their own credentials. Nothing is shared between users.

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

## Updating

Pull the latest changes from GitHub and restart the service:

```bash
bash update.sh
```

That's it. The script fetches the latest code and reloads the LaunchAgent (macOS) or restarts the cron job (Linux).

### Sharing with neighbors

Each person installs their own copy by cloning this repo and running `bash install.sh`. They use their own FranklinWH credentials — nothing is shared between installs.

When improvements are pushed to this repo, neighbors update by running `bash update.sh` from their local copy. No server or account required.

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
- **3+ sunny days**: Solar generation estimates improve as the system self-calibrates — on sunny days it records your measured panel output vs. irradiance and builds a calibrated system peak kW (median of all readings). Morning previews and 12h solar forecasts use GHI forecast × calibrated peak kW
- **3+ complete days tracked**: An empirical **performance ratio** (PR) activates. Each morning the system compares yesterday's actual solar kWh (from your readings) against what it predicted the morning before, and stores the ratio. After 3 days the rolling median of those ratios is applied to all future forecasts — automatically correcting for inverter losses, temperature derating, panel tilt, and any site-specific shading. The morning message shows `PR=0.82` (or whatever your system settles to). A typical residential system lands between 0.75–0.87.

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
  advisor.py              TOU-aware mode recommendation logic
  tou.py                  SDG&E EV-TOU-5 schedule and rates
  weather.py              Open-Meteo solar forecast
  history.py              SQLite usage history
  predictor.py            Seasonal load + solar forecasting
  notifier.py             Telegram / iMessage dispatchers
  chatbot.py              AI chatbot (Claude / Ollama)
  cli.py                  All commands and alert logic
  config.py               Configuration persistence
output/
  history.db              Your usage history (improves predictions over time)
  advisor_log.jsonl       Every recommendation, with full context
  advisor.log             Raw cron output
~/.franklinwh.json        Your private config (never share this file)
```

### Adapting TOU rates for your utility

Edit `franklinwh_scraper/tou.py` to match your electricity plan's on-peak hours and rates. The file is short and clearly commented.

---

## Appendix: Terms and units

**SoC (State of Charge)** — Battery charge level as a percentage of usable capacity. 100% = fully charged, 0% = empty. The advisor targets having enough SoC to cover on-peak hours without importing from the grid.

**kW (kilowatt)** — Rate of power flow right now. Positive grid_use_kw means you're importing; negative means you're exporting (selling back). Solar and home load are always shown as positive values.

**kWh (kilowatt-hour)** — Energy consumed or produced over time. 1 kW flowing for 1 hour = 1 kWh. A 13.6 kWh battery at 50% SoC holds 6.8 kWh of usable energy.

**GHI / Irradiance (W/m²)** — Global Horizontal Irradiance: total solar energy hitting a flat surface per square meter per second. Clear midday sun ≈ 900–1000 W/m². Overcast ≈ 50–200 W/m². The advisor uses hourly GHI forecasts from Open-Meteo to predict how much your panels will generate.

**PR (Performance Ratio)** — Actual solar generation ÷ theoretical maximum based on irradiance. A PR of 1.0 means perfectly efficient; real systems land at 0.75–0.90 due to panel temperature, inverter losses, wiring, and shading. The advisor self-calibrates your PR over time and applies it to forecasts. Morning previews show your current PR (e.g. `PR=0.82`).

**TOU (Time-of-Use)** — Electricity pricing that varies by time of day. On-peak hours (typically evenings) cost 3–7× more than off-peak. The advisor is pre-configured for SDG&E EV-TOU-5 but the rates file is easy to adapt.

**Super Off-Peak** — The cheapest electricity period. On SDG&E EV-TOU-5 this is 10 am–2 pm on weekdays — the ideal window to charge your battery from the grid using Emergency Backup mode before the 4–9 pm on-peak window.

**Emergency Backup (EB) mode** — FranklinWH battery mode that charges the battery from grid power. Useful before on-peak hours when solar won't fully charge the battery in time. The advisor calculates exactly how long to run it.

**Self-Consumption mode** — Default FranklinWH mode: use solar first, then battery, then grid. Best when solar is plentiful.

**Net energy (kWh net)** — Solar generated minus home load. Positive = surplus (can export or store). Negative = deficit (must draw from battery or grid).

**Peak coverage %** — Percentage of 15-minute intervals between 4–9 pm where you drew zero grid power (battery/solar covered all load). 100% = perfect peak avoidance. Shown in the end-of-day digest.
