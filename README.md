# FranklinWH Energy Advisor

Get smart alerts about your FranklinWH home battery system — right on your phone.

No logging in, no app to babysit — just a message when something needs your
attention. A local dashboard is there when you do want the detail.

---

## What you'll get

### Alerts that fire automatically

| Alert | When it fires |
|-------|--------------|
| **Morning preview** | 7–8 am — today's solar forecast, peak solar window, rate countdown to on-peak, and whether you need to pre-charge before 4–9 pm |
| **2-day cloudy warning** | 7–9 am — when two consecutive low-solar days are forecast and battery is below 65%; advises charging now at cheap super-off-peak rate |
| **Low battery at 1 pm** | SoC < 40% heading into 4–9 pm peak; shows estimated time to empty at current load |
| **Low battery at noon** | SoC < 30% at 11am–noon despite solar; flags battery mode issue |
| **Tonight may run short** | 12:30–1:10 pm, only when needed — the forecast battery level at sundown is under what a heavy night for this weekday needs (learned from your 4 PM–midnight history; see `/evening`); tells you to top up before 2 PM while grid power is cheap, with the kWh, minutes, cost and the start-by time (counts anything you gave `/plan`). Silent otherwise, once a day |
| **Sundown projection updated** | After you ask `/sundown`: if the projection moves 15+ points from the last number sent, a corrected one goes out on its own (45 min apart, 3 a day at most, not in the last hour before sundown or while the battery is grid-charging). The 9 pm summary compares the actual battery level to the **first** projection of the day and lists each correction |
| **Bill running high** | Sunday 9 am, at most once a week — this cycle is on pace for 15%+ (and $10+) more than the whole last cycle, from the recorded readings. `/bill` now shows the same comparison |
| **Away mode** | Mornings 8–10 — the house has sat at standby load for 2+ days (under half your usual daily kWh, no hour above 1 kW). Holds the EB timer, evening, car and "what was that?" prompts until the load picks up, then says welcome back. Grid-outage and battery alerts stay on |
| **Is this a routine?** | 8 pm, at most one a day — when the load history shows a big load on 50%+ of a weekday (4+ times) that you haven't named, asks what it is, once, with one-tap buttons (dryer, oven, dishwasher, other). Optional: nothing depends on the answer, it only puts the name in `/habits` and `/sundown` |
| **Battery full — solar surplus** | 10 am–6 pm — battery at 100% and solar > load; reports the surplus (Self-Consumption already exports it to grid automatically). Fires once per fill, not once per day — stays quiet as long as the battery is still full |
| **Grid import during peak** | 4–9 pm — drawing from grid when battery should be covering load |
| **Not charging** | Solar > 1.5 kW but battery not charging; possible mode or inverter issue |
| **Solar dropped mid-day** | Solar kW dropped sharply during 11am–3pm; cloud cover or inverter alert |
| **Low morning solar** | 9–10 am and solar < 0.5 kW; flags cloudy day ahead |
| **EB target reached** | SoC hits ≥ 80% while actively charging at ~5.0 kW (the FranklinWH Emergency Backup AC charge rate) — confirms EB is genuinely charging from the grid, not just a coincidental SoC crossing from solar |
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
🌅 Without EV charging @ 7 am: ~68%     ← plain "Predicted SoC" if no EV configured
🔌 With EV charging (~7.6 kW): ~41%     ← only shown if you charge an EV at home
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

## Live dashboard

Alerts cover the moments that need you. When you want the whole picture —
live energy flow, forecast, bill projection, savings, battery health — run:

```bash
python3 scrape.py dashboard
```

Then open **http://127.0.0.1:8093**.

To have it start automatically on login alongside the advisor:

```bash
python3 scrape.py install-service --dashboard
```

