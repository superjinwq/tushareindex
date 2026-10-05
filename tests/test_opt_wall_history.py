from __future__ import annotations

from datetime import date
import logging

import pytest

import opt_wall_history as history
from opt_wall_history import (
    _baselineDates,
    _selectHistoricalDate,
    createApp,
    loadHistoricalSnapshot,
)


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def optionRow(
    tsCode: str,
    callPut: str,
    strike: float,
    maturityDate: date,
    oi: int,
    tradeDate: date,
) -> dict[str, object]:
    return {
        'ts_code': tsCode,
        'call_put': callPut,
        'exercise_price': strike,
        'maturity_date': maturityDate,
        'per_unit': 10000,
        'trade_date': tradeDate,
        'oi': oi,
    }


def testSelectHistoricalDateDefaultsToNewestAndRejectsUnavailableDate() -> None:
    dates = [date(2026, 9, 24), date(2026, 9, 23)]

    assert _selectHistoricalDate(dates) == date(2026, 9, 24)
    assert _selectHistoricalDate(dates, '2026-09-23') == date(2026, 9, 23)

    with pytest.raises(history.DataUnavailableError, match='没有可用'):
        _selectHistoricalDate(dates, '2026-09-22')


def testBaselineDatesUseAvailableCommonTradingDaysWithoutCalendarSubstitution() -> None:
    dates = [
        date(2026, 9, 24),
        date(2026, 9, 23),
        date(2026, 9, 22),
        date(2026, 9, 21),
    ]

    assert _baselineDates(dates, date(2026, 9, 22)) == {
        '1D': date(2026, 9, 21),
        '3D': None,
        '5D': None,
    }


def testLoadHistoricalSnapshotBuildsSameDateOiAndFlow(monkeypatch: pytest.MonkeyPatch) -> None:
    selectedDate = date(2026, 9, 22)
    maturityDate = date(2026, 10, 28)
    availableDates = [
        date(2026, 9, 24),
        date(2026, 9, 23),
        selectedDate,
        date(2026, 9, 21),
        date(2026, 9, 19),
        date(2026, 9, 18),
    ]
    currentRows = [
        optionRow('C-3000', 'C', 3.0, maturityDate, 120, selectedDate),
        optionRow('P-2900', 'P', 2.9, maturityDate, 80, selectedDate),
    ]
    baselineRows = [
        optionRow('C-3000', 'C', 3.0, maturityDate, 100, date(2026, 9, 21)),
        optionRow('P-2900', 'P', 2.9, maturityDate, 60, date(2026, 9, 21)),
    ]
    flowDates: list[date] = []

    monkeypatch.setattr(history, '_commonDataDates', lambda *args, **kwargs: availableDates)
    monkeypatch.setattr(history, '_spotOnDate', lambda *args, **kwargs: 3.0)
    monkeypatch.setattr(history, '_optionRowsOnDate', lambda *args, **kwargs: currentRows)
    monkeypatch.setattr(history, '_ivRowsOnDate', lambda *args, **kwargs: [])

    def fakeFlowRows(_conn, dates, _maturityDates, **_kwargs):
        flowDates.extend(dates)
        return [*currentRows, *baselineRows]

    monkeypatch.setattr(history, '_flowRowsOnDates', fakeFlowRows)

    connection = FakeConnection()
    snapshot = loadHistoricalSnapshot(
        '510050',
        '2026-09-22',
        connectionFactory=lambda: connection,
    )

    assert connection.closed is True
    assert snapshot['dataDate'] == '2026-09-22'
    assert snapshot['spot'] == 3
    assert snapshot['expiries'][0]['call']['wallStrikes'] == [3]
    assert snapshot['expiries'][0]['put']['wallStrikes'] == [2.9]
    assert snapshot['history']['selectedDate'] == '2026-09-22'
    assert snapshot['history']['baselineDates'] == {
        '1D': '2026-09-21',
        '3D': '2026-09-18',
        '5D': None,
    }
    assert flowDates == [
        selectedDate,
        date(2026, 9, 21),
        date(2026, 9, 18),
    ]
    assert snapshot['flow']['horizons']['1D']['baselineDate'] == '2026-09-21'
    assert snapshot['flow']['horizons']['1D']['expiries'][0]['call']['positiveWallOi'] == 20


def testStandaloneRoutesExposeHistoryPageAndRejectUnknownUnderlying() -> None:
    expectedSnapshot = {
        'underlyingCode': '510050',
        'underlyingLabel': '上证50ETF',
        'dataDate': '2026-09-22',
        'spot': 3.015,
        'expiries': [],
        'quality': {'source': 'test'},
        'history': {'availableDates': ['2026-09-22'], 'selectedDate': '2026-09-22'},
    }
    requested: list[tuple[str, object]] = []
    app = createApp(
        snapshotLoader=lambda underlying, selectedDate: requested.append(
            (underlying, selectedDate)
        ) or expectedSnapshot,
        healthLoader=lambda: {'ok': True, 'service': 'opt-wall-history', 'database': 'ok'},
    )
    client = app.test_client()

    page = client.get('/')
    assert page.status_code == 200
    pageText = page.get_data(as_text=True)
    assert '历史期权墙' in pageText
    assert 'Call / Put OI Wall' in pageText
    assert 'Top 1 集中度' in pageText
    assert 'Call / Put 比值' in pageText
    assert '1D / 3D / 5D Flow Wall' in pageText
    assert 'OI Wall = arg max' in pageText
    assert "if (value === null || value === undefined || value === '') return null;" in pageText
    assert 'function fmtVelocityShare(value)' in pageText
    assert all(code in pageText for code in ('510050', '510300', '510500', '588000', '159915'))

    response = client.get('/api/history?underlying=510050&date=2026-09-22')
    assert response.status_code == 200
    assert requested == [('510050.SH', '2026-09-22')]
    assert response.get_json() == {'ok': True, 'snapshot': expectedSnapshot}

    invalid = client.get('/api/history?underlying=000300')
    assert invalid.status_code == 400
    assert invalid.get_json()['errorCode'] == 'INVALID_UNDERLYING'

    health = client.get('/health')
    assert health.status_code == 200
    assert health.get_json()['service'] == 'opt-wall-history'


def testApiDoesNotExposeHistoryLoaderErrorDetails(caplog: pytest.LogCaptureFixture) -> None:
    def failingLoader(_underlying: str, _selectedDate: object) -> dict[str, object]:
        raise RuntimeError('password=history-secret')

    app = createApp(snapshotLoader=failingLoader)
    with caplog.at_level(logging.ERROR):
        response = app.test_client().get('/api/history')

    assert response.status_code == 503
    assert 'history-secret' not in response.get_data(as_text=True)
    assert 'history-secret' not in caplog.text
    assert response.get_json()['ok'] is False


# 代码更新时间：2026-09-25 15:01 L068 历史 Flow 缺失值语义守卫
