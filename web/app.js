// F1 Live Tracker front end: track map + cars on a canvas, timing tower.
// Snapshots arrive at ~10 Hz; we render ~120 ms in the past and interpolate between
// the two snapshots around that time so motion is smooth at display frame rate.
// Extras: replay speed control, hover cards, comet trails, fastest-lap halo, event toasts.
"use strict";

const canvas = document.getElementById("c");
const ctx = canvas.getContext("2d");
const RENDER_DELAY = 120; // ms
const TRAIL_LEN = 26;
const SPEEDS = [1, 2, 5, 10, 20, 50];
const TYRES = { SOFT: ["S", "#e5484d"], MEDIUM: ["M", "#f5c518"], HARD: ["H", "#e8e8ea"],
                INTERMEDIATE: ["I", "#3fb950"], WET: ["W", "#4c8dff"] };
const FLAGS = { "1": ["GREEN", ""], "2": ["YELLOW", "sc"], "4": ["SAFETY CAR", "sc"], "5": ["RED FLAG", "red"],
                "6": ["VSC", "sc"], "7": ["VSC ENDING", "sc"] };

let track = null;
let view = null;           // {scale, ox, oy}
let ws = null;
const snaps = [];          // [{at: performance.now(), data}]
let latest = null;         // newest snapshot data
let lastTowerUpdate = 0;
let hovered = null;        // car number under the mouse / tower row
let pinned = null;         // car number clicked
let mouse = null;          // {x, y} in canvas CSS px
const screenPos = new Map(); // num -> {x, y} last drawn
const trails = new Map();    // num -> [{x, y}] world coords
const prevPos = new Map();   // num -> last tower position (for flashes / toasts)
const flash = new Map();     // num -> {dir, until}
let lastFastest = null;
let currentLabel = null;

// ---- track & view ---------------------------------------------------------------------
let trackLoading = false;
function resetScene() {
  snaps.length = 0; trails.clear(); prevPos.clear(); flash.clear();
  lastFastest = null; pinned = null; hovered = null; latest = null;
}
async function ensureTrack(name) {            // fetch the track once per race; reset the scene when it changes
  if (trackLoading) return;
  trackLoading = true;
  try {
    const r = await fetch("/track");
    if (r.ok) { track = await r.json(); resetScene(); fit(); }
  } catch (e) { /* retried on the next frame */ }
  trackLoading = false;
}

function fit() {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (!track) return;
  const xs = track.x, ys = track.y;
  const minx = Math.min(...xs), maxx = Math.max(...xs), miny = Math.min(...ys), maxy = Math.max(...ys);
  const pad = 50, padTop = w < 700 ? 100 : pad;   // on phones the control bar sits over the top of the map
  const scale = Math.min((w - 2 * pad) / (maxx - minx), (h - pad - padTop) / (maxy - miny));
  view = { scale, ox: (w - scale * (maxx - minx)) / 2 - scale * minx,
           oy: (padTop + h - pad + scale * (maxy - miny)) / 2 + scale * miny };
}

const sx = (x) => view.ox + view.scale * x;
const sy = (y) => view.oy - view.scale * y;   // y up in the F1 frame

