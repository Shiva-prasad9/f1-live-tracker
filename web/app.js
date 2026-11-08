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

// ---- track & view ---------------------------------------------------------------------
async function loadTrack() {
  const r = await fetch("/track");
  track = await r.json();
  fit();
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
  const pad = 50;
  const scale = Math.min((w - 2 * pad) / (maxx - minx), (h - 2 * pad) / (maxy - miny));
  view = { scale, ox: (w - scale * (maxx - minx)) / 2 - scale * minx,
           oy: (h + scale * (maxy - miny)) / 2 + scale * miny };
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
    `${d.session || ""} · ${d.session_status || "—"} · ${clock}${d.mode === "replay" ? ` · replay ${d.speed}×${d.paused ? " (paused)" : ""}` : ""}`;
  document.getElementById("lap").textContent = d.lap ? `Lap ${d.lap}${d.total_laps ? " / " + d.total_laps : ""}` : "";
  const [txt, cls] = FLAGS[d.track_status] || ["", ""];
  const flag = document.getElementById("flag");
  flag.textContent = txt;
  flag.className = "pill " + cls;
  updateControls(d);
  if (window.FX) FX.update(d);
}

// ---- replay controls ---------------------------------------------------------------------
const ctl = document.getElementById("ctl");
let ctlKey = "";
function updateControls(d) {
  if (d.mode !== "replay") { ctl.style.display = "none"; return; }
  ctl.style.display = "flex";
  const key = `${d.speed}|${d.paused}`;
  if (key === ctlKey) return;
  ctlKey = key;
  ctl.innerHTML = `<button data-p="1" class="${d.paused ? "on" : ""}">${d.paused ? "▶ Play" : "⏸ Pause"}</button>` +
    SPEEDS.map((s) => `<button data-s="${s}" class="${s === d.speed && !d.paused ? "on" : ""}">${s}×</button>`).join("");
}
ctl.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b || !ws || ws.readyState !== 1) return;
  if (b.dataset.p) ws.send(JSON.stringify({ paused: !(latest && latest.paused) }));
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

function connect() {
  const conn = document.getElementById("conn");
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { conn.className = "pill"; conn.title = "connected"; };
  ws.onmessage = (ev) => {
    const data = JSON.parse(ev.data);
    const at = performance.now();
    latest = data;
    snaps.push({ at, data });
    while (snaps.length > 30) snaps.shift();
    if (at - lastTowerUpdate > 250) {
      lastTowerUpdate = at;
      updateTower(data);
      updateHeader(data);
    }
  };
  ws.onclose = () => { conn.className = "pill conn-bad"; conn.title = "disconnected"; setTimeout(connect, 1500); };
}

const varCache = {};
function getVar(name) {
  return varCache[name] || (varCache[name] = getComputedStyle(document.documentElement).getPropertyValue(name).trim());
}

window.addEventListener("resize", fit);
loadTrack().then(() => { connect(); requestAnimationFrame(frame); });
