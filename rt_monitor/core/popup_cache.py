"""弹窗序列短 TTL 进程内缓存（margin / marketinfo / up_num_ma5 等共用）。"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

POPUP_SERIES_CACHE_TTL = 3.0
POPUP_SERIES_CACHE_MAX = 96

_cacheLock = threading.Lock()
_popupSeriesCache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()


def popupSeriesCacheGet(cacheKey: tuple):
    nowTs = time.time()
    with _cacheLock:
        hit = _popupSeriesCache.get(cacheKey)
        if hit is not None and nowTs < hit[0]:
            _popupSeriesCache.move_to_end(cacheKey)
            return hit[1]
    return None


def popupSeriesCachePut(cacheKey: tuple, payload: dict, ttlSec: float | None = None):
    ttl = POPUP_SERIES_CACHE_TTL if ttlSec is None else float(ttlSec)
    with _cacheLock:
        while len(_popupSeriesCache) >= POPUP_SERIES_CACHE_MAX:
            _popupSeriesCache.popitem(last=False)
        _popupSeriesCache[cacheKey] = (time.time() + ttl, payload)
        _popupSeriesCache.move_to_end(cacheKey)

# 代码更新时间：2026-06-17 22:58
# 代码更新时间：2026-09-02 10:50 允许按 key 指定 TTL（Top5% 全历史序列）
