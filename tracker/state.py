"""Session state: stores every topic, applies deltas and emits estimator events.

Events are detected by comparing a car's TimingData line before and after a delta:
  ("gate", car, t, gate, sector_seconds)   gate 1, 2 = end of S1/S2, gate 0 = line
  ("pit_in", car, t) / ("pit_out", car, t)
  ("out", car, t, reason)                    Retired or Stopped
  ("track_status", t, code) / ("session_status", t, status)
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

from .merge import deep_merge

log = logging.getLogger(__name__)
LINE_DEDUP_S = 3.0
SAME_VALUE_GAP_S = 20.0  # no sector is shorter than this


def parse_laptime(v: Any) -> float | None:
    """'1:12.345' / '23.456' -> seconds."""
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        if ":" in v:
            m, s = v.split(":", 1)
            return int(m) * 60 + float(s)
        return float(v)
    except ValueError:
        return None


def parse_gap(v: Any) -> float | None:
    """'+1.234' -> 1.234 s; 'LAP 12' / '' / '+1 LAP' -> None."""
    if not isinstance(v, str):
        return None
    v = v.strip().lstrip("+")
    try:
        return float(v)
    except ValueError:
        return None


def _get(d: Any, *path, default=None):
    for p in path:
        if isinstance(d, dict):
            d = d.get(p, d.get(str(p)))
        elif isinstance(d, list) and isinstance(p, int) and p < len(d):
            d = d[p]
        else:
            return default
        if d is None:
            return default
    return d


def _sector_value(line: dict | None, i: int) -> str:
    v = _get(line or {}, "Sectors", i, "Value", default="")
    return v if isinstance(v, str) else ""


@dataclass
class SessionState:
    topics: dict[str, Any] = field(default_factory=dict)
    last_gate_t: dict[tuple[str, int], float] = field(default_factory=dict)
    clock: float = 0.0  # time of last applied message

    # ---- convenience views ---------------------------------------------------
    @property
    def lines(self) -> dict:
        return _get(self.topics, "TimingData", "Lines", default={}) or {}

    @property
    def drivers(self) -> dict:
        dl = self.topics.get("DriverList") or {}
        return {k: v for k, v in dl.items() if isinstance(v, dict) and k.isdigit()}

    @property
    def track_status(self) -> str:
        return str(_get(self.topics, "TrackStatus", "Status", default="1"))

    @property
    def session_status(self) -> str:
        return str(_get(self.topics, "SessionStatus", "Status", default=""))

    @property
    def session_type(self) -> str:
        return str(_get(self.topics, "SessionInfo", "Type", default=""))

    def laps_done(self, car: str) -> int:
        v = _get(self.lines, car, "NumberOfLaps", default=0)
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    def tyre(self, car: str) -> str:
        stints = _get(self.topics, "TimingAppData", "Lines", car, "Stints", default=None)
        if isinstance(stints, dict):
            stints = [stints[k] for k in sorted(stints, key=lambda k: int(k))]
        if stints:
            return str((stints[-1] or {}).get("Compound", "") or "")
        return ""

    def grid_pos(self, car: str) -> int | None:
        for v in (_get(self.topics, "TimingAppData", "Lines", car, "GridPos"),
                  _get(self.lines, car, "Position")):
            try:
                if v not in (None, ""):
                    return int(v)
            except (TypeError, ValueError):
                pass
        return None

    # ---- applying messages -----------------------------------------------------
    def load_initial(self, topic: str, data: Any) -> None:
        """Full state (Subscribe result / keyframe): store, no events."""
        self.topics[topic] = copy.deepcopy(data)

    def apply(self, topic: str, data: Any, t: float) -> list[tuple]:
        """Merge a partial update and return detected events."""
        self.clock = max(self.clock, t)
        events: list[tuple] = []
        if topic == "TimingData" and isinstance(data, dict) and isinstance(data.get("Lines"), dict):
            old_lines = self.lines
            for car, delta in data["Lines"].items():
                if isinstance(delta, dict):
                    old = copy.deepcopy(old_lines.get(car) or {})
                    events.extend(self._line_events(car, old, delta, t))
        if topic == "TrackStatus":
            st = str((data or {}).get("Status", ""))
            if st and st != self.track_status:
                events.append(("track_status", t, st))
        if topic == "SessionStatus":
            st = str((data or {}).get("Status", ""))
            if st and st != self.session_status:
                events.append(("session_status", t, st))
        self.topics[topic] = deep_merge(self.topics.get(topic), copy.deepcopy(data))
        return events

    def _line_events(self, car: str, old: dict, delta: dict, t: float) -> list[tuple]:
        ev: list[tuple] = []
        # sectors: a Value appearing / changing to a new non-empty value = gate passed
        sectors = delta.get("Sectors")
        if isinstance(sectors, (dict, list)):
            items = sectors.items() if isinstance(sectors, dict) else enumerate(sectors)
            for k, sd in items:
                i = int(k)
                if not isinstance(sd, dict) or i > 2:
                    continue
                v = sd.get("Value")
                if not (isinstance(v, str) and v.strip()):
                    continue
                gate = (i + 1) % 3
                last = self.last_gate_t.get((car, gate))
                # a new value is a crossing; an identical value re-sent long after the
                # last crossing of this gate is too (same sector time to the ms)
                if v != _sector_value(old, i) or (last is not None and t - last > SAME_VALUE_GAP_S):
                    ev += self._gate(car, t, gate, parse_laptime(v))
        # lap count increment = line crossing
        if "NumberOfLaps" in delta:
            try:
                new, prev = int(delta["NumberOfLaps"]), int(old.get("NumberOfLaps") or 0)
                if new > prev:
                    ev += self._gate(car, t, 0, None)
            except (TypeError, ValueError):
                pass
        # pits
        if delta.get("InPit") is True and not old.get("InPit"):
            ev.append(("pit_in", car, t))
        if "InPit" in delta and delta.get("InPit") is False and old.get("InPit"):
            ev.append(("pit_out", car, t))
        elif delta.get("PitOut") is True and not old.get("PitOut") and not old.get("InPit"):
            pass  # PitOut without a preceding InPit (e.g. joined mid-session): ignore
        for flag in ("Retired", "Stopped"):
            if delta.get(flag) is True and not old.get(flag):
                ev.append(("out", car, t, flag))
        return ev

    def _gate(self, car: str, t: float, gate: int, sector_s: float | None) -> list[tuple]:
        key = (car, gate)
        last = self.last_gate_t.get(key)
        if last is not None and abs(t - last) < LINE_DEDUP_S:
            return []
        self.last_gate_t[key] = t
        return [("gate", car, t, gate, sector_s)]
