"""Race picker backend: calendar grouping, track store lookup, hub states."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

from tracker import server, tracks_store
from tracker.server import Broadcaster, ReplayHub


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def test_calendar_groups_and_filters(monkeypatch):
    now = datetime.now(timezone.utc)
    past = now - timedelta(days=30)
    sessions = [
        {"session_key": 1, "meeting_key": 10, "session_name": "Practice 1", "date_start": _iso(past - timedelta(days=2)),
         "date_end": _iso(past - timedelta(days=2)), "location": "Monza", "country_name": "Italy"},
        {"session_key": 2, "meeting_key": 10, "session_name": "Sprint", "date_start": _iso(past - timedelta(days=1)),
         "date_end": _iso(past - timedelta(days=1)), "location": "Monza", "country_name": "Italy"},
        {"session_key": 3, "meeting_key": 10, "session_name": "Race", "date_start": _iso(past),
         "date_end": _iso(past + timedelta(hours=2)), "location": "Monza", "country_name": "Italy"},
        {"session_key": 4, "meeting_key": 11, "session_name": "Race", "date_start": _iso(now + timedelta(days=5)),
         "date_end": _iso(now + timedelta(days=5, hours=2)), "location": "Baku", "country_name": "Azerbaijan"},
        {"session_key": 5, "meeting_key": 9, "session_name": "Race", "date_start": _iso(past - timedelta(days=60)),
         "date_end": _iso(past - timedelta(days=60) + timedelta(hours=2)), "location": "Imola", "country_name": "Italy"},
    ]
    meetings = [{"meeting_key": 10, "meeting_name": "Italian Grand Prix"}]

    def fake_get(endpoint, *a, **k):
        return sessions if endpoint == "sessions" else meetings

    monkeypatch.setattr("tracker.openf1.get", fake_get)
    cal = server.calendar_for(now.year)
    assert [c["meeting_key"] for c in cal] == [9, 10]             # future race and practice dropped, date order
    assert cal[1]["name"] == "Italian Grand Prix"
    assert [s["name"] for s in cal[1]["sessions"]] == ["Sprint", "Race"]
    assert cal[0]["name"] == "Imola Grand Prix"                   # falls back to the location


def test_track_store_finds_newest_by_circuit_key(tmp_path, monkeypatch):
    built, committed = tmp_path / "built", tmp_path / "committed"
    built.mkdir(), committed.mkdir()
    for d, name, year, key, circ in ((built, "monza_100", 2024, 39, "Monza"), (built, "monza_200", 2025, 39, "Monza"),
                                      (committed, "other", 2025, 7, "Elsewhere")):
        (d / f"{name}.json").write_text(json.dumps({"meta": {"year": year, "circuit_key": key, "circuit": circ}}))
    monkeypatch.setattr(tracks_store, "BUILT_DIR", built)
    monkeypatch.setattr(tracks_store, "TRACK_DIR", committed)
    assert tracks_store._local_candidates(39, None)[0] == (2025, "monza_200")
    assert tracks_store._local_candidates(None, "monza")[0][1] == "monza_200"   # by name, any case
    assert tracks_store._local_candidates(999, "Nowhere") == []


def test_hub_load_failure_shows_error_and_home_returns_to_lobby(monkeypatch):
    async def go():
        hub = ReplayHub(Broadcaster())
        monkeypatch.setattr(hub, "_blocking_load", lambda spec: (_ for _ in ()).throw(RuntimeError("OpenF1 is locked")))
        await hub.load({"kind": "openf1", "session_key": 1})
        assert hub.state == "error" and "locked" in hub.message
        assert hub.engine is None

    asyncio.run(go())
