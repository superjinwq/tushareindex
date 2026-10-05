# coding: utf-8
"""IV 看板：QMT 快照重算五只 ETF「最新」代表 IV；中金所不叠。"""
from __future__ import annotations

import copy
import unittest
from datetime import date, datetime, timedelta

from option_iv import black76_price
from rt_monitor.services.opt_iv_board import applyLiveIvToBlocks, liveRepIvByTenor


def _ms(asof: date, hh: int, mm: int, ss: int) -> float:
    """构造 asof 当天某时刻的毫秒时间戳，供字段级 as-of 校验使用。"""
    return datetime(asof.year, asof.month, asof.day, hh, mm, ss).timestamp() * 1000.0


def _atmChain(asof: date, spot: float, sigma: float, rate: float):
    md = asof + timedelta(days=30)
    tExp = (md - asof).days / 365.0
    k = round(spot, 2)
    callPx = black76_price(spot, k, tExp, rate, sigma, 'C')
    putPx = black76_price(spot, k, tExp, rate, sigma, 'P')
    contracts = [
        {
            'ts_code': '10000001.SH',
            'call_put': 'C',
            'exercise_price': k,
            'maturity_date': md,
        },
        {
            'ts_code': '10000002.SH',
            'call_put': 'P',
            'exercise_price': k,
            'maturity_date': md,
        },
    ]
    quotes = {
        '10000001.SH': {'close': callPx, 'vol': 100.0, 'oi': 200.0},
        '10000002.SH': {'close': putPx, 'vol': 80.0, 'oi': 180.0},
    }
    return contracts, quotes, md


class LiveRepIvTest(unittest.TestCase):
    def testAtmRecoversSigma(self):
        asof = date(2026, 8, 24)
        spot = 1.20
        sigma = 0.25
        rate = 0.02
        contracts, quotes, _md = _atmChain(asof, spot, sigma, rate)
        out = liveRepIvByTenor(contracts, quotes, spot, rate, asof)
        self.assertIn(('current_month', 'C'), out)
        self.assertIn(('current_month', 'P'), out)
        self.assertAlmostEqual(out[('current_month', 'C')], sigma, delta=0.02)
        self.assertAlmostEqual(out[('current_month', 'P')], sigma, delta=0.02)

    def testExpiryDayStillSolvesRepIv(self):
        asof = date(2026, 8, 24)
        spot = 1.20
        sigma = 0.25
        rate = 0.02
        contracts, quotes, md = _atmChain(asof, spot, sigma, rate)
        for row in contracts:
            row['maturity_date'] = asof
        out = liveRepIvByTenor(contracts, quotes, spot, rate, asof)
        self.assertIn(('current_month', 'C'), out)
        self.assertGreater(out[('current_month', 'C')], 0.0)


