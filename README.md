# FranklinWH Energy Advisor

Get smart alerts about your FranklinWH home battery system — right on your phone.

No dashboard to check. No logging in. Just a message when something needs your attention.

---

## What you'll get

### Alerts that fire automatically

| Alert | When it fires |
|-------|--------------|
| **Morning preview** | 7–8 am — today's solar forecast, peak solar window, rate countdown to on-peak, and whether you need to pre-charge before 4–9 pm |
| **2-day cloudy warning** | 7–9 am — when two consecutive low-solar days are forecast and battery is below 65%; advises charging now at cheap super-off-peak rate |
| **Low battery at 1 pm** | SoC < 40% heading into 4–9 pm peak; shows estimated time to empty at current load |
| **Low battery at noon** | SoC < 30% at 11am–noon despite solar; flags battery mode issue |
| **Solar surplus** | 10 am–2 pm — battery full (≥ 93%) and solar > load; advises switching to Time-of-Use to export surplus |
| **Grid import during peak** | 4–9 pm — drawing from grid when battery should be covering load |
| **Not charging** | Solar > 1.5 kW but battery not charging; possible mode or inverter issue |
| **Solar dropped mid-day** | Solar kW dropped sharply during 11am–3pm; cloud cover or inverter alert |
| **Low morning solar** | 9–10 am and solar < 0.5 kW; flags cloudy day ahead |
| **Battery full** | SoC reaches 100% |
| **EB target reached** | SoC hits ≥ 80% during 1–2 pm window — battery ready for peak |
| **Fast drain** | SoC dropping >8%/hr below 35%; shows estimated time to empty |
| **Grid outage** | Immediate alert with estimated backup time, generator status (if running), conservation advice |
| **Grid restored** | Outage duration and kWh used from battery |
| **Evening digest** | 9–10 pm — solar generated, grid import/export, battery charge/discharge, self-sufficiency, peak coverage, estimated grid cost, predicted SoC at 7 am |
| **Weekly summary** | Sunday 9–10 pm — TOU import/export cost, estimated battery + solar savings, solar forecast accuracy ±%, battery cycle count |
| **Monthly billing cycle** | 19th of each month — cycle totals vs prior cycle, NEM true-up estimate, annualized run rate |
| **Bill projection** | 5th of each month — partial billing cycle extrapolated to 30-day estimate |
| **Solar degradation** | 7-day performance ratio drops >5% vs 30-day baseline — possible panel soiling or inverter loss |
| **Solar back to normal** | Confirms when output recovers after a degradation alert |
| **Battery capacity fade** | Effective usable kWh trending down vs earlier baseline — possible cell degradation |
| **Peak coverage streak** | 3 consecutive days with < 50% battery coverage during 4–9 pm |
| **Heat wave prep** | Evening before a forecast > 95°F day — AC load spike warning, charge advice |
| **Storm prep** | Evening when an NWS storm/wind alert is active and battery < 90% |
| **EV charge window** | Evening advisory on the cheapest overnight charging window (configurable) |
| **Export arbitrage** | Aug/Sep only — when battery is full and highest evening export rate is approaching |
| **Area power outage** | Cross-signal from CMR News bot (if installed) — confirmed outage nearby |

Safety alerts (grid outage, fast drain, area outage) are always on and cannot be muted.

---

### Evening digest detail

The EOD digest is the most data-rich alert. It includes:

```
Solar:    28.5 kWh      ← today's production
Grid in:   8.2 kWh      ← pulled from grid
Grid out:  4.1 kWh      ← exported to grid
Batt chg:  9.3 kWh      ← charged into battery today
Batt dis:  7.8 kWh      ← drawn from battery today
Home:     32.4 kWh      ← total home consumption
─────────────────────
🟢 ████████░░ 81%
⏱ Backup: ~4.2 hr at current load
🌅 Predicted SoC @ 7 am: ~68%
🎯 Solar forecast vs actual:
  Predicted: 26.0 kWh
  Actual:    28.5 kWh
  Delta:     +2.5 kWh (+10%)
💰 Grid cost: $1.23 in · $0.45 out · +$0.53 base → net $1.31
```

---

### Solar prediction accuracy

The solar forecast improves automatically as it collects readings:

- **Days 1–2** — rough estimates only
- **Day 3+** — load-pattern forecasting activates from your actual usage history
- **After a week** — solar output calibrated to your roof using a P75 system-peak estimate
- **Ongoing** — per-hour bias correction map learns systematic patterns (morning shade, afternoon clipping, inverter behavior) from every poll

The temperature model accounts for both cooling (AC draw above 27°C / 80°F) and heating (heat pump / resistive below 18°C / 64°F).

---

### Bill estimates

Uses SDG&E EV-TOU-5 rates (effective Jan 2026). Key details:

- Import and export rates are modeled separately — NEM 3.0 avoided-cost rates apply to most grid exports (~$0.05/kWh); Aug/Sep boosted export hours use actual schedule rates
- SDG&E holidays treated as Sunday schedule (affects 6–9 am billing)
- Weekly summary warns when rate table is >180 days old (SDG&E revises ~twice/year)
- To use a different rate plan, edit `franklinwh_scraper/tou.py`

