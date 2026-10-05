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
    assert 'data-selected-underlying="510050"' in pageText
    assert all(code in pageText for code in ('510050', '510300', '510500', '588000', '159915'))
    assert '计算过程' in pageText
    assert '解释边界' in pageText
    assert '日均流速占当前同侧OI' in pageText
    assert '日均流速 ÷ 当前期限同侧总OI' in pageText
    assert 'function buildFlowTraceInstance(horizon, flowExpiry, sideName, item, currentExpiry, dynamicsExpiry = null)' in pageText
    assert 'const currentTotalOi = currentExpiry?.[sideName]?.totalOi ?? null;' in pageText
    assert 'horizon.baselineDate} → ${state.snapshot.dataDate} · ${flowExpiry.maturityDate}' in pageText
    assert 'formatOiWan(currentTotalOi)} = ${velocityShareText}' in pageText
    assert 'renderFlowTrace(flowExpiry, horizon, selections = null)' in pageText
    assert 'buildFlowTraceInstance(horizon, flowExpiry, sideName, item, currentExpiry, dynamicsExpiry)' in pageText
    assert 'Flow Acceleration' in pageText
    assert 'FlowAcceleration = FlowVelocity1D - FlowVelocity3D' in pageText
    assert 'function renderFlowDynamics(expiry)' in pageText
    assert 'flowAccelerationRatio' in pageText
    assert 'Max Pain' in pageText
    assert 'function renderMaxPain(expiry)' in pageText
    assert 'Pain(K) = Σ CallOIᵢ × max(K − Kᵢ, 0) × per_unit' in pageText
    assert 'Absolute Gamma Wall' in pageText
    assert 'function renderGammaWall(expiry)' in pageText
    assert 'GammaExposure(K, side)' in pageText
    assert 'Absolute Gamma Zone' in pageText
    assert 'Gamma Flip 代理' in pageText
    assert 'Call |GEX| − Put |GEX|' in pageText
    assert 'Call−Put 代理' in pageText
    assert 'formatGammaExposure,' in pageText

    selectedPage = client.get('/?underlying=159915.SZ')
    assert selectedPage.status_code == 200
    assert 'data-selected-underlying="159915"' in selectedPage.get_data(as_text=True)

    response = client.get('/api/oi-wall')
    assert response.status_code == 200
    assert response.get_json() == {'ok': True, 'snapshot': expectedSnapshot}

    health = client.get('/health')
    assert health.status_code == 200
    assert health.get_json()['database'] == 'ok'


def testSupportedUnderlyingCatalogResolvesFiveEtfOptionChains() -> None:
    assert wall.SUPPORTED_UNDERLYINGS == (
        '510050',
        '510300',
        '510500',
        '588000',
        '159915',
    )
    assert wall.resolveUnderlying('510300').optionRoot == 'OP510300.SH'
    assert wall.resolveUnderlying('159915.SZ').code == '159915.SZ'
    with pytest.raises(ValueError, match='不支持的 ETF 品种'):
        wall.resolveUnderlying('000300')


