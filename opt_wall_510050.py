"""510050 独立 OI Wall 教学页（只读、EOD）。"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import math
import os
from typing import Any, Callable, Iterable

import pymysql
from flask import Flask, jsonify, render_template
from flask.json.provider import DefaultJSONProvider
from pymysql.cursors import DictCursor

from logger_config import get_logger
from rt_monitor.core.config import DB_CONFIG_TUSHARE


logger = get_logger(__name__)

UNDERLYING = '510050.SH'
OPTION_ROOT = 'OP510050.SH'
STANDARD_PER_UNIT = 10000.0
EXPIRY_LABELS = ('当月', '下月', '当季', '下季')


class DataUnavailableError(RuntimeError):
    """当前数据不足以生成 OI Wall。"""


def _toDate(value: object, fieldName: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        raw = value.strip()
        for fmt in ('%Y-%m-%d', '%Y%m%d'):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    raise ValueError(f'{fieldName} 不是有效日期')


def _finiteFloat(value: object, fieldName: str) -> float:
    if not isinstance(value, (int, float, Decimal, str)):
        raise ValueError(f'{fieldName} 不是有效数值')
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{fieldName} 不是有效数值') from exc
    if not math.isfinite(number):
        raise ValueError(f'{fieldName} 不是有限数值')
    return number


def _jsonNumber(value: float) -> int | float:
    if abs(value - round(value)) < 1e-9:
        return int(round(value))
    return round(value, 8)


def _sideSnapshot(
    strikeMap: dict[float, dict[str, Any]],
    *,
    spot: float,
) -> dict[str, Any]:
    if not strikeMap:
        return {
            'totalOi': 0,
            'wallOi': None,
            'wallStrikes': [],
            'top3Share': None,
            'strikes': [],
        }

    ranked = sorted(strikeMap.items(), key=lambda item: (-item[1]['oi'], item[0]))
    wallOi = ranked[0][1]['oi']
    wallStrikes = sorted(strike for strike, item in ranked if item['oi'] == wallOi)
    totalOi = sum(item['oi'] for item in strikeMap.values())
    top3Oi = sum(item['oi'] for _, item in ranked[:3])
    top3Share = None if totalOi == 0 else top3Oi / totalOi

    denseRanks: dict[float, int] = {}
    previousOi: float | None = None
    currentRank = 0
    for _, item in ranked:
        if previousOi is None or item['oi'] != previousOi:
            currentRank += 1
            previousOi = item['oi']
        denseRanks[item['oi']] = currentRank

    top3Strikes = {strike for strike, _ in ranked[:3]}
    strikes: list[dict[str, object]] = []
    for strike, item in sorted(strikeMap.items()):
        contracts = sorted(item['contracts'], key=lambda contract: contract['tsCode'])
        strikes.append(
            {
                'strike': _jsonNumber(strike),
                'oi': _jsonNumber(item['oi']),
                'distancePct': round((strike / spot - 1.0) * 100.0, 6),
                'rank': denseRanks[item['oi']],
                'isWall': strike in wallStrikes,
                'isTop3': strike in top3Strikes,
                'contracts': contracts,
            }
        )

    return {
        'totalOi': _jsonNumber(totalOi),
        'wallOi': _jsonNumber(wallOi),
        'wallStrikes': [_jsonNumber(strike) for strike in wallStrikes],
        'top3Share': None if top3Share is None else round(top3Share, 8),
        'strikes': strikes,
    }


def buildOiWallSnapshot(
    optionRows: Iterable[dict[str, object]],
    *,
    dataDate: date,
    spot: float,
) -> dict[str, Any]:
    """把同一交易日的合约行转换为可追溯的四期限 OI Wall 快照。"""
    dataDate = _toDate(dataDate, 'dataDate')
    spotValue = _finiteFloat(spot, 'spot')
    if spotValue <= 0:
        raise ValueError('spot 必须大于 0')

    rows = list(optionRows)
    parsedRows: list[tuple[dict[str, object], date]] = []
    maturitySet: set[date] = set()
    expiredRows = 0
    for row in rows:
        rowTradeDate = _toDate(row.get('trade_date'), 'trade_date')
        if rowTradeDate != dataDate:
            raise ValueError('期权行与共同数据日不一致')
        maturityDate = _toDate(row.get('maturity_date'), 'maturity_date')
        if maturityDate < dataDate:
            expiredRows += 1
            continue
        parsedRows.append((row, maturityDate))
        maturitySet.add(maturityDate)

    selectedMaturities = sorted(maturitySet)[:4]
    selectedSet = set(selectedMaturities)
    buckets: dict[date, dict[str, dict[float, dict[str, Any]]]] = {
        maturity: {'C': {}, 'P': {}} for maturity in selectedMaturities
    }

    standardRows = 0
    adjustedRows = 0
    missingOiRows = 0
    invalidRows = 0
    adjustedContracts: list[dict[str, object]] = []

    for row, maturityDate in parsedRows:
        if maturityDate not in selectedSet:
            continue
        try:
            perUnit = _finiteFloat(row.get('per_unit'), 'per_unit')
            strike = _finiteFloat(row.get('exercise_price'), 'exercise_price')
        except ValueError:
            invalidRows += 1
            continue
        if perUnit <= 0 or strike <= 0:
            invalidRows += 1
            continue

        tsCode = str(row.get('ts_code') or '').strip()
        callPut = str(row.get('call_put') or '').strip().upper()
        if not tsCode or callPut not in {'C', 'P'}:
            invalidRows += 1
            continue

        oiRaw = row.get('oi')
        if abs(perUnit - STANDARD_PER_UNIT) >= 1e-6:
            adjustedRows += 1
            adjustedOi: float | None = None
            if oiRaw is not None:
                try:
                    adjustedOi = _finiteFloat(oiRaw, 'oi')
                except ValueError:
                    adjustedOi = None
            adjustedContracts.append(
                {
                    'tsCode': tsCode,
                    'maturityDate': maturityDate.isoformat(),
                    'callPut': callPut,
                    'strike': _jsonNumber(strike),
                    'oi': None if adjustedOi is None else _jsonNumber(adjustedOi),
                    'perUnit': _jsonNumber(perUnit),
                    'underlyingUnits': (
                        None if adjustedOi is None else _jsonNumber(adjustedOi * perUnit)
                    ),
                }
            )
            continue

        standardRows += 1
        if oiRaw is None:
            missingOiRows += 1
            continue
        try:
            oi = _finiteFloat(oiRaw, 'oi')
        except ValueError:
            missingOiRows += 1
            continue
        if oi < 0:
            invalidRows += 1
            continue

        strikeBucket = buckets[maturityDate][callPut].setdefault(
            strike,
            {'oi': 0.0, 'contracts': []},
        )
        strikeBucket['oi'] += oi
        strikeBucket['contracts'].append(
            {
                'tsCode': tsCode,
                'oi': _jsonNumber(oi),
                'perUnit': _jsonNumber(perUnit),
            }
        )

    expiries: list[dict[str, object]] = []
    for index, maturityDate in enumerate(selectedMaturities):
        callSnapshot = _sideSnapshot(buckets[maturityDate]['C'], spot=spotValue)
        putSnapshot = _sideSnapshot(buckets[maturityDate]['P'], spot=spotValue)
        expiries.append(
            {
                'maturityDate': maturityDate.isoformat(),
                'expiryLabel': EXPIRY_LABELS[index],
                'dteCalendar': (maturityDate - dataDate).days,
                'totalOi': callSnapshot['totalOi'] + putSnapshot['totalOi'],
                'call': callSnapshot,
                'put': putSnapshot,
            }
        )

    warnings: list[str] = []
    if missingOiRows:
        warnings.append(f'{missingOiRows} 条标准合约 OI 缺失，已排除且未补零。')
    if adjustedRows:
        warnings.append(f'{adjustedRows} 条调整合约未并入标准合约墙位。')
    if invalidRows:
        warnings.append(f'{invalidRows} 条合约字段无效，已排除。')
    if len(selectedMaturities) < 4:
        warnings.append(f'仅找到 {len(selectedMaturities)} 个有数据的未到期期限。')

    return {
        'underlying': UNDERLYING,
        'dataDate': dataDate.isoformat(),
        'spot': _jsonNumber(spotValue),
        'spotField': 'close_qfq',
        'expiries': expiries,
        'quality': {
            'source': 'stock_tushare.opt_basic + opt_daily + etf_daily',
            'optionRows': len(rows),
            'standardRows': standardRows,
            'adjustedRows': adjustedRows,
            'missingOiRows': missingOiRows,
            'invalidRows': invalidRows,
            'expiredRows': expiredRows,
            'dateAligned': True,
            'adjustedContracts': adjustedContracts,
            'warnings': warnings,
        },
    }


def _connection() -> pymysql.connections.Connection:
    connectionConfig = dict(DB_CONFIG_TUSHARE)
    connectionConfig['cursorclass'] = DictCursor
    connectionConfig.setdefault('read_timeout', 20)
    connectionConfig.setdefault('write_timeout', 20)
    return pymysql.connect(**connectionConfig)


def _latestCommonDataDate(conn: pymysql.connections.Connection) -> date:
    sql = """
        SELECT od.trade_date AS data_date
        FROM opt_daily od
        INNER JOIN opt_basic ob ON ob.ts_code = od.ts_code
        INNER JOIN etf_daily ed
            ON ed.ts_code = %s
           AND ed.trade_date = od.trade_date
        WHERE ob.opt_code = %s
        GROUP BY od.trade_date
        ORDER BY od.trade_date DESC
        LIMIT 1
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, (UNDERLYING, OPTION_ROOT))
        row = cursor.fetchone()
    if not row or row.get('data_date') is None:
        raise DataUnavailableError('找不到期权与 ETF 的共同交易日')
    return _toDate(row['data_date'], 'data_date')


