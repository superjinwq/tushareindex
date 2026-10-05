"""A 股交易时段判定（行情监控各缓存/后台刷新共用）。

- 交易日：stock_tushare.trade_cal（exchange='SSE', is_open=1），按自然年缓存。
- 时段：09:15–11:30、13:00–15:00（含盘前集合竞价，避免开盘延迟）。
- trade_cal 查询失败或该年无数据时退化为「周一至周五即交易日」：宁可多刷新，不误判休市。
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, time as dtime, timedelta

import pymysql
from logger_config import get_logger

from rt_monitor.core.config import DB_CONFIG_TUSHARE

logger = get_logger(__name__)

SESSION_WINDOWS: tuple[tuple[dtime, dtime], ...] = (
    (dtime(9, 15), dtime(11, 30)),
    (dtime(13, 0), dtime(15, 0)),
)
_YEAR_CACHE_TTL_SEC = 6 * 3600.0
_YEAR_FAIL_RETRY_SEC = 60.0

_yearCacheLock = threading.Lock()
# year -> (过期时间戳, 开市日集合；None 表示查询失败/无数据，走工作日退化)
_yearCache: dict[int, tuple[float, frozenset[date] | None]] = {}


def _loadOpenDaysOfYear(year: int) -> frozenset[date] | None:
    """查询某年 SSE 开市日；失败或无数据返回 None。"""
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    except Exception as e:
        logger.warning('trade_cal 连接失败，交易日退化为工作日判定: %s', e)
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cal_date FROM trade_cal WHERE exchange='SSE' AND is_open=1"
                " AND cal_date BETWEEN %s AND %s",
                (f'{year}-01-01', f'{year}-12-31'),
            )
            days = frozenset(
                r[0] if isinstance(r[0], date) else datetime.strptime(str(r[0])[:10], '%Y-%m-%d').date()
                for r in cur.fetchall()
            )
    except Exception as e:
        logger.warning('trade_cal 查询失败，交易日退化为工作日判定: %s', e)
        return None
    finally:
        conn.close()
    return days or None


def resetTradeDayCache() -> None:
    with _yearCacheLock:
        _yearCache.clear()


def isTradeDay(d: date) -> bool:
    nowTs = time.time()
    with _yearCacheLock:
        hit = _yearCache.get(d.year)
    if hit is None or nowTs >= hit[0]:
        days = _loadOpenDaysOfYear(d.year)
        ttl = _YEAR_CACHE_TTL_SEC if days is not None else _YEAR_FAIL_RETRY_SEC
        hit = (nowTs + ttl, days)
        with _yearCacheLock:
            _yearCache[d.year] = hit
    days = hit[1]
    if days is None:
        return d.weekday() < 5
    return d in days


def isAShareMarketActive(now: datetime | None = None, closeGraceSec: float = 0.0) -> bool:
    """当前是否处于 A 股交易时段；closeGraceSec 把每段收盘时刻向后顺延（收盘数据落库缓冲）。"""
    now = now or datetime.now()
    if not isTradeDay(now.date()):
        return False
    for start, end in SESSION_WINDOWS:
        startDt = datetime.combine(now.date(), start)
        endDt = datetime.combine(now.date(), end) + timedelta(seconds=closeGraceSec)
        if startDt <= now < endDt:
            return True
    return False

# 代码更新时间：2026-10-04 17:42 交易时段共用判定
