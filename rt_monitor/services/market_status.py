"""marketinfo 市场状态只读查询。"""
from __future__ import annotations

import json
import os
import threading

import pymysql
from logger_config import get_logger

from rt_monitor.core.config import DB_CONFIG_TUSHARE
from rt_monitor.core.numbers import toFloatOrNone

logger = get_logger(__name__)

# 国证A指，HisHC 弹窗右轴
IDX_TS_CODE_GZAZ = '399317.SZ'


def normalizeMarketinfoRecentLimit(limit) -> int:
    """marketinfo_recent 仅允许 0（全量）、300、1300；其它值回退 300。"""
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        return 300
    if lim in (0, 300, 1300):
        return lim
    return 300


def queryMarketinfoRecent(limit: int = 300, conn=None):
    """只读 marketinfo：基础列 + 超跌/涨跌停 + HHLL/CCCC + HisHC/LC；
    LEFT JOIN index_daily(399317.SZ).close 供 HisHC 弹窗右轴。
    limit=0 全量，否则 trade_date DESC LIMIT n。
    """
    lim = normalizeMarketinfoRecentLimit(limit)
    ownConn = conn is None
    if ownConn:
        try:
            conn = pymysql.connect(**DB_CONFIG_TUSHARE)
        except Exception as e:
            logger.exception("连接 stock_tushare 失败（marketinfo_recent）: %s", e)
            return {'rows': [], 'limit': lim, 'error': '数据库连接失败'}
    sql = (
        "SELECT m.trade_date, m.amount, m.up_num, m.down_num, m.up_pct, m.total_num, m.med_pct_chg, "
        "m.up_num_ma5, m.high_limit, m.high_limit_lifting, m.high_limit_pct, m.low_limit, m.low_limit_lifting, "
        "m.HL5_HHLL, m.HL10_HHLL, m.HL20_HHLL, m.HL60_HHLL, m.HL120_HHLL, m.HL200_HHLL, m.HL250_HHLL, "
        "m.HL850_HHLL, m.HLhis_HHLL, "
        "m.HL5_CCCC, m.HL10_CCCC, m.HL20_CCCC, m.HL60_CCCC, m.HL120_CCCC, m.HL200_CCCC, m.HL250_CCCC, "
        "m.HL850_CCCC, m.HLhis_CCCC, "
        "m.total_mv, m.circ_mv, m.free_mv, m.amplitude_399317, m.amount_top5_pct, "
        "m.HLhis_CH, m.HLhis_CL, i.close AS idx_close "
        "FROM marketinfo m "
        "LEFT JOIN index_daily i "
        "  ON i.trade_date = m.trade_date AND i.ts_code = %s "
        "ORDER BY m.trade_date DESC"
    )
    params: list = [IDX_TS_CODE_GZAZ]
    if lim > 0:
        sql += " LIMIT %s"
        params.append(lim)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = cur.fetchall()
        rows = []
        for r in raw or []:
            td = r[0]
            tdStr = td.isoformat() if hasattr(td, 'isoformat') else str(td)
            amountV = r[1]
            upNumV = r[2]
            downNumV = r[3]
            totalNumV = r[5]
            rows.append({
                'trade_date': tdStr,
                'amount': int(amountV) if amountV is not None else None,
                'up_num': int(upNumV) if upNumV is not None else None,
                'down_num': int(downNumV) if downNumV is not None else None,
                'up_pct': toFloatOrNone(r[4]),
                'total_num': int(totalNumV) if totalNumV is not None else None,
                'med_pct_chg': toFloatOrNone(r[6]),
                'up_num_ma5': toFloatOrNone(r[7]),
                'high_limit': int(r[8]) if r[8] is not None else None,
                'high_limit_lifting': int(r[9]) if r[9] is not None else None,
                'high_limit_pct': toFloatOrNone(r[10]),
                'low_limit': int(r[11]) if r[11] is not None else None,
                'low_limit_lifting': int(r[12]) if r[12] is not None else None,
                'hl5_hhll': toFloatOrNone(r[13]),
                'hl10_hhll': toFloatOrNone(r[14]),
                'hl20_hhll': toFloatOrNone(r[15]),
                'hl60_hhll': toFloatOrNone(r[16]),
                'hl120_hhll': toFloatOrNone(r[17]),
                'hl200_hhll': toFloatOrNone(r[18]),
                'hl250_hhll': toFloatOrNone(r[19]),
                'hl850_hhll': toFloatOrNone(r[20]),
                'hlhis_hhll': toFloatOrNone(r[21]),
                'hl5_cccc': toFloatOrNone(r[22]),
                'hl10_cccc': toFloatOrNone(r[23]),
                'hl20_cccc': toFloatOrNone(r[24]),
                'hl60_cccc': toFloatOrNone(r[25]),
                'hl120_cccc': toFloatOrNone(r[26]),
                'hl200_cccc': toFloatOrNone(r[27]),
                'hl250_cccc': toFloatOrNone(r[28]),
                'hl850_cccc': toFloatOrNone(r[29]),
                'hlhis_cccc': toFloatOrNone(r[30]),
                'total_mv_wan': (round(float(r[31]) / 10000.0, 2) if r[31] is not None else None),
                'circ_mv_wan': (round(float(r[32]) / 10000.0, 2) if r[32] is not None else None),
                'free_mv_wan': (round(float(r[33]) / 10000.0, 2) if r[33] is not None else None),
                'amplitude': toFloatOrNone(r[34]),
                'amount_top5_pct': toFloatOrNone(r[35]),
                'hlhis_ch': int(r[36]) if len(r) > 36 and r[36] is not None else None,
                'hlhis_cl': int(r[37]) if len(r) > 37 and r[37] is not None else None,
                'idx_close': toFloatOrNone(r[38]) if len(r) > 38 else None,
            })
        return {'rows': rows, 'limit': lim, 'error': None}
    except Exception as e:
        logger.exception("查询 marketinfo_recent 失败: %s", e)
        return {'rows': [], 'limit': lim, 'error': str(e)}
    finally:
        if ownConn and conn is not None:
            conn.close()


