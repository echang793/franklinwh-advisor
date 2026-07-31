"""Tesla Fleet API wrapper for the EV charging controller.

Synchronous facade over the asyncio `tesla-fleet-api` library (Teslemetry;
verified 1.7.6): each operation opens a session, runs the coroutine via
asyncio.run(), persists any rotated tokens, and returns plain data. The
advisor loop stays synchronous and never touches aiohttp directly.

Everything Fleet-API-specific lives here — auth, token rotation, command
signing, and the per-call spend meter — so if Python-side command signing
ever proves flaky the fallback (Tesla's Go `vehicle-command` HTTP proxy)
changes only this module.

Tokens live in ~/.franklinwh_tesla.json, NOT ~/.franklinwh.json: Fleet API
refresh tokens rotate on every refresh, and a concurrent setup-wizard
`config.save()` would clobber a rotated token and brick auth.

Costs (verified July 2026): commands $0.001, vehicle data $0.002,
wake $0.02 — against a $10/month credit. The spend callback lets the
controller meter every billable call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .ev_policy import VehicleChargeState

logger = logging.getLogger(__name__)

TESLA_TOKEN_PATH = Path.home() / ".franklinwh_tesla.json"
# App keypair for the Vehicle Command protocol (generated during setup —
# see docs/TESLA_SETUP.md; the matching public key is hosted on the app
# domain at /.well-known/appspecific/com.tesla.3p.public-key.pem).
TESLA_KEY_PATH = Path.home() / ".franklinwh_tesla_key.pem"

_SCOPES = ["openid", "offline_access", "vehicle_device_data",
           "vehicle_charging_cmds"]
_REGION = "na"


class TeslaError(RuntimeError):
    """Any Fleet API failure the controller should degrade gracefully on."""


class VehicleAsleep(TeslaError):
    """Vehicle is asleep/offline. Caller decides whether a $0.02 wake is worth it."""


class NotAuthorized(TeslaError):
    """No usable tokens — run `franklinwh tesla auth`."""


def _read_tokens() -> dict:
    try:
        return json.loads(TESLA_TOKEN_PATH.read_text())
    except FileNotFoundError:
        raise NotAuthorized(f"{TESLA_TOKEN_PATH} missing — run `franklinwh tesla auth`")
    except (json.JSONDecodeError, OSError) as e:
        raise NotAuthorized(f"{TESLA_TOKEN_PATH} unreadable ({e}) — re-run `franklinwh tesla auth`")


def _write_tokens(data: dict) -> None:
    """0600 from birth + atomic replace (same pattern as config.save())."""
    tmp = TESLA_TOKEN_PATH.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                 stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2))
        tmp.replace(TESLA_TOKEN_PATH)
    finally:
        tmp.unlink(missing_ok=True)


def _import_lib():
    """Lazy import so the advisor runs without tesla-fleet-api installed."""
    try:
        from tesla_fleet_api import TeslaFleetOAuth  # noqa: PLC0415
        return TeslaFleetOAuth
    except ImportError as e:
        raise TeslaError(
            "tesla-fleet-api not installed — "
            "pip3 install 'franklinwh-scraper[ev]' or pip3 install tesla-fleet-api"
        ) from e


def parse_charge_state(data: dict, fetched_at: datetime) -> VehicleChargeState:
    """Map a vehicle_data response's charge_state onto our policy dataclass."""
    cs = (data.get("response") or {}).get("charge_state") or {}
    charging_state = cs.get("charging_state") or "Disconnected"
    return VehicleChargeState(
        plugged_in=charging_state not in ("Disconnected",),
        charging=charging_state == "Charging",
        requested_amps=int(cs.get("charge_current_request") or 0),
        actual_current_a=float(cs.get("charger_actual_current") or 0.0),
        charger_voltage=float(cs.get("charger_voltage") or 0.0),
        vehicle_soc_pct=float(cs.get("battery_level") or 0.0),
        charge_limit_pct=float(cs.get("charge_limit_soc") or 0.0),
        charger_max_amps=int(cs.get("charge_current_request_max") or 0),
        fast_charger=bool(cs.get("fast_charger_present")),
        fetched_at=fetched_at,
    )


