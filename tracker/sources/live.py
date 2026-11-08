"""Live F1 timing over SignalR Core, no authentication (mirrors FastF1 3.9's
fastf1/livetiming/client.py, minus the F1-account token).

- pre-negotiate to get the AWSALBCORS cookie
- HubConnectionBuilder -> wss://livetiming.formula1.com/signalrcore
- send("Subscribe", [topics]) -> completion result {topic: full state}
- hub method "feed" -> [topic, delta, timestamp]
- every raw message is appended to recordings/<date>.jsonl
- auto-reconnect: a supervisor rebuilds the connection when it closes or goes quiet
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from signalrcore.hub_connection_builder import HubConnectionBuilder
from signalrcore.messages.completion_message import CompletionMessage

from .recording import feed_time

log = logging.getLogger(__name__)
URL = "wss://livetiming.formula1.com/signalrcore"
NEGOTIATE = "https://livetiming.formula1.com/signalrcore/negotiate"
TOPICS = ["Heartbeat", "SessionInfo", "SessionStatus", "DriverList", "TimingData", "TimingAppData",
          "TrackStatus", "LapCount", "ExtrapolatedClock", "RaceControlMessages", "TopThree", "WeatherData"]
REC_DIR = Path(__file__).resolve().parents[2] / "recordings"
QUIET_RECONNECT_S = 90  # heartbeats arrive every ~15 s during a session


class LiveClient:
    """Runs the (threaded) signalrcore client and hands messages to an asyncio queue."""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue, record: bool = True):
        self.loop, self.queue = loop, queue
        self.conn = None
        self.connected = threading.Event()
        self.last_msg = time.time()
        self.stop_flag = threading.Event()
        self.rec = None
        if record:
            REC_DIR.mkdir(exist_ok=True)
            name = datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl"
            self.rec_path = REC_DIR / name
            self.rec = open(self.rec_path, "a", encoding="utf-8")  # noqa: SIM115
            log.info("recording raw messages to %s", self.rec_path)

    # ---- callbacks (signalrcore thread) ---------------------------------------
    def _write(self, rec: dict) -> None:
        if self.rec:
            try:
                self.rec.write(json.dumps(rec, separators=(",", ":")) + "\n")
                self.rec.flush()
            except Exception:  # noqa: BLE001
                log.exception("recording write failed")

    def _put(self, item) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, item)

    def _on_feed(self, msg) -> None:
        self.last_msg = recv = time.time()
        self._write({"recv": recv, "feed": msg})
        if isinstance(msg, list) and len(msg) >= 2:
            self._put(("feed", feed_time(msg, recv), msg[0], msg[1], recv))

    def _on_subscribe(self, msg) -> None:
        self.last_msg = recv = time.time()
        if isinstance(msg, CompletionMessage):
            if msg.error:
                log.error("subscribe error: %s", msg.error)
            res = msg.result or {}
            self._write({"recv": recv, "init": res})
            log.info("subscribed: %s", ", ".join(sorted(k for k, v in res.items() if v is not None)))
            self._put(("init", recv, res))

    # ---- connection management ------------------------------------------------
    def _connect(self) -> None:
        r = requests.options(NEGOTIATE, timeout=15)
        headers = {}
        if "AWSALBCORS" in r.cookies:
            headers["Cookie"] = f"AWSALBCORS={r.cookies['AWSALBCORS']}"
        self.connected.clear()
        conn = HubConnectionBuilder().with_url(URL, options={"verify_ssl": True, "headers": headers}) \
            .configure_logging(logging.WARNING).build()
        conn.on_open(self.connected.set)
        conn.on_close(lambda: (log.warning("connection closed"), self.connected.clear()))
        conn.on_error(lambda e: log.warning("hub error: %s", e))
        conn.on("feed", self._on_feed)
        conn.start()
        if not self.connected.wait(20):
            raise ConnectionError("timeout opening SignalR connection")
        self.conn = conn
        self.last_msg = time.time()
        conn.send("Subscribe", [TOPICS], on_invocation=self._on_subscribe)
        log.info("connected to %s", URL)

    def _close(self) -> None:
        try:
            if self.conn:
                self.conn.stop()
        except Exception:  # noqa: BLE001
            pass
        self.conn = None

    def supervise(self) -> None:
        """Blocking loop (run in a thread): connect, watch, reconnect with backoff."""
        backoff = 2
        while not self.stop_flag.is_set():
            try:
                self._connect()
                backoff = 2
                while not self.stop_flag.is_set():
                    time.sleep(1)
                    if not self.connected.is_set():
                        raise ConnectionError("disconnected")
                    if time.time() - self.last_msg > QUIET_RECONNECT_S:
                        raise ConnectionError(f"no data for {QUIET_RECONNECT_S}s")
            except Exception as e:  # noqa: BLE001
                log.warning("live connection problem: %s; reconnecting in %ss", e, backoff)
                self._close()
                self.stop_flag.wait(backoff)
                backoff = min(backoff * 2, 30)
        self._close()
        if self.rec:
            self.rec.close()
