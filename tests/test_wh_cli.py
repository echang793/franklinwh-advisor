"""CLI tests for `franklinwh waterheater` — fake client, tmp creds and config."""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from franklinwh_scraper import wh_cli
from franklinwh_scraper.config import Config
from franklinwh_scraper.econet import EcoNetError, HeaterReading
from franklinwh_scraper.wh_policy import WaterHeaterState, WhMode


class _OkClient:
    def read_state(self, now):
        return HeaterReading(WaterHeaterState(WhMode.ENERGY_SAVER, 125, False, now), "", 100)


class _BadClient:
    def read_state(self, now):
        raise EcoNetError("EcoNet rejected the login")


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = Config()
    cfg.output_dir = str(tmp_path)
    saved = []
    monkeypatch.setattr(wh_cli, "CREDS_PATH", tmp_path / "econet.json")
    monkeypatch.setattr(wh_cli, "save_config", lambda c: saved.append(
        (c.wh_control_enabled, c.wh_dry_run)))

    @click.group()
    @click.pass_context
    def root(ctx):
        ctx.ensure_object(dict)
        ctx.obj["config"] = cfg

    root.add_command(wh_cli.grp_wh)
    return root, cfg, saved, tmp_path


def test_setup_enables_dry_run_and_never_echoes_password(env, monkeypatch):
    root, cfg, saved, tmp = env
    monkeypatch.setattr(wh_cli, "EcoNetClient", lambda creds_path=None: _OkClient())
    res = CliRunner().invoke(root, ["waterheater", "setup"], input="me@x.com\nhunter2\n")
    assert res.exit_code == 0, res.output
    assert "hunter2" not in res.output
    assert saved == [(True, True)]
    assert json.loads((tmp / "econet.json").read_text())["email"] == "me@x.com"
    assert "energy_saver" in res.output


def test_setup_failed_read_does_not_enable(env, monkeypatch):
    root, cfg, saved, _ = env
    monkeypatch.setattr(wh_cli, "EcoNetClient", lambda creds_path=None: _BadClient())
    res = CliRunner().invoke(root, ["waterheater", "setup"], input="me@x.com\nbad\n")
    assert res.exit_code != 0
    assert saved == []


def test_go_live_requires_setup(env):
    root, cfg, saved, _ = env
    res = CliRunner().invoke(root, ["waterheater", "go-live"], input="y\n")
    assert res.exit_code != 0 and saved == []


def test_go_live_turns_off_dry_run(env):
    root, cfg, saved, _ = env
    cfg.wh_control_enabled = True
    res = CliRunner().invoke(root, ["waterheater", "go-live"], input="y\n")
    assert res.exit_code == 0, res.output
    assert saved == [(True, False)]


def test_status_summarizes_log(env):
    root, cfg, _, tmp = env
    lines = [
        {"timestamp": "2026-11-16T17:00:00", "period": "on_peak", "running": True,
         "running_state": "Electric Running", "action": "set", "reason": "on-peak — heat pump only"},
        {"timestamp": "2026-11-16T17:05:00", "period": "on_peak", "running": True,
         "running_state": "Heat Pump Running", "action": "none", "reason": "x"},
    ]
    (tmp / "wh_controller.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    res = CliRunner().invoke(root, ["waterheater", "status"])
    assert res.exit_code == 0, res.output
    assert "on-peak element runs: 1" in res.output
