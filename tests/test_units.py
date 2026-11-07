import base64
import zlib

import pytest

from tracker.engine import Engine
from tracker.merge import deep_merge
from tracker.sources.archive import decode_z, parse_stream
from tracker.state import SessionState, parse_gap, parse_laptime

from .helpers import synthetic_track, timing_line


# ---- delta merge -------------------------------------------------------------------
def test_merge_nested_dicts():
    base = {"Lines": {"1": {"Position": "1", "GapToLeader": ""}}}
    deep_merge(base, {"Lines": {"1": {"GapToLeader": "+0.5"}, "44": {"Position": "2"}}})
    assert base == {"Lines": {"1": {"Position": "1", "GapToLeader": "+0.5"}, "44": {"Position": "2"}}}


def test_merge_index_keyed_dict_into_list():
    base = {"Sectors": [{"Value": "1"}, {"Value": "2"}, {"Value": "3"}]}
    deep_merge(base, {"Sectors": {"1": {"Value": "22.2"}, "4": {"Value": "x"}}})
    assert base["Sectors"][1] == {"Value": "22.2"}
    assert base["Sectors"][0] == {"Value": "1"}
    assert len(base["Sectors"]) == 5 and base["Sectors"][4] == {"Value": "x"}


def test_merge_list_replaces_and_deleted():
    base = {"a": [1, 2, 3], "b": {"x": 1, "y": 2}}
    deep_merge(base, {"a": [9], "b": {"_deleted": ["x"]}})
    assert base == {"a": [9], "b": {"y": 2}}


def test_merge_into_none():
    assert deep_merge(None, {"Status": "1"}) == {"Status": "1"}


def test_parsers():
    assert parse_laptime("1:12.345") == pytest.approx(72.345)
    assert parse_laptime("23.456") == pytest.approx(23.456)
    assert parse_laptime("") is None
    assert parse_gap("+1.234") == pytest.approx(1.234)
    assert parse_gap("+1 LAP") is None and parse_gap("LAP 12") is None


def test_archive_stream_and_z_decoding():
    text = "﻿00:00:01.500{\"a\": 1}\r\n00:01:02.250\"abc\"\r\n"
    assert parse_stream(text.lstrip("﻿")) == [(1.5, {"a": 1}), (62.25, "abc")]
    c = zlib.compressobj(wbits=-15)
    raw = c.compress(b'{"Position": []}') + c.flush()
    assert decode_z(base64.b64encode(raw).decode()) == {"Position": []}


# ---- sector events -------------------------------------------------------------------
def _state():
    st = SessionState()
    st.load_initial("TimingData", {"Lines": {"1": timing_line(1), "4": timing_line(2)}})
    return st


def test_sector_event_detection():
    st = _state()
    ev = st.apply("TimingData", {"Lines": {"1": {"Sectors": {"0": {"Value": "22.000"}}}}}, 100.0)
    assert ev == [("gate", "1", 100.0, 1, 22.0)]
    # same value again (e.g. status-only update) -> nothing
    assert st.apply("TimingData", {"Lines": {"1": {"Sectors": {"0": {"Value": "22.000"}}}}}, 101.0) == []
    # empty value (sector cleared) -> nothing
    assert st.apply("TimingData", {"Lines": {"1": {"Sectors": {"1": {"Value": ""}}}}}, 102.0) == []
    # sector 2 -> gate 2
    ev = st.apply("TimingData", {"Lines": {"1": {"Sectors": {"1": {"Value": "25.100"}}}}}, 125.0)
    assert ev == [("gate", "1", 125.0, 2, 25.1)]


def test_line_crossing_dedup():
    st = _state()
    ev = st.apply("TimingData", {"Lines": {"4": {"Sectors": {"2": {"Value": "24.000"}}}}}, 200.0)
    assert ev == [("gate", "4", 200.0, 0, 24.0)]
    # NumberOfLaps increment 1 s later is the same crossing -> deduplicated
    assert st.apply("TimingData", {"Lines": {"4": {"NumberOfLaps": 1}}}, 201.0) == []
    # next lap's increment is a new crossing
    ev = st.apply("TimingData", {"Lines": {"4": {"NumberOfLaps": 2}}}, 272.0)
    assert ev == [("gate", "4", 272.0, 0, None)]


def test_initial_state_emits_nothing_and_list_sectors():
    st = SessionState()
    line = timing_line(1)
    line["Sectors"][0]["Value"] = "22.0"
    st.load_initial("TimingData", {"Lines": {"1": line}})
    # a delta that carries the full sector list form
    ev = st.apply("TimingData", {"Lines": {"1": {"Sectors": [{"Value": "22.0"}, {"Value": "25.0"}]}}}, 5.0)
    assert ev == [("gate", "1", 5.0, 2, 25.0)]


