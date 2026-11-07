"""Deep merge of F1 live-timing partial updates into stored state."""
from __future__ import annotations

from typing import Any


def _index(key: Any) -> int | None:
    if isinstance(key, int):
        return key
    if isinstance(key, str) and key.isdigit():
        return int(key)
    return None


def deep_merge(base: Any, delta: Any) -> Any:
    """Merge ``delta`` into ``base`` in place where possible and return the result.

    - dict into dict: recursive, keys listed in ``_deleted`` are removed.
    - dict keyed "0","1",... into a list: element-wise update, extending as needed.
    - list into list (or anything else): replaced.
    """
    if isinstance(delta, dict):
        if isinstance(base, list):
            for k, v in delta.items():
                i = _index(k)
                if i is None:
                    continue
                while len(base) <= i:
                    base.append({} if isinstance(v, dict) else None)
                base[i] = deep_merge(base[i], v)
            return base
        if not isinstance(base, dict):
            base = {}
        for k, v in delta.items():
            if k == "_deleted":
                for dk in v or []:
                    base.pop(str(dk), None)
                continue
            base[k] = deep_merge(base.get(k), v) if isinstance(v, dict) else v
        return base
    return delta
