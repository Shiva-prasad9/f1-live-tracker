"""Synthetic track and timing-event generation for tests."""
from __future__ import annotations

import numpy as np

from tracker.track import Track


def synthetic_track(L: float = 4300.0, T: float = 72.0) -> Track:
    """A wobbly loop with a speed profile that varies between ~40 and ~90 m/s."""
    s = np.linspace(0, L, int(L / 2) + 1)
    th = 2 * np.pi * s / L
    r = L / (2 * np.pi) * (1 + 0.15 * np.sin(3 * th))
    x, y = r * np.cos(th), r * np.sin(th)
    v = 65 + 25 * np.cos(4 * th)  # m/s
    t = np.concatenate([[0], np.cumsum(np.diff(s) / ((v[1:] + v[:-1]) / 2))])
    t *= T / t[-1]
    return Track(name="synthetic", length=L, lap_time=T, s=s, x=x, y=y, t=t,
                 gates=[0.0, L * 0.3, L * 0.65], pit_entry_s=L - 250, pit_exit_s=250,
                 pit_lane=[[float(xx), float(yy) + 20] for xx, yy in zip(x[-100::10], y[-100::10])])


def timing_line(pos: int, laps: int = 0) -> dict:
    return {"Position": str(pos), "Line": pos, "NumberOfLaps": laps, "InPit": False, "PitOut": False,
            "Retired": False, "Stopped": False, "GapToLeader": "",
            "Sectors": [{"Value": ""}, {"Value": ""}, {"Value": ""}]}


def fmt(sec: float) -> str:
    return f"{int(sec // 60)}:{sec % 60:06.3f}" if sec >= 60 else f"{sec:.3f}"
