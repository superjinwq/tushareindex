from __future__ import annotations

from datetime import date

from scripts.opt_wall_historical_study import (
    FlowMetric,
    _returnSummary,
    calculateFlow,
    summarizeStrikeOis,
    summarizeValues,
)


def testSummarizeStrikeOisKeepsWallTop1AndTop3() -> None:
    result = summarizeStrikeOis({2.9: 100.0, 3.0: 300.0, 3.1: 200.0, 3.2: 50.0})

    assert result.wallStrikes == (3.0,)
    assert result.wallOi == 300.0
    assert result.totalOi == 650.0
    assert result.top1Share == 300 / 650
    assert result.top3Share == 600 / 650


def testCalculateFlowUsesOnlyCommonContractIdsAndKeepsPositiveNegativeWalls() -> None:
    maps = {
        (date(2026, 9, 23), date(2026, 10, 28), 'C'): {
            'C1': (3.0, 120.0),
            'C2': (3.1, 80.0),
            'NEW': (3.2, 999.0),
        },
        (date(2026, 9, 22), date(2026, 10, 28), 'C'): {
            'C1': (3.0, 100.0),
            'C2': (3.1, 100.0),
        },
    }

    result = calculateFlow(
        maps,
        date(2026, 9, 23),
        date(2026, 9, 22),
        date(2026, 10, 28),
        'C',
    )

    assert isinstance(result, FlowMetric)
    assert result.commonCount == 2
    assert result.totalDelta == 0
    assert result.positiveTotal == 20
    assert result.negativeTotal == -20
    assert result.positiveWall == (3.0, 20.0)
    assert result.negativeWall == (3.1, -20.0)


def testSummarizeValuesDistinguishesEmptyAndPercentageScale() -> None:
    assert summarizeValues([None, None]) == {'n': 0}
    result = summarizeValues([0.01, -0.02, None], percent=True)

    assert result['n'] == 2
    assert result['median'] == -0.5
    assert result['positiveRate'] == 50.0


def testReturnSummaryReadsNestedDateEventReturns() -> None:
    events = [
        {
            'call': {'return1d': 0.01, 'return3d': 0.02, 'return5d': -0.01},
            'put': {'return1d': 0.01, 'return3d': 0.02, 'return5d': -0.01},
        }
    ]

    assert _returnSummary(events)['1D']['n'] == 1
    assert _returnSummary(events)['1D']['median'] == 1.0


# 代码更新时间：2026-09-25 16:20
