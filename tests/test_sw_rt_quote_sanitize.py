# coding: utf-8
"""申万 rt_sw_k 盘前脏数据（close=0、pct_change=-100）不得写入 T 列。"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pandas as pd

from rt_monitor.services.sw_industry import (
    _sw_apply_live_row_market_fields,
    _sw_build_rt_sw_quote,
    _sw_fetch_live_market_maps,
)
from rt_sw_k import dropUnusableRtSwKRows


class SwBuildRtSwQuoteTest(unittest.TestCase):
    def testRejectsZeroCloseAndMinus100Pct(self):
        q = _sw_build_rt_sw_quote(
            0.0, datetime(2026, 9, 2, 8, 43), -100.0, 0.0
        )
        self.assertIsNone(q)

    def testRejectsNoneClose(self):
        q = _sw_build_rt_sw_quote(None, datetime(2026, 9, 2, 9, 30), 1.2, 1e8)
        self.assertIsNone(q)

    def testKeepsValidQuote(self):
        q = _sw_build_rt_sw_quote(
            2659.05, datetime(2026, 9, 2, 9, 31), 2.48, 3.7e10
        )
        self.assertIsNotNone(q)
        self.assertEqual(q['etf_last'], 2659.05)
        self.assertEqual(q['pct_change'], 2.48)
        self.assertEqual(q['etf_rt_date'], date(2026, 9, 2))

    def testDropsAbsurdPctButKeepsValidClose(self):
        q = _sw_build_rt_sw_quote(
            2659.05, datetime(2026, 9, 2, 9, 31), -100.0, 3.7e10
        )
        self.assertIsNotNone(q)
        self.assertEqual(q['etf_last'], 2659.05)
        self.assertIsNone(q['pct_change'])

    def testDerivesPctFromCloseAndPreCloseWhenSourceIsMissing(self):
        q = _sw_build_rt_sw_quote(
            2659.05,
            datetime(2026, 9, 2, 9, 31),
            None,
            3.7e10,
            preCloseRaw=2600.0,
        )
        self.assertIsNotNone(q)
        self.assertEqual(q['pct_change'], 2.2712)

    def testDoesNotDerivePctWithoutUsablePreClose(self):
        q = _sw_build_rt_sw_quote(
            2659.05,
            datetime(2026, 9, 2, 9, 31),
            None,
            3.7e10,
            preCloseRaw=0.0,
        )
        self.assertIsNotNone(q)
        self.assertIsNone(q['pct_change'])


class SwApplyLiveRowIgnoresJunkPctTest(unittest.TestCase):
    def testNoneLastDoesNotUseMinus100Pct(self):
        row = {'l1_code': '801010.SI'}
        _sw_apply_live_row_market_fields(
            row,
            rt_sw_quotes={
                '801010.SI': {
                    'etf_last': None,
                    'etf_rt_date': date(2026, 9, 2),
                    'pct_change': -100.0,
                    'amount_yuan': None,
                }
            },
            range_cache_sw={},
            amt_pctile_cache_sw={},
            sw_t0_count_maps={},
            sw_hlc_count_maps={},
        )
        self.assertIsNone(row['sw_pct'])

    def testValidPctWritesTCol(self):
        row = {'l1_code': '801010.SI'}
        _sw_apply_live_row_market_fields(
            row,
            rt_sw_quotes={
                '801010.SI': {
                    'etf_last': 2659.05,
                    'etf_rt_date': date(2026, 9, 2),
                    'pct_change': 2.48,
                    'amount_yuan': 1.0,
                }
            },
            range_cache_sw={},
            amt_pctile_cache_sw={},
            sw_t0_count_maps={},
            sw_hlc_count_maps={},
        )
        self.assertEqual(row['sw_pct'], 2.48)

    def testDerivedPctWritesTColDuringLightweightRefresh(self):
        row = {'l1_code': '801010.SI'}
        quote = _sw_build_rt_sw_quote(
            2609.963,
            datetime(2026, 9, 15, 10, 26, 51),
            None,
            1.0,
            preCloseRaw=2612.410,
        )
        self.assertIsNotNone(quote)
        _sw_apply_live_row_market_fields(
            row,
            rt_sw_quotes={'801010.SI': quote},
            range_cache_sw={},
            amt_pctile_cache_sw={},
            sw_t0_count_maps={},
            sw_hlc_count_maps={},
        )
        self.assertEqual(row['sw_pct'], -0.09)


class SwLiveMapsDeferHlcTest(unittest.TestCase):
    def testWaitLimitScanFalseDoesNotScan(self):
        from rt_monitor.services import sw_industry as swMod

        swMod._swLiveMarketMapsCache.clear()
        tsToInd = {'600000.SH': ('801010.SI', '农林牧渔')}
        with patch(
            'rt_monitor.services.sw_industry.pymysql.connect'
        ), patch(
            'rt_monitor.services.sw_industry._fetch_rt_sw_k_quotes',
            return_value={},
        ), patch(
            'rt_monitor.services.sw_industry._load_index_sw_daily_panel_bulk',
            return_value=({}, {}),
        ) as panelFn, patch(
            'rt_monitor.services.sw_industry._read_sw_amount_ratio_percentiles'
        ) as amtFn, patch(
            'rt_monitor.services.sw_industry._sw_peek_limit_count_flagged',
            return_value=None,
        ) as peek, patch(
            'rt_monitor.services.sw_industry._sw_fetch_limit_count_maps_cached'
        ) as scan:
            _sw_fetch_live_market_maps(
                tsToInd, None, None, waitLimitScan=False
            )
            peek.assert_called()
            scan.assert_not_called()
            amtFn.assert_not_called()
            minD = panelFn.call_args[0][2]
            self.assertGreaterEqual(
                minD, date.today() - timedelta(days=230)
            )


class RtSwKWriterDropUnusableTest(unittest.TestCase):
    def testDropsZeroCloseKeepsValid(self):
        df = pd.DataFrame(
            [
                {
                    'ts_code': '801010.SI',
                    'close': 0.0,
                    'pct_change': -100.0,
                },
                {
                    'ts_code': '801030.SI',
                    'close': 2659.05,
                    'pct_change': 2.48,
                },
            ]
        )
        out = dropUnusableRtSwKRows(df)
        self.assertEqual(list(out['ts_code']), ['801030.SI'])


if __name__ == '__main__':
    unittest.main()
