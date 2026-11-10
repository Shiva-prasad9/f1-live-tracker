"""Find or build track models.

* ``tracks/`` holds the committed Interlagos model (used by the bundled replay).
* ``data/tracks/`` (git-ignored) holds models built on demand from OpenF1, one per race session,
  because circuit layouts change between years. Building takes a few seconds and needs OpenF1,
  which is locked while a real F1 session is running.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from . import openf1
from .track import BUILT_DIR, TRACK_DIR, Track
from .trackbuild import build_track, parse_date

log = logging.getLogger(__name__)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_") or "circuit"


def _local_candidates(circuit_key: int | None, short_name: str | None) -> list[tuple[int, str]]:
    """[(year, name)] of stored tracks for this circuit, newest first."""
    out = []
    for d in (BUILT_DIR, TRACK_DIR):
        if not d.exists():
            continue
        for p in d.glob("*.json"):
            try:
                meta = json.loads(p.read_text()).get("meta", {})
            except (OSError, ValueError):
                continue
            same = (circuit_key is not None and meta.get("circuit_key") == circuit_key) or \
                   (short_name and slug(meta.get("circuit", "")) == slug(short_name))
            if same:
                out.append((int(meta.get("year") or 0), p.stem))
    return sorted(out, reverse=True)


def for_session(sess: dict) -> Track:
    """Track for one OpenF1 session (built from that very session so the layout matches its year)."""
    name = f"{slug(sess.get('circuit_short_name'))}_{sess['session_key']}"
    try:
        return Track.load(name)
    except FileNotFoundError:
        pass
    log.info("building track %s from OpenF1 ...", name)
    tr = build_track(sess.get("circuit_short_name") or "circuit", name=name, session=sess)
    tr.save(BUILT_DIR)
    return tr


def for_circuit(circuit_key: int | None, short_name: str | None) -> Track:
    """Track for a circuit (live mode): newest stored model, else build from its latest past Race."""
    cands = _local_candidates(circuit_key, short_name)
    if cands:
        return Track.load(cands[0][1])
    sessions = []
    if circuit_key is not None:
        sessions = openf1.get("sessions", circuit_key=circuit_key, session_name="Race")
    if not sessions and short_name:
        sessions = openf1.get("sessions", circuit_short_name=short_name, session_name="Race")
    now = datetime.now(timezone.utc)
    past = [s for s in sessions if s.get("date_end") and parse_date(s["date_end"]) < now]
    if not past:
        raise RuntimeError(f"no past race data for circuit {short_name or circuit_key} (new circuit?)")
    return for_session(max(past, key=lambda s: s["date_start"]))
