# coding: utf-8
"""SMA120 状态灯纯函数单测。"""
from datetime import date

from rt_monitor.services.opt_t_board import buildTrendSma120


def test_build_trend_pass_and_gap():
    closes = [1.0] * 119 + [2.0]
    out = buildTrendSma120(closes, spot=2.0, asof=date(2026, 9, 7), source='etf_daily')
    assert out['ok'] is True
    assert out['pass'] is True
    assert out['label'] == 'SMA120'
    assert out['gap_text'].startswith('+')
    assert out['gap_text'].endswith('%')
    # 一位整数部分 + 两位小数，如 +66.67%
    assert len(out['gap_text'].split('.')[-1].rstrip('%')) == 2
    assert '动量闸' in out['title']


def test_build_trend_fail():
    closes = [2.0] * 120
    out = buildTrendSma120(closes, spot=1.5, asof=date(2026, 9, 7))
    assert out['ok'] is True
    assert out['pass'] is False
    assert out['label'] == 'SMA120'
    assert out['gap_pct'] < 0


def test_build_trend_insufficient():
    out = buildTrendSma120([1.0] * 50, spot=1.0)
    assert out['ok'] is False
    assert out['label'] == '—'
    assert out['pass'] is None


def test_build_trend_spot_fallback_to_last_close():
    closes = [1.0] * 119 + [1.2]
    out = buildTrendSma120(closes, spot=None)
    assert out['ok'] is True
    assert out['spot'] == 1.2
