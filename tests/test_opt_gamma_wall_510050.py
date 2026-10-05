from __future__ import annotations

from datetime import date

import pytest

import opt_wall_510050 as wall
from opt_wall_510050 import buildGammaWallSnapshot, buildOiWallSnapshot, calculateGammaExposure


DATA_DATE = date(2026, 9, 22)
MATURITY_DATE = date(2026, 10, 28)


def optionRow(
    tsCode: str,
    callPut: str,
    strike: float,
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
        'trade_date': DATA_DATE,
    }


def ivRow(
    callPut: str,
    *,
    representativeIv: float = 0.20,
    forward: float = 3.01,
) -> dict[str, object]:
    return {
        'underlying': '510050.SH',
        'trade_date': DATA_DATE,
        'maturity_date': MATURITY_DATE,
        'tenor': 'next_month',
        'call_put': callPut,
        'representative_iv': representativeIv,
        'forward': forward,
        'spot': 3.0,
        'dte': 36,
    }


def testCalculateGammaExposureUsesBlack76GammaAndPerUnit() -> None:
    result = calculateGammaExposure(
        forward=3.01,
        strike=3.00,
        timeToExpiry=36 / 365,
        representativeIv=0.20,
        spot=3.00,
        oi=100,
        perUnit=10100,
    )

    expectedGamma = wall.black76_gamma(
        forward=3.01,
        strike=3.00,
        time_to_expiry=36 / 365,
        volatility=0.20,
        spot=3.00,
    )
    expectedExposure = expectedGamma * 100 * 10100 * 3.00**2 * 0.01

    assert result['available'] is True
    assert result['gamma'] == pytest.approx(expectedGamma)
    assert result['gammaExposure'] == pytest.approx(expectedExposure)
    assert result['perUnit'] == 10100


def testBuildGammaWallSnapshotAggregatesCallPutAndKeepsAdjustedPerUnit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wall, 'black76_gamma', lambda **_kwargs: 1.0)
    rows = [
        optionRow('C-3000', 'C', 3.00, 100),
        optionRow('C-3100-A', 'C', 3.10, 50, perUnit=10100.0),
        optionRow('P-3000', 'P', 3.00, 80),
        optionRow('P-MISSING', 'P', 3.10, None),
    ]

    snapshot = buildGammaWallSnapshot(
        rows,
        [ivRow('C'), ivRow('P')],
        dataDate=DATA_DATE,
        spot=3.00,
    )

    expiry = snapshot['expiries'][0]
    assert expiry['available'] is True
    assert expiry['aggregate']['wallStrikes'] == [3.0]
    assert expiry['aggregate']['wallExposure'] == pytest.approx(162000.0)
    assert expiry['aggregate']['top1Share'] == pytest.approx(162000 / (162000 + 45450))
    assert expiry['call']['totalGammaExposure'] == pytest.approx(135450.0)
    assert expiry['put']['totalGammaExposure'] == pytest.approx(72000.0)
    assert snapshot['quality']['adjustedRows'] == 1
    assert snapshot['quality']['missingOiRows'] == 1

    adjusted = next(
        contract
        for item in expiry['call']['strikes']
        if item['strike'] == 3.1
        for contract in item['contracts']
    )
    assert adjusted['contractClass'] == 'adjusted'
    assert adjusted['perUnit'] == 10100


def testBuildGammaWallSnapshotKeepsMissingIvSideUnavailable() -> None:
    snapshot = buildGammaWallSnapshot(
        [optionRow('C-3000', 'C', 3.00, 100), optionRow('P-3000', 'P', 3.00, 80)],
        [ivRow('C')],
        dataDate=DATA_DATE,
        spot=3.00,
    )

    expiry = snapshot['expiries'][0]
    assert expiry['available'] is True
    assert expiry['call']['totalGammaExposure'] is not None
    assert expiry['put']['totalGammaExposure'] is None
    assert expiry['aggregate']['wallStrikes'] == [3.0]
    assert snapshot['quality']['missingIvRows'] == 1


def testBuildOiWallSnapshotAttachesGammaSnapshotWhenIvRowsProvided() -> None:
    snapshot = buildOiWallSnapshot(
        [optionRow('C-3000', 'C', 3.00, 100), optionRow('P-3000', 'P', 3.00, 80)],
        dataDate=DATA_DATE,
        spot=3.00,
        ivRows=[ivRow('C'), ivRow('P')],
    )

    gamma = snapshot['expiries'][0]['gamma']
    assert gamma['available'] is True
    assert gamma['aggregate']['wallStrikes'] == [3.0]
    assert gamma['formula'].startswith('GammaExposure(K, side)')


def testBuildGammaWallSnapshotReturnsUnavailableWhenNoValidIv() -> None:
    snapshot = buildGammaWallSnapshot(
        [optionRow('C-3000', 'C', 3.00, 100)],
        [],
        dataDate=DATA_DATE,
        spot=3.00,
    )

    expiry = snapshot['expiries'][0]
    assert expiry['available'] is False
    assert expiry['aggregate']['wallStrikes'] == []
    assert snapshot['quality']['missingIvRows'] == 1


def testBuildGammaWallSnapshotReportsQualityPerExpiry() -> None:
    secondRow = optionRow('C-DEC', 'C', 3.00, 80)
    secondRow['maturity_date'] = date(2026, 12, 23)
    secondIv = ivRow('C')
    secondIv['maturity_date'] = date(2026, 12, 23)

    snapshot = buildGammaWallSnapshot(
        [optionRow('C-OCT', 'C', 3.00, 100), secondRow],
        [ivRow('C'), secondIv],
        dataDate=DATA_DATE,
        spot=3.00,
    )

    assert snapshot['expiries'][0]['quality']['validContracts'] == 1
    assert snapshot['expiries'][1]['quality']['validContracts'] == 1


