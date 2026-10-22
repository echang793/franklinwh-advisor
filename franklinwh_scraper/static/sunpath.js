/* Sun-path panel: isometric house, sun arc, per-roof-face output, day + year charts.
 * The sun/PV math mirrors franklinwh_scraper/sunpath.py (NOAA position, Meinel clear sky)
 * so scrubbing and the year sweep need no network. The server supplies the house config
 * (location, fitted roof faces), per-day UTC offsets/noon, and measured data.
 * index.html loads this first, then calls SunPath.mount(panelSection). */
(function () {
'use strict';
if (!CanvasRenderingContext2D.prototype.roundRect) CanvasRenderingContext2D.prototype.roundRect = function (x, y, w, h) { this.rect(x, y, w, h); };
const RAD = Math.PI / 180, ALBEDO = 0.2, SUNRISE_EL = -0.833;
const MONTHS = ['J', 'F', 'M', 'A', 'M', 'J', 'J', 'A', 'S', 'O', 'N', 'D'];
const MONO = "'JetBrains Mono',monospace";
const C = { green: '#8df0b0', amber: '#f5b942', blue: '#8fb4ff', violet: '#c79bff', text: '#9db8a2',
            muted: '#5f7263', line: '#1c2a1e', bright: '#e6f2e8', red: '#ff8a7a' };
const SPEEDS = { slow: 90, med: 240, fast: 720 };   // simulated minutes per second
const PITCH = 27 * RAD, CP = Math.cos(PITCH), SP = Math.sin(PITCH);
const DOME_R = 19, GROUND_R = 14;
const FRONT_AZ = 220;                                    // compass direction the front (garage/door side) faces
const HOUSE = { w: 8, d: 12.5, floor: 3 };               // front width, depth, storey height (3 storeys)
const STUCCO = [188, 156, 120], TRIM = [218, 200, 170], STONE = [150, 132, 108], DOOR = [78, 50, 34],
      GARAGE = [96, 74, 58], ROOF = [84, 70, 62], CONCRETE = [176, 168, 154];

const CSS = `
.sp-root{display:flex;flex-direction:column;gap:10px;margin-top:12px}
.sp-scene{width:100%;display:block;border-radius:12px;background:#0a110b}
.sp-chips{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}
.sp-chip{background:#0c130d;border:1px solid #1c2a1e;border-radius:10px;padding:7px 9px;min-width:0}
.sp-chip .k{font:10px ${MONO};letter-spacing:.14em;color:#5f7263}
.sp-chip .v{font:600 15px ${MONO};color:#e6f2e8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.sp-chip .v small{font-size:11px;color:#9db8a2;font-weight:400}
.sp-chip .x{font:11px ${MONO};color:#9db8a2;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sp-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.sp-btn{min-height:34px;padding:0 12px;border-radius:999px;border:1px solid #24402b;background:#0c130d;color:#9db8a2;font:600 11px ${MONO};letter-spacing:.08em;cursor:pointer}
.sp-btn:hover{border-color:#3b6a48;color:#e6f2e8}
.sp-btn[aria-pressed=true]{background:#17331f;border-color:#57e389;color:#8df0b0}
.sp-btn:focus-visible,.sp-slider:focus-visible{outline:2px solid #8fb4ff;outline-offset:2px}
.sp-slider{flex:1 1 180px;min-width:140px;accent-color:#57e389;height:34px}
.sp-date{font:12px ${MONO};color:#9db8a2;min-width:128px}
.sp-chart{width:100%;display:block;border-radius:10px;background:#0c130d;border:1px solid #1c2a1e;touch-action:pan-y;cursor:crosshair}
.sp-cap{font:10px ${MONO};letter-spacing:.14em;color:#5f7263;margin-bottom:-4px}
.sp-err{font:12px ${MONO};color:#ff8a7a;padding:6px 2px}
@media (max-width:520px){.sp-chips{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (prefers-reduced-motion:reduce){.sp-btn{transition:none}}
`;

const HTML = `
<div class="plabel">SUN PATH · SOLAR GEOMETRY</div>
<div class="sp-root">
  <canvas class="sp-scene" data-sp="scene" role="img" aria-label="Isometric house with the sun's path and solar output"></canvas>
  <div class="sp-err" data-sp="err" hidden></div>
  <div class="sp-chips" data-sp="chips" aria-live="off"></div>
  <div class="sp-row">
    <button class="sp-btn" data-sp="live" aria-pressed="true">● LIVE</button>
    <button class="sp-btn" data-sp="play" aria-pressed="false">▶ PLAY DAY</button>
    <span class="sp-row" role="group" aria-label="Playback speed">
      <button class="sp-btn" data-sp="spd" data-v="slow" aria-pressed="false">1×</button>
      <button class="sp-btn" data-sp="spd" data-v="med" aria-pressed="true">3×</button>
      <button class="sp-btn" data-sp="spd" data-v="fast" aria-pressed="false">8×</button>
    </span>
    <button class="sp-btn" data-sp="sweep" aria-pressed="false">⟳ YEAR SWEEP</button>
  </div>
  <div class="sp-row">
    <input class="sp-slider" data-sp="slider" type="range" min="1" max="365" step="1" value="1" aria-label="Day of year">
    <span class="sp-date" data-sp="date"></span>
  </div>
  <div class="sp-row" role="group" aria-label="Jump to season">
    <button class="sp-btn" data-sp="jump" data-m="11" data-d="21">WINTER</button>
    <button class="sp-btn" data-sp="jump" data-m="2" data-d="20">SPRING</button>
    <button class="sp-btn" data-sp="jump" data-m="5" data-d="21">SUMMER</button>
    <button class="sp-btn" data-sp="jump" data-m="8" data-d="22">FALL</button>
    <button class="sp-btn" data-sp="jump" data-m="today">TODAY</button>
  </div>
  <div class="sp-cap">THE DAY · KW BY TIME</div>
  <canvas class="sp-chart" data-sp="day" role="img" aria-label="Solar output by time of day"></canvas>
  <div class="sp-cap">THE YEAR · KWH PER CLEAR DAY</div>
  <canvas class="sp-chart" data-sp="year" role="img" aria-label="Daily solar energy across the year"></canvas>
</div>`;

// ── sun + PV model (mirror of sunpath.py) ─────────────────────────────
function sunPos(utcMs, lat, lon) {
  const jd = utcMs / 86400000 + 2440587.5, t = (jd - 2451545) / 36525;
  const l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360;
  const m = 357.52911 + t * (35999.05029 - 0.0001537 * t);
  const e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t);
  const sm = Math.sin(m * RAD);
  const c = sm * (1.914602 - t * (0.004817 + 0.000014 * t)) + Math.sin(2 * m * RAD) * (0.019993 - 0.000101 * t)
          + Math.sin(3 * m * RAD) * 0.000289;
  const omega = 125.04 - 1934.136 * t;
  const lam = l0 + c - 0.00569 - 0.00478 * Math.sin(omega * RAD);
  const eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60;
  const eps = eps0 + 0.00256 * Math.cos(omega * RAD);
  const decl = Math.asin(Math.sin(eps * RAD) * Math.sin(lam * RAD));
  const y = Math.tan(eps * RAD / 2) ** 2;
  const eot = 4 / RAD * (y * Math.sin(2 * l0 * RAD) - 2 * e * sm + 4 * e * y * sm * Math.cos(2 * l0 * RAD)
            - 0.5 * y * y * Math.sin(4 * l0 * RAD) - 1.25 * e * e * Math.sin(2 * m * RAD));
  const minUtc = ((utcMs / 60000) % 1440 + 1440) % 1440;
  const tst = (((minUtc + eot + 4 * lon) % 1440) + 1440) % 1440;   // JS % keeps the sign: normalise like Python
  const ha = tst / 4 - 180, latR = lat * RAD;
  let cz = Math.sin(latR) * Math.sin(decl) + Math.cos(latR) * Math.cos(decl) * Math.cos(ha * RAD);
  cz = Math.max(-1, Math.min(1, cz));
  const zen = Math.acos(cz), el = 90 - zen / RAD, sz = Math.sin(zen);
  if (sz < 1e-9) return { az: 180, el };
  let az = Math.acos(Math.max(-1, Math.min(1, (Math.sin(latR) * cz - Math.sin(decl)) / (Math.cos(latR) * sz)))) / RAD;
  az = ha > 0 ? (az + 180) % 360 : (540 - az) % 360;
  return { az, el };
}
function clearSky(el) {
  if (el <= 0) return [0, 0, 0];
  const zen = 90 - el, cz = Math.cos(zen * RAD);
  const am = 1 / (cz + 0.50572 * Math.pow(96.07995 - zen, -1.6364));
  const dni = 1353 * ((1 - 0.014) * Math.pow(0.7, Math.pow(am, 0.678)) + 0.014);
  return [dni, 0.1 * dni, dni * cz + 0.1 * dni];
}
function incCos(sAz, sEl, fAz, tilt) {
  return Math.sin(sEl * RAD) * Math.cos(tilt * RAD) + Math.cos(sEl * RAD) * Math.sin(tilt * RAD) * Math.cos((sAz - fAz) * RAD);
}
function poaFrac(sky, az, el, fAz, tilt) {      // plane-of-array irradiance / 1000
  if (el <= 0) return 0;
  const t = tilt * RAD;
  return (sky[0] * Math.max(0, incCos(az, el, fAz, tilt)) + sky[1] * (1 + Math.cos(t)) / 2
        + sky[2] * ALBEDO * (1 - Math.cos(t)) / 2) / 1000;
}

// ── state ─────────────────────────────────────────────────────────────
const st = {
  cfg: null, days: null, year: new Date().getFullYear(), doy: 1, minute: 720,
  live: true, playing: false, speed: 'med', sweeping: false, sweepStart: null, sweepF: 0,
  dragging: false, derived: new Map(), actual: new Map(), inflight: new Set(), err: null,
};
let root, els = {}, ctxs = {}, raf = 0, timer = 0, lastTs = 0, active = true, fetchT = 0;
let lastChips = '', yearLoaded = 0;

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const wrapDoy = n => ((Math.round(n) - 1) % 365 + 365) % 365 + 1;
const dayRow = d => st.days && st.days[clamp(d, 1, st.days.length) - 1];
const isoOf = d => new Date(Date.UTC(st.year, 0, d)).toISOString().slice(0, 10);
const doyOf = (m, d) => Math.round((Date.UTC(st.year, m, d) - Date.UTC(st.year, 0, 1)) / 864e5) + 1;
function fmtTime(min) {
  const m = ((Math.round(min) % 1440) + 1440) % 1440, h = Math.floor(m / 60), mm = String(m % 60).padStart(2, '0');
  return `${h % 12 || 12}:${mm} ${h < 12 ? 'AM' : 'PM'}`;
}
function fmtDate(d) {
  return new Date(Date.UTC(st.year, 0, d)).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' });
}
const DIRS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
const dirName = az => DIRS[Math.round(az / 22.5) % 16];
function seasonTag(d) {
  const tags = [[doyOf(11, 21), 'Winter solstice'], [doyOf(2, 20), 'Spring equinox'], [doyOf(5, 21), 'Summer solstice'], [doyOf(8, 22), 'Fall equinox']];
  const hit = tags.find(([k]) => Math.abs(k - d) <= 1);
  return hit ? hit[1] : '';
}

function at(doy, minute) {
  const row = dayRow(doy), cfg = st.cfg;
  const utc = Date.UTC(st.year, 0, doy) + (minute - row.utc_offset_min) * 60000;
  const { az, el } = sunPos(utc, cfg.lat, cfg.lon), sky = clearSky(el);
  const fr = cfg.faces.map(f => poaFrac(sky, az, el, f.azimuth, f.tilt));
  const kws = cfg.faces.map((f, i) => cfg.scale_kw * f.weight * fr[i]);
  return { az, el, fr, kws, kw: kws.reduce((a, b) => a + b, 0) };
}
function derived(doy) {            // per-day series at 5-min steps, cached
  let d = st.derived.get(doy);
  if (d) return d;
  const n = 288, series = [];
  let kwh = 0, peak = 0, rise = null, set = null, prev = null;
  for (let i = 0; i < n; i++) {
    const s = at(doy, i * 5);
    s.min = i * 5; series.push(s);
    kwh += s.kw * 5 / 60; peak = Math.max(peak, s.kw);
    if (prev) {
      if (rise === null && prev.el <= SUNRISE_EL && s.el > SUNRISE_EL) rise = prev.min + 5 * (SUNRISE_EL - prev.el) / (s.el - prev.el);
      if (prev.el > SUNRISE_EL && s.el <= SUNRISE_EL) set = prev.min + 5 * (prev.el - SUNRISE_EL) / (prev.el - s.el);
    }
    prev = s;
  }
  d = { series, kwh, peak, rise, set };
  if (st.derived.size > 400) st.derived.clear();
  st.derived.set(doy, d);
  return d;
}
const noonOf = doy => derived(doy).series.reduce((a, b) => b.el > a.el ? b : a).min;   // 5-min precision
function liveMoment() {
  if (typeof DEMO !== 'undefined' && DEMO && typeof S !== 'undefined') {
    const t = S.simTime;
    return { doy: doyOf(t.getMonth(), t.getDate()), minute: t.getHours() * 60 + t.getMinutes() + t.getSeconds() / 60 };
  }
  const now = Date.now(), approx = new Date(now);
  const doy = doyOf(approx.getUTCMonth(), approx.getUTCDate());
  const off = dayRow(doy).utc_offset_min;
  const local = new Date(now + off * 60000);       // house-local wall clock, independent of viewer timezone
  return { doy: doyOf(local.getUTCMonth(), local.getUTCDate()),
           minute: local.getUTCHours() * 60 + local.getUTCMinutes() + local.getUTCSeconds() / 60 };
}

// ── data ──────────────────────────────────────────────────────────────
async function loadYear() {
  try {
    const j = await J('/api/sunpath/year');
    if (!j || !j.days || !j.days.length || !j.config) throw new Error('empty sun-path response');
    st.cfg = j.config; st.days = j.days; st.year = j.year; st.err = null; st.derived.clear(); yearLoaded = Date.now();
    if (st.live) { const m = liveMoment(); st.doy = m.doy; st.minute = m.minute; }
    syncUI(); fetchDay(st.doy, true);
  } catch (e) {
    st.err = 'Sun model unavailable — retrying';
    setTimeout(loadYear, 20000);
  }
  requestDraw();
}
function fetchDay(doy, force) {
  if (!st.days || st.inflight.has(doy)) return;
  const c = st.actual.get(doy);
  if (c && !force && Date.now() - c.at < 5 * 60000) return;
  st.inflight.add(doy);
  J('/api/sunpath?date=' + isoOf(doy)).then(j => {
    st.actual.set(doy, { at: Date.now(), actual: j.actual || null });
  }).catch(() => { st.actual.set(doy, { at: Date.now(), actual: null }); })
    .finally(() => { st.inflight.delete(doy); requestDraw(); });
}
function scheduleFetch() { clearTimeout(fetchT); fetchT = setTimeout(() => fetchDay(st.doy), 150); }

// ── canvas helpers ────────────────────────────────────────────────────
function fit(cv, cssH) {
  const dpr = Math.min(window.devicePixelRatio || 1, 3), w = Math.max(200, cv.clientWidth || 300);
  const bw = Math.round(w * dpr), bh = Math.round(cssH * dpr);
  if (cv.width !== bw || cv.height !== bh) { cv.width = bw; cv.height = bh; cv.style.height = cssH + 'px'; }
  const ctx = cv.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h: cssH };
}
const hex = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
const mix = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
const rgb = a => `rgb(${a[0]},${a[1]},${a[2]})`;
const SKY = [[-14, '#050810', '#0a1020'], [-3, '#161a36', '#5a3550'], [4, '#37477f', '#f0975a'],
             [18, '#2a5a98', '#86b8e4'], [50, '#1d58a6', '#6db2e6']];
