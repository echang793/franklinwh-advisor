# FranklinWH Energy Advisor

Get smart alerts about your FranklinWH home battery system — right on your phone.

No dashboard to check. No logging in. Just a message when something needs your attention.

---

## What you'll get

### Alerts that fire automatically

| Alert | When it fires |
|-------|--------------|
| **Morning preview** | 7–8 am — today's solar forecast, peak solar window, and whether you need to pre-charge before 4–9 pm |
| **2-day cloudy warning** | 7–9 am — when two consecutive low-solar days are forecast and your battery is below 65%, so you can charge at today's cheap rate |
| **Low battery at 1 pm** | If SoC < 40% heading into peak hours |
| **Solar surplus** | 10 am–2 pm — battery is full and solar is still producing; advises switching to Time-of-Use mode to export to grid |
| **Grid import during peak** | 4–9 pm — if you're drawing from the grid when the battery should be covering load |
| **Battery charged** | When SoC hits 100% |
| **Fast drain** | If SoC drops more than 8%/hr below 35% |
| **Grid outage** | Instant alert with estimated backup runtime |
| **Grid restored** | With outage duration and battery used |
| **Evening digest** | 9–10 pm — solar generated, grid cost, peak coverage, predicted overnight SoC |
| **Weekly summary** | Sunday evening — TOU import/export cost, savings from battery + solar, solar forecast accuracy |
| **Monthly billing cycle** | 19th of each month — cycle totals vs prior cycle, NEM true-up estimate |
| **Bill projection** | 5th of each month — partial-cycle data extrapolated to a full 30-day estimate |
| **Solar degradation** | If 7-day performance ratio drops >5% vs 30-day baseline — possible soiling or inverter issue |
| **Solar recovery** | After a degradation alert, confirms when output returns to normal |
| **Battery capacity fade** | If effective usable capacity trends down vs an earlier baseline |
| **Peak coverage streak** | 3 consecutive days with <50% battery coverage during 4–9 pm |
| **Heat wave prep** | Evening before a forecast >95°F day — AC load spike warning |
| **Storm prep** | Evening when an NWS storm/wind alert is active and battery < 90% |
| **Area power outage** | Cross-signal from CMR News bot (if also installed) |

### Solar prediction accuracy

The solar forecast improves automatically over time:
- **Days 1–2** — rough estimates only
- **Day 3+** — usage-pattern forecasting activates using your actual load history
- **After a week** — solar output calibrated to your roof using a P75 system-peak estimate
- **Ongoing** — per-hour bias correction map learns systematic patterns (morning shade, afternoon clipping) from every poll and applies them to future predictions

### Bill estimates

Uses SDG&E EV-TOU-5 rates (effective Jan 2026). NEM 3.0 export credits are modeled separately from import rates — Aug/Sep boosted evening export hours are included. Rates include a staleness warning if they're more than 180 days old.

---

## Before you start

You'll need:

1. **A FranklinWH account** — the same email and password you use in the FranklinWH app
2. **Python 3.9 or newer** — already installed on most Macs; [download here](https://python.org/downloads) if needed
3. **One of these for alerts:**
   - **Telegram** (recommended) — free app for iPhone or Android. Takes about 2 minutes to set up.
   - **Email** — any Gmail, Outlook, or other email account

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

**Webhook** *(for Slack, Discord, or custom tools)*

Paste the webhook URL and alerts will be posted as JSON.

### Step 4 — Which alerts you want
The wizard shows four groups. Press **Y** to enable (default) or **N** to skip. Safety alerts (grid outage, fast drain) are always on.

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
Installs a LaunchAgent — runs in the background whenever your Mac is on.

**Linux / Raspberry Pi:**
```bash
(crontab -l; echo "*/15 7-23 * * * cd $(pwd) && python3 scrape.py account advise >> output/advisor.log 2>&1") | crontab -
```

---

## Updating

```bash
git pull
```

Settings in `~/.franklinwh.json` are kept — nothing to re-configure.

---

## Optional: AI chat assistant

If you set up Telegram, you can enable an AI assistant that answers questions in plain English:

> "How much did I save this week?"  
> "Should I charge my car now or wait tonight?"  
> "Why is my battery already at 30%?"

Enable it by running `python3 scrape.py setup` again and picking a chatbot backend:

- **Anthropic Claude** — most accurate. Free API key at [console.anthropic.com](https://console.anthropic.com).
- **Ollama** — runs on your own computer. Free and private. Requires [Ollama](https://ollama.com).

Built-in commands (no AI needed):

| Command | What you get |
|---------|-------------|
| `/status` | Live battery, solar, and grid snapshot |
| `/forecast` | Today's and tomorrow's solar outlook |
| `/history` | 7-day energy and cost summary |
| `/modes` | Explanation of Self-Consumption vs Emergency Backup mode |

---

## Frequently asked questions

**Do I need to leave my computer on?**  
Yes — a Mac mini, Raspberry Pi, or any always-on machine works well.

**Is my password safe?**  
Credentials are saved to `~/.franklinwh.json` on your own computer. Nothing is sent to any third party.

**What utility rates does it use?**  
SDG&E EV-TOU-5 (effective Jan 2026). To use different rates, edit `franklinwh_scraper/tou.py` — the file is short and commented. Export credits use NEM 3.0 avoided-cost rates by default.

**The predictions aren't accurate on day one — is that normal?**  
Yes. The advisor improves as it collects data:
- Days 1–2: rough estimates
- Day 3+: load-pattern forecasting activates
- After a week: solar calibrated to your roof
- Ongoing: per-hour solar bias corrections accumulate automatically

**Something isn't working?**  
Run `python3 scrape.py doctor` — it checks every component and tells you what to fix.

---

## Privacy

- Credentials live only in `~/.franklinwh.json` on your computer — never shared
- Energy data fetched from FranklinWH's own servers (same as the mobile app)
- Weather from [Open-Meteo](https://open-meteo.com) — free and anonymous
- Alerts go to your own Telegram bot or email — no central server
