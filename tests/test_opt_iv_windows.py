#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opt_iv_windows 三窗口分位（Per 1y / Per C / Per A）纯函数单测。"""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rt_monitor.core.config import OPT_IV_PERC_ANCHORS  # noqa: E402
from rt_monitor.services.opt_iv_windows import (  # noqa: E402
    MIN_HISTORY,
    PERC_OVERLAP_HINT_N,
    WINDOW_1Y,
    anchorFor,
    buildPerWindows,
    emptyWindows,
    parseAnchor,
    percentileSorted,
    perTitle,
)


def _series(n: int, *, start: date = date(2020, 1, 1)) -> tuple[list[date], list[float]]:
    """构造 n 个递增日期与 1.0, 2.0, ... n.0 的 IV 序列（便于手算分位）。"""
    dates = [start + timedelta(days=i) for i in range(n)]
    vals = [float(i + 1) for i in range(n)]
    return dates, vals


def test_percentile_mid_rank_definition():
    vals = [1.0, 2.0, 3.0, 4.0]
    # 小于 2 的有 1 个，等于 2 的有 1 个 → (1 + 0.5) / 4 = 37.5
    assert percentileSorted(vals, 2.0) == pytest.approx(37.5)
    assert percentileSorted(vals, 0.5) == pytest.approx(0.0)
    assert percentileSorted(vals, 9.0) == pytest.approx(100.0)


def test_percentile_handles_ties_and_bad_input():
    assert percentileSorted([2.0, 2.0, 2.0, 2.0], 2.0) == pytest.approx(50.0)
    assert percentileSorted(None, 1.0) is None
    assert percentileSorted([], 1.0) is None
    assert percentileSorted([1.0], None) is None
    assert percentileSorted([1.0], float('nan')) is None
    # 代表 IV 必为正：非正当前值不定位
    assert percentileSorted([1.0], 0.0) is None
    assert percentileSorted([1.0], -1.0) is None


def test_bisect_matches_naive_scan():
    """预排序 + bisect 必须与朴素全扫给出同一个数，否则优化就是改口径。"""
    raw = [0.11, 0.25, 0.25, 0.4, 0.07, 0.33, 0.19, 0.25]
    sortedVals = sorted(raw)
    for x in (0.05, 0.07, 0.19, 0.25, 0.33, 0.4, 0.9):
        less = sum(1 for v in raw if v < x)
        equal = sum(1 for v in raw if v == x)
        naive = (less + 0.5 * equal) / len(raw) * 100.0
        assert percentileSorted(sortedVals, x) == pytest.approx(naive)


def test_window_1y_uses_last_252_only():
    dates, vals = _series(400)
    w = buildPerWindows(dates, vals, anchor=None, anchorConfigured=False)
    assert w.n1y == WINDOW_1Y
    assert w.na == 400
    got = w.evaluate(200.0)
    # 全样本里 200 排在中间偏下；近 252 日窗口只含 149.0~400.0，200 更靠低端
    assert got['pera'] == pytest.approx((199 + 0.5) / 400 * 100)
    assert got['per1y'] == pytest.approx((51 + 0.5) / 252 * 100)
    assert got['pera'] > got['per1y']


def test_short_history_yields_none_not_fake_percentile():
    dates, vals = _series(MIN_HISTORY - 1)
    w = buildPerWindows(dates, vals, anchor=None, anchorConfigured=False)
    got = w.evaluate(10.0)
    assert got['per1y'] is None
    assert got['pera'] is None
    assert w.n1y == 0 and w.na == 0
    # 刚好够就出数
    dates, vals = _series(MIN_HISTORY)
    w = buildPerWindows(dates, vals, anchor=None, anchorConfigured=False)
    assert w.evaluate(10.0)['pera'] is not None


def test_perc_window_starts_at_anchor_inclusive():
    dates, vals = _series(400, start=date(2024, 1, 1))
    anchor = dates[100]
    w = buildPerWindows(dates, vals, anchor=anchor, anchorConfigured=True)
    # 锚点当日计入
    assert w.nc == 300
    got = w.evaluate(vals[150])
    less = sum(1 for d, v in zip(dates, vals) if d >= anchor and v < vals[150])
    assert got['perc'] == pytest.approx((less + 0.5) / 300 * 100)


def test_perc_none_when_anchor_not_configured():
    dates, vals = _series(400)
    w = buildPerWindows(dates, vals, anchor=None, anchorConfigured=False)
    assert w.evaluate(200.0)['perc'] is None
    assert w.nc == 0
    assert '未配置锚点' in perTitle('perc', w)


