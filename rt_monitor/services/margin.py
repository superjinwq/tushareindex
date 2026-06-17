"""融资融券 margin_daily 只读查询。"""
from __future__ import annotations

from datetime import datetime

import pymysql
from logger_config import get_logger

from rt_monitor.core.config import DB_CONFIG_TUSHARE
from rt_monitor.core.numbers import toFloatOrNone

logger = get_logger(__name__)


def queryMarginDailyRecent(limit: int = 300, conn=None):
    """只读 margin_daily：rzye_sum、netbuy 库内单位为元，接口字段 *_yi 为亿元；netbuy_pct 为净买入占融资买入额比(%)。"""
    ownConn = conn is None
    if ownConn:
        try:
            conn = pymysql.connect(**DB_CONFIG_TUSHARE)
        except Exception as e:
            logger.exception("连接 stock_tushare 失败（margin_daily）: %s", e)
            return {'rows': [], 'error': '数据库连接失败'}
    try:
        try:
            lim = int(limit)
            lim = max(1, min(lim, 1300))
        except (TypeError, ValueError):
            lim = 300
        sql = (
            "SELECT trade_date, "
            "ROUND(rzye_sum / 100000000, 2) AS rzye_sum_yi, "
            "ROUND(netbuy / 100000000, 2) AS netbuy_yi, "
            "ROUND(netbuy_pct, 2) AS netbuy_pct "
            "FROM margin_daily ORDER BY trade_date DESC LIMIT %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (lim,))
            raw = cur.fetchall()
        rows = []
        for r in raw or []:
            td = r[0]
            tdStr = td.isoformat() if hasattr(td, 'isoformat') else str(td)
            rows.append({
                'trade_date': tdStr,
                'rzye_sum_yi': toFloatOrNone(r[1]),
                'netbuy_yi': toFloatOrNone(r[2]),
                'netbuy_pct': toFloatOrNone(r[3]),
            })
        return {
            'rows': rows,
            'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
    except Exception as e:
        logger.exception("查询 margin_daily 失败: %s", e)
        return {'rows': [], 'error': str(e)}
    finally:
        if ownConn and conn is not None:
            conn.close()


def queryMarginRzmreIdxSeries(limit: int = 300, conn=None):
    """
    margin_daily：rzmre_pct、rzmre_pct_per 与 index_daily(399317.SZ).close 按 trade_date 对齐。
    SQL 先对 margin_daily 子查询 LIMIT 再 JOIN index_daily。
    """
    idxTs = '399317.SZ'
    ownConn = conn is None
    if ownConn:
        try:
            conn = pymysql.connect(**DB_CONFIG_TUSHARE)
        except Exception as e:
            logger.exception("连接 stock_tushare 失败（margin_rzmre_idx_series）: %s", e)
            return {'rows': [], 'error': '数据库连接失败'}
    try:
        try:
            lim = int(limit)
            lim = max(1, min(lim, 1300))
        except (TypeError, ValueError):
            lim = 300
        sql = (
            "SELECT m.trade_date, m.rzmre_pct, m.rzmre_pct_per, i.close AS idx_close "
            "FROM ("
            "  SELECT trade_date, rzmre_pct, rzmre_pct_per FROM margin_daily "
            "  ORDER BY trade_date DESC LIMIT %s"
            ") m "
            "INNER JOIN index_daily i ON i.ts_code = %s AND i.trade_date = m.trade_date "
            "ORDER BY m.trade_date ASC"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (lim, idxTs))
            raw = cur.fetchall()
        rows = []
        for r in raw or []:
            td = r[0]
            tdStr = td.isoformat() if hasattr(td, 'isoformat') else str(td)
            rows.append({
                'trade_date': tdStr,
                'rzmre_pct': toFloatOrNone(r[1]),
                'rzmre_pct_per': toFloatOrNone(r[2]),
                'idx_close': toFloatOrNone(r[3]),
            })
        return {
            'rows': rows,
            'ts_code': idxTs,
            'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
    except Exception as e:
        logger.exception("查询 margin_rzmre_idx_series 失败: %s", e)
        return {'rows': [], 'error': str(e)}
    finally:
        if ownConn and conn is not None:
            conn.close()


# web_dashboard / 计划文档兼容别名
query_margin_daily_recent = queryMarginDailyRecent
query_margin_rzmre_idx_series = queryMarginRzmreIdxSeries

# 代码更新时间：2026-06-17 15:17
