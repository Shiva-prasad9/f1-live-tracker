"""Tiny OpenF1 REST client (https://api.openf1.org, free, no auth for historical data).

Responses are cached on disk under data/cache/openf1 so repeated runs don't hit the API.
Requests are rate limited to stay well within the free tier.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from urllib.parse import quote

import requests

BASE = "https://api.openf1.org/v1/"
CACHE = Path(__file__).resolve().parent.parent / "data" / "cache" / "openf1"
log = logging.getLogger(__name__)
_last_request = 0.0


class OpenF1Error(RuntimeError):
    pass


class OpenF1Locked(OpenF1Error):
    """OpenF1 refuses all requests while a real F1 session is running."""


def get(endpoint: str, *filters: str, use_cache: bool = True, **params) -> list[dict]:
    """GET an OpenF1 endpoint.

    ``params`` are equality filters; ``filters`` are raw comparison filters
    such as ``"date>=2025-11-09T17:00:00"``.
    """
    parts = [f"{k}={quote(str(v))}" for k, v in params.items()]
    for f in filters:
        for op in (">=", "<=", ">", "<"):
            if op in f:
                k, v = f.split(op, 1)
                parts.append(f"{k}{op}{quote(v)}")
                break
    url = BASE + endpoint + ("?" + "&".join(parts) if parts else "")
    key = hashlib.sha1(url.encode()).hexdigest()[:20]
    path = CACHE / f"{endpoint}_{key}.json"
    if use_cache and path.exists():
        return json.loads(path.read_text())

    global _last_request
    for attempt in range(8):
        wait = 0.6 - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
        log.info("GET %s", url)
        r = requests.get(url, timeout=120)
        if r.status_code == 429 or r.status_code >= 500:
            log.info("OpenF1 busy (%s); retrying", r.status_code)
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 404:
            data = []  # OpenF1 answers 404 for "no results"
            break
        if r.status_code == 401:
            raise OpenF1Locked("OpenF1 is locked while a live F1 session is running; "
                               "it reopens shortly after the session ends.")
        if r.status_code != 200:
            raise OpenF1Error(f"{r.status_code} for {url}: {r.text[:300]}")
        data = r.json()
        break
    else:
        raise OpenF1Error(f"giving up on {url}")
    if use_cache:
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    return data
