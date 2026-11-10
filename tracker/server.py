"""aiohttp server: static page at /, track geometry at /track, snapshots on /ws at ~10 Hz.

Replay mode starts in a *lobby*: the page shows a race picker (calendar from OpenF1), the chosen race
is loaded in the background and then played. Live mode connects to the F1 feed and resolves (or builds)
the track model for the circuit first.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import WSMsgType, web

from .engine import Engine
from .sources import Message

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
BUNDLED = ROOT / "replays" / "sao-paulo-2026-race.jsonl.gz"
BUNDLED_LABEL = "São Paulo Grand Prix 2026 · Race (recording)"
HZ = 10
MIN_YEAR = 2024


class Broadcaster:
    def __init__(self):
        self.clients: set[web.WebSocketResponse] = set()

    async def send(self, payload: dict) -> None:
        if not self.clients:
            return
        text = json.dumps(payload, separators=(",", ":"))
        dead = []
        for ws in self.clients:
            try:
                await ws.send_str(text)
            except (ConnectionError, RuntimeError):
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


# ---- calendar -------------------------------------------------------------------------
def calendar_for(year: int) -> list[dict]:
    """Completed Race/Sprint sessions of a season, grouped by meeting (blocking: calls OpenF1)."""
    from . import openf1
    from .trackbuild import parse_date
    cur = year == datetime.now(timezone.utc).year
    sessions = openf1.get("sessions", use_cache=not cur, year=year)
    meetings = {m["meeting_key"]: m for m in openf1.get("meetings", use_cache=not cur, year=year)}
    done_before = datetime.now(timezone.utc) - timedelta(hours=2)
    by: dict[int, dict] = {}
    for s in sessions:
        if s.get("session_name") not in ("Race", "Sprint") or not s.get("date_end"):
            continue
        if parse_date(s["date_end"]) > done_before:
            continue
        m = meetings.get(s["meeting_key"], {})
        e = by.setdefault(s["meeting_key"], {
            "meeting_key": s["meeting_key"],
            "name": m.get("meeting_name") or f"{s.get('location', '')} Grand Prix",
            "location": s.get("location", ""), "country": s.get("country_name", ""),
            "date": s["date_start"][:10], "sessions": []})
        e["sessions"].append({"key": s["session_key"], "name": s["session_name"], "date": s["date_start"]})
        if s["session_name"] == "Race":
            e["date"] = s["date_start"][:10]
    out = sorted(by.values(), key=lambda e: e["date"])
    for e in out:
        e["sessions"].sort(key=lambda x: x["date"])
    return out


# ---- replay hub -----------------------------------------------------------------------
class ReplayHub:
    """lobby -> loading -> playing (or error); owns the engine and the virtual clock."""

    def __init__(self, bc: Broadcaster):
        self.bc = bc
        self.state = "lobby"
        self.message = ""
        self.label = ""
        self.engine: Engine | None = None
        self.control = {"speed": 10.0, "paused": False, "restart": False, "home": False}
        self.msgs: list[Message] = []
        self.start_t = 0.0
        self.end_t = 0.0
        self.i = 0
        self.vclock = 0.0
        self.last = time.monotonic()
        self._track = None

    # -- loading
    def _blocking_load(self, spec: dict):
        from . import openf1, tracks_store
        from .sources import openf1_replay, recording
        from .track import Track
        if spec.get("kind") == "bundled":
            self.message = "Reading the recording…"
            msgs = recording.load(BUNDLED)
            track = Track.load("interlagos")
            t_start = next((m[0] for m in msgs if m[1] == "SessionStatus" and (m[2] or {}).get("Status") == "Started"),
                           msgs[0][0])
            return msgs, track, max(msgs[0][0], t_start), BUNDLED_LABEL
        key = int(spec["session_key"])
        sess = openf1.get("sessions", session_key=key)
        if not sess:
            raise RuntimeError("that session was not found on OpenF1")
        sess = sess[0]
        meeting = spec.get("name") or f"{sess.get('location', '')} Grand Prix"
        self.message = "Downloading race data from OpenF1 (the first time takes a minute)…"
        msgs, meta = openf1_replay.load_session(sess, meeting)
        self.message = "Building the track map…"
        track = tracks_store.for_session(sess)
        label = f"{meeting} {sess.get('year', '')} · {sess.get('session_name', '')}"
        return msgs, track, meta["t_start"] - 60, label

    async def load(self, spec: dict) -> None:
        self.state, self.message, self.engine = "loading", "Starting…", None
        loop = asyncio.get_running_loop()
        try:
            msgs, track, start_t, label = await loop.run_in_executor(None, self._blocking_load, spec)
        except Exception as e:  # noqa: BLE001  (shown to the user)
            log.warning("replay load failed: %s", e)
            self.state, self.message = "error", str(e)
            return
        self.set_content(Engine(track), msgs, start_t, label)

    def set_content(self, engine: Engine, msgs: list[Message], start_t: float, label: str) -> None:
        self._track = engine.track
        self.msgs, self.start_t, self.label = msgs, start_t, label
        self.end_t = msgs[-1][0] if msgs else start_t
        self._begin()
        self.state = "playing"

    def _begin(self) -> None:
        eng = Engine(self._track)
        i = 0
        while i < len(self.msgs) and (self.msgs[i][0] <= self.start_t or self.msgs[i][3]):
            t, topic, data, initial = self.msgs[i]
            eng.ingest(t, topic, data, initial)
            i += 1
        eng.start(self.start_t)
        self.engine, self.i, self.vclock = eng, i, self.start_t
        self.last = time.monotonic()
        self.control["paused"] = False

    # -- main loop
    async def run(self, loop_forever: bool = True) -> None:
        while True:
            await asyncio.sleep(1 / HZ)
            c = self.control
            if c["home"]:
                c["home"] = False
                if self.state in ("playing", "error"):
                    self.state, self.engine = "lobby", None
            if self.state != "playing":
                await self.bc.send({"mode": "replay", "state": self.state, "message": self.message})
                self.last = time.monotonic()
                continue
            if c["restart"]:
                c["restart"] = False
                self._begin()
                log.info("replay restarted")
            now = time.monotonic()
            finished = self.i >= len(self.msgs) and self.vclock > self.end_t + 10
            if not c["paused"] and not finished:
                self.vclock += (now - self.last) * c["speed"]
            self.last = now
            eng = self.engine
            while self.i < len(self.msgs) and self.msgs[self.i][0] <= self.vclock:
                t, topic, data, initial = self.msgs[self.i]
                eng.ingest(t, topic, data, initial)
                self.i += 1
            eng.tick(self.vclock)
            snap = eng.snapshot()
            snap.update(mode="replay", state="playing", speed=c["speed"], paused=c["paused"],
                        finished=finished, track=eng.track.name, label=self.label)
            await self.bc.send(snap)
            if finished and not loop_forever:
                return


# ---- app ------------------------------------------------------------------------------
def make_app(engine_ref: dict, bc: Broadcaster, hub: ReplayHub | None = None) -> web.Application:
    async def index(_):
        return web.FileResponse(WEB / "index.html")

    async def track(_):
        eng = engine_ref.get("engine")
        if eng is None:
            return web.json_response({"name": None}, status=404)
        tr = eng.track
        step = max(1, int(round(10.0 / (tr.s[1] - tr.s[0]))))  # ~10 m resolution is plenty
        return web.json_response({
            "name": tr.name, "length": tr.length,
            "x": [round(v, 1) for v in tr.x[::step]], "y": [round(v, 1) for v in tr.y[::step]],
            "gates": [list(tr.xy(g)) + list(tr.normal(g)) for g in tr.gates],
            "pit_lane": tr.pit_lane,
        })

    async def calendar(request):
        try:
            year = int(request.query.get("year", ""))
        except ValueError:
            return web.json_response({"error": "year required"}, status=400)
        if not MIN_YEAR <= year <= datetime.now(timezone.utc).year:
            return web.json_response({"races": [], "error": f"years {MIN_YEAR} onwards"}, status=400)
        try:
            races = await asyncio.get_running_loop().run_in_executor(None, calendar_for, year)
            return web.json_response({"year": year, "races": races})
        except Exception as e:  # noqa: BLE001
            return web.json_response({"year": year, "races": [], "error": str(e)})

    async def load(request):
        if hub is None:
            return web.json_response({"error": "not in replay mode"}, status=400)
        if hub.state == "loading":
            return web.json_response({"error": "already loading"}, status=409)
        try:
            spec = await request.json()
        except ValueError:
            return web.json_response({"error": "bad request"}, status=400)
        asyncio.create_task(hub.load(spec))
        return web.json_response({"ok": True})

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        bc.clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
                if msg.type == WSMsgType.TEXT and hub is not None:
                    try:  # replay controls from the page: {"speed": 5} / {"paused": true} / {"restart": true}
                        cmd = json.loads(msg.data)
                        if isinstance(cmd.get("speed"), (int, float)) and 0 < cmd["speed"] <= 100:
                            hub.control["speed"] = float(cmd["speed"])
                        if isinstance(cmd.get("paused"), bool):
                            hub.control["paused"] = cmd["paused"]
                        if cmd.get("restart") is True:
                            hub.control["restart"] = True
                        if cmd.get("home") is True:
                            hub.control["home"] = True
                    except (ValueError, AttributeError):
                        pass
        finally:
            bc.clients.discard(ws)
        return ws

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/track", track)
    app.router.add_get("/api/calendar", calendar)
    app.router.add_post("/api/load", load)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", WEB)
    return app


async def serve(engine_ref: dict, bc: Broadcaster, port: int, hub: ReplayHub | None = None) -> web.AppRunner:
    runner = web.AppRunner(make_app(engine_ref, bc, hub))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("open http://localhost:%d", port)
    return runner


class _HubRef(dict):
    """engine_ref view that always points at the hub's current engine."""
    def __init__(self, hub: ReplayHub):
        super().__init__()
        self.hub = hub

    def get(self, key, default=None):
        return self.hub.engine if key == "engine" else default


