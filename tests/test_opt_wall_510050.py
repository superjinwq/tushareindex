from __future__ import annotations

from datetime import date
import json
import logging
from pathlib import Path
import subprocess

import pytest

import opt_wall_510050 as wall
from opt_wall_510050 import buildOiWallSnapshot, createApp


DATA_DATE = date(2026, 9, 22)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def optionRow(
    tsCode: str,
    callPut: str,
    strike: float,
    maturityDate: date,
    oi: int | None,
    *,
    perUnit: float = 10000.0,
    tradeDate: date = DATA_DATE,
) -> dict[str, object]:
    return {
        'ts_code': tsCode,
        'call_put': callPut,
        'exercise_price': strike,
        'maturity_date': maturityDate,
        'oi': oi,
        'per_unit': perUnit,
        'trade_date': tradeDate,
    }


def testBuildOiWallSnapshotAggregatesAndPreservesCalculationEvidence() -> None:
    maturityDate = date(2026, 10, 28)
    rows = [
        optionRow('C-3100-A', 'C', 3.10, maturityDate, 100),
        optionRow('C-3100-B', 'C', 3.10, maturityDate, 50),
        optionRow('C-3200', 'C', 3.20, maturityDate, 150),
        optionRow('C-MISSING', 'C', 3.30, maturityDate, None),
        optionRow('C-ADJUSTED', 'C', 3.10, maturityDate, 200, perUnit=10100.0),
        optionRow('P-2900', 'P', 2.90, maturityDate, 120),
        optionRow('P-3000', 'P', 3.00, maturityDate, 40),
    ]

    snapshot = buildOiWallSnapshot(rows, dataDate=DATA_DATE, spot=3.0)

    assert snapshot['underlying'] == '510050.SH'
    assert snapshot['dataDate'] == '2026-09-22'
    expiry = snapshot['expiries'][0]

    assert expiry['call']['wallOi'] == 150
    assert expiry['call']['wallStrikes'] == [3.1, 3.2]
    assert expiry['call']['totalOi'] == 300
    assert expiry['call']['top3Share'] == pytest.approx(1.0)

    call310 = next(item for item in expiry['call']['strikes'] if item['strike'] == 3.1)
    assert call310['oi'] == 150
    assert call310['distancePct'] == pytest.approx(3.333333, abs=1e-6)
    assert call310['isWall'] is True
    assert [item['tsCode'] for item in call310['contracts']] == ['C-3100-A', 'C-3100-B']
    assert [item['oi'] for item in call310['contracts']] == [100, 50]

    assert expiry['put']['wallStrikes'] == [2.9]
    assert expiry['put']['wallOi'] == 120
    assert snapshot['quality']['optionRows'] == 7
    assert snapshot['quality']['adjustedRows'] == 1
    assert snapshot['quality']['missingOiRows'] == 1
    assert snapshot['quality']['dateAligned'] is True
    assert snapshot['quality']['adjustedContracts'][0]['underlyingUnits'] == 2_020_000


def testBuildOiWallSnapshotKeepsZeroOiAndReturnsNullForZeroDenominator() -> None:
    maturityDate = date(2026, 10, 28)
    snapshot = buildOiWallSnapshot(
        [
            optionRow('C-3000', 'C', 3.0, maturityDate, 0),
            optionRow('P-3000', 'P', 3.0, maturityDate, 10),
        ],
        dataDate=DATA_DATE,
        spot=3.0,
    )

    call = snapshot['expiries'][0]['call']
    assert call['totalOi'] == 0
    assert call['top3Share'] is None
    assert call['wallOi'] == 0
    assert call['wallStrikes'] == [3.0]


def testBuildOiWallSnapshotReturnsOnlyFourNearestUnexpiredMaturities() -> None:
    maturities = [
        date(2026, 9, 21),
        date(2026, 9, 23),
        date(2026, 10, 28),
        date(2026, 12, 23),
        date(2027, 3, 24),
        date(2027, 6, 23),
    ]
    rows = [
        optionRow(f'C-{maturity:%Y%m%d}', 'C', 3.0, maturity, 10)
        for maturity in maturities
    ]

    snapshot = buildOiWallSnapshot(rows, dataDate=DATA_DATE, spot=3.0)

    assert [item['maturityDate'] for item in snapshot['expiries']] == [
        '2026-09-23',
        '2026-10-28',
        '2026-12-23',
        '2027-03-24',
    ]


