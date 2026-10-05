"""五 ETF 独立期权墙历史回看页（只读、EOD）。"""
from __future__ import annotations

from datetime import date
import os
from typing import Any, Callable, Iterable

import pymysql
from flask import Flask, jsonify, render_template, request
from flask.json.provider import DefaultJSONProvider

from logger_config import get_logger
from opt_wall_510050 import (
    FLOW_HORIZON_DAYS,
    DataUnavailableError,
    _commonDataDates,
    _connection,
    _flowRowsOnDates,
    _ivRowsOnDate,
    _optionRowsOnDate,
    _spotOnDate,
    _toDate,
    buildFlowWallSnapshot,
    buildOiWallSnapshot,
    loadHealth,
    resolveUnderlying,
    underlyingOptions,
)


logger = get_logger(__name__)
DEFAULT_UNDERLYING_CODE = '510050'
HISTORY_DATE_LIMIT = 120


def _selectHistoricalDate(
    availableDates: list[date],
    requestedDate: object = None,
) -> date:
    """从共同数据日中选择历史快照日期，禁止回退到跨日数据。"""
    if not availableDates:
        raise DataUnavailableError('找不到期权与 ETF 的共同交易日')
    if requestedDate is None or not str(requestedDate).strip():
        return availableDates[0]
    try:
        selectedDate = _toDate(requestedDate, '历史日期')
    except ValueError as exc:
        raise DataUnavailableError('历史日期格式无效，应为 YYYY-MM-DD') from exc
    if selectedDate not in availableDates:
        raise DataUnavailableError(
            f'历史日期 {selectedDate.isoformat()} 没有可用的期权与 ETF 共同数据'
        )
    return selectedDate


def _baselineDates(
    availableDates: list[date],
    selectedDate: date,
) -> dict[str, date | None]:
    """按共同交易日序列定位 1D/3D/5D Flow 基线。"""
    selectedIndex = availableDates.index(selectedDate)
    return {
        horizon: (
            availableDates[selectedIndex + days]
            if selectedIndex + days < len(availableDates)
            else None
        )
        for horizon, days in FLOW_HORIZON_DAYS.items()
    }


def _maturityDates(optionRows: Iterable[dict[str, object]], dataDate: date) -> list[date]:
    dates = {
        _toDate(row.get('maturity_date'), 'maturity_date')
        for row in optionRows
        if _toDate(row.get('maturity_date'), 'maturity_date') >= dataDate
    }
    return sorted(dates)[:4]


def loadHistoricalSnapshot(
    underlyingCode: str = DEFAULT_UNDERLYING_CODE,
    requestedDate: object = None,
    connectionFactory: Callable[[], pymysql.connections.Connection] = _connection,
    dateLimit: int = HISTORY_DATE_LIMIT,
) -> dict[str, Any]:
    """加载指定共同交易日的 OI、Max Pain、Gamma 和 Flow 历史快照。"""
    if dateLimit <= 0:
        raise ValueError('历史共同交易日数量必须大于 0')
    underlyingConfig = resolveUnderlying(underlyingCode)
    conn = connectionFactory()
    try:
        availableDates = _commonDataDates(
            conn,
            underlying=underlyingConfig.code,
            optionRoot=underlyingConfig.optionRoot,
            limit=dateLimit,
        )
        dataDate = _selectHistoricalDate(availableDates, requestedDate)
        selectedIndex = availableDates.index(dataDate)
        spot = _spotOnDate(conn, dataDate, underlying=underlyingConfig.code)
        optionRows = _optionRowsOnDate(
            conn,
            dataDate,
            optionRoot=underlyingConfig.optionRoot,
        )
        maturityDates = _maturityDates(optionRows, dataDate)
        ivRows = _ivRowsOnDate(
            conn,
            dataDate,
            maturityDates,
            underlying=underlyingConfig.code,
        )
        snapshot = buildOiWallSnapshot(
            optionRows,
            dataDate=dataDate,
            spot=spot,
            underlying=underlyingConfig.code,
            ivRows=ivRows,
        )

        baselineDates = _baselineDates(availableDates, dataDate)
        flowDates = [
            dataDate,
            *[baseline for baseline in baselineDates.values() if baseline is not None],
        ]
        flowRows = _flowRowsOnDates(
            conn,
            flowDates,
            maturityDates,
            optionRoot=underlyingConfig.optionRoot,
        )
        snapshot['flow'] = buildFlowWallSnapshot(
            flowRows,
            dataDate=dataDate,
            spot=spot,
            baselineDates=baselineDates,
        )
        snapshot['underlyingCode'] = underlyingConfig.code.rsplit('.', maxsplit=1)[0]
        snapshot['underlyingLabel'] = underlyingConfig.label
        snapshot['optionRoot'] = underlyingConfig.optionRoot
        snapshot['history'] = {
            'selectedDate': dataDate.isoformat(),
            'availableDates': [item.isoformat() for item in availableDates],
            'availableDateCount': len(availableDates),
            'selectedDateIndex': selectedIndex,
            'baselineDates': {
                horizon: None if value is None else value.isoformat()
                for horizon, value in baselineDates.items()
            },
            'nextNewerDate': (
                None
                if selectedIndex == 0
                else availableDates[selectedIndex - 1].isoformat()
            ),
        }
        return snapshot
    finally:
        conn.close()