def _spotOnDate(conn: pymysql.connections.Connection, dataDate: date) -> float:
    sql = """
        SELECT close_qfq
        FROM etf_daily
        WHERE ts_code = %s AND trade_date = %s
        LIMIT 1
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, (UNDERLYING, dataDate))
        row = cursor.fetchone()
    if not row or row.get('close_qfq') is None:
        raise DataUnavailableError('共同交易日缺少 510050 close_qfq')
    spot = _finiteFloat(row['close_qfq'], 'close_qfq')
    if spot <= 0:
        raise DataUnavailableError('510050 close_qfq 无效')
    return spot


def _optionRowsOnDate(
    conn: pymysql.connections.Connection,
    dataDate: date,
) -> list[dict[str, object]]:
    maturitySql = """
        SELECT DISTINCT ob.maturity_date
        FROM opt_basic ob
        INNER JOIN opt_daily od
            ON od.ts_code = ob.ts_code
           AND od.trade_date = %s
        WHERE ob.opt_code = %s
          AND ob.call_put IN ('C', 'P')
          AND ob.maturity_date >= %s
          AND (ob.list_date IS NULL OR ob.list_date <= %s)
          AND (ob.delist_date IS NULL OR ob.delist_date >= %s)
        ORDER BY ob.maturity_date
        LIMIT 4
    """
    with conn.cursor() as cursor:
        cursor.execute(maturitySql, (dataDate, OPTION_ROOT, dataDate, dataDate, dataDate))
        maturityRows = cursor.fetchall()
    maturities = [row['maturity_date'] for row in maturityRows if row.get('maturity_date')]
    if not maturities:
        raise DataUnavailableError('共同交易日没有未到期的 510050 合约')

    placeholders = ','.join(['%s'] * len(maturities))
    optionSql = f"""
        SELECT
            ob.ts_code,
            ob.call_put,
            ob.exercise_price,
            ob.maturity_date,
            ob.per_unit,
            od.trade_date,
            od.oi,
            od.vol,
            od.close,
            od.settle
        FROM opt_basic ob
        INNER JOIN opt_daily od
            ON od.ts_code = ob.ts_code
           AND od.trade_date = %s
        WHERE ob.opt_code = %s
          AND ob.call_put IN ('C', 'P')
          AND ob.maturity_date IN ({placeholders})
          AND (ob.list_date IS NULL OR ob.list_date <= %s)
          AND (ob.delist_date IS NULL OR ob.delist_date >= %s)
        ORDER BY ob.maturity_date, ob.exercise_price, ob.call_put, ob.ts_code
    """
    params: list[object] = [dataDate, OPTION_ROOT, *maturities, dataDate, dataDate]
    with conn.cursor() as cursor:
        cursor.execute(optionSql, params)
        return list(cursor.fetchall())


def loadOiWallSnapshot(
    connectionFactory: Callable[[], pymysql.connections.Connection] = _connection,
) -> dict[str, Any]:
    conn = connectionFactory()
    try:
        dataDate = _latestCommonDataDate(conn)
        spot = _spotOnDate(conn, dataDate)
        optionRows = _optionRowsOnDate(conn, dataDate)
        return buildOiWallSnapshot(optionRows, dataDate=dataDate, spot=spot)
    finally:
        conn.close()


def loadHealth(
    connectionFactory: Callable[[], pymysql.connections.Connection] = _connection,
) -> dict[str, Any]:
    conn = connectionFactory()
    try:
        with conn.cursor() as cursor:
            cursor.execute('SELECT 1 AS ok')
            row = cursor.fetchone()
        return {'ok': True, 'service': 'opt-wall-510050', 'database': 'ok' if row else 'empty'}
    finally:
        conn.close()


def createApp(
    *,
    snapshotLoader: Callable[[], dict[str, Any]] | None = None,
    healthLoader: Callable[[], dict[str, Any]] | None = None,
) -> Flask:
    app = Flask(__name__)
    if isinstance(app.json, DefaultJSONProvider):
        app.json.ensure_ascii = False
    snapshotFn = snapshotLoader or loadOiWallSnapshot
    healthFn = healthLoader or loadHealth

    @app.get('/')
    def index() -> str:
        return render_template('opt_wall_510050.html')

    @app.get('/api/oi-wall')
    def apiOiWall():
        try:
            return jsonify({'ok': True, 'snapshot': snapshotFn()})
        except DataUnavailableError as exc:
            return jsonify({'ok': False, 'error': str(exc), 'errorCode': 'DATA_UNAVAILABLE'}), 422
        except Exception as exc:
            logger.error('510050 OI Wall 快照加载失败 error_type=%s', type(exc).__name__)
            return jsonify(
                {
                    'ok': False,
                    'error': 'OI Wall 数据暂时不可用，请查看服务日志。',
                    'errorCode': 'SNAPSHOT_LOAD_FAILED',
                }
            ), 503

    @app.get('/health')
    def health():
        try:
            return jsonify(healthFn())
        except Exception as exc:
            logger.error('510050 OI Wall 健康检查失败 error_type=%s', type(exc).__name__)
            return jsonify(
                {
                    'ok': False,
                    'service': 'opt-wall-510050',
                    'database': 'unavailable',
                }
            ), 503

    return app


app = createApp()


if __name__ == '__main__':
    host = os.getenv('OPT_WALL_HOST', '127.0.0.1')
    port = int(os.getenv('OPT_WALL_PORT', '5011'))
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
