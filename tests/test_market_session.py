"""rt_monitor.core.market_session：交易日（trade_cal）与交易时段边界。"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rt_monitor.core import market_session as ms  # noqa: E402

# 2026 国庆：10-01~10-07 休市，10-08(周四)/10-09(周五) 开市，10-10/11 周末，10-12 周一开市
_OPEN_2026 = frozenset({
    date(2026, 9, 30), date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12),
})


@pytest.fixture(autouse=True)
def fakeTradeCal(monkeypatch):
    calls: list[int] = []

    def loader(year: int):
        calls.append(year)
        return _OPEN_2026 if year == 2026 else None

    monkeypatch.setattr(ms, '_loadOpenDaysOfYear', loader)
    ms.resetTradeDayCache()
    yield calls
    ms.resetTradeDayCache()


@pytest.mark.parametrize('ts, expected', [
    ('2026-10-08 09:14:59', False),   # 盘前竞价前
    ('2026-10-08 09:15:00', True),    # 盘前竞价开始即属交易时段
    ('2026-10-08 10:00:00', True),
    ('2026-10-08 11:29:59', True),
    ('2026-10-08 11:30:00', False),   # 午休
    ('2026-10-08 12:59:59', False),
    ('2026-10-08 13:00:00', True),
    ('2026-10-08 14:59:59', True),
    ('2026-10-08 15:00:00', False),   # 盘后
    ('2026-10-08 20:00:00', False),
    ('2026-10-04 10:00:00', False),   # 周日
    ('2026-10-10 10:00:00', False),   # 周六
    ('2026-10-06 10:00:00', False),   # 国庆工作日但休市
    ('2026-10-12 09:30:00', True),    # 周一开市
])
def test_market_active_boundaries(ts: str, expected: bool) -> None:
    assert ms.isAShareMarketActive(datetime.fromisoformat(ts)) is expected


def test_close_grace_extends_each_session_end() -> None:
    assert ms.isAShareMarketActive(datetime(2026, 10, 8, 15, 4, 59), closeGraceSec=300) is True
    assert ms.isAShareMarketActive(datetime(2026, 10, 8, 15, 5, 0), closeGraceSec=300) is False
    assert ms.isAShareMarketActive(datetime(2026, 10, 8, 11, 34, 0), closeGraceSec=300) is True
    assert ms.isAShareMarketActive(datetime(2026, 10, 6, 15, 1, 0), closeGraceSec=300) is False


def test_trade_cal_cached_per_year(fakeTradeCal) -> None:
    for _ in range(5):
        ms.isTradeDay(date(2026, 10, 8))
    assert fakeTradeCal == [2026]


def test_missing_trade_cal_falls_back_to_weekday() -> None:
    # 2027 无数据（loader 返回 None）：工作日视为交易日，周末不是
    assert ms.isTradeDay(date(2027, 3, 1)) is True    # 周一
    assert ms.isTradeDay(date(2027, 3, 6)) is False   # 周六

# 代码更新时间：2026-10-04 17:42 交易时段判定边界测试
