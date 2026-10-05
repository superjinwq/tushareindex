from __future__ import annotations

from datetime import date

import pytest

from opt_wall_510050 import calculateMaxPain, buildOiWallSnapshot


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


def testCalculateMaxPainUsesCallPutIntrinsicValueAndPerUnit() -> None:
    result = calculateMaxPain(
        {2.90: 100, 3.10: 50},
        {2.90: 80, 3.10: 120},
        spot=3.00,
    )

    assert result['available'] is True
    assert result['candidateStrikes'] == [2.9, 3.1]
    assert result['maxPainStrikes'] == [3.1]
    assert result['minPainOiPrice'] == pytest.approx(20.0)
    assert result['minPainYuan'] == pytest.approx(200000.0)

    lowerCandidate = result['candidates'][0]
    assert lowerCandidate['strike'] == 2.9
    assert lowerCandidate['callPainOiPrice'] == pytest.approx(0.0)
    assert lowerCandidate['putPainOiPrice'] == pytest.approx(24.0)
    assert lowerCandidate['totalPainOiPrice'] == pytest.approx(24.0)
    assert lowerCandidate['totalPainYuan'] == pytest.approx(240000.0)
    assert lowerCandidate['isMaxPain'] is False
    assert lowerCandidate['distancePct'] == pytest.approx(-3.3333333333)


def testCalculateMaxPainKeepsAllTiedMinimumCandidates() -> None:
    result = calculateMaxPain(
        {2.90: 100, 3.10: 100},
        {2.90: 100, 3.10: 100},
        spot=3.00,
    )

    assert result['maxPainStrikes'] == [2.9, 3.1]
    assert [item['strike'] for item in result['candidates'] if item['isMaxPain']] == [2.9, 3.1]
    assert result['minPainOiPrice'] == pytest.approx(20.0)


def testCalculateMaxPainReturnsUnavailableForNoValidStrike() -> None:
    result = calculateMaxPain({}, {}, spot=3.00)

    assert result == {
        'available': False,
        'formula': 'Pain(K) = Σ CallOIᵢ × max(K − Kᵢ, 0) × per_unit + Σ PutOIᵢ × max(Kᵢ − K, 0) × per_unit',
        'candidateStrikes': [],
        'maxPainStrikes': [],
        'minPainOiPrice': None,
        'minPainYuan': None,
        'candidates': [],
    }


def testBuildOiWallSnapshotAttachesMaxPainAndExcludesMissingOrAdjustedOi() -> None:
    snapshot = buildOiWallSnapshot(
        [
            optionRow('C-2900', 'C', 2.90, 100),
            optionRow('C-3100', 'C', 3.10, 50),
            optionRow('C-MISSING', 'C', 3.20, None),
            optionRow('C-ADJUSTED', 'C', 2.90, 999, perUnit=10100.0),
            optionRow('P-2900', 'P', 2.90, 80),
            optionRow('P-3100', 'P', 3.10, 120),
        ],
        dataDate=DATA_DATE,
        spot=3.00,
    )

    maxPain = snapshot['expiries'][0]['maxPain']
    assert maxPain['maxPainStrikes'] == [3.1]
    assert maxPain['minPainYuan'] == pytest.approx(200000.0)
    assert snapshot['quality']['missingOiRows'] == 1
    assert snapshot['quality']['adjustedRows'] == 1