function skyColors(el) {
  if (el <= SKY[0][0]) return [hex(SKY[0][1]), hex(SKY[0][2])];
  for (let i = 1; i < SKY.length; i++) if (el <= SKY[i][0]) {
    const t = (el - SKY[i - 1][0]) / (SKY[i][0] - SKY[i - 1][0]);
    return [mix(hex(SKY[i - 1][1]), hex(SKY[i][1]), t), mix(hex(SKY[i - 1][2]), hex(SKY[i][2]), t)];
  }
  const l = SKY[SKY.length - 1]; return [hex(l[1]), hex(l[2])];
}
const STARS = Array.from({ length: 70 }, (_, i) => { const r = Math.sin(i * 91.7) * 43758.5; const q = Math.sin(i * 12.3) * 9301.1;
  return [r - Math.floor(r), q - Math.floor(q), 0.5 + ((i * 37) % 10) / 14]; });

// ── scene ─────────────────────────────────────────────────────────────
function drawScene() {
  const cv = els.scene, W0 = cv.clientWidth || 320, H0 = clamp(W0 * 0.74, 260, 480);
  const { ctx, w: W, h: H } = fit(cv, H0);
  if (!st.cfg) {
    ctx.fillStyle = '#0a110b'; ctx.fillRect(0, 0, W, H); ctx.fillStyle = C.muted; ctx.font = `12px ${MONO}`;
    ctx.textAlign = 'center'; ctx.fillText(st.err || 'Loading sun model…', W / 2, H / 2); return;
  }
  const cur = at(st.doy, st.minute), dd = derived(st.doy), cfg = st.cfg;
  const sc = Math.min(W / 38, H / 26), ox = W / 2, oy = H - 8.2 * sc;
  const P = (x, y, z) => [ox + x * sc, oy - (z * CP + y * SP) * sc];
  const el = cur.el, azR = cur.az * RAD;
  const sd = [Math.sin(azR) * Math.cos(el * RAD), Math.cos(azR) * Math.cos(el * RAD), Math.sin(el * RAD)];
  const day = clamp((el + 6) / 16, 0, 1);              // 0 night … 1 full day

  // sky
  const [top, bot] = skyColors(el);
  ctx.save(); ctx.beginPath(); ctx.roundRect(0, 0, W, H, 12); ctx.clip();
  const g = ctx.createLinearGradient(0, 0, 0, H); g.addColorStop(0, rgb(top)); g.addColorStop(1, rgb(bot));
  ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
  const night = clamp((-el - 2) / 8, 0, 1);
  if (night > 0) { ctx.fillStyle = '#fff'; for (const s of STARS) { ctx.globalAlpha = night * s[2] * 0.7;
    ctx.fillRect(s[0] * W, s[1] * H * 0.62, 1.4, 1.4); } ctx.globalAlpha = 1; }

  // ground disc + compass
  const ring = r => { ctx.beginPath(); for (let i = 0; i <= 72; i++) { const a = i / 72 * 6.2832, p = P(Math.sin(a) * r, Math.cos(a) * r, 0);
    i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]); } ctx.closePath(); };
  ring(GROUND_R);
  const gg = ctx.createLinearGradient(0, P(0, GROUND_R, 0)[1], 0, P(0, -GROUND_R, 0)[1]);
  const lift = 0.35 + 0.65 * day; gg.addColorStop(0, rgb(mix([10, 18, 12], [24, 44, 28], lift))); gg.addColorStop(1, rgb(mix([8, 14, 10], [32, 58, 36], lift)));
  ctx.fillStyle = gg; ctx.fill(); ctx.strokeStyle = 'rgba(87,227,137,.22)'; ctx.lineWidth = 1.2; ctx.stroke();
  ctx.strokeStyle = 'rgba(255,255,255,.06)'; ctx.lineWidth = 1; ring(5); ctx.stroke(); ring(9); ctx.stroke();
  ctx.font = `600 11px ${MONO}`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  [['N', 0, 1, C.red], ['E', 1, 0, C.text], ['S', 0, -1, C.text], ['W', -1, 0, C.text]].forEach(([t, x, y, col]) => {
    const p = P(x * 15.8, y * 15.8, 0); ctx.fillStyle = col; ctx.fillText(t, p[0], p[1]); });

  // sun arcs: ghost solstices, then the selected day. The part of an arc north of the house is drawn before it
  // (hidden behind), the part south of it after it (in front, nearer the camera) — otherwise the path seems to cut through the house.
  const arcPts = (series, stopMin) => series.filter(s => s.el > 0 && (stopMin === undefined || s.min <= stopMin)).map(s => {
    const a = s.az * RAD, ce = Math.cos(s.el * RAD);
    return { p: P(Math.sin(a) * ce * DOME_R, Math.cos(a) * ce * DOME_R, Math.sin(s.el * RAD) * DOME_R), y: Math.cos(a), min: s.min }; });
  const strokeArc = (pts, front, style, lw, dash) => {
    let run = [];
    const flush = () => { if (run.length > 1) { ctx.beginPath(); run.forEach((q, i) => i ? ctx.lineTo(q.p[0], q.p[1]) : ctx.moveTo(q.p[0], q.p[1]));
      ctx.setLineDash(dash || []); ctx.strokeStyle = style; ctx.lineWidth = lw; ctx.stroke(); ctx.setLineDash([]); } run = []; };
    pts.forEach((q, i) => {
      if ((q.y <= 0) === front) { if (!run.length && i > 0) run.push(pts[i - 1]); run.push(q); }
      else { if (run.length) run.push(q); flush(); }
    });
    flush();
  };
  const drawArcs = front => {
    ctx.font = `9px ${MONO}`; ctx.textAlign = 'left';
    [[doyOf(5, 21), 'JUN 21'], [doyOf(11, 21), 'DEC 21']].forEach(([d, lab]) => {
      const pts = arcPts(derived(d).series); strokeArc(pts, front, 'rgba(255,255,255,.14)', 1, [2, 4]);
      if (!front && pts.length) { const hi = pts.reduce((a, b) => b.p[1] < a.p[1] ? b : a); ctx.fillStyle = 'rgba(255,255,255,.38)'; ctx.fillText(lab, hi.p[0] + 6, hi.p[1] - 4); } });
    strokeArc(arcPts(dd.series), front, 'rgba(245,185,66,.42)', 1.5, [5, 5]);
    strokeArc(arcPts(dd.series, st.minute), front, 'rgba(245,185,66,.95)', 2.2);
  };
  drawArcs(false);

  // sun / moon behind the house when north of it
  const sunXYZ = [sd[0] * DOME_R, sd[1] * DOME_R, sd[2] * DOME_R], sp = P(...sunXYZ);
  const drawSun = () => {
    if (el > 0) {                                           // drop line + ground marker: shows how far in front of / behind the house the sun is
      const foot = P(sunXYZ[0], sunXYZ[1], 0);
      ctx.beginPath(); ctx.moveTo(sp[0], sp[1]); ctx.lineTo(foot[0], foot[1]); ctx.setLineDash([2, 4]); ctx.strokeStyle = 'rgba(245,185,66,.4)'; ctx.lineWidth = 1; ctx.stroke(); ctx.setLineDash([]);
      ctx.beginPath(); ctx.ellipse(foot[0], foot[1], 6, 6 * SP, 0, 0, 6.2832); ctx.fillStyle = 'rgba(245,185,66,.5)'; ctx.fill();
      ctx.strokeStyle = 'rgba(245,185,66,.9)'; ctx.lineWidth = 1; ctx.stroke();
    }
    if (el > -1) {
      const col = el < 4 ? '#ff9a4a' : el < 12 ? '#ffc15e' : '#fff3b0', gl = ctx.createRadialGradient(sp[0], sp[1], 0, sp[0], sp[1], 46);
      gl.addColorStop(0, col + 'cc'); gl.addColorStop(0.35, col + '44'); gl.addColorStop(1, col + '00');
      ctx.fillStyle = gl; ctx.fillRect(sp[0] - 50, sp[1] - 50, 100, 100);
      ctx.beginPath(); ctx.arc(sp[0], sp[1], 9, 0, 6.2832); ctx.fillStyle = col; ctx.fill();
    }
  };
  if (night > 0.2) { const mp = P(-sd[0] * DOME_R, -sd[1] * DOME_R, Math.max(2, -sd[2] * DOME_R));
    ctx.globalAlpha = night; ctx.beginPath(); ctx.arc(mp[0], mp[1], 6.5, 0, 6.2832); ctx.fillStyle = '#dfe6f5'; ctx.fill(); ctx.globalAlpha = 1; }
  if (sd[1] > 0) drawSun();

  // ── house: three storeys, front facing FRONT_AZ, low hip roof. Local frame: a = along the front (viewer's right),
  //    b = out from the front. The long side roof (~SE) carries most panels, the front hip roof (~SW) the rest.
  const Wh = HOUSE.w / 2, Dh = HOUSE.d / 2, FL = HOUSE.floor, TOP = FL * 3, OVER = 0.45;
  const tv = [Math.sin((FRONT_AZ - 90) * RAD), Math.cos((FRONT_AZ - 90) * RAD)], nv = [Math.sin(FRONT_AZ * RAD), Math.cos(FRONT_AZ * RAD)];
  const tanR = Math.tan(cfg.faces[0].tilt * RAD), Zr = TOP + Wh * tanR, br = Dh - Wh, A = Wh + OVER, B = Dh + OVER, ze = Zr - A * tanR;
  const w3 = (a, b, z) => [a * tv[0] + b * nv[0], a * tv[1] + b * nv[1], z];
  const PP = (a, b, z) => P(...w3(a, b, z));
  const hn = az => [Math.sin(az * RAD), Math.cos(az * RAD), 0];
  const poly = (pts, fill, stroke, lw) => { ctx.beginPath(); pts.forEach((q, i) => i ? ctx.lineTo(q[0], q[1]) : ctx.moveTo(q[0], q[1])); ctx.closePath();
    ctx.fillStyle = fill; ctx.fill(); if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = lw || 1; ctx.stroke(); } };
  if (el > 0.5) {                                          // cast shadow, clipped to the ground disc
    const k = 1 / Math.max(Math.tan(el * RAD), 0.12), hx = Math.sin(azR), hy = Math.cos(azR), pts = [];
    const add = (a, b, z) => { const w = w3(a, b, z); pts.push([w[0] - hx * z * k, w[1] - hy * z * k]); };
    for (const sa of [-1, 1]) for (const sb of [-1, 1]) { add(sa * Wh, sb * Dh, 0); add(sa * Wh, sb * Dh, TOP); }
    add(0, br, Zr); add(0, -br, Zr);
    ctx.save(); ring(GROUND_R); ctx.clip();
    poly(convexHull(pts).map(([x, y]) => P(x, y, 0)), `rgba(0,0,0,${0.42 * clamp(el / 8, 0, 1)})`);
    ctx.restore();
  }
  const amb = mix([34, 40, 62], [92, 100, 118], day).map(v => v / 255);
  const sunRGB = el < 6 ? [1, 0.62, 0.34] : el < 14 ? [1, 0.84, 0.6] : [1, 0.97, 0.88];
  const lightOn = nrm => { const l = Math.max(0, nrm[0] * sd[0] + nrm[1] * sd[1] + nrm[2] * sd[2]) * (el > 0 ? 1 : 0);
    return [0, 1, 2].map(i => clamp(amb[i] + l * sunRGB[i] * 0.95, 0, 1.2)); };
  const shade = (base, nrm, k) => rgb(lightOn(nrm).map((l, i) => clamp(Math.round(base[i] * l * (k || 1)), 0, 255)));
  const glass = idx => night > 0.3 ? (idx % 3 === 1 ? '#3a3a44' : '#ffd48a') : rgb(mix([22, 34, 52], [100, 138, 176], day));

  // ground details: driveway + walkway on the front
  const flat = (a0, a1, b0, b1, base) => poly([PP(a0, b0, 0), PP(a1, b0, 0), PP(a1, b1, 0), PP(a0, b1, 0)], shade(base, [0, 0, 1], 0.9), 'rgba(0,0,0,.25)');
  flat(-3.6, 0.6, Dh, Dh + 5.8, CONCRETE); flat(2.0, 2.9, Dh, Dh + 4.6, [120, 112, 104]);

  // walls
  const sides = [
    { id: 'front', az: FRONT_AZ, pt: (u, z) => PP(-Wh + 2 * Wh * u, Dh, z) },
    { id: 'right', az: FRONT_AZ - 90, pt: (u, z) => PP(Wh, Dh - 2 * Dh * u, z) },
    { id: 'back', az: FRONT_AZ + 180, pt: (u, z) => PP(Wh - 2 * Wh * u, -Dh, z) },
    { id: 'left', az: FRONT_AZ + 90, pt: (u, z) => PP(-Wh, -Dh + 2 * Dh * u, z) },
  ];
  const sq = (sd, u0, u1, z0, z1) => [sd.pt(u0, z0), sd.pt(u1, z0), sd.pt(u1, z1), sd.pt(u0, z1)];
  let wi = 0;
  const win = (sd, u0, u1, z0, z1) => { poly(sq(sd, u0, u1, z0, z1), glass(wi++), 'rgba(40,28,20,.85)', 1.2); };
  const uA = a => (a + Wh) / (2 * Wh);
  sides.filter(sd => Math.cos(sd.az * RAD) < -0.05).forEach(sd => {
    const nrm = hn(sd.az);
    poly(sq(sd, 0, 1, 0, TOP), shade(STUCCO, nrm), 'rgba(0,0,0,.35)');
    [FL, 2 * FL].forEach(z => poly(sq(sd, 0, 1, z - 0.13, z + 0.13), shade(TRIM, nrm), 'rgba(0,0,0,.18)', 0.8));
    if (sd.id === 'front') {
      // stone veneer + front door + lanterns
      const stoneRows = 8, stoneCols = 4, su0 = uA(1.3), su1 = uA(3.6);
      for (let r = 0; r < stoneRows; r++) for (let c = 0; c < stoneCols; c++) {
        const j = ((r * 7 + c * 13) % 5) / 5, off = (r % 2) * 0.5 / stoneCols;
        const ua = su0 + (su1 - su0) * clamp((c + off) / stoneCols, 0, 1), ub = su0 + (su1 - su0) * clamp((c + 1 + off) / stoneCols, 0, 1);
        poly(sq(sd, ua, ub, r * 3 / stoneRows, (r + 1) * 3 / stoneRows - 0.03), shade(mix(STONE, [176, 158, 130], j), nrm), 'rgba(0,0,0,.3)', 0.7);
      }
      poly(sq(sd, uA(2.0), uA(2.9), 0, 2.45), shade(DOOR, nrm), 'rgba(0,0,0,.5)', 1);
      // garage door with panel lines
      poly(sq(sd, uA(-3.6), uA(0.5), 0, 2.5), shade(GARAGE, nrm, 0.9), 'rgba(0,0,0,.5)', 1.2);
      for (let i = 1; i < 4; i++) { ctx.beginPath(); ctx.moveTo(...sd.pt(uA(-3.6), 2.5 * i / 4)); ctx.lineTo(...sd.pt(uA(0.5), 2.5 * i / 4));
        ctx.strokeStyle = 'rgba(0,0,0,.35)'; ctx.lineWidth = 1; ctx.stroke(); }
      [[uA(1.0)], [uA(3.85)]].forEach(([u]) => { poly(sq(sd, u, u + 0.012, 1.5, 2.1), night > 0.3 ? '#ffd48a' : '#2a2420'); });
      // second-floor loggia, recessed 1.6 under the third floor
      const z0 = FL + 0.35, z1 = 2 * FL - 0.3, a0 = -3.7, a1 = 0.9, dep = 1.6;
      poly([PP(a0, Dh, z0), PP(a1, Dh, z0), PP(a1, Dh, z1), PP(a0, Dh, z1)], '#1a1614');
      poly([PP(a0, Dh - dep, z0), PP(a1, Dh - dep, z0), PP(a1, Dh - dep, z1), PP(a0, Dh - dep, z1)], shade(STUCCO, nrm, 0.62));
      poly([PP(a0, Dh, z0), PP(a0, Dh - dep, z0), PP(a0, Dh - dep, z1), PP(a0, Dh, z1)], shade(STUCCO, hn(FRONT_AZ - 90), 0.85));
      poly([PP(a0, Dh, z0), PP(a1, Dh, z0), PP(a1, Dh - dep, z0), PP(a0, Dh - dep, z0)], shade(CONCRETE, [0, 0, 1], 0.85));
      const gz1 = z1 - 0.25;
      for (let i = 0; i < 3; i++) { const ga = a0 + 0.35 + i * 1.3;
        poly([PP(ga, Dh - dep, z0), PP(ga + 1.2, Dh - dep, z0), PP(ga + 1.2, Dh - dep, gz1), PP(ga, Dh - dep, gz1)], glass(wi++), 'rgba(40,28,20,.85)', 1.2); }
      ctx.strokeStyle = '#1b1715'; ctx.lineWidth = 1.7;                      // railing: rails + balusters on the front plane
      [[z0 + 1.05], [z0 + 0.08]].forEach(([zz]) => { ctx.beginPath(); ctx.moveTo(...PP(a0, Dh, zz)); ctx.lineTo(...PP(a1, Dh, zz)); ctx.stroke(); });
      ctx.lineWidth = 1.1; for (let a = a0; a <= a1 + 0.01; a += 0.34) { ctx.beginPath(); ctx.moveTo(...PP(a, Dh, z0 + 0.08)); ctx.lineTo(...PP(a, Dh, z0 + 1.05)); ctx.stroke(); }
      // windows: second-floor slit + third-floor groups
      win(sd, uA(2.3), uA(2.8), FL + 1.0, FL + 2.5);
      [-3.4, -2.3, -1.2].forEach(a => win(sd, uA(a), uA(a + 0.85), 2 * FL + 0.55, 2 * FL + 2.2));
      [0.8, 1.85, 2.9].forEach(a => win(sd, uA(a), uA(a + 0.8), 2 * FL + 0.55, 2 * FL + 2.2));
    } else if (sd.id === 'right') {
      [0, 1, 2].forEach(f => [[0.16, 0.34], [0.5, 0.68], [0.78, 0.9]].forEach(([u0, u1], k) => { if (f === 0 && k < 2) return;
        win(sd, u0, u1, f * FL + 0.8, f * FL + 2.2); }));
    } else {
      [1, 2].forEach(f => win(sd, 0.3, 0.45, f * FL + 0.8, f * FL + 2.2));
    }
  });

  // roof: four low planes (back planes first). Panels go on the planes closest to the fitted face azimuths.
  const planes = [
    { az: FRONT_AZ - 90, E: [[A, -B], [A, B]], R: [[0, -br], [0, br]] },
    { az: FRONT_AZ + 90, E: [[-A, -B], [-A, B]], R: [[0, -br], [0, br]] },
    { az: FRONT_AZ, E: [[-A, B], [A, B]], R: [[0, br], [0, br]] },
    { az: FRONT_AZ + 180, E: [[-A, -B], [A, -B]], R: [[0, -br], [0, -br]] },
  ];
  const angDiff = (x, y) => Math.abs(((x - y + 540) % 360) - 180);
  const pick = (az, skip) => planes.map((pl, i) => [angDiff(pl.az, az), i]).filter(([, i]) => i !== skip).sort((x, y) => x[0] - y[0])[0][1];
  const pi0 = pick(cfg.faces[0].azimuth, -1), pi1 = pick(cfg.faces[1].azimuth, pi0), TOTAL = 24;
  const faceOf = i => i === pi0 ? 0 : i === pi1 ? 1 : -1;
  const lerp2 = (p, q, t) => [p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t];
  planes.map((pl, i) => [pl, i]).sort((x, y) => Math.cos(y[0].az * RAD) - Math.cos(x[0].az * RAD)).forEach(([pl, i]) => {
    const pt = (s, v) => { const ab = lerp2(lerp2(pl.E[0], pl.E[1], s), lerp2(pl.R[0], pl.R[1], s), v); return PP(ab[0], ab[1], ze + (Zr - ze) * v); };
    const nrm = [Math.sin(pl.az * RAD) * Math.sin(cfg.faces[0].tilt * RAD), Math.cos(pl.az * RAD) * Math.sin(cfg.faces[0].tilt * RAD), Math.cos(cfg.faces[0].tilt * RAD)];
    const outline = [pt(0, 0), pt(1, 0), pt(1, 1), pt(0, 1)];
    poly(pl.R[0][0] === pl.R[1][0] && pl.R[0][1] === pl.R[1][1] ? [outline[0], outline[1], outline[2]] : outline, shade(ROOF, nrm), 'rgba(0,0,0,.5)', 1.1);
    const fi = faceOf(i); if (fi < 0) return;
    const eaveL = Math.hypot(pl.E[1][0] - pl.E[0][0], pl.E[1][1] - pl.E[0][1]), ridgeL = Math.hypot(pl.R[1][0] - pl.R[0][0], pl.R[1][1] - pl.R[0][1]);
    const realW = v => eaveL * (1 - v) + ridgeL * v, CELL = 1.05;
    const f = clamp(cur.fr[fi], 0, 1.2), fill = rgb(mix(hex('#101a28'), mix(hex('#12202a'), hex('#8df0b0'), clamp(f, 0, 1)), el > 0 ? 1 : 0));
    let left = Math.round(TOTAL * cfg.faces[fi].weight);
    for (let r = 0; r < 3 && left > 0; r++) {
      const v0 = 0.08 + r * 0.29, v1 = v0 + 0.24, cap = Math.max(1, Math.floor(realW((v0 + v1) / 2) * 0.88 / CELL)), n = Math.min(cap, left); left -= n;
      const tw = CELL / realW((v0 + v1) / 2);
      for (let j = 0; j < n; j++) {
        const ta = 0.5 - (n * tw) / 2 + j * tw + tw * 0.05, tb = 0.5 - (n * tw) / 2 + (j + 1) * tw - tw * 0.05;
        ctx.beginPath(); [pt(ta, v0), pt(tb, v0), pt(tb, v1), pt(ta, v1)].forEach((q, k) => k ? ctx.lineTo(...q) : ctx.moveTo(...q)); ctx.closePath();
        ctx.shadowColor = `rgba(141,240,176,${0.5 * clamp(f, 0, 1)})`; ctx.shadowBlur = f > 0.35 ? 9 : 0;
        ctx.fillStyle = fill; ctx.fill(); ctx.shadowBlur = 0; ctx.strokeStyle = 'rgba(255,255,255,.22)'; ctx.lineWidth = 0.8; ctx.stroke();
      }
    }
  });

  if (el > 2) { const rp = P(...w3(0, 0, Zr + 0.6)); ctx.beginPath(); ctx.moveTo(...sp); ctx.lineTo(...rp);
    ctx.setLineDash([3, 5]); ctx.strokeStyle = 'rgba(245,185,66,.3)'; ctx.lineWidth = 1.2; ctx.stroke(); ctx.setLineDash([]); }
  drawArcs(true);
  if (sd[1] <= 0) drawSun();

  // HUD
  ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
  ctx.fillStyle = 'rgba(0,0,0,.28)'; ctx.beginPath(); ctx.roundRect(8, 8, 150, 52, 8); ctx.fill();
  ctx.fillStyle = C.bright; ctx.font = `700 22px ${MONO}`; ctx.fillText(fmtTime(st.minute), 16, 33);
  ctx.fillStyle = C.text; ctx.font = `11px ${MONO}`; ctx.fillText(fmtDate(st.doy), 16, 51);
  const tag = st.sweeping ? 'YEAR SWEEP · SOLAR NOON' : seasonTag(st.doy).toUpperCase();
  if (tag) { ctx.textAlign = 'right'; ctx.fillStyle = C.amber; ctx.font = `600 10px ${MONO}`; ctx.fillText(tag, W - 12, 20); }
  ctx.restore();
}
function convexHull(pts) {
  const p = pts.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]), cr = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lo = [], up = [];
  for (const q of p) { while (lo.length > 1 && cr(lo[lo.length - 2], lo[lo.length - 1], q) <= 0) lo.pop(); lo.push(q); }
  for (let i = p.length - 1; i >= 0; i--) { const q = p[i]; while (up.length > 1 && cr(up[up.length - 2], up[up.length - 1], q) <= 0) up.pop(); up.push(q); }
  return lo.slice(0, -1).concat(up.slice(0, -1));
}