def testUiHelpersFormatMaxPainInReadableUnits() -> None:
    script = r"""
      const ui = require('./static/js/opt_wall_510050.js');
      console.log(JSON.stringify({
        thousandWan: ui.formatPainYuan(340000),
        hundredMillion: ui.formatPainYuan(200000000),
        zero: ui.formatPainYuan(0),
        unavailable: ui.formatPainYuan(null),
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
        'thousandWan': '34.00万元',
        'hundredMillion': '2.00亿元',
        'zero': '0.00元',
        'unavailable': '不可计算',
    }


def testApiUsesRequestedUnderlyingAndRejectsUnknownCode() -> None:
    requested: list[str] = []
    expectedSnapshot = {
        'underlying': '510300.SH',
        'dataDate': '2026-09-22',
        'spot': 4.0,
        'expiries': [],
        'quality': {'dateAligned': True},
    }
    app = createApp(
        snapshotLoaderByUnderlying=lambda underlyingCode: requested.append(underlyingCode) or expectedSnapshot,
        healthLoader=lambda: {'database': 'ok'},
    )
    client = app.test_client()

    response = client.get('/api/oi-wall?underlying=510300')

    assert response.status_code == 200
    assert requested == ['510300.SH']
    assert response.get_json() == {'ok': True, 'snapshot': expectedSnapshot}

    invalid = client.get('/api/oi-wall?underlying=000300')
    assert invalid.status_code == 400
    assert invalid.get_json() == {
        'ok': False,
        'error': '不支持的 ETF 品种：000300',
        'errorCode': 'INVALID_UNDERLYING',
    }


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
          wallStrikes: [3.00],
          wallOi: 95320,
          totalOi: 120000,
          top3Share: 0.8123,
          strikes: [
            { strike: 3.20, oi: 20000, isWall: false },
            { strike: 3.00, oi: 95320, isWall: true, isTop3: true },
          ],
        },
        put: {
          wallStrikes: [3.10],
          wallOi: 37576,
          totalOi: 80000,
          top3Share: 0.6543,
          strikes: [
            { strike: 3.10, oi: 37576, isWall: true, isTop3: true },
            { strike: 2.90, oi: 10000, isWall: false },
          ],
        },
      };
      console.log(JSON.stringify({
        strikes: ui.collectStrikesAsc(expiry),
        formattedCallOi: ui.formatOiWan(95320),
        formattedPutOi: ui.formatOiWan(37576),
        formattedGammaExposure: ui.formatGammaExposure(1234567),
        formattedUnavailableGammaExposure: ui.formatGammaExposure(null),
        formattedIv: ui.formatIvPct(0.172345),
        formattedUnavailableIv: ui.formatIvPct(null),
        formattedPositiveFlow: ui.formatFlowWan(12500),
        formattedNegativeFlow: ui.formatFlowWan(-6000),
        formattedFlowVelocity: ui.formatFlowVelocityWan(12500 / 3),
        formattedFlowAcceleration: ui.formatFlowAccelerationWan(12500 / 3),
        formattedPositiveAccelerationRatio: ui.formatFlowAccelerationRatio(50 / (80 / 3)),
        formattedNegativeAccelerationRatio: ui.formatFlowAccelerationRatio(-1.5),
        formattedUnavailableAccelerationRatio: ui.formatFlowAccelerationRatio(null),
        formattedSmallPositiveVelocity: ui.formatFlowVelocityWan(33.333333),
        formattedSmallNegativeVelocity: ui.formatFlowVelocityWan(-33.333333),
        formattedPositiveVelocityShare: ui.formatFlowVelocityShare(12500 / 3, 200000),
        formattedNegativeVelocityShare: ui.formatFlowVelocityShare(-6000 / 3, 200000),
        formattedSmallPositiveVelocityShare: ui.formatFlowVelocityShare(1, 200000),
        formattedFlowShare: ui.formatFlowShare(0.625),
        formattedPositiveDistance: ui.formatDistancePct(0.2179),
        formattedNegativeDistance: ui.formatDistancePct(-3.3333),
        formattedUnavailableDistance: ui.formatDistancePct(null),
        balance: ui.summarizeBalance(expiry),
        summaries: ['call', 'put'].map((sideName) => ui.summarizeSide(expiry[sideName])),
        flow: ui.summarizeFlowSide({
          positiveWallOi: 12500,
          positiveWallStrikes: [3.10],
          positiveFlowTotalOi: 20000,
          positiveWallConcentration: 0.625,
          positiveWallVelocityOi: 12500 / 3,
          negativeWallOi: -6000,
          negativeWallStrikes: [2.90, 3.00],
          negativeFlowTotalOi: -10000,
          negativeWallConcentration: 0.6,
          negativeWallVelocityOi: -6000 / 3,
          strikes: [],
        }, 200000),
        emptyFlow: ui.summarizeFlowSide({
          positiveWallOi: null,
          positiveWallStrikes: [],
          positiveFlowTotalOi: 0,
          positiveWallConcentration: null,
          negativeWallOi: null,
          negativeWallStrikes: [],
          negativeFlowTotalOi: 0,
          negativeWallConcentration: null,
          strikes: [],
        }),
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
        'formattedGammaExposure': '123.46万元/1%',
        'formattedUnavailableGammaExposure': '不可计算',
        'formattedIv': '17.23%',
        'formattedUnavailableIv': '不可计算',
        'formattedPositiveFlow': '+1.25万',
        'formattedNegativeFlow': '-0.60万',
        'formattedFlowVelocity': '+0.42万/日',
        'formattedFlowAcceleration': '+0.42万/日',
        'formattedPositiveAccelerationRatio': '+1.88×',
        'formattedNegativeAccelerationRatio': '-1.50×',
        'formattedUnavailableAccelerationRatio': '不可计算',
        'formattedSmallPositiveVelocity': '+<0.01万/日',
        'formattedSmallNegativeVelocity': '-<0.01万/日',
        'formattedPositiveVelocityShare': '+2.08%/日',
        'formattedNegativeVelocityShare': '-1.00%/日',
        'formattedSmallPositiveVelocityShare': '+<0.01%/日',
        'formattedFlowShare': '62.50%',
        'formattedPositiveDistance': '+0.22%',
        'formattedNegativeDistance': '-3.33%',
        'formattedUnavailableDistance': '不可计算',
        'balance': {
            'callOi': 120000,
            'putOi': 80000,
            'totalOi': 200000,
            'callPutRatio': 1.5,
            'callShare': 0.6,
            'putShare': 0.4,
        },
        'summaries': [
            {
                'wallStrikes': [3],
                'wallOi': 95320,
                'top1Share': 0.7943333333333333,
                'top3Strikes': [3],
                'top3Share': 0.8123,
            },
            {
                'wallStrikes': [3.1],
                'wallOi': 37576,
                'top1Share': 0.4697,
                'top3Strikes': [3.1],
                'top3Share': 0.6543,
          },
        ],
        'flow': {
          'positiveWallOi': 12500,
          'positiveWallStrikes': [3.1],
          'positiveFlowTotalOi': 20000,
          'positiveWallConcentration': 0.625,
          'positiveWallVelocityOi': 12500 / 3,
          'positiveWallVelocityLabel': '+0.42万/日',
          'positiveWallVelocityShare': (12500 / 3) / 200000,
          'positiveWallVelocityShareLabel': '+2.08%/日',
          'positiveConcentrationLabel': '62.50%',
          'negativeWallOi': -6000,
          'negativeWallStrikes': [2.9, 3],
          'negativeFlowTotalOi': -10000,
          'negativeWallConcentration': 0.6,
          'negativeWallVelocityOi': -6000 / 3,
          'negativeWallVelocityLabel': '-0.20万/日',
          'negativeWallVelocityShare': (-6000 / 3) / 200000,
          'negativeWallVelocityShareLabel': '-1.00%/日',
          'negativeConcentrationLabel': '60.00%',
          'positiveWallLabel': '+1.25万',
          'negativeWallLabel': '-0.60万',
        },
        'emptyFlow': {
          'positiveFlowTotalOi': 0,
          'positiveWallOi': None,
          'positiveWallStrikes': [],
          'positiveWallConcentration': None,
          'positiveWallVelocityOi': None,
          'positiveWallVelocityLabel': '不可计算',
          'positiveWallVelocityShare': None,
          'positiveWallVelocityShareLabel': '不可计算',
          'positiveConcentrationLabel': '不可计算',
          'negativeFlowTotalOi': 0,
          'negativeWallOi': None,
          'negativeWallStrikes': [],
          'negativeWallConcentration': None,
          'negativeWallVelocityOi': None,
          'negativeWallVelocityLabel': '不可计算',
          'negativeWallVelocityShare': None,
          'negativeWallVelocityShareLabel': '不可计算',
          'negativeConcentrationLabel': '不可计算',
          'positiveWallLabel': '—',
          'negativeWallLabel': '—',
        },
        'walls': [
            {'sideName': 'call', 'strike': 3},
            {'sideName': 'put', 'strike': 3.1},
        ],
    }