def testBuildOiWallSnapshotRejectsCrossDateRows() -> None:
    rows = [
        optionRow(
            'C-WRONG-DATE',
            'C',
            3.0,
            date(2026, 10, 28),
            10,
            tradeDate=date(2026, 9, 21),
        )
    ]

    with pytest.raises(ValueError, match='共同数据日'):
        buildOiWallSnapshot(rows, dataDate=DATA_DATE, spot=3.0)


def testStandaloneRoutesReturnPageSnapshotAndHealth() -> None:
    expectedSnapshot = {
        'underlying': '510050.SH',
        'dataDate': '2026-09-22',
        'spot': 3.015,
        'expiries': [],
        'quality': {'dateAligned': True},
    }
    app = createApp(
        snapshotLoader=lambda: expectedSnapshot,
        healthLoader=lambda: {'database': 'ok'},
    )
    client = app.test_client()

    page = client.get('/')
    assert page.status_code == 200
    pageText = page.get_data(as_text=True)
    assert '510050 独立期权墙' in pageText
    assert 'data-endpoint="/api/oi-wall"' in pageText
    assert '计算过程' in pageText
    assert '解释边界' in pageText

    response = client.get('/api/oi-wall')
    assert response.status_code == 200
    assert response.get_json() == {'ok': True, 'snapshot': expectedSnapshot}

    health = client.get('/health')
    assert health.status_code == 200
    assert health.get_json()['database'] == 'ok'


def testApiDoesNotExposeDatabaseErrorDetails(caplog: pytest.LogCaptureFixture) -> None:
    def failingLoader() -> dict[str, object]:
        raise RuntimeError('password=top-secret')

    app = createApp(snapshotLoader=failingLoader)
    with caplog.at_level(logging.ERROR):
        response = app.test_client().get('/api/oi-wall')

    assert response.status_code == 503
    body = response.get_data(as_text=True)
    assert 'top-secret' not in body
    assert 'top-secret' not in caplog.text
    assert response.get_json()['ok'] is False


def testConnectionDoesNotDuplicateConfiguredTimeoutKeywords(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    captured: dict[str, object] = {}

    def fakeConnect(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        wall,
        'DB_CONFIG_TUSHARE',
        {'host': '127.0.0.1', 'read_timeout': 7},
    )
    monkeypatch.setattr(wall.pymysql, 'connect', fakeConnect)

    assert wall._connection() is sentinel
    assert captured['read_timeout'] == 7
    assert captured['write_timeout'] == 20


def testUiHelpersSortStrikesFormatOiInWanAndKeepBothWallsSeparate() -> None:
    script = r"""
      const ui = require('./static/js/opt_wall_510050.js');
      const expiry = {
        call: {
          strikes: [
            { strike: 3.20, oi: 20000, isWall: false },
            { strike: 3.00, oi: 95320, isWall: true },
          ],
        },
        put: {
          strikes: [
            { strike: 3.10, oi: 37576, isWall: true },
            { strike: 2.90, oi: 10000, isWall: false },
          ],
        },
      };
      console.log(JSON.stringify({
        strikes: ui.collectStrikesAsc(expiry),
        formattedCallOi: ui.formatOiWan(95320),
        formattedPutOi: ui.formatOiWan(37576),
        walls: ui.collectWallSelections(expiry).map((item) => ({
          sideName: item.sideName,
          strike: item.item.strike,
        })),
      }));
    """
    result = subprocess.run(
        ['node', '-e', script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        text=True,
        encoding='utf-8',
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        'strikes': [2.9, 3, 3.1, 3.2],
        'formattedCallOi': '9.53万',
        'formattedPutOi': '3.76万',
        'walls': [
            {'sideName': 'call', 'strike': 3},
            {'sideName': 'put', 'strike': 3.1},
        ],
    }
