"""Phone layout: bottom tabs, routines week grid, capped tall panels. Reads the HTML."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).parent.parent / "franklinwh_scraper" / "static" / "index.html").read_text()


def _panel_ids() -> list[str]:
    m = re.search(r"PANEL_IDS = (\[[^\]]*\])", HTML)
    return json.loads(m.group(1).replace("'", '"'))


def _tabs() -> dict:
    m = re.search(r"const PHONE_TABS = (\{.*?\});", HTML, re.S)
    assert m, "PHONE_TABS missing"
    js = re.sub(r"(\w+):", r'"\1":', m.group(1)).replace("'", '"')
    return json.loads(js)


def test_every_panel_is_on_exactly_one_phone_tab():
    tabs = _tabs()
    assert list(tabs) == ["now", "today", "money", "learned"]
    placed = [p for ids in tabs.values() for p in ids]
    assert sorted(placed) == sorted(_panel_ids()) and len(placed) == len(set(placed))


def test_tab_bar_only_on_phones_and_not_on_the_wall():
    assert 'id="tabbar"' in HTML or "bar.id = 'tabbar'" in HTML
    assert "@media (max-width:719px)" in HTML
    assert "body.wall #tabbar{display:none!important}" in HTML
    assert "env(safe-area-inset-bottom)" in HTML


def test_tab_choice_is_remembered_safely():
    block = HTML[HTML.index("function getPhoneTab"):][:400]
    assert "try" in block and "fwh-dash-tab" in block


def test_apply_panel_styles_respects_the_phone_tab():
    body = HTML[HTML.index("function applyPanelStyles"):][:1500]
    assert "phoneHides(" in body


def test_routines_show_as_a_week_grid_with_the_list_behind_details():
    assert "function renderHabitWeek" in HTML and 'id="hb-week"' in HTML
    assert "<details" in HTML[HTML.index("mkPanel('habits'"):][:1200]


def test_tall_panels_are_capped_with_a_show_more():
    assert ".capped" in HTML and "CAPPED_PANELS" in HTML
    assert "'sunpath'" in HTML[HTML.index("CAPPED_PANELS"):][:120]


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_week_grid_places_blocks_by_weekday_and_hour():
    src = HTML[HTML.index("function habitWeekCells"):]
    src = src[:src.index("\n}\n") + 3]
    js = src + """
    const cells = habitWeekCells([{weekday: 0, start: 21.31, hours: 0.54, kw: 2.76, name: null},
                                  {weekday: 6, start: 15.0, hours: 1.0, kw: 4.0, name: 'dryer'}]);
    console.log(JSON.stringify(cells));
    """
    out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    cells = json.loads(out.stdout)
    sun = [c for c in cells if c["col"] == 0][0]                      # Sunday is the first column
    mon = [c for c in cells if c["col"] == 1][0]
    assert sun["label"] == "dryer" and abs(sun["top"] - 15 / 24) < 1e-9 and abs(sun["height"] - 1 / 24) < 1e-9
    assert mon["label"] == "" and abs(mon["top"] - 21.31 / 24) < 1e-9