It binds to `127.0.0.1` (this machine only) by default. The `/api/*` routes
expose live household load and billing data, so if you bind it to a LAN
address the command will refuse unless you first set `"dashboard_token"` in
`~/.franklinwh.json`.

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
| `/today` | The day so far: solar, house load, grid in/out and its cost, battery charge/discharge, and how much solar is still to come against the sunrise forecast |
| `/reserve` | What to charge the battery to overnight so the morning needs no grid: when it would reach the floor on its own, the 6 AM level, and the grid energy and cost to get there |
| `/evening` | This weekday's 4 PM–midnight usage (typical and heavy night, where it goes), the battery level to have at sundown, and whether today's forecast gets there; plus the typical/heavy level for every weekday |
| `/habits` | The big loads (dryer, oven, and so on) your load history says you run, by weekday: start time, kW, length, and how many of that weekday's days they showed up on (learned from the last 84 days, no labels needed). `/sundown` adds an "if it's your usual Sunday" line with the battery level when they all happen, and `/ebplan`, the 12:30 alert and `/sundown` count the ones you run on 60%+ of a weekday. It learns on its own: the last month counts double, so a changed routine shows within weeks, and it leads over `habit_loads` in the config (which is now only the fallback for a day with nothing learned) |
| `/plan dryer 4pm, oven 5pm for 2h` | Tell the advisor what you will run today (dryer, oven, kitchen, ac; `/plan clear` removes it). `/sundown` shows the outcome with it, `/evening` and the 12:30 alert add it to the level you need, `/ebplan` counts it |
| `/accuracy` | A scoreboard of how far `/sundown` (by time of day) and the whole-day solar forecast have actually been off. `/sundown` itself now shows a likely range from this record |
| `/ebplan [kWh] [load kW] [dryer\|oven\|ac [H] [Nh]] [from H] [to H\|midnight]` | How much Emergency Backup, and when to start, to stay off the grid from 2 PM (or `from H`) to 11 PM (or `to H`/`midnight`). Name the dryer/oven/AC (`/ebplan dryer 2pm 1h oven 5pm 2h to midnight`) or set usual loads per weekday in `habit_loads` (or leave it empty and let it learn them: see "Learning your appliances"); it also shows the answer if the sun comes in at only 60%, and says whether today is a cloudy day with a big drop in sun (the only days the morning plan and the start/stop timer bring up EB) |
| `/whatif kW [hours] [from H]` | What a planned load (dryer ~3 kW, AC) does to the battery at sunset, e.g. `/whatif 3 1.5 from 2pm` |
| `/alerts` | What fired in the last 30 days, which alerts you snooze, and which to consider turning off |
| `/mute` | Snooze non-safety alerts — tap 2h or 8h, or type `/mute 2` / `/mute 8` directly. Grid outage, fast drain, and area-outage alerts are never muted |
| `/unmute` | Cancel an active mute |
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

### Learning your appliances

When the advisor asks "What was that?" about a big steady draw, tapping Appliance (or "No, something else"
on the car question) now follows up with Dryer / Oven / Dishwasher / Something else. Once you have run an
appliance on at least three of a weekday's last eight occurrences (and on 40% or more of them), `/ebplan`
counts it on that weekday by itself, at the middle of your usual start time, length and power. Loads you
set in `habit_loads` for a weekday still win. Only spikes asked about after this change can be named.

### Emergency Backup timer and mode button

On a cloudy day whose forecast sun is at or under 65% of a good day (the median of your better days
in the last 30), the morning alert plans EB and the advisor sends two pings: "Start EB now" shortly
before the start-by time, and "Switch EB off" when the target is reached or at 2 PM. On every other
day neither mentions EB. The cushion the plan keeps (5 points) grows to 8 or 10 when plans you
followed still left the grid supplying the window. The 65% line moves too: after ten cloudy days with
no EB run, if the grid still had to supply the window on at least three of them, the line rises to cover
the milder of those days (between 50% and 85%).

To get a one-tap button on those pings, set `"mode_switch_enabled": true` in `~/.franklinwh.json`.
`mode_switch_dry_run` stays `true` until you have seen it work (the call comes from a community
library and is unconfirmed on this gateway): in dry run the button only says what it would do.
`mode_switch_home` (`tou`, `self` or `eb`, default `self`) is the mode "Back to normal" returns to.