DATA_HEALTH_LATEST_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'output', 'data_health', 'latest.json')
_dataHealthCache: dict = {'mtime': None, 'payload': None}
_dataHealthLock = threading.Lock()


def readDataHealthLatest(path: str = DATA_HEALTH_LATEST_JSON) -> dict:
    """读 data_health_check.py 写出的最近一次体检 JSON（按 mtime 缓存，不查库）；只返回摘要与需关注项。"""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {'available': False, 'error': '尚无体检结果，请先运行 data_health_check.py'}
    with _dataHealthLock:
        if _dataHealthCache['mtime'] == mtime and _dataHealthCache['payload'] is not None:
            return _dataHealthCache['payload']
    try:
        with open(path, encoding='utf-8') as fh:
            report = json.load(fh)
    except (OSError, ValueError) as e:
        return {'available': False, 'error': f'体检结果读取失败: {e}'}
    attention = [
        {'table': f"{r.get('schema')}.{r.get('table')}", 'status': r.get('status'),
         'page': r.get('page', ''), 'brief': r.get('brief', '')}
        for r in report.get('results', []) if r.get('status') not in ('OK', 'INFO')
    ]
    payload = {
        'available': True, 'error': None,
        'generatedAt': report.get('generatedAt'), 'overall': report.get('overall'),
        'counts': report.get('counts'), 'tables': report.get('tables'),
        'elapsedSec': report.get('elapsedSec'), 'expectedDaily': report.get('expectedDaily'),
        'expectedPeriod': report.get('expectedPeriod'), 'attention': attention,
    }
    with _dataHealthLock:
        _dataHealthCache['mtime'] = mtime
        _dataHealthCache['payload'] = payload
    return payload


query_marketinfo_recent = queryMarketinfoRecent
normalize_marketinfo_recent_limit = normalizeMarketinfoRecentLimit

# 代码更新时间：2026-07-20 18:06
# 代码更新时间：2026-10-04 21:22 新增 /api/data_health_latest：读 output/data_health/latest.json（按 mtime 缓存，不查库）