def test_perc_none_when_anchor_too_recent():
    dates, vals = _series(400, start=date(2024, 1, 1))
    w = buildPerWindows(dates, vals, anchor=dates[-10], anchorConfigured=True)
    assert w.evaluate(200.0)['perc'] is None
    assert '样本不足' in perTitle('perc', w)


def test_drops_nonpositive_and_nan_samples():
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(5)]
    vals = [0.2, 0.0, -0.1, float('nan'), 0.3]
    w = buildPerWindows(dates, vals, anchor=None, anchorConfigured=False)
    # 仅 2 个有效样本，远不足 MIN_HISTORY
    assert w.na == 0
    assert w.evaluate(0.25)['pera'] is None


def test_unsorted_input_is_normalized():
    """调用方给乱序也要正确切窗口，否则 Per C 会漏样本。"""
    dates, vals = _series(200, start=date(2024, 1, 1))
    pairs = list(zip(dates, vals))
    shuffled = pairs[100:] + pairs[:100]
    anchor = dates[50]
    wOrdered = buildPerWindows(dates, vals, anchor=anchor, anchorConfigured=True)
    wShuffled = buildPerWindows(
        [d for d, _ in shuffled], [v for _, v in shuffled],
        anchor=anchor, anchorConfigured=True,
    )
    assert wOrdered.nc == wShuffled.nc
    assert wOrdered.n1y == wShuffled.n1y
    assert wOrdered.evaluate(120.0) == wShuffled.evaluate(120.0)


def test_parse_anchor_formats():
    assert parseAnchor('2024-09-24') == date(2024, 9, 24)
    assert parseAnchor(date(2024, 9, 24)) == date(2024, 9, 24)
    assert parseAnchor('') is None
    assert parseAnchor(None) is None
    assert parseAnchor('not-a-date') is None


def test_default_anchor_table():
    from rt_monitor.core.config import OPT_IV_PERC_ANCHORS_DEFAULT

    assert parseAnchor(OPT_IV_PERC_ANCHORS_DEFAULT['588000.SH']) == date(2024, 9, 24)
    assert parseAnchor(OPT_IV_PERC_ANCHORS_DEFAULT['159915.SZ']) == date(2024, 9, 24)
    assert parseAnchor(OPT_IV_PERC_ANCHORS_DEFAULT['510500.SH']) == date(2024, 9, 24)
    assert parseAnchor(OPT_IV_PERC_ANCHORS_DEFAULT['000852.SH']) == date(2024, 9, 24)
    # 蓝筹留空：Per C 不出数
    assert parseAnchor(OPT_IV_PERC_ANCHORS_DEFAULT['510050.SH']) is None
    assert parseAnchor(OPT_IV_PERC_ANCHORS_DEFAULT['510300.SH']) is None
    assert parseAnchor(OPT_IV_PERC_ANCHORS_DEFAULT['000300.SH']) is None
    assert parseAnchor(OPT_IV_PERC_ANCHORS_DEFAULT['000016.SH']) is None


def test_anchor_for_reads_live_dict(monkeypatch):
    monkeypatch.setitem(OPT_IV_PERC_ANCHORS, '510050.SH', '2024-10-01')
    assert anchorFor('510050.SH') == date(2024, 10, 1)
    monkeypatch.setitem(OPT_IV_PERC_ANCHORS, '510050.SH', '')
    assert anchorFor('510050.SH') is None
    assert anchorFor('不存在.SH') is None


def test_empty_windows_placeholder():
    w = emptyWindows()
    assert w.isEmpty
    got = w.evaluate(0.3)
    assert got == {'per1y': None, 'perc': None, 'pera': None}


def test_titles_carry_window_anchor_and_sample():
    dates, vals = _series(400, start=date(2024, 1, 1))
    w = buildPerWindows(dates, vals, anchor=dates[0], anchorConfigured=True)
    assert f'n={WINDOW_1Y}' in perTitle('per1y', w)
    assert 'n=400' in perTitle('pera', w)
    percTitle = perTitle('perc', w)
    assert '2024-01-01' in percTitle and 'n=400' in percTitle
    # 样本充足时不再提示重叠
    assert '高度重叠' not in percTitle
    wNarrow = buildPerWindows(
        dates, vals, anchor=dates[-(PERC_OVERLAP_HINT_N - 1)], anchorConfigured=True
    )
    assert '高度重叠' in perTitle('perc', wNarrow)
    assert perTitle('unknown', w) == ''


def test_title_prefix_for_side_labels():
    dates, vals = _series(300, start=date(2024, 1, 1))
    w = buildPerWindows(dates, vals, anchor=None, anchorConfigured=False)
    assert perTitle('pera', w, prefix='认购').startswith('认购：')


# 代码更新时间：2026-09-21 06:31
