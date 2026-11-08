"""OpenF1 replay source (fallback while the F1 static archive needs a login).

OpenF1 republishes the same timing data in a tabular form. We rebuild live-timing
style messages from it (DriverList, SessionInfo/Status, TrackStatus, TimingData
sectors / laps / positions / gaps / pits, TimingAppData stints) so they go
through exactly the same parser and estimator as the live feed.

Ground truth for validation comes from OpenF1 `location` (the same x/y as
Position.z, ~3.7 Hz).

Real sector messages arrive a little after the car crosses the gate; we add a
fixed `latency` to every timing message to stay honest about that.
"""
from __future__ import annotations

import logging
import random
from collections import defaultdict
from datetime import timedelta

from .. import openf1
from ..trackbuild import SCALE, iso, parse_date
from . import Message

log = logging.getLogger(__name__)


def find_session(year: int, event: str, session: str) -> dict:
    sessions = openf1.get("sessions", year=year)
    ev = event.lower()
    names = {"race": "Race", "sprint": "Sprint", "qualifying": "Qualifying"}
    want = names.get(session.lower(), session)
    cands = []
    for s in sessions:
        meet_txt = " ".join(str(s.get(k, "")) for k in ("location", "country_name", "circuit_short_name", "meeting_name"))
        if s.get("session_name") == want:
            cands.append((s, meet_txt.lower()))
    for s, txt in cands:
        if any(w in txt for w in ev.replace("grand prix", "").replace("ã", "a").split() if len(w) > 2) or \
                any(w in txt for w in ev.replace("grand prix", "").split() if len(w) > 2):
            return s
    meetings = {m["meeting_key"]: m for m in openf1.get("meetings", year=year)}
    for s, _ in cands:
        m = meetings.get(s["meeting_key"], {})
        if ev in (m.get("meeting_name", "") + " " + m.get("meeting_official_name", "")).lower():
            return s
    raise RuntimeError(f"OpenF1 session not found: {year} {event} {session}")


def _ts(d: str) -> float:
    return parse_date(d).timestamp()


def _fmt(sec: float) -> str:
    if sec >= 60:
        return f"{int(sec // 60)}:{sec % 60:06.3f}"
    return f"{sec:.3f}"


