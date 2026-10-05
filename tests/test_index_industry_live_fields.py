# coding: utf-8
"""宽基总览实时列：成分重叠计数 + 行情字段契约。"""
from __future__ import annotations

import unittest

from rt_monitor.services.sw_industry import (
    _index_aggregate_hl_by_constituents,
    _index_apply_hl_margin_to_row,
    _index_apply_pct_amt_to_row,
    _index_live_amt_null,
    _index_live_pct_null,
)


class IndexAggregateHlTest(unittest.TestCase):
    def testOverlapCountsPerIndex(self):
        flagged = {'600000.SH', '000001.SZ'}
        cons = {
            '000300.SH': {'600000.SH', '000001.SZ', '600519.SH'},
            '000905.SH': {'000001.SZ'},
        }
        out = _index_aggregate_hl_by_constituents(flagged, cons)
        self.assertEqual(out['000300.SH'], 2)
        self.assertEqual(out['000905.SH'], 1)

    def testEmptyFlaggedIsZero(self):
        cons = {'000016.SH': {'600000.SH'}}
        out = _index_aggregate_hl_by_constituents(set(), cons)
        self.assertEqual(out['000016.SH'], 0)


class IndexLiveNullContractTest(unittest.TestCase):
    def testPctAndAmtKeySets(self):
        pct = _index_live_pct_null()
        amt = _index_live_amt_null()
        self.assertEqual(set(pct), {
            'sw_pct', 'sw_pct_t1', 'sw_pct_t5', 'sw_pct_t20', 'sw_pct_t60', 'sw_pct_t120',
        })
        self.assertEqual(set(amt), {
            'sw_amt_pctile_t0', 'sw_amt_pctile_t1', 'sw_amt_pctile_t5',
            'sw_amt_pctile_t20', 'sw_amt_pctile_t60', 'sw_amt_pctile_t120',
        })
        self.assertTrue(all(v is None for v in pct.values()))
        self.assertTrue(all(v is None for v in amt.values()))


class IndexApplyLiveRowTest(unittest.TestCase):
    def testOverviewWritesPctAmtFromCache(self):
        row = {'l1_code': '000300.SH'}
        _index_apply_pct_amt_to_row(
            row,
            '000300.SH',
            isOverview=True,
            level='l1',
            rtQuotes={'000300.SH': {'pct_change': 1.23}},
            rangeCache={'000300.SH': {
                'range_pct_t1': 0.5, 'range_pct_t5': 1.5,
                'range_pct_t20': 2.0, 'range_pct_t60': 3.0, 'range_pct_t120': 4.0,
            }},
            amtCache={'000300.SH': {
                'sw_amt_pctile_t0': 80.0, 'sw_amt_pctile_t1': 70.0,
                'sw_amt_pctile_t5': 60.0, 'sw_amt_pctile_t20': 50.0,
                'sw_amt_pctile_t60': 40.0, 'sw_amt_pctile_t120': 30.0,
            }},
        )
        self.assertEqual(row['sw_pct'], 1.23)
        self.assertEqual(row['sw_pct_t5'], 1.5)
        self.assertEqual(row['sw_amt_pctile_t0'], 80.0)

    def testOverviewWritesHlAndMargin(self):
        row = {'l1_code': '000300.SH'}
        _index_apply_hl_margin_to_row(
            row,
            '000300.SH',
            level='l1',
            t0Maps={'sw_h250_t0': {'000300.SH': 7}},
            hlcMaps={'sw_hlc_h250': {'000300.SH': 3}},
            marginByGroup={'000300.SH': {
                'sw_margin_rzye': 12.5, 'sw_margin_1d': 0.1,
                'sw_margin_3d': 0.2, 'sw_margin_5d': 0.3,
                'sw_margin_20d': 0.4, 'sw_margin_60d': 0.5,
            }},
        )
        self.assertEqual(row['sw_h250_t0'], 7)
        self.assertEqual(row['sw_hlc_h250'], 3)
        self.assertEqual(row['sw_margin_rzye'], 12.5)

    def testL3KeepsLiveNull(self):
        row = {'l1_code': '801010'}
        _index_apply_pct_amt_to_row(
            row, '801010', isOverview=False, level='l3',
            rtQuotes={}, rangeCache={}, amtCache={},
        )
        _index_apply_hl_margin_to_row(
            row, '801010', level='l3',
            t0Maps={}, hlcMaps={}, marginByGroup={},
        )
        self.assertIsNone(row['sw_pct'])
        self.assertIsNone(row['sw_h250_t0'])
        self.assertIsNone(row['sw_margin_rzye'])


if __name__ == '__main__':
    unittest.main()
