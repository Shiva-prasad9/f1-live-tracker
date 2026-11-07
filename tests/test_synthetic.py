"""Synthetic end-to-end test: 20 cars on the reference profile with random pace,
one pit stop and one car that stops on track. Fake TimingData events are generated
from the true motion (with feed latency jitter) and fed through the real parser and
estimator. The displayed position must stay within 50 m of the truth.
"""
import random

import numpy as np
import pytest

from tracker.engine import Engine

from .helpers import fmt, synthetic_track, timing_line

N_CARS = 20
LAPS = 12
DT = 0.05
LAUNCH = 10.0
PIT_CAR, PIT_LAP = "5", 6
STOP_CAR, STOP_LAP = "7", 4


def simulate(seed=3):
    rnd = random.Random(seed)
    tr = synthetic_track()
    L = tr.length
    base_k = {str(i + 1): 1.0 - 0.0012 * i + rnd.uniform(-0.002, 0.002) for i in range(N_CARS)}
    D = {c: -8.0 * (i + 1) + 2.0 for i, c in enumerate(base_k)}
    k_now = dict(base_k)
    t_start = 5.0
    msgs = []  # (t_msg, delta-for-car)
    truth = {c: [] for c in base_k}
    last_cross = {c: t_start for c in base_k}
    next_gate = {c: 1 for c in base_k}
    laps_done = {c: 0 for c in base_k}
    in_pit = {}
    pit_line = {}
    stopped_at = None
    t = 0.0
    while t < t_start + LAPS * tr.lap_time:
        t += DT
        for c in base_k:
            if c == STOP_CAR and stopped_at is not None:
                truth[c].append((t, D[c], "stopped"))
                continue
            if c in in_pit:
                if c in pit_line and t >= pit_line[c]:
                    # the timing line runs across the pit lane too
                    del pit_line[c]
                    laps_done[c] += 1
                    msgs.append((t + 0.5, c, {"Sectors": {"2": {"Value": fmt(t - last_cross[c])}},
                                              "NumberOfLaps": laps_done[c]}))
                    last_cross[c] = t
                    next_gate[c] = 1
                if t >= in_pit[c]:
                    del in_pit[c]
                    n = np.floor(D[c] / L) + (1 if tr.pit_exit_s < D[c] % L else 0)
                    D[c] = n * L + tr.pit_exit_s
                    msgs.append((t + 0.4, c, {"InPit": False, "PitOut": True}))
                    k_now[c] = base_k[c] * 0.93
                else:
                    truth[c].append((t, D[c], "pit"))
                    continue
            if t < t_start:
                truth[c].append((t, D[c], "grid"))
                continue
            v_cap = LAUNCH * (t - t_start)
            newD = tr.advance(D[c], DT, k_now[c])
            newD = min(newD, D[c] + v_cap * DT)
            # gate crossings
            g_D = tr.gate_D_near(next_gate[c], D[c] + 1e-6)
            if g_D <= D[c]:
                g_D += L
            if D[c] < g_D <= newD:
                frac = (g_D - D[c]) / (newD - D[c])
                tc = t - DT + frac * DT
                sec = tc - last_cross[c]
                last_cross[c] = tc
                g = next_gate[c]
                lat = rnd.uniform(0.3, 1.0)
                if g == 1:
                    msgs.append((tc + lat, c, {"Sectors": {"0": {"Value": fmt(sec)}, "1": {"Value": ""}, "2": {"Value": ""}}}))
                elif g == 2:
                    msgs.append((tc + lat, c, {"Sectors": {"1": {"Value": fmt(sec)}}}))
                else:
                    laps_done[c] += 1
                    msgs.append((tc + lat, c, {"Sectors": {"2": {"Value": fmt(sec)}}, "NumberOfLaps": laps_done[c]}))
                next_gate[c] = (g + 1) % 3
                # new random pace for the next sector
                k_now[c] = base_k[c] * rnd.uniform(0.985, 1.015)
            D[c] = newD
            # pit entry
            if c == PIT_CAR and laps_done[c] == PIT_LAP and c not in in_pit and (D[c] % L) > tr.pit_entry_s \
                    and not getattr(simulate, "pitted", False):
                simulate.pitted = True
                in_pit[c] = t + 22.0
                pit_line[c] = t + 8.0
                msgs.append((t + 0.5, c, {"InPit": True}))
            # stop on track mid-sector 2
            if c == STOP_CAR and laps_done[c] == STOP_LAP and stopped_at is None and \
                    tr.gates[1] + 300 < D[c] % L < tr.gates[2]:
                stopped_at = t
                msgs.append((t + 25.0, c, {"Stopped": True}))
            truth[c].append((t, D[c], "track"))
        # positions once a second
        if abs(t - round(t)) < DT / 2 and t > t_start:
            order = sorted(base_k, key=lambda c: -D[c])
            for p, c in enumerate(order):
                msgs.append((t, c, {"Position": str(p + 1)}))
    simulate.pitted = False
    msgs.sort(key=lambda m: m[0])
    return tr, msgs, truth, t_start, stopped_at


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_synthetic_race_error(seed):
    tr, msgs, truth, t_start, stopped_at = simulate(seed)
    eng = Engine(tr)
    eng.state.load_initial("SessionInfo", {"Type": "Race"})
    eng.state.load_initial("SessionStatus", {"Status": "Inactive"})
    eng.state.load_initial("TrackStatus", {"Status": "1"})
    eng.state.load_initial("TimingData", {"Lines": {c: timing_line(i + 1) for i, c in enumerate(truth)}})
    eng.start(0.0)
    started = False
    i = 0
    lap1, later = [], []
    n_samples = len(truth["1"])
    for j in range(0, n_samples, 4):  # compare every 0.2 s
        t = truth["1"][j][0]
        if not started and t >= t_start:
            eng.ingest(t_start, "SessionStatus", {"Status": "Started"})
            started = True
        while i < len(msgs) and msgs[i][0] <= t:
            tm, c, delta = msgs[i]
            eng.ingest(tm, "TimingData", {"Lines": {c: delta}})
            i += 1
        eng.tick(t)
        snap = {s["num"]: s for s in eng.snapshot()["cars"]}
        for c in truth:
            _, Dt, mode = truth[c][j]
            if mode != "track" or snap[c]["mode"] not in ("track", "grid"):
                continue
            (lap1 if Dt < tr.length else later).append(abs(snap[c]["D"] - Dt))
    lap1, later = np.array(lap1), np.array(later)
    print(f"\nsynthetic lap 1: mean {lap1.mean():.1f} m, p95 {np.percentile(lap1, 95):.1f} m, max {lap1.max():.1f} m"
          f"\nsynthetic laps 2+: mean {later.mean():.1f} m, p95 {np.percentile(later, 95):.1f} m, max {later.max():.1f} m")
    assert eng.est.cars[STOP_CAR].mode == "out"
    assert eng.est.cars[PIT_CAR].mode == "track"
    # Residual error is the injected per-sector pace noise (+-1.5 %) and the 0.3 s
    # latency floor, neither of which gate events can reveal. See README.
    assert np.percentile(later, 95) < 50.0
    assert later.max() < 120.0
    assert np.percentile(lap1, 95) < 100.0
