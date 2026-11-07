"""Per-car position estimator: dead reckoning along a reference lap profile,
corrected by timing gate events (sector ends and the finish line).

Car state is a continuous race distance D (metres, D = lap*L + s). Prediction:
    dD/dt = k * f_track * v_ref(s)
with pace factor k and a neutralisation factor f_track under SC/VSC.
Corrections are blended in over ~1.5 s and never move a car backwards.
"""
from __future__ import annotations

import bisect
import logging
import math
from collections import deque
from dataclasses import dataclass, field

from .state import SessionState, _sector_value as _sector_val, parse_gap
from .track import Track

log = logging.getLogger(__name__)

K_MIN, K_MAX = 0.85, 1.10
SC_FACTOR = 0.6
SC_CODES = {"4", "6", "7"}
RED_CODES = {"5"}
BLEND_TAU = 0.5            # s; ~95 % of a correction is applied within 1.5 s
SNAP_DIST = 400.0          # m; errors bigger than this snap (rejoin, reconnect)
HOLD_START = 80.0          # m past an un-reported gate: start slowing down
HOLD_LIMIT = 120.0         # m past an un-reported gate: hold here
GRID_SLOT = 8.0            # m between grid slots
GRID_LATERAL = 3.0         # m left/right stagger
LAUNCH_ACCEL = 10.0        # m/s^2 from a standing start
PIT_K = 0.92               # pace multiplier on an out-lap until the next gate
PIT_LANE_TIME = 22.0       # s, rough pit-lane traversal time for drawing
ORDER_GAP = 6.0            # m minimum drawn gap to the car ahead in Position
HISTORY_S = 150.0          # s of leader history for the gap fallback
CHAIN_RESET_S = 5.0        # s; restart the sector-time chain if it disagrees this much
ASSUMED_LATENCY = 0.0      # s; constant feed latency floor to subtract (unknown, see README)


@dataclass
class Car:
    num: str
    D: float = 0.0
    k: float = 1.0
    mode: str = "unknown"      # unknown | grid | track | gap | pit | out
    corr: float = 0.0          # pending correction (m)
    last_gate: int | None = None
    last_gate_D: float | None = None
    last_gate_t: float | None = None
    out_lap: bool = False
    pit_t: float = 0.0
    pit_D: float = 0.0
    grid_slot: int = 0
    out_reason: str = ""
    draw_D: float = 0.0
    events: int = 0
    chain_anchor: float | None = None
    chain_sum: float = 0.0