// ── day chart ─────────────────────────────────────────────────────────
let dayGeom = null;
function drawDay() {
  const { ctx, w, h } = fit(els.day, 150);
  ctx.clearRect(0, 0, w, h); if (!st.cfg) return;
  const dd = derived(st.doy), act = (st.actual.get(st.doy) || {}).actual;
  const padL = 34, padR = 10, padT = 14, padB = 20;
  const t0 = dd.rise === null ? 300 : Math.max(0, Math.floor((dd.rise - 45) / 60) * 60), t1 = dd.set === null ? 1200 : Math.min(1440, Math.ceil((dd.set + 45) / 60) * 60);
  const aPeak = act ? Math.max(0, ...act.map(a => a.kw)) : 0, ymax = Math.max(1, Math.ceil(Math.max(dd.peak * 1.08, aPeak * 1.05)));
  const X = m => padL + (m - t0) / (t1 - t0) * (w - padL - padR), Y = k => h - padB - k / ymax * (h - padT - padB);
  dayGeom = { padL, padR, t0, t1, w };
  ctx.font = `10px ${MONO}`; ctx.textBaseline = 'middle'; ctx.textAlign = 'right';
  for (let k = 0; k <= ymax; k++) { ctx.strokeStyle = k ? 'rgba(255,255,255,.06)' : 'rgba(255,255,255,.18)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, Math.round(Y(k)) + 0.5); ctx.lineTo(w - padR, Math.round(Y(k)) + 0.5); ctx.stroke();
    ctx.fillStyle = C.muted; ctx.fillText(k, padL - 6, Y(k)); }
  ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
  for (let m = Math.ceil(t0 / 120) * 120; m <= t1; m += 120) { ctx.fillStyle = C.muted; const hh = m / 60;
    ctx.fillText(hh === 12 ? '12p' : hh > 12 ? (hh - 12) + 'p' : hh + 'a', X(m), h - 6); }
  // stacked model by roof face
  const s = dd.series.filter(p => p.min >= t0 && p.min <= t1), cols = [['rgba(127,182,255,.30)', C.blue], ['rgba(199,155,255,.34)', C.violet]];
  let lower = s.map(() => 0);
  st.cfg.faces.forEach((f, i) => { const upper = s.map((p, j) => lower[j] + p.kws[i]);
    ctx.beginPath(); s.forEach((p, j) => j ? ctx.lineTo(X(p.min), Y(upper[j])) : ctx.moveTo(X(p.min), Y(upper[j])));
    for (let j = s.length - 1; j >= 0; j--) ctx.lineTo(X(s[j].min), Y(lower[j])); ctx.closePath(); ctx.fillStyle = cols[i % 2][0]; ctx.fill(); lower = upper; });
  ctx.beginPath(); s.forEach((p, j) => j ? ctx.lineTo(X(p.min), Y(p.kw)) : ctx.moveTo(X(p.min), Y(p.kw)));
  ctx.setLineDash([4, 3]); ctx.strokeStyle = C.blue; ctx.lineWidth = 1.5; ctx.stroke(); ctx.setLineDash([]);
  if (act && act.length) {
    const pts = act.filter(a => a.min >= t0 - 5 && a.min <= t1 + 5);
    ctx.beginPath(); pts.forEach((a, j) => j ? ctx.lineTo(X(a.min), Y(a.kw)) : ctx.moveTo(X(a.min), Y(a.kw)));
    ctx.strokeStyle = C.green; ctx.lineWidth = 2; ctx.stroke();
    ctx.lineTo(X(pts[pts.length - 1].min), Y(0)); ctx.lineTo(X(pts[0].min), Y(0)); ctx.closePath(); ctx.fillStyle = 'rgba(141,240,176,.16)'; ctx.fill();
  }
  const cx = X(clamp(st.minute, t0, t1)), cur = at(st.doy, st.minute);
  ctx.strokeStyle = C.amber; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.moveTo(cx, padT - 4); ctx.lineTo(cx, h - padB); ctx.stroke();
  ctx.beginPath(); ctx.arc(cx, Y(cur.kw), 4, 0, 6.2832); ctx.fillStyle = C.amber; ctx.fill();
  ctx.textAlign = 'left'; ctx.font = `9px ${MONO}`; let lx = padL + 4;
  [[C.blue, st.cfg.faces[0].name.split(' ')[0]], [C.violet, st.cfg.faces[1].name.split(' ')[0]], [C.green, act ? 'MEASURED' : '']].forEach(([c, t]) => { if (!t) return; ctx.fillStyle = c; ctx.fillRect(lx, 5, 8, 8); ctx.fillStyle = C.text; ctx.fillText(t, lx + 12, 12); lx += 22 + t.length * 6; });
  ctx.fillStyle = C.muted; ctx.fillText('- - MODEL', lx, 12);
}