function drawTrack(now) {
  const { x, y } = track;
  const path = () => {
    ctx.beginPath();
    ctx.moveTo(sx(x[0]), sy(y[0]));
    for (let i = 1; i < x.length; i++) ctx.lineTo(sx(x[i]), sy(y[i]));
    ctx.closePath();
  };
  // mood: the track edge glows with the flag
  const ts = latest ? String(latest.track_status) : "1";
  const pulse = 0.55 + 0.45 * Math.sin(now / 260);
  const calm = "#6b7280";
  let glow = calm, blur = 6;
  if (ts === "4" || ts === "6" || ts === "7" || ts === "2") { glow = `rgba(245,197,24,${0.5 + 0.5 * pulse})`; blur = 18; }
  else if (ts === "5") { glow = `rgba(229,72,77,${0.5 + 0.5 * pulse})`; blur = 20; }
  ctx.lineJoin = "round";
  ctx.save();
  ctx.shadowColor = glow; ctx.shadowBlur = blur;
  ctx.strokeStyle = glow === calm ? getVar("--track-edge") : glow; ctx.lineWidth = 15; path(); ctx.stroke();
  ctx.restore();
  ctx.strokeStyle = getVar("--track"); ctx.lineWidth = 11; path(); ctx.stroke();
  // centre dashes for a bit of motion cue
  ctx.save();
  ctx.strokeStyle = "#ffffff12"; ctx.lineWidth = 1.2; ctx.setLineDash([6, 10]); ctx.lineDashOffset = -now / 60;
  path(); ctx.stroke();
  ctx.restore();
  if (track.pit_lane && track.pit_lane.length > 1) {
    ctx.strokeStyle = getVar("--pit"); ctx.lineWidth = 6; ctx.setLineDash([]);
    ctx.beginPath();
    track.pit_lane.forEach(([px, py], i) => (i ? ctx.lineTo(sx(px), sy(py)) : ctx.moveTo(sx(px), sy(py))));
    ctx.stroke();
  }
  // start/finish line (chequered) and sector gates
  track.gates.forEach(([gx, gy, nx, ny], i) => {
    const len = i === 0 ? 11 : 8;
    ctx.strokeStyle = i === 0 ? getVar("--line") : "#6b7280";
    ctx.lineWidth = i === 0 ? 3 : 2;
    ctx.beginPath();
    ctx.moveTo(sx(gx) + nx * len, sy(gy) - ny * len);
    ctx.lineTo(sx(gx) - nx * len, sy(gy) + ny * len);
    ctx.stroke();
  });
}

// ---- interpolation ----------------------------------------------------------------------
function interpolatedCars(now) {
  if (!snaps.length) return [];
  const t = now - RENDER_DELAY;
  let a = snaps[0], b = snaps[snaps.length - 1];
  for (let i = snaps.length - 1; i > 0; i--) {
    if (snaps[i - 1].at <= t) { a = snaps[i - 1]; b = snaps[i]; break; }
  }
  const span = b.at - a.at;
  const u = span > 0 ? Math.min(1, Math.max(0, (t - a.at) / span)) : 1;
  const prev = new Map(a.data.cars.map((c) => [c.num, c]));
  return b.data.cars.map((c) => {
    const p = prev.get(c.num);
    if (p && p.x != null && c.x != null && p.mode === c.mode) {
      const d = Math.hypot(c.x - p.x, c.y - p.y);
      if (d < 400) return { ...c, x: p.x + (c.x - p.x) * u, y: p.y + (c.y - p.y) * u };
    }
    return c;
  });
}

// ---- cars -------------------------------------------------------------------------------
function updateTrails(cars) {
  for (const c of cars) {
    if (c.x == null || c.mode === "out" || c.mode === "pit") { trails.delete(c.num); continue; }
    let t = trails.get(c.num);
    if (!t) trails.set(c.num, (t = []));
    const last = t[t.length - 1];
    if (last && Math.hypot(c.x - last.x, c.y - last.y) > 400) t.length = 0;   // wrapped/jumped
    if (!last || Math.hypot(c.x - last.x, c.y - last.y) > 3) {
      t.push({ x: c.x, y: c.y });
      if (t.length > TRAIL_LEN) t.shift();
    }
  }
}

