#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opt_iv_board 中轴 HV / 溢 / 随后 RV 纯函数单测。"""
import math
import os
import sys
from datetime import date

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rt_monitor.services import opt_iv_windows as ivWindows  # noqa: E402
from rt_monitor.services.opt_iv_board import (  # noqa: E402
    alignedHvWindowN,
    attachAxisSkew,
    axisSkewPp,
    isHvMuted,
    logReturnsFromCloses,
    midIvRaw,
    overlayLiveIvOnStats,
    rollingAnnHv,
    subsequentAnnRv,
    summarizeVrp,
    _buildAxisStats,
    _fmtDeltaPp,
    _PER_CELL_KEYS,
    _perCells,
    _skewCls,
    _stripIvHistVals,
)


def test_aligned_window_clips_and_maps():
    assert alignedHvWindowN(5) == 5
    assert alignedHvWindowN(33) == 23
    assert alignedHvWindowN(124) == 86
    assert alignedHvWindowN(215) == 120
    assert alignedHvWindowN(None) is None
    assert alignedHvWindowN(0) is None


def test_hv_muted_only_short_front_month():
    assert isHvMuted('current_month', 5) is True
    assert isHvMuted('current_month', 9.9) is True
    assert isHvMuted('current_month', 10) is False
    assert isHvMuted('next_month', 5) is False


def test_mid_iv_and_delta_fmt():
    assert midIvRaw(0.15, 0.17) == 0.16
    assert midIvRaw(0.15, None) == 0.15
    assert midIvRaw(None, None) is None
    assert _fmtDeltaPp(2.14) == '+2.1'
    assert _fmtDeltaPp(-17.37).startswith('-17.4')


def test_axis_skew_put_minus_call():
    assert abs(axisSkewPp(0.15, 0.17) - 2.0) < 1e-12
    assert abs(axisSkewPp(0.20, 0.18) - (-2.0)) < 1e-12
    assert axisSkewPp(0.15, None) is None
    assert _skewCls(1.2) == 'opt-iv-skew--put'
    assert _skewCls(2.5) == 'opt-iv-skew--fat'
    assert _skewCls(-0.8) == 'opt-iv-skew--call'
    assert _skewCls(0.2) == ''
    axis = attachAxisSkew({'hv': '—'}, 0.20, 0.215)
    assert axis['skew'] == '+1.5'
    assert axis['skew_cls'] == 'opt-iv-skew--put'


def test_rolling_hv_matches_sample_std():
    closes = np.array([100.0, 101.0, 99.0, 100.5, 102.0, 101.0], dtype=float)
    logret = logReturnsFromCloses(closes)
    hv = rollingAnnHv(logret, 3)
    sl = logret[2:5]
    expect = float(np.std(sl, ddof=1) * math.sqrt(252.0))
    assert math.isfinite(hv[4])
    assert abs(hv[4] - expect) < 1e-12
    assert not math.isfinite(hv[1])


def test_subsequent_rv_is_forward_only():
    closes = np.exp(np.cumsum(np.array([0.0, 0.01, -0.02, 0.015, 0.01, -0.005, 0.02])))
    logret = logReturnsFromCloses(closes)
    rv = subsequentAnnRv(logret, 1, 3)
    expect = float(np.std(logret[2:5], ddof=1) * math.sqrt(252.0))
    assert abs(rv - expect) < 1e-12
    assert subsequentAnnRv(logret, 5, 3) is None


def test_vrp_summary_and_axis_mute():
    ivs = np.array([0.20, 0.22, 0.18, 0.25, 0.21, 0.19, 0.23, 0.24], dtype=float)
    rvs = np.array([0.16, 0.30, 0.15, 0.17, 0.20, 0.18, 0.14, 0.40], dtype=float)
    stats = summarizeVrp(ivs, rvs)
    assert stats['n'] == 8
    assert stats['median'] is not None
    assert 0.0 < stats['win_rate'] < 1.0

    dates = [date(2026, 6, 1) + __import__('datetime').timedelta(days=i) for i in range(40)]
    rng = np.random.default_rng(1)
    closes = 10.0 * np.exp(np.cumsum(rng.normal(0.0, 0.012, size=40)))
    logret = logReturnsFromCloses(closes)
    axis = _buildAxisStats(dates, logret, dates[-1], 5, 0.20, 'current_month')
    assert axis['muted'] is True
    assert 'opt-iv-hv--muted' in axis['muted_cls']
    assert axis['n'] == 5
    assert axis['hv'] != '—'
    axis2 = _buildAxisStats(dates, logret, dates[-1], 33, 0.20, 'next_month')
    assert axis2['muted'] is False
    assert axis2['n'] == 23
    assert axis2['hv'] != '—'


def _perWindowsForTest(n: int = 300, anchorIdx: int | None = None):
    """构造递增历史：分位与下标线性对应，便于断言方向而非只断言「有值」。"""
    import datetime as dt

    dates = [date(2025, 1, 1) + dt.timedelta(days=i) for i in range(n)]
    vals = [0.10 + i * 0.0005 for i in range(n)]
    anchor = dates[anchorIdx] if anchorIdx is not None else None
    return dates, vals, ivWindows.buildPerWindows(
        dates, vals, anchor=anchor, anchorConfigured=anchor is not None
    )