// ── year chart ────────────────────────────────────────────────────────
let yearGeom = null;
function drawYear() {
  const { ctx, w, h } = fit(els.year, 110);
  ctx.clearRect(0, 0, w, h); if (!st.days) return;
  const padL = 34, padR = 10, padT = 16, padB = 18, days = st.days;
  const ymax = Math.ceil(Math.max(...days.map(d => d.model_kwh), ...days.map(d => d.actual_kwh || 0)) / 5) * 5 || 5;
  const X = d => padL + (d - 1) / 364 * (w - padL - padR), Y = k => h - padB - k / ymax * (h - padT - padB);
  yearGeom = { padL, padR, w };
  ctx.font = `10px ${MONO}`; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  for (let k = 0; k <= ymax; k += ymax / 2) { ctx.strokeStyle = k ? 'rgba(255,255,255,.06)' : 'rgba(255,255,255,.18)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, Math.round(Y(k)) + 0.5); ctx.lineTo(w - padR, Math.round(Y(k)) + 0.5); ctx.stroke(); ctx.fillStyle = C.muted; ctx.fillText(Math.round(k), padL - 6, Y(k)); }
  ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
  MONTHS.forEach((m, i) => { const d = doyOf(i, 15); ctx.fillStyle = C.muted; ctx.fillText(m, X(d), h - 5); });
  [doyOf(5, 21), doyOf(11, 21)].forEach(d => { ctx.strokeStyle = 'rgba(255,255,255,.1)'; ctx.setLineDash([2, 4]); ctx.beginPath(); ctx.moveTo(X(d), padT); ctx.lineTo(X(d), h - padB); ctx.stroke(); ctx.setLineDash([]); });
  ctx.beginPath(); days.forEach((d, i) => i ? ctx.lineTo(X(d.doy), Y(d.model_kwh)) : ctx.moveTo(X(d.doy), Y(d.model_kwh)));
  ctx.strokeStyle = C.blue; ctx.lineWidth = 1.6; ctx.stroke(); ctx.lineTo(X(365), Y(0)); ctx.lineTo(X(1), Y(0)); ctx.closePath(); ctx.fillStyle = 'rgba(143,180,255,.12)'; ctx.fill();
  ctx.fillStyle = C.green; days.forEach(d => { if (d.actual_kwh) { ctx.beginPath(); ctx.arc(X(d.doy), Y(d.actual_kwh), 1.7, 0, 6.2832); ctx.fill(); } });
  const row = dayRow(st.doy), cx = X(st.doy);
  ctx.strokeStyle = C.amber; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.moveTo(cx, padT - 4); ctx.lineTo(cx, h - padB); ctx.stroke();
  ctx.beginPath(); ctx.arc(cx, Y(row.model_kwh), 4, 0, 6.2832); ctx.fillStyle = C.amber; ctx.fill();
  ctx.textAlign = 'left'; ctx.font = `9px ${MONO}`; ctx.fillStyle = C.text;
  ctx.fillText(`${row.model_kwh.toFixed(1)} kWh clear-sky` + (row.actual_kwh ? ` · ${row.actual_kwh.toFixed(1)} measured` : ''), padL + 4, 11);
}

// ── readout + UI ──────────────────────────────────────────────────────
const fname = i => st.cfg.faces[i].name.toUpperCase();
function chip(k, v, extra) { return `<div class="sp-chip"><div class="k">${k}</div><div class="v">${v}</div>${extra ? `<div class="x">${extra}</div>` : ''}</div>`; }
function updateChips() {
  if (!st.cfg) return;
  const c = at(st.doy, st.minute), dd = derived(st.doy), row = dayRow(st.doy), act = (st.actual.get(st.doy) || {}).actual;
  let meas = '';
  if (act && act.length) { const near = act.reduce((a, b) => Math.abs(b.min - st.minute) < Math.abs(a.min - st.minute) ? b : a);
    if (Math.abs(near.min - st.minute) <= 8) meas = `${near.kw.toFixed(2)} measured`; }
  const html = [
    chip('SUN', c.el > 0 ? `${c.el.toFixed(0)}° <small>${dirName(c.az)} ${c.az.toFixed(0)}°</small>` : '<small>below horizon</small>'),
    chip('OUTPUT NOW', `${c.kw.toFixed(2)} <small>kW model</small>`, meas),
    chip('DAYLIGHT', dd.rise === null ? '—' : `${((dd.set - dd.rise) / 60).toFixed(1)} h`, dd.rise === null ? '' : `${fmtTime(dd.rise)}–${fmtTime(dd.set)}`),
    chip(fname(0), `${c.kws[0].toFixed(2)} <small>kW · ${Math.round(st.cfg.faces[0].weight * 100)}%</small>`),
    chip(fname(1), `${c.kws[1].toFixed(2)} <small>kW · ${Math.round(st.cfg.faces[1].weight * 100)}%</small>`),
    chip('DAY TOTAL', `${dd.kwh.toFixed(1)} <small>kWh model</small>`, row.actual_kwh ? `${row.actual_kwh.toFixed(1)} measured` : ''),
  ].join('');
  if (html !== lastChips) { els.chips.innerHTML = html; lastChips = html; }
  const dateTxt = `${fmtDate(st.doy)}`;
  if (els.date.textContent !== dateTxt) els.date.textContent = dateTxt;
  if (+els.slider.value !== st.doy) els.slider.value = st.doy;
}
function syncUI() {
  const set = (b, on) => b.setAttribute('aria-pressed', on ? 'true' : 'false');
  set(els.live, st.live); set(els.play, st.playing); set(els.sweep, st.sweeping);
  els.play.textContent = st.playing ? '❚❚ PAUSE' : '▶ PLAY DAY';
  els.sweep.textContent = st.sweeping ? '■ STOP SWEEP' : '⟳ YEAR SWEEP';
  root.querySelectorAll('[data-sp=spd]').forEach(b => set(b, b.dataset.v === st.speed));
  els.err.hidden = !st.err; els.err.textContent = st.err || '';
}

// ── loop ──────────────────────────────────────────────────────────────
function draw() { try { drawScene(); drawDay(); drawYear(); updateChips(); } catch (e) { console.error('sunpath draw', e); } }
function requestDraw() { if (!raf && active) raf = requestAnimationFrame(frame); }
function frame(ts) {
  raf = 0; const dt = Math.min(0.1, lastTs ? (ts - lastTs) / 1000 : 0.016); lastTs = ts;
  if (st.cfg) {
    if (st.sweeping) {
      st.sweepF += dt * (365 / 14);
      if (st.sweepF >= 365) { st.sweeping = false; st.doy = st.sweepStart.doy; st.minute = st.sweepStart.minute; syncUI(); fetchDay(st.doy); }
      else { st.doy = wrapDoy(st.sweepStart.doy + st.sweepF); st.minute = noonOf(st.doy); }
    } else if (st.playing) {
      const night = at(st.doy, st.minute).el < -6 ? 5 : 1;
      st.minute += dt * SPEEDS[st.speed] * night; if (st.minute >= 1440) st.minute -= 1440;
    } else if (st.live) { const m = liveMoment(); if (m.doy !== st.doy) { st.doy = m.doy; scheduleFetch(); } st.minute = m.minute; }
  }
  draw();
  clearTimeout(timer);
  if (!active) return;
  if (st.sweeping || st.playing || st.dragging) raf = requestAnimationFrame(frame);
  else timer = setTimeout(() => { if (!raf && active) raf = requestAnimationFrame(frame); }, st.live ? 1000 : 2500);
}
function userControl() { st.live = false; if (st.sweeping) stopSweep(); }
function stopSweep() { st.sweeping = false; if (st.sweepStart) { st.doy = st.sweepStart.doy; st.minute = st.sweepStart.minute; } }
function setDoy(d) { st.doy = clamp(Math.round(d), 1, 365); scheduleFetch(); }

function wire() {
  const q = k => root.querySelector(`[data-sp=${k}]`);
  els = { scene: q('scene'), day: q('day'), year: q('year'), chips: q('chips'), err: q('err'), live: q('live'), play: q('play'),
          sweep: q('sweep'), slider: q('slider'), date: q('date') };
  els.live.onclick = () => { st.live = true; st.playing = false; if (st.sweeping) stopSweep(); const m = st.days ? liveMoment() : null;
    if (m) { st.doy = m.doy; st.minute = m.minute; fetchDay(st.doy); } syncUI(); requestDraw(); };
  els.play.onclick = () => { if (!st.cfg) return; if (st.sweeping) stopSweep(); st.live = false; st.playing = !st.playing; syncUI(); requestDraw(); };
  root.querySelectorAll('[data-sp=spd]').forEach(b => b.onclick = () => { st.speed = b.dataset.v; syncUI(); });
  els.sweep.onclick = () => { if (!st.cfg) return;
    if (st.sweeping) { stopSweep(); syncUI(); requestDraw(); return; }
    st.live = false; st.playing = false; st.sweeping = true; st.sweepF = 0; st.sweepStart = { doy: st.doy, minute: st.minute }; syncUI(); requestDraw(); };
  els.slider.oninput = () => { if (!st.cfg) return; userControl(); st.playing = false; setDoy(+els.slider.value); syncUI(); requestDraw(); };
  root.querySelectorAll('[data-sp=jump]').forEach(b => b.onclick = () => { if (!st.cfg) return;
    if (b.dataset.m === 'today') { els.live.onclick(); return; }
    userControl(); setDoy(doyOf(+b.dataset.m, +b.dataset.d)); st.minute = noonOf(st.doy); syncUI(); requestDraw(); });
  const scrub = (cv, fn) => {
    const at_ = e => { const r = cv.getBoundingClientRect(); fn(e.clientX - r.left); st.dragging = true; requestDraw(); };
    cv.addEventListener('pointerdown', e => { if (!st.cfg) return; cv.setPointerCapture(e.pointerId); userControl(); st.playing = false; syncUI(); at_(e); });
    cv.addEventListener('pointermove', e => { if (st.dragging && cv.hasPointerCapture(e.pointerId)) at_(e); });
    const end = e => { st.dragging = false; try { cv.releasePointerCapture(e.pointerId); } catch (_) {} requestDraw(); };
    cv.addEventListener('pointerup', end); cv.addEventListener('pointercancel', end);
  };
  scrub(els.day, x => { if (!dayGeom) return; const g = dayGeom;
    st.minute = clamp(Math.round((g.t0 + (x - g.padL) / (g.w - g.padL - g.padR) * (g.t1 - g.t0)) / 5) * 5, 0, 1435); });
  scrub(els.year, x => { if (!yearGeom) return; const g = yearGeom; setDoy(1 + (x - g.padL) / (g.w - g.padL - g.padR) * 364); });
}

function mount(section) {
  if (!document.getElementById('sp-style')) { const s = document.createElement('style'); s.id = 'sp-style'; s.textContent = CSS; document.head.appendChild(s); }
  root = section; wire();
  new ResizeObserver(() => requestDraw()).observe(section);
  if ('IntersectionObserver' in window) new IntersectionObserver(es => { active = es[0].isIntersecting && !document.hidden; if (active) requestDraw(); }).observe(section);
  document.addEventListener('visibilitychange', () => { active = !document.hidden; if (active) requestDraw(); });
  loadYear(); setInterval(() => { if (Date.now() - yearLoaded > 30 * 60000) loadYear(); }, 60000);
  syncUI(); requestDraw();
}
window.SunPath = { html: HTML, mount, _test: { sunPos, clearSky, poaFrac } };   // _test: parity checks vs sunpath.py
})();
