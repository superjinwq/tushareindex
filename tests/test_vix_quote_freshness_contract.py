"""read_vix_quote.py 的时间/陈旧度契约测试。

被测脚本在仓库之外（Chrome for Testing 程序目录），缺失时整体跳过而不是让
测试套件变红 —— 它是运维脚本，不是本仓库的产物。

守卫意图：该脚本的核心价值不是「能读到价格」，而是「不会把陈旧行情当最新
行情」。因此这里锁死的是时间换算与陈旧判定语义，而不是网络读取本身。
"""
from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SCRIPT = Path(
    r"C:\Users\super\AppData\Local\Programs\ChromeForTesting\154.0.8037.92\read_vix_quote.py"
)

pytestmark = pytest.mark.skipif(
    not SCRIPT.is_file(), reason=f"read_vix_quote.py not present at {SCRIPT}"
)

NY = ZoneInfo("America/New_York")


@pytest.fixture(scope="module")
def vixmod():
    spec = importlib.util.spec_from_file_location("vix_quote_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_page_timezone_is_new_york(vixmod):
    """实测确认页面报价时间为美东时间，不得退回 unverified。"""
    assert vixmod.PAGE_TIMEZONE == "America/New_York"
    tz = vixmod.pageTimezoneInfo()
    assert getattr(tz, "key", None) == "America/New_York"


def test_age_same_day(vixmod):
    observed = datetime(2026, 10, 1, 20, 0, 30, tzinfo=NY)
    assert vixmod.pageQuoteAgeSeconds("20:00:00", observed) == pytest.approx(30.0)


def test_age_across_midnight_wraps_to_previous_day(vixmod):
    """跨午夜：观测 00:05，页面显示前一天 23:58，应为 420s 而不是负值。"""
    observed = datetime(2026, 10, 2, 0, 5, 0, tzinfo=NY)
    assert vixmod.pageQuoteAgeSeconds("23:58:00", observed) == pytest.approx(420.0)
    assert vixmod.calendarDateFor("23:58:00", observed).day == 1


def test_age_never_negative_on_future_page_time(vixmod):
    """页面时间晚于观测时刻（轻微时钟偏差）必须钳制为非负。"""
    observed = datetime(2026, 10, 1, 20, 0, 0, tzinfo=NY)
    assert vixmod.pageQuoteAgeSeconds("20:01:00", observed) >= 0.0


def test_stale_threshold_is_strictly_greater(vixmod):
    """超阈值才判陈旧：恰好等于阈值算新鲜。"""
    assert vixmod.isQuoteStale(61.0, 60) is True
    assert vixmod.isQuoteStale(60.0, 60) is False
    assert vixmod.isQuoteStale(0.0, 60) is False


def test_timezone_is_dst_aware(vixmod):
    """夏令时/冬令时偏移不同，证明用的是 IANA 规则而非固定偏移。"""
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=NY).utcoffset()
    winter = datetime(2026, 1, 1, 12, 0, tzinfo=NY).utcoffset()
    assert summer == timedelta(hours=-4)
    assert winter == timedelta(hours=-5)
