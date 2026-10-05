# coding: utf-8
"""amount_top5_pct 弹窗滚动五年 2σ：正确性 + 全历史长度必须亚秒级。"""
from __future__ import annotations

import statistics
import time
import unittest
from datetime import date, timedelta

from rt_monitor.monolith import (
    AMOUNT_TOP5_PCTILE_LOOKBACK_YEARS,
    _compute_amount_top5_pct_rolling_sigma2_series,
)


def _naiveRollingSigma2(
    tradeDates: list[date],
    values: list[float | None],
) -> tuple[list[float | None], list[float | None]]:
    n = len(tradeDates)
    lowOut: list[float | None] = [None] * n
    highOut: list[float | None] = [None] * n
    if n == 0:
        return lowOut, highOut
    spanDays = AMOUNT_TOP5_PCTILE_LOOKBACK_YEARS * 366 + 30
    firstLow: float | None = None
    firstHigh: float | None = None
    for i in range(n):
        tdI = tradeDates[i]
        if tdI is None:
            continue
        loBound = tdI - timedelta(days=spanDays)
        sample: list[float] = []
        for j in range(i):
            tdJ = tradeDates[j]
            vJ = values[j] if j < len(values) else None
            if tdJ is None or vJ is None:
                continue
            if loBound <= tdJ < tdI:
                sample.append(float(vJ))
        if len(sample) < 2:
            continue
        meanV = statistics.mean(sample)
        stdV = statistics.pstdev(sample)
        if stdV <= 0:
            continue
        lowV = round(meanV - 2 * stdV, 2)
        highV = round(meanV + 2 * stdV, 2)
        lowOut[i] = lowV
        highOut[i] = highV
        if firstLow is None:
            firstLow = lowV
            firstHigh = highV
    if firstLow is not None and firstHigh is not None:
        for i in range(n):
            if lowOut[i] is None:
                lowOut[i] = firstLow
                highOut[i] = firstHigh
    return lowOut, highOut


class AmountTop5PctRollingSigma2Test(unittest.TestCase):
    def testMatchesNaiveOnHolesAndShortSeries(self):
        dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(80)]
        vals: list[float | None] = [
            None if i % 11 == 0 else 20.0 + (i % 9) * 1.7 for i in range(80)
        ]
        gotLo, gotHi = _compute_amount_top5_pct_rolling_sigma2_series(dates, vals)
        expLo, expHi = _naiveRollingSigma2(dates, vals)
        self.assertEqual(len(gotLo), 80)
        for i in range(80):
            if expLo[i] is None:
                self.assertIsNone(gotLo[i])
                continue
            self.assertIsNotNone(gotLo[i])
            self.assertAlmostEqual(gotLo[i], expLo[i], delta=0.02)
            self.assertAlmostEqual(gotHi[i], expHi[i], delta=0.02)

    def testFourThousandDaysFinishesUnder350ms(self):
        n = 4000
        start = date(2010, 1, 4)
        dates = [start + timedelta(days=i) for i in range(n)]
        vals = [30.0 + (i % 17) * 0.4 for i in range(n)]
        t0 = time.perf_counter()
        lowOut, highOut = _compute_amount_top5_pct_rolling_sigma2_series(dates, vals)
        elapsed = time.perf_counter() - t0
        self.assertEqual(len(lowOut), n)
        self.assertGreater(sum(1 for x in highOut if x is not None), 3000)
        self.assertLess(
            elapsed,
            0.35,
            f'滚动 2σ 过慢: {elapsed:.3f}s（全历史弹窗会被 12s 前端超时打死）',
        )


if __name__ == '__main__':
    unittest.main()
