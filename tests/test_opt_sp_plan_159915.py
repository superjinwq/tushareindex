# coding: utf-8
"""159915 Sell Put 合槽 5/5/3/3：通道 N=25、Gap p97、L10、单期限。"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from rt_monitor.services.opt_sp_plan import (
    LOW_NS,
    SP_PROFILES,
    attachBookLots,
    chNHighOf,
    enrichEtfSignals,
    flagsOf,
    gapPThrOf,
    signalRules,
    usedLowNs,
    wCapOf,
)


def _atm() -> list[dict]:
    return [
        {'tsCode': '90000001.SZ', 'tenorKey': 'CM', 'tenor': '当月', 'K': 3.0, 'dte': 10, 'px': 0.05},
        {'tsCode': '90000002.SZ', 'tenorKey': 'NM', 'tenor': '次月', 'K': 3.0, 'dte': 38, 'px': 0.08},
        {'tsCode': '90000003.SZ', 'tenorKey': 'CQ', 'tenor': '当季', 'K': 3.0, 'dte': 70, 'px': 0.12},
        {'tsCode': '90000004.SZ', 'tenorKey': 'NQ', 'tenor': '下季', 'K': 3.0, 'dte': 160, 'px': 0.18},
    ]


class Profile159915Test(unittest.TestCase):
    def setUp(self):
        self.cfg = SP_PROFILES['159915']

    def testAllocAndCap(self):
        self.assertEqual(self.cfg['alloc'], '5/5/3/3')
        self.assertAlmostEqual(wCapOf(self.cfg), 1.6, places=6)
        self.assertEqual(chNHighOf(self.cfg), 25)
        self.assertEqual(gapPThrOf(self.cfg), 97.0)
        self.assertEqual(int(self.cfg['chNLow']), 10)
        self.assertIn(10, LOW_NS)
        self.assertEqual(usedLowNs(self.cfg), (4, 7, 10))

    def testTenorAttach(self):
        spot = 3.0
        ch = attachBookLots('通道', spot, _atm(), 1_000_000.0, self.cfg)
        gap = attachBookLots('Gap', spot, _atm(), 1_000_000.0, self.cfg)
        mom = attachBookLots('动量', spot, _atm(), 1_000_000.0, self.cfg)
        bias = attachBookLots('BIAS', spot, _atm(), 1_000_000.0, self.cfg)
        self.assertEqual([x['tenorKey'] for x in ch], ['NM'])
        self.assertEqual([x['tenorKey'] for x in gap], ['CQ'])
        self.assertEqual([x['tenorKey'] for x in mom], ['CM'])
        self.assertEqual([x['tenorKey'] for x in bias], ['NM'])

    def testChannelBreakUsesN25(self):
        n = 40
        dates = pd.bdate_range(end='2026-08-24', periods=n)
        high = np.full(n, 1.70)
        high[-26:-21] = 1.90
        raw = pd.DataFrame({
            'trade_date': dates,
            'open': 1.60,
            'high': high,
            'low': 1.50,
            'close': 1.60,
            'pre_close': 1.60,
            'vol': 1_000_000.0,
        })
        etf20 = enrichEtfSignals(raw, SP_PROFILES['588000'])
        etf25 = enrichEtfSignals(raw, self.cfg)
        last20 = etf20.iloc[-1]
        last25 = etf25.iloc[-1]
        self.assertAlmostEqual(float(last20['maxHN']), 1.70, places=4)
        self.assertAlmostEqual(float(last25['maxHN']), 1.90, places=4)
        todayHigh = 1.85
        self.assertTrue(bool(todayHigh > last20['maxHN']))
        self.assertFalse(bool(todayHigh > last25['maxHN']))
        self.assertTrue(bool(last25['exitL10'] is not None))
        self.assertIn('exitL10', etf25.columns)

    def testGapP97NotP95(self):
        r = pd.Series({
            'amp_p': 50.0, 'pct_p': 50.0, 'gap_p': 96.0, 'volRatio_p': 50.0,
            'iv_p': 50.0, 'bias_p': 50.0, 'prior5_p': 50.0, 'prior10_p': 50.0,
            'n_down10_p': 50.0, 'pct': 0.5,
        })
        self.assertTrue(flagsOf(r)['gapExt'])
        self.assertTrue(flagsOf(r, 95)['gapExt'])
        self.assertFalse(flagsOf(r, 97)['gapExt'])
        r97 = r.copy()
        r97['gap_p'] = 97.5
        self.assertTrue(flagsOf(r97, 97)['gapExt'])

    def testRuleCardNames(self):
        rules = {x['name']: x for x in signalRules(self.cfg)}
        self.assertIn('高低25/10 通道', rules)
        self.assertEqual(rules['高低25/10 通道']['tenorHint'], '只次月')
        self.assertIn('前25日最高', rules['高低25/10 通道']['open'])
        self.assertIn('P97', rules['Gap']['open'])
        self.assertEqual(rules['Gap']['tenorHint'], '只当季')
        self.assertEqual(rules['动量']['tenorHint'], '只当月')
        self.assertEqual(rules['BIAS']['tenorHint'], '只次月')
        self.assertIn('10日新低', rules['高低25/10 通道']['exitLines'])
        self.assertEqual(rules['高低25/10 通道']['weight'], '50%')
        self.assertEqual(rules['Gap']['weight'], '50%')
        self.assertEqual(rules['动量']['weight'], '30%')
        self.assertEqual(rules['BIAS']['weight'], '30%')


if __name__ == '__main__':
    unittest.main()

# 代码更新时间：2026-09-02 08:05
