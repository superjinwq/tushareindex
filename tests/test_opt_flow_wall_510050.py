from __future__ import annotations

from datetime import date

import pytest

from opt_wall_510050 import buildFlowWallSnapshot


DATA_DATE = date(2026, 9, 22)
BASELINE_1D = date(2026, 9, 21)
BASELINE_3D = date(2026, 9, 17)
BASELINE_5D = date(2026, 9, 15)
MATURITY_DATE = date(2026, 10, 28)


def flowRow(
    tsCode: str,
    callPut: str,
    strike: float,
    tradeDate: date,
    oi: int | None,
    *,
    perUnit: float = 10000.0,
) -> dict[str, object]:
    return {
        'ts_code': tsCode,
        'call_put': callPut,
        'exercise_price': strike,
        'maturity_date': MATURITY_DATE,
        'oi': oi,
        'per_unit': perUnit,
        'trade_date': tradeDate,
    }


def testBuildFlowWallSnapshotSeparatesPositiveNegativeWallsAndUsesTradingDateBaselines() -> None:
    rows = [
        flowRow('C-3100', 'C', 3.10, DATA_DATE, 150),
        flowRow('C-3200', 'C', 3.20, DATA_DATE, 90),
        flowRow('C-3300', 'C', 3.30, DATA_DATE, 10),
        flowRow('P-2900', 'P', 2.90, DATA_DATE, 80),
        flowRow('P-3000', 'P', 3.00, DATA_DATE, 100),
        flowRow('P-3100', 'P', 3.10, DATA_DATE, 0),
        flowRow('C-3100', 'C', 3.10, BASELINE_1D, 100),
        flowRow('C-3200', 'C', 3.20, BASELINE_1D, 150),
        flowRow('C-3300', 'C', 3.30, BASELINE_1D, 10),
        flowRow('P-2900', 'P', 2.90, BASELINE_1D, 120),
        flowRow('P-3000', 'P', 3.00, BASELINE_1D, 60),
        flowRow('P-3100', 'P', 3.10, BASELINE_1D, 0),
        flowRow('C-3100', 'C', 3.10, BASELINE_3D, 70),
        flowRow('C-3200', 'C', 3.20, BASELINE_3D, 90),
        flowRow('P-2900', 'P', 2.90, BASELINE_3D, 100),
        flowRow('P-3000', 'P', 3.00, BASELINE_3D, 70),
        flowRow('C-3100', 'C', 3.10, BASELINE_5D, 130),
        flowRow('C-3200', 'C', 3.20, BASELINE_5D, 100),
        flowRow('P-2900', 'P', 2.90, BASELINE_5D, 80),
        flowRow('P-3000', 'P', 3.00, BASELINE_5D, 90),
    ]

    snapshot = buildFlowWallSnapshot(
        rows,
        dataDate=DATA_DATE,
        spot=3.0,
        baselineDates={'1D': BASELINE_1D, '3D': BASELINE_3D, '5D': BASELINE_5D},
    )

    oneDay = snapshot['horizons']['1D']
    expiry = oneDay['expiries'][0]
    assert oneDay['baselineDate'] == '2026-09-21'
    assert expiry['call']['positiveWallOi'] == 50
    assert expiry['call']['positiveWallStrikes'] == [3.1]
    assert expiry['call']['positiveWallVelocityOi'] == 50
    assert expiry['call']['negativeWallOi'] == -60
    assert expiry['call']['negativeWallStrikes'] == [3.2]
    assert expiry['call']['negativeWallVelocityOi'] == -60
    assert expiry['put']['positiveWallOi'] == 40
    assert expiry['put']['positiveWallStrikes'] == [3.0]
    assert expiry['put']['negativeWallOi'] == -40
    assert expiry['put']['negativeWallStrikes'] == [2.9]

    threeDay = snapshot['horizons']['3D']['expiries'][0]
    assert snapshot['horizons']['3D']['baselineDate'] == '2026-09-17'
    assert threeDay['call']['positiveWallOi'] == 80
    assert threeDay['call']['positiveWallStrikes'] == [3.1]
    assert threeDay['call']['positiveWallVelocityOi'] == pytest.approx(80 / 3)
    threeDayCallWall = next(item for item in threeDay['call']['strikes'] if item['strike'] == 3.1)
    assert threeDayCallWall['flowVelocityOi'] == pytest.approx(80 / 3)

    fiveDay = snapshot['horizons']['5D']['expiries'][0]
    assert snapshot['horizons']['5D']['baselineDate'] == '2026-09-15'
    assert fiveDay['call']['positiveWallOi'] == 20
    assert fiveDay['call']['positiveWallStrikes'] == [3.1]
    assert fiveDay['call']['positiveWallVelocityOi'] == 4