def testCalculateGammaZoneFindsSmallestWallCenteredInterval() -> None:
    rows = [
        {'strike': 2.90, 'totalGammaExposure': 20.0, 'isWall': False},
        {'strike': 3.00, 'totalGammaExposure': 40.0, 'isWall': True},
        {'strike': 3.10, 'totalGammaExposure': 15.0, 'isWall': False},
        {'strike': 3.20, 'totalGammaExposure': 25.0, 'isWall': False},
    ]

    zone = wall.calculateGammaZone(rows, spot=3.00, coverageThreshold=0.50)

    assert zone['available'] is True
    assert zone['zoneStrikes'] == [2.9, 3.0]
    assert zone['lowerStrike'] == 2.9
    assert zone['upperStrike'] == 3.0
    assert zone['zoneGammaExposure'] == pytest.approx(60.0)
    assert zone['totalGammaExposure'] == pytest.approx(100.0)
    assert zone['zoneShare'] == pytest.approx(0.60)
    assert zone['lowerDistancePct'] == pytest.approx(-3.333333, abs=1e-6)
    assert zone['upperDistancePct'] == pytest.approx(0.0)


def testCalculateGammaZoneReturnsUnavailableWithoutPositiveExposure() -> None:
    zone = wall.calculateGammaZone(
        [{'strike': 3.00, 'totalGammaExposure': 0.0, 'isWall': True}],
        spot=3.00,
    )

    assert zone['available'] is False
    assert zone['zoneStrikes'] == []
    assert zone['zoneShare'] is None


def testCalculateGammaFlipInterpolatesCallPutBalanceProxy() -> None:
    rows = [
        {'strike': 3.00, 'callGammaExposure': 80.0, 'putGammaExposure': 60.0},
        {'strike': 3.10, 'callGammaExposure': 40.0, 'putGammaExposure': 60.0},
    ]

    flip = wall.calculateGammaFlip(rows, spot=3.00)

    assert flip['available'] is True
    assert flip['strike'] == pytest.approx(3.05)
    assert flip['lowerStrike'] == 3.0
    assert flip['upperStrike'] == 3.1
    assert flip['lowerNetGammaProxy'] == pytest.approx(20.0)
    assert flip['upperNetGammaProxy'] == pytest.approx(-20.0)
    assert flip['interpolationRatio'] == pytest.approx(0.5)
    assert flip['method'] == 'call_minus_put_gex_balance_proxy'
    assert flip['scenario'] is True


def testCalculateGammaFlipUsesExactZeroAndDoesNotFillMissingPut() -> None:
    exact = wall.calculateGammaFlip(
        [
            {'strike': 3.00, 'callGammaExposure': 60.0, 'putGammaExposure': 60.0},
            {'strike': 3.10, 'callGammaExposure': 40.0, 'putGammaExposure': 60.0},
        ],
        spot=3.00,
    )
    unavailable = wall.calculateGammaFlip(
        [
            {'strike': 3.00, 'callGammaExposure': 80.0, 'putGammaExposure': None},
            {'strike': 3.10, 'callGammaExposure': 40.0, 'putGammaExposure': 60.0},
        ],
        spot=3.00,
    )
    zeroExposure = wall.calculateGammaFlip(
        [{'strike': 3.00, 'callGammaExposure': 0.0, 'putGammaExposure': 0.0}],
        spot=3.00,
    )
    noCross = wall.calculateGammaFlip(
        [
            {'strike': 3.00, 'callGammaExposure': 80.0, 'putGammaExposure': 60.0},
            {'strike': 3.10, 'callGammaExposure': 70.0, 'putGammaExposure': 60.0},
        ],
        spot=3.00,
    )

    assert exact['available'] is True
    assert exact['strike'] == 3.0
    assert exact['exact'] is True
    assert unavailable['available'] is False
    assert unavailable['strike'] is None
    assert zeroExposure['available'] is False
    assert noCross['available'] is False


def testBuildGammaWallSnapshotAttachesZoneAndFlipProxyToAggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wall, 'black76_gamma', lambda **_kwargs: 1.0)
    rows = [
        optionRow('C-3000', 'C', 3.00, 100),
        optionRow('P-3000', 'P', 3.00, 50),
        optionRow('C-3100', 'C', 3.10, 50),
        optionRow('P-3100', 'P', 3.10, 100),
    ]

    snapshot = buildGammaWallSnapshot(
        rows,
        [ivRow('C'), ivRow('P')],
        dataDate=DATA_DATE,
        spot=3.00,
    )

    aggregate = snapshot['expiries'][0]['aggregate']
    assert aggregate['zone']['available'] is True
    assert aggregate['zone']['zoneStrikes'] == [3.0, 3.1]
    assert aggregate['zone']['zoneShare'] == pytest.approx(1.0)
    assert aggregate['flip']['available'] is True
    assert aggregate['flip']['strike'] == pytest.approx(3.05)
    assert aggregate['flip']['method'] == 'call_minus_put_gex_balance_proxy'
    assert aggregate['strikes'][0]['netGammaProxy'] == pytest.approx(45000.0)
    assert aggregate['strikes'][1]['netGammaProxy'] == pytest.approx(-45000.0)
    assert all(item['isGammaZone'] for item in aggregate['strikes'])
    assert all(item['isGammaFlipBracket'] for item in aggregate['strikes'])