# ---- replay mode ------------------------------------------------------------------
async def run_lobby(port: int, speed: float = 10.0) -> None:
    """Replay server that starts with the race picker."""
    bc = Broadcaster()
    hub = ReplayHub(bc)
    hub.control["speed"] = float(speed)
    await serve(_HubRef(hub), bc, port, hub)
    await hub.run()


async def run_replay(engine: Engine, msgs: list[Message], start_t: float, speed: float,
                     port: int, loop_forever: bool = True) -> None:
    """Replay a prepared message list straight away (dev: `tracker replay ...`)."""
    bc = Broadcaster()
    hub = ReplayHub(bc)
    hub.control["speed"] = float(speed)
    await serve(_HubRef(hub), bc, port, hub)
    hub.set_content(engine, msgs, start_t, "replay")
    log.info("replay from %.0f s before the last message at %sx", hub.end_t - start_t, speed)
    await hub.run(loop_forever)


# ---- live mode ------------------------------------------------------------------------
def _circuit_of(topics: dict) -> tuple[int | None, str | None]:
    c = ((topics.get("SessionInfo") or {}).get("Meeting") or {}).get("Circuit") or {}
    return c.get("Key"), c.get("ShortName")


def _fetch_static_session_info() -> dict:
    import requests
    r = requests.get("https://livetiming.formula1.com/static/SessionInfo.json", timeout=15)
    r.raise_for_status()
    return json.loads(r.content.decode("utf-8-sig"))