class TeslaClient:
    """One instance per advisor process. All methods are synchronous."""

    def __init__(self, vin: str, client_id: str,
                 token_path: Path = TESLA_TOKEN_PATH,
                 key_path: Path = TESLA_KEY_PATH,
                 on_spend: Callable[[str], None] | None = None):
        if not vin:
            raise TeslaError("tesla_vin not configured")
        if not client_id:
            raise TeslaError("tesla_client_id not configured")
        self.vin = vin
        self.client_id = client_id
        self.token_path = token_path
        self.key_path = key_path
        # on_spend("data"|"cmd"|"wake") — controller persists monthly counters.
        self._on_spend = on_spend or (lambda kind: None)

    # -- internals ----------------------------------------------------------

    async def _run(self, op: str, coro_name: str, *args) -> dict:
        import aiohttp  # noqa: PLC0415 — lazy, same reason as _import_lib

        TeslaFleetOAuth = _import_lib()
        tokens = _read_tokens()
        if not tokens.get("refresh_token"):
            raise NotAuthorized("no refresh_token on file — run `franklinwh tesla auth`")

        async with aiohttp.ClientSession() as session:
            api = TeslaFleetOAuth(
                session, region=_REGION, client_id=self.client_id,
                access_token=tokens.get("access_token"),
                refresh_token=tokens["refresh_token"],
                expires=int(tokens.get("expires", 0)),
            )
            try:
                await api.check_access_token()
            except Exception as e:
                raise NotAuthorized(f"token refresh failed ({e}) — "
                                    "re-run `franklinwh tesla auth`") from e
            # Persist rotation immediately — losing a rotated refresh token
            # bricks auth (the old one is single-use).
            if api.refresh_token and api.refresh_token != tokens["refresh_token"]:
                tokens.update(access_token=api._access_token,
                              refresh_token=api.refresh_token,
                              expires=api.expires)
                _write_tokens(tokens)

            if op == "cmd":
                await api.get_private_key(str(self.key_path))
                vehicle = api.vehicles.createSigned(self.vin)
            else:
                vehicle = api.vehicles.createFleet(self.vin)
            self._on_spend(op)
            return await getattr(vehicle, coro_name)(*args)

    def _call(self, op: str, coro_name: str, *args) -> dict:
        from tesla_fleet_api.exceptions import TeslaFleetError  # noqa: PLC0415
        try:
            return asyncio.run(self._run(op, coro_name, *args))
        except (NotAuthorized, VehicleAsleep):
            raise
        except TeslaFleetError as e:
            status = getattr(e, "status", None)
            if status == 408:  # vehicle unavailable = asleep/offline
                raise VehicleAsleep(str(e)) from e
            raise TeslaError(f"{coro_name}: {e}") from e
        except OSError as e:
            raise TeslaError(f"{coro_name}: network error {e}") from e

    # -- public surface ------------------------------------------------------

    def get_charge_state(self) -> VehicleChargeState:
        """One billable vehicle-data poll ($0.002). Raises VehicleAsleep
        rather than waking — the wake decision costs 10x a poll."""
        data = self._call("data", "vehicle_data", ["charge_state"])
        return parse_charge_state(data, datetime.now())

    def wake(self) -> None:
        """$0.02 — controller calls this only when it intends to command."""
        self._call("wake", "wake_up")

    def set_charging_amps(self, amps: int) -> None:
        self._call("cmd", "set_charging_amps", int(amps))

    def charge_start(self) -> None:
        self._call("cmd", "charge_start")

    def charge_stop(self) -> None:
        self._call("cmd", "charge_stop")


# -- one-time auth bootstrap (used by `franklinwh tesla auth`) ---------------

def build_login_url(client_id: str, redirect_uri: str) -> str:
    scopes = "+".join(_SCOPES)
    return ("https://auth.tesla.com/oauth2/v3/authorize?response_type=code"
            f"&client_id={client_id}&redirect_uri={redirect_uri}"
            f"&scope={scopes}&state=franklinwh")


def exchange_code(client_id: str, client_secret: str, redirect_uri: str,
                  code: str) -> None:
    """Exchange an authorization code and write the token file."""
    import aiohttp  # noqa: PLC0415

    TeslaFleetOAuth = _import_lib()

    async def _go() -> dict:
        async with aiohttp.ClientSession() as session:
            api = TeslaFleetOAuth(session, region=_REGION, client_id=client_id,
                                  client_secret=client_secret,
                                  redirect_uri=redirect_uri)
            await api.get_refresh_token(code)
            return {"access_token": api._access_token,
                    "refresh_token": api.refresh_token,
                    "expires": api.expires}

    tokens = asyncio.run(_go())
    if not tokens.get("refresh_token"):
        raise TeslaError("token exchange returned no refresh_token")
    _write_tokens(tokens)
