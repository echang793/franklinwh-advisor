# Monitoring Stack — RUNBOOK

## Systems

| System | Location | Log | Runs as |
|--------|----------|-----|---------|
| FranklinWH Advisor (alerts + Telegram bot) | `~/Projects/franklinwh` | `~/Library/Logs/franklinwh-advisor.log` | LaunchAgent `com.franklinwh.advisor` |
| FranklinWH Dashboard (web UI, :8093) | `~/Projects/franklinwh` | `~/Library/Logs/franklinwh-dashboard.log` | LaunchAgent `com.franklinwh.dashboard` |
| CMR News Bot | `~/Projects/cmr-news` | `bot.log` in the repo | LaunchAgent `com.cmrnews.bot` |

**One host only.** The advisor and both bots must run on exactly one machine (currently the Mac mini, `run_on_host: Mac-mini` in `~/.franklinwh.json`). Two copies double every alert and fight over the Telegram token. Keep repos under `~/Projects`, **not** `~/Desktop` / `~/Documents` (iCloud sync locks files — see incidents below). `franklinwh doctor` checks the host guard, the iCloud path and both LaunchAgents. Logs rotate at 5 MB (3 copies kept) from inside the advisor loop.

## Adding or moving to another machine

1. Clone to `~/Projects/<repo>` (not Desktop/Documents), then `pip install -e .` — or run `./install.sh`.
2. Copy `~/.franklinwh.json` (and `~/.franklinwh_tesla.json`, `~/.franklinwh.license` if used) **by hand** (AirDrop/scp). Never commit them; they hold passwords and tokens.
3. **Stop and disable the agents on the machine that is giving up hosting first**, then confirm nothing is left:
   `for l in com.franklinwh.advisor com.franklinwh.dashboard com.cmrnews.bot; do launchctl bootout gui/$(id -u)/$l 2>/dev/null; launchctl disable gui/$(id -u)/$l; done; launchctl list | grep -Ei "franklinwh|cmrnews"`
4. On the new host set `run_on_host` to its short hostname, run `franklinwh install-service`, then `xattr -c ~/Library/LaunchAgents/com.franklinwh.*.plist`.
5. Check: `franklinwh doctor` is all green and one "advisor started on <host>" Telegram message arrives. A second one from another hostname means two hosts are running.

---

## Incidents

### 2026-05-05 — open-meteo.com intermittent 502 (FranklinWH)

**Symptom**: `WARNING franklinwh_scraper.cli Weather forecast fetch failed: 502 Server Error: Bad Gateway` in `output/advisor.log`. ~20+ occurrences per 24h.

**Root cause**: `fetch_solar_outlook()` in `weather.py` called `raise_for_status()` with no retry. open-meteo CDN has periodic 502 blips (transient, <30s). On failure the CLI cache layer served stale data, but the WARNING noise was high.

**Fix**: Added exponential backoff retry to `fetch_solar_outlook()` — 3 attempts, 5/10/20s delays (`weather.py`). Advisor continues operating on stale cache if all retries fail (graceful degradation unchanged).

**Commit**: `fix: retry open-meteo 502s with exponential backoff`

---

### 2026-05-05 — cmr-news bot.py Permission Denied (CMR News)

**Symptom**: `bot.log` filled with `can't open file '/Users/erichang/Desktop/cmr-news/bot.py': [Errno 1] Operation not permitted`. Bot never ran.

**Root cause**: LaunchAgent plist `com.cmrnews.bot` used `/Library/Developer/CommandLineTools/usr/bin/python3`, which lacks macOS TCC Full Disk Access for `~/Desktop`. The cron-based franklinwh advisor uses `/opt/homebrew/bin/python3.13` (has Desktop access as proven by working cron job).

**Fix**: Updated `~/Library/LaunchAgents/com.cmrnews.bot.plist` — changed `ProgramArguments[0]` to `/opt/homebrew/bin/python3.13`. Reloaded with `launchctl unload/load`. Process PID confirmed running.

