"""Build a Track model from OpenF1 historical data (free, no auth).

1. Most recent past Race session at the circuit.
2. Clean fast lap (fastest green lap that is not an in/out lap).
3. `location` samples of that driver over the lap -> smoothed periodic centreline.
4. time-into-lap -> s profile (reference speed), sector gates, pit entry/exit.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import numpy as np

from . import openf1
from .track import Track

log = logging.getLogger(__name__)
SCALE = 0.1  # Position.z / OpenF1 location units are decimetres


def parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def find_race_session(circuit: str, before: datetime | None = None, session_name: str = "Race") -> dict:
    before = before or datetime.now(timezone.utc)
    sessions = openf1.get("sessions", circuit_short_name=circuit, session_name=session_name)
    past = [s for s in sessions if s.get("date_end") and parse_date(s["date_end"]) < before]
    if not past:
        raise RuntimeError(f"no past {session_name} session at {circuit}")
    return max(past, key=lambda s: s["date_start"])


def _sc_windows(session_key: int) -> list[tuple[datetime, datetime]]:
    """Rough SC/VSC/red-flag windows from race control (to avoid picking a neutralised lap)."""
    msgs = openf1.get("race_control", session_key=session_key)
    wins, start = [], None
    for m in sorted(msgs, key=lambda m: m["date"]):
        txt = (m.get("message") or "").upper()
        if start is None and ("DEPLOYED" in txt or m.get("flag") == "RED"):
            start = parse_date(m["date"])
        elif start is not None and ("IN THIS LAP" in txt or "ENDING" in txt or m.get("flag") == "GREEN"):
            wins.append((start, parse_date(m["date"]) + timedelta(seconds=60)))
            start = None
    if start is not None:
        wins.append((start, start + timedelta(hours=3)))
    return wins


def pick_reference_lap(session_key: int) -> dict:
    laps = openf1.get("laps", session_key=session_key)
    sc = _sc_windows(session_key)
    pit_laps = {(p["driver_number"], p["lap_number"]) for p in openf1.get("pit", session_key=session_key)}
    good = []
    for lap in laps:
        if not lap.get("lap_duration") or not lap.get("date_start") or lap.get("is_pit_out_lap"):
            continue
        if not lap.get("duration_sector_1") or not lap.get("duration_sector_2") or not lap.get("duration_sector_3"):
            continue
        if lap["lap_number"] <= 1 or (lap["driver_number"], lap["lap_number"]) in pit_laps:
            continue
        t0 = parse_date(lap["date_start"])
        t1 = t0 + timedelta(seconds=lap["lap_duration"])
        if any(a < t1 and t0 < b for a, b in sc):
            continue
        good.append(lap)
    if not good:
        raise RuntimeError("no clean lap found")
    good.sort(key=lambda lap: lap["lap_duration"])
    return good[0]


def fetch_location(session_key: int, driver: int, t0: datetime, t1: datetime) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (t seconds since t0, x m, y m)."""
    rows = openf1.get("location", f"date>={iso(t0)}", f"date<={iso(t1)}",
                      session_key=session_key, driver_number=driver)
    rows = [r for r in rows if r.get("x") is not None]
    rows.sort(key=lambda r: r["date"])
    t = np.array([(parse_date(r["date"]) - t0).total_seconds() for r in rows])
    x = np.array([r["x"] for r in rows], float) * SCALE
    y = np.array([r["y"] for r in rows], float) * SCALE
    return t, x, y


def _periodic_smooth(v: np.ndarray, keep: int) -> np.ndarray:
    """Low-pass a periodic, uniformly-sampled signal by keeping the first `keep` harmonics."""
    F = np.fft.rfft(v)
    F[keep + 1:] = 0
    return np.fft.irfft(F, n=len(v))


