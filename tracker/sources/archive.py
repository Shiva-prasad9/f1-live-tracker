"""F1 static archive replay source: https://livetiming.formula1.com/static/

Index.json -> year -> meeting -> session Path -> <Topic>.jsonStream, each line
"HH:MM:SS.mmm{json}" (offset from stream start). The first line of each stream
is the full initial state, the rest are partial updates.

NOTE (Nov 2026): the archive currently answers 401 to anonymous requests (FastF1
3.9 signs in with an F1 account for it). This source is kept so it works again
if the archive reopens; use `--source openf1` otherwise.
"""
from __future__ import annotations

import base64
import json
import logging
import unicodedata
import zlib
from datetime import datetime
from pathlib import Path

import requests

from . import Message

BASE = "https://livetiming.formula1.com/static/"
CACHE = Path(__file__).resolve().parents[2] / "data" / "cache" / "static"
TOPICS = ["SessionInfo", "SessionStatus", "DriverList", "TimingData", "TimingAppData",
          "TrackStatus", "LapCount", "Heartbeat"]
log = logging.getLogger(__name__)


class ArchiveUnavailable(RuntimeError):
    pass


def _fetch(path: str) -> str:
    cache = CACHE / path
    if cache.exists():
        return cache.read_text(encoding="utf-8")
    r = requests.get(BASE + path, timeout=60)
    if r.status_code in (401, 403):
        raise ArchiveUnavailable(
            f"{r.status_code} from the F1 static archive for {path}: the archive now needs an F1 "
            f"account. Use --source openf1 instead.")
    if r.status_code != 200:
        raise ArchiveUnavailable(f"{r.status_code} for {BASE + path}")
    text = r.content.decode("utf-8-sig")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return text


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()


def find_session_path(year: int, event: str, session: str) -> str:
    idx = json.loads(_fetch(f"{year}/Index.json"))
    for m in idx.get("Meetings", []):
        if _norm(event) in (_norm(m.get("Name", "")), _norm(m.get("OfficialName", ""))) or \
                _norm(event) in _norm(m.get("Name", "")):
            for s in m.get("Sessions", []):
                if _norm(s.get("Name", "")) == _norm(session) and s.get("Path"):
                    return s["Path"]
    raise ArchiveUnavailable(f"session {year} {event} {session} not in Index.json")


def parse_stream(text: str) -> list[tuple[float, object]]:
    """'HH:MM:SS.mmm{json}' lines -> [(offset_seconds, payload)]. Payload may be a str (.z)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if len(line) < 13:
            continue
        ts, body = line[:12], line[12:]
        try:
            h, m, s = ts.split(":")
            off = int(h) * 3600 + int(m) * 60 + float(s)
            payload = json.loads(body)
        except ValueError:
            continue
        out.append((off, payload))
    return out


def decode_z(payload: str) -> object:
    """Position.z / CarData.z: base64 of raw-deflate JSON."""
    return json.loads(zlib.decompress(base64.b64decode(payload), -zlib.MAX_WBITS))


def utc_epoch(s: str) -> float:
    s = s.rstrip("Z")
    if "." in s:  # trim 7-digit fractions
        a, b = s.split(".")
        s = f"{a}.{b[:6]}"
    return datetime.fromisoformat(s + "+00:00").timestamp()


def load(year: int, event: str, session: str) -> tuple[list[Message], str]:
    """Download and normalise a session. Returns (messages, session path)."""
    path = find_session_path(year, event, session)
    streams = {t: parse_stream(_fetch(f"{path}{t}.jsonStream")) for t in TOPICS}
    # offset -> UTC from Heartbeat
    base = None
    for off, hb in streams.get("Heartbeat", []):
        if isinstance(hb, dict) and hb.get("Utc"):
            base = utc_epoch(hb["Utc"]) - off
            break
    if base is None:
        raise ArchiveUnavailable("no Heartbeat to anchor the clock")
    msgs: list[Message] = []
    for topic, entries in streams.items():
        for i, (off, data) in enumerate(entries):
            msgs.append((base + off, topic, data, i == 0))
    msgs.sort(key=lambda m: (m[0], not m[3]))
    return msgs, path


def load_positions(path: str) -> dict[str, list[tuple[float, float, float]]]:
    """Ground truth from Position.z: {car: [(t_epoch, x_m, y_m)]}. Validation only."""
    out: dict[str, list] = {}
    for _off, payload in parse_stream(_fetch(f"{path}Position.z.jsonStream")):
        data = decode_z(payload) if isinstance(payload, str) else payload
        for p in data.get("Position", []):
            t = utc_epoch(p["Timestamp"])
            for car, e in p.get("Entries", {}).items():
                if e.get("Status") == "OnTrack" or e.get("X") or e.get("Y"):
                    out.setdefault(car, []).append((t, e["X"] / 10.0, e["Y"] / 10.0))
    return out
