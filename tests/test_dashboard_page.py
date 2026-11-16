"""Dashboard page structure: never-blank loading, per-panel failure, forecast source. Reads the HTML."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).parent.parent / "franklinwh_scraper" / "static" / "index.html").read_text()


def _css_rule(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\{([^}]*)\}", HTML)
    assert m, selector
    return m.group(1)


# ── cards can never be stuck invisible ───────────────────────────────────────────────────────────

def test_cards_are_visible_by_default_and_only_animate_with_the_enter_class():
    assert "animation" not in _css_rule(".card")                    # base rule: no opacity-0 starting state
    assert ".card.enter{" in HTML


def test_entry_animation_has_no_blur_filter():
    kf = re.search(r"@keyframes cardin\{(.*?)\}\}", HTML).group(1)
    assert "filter" not in kf and "opacity:0" in kf


def test_enter_class_is_removed_on_animationend_and_by_a_safety_timer():
    assert "animationend" in HTML and "classList.remove('enter')" in HTML
    assert re.search(r"setTimeout\(\s*\(\)\s*=>\s*document\.querySelectorAll\('\.enter'\)", HTML)


def test_hero_tiles_follow_the_same_rule():
    assert "animation" not in _css_rule(".hero-tile")


# ── slow panels load independently ───────────────────────────────────────────────────────────────

SLOW_URLS = ["/api/history?hours=48", "/api/forecast", "/api/accuracy", "/api/tou", "/api/bill",
             "/api/recommendation", "/api/health", "/api/battery-health", "/api/ev", "/api/attribution",
             "/api/savings?days=30", "/api/vpp", "/api/sundown", "/api/plan", "/api/habits"]


def test_every_slow_endpoint_is_its_own_task():
    block = HTML[HTML.index("const SLOW_TASKS"):HTML.index("async function pollSlow")]
    for url in SLOW_URLS:
        assert url in block, url


def test_pollslow_no_longer_waits_for_every_endpoint_before_painting():
    body = HTML[HTML.index("async function pollSlow"):]
    body = body[:body.index("\n}\n")]
    assert "SLOW_TASKS.map(runTask)" in body
    assert "J('/api/" not in body                                     # no batched fetches left in here


def test_panels_show_a_skeleton_then_a_retry_row_on_failure():
    for name in ("function markLoading", "function markLoaded", "function markFailed", ".skel{", ".pfail"):
        assert name in HTML, name
    assert "retry" in HTML[HTML.index("function markFailed"):][:900].lower()


def test_fetches_time_out():
    j = HTML[HTML.index("async function J(url)"):][:700]
    assert "AbortController" in j and "12000" in j


def test_forecast_panel_says_when_the_fallback_or_an_old_forecast_is_shown():
    assert "NWS FALLBACK" in HTML and "FORECAST FROM" in HTML and "forecast_source" in HTML


def test_numbers_that_d_does_not_cover_are_tweened():
    assert "function tweenNum" in HTML
    assert "prefers-reduced-motion" in HTML[HTML.index("function tweenNum"):][:600]


# ── logic checks that need a JS engine ───────────────────────────────────────────────────────────

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_tween_lands_exactly_on_the_target_value():
    src = HTML[HTML.index("function tweenNum"):]
    src = src[:src.index("\n}\n") + 3]
    js = f"""
    const frames = []; let t = 0;
    globalThis.performance = {{ now: () => t }};
    globalThis.requestAnimationFrame = f => frames.push(f);
    globalThis.matchMedia = () => ({{ matches: false }});
    {src}
    const el = {{ textContent: '$1.00' }};
    tweenNum(el, 2.5, v => '$' + v.toFixed(2));
    for (let i = 0; i < 40 && frames.length; i++) {{ t += 16; frames.shift()(t); }}
    console.log(el.textContent);
    tweenNum(el, 7, v => '$' + v.toFixed(2));                          // a second call starts from 2.50
    t += 1; frames.shift()(t);
    console.log(el.textContent);
    """
    out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=10)
    lines = out.stdout.split()
    assert out.returncode == 0, out.stderr
    assert lines[0] == "$2.50"
    assert 2.5 <= float(lines[1][1:]) < 3.0


# ── sun-path panel: a day whose readings all fall outside the chart window ───────────────────────

SUNPATH = (Path(__file__).parent.parent / "franklinwh_scraper" / "static" / "sunpath.js").read_text()


def test_measured_overlay_is_skipped_when_no_reading_falls_inside_the_window():
    """Regression: after midnight today's readings exist but all predate sunrise, so the filtered
    list was empty and pts[pts.length - 1].min threw on every frame. draw() then never reached
    drawYear, leaving the day and year charts blank all night."""
    act = SUNPATH[SUNPATH.index("if (act && act.length) {"):]
    act = act[:act.index("ctx.fillStyle = 'rgba(141,240,176,.16)'")]
    assert "if (!pts.length)" in act or "if (pts.length" in act


def test_one_failing_chart_cannot_blank_the_others():
    draw = SUNPATH[SUNPATH.index("function draw()"):]
    draw = draw[:draw.index("\n}\n")]
    assert "for (const f of [drawScene, drawDay, drawYear, updateChips])" in draw
    assert "try { f(); }" in draw


def test_sunpath_script_tag_carries_the_files_own_hash():
    """Browsers cached sunpath.js?v=5 across a fix, so the page kept running the broken copy.
    The tag is the file's sha1 (8 chars): editing sunpath.js without updating it fails here."""
    import hashlib
    want = hashlib.sha1(SUNPATH.encode()).hexdigest()[:8]
    assert f'<script src="/sunpath.js?v={want}"></script>' in HTML