def test_per_cells_keys_match_shared_window_keys():
    """展示键名必须与共享模块窗口键、T 板字段名同名；此前 Per A 叫 'per' 导致跨页错引。"""
    assert set(_PER_CELL_KEYS) == set(ivWindows.PerWindows().evaluate(0.2).keys())
    _, vals, windows = _perWindowsForTest()
    cells = _perCells(windows, vals[-1])
    for key in _PER_CELL_KEYS:
        assert key in cells and f'{key}_cls' in cells and f'{key}_title' in cells


def test_per_cells_display_scale_is_one_decimal_percent():
    """展示口径：0~100 一位小数（与 T 板 _opt_t_per_cells 逐字符一致）。

    共享模块给 0~100，而本页 _fmtPer 期望 0~1，中间必须折算一次；漏掉折算会让
    所有列恒为 100.0，多除一次则恒为 0.x —— 两种都不会抛错，只会静默给错数。
    """
    _, vals, windows = _perWindowsForTest()
    assert _perCells(windows, vals[-1] + 1.0)['pera'] == '100.0'
    assert _perCells(windows, 0.0001)['pera'] == '0.0'
    mid = _perCells(windows, vals[len(vals) // 2])
    assert 30.0 < float(mid['pera']) < 70.0
    assert mid['pera'].count('.') == 1 and len(mid['pera'].split('.')[1]) == 1


def test_overlay_live_iv_recomputes_all_three_per_columns():
    """盘中覆盖 IV 后三列必须同步重算；只改 current 会让分位挂在昨收上。"""
    ivWindows.clearCache()
    dates, vals, windows = _perWindowsForTest(anchorIdx=200)
    key = ivWindows.cacheKey('588000.SH', 'current_month', 'C', dates[-1])
    ivWindows.cachedPerWindows(key, lambda: windows)

    lowIv = vals[10]
    stats = {
        '_hist_vals': list(vals),
        '_per_key': key,
        **_perCells(windows, lowIv),
        'current': '—',
    }
    before = {k: stats[k] for k in _PER_CELL_KEYS}

    overlayLiveIvOnStats(stats, vals[-5], '实时 10:30')

    after = {k: stats[k] for k in _PER_CELL_KEYS}
    assert after != before
    for k in _PER_CELL_KEYS:
        assert float(after[k]) > float(before[k]), f'{k} 未随实时 IV 上移'
    assert stats['current_raw'] == vals[-5]
    assert stats['asof'] == '实时 10:30'


def test_overlay_live_iv_without_cached_windows_keeps_daily_per():
    """缓存未命中时保留日频分位，且不得把空窗口写进缓存（否则该键三列永久变 —）。"""
    ivWindows.clearCache()
    dates, vals, windows = _perWindowsForTest()
    daily = _perCells(windows, vals[100])
    missKey = ivWindows.cacheKey('588000.SH', 'current_month', 'C', dates[-1])
    stats = {'_hist_vals': list(vals), '_per_key': missKey, **daily}

    overlayLiveIvOnStats(stats, vals[-1], '实时 10:30')

    for k in _PER_CELL_KEYS:
        assert stats[k] == daily[k]
    assert ivWindows.cacheSize() == 0
    # 缓存随后被正常路径填好，同一键即可恢复重算能力
    ivWindows.cachedPerWindows(missKey, lambda: windows)
    overlayLiveIvOnStats(stats, vals[-1], '实时 10:31')
    assert stats['pera'] == '99.8'


def test_strip_iv_hist_vals_drops_internal_per_key():
    """内部键不得随 payload 出站（JSON 里会变成裸 list，前端也可能误用）。"""
    _, vals, windows = _perWindowsForTest()
    blocks = [{
        'tenors': [{
            'call': {'_hist_vals': list(vals), '_per_key': ('a', 'b', 'c', 'd', 'e'), 'pera': '50.0'},
            'put': {'_hist_vals': list(vals), '_per_key': ('a', 'b', 'c', 'd', 'e'), 'pera': '50.0'},
        }],
    }]
    _stripIvHistVals(blocks)
    for side in ('call', 'put'):
        stats = blocks[0]['tenors'][0][side]
        assert '_hist_vals' not in stats
        assert '_per_key' not in stats
        assert stats['pera'] == '50.0'


def testIvBoardHeaderUsesYiNotOptionDelta() -> None:
    """IV 看板中轴列展示名是「溢」；T 表期权 Δ 与读盘扫表 Δ 仍保留。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    iv = (root / 'templates' / 'rt_monitor_opt_iv_board.html').read_text(encoding='utf-8')
    guide = (root / 'templates' / 'rt_monitor_opt_vol_guide.html').read_text(
        encoding='utf-8'
    )
    tBody = (root / 'templates' / 'rt_monitor_opt_t_board_body.html').read_text(
        encoding='utf-8'
    )
    header = iv.split('class="opt-iv-th-delta"', 1)[1].split('</th>', 1)[0]
    assert '>溢' in header
    assert 'Δ' not in header
    assert '中轴IV − HV' in header
    assert '<strong>溢</strong>：中轴 IV − HV' in guide
    assert '<strong>Δ</strong>：中轴 IV − HV' not in guide
    assert '<strong>Δ</strong>：Black-76 现货 Δ' in guide
    assert 'Black-76 现货Δ' in tBody
    assert 'title="Black-76 现货Δ；Call为正' in tBody


# 代码更新时间：2026-09-20 21:26 三列分位展示口径、盘中覆盖重算与内部键剥离回归
# 代码更新时间：2026-09-23 06:54 IV看板表头改「溢」、与T表期权Δ分名回归
