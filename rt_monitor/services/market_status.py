"""marketinfo 市场状态只读查询。"""
from __future__ import annotations

import pymysql
from logger_config import get_logger

from rt_monitor.core.config import DB_CONFIG_TUSHARE
from rt_monitor.core.numbers import toFloatOrNone

logger = get_logger(__name__)


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
    """只读 marketinfo：基础七列 + 超跌/涨跌停/封板率；limit=0 全量，否则 trade_date DESC LIMIT n。"""
    lim = normalizeMarketinfoRecentLimit(limit)
    ownConn = conn is None
    if ownConn:
        try:
            conn = pymysql.connect(**DB_CONFIG_TUSHARE)
        except Exception as e:
            logger.exception("连接 stock_tushare 失败（marketinfo_recent）: %s", e)
            return {'rows': [], 'limit': lim, 'error': '数据库连接失败'}
    sql = (
        "SELECT trade_date, amount, up_num, down_num, up_pct, total_num, med_pct_chg, "
        "up_num_ma5, high_limit, high_limit_lifting, high_limit_pct, low_limit, low_limit_lifting, "
        "HL5_HHLL, HL10_HHLL, HL20_HHLL, HL60_HHLL, HL120_HHLL, HL200_HHLL, HL250_HHLL, HL850_HHLL, HLhis_HHLL, "
        "HL5_CCCC, HL10_CCCC, HL20_CCCC, HL60_CCCC, HL120_CCCC, HL200_CCCC, HL250_CCCC, HL850_CCCC, HLhis_CCCC, "
        "total_mv, circ_mv, free_mv, amplitude_399317, amount_top5_pct "
        "FROM marketinfo ORDER BY trade_date DESC"
    )
    params: tuple = ()
    if lim > 0:
        sql += " LIMIT %s"
        params = (lim,)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
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
            })
        return {'rows': rows, 'limit': lim, 'error': None}
    except Exception as e:
        logger.exception("查询 marketinfo_recent 失败: %s", e)
        return {'rows': [], 'limit': lim, 'error': str(e)}
    finally:
        if ownConn and conn is not None:
            conn.close()


query_marketinfo_recent = queryMarketinfoRecent
normalize_marketinfo_recent_limit = normalizeMarketinfoRecentLimit

# 代码更新时间：2026-06-17 15:17