def load(year: int, event: str, session: str, latency: float = 1.0, jitter: float = 0.3,
         seed: int = 1) -> tuple[list[Message], dict]:
    rnd = random.Random(seed)
    sess = find_session(year, event, session)
    key = sess["session_key"]
    log.info("OpenF1 session %s: %s %s", key, sess.get("location"), sess.get("session_name"))
    drivers = openf1.get("drivers", session_key=key)
    laps = openf1.get("laps", session_key=key)
    positions = openf1.get("position", session_key=key)
    intervals = openf1.get("intervals", session_key=key)
    pits = openf1.get("pit", session_key=key)
    stints = openf1.get("stints", session_key=key)
    rc = openf1.get("race_control", session_key=key)
    try:
        weather = openf1.get("weather", session_key=key)
    except openf1.OpenF1Error:
        weather = []
    try:
        grid = openf1.get("starting_grid", session_key=key)
    except openf1.OpenF1Error:
        grid = []

    msgs: list[Message] = []
    lat = lambda: latency + rnd.uniform(-jitter, jitter)  # noqa: E731

    # ---- race start: earliest lap-2 start minus that driver's lap-1 duration
    by_car: dict[int, dict[int, dict]] = defaultdict(dict)
    for lp in laps:
        by_car[lp["driver_number"]][lp["lap_number"]] = lp
    starts = []
    for car, ls in by_car.items():
        l1, l2 = ls.get(1), ls.get(2)
        if l2 and l2.get("date_start") and l1 and l1.get("lap_duration"):
            starts.append(_ts(l2["date_start"]) - l1["lap_duration"])
    t_start = min(starts) if starts else _ts(sess["date_start"])
    # lap 1 duration includes the run from the grid; start is at most a couple of s before this
    t0 = t_start - 600

    # ---- initial state
    gridpos: dict[int, int] = {g["driver_number"]: g["position"] for g in grid if g.get("position")}
    if not gridpos:
        firsts: dict[int, tuple[float, int]] = {}
        for p in positions:
            t = _ts(p["date"])
            if t <= t_start + 5 and (p["driver_number"] not in firsts or t >= firsts[p["driver_number"]][0]):
                firsts[p["driver_number"]] = (t, p["position"])
        gridpos = {k: v[1] for k, v in firsts.items()}
    dl, lines, app = {}, {}, {}
    for i, d in enumerate(sorted(drivers, key=lambda d: gridpos.get(d["driver_number"], 99))):
        n = str(d["driver_number"])
        dl[n] = {"RacingNumber": n, "Tla": d.get("name_acronym") or n, "TeamColour": d.get("team_colour") or "888888",
                 "TeamName": d.get("team_name", ""), "FullName": d.get("full_name", ""), "Line": i + 1}
        gp = gridpos.get(d["driver_number"], i + 1)
        lines[n] = {"Position": str(gp), "Line": gp, "NumberOfLaps": 0, "InPit": False, "PitOut": False,
                    "Retired": False, "Stopped": False, "GapToLeader": "", "IntervalToPositionAhead": {"Value": ""},
                    "Sectors": [{"Value": ""}, {"Value": ""}, {"Value": ""}]}
        app[n] = {"GridPos": str(gp), "Stints": []}
    info = {"Type": sess.get("session_type", "Race") if sess.get("session_name") in ("Race", "Sprint") else
            sess.get("session_type"), "Name": sess.get("session_name"),
            "Meeting": {"Name": event, "Circuit": {"ShortName": sess.get("circuit_short_name")}}}
    if sess.get("session_name") == "Sprint":
        info["Type"] = "Race"
    msgs += [(t0, "SessionInfo", info, True), (t0, "DriverList", dl, True),
             (t0, "TimingData", {"Lines": lines}, True), (t0, "TimingAppData", {"Lines": app}, True),
             (t0, "TrackStatus", {"Status": "1", "Message": "AllClear"}, True),
             (t0, "SessionStatus", {"Status": "Inactive"}, True)]
    msgs.append((t_start, "SessionStatus", {"Status": "Started"}, False))

    def td(t: float, car, delta: dict):
        msgs.append((t, "TimingData", {"Lines": {str(car): delta}}, False))

    # ---- weather (same shape as the live WeatherData topic: all values are strings)
    def wx(w: dict) -> dict:
        m = {"AirTemp": "air_temperature", "TrackTemp": "track_temperature", "Humidity": "humidity",
             "Pressure": "pressure", "WindSpeed": "wind_speed", "WindDirection": "wind_direction", "Rainfall": "rainfall"}
        return {k: str(w[v]) for k, v in m.items() if w.get(v) is not None}
    wsorted = sorted(weather, key=lambda w: w["date"])
    before = [w for w in wsorted if _ts(w["date"]) <= t_start]
    if before:
        msgs.append((t0, "WeatherData", wx(before[-1]), True))
    for w in wsorted:
        if _ts(w["date"]) > t_start:
            msgs.append((_ts(w["date"]), "WeatherData", wx(w), False))

    # ---- laps -> sector values and lap counts
    last_lap_end: dict[int, float] = {}
    for car, ls in by_car.items():
        for n in sorted(ls):
            lp = ls[n]
            ds = _ts(lp["date_start"]) if lp.get("date_start") else (t_start if n == 1 else None)
            if ds is None:
                continue
            s1, s2, s3 = lp.get("duration_sector_1"), lp.get("duration_sector_2"), lp.get("duration_sector_3")
            if s1:
                td(ds + s1 + lat(), car, {"Sectors": {"0": {"Value": _fmt(s1)}, "1": {"Value": ""}, "2": {"Value": ""}}})
            if s1 and s2:
                td(ds + s1 + s2 + lat(), car, {"Sectors": {"1": {"Value": _fmt(s2)}}})
            dur = lp.get("lap_duration")
            if dur:
                end = ds + dur
                delta = {"NumberOfLaps": n, "LastLapTime": {"Value": _fmt(dur)}}
                if s3:
                    delta["Sectors"] = {"2": {"Value": _fmt(s3)}}
                td(end + lat(), car, delta)
                last_lap_end[car] = max(last_lap_end.get(car, 0), end)
            elif n + 1 in ls and ls[n + 1].get("date_start"):
                td(_ts(ls[n + 1]["date_start"]) + lat(), car, {"NumberOfLaps": n})

    # ---- position / gaps
    for p in positions:
        t = _ts(p["date"])
        if t > t_start + 5:
            td(t, p["driver_number"], {"Position": str(p["position"]), "Line": p["position"]})
    for iv in intervals:
        g, i = iv.get("gap_to_leader"), iv.get("interval")
        gs = f"+{g:.3f}" if isinstance(g, (int, float)) else (str(g) if g is not None else "")
        is_ = f"+{i:.3f}" if isinstance(i, (int, float)) else (str(i) if i is not None else "")
        td(_ts(iv["date"]), iv["driver_number"], {"GapToLeader": gs, "IntervalToPositionAhead": {"Value": is_}})

    # ---- pits
    pit_windows: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for p in pits:
        lane = p.get("lane_duration") or p.get("pit_duration")
        if not p.get("date") or not lane:
            continue
        a = _ts(p["date"])
        td(a + lat(), p["driver_number"], {"InPit": True, "PitOut": False})
        td(a + lane + lat(), p["driver_number"], {"InPit": False, "PitOut": True})
        pit_windows[p["driver_number"]].append((a, a + lane))

    # ---- tyres
    stint_list: dict[int, list] = defaultdict(list)
    for st in sorted(stints, key=lambda s: (s["driver_number"], s.get("stint_number") or 0)):
        car = st["driver_number"]
        stint_list[car].append({"Compound": st.get("compound") or "", "TotalLaps": st.get("tyre_age_at_start") or 0})
        lap = by_car.get(car, {}).get(st.get("lap_start") or 1, {})
        t = _ts(lap["date_start"]) if lap.get("date_start") else t_start
        msgs.append((t, "TimingAppData", {"Lines": {str(car): {"Stints": {str(len(stint_list[car]) - 1): stint_list[car][-1]}}}}, False))

    # ---- race control -> track status
    for m in sorted(rc, key=lambda m: m["date"]):
        txt = (m.get("message") or "").upper()
        flag = (m.get("flag") or "").upper()
        code = None
        if "VIRTUAL SAFETY CAR DEPLOYED" in txt:
            code = "6"
        elif "VIRTUAL SAFETY CAR ENDING" in txt:
            code = "7"
        elif "SAFETY CAR DEPLOYED" in txt:
            code = "4"
        elif flag == "RED":
            code = "5"
        elif flag in ("GREEN", "CLEAR") and m.get("scope") == "Track":
            code = "1"
        elif "TRACK CLEAR" in txt:
            code = "1"
        if code:
            msgs.append((_ts(m["date"]), "TrackStatus", {"Status": code}, False))

    # ---- retirements: car stops completing laps well before the end
    t_end = max(last_lap_end.values()) if last_lap_end else t_start + 7200
    t_finish = min(v for v in last_lap_end.values() if v > t_end - 300) if last_lap_end else t_end
    for car, end in last_lap_end.items():
        if end < t_finish - 200:
            td(end + 90, car, {"Retired": True})
    # a car that never completed a lap (lap-1 crash) has no entry above
    for d in drivers:
        car = d["driver_number"]
        if car not in last_lap_end:
            starts = [_ts(lp["date_start"]) for lp in by_car.get(car, {}).values() if lp.get("date_start")]
            td(max(starts, default=t_start) + 90, car, {"Retired": True})

    msgs.sort(key=lambda m: (m[0], not m[3]))
    meta = {"session_key": key, "t_start": t_start, "t_end": t_end, "t_finish": t_finish,
            "drivers": [d["driver_number"] for d in drivers], "pit_windows": dict(pit_windows),
            "source": "openf1"}
    return msgs, meta


def load_positions(session_key: int, cars: list[int], t_from: float, t_to: float) -> dict[str, list]:
    """Ground truth {car: [(t, x_m, y_m)]} from OpenF1 location (validation only)."""
    from datetime import datetime, timezone
    out = {}
    a = datetime.fromtimestamp(t_from, timezone.utc)
    b = datetime.fromtimestamp(t_to, timezone.utc)
    for car in cars:
        rows = []
        # chunk by 30 min to keep responses manageable
        c = a
        while c < b:
            d = min(b, c + timedelta(minutes=30))
            rows += openf1.get("location", f"date>={iso(c)}", f"date<{iso(d)}",
                               session_key=session_key, driver_number=car)
            c = d
        out[str(car)] = [(_ts(r["date"]), r["x"] * SCALE, r["y"] * SCALE) for r in rows if r.get("x") is not None]
    return out