def loadHistoryHealth(
    connectionFactory: Callable[[], pymysql.connections.Connection] = _connection,
) -> dict[str, Any]:
    """复用只读数据库探测，但把服务名标成历史回看服务。"""
    health = loadHealth(connectionFactory)
    health['service'] = 'opt-wall-history'
    return health


def createApp(
    *,
    snapshotLoader: Callable[[str, object], dict[str, Any]] | None = None,
    healthLoader: Callable[[], dict[str, Any]] | None = None,
) -> Flask:
    app = Flask(__name__)
    if isinstance(app.json, DefaultJSONProvider):
        app.json.ensure_ascii = False
    snapshotFn = snapshotLoader or loadHistoricalSnapshot
    healthFn = healthLoader or loadHistoryHealth

    @app.get('/')
    def index() -> str:
        try:
            selectedConfig = resolveUnderlying(request.args.get('underlying'))
        except ValueError:
            selectedConfig = resolveUnderlying()
        selectedCode = selectedConfig.code.rsplit('.', maxsplit=1)[0]
        return render_template(
            'opt_wall_history.html',
            underlyingOptions=underlyingOptions(),
            selectedUnderlying=selectedCode,
            selectedUnderlyingLabel=selectedConfig.label,
        )

    @app.get('/api/history')
    def apiHistory():
        rawUnderlying = request.args.get('underlying') or DEFAULT_UNDERLYING_CODE
        try:
            selectedConfig = resolveUnderlying(rawUnderlying)
        except ValueError:
            rawCode = str(rawUnderlying).strip().upper()
            if rawCode.endswith('.SH') or rawCode.endswith('.SZ'):
                rawCode = rawCode[:-3]
            return jsonify(
                {
                    'ok': False,
                    'error': f'不支持的 ETF 品种：{rawCode}',
                    'errorCode': 'INVALID_UNDERLYING',
                }
            ), 400

        try:
            snapshot = snapshotFn(
                selectedConfig.code,
                request.args.get('date'),
            )
            return jsonify({'ok': True, 'snapshot': snapshot})
        except DataUnavailableError as exc:
            return jsonify(
                {
                    'ok': False,
                    'error': str(exc),
                    'errorCode': 'DATA_UNAVAILABLE',
                }
            ), 422
        except Exception as exc:
            logger.error(
                '%s 历史期权墙加载失败 error_type=%s',
                selectedConfig.code,
                type(exc).__name__,
            )
            return jsonify(
                {
                    'ok': False,
                    'error': '历史期权墙数据暂时不可用，请查看服务日志。',
                    'errorCode': 'SNAPSHOT_LOAD_FAILED',
                }
            ), 503

    @app.get('/health')
    def health():
        try:
            return jsonify(healthFn())
        except Exception as exc:
            logger.error(
                '历史期权墙健康检查失败 error_type=%s',
                type(exc).__name__,
            )
            return jsonify(
                {
                    'ok': False,
                    'service': 'opt-wall-history',
                    'database': 'unavailable',
                }
            ), 503

    return app


app = createApp()


if __name__ == '__main__':
    host = os.getenv('OPT_WALL_HISTORY_HOST', '127.0.0.1')
    port = int(os.getenv('OPT_WALL_HISTORY_PORT', '5012'))
    app.run(host=host, port=port, debug=False, use_reloader=False)


# 代码更新时间：2026-09-25 14:49
