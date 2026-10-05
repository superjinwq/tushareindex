# coding: utf-8
"""Sell Put 盘中预演：通道/Gap 对齐交易日窗口，昨日 K 隐藏。"""
from __future__ import annotations

import unittest
from datetime import date

import numpy as np
import pandas as pd

from rt_monitor.services.opt_sp_plan import (
    SP_PROFILES,
    buildSpPreview,
    enrichEtfSignals,
    extractQmtEtfTick,
    isYesterdayFrozenBar,
)


def _hist(n: int, *, close: float = 1.60, high: float = 1.65, low: float = 1.55) -> pd.DataFrame:
    dates = pd.bdate_range(end='2026-08-24', periods=n)
    return pd.DataFrame({
        'trade_date': dates,
        'open': close,
        'high': high,
        'low': low,
        'close': close,
        'pre_close': close,
        'vol': 1_000_000.0,
        'representative_iv': np.nan,
    })


def _basic() -> pd.DataFrame:
    return pd.DataFrame({
        'ts_code': ['10000001.SH', '10000002.SH', '10000003.SH', '10000004.SH'],
        'name': ['P1', 'P2', 'P3', 'P4'],
        'exercise_price': [1.60, 1.60, 1.60, 1.60],
        'maturity_date': pd.to_datetime([
            '2026-09-23', '2026-10-28', '2026-12-23', '2027-03-24',
        ]),
        'list_date': pd.to_datetime(['2026-01-01'] * 4),
        'delist_date': pd.to_datetime([
            '2026-09-24', '2026-10-29', '2026-12-24', '2027-03-25',
        ]),
    })


class YesterdayBarTest(unittest.TestCase):
    def testFrozenMatchesLastDaily(self):
        last = pd.Series({
            'pre_close': 1.745, 'open': 1.741, 'high': 1.743,
            'low': 1.662, 'close': 1.691,
        })
        tick = {
            'lastClose': 1.745, 'open': 1.741, 'high': 1.743,
            'low': 1.662, 'last': 1.691,
        }
        self.assertTrue(isYesterdayFrozenBar(last, tick))

    def testNewSessionLastCloseCuts(self):
        last = pd.Series({
            'pre_close': 1.745, 'open': 1.741, 'high': 1.743,
            'low': 1.662, 'close': 1.691,
        })
        tick = {
            'lastClose': 1.691, 'open': 1.700, 'high': 1.710,
            'low': 1.690, 'last': 1.705,
        }
        self.assertFalse(isYesterdayFrozenBar(last, tick))


class StubSnapshotTickTest(unittest.TestCase):
    def testEmptyShellRejected(self):
        packed = {
            'last': 1.691,
            'open': 1.691,
            'high': 1.691,
            'low': 1.691,
            'vol': 0,
            'lastClose': 1.691,
        }
        self.assertIsNone(extractQmtEtfTick(packed))

    def testRealBarAccepted(self):
        packed = {
            'last': 1.693,
            'open': 1.666,
            'high': 1.714,
            'low': 1.651,
            'vol': 3.3e9,
            'lastClose': 1.691,
        }
        tick = extractQmtEtfTick(packed)
        self.assertIsNotNone(tick)
        self.assertEqual(tick['last'], 1.693)