function drawCars(cars, now) {
  updateTrails(cars);
  const focus = hovered || pinned;
  screenPos.clear();
  // draw back-markers first so leaders are on top; the focused car last
  const sorted = [...cars].filter((c) => c.x != null && c.mode !== "out")
    .sort((p, q) => (focus === p.num) - (focus === q.num) || (q.pos || 99) - (p.pos || 99));

  // comet trails
  ctx.lineCap = "round";
  for (const c of sorted) {
    const t = trails.get(c.num);
    if (!t || t.length < 2) continue;
    const dim = focus && focus !== c.num ? 0.25 : 1;
    for (let i = 1; i < t.length; i++) {
      const k = i / t.length;
      ctx.strokeStyle = c.color;
      ctx.globalAlpha = k * 0.55 * dim;
      ctx.lineWidth = 1 + k * 4.5;
      ctx.beginPath();
      ctx.moveTo(sx(t[i - 1].x), sy(t[i - 1].y));
      ctx.lineTo(sx(t[i].x), sy(t[i].y));
      ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;

  ctx.font = "600 11px system-ui, sans-serif";
  ctx.textBaseline = "middle";
  for (const c of sorted) {
    const x = sx(c.x), y = sy(c.y);
    screenPos.set(c.num, { x, y });
    const isFocus = focus === c.num;
    const dim = focus && !isFocus ? 0.3 : 1;
    ctx.globalAlpha = dim;
    if (c.fastest) {   // purple fastest-lap halo
      const r = 11 + 3 * Math.sin(now / 220);
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.strokeStyle = "#c77dff"; ctx.lineWidth = 2; ctx.shadowColor = "#c77dff"; ctx.shadowBlur = 14; ctx.stroke();
      ctx.shadowBlur = 0;
    }
    if (c.pos === 1 && c.mode !== "pit") {   // leader ring
      ctx.beginPath(); ctx.arc(x, y, 9.5, 0, Math.PI * 2);
      ctx.strokeStyle = "#ffd54a"; ctx.lineWidth = 1.5; ctx.stroke();
    }
    ctx.save();
    ctx.shadowColor = c.color; ctx.shadowBlur = isFocus ? 22 : 9;
    ctx.beginPath(); ctx.arc(x, y, isFocus ? 8.5 : 6, 0, Math.PI * 2);
    ctx.fillStyle = c.mode === "pit" ? "#14171d" : c.color;
    ctx.fill();
    ctx.restore();
    ctx.lineWidth = c.mode === "pit" ? 2.2 : 1.5;
    ctx.strokeStyle = c.mode === "pit" ? c.color : "#000";
    if (c.mode === "pit") ctx.setLineDash([3, 2]);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = getVar("--text");
    ctx.font = (isFocus ? "700 12px" : "600 11px") + " system-ui, sans-serif";
    ctx.fillText(c.code + (c.mode === "pit" ? " PIT" : ""), x + (isFocus ? 13 : 9), y);
    ctx.globalAlpha = 1;
  }
}

// ---- hover card -------------------------------------------------------------------------
const card = document.getElementById("card");

function fmtLap(s) {
  if (!s) return "—";
  const m = Math.floor(s / 60), r = s - m * 60;
  return m ? `${m}:${r.toFixed(3).padStart(6, "0")}` : r.toFixed(3);
}

function showCard() {
  const num = hovered || pinned;
  const c = num && latest && latest.cars.find((k) => k.num === num);
  if (!c) { card.classList.remove("show"); return; }
  const [tl, tc] = TYRES[(c.tyre || "").toUpperCase()] || ["?", "#888"];
  const lf = c.last_flag, secs = (c.sectors || []).map(([v, f]) => `<div class="${f}">${v || "–"}</div>`).join("");
  const state = c.mode === "out" ? (c.out || "Out") : c.mode === "pit" ? "In the pits" : "On track";
  card.style.setProperty("--accent", c.color);
  card.innerHTML =
    `<div class="hd"><span class="code" style="color:${c.color}">${esc(c.code)}</span><span class="nm">${esc(c.name || "")}</span></div>` +
    `<div class="team">${esc(c.team || "")}${pinned === c.num ? " · 📌 pinned" : ""}</div>` +
    `<div class="kv">` +
    `<span>Position</span><span>P${c.pos ?? "–"}${c.lap ? ` · lap ${c.lap}` : ""}</span>` +
    `<span>Last lap</span><span class="${lf}">${fmtLap(c.last)}</span>` +
    `<span>Best lap</span><span class="${c.fastest ? "purple" : ""}">${fmtLap(c.best)}</span>` +
    `<span>Gap</span><span>${esc(c.pos === 1 ? "Leader" : c.gap || "–")}</span>` +
    `<span>Interval</span><span>${esc(c.pos === 1 ? "–" : c.interval || "–")}</span>` +
    `<span>Tyre</span><span style="color:${tc}">${tl} · ${c.stops} stop${c.stops === 1 ? "" : "s"}</span>` +
    `<span>Status</span><span>${state}</span></div>` +
    (secs ? `<div class="secs">${secs}</div>` : "") +
    (c.fastest ? `<div class="badge">⚡ FASTEST LAP OF THE RACE</div>` : "");
  // position near the car (or the mouse), kept inside the map
  const sp = screenPos.get(num) || mouse || { x: 100, y: 100 };
  const W = canvas.clientWidth, H = canvas.clientHeight;
  let left = sp.x + 20, top = sp.y - 20;
  if (left + 240 > W) left = sp.x - 250;
  top = Math.max(50, Math.min(top, H - card.offsetHeight - 10));
  card.style.left = left + "px"; card.style.top = top + "px";
  card.classList.add("show");
}

canvas.addEventListener("mousemove", (e) => {
  const r = canvas.getBoundingClientRect();
  mouse = { x: e.clientX - r.left, y: e.clientY - r.top };
  let best = null, bd = 16;
  for (const [num, p] of screenPos) {
    const d = Math.hypot(p.x - mouse.x, p.y - mouse.y);
    if (d < bd) { bd = d; best = num; }
  }
  if (best !== hovered) { hovered = best; canvas.style.cursor = best ? "pointer" : ""; markTower(); }
});
canvas.addEventListener("mouseleave", () => { hovered = null; mouse = null; markTower(); });
canvas.addEventListener("click", () => { if (hovered) pinned = pinned === hovered ? null : hovered; markTower(); });

const rowsEl = document.getElementById("rows");
rowsEl.addEventListener("mouseover", (e) => { const r = e.target.closest(".row"); if (r) { hovered = r.dataset.num; markTower(); } });
rowsEl.addEventListener("mouseleave", () => { hovered = null; markTower(); });
rowsEl.addEventListener("click", (e) => {
  const r = e.target.closest(".row");
  if (r) { pinned = pinned === r.dataset.num ? null : r.dataset.num; markTower(); }
});

function markTower() {
  document.querySelectorAll(".row").forEach((el) => {
    el.classList.toggle("hl", el.dataset.num === hovered);
    el.classList.toggle("pinned", el.dataset.num === pinned);
  });
}

// ---- tower ----------------------------------------------------------------------------------
function updateTower(data) {
  const el = document.getElementById("rows");
  const out = document.getElementById("outlist");
  const live = data.cars.filter((c) => c.mode !== "out").sort((a, b) => (a.pos || 99) - (b.pos || 99));
  const gone = data.cars.filter((c) => c.mode === "out");
  // position-change flashes + toasts
  const racing = data.session_status === "Started" && (data.lap || 0) > 1;
  const now = performance.now();
  for (const c of data.cars) {
    const p = prevPos.get(c.num);
    if (p != null && c.pos != null && c.pos !== p && c.mode !== "out") {
      flash.set(c.num, { dir: c.pos < p ? "up" : "down", until: now + 1400 });
      if (racing && c.pos < p && c.pos <= 10 && c.pos === p - 1 && c.mode === "track") {
        toast(`<b style="color:${c.color}">${esc(c.code)}</b> ▲ up to P${c.pos}`, c.color);
      }
    }
    if (c.pos != null) prevPos.set(c.num, c.pos);
  }
  const f = data.fastest;
  if (f && (!lastFastest || f.num !== lastFastest.num || f.time < lastFastest.time - 1e-6)) {
    if (lastFastest && data.session_status === "Started") {
      const c = data.cars.find((k) => k.num === f.num);
      toast(`🟣 <b>FASTEST LAP</b> <span style="color:${c ? c.color : "#c77dff"}">${esc(c ? c.code : f.num)}</span> ${fmtLap(f.time)}`, "#c77dff");
    }
    if (lastFastest && window.FX) FX.pulse();
    lastFastest = f;
  }
  el.innerHTML = live.map(row).join("");
  out.innerHTML = gone.length ? `<div class="label">OUT</div>` + gone.map(row).join("") : "";
  markTower();
}

function row(c) {
  const [tl, tc] = TYRES[(c.tyre || "").toUpperCase()] || ["", "#666"];
  const gap = c.pos === 1 ? "Leader" : (c.gap || "");
  const fl = flash.get(c.num);
  const flashCls = fl && fl.until > performance.now() ? " " + fl.dir : "";
  const cls = (c.mode === "pit" ? "row pit" : c.mode === "out" ? "row out" : "row") + flashCls;
  return `<div class="${cls}" data-num="${c.num}" style="--c:${c.color}"><span class="pos">${c.pos ?? ""}</span>` +
    `<span class="bar" style="background:${c.color}"></span><span class="code">${esc(c.code)}${c.fastest ? '<span class="fl">⚡</span>' : ""}</span>` +
    `<span class="gap">${c.mode === "out" ? (c.out || "OUT") : esc(gap)}</span>` +
    `<span class="tyre" style="color:${tc}">${tl}</span></div>`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}

function toast(html, color) {
  const box = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = "toast"; el.style.setProperty("--c", color || "#888"); el.innerHTML = html;
  box.prepend(el);
  while (box.children.length > 4) box.lastChild.remove();
  setTimeout(() => el.remove(), 4700);
}

function updateHeader(d) {
  document.getElementById("title").textContent = d.meeting || "F1 Live Tracker";
  const clock = d.t ? new Date(d.t * 1000).toISOString().slice(11, 19) + " UTC" : "";
  document.getElementById("subtitle").textContent =
    `${d.session || ""} · ${d.session_status || "—"} · ${clock}${d.mode === "replay" ? ` · replay ${d.speed}×${d.paused ? " (paused)" : ""}${d.finished ? " · finished" : ""}` : ""}`;
  document.getElementById("lap").textContent = d.lap ? `Lap ${d.lap}${d.total_laps ? " / " + d.total_laps : ""}` : "";
  const [txt, cls] = FLAGS[d.track_status] || ["", ""];
  const flag = document.getElementById("flag");
  flag.textContent = txt;
  flag.className = "pill " + cls;
  updateControls(d);
  updateWeather(d);
  if (window.FX) FX.update(d);
}

// ---- weather -----------------------------------------------------------------------------
// ?wx=rain|hot|cold previews the effects when the real weather is boring.
const DEMO_WX = { rain: { air: 14, track: 19, humidity: 92, wind: 6.5, wind_dir: 250, rain: true },
                  hot: { air: 31, track: 52, humidity: 40, wind: 1.2, wind_dir: 120, rain: false },
                  cold: { air: 8, track: 11, humidity: 60, wind: 3, wind_dir: 20, rain: false } };
const demoWx = DEMO_WX[new URLSearchParams(location.search).get("wx")];
const wxEl = document.getElementById("wx");
function updateWeather(d) {
  if (demoWx) d.weather = demoWx;
  const w = d.weather;
  if (!w) { wxEl.style.display = "none"; return; }
  const icon = w.rain ? "🌧" : w.track >= 38 ? "☀️" : "⛅";
  const n = (v, u = "") => (v == null ? "–" : Math.round(v * 10) / 10 + u);
  wxEl.style.display = "block";
  wxEl.className = w.rain ? "rain" : "";
  wxEl.innerHTML = `<div class="big">${icon} ${w.rain ? "Rain" : "Dry"} · ${n(w.air, "°")}</div>` +
    `<div><span class="m">Track</span> ${n(w.track, "°C")} · <span class="m">Humidity</span> ${n(w.humidity, "%")}</div>` +
    `<div><span class="m">Wind</span> ${n(w.wind, " m/s")} <span class="arrow" style="transform:rotate(${(w.wind_dir || 0) + 180}deg)">↑</span></div>`;
}

// ---- race picker ----------------------------------------------------------------------------
const pickerEl = document.getElementById("picker");
const NOW_YEAR = new Date().getFullYear();
const YEARS = []; for (let y = NOW_YEAR; y >= 2024; y--) YEARS.push(y);
let pickerOpen = false, pickYear = YEARS[0], calendar = {}, calError = {}, serverState = "lobby", serverMsg = "";
let pickerKey = "";

function setPicker(d) {
  if (d.mode !== "replay") { pickerEl.style.display = "none"; return; }
  serverState = d.state || "playing"; serverMsg = d.message || "";
  const show = pickerOpen || serverState !== "playing";
  pickerEl.style.display = show ? "block" : "none";
  if (!show) return;
  const key = `${serverState}|${serverMsg}|${pickYear}|${!!calendar[pickYear]}|${calError[pickYear] || ""}|${pickerOpen}`;
  if (key !== pickerKey) { pickerKey = key; renderPicker(); }
  if (!calendar[pickYear] && !calError[pickYear] && !fetching[pickYear]) fetchYear(pickYear);
}
const fetching = {};
async function fetchYear(y) {
  fetching[y] = true;
  try {
    const r = await fetch(`/api/calendar?year=${y}`);
    const d = await r.json();
    if (d.error && !(d.races && d.races.length)) calError[y] = d.error; else calendar[y] = d.races;
  } catch (e) { calError[y] = "could not reach the server"; }
  fetching[y] = false; pickerKey = ""; setPicker({ mode: "replay", state: serverState, message: serverMsg });
}
function openPicker() { pickerOpen = true; pickerKey = ""; setPicker({ mode: "replay", state: serverState, message: serverMsg }); }
function closePicker() { pickerOpen = false; pickerKey = ""; setPicker({ mode: "replay", state: serverState, message: serverMsg }); }
async function startLoad(spec) {
  pickerOpen = false;
  serverState = "loading"; serverMsg = "Starting…"; pickerKey = "";
  try { await fetch("/api/load", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(spec) }); }
  catch (e) { serverState = "error"; serverMsg = "could not reach the server"; }
  setPicker({ mode: "replay", state: serverState, message: serverMsg });
}
function fmtDate(iso) {
  const d = new Date(iso + "T12:00:00Z");
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}
function renderPicker() {
  let body = "";
  if (serverState === "loading") {
    body = `<div class="msg"><span class="spin"></span>${esc(serverMsg || "Loading…")}</div>`;
  } else {
    const races = calendar[pickYear];
    let list;
    if (calError[pickYear]) list = `<div class="msg err">Couldn't load the ${pickYear} calendar: ${esc(calError[pickYear])}<br>The saved recording above still works.</div>`;
    else if (!races) list = `<div class="msg"><span class="spin"></span>Loading the ${pickYear} calendar…</div>`;
    else if (!races.length) list = `<div class="msg">No completed races found for ${pickYear}.</div>`;
    else list = `<div class="races">` + races.map((r, i) =>
      `<div class="race"><div class="n"><span class="rn">R${i + 1}</span>${esc(r.name)}</div>` +
      `<div class="m">${esc(r.location)}${r.country ? " · " + esc(r.country) : ""} · ${fmtDate(r.date)}</div>` +
      r.sessions.map((q) => `<button class="btn ${q.name === "Race" ? "go" : ""}" data-key="${q.key}" data-name="${esc(r.name)}">▶ ${q.name}</button>`).join("") +
      `</div>`).join("") + `</div>`;
    body = (serverState === "error" ? `<div class="msg err">${esc(serverMsg)}</div>` : "") +
      `<div class="feat"><div class="t"><b>São Paulo Grand Prix 2026 · Race</b><span>Recording of the real live feed, works offline</span></div>` +
      `<button class="btn go" data-bundled="1">▶ Watch</button></div>` +
      `<div class="tabs">${YEARS.map((y) => `<button data-year="${y}" class="${y === pickYear ? "on" : ""}">${y}</button>`).join("")}</div>` + list;
  }
  pickerEl.innerHTML = `<div class="panel">${serverState === "playing" || pickerOpen ? `<button class="close" title="Close">✕</button>` : ""}` +
    `<h2>Choose a race to replay</h2><p class="lead">Races from 2024 onwards. Race data and track maps are downloaded from OpenF1 when you pick one (cached on this computer afterwards).</p>` +
    body +
    `<p class="fine">Unofficial fan project, not associated in any way with the Formula 1 companies. Estimated positions, not GPS. Data via OpenF1.</p></div>`;
}
pickerEl.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.classList.contains("close")) closePicker();
  else if (b.dataset.year) { pickYear = Number(b.dataset.year); pickerKey = ""; setPicker({ mode: "replay", state: serverState, message: serverMsg }); }
  else if (b.dataset.bundled) startLoad({ kind: "bundled" });
  else if (b.dataset.key) startLoad({ kind: "openf1", session_key: Number(b.dataset.key), name: b.dataset.name });
});

