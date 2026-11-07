"""Replay a raw live recording (recordings/<date>.jsonl written by the live client).

Line formats:
  {"recv": epoch, "init": {topic: full_state, ...}}     Subscribe result (after each (re)connect)
  {"recv": epoch, "feed": [topic, data, "ISO timestamp"]}
"""
from __future__ import annotations

import json
from pathlib import Path

from . import Message
from .archive import utc_epoch


def feed_time(item: list, recv: float | None) -> float:
    if len(item) > 2 and isinstance(item[2], str) and item[2]:
        try:
            return utc_epoch(item[2])
        except ValueError:
            pass
    return recv or 0.0


def load(path: str | Path) -> list[Message]:
    msgs: list[Message] = []
    last_t = 0.0
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        recv = rec.get("recv") or rec.get("t")
        if "init" in rec and isinstance(rec["init"], dict):
            hb = (rec["init"].get("Heartbeat") or {}).get("Utc")
            t = utc_epoch(hb) if hb else (recv or last_t)
            for topic, data in rec["init"].items():
                if data is not None:
                    msgs.append((t, topic, data, True))
        elif "feed" in rec:
            item = rec["feed"]
            if isinstance(item, list) and len(item) >= 2:
                t = feed_time(item, recv)
                last_t = max(last_t, t)
                msgs.append((t, item[0], item[1], False))
    # keep file order for equal times; inits first
    msgs.sort(key=lambda m: m[0])
    return msgs