class PreviewLogicTest(unittest.TestCase):
    def setUp(self):
        self.cfg = SP_PROFILES['588000']
        self.today = date(2026, 8, 25)
        self.basic = _basic()

    def testHideYesterdayBar(self):
        etf = enrichEtfSignals(_hist(130), self.cfg)
        last = etf.iloc[-1]
        tick = {
            'last': float(last['close']),
            'open': float(last['open']),
            'high': float(last['high']),
            'low': float(last['low']),
            'lastClose': float(last['pre_close']),
            'vol': float(last['vol']),
        }
        out = buildSpPreview(
            etf, self.basic, [], tick, {}, 1_000_000.0, self.cfg,
            today=self.today,
        )
        self.assertFalse(out['active'])
        self.assertEqual(out['reason'], 'yesterday_bar')

    def testHideWhenDailyCaughtUp(self):
        raw = _hist(130)
        raw.loc[raw.index[-1], 'trade_date'] = pd.Timestamp('2026-08-25')
        etf = enrichEtfSignals(raw, self.cfg)
        tick = {'last': 1.7, 'open': 1.7, 'high': 1.8, 'low': 1.6, 'lastClose': 1.6, 'vol': 1.0}
        out = buildSpPreview(
            etf, self.basic, [], tick, {}, 1_000_000.0, self.cfg,
            today=self.today,
        )
        self.assertFalse(out['active'])
        self.assertEqual(out['reason'], 'daily_caught_up')

    def testChannelUsesTradeDayHighWindow(self):
        raw = _hist(40, close=1.60, high=1.80, low=1.50)
        etf = enrichEtfSignals(raw, self.cfg)
        tick = {
            'last': 1.91, 'open': 1.70, 'high': 1.91, 'low': 1.69,
            'lastClose': 1.60, 'vol': 2_000_000.0,
        }
        out = buildSpPreview(
            etf, self.basic, [], tick, {}, 1_000_000.0, self.cfg,
            today=self.today, dailyPxMap={'10000001.SH': 0.05, '10000002.SH': 0.06},
        )
        self.assertTrue(out['active'])
        self.assertTrue(out['brkH'])
        self.assertAlmostEqual(out['maxH20'], 1.80, places=4)
        books = [x['book'] for x in out['opens']]
        self.assertIn('通道', books)
        self.assertNotIn('动量', books)
        self.assertNotIn('BIAS', books)

    def testGapPercentileIsTradeDayExpanding(self):
        raw = _hist(130, close=1.60, high=1.61, low=1.59)
        etf = enrichEtfSignals(raw, self.cfg)
        tick = {
            'last': 1.70, 'open': 1.80, 'high': 1.81, 'low': 1.69,
            'lastClose': 1.60, 'vol': 2_000_000.0,
        }
        out = buildSpPreview(
            etf, self.basic, [], tick, {}, 1_000_000.0, self.cfg,
            today=self.today, dailyPxMap={'10000001.SH': 0.05, '10000002.SH': 0.06},
        )
        self.assertTrue(out['active'])
        self.assertTrue(out['gapExt'])
        self.assertGreaterEqual(out['gapP'], 95.0)
        self.assertIn('Gap', [x['book'] for x in out['opens']])

    def testGapIgnoresDangerBan(self):
        raw = _hist(130, close=1.60, high=1.61, low=1.59)
        etf = enrichEtfSignals(raw, self.cfg)
        tick = {
            'last': 1.70, 'open': 1.80, 'high': 1.81, 'low': 1.69,
            'lastClose': 1.60, 'vol': 2_000_000.0,
        }
        out = buildSpPreview(
            etf, self.basic, [], tick, {}, 1_000_000.0, self.cfg,
            today=self.today, dailyPxMap={'10000001.SH': 0.05, '10000002.SH': 0.06},
        )
        self.assertTrue(out['dangerPending'])
        self.assertIn('Gap', [x['book'] for x in out['opens']])

    def testChannelLowExitPreview(self):
        raw = _hist(40, close=1.80, high=1.82, low=1.78)
        etf = enrichEtfSignals(raw, self.cfg)
        pos = [{
            'id': 1,
            'book': '通道',
            'signalName': '高低20/6',
            'tsCode': '10000001.SH',
            'tenor': '次月',
            'tenorKey': 'NM',
            'K': 1.60,
            'lots': 10,
            'entryDate': '2026-08-10',
            'signalDate': '2026-08-07',
            'entryPrem': 0.10,
            'entrySpot': 1.70,
            'exitProfile': '通道A0.6',
            'mat': '2026-10-28',
        }]
        tick = {
            'last': 1.70, 'open': 1.72, 'high': 1.73, 'low': 1.50,
            'lastClose': 1.80, 'vol': 1.0,
        }
        out = buildSpPreview(
            etf, self.basic, pos, tick, {}, 1_000_000.0, self.cfg,
            today=self.today, dailyPxMap={'10000001.SH': 0.08},
        )
        self.assertTrue(out['active'])
        self.assertTrue(out['closes'])
        self.assertEqual(out['closes'][0]['reason'], '6日新低')
        self.assertFalse(out['opens'])

    def testNoDangerForceCloseInPreview(self):
        raw = _hist(40, close=1.80, high=1.82, low=1.78)
        etf = enrichEtfSignals(raw, self.cfg)
        pos = [{
            'id': 2,
            'book': 'Gap',
            'signalName': 'Gap',
            'tsCode': '10000001.SH',
            'tenor': '次月',
            'tenorKey': 'NM',
            'K': 1.60,
            'lots': 10,
            'entryDate': '2026-08-10',
            'signalDate': '2026-08-07',
            'entryPrem': 0.10,
            'entrySpot': 1.70,
            'exitProfile': '通道L5',
            'mat': '2026-10-28',
        }]
        tick = {
            'last': 1.79, 'open': 1.80, 'high': 1.81, 'low': 1.78,
            'lastClose': 1.80, 'vol': 1.0,
        }
        out = buildSpPreview(
            etf, self.basic, pos, tick, {}, 1_000_000.0, self.cfg,
            today=self.today, dailyPxMap={'10000001.SH': 0.11},
        )
        self.assertTrue(out['active'])
        self.assertFalse(any(c['reason'] == '危险强平' for c in out['closes']))


if __name__ == '__main__':
    unittest.main()

# 代码更新时间：2026-08-25 18:27 预演拒 ETF 占位柱