def test_pit_and_out_events():
    st = _state()
    assert st.apply("TimingData", {"Lines": {"1": {"InPit": True}}}, 10.0) == [("pit_in", "1", 10.0)]
    assert st.apply("TimingData", {"Lines": {"1": {"InPit": False, "PitOut": True}}}, 30.0) == [("pit_out", "1", 30.0)]
    assert st.apply("TimingData", {"Lines": {"4": {"Retired": True}}}, 40.0) == [("out", "4", 40.0, "Retired")]


# ---- track ---------------------------------------------------------------------------
def test_track_interpolation_and_profile():
    tr = synthetic_track()
    L, T = tr.length, tr.lap_time
    assert tr.ref_time(0) == pytest.approx(0)
    assert tr.ref_time(L) == pytest.approx(T)
    assert tr.ref_time(L + 100) == pytest.approx(T + tr.ref_time(100))
    for D in (10.0, 1234.5, L - 1, 2 * L + 777):
        assert tr.dist_at_ref_time(tr.ref_time(D)) == pytest.approx(D, abs=1e-6)
    # advancing a full reference lap at k=1 gives exactly one lap
    assert tr.advance(500.0, T, 1.0) == pytest.approx(500.0 + L, abs=1e-6)
    x, y = tr.xy(L + 100)
    assert (x, y) == pytest.approx(tr.xy(100))
    s, d = tr.project(*tr.xy(321.0))
    assert s == pytest.approx(321.0, abs=0.5) and d < 0.5
    assert sum(tr.sector_ref_time(i) for i in range(3)) == pytest.approx(T)
    assert tr.gate_D_near(1, 3 * L + 100) == pytest.approx(3 * L + tr.gates[1])


# ---- estimator -----------------------------------------------------------------------
def _engine_racing(n=2):
    tr = synthetic_track()
    eng = Engine(tr)
    eng.state.load_initial("SessionInfo", {"Type": "Race"})
    eng.state.load_initial("SessionStatus", {"Status": "Inactive"})
    eng.state.load_initial("TrackStatus", {"Status": "1"})
    eng.state.load_initial("TimingData", {"Lines": {str(i + 1): timing_line(i + 1) for i in range(n)}})
    eng.start(0.0)
    eng.ingest(10.0, "SessionStatus", {"Status": "Started"})
    return eng, tr


def test_grid_placement_before_start():
    tr = synthetic_track()
    eng = Engine(tr)
    eng.state.load_initial("SessionInfo", {"Type": "Race"})
    eng.state.load_initial("SessionStatus", {"Status": "Inactive"})
    eng.state.load_initial("TimingData", {"Lines": {"1": timing_line(1), "2": timing_line(2), "3": timing_line(3)}})
    eng.start(0.0)
    eng.tick(5.0)
    D = {n: c.D for n, c in eng.est.cars.items()}
    assert D["1"] > D["2"] > D["3"] and D["1"] < 0
    assert all(c.mode == "grid" for c in eng.est.cars.values())


def test_gate_hold_when_no_event():
    eng, tr = _engine_racing(1)
    eng.tick(200.0)  # long past gate 1 with no sector event
    c = eng.est.cars["1"]
    assert c.D <= tr.gates[1] + 120.0 + 1e-6
    assert c.D > tr.gates[1]


def test_correction_blends_and_never_goes_backwards():
    eng, tr = _engine_racing(1)
    eng.tick(15.0)
    c = eng.est.cars["1"]
    prev = c.D
    # event says the car is far behind the estimate: it must slow, not jump back
    eng.ingest(15.0, "TimingData", {"Lines": {"1": {"Sectors": {"0": {"Value": "5.000"}}}}})
    for i in range(1, 40):
        eng.tick(15.0 + i * 0.1)
        assert c.D >= prev - 1e-9
        prev = c.D


def test_pit_in_and_out():
    eng, tr = _engine_racing(1)
    eng.tick(60.0)
    eng.ingest(60.0, "TimingData", {"Lines": {"1": {"InPit": True}}})
    c = eng.est.cars["1"]
    assert c.mode == "pit"
    snap = {s["num"]: s for s in eng.snapshot()["cars"]}
    assert snap["1"]["mode"] == "pit" and "x" in snap["1"]
    eng.tick(80.0)
    eng.ingest(80.0, "TimingData", {"Lines": {"1": {"InPit": False, "PitOut": True}}})
    assert c.mode == "track" and c.out_lap
    assert c.D % tr.length == pytest.approx(tr.pit_exit_s, abs=1.0)


def test_retired_goes_out():
    eng, _ = _engine_racing(2)
    eng.ingest(30.0, "TimingData", {"Lines": {"2": {"Retired": True}}})
    snap = {s["num"]: s for s in eng.snapshot()["cars"]}
    assert snap["2"]["mode"] == "out"


def test_order_constraint():
    eng, tr = _engine_racing(2)
    eng.tick(30.0)
    a, b = eng.est.cars["1"], eng.est.cars["2"]
    b.D = a.D + 50  # P2 estimated ahead of P1
    snap = {s["num"]: s for s in eng.snapshot()["cars"]}
    assert snap["2"]["D"] < snap["1"]["D"]
