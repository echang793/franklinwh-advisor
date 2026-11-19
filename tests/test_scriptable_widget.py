"""iPhone Scriptable widget: the pure view logic, run under node with a sample /api/glance payload."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "scriptable" / "franklinwh-widget.js"
NODE = shutil.which("node")

SAMPLE = {"ok": True, "stale": False, "age_min": 3, "ts": "2026-11-15T20:01:59", "soc_pct": 52,
          "battery_state": "discharging", "battery_kw": 0.58, "solar_kw": 0.0, "load_kw": 0.56,
          "grid_kw": -0.02, "grid_status": "normal", "eta_full_min": None, "eta_empty_min": 736,
          "solar_today_kwh": 17.6, "text": "52% 🔋 ☀0.0kW 🏠0.6kW"}


def _run(expr: str):
    # Scriptable allows top-level await, so load the file as an async function body (not require()).
    js = (f"const src = require('fs').readFileSync({json.dumps(str(SCRIPT))}, 'utf8');"
          "const AF = Object.getPrototypeOf(async function(){}).constructor;"
          "const module = {exports: {}};"
          f"new AF('module', src)(module).then(() => {{ const W = module.exports;"
          f" console.log(JSON.stringify({expr})); }});")
    out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_script_holds_no_secret_and_keeps_the_token_in_the_keychain():
    src = SCRIPT.read_text()
    assert "Keychain" in src and "X-Dashboard-Token" in src
    cfg = json.loads((Path.home() / ".franklinwh.json").read_text()) if (Path.home() / ".franklinwh.json").exists() else {}
    tok = cfg.get("dashboard_token")
    if tok:
        assert tok not in src


pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def test_eta_formatting():
    assert _run("[W.fmtEta(736), W.fmtEta(45), W.fmtEta(60), W.fmtEta(null), W.fmtEta(0)]") == \
        ["12h 16m", "45m", "1h 0m", None, "0m"]


def test_view_of_a_discharging_battery():
    v = _run(f"W.view({json.dumps(SAMPLE)}, {{}})")
    assert v["pct"] == 52 and v["frac"] == 0.52
    assert v["detail"] == "empty in 12h 16m"
    assert v["solar"] == "0.0 kW" and v["load"] == "0.6 kW"
    assert v["level"] == "ok" and v["warn"] is None


def test_view_low_battery_charging_and_grid_down():
    g = dict(SAMPLE, soc_pct=15, battery_state="charging", eta_full_min=95, eta_empty_min=None,
             grid_status="down")
    v = _run(f"W.view({json.dumps(g)}, {{}})")
    assert v["level"] == "low" and v["detail"] == "full in 1h 35m" and v["warn"] == "GRID DOWN"


def test_view_stale_and_offline_say_so():
    v = _run(f"W.view({json.dumps(dict(SAMPLE, stale=True, age_min=90))}, {{}})")
    assert v["level"] == "stale" and v["warn"] == "90m old"
    v = _run(f"W.view({json.dumps(SAMPLE)}, {{offline: true, cachedMin: 25}})")
    assert v["level"] == "stale" and v["warn"] == "offline · 25m"


def test_view_handles_missing_numbers_without_nan():
    g = dict(SAMPLE, soc_pct=None, solar_kw=None, load_kw=None, battery_state="idle", eta_empty_min=None)
    v = _run(f"W.view({json.dumps(g)}, {{}})")
    assert v["pct"] is None and v["frac"] == 0 and v["solar"] == "--" and v["detail"] == "idle"
    assert "NaN" not in json.dumps(v)


def test_inline_text_is_short_for_the_lock_screen():
    s = _run(f"W.inlineText(W.view({json.dumps(SAMPLE)}, {{}}))")
    assert s.startswith("52%") and len(s) <= 28


def test_grid_down_with_an_outage_line_leads_with_runtime_and_verdict():
    g = dict(SAMPLE, grid_status="down", outage_line="5h 28m left · 🚨 no sunrise")
    v = _run(f"W.view({json.dumps(g)}, {{}})")
    assert v["warn"] == "GRID DOWN · 5h 28m left" and v["detail"] == "🚨 no sunrise"
    assert v["warn"].startswith("GRID")                      # still drawn in the alarm colour


def test_grid_down_inline_text_keeps_the_runtime_on_the_lock_screen():
    g = dict(SAMPLE, grid_status="down", outage_line="lasts 24h+ · ✅ sunrise")
    s = _run(f"W.inlineText(W.view({json.dumps(g)}, {{}}))")
    assert "lasts 24h+" in s and len(s) <= 40


def test_grid_down_without_an_outage_line_is_the_old_behaviour():
    g = dict(SAMPLE, grid_status="down", outage_line=None)
    v = _run(f"W.view({json.dumps(g)}, {{}})")
    assert v["warn"] == "GRID DOWN" and v["detail"] == "empty in 12h 16m"


def test_outage_line_is_ignored_when_the_data_is_stale_or_the_grid_is_up():
    up = _run(f"W.view({json.dumps(dict(SAMPLE, outage_line='3h left'))}, {{}})")
    assert up["warn"] is None
    old = _run(f"W.view({json.dumps(dict(SAMPLE, grid_status='down', stale=True, age_min=40, outage_line='3h left'))}, {{}})")
    assert old["level"] == "stale" and old["warn"].startswith("GRID DOWN")
