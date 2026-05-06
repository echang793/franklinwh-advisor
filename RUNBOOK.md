# Monitoring Stack — RUNBOOK

## Systems

| System | Location | Log | Schedule |
|--------|----------|-----|----------|
| FranklinWH Advisor | `~/Desktop/franklinwh` | `output/advisor.log` | cron `*/15 8-23 * * *` |
| CMR News Bot | `~/Desktop/cmr-news` | `bot.log` | LaunchAgent `com.cmrnews.bot` |

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

## Diagnostics

```bash
# FranklinWH — recent errors
grep -i "error\|warn\|fail" ~/Desktop/franklinwh/output/advisor.log | tail -30

# CMR News — service status
launchctl list com.cmrnews.bot
tail -20 ~/Desktop/cmr-news/bot.log

# Reload CMR News bot after plist changes
launchctl unload ~/Library/LaunchAgents/com.cmrnews.bot.plist
launchctl load  ~/Library/LaunchAgents/com.cmrnews.bot.plist

# Test open-meteo reachability
curl -s "https://api.open-meteo.com/v1/forecast?latitude=32.97&longitude=-117.07&hourly=cloud_cover&forecast_days=1" | python3.13 -c "import sys,json; d=json.load(sys.stdin); print('ok', len(d['hourly']['time']), 'hours')"
```

## Alert Channels

- **Telegram**: chat ID `5650189923` (FranklinWH advisor + CMR News bot both configured)
- **iMessage**: not configured