def build_centreline(t: np.ndarray, x: np.ndarray, y: np.ndarray, T: float,
                     harmonics: int = 180, ds: float = 2.0):
    """Turn one lap of samples into (s, x, y, t_ref) arrays on a uniform s grid."""
    n = 4000
    tu = np.linspace(0, T, n, endpoint=False)
    xu = np.interp(tu, t, x)
    yu = np.interp(tu, t, y)
    # close the loop: distribute the start/end mismatch linearly over the lap
    xe, ye = np.interp(T, t, x), np.interp(T, t, y)
    ramp = tu / T
    xu -= ramp * (xe - xu[0])
    yu -= ramp * (ye - yu[0])
    xs, ys = _periodic_smooth(xu, harmonics), _periodic_smooth(yu, harmonics)
    # arc length along the smoothed loop
    xc, yc = np.append(xs, xs[0]), np.append(ys, ys[0])
    tc = np.append(tu, T)
    seg = np.hypot(np.diff(xc), np.diff(yc))
    sc = np.concatenate([[0], np.cumsum(seg)])
    L = float(sc[-1])
    sg = np.linspace(0, L, int(round(L / ds)) + 1)
    return sg, np.interp(sg, sc, xc), np.interp(sg, sc, yc), np.interp(sg, sc, tc), L


def find_pit_lane(track: Track, session_key: int, ref_lap_date: datetime):
    """Find pit entry/exit s from a real pit stop's trajectory (points far from the centreline)."""
    pits = openf1.get("pit", session_key=session_key)
    pits = [p for p in pits if p.get("date") and (p.get("lane_duration") or p.get("pit_duration"))]
    pits.sort(key=lambda p: (p.get("lane_duration") or p.get("pit_duration") or 99))
    for p in pits[:5]:
        tp = parse_date(p["date"])
        t, x, y = fetch_location(session_key, p["driver_number"], tp - timedelta(seconds=60), tp + timedelta(seconds=90))
        if len(t) < 50:
            continue
        proj = [track.project(xi, yi) for xi, yi in zip(x, y)]
        d = np.array([q[1] for q in proj])
        s = np.array([q[0] for q in proj])
        off = d > 9.0
        if off.sum() < 10:
            continue
        # longest run of off-track samples = pit lane
        best, cur, start = (0, 0, 0), 0, 0
        for i, o in enumerate(off):
            if o:
                if cur == 0:
                    start = i
                cur += 1
                if cur > best[0]:
                    best = (cur, start, i)
            else:
                cur = 0
        _, a, b = best
        lane = [[float(x[i]), float(y[i])] for i in range(a, b + 1)]
        # include the merge points so the drawn lane connects to the track
        a0, b1 = max(a - 1, 0), min(b + 1, len(s) - 1)
        log.info("pit lane from car %s: %d samples, entry s=%.0f exit s=%.0f",
                 p["driver_number"], b - a + 1, s[a0], s[b1])
        return float(s[a0]), float(s[b1]), lane
    log.warning("pit lane not found; using defaults around the line")
    return track.length - 300.0, 300.0, []


def build_track(circuit: str = "Interlagos", name: str | None = None) -> Track:
    sess = find_race_session(circuit)
    key = sess["session_key"]
    log.info("reference session %s %s (%s)", sess.get("year"), sess.get("session_name"), key)
    lap = pick_reference_lap(key)
    T = float(lap["lap_duration"])
    t0 = parse_date(lap["date_start"])
    log.info("reference lap: car %s lap %s %.3fs", lap["driver_number"], lap["lap_number"], T)
    t, x, y = fetch_location(key, lap["driver_number"], t0 - timedelta(seconds=3), t0 + timedelta(seconds=T + 3))
    t = t - 3.0
    s, xs, ys, ts, L = build_centreline(t, x, y, T)
    g1 = float(np.interp(lap["duration_sector_1"], ts, s))
    g2 = float(np.interp(lap["duration_sector_1"] + lap["duration_sector_2"], ts, s))
    track = Track(name=name or circuit.lower().replace(" ", "_"), length=L, lap_time=T,
                  s=s, x=xs, y=ys, t=ts, gates=[0.0, g1, g2], pit_entry_s=L - 300, pit_exit_s=300)
    entry, exit_, lane = find_pit_lane(track, key, t0)
    track.pit_entry_s, track.pit_exit_s, track.pit_lane = entry, exit_, lane
    track.meta = {
        "source": "OpenF1", "session_key": key, "year": sess.get("year"),
        "circuit": circuit, "ref_driver": lap["driver_number"], "ref_lap": lap["lap_number"],
        "sector_times": [lap["duration_sector_1"], lap["duration_sector_2"], lap["duration_sector_3"]],
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return track
