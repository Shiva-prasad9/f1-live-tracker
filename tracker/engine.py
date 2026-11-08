"""Glue: messages -> SessionState -> Estimator, with a clock."""
from __future__ import annotations

import logging

from .estimator import Estimator
from .state import SessionState, parse_laptime
from .track import Track

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, track: Track):
        self.track = track
        self.state = SessionState()
        self.est = Estimator(track=track, state=self.state)
        self.initialised = False
        self.best: dict[str, float] = {}   # personal best lap seen per car (s)

    def ingest(self, t: float, topic: str, data, initial: bool = False) -> None:
        if initial:
            self.state.load_initial(topic, data)
            return
        if self.initialised:
            self.est.advance(max(t, self.est.t or t))
        for ev in self.state.apply(topic, data, t):
            if self.initialised:
                self.est.handle(ev)

    def start(self, t: float) -> None:
        """Initialise car placement from current state at time t."""
        self.est.init_from_state(t)
        self.initialised = True

    def tick(self, t: float) -> None:
        if self.initialised:
            self.est.advance(t)

    def _details(self, cars: list[dict]) -> dict | None:
        """Add per-car race details (names, lap times, sectors, stops); return the overall fastest lap."""
        st = self.state
        for c in cars:
            line = st.lines.get(c["num"], {})
            last = parse_laptime((line.get("LastLapTime") or {}).get("Value"))
            for v in (last, parse_laptime((line.get("BestLapTime") or {}).get("Value"))):
                if v and v < self.best.get(c["num"], 1e9):
                    self.best[c["num"]] = v
        fastest = min(((t, n) for n, t in self.best.items()), default=None)
        for c in cars:
            n = c["num"]
            dl = st.drivers.get(n, {})
            line = st.lines.get(n, {})
            ll = line.get("LastLapTime") or {}
            last = parse_laptime(ll.get("Value"))
            best = self.best.get(n)
            c["name"] = dl.get("FullName") or dl.get("BroadcastName") or c["code"]
            c["team"] = dl.get("TeamName") or ""
            c["last"] = last
            c["last_flag"] = ("purple" if ll.get("OverallFastest") or (fastest and last and abs(last - fastest[0]) < 1e-6)
                              else "green" if ll.get("PersonalFastest") or (best and last and abs(last - best) < 1e-6) else "")
            c["best"] = best
            c["fastest"] = bool(fastest and fastest[1] == n)
            c["interval"] = (line.get("IntervalToPositionAhead") or {}).get("Value", "") \
                if isinstance(line.get("IntervalToPositionAhead"), dict) else ""
            stints = (st.topics.get("TimingAppData") or {}).get("Lines", {}).get(n, {}).get("Stints") or {}
            c["stops"] = max(0, len(stints) - 1)
            secs = []
            for i in range(3):
                sec = (line.get("Sectors") or {})
                sec = sec.get(i, sec.get(str(i))) if isinstance(sec, dict) else (sec[i] if i < len(sec) else None)
                sec = sec if isinstance(sec, dict) else {}
                secs.append([sec.get("Value") or "", "purple" if sec.get("OverallFastest")
                             else "green" if sec.get("PersonalFastest") else ""])
            c["sectors"] = secs
        return {"num": fastest[1], "time": fastest[0]} if fastest else None

    def snapshot(self) -> dict:
        lc = self.state.topics.get("LapCount") or {}
        cars = self.est.snapshot() if self.initialised else []
        fastest = self._details(cars) if cars else None
        return {
            "t": self.est.t,
            "session": (self.state.topics.get("SessionInfo") or {}).get("Name", ""),
            "meeting": ((self.state.topics.get("SessionInfo") or {}).get("Meeting") or {}).get("Name", ""),
            "session_status": self.state.session_status,
            "track_status": self.state.track_status,
            "lap": lc.get("CurrentLap"), "total_laps": lc.get("TotalLaps"),
            "cars": cars, "fastest": fastest,
        }


def track_for_session(state: SessionState, override: str | None = None) -> str:
    if override:
        return override
    circ = ((state.topics.get("SessionInfo") or {}).get("Meeting") or {}).get("Circuit", {}).get("ShortName")
    return (circ or "interlagos").lower().replace(" ", "_")
