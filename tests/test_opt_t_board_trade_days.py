# coding: utf-8
"""剩余自然日[.交易日] 展示与 trade_cal 计数。"""
from datetime import date

from rt_monitor.services.opt_t_board import (
    _opt_t_board_build_obligation_selector,
    _opt_t_board_build_rights_selector,
    _opt_t_board_days_to_expiry_label,
    _opt_t_board_selector_dte_text,
    _opt_t_board_trade_days_to_expiry,
)


def test_dte_pair_text_formats_cal_and_trade() -> None:
    assert _opt_t_board_selector_dte_text(36, 25) == '36天.25'
    assert _opt_t_board_selector_dte_text(36, None) == '36天'
    assert _opt_t_board_selector_dte_text(0, 0) == '今日到期'
    assert _opt_t_board_days_to_expiry_label(36, 25) == '剩36天.25'
    assert _opt_t_board_days_to_expiry_label(36, None) == '剩36天'
    assert _opt_t_board_days_to_expiry_label(0, 0) == '今日到期'


def test_trade_days_count_uses_open_dates_between_asof_and_expiry() -> None:
    openDates = {
        date(2026, 9, 22),  # asof 当日不计
        date(2026, 9, 23),
        date(2026, 9, 24),
        date(2026, 9, 25),
        date(2026, 9, 28),  # 周末后
        date(2026, 10, 28),  # 到期日计入
    }
    n = _opt_t_board_trade_days_to_expiry(
        date(2026, 10, 28),
        asof=date(2026, 9, 22),
        openDates=openDates,
    )
    assert n == 5


def test_trade_days_none_when_calendar_truncated_before_maturity() -> None:
    """库只铺到年底、到期在次年 → 不得返回截断低估值。"""
    openDates = {
        date(2026, 9, 23),
        date(2026, 12, 23),
        date(2026, 12, 31),  # 库末日
    }
    assert (
        _opt_t_board_trade_days_to_expiry(
            date(2027, 3, 24),
            asof=date(2026, 9, 22),
            openDates=openDates,
        )
        is None
    )
    # 覆盖到到期日则正常计数
    openDates.add(date(2027, 3, 24))
    assert (
        _opt_t_board_trade_days_to_expiry(
            date(2027, 3, 24),
            asof=date(2026, 9, 22),
            openDates=openDates,
        )
        == 4
    )

def test_selector_heads_append_trade_days_when_present() -> None:
    sections = [
        {
            'expiry_kind': kind,
            'selector_days_to_expiry': days,
            'selector_trade_days_to_expiry': trade,
            'rows': [],
        }
        for kind, days, trade in (
            ('near', 6, 4),
            ('next', 41, 28),
            ('quarter', 97, 66),
            ('next_quarter', 189, 128),
        )
    ]
    obligation = _opt_t_board_build_obligation_selector(sections)
    assert [c['head_expiry'] for c in obligation['columns']] == [
        '当月（6天.4）',
        '下月（41天.28）',
        '当季（97天.66）',
        '下季（189天.128）',
    ]
    rights = _opt_t_board_build_rights_selector(sections, {})
    assert [c['head_expiry'] for c in rights['columns']] == [
        '当月（6天.4）',
        '下月（41天.28）',
        '当季（97天.66）',
        '下季（189天.128）',
    ]
