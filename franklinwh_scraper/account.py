"""FranklinWH account API client.

Reverse-engineered from https://github.com/richo/franklinwh-python
API base: https://energy.franklinwh.com/
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import zlib
from dataclasses import asdict, dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://energy.franklinwh.com/"


# ------------------------------------------------------------------ #
# Data classes                                                         #
# ------------------------------------------------------------------ #

@dataclass
class Current:
    solar_production_kw: float    # p_sun
    generator_production_kw: float  # p_gen
    generator_enabled: bool       # genStat > 1
    battery_use_kw: float         # p_fhp  (negative = charging, positive = discharging)
    grid_use_kw: float            # p_uti  (positive = import, negative = export)
    home_load_kw: float           # p_load
    battery_soc_pct: float        # soc
    grid_status: str              # "normal" | "down" | "off"


@dataclass
class Totals:
    battery_charge_kwh: float     # kwh_fhp_chg
    battery_discharge_kwh: float  # kwh_fhp_di
    grid_import_kwh: float        # kwh_uti_in  — utility meter (includes grid→battery + noise)
    grid_export_kwh: float        # soOutGrid — solar exported to grid (matches app)
    grid_load_kwh: float          # kwhGridLoad/1000 — grid consumed by home (matches app)
    solar_kwh: float              # kwh_sun
    generator_kwh: float          # kwh_gen
    home_use_kwh: float           # (kwhFhpLoad+kwhSolarLoad+kwhGridLoad)/1000 — matches app


@dataclass
class Stats:
    timestamp: str
    gateway_id: str
    current: Current
    totals: Totals

    def to_flat_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"timestamp": self.timestamp, "gateway_id": self.gateway_id}
        for k, v in asdict(self.current).items():
            d[f"current_{k}"] = v
        for k, v in asdict(self.totals).items():
            d[f"totals_{k}"] = v
        return d


# ------------------------------------------------------------------ #
# Client                                                               #
# ------------------------------------------------------------------ #

class AccountClient:
    """Synchronous client for the FranklinWH account API."""

    def __init__(self, email: str, password: str, timeout: int = 15):
        self.email = email
        self._password_md5 = hashlib.md5(password.encode("ascii")).hexdigest()
        self.timeout = timeout
        self._token: str | None = None
        self._snno = 0
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "FranklinWH/2026 Python/3.13",
        })

    # -------------------------------------------------------------- #
    # Auth                                                             #
    # -------------------------------------------------------------- #

    def login(self) -> str:
        """Authenticate and store token. Returns the token."""
        url = API_BASE + "hes-gateway/terminal/initialize/appUserOrInstallerLogin"
        resp = self.session.post(url, data={
            "account": self.email,
            "password": self._password_md5,
            "lang": "en_US",
            "type": 1,
        }, timeout=self.timeout)
        resp.raise_for_status()
        js = resp.json()
        if js.get("code") == 401:
            raise ValueError(f"Invalid credentials: {js.get('message')}")
        if js.get("code") == 400:
            raise ValueError(f"Account locked: {js.get('message')}")
        if js.get("code") != 200 or "result" not in js:
            # Any other API error code (500, rate-limit, maintenance, etc.)
            # used to fall through to js["result"]["token"] and crash with a
            # bare KeyError instead of a clear auth error.
            raise RuntimeError(f"Login failed, code {js.get('code')}: {js.get('message')}")
        self._token = js["result"]["token"]
        logger.info("Logged in as %s", self.email)
        return self._token

    def _ensure_token(self):
        if not self._token:
            self.login()

    # -------------------------------------------------------------- #
    # HTTP helpers                                                     #
    # -------------------------------------------------------------- #

    def _get(self, path: str, params: dict | None = None, _retried: bool = False) -> Any:
        self._ensure_token()
        if params is None:
            params = {}
        url = API_BASE + path
        resp = self.session.get(
            url,
            params=params,
            headers={"loginToken": self._token},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        js = resp.json()
        if js.get("code") == 401:
            if _retried:
                raise ConnectionError(f"Still unauthorized after re-login: {js.get('message')}")
            self.login()
            return self._get(path, params, _retried=True)
        if js.get("code") not in (200, None):
            # Callers (get_gateways, get_composite_info, ...) fall back to an
            # empty result on a missing "result" key by design — that's the
            # right behavior for e.g. a gateway handshake timeout. But a real
            # API error code (500, rate-limit) hitting that same fallback
            # used to look identical to "genuinely empty account" with zero
            # trace in the logs. Surface it without changing control flow.
            logger.warning("API call to %s returned code %s: %s", path, js.get("code"), js.get("message"))
        return js

    # -------------------------------------------------------------- #
    # MQTT wrapper (real-time data)                                    #
    # -------------------------------------------------------------- #

    def _next_snno(self) -> int:
        self._snno += 1
        return self._snno

    def _build_mqtt_payload(self, cmd_type: int, data: dict, gateway: str) -> str:
        raw = json.dumps(data, separators=(",", ":"))
        blob = raw.encode("utf-8")
        crc = f"{zlib.crc32(blob):08X}"
        ts = int(time.time())
        envelope = json.dumps({
            "lang": "EN_US",
            "cmdType": cmd_type,
            "equipNo": gateway,
            "type": 0,
            "timeStamp": ts,
            "snno": self._next_snno(),
            "len": len(blob),
            "crc": crc,
            "dataArea": "DATA",
        })
        return envelope.replace('"DATA"', raw)

    def _mqtt_send(self, payload: str, gateway: str, _retried: bool = False) -> Any:
        self._ensure_token()
        url = API_BASE + "hes-gateway/terminal/sendMqtt"
        resp = self.session.post(
            url,
            params={"gatewayId": gateway, "lang": "en_US"},
            headers={"loginToken": self._token, "Content-Type": "application/json"},
            data=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        js = resp.json()
        if js.get("code") == 401:
            if _retried:
                raise ConnectionError(f"Still unauthorized after re-login: {js.get('message')}")
            self.login()
            return self._mqtt_send(payload, gateway, _retried=True)
        if js.get("code") == 102:
            raise TimeoutError(f"Device timeout: {js.get('message')}")
        if js.get("code") == 136:
            raise ConnectionError(f"Gateway offline: {js.get('message')}")
        if js.get("code") != 200:
            raise RuntimeError(f"MQTT error {js.get('code')}: {js.get('message')}")
        return js

    # -------------------------------------------------------------- #
    # Public API                                                       #
    # -------------------------------------------------------------- #

    def get_gateways(self) -> list[dict[str, Any]]:
        """List all gateways / aGates on the account."""
        js = self._get("hes-gateway/terminal/getHomeGatewayList")
        return js.get("result", [])

    def get_composite_info(self, gateway: str) -> dict[str, Any]:
        """Get device composite info (runtime data, totals)."""
        js = self._get(
            "hes-gateway/terminal/getDeviceCompositeInfo",
            params={"gatewayId": gateway, "lang": "en_US", "refreshFlag": 1},
        )
        return js.get("result", {})

    def get_switch_usage(self, gateway: str) -> dict[str, Any]:
        """Get real-time smart-circuit load data via MQTT cmd 353."""
        payload = self._build_mqtt_payload(353, {"opt": 0, "order": gateway}, gateway)
        data_area = self._mqtt_send(payload, gateway)["result"]["dataArea"]
        return json.loads(data_area)

    def get_stats(self, gateway: str, _empty_retries: int = 2) -> Stats:
        """Get instantaneous + daily total stats for a gateway."""
        info = self.get_composite_info(gateway)
        data = info.get("runtimeData") or {}
        if not data:
            # getDeviceCompositeInfo(refreshFlag=1) forces a live query to the
            # gateway hardware — if the gateway doesn't answer in time, the API
            # returns success with an empty runtimeData payload (a device
            # handshake timing hiccup, not an account/API outage — the mobile
            # app looks fine because it isn't forcing a live refresh). A short
            # retry absorbs this instead of failing the whole poll cycle.
            # Every field below defaults to 0.0 if we proceed with empty data,
            # which fabricates a fake "solar/battery/load all zero" reading
            # that trips false alerts (e.g. "solar dropped mid-day") and
            # pollutes history — so still raise if retries are exhausted.
            if _empty_retries > 0:
                time.sleep(3)
                return self.get_stats(gateway, _empty_retries=_empty_retries - 1)
            raise ConnectionError(f"Empty runtimeData from API for gateway {gateway}")

        # grid status
        offgrid = data.get("offgridreason")
        if offgrid is None or offgrid == -1:
            grid_status = "normal"
        elif offgrid == 0:
            grid_status = "down"
        else:
            grid_status = "off"

        # smart-switch data (may fail if no smart circuits installed)
        sw: dict[str, Any] = {}
        try:
            sw = self.get_switch_usage(gateway)
        except Exception as e:
            logger.debug("Smart switch data unavailable: %s", e)

        current = Current(
            solar_production_kw=data.get("p_sun", 0.0),
            generator_production_kw=data.get("p_gen", 0.0),
            generator_enabled=bool(data.get("genStat", 0) > 1),
            battery_use_kw=data.get("p_fhp", 0.0),
            grid_use_kw=data.get("p_uti", 0.0),
            home_load_kw=data.get("p_load", 0.0),
            battery_soc_pct=data.get("soc", 0.0),
            grid_status=grid_status,
        )
        totals = Totals(
            battery_charge_kwh=data.get("kwh_fhp_chg", 0.0),
            battery_discharge_kwh=data.get("kwh_fhp_di", 0.0),
            grid_import_kwh=data.get("kwh_uti_in", 0.0),
            grid_export_kwh=data.get("soOutGrid", 0.0),
            grid_load_kwh=data.get("kwhGridLoad", 0.0) / 1000.0,
            solar_kwh=data.get("kwh_sun", 0.0),
            generator_kwh=data.get("kwh_gen", 0.0),
            home_use_kwh=(data.get("kwhFhpLoad", 0) + data.get("kwhSolarLoad", 0) + data.get("kwhGridLoad", 0)) / 1000.0,
        )
        return Stats(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            gateway_id=gateway,
            current=current,
            totals=totals,
        )

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