def testBuildFlowWallSnapshotDoesNotTreatMissingBaselineAsZeroAndKeepsTies() -> None:
    rows = [
        flowRow('C-3100-A', 'C', 3.10, DATA_DATE, 120),
        flowRow('C-3100-B', 'C', 3.10, DATA_DATE, 120),
        flowRow('C-3200', 'C', 3.20, DATA_DATE, 100),
        flowRow('C-3300', 'C', 3.30, DATA_DATE, 50),
        flowRow('C-3400-MISSING', 'C', 3.40, DATA_DATE, 25),
        flowRow('C-3100-A', 'C', 3.10, BASELINE_1D, 100),
        flowRow('C-3100-B', 'C', 3.10, BASELINE_1D, 100),
        flowRow('C-3200', 'C', 3.20, BASELINE_1D, 60),
        flowRow('C-3300', 'C', 3.30, BASELINE_1D, 80),
    ]

    snapshot = buildFlowWallSnapshot(
        rows,
        dataDate=DATA_DATE,
        spot=3.0,
        baselineDates={'1D': BASELINE_1D, '3D': None, '5D': None},
    )

    side = snapshot['horizons']['1D']['expiries'][0]['call']
    assert side['positiveWallOi'] == 40
    assert side['positiveWallStrikes'] == [3.1, 3.2]
    assert side['negativeWallOi'] == -30
    assert side['negativeWallStrikes'] == [3.3]
    assert 3.4 not in [item['strike'] for item in side['strikes']]
    quality = snapshot['horizons']['1D']['quality']
    assert quality['missingBaselineRows'] == 1
    assert quality['flowEligibleRows'] == 4

    assert snapshot['horizons']['3D']['available'] is False
    assert snapshot['horizons']['3D']['baselineDate'] is None
    assert snapshot['horizons']['3D']['expiries'] == []


def testBuildFlowWallSnapshotReturnsSignedConcentrationAndWallChangeRate() -> None:
    rows = [
        flowRow('C-3100', 'C', 3.10, DATA_DATE, 150),
        flowRow('C-3200', 'C', 3.20, DATA_DATE, 130),
        flowRow('C-3300', 'C', 3.30, DATA_DATE, 70),
        flowRow('C-3400', 'C', 3.40, DATA_DATE, 20),
        flowRow('C-3500', 'C', 3.50, DATA_DATE, 20),
        flowRow('C-3100', 'C', 3.10, BASELINE_1D, 100),
        flowRow('C-3200', 'C', 3.20, BASELINE_1D, 100),
        flowRow('C-3300', 'C', 3.30, BASELINE_1D, 100),
        flowRow('C-3400', 'C', 3.40, BASELINE_1D, 80),
        flowRow('C-3500', 'C', 3.50, BASELINE_1D, 0),
    ]

    snapshot = buildFlowWallSnapshot(
        rows,
        dataDate=DATA_DATE,
        spot=3.0,
        baselineDates={'1D': BASELINE_1D, '3D': None, '5D': None},
    )

    side = snapshot['horizons']['1D']['expiries'][0]['call']
    assert side['positiveFlowTotalOi'] == 100
    assert side['negativeFlowTotalOi'] == -90
    assert side['positiveWallConcentration'] == pytest.approx(0.5)
    assert side['negativeWallConcentration'] == pytest.approx(2 / 3)

    positiveWall = next(item for item in side['strikes'] if item['strike'] == 3.1)
    negativeWall = next(item for item in side['strikes'] if item['strike'] == 3.4)
    zeroBaseline = next(item for item in side['strikes'] if item['strike'] == 3.5)
    assert positiveWall['currentOi'] == 150
    assert positiveWall['baselineOi'] == 100
    assert positiveWall['changeRate'] == pytest.approx(0.5)
    assert positiveWall['flowVelocityOi'] == 50
    assert negativeWall['changeRate'] == pytest.approx(-0.75)
    assert zeroBaseline['changeRate'] is None


