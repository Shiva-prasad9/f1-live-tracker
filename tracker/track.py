"""Track model: centreline polyline with arc length s, reference lap-time profile
t_ref(s) (equivalently a reference speed v_ref(s)), sector gates and pit info.

Coordinates are metres in the Position.z / OpenF1 `location` frame (raw / 10).

Distances along the race are handled as a continuous "race distance"
D = lap * L + s, so lap wrap-around never needs special casing in the estimator.
The same trick is used for time: reference time tau = lap * T + t_ref(s).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

TRACK_DIR = Path(__file__).resolve().parent.parent / "tracks"


@dataclass
class Track:
    name: str
    length: float                 # L, metres
    lap_time: float               # T, reference lap time in seconds
    s: np.ndarray                 # arc length samples, 0..L (inclusive)
    x: np.ndarray
    y: np.ndarray
    t: np.ndarray                 # reference time into lap at s, 0..T (inclusive, monotonic)
    gates: list[float]            # s of gate 0 (line), gate 1 (end S1), gate 2 (end S2)
    pit_entry_s: float
    pit_exit_s: float
    pit_lane: list[list[float]] = field(default_factory=list)  # optional [[x, y], ...]
    meta: dict = field(default_factory=dict)

    # ---- construction / io -------------------------------------------------
    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        return cls(
            name=d["name"], length=float(d["length"]), lap_time=float(d["lap_time"]),
            s=np.asarray(d["s"], float), x=np.asarray(d["x"], float),
            y=np.asarray(d["y"], float), t=np.asarray(d["t"], float),
            gates=[float(g) for g in d["gates"]],
            pit_entry_s=float(d["pit_entry_s"]), pit_exit_s=float(d["pit_exit_s"]),
            pit_lane=d.get("pit_lane", []), meta=d.get("meta", {}),
        )

    def to_dict(self) -> dict:
        r = lambda a, n=2: [round(float(v), n) for v in a]  # noqa: E731
        return {
            "name": self.name, "length": round(self.length, 2), "lap_time": round(self.lap_time, 3),
            "gates": [round(g, 2) for g in self.gates],
            "pit_entry_s": round(self.pit_entry_s, 1), "pit_exit_s": round(self.pit_exit_s, 1),
            "meta": self.meta,
            "s": r(self.s), "x": r(self.x), "y": r(self.y), "t": r(self.t, 4),
            "pit_lane": [[round(p[0], 1), round(p[1], 1)] for p in self.pit_lane],
        }

    @classmethod
    def load(cls, name: str) -> "Track":
        return cls.from_dict(json.loads((TRACK_DIR / f"{name}.json").read_text()))

    def save(self) -> Path:
        TRACK_DIR.mkdir(exist_ok=True)
        p = TRACK_DIR / f"{self.name}.json"
        p.write_text(json.dumps(self.to_dict(), separators=(",", ":")))
        return p

    # ---- geometry ----------------------------------------------------------
    def wrap(self, D: float) -> float:
        return D % self.length

    def xy(self, D: float, lateral: float = 0.0) -> tuple[float, float]:
        """Point on the centreline at race distance D, optionally offset sideways (m, +left)."""
        s = D % self.length
        x = float(np.interp(s, self.s, self.x))
        y = float(np.interp(s, self.s, self.y))
        if lateral:
            nx, ny = self.normal(s)
            x += lateral * nx
            y += lateral * ny
        return x, y

    def normal(self, s: float) -> tuple[float, float]:
        ds = 3.0
        x0, y0 = self.xy(s - ds)
        x1, y1 = self.xy(s + ds)
        dx, dy = x1 - x0, y1 - y0
        n = float(np.hypot(dx, dy)) or 1.0
        return -dy / n, dx / n

    def project(self, x: float, y: float, near_s: float | None = None, window: float = 400.0):
        """Closest centreline point to (x, y). Returns (s, distance). Optionally search near s."""
        if near_s is None:
            idx = np.arange(len(self.s) - 1)
        else:
            step = self.s[1] - self.s[0]
            n = len(self.s) - 1
            c = int(round((near_s % self.length) / step))
            w = int(window / step)
            idx = np.arange(c - w, c + w + 1) % n
        ax, ay = self.x[idx], self.y[idx]
        bx, by = self.x[idx + 1], self.y[idx + 1]
        vx, vy = bx - ax, by - ay
        l2 = vx * vx + vy * vy
        l2[l2 == 0] = 1e-9
        u = np.clip(((x - ax) * vx + (y - ay) * vy) / l2, 0, 1)
        px, py = ax + u * vx, ay + u * vy
        d = np.hypot(px - x, py - y)
        i = int(np.argmin(d))
        s = self.s[idx[i]] + u[i] * (self.s[idx[i] + 1] - self.s[idx[i]])
        return float(s % self.length), float(d[i])

    # ---- reference timing --------------------------------------------------
    def ref_time(self, D: float) -> float:
        """Reference time tau for continuous race distance D (tau = lap*T + t_ref(s))."""
        lap = np.floor(D / self.length)
        return float(lap * self.lap_time + np.interp(D - lap * self.length, self.s, self.t))

    def dist_at_ref_time(self, tau: float) -> float:
        """Inverse of ref_time."""
        lap = np.floor(tau / self.lap_time)
        return float(lap * self.length + np.interp(tau - lap * self.lap_time, self.t, self.s))

    def v_ref(self, D: float) -> float:
        s = D % self.length
        ds = 5.0
        s0, s1 = s - ds, s + ds
        dt = self.ref_time(s1) - self.ref_time(s0)
        return 2 * ds / dt if dt > 0 else 50.0

    def advance(self, D: float, dt: float, k: float) -> float:
        """Move along the reference profile for dt seconds at pace factor k (ds/dt = k*v_ref(s))."""
        if dt <= 0 or k <= 0:
            return D
        return self.dist_at_ref_time(self.ref_time(D) + k * dt)

    def gate_D_near(self, gate: int, D: float) -> float:
        """Race distance of gate `gate` closest to D."""
        g = self.gates[gate]
        n = round((D - g) / self.length)
        return n * self.length + g

    def sector_ref_time(self, sector: int) -> float:
        """Reference duration of sector 0, 1, 2."""
        g = self.gates + [self.length]
        a, b = g[sector], g[sector + 1]
        return float(np.interp(b, self.s, self.t) - np.interp(a, self.s, self.t))