@dataclass
class Estimator:
    track: Track
    state: SessionState
    cars: dict[str, Car] = field(default_factory=dict)
    t: float | None = None
    start_t: float | None = None       # race start (lights out)
    leader_hist: deque = field(default_factory=lambda: deque(maxlen=4000))
    is_race: bool = True

    # ---- helpers ---------------------------------------------------------------
    def car(self, num: str) -> Car:
        c = self.cars.get(num)
        if c is None:
            c = self.cars[num] = Car(num=num)
        return c

    def _f_track(self) -> float:
        st = self.state.track_status
        if st in RED_CODES:
            return 0.0
        return SC_FACTOR if st in SC_CODES else 1.0

    # ---- initialisation --------------------------------------------------------
    def init_from_state(self, t: float) -> None:
        """Place every car from the current state (start of replay / live connect)."""
        self.t = t
        self.is_race = self.state.session_type in ("Race", "Sprint", "")
        started = self.state.session_status in ("Started", "Finished", "Finalised", "Ends", "Aborted")
        for num in sorted(set(self.state.drivers) | set(self.state.lines)):
            line = self.state.lines.get(num, {})
            c = self.car(num)
            if line.get("Retired") or line.get("Stopped"):
                c.mode, c.out_reason = "out", "Retired" if line.get("Retired") else "Stopped"
            elif line.get("InPit"):
                self._enter_pit(c, t)
            elif self.is_race and not started:
                self._place_on_grid(c)
            else:
                self._place_from_sectors(c, line)
        if self.is_race and started and self.start_t is None:
            self.start_t = t - 3600  # joined mid-race: launch limit never applies

    def _place_on_grid(self, c: Car) -> None:
        gp = self.state.grid_pos(c.num) or 20
        c.grid_slot = gp
        c.D = c.draw_D = -GRID_SLOT * gp + 2.0
        c.mode, c.k, c.corr = "grid", 1.0, 0.0
        c.last_gate, c.last_gate_D = 0, c.D  # next gate to expect: end of S1

    def _place_from_sectors(self, c: Car, line: dict) -> None:
        """Mid-session join: car is somewhere after its last completed sector."""
        laps = self.state.laps_done(c.num)
        done = [i for i in range(3) if _sector_val(line, i)]
        g = max(done) + 1 if done and max(done) < 2 else 0
        D = laps * self.track.length + self.track.gates[g]
        c.D = c.draw_D = D
        c.last_gate, c.last_gate_D, c.last_gate_t = g, D, None
        c.mode = "gap"  # refined by the gap fallback until the first real gate event

    # ---- events ------------------------------------------------------------------
    def handle(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "gate":
            _, num, t, gate, sector_s = ev
            self._gate(self.car(num), t, gate, sector_s)
        elif kind == "pit_in":
            c = self.car(ev[1])
            if c.mode != "out":
                self._enter_pit(c, ev[2])
        elif kind == "pit_out":
            self._exit_pit(self.car(ev[1]), ev[2])
        elif kind == "out":
            c = self.car(ev[1])
            c.mode, c.out_reason = "out", ev[3]
        elif kind == "session_status":
            if ev[2] == "Started" and self.is_race and self.start_t is None:
                self.start_t = ev[1]
                for c in self.cars.values():
                    if c.mode == "grid":
                        c.mode = "track"
                log.info("race start at %.1f", ev[1])

    def _gate(self, c: Car, t_msg: float, gate: int, sector_s: float | None) -> None:
        if c.mode in ("out",):
            return
        c.events += 1
        if c.mode == "pit":
            # line crossings happen in the pit lane too; just remember them
            c.last_gate, c.last_gate_t = gate, t_msg
            c.last_gate_D = self.track.gate_D_near(gate, c.pit_D)
            return
        t_cross = self._crossing_time(c, gate, t_msg, sector_s)
        prev_gate = (gate - 1) % 3
        # which instance of this gate (lap)? the one nearest to where we believe the car was
        D_then = c.D - (self.t - t_cross) * self.track.v_ref(c.D) * c.k if self.t else c.D
        if c.last_gate_D is not None and c.mode in ("track", "grid"):
            # must be ahead of the previous gate event
            D_gate = self.track.gate_D_near(gate, max(D_then, c.last_gate_D + 50))
            if D_gate <= c.last_gate_D:
                D_gate += self.track.length
        else:
            D_gate = self.track.gate_D_near(gate, D_then)
        # pace from the sector that just finished (green, no pit involvement)
        # (not from a sector that began on the grid: it includes the standing start)
        if (sector_s and c.last_gate == prev_gate and not c.out_lap and c.last_gate_t is not None
                and self.state.track_status in ("1", "2") and self._f_track() == 1.0):
            ref = self.track.sector_ref_time(prev_gate)
            km = ref / sector_s
            if 0.75 < km < 1.2:
                c.k = min(K_MAX, max(K_MIN, 0.6 * c.k + 0.4 * km))
        c.out_lap = False
        # where the car should be now
        target = self.track.advance(D_gate, max(0.0, (self.t or t_cross) - t_cross), c.k * self._f_track())
        err = target - (c.D + c.corr)
        if c.mode in ("gap", "unknown") or abs(err) > SNAP_DIST:
            c.D, c.corr = target, 0.0
            if c.mode in ("gap", "unknown"):
                c.draw_D = target
        else:
            c.corr += err
        if c.mode != "grid" or (self.start_t is not None):
            c.mode = "track"
        c.last_gate, c.last_gate_D, c.last_gate_t = gate, D_gate, t_cross

    def _crossing_time(self, c: Car, gate: int, t_msg: float, sector_s: float | None) -> float:
        """Best estimate of when the car physically crossed the gate.

        Sector durations are exact, so consecutive crossings form a chain
        t_cross_n = anchor + sum(sector times). Every message satisfies
        t_msg_n >= t_cross_n, so the tightest anchor is min(t_msg_n - sum_n):
        this removes feed jitter, leaving only its constant floor latency.
        """
        prev_gate = (gate - 1) % 3
        if sector_s and c.last_gate == prev_gate and c.chain_anchor is not None:
            c.chain_sum += sector_s
            cand = t_msg - c.chain_sum
            if abs(cand - c.chain_anchor) > CHAIN_RESET_S:
                c.chain_anchor, c.chain_sum = t_msg, 0.0
            else:
                c.chain_anchor = min(c.chain_anchor, cand)
        else:
            c.chain_anchor, c.chain_sum = t_msg, 0.0
        return c.chain_anchor + c.chain_sum - ASSUMED_LATENCY

    def _enter_pit(self, c: Car, t: float) -> None:
        c.mode, c.pit_t, c.pit_D, c.corr = "pit", t, c.D, 0.0
        c.chain_anchor = None

    def _exit_pit(self, c: Car, t: float) -> None:
        if c.mode != "pit":
            return
        L = self.track.length
        base = max(c.pit_D, c.last_gate_D or c.pit_D)
        # the pit exit instance after the pit entry
        n = math.floor((base - self.track.pit_exit_s) / L) + 1
        D = n * L + self.track.pit_exit_s
        if D - base > L * 0.9:
            D -= L
        c.D = c.draw_D = max(D, base)
        c.mode, c.out_lap, c.corr = "track", True, 0.0
        self._expect_next_gate_after(c)
        c.last_gate_t = None

    def _expect_next_gate_after(self, c: Car) -> None:
        """Make the first gate ahead of c.D the next expected gate."""
        L = self.track.length
        best = None
        for g in range(3):
            Dg = self.track.gate_D_near(g, c.D)
            if Dg <= c.D:
                Dg += L
            if best is None or Dg < best[1]:
                best = (g, Dg)
        c.last_gate = (best[0] - 1) % 3
        c.last_gate_D = c.D - 1.0

    # ---- prediction --------------------------------------------------------------
    def advance(self, t: float) -> None:
        if self.t is None:
            self.t = t
            return
        while self.t < t - 1e-9:
            dt = min(0.1, t - self.t)
            self.t += dt
            self._step(dt)
        self._record_leader()

    def _next_gate_D(self, c: Car) -> float | None:
        if c.last_gate is None or c.last_gate_D is None:
            return None
        g = (c.last_gate + 1) % 3
        D = self.track.gate_D_near(g, c.last_gate_D + 1.0)
        while D <= c.last_gate_D + 1.0:
            D += self.track.length
        return D

    def _step(self, dt: float) -> None:
        f = self._f_track()
        for c in self.cars.values():
            if c.mode in ("track",):
                k = c.k * f * (PIT_K if c.out_lap else 1.0)
                newD = self.track.advance(c.D, dt, k)
                # standing start: limit speed by launch acceleration
                if self.start_t is not None and self.is_race:
                    since = self.t - self.start_t
                    if 0 <= since < 15:
                        newD = min(newD, c.D + max(0.0, LAUNCH_ACCEL * since) * dt)
                    elif since < 0:
                        newD = c.D
                nominal = newD - c.D
                # blend in pending correction; never backwards
                step_c = c.corr * (1 - math.exp(-dt / BLEND_TAU))
                if step_c < -0.85 * nominal:
                    step_c = -0.85 * nominal
                c.corr -= step_c
                move = nominal + step_c
                # gate hold: decelerate and hold past an unreported gate
                g = self._next_gate_D(c)
                if g is not None:
                    limit = g + HOLD_LIMIT
                    if c.D + move > g + HOLD_START:
                        room = max(0.0, limit - c.D)
                        frac = room / (HOLD_LIMIT - HOLD_START)
                        move = min(move, room, move * frac)
                c.D += max(0.0, move)
            elif c.mode == "gap":
                self._gap_fallback(c)

    # ---- gap fallback ------------------------------------------------------------
    def _record_leader(self) -> None:
        leader = self._leader()
        if leader is not None and leader.mode == "track":
            self.leader_hist.append((self.t, leader.D))
            while self.leader_hist and self.leader_hist[0][0] < self.t - HISTORY_S:
                self.leader_hist.popleft()

    def _leader(self) -> Car | None:
        for num, line in self.state.lines.items():
            if str(line.get("Position")) == "1":
                return self.cars.get(num)
        return None

    def _gap_fallback(self, c: Car) -> None:
        line = self.state.lines.get(c.num, {})
        gap = parse_gap(line.get("GapToLeader"))
        if gap is None or not self.leader_hist:
            return
        ts = [h[0] for h in self.leader_hist]
        i = bisect.bisect_left(ts, self.t - gap)
        if i >= len(ts) or (i == 0 and ts[0] > self.t - gap + 1):
            return
        D = self.leader_hist[i][1]
        if D > c.D or c.D - D > SNAP_DIST:
            c.D = D

    # ---- output ------------------------------------------------------------------
    def snapshot(self) -> list[dict]:
        """Positions for drawing, with the car-order constraint applied."""
        order = []
        for num, c in self.cars.items():
            line = self.state.lines.get(num, {})
            try:
                pos = int(line.get("Position") or 99)
            except (TypeError, ValueError):
                pos = 99
            order.append((pos, num, c))
        order.sort(key=lambda o: (o[0], o[1]))
        # car must not be drawn ahead of the car in front of it in Position
        prev_draw = None
        for pos, num, c in order:
            D = c.D
            if c.mode in ("track", "grid", "gap"):
                if self.is_race and prev_draw is not None and D > prev_draw - ORDER_GAP:
                    D = prev_draw - ORDER_GAP
                prev_draw = D
            c.draw_D = D
        out = []
        drivers = self.state.drivers
        for pos, num, c in order:
            dl = drivers.get(num, {})
            line = self.state.lines.get(num, {})
            item = {
                "num": num, "code": dl.get("Tla") or num, "color": "#" + (dl.get("TeamColour") or "888888"),
                "pos": pos if pos != 99 else None, "mode": c.mode,
                "lap": self.state.laps_done(num),
                "gap": line.get("GapToLeader") or _ival(line), "tyre": self.state.tyre(num),
                "out": c.out_reason,
            }
            if c.mode == "pit":
                item["x"], item["y"] = self._pit_xy(c)
            elif c.mode == "grid":
                lat = GRID_LATERAL if c.grid_slot % 2 else -GRID_LATERAL
                item["x"], item["y"] = self.track.xy(c.draw_D, lat)
            elif c.mode in ("track", "gap"):
                item["x"], item["y"] = self.track.xy(c.draw_D)
            item["D"] = round(c.draw_D, 1)
            if "x" in item:
                item["x"], item["y"] = round(item["x"], 1), round(item["y"], 1)
            out.append(item)
        return out

    def _pit_xy(self, c: Car) -> tuple[float, float]:
        lane = self.track.pit_lane
        frac = min(0.97, max(0.0, (self.t - c.pit_t) / PIT_LANE_TIME)) if self.t else 0.5
        if len(lane) >= 2:
            i = frac * (len(lane) - 1)
            a = int(i)
            b = min(a + 1, len(lane) - 1)
            u = i - a
            return (lane[a][0] + u * (lane[b][0] - lane[a][0]), lane[a][1] + u * (lane[b][1] - lane[a][1]))
        # no pit lane geometry: a row beside the main straight
        return self.track.xy(self.track.length - 150 + frac * 300, 25.0)


def _ival(line: dict) -> str:
    v = line.get("IntervalToPositionAhead")
    return (v or {}).get("Value", "") if isinstance(v, dict) else ""