class OverlayBlocksTest(unittest.TestCase):
    def testEtfLatestOverlaidCffexUntouched(self):
        asof = date(2026, 8, 24)
        spot = 2.80
        sigma = 0.25
        rate = 0.02
        contracts, quotes, _md = _atmChain(asof, spot, sigma, rate)
        snap = {
            'ok': True,
            'etf': {
                '510050.SH': {'last': spot, 'time': _ms(asof, 10, 15, 1)},
                '000300.SH': {'last': 4000.0, 'time': _ms(asof, 10, 15, 1)},
            },
            'opt': {
                '10000001.SHO': {
                    'last': quotes['10000001.SH']['close'],
                    'vol': 100,
                    'oi': 200,
                    'time': _ms(asof, 10, 15, 1),
                    'lastTime': _ms(asof, 10, 15, 1),
                    'qtyTime': _ms(asof, 10, 15, 1),
                    'oiTime': _ms(asof, 10, 15, 1),
                },
                '10000002.SHO': {
                    'last': quotes['10000002.SH']['close'],
                    'vol': 80,
                    'oi': 180,
                    'time': _ms(asof, 10, 15, 1),
                    'lastTime': _ms(asof, 10, 15, 1),
                    'qtyTime': _ms(asof, 10, 15, 1),
                    'oiTime': _ms(asof, 10, 15, 1),
                },
            },
            'optWall': '2026-08-24T10:15:01',
        }
        callStats = {
            'min': '10.00',
            'med': '20.00',
            'current': '20.00',
            'current_raw': 0.20,
            '_hist_vals': [0.15, 0.20, 0.25],
        }
        putStats = {
            'min': '11.00',
            'med': '21.00',
            'current': '21.00',
            'current_raw': 0.21,
            '_hist_vals': [0.16, 0.21, 0.26],
        }
        etfBlock = {
            'underlying': '510050.SH',
            'asof': '2026-08-21',
            'tenors': [
                {
                    'tenor': 'current_month',
                    'call': copy.deepcopy(callStats),
                    'put': copy.deepcopy(putStats),
                    'axis': {
                        'hv': '18.00',
                        'hv_raw': 0.18,
                        'delta': '+2.0',
                        'muted': False,
                    },
                }
            ],
        }
        cffexBlock = {
            'underlying': '000300.SH',
            'asof': '2026-08-21',
            'tenors': [
                {
                    'tenor': 'current_month',
                    'call': copy.deepcopy(callStats),
                    'put': copy.deepcopy(putStats),
                    'axis': {'hv': '18.00', 'hv_raw': 0.18, 'delta': '+2.0'},
                }
            ],
        }
        blocks = [etfBlock, cffexBlock]
        hitN = applyLiveIvToBlocks(
            blocks,
            snap,
            {'510050.SH': contracts},
            asof=asof,
            rate=rate,
            asofLabel='实时 10:15:01',
        )
        self.assertGreaterEqual(hitN, 1)
        self.assertEqual(etfBlock['asof'], '实时 10:15:01')
        self.assertEqual(etfBlock['tenors'][0]['call']['min'], '10.00')
        self.assertAlmostEqual(
            float(etfBlock['tenors'][0]['call']['current_raw']),
            sigma,
            delta=0.02,
        )
        self.assertEqual(cffexBlock['asof'], '2026-08-21')
        self.assertEqual(cffexBlock['tenors'][0]['call']['current'], '20.00')
        self.assertEqual(cffexBlock['tenors'][0]['call']['current_raw'], 0.20)

    def testCrossDayTickWithLiveLookingValuesRejected(self):
        """回归（2026-09-11）：昨日 tick 带正 last/vol 也不得冒充当日实时。

        旧语义只查 last>0 且 vol>0，跨日旧值会被判 live 并把 asof 改成今天。
        新语义要求 last/qty 字段有自己的当日时钟。
        """
        asof = date(2026, 8, 25)
        prevMs = datetime(2026, 8, 24, 14, 55).timestamp() * 1000.0
        contracts, quotes, _md = _atmChain(asof, 2.80, 0.25, 0.02)
        snap = {
            'ok': True,
            'etf': {'510050.SH': {'last': 2.80, 'time': prevMs}},
            'opt': {
                '10000001.SHO': {
                    'last': quotes['10000001.SH']['close'],
                    'vol': 100,
                    'oi': 200,
                    'time': prevMs,
                    'lastTime': prevMs,
                    'qtyTime': prevMs,
                },
            },
        }
        etfBlock = {
            'underlying': '510050.SH',
            'asof': '2026-08-24',
            'tenors': [
                {
                    'tenor': 'current_month',
                    'call': {'current': '20.00', 'current_raw': 0.20, '_hist_vals': [0.2]},
                    'put': {'current': '21.00', 'current_raw': 0.21, '_hist_vals': [0.21]},
                    'axis': {'hv': '18.00', 'hv_raw': 0.18, 'delta': '+2.0'},
                }
            ],
        }
        hitN = applyLiveIvToBlocks(
            [etfBlock],
            snap,
            {'510050.SH': contracts},
            asof=asof,
            rate=0.02,
            asofLabel='实时 10:15:01',
        )
        self.assertEqual(hitN, 0)
        self.assertEqual(etfBlock['asof'], '2026-08-24')

    def testStaleTickSkipped(self):
        asof = date(2026, 8, 25)
        oldMs = datetime(2026, 8, 21, 10, 11).timestamp() * 1000.0
        contracts, quotes, _md = _atmChain(asof, 2.80, 0.25, 0.02)
        snap = {
            'ok': True,
            'etf': {'510050.SH': {'last': 2.80}},
            'opt': {
                '10000001.SHO': {
                    'last': quotes['10000001.SH']['close'],
                    'vol': 0,
                    'oi': 200,
                    't': oldMs,
                    'bid1': 0.10,
                    'ask1': 0.20,
                },
                '10000002.SHO': {
                    'last': quotes['10000002.SH']['close'],
                    'vol': 0,
                    'oi': 180,
                    't': oldMs,
                },
            },
        }
        etfBlock = {
            'underlying': '510050.SH',
            'asof': '2026-08-24',
            'tenors': [
                {
                    'tenor': 'current_month',
                    'call': {'current': '20.00', 'current_raw': 0.20, '_hist_vals': [0.2]},
                    'put': {'current': '21.00', 'current_raw': 0.21, '_hist_vals': [0.21]},
                    'axis': {'hv': '18.00', 'hv_raw': 0.18, 'delta': '+2.0'},
                }
            ],
        }
        hitN = applyLiveIvToBlocks(
            [etfBlock],
            snap,
            {'510050.SH': contracts},
            asof=asof,
            rate=0.02,
            asofLabel='实时 10:15:01',
        )
        self.assertEqual(hitN, 0)
        self.assertEqual(etfBlock['asof'], '2026-08-24')
        self.assertEqual(etfBlock['tenors'][0]['call']['current'], '20.00')

    def testStubEtfSpotSkipped(self):
        asof = date(2026, 8, 25)
        contracts, quotes, _md = _atmChain(asof, 1.693, 0.25, 0.02)
        etfBlock = {
            'underlying': '588000.SH',
            'asof': '2026-08-24',
            'tenors': [
                {
                    'tenor': 'current_month',
                    'call': {'current': '20.00', 'current_raw': 0.20, '_hist_vals': [0.2]},
                    'put': {'current': '21.00', 'current_raw': 0.21, '_hist_vals': [0.21]},
                    'axis': {'hv': '18.00', 'hv_raw': 0.18, 'delta': '+2.0'},
                }
            ],
        }
        snap = {
            'ok': True,
            'etf': {
                '588000.SH': {
                    'last': 1.691,
                    'open': 1.691,
                    'high': 1.691,
                    'low': 1.691,
                    'vol': 0,
                },
            },
            'opt': {
                '10000001.SHO': {
                    'last': quotes['10000001.SH']['close'],
                    'vol': 100,
                    'oi': 200,
                },
                '10000002.SHO': {
                    'last': quotes['10000002.SH']['close'],
                    'vol': 80,
                    'oi': 180,
                },
            },
        }
        hitN = applyLiveIvToBlocks(
            [etfBlock],
            snap,
            {'588000.SH': contracts},
            asof=asof,
            rate=0.02,
            asofLabel='实时 15:10:02',
        )
        self.assertEqual(hitN, 0)
        self.assertEqual(etfBlock['asof'], '2026-08-24')
        self.assertEqual(etfBlock['tenors'][0]['call']['current'], '20.00')


