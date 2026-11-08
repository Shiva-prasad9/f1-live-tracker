"""aiohttp server: static page at /, track geometry at /track, snapshots on /ws at ~10 Hz."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from .engine import Engine
from .sources import Message

log = logging.getLogger(__name__)
WEB = Path(__file__).resolve().parent.parent / "web"
HZ = 10


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


def make_app(engine_ref: dict, bc: Broadcaster, control: dict | None = None) -> web.Application:
    async def index(_):
        return web.FileResponse(WEB / "index.html")

    async def track(_):
        tr = engine_ref["engine"].track
        step = max(1, int(round(10.0 / (tr.s[1] - tr.s[0]))))  # ~10 m resolution is plenty
        return web.json_response({
            "name": tr.name, "length": tr.length,
            "x": [round(v, 1) for v in tr.x[::step]], "y": [round(v, 1) for v in tr.y[::step]],
            "gates": [list(tr.xy(g)) + list(tr.normal(g)) for g in tr.gates],
            "pit_lane": tr.pit_lane,
        })

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        bc.clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
                if msg.type == WSMsgType.TEXT and control is not None:
                    try:  # replay controls from the page: {"speed": 5} / {"paused": true}
                        cmd = json.loads(msg.data)
                        if isinstance(cmd.get("speed"), (int, float)) and 0 < cmd["speed"] <= 100:
                            control["speed"] = float(cmd["speed"])
                        if isinstance(cmd.get("paused"), bool):
                            control["paused"] = cmd["paused"]
                        if cmd.get("restart") is True:
                            control["restart"] = True
                    except (ValueError, AttributeError):
                        pass
        finally:
            bc.clients.discard(ws)
        return ws

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/track", track)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", WEB)
    return app


async def serve(engine_ref: dict, bc: Broadcaster, port: int, control: dict | None = None) -> web.AppRunner:
    runner = web.AppRunner(make_app(engine_ref, bc, control))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("open http://localhost:%d", port)
    return runner


# ---- replay mode ------------------------------------------------------------------
async def run_replay(engine: Engine, msgs: list[Message], start_t: float, speed: float,
                     port: int, loop_forever: bool = True) -> None:
    bc = Broadcaster()
    ref = {"engine": engine}
    control = {"speed": float(speed), "paused": False, "restart": False}
    await serve(ref, bc, port, control)
    end_t = msgs[-1][0] if msgs else start_t

    def begin() -> tuple[int, float]:
        """(Re)build the engine at start_t; returns (next message index, virtual clock)."""
        eng = Engine(ref["engine"].track)
        ref["engine"] = eng
        i = 0
        while i < len(msgs) and (msgs[i][0] <= start_t or msgs[i][3]):
            t, topic, data, initial = msgs[i]
            eng.ingest(t, topic, data, initial)
            i += 1
        eng.start(start_t)
        return i, start_t

    i, vclock = begin()
    last = time.monotonic()
    log.info("replay from %.0f s before the last message at %sx", end_t - start_t, speed)
    while True:
        await asyncio.sleep(1 / HZ)
        now = time.monotonic()
        if control["restart"]:
            control["restart"] = False
            i, vclock = begin()
            control["paused"] = False
            log.info("replay restarted")
        finished = i >= len(msgs) and vclock > end_t + 10
        if not control["paused"] and not finished:
            vclock += (now - last) * control["speed"]
        last = now
        eng = ref["engine"]
        while i < len(msgs) and msgs[i][0] <= vclock:
            t, topic, data, initial = msgs[i]
            eng.ingest(t, topic, data, initial)
            i += 1
        eng.tick(vclock)
        snap = eng.snapshot()
        snap["mode"], snap["speed"], snap["paused"] = "replay", control["speed"], control["paused"]
        snap["finished"] = finished
        await bc.send(snap)   # keeps sending the final state so new tabs still get a picture
        if finished and not loop_forever:
            return


# ---- live mode ------------------------------------------------------------------------
async def run_live(track_name: str | None, port: int) -> None:
    from .engine import track_for_session
    from .sources.live import LiveClient
    from .track import Track
    import threading

    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    client = LiveClient(loop, q)
    threading.Thread(target=client.supervise, daemon=True, name="signalr").start()

    bc = Broadcaster()
    ref: dict = {"engine": Engine(Track.load(track_name or "interlagos"))}
    await serve(ref, bc, port)
    offset = None  # server clock - local clock, smoothed

    def now() -> float:
        return time.time() + (offset or 0.0)

    async def consume():
        nonlocal offset
        while True:
            item = await q.get()
            eng: Engine = ref["engine"]
            if item[0] == "init":
                _, recv, res = item
                hb = (res.get("Heartbeat") or {}).get("Utc")
                if hb:
                    from .sources.archive import utc_epoch
                    offset = utc_epoch(hb) - recv
                if not eng.initialised:
                    for topic, data in res.items():
                        if data is not None:
                            eng.state.load_initial(topic, data)
                    want = track_for_session(eng.state, track_name)
                    if want != eng.track.name:
                        try:
                            new = Engine(Track.load(want))
                            new.state = eng.state
                            new.est.state = eng.state
                            ref["engine"] = eng = new
                        except FileNotFoundError:
                            log.error("no track model tracks/%s.json; using %s", want, eng.track.name)
                    eng.start(now())
                    log.info("session: %s %s (%s)", eng.snapshot()["meeting"], eng.snapshot()["session"],
                             eng.state.session_status)
                else:
                    # re-subscribe after reconnect: refresh state without re-placing cars
                    for topic, data in res.items():
                        if data is not None:
                            eng.state.load_initial(topic, data)
            else:
                _, t_msg, topic, data, recv = item
                d = t_msg - recv
                if abs(d) < 600:
                    offset = d if offset is None else 0.98 * offset + 0.02 * d
                eng.ingest(min(t_msg, now()), topic, data)

    asyncio.create_task(consume())
    while True:
        await asyncio.sleep(1 / HZ)
        eng = ref["engine"]
        eng.tick(now())
        snap = eng.snapshot()
        snap["mode"] = "live"
        await bc.send(snap)