async def run_live(track_name: str | None, port: int) -> None:
    from . import tracks_store
    from .sources.live import LiveClient
    from .state import SessionState
    from .track import Track
    import threading

    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    client = LiveClient(loop, q)
    threading.Thread(target=client.supervise, daemon=True, name="signalr").start()

    bc = Broadcaster()
    state = SessionState()                      # shared by every engine we create
    ref: dict = {"engine": None, "notice": "Looking up the circuit…", "circuit": None}
    await serve(ref, bc, port)
    offset = None  # server clock - local clock, smoothed

    def now() -> float:
        return time.time() + (offset or 0.0)

    def install(track: Track) -> None:
        eng = Engine(track)
        eng.state = state
        eng.est.state = state
        ref["engine"], ref["notice"] = eng, ""
        eng.start(now())
        info = eng.snapshot()
        log.info("track %s ready; session: %s %s (%s)", track.name, info["meeting"], info["session"],
                 state.session_status)

    async def ensure_track(circuit: tuple[int | None, str | None]) -> None:
        key, short = circuit
        if key is None and not short:
            return
        ref["notice"] = f"Preparing the track map for {short or key}…"
        try:
            track = await loop.run_in_executor(None, tracks_store.for_circuit, key, short)
        except Exception as e:  # noqa: BLE001  (shown on the page)
            log.error("no track model for %s: %s", short or key, e)
            ref["notice"] = (f"No track map for {short or key} yet: {e} "
                             "Start live mode before the session begins, or try the replay.")
            return
        install(track)

    if track_name:
        install(Track.load(track_name))

    async def prefetch():
        try:
            info = await loop.run_in_executor(None, _fetch_static_session_info)
        except Exception as e:  # noqa: BLE001
            log.info("could not read the static session info: %s", e)
            return
        circ = _circuit_of({"SessionInfo": info})
        if ref["circuit"] is None and not track_name:
            ref["circuit"] = circ
            await ensure_track(circ)

    async def consume():
        nonlocal offset
        while True:
            item = await q.get()
            if item[0] == "init":
                _, recv, res = item
                hb = (res.get("Heartbeat") or {}).get("Utc")
                if hb:
                    from .sources.archive import utc_epoch
                    offset = utc_epoch(hb) - recv
                for topic, data in res.items():
                    if data is not None:
                        state.load_initial(topic, data)
                circ = _circuit_of(state.topics)
                if not track_name and circ != ref["circuit"] and (circ[0] is not None or circ[1]):
                    ref["circuit"] = circ
                    asyncio.create_task(ensure_track(circ))
            else:
                _, t_msg, topic, data, recv = item
                d = t_msg - recv
                if abs(d) < 600:
                    offset = d if offset is None else 0.98 * offset + 0.02 * d
                eng = ref["engine"]
                if eng is None:
                    state.apply(topic, data, min(t_msg, now()))
                else:
                    eng.ingest(min(t_msg, now()), topic, data)

    asyncio.create_task(prefetch())
    asyncio.create_task(consume())
    while True:
        await asyncio.sleep(1 / HZ)
        eng = ref["engine"]
        if eng is None:
            await bc.send({"mode": "live", "state": "no_track", "notice": ref["notice"], "cars": []})
            continue
        eng.tick(now())
        snap = eng.snapshot()
        snap.update(mode="live", state="playing", track=eng.track.name)
        await bc.send(snap)