class LiveChainSqlTest(unittest.TestCase):
    def testNoAOrUnitFilter(self):
        import inspect
        from rt_monitor.services.opt_iv_board import _fetchEtfLiveChain

        src = inspect.getsource(_fetchEtfLiveChain)
        self.assertNotIn("<> 'A'", src)
        self.assertNotIn('= 10000', src)
        self.assertIn("IN ('C', 'P')", src)
        self.assertIn('delist_date >= CURDATE()', src)
        self.assertNotIn('delist_date > CURDATE()', src)


class FmtIvPctMaxTest(unittest.TestCase):
    def testThreeDigitOneDecimal(self):
        from rt_monitor.services.opt_iv_board import _fmtIvPctMax

        self.assertEqual(_fmtIvPctMax(1.2302), '123.0')
        self.assertEqual(_fmtIvPctMax(1.0), '100.0')
        self.assertEqual(_fmtIvPctMax(1.887), '188.7')
        self.assertEqual(_fmtIvPctMax(0.6663), '66.63')
        self.assertEqual(_fmtIvPctMax(0.9999), '99.99')
        self.assertEqual(_fmtIvPctMax(None), '—')

    def testPlus3SigmaDisplayUsesCompact(self):
        import numpy as np
        from rt_monitor.services.opt_iv_board import _empSigmaBand, _fmtIvPctMax

        vals = np.linspace(0.20, 1.2302, 800)
        text, title, raw = _empSigmaBand(vals, 3.0)
        self.assertIsNotNone(raw)
        self.assertGreaterEqual(raw * 100.0, 100.0)
        self.assertEqual(text, _fmtIvPctMax(raw))
        self.assertRegex(text, r'^\d{3}\.\d$')
        self.assertIn(f'{raw * 100:.2f}%', title)


if __name__ == '__main__':
    unittest.main()

# 代码更新时间：2026-08-24 21:12
# 代码更新时间：2026-08-25 14:52 僵 tick 不叠实时 IV
# 代码更新时间：2026-08-25 18:27 占位现货不叠实时 IV
# 代码更新时间：2026-08-25 20:41 活链 SQL 不过滤 A
# 代码更新时间：2026-08-26 06:17 活链含摘牌当日
# 代码更新时间：2026-09-21 07:03 ≥100% 一位小数（123.0）对齐 ±σ/Max
