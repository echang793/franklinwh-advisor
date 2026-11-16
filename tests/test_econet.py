"""Unit tests for econet.py — fakes stand in for pyeconet, no network."""

from __future__ import annotations

import enum
import os
import stat
from datetime import datetime

import pytest

from franklinwh_scraper import econet
from franklinwh_scraper.econet import (
    EcoNetAuthError,
    EcoNetClient,
    EcoNetError,
    EcoNetRateLimited,
    econet_name_for,
    mode_from_econet,
    read_creds,
    write_creds,
)
from franklinwh_scraper.wh_policy import WhMode

NOW = datetime(2026, 11, 15, 12, 0)


class FakeOpMode(enum.IntEnum):
    OFF = 1
    ELECTRIC_MODE = 2
    ENERGY_SAVING = 3
    HEAT_PUMP_ONLY = 4
    HIGH_DEMAND = 5
    VACATION = 9


class FakeHeater:
    def __init__(self, mode=FakeOpMode.ENERGY_SAVING, set_point=125, running=False,
                 running_state="", connected=True, tank=100,
                 modes=(FakeOpMode.OFF, FakeOpMode.ENERGY_SAVING, FakeOpMode.HEAT_PUMP_ONLY,
                        FakeOpMode.HIGH_DEMAND, FakeOpMode.ELECTRIC_MODE, FakeOpMode.VACATION)):
        self.mode = mode
        self.set_point = set_point
        self.set_point_limits = (110, 140)
        self.running = running
        self.running_state = running_state
        self.connected = connected
        self.tank_hot_water_availability = tank
        self.modes = list(modes)
        self.calls: list[tuple] = []

    def set_mode(self, m):
        self.calls.append(("mode", m))

    def set_set_point(self, sp):
        self.calls.append(("setpoint", sp))


class FakeApi:
    def __init__(self, heaters, fail_first: Exception | None = None, connected=True):
        self.heaters = heaters
        self.fail_first = fail_first
        self.subscribed = False
        self.unsubscribed = False
        self._connected = connected
        self.user_token = "tok"
        self.account_id = "acct"

    async def water_heaters(self):
        if self.fail_first is not None:
            e, self.fail_first = self.fail_first, None
            raise e
        return list(self.heaters)

    def subscribe(self):
        self.subscribed = True

    def is_connected(self):
        return self._connected

    def unsubscribe(self):
        self.unsubscribed = True


class FakeHTTPError(Exception):
    """Shaped like pyeconet's GenericHTTPError(resp.status)."""


TOKEN = {"user_token": "old", "account_id": "acct"}


def _client(api_or_apis, tmp_path, creds_extra=None, connect_error=None, **kw):
    apis = list(api_or_apis) if isinstance(api_or_apis, list) else [api_or_apis]
    connects: list[bool] = []

    async def connect(creds, fresh):
        connects.append(fresh)
        if connect_error is not None:
            raise connect_error
        return apis.pop(0) if len(apis) > 1 else apis[0]

    path = tmp_path / "econet.json"
    write_creds({"email": "a@b.c", "password": "pw", **(creds_extra or {})}, path)
    c = EcoNetClient(connect=connect, creds_path=path, mode_enum=FakeOpMode,
                     settle_s=0, connect_timeout_s=0.05, **kw)
    return c, connects


# ---------------------------------------------------------------- mode mapping

@pytest.mark.parametrize("name,mode", [
    ("HEAT_PUMP_ONLY", WhMode.HEAT_PUMP), ("ENERGY_SAVING", WhMode.ENERGY_SAVER),
    ("HIGH_DEMAND", WhMode.HIGH_DEMAND), ("ELECTRIC_MODE", WhMode.ELECTRIC),
    ("VACATION", WhMode.VACATION), ("OFF", WhMode.OFF),
])
def test_mode_round_trip(name, mode):
    assert mode_from_econet(name) == mode
    assert econet_name_for(mode) == name


def test_unknown_mode_maps_to_none():
    assert mode_from_econet("GAS") is None


# ---------------------------------------------------------------- credentials