**Note**: The plist lives in `~/Library/LaunchAgents/` (not in the git repo). If re-deploying, update the plist python path manually.

---

### 2026-05-05 — Duplicate Alerts (FranklinWH)

**Symptom**: User received duplicate Emergency Backup and End-of-Day Summary alerts on the same day.

**Root cause**: Two advisor processes running concurrently:
1. Orphaned `--watch` process (PID 4904, Python 3.9/Xcode CLT) running since Wed 8am — fires every 15 min internally
2. Cron job `*/15 8-23 * * *` also firing every 15 min

The PID lock in `_acquire_pid_lock()` only gates `--watch` startup, not single-shot cron invocations. Both processes read `.peak_alert_state.json` before either writes → race condition → both see alert not yet sent → both send.

**Fix**: Killed orphaned PID 4904. Cron alone handles polling. If `--watch` mode is needed in future, disable the cron first to avoid the conflict.

**Detection**: `ps aux | grep scrape.py` — should show zero or one process. Paired entries seconds apart in `output/advisor_log.jsonl` indicate two concurrent processes.

---

### 2026-09-09 → 09-17 — iCloud sync crashed the advisor; launchd wedged (FranklinWH)

**Symptom**: no evening digest; `.health.json` `last_success` ~17 h old; `launchctl list` showed PID `-`.

**Root cause**: iCloud Desktop & Documents sync (`bird`) briefly locked `output/.last_rollup` mid-write → `OSError: [Errno 11] Resource deadlock avoided`, uncaught, crash-looping every cycle (the write runs before the alert engine). Earlier (09-09) moving the folder under iCloud also broke path resolution for fresh processes. Separately launchd stuck at `state = spawn scheduled` with no PID: `bootout`/`bootstrap`/`kickstart -k` all returned 0 but never spawned, while running the same command by hand worked — the wedge was launchd's own per-job state.

**Fix**: moved both repos to `~/Projects` (out of iCloud); the marker write is now non-fatal (`_write_rollup_marker`); **removed the plist file and recreated it** (editing it did not clear the wedge), added the missing `RunAtLoad` key, `xattr -c`, fresh `bootstrap`. Transient `Bootstrap failed: 5: Input/output error` on restart: just retry once.

**Detection**: `franklinwh doctor` now flags `spawn scheduled` with no PID and any iCloud-synced install path.

---

### 2026-09-24 — Duplicate alerts from two Macs (FranklinWH, CMR News)

**Symptom**: the daily summary arrived twice within 5 minutes.

**Root cause**: after the new Mac mini, the MacBook Air still had all three LaunchAgents loaded. Each copy keeps its own state, so both sent every alert, and both bots polled one Telegram token. (Same failure shape as the 2026-05-05 incident below.)

**Fix**: agents booted out and `launchctl disable`d on the Air; `run_on_host` guard added so a second host stands down; the advisor sends "advisor started on <host>" at startup so a second host is visible at once.

---

## Diagnostics

```bash
# FranklinWH — overall health (host guard, iCloud path, LaunchAgents, uptime monitor)
franklinwh doctor
cat ~/Projects/franklinwh/output/.health.json          # last_success should be < ~10 min old
launchctl print gui/$(id -u)/com.franklinwh.advisor | grep -E "state|pid"

# FranklinWH — recent errors
grep -i "error\|warn\|fail" ~/Library/Logs/franklinwh-advisor.log | tail -30

# CMR News — service status
launchctl list com.cmrnews.bot
tail -20 ~/Projects/cmr-news/bot.log

# Reload CMR News bot after plist changes
launchctl unload ~/Library/LaunchAgents/com.cmrnews.bot.plist
launchctl load  ~/Library/LaunchAgents/com.cmrnews.bot.plist

# Test open-meteo reachability
curl -s "https://api.open-meteo.com/v1/forecast?latitude=32.97&longitude=-117.07&hourly=cloud_cover&forecast_days=1" | python3.13 -c "import sys,json; d=json.load(sys.stdin); print('ok', len(d['hourly']['time']), 'hours')"
```