---

## Before you start

You'll need:

1. **A FranklinWH account** — the same email and password you use in the FranklinWH app
2. **Python 3.9 or newer** — already installed on most Macs; [download here](https://python.org/downloads) if needed
3. **One of these for alerts:**
   - **Telegram** (recommended) — free app for iPhone or Android. Takes about 2 minutes to set up.
   - **Email** — any Gmail, Outlook, or other email account
   - **Webhook** — POST JSON to Slack, Discord, or any custom URL

---

## Install

```bash
git clone https://github.com/echang793/franklinwh-advisor
cd franklinwh-advisor
pip3 install -e .
```

---

## Set up

```bash
python3 scrape.py setup
```

The wizard walks you through five steps — just answer the prompts. Most have a default you can accept by pressing Enter.

### Step 1 — Your FranklinWH account
Enter the email and password from your FranklinWH app. The wizard checks they work before continuing.

### Step 2 — Your location
Type your city name (e.g. *San Diego*). Used to fetch solar and weather forecasts — nothing else.

### Step 3 — How you want to receive alerts

**Telegram (recommended)**

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts
3. BotFather will give you a token like `123456789:ABCdef...` — paste it into the wizard
4. Send any message to your new bot — the wizard detects your chat ID automatically

**Email**

Enter your email address and SMTP details. For Gmail, you'll need an [App Password](https://myaccount.google.com) (Security → 2-Step Verification → App Passwords).

**Webhook**

Paste the webhook URL and alerts will be posted as JSON.

### Step 4 — Which alerts you want
The wizard shows four groups. Press **Y** to enable (default) or **N** to skip. You can always re-run setup to change this.

### Step 5 — Your battery model
Pick your FranklinWH battery (aPower 10, aPower 15, or stacked). This lets the advisor estimate backup runtime correctly.

---

## Start monitoring

Check everything is configured:

```bash
python3 scrape.py doctor
```

All green? Start the advisor:

```bash
python3 scrape.py start
```

### Run automatically

**Mac:**
```bash
python3 scrape.py install-service
```
Installs a LaunchAgent — runs in the background whenever your Mac is on, restarts automatically.

**Linux / Raspberry Pi:**
```bash
(crontab -l; echo "*/15 7-23 * * * cd $(pwd) && python3 scrape.py account advise >> output/advisor.log 2>&1") | crontab -
```

---

## Updating

```bash
git pull
```

Then restart the service:
```bash
launchctl stop com.franklinwh.advisor && launchctl start com.franklinwh.advisor
```

Settings in `~/.franklinwh.json` are kept — nothing to re-configure.

---

## AI chat assistant

If you set up Telegram, you can enable an AI assistant that answers questions in plain English — and several commands work without AI at all.

> "How much did I save this week?"  
> "Should I charge my car now or wait until tonight?"  
> "Why is my battery already at 30%?"

Enable it by running `python3 scrape.py setup` again and picking a chatbot backend:

- **Anthropic Claude** — most accurate. Free API key at [console.anthropic.com](https://console.anthropic.com).
- **Ollama** — runs on your own computer. Free and private. Requires [Ollama](https://ollama.com).

### Commands (no AI needed)

| Command | What you get |
|---------|-------------|
| `/status` | Live battery, solar, grid snapshot — includes charge rate and estimated time to empty/full |
| `/forecast` | Today's and tomorrow's solar outlook with irradiance bar chart |
| `/history` | 7-day energy and TOU cost summary |
| `/bill` | Current billing cycle cost and projected full-cycle total |
| `/tip` | Best action right now based on current SoC, rate, and solar |
| `/modes` | Explanation of Self-Consumption, Emergency Backup, and Time-of-Use modes |
| `/until N` | Estimated time to reach N% SoC at the current charge or discharge rate — e.g. `/until 20`, `/until 80` |
| `/clear` | Reset conversation history |

The `/until N` command also understands natural language: `"time to 80%"`, `"until 50%"`, `"reach 30%"`.

---

## Frequently asked questions

**Do I need to leave my computer on?**  
Yes — a Mac mini, Raspberry Pi, or any always-on machine works well.

**Is my password safe?**  
Credentials are saved to `~/.franklinwh.json` on your own computer. Nothing is sent to any third party.

**What utility rates does it use?**  
SDG&E EV-TOU-5 (effective Jan 2026). To use different rates, edit `franklinwh_scraper/tou.py` — it's short and well-commented. Export credits use NEM 3.0 avoided-cost rates by default.

**The predictions aren't accurate on day one — is that normal?**  
Yes. The advisor improves as it collects data:
- Days 1–2: rough estimates
- Day 3+: load-pattern forecasting activates
- After a week: solar calibrated to your roof
- Ongoing: per-hour solar bias corrections accumulate automatically

**Something isn't working?**  
Run `python3 scrape.py doctor` — it checks every component and tells you exactly what to fix.

---

## Privacy

- Credentials live only in `~/.franklinwh.json` on your computer — never shared
- Energy data fetched from FranklinWH's own servers (same as the mobile app)
- Weather from [Open-Meteo](https://open-meteo.com) — free and anonymous
- Alerts go to your own Telegram bot or email — no central server