// ---- replay controls ---------------------------------------------------------------------
const ctl = document.getElementById("ctl");
let ctlKey = "";
function updateControls(d) {
  if (d.mode !== "replay") { ctl.style.display = "none"; return; }
  ctl.style.display = "flex";
  const key = `${d.speed}|${d.paused}|${!!d.finished}`;
  if (key === ctlKey) return;
  ctlKey = key;
  ctl.innerHTML = `<button data-races="1" title="Choose another race">☰ Races</button>` +
    `<button data-r="1" title="Restart the replay">⟲</button>` +
    `<button data-p="1" class="${d.paused ? "on" : ""}">${d.paused ? "▶ Play" : "⏸ Pause"}</button>` +
    SPEEDS.map((s) => `<button data-s="${s}" class="${s === d.speed && !d.paused ? "on" : ""}">${s}×</button>`).join("");
}
ctl.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b || !ws || ws.readyState !== 1) return;
  if (b.dataset.races) { openPicker(); return; }
  if (b.dataset.r) { ws.send(JSON.stringify({ restart: true })); lastFastest = null; prevPos.clear(); trails.clear(); }
  else if (b.dataset.p) ws.send(JSON.stringify({ paused: !(latest && latest.paused) }));
  else ws.send(JSON.stringify({ speed: Number(b.dataset.s), paused: false }));
});
window.addEventListener("keydown", (e) => {
  if (!ws || ws.readyState !== 1 || !latest || latest.mode !== "replay") return;
  if (e.code === "Space") { e.preventDefault(); ws.send(JSON.stringify({ paused: !latest.paused })); }
  const i = SPEEDS.indexOf(latest.speed);
  if (e.key === "+" || e.key === "]") ws.send(JSON.stringify({ speed: SPEEDS[Math.min(SPEEDS.length - 1, i + 1)] }));
  if (e.key === "-" || e.key === "[") ws.send(JSON.stringify({ speed: SPEEDS[Math.max(0, i - 1)] }));
  if (e.key === "Escape") { pinned = null; markTower(); }
});

