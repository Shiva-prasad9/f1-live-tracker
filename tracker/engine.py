"""Glue: messages -> SessionState -> Estimator, with a clock."""
from __future__ import annotations

import logging

from .estimator import Estimator
from .state import SessionState
from .track import Track

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, track: Track):
        self.track = track
        self.state = SessionState()
        self.est = Estimator(track=track, state=self.state)
        self.initialised = False

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

    def snapshot(self) -> dict:
        lc = self.state.topics.get("LapCount") or {}
        return {
            "t": self.est.t,
            "session": (self.state.topics.get("SessionInfo") or {}).get("Name", ""),
            "meeting": ((self.state.topics.get("SessionInfo") or {}).get("Meeting") or {}).get("Name", ""),
            "session_status": self.state.session_status,
            "track_status": self.state.track_status,
            "lap": lc.get("CurrentLap"), "total_laps": lc.get("TotalLaps"),
            "cars": self.est.snapshot() if self.initialised else [],
        }


def track_for_session(state: SessionState, override: str | None = None) -> str:
    if override:
        return override
    circ = ((state.topics.get("SessionInfo") or {}).get("Meeting") or {}).get("Circuit", {}).get("ShortName")
    return (circ or "interlagos").lower().replace(" ", "_")
