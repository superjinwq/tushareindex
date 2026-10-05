# coding: utf-8
"""涨跌幅表全历史分位：严格小于，四舍五入到整数。"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pymysql

from rt_monitor.core.config import DB_CONFIG_TUSHARE
from rt_monitor.monolith import (
    _idx_gap_percentile,
    _load_etf_metric_history,
    _load_idx_gap_history,
    _load_idx_metric_history,
)


def testIdxGapPercentileBounds():
    assert _idx_gap_percentile([1.0, 2.0, 3.0], 2.0) == 33
    assert _idx_gap_percentile([1.0, 1.0, 1.0], 1.0) == 0
    assert _idx_gap_percentile([-1.0, 0.0, 1.0], 2.0) == 100
    assert _idx_gap_percentile([], 1.0) is None
    assert _idx_gap_percentile([1.0], None) is None
    sample = [0.1] * 10 + [0.2]
    assert _idx_gap_percentile(sample, 0.2) == 91


def _assertSampleRank(sample: tuple[float, ...]) -> None:
    assert len(sample) > 1000
    gap = sample[len(sample) // 2]
    lt = sum(1 for v in sample if v < gap)
    expected = int(
        (Decimal(lt) / Decimal(len(sample)) * Decimal(100)).quantize(
            Decimal('1'), rounding=ROUND_HALF_UP
        )
    )
    assert _idx_gap_percentile(sample, gap) == expected


def testIdxGapHistoryRankMatchesIndependentCount():
    conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    try:
        hist = _load_idx_gap_history(conn, ['000300.SH']).get('000300.SH') or ()
        _assertSampleRank(hist)
    finally:
        conn.close()


def testEtfMetricHistoryFiveSeries():
    conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    try:
        packed = _load_etf_metric_history(conn, ['510300.SH']).get('510300.SH') or {}
        for key in ('amt', 'pct', 'intra', 'gap', 'amp'):
            _assertSampleRank(packed.get(key) or ())
        amt = packed.get('amt') or ()
        mid = sorted(amt)[len(amt) // 2]
        assert 1 < mid < 500
    finally:
        conn.close()


def testIdxMetricHistoryFourSeries():
    conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    try:
        packed = _load_idx_metric_history(conn, ['000300.SH']).get('000300.SH') or {}
        for key in ('pct', 'intra', 'gap', 'amp'):
            _assertSampleRank(packed.get(key) or ())
    finally:
        conn.close()
