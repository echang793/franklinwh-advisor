# FranklinWH Energy Advisor

Get smart alerts about your FranklinWH home battery system — right on your phone.

No dashboard to check. No logging in. Just a message when something needs your attention.

---

## What you'll get

- **Morning preview** — today's solar forecast and whether you need to pre-charge before the expensive evening hours
- **Peak-hour alerts** — a heads-up if your battery is going to run low before 4–9 pm
- **Evening digest** — how much solar you generated, what grid power cost you, and how well your battery covered peak hours
- **Weekly & monthly reports** — your energy bill trend and how much you saved
- **Instant alerts** — battery full, grid outage, battery draining unusually fast

Alerts arrive via **Telegram** (free app, any phone), **email**, or both.

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

Open Terminal (on Mac: press ⌘ Space, type *Terminal*, press Enter) and run:

```bash
pip install franklinwh-advisor
```

> **Windows users:** if that doesn't work, try `pip3 install franklinwh-advisor`

---

## Set up

Run the setup wizard:

```bash
franklinwh setup
```

It will walk you through five steps — just answer the prompts. Most have a default you can accept by pressing Enter.

### Step 1 — Your FranklinWH account
Enter the email and password from your FranklinWH app. The wizard will check they work before continuing.

### Step 2 — Your location
Type your city name (e.g. *San Diego*). This is used to fetch local weather forecasts for solar predictions — nothing else.

### Step 3 — How you want to receive alerts

**Telegram (recommended)**

1. Open Telegram and search for **@BotFather**
2. Send it the message `/newbot`
3. Follow the prompts — pick any name and username for your bot
4. BotFather will give you a **token** that looks like `123456789:ABCdef...` — paste it into the wizard
5. Send *any* message to your new bot — the wizard will detect your chat ID automatically

**Email**

Enter your email address and SMTP details. For Gmail:
- Host: `smtp.gmail.com`, Port: `587`
- You'll need an **App Password** (not your regular Gmail password):
  1. Go to [myaccount.google.com](https://myaccount.google.com) → Security → 2-Step Verification → App Passwords
  2. Create one for "Mail" and paste it into the wizard

**Webhook** *(advanced — for Slack, Discord, or custom tools)*

Paste the webhook URL and alerts will be posted as JSON.

### Step 4 — Which alerts you want
The wizard shows four groups of alerts. Press **Y** to enable a group (default) or **N** to skip it. You can always re-run `franklinwh setup` to change this.

Safety alerts (grid outage, fast battery drain) are always on — they can't be turned off.

### Step 5 — Your battery model
Pick your FranklinWH battery model from the list (aPower 10, aPower 15, or stacked). This helps the advisor estimate your backup runtime correctly.

---

## Start monitoring

After setup, run a quick health check:

```bash
franklinwh doctor
```

You should see all green checkmarks. If anything is red, it will tell you what to fix.

Then start the advisor:

```bash
franklinwh start
```

You'll receive a test alert confirming it's working.

### Run automatically (so you don't have to start it manually)

**Mac:**
```bash
franklinwh install-service
```
This sets up automatic startup — the advisor will run in the background whenever your Mac is on, and restart itself if it ever stops.

**Linux / Raspberry Pi:**
```bash
(crontab -l; echo '*/5 7-23 * * * franklinwh account advise >> ~/franklinwh.log 2>&1') | crontab -
```
This runs the advisor every 5 minutes between 7 am and 11 pm.

---

## Updating

```bash
pip install --upgrade franklinwh-advisor
```

Your settings are kept — nothing to re-configure.

---

## Optional: AI chat assistant

If you set up Telegram, you can enable an AI assistant that answers questions about your system in plain English:

> "How much did I save this week?"
> "Should I charge my car now or wait until tonight?"
> "Why is my battery already at 30%?"

To enable it, run `franklinwh setup` again and choose a chatbot backend:

- **Anthropic Claude** — most accurate. Get a free API key at [console.anthropic.com](https://console.anthropic.com) (free credits available).
- **Ollama** — runs entirely on your own computer. Free and private. Requires [Ollama](https://ollama.com) to be installed.

Built-in commands (no AI needed):

| Message your bot | What you get |
|---|---|
| `/status` | Live battery, solar, and grid snapshot |
| `/forecast` | Today's and tomorrow's solar outlook |
| `/history` | 7-day energy and cost summary |

---

## Frequently asked questions

**Do I need to leave my computer on?**
Yes, the advisor needs to run on a computer that's awake to check your system. A Mac mini, Raspberry Pi, or any always-on Linux machine works great.

**Is my password safe?**
Your credentials are saved to `~/.franklinwh.json` on your own computer — readable only by you. Nothing is sent to any third party.

**What utility rates does it use?**
The app ships with SDG&E EV-TOU-5 rates. If you're on a different plan, you can edit `franklinwh_scraper/tou.py` to match your rates — the file is short and commented.

**The predictions aren't accurate on day one — is that normal?**
Yes. The advisor improves as it learns your home's usage patterns:
- Days 1–2: uses rough estimates
- Day 3+: usage-pattern forecasting activates
- After a week: solar predictions are calibrated to your specific roof and panels

**Something isn't working — where do I start?**
Run `franklinwh doctor` — it checks every component and tells you exactly what's wrong.

---

## Privacy

- Your credentials live only on your computer (`~/.franklinwh.json`) — never shared
- Energy data is fetched from FranklinWH's own servers (same as the mobile app)
- Weather data from [Open-Meteo](https://open-meteo.com) — free and anonymous
- Alerts go only to your own Telegram bot or email — no central server involved
