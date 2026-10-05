"""进程内小 TTL OrderedDict 缓存（申万/宽基等模块共用）。"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

_cacheLock = threading.Lock()


def smallTtlCacheGet(cacheObj: OrderedDict, cacheKey: Any, nowTs: float):
    with _cacheLock:
        hit = cacheObj.get(cacheKey)
        if hit is not None and nowTs < hit[0]:
            cacheObj.move_to_end(cacheKey)
            return hit[1]
    return None


def smallTtlCachePut(
    cacheObj: OrderedDict,
    cacheKey: Any,
    payload: dict,
    ttl: float,
    maxItems: int,
) -> None:
    with _cacheLock:
        while len(cacheObj) >= maxItems:
            cacheObj.popitem(last=False)
        cacheObj[cacheKey] = (time.time() + ttl, payload)
        cacheObj.move_to_end(cacheKey)

# 代码更新时间：2026-06-16 14:30
