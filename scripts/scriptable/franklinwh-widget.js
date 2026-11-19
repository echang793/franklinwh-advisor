// FranklinWH battery widget for Scriptable (iOS). Reads /api/glance from the dashboard.
//
// Setup: paste this into a new Scriptable script, run it once in the app and enter the
// dashboard address and dashboard_token when asked (they are stored in the iOS Keychain,
// never in this file). Then add a Scriptable widget (Home Screen small/medium, or Lock
// Screen circular/rectangular/inline) and pick this script. See RUNBOOK.md.
//
// The pure view logic at the top is unit-tested with node (tests/test_scriptable_widget.py);
// everything that touches Scriptable APIs is in main(), which only runs inside Scriptable.

const DEFAULT_HOST = "http://100.77.88.77:8093";
const KEY_HOST = "fwh.host";
const KEY_TOKEN = "fwh.token";
const CACHE_FILE = "fwh-glance-cache.json";
const REFRESH_MIN = 15;
const LOW_PCT = 20;
const MID_PCT = 40;

function fmtEta(min) {
  if (min === null || min === undefined || !isFinite(min)) return null;
  const m = Math.max(0, Math.round(min));
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function fmtKw(x) {
  return (x === null || x === undefined || !isFinite(x)) ? "--" : `${Number(x).toFixed(1)} kW`;
}

// g = /api/glance payload; opts = {offline: bool, cachedMin: minutes since the cached copy}
function view(g, opts) {
  opts = opts || {};
  const pct = (g && isFinite(g.soc_pct) && g.soc_pct !== null) ? Math.round(g.soc_pct) : null;
  const state = (g && g.battery_state) || "idle";
  let detail = "idle";
  if (state === "charging") detail = fmtEta(g.eta_full_min) ? `full in ${fmtEta(g.eta_full_min)}` : "charging";
  else if (state === "discharging") detail = fmtEta(g.eta_empty_min) ? `empty in ${fmtEta(g.eta_empty_min)}` : "discharging";

  let warn = null;
  const grid = g && g.grid_status;
  if (grid && grid !== "normal" && grid !== "unknown") warn = grid === "down" ? "GRID DOWN" : `GRID ${String(grid).toUpperCase()}`;
  // Grid down: the server's outage line ("5h 28m left · 🚨 no sunrise") answers "how long do I have?"
  if (warn === "GRID DOWN" && g.outage_line && !g.stale && !opts.offline) {
    const [runtime, ...verdict] = String(g.outage_line).split(" · ");
    warn = `GRID DOWN · ${runtime}`;
    if (verdict.length) detail = verdict.join(" · ");
  }
  let level = pct === null ? "stale" : pct < LOW_PCT ? "low" : pct < MID_PCT ? "mid" : "ok";
  if (opts.offline) {
    level = "stale";
    warn = `offline · ${fmtEta(opts.cachedMin) || "?"}`;
  } else if (g && g.stale) {
    level = "stale";
    warn = warn || `${g.age_min ?? "?"}m old`;
  }
  return {
    pct, frac: pct === null ? 0 : Math.min(1, Math.max(0, pct / 100)),
    state, detail, warn, level,
    icon: state === "charging" ? "⚡" : state === "discharging" ? "🔋" : "·",
    solar: fmtKw(g && g.solar_kw), load: fmtKw(g && g.load_kw),
    solarToday: (g && isFinite(g.solar_today_kwh) && g.solar_today_kwh !== null) ? `${Number(g.solar_today_kwh).toFixed(1)} kWh today` : null,
  };
}

function inlineText(v) {
  const head = `${v.pct === null ? "--" : v.pct}% ${v.icon}`;
  return v.warn ? `${head} ${v.warn}` : `${head} ${v.detail}`;
}

const COLORS = { ok: "#4ade80", mid: "#fbbf24", low: "#f87171", stale: "#8a8a96" };

if (typeof module !== "undefined") module.exports = { fmtEta, fmtKw, view, inlineText, COLORS };

// ---------------------------------------------------------------- Scriptable only below

async function askSettings() {
  const a = new Alert();
  a.title = "FranklinWH widget";
  a.message = "Dashboard address (Tailscale) and dashboard_token from ~/.franklinwh.json. Stored in the Keychain.";
  a.addTextField("http://100.x.x.x:8093", Keychain.contains(KEY_HOST) ? Keychain.get(KEY_HOST) : DEFAULT_HOST);
  a.addSecureTextField("dashboard_token", "");
  a.addAction("Save");
  a.addCancelAction("Cancel");
  if (await a.presentAlert() === -1) return false;
  const host = a.textFieldValue(0).trim().replace(/\/+$/, "");
  const token = a.textFieldValue(1).trim();
  if (host) Keychain.set(KEY_HOST, host);
  if (token) Keychain.set(KEY_TOKEN, token);
  return true;
}

function cachePath() {
  const fm = FileManager.local();
  return [fm, fm.joinPath(fm.documentsDirectory(), CACHE_FILE)];
}

async function fetchGlance() {
  const host = Keychain.contains(KEY_HOST) ? Keychain.get(KEY_HOST) : DEFAULT_HOST;
  const [fm, path] = cachePath();
  try {
    const req = new Request(`${host}/api/glance`);
    req.headers = { "X-Dashboard-Token": Keychain.contains(KEY_TOKEN) ? Keychain.get(KEY_TOKEN) : "" };
    req.timeoutInterval = 8;
    const g = await req.loadJSON();
    if (req.response.statusCode !== 200 || !g || g.ok !== true) throw new Error(`HTTP ${req.response.statusCode}`);
    fm.writeString(path, JSON.stringify({ at: Date.now(), g }));
    return { g, opts: {} };
  } catch (e) {
    if (fm.fileExists(path)) {
      const c = JSON.parse(fm.readString(path));
      return { g: c.g, opts: { offline: true, cachedMin: (Date.now() - c.at) / 60000 } };
    }
    return { g: null, opts: { offline: true, cachedMin: null }, error: String(e) };
  }
}

function ring(v, size, width) {
  const dc = new DrawContext();
  dc.size = new Size(size, size);
  dc.opaque = false;
  dc.respectScreenScale = true;
  const c = size / 2, r = c - width / 2 - 1;
  const arc = (from, to, color) => {
    const p = new Path();
    const steps = Math.max(2, Math.ceil((to - from) * 120));
    for (let i = 0; i <= steps; i++) {
      const t = from + (to - from) * i / steps, a = -Math.PI / 2 + t * 2 * Math.PI;
      const pt = new Point(c + r * Math.cos(a), c + r * Math.sin(a));
      i === 0 ? p.move(pt) : p.addLine(pt);
    }
    dc.addPath(p);
    dc.setStrokeColor(color);
    dc.setLineWidth(width);
    dc.strokePath();
  };
  arc(0, 1, new Color("#ffffff", 0.15));
  if (v.frac > 0) arc(0, v.frac, new Color(COLORS[v.level]));
  return dc.getImage();
}

function text(stack, s, font, color, opacity) {
  const t = stack.addText(s);
  t.font = font;
  t.textColor = color || Color.white();
  if (opacity !== undefined) t.textOpacity = opacity;
  t.lineLimit = 1;
  t.minimumScaleFactor = 0.6;
  return t;
}

function buildWidget(v, family, host) {
  const w = new ListWidget();
  w.refreshAfterDate = new Date(Date.now() + REFRESH_MIN * 60000);
  w.url = host;
  const accent = new Color(COLORS[v.level]);
  const pctStr = v.pct === null ? "--" : `${v.pct}`;

  if (family === "accessoryInline") {
    text(w, inlineText(v), Font.systemFont(12));
    return w;
  }
  if (family === "accessoryCircular") {
    w.addAccessoryWidgetBackground = true;
    const s = w.addStack();
    s.size = new Size(58, 58);
    s.backgroundImage = ring(v, 58, 5);
    s.centerAlignContent();
    text(s, pctStr, Font.boldRoundedSystemFont(17));
    return w;
  }
  if (family === "accessoryRectangular") {
    text(w, `${pctStr}% ${v.icon}  ${v.warn || v.detail}`, Font.boldSystemFont(14));
    text(w, `☀ ${v.solar}   🏠 ${v.load}`, Font.systemFont(12), null, 0.85);
    if (v.solarToday) text(w, v.solarToday, Font.systemFont(11), null, 0.7);
    return w;
  }

  // Home Screen small / medium / large
  w.backgroundColor = new Color("#0c0c10");
  w.setPadding(14, 14, 14, 14);
  const top = w.addStack();
  top.centerAlignContent();
  const ringStack = top.addStack();
  ringStack.size = new Size(62, 62);
  ringStack.backgroundImage = ring(v, 62, 6);
  ringStack.centerAlignContent();
  text(ringStack, `${pctStr}%`, Font.boldRoundedSystemFont(16));
  top.addSpacer(10);
  const right = top.addStack();
  right.layoutVertically();
  text(right, `${v.icon} ${v.state}`, Font.semiboldSystemFont(13), accent);
  text(right, v.detail, Font.systemFont(12), null, 0.8);
  if (family !== "small") {
    right.addSpacer(4);
    text(right, `☀ ${v.solar}   🏠 ${v.load}`, Font.systemFont(13));
    if (v.solarToday) text(right, v.solarToday, Font.systemFont(11), null, 0.6);
  }
  w.addSpacer();
  if (family === "small") text(w, `☀ ${v.solar}  🏠 ${v.load}`, Font.systemFont(11), null, 0.85);
  const foot = w.addStack();
  if (v.warn) text(foot, `⚠ ${v.warn}`, Font.semiboldSystemFont(11), new Color(v.warn.startsWith("GRID") ? COLORS.low : COLORS.mid));
  else {
    const d = foot.addDate(new Date());
    d.applyTimeStyle();
    d.font = Font.systemFont(10);
    d.textColor = Color.white();
    d.textOpacity = 0.45;
  }
  return w;
}

async function main() {
  if (config.runsInApp && !Keychain.contains(KEY_TOKEN)) {
    if (!(await askSettings())) return;
  }
  const host = Keychain.contains(KEY_HOST) ? Keychain.get(KEY_HOST) : DEFAULT_HOST;
  const { g, opts, error } = await fetchGlance();
  const v = g ? view(g, opts)
    : { ...view({}, opts), detail: "no data yet", warn: /HTTP 401/.test(error) ? "token rejected" : "can't reach dashboard" };

  if (config.runsInWidget) {
    Script.setWidget(buildWidget(v, config.widgetFamily || "small", host));
    Script.complete();
    return;
  }
  const menu = new Alert();
  menu.title = "FranklinWH widget";
  menu.message = error ? `Fetch failed: ${error}` : inlineText(v);
  ["Preview small", "Preview medium", "Change address / token"].forEach(x => menu.addAction(x));
  menu.addCancelAction("Done");
  const pick = await menu.presentSheet();
  if (pick === 0) await buildWidget(v, "small", host).presentSmall();
  if (pick === 1) await buildWidget(v, "medium", host).presentMedium();
  if (pick === 2) await askSettings();
  Script.complete();
}

if (typeof Script !== "undefined") await main();