def test_creds_file_is_owner_only(tmp_path):
    p = tmp_path / "c.json"
    write_creds({"email": "a@b.c", "password": "pw"}, p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert read_creds(p)["email"] == "a@b.c"


def test_missing_creds_raise_auth_error(tmp_path):
    with pytest.raises(EcoNetAuthError):
        read_creds(tmp_path / "nope.json")


# ---------------------------------------------------------------- read_state

def test_read_state_maps_heater(tmp_path):
    h = FakeHeater(mode=FakeOpMode.HEAT_PUMP_ONLY, set_point=130, running=True,
                   running_state="Heat Pump Running", tank=66)
    c, _ = _client(FakeApi([h]), tmp_path)
    r = c.read_state(NOW)
    assert r.state.mode == WhMode.HEAT_PUMP
    assert r.state.setpoint_f == 130
    assert r.state.running is True
    assert r.state.fetched_at == NOW
    assert r.running_state == "Heat Pump Running"
    assert r.tank_pct == 66


def test_read_state_caches_token(tmp_path):
    c, _ = _client(FakeApi([FakeHeater()]), tmp_path)
    c.read_state(NOW)
    saved = read_creds(c.creds_path)
    assert saved["user_token"] == "tok" and saved["account_id"] == "acct"
    assert saved["password"] == "pw"


def test_read_state_relogs_in_once_on_stale_token(tmp_path):
    stale = FakeApi([FakeHeater()], fail_first=RuntimeError("401"))
    fresh = FakeApi([FakeHeater()])
    c, connects = _client([stale, fresh], tmp_path, creds_extra=TOKEN)
    assert c.read_state(NOW).state.mode == WhMode.ENERGY_SAVER
    assert connects == [False, True]


# ---------------------------------------------------------------- rate limits (HTTP 429)

def test_first_login_is_attempted_once(tmp_path):
    # No cached token: the first attempt already logs in; a second would double the hits.
    c, connects = _client(FakeApi([FakeHeater()], fail_first=RuntimeError("boom")), tmp_path)
    with pytest.raises(EcoNetError):
        c.read_state(NOW)
    assert connects == [True]


def test_429_on_login_raises_rate_limited_without_retry(tmp_path):
    c, connects = _client(FakeApi([FakeHeater()]), tmp_path, creds_extra=TOKEN,
                          connect_error=FakeHTTPError(429))
    with pytest.raises(EcoNetRateLimited) as ei:
        c.read_state(NOW)
    assert connects == [False]
    assert ei.value.stage == "login"
    assert "429" in str(ei.value)


def test_429_on_equipment_list_raises_rate_limited_without_retry(tmp_path):
    c, connects = _client(FakeApi([FakeHeater()], fail_first=FakeHTTPError(429)), tmp_path,
                          creds_extra=TOKEN)
    with pytest.raises(EcoNetRateLimited) as ei:
        c.read_state(NOW)
    assert connects == [False]
    assert ei.value.stage == "equipment list"


def test_rate_limited_is_an_econet_error():
    assert issubclass(EcoNetRateLimited, EcoNetError)


def test_read_state_no_heater_raises(tmp_path):
    c, _ = _client(FakeApi([]), tmp_path)
    with pytest.raises(EcoNetError):
        c.read_state(NOW)


def test_read_state_offline_heater_raises(tmp_path):
    c, _ = _client(FakeApi([FakeHeater(connected=False)]), tmp_path)
    with pytest.raises(EcoNetError):
        c.read_state(NOW)


def test_read_state_unknown_mode_raises(tmp_path):
    class Odd(enum.IntEnum):
        GAS = 6
    c, _ = _client(FakeApi([FakeHeater(mode=Odd.GAS)]), tmp_path)
    with pytest.raises(EcoNetError):
        c.read_state(NOW)


# ---------------------------------------------------------------- apply

def test_apply_publishes_mode_then_setpoint(tmp_path):
    h = FakeHeater()
    api = FakeApi([h])
    c, _ = _client(api, tmp_path)
    c.apply(WhMode.HEAT_PUMP, 120)
    assert h.calls == [("mode", FakeOpMode.HEAT_PUMP_ONLY), ("setpoint", 120)]
    assert api.subscribed and api.unsubscribed


def test_apply_mode_only_when_setpoint_none(tmp_path):
    h = FakeHeater()
    c, _ = _client(FakeApi([h]), tmp_path)
    c.apply(WhMode.VACATION, None)
    assert h.calls == [("mode", FakeOpMode.VACATION)]


def test_apply_rejects_unsupported_mode(tmp_path):
    h = FakeHeater(modes=(FakeOpMode.ENERGY_SAVING,))
    c, _ = _client(FakeApi([h]), tmp_path)
    with pytest.raises(EcoNetError):
        c.apply(WhMode.HEAT_PUMP, 120)
    assert h.calls == []


def test_apply_rejects_setpoint_outside_heater_limits(tmp_path):
    h = FakeHeater()
    h.set_point_limits = (125, 140)
    c, _ = _client(FakeApi([h]), tmp_path)
    with pytest.raises(EcoNetError):
        c.apply(WhMode.HEAT_PUMP, 120)
    assert h.calls == []


def test_apply_mqtt_never_connects_raises_and_cleans_up(tmp_path):
    h = FakeHeater()
    api = FakeApi([h], connected=False)
    c, _ = _client(api, tmp_path)
    with pytest.raises(EcoNetError):
        c.apply(WhMode.HEAT_PUMP, 120)
    assert h.calls == []
    assert api.unsubscribed


def test_module_does_not_import_pyeconet_at_load():
    # Advisor must start even where pyeconet isn't installed.
    src = open(econet.__file__).read()
    assert "\nimport pyeconet" not in src and "\nfrom pyeconet" not in src