// ---- loop & socket ------------------------------------------------------------------------
function frame(now) {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (track && view) {
    drawTrack(now);
    drawCars(interpolatedCars(now), now);
    if (hovered || pinned) showCard(); else card.classList.remove("show");
  }
  requestAnimationFrame(frame);
}

function clearTower() {
  document.getElementById("rows").innerHTML = "";
  document.getElementById("outlist").innerHTML = "";
}

let wasPlaying = false;
function onFrame(data) {
  setPicker(data);
  const playingNow = !data.state || data.state === "playing";
  if (playingNow && !wasPlaying) resetScene();     // a new or restarted replay: start the scene clean
  wasPlaying = playingNow;
  const notice = document.getElementById("notice");
  if (data.state === "no_track") {                       // live, but no map for this circuit
    notice.textContent = data.notice || ""; notice.style.display = data.notice ? "block" : "none";
    snaps.length = 0; latest = null; clearTower(); return;
  }
  notice.style.display = "none";
  if (data.state && data.state !== "playing") {
    snaps.length = 0; latest = null; clearTower();
    document.getElementById("title").textContent = "F1 Live Tracker";
    document.getElementById("subtitle").textContent = data.state === "loading" ? "loading…" : "pick a race";
    return;
  }
  if (!track || (data.track && track.name !== data.track)) { ensureTrack(data.track); return; }
  if (data.label !== undefined && data.label !== currentLabel) { currentLabel = data.label; resetScene(); }
  const at = performance.now();
  latest = data;
  snaps.push({ at, data });
  while (snaps.length > 30) snaps.shift();
  if (at - lastTowerUpdate > 250) {
    lastTowerUpdate = at;
    updateTower(data);
    updateHeader(data);
  }
}

function connect() {
  const conn = document.getElementById("conn");
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { conn.className = "pill"; conn.title = "connected"; };
  ws.onmessage = (ev) => onFrame(JSON.parse(ev.data));
  ws.onclose = () => { conn.className = "pill conn-bad"; conn.title = "disconnected"; setTimeout(connect, 1500); };
}

const varCache = {};
function getVar(name) {
  return varCache[name] || (varCache[name] = getComputedStyle(document.documentElement).getPropertyValue(name).trim());
}

window.addEventListener("resize", fit);
fit();
connect();
requestAnimationFrame(frame);
