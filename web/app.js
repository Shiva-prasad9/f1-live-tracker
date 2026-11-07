// F1 Live Tracker front end: track map + cars on a canvas, timing tower.
// Snapshots arrive at ~10 Hz; we render ~120 ms in the past and interpolate between
// the two snapshots around that time so motion is smooth at display frame rate.
"use strict";

const canvas = document.getElementById("c");
const ctx = canvas.getContext("2d");
const RENDER_DELAY = 120; // ms
const TYRES = { SOFT: ["S", "#e5484d"], MEDIUM: ["M", "#f5c518"], HARD: ["H", "#e8e8ea"],
                INTERMEDIATE: ["I", "#3fb950"], WET: ["W", "#4c8dff"] };
const FLAGS = { "1": ["GREEN", ""], "2": ["YELLOW", "sc"], "4": ["SAFETY CAR", "sc"], "5": ["RED FLAG", "red"],
                "6": ["VSC", "sc"], "7": ["VSC ENDING", "sc"] };

let track = null;
let view = null;           // {scale, ox, oy}
const snaps = [];          // [{at: performance.now(), data}]
let lastTowerUpdate = 0;

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
  const pad = 40;
  const scale = Math.min((w - 2 * pad) / (maxx - minx), (h - 2 * pad) / (maxy - miny));
  view = { scale, ox: (w - scale * (maxx - minx)) / 2 - scale * minx,
           oy: (h + scale * (maxy - miny)) / 2 + scale * miny };
}

const sx = (x) => view.ox + view.scale * x;
const sy = (y) => view.oy - view.scale * y;   // y up in the F1 frame

function drawTrack() {
  const { x, y } = track;
  const path = () => {
    ctx.beginPath();
    ctx.moveTo(sx(x[0]), sy(y[0]));
    for (let i = 1; i < x.length; i++) ctx.lineTo(sx(x[i]), sy(y[i]));
    ctx.closePath();
  };
  ctx.lineJoin = "round";
  ctx.strokeStyle = getVar("--track-edge"); ctx.lineWidth = 14; path(); ctx.stroke();
  ctx.strokeStyle = getVar("--track"); ctx.lineWidth = 11; path(); ctx.stroke();
  if (track.pit_lane && track.pit_lane.length > 1) {
    ctx.strokeStyle = getVar("--pit"); ctx.lineWidth = 6; ctx.setLineDash([]);
    ctx.beginPath();
    track.pit_lane.forEach(([px, py], i) => (i ? ctx.lineTo(sx(px), sy(py)) : ctx.moveTo(sx(px), sy(py))));
    ctx.stroke();
  }
  // start/finish line and sector gates
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

function drawCars(cars) {
  // draw back-markers first so leaders are on top
  const sorted = [...cars].filter((c) => c.x != null && c.mode !== "out").sort((p, q) => (q.pos || 99) - (p.pos || 99));
  ctx.font = "600 11px system-ui, sans-serif";
  ctx.textBaseline = "middle";
  for (const c of sorted) {
    const x = sx(c.x), y = sy(c.y);
    ctx.beginPath();
    ctx.arc(x, y, 6, 0, Math.PI * 2);
    ctx.fillStyle = c.color;
    ctx.fill();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = c.mode === "pit" ? getVar("--sc") : "#000";
    ctx.stroke();
    ctx.fillStyle = getVar("--text");
    ctx.fillText(c.code, x + 9, y);
  }
}

// ---- tower ----------------------------------------------------------------------------------
function updateTower(data) {
  const el = document.getElementById("rows");
  const out = document.getElementById("outlist");
  const live = data.cars.filter((c) => c.mode !== "out").sort((a, b) => (a.pos || 99) - (b.pos || 99));
  const gone = data.cars.filter((c) => c.mode === "out");
  el.innerHTML = live.map(row).join("");
  out.innerHTML = gone.length ? `<div class="label">OUT</div>` + gone.map(row).join("") : "";
}

function row(c) {
  const [tl, tc] = TYRES[(c.tyre || "").toUpperCase()] || ["", "#666"];
  const gap = c.pos === 1 ? "Leader" : (c.gap || "");
  const cls = c.mode === "pit" ? "row pit" : c.mode === "out" ? "row out" : "row";
  return `<div class="${cls}"><span class="pos">${c.pos ?? ""}</span>` +
    `<span class="bar" style="background:${c.color}"></span><span class="code">${c.code}</span>` +
    `<span class="gap">${c.mode === "out" ? (c.out || "OUT") : escapeHtml(gap)}</span>` +
    `<span class="tyre" style="color:${tc}">${tl}</span></div>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}

function updateHeader(d) {
  document.getElementById("title").textContent = d.meeting || "F1 Live Tracker";
  const clock = d.t ? new Date(d.t * 1000).toISOString().slice(11, 19) + " UTC" : "";
  document.getElementById("subtitle").textContent =
    `${d.session || ""} · ${d.session_status || "—"} · ${clock}${d.mode === "replay" ? ` · replay ${d.speed}×` : ""}`;
  document.getElementById("lap").textContent = d.lap ? `Lap ${d.lap}${d.total_laps ? " / " + d.total_laps : ""}` : "";
  const [txt, cls] = FLAGS[d.track_status] || ["", ""];
  const flag = document.getElementById("flag");
  flag.textContent = txt;
  flag.className = "pill " + cls;
}

// ---- loop & socket ------------------------------------------------------------------------
function frame(now) {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (track && view) {
    drawTrack();
    drawCars(interpolatedCars(now));
  }
  requestAnimationFrame(frame);
}

function connect() {
  const conn = document.getElementById("conn");
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { conn.className = "pill"; conn.title = "connected"; };
  ws.onmessage = (ev) => {
    const data = JSON.parse(ev.data);
    const at = performance.now();
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