def testBuildFlowWallSnapshotAddsOneDayVsThreeDayAcceleration() -> None:
    rows = [
        flowRow('C-3100', 'C', 3.10, DATA_DATE, 150),
        flowRow('C-3200', 'C', 3.20, DATA_DATE, 90),
        flowRow('C-3300', 'C', 3.30, DATA_DATE, 100),
        flowRow('C-3100', 'C', 3.10, BASELINE_1D, 100),
        flowRow('C-3200', 'C', 3.20, BASELINE_1D, 150),
        flowRow('C-3300', 'C', 3.30, BASELINE_1D, 100),
        flowRow('C-3100', 'C', 3.10, BASELINE_3D, 70),
        flowRow('C-3200', 'C', 3.20, BASELINE_3D, 90),
        flowRow('C-3300', 'C', 3.30, BASELINE_3D, 100),
    ]

    snapshot = buildFlowWallSnapshot(
        rows,
        dataDate=DATA_DATE,
        spot=3.0,
        baselineDates={'1D': BASELINE_1D, '3D': BASELINE_3D, '5D': None},
    )

    acceleration = snapshot['dynamics']['acceleration1D3D']
    assert acceleration['available'] is True
    assert acceleration['baselineDates'] == {
        '1D': '2026-09-21',
        '3D': '2026-09-17',
    }
    assert acceleration['quality']['pairedRows'] == 3

    call = acceleration['expiries'][0]['call']
    assert call['positiveAccelerationWallOi'] == pytest.approx(50 - 80 / 3)
    assert call['positiveAccelerationWallStrikes'] == [3.10]
    assert call['negativeAccelerationWallOi'] == pytest.approx(-60 - 0)
    assert call['negativeAccelerationWallStrikes'] == [3.20]
    wall = next(item for item in call['strikes'] if item['strike'] == 3.10)
    assert wall['flowVelocity1DOi'] == 50
    assert wall['flowVelocity3DOi'] == pytest.approx(80 / 3)
    assert wall['flowAccelerationOi'] == pytest.approx(50 - 80 / 3)
    assert wall['flowAccelerationRatio'] == pytest.approx(50 / (80 / 3))


def testBuildFlowWallSnapshotAccelerationHandlesZeroThreeDayVelocityAndMissingPair() -> None:
    rows = [
        flowRow('C-3100', 'C', 3.10, DATA_DATE, 150),
        flowRow('C-3200', 'C', 3.20, DATA_DATE, 100),
        flowRow('C-3100', 'C', 3.10, BASELINE_1D, 100),
        flowRow('C-3200', 'C', 3.20, BASELINE_1D, 100),
        flowRow('C-3100', 'C', 3.10, BASELINE_3D, 150),
    ]

    snapshot = buildFlowWallSnapshot(
        rows,
        dataDate=DATA_DATE,
        spot=3.0,
        baselineDates={'1D': BASELINE_1D, '3D': BASELINE_3D, '5D': None},
    )

    acceleration = snapshot['dynamics']['acceleration1D3D']
    assert acceleration['quality']['pairedRows'] == 1
    assert acceleration['quality']['missing3dRows'] == 1
    call = acceleration['expiries'][0]['call']
    item = call['strikes'][0]
    assert item['flowVelocity3DOi'] == 0
    assert item['flowAccelerationOi'] == 50
    assert item['flowAccelerationRatio'] is None


def testBuildFlowWallSnapshotRejectsDuplicateContractDateKeys() -> None:
    rows = [
        flowRow('C-3100', 'C', 3.10, DATA_DATE, 100),
        flowRow('C-3100', 'C', 3.10, DATA_DATE, 101),
    ]

    with pytest.raises(ValueError, match='重复合约日期'):
        buildFlowWallSnapshot(
            rows,
            dataDate=DATA_DATE,
            spot=3.0,
            baselineDates={'1D': BASELINE_1D, '3D': None, '5D': None},
        )
