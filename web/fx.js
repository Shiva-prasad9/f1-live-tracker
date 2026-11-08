// Background effects: slow aurora blobs that follow the race state + speed streaks that follow the
// replay speed. Self-contained: remove the <script> tag and #bg canvas to get the plain look back.
"use strict";
(function () {
  const cv = document.getElementById("bg");
  const g = cv.getContext("2d");
  const PALETTES = {           // [blob colours as r,g,b]
    green:  [[40, 90, 220], [120, 60, 220], [0, 170, 190], [70, 60, 200]],
    sc:     [[255, 170, 0], [255, 110, 20], [200, 140, 0], [255, 190, 60]],
    red:    [[235, 40, 50], [170, 20, 60], [255, 80, 40], [200, 30, 30]],
    purple: [[190, 90, 255], [120, 60, 255], [230, 90, 220], [150, 70, 240]],
  };
  const st = { mood: "green", speed: 1, paused: false, boost: 0, on: true };
  try { st.on = localStorage.getItem("fx") !== "off"; } catch (e) {}
  if (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches) st.on = false;

  const blobs = Array.from({ length: 4 }, (_, i) => ({
    ph: i * 1.7, sp: 0.00006 + i * 0.000015, r: 0.32 + 0.06 * (i % 2),
    c: PALETTES.green[i].slice(),
  }));
  const streaks = Array.from({ length: 46 }, () => mk(true));
  function mk(anywhere) {
    return { x: Math.random(), y: Math.random(), l: 0.05 + Math.random() * 0.14,
             v: 0.04 + Math.random() * 0.1, a: 0.04 + Math.random() * 0.08, w: 0.6 + Math.random() * 1.4,
             h: Math.random() < 0.5 ? 215 : 265 };
  }

  function size() {
    const d = Math.min(window.devicePixelRatio || 1, 2);
    cv.width = cv.clientWidth * d; cv.height = cv.clientHeight * d;
    g.setTransform(d, 0, 0, d, 0, 0);
  }
  window.addEventListener("resize", size);

  let last = performance.now();
  function frame(now) {
    requestAnimationFrame(frame);
    const dt = Math.min(64, now - last); last = now;
    const W = cv.clientWidth, H = cv.clientHeight;
    g.clearRect(0, 0, W, H);
    if (!st.on) return;
    st.boost = Math.max(0, st.boost - dt / 2200);
    const pal = PALETTES[st.boost > 0 ? "purple" : st.mood];
    const k = Math.min(1, dt / 500);                       // ease colours toward the palette
    g.globalCompositeOperation = "lighter";
    blobs.forEach((b, i) => {
      for (let j = 0; j < 3; j++) b.c[j] += (pal[i][j] - b.c[j]) * k;
      b.ph += dt * b.sp * (st.paused ? 0.2 : 1);
      const x = W * (0.5 + 0.42 * Math.sin(b.ph * 2.1 + i)), y = H * (0.5 + 0.38 * Math.cos(b.ph * 1.7 + i * 2));
      const r = Math.max(W, H) * b.r;
      const gr = g.createRadialGradient(x, y, 0, x, y, r);
      const a = 0.17 + st.boost * 0.14;
      gr.addColorStop(0, `rgba(${b.c.map(Math.round)},${a})`);
      gr.addColorStop(1, `rgba(${b.c.map(Math.round)},0)`);
      g.fillStyle = gr; g.fillRect(0, 0, W, H);
    });
    // speed streaks: faster replay -> faster, longer streaks; paused -> frozen
    const mult = st.paused ? 0 : 0.35 + Math.log2(st.speed + 1) * 0.55;
    g.lineCap = "round";
    for (const s of streaks) {
      s.x += s.v * mult * dt / 1000; s.y -= s.v * mult * dt / 1000 * 0.36;
      if (s.x > 1.2 || s.y < -0.2) { Object.assign(s, mk()); s.x = -0.15; s.y = Math.random() * 1.2; }
      const len = s.l * (0.6 + mult * 0.35) * W;
      const x = s.x * W, y = s.y * H;
      g.strokeStyle = `hsla(${s.h},90%,70%,${s.a})`; g.lineWidth = s.w;
      g.beginPath(); g.moveTo(x, y); g.lineTo(x - len, y + len * 0.36); g.stroke();
    }
    g.globalCompositeOperation = "source-over";
  }

  function setOn(v) {
    st.on = v; document.body.classList.toggle("fx-on", v);
    try { localStorage.setItem("fx", v ? "on" : "off"); } catch (e) {}
    const b = document.getElementById("fxbtn"); if (b) b.classList.toggle("on", v);
  }
  window.FX = {
    update(d) {                                         // called from app.js with each header refresh
      const ts = String(d.track_status);
      st.mood = ts === "5" ? "red" : ["2", "4", "6", "7"].includes(ts) ? "sc" : "green";
      st.speed = d.mode === "replay" ? d.speed : 1; st.paused = !!d.paused;
    },
    pulse() { st.boost = 1; },                          // new fastest lap
    toggle() { setOn(!st.on); },
  };
  window.addEventListener("keydown", (e) => { if (e.key === "b" || e.key === "B") window.FX.toggle(); });
  document.getElementById("fxbtn").addEventListener("click", () => window.FX.toggle());
  size(); setOn(st.on); requestAnimationFrame(frame);
})();
