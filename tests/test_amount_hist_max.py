# coding: utf-8
"""成交额表全历史 MAX：换算口径，且不低于近 60 个交易日峰值。"""
from __future__ import annotations

from datetime import date

import pymysql

from rt_monitor.core.config import DB_CONFIG_TUSHARE
from rt_monitor.monolith import (
    AMOUNT_CATEGORY_TO_INDEX_TS,
    _amount_extreme_to_yi_date,
    _batch_index_daily_max_amount_all,
    _index_daily_max_amount_all,
    _index_daily_max_amount_last_n_trade_dates,
    _marketinfo_max_amount_all,
    _marketinfo_max_amount_last_n_trade_dates,
)


def testAmountExtremeToYiDateUnits():
    assert _amount_extreme_to_yi_date(date(2026, 6, 22), 1145900296.758, 1e5) == (11459, "2026-06-22")
    assert _amount_extreme_to_yi_date(date(2026, 1, 14), 39869.0, 1.0) == (39869, "2026-01-14")
    assert _amount_extreme_to_yi_date(None, 1.0, 1.0) == (None, None)
    assert _amount_extreme_to_yi_date(date(2026, 1, 1), None, 1.0) == (None, None)


def testHistMaxCoversT60AndMatchesExtremeRow():
    """全历史峰值日期与金额必须等于 ORDER BY amount DESC, trade_date DESC 的首行，且不低于 T-60。"""
    conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    try:
        codes = sorted(set(AMOUNT_CATEGORY_TO_INDEX_TS.values()))
        batch = _batch_index_daily_max_amount_all(conn, codes)
        assert set(batch) == set(codes)
        for ts in codes:
            yiAll, dsAll = batch[ts]
            assert (yiAll, dsAll) == _index_daily_max_amount_all(conn, ts)
            yi60, _ds60 = _index_daily_max_amount_last_n_trade_dates(conn, ts, 60)
            assert yiAll is not None and dsAll
            if yi60 is not None:
                assert yiAll >= yi60
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT trade_date, amount
                    FROM stock_tushare.index_daily
                    WHERE ts_code = %s AND amount IS NOT NULL
                    ORDER BY amount DESC, trade_date DESC
                    LIMIT 1
                    """,
                    (ts,),
                )
                row = cur.fetchone()
            assert (yiAll, dsAll) == _amount_extreme_to_yi_date(row[0], row[1], 1e5)
        yiM, dsM = _marketinfo_max_amount_all(conn)
        yiM60, _dsM60 = _marketinfo_max_amount_last_n_trade_dates(conn, 60)
        assert yiM is not None and dsM
        if yiM60 is not None:
            assert yiM >= yiM60
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date, amount
                FROM stock_tushare.marketinfo
                WHERE amount IS NOT NULL
                ORDER BY amount DESC, trade_date DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
        assert (yiM, dsM) == _amount_extreme_to_yi_date(row[0], row[1], 1.0)
    finally:
        conn.close()