## Recording your bill (keeps every estimate accurate)

When each SDG&E/SDCP bill arrives, copy its text (the "Electric Service – Solar Billing Plan" page **and** the CCA generation page) and run:

```bash
franklinwh bill-record --from-text -      # paste the text, then press Ctrl-D
franklinwh bill-record --from-text bill.txt --dry-run   # preview only
```

It reads the billing period, next meter-read date, SDCP generation rates, export credits, fixed charge and the SDG&E delivery charge, then recalibrates: the actual-bill comparison (delivery + net generation, **excluding** the Climate Credit), the effective export $/kWh, the summer/winter generation rates, the fixed charge, the delivery residual (PCIA etc.) and the **real billing-cycle dates** — so cycles follow the meter read instead of a fixed day of the month. Estimates lag by at most one bill. If it says "Couldn't find …", paste the missing page; the old `--amount` form still works.

## Export credit pricing (hourly schedule)

Export credits are priced from SDG&E's published hourly Solar Billing Plan schedule (`franklinwh_scraper/data/export_prices_legacy2024.json`, the **Legacy 2024 / NBT24** vintage): delivery ~$0.004/kWh at midday but ~$0.27 in the evening, generation above $2/kWh at 6–8 PM in September. That is why one flat $/kWh was off 4× between the Aug and Sep 2026 bills; replayed over both real cycles the schedule lands within 1–4%. `bill-record --from-text` also learns a small `scale` (and the CCA's per-kWh adder) from each bill; `franklinwh doctor` shows whether the schedule loaded (`Export pricing`). The "Export opportunity today" alert now names the best export hour and its real rate.

If your bill's `Export Pricing:` line names a different vintage (Legacy 2023/2025/2026), download that file from sdge.com/solar/solar-billing-plan/export-pricing (the `.zip`, unzip it) and run `python scripts/build_export_prices.py "<file>.csv"` to rebuild the JSON. Values past your 9-year lock-in are illustrative.

## iPhone / Apple Watch glance widget

`GET /api/glance` returns a flat object with a ready-to-display `text` (e.g. `74% 🔋 ☀0.0kW 🏠1.5kW`), plus `soc_pct`, `battery_state` (charging/discharging/idle), `eta_full_min` / `eta_empty_min`, `grid_status` and a `stale` flag (true when the newest reading is over 15 min old — the text is then prefixed `⚠ 90m old`).

**Reach it from anywhere with Tailscale** (no router changes, works on any Wi-Fi/cellular): install Tailscale on the iPhone, sign in to the same tailnet as the Mac, and use the host's tailnet address — `tailscale ip -4` on the host, currently `100.77.88.77` (or its MagicDNS name). Do **not** port-forward or expose the dashboard publicly.

**Shortcuts app** (iPhone): New Shortcut →
1. *Get Contents of URL* — URL `http://100.77.88.77:8093/api/glance`, Method GET, Headers: `X-Dashboard-Token` = the `dashboard_token` from `~/.franklinwh.json`.
2. *Get Dictionary Value* — key `text` (from the previous step).
3. *Show Result* (or *Show Notification*).
Add it to the Home Screen / a Lock Screen widget (Shortcuts widget), or add it to the Watch. Apple Watch caveat: a Watch shortcut runs through the iPhone, so it needs the iPhone in range; the Watch has no Tailscale client of its own.

The token is a shared secret: it sits in the Shortcut, so don't share the shortcut. Rotate it by editing `dashboard_token` in `~/.franklinwh.json` and restarting the dashboard agent.

## Alert Channels

- **Telegram**: chat ID `5650189923` (FranklinWH advisor + CMR News bot both configured)
- **iMessage**: not configured
