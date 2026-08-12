# Tesla EV Charge Control — One-Time Setup

The advisor can automatically adjust your Tesla's charging speed to track
your solar surplus (and fall back to full-speed super-off-peak charging
overnight). This requires a free-tier Tesla developer app — a one-time,
~30-minute setup. After this, everything is automatic.

**Cost:** Tesla's Fleet API is pay-per-use (commands $0.001, vehicle data
$0.002, wake $0.02) with a **$10/month credit** per developer account. The
advisor's adaptive polling budgets ≈ $4/month worst case — comfortably
inside the credit, so in practice it costs $0. The advisor meters every
call (`franklinwh account ev-status`) and stops before exceeding the credit.

## 1. Create a Tesla developer app

1. Sign in at <https://developer.tesla.com> with your Tesla account.
2. Create an application:
   - **Allowed origin:** your public HTTPS domain (the Tailscale Funnel /
     vantage-hub domain works).
   - **Redirect URI:** e.g. `https://<your-domain>/tesla/callback` — it can
     be a dead URL; the auth flow just needs you to copy the code out of the
     address bar.
   - **Scopes:** `vehicle_device_data` and `vehicle_charging_cmds` only.
3. Note the **client ID** and **client secret**.

## 2. Generate and host the command-signing keypair

Newer vehicles only accept signed commands, which requires an EC keypair
whose public half is hosted on your app's domain.

```bash
python3 scrape.py tesla keygen
```

Writes the private key to `~/.franklinwh_tesla_key.pem` (0600) and the
public key to `output/tesla-public-key.pem`, and prints the exact path to
host it at plus a ready-to-paste Caddy route block. Host it, then verify:

```bash
curl -s https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem
```

(Equivalent manual openssl commands, if you'd rather not run Python for this:
`openssl ecparam -name prime256v1 -genkey -noout -out ~/.franklinwh_tesla_key.pem`
then `chmod 600` it, then `openssl ec -in ~/.franklinwh_tesla_key.pem -pubout -out output/tesla-public-key.pem`.)

## 3. Register the partner account (once)

Tesla requires one registration call binding your domain to the app. Get a
partner token and register (client credentials — this is the only time the
client secret is used outside `tesla auth`):

```bash
TOKEN=$(curl -s -X POST https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token \
  -d grant_type=client_credentials \
  -d client_id=<CLIENT_ID> -d client_secret=<CLIENT_SECRET> \
  -d scope=openid \
  -d audience=https://fleet-api.prd.na.vn.cloud.tesla.com | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s -X POST https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/partner_accounts \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"domain": "<your-domain>"}'
```

## 4. Pair the virtual key with your car

On your phone (with the Tesla app installed), open:

```
https://tesla.com/_ak/<your-domain>
```

Approve the key in the Tesla app. Without this step, charging commands are
rejected on 2021+ vehicles.

## 5. Configure and authorize the advisor

```bash
python3 scrape.py setup          # enable EV control, enter VIN + client ID
python3 scrape.py tesla auth     # OAuth: paste the redirect URL back
python3 scrape.py doctor         # all EV lines should be green
```

## 6. Dry-run soak, then go live

The controller starts in **dry-run**: it logs every decision to
`output/ev_controller.jsonl` but sends no commands. Leave it for a sunny
day or two and compare its decisions with what you would have done in the
apps:

```bash
python3 scrape.py account ev-status
```

When the decisions look right:

```bash
python3 scrape.py tesla go-live
launchctl stop com.franklinwh.advisor && launchctl start com.franklinwh.advisor
```

## Recommended backstop

Leave **Scheduled Charging enabled in the Tesla app at 12:00 am** with your
usual charge limit. The car then starts overnight charging on its own even
if the advisor machine is off — the advisor's job becomes making daytime
charging smarter, with the car's own schedule as the safety net.

## How it behaves

- **Daytime:** charging amps track the solar surplus left after the house
  and the FranklinWH battery (until it reaches `ev_battery_first_soc`,
  default 80%). Below ~1.2 kW of surplus, charging pauses rather than pull
  from grid/battery.
- **On-peak (4–9 pm):** never charges; stops any session it started.
- **Overnight super-off-peak (12–6 am):** full-speed charge up to the car's
  own charge limit.
- **You take over anytime:** change amps in the Tesla app and the
  controller stands down for 4 hours (unplugging resets it).
- **Any failure** (Tesla API down, tokens expired, budget reached) degrades
  to doing nothing — you're back to exactly today's manual behavior, and
  the car's scheduled-charging backstop still works.
