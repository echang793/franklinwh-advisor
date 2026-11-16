"""Rheem EcoNet wrapper for the water heater controller.

Synchronous facade over the asyncio `pyeconet` library (verified 0.3.1),
same shape as tesla.py: each call opens a session, runs a coroutine with
asyncio.run(), and returns plain data. pyeconet is imported lazily, so the
advisor runs fine where it isn't installed.

Reads go over EcoNet's REST API. Mode and setpoint changes go over MQTT:
connect, publish, wait briefly, disconnect. A command that can't connect
raises before anything is published.

Credentials (EcoNet email + password, plus the cached user token) live in
~/.franklinwh_econet.json, owner-only, never in ~/.franklinwh.json — same
reasoning as the Tesla token file.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .wh_policy import WaterHeaterState, WhMode

CREDS_PATH = Path.home() / ".franklinwh_econet.json"

# pyeconet WaterHeaterOperationMode name -> WhMode. Several names are aliases
# pyeconet itself treats as the same mode.
_FROM_ECONET = {
    "OFF": WhMode.OFF,
    "HEAT_PUMP_ONLY": WhMode.HEAT_PUMP, "HEAT_PUMP": WhMode.HEAT_PUMP,
    "ENERGY_SAVING": WhMode.ENERGY_SAVER, "ENERGY_SAVER": WhMode.ENERGY_SAVER,
    "HIGH_DEMAND": WhMode.HIGH_DEMAND, "PERFORMANCE": WhMode.HIGH_DEMAND,
    "ELECTRIC_MODE": WhMode.ELECTRIC, "ELECTRIC": WhMode.ELECTRIC,
    "VACATION": WhMode.VACATION,
}
# WhMode -> the canonical pyeconet name used when commanding.
_TO_ECONET = {
    WhMode.OFF: "OFF", WhMode.HEAT_PUMP: "HEAT_PUMP_ONLY",
    WhMode.ENERGY_SAVER: "ENERGY_SAVING", WhMode.HIGH_DEMAND: "HIGH_DEMAND",
    WhMode.ELECTRIC: "ELECTRIC_MODE", WhMode.VACATION: "VACATION",
}


class EcoNetError(RuntimeError):
    pass


class EcoNetAuthError(EcoNetError):
    pass


@dataclass(frozen=True)
class HeaterReading:
    state: WaterHeaterState
    running_state: str          # raw EcoNet text, logged for the dry-run soak
    tank_pct: int | None        # hot water availability (100/66/33/0)


def mode_from_econet(name: str) -> WhMode | None:
    return _FROM_ECONET.get(name)


def econet_name_for(mode: WhMode) -> str:
    return _TO_ECONET[mode]


# ---------------------------------------------------------------- credentials

def read_creds(path: Path | None = None) -> dict:
    p = Path(path or CREDS_PATH)
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise EcoNetAuthError(f"no EcoNet credentials at {p}") from e
    if not data.get("email") or not data.get("password"):
        raise EcoNetAuthError(f"{p} needs email and password")
    return data


def write_creds(data: dict, path: Path | None = None) -> None:
    """Atomic, owner-only write (no window where the file is world-readable)."""
    p = Path(path or CREDS_PATH)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, p)
    p.chmod(stat.S_IRUSR | stat.S_IWUSR)


# ---------------------------------------------------------------- pyeconet adapter

class _Adapter:
    """The few calls the client needs, over a real EcoNetApiInterface."""

    def __init__(self, api: Any):
        self._api = api

    @property
    def user_token(self) -> str:
        return self._api.user_token

    @property
    def account_id(self) -> str:
        return self._api.account_id

    async def water_heaters(self) -> list:
        from pyeconet.equipment import EquipmentType
        found = await self._api.get_equipment_by_type([EquipmentType.WATER_HEATER])
        return list(found.get(EquipmentType.WATER_HEATER, []))

    def subscribe(self) -> None:
        self._api.subscribe()

    def is_connected(self) -> bool:
        client = getattr(self._api, "_mqtt_client", None)
        return bool(client is not None and client.is_connected())

    def unsubscribe(self) -> None:
        client = getattr(self._api, "_mqtt_client", None)
        if client is not None:
            client.loop_stop()
            client.disconnect()


async def _default_connect(creds: dict, fresh: bool) -> _Adapter:
    from pyeconet import EcoNetApiInterface
    from pyeconet.errors import InvalidCredentialsError
    if not fresh and creds.get("user_token") and creds.get("account_id"):
        return _Adapter(EcoNetApiInterface(creds["email"], creds["password"],
                                           creds["account_id"], creds["user_token"]))
    try:
        return _Adapter(await EcoNetApiInterface.login(creds["email"], creds["password"]))
    except InvalidCredentialsError as e:
        raise EcoNetAuthError(f"EcoNet rejected the login: {e}") from e


def _default_mode_enum():
    from pyeconet.equipment.water_heater import WaterHeaterOperationMode
    return WaterHeaterOperationMode


# ---------------------------------------------------------------- client

class EcoNetClient:
    def __init__(self, connect: Callable[[dict, bool], Awaitable[Any]] | None = None,
                 creds_path: Path | None = None, mode_enum: Any = None,
                 settle_s: float = 3.0, connect_timeout_s: float = 15.0):
        self._connect = connect or _default_connect
        self.creds_path = Path(creds_path or CREDS_PATH)
        self._mode_enum = mode_enum
        self._settle_s = settle_s
        self._connect_timeout_s = connect_timeout_s

    def read_state(self, now: datetime) -> HeaterReading:
        return asyncio.run(self._read(now))

    def apply(self, mode: WhMode, setpoint_f: int | None) -> None:
        asyncio.run(self._apply(mode, setpoint_f))

    # ------------------------------------------------------------ internals

    async def _heater(self) -> tuple[Any, Any]:
        """(api, first water heater). Retries once with a fresh login."""
        creds = read_creds(self.creds_path)
        last: Exception | None = None
        for fresh in (False, True):
            try:
                api = await self._connect(creds, fresh)
                heaters = await api.water_heaters()
            except EcoNetAuthError:
                raise
            except Exception as e:      # stale token, HTTP error, network
                last = e
                continue
            self._remember_token(creds, api)
            if not heaters:
                raise EcoNetError("no water heater on this EcoNet account")
            return api, heaters[0]
        raise EcoNetError(f"EcoNet unreachable: {last}") from last

    def _remember_token(self, creds: dict, api: Any) -> None:
        token, acct = getattr(api, "user_token", None), getattr(api, "account_id", None)
        if token and (token, acct) != (creds.get("user_token"), creds.get("account_id")):
            write_creds({**creds, "user_token": token, "account_id": acct}, self.creds_path)

    async def _read(self, now: datetime) -> HeaterReading:
        _, h = await self._heater()
        if not h.connected:
            raise EcoNetError("water heater is offline in EcoNet")
        mode = mode_from_econet(getattr(h.mode, "name", ""))
        if mode is None:
            raise EcoNetError(f"unrecognized water heater mode {h.mode!r}")
        state = WaterHeaterState(mode=mode, setpoint_f=int(h.set_point),
                                 running=bool(h.running), fetched_at=now)
        return HeaterReading(state=state, running_state=str(h.running_state or ""),
                             tank_pct=h.tank_hot_water_availability)

    async def _apply(self, mode: WhMode, setpoint_f: int | None) -> None:
        api, h = await self._heater()
        enum_cls = self._mode_enum or _default_mode_enum()
        target = enum_cls[econet_name_for(mode)]
        if target not in h.modes:
            raise EcoNetError(f"heater doesn't support {mode.value}")
        if setpoint_f is not None:
            lo, hi = h.set_point_limits
            if not lo <= setpoint_f <= hi:
                raise EcoNetError(f"setpoint {setpoint_f} outside heater limits {lo}-{hi}")
        api.subscribe()
        try:
            waited = 0.0
            while not api.is_connected():
                if waited >= self._connect_timeout_s:
                    raise EcoNetError("EcoNet MQTT never connected; nothing sent")
                await asyncio.sleep(0.05)
                waited += 0.05
            h.set_mode(target)
            if setpoint_f is not None:
                h.set_set_point(setpoint_f)
            await asyncio.sleep(self._settle_s)    # let paho flush before disconnect
        finally:
            api.unsubscribe()
