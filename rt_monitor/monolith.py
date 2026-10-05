#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行情监控 Web 服务（独立单文件，不依赖 web_dashboard；database_pool 可选，缺失时自动直连）
功能：展示当日、上一交易日、前五/前二十/前六十/前一百二十交易日涨跌幅中位数；按 12 分类表格；ETF BIAS(Per) 表；ETF 通达信式 MA/EMA/REF 共振信号表（60MIN+日线）及内嵌图表。
运行: python rt_monitor_app.py  访问: http://localhost:5000（端口由 RT_MONITOR_PORT 控制，默认 5000）
反代在子路径下对外提供时：需让 Flask 收到正确 SCRIPT_NAME，或设置环境变量 RT_MONITOR_API_PREFIX（无前导斜杠、无尾部斜杠，例如 /monitor），否则前端对 /api/... 的请求会打到站点根路径，弹窗等接口会一直「加载中」。
改 templates/rt_monitor.html 后：已启用 TEMPLATES_AUTO_RELOAD，保存后刷新即可；若仍旧版请确认本进程（响应头 X-RT-Monitor-Build）并强刷 Ctrl+F5。
重启代码后再运行：默认不自动结束其它「python … rt_monitor_app.py」进程（RT_MONITOR_AUTO_KILL_PREVIOUS=0），避免并发启动时互相误杀；需要自动清理旧进程时可显式设 RT_MONITOR_AUTO_KILL_PREVIOUS=1。

职责边界：本脚本从数据表读取并展示，不写入行情等业务表、不刷新行情表；MA 支撑/阻力价位由 /api/ma_sr_levels 写入工程目录 ma_sr_levels.json（与浏览器无关）。启动时幂等创建 rt_k(trade_time) 索引 idx_rt_k_trade_time（DDL）；涨跌家数等依赖 stockstat 字段时，以盘中实时统计写入为前提，否则列可能陈旧。
- stockstat / rt_k（stock_wind）、stk_factor_ma（stock_tushare）：涨跌家数面板 /api/limit_stats 只读；新高/低十八项为 rt_k 与 stk_factor_ma 最新行实时比较 COUNT（与盘中实时统计口径一致）。
- limit_list_ths（stock_tushare）：同花顺涨跌停池历史，由入库脚本写入；/api/limit_stats 只读。
- margin_daily（stock_tushare）：融资融券按日汇总，由 margin_sum.py 写入；/api/margin_daily_recent 只读（rzye_sum、netbuy、netbuy_pct，limit 默认300上限1300）；/api/margin_rzmre_idx_series 只读（rzmre_pct、rzmre_pct_per + index_daily 399317.SZ close）；/api/up_num_ma5_series 只读（marketinfo.up_num_ma5 + 399317 收盘，limit 默认300上限1300）。
- index_stat（stock_wind）：由盘中实时统计.py 写入；本服务只读，按 TTL 读表供 API 返回。
- stk_factor_pro（stock_tushare）：只读，用于启动时取历史日期列表（t5_dates 等）。
- etf_rt_min / etf_stk_mins_ma / etf_bias_realtime（stock_tushare）：只读；BIAS(Per) 表直接读 etf_bias_realtime 中 per（daemon 用 1250 根算好）；无行时本地用 PER_BIAS_WINDOW 根 K 回算。弹窗序列先取≥1250 根再算 per、再截 300 点返回。
表 index_stat 需以 index_name 为主键或唯一键。
"""

import os
import sys
import json
import time
import statistics
import hashlib
import subprocess
import re
import uuid
import threading
import concurrent.futures
import atexit
import signal
import pandas as pd
import numpy as np
import pymysql
from datetime import date, datetime, timedelta
from flask import Flask, render_template, jsonify, request, make_response
from flask_socketio import SocketIO
import warnings
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from logger_config import get_logger

warnings.filterwarnings('ignore')

logger = get_logger(__name__)

from rt_monitor.core.popup_cache import popupSeriesCacheGet, popupSeriesCachePut

from rt_monitor.core.config import (
    REPO_ROOT as _APP_DIR,
    DB_CONFIG,
    DB_CONFIG_TUSHARE,
    MA_SR_LEVELS_PATH as _MA_SR_LEVELS_PATH,
    NIUSAN_HOLDER_CONFIG_PATH as _NIUSAN_HOLDER_CONFIG_PATH,
    RT_MONITOR_LISTEN_PORT,
    RT_MONITOR_STATIC_VER,
)

# ---------- rt_k：新鲜度 SQL + trade_time 索引（与 盘中实时统计.py 内联片段保持一致，修改时请两处同步）----------
RT_K_TRADE_TIME_INDEX_NAME = "idx_rt_k_trade_time"
SQL_RT_K_MAX_TRADE_TIME_SUBQUERY = (
    "(SELECT MAX(trade_time) FROM stock_wind.rt_k rt2 WHERE rt2.trade_time IS NOT NULL)"
)
SQL_RT_K_FRESH_WHERE_RT = (
    "rt.trade_time IS NOT NULL AND "
    "rt.trade_time >= DATE(" + SQL_RT_K_MAX_TRADE_TIME_SUBQUERY + ") "
    "AND rt.trade_time < DATE(" + SQL_RT_K_MAX_TRADE_TIME_SUBQUERY + ") + INTERVAL 1 DAY"
)


def ensure_rt_k_trade_time_index(conn, log) -> None:
    """若 stock_wind.rt_k 尚无 idx_rt_k_trade_time 则创建；已存在则跳过。"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.statistics
                WHERE table_schema = DATABASE()
                  AND table_name = 'rt_k'
                  AND index_name = %s
                """,
                (RT_K_TRADE_TIME_INDEX_NAME,),
            )
            row = cur.fetchone()
            if row and int(row[0] or 0) > 0:
                return
            cur.execute(
                f"CREATE INDEX `{RT_K_TRADE_TIME_INDEX_NAME}` ON `rt_k` (`trade_time`)"
            )
        conn.commit()
        log.info(
            "已创建索引 %s ON stock_wind.rt_k(trade_time)",
            RT_K_TRADE_TIME_INDEX_NAME,
        )
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning("ensure_rt_k_trade_time_index 跳过: %s", e)


# stock_tushare.index_member_all_sw：与 index_member_all.py 中 idx_member_sw_ts_indate 同名，供申万映射 SQL 走索引
INDEX_MEMBER_SW_TABLE = "index_member_all_sw"
INDEX_MEMBER_SW_TS_INDATE_NAME = "idx_member_sw_ts_indate"
ETF_STK_MINS_MA_FAST_IDX = "idx_etf_stk_mins_ma_tft"
ETF_RT_MIN_FAST_IDX = "idx_etf_rt_min_tft"
ETF_BIAS_RT_UPDATED_IDX = "idx_etf_bias_rt_tfu"
ETF_BIAS_RT_TRADE_IDX = "idx_etf_bias_rt_tft"
TOP10_HOLDERS_TS_END_DATE_IDX = "idx_top10_holders_ts_end_date"


def ensure_index_member_sw_ts_indate_tushare(conn, log) -> None:
    """若尚无 idx_member_sw_ts_indate 则创建 (ts_code, in_date DESC)；已存在则跳过。失败仅告警（如无建表权限）。"""
    dbn = (DB_CONFIG_TUSHARE.get("database") or "stock_tushare").strip() or "stock_tushare"
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.statistics
                WHERE table_schema = %s AND table_name = %s AND index_name = %s
                """,
                (dbn, INDEX_MEMBER_SW_TABLE, INDEX_MEMBER_SW_TS_INDATE_NAME),
            )
            row = cur.fetchone()
            if row and int(row[0] or 0) > 0:
                return
            cur.execute(
                f"CREATE INDEX `{INDEX_MEMBER_SW_TS_INDATE_NAME}` "
                f"ON `{dbn}`.`{INDEX_MEMBER_SW_TABLE}` (`ts_code`, `in_date` DESC)"
            )
        conn.commit()
        log.info(
            "已创建索引 %s ON %s.%s(ts_code, in_date DESC)（申万 JOIN）",
            INDEX_MEMBER_SW_TS_INDATE_NAME,
            dbn,
            INDEX_MEMBER_SW_TABLE,
        )
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning("ensure_index_member_sw_ts_indate_tushare 跳过: %s", e)


def _ensure_tushare_index(conn, log, table_name: str, index_name: str, ddl: str) -> None:
    """通用幂等索引创建：存在即跳过，不存在则创建。"""
    dbn = (DB_CONFIG_TUSHARE.get("database") or "stock_tushare").strip() or "stock_tushare"
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.statistics
                WHERE table_schema = %s AND table_name = %s AND index_name = %s
                """,
                (dbn, table_name, index_name),
            )
            row = cur.fetchone()
            if row and int(row[0] or 0) > 0:
                return
            cur.execute(ddl)
        conn.commit()
        log.info("已创建索引 %s ON %s.%s", index_name, dbn, table_name)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning("ensure index %s on %s 跳过: %s", index_name, table_name, e)


def ensure_etf_popup_hot_indexes_tushare(conn, log) -> None:
    """弹窗热点表索引：加速 ts_code/freq/trade_time 与 realtime updated_at 过滤。"""
    dbn = (DB_CONFIG_TUSHARE.get("database") or "stock_tushare").strip() or "stock_tushare"
    _ensure_tushare_index(
        conn,
        log,
        "etf_stk_mins_ma",
        ETF_STK_MINS_MA_FAST_IDX,
        f"CREATE INDEX `{ETF_STK_MINS_MA_FAST_IDX}` "
        f"ON `{dbn}`.`etf_stk_mins_ma` (`ts_code`, `freq`, `trade_time`)",
    )
    _ensure_tushare_index(
        conn,
        log,
        "etf_rt_min",
        ETF_RT_MIN_FAST_IDX,
        f"CREATE INDEX `{ETF_RT_MIN_FAST_IDX}` "
        f"ON `{dbn}`.`etf_rt_min` (`ts_code`, `freq`, `trade_time`)",
    )
    _ensure_tushare_index(
        conn,
        log,
        "etf_bias_realtime",
        ETF_BIAS_RT_UPDATED_IDX,
        f"CREATE INDEX `{ETF_BIAS_RT_UPDATED_IDX}` "
        f"ON `{dbn}`.`etf_bias_realtime` (`ts_code`, `freq`, `updated_at`)",
    )
    _ensure_tushare_index(
        conn,
        log,
        "etf_bias_realtime",
        ETF_BIAS_RT_TRADE_IDX,
        f"CREATE INDEX `{ETF_BIAS_RT_TRADE_IDX}` "
        f"ON `{dbn}`.`etf_bias_realtime` (`ts_code`, `freq`, `trade_time`)",
    )


def ensure_top10_holders_ts_end_date_index_tushare(conn, log) -> None:
    """财报表 top10 前三持股：按 ts_code 取 MAX(end_date) 时走 (ts_code, end_date) 索引。"""
    dbn = (DB_CONFIG_TUSHARE.get("database") or "stock_tushare").strip() or "stock_tushare"
    _ensure_tushare_index(
        conn,
        log,
        "top10_holders",
        TOP10_HOLDERS_TS_END_DATE_IDX,
        f"CREATE INDEX `{TOP10_HOLDERS_TS_END_DATE_IDX}` "
        f"ON `{dbn}`.`top10_holders` (`ts_code`, `end_date`)",
    )


_LIMIT_STATS_RT_K_FRESH_TRADE_DATE = SQL_RT_K_FRESH_WHERE_RT

_TUSHARE_COLUMN_EXISTS_CACHE: dict = {}

# stock_wind.rt_k 有效代码全集：财报筛选 INNER JOIN 原先用「DISTINCT ts_code」子查询，每笔请求扫一遍 rt_k；
# 进程内短 TTL 缓存 + 快路径 WHERE base.ts_code IN (...) 减轻重复 IO（列表变动频率低）。
_RT_K_TS_CODES_CACHE_ROW: tuple[float, tuple[str, ...]] | None = None
_RT_K_TS_CODES_CACHE_TTL_SEC = 180.0


def _get_cached_rt_k_ts_codes_tuple(conn) -> tuple[str, ...] | None:
    """返回 rt_k 中 ts_code 全集（排序元组）；失败时返回 None，调用方回退 INNER JOIN 子查询。

    返回 () 表示库内确无代码（极少见）；与 None（查询失败）区分。
    """
    global _RT_K_TS_CODES_CACHE_ROW
    now = time.monotonic()
    hit = _RT_K_TS_CODES_CACHE_ROW
    if hit is not None and (now - hit[0]) < _RT_K_TS_CODES_CACHE_TTL_SEC:
        return hit[1]
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT ts_code FROM stock_wind.rt_k "
                "WHERE ts_code IS NOT NULL AND ts_code <> ''"
            )
            rows = cur.fetchall() or []
    except Exception as e:
        logger.warning('读取 rt_k ts_code 全集失败，财报筛选回退 INNER JOIN rt_k：%s', e)
        return None
    codes = tuple(sorted({str(r[0]).strip() for r in rows if r and r[0]}))
    _RT_K_TS_CODES_CACHE_ROW = (now, codes)
    return codes


def _tushare_column_exists(conn, table: str, column: str) -> bool:
    """检测 stock_tushare 下某表是否存在列（缺列时避免 SQL 直接失败）。

    进程级缓存：列结构在运行期不变，避免每次请求命中 information_schema 多次。
    若查询异常不缓存 False，下次可重试。
    """
    db = DB_CONFIG_TUSHARE.get('database') or 'stock_tushare'
    key = (db, table, column)
    cached = _TUSHARE_COLUMN_EXISTS_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s LIMIT 1",
                (db, table, column),
            )
            exists = cur.fetchone() is not None
        _TUSHARE_COLUMN_EXISTS_CACHE[key] = exists
        return exists
    except Exception:
        return False


# /api/limit_stats：剔除 ST/*ST/S*ST 等风险股（按证券简称前缀；名称为 NULL 时仍计入，避免误伤）
# 注意：PyMySQL 对 SQL 字符串做 % 格式化，LIKE 中的 % 必须写成 %%，否则报「not enough arguments for format string」
_LIMIT_STATS_SQL_NOT_ST_STOCKSTAT = (
    " AND (sec_name IS NULL OR ("
    " sec_name NOT LIKE '*ST%%' AND sec_name NOT LIKE 'ST%%' AND sec_name NOT LIKE 'S*ST%%'"
    "))"
)
_LIMIT_STATS_SQL_NOT_ST_THS = (
    " AND (name IS NULL OR ("
    " name NOT LIKE '*ST%%' AND name NOT LIKE 'ST%%' AND name NOT LIKE 'S*ST%%'"
    "))"
)
_LIMIT_STATS_SQL_NOT_ST_JOIN_S = (
    " AND (s.sec_name IS NULL OR ("
    " s.sec_name NOT LIKE '*ST%%' AND s.sec_name NOT LIKE 'ST%%' AND s.sec_name NOT LIKE 'S*ST%%'"
    "))"
)
# 新高/低十八项：stockstat 别名 s1（与 rt_k、ma 同查）
_LIMIT_STATS_SQL_NOT_ST_STOCKSTAT_S1 = (
    " AND (s1.sec_name IS NULL OR ("
    " s1.sec_name NOT LIKE '*ST%%' AND s1.sec_name NOT LIKE 'ST%%' AND s1.sec_name NOT LIKE 'S*ST%%'"
    "))"
)
# 与 盘中实时统计.py 一致：HL 为前复权口径，rt_k 换算后再比
# 未复权价来自 stk_factor_pro（与 ma 同日一行）；stk_factor_ma 通常仅有 *_qfq 无 high/low/close
_LIMIT_STATS_RT_HIGH_QFQ = (
    "(rt.high * COALESCE("
    "NULLIF(ma.high_qfq, 0) / NULLIF(ma.pro_high, 0), "
    "NULLIF(ma.close_qfq, 0) / NULLIF(ma.pro_close, 0), "
    "1))"
)
_LIMIT_STATS_RT_LOW_QFQ = (
    "(rt.low * COALESCE("
    "NULLIF(ma.low_qfq, 0) / NULLIF(ma.pro_low, 0), "
    "NULLIF(ma.close_qfq, 0) / NULLIF(ma.pro_close, 0), "
    "1))"
)
# 盘中收盘：前复权口径（与 high/low 一致用 pro_close 换算）
_LIMIT_STATS_RT_CLOSE_QFQ = (
    "(rt.close * COALESCE("
    "NULLIF(ma.close_qfq, 0) / NULLIF(ma.pro_close, 0), "
    "1))"
)
# ===== 申万行业页实时层：新高/低分布（收盘价口径）所需 MA JOIN 与判定条件 =====
# ma：rt_k 最新日 D 对应的复权换算行；mp：D 的上一交易日 MA 阈值行。
# 阈值必须取 mp.*，避免 stk_factor_ma 已更新到 D 后 high_N/low_N 含当日 K 线导致 HL_C 恒为 0。
_LIMIT_STATS_MA_HLC_JOIN_SQL = """
    INNER JOIN (
        SELECT m.ts_code, m.high_qfq, m.low_qfq, m.close_qfq,
               p.high AS pro_high, p.low AS pro_low, p.close AS pro_close
        FROM stock_tushare.stk_factor_ma m
        INNER JOIN (
            SELECT ts_code, MAX(trade_date) AS md
            FROM stock_tushare.stk_factor_ma
            WHERE trade_date <= DATE((SELECT MAX(trade_time) FROM stock_wind.rt_k rt2 WHERE rt2.trade_time IS NOT NULL))
            GROUP BY ts_code
        ) mx ON m.ts_code = mx.ts_code AND m.trade_date = mx.md
        LEFT JOIN stock_tushare.stk_factor_pro p
          ON p.ts_code = m.ts_code AND p.trade_date = m.trade_date
    ) ma ON ma.ts_code = rt.ts_code
    INNER JOIN (
        SELECT m.ts_code, m.high_250, m.high_850, m.h_high, m.low_250, m.low_850, m.h_low
        FROM stock_tushare.stk_factor_ma m
        INNER JOIN (
            SELECT ts_code, MAX(trade_date) AS md
            FROM stock_tushare.stk_factor_ma
            WHERE trade_date < DATE((SELECT MAX(trade_time) FROM stock_wind.rt_k rt2 WHERE rt2.trade_time IS NOT NULL))
            GROUP BY ts_code
        ) mx ON m.ts_code = mx.ts_code AND m.trade_date = mx.md
    ) mp ON mp.ts_code = rt.ts_code
"""
# 收盘价口径新高/低：rt 收盘前复权价突破/跌破上一交易日 mp.high_N / mp.low_N（涨跌家数 HL_C 明细同源，不含 ST）
_LIMIT_STATS_HLC_DETAIL_WHERE = {
    'hlc_ch_250': (
        f"NOT (rt.vol = 0 AND rt.amount = 0) AND mp.high_250 IS NOT NULL "
        f"AND mp.low_250 IS NOT NULL AND ({_LIMIT_STATS_RT_CLOSE_QFQ}) > mp.high_250"
    ),
    'hlc_cl_250': (
        f"NOT (rt.vol = 0 AND rt.amount = 0) AND mp.high_250 IS NOT NULL "
        f"AND mp.low_250 IS NOT NULL AND ({_LIMIT_STATS_RT_CLOSE_QFQ}) < mp.low_250"
    ),
    'hlc_ch_850': (
        f"NOT (rt.vol = 0 AND rt.amount = 0) AND mp.high_850 IS NOT NULL "
        f"AND mp.low_850 IS NOT NULL AND ({_LIMIT_STATS_RT_CLOSE_QFQ}) > mp.high_850"
    ),
    'hlc_cl_850': (
        f"NOT (rt.vol = 0 AND rt.amount = 0) AND mp.high_850 IS NOT NULL "
        f"AND mp.low_850 IS NOT NULL AND ({_LIMIT_STATS_RT_CLOSE_QFQ}) < mp.low_850"
    ),
    'hlc_ch_h': (
        f"NOT (rt.vol = 0 AND rt.amount = 0) AND mp.h_high IS NOT NULL "
        f"AND mp.h_low IS NOT NULL AND ({_LIMIT_STATS_RT_CLOSE_QFQ}) > mp.h_high"
    ),
    'hlc_cl_h': (
        f"NOT (rt.vol = 0 AND rt.amount = 0) AND mp.h_high IS NOT NULL "
        f"AND mp.h_low IS NOT NULL AND ({_LIMIT_STATS_RT_CLOSE_QFQ}) < mp.h_low"
    ),
}
# 历史归档明细表（按 trade_date + kind 取个股集合，供新高/低明细弹窗的历史模式）
TABLE_LIMIT_STATS_ARCHIVE_DETAIL = "limit_stats_archive_detail"
# 新高/低、HL_C：rt_k 新鲜度见上文 _LIMIT_STATS_RT_K_FRESH_TRADE_DATE（与盘中实时统计.py 同步维护）
_LIMIT_STATS_HIGHLOW_COUNT_SQL = (
    """
SELECT
  SUM(CASE WHEN ma.high_5 IS NOT NULL AND """
    + _LIMIT_STATS_RT_HIGH_QFQ
    + """ > ma.high_5 THEN 1 ELSE 0 END) AS cnt_h5,
  SUM(CASE WHEN ma.high_10 IS NOT NULL AND """
    + _LIMIT_STATS_RT_HIGH_QFQ
    + """ > ma.high_10 THEN 1 ELSE 0 END) AS cnt_h10,
  SUM(CASE WHEN ma.high_20 IS NOT NULL AND """
    + _LIMIT_STATS_RT_HIGH_QFQ
    + """ > ma.high_20 THEN 1 ELSE 0 END) AS cnt_h20,
  SUM(CASE WHEN ma.high_60 IS NOT NULL AND """
    + _LIMIT_STATS_RT_HIGH_QFQ
    + """ > ma.high_60 THEN 1 ELSE 0 END) AS cnt_h60,
  SUM(CASE WHEN ma.high_120 IS NOT NULL AND """
    + _LIMIT_STATS_RT_HIGH_QFQ
    + """ > ma.high_120 THEN 1 ELSE 0 END) AS cnt_h120,
  SUM(CASE WHEN ma.high_200 IS NOT NULL AND """
    + _LIMIT_STATS_RT_HIGH_QFQ
    + """ > ma.high_200 THEN 1 ELSE 0 END) AS cnt_h200,
  SUM(CASE WHEN ma.high_250 IS NOT NULL AND """
    + _LIMIT_STATS_RT_HIGH_QFQ
    + """ > ma.high_250 THEN 1 ELSE 0 END) AS cnt_h250,
  SUM(CASE WHEN ma.high_850 IS NOT NULL AND """
    + _LIMIT_STATS_RT_HIGH_QFQ
    + """ > ma.high_850 THEN 1 ELSE 0 END) AS cnt_h850,
  SUM(CASE WHEN ma.h_high IS NOT NULL AND """
    + _LIMIT_STATS_RT_HIGH_QFQ
    + """ > ma.h_high THEN 1 ELSE 0 END) AS cnt_h_high,
  SUM(CASE WHEN ma.low_5 IS NOT NULL AND """
    + _LIMIT_STATS_RT_LOW_QFQ
    + """ < ma.low_5 THEN 1 ELSE 0 END) AS cnt_l5,
  SUM(CASE WHEN ma.low_10 IS NOT NULL AND """
    + _LIMIT_STATS_RT_LOW_QFQ
    + """ < ma.low_10 THEN 1 ELSE 0 END) AS cnt_l10,
  SUM(CASE WHEN ma.low_20 IS NOT NULL AND """
    + _LIMIT_STATS_RT_LOW_QFQ
    + """ < ma.low_20 THEN 1 ELSE 0 END) AS cnt_l20,
  SUM(CASE WHEN ma.low_60 IS NOT NULL AND """
    + _LIMIT_STATS_RT_LOW_QFQ
    + """ < ma.low_60 THEN 1 ELSE 0 END) AS cnt_l60,
  SUM(CASE WHEN ma.low_120 IS NOT NULL AND """
    + _LIMIT_STATS_RT_LOW_QFQ
    + """ < ma.low_120 THEN 1 ELSE 0 END) AS cnt_l120,
  SUM(CASE WHEN ma.low_200 IS NOT NULL AND """
    + _LIMIT_STATS_RT_LOW_QFQ
    + """ < ma.low_200 THEN 1 ELSE 0 END) AS cnt_l200,
  SUM(CASE WHEN ma.low_250 IS NOT NULL AND """
    + _LIMIT_STATS_RT_LOW_QFQ
    + """ < ma.low_250 THEN 1 ELSE 0 END) AS cnt_l250,
  SUM(CASE WHEN ma.low_850 IS NOT NULL AND """
    + _LIMIT_STATS_RT_LOW_QFQ
    + """ < ma.low_850 THEN 1 ELSE 0 END) AS cnt_l850,
  SUM(CASE WHEN ma.h_low IS NOT NULL AND """
    + _LIMIT_STATS_RT_LOW_QFQ
    + """ < ma.h_low THEN 1 ELSE 0 END) AS cnt_l_low
FROM stock_wind.stockstat s1
INNER JOIN stock_wind.rt_k rt ON s1.windcodes = rt.ts_code
INNER JOIN (
    SELECT m.ts_code, m.high_5, m.low_5, m.high_10, m.low_10, m.high_20, m.low_20,
           m.high_60, m.low_60, m.high_120, m.low_120, m.high_200, m.low_200,
           m.high_250, m.high_850, m.h_high, m.low_250, m.low_850, m.h_low,
           m.high_qfq, m.low_qfq, m.close_qfq,
           p.high AS pro_high, p.low AS pro_low, p.close AS pro_close
    FROM stock_tushare.stk_factor_ma m
    INNER JOIN (
        SELECT ts_code, MAX(trade_date) AS md
        FROM stock_tushare.stk_factor_ma
        GROUP BY ts_code
    ) mx ON m.ts_code = mx.ts_code AND m.trade_date = mx.md
    LEFT JOIN stock_tushare.stk_factor_pro p
      ON p.ts_code = m.ts_code AND p.trade_date = m.trade_date
) ma ON ma.ts_code = rt.ts_code
WHERE NOT (rt.vol = 0 AND rt.amount = 0)
AND """
    + _LIMIT_STATS_RT_K_FRESH_TRADE_DATE
    + """
"""
    + _LIMIT_STATS_SQL_NOT_ST_STOCKSTAT_S1
)


# 新高/低十八项明细：与 _LIMIT_STATS_HIGHLOW_COUNT_SQL 中 CASE 条件一致
_LIMIT_STATS_HL_DETAIL_WHERE = {
    'cnt_h5': f"ma.high_5 IS NOT NULL AND ({_LIMIT_STATS_RT_HIGH_QFQ}) > ma.high_5",
    'cnt_h10': f"ma.high_10 IS NOT NULL AND ({_LIMIT_STATS_RT_HIGH_QFQ}) > ma.high_10",
    'cnt_h20': f"ma.high_20 IS NOT NULL AND ({_LIMIT_STATS_RT_HIGH_QFQ}) > ma.high_20",
    'cnt_h60': f"ma.high_60 IS NOT NULL AND ({_LIMIT_STATS_RT_HIGH_QFQ}) > ma.high_60",
    'cnt_h120': f"ma.high_120 IS NOT NULL AND ({_LIMIT_STATS_RT_HIGH_QFQ}) > ma.high_120",
    'cnt_h200': f"ma.high_200 IS NOT NULL AND ({_LIMIT_STATS_RT_HIGH_QFQ}) > ma.high_200",
    'cnt_h250': f"ma.high_250 IS NOT NULL AND ({_LIMIT_STATS_RT_HIGH_QFQ}) > ma.high_250",
    'cnt_h850': f"ma.high_850 IS NOT NULL AND ({_LIMIT_STATS_RT_HIGH_QFQ}) > ma.high_850",
    'cnt_h_high': f"ma.h_high IS NOT NULL AND ({_LIMIT_STATS_RT_HIGH_QFQ}) > ma.h_high",
    'cnt_l5': f"ma.low_5 IS NOT NULL AND ({_LIMIT_STATS_RT_LOW_QFQ}) < ma.low_5",
    'cnt_l10': f"ma.low_10 IS NOT NULL AND ({_LIMIT_STATS_RT_LOW_QFQ}) < ma.low_10",
    'cnt_l20': f"ma.low_20 IS NOT NULL AND ({_LIMIT_STATS_RT_LOW_QFQ}) < ma.low_20",
    'cnt_l60': f"ma.low_60 IS NOT NULL AND ({_LIMIT_STATS_RT_LOW_QFQ}) < ma.low_60",
    'cnt_l120': f"ma.low_120 IS NOT NULL AND ({_LIMIT_STATS_RT_LOW_QFQ}) < ma.low_120",
    'cnt_l200': f"ma.low_200 IS NOT NULL AND ({_LIMIT_STATS_RT_LOW_QFQ}) < ma.low_200",
    'cnt_l250': f"ma.low_250 IS NOT NULL AND ({_LIMIT_STATS_RT_LOW_QFQ}) < ma.low_250",
    'cnt_l850': f"ma.low_850 IS NOT NULL AND ({_LIMIT_STATS_RT_LOW_QFQ}) < ma.low_850",
    'cnt_l_low': f"ma.h_low IS NOT NULL AND ({_LIMIT_STATS_RT_LOW_QFQ}) < ma.h_low",
}

# 面板直连 stk_factor_ma 失败时回退 stockstat 标志列（与 read_limit_stats_panel 一致）
_LIMIT_STATS_HL_DETAIL_FALLBACK_COL = {
    'cnt_h5': 'windcodes_high_5',
    'cnt_h10': 'windcodes_high_10',
    'cnt_h20': 'windcodes_high_20',
    'cnt_h60': 'windcodes_high_60',
    'cnt_h120': 'windcodes_high_120',
    'cnt_h200': 'windcodes_high_200',
    'cnt_h250': 'windcodes_high_250',
    'cnt_h850': 'windcodes_high_850',
    'cnt_h_high': 'windcodes_h_high',
    'cnt_l5': 'windcodes_low_5',
    'cnt_l10': 'windcodes_low_10',
    'cnt_l20': 'windcodes_low_20',
    'cnt_l60': 'windcodes_low_60',
    'cnt_l120': 'windcodes_low_120',
    'cnt_l200': 'windcodes_low_200',
    'cnt_l250': 'windcodes_low_250',
    'cnt_l850': 'windcodes_low_850',
    'cnt_l_low': 'windcodes_h_low',
}
# 涨跌家数面板新高/低：与 _LIMIT_STATS_HIGHLOW_COUNT_SQL 列顺序一致
_LIMIT_STATS_PANEL_HL_KEYS = (
    'cnt_h5',
    'cnt_h10',
    'cnt_h20',
    'cnt_h60',
    'cnt_h120',
    'cnt_h200',
    'cnt_h250',
    'cnt_h850',
    'cnt_h_high',
    'cnt_l5',
    'cnt_l10',
    'cnt_l20',
    'cnt_l60',
    'cnt_l120',
    'cnt_l200',
    'cnt_l250',
    'cnt_l850',
    'cnt_l_low',
)

# 涨跌家数 HL_C 行：百分点差键（与 _LIMIT_STATS_HLC_COUNT_SQL 列顺序一致）
_LIMIT_STATS_PANEL_HLC_KEYS = (
    'hlc_cccc_5',
    'hlc_cccc_10',
    'hlc_cccc_20',
    'hlc_cccc_60',
    'hlc_cccc_120',
    'hlc_cccc_200',
    'hlc_cccc_250',
    'hlc_cccc_850',
    'hlc_cccc_h',
)


def _limit_stats_pct_diff_pp(a_cnt: int, b_cnt: int, base: int) -> Decimal:
    """与 marketinfo _pct_diff_pp 一致：(CH/base×100) − (CL/base×100)，保留 2 位小数（百分点差）。"""
    if base <= 0:
        return Decimal('0.00')
    pa = (Decimal(a_cnt) / Decimal(base) * Decimal('100')).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP
    )
    pb = (Decimal(b_cnt) / Decimal(base) * Decimal('100')).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP
    )
    return (pa - pb).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


# stockstat HL_C 标志列后缀 → 面板键（盘中实时统计.py 每轮写入，与 _LIMIT_STATS_HLC_COUNT_SQL 同口径）
_LIMIT_STATS_HLC_STOCKSTAT_SPECS: tuple[tuple[str, str], ...] = (
    ('5', 'hlc_cccc_5'),
    ('10', 'hlc_cccc_10'),
    ('20', 'hlc_cccc_20'),
    ('60', 'hlc_cccc_60'),
    ('120', 'hlc_cccc_120'),
    ('200', 'hlc_cccc_200'),
    ('250', 'hlc_cccc_250'),
    ('850', 'hlc_cccc_850'),
    ('h', 'hlc_cccc_h'),
)

# 子查询偶发 null 时保留上次成功值，避免面板闪 --
_LIMIT_STATS_PANEL_STICKY_KEYS: tuple[str, ...] = (
    *_LIMIT_STATS_PANEL_HLC_KEYS,
    'zt_t1',
    'zt_t5',
    'dt_t1',
    'dt_t5',
    'cnt_lb_zt',
    'cnt_lb_dt',
    'prev_zt_pct',
    'prev_dt_pct',
    'up_num_ma5_rt',
    'up_num_ma5_rt_per',
)


def _read_limit_stats_hlc_from_stockstat(cursor) -> dict[str, str | None]:
    """
    HL_C 百分点差：读 stockstat.windcodes_hlc_valid/ch/cl_* 聚合（盘中实时统计预写）。
    避免 _LIMIT_STATS_HLC_COUNT_SQL 重型 ma JOIN 超过 read_timeout=8s 拖死同连接，导致后续 limit_list_ths 等一并失败。
    """
    out: dict[str, str | None] = {k: None for k in _LIMIT_STATS_PANEL_HLC_KEYS}
    sum_exprs: list[str] = []
    for sfx, _ in _LIMIT_STATS_HLC_STOCKSTAT_SPECS:
        sum_exprs.append(
            f"SUM(CASE WHEN IFNULL(windcodes_hlc_valid_{sfx},0)=1 THEN 1 ELSE 0 END)"
        )
        sum_exprs.append(
            f"SUM(CASE WHEN IFNULL(windcodes_hlc_ch_{sfx},0)=1 THEN 1 ELSE 0 END)"
        )
        sum_exprs.append(
            f"SUM(CASE WHEN IFNULL(windcodes_hlc_cl_{sfx},0)=1 THEN 1 ELSE 0 END)"
        )
    sql = (
        "SELECT "
        + ", ".join(sum_exprs)
        + " FROM stock_wind.stockstat WHERE 1=1"
        + _LIMIT_STATS_SQL_NOT_ST_STOCKSTAT
    )
    cursor.execute(sql)
    row = cursor.fetchone()
    if not row or len(row) < len(_LIMIT_STATS_HLC_STOCKSTAT_SPECS) * 3:
        return out
    idx = 0
    for sfx, key in _LIMIT_STATS_HLC_STOCKSTAT_SPECS:
        base_cnt = int(row[idx] or 0)
        ch_cnt = int(row[idx + 1] or 0)
        cl_cnt = int(row[idx + 2] or 0)
        idx += 3
        out[key] = format(float(_limit_stats_pct_diff_pp(ch_cnt, cl_cnt, base_cnt)), '.2f')
    return out


def _merge_limit_stats_panel(prev: dict | None, new: dict) -> dict:
    """新面板中值为 null 的粘性字段沿用上次成功快照。"""
    if not prev or not prev.get('ok'):
        return new
    if not new.get('ok'):
        return prev
    merged = dict(new)
    for key in _LIMIT_STATS_PANEL_STICKY_KEYS:
        if merged.get(key) is None and prev.get(key) is not None:
            merged[key] = prev[key]
    return merged


def _build_limit_stats_hlc_count_sql() -> str:
    """HL_C：收盘价相对上一交易日 MA 阈值的 CH/CL 百分点差，避免 ma 最新行含当日 K 线导致恒为 0。"""
    c = _LIMIT_STATS_RT_CLOSE_QFQ
    periods = [
        (5, 'high_5', 'low_5'),
        (10, 'high_10', 'low_10'),
        (20, 'high_20', 'low_20'),
        (60, 'high_60', 'low_60'),
        (120, 'high_120', 'low_120'),
        (200, 'high_200', 'low_200'),
        (250, 'high_250', 'low_250'),
        (850, 'high_850', 'low_850'),
    ]
    lines: list[str] = []
    for n, hc, lc in periods:
        cond = (
            f'NOT (rt.vol = 0 AND rt.amount = 0) AND mp.{hc} IS NOT NULL AND mp.{lc} IS NOT NULL'
        )
        lines.append(f'  SUM(CASE WHEN {cond} THEN 1 ELSE 0 END) AS base_{n},')
        lines.append(f'  SUM(CASE WHEN {cond} AND ({c}) > mp.{hc} THEN 1 ELSE 0 END) AS ch_{n},')
        lines.append(f'  SUM(CASE WHEN {cond} AND ({c}) < mp.{lc} THEN 1 ELSE 0 END) AS cl_{n},')
    cond_h = 'NOT (rt.vol = 0 AND rt.amount = 0) AND mp.h_high IS NOT NULL AND mp.h_low IS NOT NULL'
    lines.append(f'  SUM(CASE WHEN {cond_h} THEN 1 ELSE 0 END) AS base_h,')
    lines.append(f'  SUM(CASE WHEN {cond_h} AND ({c}) > mp.h_high THEN 1 ELSE 0 END) AS ch_h,')
    lines.append(f'  SUM(CASE WHEN {cond_h} AND ({c}) < mp.h_low THEN 1 ELSE 0 END) AS cl_h')
    select_body = 'SELECT\n' + '\n'.join(lines)
    return (
        """
WITH d AS (
    SELECT DATE(MAX(trade_time)) AS trade_date
    FROM stock_wind.rt_k
    WHERE trade_time IS NOT NULL
),
ma AS (
    SELECT m.ts_code, m.close_qfq, p.close AS pro_close
    FROM stock_tushare.stk_factor_ma m
    INNER JOIN (
        SELECT m2.ts_code, MAX(m2.trade_date) AS md
        FROM stock_tushare.stk_factor_ma m2
        INNER JOIN d ON m2.trade_date <= d.trade_date
        GROUP BY m2.ts_code
    ) mx ON m.ts_code = mx.ts_code AND m.trade_date = mx.md
    LEFT JOIN stock_tushare.stk_factor_pro p
      ON p.ts_code = m.ts_code AND p.trade_date = m.trade_date
),
mp AS (
    SELECT m.ts_code, m.high_5, m.low_5, m.high_10, m.low_10, m.high_20, m.low_20,
           m.high_60, m.low_60, m.high_120, m.low_120, m.high_200, m.low_200,
           m.high_250, m.high_850, m.h_high, m.low_250, m.low_850, m.h_low
    FROM stock_tushare.stk_factor_ma m
    INNER JOIN (
        SELECT m2.ts_code, MAX(m2.trade_date) AS md
        FROM stock_tushare.stk_factor_ma m2
        INNER JOIN d ON m2.trade_date < d.trade_date
        GROUP BY m2.ts_code
    ) mx ON m.ts_code = mx.ts_code AND m.trade_date = mx.md
)
"""
        + select_body
        + """
FROM stock_wind.stockstat s1
INNER JOIN stock_wind.rt_k rt ON s1.windcodes = rt.ts_code
INNER JOIN ma ON ma.ts_code = rt.ts_code
INNER JOIN mp ON mp.ts_code = rt.ts_code
WHERE NOT (rt.vol = 0 AND rt.amount = 0)
AND """
        + _LIMIT_STATS_RT_K_FRESH_TRADE_DATE
        + """
"""
        + _LIMIT_STATS_SQL_NOT_ST_STOCKSTAT_S1
    )


_LIMIT_STATS_HLC_COUNT_SQL = _build_limit_stats_hlc_count_sql()

# 实时 up_num_ma5：当日 rt_k 全表（含 ST），与 marketinfo/compute_marketinfo 口径一致；新鲜度同 SQL_RT_K_FRESH_WHERE_RT
_LIMIT_STATS_RT_K_UP_NUM_MA5_BODY = (
    """
SELECT
  SUM(CASE WHEN IFNULL(
    CASE WHEN IFNULL(rt.pre_close, 0) > 0
      THEN (rt.close - rt.pre_close) / rt.pre_close * 100
      ELSE NULL END,
    0) > 0 THEN 1 ELSE 0 END) AS up_num,
  COUNT(*) AS total_num
FROM stock_wind.rt_k rt
WHERE IFNULL(rt.vol, 0) > 0
  AND IFNULL(rt.amount, 0) > 0
  AND """
    + SQL_RT_K_FRESH_WHERE_RT
)


def _sql_date_to_py_date(val) -> date | None:
    """pymysql 返回的 date/datetime 转为 datetime.date。"""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    s = str(val).replace("-", "")[:8]
    if len(s) == 8 and s.isdigit():
        return datetime.strptime(s, "%Y%m%d").date()
    return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()


def _read_up_num_ma5_rt(conn) -> str | None:
    """
    近 5 交易日 sum(up_num)/sum(total_num)*100；前序日来自 marketinfo，当日来自 rt_k（新鲜度 + vol/amount>0，涨跌幅含 ST，对齐 compute_marketinfo）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DATE(MAX(trade_time)) FROM stock_wind.rt_k WHERE trade_time IS NOT NULL"
        )
        row_anchor = cur.fetchone()
        if not row_anchor or row_anchor[0] is None:
            return None
        anchor_date = _sql_date_to_py_date(row_anchor[0])
        if anchor_date is None:
            return None

        cur.execute(
            """
            SELECT DISTINCT trade_date
            FROM stock_tushare.stk_factor_pro
            WHERE trade_date <= %s
            ORDER BY trade_date DESC
            LIMIT 5
            """,
            (anchor_date.strftime("%Y-%m-%d"),),
        )
        raw_last5 = cur.fetchall() or []
        last5_desc: list[date] = []
        for r in raw_last5:
            d = _sql_date_to_py_date(r[0])
            if d is not None:
                last5_desc.append(d)
        if len(last5_desc) < 5:
            return None

        d0 = last5_desc[0]
        if d0 == anchor_date:
            prev_dates = last5_desc[1:5]
        elif d0 < anchor_date:
            prev_dates = last5_desc[0:4]
        else:
            return None

        if len(prev_dates) != 4:
            return None

        ph = ",".join(["%s"] * 4)
        cur.execute(
            f"""
            SELECT trade_date, up_num, total_num
            FROM stock_tushare.marketinfo
            WHERE trade_date IN ({ph})
            """,
            tuple(d.strftime("%Y-%m-%d") for d in prev_dates),
        )
        mi_rows = cur.fetchall() or []
        mi_map: dict[date, tuple[Decimal, Decimal]] = {}
        for trd, up_n, tot_n in mi_rows:
            dk = _sql_date_to_py_date(trd)
            if dk is None:
                continue
            mi_map[dk] = (
                Decimal(str(up_n or 0)),
                Decimal(str(tot_n or 0)),
            )
        for prev_d in prev_dates:
            if prev_d not in mi_map:
                return None

        sum_prev_up = Decimal("0")
        sum_prev_total = Decimal("0")
        for prev_d in prev_dates:
            u, t = mi_map[prev_d]
            sum_prev_up += u
            sum_prev_total += t

        cur.execute(_LIMIT_STATS_RT_K_UP_NUM_MA5_BODY)
        row_rt = cur.fetchone()
        if not row_rt or row_rt[1] is None:
            return None
        cur_up = Decimal(str(int(row_rt[0] or 0)))
        cur_total = Decimal(str(int(row_rt[1] or 0)))
        sum_up = sum_prev_up + cur_up
        sum_total = sum_prev_total + cur_total
        if sum_total <= 0:
            return None
        ma5 = (sum_up / sum_total * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        return format(ma5, "f")


def _read_up_num_ma5_rt_percentile(conn, up_num_ma5_rt_str: str | None) -> str | None:
    """
    当前实时 up_num_ma5（字符串）在 marketinfo.up_num_ma5 最近 500 条历史中的百分位（0~100，不含当前、严格小于，与 calc_percentile_in_window 一致）。
    历史样本排除 anchor 当日，避免与盘中实时重复比较。
    """
    if up_num_ma5_rt_str is None:
        return None
    try:
        cur_val = Decimal(str(up_num_ma5_rt_str).strip())
    except Exception:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DATE(MAX(trade_time)) FROM stock_wind.rt_k WHERE trade_time IS NOT NULL"
        )
        row_a = cur.fetchone()
        if not row_a or row_a[0] is None:
            return None
        anchor_date = _sql_date_to_py_date(row_a[0])
        if anchor_date is None:
            return None
        cur.execute(
            """
            SELECT up_num_ma5
            FROM stock_tushare.marketinfo
            WHERE up_num_ma5 IS NOT NULL
              AND trade_date < %s
            ORDER BY trade_date DESC
            LIMIT 500
            """,
            (anchor_date.strftime("%Y-%m-%d"),),
        )
        rows = cur.fetchall() or []
    values: list[Decimal] = []
    for r in rows:
        if r[0] is None:
            continue
        values.append(Decimal(str(r[0])))
    if not values:
        return None
    lt_cnt = sum(1 for v in values if v < cur_val)
    per = (Decimal(lt_cnt) / Decimal(len(values)) * Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return format(per, "f")


AMOUNT_TOP5_PCTILE_LOOKBACK_YEARS = 5
# Top5% 弹窗：start_date 模式全量上限（2005 起约 5200 交易日）；无 start_date 时与超跌弹窗 tail limit 一致
AMOUNT_TOP5_PCT_MODAL_SERIES_CAP = 6000
AMOUNT_TOP5_PCT_MODAL_LIMIT_TAIL = 1300


def _subtract_years_safe(d: date, years: int) -> date:
    """按自然年回退；2 月 29 日回退到 2 月 28 日，避免闰年异常。"""
    try:
        return d.replace(year=d.year - int(years))
    except ValueError:
        return d.replace(year=d.year - int(years), day=28)


def _read_rt_k_anchor_date(conn) -> date | None:
    """rt_k 最新自然日，作为盘中实时指标的锚定日。"""
    with conn.cursor() as cur:
        cur.execute("SELECT DATE(MAX(trade_time)) FROM stock_wind.rt_k WHERE trade_time IS NOT NULL")
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return _sql_date_to_py_date(row[0])


def _compute_stockstat_amount_top5_pct(conn) -> str | None:
    """
    实时成交额集中度：A 股非 ST 样本中，成交额排名前 ceil(5%) 家合计占全市场成交额比例。
    口径与页面提示一致，使用 rt_k 最新自然日行情与 rt_k_code_category A股分类。
    """
    sql = (
        """
        WITH sample AS (
            SELECT rt.ts_code, rt.amount
            FROM stock_wind.rt_k rt
            INNER JOIN stock_wind.rt_k_code_category c
              ON c.ts_code = rt.ts_code AND c.major_type = 'A股'
            LEFT JOIN stock_wind.stockstat s
              ON s.windcodes = rt.ts_code
            WHERE IFNULL(rt.vol, 0) > 0
              AND IFNULL(rt.amount, 0) > 0
              AND """
        + SQL_RT_K_FRESH_WHERE_RT
        + _LIMIT_STATS_SQL_NOT_ST_JOIN_S
        + """
        ),
        ranked AS (
            SELECT
                amount,
                ROW_NUMBER() OVER (ORDER BY amount DESC, ts_code ASC) AS rn,
                COUNT(*) OVER () AS cnt,
                SUM(amount) OVER () AS total_amount
            FROM sample
        )
        SELECT
            SUM(CASE WHEN rn <= CEIL(cnt * 0.05) THEN amount ELSE 0 END) AS top_amount,
            MAX(total_amount) AS total_amount
        FROM ranked
        """
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
    except Exception as e:
        logger.warning("stockstat Top5%%_pct 读取失败: %s", e)
        return None
    if not row or row[0] is None or row[1] in (None, 0):
        return None
    try:
        top_amount = Decimal(str(row[0]))
        total_amount = Decimal(str(row[1]))
        if total_amount <= 0:
            return None
        pct = (top_amount / total_amount * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        return format(pct, "f")
    except Exception as e:
        logger.warning("stockstat Top5%%_pct 计算失败: %s", e)
        return None


def _read_amount_top5_pct_percentile(
    conn_tushare, conn_wind, amount_top5_pct_str: str | None
) -> str | None:
    """当前 Top5%_pct 在近五年 marketinfo.amount_top5_pct 历史中的经验分位。"""
    if amount_top5_pct_str is None:
        return None
    try:
        cur_val = Decimal(str(amount_top5_pct_str).strip())
    except Exception:
        return None
    anchor_date = _read_rt_k_anchor_date(conn_wind)
    if anchor_date is None:
        return None
    lo_date = _subtract_years_safe(anchor_date, AMOUNT_TOP5_PCTILE_LOOKBACK_YEARS)
    with conn_tushare.cursor() as cur:
        cur.execute(
            """
            SELECT amount_top5_pct
            FROM stock_tushare.marketinfo
            WHERE amount_top5_pct IS NOT NULL
              AND trade_date >= %s
              AND trade_date < %s
            """,
            (lo_date.strftime("%Y-%m-%d"), anchor_date.strftime("%Y-%m-%d")),
        )
        rows = cur.fetchall() or []
    values: list[Decimal] = []
    for r in rows:
        if r[0] is not None:
            values.append(Decimal(str(r[0])))
    if not values:
        return None
    lt_cnt = sum(1 for v in values if v < cur_val)
    per = (Decimal(lt_cnt) / Decimal(len(values)) * Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return format(per, "f")


def _compute_amount_top5_pct_sigma2_bounds(conn_tushare, conn_wind) -> tuple[float | None, float | None]:
    """近五年 amount_top5_pct 的 mean±2σ，用于 Top5% 弹窗参考线。"""
    anchor_date = _read_rt_k_anchor_date(conn_wind)
    if anchor_date is None:
        return None, None
    lo_date = _subtract_years_safe(anchor_date, AMOUNT_TOP5_PCTILE_LOOKBACK_YEARS)
    with conn_tushare.cursor() as cur:
        cur.execute(
            """
            SELECT amount_top5_pct
            FROM stock_tushare.marketinfo
            WHERE amount_top5_pct IS NOT NULL
              AND trade_date >= %s
              AND trade_date < %s
            ORDER BY trade_date ASC
            """,
            (lo_date.strftime("%Y-%m-%d"), anchor_date.strftime("%Y-%m-%d")),
        )
        rows = cur.fetchall() or []
    vals = [_to_float_or_none(r[0]) for r in rows if r and r[0] is not None]
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return None, None
    mean_v = statistics.mean(vals)
    std_v = statistics.pstdev(vals)
    if std_v <= 0:
        return None, None
    return round(mean_v - 2 * std_v, 2), round(mean_v + 2 * std_v, 2)


def _parse_amount_top5_series_start_date(raw: str | None) -> date | None:
    """解析 amount_top5_pct_series 的 start_date（YYYY-MM-DD）。"""
    s = (raw or '').strip()
    if not s:
        return None
    try:
        dt = pd.to_datetime(s, errors='coerce')
        if pd.isna(dt):
            return None
        return dt.date()
    except Exception:
        return None


def _compute_amount_top5_pct_rolling_sigma2_series(
    trade_dates: list[date],
    values: list[float | None],
) -> tuple[list[float | None], list[float | None]]:
    """逐日滚动五年 mean±2σ（锚定日 t 不参与样本）；初段无窗口时用首条有效 2σ 水平回填。

    双指针 + 滑动和/平方和，O(n)；旧实现逐日重扫样本在 5000+ 交易日上要数秒，
    会超过首页弹窗 12s abort。
    """
    n = len(trade_dates)
    low_out: list[float | None] = [None] * n
    high_out: list[float | None] = [None] * n
    if n == 0:
        return low_out, high_out
    span_days = AMOUNT_TOP5_PCTILE_LOOKBACK_YEARS * 366 + 30
    left = 0
    sumX = 0.0
    sumX2 = 0.0
    cnt = 0
    first_low: float | None = None
    first_high: float | None = None

    for i in range(n):
        if i > 0:
            tdPrev = trade_dates[i - 1]
            vPrev = values[i - 1] if i - 1 < len(values) else None
            if tdPrev is not None and vPrev is not None:
                fv = float(vPrev)
                sumX += fv
                sumX2 += fv * fv
                cnt += 1
        td_i = trade_dates[i]
        if td_i is None:
            continue
        lo_bound = td_i - timedelta(days=span_days)
        while left < i:
            tdL = trade_dates[left]
            vL = values[left] if left < len(values) else None
            if tdL is None or vL is None:
                left += 1
                continue
            if tdL < lo_bound:
                fv = float(vL)
                sumX -= fv
                sumX2 -= fv * fv
                cnt -= 1
                left += 1
                continue
            break
        if cnt < 2:
            continue
        mean_v = sumX / cnt
        varPop = sumX2 / cnt - mean_v * mean_v
        if varPop < 0.0:
            varPop = 0.0
        std_v = varPop ** 0.5
        if std_v <= 0:
            continue
        low_v = round(mean_v - 2 * std_v, 2)
        high_v = round(mean_v + 2 * std_v, 2)
        low_out[i] = low_v
        high_out[i] = high_v
        if first_low is None:
            first_low = low_v
            first_high = high_v
    if first_low is not None and first_high is not None:
        for i in range(n):
            if low_out[i] is None:
                low_out[i] = first_low
                high_out[i] = first_high
    return low_out, high_out


# 一字涨/涨停/一字跌/跌停：stockstat 列名
_LIMIT_STATS_STOCKSTAT_FLAG_COL = {
    'cnt_high_limitone': 'high_limitone',
    'cnt_high_limit': 'high_limit',
    'cnt_high_limit_lifting': 'high_limit_lifting',
    'cnt_low_limitone': 'low_limitone',
    'cnt_low_limit': 'low_limit',
    'cnt_low_limit_lifting': 'low_limit_lifting',
}
# /api/limit_stats/detail 支持的 kind
_LIMIT_STATS_DETAIL_KINDS = frozenset(
    list(_LIMIT_STATS_HL_DETAIL_WHERE.keys())
    + list(_LIMIT_STATS_STOCKSTAT_FLAG_COL.keys())
    + [
        'cnt_lb_zt',
        'cnt_lb_dt',
        'zt_t1',
        'dt_t1',
    ]
)


def _limit_stats_sw_join_sql(on_ts_code_expr: str) -> str:
    """LEFT JOIN：每股一行 sw_name（is_new 为 Y/NULL；按 in_date 降序，同日多行取 sw_name 升序最小，与原 MIN 语义一致）。

    使用 ROW_NUMBER 一次扫描，配合索引 idx_member_sw_ts_indate(ts_code, in_date DESC) 利于按分区排序。
    用于：read_limit_stats_detail、_query_top_moves 等。
    """
    return (
        f" LEFT JOIN (\n"
        f"        SELECT z.ts_code, z.sw_name\n"
        f"        FROM (\n"
        f"            SELECT ts_code, sw_name,\n"
        f"                ROW_NUMBER() OVER (\n"
        f"                    PARTITION BY ts_code\n"
        f"                    ORDER BY in_date DESC, sw_name ASC\n"
        f"                ) AS rn\n"
        f"            FROM stock_tushare.index_member_all_sw\n"
        f"            WHERE is_new = 'Y' OR is_new IS NULL\n"
        f"        ) z\n"
        f"        WHERE z.rn = 1\n"
        f"    ) rt_sw_map ON rt_sw_map.ts_code = {on_ts_code_expr}\n"
    )


# 与 _limit_stats_sw_join_sql 配套；SELECT 列表第 7 列仍为 sw_name
_LIMIT_STATS_SW_NAME_SELECT = "rt_sw_map.sw_name AS sw_name"


def _limit_stats_sql_not_st_ths_alias(alias: str) -> str:
    """limit_list_ths 剔除 ST：alias 为表别名（如 l）。"""
    return (
        f" AND ({alias}.name IS NULL OR ("
        f" {alias}.name NOT LIKE '*ST%%' AND {alias}.name NOT LIKE 'ST%%' AND {alias}.name NOT LIKE 'S*ST%%'"
        f"))"
    )


def _limit_detail_rows_to_dicts(rows, include_trade_date: bool = False):
    """明细行转 JSON（与 read_limit_lb_zt_detail 输出字段一致，可选 trade_date）。"""
    out_rows = []
    for r in rows or []:
        amount_raw = _to_float_or_none(r[2] if len(r) > 2 else None)
        amount_yi = round(amount_raw / 1e8, 2) if amount_raw is not None else None
        item = {
            'windcodes': r[0] if len(r) > 0 else None,
            'sec_name': r[1] if len(r) > 1 else None,
            'amount_yi': amount_yi,
            'pctchange': _to_float_or_none(r[3] if len(r) > 3 else None),
            'lu_desc': r[4] if len(r) > 4 else None,
            'tag': r[5] if len(r) > 5 else None,
            'sw_name': r[6] if len(r) > 6 else None,
        }
        if include_trade_date and len(r) > 7 and r[7] is not None:
            item['trade_date'] = str(r[7])
        out_rows.append(item)
    return out_rows


# 同目录存在 database_pool.py 时用连接池；单独复制本文件到其它工程时可无该模块，改为直连
try:
    from database_pool import get_db_connection as _pool_get_db_connection
    from database_pool import retry_with_backoff as _pool_retry_with_backoff

    get_db_connection = _pool_get_db_connection
    retry_with_backoff = _pool_retry_with_backoff
    _HAS_DATABASE_POOL = True
except ModuleNotFoundError:
    _HAS_DATABASE_POOL = False

    @contextmanager
    def get_db_connection(pool_name='default', timeout=30):
        """无 database_pool 时：每次 with 新建 stock_wind 直连，退出时关闭。"""
        conn = pymysql.connect(**DB_CONFIG)
        try:
            yield conn
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def retry_with_backoff(func, *args, **kwargs):
        """无连接池模块时的轻量重试（与 database_pool 行为相近）。"""
        max_retry_times = 3
        retry_delay = 0.2
        max_retry_delay = 3.0
        last_err = None
        for attempt in range(max_retry_times):
            try:
                return func(*args, **kwargs)
            except (pymysql.Error, ConnectionError, TimeoutError) as e:
                last_err = e
                err_code = getattr(e, 'args', [0])[0] if getattr(e, 'args', None) else 0
                if err_code in (1213, 1205) and attempt >= 2:
                    raise
                delay = 0.1 if err_code in (1213, 1205) else min(retry_delay * (2 ** attempt), max_retry_delay)
                if attempt == max_retry_times - 1:
                    raise
                logger.warning("数据库操作失败，%.1fs 后重试 (%d/%d): %s", delay, attempt + 1, max_retry_times, e)
                time.sleep(delay)
        if last_err:
            raise last_err

# 牛散持股：方案名仅展示；关联人写入 JSON，参与 top10_floatholders.holder_name OR LIKE
app = Flask(
    __name__,
    template_folder=os.path.join(_APP_DIR, 'templates'),
    static_folder=os.path.join(_APP_DIR, 'static'),
)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'rt-monitor-secret')
# 静态 css/js 不长期缓存，避免改表间距等样式后浏览器仍用旧文件
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
# debug=False 时 Flask 默认 TEMPLATES_AUTO_RELOAD=False，Jinja 会长期缓存模板；改 rt_monitor.html 后不重启会一直返回旧 DOM
app.config['TEMPLATES_AUTO_RELOAD'] = True
socketio = SocketIO(app, cors_allowed_origins="*")

# 禁止缓存的 JSON GET：除精确 path 外按「后缀」匹配，兼容反代挂载后 request.path 为 /prefix/api/... 仍下发 no-store
_JSON_API_NO_STORE_SUFFIXES: tuple[str, ...] = (
    '/api/limit_stats',
    '/api/limit_stats_detail',
    '/api/rt_median',
    '/api/margin_daily_recent',
    '/api/margin_rzmre_idx_series',
    '/api/marketinfo_recent',
    '/api/fina_arturn_days_table',
    '/api/fina_fixed_assets_revenue_series',
    '/api/fina_stock_basic_act',
    '/api/fina_top10_floatholders',
    '/api/fina_system_scheme_codes',
    '/api/up_num_ma5_series',
    '/api/amount_top5_pct_series',
    '/api/etf_bias_series',
    '/api/etf_mcd_series',
    '/api/niusan_floatholder_history',
    '/api/niusan_holder_config',
    '/api/niusan_floatholder_detail',
    '/api/niusan_scheme_holdings',
    '/api/sw_industry_stats',
    '/api/sw_industry_limit_detail',
    '/api/sw_index_amt_pctile_series',
    '/api/index_industry_stats',
    '/api/opt-t-board',
    '/api/opt-iv-board',
    '/api/opt-sp-plan',
)


def _request_path_matches_no_store_json_api(path: str) -> bool:
    if not path:
        return False
    if path.startswith('/api/limit_stats/'):
        return True
    for suf in _JSON_API_NO_STORE_SUFFIXES:
        if path == suf or path.endswith(suf):
            return True
    return False


@app.after_request
def _no_cache_limit_stats_json_api(resp):
    """
    禁止缓存涨跌家数相关 GET：部分环境按「仅路径」缓存 /api/limit_stats，
    会使 ?detail=1&kind= 的明细请求拿到面板 JSON（无 rows），弹窗显示暂无数据。
    """
    try:
        path = request.path or ''
    except RuntimeError:
        return resp
    if _request_path_matches_no_store_json_api(path):
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
    from rt_monitor.core.response_compress import maybeGzipFlaskResponse

    try:
        return maybeGzipFlaskResponse(resp, request)
    except RuntimeError:
        return resp


@app.context_processor
def _inject_rt_monitor_template_globals():
    """静态资源版本号 + 前端 API 路径前缀（反代子路径 / Werkzeug SCRIPT_NAME）。"""
    envp = (os.getenv('RT_MONITOR_API_PREFIX') or '').strip().rstrip('/')
    try:
        sr = (request.script_root or '').rstrip('/')
    except RuntimeError:
        sr = ''
    prefix = envp or sr
    return {
        'rt_monitor_static_ver': RT_MONITOR_STATIC_VER,
        'rt_monitor_api_prefix': prefix,
    }


def _render_rt_monitor_no_cache():
    """监控页禁止缓存 HTML，避免旧表头与新 JS 列数错位（如缺「成交额」列或缩量%列）。"""
    resp = make_response(render_template('rt_monitor.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    # 浏览器 F12 → 网络 → 选文档：可确认是否本进程（与 web_dashboard 旧页区分）
    resp.headers['X-RT-Monitor-Build'] = RT_MONITOR_STATIC_VER
    resp.headers['X-RT-Monitor-App'] = 'rt_monitor_app'
    return resp


def _render_rt_monitor_fina_no_cache():
    """财报筛选单页（应收账款周转天数等）：禁止缓存 HTML。"""
    resp = make_response(render_template('rt_monitor_fina.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['X-RT-Monitor-Build'] = RT_MONITOR_STATIC_VER
    resp.headers['X-RT-Monitor-App'] = 'rt_monitor_app'
    return resp


def _render_rt_monitor_sw_no_cache():
    """申万行业分析单页：禁止缓存 HTML。"""
    resp = make_response(render_template('rt_monitor_sw.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['X-RT-Monitor-Build'] = RT_MONITOR_STATIC_VER
    resp.headers['X-RT-Monitor-App'] = 'rt_monitor_app'
    return resp


def _render_rt_monitor_index_no_cache():
    """宽基指数行业分析单页：禁止缓存 HTML。"""
    resp = make_response(
        render_template(
            'rt_monitor_sw.html',
            sw_page_mode='index',
            sw_api_path='/api/index_industry_stats',
            sw_page_title='宽基指数行业分析',
            sw_show_scheme_config=False,
            sw_index_options=[{'index_code': code, 'index_name': name} for code, name in INDEX_MONITOR_CODES],
        )
    )
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['X-RT-Monitor-Build'] = RT_MONITOR_STATIC_VER
    resp.headers['X-RT-Monitor-App'] = 'rt_monitor_app'
    return resp


# 只读 index_stat，不写表。TTL 内复用缓存，过期则重新读表
rt_date_lists = None      # 启动时 get_historical_median() 取一次，供 API 返回 t5_dates 等
rt_table_cache = None     # 最近一次读到的 12 行表格
rt_last_read_ts = 0       # 上次从 index_stat 读表时间
RT_MEDIAN_CACHE_TTL = 5  # 超过此秒数则重新读表
etf_ma_signal_cache = None
etf_ma_signal_last_read_ts = 0
ETF_MA_SIGNAL_CACHE_TTL = 12  # 放宽 TTL，避免每轮轮询都触发重查询导致首载和盘中抖动
ETF_MA_SIGNAL_REFRESH_MIN_GAP = 2.0
ETF_MA_SIGNAL_PROCESS_ISOLATION = os.getenv(
    "RT_MONITOR_MA_SIGNAL_PROCESS_ISOLATION", "1"
) not in ("0", "false", "False")
ETF_MA_SIGNAL_PROCESS_TIMEOUT_SEC = max(
    30.0, float(os.getenv("RT_MONITOR_MA_SIGNAL_PROCESS_TIMEOUT_SEC", "120"))
)
etf_ma_signal_refreshing = False
etf_ma_signal_refresh_started_ts = 0.0
_ma_signal_process_executor = None
_ma_signal_process_executor_lock = threading.Lock()
etf_bias_cache = None
etf_bias_last_read_ts = 0
ETF_BIAS_CACHE_TTL = 3  # BIAS 表查询较重，短 TTL 缓存降低数据库压力
limit_stats_cache = None
limit_stats_last_read_ts = 0
LIMIT_STATS_CACHE_TTL = 1  # 与首页 1s 轮询对齐；实测重算约 0.2～0.3s，1s 节拍有余量
_cache_lock = threading.Lock()
# BIAS 弹窗序列 /api/etf_bias_series：同参数短 TTL 命中则跳过 DB + 百分位（减轻重复打开与 rolling 热路径）
ETF_BIAS_SERIES_CACHE_TTL = 4.0
ETF_BIAS_SERIES_CACHE_MAX = 64
_etf_bias_series_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
ETF_MCD_SERIES_CACHE_TTL = 4.0
ETF_MCD_SERIES_CACHE_MAX = 64
_etf_mcd_series_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
LIMIT_STATS_DETAIL_CACHE_TTL = 3.0
LIMIT_STATS_DETAIL_CACHE_MAX = 96
_limit_stats_detail_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
# 首页 Top30：HTTP 只读内存；后台按查询耗时在 2～8s 自适应刷一份
TOP_MOVES_HOME_LIMIT = 30
TOP_MOVES_REFRESH_MIN_SEC = 2.0
TOP_MOVES_REFRESH_MAX_SEC = 8.0
TOP_MOVES_CACHE_TTL = TOP_MOVES_REFRESH_MIN_SEC
TOP_MOVES_CACHE_MAX = 32
_top_moves_cache: OrderedDict[int, tuple[float, dict]] = OrderedDict()
_topMovesLastOk: dict[int, dict] = {}
_topMovesInflight: dict[int, threading.Event] = {}
_topMovesInflightLock = threading.Lock()
_topMovesRefreshStarted = False
_topMovesLastHttpMono = 0.0
TOP_MOVES_IDLE_PAUSE_SEC = 15.0
TOP_MOVES_FACTOR_TTL = 120.0
_topMovesFactorByCode: dict = {}
_topMovesFactorExp = 0.0
MA_SIGNAL_SERIES_CACHE_TTL = 5.0
MA_SIGNAL_SERIES_CACHE_MAX = 96
_ma_signal_series_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
SLOW_API_WARN_SECONDS = 0.45
SLOW_API_WARN_COOLDOWN_SEC = 20.0
_slowApiLastWarnAt: dict[str, float] = {}


def _small_ttl_cache_get(cache_obj: OrderedDict, cache_key, now_ts: float):
    with _cache_lock:
        hit = cache_obj.get(cache_key)
        if hit is not None and now_ts < hit[0]:
            cache_obj.move_to_end(cache_key)
            return hit[1]
    return None


def _small_ttl_cache_put(cache_obj: OrderedDict, cache_key, payload: dict, ttl: float, max_items: int):
    with _cache_lock:
        while len(cache_obj) >= max_items:
            cache_obj.popitem(last=False)
        cache_obj[cache_key] = (time.time() + ttl, payload)
        cache_obj.move_to_end(cache_key)




def _log_slow_api(api_name: str, started_at: float, extra: str = ''):
    elapsed = time.perf_counter() - started_at
    if elapsed < SLOW_API_WARN_SECONDS:
        return
    nowMono = time.monotonic()
    last = _slowApiLastWarnAt.get(api_name, 0.0)
    if nowMono - last < SLOW_API_WARN_COOLDOWN_SEC:
        return
    _slowApiLastWarnAt[api_name] = nowMono
    logger.warning("slow_api %s elapsed=%.3fs %s", api_name, elapsed, extra)


def _etf_mcd_series_cache_get(cache_key: tuple):
    now_ts = time.time()
    with _cache_lock:
        hit = _etf_mcd_series_cache.get(cache_key)
        if hit is not None and now_ts < hit[0]:
            _etf_mcd_series_cache.move_to_end(cache_key)
            return hit[1]
    return None


def _etf_mcd_series_cache_put(cache_key: tuple, payload: dict):
    with _cache_lock:
        while len(_etf_mcd_series_cache) >= ETF_MCD_SERIES_CACHE_MAX:
            _etf_mcd_series_cache.popitem(last=False)
        _etf_mcd_series_cache[cache_key] = (time.time() + ETF_MCD_SERIES_CACHE_TTL, payload)
        _etf_mcd_series_cache.move_to_end(cache_key)

INDEX_STAT_TABLE = 'index_stat'

# ETF BIAS 表格：行顺序与表头 15MIN/60MIN 下 10MA、20MA、20EMA（已去除 60MA/60EMA）
ETF_BIAS_ORDER = [
    '510300.SH', '510500.SH', '588000.SH', '159915.SZ', '588080.SH',
    '510050.SH', '159919.SZ', '159922.SZ', '159901.SZ',
]
ETF_BIAS_FREQS = ['15MIN', '60MIN']  # etf_rt_min 用大写；etf_stk_mins_ma 用 15min/60min
# (DB 列名, 表头展示名)：10MA、20MA、20EMA
ETF_BIAS_COLUMNS = [
    ('per_bias10ma', '10MA'), ('per_bias20ma', '20MA'), ('per_bias20ema', '20EMA'),
]
# 日线 D：仅 etf_bias_realtime 表有 freq='D' 表示日线；etf_daily 表无 freq 字段，仅为按日行情
ETF_BIAS_DAILY_FREQ = 'D'
ETF_BIAS_DAILY_COLUMNS = [('per_bias10ma', '10MA')]


def _row_from_result(r, col_names, columns_spec):
    """从查询结果行 r 和列名 col_names 按 columns_spec (db_col, label) 填出 {label: value}。"""
    if r is None:
        return {label: None for _, label in columns_spec}
    name_to_idx = {name: i for i, name in enumerate(col_names)}
    out = {}
    for db_col, label in columns_spec:
        idx = name_to_idx.get(db_col)
        if idx is None:
            out[label] = None
            continue
        val = r[idx]
        if val is None or (isinstance(val, float) and pd.isna(val)):
            out[label] = None
        else:
            try:
                out[label] = round(float(val), 2)
            except (TypeError, ValueError):
                out[label] = None
    return out


# 实时 BIAS 视为有效的最大间隔（秒），超则回退到 etf_rt_min / etf_stk_mins_ma 重算。
# etf_rt_min.py 命中最新 15/60 分钟 bar 后约 SLEEP_AFTER_HIT_SEC(50)+jitter(10) 秒才再次执行 run_realtime_bias_update，
# 若此处取 60 秒，会在两轮写入之间误判「过期」，前端 3 秒轮询时会在「实时表」与 qfq 重算之间来回跳。取 120 秒覆盖正常间隔，daemon 长时间停跑时再回退。
# 约定：DB 的 updated_at 与 datetime.now() 需同一时区（如均为本机本地时间），否则窗口可能不准。
ETF_BIAS_REALTIME_MAX_AGE = 120


def _read_etf_bias_per_from_realtime(conn, ts_code: str, freq_rt: str):
    """从 etf_bias_realtime 读 daemon 用 1250 根历史算好的 per（按 updated_at 最新值）。"""
    sql = (
        "SELECT per_bias10ma, per_bias20ma, per_bias20ema "
        "FROM stock_tushare.etf_bias_realtime "
        "WHERE ts_code = %s AND freq = %s "
        "ORDER BY updated_at DESC LIMIT 1"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (ts_code, freq_rt))
            r = cur.fetchone()
    except Exception as e:
        logger.debug("读取 etf_bias_realtime per 失败 ts_code=%s freq=%s: %s", ts_code, freq_rt, e)
        return None
    if not r:
        return None
    out = {}
    for i, (_, label) in enumerate(ETF_BIAS_COLUMNS):
        val = r[i]
        if val is None or (isinstance(val, float) and pd.isna(val)):
            out[label] = None
        else:
            try:
                out[label] = round(float(val), 2)
            except (TypeError, ValueError):
                out[label] = None
    if all(v is None for v in out.values()):
        return None
    return out


def _read_etf_bias_daily_per10_from_realtime(conn, ts_code: str):
    """日线 D：读 etf_bias_realtime(freq=D) 的 per_bias10ma（daemon 1250 日口径）。"""
    sql = (
        "SELECT per_bias10ma FROM stock_tushare.etf_bias_realtime "
        "WHERE ts_code = %s AND freq = 'D' AND trade_time IS NOT NULL "
        "ORDER BY updated_at DESC LIMIT 1"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (ts_code,))
            r = cur.fetchone()
    except Exception as e:
        logger.debug("读取日线 etf_bias_realtime per 失败 ts_code=%s: %s", ts_code, e)
        return None
    if not r or r[0] is None:
        return None
    try:
        return round(float(r[0]), 2)
    except (TypeError, ValueError):
        return None


def read_etf_bias_table():
    """BIAS(Per) 表：15/60/D 均直接读 etf_bias_realtime 中 per（与 etf_rt_min 1250 根口径一致）；无行时再本地用≥1250 根 K 重算回退。"""
    rows_out = []
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    except Exception as e:
        logger.exception("连接 stock_tushare 失败: %s", e)
        return []
    try:
        for ts_code in ETF_BIAS_ORDER:
            row = {'ts_code': ts_code, '15MIN': {}, '60MIN': {}, ETF_BIAS_DAILY_FREQ: {}}
            for freq in ETF_BIAS_FREQS:
                try:
                    rt_map = _read_etf_bias_per_from_realtime(conn, ts_code, freq)
                    if rt_map is not None:
                        row[freq] = rt_map
                        continue
                    df_freq = _load_merged_minute_close_df(conn, ts_code, freq, PER_BIAS_WINDOW)
                    per_vals = _calc_bias_per_from_qfq_close(
                        df_freq['close'] if df_freq is not None and not df_freq.empty else pd.Series(dtype=float)
                    )
                    row[freq] = {label: per_vals.get(label) for _, label in ETF_BIAS_COLUMNS}
                except Exception as e:
                    logger.debug("qfq 重算 BIAS 跳过 ts_code=%s freq=%s: %s", ts_code, freq, e)
                    row[freq] = {label: None for _, label in ETF_BIAS_COLUMNS}
            try:
                d10 = _read_etf_bias_daily_per10_from_realtime(conn, ts_code)
                if d10 is not None:
                    row[ETF_BIAS_DAILY_FREQ] = {'10MA': d10}
                else:
                    df_d = _load_daily_close_df(conn, ts_code, PER_BIAS_WINDOW)
                    per_d = _calc_bias_per_from_qfq_close(
                        df_d['close'] if df_d is not None and not df_d.empty else pd.Series(dtype=float)
                    )
                    row[ETF_BIAS_DAILY_FREQ] = {'10MA': per_d.get('10MA')}
            except Exception as e:
                logger.debug("qfq 重算日线 BIAS 跳过 ts_code=%s: %s", ts_code, e)
                row[ETF_BIAS_DAILY_FREQ] = {label: None for _, label in ETF_BIAS_DAILY_COLUMNS}
            rows_out.append(row)
    finally:
        conn.close()
    return rows_out


def get_prev_n_trade_dates(reference_date, n: int = 5) -> list[str]:
    """从 stock_tushare.trade_cal 取「基准日」之前的 n 个交易日（按交易日历，非自然日）。
    reference_date: date/datetime/str(YYYY-MM-DD)。返回 YYYY-MM-DD 字符串列表，从近到远；失败或不足返回 []。"""
    if reference_date is None:
        return []
    try:
        if isinstance(reference_date, str):
            ref_str = reference_date[:10] if len(reference_date) >= 10 else reference_date
        elif hasattr(reference_date, 'date'):
            ref_str = reference_date.date().isoformat()
        else:
            ref_str = str(reference_date)[:10]
    except Exception:
        return []
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    except Exception as e:
        logger.exception("连接 stock_tushare 失败（前 N 交易日）: %s", e)
        return []
    try:
        sql = (
            "SELECT cal_date FROM stock_tushare.trade_cal "
            "WHERE exchange = 'SSE' AND is_open = 1 AND cal_date < %s "
            "ORDER BY cal_date DESC LIMIT %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (ref_str, n))
            rows = cur.fetchall()
        out = [str(r[0])[:10] for r in rows if r and r[0]]
        return out
    except Exception as e:
        logger.exception("查询前 %d 个交易日失败: %s", n, e)
        return []
    finally:
        conn.close()


def get_prev_trade_date_for_idx(ts_code: str):
    """从 stock_tushare.idx_mins 中获取指定指数上一交易日的日期（YYYY-MM-DD 字符串）。
    表中无独立 trade_date 列，这里使用 DATE(trade_time) 聚合。仅只读，获取失败或不足两日时返回 None。"""
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    except Exception as e:
        logger.exception("连接 stock_tushare 失败（获取上一交易日）: %s", e)
        return None
    try:
        sql = (
            "SELECT DISTINCT DATE(trade_time) AS d "
            "FROM stock_tushare.idx_mins "
            "WHERE ts_code = %s AND trade_time IS NOT NULL "
            "ORDER BY d DESC LIMIT 2"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (ts_code,))
            rows = cur.fetchall()
        if not rows or len(rows) < 2:
            return None
        prev_date = rows[1][0]
        # 直接按日期字符串返回 YYYY-MM-DD
        if isinstance(prev_date, datetime):
            return prev_date.date().isoformat()
        return str(prev_date)
    except Exception as e:
        logger.exception("获取 %s 上一交易日失败: %s", ts_code, e)
        return None
    finally:
        conn.close()


def _to_float_or_none(v):
    """辅助：将值转为 float，失败返回 None。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ocf_to_or_display_pct(raw) -> float | None:
    """fina_indicator.ocf_to_or 为小数比（库内 decimal 四位）；×100 为百分数并保留两位。

    用 Decimal(str(raw)) 做乘法与 quantize，避免二进制 float 误差导致 JSON/前端出现「整数+.00」假象。
    不在 SQL 中对 ocf_to_or 做 ROUND，避免小比值先被舍成 0。
    """
    if raw is None:
        return None
    try:
        d = Decimal(str(raw))
    except (ArithmeticError, ValueError, TypeError):
        return None
    out = (d * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(out)


def _mysql_cal_date_key(val) -> str:
    """pymysql 返回的 date/datetime/str 统一为 YYYY-MM-DD，供 idx_mins 按交易日分组与 IN 列表对齐。"""
    if val is None:
        return ""
    if isinstance(val, datetime):
        return val.date().isoformat()
    if isinstance(val, date):
        return val.isoformat()
    s = str(val).strip()
    return s[:10] if len(s) >= 10 else s


def _fixed_idx_minute_axis(base_date) -> tuple[list, list]:
    """固定时间轴 9:30-11:30、13:01-15:00，返回 (dt_list, times_iso)。rt_idx_min 有无数据都使用同一 X 轴。"""
    if isinstance(base_date, str):
        from datetime import date as _date_cls
        base_date = _date_cls.fromisoformat(base_date)
    elif hasattr(base_date, 'date'):
        base_date = base_date.date()
    cur = datetime(base_date.year, base_date.month, base_date.day, 9, 30)
    end_am = datetime(base_date.year, base_date.month, base_date.day, 11, 30)
    dt_list = []
    while cur <= end_am:
        dt_list.append(cur)
        cur = cur + pd.Timedelta(minutes=1)
    cur = datetime(base_date.year, base_date.month, base_date.day, 13, 1)
    end_pm = datetime(base_date.year, base_date.month, base_date.day, 15, 0)
    while cur <= end_pm:
        dt_list.append(cur)
        cur = cur + pd.Timedelta(minutes=1)
    times = [dt.isoformat() for dt in dt_list]
    return dt_list, times


def _compute_idx_predict_today_amounts(conn, ts_code: str, latest_trade_date, dt_list, today_cumsum: list):
    """
    与 read_idx_minute_diff 一致：通达信线性外推 + Wind 前9日同结构比值预测，返回 (predict_linear_yi, predict_wind_yi)，单位亿。
    today_cumsum 为与 dt_list 等长的当日分钟累计成交额（亿）；latest_trade_date 可为 date 或 None。
    """
    latest_idx_with_today = -1
    for i, v in enumerate(today_cumsum):
        if v is not None:
            latest_idx_with_today = i
    TRADING_MINUTES_FULL_DAY = 240
    predict_today_amount = None
    if latest_idx_with_today >= 0:
        opened_minutes = latest_idx_with_today + 1
        if opened_minutes >= 20:
            latest_cum_today = today_cumsum[latest_idx_with_today]
            if latest_cum_today is not None:
                try:
                    predict_today_amount = round(
                        float(latest_cum_today) * TRADING_MINUTES_FULL_DAY / float(opened_minutes), 0
                    )
                except Exception:
                    predict_today_amount = None
    predict_today_amount_wind = None
    if latest_trade_date and latest_idx_with_today >= 0:
        sql_prev9_dates = (
            "SELECT DISTINCT DATE(trade_time) AS d FROM stock_tushare.idx_mins "
            "WHERE ts_code = %s AND trade_time IS NOT NULL AND DATE(trade_time) < %s "
            "ORDER BY d DESC LIMIT 9"
        )
        with conn.cursor() as cur:
            cur.execute(sql_prev9_dates, (ts_code, latest_trade_date))
            prev9_rows = cur.fetchall()
        prev9_dates = [str(r[0]) for r in prev9_rows if r and r[0]]
        if len(prev9_dates) >= 1:
            prev9_cum_at_idx = []
            prev9_full_day_yi = []
            norm_days = list(
                dict.fromkeys(_mysql_cal_date_key(d) for d in prev9_dates if _mysql_cal_date_key(d))
            )
            by_day: dict[str, list] = defaultdict(list)
            if norm_days:
                ph = ",".join(["%s"] * len(norm_days))
                sql_batch = (
                    "SELECT DATE(trade_time) AS d, trade_time, amount FROM stock_tushare.idx_mins "
                    f"WHERE ts_code = %s AND DATE(trade_time) IN ({ph}) "
                    "AND trade_time IS NOT NULL ORDER BY trade_time"
                )
                try:
                    with conn.cursor() as cur:
                        cur.execute(sql_batch, (ts_code, *norm_days))
                        for r in cur.fetchall() or []:
                            if not r or len(r) < 3:
                                continue
                            dk = _mysql_cal_date_key(r[0])
                            if dk:
                                by_day[dk].append((r[1], r[2]))
                except Exception as e:
                    logger.warning("分钟预测：idx_mins 前9日批量读取失败 ts=%s: %s", ts_code, e)
            for d in prev9_dates:
                d_key = _mysql_cal_date_key(d)
                rows_d = by_day.get(d_key, [])
                day_amount_by_min = {}
                day_full_sum = 0.0
                for trade_time, amount in rows_d:
                    if isinstance(trade_time, datetime):
                        dt = trade_time
                    else:
                        try:
                            dt = datetime.fromtimestamp(trade_time.timestamp())
                        except Exception:
                            dt = None
                    key = f"{dt.hour:02d}:{dt.minute:02d}" if dt else str(trade_time)[11:16] if len(str(trade_time)) >= 16 else ""
                    if key and key not in day_amount_by_min:
                        v = _to_float_or_none(amount)
                        day_amount_by_min[key] = v
                        if v is not None:
                            day_full_sum += v
                cum_d = []
                cum = 0.0
                for dt in dt_list:
                    key = f"{dt.hour:02d}:{dt.minute:02d}"
                    at = day_amount_by_min.get(key)
                    if at is not None:
                        cum += float(at) / 1e8
                    cum_d.append(round(cum, 2))
                prev9_cum_at_idx.append(cum_d)
                prev9_full_day_yi.append(round(day_full_sum / 1e8, 2))
            latest_cum_today_val = today_cumsum[latest_idx_with_today]
            if latest_cum_today_val is not None and latest_idx_with_today < len(dt_list):
                vals_at_idx = []
                for cum_d in prev9_cum_at_idx:
                    if latest_idx_with_today < len(cum_d) and cum_d[latest_idx_with_today] is not None:
                        v = cum_d[latest_idx_with_today]
                        if isinstance(v, (int, float)) and v > 0:
                            vals_at_idx.append(float(v))
                if vals_at_idx and prev9_full_day_yi:
                    prev9_avg_cum = sum(vals_at_idx) / len(vals_at_idx)
                    prev9_avg_full = sum(prev9_full_day_yi) / len(prev9_full_day_yi)
                    if prev9_avg_cum > 0 and prev9_avg_full > 0:
                        try:
                            ratio = float(latest_cum_today_val) / prev9_avg_cum
                            predict_today_amount_wind = round(ratio * prev9_avg_full, 0)
                        except Exception:
                            pass
    return predict_today_amount, predict_today_amount_wind


def read_idx_minute_diff(ts_code: str):
    """读取 399317.SZ 当日 rt_idx_min 与上一交易日 idx_mins 的每分钟成交额差值（亿元）及当日 close。
    Max(60) 数据源：index_daily 同 ts_code 近 60 交易日 amount（千元）最大日，整数亿（JSON 键仍为 marketinfo_max60_*）。
    Ma(20) 见 read_idx_minute_diff_prev5 响应（与「分钟差值 vs 前五均值」表头同显）。
    仅只读 stock_tushare 库，不修改任何表。"""
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    except Exception as e:
        logger.exception("连接 stock_tushare 失败（分钟差值）: %s", e)
        _, times_empty = _fixed_idx_minute_axis(datetime.now().date())
        n = len(times_empty)
        return {
            'ts_code': ts_code,
            'times': times_empty,
            'diff_amount': [None] * n,
            'close': [None] * n,
            'diff_cumsum': [None] * n,
            'yesterday_cumsum': [None] * n,
            'today_cumsum': [None] * n,
            'predict_today_amount': None,
            'predict_today_amount_wind': None,
            'yesterday_full_amount_yi': None,
            'marketinfo_max60_trade_date': None,
            'marketinfo_max60_amount': None,
            'trade_date': None,
            'prev_trade_date': None,
            'error': '数据库连接失败',
        }
    try:
        # 从 rt_idx_min 取最新交易日（不一定是今天），作为"当日"基准
        sql_latest_date = (
            "SELECT MAX(DATE(trade_time)) "
            "FROM stock_tushare.rt_idx_min "
            "WHERE ts_code = %s AND trade_time IS NOT NULL"
        )
        with conn.cursor() as cur:
            cur.execute(sql_latest_date, (ts_code,))
            row_date = cur.fetchone()
        latest_trade_date = row_date[0] if row_date and row_date[0] else None

        # 用最新交易日查 rt_idx_min 分钟数据
        sql_today = (
            "SELECT trade_time, amount, close "
            "FROM stock_tushare.rt_idx_min "
            "WHERE ts_code = %s AND DATE(trade_time) = %s "
            "ORDER BY trade_time"
        )
        rows_today = []
        if latest_trade_date:
            with conn.cursor() as cur:
                cur.execute(sql_today, (ts_code, latest_trade_date))
                rows_today = cur.fetchall()

        today_amount_by_min = {}
        today_close_by_min = {}
        for trade_time, amount, close in rows_today:
            if isinstance(trade_time, datetime):
                dt = trade_time
            else:
                try:
                    dt = datetime.fromtimestamp(trade_time.timestamp())
                except Exception:
                    dt = None
            if dt is not None:
                key = f"{dt.hour:02d}:{dt.minute:02d}"
            else:
                s = str(trade_time)
                key = s[11:16] if len(s) >= 16 else s
            if key not in today_amount_by_min:
                today_amount_by_min[key] = _to_float_or_none(amount)
                today_close_by_min[key] = _to_float_or_none(close)

        # 从 idx_mins 取「当日基准」的上一个交易日
        yday_map = {}
        prev_trade_date = None
        if latest_trade_date:
            sql_prev = (
                "SELECT DISTINCT DATE(trade_time) AS d "
                "FROM stock_tushare.idx_mins "
                "WHERE ts_code = %s AND trade_time IS NOT NULL AND DATE(trade_time) < %s "
                "ORDER BY d DESC LIMIT 1"
            )
            with conn.cursor() as cur:
                cur.execute(sql_prev, (ts_code, latest_trade_date))
                row_prev = cur.fetchone()
            if row_prev and row_prev[0]:
                prev_trade_date = str(row_prev[0])
        if not prev_trade_date:
            prev_trade_date = get_prev_trade_date_for_idx(ts_code)
        if prev_trade_date:
            sql_yday = (
                "SELECT trade_time, amount "
                "FROM stock_tushare.idx_mins "
                "WHERE ts_code = %s AND trade_time IS NOT NULL "
                "AND DATE(trade_time) = %s "
                "ORDER BY trade_time"
            )
            with conn.cursor() as cur:
                cur.execute(sql_yday, (ts_code, prev_trade_date))
                rows_yday = cur.fetchall()
            for trade_time, amount in rows_yday:
                if isinstance(trade_time, datetime):
                    dt = trade_time
                else:
                    try:
                        dt = datetime.fromtimestamp(trade_time.timestamp())
                    except Exception:
                        dt = None
                if dt is not None:
                    key = f"{dt.hour:02d}:{dt.minute:02d}"
                else:
                    s = str(trade_time)
                    key = s[11:16] if len(s) >= 16 else s
                if key not in yday_map:
                    yday_map[key] = _to_float_or_none(amount)
        # 上一交易日全日成交额(亿)，用于前端显示预测与昨日的变化
        yesterday_full_amount_yi = None
        if yday_map:
            total_yday = sum(float(v) for v in yday_map.values() if v is not None)
            if total_yday >= 0:
                yesterday_full_amount_yi = round(total_yday / 1e8, 2)

        # T-60 峰值：index_daily(ts_code) 与分钟图同源指数，近 60 交易日最大 amount→整数亿；键名仍为 marketinfo_max60_* 供前端兼容
        marketinfo_max60_trade_date = None
        marketinfo_max60_amount = None
        try:
            _yi60, _ds60 = _index_daily_max_amount_last_n_trade_dates(conn, ts_code, 60)
            if _yi60 is not None:
                marketinfo_max60_amount = int(_yi60)
            if _ds60:
                marketinfo_max60_trade_date = _ds60
        except Exception as e_mi:
            logger.warning("分钟差值图 Max(60) index_daily(%s) 失败: %s", ts_code, e_mi)

        # 固定时间轴：9:30-11:30、13:01-15:00，有无数据都一致
        _raw = latest_trade_date or datetime.now().date()
        if isinstance(_raw, str):
            from datetime import date as _date_cls
            base_date = _date_cls.fromisoformat(_raw)
        elif isinstance(_raw, datetime):
            base_date = _raw.date()
        else:
            base_date = _raw
        dt_list, times = _fixed_idx_minute_axis(base_date)

        # X 轴固定；仅当 rt_idx_min 在该分钟有数据时才计算差值，无数据的分钟不当作 0，显示为空
        diff_list = []
        closes = []
        for dt in dt_list:
            key = f"{dt.hour:02d}:{dt.minute:02d}"
            amt_today = today_amount_by_min.get(key)
            amt_yday = yday_map.get(key)
            closes.append(today_close_by_min.get(key))
            if amt_today is None:
                # 该分钟 rt_idx_min 尚无数据，不显示（不当作 0）
                diff_list.append(None)
                continue
            if amt_yday is None:
                diff_list.append(None)
                continue
            try:
                diff = (float(amt_today) - float(amt_yday)) / 1e8
                diff_list.append(round(diff, 2))
            except Exception:
                diff_list.append(None)

        # 累计值：差额累计与当日累计仅在有 rt_idx_min 数据的分钟有值；昨日累计按固定时序逐分钟累加显示
        diff_cumsum = []
        yesterday_cumsum = []
        today_cumsum = []
        cum_diff = 0.0
        cum_yday = 0.0
        cum_today = 0.0
        for i, dt in enumerate(dt_list):
            key = f"{dt.hour:02d}:{dt.minute:02d}"
            at = today_amount_by_min.get(key)
            ay = yday_map.get(key)
            yv = (float(ay) if ay is not None else 0.0) / 1e8
            cum_yday += yv
            yesterday_cumsum.append(round(cum_yday, 2))
            if at is None:
                diff_cumsum.append(None)
                today_cumsum.append(None)
                continue
            d = diff_list[i] if i < len(diff_list) else None
            if d is not None:
                cum_diff += float(d)
            diff_cumsum.append(round(cum_diff, 2))
            cum_today += float(at) / 1e8
            today_cumsum.append(round(cum_today, 2))

        predict_today_amount, predict_today_amount_wind = _compute_idx_predict_today_amounts(
            conn, ts_code, latest_trade_date, dt_list, today_cumsum
        )

        return {
            'ts_code': ts_code,
            'times': times,
            'diff_amount': diff_list,
            'close': closes,
            'diff_cumsum': diff_cumsum,
            'yesterday_cumsum': yesterday_cumsum,
            'today_cumsum': today_cumsum,
            'predict_today_amount': predict_today_amount,
            'predict_today_amount_wind': predict_today_amount_wind,
            'yesterday_full_amount_yi': yesterday_full_amount_yi,
            'marketinfo_max60_trade_date': marketinfo_max60_trade_date,
            'marketinfo_max60_amount': marketinfo_max60_amount,
            'trade_date': str(latest_trade_date) if latest_trade_date else None,
            'prev_trade_date': prev_trade_date,
        }
    except Exception as e:
        logger.exception("读取 %s 分钟差值失败: %s", ts_code, e)
        _, times_empty = _fixed_idx_minute_axis(datetime.now().date())
        n = len(times_empty)
        return {
            'ts_code': ts_code,
            'times': times_empty,
            'diff_amount': [None] * n,
            'close': [None] * n,
            'diff_cumsum': [None] * n,
            'yesterday_cumsum': [None] * n,
            'today_cumsum': [None] * n,
            'predict_today_amount': None,
            'predict_today_amount_wind': None,
            'yesterday_full_amount_yi': None,
            'marketinfo_max60_trade_date': None,
            'marketinfo_max60_amount': None,
            'prev_trade_date': None,
            'error': str(e),
        }
    finally:
        conn.close()


def read_idx_minute_diff_prev5(ts_code: str):
    """读取 rt_idx_min 当日与「该日期的前五个交易日」的分钟成交额均值 的差值及累计值。
    前五个交易日按 stock_tushare.trade_cal 交易日历取（交易日，非自然日）；无日历则退化为 idx_mins/rt_idx_min 表内最近 5 个日期。
    附带与 read_idx_minute_diff 一致的今日预测（线性+Wind）及 index_daily Ma(20)（亿），供表头展示。
    仅只读 stock_tushare，不修改任何表。"""
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    except Exception as e:
        logger.exception("连接 stock_tushare 失败（前五日均值）: %s", e)
        _, times_empty = _fixed_idx_minute_axis(datetime.now().date())
        n = len(times_empty)
        return {
            'ts_code': ts_code,
            'times': times_empty,
            'diff_amount': [None] * n,
            'close': [None] * n,
            'diff_cumsum': [None] * n,
            'prev5_avg_cumsum': [None] * n,
            'today_cumsum': [None] * n,
            'predict_today_amount': None,
            'predict_today_amount_wind': None,
            'index_daily_ma20_amount_yi': None,
            'trade_date': None,
            'prev_trade_dates': [],
            'error': '数据库连接失败',
        }
    try:
        sql_latest_date = (
            "SELECT MAX(DATE(trade_time)) FROM stock_tushare.rt_idx_min "
            "WHERE ts_code = %s AND trade_time IS NOT NULL"
        )
        with conn.cursor() as cur:
            cur.execute(sql_latest_date, (ts_code,))
            row_date = cur.fetchone()
        latest_trade_date = row_date[0] if row_date and row_date[0] else None

        sql_today = (
            "SELECT trade_time, amount, close FROM stock_tushare.rt_idx_min "
            "WHERE ts_code = %s AND DATE(trade_time) = %s ORDER BY trade_time"
        )
        rows_today = []
        if latest_trade_date:
            with conn.cursor() as cur:
                cur.execute(sql_today, (ts_code, latest_trade_date))
                rows_today = cur.fetchall()

        today_amount_by_min = {}
        today_close_by_min = {}
        for trade_time, amount, close in rows_today:
            if isinstance(trade_time, datetime):
                dt = trade_time
            else:
                try:
                    dt = datetime.fromtimestamp(trade_time.timestamp())
                except Exception:
                    dt = None
            if dt is not None:
                key = f"{dt.hour:02d}:{dt.minute:02d}"
            else:
                key = str(trade_time)[11:16] if len(str(trade_time)) >= 16 else str(trade_time)
            if key not in today_amount_by_min:
                today_amount_by_min[key] = _to_float_or_none(amount)
                today_close_by_min[key] = _to_float_or_none(close)

        # 前五个交易日：按交易日历（交易日，非自然日）取“参考日”之前的 5 个交易日；
        # 注意：早盘 rt_idx_min 可能还没有任何数据，此时 latest_trade_date=None。
        # 这里用“参考日”兜底为今天，确保前五日均值线也能显示（不依赖 rt_idx_min 是否已有数据）。
        ref_date_for_prev5 = latest_trade_date or datetime.now().date()
        prev_dates: list[str] = get_prev_n_trade_dates(ref_date_for_prev5, 5)
        if len(prev_dates) < 5:
            sql_prev5_idx = (
                "SELECT DISTINCT DATE(trade_time) AS d FROM stock_tushare.idx_mins "
                "WHERE ts_code = %s AND trade_time IS NOT NULL AND DATE(trade_time) < %s "
                "ORDER BY d DESC LIMIT 5"
            )
            with conn.cursor() as cur:
                cur.execute(sql_prev5_idx, (ts_code, ref_date_for_prev5))
                prev_dates = [str(r[0]) for r in cur.fetchall() if r and r[0]]
        if len(prev_dates) < 5:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT DATE(trade_time) AS d FROM stock_tushare.rt_idx_min "
                    "WHERE ts_code = %s AND trade_time IS NOT NULL AND DATE(trade_time) < %s ORDER BY d DESC LIMIT 5",
                    (ts_code, ref_date_for_prev5),
                )
                prev_dates = [str(r[0]) for r in cur.fetchall() if r and r[0]]

        def _fill_minute_amounts(dates_to_use: list[str], from_table: str) -> None:
            """用给定日期列表从指定表填充 minute_amounts（原地修改）；多日用 IN 一次查齐，避免逐日往返。"""
            if not dates_to_use:
                return
            norm_days = list(
                dict.fromkeys(_mysql_cal_date_key(d) for d in dates_to_use if _mysql_cal_date_key(d))
            )
            if not norm_days:
                return
            ph = ",".join(["%s"] * len(norm_days))
            sql_batch = (
                f"SELECT DATE(trade_time) AS d, trade_time, amount FROM stock_tushare.{from_table} "
                f"WHERE ts_code = %s AND DATE(trade_time) IN ({ph}) "
                "AND trade_time IS NOT NULL ORDER BY trade_time"
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(sql_batch, (ts_code, *norm_days))
                    all_rows = cur.fetchall() or []
            except Exception as e:
                logger.warning("前五日均值：批量读 %s 失败 ts=%s: %s", from_table, ts_code, e)
                return
            by_day: dict[str, list] = defaultdict(list)
            for r in all_rows:
                if not r or len(r) < 3:
                    continue
                dk = _mysql_cal_date_key(r[0])
                if dk:
                    by_day[dk].append((r[1], r[2]))
            for d in dates_to_use:
                rows_d = by_day.get(_mysql_cal_date_key(d), [])
                for trade_time, amount in rows_d:
                    if isinstance(trade_time, datetime):
                        key = f"{trade_time.hour:02d}:{trade_time.minute:02d}"
                    else:
                        s = str(trade_time)
                        key = s[11:16] if len(s) >= 16 else s
                    v = _to_float_or_none(amount)
                    if v is not None:
                        minute_amounts.setdefault(key, []).append(v)

        # 按分钟汇总：每个 minute key 对应前 5 日该分钟的 amount 列表，再求均值；先查 idx_mins，无数据再查 rt_idx_min
        minute_amounts: dict[str, list[float]] = {}
        _fill_minute_amounts(prev_dates, "idx_mins")
        if not minute_amounts and prev_dates:
            _fill_minute_amounts(prev_dates, "rt_idx_min")
        # 若交易日历的 5 日在表内均无分钟数据，改用「表内实际有数据的最近 5 个日期」再填一次，保证前五日均值线能显示
        if not minute_amounts:
            data_dates: list[str] = []
            for tbl in ("idx_mins", "rt_idx_min"):
                sql_data_dates = (
                    "SELECT DISTINCT DATE(trade_time) AS d FROM stock_tushare." + tbl + " "
                    "WHERE ts_code = %s AND trade_time IS NOT NULL AND DATE(trade_time) < %s ORDER BY d DESC LIMIT 5"
                )
                with conn.cursor() as cur:
                    cur.execute(sql_data_dates, (ts_code, ref_date_for_prev5))
                    data_dates = [str(r[0]) for r in cur.fetchall() if r and r[0]]
                if data_dates:
                    break
            if data_dates:
                _fill_minute_amounts(data_dates, "idx_mins")
                if not minute_amounts:
                    _fill_minute_amounts(data_dates, "rt_idx_min")

        prev5_avg_by_min = {}
        for key, vals in minute_amounts.items():
            if vals:
                prev5_avg_by_min[key] = sum(vals) / len(vals)

        # 固定时间轴：有无数据都一致
        _raw = latest_trade_date or datetime.now().date()
        if isinstance(_raw, str):
            from datetime import date as _date_cls
            base_date = _date_cls.fromisoformat(_raw)
        elif isinstance(_raw, datetime):
            base_date = _raw.date()
        else:
            base_date = _raw
        dt_list, times = _fixed_idx_minute_axis(base_date)

        # 仅当 rt_idx_min 在该分钟有数据时才计算差值，无数据的分钟不当作 0，显示为空
        diff_list = []
        closes = []
        for dt in dt_list:
            key = f"{dt.hour:02d}:{dt.minute:02d}"
            amt_today = today_amount_by_min.get(key)
            amt_avg = prev5_avg_by_min.get(key)
            closes.append(today_close_by_min.get(key))
            if amt_today is None:
                diff_list.append(None)
                continue
            if amt_avg is None:
                diff_list.append(None)
                continue
            try:
                diff = (float(amt_today) - float(amt_avg)) / 1e8
                diff_list.append(round(diff, 2))
            except Exception:
                diff_list.append(None)

        # 累计值：差额累计与当日累计仅在有 rt_idx_min 数据的分钟有值；前五日均值累计按固定时序逐分钟累加显示
        diff_cumsum = []
        prev5_avg_cumsum = []
        today_cumsum = []
        cum_diff = 0.0
        cum_prev5 = 0.0
        cum_today = 0.0
        for i, dt in enumerate(dt_list):
            key = f"{dt.hour:02d}:{dt.minute:02d}"
            at = today_amount_by_min.get(key)
            avg_val = (prev5_avg_by_min.get(key) or 0.0) / 1e8
            cum_prev5 += avg_val
            prev5_avg_cumsum.append(round(cum_prev5, 2))
            if at is None:
                diff_cumsum.append(None)
                today_cumsum.append(None)
                continue
            d = diff_list[i] if i < len(diff_list) else None
            if d is not None:
                cum_diff += float(d)
            diff_cumsum.append(round(cum_diff, 2))
            cum_today += float(at) / 1e8
            today_cumsum.append(round(cum_today, 2))

        predict_today_amount, predict_today_amount_wind = _compute_idx_predict_today_amounts(
            conn, ts_code, latest_trade_date, dt_list, today_cumsum
        )
        index_daily_ma20_amount_yi = None
        try:
            index_daily_ma20_amount_yi = _index_daily_mean_amount_last_n_trade_dates(conn, ts_code, 20)
        except Exception as e_m20:
            logger.warning("前五日均值分钟图 Ma(20) index_daily(%s) 失败: %s", ts_code, e_m20)

        return {
            'ts_code': ts_code,
            'times': times,
            'diff_amount': diff_list,
            'close': closes,
            'diff_cumsum': diff_cumsum,
            'prev5_avg_cumsum': prev5_avg_cumsum,
            'today_cumsum': today_cumsum,
            'predict_today_amount': predict_today_amount,
            'predict_today_amount_wind': predict_today_amount_wind,
            'index_daily_ma20_amount_yi': index_daily_ma20_amount_yi,
            'trade_date': str(latest_trade_date) if latest_trade_date else None,
            'prev_trade_dates': prev_dates,
        }
    except Exception as e:
        logger.exception("读取 %s 前五日均值分钟差值失败: %s", ts_code, e)
        _, times_empty = _fixed_idx_minute_axis(datetime.now().date())
        n = len(times_empty)
        return {
            'ts_code': ts_code,
            'times': times_empty,
            'diff_amount': [None] * n,
            'close': [None] * n,
            'diff_cumsum': [None] * n,
            'prev5_avg_cumsum': [None] * n,
            'today_cumsum': [None] * n,
            'predict_today_amount': None,
            'predict_today_amount_wind': None,
            'index_daily_ma20_amount_yi': None,
            'trade_date': None,
            'prev_trade_dates': [],
            'error': str(e),
        }
    finally:
        conn.close()


class DataFetcher:
    """仅用于取历史取数日期列表（t5_date_list 等），供 API 返回。写表与当日/历史计算均由盘中实时统计负责。"""

    def __init__(self):
        self.logger = logger

    def get_historical_median(self):
        """仅查 stk_factor_pro：上一日/前五/前二十/前六十/前一百二十交易日复利中位数，历史数据只读一次。"""
        def _query():
            import pymysql
            result = {
                'prev1_median_pct': None, 'prev1_count': 0, 'prev1_date': None,
                'prev5_median_pct': None, 'prev5_count': 0, 'prev5_dates': None,
                'prev20_median_pct': None, 'prev20_count': 0, 'prev20_dates': None,
                'prev60_median_pct': None, 'prev60_count': 0, 'prev60_dates': None,
                'prev120_median_pct': None, 'prev120_count': 0, 'prev120_dates': None,
            }
            df5 = df20 = df60 = df120 = None
            conn_ts = pymysql.connect(**DB_CONFIG_TUSHARE)
            try:
                with conn_ts.cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT trade_date FROM stock_tushare.stk_factor_pro "
                        "ORDER BY trade_date DESC LIMIT 121"
                    )
                    rows = cur.fetchall()
                if not rows:
                    return result
                dates = [str(r[0]).replace('-', '')[:8] for r in rows]
                last1 = dates[0]
                last5 = dates[:5]
                last20 = dates[:20]
                last60 = dates[:60]
                last120 = dates[:120]

                df1 = pd.read_sql(
                    "SELECT pct_chg FROM stock_tushare.stk_factor_pro "
                    "WHERE trade_date = %s AND pct_chg IS NOT NULL",
                    conn_ts, params=[last1]
                )
                if not df1.empty:
                    s1 = pd.to_numeric(df1['pct_chg'], errors='coerce').dropna()
                    if len(s1) > 0:
                        result['prev1_median_pct'] = round(float(s1.median()), 2)
                        result['prev1_count'] = int(len(s1))
                        result['prev1_date'] = _fmt_date(last1)

                if len(last5) >= 5:
                    ph = ','.join(['%s'] * 5)
                    df5 = pd.read_sql(
                        "SELECT ts_code, trade_date, pct_chg FROM stock_tushare.stk_factor_pro "
                        "WHERE trade_date IN ({}) AND pct_chg IS NOT NULL ORDER BY ts_code, trade_date".format(ph),
                        conn_ts, params=last5
                    )
                if len(last20) >= 20:
                    ph20 = ','.join(['%s'] * 20)
                    df20 = pd.read_sql(
                        "SELECT ts_code, trade_date, pct_chg FROM stock_tushare.stk_factor_pro "
                        "WHERE trade_date IN ({}) AND pct_chg IS NOT NULL ORDER BY ts_code, trade_date".format(ph20),
                        conn_ts, params=last20
                    )
                if len(last60) >= 60:
                    ph60 = ','.join(['%s'] * 60)
                    df60 = pd.read_sql(
                        "SELECT ts_code, trade_date, pct_chg FROM stock_tushare.stk_factor_pro "
                        "WHERE trade_date IN ({}) AND pct_chg IS NOT NULL ORDER BY ts_code, trade_date".format(ph60),
                        conn_ts, params=last60
                    )
                if len(last120) >= 120:
                    ph120 = ','.join(['%s'] * 120)
                    df120 = pd.read_sql(
                        "SELECT ts_code, trade_date, pct_chg FROM stock_tushare.stk_factor_pro "
                        "WHERE trade_date IN ({}) AND pct_chg IS NOT NULL ORDER BY ts_code, trade_date".format(ph120),
                        conn_ts, params=last120
                    )
            finally:
                conn_ts.close()

            def _compound_median(df, date_list, key_prefix):
                if df is None or df.empty or len(date_list) < 2:
                    return
                df = df.copy()
                df['pct_chg'] = pd.to_numeric(df['pct_chg'], errors='coerce')
                df = df.dropna(subset=['pct_chg'])
                date_set = set(d[:8] if len(d) >= 8 else d for d in date_list)
                ret_list = []
                for ts_code, grp in df.groupby('ts_code'):
                    grp = grp.sort_values('trade_date')
                    grp_dates = set(str(d).replace('-', '')[:8] for d in grp['trade_date'])
                    if len(grp) < len(date_list) or grp_dates != date_set:
                        continue
                    r = (1 + grp['pct_chg'].values.astype(float) / 100.0).prod() - 1.0
                    ret_list.append(r * 100.0)
                if ret_list:
                    result[key_prefix + '_median_pct'] = round(float(pd.Series(ret_list).median()), 2)
                    result[key_prefix + '_count'] = len(ret_list)
                    result[key_prefix + '_dates'] = _fmt_date(date_list[0]) + ' 至 ' + _fmt_date(date_list[-1])

            if df5 is not None and len(last5) >= 5:
                _compound_median(df5, last5, 'prev5')
            if df20 is not None and len(last20) >= 20:
                _compound_median(df20, last20, 'prev20')
            if df60 is not None and len(last60) >= 60:
                _compound_median(df60, last60, 'prev60')
            if df120 is not None and len(last120) >= 120:
                _compound_median(df120, last120, 'prev120')
            result['t5_date_list'] = [_fmt_date(d) for d in last5] if len(last5) >= 5 else None
            result['t20_date_list'] = [_fmt_date(d) for d in last20] if len(last20) >= 20 else None
            result['t60_date_list'] = [_fmt_date(d) for d in last60] if len(last60) >= 60 else None
            result['t120_date_list'] = [_fmt_date(d) for d in last120] if len(last120) >= 120 else None
            return result
        try:
            return retry_with_backoff(_query)
        except Exception as e:
            self.logger.exception("获取历史涨跌中位数失败")
            return {'error': f'查询失败: {str(e)}'}

data_fetcher = DataFetcher()


# 表格 12 行固定顺序：(index_code, compindex 列名, 展示名)；前三张表首行为全部A股，涨跌幅% 表首行在 API 中单独用国证A指。compindex* 由盘中实时统计维护。
# 涨跌幅% 表 12 行：展示名 → index_daily/rt_idx_k 的 ts_code（与 盘中实时统计 CATEGORY_TO_TS_CODE 一致）
IDX_PCT_CATEGORY_TO_TS = {
    '国证A指': '399317.SZ',
    'A500': '000510.SH',
    '沪深300': '000300.SH',
    '中证500': '000905.SH',
    '中证1000': '000852.SH',
    '中证2000': '932000.CSI',
    '北证50': '899050.BJ',
    '上证50': '000016.SH',
    '科创综指': '000680.SH',
    '科创50': '000688.SH',
    '创业板指': '399006.SZ',
    '创业板综': '399102.SZ',
}
# 区间涨跌幅默认拼 rt_idx_k；空集=全部可拼（中证2000/北证50 由 tdx_opt_live 写入）
IDX_PCT_NO_RT_SPLICE_CATEGORIES = frozenset()

# Gap 全历史分位样本：按自然日缓存，盘中跳空样本不变，避免 rt_median 每 5 秒重扫 index_daily
_idx_gap_hist_cache: dict[str, tuple[float, ...]] = {}
_idx_gap_hist_day = ''
_idx_gap_hist_lock = threading.Lock()


def _idx_gap_percentile(sample, gap) -> int | None:
    """经验分位 0–100：100 ×（样本中严格小于 gap 的个数）/ n，四舍五入到整数。
    与成交额 Top5% Per 一样用严格小于；样本不含今日。"""
    if gap is None or not sample:
        return None
    g = _to_float_or_none(gap)
    if g is None:
        return None
    lt = sum(1 for v in sample if v < g)
    raw = Decimal(lt) / Decimal(len(sample)) * Decimal(100)
    return int(raw.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _load_idx_gap_history(conn, ts_codes: list[str]) -> dict[str, tuple[float, ...]]:
    """各指数 index_daily 全历史跳空%：(open-pre_close)/pre_close×100，不含 CURDATE()。"""
    uniq = sorted({c for c in ts_codes if c})
    if not uniq:
        return {}
    placeholders = ','.join(['%s'] * len(uniq))
    sql = (
        'SELECT ts_code, (open - pre_close) / pre_close * 100 AS gap_pct '
        'FROM stock_tushare.index_daily '
        f'WHERE ts_code IN ({placeholders}) '
        'AND open IS NOT NULL AND pre_close IS NOT NULL AND pre_close <> 0 '
        'AND trade_date < CURDATE()'
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, uniq)
            raw = cur.fetchall() or []
    except Exception as e:
        logger.warning('涨跌幅表 Gap 全历史分位样本读取失败: %s', e)
        return {}
    buckets: dict[str, list[float]] = {c: [] for c in uniq}
    for row in raw:
        if not row or len(row) < 2:
            continue
        av = _to_float_or_none(row[1])
        if av is None:
            continue
        buckets.setdefault(str(row[0]), []).append(av)
    return {k: tuple(v) for k, v in buckets.items() if v}


def _ensure_idx_gap_history(conn, ts_codes: list[str]) -> dict[str, tuple[float, ...]]:
    """当日已加载且覆盖请求代码时直接返回内存样本。"""
    global _idx_gap_hist_day
    today = date.today().isoformat()
    uniq = [c for c in ts_codes if c]
    with _idx_gap_hist_lock:
        cached = (
            _idx_gap_hist_day == today
            and _idx_gap_hist_cache
            and all(c in _idx_gap_hist_cache for c in uniq)
        )
        if cached:
            return dict(_idx_gap_hist_cache)
    loaded = _load_idx_gap_history(conn, uniq)
    if not loaded:
        with _idx_gap_hist_lock:
            return dict(_idx_gap_hist_cache)
    with _idx_gap_hist_lock:
        _idx_gap_hist_cache.update(loaded)
        _idx_gap_hist_day = today
        return dict(_idx_gap_hist_cache)


# Pct / Intra / Gap / Amp 全历史样本，按自然日缓存。键为 pct、intra、gap、amp。
_IDX_METRIC_SAMPLE_KEYS = ('pct', 'intra', 'gap', 'amp')
_idx_metric_hist_cache: dict[str, dict[str, tuple[float, ...]]] = {}
_idx_metric_hist_day = ''
_idx_metric_hist_lock = threading.Lock()


def _load_idx_metric_history(conn, ts_codes: list[str]) -> dict[str, dict[str, tuple[float, ...]]]:
    """各指数全历史四条序列，不含 CURDATE()。
    pct 用 pct_chg（已是百分数）；intra=(close-open)/open×100；
    gap=(open-pre_close)/pre_close×100；amp=(high-low)/pre_close×100。
    """
    uniq = sorted({c for c in ts_codes if c})
    if not uniq:
        return {}
    placeholders = ','.join(['%s'] * len(uniq))
    sql = (
        'SELECT ts_code, pct_chg, '
        'CASE WHEN open IS NOT NULL AND open <> 0 AND close IS NOT NULL '
        'THEN (close - open) / open * 100 END, '
        'CASE WHEN open IS NOT NULL AND pre_close IS NOT NULL AND pre_close <> 0 '
        'THEN (open - pre_close) / pre_close * 100 END, '
        'CASE WHEN high IS NOT NULL AND low IS NOT NULL AND pre_close IS NOT NULL AND pre_close <> 0 '
        'THEN (high - low) / pre_close * 100 END '
        'FROM stock_tushare.index_daily '
        f'WHERE ts_code IN ({placeholders}) AND trade_date < CURDATE()'
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, uniq)
            raw = cur.fetchall() or []
    except Exception as e:
        logger.warning('涨跌幅表全历史分位样本读取失败: %s', e)
        return {}
    buckets: dict[str, dict[str, list[float]]] = {
        c: {k: [] for k in _IDX_METRIC_SAMPLE_KEYS} for c in uniq
    }
    for row in raw:
        if not row or len(row) < 5:
            continue
        ts = str(row[0])
        slot = buckets.setdefault(ts, {k: [] for k in _IDX_METRIC_SAMPLE_KEYS})
        for idx, key in enumerate(_IDX_METRIC_SAMPLE_KEYS, start=1):
            av = _to_float_or_none(row[idx])
            if av is not None:
                slot[key].append(av)
    out: dict[str, dict[str, tuple[float, ...]]] = {}
    for ts, slot in buckets.items():
        packed = {k: tuple(v) for k, v in slot.items() if v}
        if packed:
            out[ts] = packed
    return out


def _ensure_idx_metric_history(conn, ts_codes: list[str]) -> dict[str, dict[str, tuple[float, ...]]]:
    """当日已加载且覆盖请求代码时直接返回内存样本。"""
    global _idx_metric_hist_day
    today = date.today().isoformat()
    uniq = [c for c in ts_codes if c]
    with _idx_metric_hist_lock:
        cached = (
            _idx_metric_hist_day == today
            and _idx_metric_hist_cache
            and all(c in _idx_metric_hist_cache for c in uniq)
        )
        if cached:
            return dict(_idx_metric_hist_cache)
    loaded = _load_idx_metric_history(conn, uniq)
    if not loaded:
        with _idx_metric_hist_lock:
            return dict(_idx_metric_hist_cache)
    with _idx_metric_hist_lock:
        _idx_metric_hist_cache.update(loaded)
        _idx_metric_hist_day = today
        return dict(_idx_metric_hist_cache)


# ETF Ma/Ema 表：AMT/Pct/Intra/Gap/Amp 全历史样本。AMT 为 etf_daily.amount（千元）÷1e5 的亿元。
_ETF_METRIC_SAMPLE_KEYS = ('amt', 'pct', 'intra', 'gap', 'amp')
_etf_metric_hist_cache: dict[str, dict[str, tuple[float, ...]]] = {}
_etf_metric_hist_day = ''
_etf_metric_hist_lock = threading.Lock()


def _load_etf_metric_history(conn, ts_codes: list[str]) -> dict[str, dict[str, tuple[float, ...]]]:
    """各 ETF etf_daily 全历史五条序列，不含 CURDATE()。"""
    uniq = sorted({c for c in ts_codes if c})
    if not uniq:
        return {}
    placeholders = ','.join(['%s'] * len(uniq))
    sql = (
        'SELECT ts_code, '
        'CASE WHEN amount IS NOT NULL THEN amount / 100000 END, '
        'pct_chg, '
        'CASE WHEN open IS NOT NULL AND open <> 0 AND close IS NOT NULL '
        'THEN (close - open) / open * 100 END, '
        'CASE WHEN open IS NOT NULL AND pre_close IS NOT NULL AND pre_close <> 0 '
        'THEN (open - pre_close) / pre_close * 100 END, '
        'CASE WHEN high IS NOT NULL AND low IS NOT NULL AND pre_close IS NOT NULL AND pre_close <> 0 '
        'THEN (high - low) / pre_close * 100 END '
        'FROM stock_tushare.etf_daily '
        f'WHERE ts_code IN ({placeholders}) AND trade_date < CURDATE()'
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, uniq)
            raw = cur.fetchall() or []
    except Exception as e:
        logger.warning('MA 信号表全历史分位样本读取失败: %s', e)
        return {}
    buckets: dict[str, dict[str, list[float]]] = {
        c: {k: [] for k in _ETF_METRIC_SAMPLE_KEYS} for c in uniq
    }
    for row in raw:
        if not row or len(row) < 6:
            continue
        ts = str(row[0])
        slot = buckets.setdefault(ts, {k: [] for k in _ETF_METRIC_SAMPLE_KEYS})
        for idx, key in enumerate(_ETF_METRIC_SAMPLE_KEYS, start=1):
            av = _to_float_or_none(row[idx])
            if av is not None:
                slot[key].append(av)
    out: dict[str, dict[str, tuple[float, ...]]] = {}
    for ts, slot in buckets.items():
        packed = {k: tuple(v) for k, v in slot.items() if v}
        if packed:
            out[ts] = packed
    return out


def _ensure_etf_metric_history(conn, ts_codes: list[str]) -> dict[str, dict[str, tuple[float, ...]]]:
    """当日已加载且覆盖请求代码时直接返回内存样本。"""
    global _etf_metric_hist_day
    today = date.today().isoformat()
    uniq = [c for c in ts_codes if c]
    with _etf_metric_hist_lock:
        cached = (
            _etf_metric_hist_day == today
            and _etf_metric_hist_cache
            and all(c in _etf_metric_hist_cache for c in uniq)
        )
        if cached:
            return dict(_etf_metric_hist_cache)
    loaded = _load_etf_metric_history(conn, uniq)
    if not loaded:
        with _etf_metric_hist_lock:
            return dict(_etf_metric_hist_cache)
    with _etf_metric_hist_lock:
        _etf_metric_hist_cache.update(loaded)
        _etf_metric_hist_day = today
        return dict(_etf_metric_hist_cache)


def _attach_etf_ma_metric_percentiles(rows: list) -> list:
    """给 MA 信号行补 etf_amount_per / etf_pct_per / etf_intra_per / etf_gap_per / etf_amp_per。"""
    codes = [str(r.get('ts_code') or '') for r in rows if r and r.get('ts_code')]
    if not codes:
        return rows
    conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    try:
        hist = _ensure_etf_metric_history(conn, codes)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    live_keys = (
        ('amt', 'etf_amount_yi', 'etf_amount_per'),
        ('pct', 'etf_pct', 'etf_pct_per'),
        ('intra', 'etf_intra', 'etf_intra_per'),
        ('gap', 'etf_gap', 'etf_gap_per'),
        ('amp', 'etf_amp', 'etf_amp_per'),
    )
    for row in rows:
        if not row:
            continue
        samples = hist.get(str(row.get('ts_code') or '')) or {}
        for sample_key, live_key, per_key in live_keys:
            row[per_key] = _idx_gap_percentile(samples.get(sample_key) or (), row.get(live_key))
    return rows


def _safe_attach_etf_ma_metric_percentiles(rows: list) -> list:
    try:
        return _attach_etf_ma_metric_percentiles(rows)
    except Exception as e:
        logger.warning('MA 信号表全历史分位失败: %s', e)
        return rows

MEDIAN_TABLE_SPEC = [
    (None, None, '全部A股'),
    ('000510.SH', 'compindex000510', 'A500'),
    ('000300.SH', 'compindex000300', '沪深300'),
    ('000905.SH', 'compindex000905', '中证500'),
    ('000852.SH', 'compindex000852', '中证1000'),
    ('932000.CSI', 'compindex932000', '中证2000'),
    ('899050.BJ', 'compindex899050', '北证50'),
    ('000016.SH', 'compindex000016', '上证50'),
    ('000680.SH', 'compindex000680', '科创综指'),
    ('000688.SH', 'compindex000688', '科创50'),
    ('399006.SZ', 'compindex399006', '创业板指'),
    ('399102.SZ', 'compindex399102', '创业板综'),
]

# 成交额（亿）表各行对应的 index_daily.ts_code（「全部A股」无单码：MAX T-60 用 marketinfo.amount）
AMOUNT_CATEGORY_TO_INDEX_TS = {spec[2]: spec[0] for spec in MEDIAN_TABLE_SPEC if spec[0]}

# 「全部A股」行 MAX T-60 / 峰值日 与 index_daily 行区分数据源（见 get_rt_median 成交额表附加逻辑）
AMOUNT_ALL_A_CATEGORY = '全部A股'


# --- 成交额历史极值/均值：services/amount_extremes（P5.1）---
from rt_monitor.services.amount_extremes import (  # noqa: E402
    _marketinfo_max_amount_last_n_trade_dates,
    _marketinfo_min_amount_last_n_trade_dates,
    _index_daily_max_amount_last_n_trade_dates,
    _index_daily_min_amount_last_n_trade_dates,
    _batch_index_daily_peak_amount_last_n_trade_dates,
    _batch_index_daily_valley_amount_last_n_trade_dates,
    _amount_extreme_to_yi_date,
    _marketinfo_max_amount_all,
    _index_daily_max_amount_all,
    _batch_index_daily_max_amount_all,
    _blank_amount_extreme_fields,
    _index_daily_mean_amount_last_n_trade_dates,
)


# ---------- index_stat 只读（写表由盘中实时统计负责） ----------
INDEX_STAT_ORDER = [spec[2] for spec in MEDIAN_TABLE_SPEC]


def read_index_stat():
    """从 stock_wind.index_stat 读取 12 行，按 MEDIAN_TABLE_SPEC 顺序返回。含 category, med, amount, uppct 及指数项 idx_pct_t/idx_gap_t/idx_intra_t/idx_amp_t。"""
    sql_base = (
        "SELECT index_name, `med-t` AS t, `med-t1` AS t1, `med-t5` AS t5, `med-t20` AS t20, `med-t60` AS t60, `med-t120` AS t120, "
        "`amount-t` AS amount_t, `amount-t1` AS amount_t1, `amount-t5` AS amount_t5, `amount-t20` AS amount_t20, `amount-t60` AS amount_t60, `amount-t120` AS amount_t120, "
        "`uppct-t` AS uppct_t, `uppct-t1` AS uppct_t1, `uppct-t5` AS uppct_t5, `uppct-t20` AS uppct_t20, `uppct-t60` AS uppct_t60, `uppct-t120` AS uppct_t120 "
    )
    sql_with_idx = sql_base + ", `idx_pct_t`, `idx_gap_t`, `idx_intra_t`, `idx_amp_t` FROM stock_wind.{}".format(INDEX_STAT_TABLE)
    sql_no_idx = sql_base + " FROM stock_wind.{}".format(INDEX_STAT_TABLE)
    try:
        with get_db_connection() as conn:
            try:
                df = pd.read_sql(sql_with_idx, conn)
            except Exception:
                df = pd.read_sql(sql_no_idx, conn)
                df['idx_pct_t'] = None
                df['idx_gap_t'] = None
                df['idx_intra_t'] = None
                df['idx_amp_t'] = None
        if df.empty:
            return []
        # 排序：全部A股…创业板综(12) + 国证A指(13)；涨跌幅% 表单独用国证A指首行，见 get_rt_median 的 table_idx
        order_for_sort = INDEX_STAT_ORDER + ['国证A指']
        df['_ord'] = df['index_name'].map(lambda x: order_for_sort.index(x) if x in order_for_sort else 999)
        df = df[df['index_name'].isin(order_for_sort)].sort_values('_ord').drop(columns=['_ord'])

        def _int_or_none(v):
            if pd.isna(v):
                return None
            try:
                return int(v)
            except (ValueError, TypeError):
                return None

        def _float_or_none(v):
            if pd.isna(v):
                return None
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        return [
            {
                'category': row['index_name'],
                't': _float_or_none(row['t']),
                't1': _float_or_none(row['t1']),
                't5': _float_or_none(row['t5']),
                't20': _float_or_none(row['t20']),
                't60': _float_or_none(row['t60']),
                't120': _float_or_none(row['t120']),
                'amount_t': _int_or_none(row.get('amount_t')),
                'amount_t1': _int_or_none(row.get('amount_t1')),
                'amount_t5': _int_or_none(row.get('amount_t5')),
                'amount_t20': _int_or_none(row.get('amount_t20')),
                'amount_t60': _int_or_none(row.get('amount_t60')),
                'amount_t120': _int_or_none(row.get('amount_t120')),
                'uppct_t': _float_or_none(row.get('uppct_t')),
                'uppct_t1': _float_or_none(row.get('uppct_t1')),
                'uppct_t5': _float_or_none(row.get('uppct_t5')),
                'uppct_t20': _float_or_none(row.get('uppct_t20')),
                'uppct_t60': _float_or_none(row.get('uppct_t60')),
                'uppct_t120': _float_or_none(row.get('uppct_t120')),
                'idx_pct_t': _float_or_none(row.get('idx_pct_t')),
                'idx_gap_t': _float_or_none(row.get('idx_gap_t')),
                'idx_intra_t': _float_or_none(row.get('idx_intra_t')),
                'idx_amp_t': _float_or_none(row.get('idx_amp_t')),
            }
            for _, row in df.iterrows()
        ]
    except Exception as e:
        logger.exception("读取 index_stat 失败: %s", e)
        return []


def _fmt_date(d):
    """YYYYMMDD -> YYYY-MM-DD"""
    s = str(d).replace('-', '')[:8]
    return s[:4] + '-' + s[4:6] + '-' + s[6:8] if len(s) >= 8 else s


def load_initial_cache():
    """主线程同步：连接池初始化（若存在 database_pool）；取一次历史取数日期列表供 API 返回。不写 index_stat（由盘中实时统计负责）。"""
    global rt_date_lists
    if _HAS_DATABASE_POOL:
        from database_pool import pool_manager

        pool_manager._default_db_config = DB_CONFIG
        pool_manager._default_pool_size = 5
        pool_manager._default_max_overflow = 10
        pool_manager._default_pool_timeout = 10
        pool_manager._default_pool_recycle = 1800
        pool = pool_manager.get_pool('default')
        logger.info("连接池已初始化: %s", pool.get_stats())
    else:
        logger.info("未加载 database_pool：read_index_stat 等使用直连 pymysql；可将 database_pool.py 置于同目录以启用连接池")
    try:
        hist = data_fetcher.get_historical_median()
        if hist and 'error' not in hist:
            with _cache_lock:
                rt_date_lists = {
                    't5_date_list': hist.get('t5_date_list'),
                    't20_date_list': hist.get('t20_date_list'),
                    't60_date_list': hist.get('t60_date_list'),
                    't120_date_list': hist.get('t120_date_list'),
                }
            logger.info("日期列表已加载（供 API t5_dates 等返回）")
        else:
            logger.warning("历史取数日期加载失败，API 的 t5_dates 等将为空")
    except Exception as e:
        logger.warning("启动加载日期列表异常: %s", e)
    try:
        _c = pymysql.connect(**DB_CONFIG)
        try:
            ensure_rt_k_trade_time_index(_c, logger)
        finally:
            _c.close()
    except Exception as e:
        logger.warning("启动时检查 rt_k 索引失败: %s", e)
    try:
        _ct = pymysql.connect(**DB_CONFIG_TUSHARE)
        try:
            ensure_index_member_sw_ts_indate_tushare(_ct, logger)
            ensure_etf_popup_hot_indexes_tushare(_ct, logger)
            ensure_top10_holders_ts_end_date_index_tushare(_ct, logger)
        finally:
            _ct.close()
    except Exception as e:
        logger.warning("启动时检查 stock_tushare 热点索引失败: %s", e)


def _build_ma_signal_fallback_rows() -> list:
    """兜底行：当 MA 信号重查询尚未完成时，先返回可展示的基础行情行，避免前端长时间空白。"""
    out = []
    try:
        rt_quotes = _fetch_rt_k_etf_quotes(list(ETF_MA_SIGNAL_ORDER))
    except Exception:
        rt_quotes = {}
    for ts_code in ETF_MA_SIGNAL_ORDER:
        q = rt_quotes.get(ts_code) or {}
        out.append(
            {
                'ts_code': ts_code,
                '60MIN_20': 'none',
                '60MIN_60': 'none',
                'D_20': 'none',
                'D_60': 'none',
                'PMCD_15': None,
                'PMCD_60': None,
                'PMCD_D': None,
                'etf_last': q.get('etf_last'),
                'etf_high': q.get('etf_high'),
                'etf_low': q.get('etf_low'),
                'etf_amount_yi': q.get('etf_amount_yi'),
                'etf_pct': q.get('etf_pct'),
                'etf_gap': q.get('etf_gap'),
                'etf_intra': q.get('etf_intra'),
                'etf_amp': q.get('etf_amp'),
                'etf_rt_date': q.get('etf_rt_date'),
                'etf_observed_at': q.get('etf_observed_at'),
                'range_pct_t1': None,
                'range_pct_t5': None,
                'range_pct_t20': None,
                'range_pct_t60': None,
                'range_pct_t120': None,
            }
        )
    return out


def _get_ma_signal_process_executor():
    """懒启动单 worker 进程，隔离 Flask 常驻进程的 GIL/后台线程争用。"""
    global _ma_signal_process_executor
    with _ma_signal_process_executor_lock:
        if _ma_signal_process_executor is None:
            _ma_signal_process_executor = concurrent.futures.ProcessPoolExecutor(
                max_workers=1
            )
        return _ma_signal_process_executor


def _shutdown_ma_signal_process_executor() -> None:
    global _ma_signal_process_executor
    with _ma_signal_process_executor_lock:
        executor = _ma_signal_process_executor
        _ma_signal_process_executor = None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


atexit.register(_shutdown_ma_signal_process_executor)


def _read_ma_signal_rows_for_refresh(executor=None) -> list:
    """保持原 builder 口径，只替换执行边界；环境开关可立即回退原线程路径。"""
    if executor is None and not ETF_MA_SIGNAL_PROCESS_ISOLATION:
        return read_etf_ma_signal_table_rows()
    pool = executor or _get_ma_signal_process_executor()
    future = pool.submit(read_etf_ma_signal_table_rows)
    return future.result(timeout=ETF_MA_SIGNAL_PROCESS_TIMEOUT_SEC)


def _kickoff_ma_signal_table_refresh_async(force: bool = False) -> bool:
    """异步刷新 MA 信号缓存；并发/高频触发下仅允许一个刷新任务在跑。"""
    def _job():
        global etf_ma_signal_cache, etf_ma_signal_last_read_ts, etf_ma_signal_refreshing
        t0 = time.perf_counter()
        try:
            rows = _read_ma_signal_rows_for_refresh()
            if rows:
                with _cache_lock:
                    etf_ma_signal_cache = rows
                    etf_ma_signal_last_read_ts = time.time()
                logger.info(
                    "ma_signal_table 刷新完成 rows=%s elapsed=%.3fs",
                    len(rows), time.perf_counter() - t0
                )
            else:
                logger.warning("ma_signal_table 刷新返回空 rows，沿用旧缓存")
        except Exception as e:
            logger.warning("ma_signal_table 异步刷新失败: %s", e)
        finally:
            with _cache_lock:
                etf_ma_signal_refreshing = False

    global etf_ma_signal_refreshing, etf_ma_signal_refresh_started_ts
    now = time.time()
    with _cache_lock:
        if etf_ma_signal_refreshing:
            return False
        if not force and (now - etf_ma_signal_refresh_started_ts) < ETF_MA_SIGNAL_REFRESH_MIN_GAP:
            return False
        etf_ma_signal_refreshing = True
        etf_ma_signal_refresh_started_ts = now
    try:
        t = threading.Thread(target=_job, name='ma-signal-table-refresh', daemon=True)
        t.start()
        return True
    except Exception as e:
        with _cache_lock:
            etf_ma_signal_refreshing = False
        logger.warning("启动 ma_signal_table 刷新线程失败: %s", e)
        return False


def _warmup_ma_signal_table_cache_async():
    """启动时后台预热 MA 信号表缓存。"""
    with _cache_lock:
        has_cache = bool(etf_ma_signal_cache)
    if has_cache:
        return
    _kickoff_ma_signal_table_refresh_async(force=True)


def read_limit_stats_panel():
    """
    涨跌家数统计面板：读 stock_wind.stockstat、rt_k、stock_tushare.limit_list_ths。
    up_num_ma5_rt / up_num_ma5_rt_per：近 5 日上涨占比及该值在 marketinfo.up_num_ma5 近 500 日历史中的百分位（不含当日、严格小于）。
    新高/低十八项、HL_C：新高读 stockstat.windcodes_high_*；HL_C 读 stockstat.windcodes_hlc_valid/ch/cl_*（盘中实时统计预写，避免 ma JOIN 读超时）。
    pct_gt* / pct_lt*：仅 COUNT stockstat.pctchange，依赖盘中脚本对 stockstat 的更新；与直连 rt_k 的 HL 计数口径独立。
    涨跌停与连板等：stockstat 标志列或 limit_list_ths；剔除 ST/*ST/S*ST：stockstat 用 sec_name，同花顺池用 name。
    """
    base = {
        'ok': True,
        'error': None,
        # 上四组：标签行 + 数值行（前端用同一 dict 填 8 行）
        'pct_gt5': None,
        'pct_gt7': None,
        'pct_gt10': None,
        'cnt_high_limitone': None,
        'cnt_high_limit': None,
        'cnt_high_limit_lifting': None,
        'cnt_lb_zt': None,
        'pct_lt_neg5': None,
        'pct_lt_neg7': None,
        'pct_lt_neg10': None,
        'cnt_low_limitone': None,
        'cnt_low_limit': None,
        'cnt_low_limit_lifting': None,
        'cnt_lb_dt': None,
        'cnt_h5': None,
        'cnt_h10': None,
        'cnt_h20': None,
        'cnt_h60': None,
        'cnt_h120': None,
        'cnt_h200': None,
        'cnt_h250': None,
        'cnt_h850': None,
        'cnt_h_high': None,
        'cnt_l5': None,
        'cnt_l10': None,
        'cnt_l20': None,
        'cnt_l60': None,
        'cnt_l120': None,
        'cnt_l200': None,
        'cnt_l250': None,
        'cnt_l850': None,
        'cnt_l_low': None,
        'hlc_cccc_5': None,
        'hlc_cccc_10': None,
        'hlc_cccc_20': None,
        'hlc_cccc_60': None,
        'hlc_cccc_120': None,
        'hlc_cccc_200': None,
        'hlc_cccc_250': None,
        'hlc_cccc_850': None,
        'hlc_cccc_h': None,
        'zt_t1': None,
        'zt_t5': None,
        'dt_t1': None,
        'dt_t5': None,
        # 昨涨停池/昨跌停池（同 zt_t1、dt_t1 的 trade_date）内，今日涨跌幅按 rt_k.amount（成交额，元）加权平均（%）
        'prev_zt_pct': None,
        'prev_dt_pct': None,
        'up_num_ma5_rt': None,
        'up_num_ma5_rt_per': None,
        'cnt_up': None,
        'cnt_flat': None,
        'cnt_down': None,
    }

    def _scalar_int(cur, sql, params=None):
        cur.execute(sql, params or ())
        row = cur.fetchone()
        if not row or row[0] is None:
            return 0
        return int(row[0])

    try:
        conn = pymysql.connect(**DB_CONFIG)
    except Exception as e:
        logger.exception("limit_stats: 连接 stock_wind 失败: %s", e)
        base['ok'] = False
        base['error'] = '数据库连接失败'
        return base

    try:
        with conn.cursor() as cursor:
            # stockstat 12 项一次性条件聚合（原 12 条 COUNT → 1 条，只扫描一次表）
            _sql_stockstat_agg = (
                "SELECT "
                "SUM(CASE WHEN pctchange IS NOT NULL AND pctchange > 5 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN pctchange IS NOT NULL AND pctchange > 7 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN pctchange IS NOT NULL AND pctchange > 10 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN pctchange IS NOT NULL AND pctchange < -5 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN pctchange IS NOT NULL AND pctchange < -7 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN pctchange IS NOT NULL AND pctchange < -10 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(high_limitone,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(high_limit,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(high_limit_lifting,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(low_limitone,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(low_limit,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(low_limit_lifting,0)=1 THEN 1 ELSE 0 END)"
                " FROM stock_wind.stockstat WHERE 1=1"
                + _LIMIT_STATS_SQL_NOT_ST_STOCKSTAT
            )
            cursor.execute(_sql_stockstat_agg)
            _agg_row = cursor.fetchone()
            if _agg_row:
                _agg_keys = [
                    'pct_gt5', 'pct_gt7', 'pct_gt10',
                    'pct_lt_neg5', 'pct_lt_neg7', 'pct_lt_neg10',
                    'cnt_high_limitone', 'cnt_high_limit', 'cnt_high_limit_lifting',
                    'cnt_low_limitone', 'cnt_low_limit', 'cnt_low_limit_lifting',
                ]
                for _i, _k in enumerate(_agg_keys):
                    base[_k] = int(_agg_row[_i] or 0)

            # 涨 / 平 / 跌：实时 A 股非 ST、有效成交样本；与表头设计文档口径一致。
            _sql_breadth_agg = (
                """
                SELECT
                  SUM(CASE WHEN pct_chg > 0 THEN 1 ELSE 0 END) AS cnt_up,
                  SUM(CASE WHEN pct_chg = 0 THEN 1 ELSE 0 END) AS cnt_flat,
                  SUM(CASE WHEN pct_chg < 0 THEN 1 ELSE 0 END) AS cnt_down
                FROM (
                  SELECT ROUND((rt.close - rt.pre_close) / rt.pre_close * 100, 2) AS pct_chg
                  FROM stock_wind.rt_k rt
                  INNER JOIN stock_wind.rt_k_code_category c
                    ON c.ts_code = rt.ts_code AND c.major_type = 'A股'
                  LEFT JOIN stock_wind.stockstat s
                    ON s.windcodes = rt.ts_code
                  WHERE IFNULL(rt.vol, 0) > 0
                    AND IFNULL(rt.amount, 0) > 0
                    AND IFNULL(rt.pre_close, 0) > 0
                    AND """
                + _LIMIT_STATS_RT_K_FRESH_TRADE_DATE
                + _LIMIT_STATS_SQL_NOT_ST_JOIN_S
                + """
                ) breadth
                """
            )
            try:
                cursor.execute(_sql_breadth_agg)
                _breadth_row = cursor.fetchone()
                if _breadth_row:
                    base['cnt_up'] = int(_breadth_row[0] or 0)
                    base['cnt_flat'] = int(_breadth_row[1] or 0)
                    base['cnt_down'] = int(_breadth_row[2] or 0)
            except Exception as e_breadth:
                logger.warning("limit_stats: 涨平跌汇总查询失败: %s", e_breadth)
                base['cnt_up'] = None
                base['cnt_flat'] = None
                base['cnt_down'] = None

            # 新高/低 18 项直接读 stockstat（盘中实时统计.py 每 3 秒写入，无需重复 JOIN stk_factor_ma）
            # 原 stk_factor_ma 联合查询因 GROUP BY 全表扫描在高负载时频繁超时，已改为单条 SUM(CASE) 聚合
            _sql_hl_agg = (
                "SELECT "
                "SUM(CASE WHEN IFNULL(windcodes_high_5,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_high_10,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_high_20,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_high_60,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_high_120,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_high_200,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_high_250,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_high_850,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_h_high,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_low_5,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_low_10,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_low_20,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_low_60,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_low_120,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_low_200,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_low_250,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_low_850,0)=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN IFNULL(windcodes_h_low,0)=1 THEN 1 ELSE 0 END)"
                " FROM stock_wind.stockstat WHERE 1=1"
                + _LIMIT_STATS_SQL_NOT_ST_STOCKSTAT
            )
            cursor.execute(_sql_hl_agg)
            _hl_row = cursor.fetchone()
            if _hl_row:
                for _i, _k in enumerate(_LIMIT_STATS_PANEL_HL_KEYS):
                    base[_k] = int(_hl_row[_i] or 0)

            # limit_list_ths：须在 HL_C 之前（历史上重型 HL_C JOIN 超时会拖死同连接；现改读 stockstat 仍保持顺序稳妥）
            try:
                cursor.execute(
                    "SELECT MAX(trade_date) AS md FROM stock_tushare.limit_list_ths WHERE limit_type IN ('涨停池','跌停池')"
                )
                row_md = cursor.fetchone()
                latest_td = row_md[0] if row_md else None
                if latest_td is not None:
                    td_param = latest_td
                    base['zt_t1'] = _scalar_int(
                        cursor,
                        """
                        SELECT COUNT(*) FROM stock_tushare.limit_list_ths
                        WHERE trade_date = %s AND limit_type = '涨停池'
                        """
                        + _LIMIT_STATS_SQL_NOT_ST_THS,
                        (td_param,),
                    )
                    base['dt_t1'] = _scalar_int(
                        cursor,
                        """
                        SELECT COUNT(*) FROM stock_tushare.limit_list_ths
                        WHERE trade_date = %s AND limit_type = '跌停池'
                        """
                        + _LIMIT_STATS_SQL_NOT_ST_THS,
                        (td_param,),
                    )

                    base['cnt_lb_zt'] = _scalar_int(
                        cursor,
                        """
                        SELECT COUNT(*) FROM stock_wind.stockstat s
                        INNER JOIN stock_tushare.limit_list_ths l
                          ON s.windcodes = l.ts_code
                        WHERE l.trade_date = %s AND l.limit_type = '涨停池' AND IFNULL(s.high_limit,0) = 1
                        """
                        + _LIMIT_STATS_SQL_NOT_ST_JOIN_S,
                        (td_param,),
                    )
                    base['cnt_lb_dt'] = _scalar_int(
                        cursor,
                        """
                        SELECT COUNT(*) FROM stock_wind.stockstat s
                        INNER JOIN stock_tushare.limit_list_ths l
                          ON s.windcodes = l.ts_code
                        WHERE l.trade_date = %s AND l.limit_type = '跌停池' AND IFNULL(s.low_limit,0) = 1
                        """
                        + _LIMIT_STATS_SQL_NOT_ST_JOIN_S,
                        (td_param,),
                    )
                    # 昨涨停/跌停池个股：今日 pctchange 按 rt_k.amount（成交额）加权，与 Top30 表「成交额加权涨跌」口径一致（权重用金额不用手数）
                    _sql_prev_zt_amt_w = (
                        "SELECT SUM(s.pctchange * rt.amount) / NULLIF(SUM(rt.amount), 0) "
                        "FROM ( "
                        "  SELECT DISTINCT ts_code FROM stock_tushare.limit_list_ths "
                        "  WHERE trade_date = %s AND limit_type = '涨停池' "
                        + _LIMIT_STATS_SQL_NOT_ST_THS
                        + " ) l "
                        "INNER JOIN stock_wind.stockstat s ON s.windcodes = l.ts_code "
                        "INNER JOIN stock_wind.rt_k rt ON rt.ts_code = l.ts_code "
                        "WHERE s.pctchange IS NOT NULL "
                        "AND rt.amount IS NOT NULL AND rt.amount > 0 "
                        "AND NOT (rt.vol = 0 AND rt.amount = 0) "
                        "AND "
                        + _LIMIT_STATS_RT_K_FRESH_TRADE_DATE
                        + _LIMIT_STATS_SQL_NOT_ST_JOIN_S
                    )
                    _sql_prev_dt_amt_w = (
                        "SELECT SUM(s.pctchange * rt.amount) / NULLIF(SUM(rt.amount), 0) "
                        "FROM ( "
                        "  SELECT DISTINCT ts_code FROM stock_tushare.limit_list_ths "
                        "  WHERE trade_date = %s AND limit_type = '跌停池' "
                        + _LIMIT_STATS_SQL_NOT_ST_THS
                        + " ) l "
                        "INNER JOIN stock_wind.stockstat s ON s.windcodes = l.ts_code "
                        "INNER JOIN stock_wind.rt_k rt ON rt.ts_code = l.ts_code "
                        "WHERE s.pctchange IS NOT NULL "
                        "AND rt.amount IS NOT NULL AND rt.amount > 0 "
                        "AND NOT (rt.vol = 0 AND rt.amount = 0) "
                        "AND "
                        + _LIMIT_STATS_RT_K_FRESH_TRADE_DATE
                        + _LIMIT_STATS_SQL_NOT_ST_JOIN_S
                    )
                    try:
                        cursor.execute(_sql_prev_zt_amt_w, (td_param,))
                        _rw = cursor.fetchone()
                        if _rw and _rw[0] is not None:
                            base['prev_zt_pct'] = round(float(_rw[0]), 2)
                        cursor.execute(_sql_prev_dt_amt_w, (td_param,))
                        _rd = cursor.fetchone()
                        if _rd and _rd[0] is not None:
                            base['prev_dt_pct'] = round(float(_rd[0]), 2)
                    except Exception as e_pv:
                        logger.warning("limit_stats: 昨涨Pct/昨跌Pct 加权计算失败: %s", e_pv)
                        base['prev_zt_pct'] = None
                        base['prev_dt_pct'] = None
                else:
                    base['zt_t1'] = None
                    base['dt_t1'] = None
                    base['cnt_lb_zt'] = None
                    base['cnt_lb_dt'] = None
                    base['prev_zt_pct'] = None
                    base['prev_dt_pct'] = None
            except Exception as e_ths:
                logger.warning("limit_stats: limit_list_ths 查询失败（表是否存在？）: %s", e_ths)
                base['zt_t1'] = None
                base['zt_t5'] = None
                base['dt_t1'] = None
                base['dt_t5'] = None
                base['cnt_lb_zt'] = None
                base['cnt_lb_dt'] = None
                base['prev_zt_pct'] = None
                base['prev_dt_pct'] = None

            # HL_C：读 stockstat 预写标志列（与盘中实时统计同源），避免 ma JOIN 超 8s 读超时
            try:
                base.update(_read_limit_stats_hlc_from_stockstat(cursor))
            except Exception as e_hlc:
                logger.warning('limit_stats: HL_C stockstat 聚合失败: %s', e_hlc)
                for _k in _LIMIT_STATS_PANEL_HLC_KEYS:
                    base[_k] = None

        # 涨停T-5均 / 跌停T-5均：一条聚合 SQL 取最近 5 日涨停/跌停计数均值（原 10 次 COUNT → 1 次）
        try:
            with conn.cursor() as cur_t5:
                _sql_t5 = (
                    "SELECT trade_date,"
                    " SUM(CASE WHEN limit_type='涨停池' THEN 1 ELSE 0 END) AS zt_cnt,"
                    " SUM(CASE WHEN limit_type='跌停池' THEN 1 ELSE 0 END) AS dt_cnt"
                    " FROM stock_tushare.limit_list_ths"
                    " WHERE trade_date IN ("
                    "   SELECT td FROM ("
                    "     SELECT DISTINCT trade_date AS td FROM stock_tushare.limit_list_ths"
                    "     ORDER BY trade_date DESC LIMIT 5"
                    "   ) _t5"
                    " )"
                    + _LIMIT_STATS_SQL_NOT_ST_THS
                    + " GROUP BY trade_date"
                )
                cur_t5.execute(_sql_t5)
                _t5_rows = cur_t5.fetchall() or []
                _nw = len(_t5_rows)
                if _nw >= 1:
                    _zs = sum(int(r[1] or 0) for r in _t5_rows)
                    _ds = sum(int(r[2] or 0) for r in _t5_rows)
                    base['zt_t5'] = int(round(_zs / float(_nw)))
                    base['dt_t5'] = int(round(_ds / float(_nw)))
                else:
                    base['zt_t5'] = None
                    base['dt_t5'] = None
        except Exception as e_t5:
            logger.warning("limit_stats: 涨停T-5均/跌停T-5均计算失败: %s", e_t5)
            base['zt_t5'] = None
            base['dt_t5'] = None

        try:
            base['up_num_ma5_rt'] = _read_up_num_ma5_rt(conn)
        except Exception as e_ma5:
            logger.warning("limit_stats: up_num_ma5_rt 计算失败: %s", e_ma5)
            base['up_num_ma5_rt'] = None
        try:
            base['up_num_ma5_rt_per'] = _read_up_num_ma5_rt_percentile(
                conn, base.get('up_num_ma5_rt')
            )
        except Exception as e_per:
            logger.warning("limit_stats: up_num_ma5_rt_per 计算失败: %s", e_per)
            base['up_num_ma5_rt_per'] = None
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return base


def read_limit_stats_detail(kind: str, limit: int = 500):
    """
    涨跌家数面板可点击指标明细（与 read_limit_stats_panel 同口径）。
    kind：cnt_h5/cnt_l5/…/cnt_h250/…、一字涨/涨停/炸板/一字跌/跌停/开板、cnt_lb_zt/cnt_lb_dt、zt_t1/dt_t1。
    """
    kind = (kind or '').strip()
    result = {
        'ok': True,
        'rows': [],
        'error': None,
        'trade_date': None,
        'kind': kind,
    }
    if kind not in _LIMIT_STATS_DETAIL_KINDS:
        result['ok'] = False
        result['error'] = '不支持的 kind'
        return result
    try:
        limit_n = int(limit)
    except Exception:
        limit_n = 500
    limit_n = max(1, min(limit_n, 2000))
    try:
        conn = pymysql.connect(**DB_CONFIG)
    except Exception as e:
        logger.exception("limit_stats_detail: 连接 stock_wind 失败: %s", e)
        result['ok'] = False
        result['error'] = '数据库连接失败'
        return result
    try:
        with conn.cursor() as cursor:
            # 一字涨/涨停/一字跌/跌停
            if kind in _LIMIT_STATS_STOCKSTAT_FLAG_COL:
                col = _LIMIT_STATS_STOCKSTAT_FLAG_COL[kind]
                sql = f"""
                    SELECT s.windcodes, s.sec_name, s.amount, s.pctchange,
                           NULL AS lu_desc, NULL AS tag,
                           {_LIMIT_STATS_SW_NAME_SELECT}
                    FROM stock_wind.stockstat s
                    {_limit_stats_sw_join_sql('s.windcodes')}WHERE IFNULL(s.{col}, 0) = 1
                    {_LIMIT_STATS_SQL_NOT_ST_JOIN_S}
                    ORDER BY IFNULL(s.amount, 0) DESC, s.windcodes ASC
                    LIMIT %s
                """
                cursor.execute(sql, (limit_n,))
                rows = cursor.fetchall()
                result['rows'] = _limit_detail_rows_to_dicts(rows, False)
                return result

            # 新高/低十八项：与 _LIMIT_STATS_HIGHLOW_COUNT_SQL 同条件；失败则回退 stockstat 列
            if kind in _LIMIT_STATS_HL_DETAIL_WHERE:
                cond = _LIMIT_STATS_HL_DETAIL_WHERE[kind]
                sql = f"""
                    SELECT s1.windcodes, s1.sec_name, s1.amount, s1.pctchange,
                           NULL AS lu_desc, NULL AS tag,
                           {_LIMIT_STATS_SW_NAME_SELECT}
                    FROM stock_wind.stockstat s1
                    INNER JOIN stock_wind.rt_k rt ON s1.windcodes = rt.ts_code
                    INNER JOIN (
                        SELECT m.ts_code, m.high_5, m.low_5, m.high_10, m.low_10, m.high_20, m.low_20,
                               m.high_60, m.low_60, m.high_120, m.low_120, m.high_200, m.low_200,
                               m.high_250, m.high_850, m.h_high, m.low_250, m.low_850, m.h_low,
                               m.high_qfq, m.low_qfq, m.close_qfq,
                               p.high AS pro_high, p.low AS pro_low, p.close AS pro_close
                        FROM stock_tushare.stk_factor_ma m
                        INNER JOIN (
                            SELECT ts_code, MAX(trade_date) AS md
                            FROM stock_tushare.stk_factor_ma
                            GROUP BY ts_code
                        ) mx ON m.ts_code = mx.ts_code AND m.trade_date = mx.md
                        LEFT JOIN stock_tushare.stk_factor_pro p
                          ON p.ts_code = m.ts_code AND p.trade_date = m.trade_date
                    ) ma ON ma.ts_code = rt.ts_code
                    {_limit_stats_sw_join_sql('s1.windcodes')}WHERE NOT (rt.vol = 0 AND rt.amount = 0)
                    AND {_LIMIT_STATS_RT_K_FRESH_TRADE_DATE}
                    {_LIMIT_STATS_SQL_NOT_ST_STOCKSTAT_S1}
                    AND ({cond})
                    ORDER BY IFNULL(s1.amount, 0) DESC, s1.windcodes ASC
                    LIMIT %s
                """
                try:
                    cursor.execute(sql, (limit_n,))
                    rows = cursor.fetchall()
                except Exception as e_hl:
                    logger.warning("limit_stats_detail: 新高/低十八项直连 stk_factor_ma 失败，回退 stockstat: %s", e_hl)
                    fb_col = _LIMIT_STATS_HL_DETAIL_FALLBACK_COL[kind]
                    sql_fb = f"""
                        SELECT s.windcodes, s.sec_name, s.amount, s.pctchange,
                               NULL AS lu_desc, NULL AS tag,
                               {_LIMIT_STATS_SW_NAME_SELECT}
                        FROM stock_wind.stockstat s
                        {_limit_stats_sw_join_sql('s.windcodes')}WHERE IFNULL(s.{fb_col}, 0) = 1
                        {_LIMIT_STATS_SQL_NOT_ST_JOIN_S}
                        ORDER BY IFNULL(s.amount, 0) DESC, s.windcodes ASC
                        LIMIT %s
                    """
                    cursor.execute(sql_fb, (limit_n,))
                    rows = cursor.fetchall()
                result['rows'] = _limit_detail_rows_to_dicts(rows, False)
                return result

            # 以下为 limit_list_ths：与面板一致，最新交易日取 涨停池+跌停池 的 MAX(trade_date)
            cursor.execute(
                "SELECT MAX(trade_date) AS md FROM stock_tushare.limit_list_ths "
                "WHERE limit_type IN ('涨停池','跌停池')"
            )
            row_md = cursor.fetchone()
            latest_td = row_md[0] if row_md else None
            result['trade_date'] = str(latest_td) if latest_td is not None else None

            if kind == 'cnt_lb_zt':
                if latest_td is None:
                    return result
                sql = f"""
                    SELECT s.windcodes, s.sec_name, s.amount, s.pctchange, l.lu_desc, l.tag,
                           {_LIMIT_STATS_SW_NAME_SELECT}
                    FROM stock_wind.stockstat s
                    INNER JOIN stock_tushare.limit_list_ths l ON s.windcodes = l.ts_code
                    {_limit_stats_sw_join_sql('s.windcodes')}WHERE l.trade_date = %s
                      AND l.limit_type = '涨停池'
                      AND IFNULL(s.high_limit, 0) = 1
                      {_LIMIT_STATS_SQL_NOT_ST_JOIN_S}
                    ORDER BY IFNULL(s.amount, 0) DESC, s.windcodes ASC
                    LIMIT %s
                """
                cursor.execute(sql, (latest_td, limit_n))
                rows = cursor.fetchall()
                result['rows'] = _limit_detail_rows_to_dicts(rows, False)
                return result

            if kind == 'cnt_lb_dt':
                if latest_td is None:
                    return result
                sql = f"""
                    SELECT s.windcodes, s.sec_name, s.amount, s.pctchange, l.lu_desc, l.tag,
                           {_LIMIT_STATS_SW_NAME_SELECT}
                    FROM stock_wind.stockstat s
                    INNER JOIN stock_tushare.limit_list_ths l ON s.windcodes = l.ts_code
                    {_limit_stats_sw_join_sql('s.windcodes')}WHERE l.trade_date = %s
                      AND l.limit_type = '跌停池'
                      AND IFNULL(s.low_limit, 0) = 1
                      {_LIMIT_STATS_SQL_NOT_ST_JOIN_S}
                    ORDER BY IFNULL(s.amount, 0) DESC, s.windcodes ASC
                    LIMIT %s
                """
                cursor.execute(sql, (latest_td, limit_n))
                rows = cursor.fetchall()
                result['rows'] = _limit_detail_rows_to_dicts(rows, False)
                return result

            if kind == 'zt_t1':
                if latest_td is None:
                    return result
                sql = f"""
                    SELECT l.ts_code, COALESCE(s.sec_name, l.name), s.amount, s.pctchange, l.lu_desc, l.tag,
                           {_LIMIT_STATS_SW_NAME_SELECT}
                    FROM stock_tushare.limit_list_ths l
                    LEFT JOIN stock_wind.stockstat s ON s.windcodes = l.ts_code
                    {_limit_stats_sw_join_sql('l.ts_code')}WHERE l.trade_date = %s
                      AND l.limit_type = '涨停池'
                    {_limit_stats_sql_not_st_ths_alias('l')}
                    ORDER BY IFNULL(s.amount, 0) DESC, l.ts_code ASC
                    LIMIT %s
                """
                cursor.execute(sql, (latest_td, limit_n))
                rows = cursor.fetchall()
                result['rows'] = _limit_detail_rows_to_dicts(rows, False)
                return result

            if kind == 'dt_t1':
                if latest_td is None:
                    return result
                sql = f"""
                    SELECT l.ts_code, COALESCE(s.sec_name, l.name), s.amount, s.pctchange, l.lu_desc, l.tag,
                           {_LIMIT_STATS_SW_NAME_SELECT}
                    FROM stock_tushare.limit_list_ths l
                    LEFT JOIN stock_wind.stockstat s ON s.windcodes = l.ts_code
                    {_limit_stats_sw_join_sql('l.ts_code')}WHERE l.trade_date = %s
                      AND l.limit_type = '跌停池'
                    {_limit_stats_sql_not_st_ths_alias('l')}
                    ORDER BY IFNULL(s.amount, 0) DESC, l.ts_code ASC
                    LIMIT %s
                """
                cursor.execute(sql, (latest_td, limit_n))
                rows = cursor.fetchall()
                result['rows'] = _limit_detail_rows_to_dicts(rows, False)
                return result

            result['ok'] = False
            result['error'] = '未实现的 kind'
            return result
    except Exception as e:
        logger.exception("limit_stats_detail 查询失败: %s", e)
        result['ok'] = False
        result['error'] = str(e)
        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


def read_limit_lb_zt_detail(limit: int = 500):
    """连板涨停明细（与 cnt_lb_zt 同口径）：委托 read_limit_stats_detail。"""
    return read_limit_stats_detail('cnt_lb_zt', limit=limit)


def index():
    """行情监控单页"""
    return _render_rt_monitor_no_cache()


def rt_monitor():
    """行情监控页（与 / 相同）"""
    return _render_rt_monitor_no_cache()



def get_rt_median():
    """行情涨跌中位数 API：只读 index_stat。按 TTL 重新读表以拿到盘中实时统计写入的最新数据。
    指数区间涨跌幅、成交额全历史 MAX 与 MAX/MIN T-60 仅在 TTL 刷新时重算，与 index_stat 缓存同周期，避免轮询时重复打库。"""
    global rt_table_cache, rt_last_read_ts
    now = time.time()
    with _cache_lock:
        need_read = (rt_table_cache is None or (now - rt_last_read_ts) >= RT_MEDIAN_CACHE_TTL)
    if need_read:
        table = read_index_stat()
        with _cache_lock:
            if table:
                rt_table_cache = table
                rt_last_read_ts = now
            else:
                # 读为空（表空或读失败）：若有旧缓存则沿用且不更新 last_update，否则返回 []
                table = rt_table_cache if (rt_table_cache and len(rt_table_cache) > 0) else []
                if table:
                    logger.warning("read_index_stat 返回空，沿用旧缓存；last_update 不更新")
    else:
        table = rt_table_cache or []
    # 前三张表用 table（12 行：全部A股…创业板综）；涨跌幅% 表用 table_idx（国证A指首行 + A500…创业板综）
    full_list = table  # 可能 13 行：全部A股…创业板综 + 国证A指
    table_med = [r for r in full_list if r.get('category') != '国证A指'][:12]  # 12 行，全部A股打头
    gj_row = next((r for r in full_list if r.get('category') == '国证A指'), None)
    table_idx = ([gj_row] + [r for r in full_list if r.get('category') not in (None, '全部A股', '国证A指')])[:12] if gj_row else table_med
    # 涨跌幅%%表：区间 T-1/T-5/…（index_daily + rt_idx_k；中证2000/北证50 不拼 rt）
    if need_read and table_idx:
        try:
            idx_ts_list = []
            for _r in table_idx:
                _cat = (_r.get('category') or '').strip()
                _ts = IDX_PCT_CATEGORY_TO_TS.get(_cat)
                if _ts:
                    idx_ts_list.append(_ts)
            conn_idx = None
            try:
                conn_idx = pymysql.connect(**DB_CONFIG_TUSHARE)
                rt_idx_map = (
                    _fetch_rt_idx_k_quotes(idx_ts_list, conn=conn_idx) if idx_ts_list else {}
                )
                for _r in table_idx:
                    _cat = (_r.get('category') or '').strip()
                    _ts = IDX_PCT_CATEGORY_TO_TS.get(_cat)
                    if not _ts:
                        continue
                    try:
                        _splice = _cat not in IDX_PCT_NO_RT_SPLICE_CATEGORIES
                        _q = rt_idx_map.get(_ts) if _splice else None
                        _r.update(
                            _read_index_daily_interval_range_pct(
                                conn_idx, _ts, _q, splice_rt=_splice
                            )
                        )
                    except Exception as row_e:
                        logger.warning(
                            "涨跌幅表：单行区间涨跌幅异常 category=%s ts=%s: %s",
                            _cat,
                            _ts,
                            row_e,
                        )
                try:
                    metric_hist = _ensure_idx_metric_history(conn_idx, idx_ts_list)
                    live_keys = (
                        ('pct', 'idx_pct_t', 'idx_pct_per'),
                        ('intra', 'idx_intra_t', 'idx_intra_per'),
                        ('gap', 'idx_gap_t', 'idx_gap_per'),
                        ('amp', 'idx_amp_t', 'idx_amp_per'),
                    )
                    for _r in table_idx:
                        _cat = (_r.get('category') or '').strip()
                        _ts = IDX_PCT_CATEGORY_TO_TS.get(_cat)
                        samples = metric_hist.get(_ts) or {} if _ts else {}
                        for sample_key, live_key, per_key in live_keys:
                            if not _ts:
                                _r[per_key] = None
                                continue
                            _r[per_key] = _idx_gap_percentile(
                                samples.get(sample_key) or (), _r.get(live_key)
                            )
                except Exception as gap_e:
                    logger.warning('涨跌幅表全历史分位失败: %s', gap_e)
            except Exception as e:
                logger.warning("table_idx 指数区间涨跌幅失败: %s", e)
            finally:
                if conn_idx is not None:
                    try:
                        conn_idx.close()
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("table_idx 区间涨跌幅外层失败: %s", e)
    # 成交额（亿）表：全历史 MAX/峰值日，以及最近 60 个交易日窗口内最大/最小 amount（亿）及对应 trade_date
    if need_read and table_med:
        try:
            conn_amt = pymysql.connect(**DB_CONFIG_TUSHARE)
            try:
                ts_for_batch: list[str] = []
                for _r in table_med:
                    _cat = (_r.get('category') or '').strip()
                    _ts = AMOUNT_CATEGORY_TO_INDEX_TS.get(_cat)
                    if _ts:
                        ts_for_batch.append(_ts)
                hist_map = _batch_index_daily_max_amount_all(conn_amt, ts_for_batch)
                peak_map = _batch_index_daily_peak_amount_last_n_trade_dates(conn_amt, ts_for_batch, 60)
                valley_map = _batch_index_daily_valley_amount_last_n_trade_dates(conn_amt, ts_for_batch, 60)
                if ts_for_batch and not hist_map:
                    hist_map = {}
                    for t in sorted(set(ts_for_batch)):
                        try:
                            hist_map[t] = _index_daily_max_amount_all(conn_amt, t)
                        except Exception as fb_e:
                            logger.warning("成交额表：逐码全历史 MAX 失败 ts=%s: %s", t, fb_e)
                            hist_map[t] = (None, None)
                if ts_for_batch and not peak_map:
                    peak_map = {}
                    for t in sorted(set(ts_for_batch)):
                        try:
                            peak_map[t] = _index_daily_max_amount_last_n_trade_dates(conn_amt, t, 60)
                        except Exception as fb_e:
                            logger.warning("成交额表：逐码 MAX T-60 失败 ts=%s: %s", t, fb_e)
                            peak_map[t] = (None, None)
                if ts_for_batch and not valley_map:
                    valley_map = {}
                    for t in sorted(set(ts_for_batch)):
                        try:
                            valley_map[t] = _index_daily_min_amount_last_n_trade_dates(conn_amt, t, 60)
                        except Exception as fb_e:
                            logger.warning("成交额表：逐码 MIN T-60 失败 ts=%s: %s", t, fb_e)
                            valley_map[t] = (None, None)
                for _r in table_med:
                    _cat = (_r.get('category') or '').strip()
                    try:
                        _ts = AMOUNT_CATEGORY_TO_INDEX_TS.get(_cat)
                        if not _ts:
                            if _cat == AMOUNT_ALL_A_CATEGORY:
                                _hall_yi, _hall_ds = _marketinfo_max_amount_all(conn_amt)
                                _r['idx_daily_max_all'] = _hall_yi
                                _r['idx_daily_max_all_date'] = _hall_ds
                                _yi, _ds = _marketinfo_max_amount_last_n_trade_dates(conn_amt, 60)
                                _r['idx_daily_max_t60'] = _yi
                                _r['idx_daily_max_t60_date'] = _ds
                                _min_yi, _min_ds = _marketinfo_min_amount_last_n_trade_dates(conn_amt, 60)
                                _r['idx_daily_min_t60'] = _min_yi
                                _r['idx_daily_min_t60_date'] = _min_ds
                            else:
                                _blank_amount_extreme_fields(_r)
                            continue
                        _hall_yi, _hall_ds = hist_map.get(_ts, (None, None))
                        _r['idx_daily_max_all'] = _hall_yi
                        _r['idx_daily_max_all_date'] = _hall_ds
                        _yi, _ds = peak_map.get(_ts, (None, None))
                        _r['idx_daily_max_t60'] = _yi
                        _r['idx_daily_max_t60_date'] = _ds
                        _min_yi, _min_ds = valley_map.get(_ts, (None, None))
                        _r['idx_daily_min_t60'] = _min_yi
                        _r['idx_daily_min_t60_date'] = _min_ds
                    except Exception as amt_row_e:
                        logger.warning("成交额表：单行峰值附加失败 category=%s: %s", _cat, amt_row_e)
                        _blank_amount_extreme_fields(_r)
            finally:
                try:
                    conn_amt.close()
                except Exception:
                    pass
        except Exception as e:
            logger.warning("成交额表 index_daily MAX/MIN T-60 附加失败: %s", e)
    amount_top5_pct = None
    amount_top5_pct_per = None
    try:
        conn_top5_w = pymysql.connect(**DB_CONFIG)
        conn_top5_t = None
        try:
            amount_top5_pct = _compute_stockstat_amount_top5_pct(conn_top5_w)
            conn_top5_t = pymysql.connect(**DB_CONFIG_TUSHARE)
            amount_top5_pct_per = _read_amount_top5_pct_percentile(
                conn_top5_t, conn_top5_w, amount_top5_pct
            )
        finally:
            try:
                conn_top5_w.close()
            except Exception:
                pass
            if conn_top5_t is not None:
                try:
                    conn_top5_t.close()
                except Exception:
                    pass
    except Exception as e:
        logger.warning("成交额 Top5%%_pct / Per 计算失败: %s", e)
    first = table_med[0] if table_med else {}
    result = {
        'today_median_pct': first.get('t'),
        'today_count': None,
        'last_update': datetime.fromtimestamp(rt_last_read_ts).strftime('%Y-%m-%d %H:%M:%S') if rt_last_read_ts else '--',
        'prev1_median_pct': first.get('t1'),
        'prev5_median_pct': first.get('t5'),
        'prev20_median_pct': first.get('t20'),
        'prev60_median_pct': first.get('t60'),
        'prev120_median_pct': first.get('t120'),
        'table': table_med,
        'table_idx': table_idx,
        'amount_top5_pct': amount_top5_pct,
        'amount_top5_pct_per': amount_top5_pct_per,
        't5_dates': (rt_date_lists or {}).get('t5_date_list'),
        't20_dates': (rt_date_lists or {}).get('t20_date_list'),
        't60_dates': (rt_date_lists or {}).get('t60_date_list'),
        't120_dates': (rt_date_lists or {}).get('t120_date_list'),
    }
    return jsonify(result)


def api_limit_stats():
    """
    A 股涨跌家数面板：只读 stockstat + limit_list_ths。
    若带 detail=1&kind=cnt_h250 等，则返回该指标明细 JSON（与面板同源 URL，避免独立明细路径在代理/环境下 404）。
    """
    if (request.args.get('detail') or '').strip() == '1':
        kind = (request.args.get('kind') or '').strip()
        return _api_limit_stats_detail_response(kind)
    global limit_stats_cache, limit_stats_last_read_ts
    now_ts = time.time()
    with _cache_lock:
        if limit_stats_cache is not None and (now_ts - limit_stats_last_read_ts) < LIMIT_STATS_CACHE_TTL:
            return jsonify(limit_stats_cache)
    try:
        data = read_limit_stats_panel()
        with _cache_lock:
            if limit_stats_cache is not None:
                data = _merge_limit_stats_panel(limit_stats_cache, data)
            limit_stats_cache = data
            limit_stats_last_read_ts = now_ts
        resp = jsonify(data)
        # 自检：若缺少 T-5 键，说明进程内仍是旧版模块（未重启或 5000 端口被另一旧进程占用）
        if 'zt_t5' not in data or 'dt_t5' not in data:
            logger.error(
                "limit_stats: 面板返回缺少 zt_t5/dt_t5 键，当前 Web 进程很可能未加载最新 rt_monitor_app.py。"
                "请结束所有占用 5000 端口的 Python 后，仅启动本仓库中的 rt_monitor_app.py 再试。"
            )
            resp.headers['X-Limit-Stats-Panel'] = 'legacy-missing-t5'
        else:
            resp.headers['X-Limit-Stats-Panel'] = 'ok'
        return resp
    except Exception as e:
        logger.exception("limit_stats 接口异常: %s", e)
        return jsonify({'ok': False, 'error': '查询失败，请稍后重试'}), 500


def _api_limit_stats_detail_response(detail_kind: str):
    """
    涨跌家数明细统一返回。detail_kind 与 read_limit_stats_detail 一致（如 zt_t1、cnt_lb_zt）。
    路径片段 lb_zt 视为 cnt_lb_zt，与旧版 /detail/lb_zt 一致。
    """
    t0 = time.perf_counter()
    try:
        kind = (detail_kind or '').strip()
        if kind == 'lb_zt':
            kind = 'cnt_lb_zt'
        if not kind:
            return jsonify({'ok': False, 'rows': [], 'error': '缺少 kind 参数'}), 400
        limit_raw = request.args.get('limit', default='500')
        try:
            limit_n = int(limit_raw)
        except Exception:
            limit_n = 500
        limit_n = max(1, min(limit_n, 2000))
        cache_key = (str(kind), int(limit_n))
        payload = _small_ttl_cache_get(_limit_stats_detail_cache, cache_key, time.time())
        if payload is None:
            payload = read_limit_stats_detail(kind, limit=limit_n)
            _small_ttl_cache_put(
                _limit_stats_detail_cache,
                cache_key,
                payload,
                LIMIT_STATS_DETAIL_CACHE_TTL,
                LIMIT_STATS_DETAIL_CACHE_MAX,
            )
        if payload.get('error') == '不支持的 kind':
            return jsonify(payload), 400
        status = 200 if payload.get('ok') else 500
        _log_slow_api(
            'limit_stats_detail',
            t0,
            f"kind={kind} limit={limit_n} rows={len(payload.get('rows') or [])}",
        )
        return jsonify(payload), status
    except Exception as e:
        logger.exception("limit_stats_detail 接口异常: %s", e)
        return jsonify({'ok': False, 'rows': [], 'error': '查询失败，请稍后重试'}), 500


def api_limit_stats_detail_flat():
    """涨跌家数明细：扁平 URL GET /api/limit_stats_detail?kind=zt_t1&limit=500（无 /detail 子路径，避免代理或多级路由下 404）。"""
    kind = (request.args.get('kind') or '').strip()
    return _api_limit_stats_detail_response(kind)


def api_limit_lb_zt_detail():
    """连板涨停明细（固定路径，兼容旧前端与代理）。"""
    return _api_limit_stats_detail_response('cnt_lb_zt')


def api_limit_stats_detail_path(detail_kind):
    """涨跌家数明细：路径形式 /api/limit_stats/detail/zt_t1?limit=500（与 /detail/lb_zt 同级，避免仅 ?kind= 时 404）。"""
    return _api_limit_stats_detail_response(detail_kind)


def api_limit_stats_detail():
    """涨跌家数明细：查询参数 ?kind=zt_t1&limit=500（与路径形式二选一）。"""
    kind = (request.args.get('kind') or '').strip()
    return _api_limit_stats_detail_response(kind)


def get_idx_minute_diff():
    """国证A指分钟成交额差值 API：读取 rt_idx_min 与 idx_mins，仅只读 stock_tushare。"""
    ts_code = (request.args.get('ts_code') or '399317.SZ').strip() or '399317.SZ'
    # 当前版本仅支持国证A指，后续可按需放开
    if ts_code != '399317.SZ':
        return jsonify({'error': '当前接口仅支持 ts_code=399317.SZ'}), 400
    try:
        payload = read_idx_minute_diff(ts_code)
        status = 200
        if payload.get('error'):
            status = 500
        return jsonify(payload), status
    except Exception as e:
        logger.exception("idx_minute_diff 接口异常: %s", e)
        return jsonify({'error': '查询失败，请稍后重试'}), 500


def get_idx_minute_diff_prev5():
    """国证A指 当日 vs 前五交易日均值 分钟差值/累计值 API，仅只读 stock_tushare。"""
    ts_code = (request.args.get('ts_code') or '399317.SZ').strip() or '399317.SZ'
    if ts_code != '399317.SZ':
        return jsonify({'error': '当前接口仅支持 ts_code=399317.SZ'}), 400
    try:
        payload = read_idx_minute_diff_prev5(ts_code)
        status = 200
        if payload.get('error'):
            status = 500
        return jsonify(payload), status
    except Exception as e:
        logger.exception("idx_minute_diff_prev5 接口异常: %s", e)
        return jsonify({'error': '查询失败，请稍后重试'}), 500


def _query_top_moves(limit: int = 30):
    """从 stock_wind.stockstat 读取冲高回落/大幅回升/当日成交额 TopN，并补充申万行业。
    - 三表均 INNER JOIN 当日新鲜 rt_k，且排除 vol=0 且 amount=0（停牌/无成交），与盘中实时统计口径一致。
    - decline：decline_rate 升序（越小回落越多）
    - recovery：recovery_rate 降序（越大回升越多）
    - amount_top：amount 降序；昨成交额来自 stk_factor_pro 各码最新 trade_date 的 amount（千元→亿）；
      自由流通换手率 = rt_k.vol（股）/ (free_share（万股）×10000) ×100；
      free_share 优先取最新日，为空则回退最近非空日（股本变动慢，盘后字段未齐时避免整列 --）。
    - amount_top_weighted_pct：TopN 内 Σ(pctchange×amount)/Σ(amount)，amount 与排序列同为 **stockstat.amount**（元）。
    """
    global _topMovesFactorExp
    empty_top = {
        'decline_top30': [],
        'recovery_top30': [],
        'amount_top30': [],
        'amount_top_weighted_pct': None,
    }
    try:
        conn = pymysql.connect(**DB_CONFIG)  # 默认 stock_wind
    except Exception as e:
        logger.exception("连接 stock_wind 失败（top_moves）: %s", e)
        return {**empty_top, 'error': '数据库连接失败'}

    def _rows_to_dicts(rows, field_rate: str):
        out = []
        for r in rows or []:
            code = r[0] if len(r) > 0 else None
            name = r[1] if len(r) > 1 else None
            amount_raw = r[2] if len(r) > 2 else None
            rate = r[3] if len(r) > 3 else None
            sw_name = r[4] if len(r) > 4 else None
            av = _to_float_or_none(amount_raw)
            amount_yi = round(av / 1e8, 2) if av is not None else None
            out.append({
                'windcodes': code,
                'sec_name': name,
                'amount_yi': amount_yi,
                field_rate: _to_float_or_none(rate),
                'sw_name': sw_name,
            })
        return out

    try:
        limit_n = int(limit)
        limit_n = max(1, min(limit_n, 200))
    except Exception:
        limit_n = 30

    try:
        # 申万行业：与 read_limit_stats_detail / _limit_stats_sw_join_sql 同一套 LEFT JOIN，禁止 SELECT 标量子查询 × TopN
        _sw_j = _limit_stats_sw_join_sql('s.windcodes')
        _rt_k_active = (
            "INNER JOIN stock_wind.rt_k rt ON rt.ts_code = s.windcodes "
            f"AND {_LIMIT_STATS_RT_K_FRESH_TRADE_DATE} "
            "AND NOT (rt.vol = 0 AND rt.amount = 0) "
        )
        sql_decline = f"""
            SELECT s.windcodes, s.sec_name, s.amount, s.decline_rate,
                   {_LIMIT_STATS_SW_NAME_SELECT}
            FROM stock_wind.stockstat s
            {_rt_k_active}{_sw_j}WHERE s.decline_rate IS NOT NULL
            ORDER BY s.decline_rate ASC
            LIMIT {limit_n}
        """
        sql_recovery = f"""
            SELECT s.windcodes, s.sec_name, s.amount, s.recovery_rate,
                   {_LIMIT_STATS_SW_NAME_SELECT}
            FROM stock_wind.stockstat s
            {_rt_k_active}{_sw_j}WHERE s.recovery_rate IS NOT NULL
            ORDER BY s.recovery_rate DESC
            LIMIT {limit_n}
        """
        sql_amount_top = f"""
            SELECT s.windcodes, s.sec_name, s.amount, s.pctchange,
                   {_LIMIT_STATS_SW_NAME_SELECT}
            FROM stock_wind.stockstat s
            {_rt_k_active}{_sw_j}WHERE s.amount IS NOT NULL AND s.amount > 0
            ORDER BY s.amount DESC
            LIMIT {limit_n}
        """
        with conn.cursor() as cur:
            cur.execute(sql_decline)
            decline_rows = cur.fetchall()
            cur.execute(sql_recovery)
            recovery_rows = cur.fetchall()
            cur.execute(sql_amount_top)
            amount_rows = cur.fetchall()

        codes = [r[0] for r in (amount_rows or []) if r and r[0]]
        volByCode = {}
        factorByCode = {}
        if codes:
            placeholders = ','.join(['%s'] * len(codes))
            sql_rt_vol = (
                f"SELECT rt.ts_code, rt.vol FROM stock_wind.rt_k rt "
                f"WHERE rt.ts_code IN ({placeholders}) "
                f"AND {_LIMIT_STATS_RT_K_FRESH_TRADE_DATE} "
                f"AND NOT (rt.vol = 0 AND rt.amount = 0)"
            )
            with conn.cursor() as cur:
                cur.execute(sql_rt_vol, codes)
                for row in cur.fetchall() or []:
                    if row and row[0] is not None:
                        volByCode[row[0]] = row[1]
            missing = []
            nowTs = time.time()
            with _topMovesInflightLock:
                factorFresh = nowTs < _topMovesFactorExp
                if factorFresh:
                    missing = [c for c in codes if c not in _topMovesFactorByCode]
                else:
                    missing = list(codes)
            if missing:
                try:
                    conn_ts = pymysql.connect(**DB_CONFIG_TUSHARE)
                    try:
                        placeholdersM = ','.join(['%s'] * len(missing))
                        sql_fp = (
                            f"SELECT a.ts_code, a.amount, "
                            f"COALESCE(a.free_share, fs.free_share) AS free_share "
                            f"FROM ( "
                            f"  SELECT f.ts_code, f.amount, f.free_share "
                            f"  FROM stock_tushare.stk_factor_pro f "
                            f"  INNER JOIN ( "
                            f"    SELECT ts_code, MAX(trade_date) AS mx "
                            f"    FROM stock_tushare.stk_factor_pro "
                            f"    WHERE ts_code IN ({placeholdersM}) "
                            f"    GROUP BY ts_code "
                            f"  ) lm ON f.ts_code = lm.ts_code AND f.trade_date = lm.mx "
                            f") a "
                            f"LEFT JOIN ( "
                            f"  SELECT f.ts_code, f.free_share "
                            f"  FROM stock_tushare.stk_factor_pro f "
                            f"  INNER JOIN ( "
                            f"    SELECT ts_code, MAX(trade_date) AS mx "
                            f"    FROM stock_tushare.stk_factor_pro "
                            f"    WHERE ts_code IN ({placeholdersM}) "
                            f"      AND free_share IS NOT NULL AND free_share > 0 "
                            f"    GROUP BY ts_code "
                            f"  ) lm2 ON f.ts_code = lm2.ts_code AND f.trade_date = lm2.mx "
                            f") fs ON a.ts_code = fs.ts_code"
                        )
                        with conn_ts.cursor() as cur:
                            cur.execute(sql_fp, missing + missing)
                            fetched = {}
                            for row in cur.fetchall() or []:
                                if row and row[0] is not None:
                                    fetched[row[0]] = (row[1], row[2])
                        with _topMovesInflightLock:
                            if not factorFresh:
                                _topMovesFactorByCode.clear()
                            _topMovesFactorByCode.update(fetched)
                            _topMovesFactorExp = time.time() + TOP_MOVES_FACTOR_TTL
                    finally:
                        conn_ts.close()
                except Exception as e_fp:
                    logger.warning("stk_factor_pro 批量查询失败（昨成交额/自由换手）: %s", e_fp)
            with _topMovesInflightLock:
                for c in codes:
                    if c in _topMovesFactorByCode:
                        factorByCode[c] = _topMovesFactorByCode[c]

        # 成交额加权涨跌：与 sql_amount_top 同一批行；权重 = stockstat.amount（与 ORDER BY 一致）
        weighted_pct_num = 0.0
        weighted_pct_den = 0.0
        amount_top30 = []
        for r in amount_rows or []:
            code = r[0] if len(r) > 0 else None
            name = r[1] if len(r) > 1 else None
            amount_raw = r[2] if len(r) > 2 else None
            pct_raw = r[3] if len(r) > 3 else None
            sw_name = r[4] if len(r) > 4 else None
            av = _to_float_or_none(amount_raw)
            pv_row = _to_float_or_none(pct_raw)
            if av is not None and av > 0 and pv_row is not None:
                weighted_pct_num += pv_row * av
                weighted_pct_den += av
            amount_yi = round(av / 1e8, 2) if av is not None else None
            prev_amount_yi = None
            free_float_turnover_pct = None
            fac = factorByCode.get(code) if code else None
            if fac is not None:
                am_qian, fshr = fac[0], fac[1]
                aq = _to_float_or_none(am_qian)
                if aq is not None:
                    prev_amount_yi = round(aq / 1e5, 2)
                vol_v = _to_float_or_none(volByCode.get(code))
                fs_v = _to_float_or_none(fshr)
                if vol_v is not None and fs_v is not None and fs_v > 0:
                    free_float_turnover_pct = round(vol_v / (fs_v * 10000.0) * 100.0, 2)
            amount_top30.append({
                'windcodes': code,
                'sec_name': name,
                'amount_yi': amount_yi,
                'pctchange': _to_float_or_none(pct_raw),
                'sw_name': sw_name,
                'prev_amount_yi': prev_amount_yi,
                'free_float_turnover_pct': free_float_turnover_pct,
            })

        amount_top_weighted_pct = None
        if weighted_pct_den > 0:
            amount_top_weighted_pct = round(weighted_pct_num / weighted_pct_den, 2)

        return {
            'decline_top30': _rows_to_dicts(decline_rows, 'decline_rate'),
            'recovery_top30': _rows_to_dicts(recovery_rows, 'recovery_rate'),
            'amount_top30': amount_top30,
            'amount_top_weighted_pct': amount_top_weighted_pct,
            'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
    except Exception as e:
        logger.exception("查询 top_moves 失败: %s", e)
        return {**empty_top, 'error': str(e)}
    finally:
        conn.close()


def _emptyTopMovesPayload():
    return {
        'decline_top30': [],
        'recovery_top30': [],
        'amount_top30': [],
        'amount_top_weighted_pct': None,
    }


def _loadTopMovesCachedOrStale(limitN: int):
    nowTs = time.time()
    hit = _small_ttl_cache_get(_top_moves_cache, limitN, nowTs)
    if hit is not None:
        return hit, True
    return _topMovesLastOk.get(limitN), False


def _topMovesIsHeavyDb() -> bool:
    try:
        from rt_monitor.services.fina_arturn_queries import isFinaArturnBuilding
        from rt_monitor.services.sw_industry import isSwIndustryRefreshing
        return isFinaArturnBuilding() or isSwIndustryRefreshing()
    except Exception:
        return False


def _topMovesAdaptiveInterval(elapsedSec: float) -> float:
    """查询越慢下次间隔越长；健康时贴前端 2s，打满库时退到 8s。"""
    if elapsedSec <= 0.25:
        return TOP_MOVES_REFRESH_MIN_SEC
    return min(
        TOP_MOVES_REFRESH_MAX_SEC,
        max(TOP_MOVES_REFRESH_MIN_SEC, float(elapsedSec) * 4.0),
    )


def _storeTopMovesOk(limitN: int, payload: dict, ttlSec: float) -> None:
    if not payload or payload.get('error'):
        return
    _topMovesLastOk[limitN] = payload
    _small_ttl_cache_put(
        _top_moves_cache, limitN, payload, ttlSec, TOP_MOVES_CACHE_MAX,
    )


def _fetchTopMovesOnce(limitN: int, force: bool = False):
    """singleflight 查库；重查询期间有旧结果则让路。force=True 时忽略 TTL（后台刷）。"""
    waitEv = None
    isOwner = False
    cached = None
    with _topMovesInflightLock:
        cached, isFresh = _loadTopMovesCachedOrStale(limitN)
        if isFresh and cached is not None and not force:
            return cached, True
        existing = _topMovesInflight.get(limitN)
        if existing is not None:
            waitEv = existing
        else:
            waitEv = threading.Event()
            _topMovesInflight[limitN] = waitEv
            isOwner = True

    if not isOwner:
        waitEv.wait(timeout=8.0)
        cached, _isFresh = _loadTopMovesCachedOrStale(limitN)
        if cached is not None:
            return cached, True
        return _emptyTopMovesPayload(), True

    try:
        if cached is not None and _topMovesIsHeavyDb():
            _storeTopMovesOk(limitN, cached, TOP_MOVES_REFRESH_MIN_SEC)
            return cached, True
        t0 = time.perf_counter()
        payload = _query_top_moves(limit=limitN)
        ttlSec = _topMovesAdaptiveInterval(time.perf_counter() - t0)
        _storeTopMovesOk(limitN, payload, ttlSec)
        return payload, False
    finally:
        with _topMovesInflightLock:
            _topMovesInflight.pop(limitN, None)
        waitEv.set()


def _topMovesRefreshPlan(
    lastHttpMono: float,
    nowMono: float,
    hasLastOk: bool,
    isHeavy: bool,
) -> tuple[bool, float]:
    """后台一轮：是否查库、本轮间隔（查库成功后由耗时覆盖）。间隔从本轮开始起算。"""
    if lastHttpMono <= 0:
        if not hasLastOk:
            return True, TOP_MOVES_REFRESH_MIN_SEC
        return False, TOP_MOVES_IDLE_PAUSE_SEC
    idleFor = nowMono - lastHttpMono
    if idleFor > TOP_MOVES_IDLE_PAUSE_SEC:
        return False, TOP_MOVES_IDLE_PAUSE_SEC
    if isHeavy:
        return False, TOP_MOVES_REFRESH_MIN_SEC
    return True, TOP_MOVES_REFRESH_MIN_SEC


def _topMovesRefreshLoop() -> None:
    """首页 Top30 后台刷：有人看 2～8s；无人看停刷并按 15s 再探；启动先预热一份。"""
    logger.info(
        'top_moves 后台刷新已启动 min=%.1fs max=%.1fs idle_pause=%.0fs',
        TOP_MOVES_REFRESH_MIN_SEC, TOP_MOVES_REFRESH_MAX_SEC, TOP_MOVES_IDLE_PAUSE_SEC,
    )
    while True:
        loopT0 = time.perf_counter()
        shouldFetch, interval = _topMovesRefreshPlan(
            _topMovesLastHttpMono,
            time.monotonic(),
            TOP_MOVES_HOME_LIMIT in _topMovesLastOk,
            _topMovesIsHeavyDb(),
        )
        try:
            if shouldFetch:
                t0 = time.perf_counter()
                payload, fromCache = _fetchTopMovesOnce(TOP_MOVES_HOME_LIMIT, force=True)
                elapsed = time.perf_counter() - t0
                if fromCache:
                    interval = TOP_MOVES_REFRESH_MIN_SEC
                elif payload and not payload.get('error'):
                    interval = _topMovesAdaptiveInterval(elapsed)
                    _log_slow_api(
                        'top_moves',
                        t0,
                        f"bg limit={TOP_MOVES_HOME_LIMIT} decline={len(payload.get('decline_top30') or [])}",
                    )
                else:
                    interval = TOP_MOVES_REFRESH_MAX_SEC
        except Exception:
            logger.exception('top_moves 后台刷新失败')
            interval = TOP_MOVES_REFRESH_MAX_SEC
        remain = interval - (time.perf_counter() - loopT0)
        time.sleep(max(0.2, remain))


def warmupTopMovesCacheAsync() -> None:
    """启动后台刷新首页 Top30，HTTP 只读这份缓存。"""
    global _topMovesRefreshStarted
    if _topMovesRefreshStarted:
        return
    _topMovesRefreshStarted = True
    threading.Thread(
        target=_topMovesRefreshLoop, daemon=True, name='top-moves-refresh',
    ).start()


def _resolveTopMovesPayload(limitN: int):
    """有旧结果立刻返回（SWR）；首页由后台刷新，其它 limit 仍按需 singleflight。"""
    cached, isFresh = _loadTopMovesCachedOrStale(limitN)
    if cached is not None and (isFresh or limitN == TOP_MOVES_HOME_LIMIT):
        return cached, True
    return _fetchTopMovesOnce(limitN)


def get_top_moves():
    """冲高回落/大幅回升/当日成交额 Top30（stockstat + 申万；成交额表含昨成交额、自由换手、加权涨跌幅）"""
    global _topMovesLastHttpMono
    t0 = time.perf_counter()
    try:
        _topMovesLastHttpMono = time.monotonic()
        try:
            limit_n = int(request.args.get('limit', default='30'))
        except Exception:
            limit_n = 30
        limit_n = max(1, min(limit_n, 200))
        payload, fromCache = _resolveTopMovesPayload(limit_n)
        status = 200 if not payload.get('error') else 500
        if not fromCache:
            _log_slow_api(
                'top_moves',
                t0,
                f"limit={limit_n} decline={len(payload.get('decline_top30') or [])}",
            )
        return jsonify(payload), status
    except Exception as e:
        logger.exception("top_moves 接口异常: %s", e)
        return jsonify({'error': str(e)}), 500


def _normalize_end_date_yyyymmdd(raw_val) -> str | None:
    """将 end_date 参数标准化为 YYYYMMDD。"""
    if raw_val is None:
        return None
    s = str(raw_val).strip()
    if not s:
        return None
    s = s.replace('-', '')
    if len(s) == 8 and s.isdigit():
        return s
    return None



# --- fina 查询：services/fina_arturn_queries（P4.2）---
from rt_monitor.services.fina_arturn_queries import (  # noqa: E402
    FINA_ARTURN_OCS_WQCV_DECAY,
    FINA_ARTURN_TABLE_CACHE_MAX,
    FINA_ARTURN_TABLE_CACHE_TTL,
    FINA_ARTURN_WARMUP_PREFETCH_N,
    _derive_financial_periods,
    _derive_quarter_periods,
    _fina_arturn_attach_ar_receivable_pct,
    _fina_batch_balancesheet_contract_fix_yi,
    _fina_batch_cashflow_n_cashflow_act,
    _fina_batch_fina_indicator_q_dtprofit,
    _fina_batch_fina_indicator_q_gs_margin_q_dt_roe,
    _fina_batch_income_total_revenue,
    _fina_end_date_str_from_yq,
    _fina_income_date_keys_with_yoy_for_periods,
    _fina_indicator_metric_table_cache,
    _fina_periods_union_yoy_for_q_dtprofit,
    _fina_prev_yq,
    _fina_quarter_single_revenue,
    _fina_yq_from_anchor,
    _fina_yoy_anchor_end_date,
    _query_arturn_end_dates,
    _query_fina_arturn_table,
    _query_fina_fixed_assets_revenue_series,
    _query_stock_basic_sec_list_act,
    _sw_attach_high_qoq_pair,
    _sw_cf_industry_single_q_sum_yuan,
    _sw_cf_yoy_yoypct_for_codes,
    _sw_dtp_yoy_yoypct_for_codes,
    _sw_median_rounded,
    _sw_rev_yoy_yoypct_for_codes,
)


# --- 申万/宽基：services/sw_industry（P2 迁出）---
from rt_monitor.services.sw_industry import (  # noqa: E402
    INDEX_MONITOR_CODES,
    api_index_industry_stats,
    api_sw_index_amt_pctile_series,
    api_sw_industry_limit_detail,
    api_sw_industry_stats,
    read_sw_industry_limit_detail,
    warmupSwIndustryCacheAsync as _warmup_sw_industry_cache_async,
)

ETF_BIAS_COLUMN_MAP = {
    '10MA': ('per_bias10ma', 'bias10ma'),
    '20MA': ('per_bias20ma', 'bias20ma'),
    '20EMA': ('per_bias20ema', 'bias20ema'),
}
# column=20MA 时同图带 20EMA；10MA 暂无 10EMA，仅主列
ETF_BIAS_PAIR_EMA_COL = {'20MA': 'per_bias20ema'}
# expanding std 最小样本数，与 bias_sigma_reversion_verify 一致，避免起步期不稳定
BIAS_SERIES_MIN_PERIODS_STD = 60
BIAS_SERIES_LIMIT = 300
PER_BIAS_WINDOW = 1250
# BIAS / 聚集度 Per 弹窗：query limit 上限（与前端 myuiux：15m≈1年、60m≈2年、日线≈5年）
ETF_BIAS_MCD_MODAL_LIMIT_CAP_15MIN = 5000
ETF_BIAS_MCD_MODAL_LIMIT_CAP_60MIN = 2500
ETF_BIAS_MCD_MODAL_LIMIT_CAP_D = 1300


def _resolve_etf_bias_mcd_series_limit(freq_param: str, limit_raw: str | None, default_limit: int) -> int:
    """解析 etf_bias_series / etf_mcd_series 的 limit，按周期封顶，非法则回退 default_limit。
    若 freq 不在 caps（路由层应已拦掉），封顶值用 default_limit，避免误放大请求。"""
    caps = {
        '15MIN': ETF_BIAS_MCD_MODAL_LIMIT_CAP_15MIN,
        '60MIN': ETF_BIAS_MCD_MODAL_LIMIT_CAP_60MIN,
        'D': ETF_BIAS_MCD_MODAL_LIMIT_CAP_D,
    }
    cap = caps.get(freq_param, default_limit)
    try:
        if limit_raw is None or str(limit_raw).strip() == '':
            lim = default_limit
        else:
            lim = int(limit_raw)
    except (TypeError, ValueError):
        lim = default_limit
    return max(1, min(lim, cap))


def _series_to_json_floats(s: pd.Series) -> list:
    """将 pandas Series 转为 JSON 安全浮点列表：NaN → None（etf_bias / etf_mcd 多列共用，避免重复定义）。"""
    return [None if pd.isna(x) else float(x) for x in s]


# 通达信式 (C-MA20)>0&(C-EMA20)>0&(C-REF(C,20))>0 等：仅用合并后的 close 在服务端 rolling/ewm 重算，与软件口径一致（不用库内 ma 列，避免与 close 不同步）
ETF_MA_SIGNAL_ORDER = ETF_BIAS_ORDER
ETF_MA_SIGNAL_FREQS = ['60MIN', 'D']  # 不含 15MIN：周期短、噪声大
ETF_MA_SIGNAL_PERIODS = [20, 60]
MA_SIGNAL_SERIES_LIMIT = BIAS_SERIES_LIMIT
PER_MCD_WINDOW_15MIN = 850
PER_MCD_WINDOW_60MIN = 500
PER_MCD_WINDOW_D = 250
MA_SIGNAL_FETCH_BARS_15MIN = max(MA_SIGNAL_SERIES_LIMIT, PER_MCD_WINDOW_15MIN)
MA_SIGNAL_FETCH_BARS_60MIN = max(MA_SIGNAL_SERIES_LIMIT, PER_MCD_WINDOW_60MIN)
MA_SIGNAL_FETCH_BARS_D = max(MA_SIGNAL_SERIES_LIMIT, PER_MCD_WINDOW_D)
MCD_WARMUP_BARS = 130  # mcd 依赖 ma120，接口侧取数需额外预热，避免弹窗前段大量 NaN


def _apply_today_rt_min_qfq(conn, ts_code: str, df: pd.DataFrame) -> pd.DataFrame:
    """当日 etf_rt_min 行为 raw 收盘；用 etf_fund_adj 转为前复权，与历史 COALESCE(close_qfq,close) 拼接一致。"""
    if df is None or df.empty:
        return df
    try:
        from fund_adj import attach_close_qfq_series, load_adj_dataframe
    except ImportError:
        return df
    adj = load_adj_dataframe(conn, ts_code)
    if adj is None or adj.empty:
        return df
    tt = pd.to_datetime(df["trade_time"], errors="coerce")
    today_d = datetime.now().date()
    mask = tt.dt.date == today_d
    if not mask.any():
        return df
    out = df.copy()
    sub = out.loc[mask].copy()
    qfq = attach_close_qfq_series(sub, "trade_time", "close", adj)
    out.loc[mask, "close"] = qfq.values
    return out


def _drop_open_anchor_bar(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """分钟序列剔除 09:30 开盘锚点，保持 60MIN=4 根/日、15MIN=16 根/日。"""
    if df is None or df.empty:
        return df
    freq_u = (freq or "").upper()
    if freq_u not in ("15MIN", "60MIN"):
        return df
    out = df.copy()
    tt = pd.to_datetime(out["trade_time"], errors="coerce")
    mask_anchor = (tt.dt.hour == 9) & (tt.dt.minute == 30)
    if mask_anchor.any():
        out = out.loc[~mask_anchor].copy()
    return out


def _qfq_single_close(conn, ts_code: str, trade_time, close_raw: float) -> float:
    try:
        from fund_adj import attach_close_qfq_series, load_adj_dataframe
    except ImportError:
        return float(close_raw)
    adj = load_adj_dataframe(conn, ts_code)
    if adj is None or adj.empty:
        return float(close_raw)
    one = pd.DataFrame({"trade_time": [trade_time], "close": [close_raw]})
    s = attach_close_qfq_series(one, "trade_time", "close", adj)
    v = s.iloc[0]
    return float(v) if not pd.isna(v) else float(close_raw)


def _load_merged_minute_close_df(conn, ts_code: str, freq_rt: str, limit: int) -> pd.DataFrame:
    """15/60 分钟：按时序拼接三段——
    ① 历史：etf_stk_mins_ma 先 LIMIT 再 JOIN etf_stk_mins（trade_time<当日 0 点）；
    ② 当日已落地 K：etf_rt_min；
    ③ 当前周期虚拟 K（rt_k 价）：etf_bias_realtime（updated_at 在 ETF_BIAS_REALTIME_MAX_AGE 内），与同名列 trade_time 重复时保留虚拟行（keep last）。
    最后取最近 limit 根，当日 raw 转 qfq，剔除 09:30 锚点。
    """
    freq_db = freq_rt.lower()
    sql_today = (
        "SELECT r.trade_time, r.close FROM stock_tushare.etf_rt_min r "
        "WHERE r.ts_code = %s AND r.freq = %s AND DATE(r.trade_time) = CURDATE() "
        "AND r.close IS NOT NULL AND r.close > 0 ORDER BY r.trade_time"
    )
    with conn.cursor() as cur:
        cur.execute(sql_today, (ts_code, freq_rt))
        rows_today = cur.fetchall()
    need = limit - len(rows_today)
    need = max(need, 1)
    # 历史段：先在 ma 表按 (ts_code,freq) 取「最近 inner_cap 根」再与 etf_stk_mins 按键 JOIN，避免两表大范围 JOIN + 全量排序（易触发 60s+ 读超时）。
    # inner_cap 需大于 need，以覆盖「少数行 close_qfq 为空需靠 s.close、或 JOIN 过滤」导致的缺口；仍与「全历史排序后 LIMIT need」在极端脏数据下可能差几根，生产 ETF 数据可忽略。
    inner_cap = min(max(need * 6, 400), 15000)
    sql_history = (
        "SELECT z.trade_time, z.close FROM ("
        "SELECT m.trade_time, COALESCE(m.close_qfq, s.close) AS close FROM ("
        "SELECT trade_time, close_qfq, ts_code, freq FROM stock_tushare.etf_stk_mins_ma "
        "WHERE ts_code = %s AND freq = %s AND trade_time < CURDATE() "
        "ORDER BY trade_time DESC LIMIT %s"
        ") m INNER JOIN stock_tushare.etf_stk_mins s "
        "ON m.ts_code = s.ts_code AND m.freq = s.freq AND m.trade_time = s.trade_time "
        "WHERE s.close IS NOT NULL AND s.close > 0 "
        "ORDER BY m.trade_time DESC LIMIT %s"
        ") z ORDER BY z.trade_time ASC"
    )
    with conn.cursor() as cur:
        cur.execute(sql_history, (ts_code, freq_db, inner_cap, need))
        rows_history = cur.fetchall()
    rows_asc = list(reversed(rows_history)) + list(rows_today)
    sql_rt = (
        "SELECT trade_time, close FROM stock_tushare.etf_bias_realtime "
        "WHERE ts_code = %s AND freq = %s AND trade_time IS NOT NULL "
        "AND close IS NOT NULL AND close > 0 "
        "AND updated_at >= DATE_SUB(NOW(), INTERVAL %s SECOND)"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql_rt, (ts_code, freq_rt, ETF_BIAS_REALTIME_MAX_AGE))
            row_rt = cur.fetchone()
    except Exception as e:
        logger.debug("etf_bias_realtime 虚拟K读取失败 ts_code=%s freq=%s: %s", ts_code, freq_rt, e)
        row_rt = None
    if row_rt:
        rt_t, rt_c = row_rt[0], row_rt[1]
        rows_asc.append((rt_t, float(rt_c)))
    if not rows_asc:
        return pd.DataFrame(columns=['trade_time', 'close'])
    df = pd.DataFrame(rows_asc, columns=['trade_time', 'close'])
    df['trade_time'] = pd.to_datetime(df['trade_time'], errors='coerce')
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    df = df.dropna(subset=['trade_time'])
    df = df.sort_values('trade_time')
    df = df.drop_duplicates(subset=['trade_time'], keep='last')
    df = df.iloc[-limit:]
    out = _apply_today_rt_min_qfq(conn, ts_code, df)
    return _drop_open_anchor_bar(out, freq_rt)


def _load_minute_bias_series_modal_df(
    conn, ts_code: str, freq_rt: str, fetch_bars: int
) -> pd.DataFrame | None:
    """BIAS 弹窗 15/60 分钟：与 etf_mcd_series 同构（单表 ma + rt_min + 虚拟K），带 per_bias*。
    避免 ma 与 stk_mins 大表 JOIN，且多数情况下可跳过全段 rolling 百分位。缺列等失败时返回 None。"""
    freq_db = freq_rt.lower()
    extra = "per_bias10ma, per_bias20ma, per_bias20ema"
    sql_today = (
        f"SELECT trade_time, close, {extra} FROM stock_tushare.etf_rt_min "
        "WHERE ts_code=%s AND freq=%s AND DATE(trade_time)=CURDATE() "
        "AND close IS NOT NULL AND close>0 ORDER BY trade_time"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql_today, (ts_code, freq_rt))
            rows_today = cur.fetchall()
        need = max(fetch_bars - len(rows_today), 1)
        sql_hist = (
            f"SELECT trade_time, close_qfq AS close, {extra} FROM stock_tushare.etf_stk_mins_ma "
            "WHERE ts_code=%s AND freq=%s AND trade_time<CURDATE() "
            "ORDER BY trade_time DESC LIMIT %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql_hist, (ts_code, freq_db, need))
            rows_hist = cur.fetchall()
    except pymysql.OperationalError as e:
        logger.debug(
            "BIAS 弹窗分钟库内 per 路径不可用 ts_code=%s freq=%s: %s", ts_code, freq_rt, e
        )
        return None
    rows_asc = list(reversed(rows_hist)) + list(rows_today)
    sql_rt = (
        f"SELECT trade_time, close, {extra} FROM stock_tushare.etf_bias_realtime "
        "WHERE ts_code=%s AND freq=%s AND trade_time IS NOT NULL "
        "AND close IS NOT NULL AND close>0 "
        "AND updated_at >= DATE_SUB(NOW(), INTERVAL %s SECOND)"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql_rt, (ts_code, freq_rt, ETF_BIAS_REALTIME_MAX_AGE))
            row_rt = cur.fetchone()
    except Exception as e:
        logger.debug("etf_bias_realtime 虚拟K读取失败 ts_code=%s freq=%s: %s", ts_code, freq_rt, e)
        row_rt = None
    if row_rt:
        rows_asc.append(row_rt)
    cols = ["trade_time", "close", "per_bias10ma", "per_bias20ma", "per_bias20ema"]
    if not rows_asc:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows_asc, columns=cols)
    df["trade_time"] = pd.to_datetime(df["trade_time"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    for c in ("per_bias10ma", "per_bias20ma", "per_bias20ema"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["trade_time"])
    df = df.sort_values("trade_time").drop_duplicates(subset=["trade_time"], keep="last")
    if len(df) > fetch_bars:
        df = df.iloc[-fetch_bars:].copy()
    df = _apply_today_rt_min_qfq(conn, ts_code, df)
    return _drop_open_anchor_bar(df, freq_rt)


def _load_minute_close_mcd_style(conn, ts_code: str, freq_rt: str, fetch_bars: int) -> pd.DataFrame:
    """MA 信号表用：与 etf_mcd 同构的 close（etf_stk_mins_ma.close_qfq + etf_rt_min + 虚拟K），无 stk_mins JOIN。
    空表表示无数据，调用方可回退 _load_merged_minute_close_df（兼容 close_qfq 全空等极端情况）。"""
    freq_db = freq_rt.lower()
    sql_today = (
        "SELECT trade_time, close FROM stock_tushare.etf_rt_min "
        "WHERE ts_code=%s AND freq=%s AND DATE(trade_time)=CURDATE() "
        "AND close IS NOT NULL AND close>0 ORDER BY trade_time"
    )
    with conn.cursor() as cur:
        cur.execute(sql_today, (ts_code, freq_rt))
        rows_today = cur.fetchall()
    need = max(fetch_bars - len(rows_today), 1)
    sql_hist = (
        "SELECT trade_time, close_qfq AS close FROM stock_tushare.etf_stk_mins_ma "
        "WHERE ts_code=%s AND freq=%s AND trade_time<CURDATE() "
        "ORDER BY trade_time DESC LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql_hist, (ts_code, freq_db, need))
        rows_hist = cur.fetchall()
    rows_asc = list(reversed(rows_hist)) + list(rows_today)
    sql_rt = (
        "SELECT trade_time, close FROM stock_tushare.etf_bias_realtime "
        "WHERE ts_code=%s AND freq=%s AND trade_time IS NOT NULL "
        "AND close IS NOT NULL AND close>0 "
        "AND updated_at >= DATE_SUB(NOW(), INTERVAL %s SECOND)"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql_rt, (ts_code, freq_rt, ETF_BIAS_REALTIME_MAX_AGE))
            row_rt = cur.fetchone()
    except Exception as e:
        logger.debug("etf_bias_realtime 虚拟K读取失败 ts_code=%s freq=%s: %s", ts_code, freq_rt, e)
        row_rt = None
    if row_rt:
        rows_asc.append(row_rt)
    if not rows_asc:
        return pd.DataFrame(columns=["trade_time", "close"])
    df = pd.DataFrame(rows_asc, columns=["trade_time", "close"])
    df["trade_time"] = pd.to_datetime(df["trade_time"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["trade_time"])
    df = df.sort_values("trade_time").drop_duplicates(subset=["trade_time"], keep="last")
    if len(df) > fetch_bars:
        df = df.iloc[-fetch_bars:].copy()
    df = _apply_today_rt_min_qfq(conn, ts_code, df)
    return _drop_open_anchor_bar(df, freq_rt)


def _load_daily_bias_modal_df(conn, ts_code: str, limit: int) -> pd.DataFrame:
    """日线 BIAS 弹窗：尽量读 etf_daily.per_bias10ma + realtime；缺列时回落为仅 close（由调用方 rolling per）。"""
    try:
        sql_daily = (
            "SELECT trade_date AS trade_time, COALESCE(close_qfq, close) AS close, per_bias10ma "
            "FROM stock_tushare.etf_daily "
            "WHERE ts_code = %s AND close IS NOT NULL AND close > 0 "
            "ORDER BY trade_date DESC LIMIT %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql_daily, (ts_code, limit))
            rows = cur.fetchall()
        rows_asc = list(reversed(rows)) if rows else []
        sql_rt = (
            "SELECT trade_time, close, per_bias10ma FROM stock_tushare.etf_bias_realtime "
            "WHERE ts_code = %s AND freq = 'D' AND trade_time IS NOT NULL "
            "AND DATE(trade_time) = CURDATE() LIMIT 1"
        )
        with conn.cursor() as cur:
            cur.execute(sql_rt, (ts_code,))
            row_rt = cur.fetchone()
        if row_rt:
            rt_t, rt_c = row_rt[0], row_rt[1]
            rt_p = row_rt[2] if len(row_rt) > 2 else None
            if rt_c is not None and float(rt_c) > 0:
                rt_c_q = _qfq_single_close(conn, ts_code, rt_t, float(rt_c))
                rows_asc.append((rt_t, rt_c_q, rt_p))
            else:
                rows_asc.append((rt_t, rt_c, rt_p))
        if not rows_asc:
            return pd.DataFrame(columns=["trade_time", "close", "per_bias10ma"])
        df = pd.DataFrame(rows_asc, columns=["trade_time", "close", "per_bias10ma"])
        df["trade_time"] = pd.to_datetime(df["trade_time"], errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["per_bias10ma"] = pd.to_numeric(df["per_bias10ma"], errors="coerce")
        df = df.dropna(subset=["trade_time"])
        df = df.sort_values("trade_time").drop_duplicates(subset=["trade_time"], keep="last")
        if len(df) > limit:
            df = df.iloc[-limit:].copy()
        return df
    except pymysql.OperationalError as e:
        logger.debug("日线 BIAS 弹窗无 per_bias10ma 列，回落仅 close: %s", e)
    df_fb = _load_daily_close_df(conn, ts_code, limit)
    if df_fb.empty:
        return df_fb
    out = df_fb.copy()
    out["per_bias10ma"] = pd.NA
    return out


def _load_daily_close_df(conn, ts_code: str, limit: int) -> pd.DataFrame:
    """日线：etf_daily 最近 limit 条升序；若 etf_bias_realtime 有当日点则追加（与 etf_bias_series 一致）。"""
    sql_daily = (
        "SELECT trade_date AS trade_time, COALESCE(close_qfq, close) AS close FROM stock_tushare.etf_daily "
        "WHERE ts_code = %s AND close IS NOT NULL AND close > 0 "
        "ORDER BY trade_date DESC LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql_daily, (ts_code, limit))
        rows = cur.fetchall()
    rows_asc = list(reversed(rows)) if rows else []
    sql_rt = (
        "SELECT trade_time, close FROM stock_tushare.etf_bias_realtime "
        "WHERE ts_code = %s AND freq = 'D' AND trade_time IS NOT NULL "
        "AND DATE(trade_time) = CURDATE() LIMIT 1"
    )
    with conn.cursor() as cur:
        cur.execute(sql_rt, (ts_code,))
        row_rt = cur.fetchone()
    if row_rt:
        rt_t, rt_c = row_rt[0], row_rt[1]
        if rt_c is not None and float(rt_c) > 0:
            rt_c_q = _qfq_single_close(conn, ts_code, rt_t, float(rt_c))
            rows_asc.append((rt_t, rt_c_q))
        else:
            rows_asc.append((rt_t, rt_c))
    if not rows_asc:
        return pd.DataFrame(columns=['trade_time', 'close'])
    rows_asc = rows_asc[-limit:]
    df = pd.DataFrame(rows_asc, columns=['trade_time', 'close'])
    df['trade_time'] = pd.to_datetime(df['trade_time'], errors='coerce')
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    return df


def _compute_ma_ref_signals(close: pd.Series, periods: list) -> dict[int, pd.Series]:
    """每组周期 n：多头 (C>MAn)&(C>EMAn)&(C>REF(C,n))；空头三式均 <0。返回 n -> Series[int] 1/-1/0。"""
    c = close.astype(float)
    out: dict[int, pd.Series] = {}
    for n in periods:
        ma_n = c.rolling(window=n, min_periods=n).mean()
        ema_n = c.ewm(span=n, adjust=False).mean()
        ref_n = c.shift(n)
        bull = (c > ma_n) & (c > ema_n) & (c > ref_n)
        bear = (c < ma_n) & (c < ema_n) & (c < ref_n)
        sig = pd.Series(0, index=c.index, dtype=int)
        sig = sig.mask(bull, 1)
        sig = sig.mask(bear, -1)
        out[n] = sig.fillna(0).astype(int)
    return out


def _signal_series_to_json(sig: pd.Series) -> list:
    return [int(x) if not pd.isna(x) else 0 for x in sig.tolist()]


def _last_signal_token(sig: pd.Series) -> str:
    if sig is None or len(sig) == 0:
        return 'none'
    v = int(sig.iloc[-1])
    if v == 1:
        return 'up'
    if v == -1:
        return 'down'
    return 'none'


def _rolling_percentile_bias(series: pd.Series, window: int) -> pd.Series:
    """滚动窗口内当前值百分位（含当前点），返回 0~100。
    NumPy 内层向量化，避免 pandas rolling.apply 对每根 K 调用 Python 回调（limit 大、窗口 1250 时极慢）。"""
    if series is None or len(series) == 0:
        return series
    idx = series.index
    x = np.asarray(series.to_numpy(dtype=np.float64), dtype=np.float64)
    n = len(x)
    out = np.full(n, np.nan, dtype=np.float64)
    for i in range(n):
        cur = x[i]
        if np.isnan(cur):
            continue
        lo = max(0, i - window + 1)
        chunk = x[lo : i + 1]
        m = ~np.isnan(chunk)
        if not np.any(m):
            continue
        valid = chunk[m]
        out[i] = float(np.sum(valid <= cur) / valid.size * 100.0)
    return pd.Series(out, index=idx)


def _bias_per_from_db_or_roll(bias_ser: pd.Series, per_db_col: pd.Series | None) -> pd.Series:
    """优先用库内 per_bias；仅当 bias 已有效而 per 仍缺时回退整列 rolling.apply（与无库列时等价）。"""
    if per_db_col is None:
        return _rolling_percentile_bias(bias_ser, PER_BIAS_WINDOW)
    per_db = pd.to_numeric(per_db_col, errors="coerce")
    if not per_db.index.equals(bias_ser.index):
        per_db = per_db.reindex(bias_ser.index)
    need_fill = per_db.isna() & bias_ser.notna()
    if not need_fill.any():
        return per_db
    rb = _rolling_percentile_bias(bias_ser, PER_BIAS_WINDOW)
    return per_db.where(per_db.notna(), rb)


def _calc_bias_per_from_qfq_close(close_px: pd.Series) -> dict:
    """基于 qfq close 计算最新 per_bias（10MA/20MA/20EMA）。"""
    close_s = pd.to_numeric(close_px, errors='coerce')
    if close_s is None or close_s.empty:
        return {'10MA': None, '20MA': None, '20EMA': None}

    ma10 = close_s.rolling(window=10, min_periods=10).mean()
    ma20 = close_s.rolling(window=20, min_periods=20).mean()
    ema20 = close_s.ewm(span=20, adjust=False).mean()

    bias10ma = (close_s - ma10) / ma10 * 100.0
    bias20ma = (close_s - ma20) / ma20 * 100.0
    bias20ema = (close_s - ema20) / ema20 * 100.0

    per10 = _rolling_percentile_bias(bias10ma, PER_BIAS_WINDOW)
    per20ma = _rolling_percentile_bias(bias20ma, PER_BIAS_WINDOW)
    per20ema = _rolling_percentile_bias(bias20ema, PER_BIAS_WINDOW)

    def _last_round(series: pd.Series):
        if series is None or series.empty:
            return None
        v = series.iloc[-1]
        if pd.isna(v):
            return None
        return round(float(v), 2)

    return {
        '10MA': _last_round(per10),
        '20MA': _last_round(per20ma),
        '20EMA': _last_round(per20ema),
    }


def _calc_mcd_per_from_qfq_close(close_px: pd.Series, per_window: int):
    """基于 qfq close 计算最新 per_mcd（六线聚集度滚动百分位）。"""
    close_s = pd.to_numeric(close_px, errors='coerce')
    if close_s is None or close_s.empty:
        return None

    ma20 = close_s.rolling(window=20, min_periods=20).mean()
    ema20 = close_s.ewm(span=20, adjust=False).mean()
    ma60 = close_s.rolling(window=60, min_periods=60).mean()
    ema60 = close_s.ewm(span=60, adjust=False).mean()
    ma120 = close_s.rolling(window=120, min_periods=120).mean()
    ema120 = close_s.ewm(span=120, adjust=False).mean()
    six = pd.concat([ma20, ema20, ma60, ema60, ma120, ema120], axis=1)
    mcd_pct = (six.std(axis=1, ddof=0) / close_s) * 100.0
    all_ok = six.notna().all(axis=1)
    valid_denom = close_s.notna() & (close_s > 0)
    mcd_pct = mcd_pct.where(all_ok & valid_denom)
    per_mcd = _rolling_percentile_bias(mcd_pct, per_window)
    if per_mcd is None or per_mcd.empty:
        return None
    v = per_mcd.iloc[-1]
    if pd.isna(v):
        return None
    return round(float(v), 2)


def _read_latest_mcd_per(conn, ts_code: str, freq_rt: str):
    """优先读取已落库最新 per_mcd。
    为保证前端两张表实时一致性：
    - 15/60MIN：先读 etf_bias_realtime，再回退 etf_rt_min→etf_stk_mins_ma
    - D：先读 etf_bias_realtime(D)，再回退 etf_daily
    """
    freq_u = (freq_rt or "").upper()
    try:
        with conn.cursor() as cur:
            if freq_u in ("15MIN", "60MIN"):
                cur.execute(
                    "SELECT per_mcd FROM stock_tushare.etf_bias_realtime "
                    "WHERE ts_code=%s AND freq=%s "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (ts_code, freq_u),
                )
                r = cur.fetchone()
                if r and r[0] is not None:
                    return round(float(r[0]), 2)
                cur.execute(
                    "SELECT per_mcd FROM stock_tushare.etf_rt_min "
                    "WHERE ts_code=%s AND freq=%s AND DATE(trade_time)=CURDATE() "
                    "ORDER BY trade_time DESC LIMIT 1",
                    (ts_code, freq_u),
                )
                r = cur.fetchone()
                if r and r[0] is not None:
                    return round(float(r[0]), 2)
                cur.execute(
                    "SELECT per_mcd FROM stock_tushare.etf_stk_mins_ma "
                    "WHERE ts_code=%s AND freq=%s "
                    "ORDER BY trade_time DESC LIMIT 1",
                    (ts_code, freq_u.lower()),
                )
                r = cur.fetchone()
                if r and r[0] is not None:
                    return round(float(r[0]), 2)
                return None
            if freq_u == "D":
                cur.execute(
                    "SELECT per_mcd FROM stock_tushare.etf_bias_realtime "
                    "WHERE ts_code=%s AND freq='D' "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (ts_code,),
                )
                r = cur.fetchone()
                if r and r[0] is not None:
                    return round(float(r[0]), 2)
                cur.execute(
                    "SELECT per_mcd FROM stock_tushare.etf_daily "
                    "WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1",
                    (ts_code,),
                )
                r = cur.fetchone()
                if r and r[0] is not None:
                    return round(float(r[0]), 2)
                return None
    except Exception as e:
        logger.debug("读取最新 per_mcd 失败 ts_code=%s freq=%s: %s", ts_code, freq_rt, e)
        return None
    return None


def _fetch_rt_k_etf_quotes(ts_codes: list) -> dict:
    """从 stock_wind.rt_k 批量取各 ts_code 当日最新一根 OHLCV（amount 元→亿 etf_amount_yi），并计算与 rt_idx_k 一致的 Pct/Gap/Intra/Amp（%）。"""
    if not ts_codes:
        return {}
    out: dict = {}
    conn = None
    try:
        conn = pymysql.connect(**DB_CONFIG)
    except Exception as e:
        logger.warning("MA 信号表：rt_k 行情连接 stock_wind 失败: %s", e)
        return {}
    try:
        placeholders = ",".join(["%s"] * len(ts_codes))
        # 与 SQL_RT_K_FRESH_WHERE_RT 同日范围 + 每代码 MAX(trade_time) 最新一行
        sql = (
            "SELECT rt.ts_code, rt.open, rt.close, rt.high, rt.low, rt.pre_close, rt.amount, rt.trade_time "
            "FROM stock_wind.rt_k rt "
            "INNER JOIN ( "
            "  SELECT ts_code, MAX(trade_time) AS mt FROM stock_wind.rt_k "
            "  WHERE ts_code IN (" + placeholders + ") "
            "    AND trade_time IS NOT NULL "
            "    AND trade_time >= DATE(" + SQL_RT_K_MAX_TRADE_TIME_SUBQUERY + ") "
            "    AND trade_time < DATE(" + SQL_RT_K_MAX_TRADE_TIME_SUBQUERY + ") + INTERVAL 1 DAY "
            "  GROUP BY ts_code "
            ") x ON rt.ts_code = x.ts_code AND rt.trade_time = x.mt "
            "WHERE rt.ts_code IN (" + placeholders + ")"
        )
        params = list(ts_codes) + list(ts_codes)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            raw = cur.fetchall()
        for row in raw:
            ts_code, open_p, close, high_p, low_p, pre_close, amount_raw, trade_time = (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
                row[7],
            )
            try:
                pre = float(pre_close) if pre_close is not None else None
                op = float(open_p) if open_p is not None else None
                cl = float(close) if close is not None else None
                hi = float(high_p) if high_p is not None else None
                lo = float(low_p) if low_p is not None else None
                amt = float(amount_raw) if amount_raw is not None else None
            except (TypeError, ValueError):
                out[ts_code] = {
                    "etf_last": None,
                    "etf_high": None,
                    "etf_low": None,
                    "etf_amount_yi": None,
                    "etf_pct": None,
                    "etf_gap": None,
                    "etf_intra": None,
                    "etf_amp": None,
                    "etf_rt_date": None,
                    "etf_observed_at": None,
                }
                continue
            etf_amount_yi = round(amt / 1e8, 2) if amt is not None else None
            idx_pct_t = (cl - pre) / pre * 100.0 if (pre is not None and pre != 0 and cl is not None) else None
            idx_gap_t = (op - pre) / pre * 100.0 if (pre is not None and pre != 0 and op is not None) else None
            idx_intra_t = (cl - op) / op * 100.0 if (op is not None and op != 0 and cl is not None) else None
            idx_amp_t = (
                (hi - lo) / pre * 100.0
                if (pre is not None and pre != 0 and hi is not None and lo is not None)
                else None
            )
            rt_d = None
            rt_observed_at = None
            if trade_time is not None:
                try:
                    rt_d = trade_time.date() if hasattr(trade_time, "date") else None
                    rt_observed_at = (
                        trade_time.strftime("%Y-%m-%d %H:%M:%S")
                        if hasattr(trade_time, "strftime")
                        else str(trade_time)
                    )
                except (AttributeError, TypeError, ValueError):
                    rt_d = None
                    rt_observed_at = None
            out[ts_code] = {
                "etf_last": round(cl, 4) if cl is not None else None,
                "etf_high": round(hi, 4) if hi is not None else None,
                "etf_low": round(lo, 4) if lo is not None else None,
                "etf_amount_yi": etf_amount_yi,
                "etf_pct": round(idx_pct_t, 4) if idx_pct_t is not None else None,
                "etf_gap": round(idx_gap_t, 4) if idx_gap_t is not None else None,
                "etf_intra": round(idx_intra_t, 4) if idx_intra_t is not None else None,
                "etf_amp": round(idx_amp_t, 4) if idx_amp_t is not None else None,
                "etf_rt_date": rt_d,
                "etf_observed_at": rt_observed_at,
            }
    except Exception as e:
        logger.warning("MA 信号表：读取 rt_k 行情失败: %s", e)
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


_MA_REALTIME_FIELDS = (
    "etf_last",
    "etf_high",
    "etf_low",
    "etf_amount_yi",
    "etf_pct",
    "etf_gap",
    "etf_intra",
    "etf_amp",
    "etf_rt_date",
    "etf_observed_at",
)
_MA_INTRADAY_RANGE_FIELDS = (
    "range_pct_t5",
    "range_pct_t20",
    "range_pct_t60",
    "range_pct_t120",
)


def _overlay_ma_signal_realtime_rows(
    cached_rows: list,
    rt_quotes: dict,
) -> tuple[list, str | None]:
    """用轻量 rt_k 快照覆盖 MA 慢缓存中的实时列，不修改原缓存。

    60MIN/日线方向和聚集度保留慢任务结果；T-5/20/60/120 根据缓存帧的
    期末价反推出同一基期，再用最新价重算。T-1 是最近完整日线收益，不随
    盘中价格改写。
    """
    rows_out: list = []
    observed_values: list[str] = []
    for cached in cached_rows or []:
        row = dict(cached or {})
        ts_code = str(row.get("ts_code") or "")
        quote = (rt_quotes or {}).get(ts_code) or {}
        old_last = row.get("etf_last")
        new_last = quote.get("etf_last")
        try:
            old_last_f = float(old_last) if old_last is not None else None
            new_last_f = float(new_last) if new_last is not None else None
        except (TypeError, ValueError):
            old_last_f = None
            new_last_f = None
        if old_last_f and old_last_f > 0 and new_last_f and new_last_f > 0:
            for field in _MA_INTRADAY_RANGE_FIELDS:
                old_pct = row.get(field)
                try:
                    old_pct_f = float(old_pct) if old_pct is not None else None
                except (TypeError, ValueError):
                    old_pct_f = None
                if old_pct_f is None or (1.0 + old_pct_f / 100.0) <= 0:
                    continue
                base_close = old_last_f / (1.0 + old_pct_f / 100.0)
                if base_close > 0:
                    row[field] = round((new_last_f / base_close - 1.0) * 100.0, 2)
        for field in _MA_REALTIME_FIELDS:
            if quote.get(field) is not None:
                row[field] = quote[field]
        observed_at = quote.get("etf_observed_at")
        if observed_at:
            observed_values.append(str(observed_at))
        rows_out.append(row)
    return rows_out, (max(observed_values) if observed_values else None)


def _wind_pct_from_prior_desc_rows(
    prior_rows: list | tuple,
    n_trading_days: int,
    end_close: float,
) -> float | None:
    """Wind 区间涨跌幅核心：prior_rows 为 trade_date 降序（新→旧），列 (date, cq)。"""
    if n_trading_days < 2 or end_close is None or end_close <= 0:
        return None
    if not prior_rows or len(prior_rows) < n_trading_days:
        return None
    try:
        base_close = float(prior_rows[n_trading_days - 1][1])
    except (TypeError, ValueError, IndexError):
        return None
    if base_close is None or base_close <= 0:
        return None
    return round((end_close / base_close - 1.0) * 100.0, 2)


def _wind_interval_range_pct(
    conn,
    ts_code: str,
    end_trade_date: date,
    end_close: float,
    n_trading_days: int,
    *,
    daily_table: str = "stock_tushare.etf_daily",
    cq_select: str = "COALESCE(close_qfq, close)",
) -> float | None:
    """Wind 区间涨跌幅（%）：(期末价 / 起始日前一交易日收盘 - 1) * 100。

    与 Wind「区间涨跌幅」一致：窗口为含首尾共 N 个交易日（例 N=5 对应 03-30→04-03），
    分母为「起始日」的前一交易日收盘（非起始日本身）。期末若在日线表未落库则用 rt_k/rt_idx_k 价。
    daily_table：etf_daily 或 index_daily（全限定名）。index_daily 可设 cq_select=\"close\"（无 close_qfq 时）。
    """
    if n_trading_days < 2 or end_close is None or end_close <= 0:
        return None
    lim = max(130, n_trading_days + 5)
    sql = (
        f"SELECT trade_date, {cq_select} AS cq "
        f"FROM {daily_table} "
        "WHERE ts_code = %s AND trade_date < %s "
        "AND close IS NOT NULL AND close > 0 "
        "ORDER BY trade_date DESC LIMIT %s"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (ts_code, end_trade_date, lim))
            prior = cur.fetchall()
    except Exception as e:
        logger.warning("MA 信号表：Wind 区间基期读取失败 %s: %s", ts_code, e)
        return None
    return _wind_pct_from_prior_desc_rows(prior, n_trading_days, end_close)


def _read_etf_daily_interval_range_pct(conn, ts_code: str, rt_quote: dict | None) -> dict:
    """MA 表「区间涨跌幅」。

    T-1：etf_daily 最新完整日相对前一交易日（cq[0]/cq[1]-1）；仅 1 根时回退 pct_chg 或 pre/close。
    T-5 / T-20 / T-60 / T-120：Wind 算法——含首尾共 N 日窗口，(期末 / 起始日前一交易日收盘 - 1) * 100；
    期末价优先用 stock_wind.rt_k 当日最新价（与 etf_daily 未落当日日线时拼接）；基期与历史段仅用 etf_daily。
    """
    empty = {
        'range_pct_t1': None,
        'range_pct_t5': None,
        'range_pct_t20': None,
        'range_pct_t60': None,
        'range_pct_t120': None,
    }
    sql = (
        "SELECT trade_date, pct_chg, pre_close, close, COALESCE(close_qfq, close) AS cq "
        "FROM stock_tushare.etf_daily "
        "WHERE ts_code = %s AND close IS NOT NULL AND close > 0 "
        "ORDER BY trade_date DESC LIMIT %s"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (ts_code, 125))
            rows = cur.fetchall()
    except Exception as e:
        logger.warning("MA 信号表：区间涨跌幅读取失败 %s: %s", ts_code, e)
        return empty
    if not rows:
        return empty

    def _cq_at(i: int):
        if i < 0 or i >= len(rows):
            return None
        v = rows[i][4]
        try:
            f = float(v) if v is not None else None
            return f if f is not None and f > 0 else None
        except (TypeError, ValueError):
            return None

    def _pct_ratio(off: int) -> float | None:
        """T-1 用：cq[0] / cq[off] - 1，off>=1。"""
        c0 = _cq_at(0)
        ck = _cq_at(off)
        if c0 is None or ck is None or ck == 0:
            return None
        return round((c0 / ck - 1.0) * 100.0, 2)

    out = dict(empty)
    if len(rows) >= 2:
        out['range_pct_t1'] = _pct_ratio(1)
    else:
        pc, pre, cl = rows[0][1], rows[0][2], rows[0][3]
        if pc is not None:
            try:
                out['range_pct_t1'] = round(float(pc), 2)
            except (TypeError, ValueError):
                pass
        if out['range_pct_t1'] is None and pre is not None and cl is not None:
            try:
                pf, cf = float(pre), float(cl)
                if pf != 0:
                    out['range_pct_t1'] = round((cf - pf) / pf * 100.0, 2)
            except (TypeError, ValueError):
                pass

    q = rt_quote or {}
    rt_last = q.get('etf_last')
    rt_d = q.get('etf_rt_date')
    end_close: float | None = None
    end_trade_date: date | None = None
    try:
        if rt_last is not None and rt_d is not None:
            end_close = float(rt_last)
            if isinstance(rt_d, datetime):
                end_trade_date = rt_d.date()
            elif isinstance(rt_d, date):
                end_trade_date = rt_d
    except (TypeError, ValueError):
        end_close = None
        end_trade_date = None
    if end_close is None or end_trade_date is None:
        try:
            td = rows[0][0]
            c0 = _cq_at(0)
            if td is not None and c0 is not None:
                if isinstance(td, datetime):
                    end_trade_date = td.date()
                elif isinstance(td, date):
                    end_trade_date = td
                end_close = float(c0)
        except (TypeError, ValueError, Exception) as e:
            logger.debug("MA 信号表：区间期末回退日线 %s: %s", ts_code, e)

    if end_close is not None and end_trade_date is not None:
        lim_prior = 130
        prior_sql = (
            "SELECT trade_date, COALESCE(close_qfq, close) AS cq FROM stock_tushare.etf_daily "
            "WHERE ts_code = %s AND trade_date < %s "
            "AND close IS NOT NULL AND close > 0 "
            "ORDER BY trade_date DESC LIMIT %s"
        )
        try:
            with conn.cursor() as cur:
                cur.execute(prior_sql, (ts_code, end_trade_date, lim_prior))
                prior_rows = cur.fetchall() or []
        except Exception as e:
            logger.warning("MA 信号表：ETF Wind 区间基期批量读取失败 %s: %s", ts_code, e)
            prior_rows = []
        out['range_pct_t5'] = _wind_pct_from_prior_desc_rows(prior_rows, 5, end_close)
        out['range_pct_t20'] = _wind_pct_from_prior_desc_rows(prior_rows, 20, end_close)
        out['range_pct_t60'] = _wind_pct_from_prior_desc_rows(prior_rows, 60, end_close)
        out['range_pct_t120'] = _wind_pct_from_prior_desc_rows(prior_rows, 120, end_close)
    return out


def _parse_rt_idx_trade_time_to_date(trade_time) -> date | None:
    """rt_idx_k.trade_time 可能为 str/datetime/date，统一为交易日 date。"""
    if trade_time is None:
        return None
    if isinstance(trade_time, datetime):
        return trade_time.date()
    if isinstance(trade_time, date):
        return trade_time
    s = str(trade_time).strip()
    if len(s) >= 10:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


def _fetch_rt_idx_k_quotes(ts_codes: list, conn=None) -> dict:
    """从 stock_tushare.rt_idx_k 读取各指数最新价（主键 ts_code 一行），供涨跌幅%%表区间拼接期末价。
    若传入 conn（已由调用方连接 stock_tushare），则不再新建连接，避免 /api/rt_median 刷新时双连接。"""
    if not ts_codes:
        return {}
    uniq = list(dict.fromkeys(ts_codes))
    own_conn = conn is None
    if conn is None:
        try:
            conn = pymysql.connect(**DB_CONFIG_TUSHARE)
        except Exception as e:
            logger.warning("涨跌幅表：rt_idx_k 连接失败: %s", e)
            return {}
    out: dict = {}
    try:
        placeholders = ",".join(["%s"] * len(uniq))
        sql = (
            "SELECT ts_code, open, close, pre_close, high, low, amount, trade_time "
            f"FROM stock_tushare.rt_idx_k WHERE ts_code IN ({placeholders})"
        )
        with conn.cursor() as cur:
            cur.execute(sql, uniq)
            raw = cur.fetchall()
        for row in raw:
            ts_code, _open_p, close, _pre_close, _high_p, _low_p, _amount_raw, trade_time = row
            try:
                cl = float(close) if close is not None else None
            except (TypeError, ValueError):
                out[ts_code] = {
                    "etf_last": None,
                    "etf_rt_date": None,
                }
                continue
            rt_d = _parse_rt_idx_trade_time_to_date(trade_time)
            out[ts_code] = {
                "etf_last": round(cl, 4) if cl is not None else None,
                "etf_rt_date": rt_d,
            }
    except Exception as e:
        logger.warning("涨跌幅表：读取 rt_idx_k 失败: %s", e)
        return {}
    finally:
        if own_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


def _read_index_daily_interval_range_pct(
    conn,
    ts_code: str,
    rt_quote: dict | None,
    *,
    splice_rt: bool = True,
) -> dict:
    """涨跌幅%%表「区间涨跌幅」：历史 index_daily.close；盘中期末用 rt_idx_k（含 TQ 的 932000/899050）。"""
    empty = {
        "range_pct_t1": None,
        "range_pct_t5": None,
        "range_pct_t20": None,
        "range_pct_t60": None,
        "range_pct_t120": None,
    }
    sql = (
        "SELECT trade_date, pct_chg, pre_close, close, close AS cq "
        "FROM stock_tushare.index_daily "
        "WHERE ts_code = %s AND close IS NOT NULL AND close > 0 "
        "ORDER BY trade_date DESC LIMIT %s"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (ts_code, 125))
            rows = cur.fetchall()
    except Exception as e:
        logger.warning("涨跌幅表：index_daily 区间读取失败 %s: %s", ts_code, e)
        return empty
    if not rows:
        return empty

    def _cq_at(i: int):
        if i < 0 or i >= len(rows):
            return None
        v = rows[i][4]
        try:
            f = float(v) if v is not None else None
            return f if f is not None and f > 0 else None
        except (TypeError, ValueError):
            return None

    def _pct_ratio(off: int) -> float | None:
        c0 = _cq_at(0)
        ck = _cq_at(off)
        if c0 is None or ck is None or ck == 0:
            return None
        return round((c0 / ck - 1.0) * 100.0, 2)

    out = dict(empty)
    if len(rows) >= 2:
        out["range_pct_t1"] = _pct_ratio(1)
    else:
        pc, pre, cl = rows[0][1], rows[0][2], rows[0][3]
        if pc is not None:
            try:
                out["range_pct_t1"] = round(float(pc), 2)
            except (TypeError, ValueError):
                pass
        if out["range_pct_t1"] is None and pre is not None and cl is not None:
            try:
                pf, cf = float(pre), float(cl)
                if pf != 0:
                    out["range_pct_t1"] = round((cf - pf) / pf * 100.0, 2)
            except (TypeError, ValueError):
                pass

    q = (rt_quote or {}) if splice_rt else {}
    rt_last = q.get("etf_last")
    rt_d = q.get("etf_rt_date")
    end_close: float | None = None
    end_trade_date: date | None = None
    try:
        if rt_last is not None and rt_d is not None:
            end_close = float(rt_last)
            if isinstance(rt_d, datetime):
                end_trade_date = rt_d.date()
            elif isinstance(rt_d, date):
                end_trade_date = rt_d
    except (TypeError, ValueError):
        end_close = None
        end_trade_date = None
    if end_close is None or end_trade_date is None:
        try:
            td = rows[0][0]
            c0 = _cq_at(0)
            if td is not None and c0 is not None:
                if isinstance(td, datetime):
                    end_trade_date = td.date()
                elif isinstance(td, date):
                    end_trade_date = td
                end_close = float(c0)
        except (TypeError, ValueError, Exception) as e:
            logger.debug("涨跌幅表：区间期末回退日线 %s: %s", ts_code, e)

    idx_daily = "stock_tushare.index_daily"
    if end_close is not None and end_trade_date is not None:
        # 一次取够 T-120 基期链，避免 4 次重复 SQL（原先每指数 5 轮询易超时、单行异常会拖垮整表）
        lim_prior = 130
        prior_sql = (
            f"SELECT trade_date, close AS cq FROM {idx_daily} "
            "WHERE ts_code = %s AND trade_date < %s "
            "AND close IS NOT NULL AND close > 0 "
            "ORDER BY trade_date DESC LIMIT %s"
        )
        try:
            with conn.cursor() as cur:
                cur.execute(prior_sql, (ts_code, end_trade_date, lim_prior))
                prior_rows = cur.fetchall() or []
        except Exception as e:
            logger.warning("涨跌幅表：指数 Wind 区间基期批量读取失败 %s: %s", ts_code, e)
            prior_rows = []
        out["range_pct_t5"] = _wind_pct_from_prior_desc_rows(prior_rows, 5, end_close)
        out["range_pct_t20"] = _wind_pct_from_prior_desc_rows(prior_rows, 20, end_close)
        out["range_pct_t60"] = _wind_pct_from_prior_desc_rows(prior_rows, 60, end_close)
        out["range_pct_t120"] = _wind_pct_from_prior_desc_rows(prior_rows, 120, end_close)
    return out


def read_etf_ma_signal_table_rows() -> list:
    """九只 ETF ×（rt_k 基本行情 + Wind 式区间涨跌幅 + 60MIN/日线 MA 信号 + 15MIN/60MIN/日线聚集度 per_mcd）。"""
    for attempt in range(2):
        conn = None
        try:
            conn = pymysql.connect(**DB_CONFIG_TUSHARE)
        except Exception as e:
            logger.exception("MA 信号表：连接 stock_tushare 失败: %s", e)
            return []
        try:
            rt_quotes = _fetch_rt_k_etf_quotes(list(ETF_MA_SIGNAL_ORDER))
            rows_out = []
            for ts_code in ETF_MA_SIGNAL_ORDER:
                try:
                    row = {'ts_code': ts_code}
                    df_15 = _load_minute_close_mcd_style(
                        conn, ts_code, "15MIN", MA_SIGNAL_FETCH_BARS_15MIN
                    )
                    if df_15.empty:
                        df_15 = _load_merged_minute_close_df(
                            conn, ts_code, "15MIN", MA_SIGNAL_FETCH_BARS_15MIN
                        )
                    df_60 = _load_minute_close_mcd_style(
                        conn, ts_code, "60MIN", MA_SIGNAL_FETCH_BARS_60MIN
                    )
                    if df_60.empty:
                        df_60 = _load_merged_minute_close_df(
                            conn, ts_code, "60MIN", MA_SIGNAL_FETCH_BARS_60MIN
                        )
                    df_d = _load_daily_close_df(conn, ts_code, MA_SIGNAL_FETCH_BARS_D)
                    if len(df_60) > 0:
                        sigs_60 = _compute_ma_ref_signals(df_60['close'], ETF_MA_SIGNAL_PERIODS)
                        row['60MIN_20'] = _last_signal_token(sigs_60[20])
                        row['60MIN_60'] = _last_signal_token(sigs_60[60])
                    else:
                        row['60MIN_20'] = row['60MIN_60'] = 'none'
                    if len(df_d) > 0:
                        sigs_d = _compute_ma_ref_signals(df_d['close'], ETF_MA_SIGNAL_PERIODS)
                        row['D_20'] = _last_signal_token(sigs_d[20])
                        row['D_60'] = _last_signal_token(sigs_d[60])
                    else:
                        row['D_20'] = row['D_60'] = 'none'
                    row['PMCD_15'] = _read_latest_mcd_per(conn, ts_code, '15MIN')
                    row['PMCD_60'] = _read_latest_mcd_per(conn, ts_code, '60MIN')
                    row['PMCD_D'] = _read_latest_mcd_per(conn, ts_code, 'D')
                    # 兜底：若库中暂无 per_mcd，再回退到现算（避免全空）
                    if row['PMCD_15'] is None and len(df_15) > 0:
                        row['PMCD_15'] = _calc_mcd_per_from_qfq_close(df_15['close'], PER_MCD_WINDOW_15MIN)
                    if row['PMCD_60'] is None and len(df_60) > 0:
                        row['PMCD_60'] = _calc_mcd_per_from_qfq_close(df_60['close'], PER_MCD_WINDOW_60MIN)
                    if row['PMCD_D'] is None and len(df_d) > 0:
                        row['PMCD_D'] = _calc_mcd_per_from_qfq_close(df_d['close'], PER_MCD_WINDOW_D)
                    q = rt_quotes.get(ts_code) or {}
                    row['etf_last'] = q.get('etf_last')
                    row['etf_high'] = q.get('etf_high')
                    row['etf_low'] = q.get('etf_low')
                    row['etf_amount_yi'] = q.get('etf_amount_yi')
                    row['etf_pct'] = q.get('etf_pct')
                    row['etf_gap'] = q.get('etf_gap')
                    row['etf_intra'] = q.get('etf_intra')
                    row['etf_amp'] = q.get('etf_amp')
                    row.update(_read_etf_daily_interval_range_pct(conn, ts_code, q))
                    rows_out.append(row)
                except Exception as row_e:
                    logger.warning("MA 信号表：单行失败 ts=%s: %s", ts_code, row_e)
                    qf = rt_quotes.get(ts_code) or {}
                    rows_out.append(
                        {
                            'ts_code': ts_code,
                            '60MIN_20': 'none',
                            '60MIN_60': 'none',
                            'D_20': 'none',
                            'D_60': 'none',
                            'PMCD_15': None,
                            'PMCD_60': None,
                            'PMCD_D': None,
                            'etf_last': qf.get('etf_last'),
                            'etf_high': qf.get('etf_high'),
                            'etf_low': qf.get('etf_low'),
                            'etf_amount_yi': qf.get('etf_amount_yi'),
                            'etf_pct': qf.get('etf_pct'),
                            'etf_gap': qf.get('etf_gap'),
                            'etf_intra': qf.get('etf_intra'),
                            'etf_amp': qf.get('etf_amp'),
                            'range_pct_t1': None,
                            'range_pct_t5': None,
                            'range_pct_t20': None,
                            'range_pct_t60': None,
                            'range_pct_t120': None,
                        }
                    )
            return rows_out
        except pymysql.OperationalError as e:
            # 2013：查询期间连接丢失/读超时；换新连接重跑一轮，避免单 ETF 失败后整表空白
            if getattr(e, "args", None) and e.args[0] == 2013 and attempt == 0:
                logger.warning("MA 信号表：查询超时或连接丢失，重试一次: %s", e)
                continue
            logger.exception("MA 信号表：查询失败: %s", e)
            return []
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return []


def _expanding_std_no_lookahead(series: pd.Series, min_periods: int) -> pd.Series:
    """截至 t-1 的 expanding std（不含 t），避免前视。"""
    return series.expanding(min_periods=min_periods).std().shift(1)


def _calc_mcd_pct_and_per(close_px: pd.Series, per_window: int) -> tuple[pd.Series, pd.Series]:
    """基于 qfq close 计算 mcd_pct 与 per_mcd。"""
    close_s = pd.to_numeric(close_px, errors="coerce")
    ma20 = close_s.rolling(window=20, min_periods=20).mean()
    ema20 = close_s.ewm(span=20, adjust=False).mean()
    ma60 = close_s.rolling(window=60, min_periods=60).mean()
    ema60 = close_s.ewm(span=60, adjust=False).mean()
    ma120 = close_s.rolling(window=120, min_periods=120).mean()
    ema120 = close_s.ewm(span=120, adjust=False).mean()
    six = pd.concat([ma20, ema20, ma60, ema60, ma120, ema120], axis=1)
    std = six.std(axis=1, ddof=0)
    valid_denom = close_s.notna() & (close_s > 0)
    all_ok = six.notna().all(axis=1)
    mcd_pct = ((std / close_s) * 100.0).where(all_ok & valid_denom)
    per_mcd = _rolling_percentile_bias(mcd_pct, per_window)
    return mcd_pct, per_mcd


def get_etf_bias_series():
    """返回指定 (ts_code, freq, column) 最近若干根 K 的 close、per_bias（及同图 per_bias_ema）与 2σ 标记。
    per 的滚动百分位窗口为 PER_BIAS_WINDOW(1250)：分钟/日线均先取不少于 max(limit,1250) 根再算，再截取尾部 limit 点返回。
    15MIN/60MIN：优先单表 etf_stk_mins_ma+etf_rt_min+虚拟K 读库内 per_bias*（与表一致）；缺列再回退 ma JOIN stk_mins 全段 rolling。
    日线：优先 etf_daily.per_bias10ma；缺列回退仅 close 后 rolling。raw bias 仍由前复权 close 向量 rolling 计算（2σ 口径不变）。
    参数: ts_code, freq=15MIN|60MIN|D, column=10MA|20MA（D 时仅 10MA）, limit 可选（默认 300；上限按周期 15MIN≤5000、60MIN≤2500、D≤1300）。
    2σ：主列 raw bias 的 expanding std（截至 t-1）无前视。
    同 (ts_code,freq,column,limit) 4s 内命中进程内缓存，避免重复 DB + 百分位。"""
    t0 = time.perf_counter()
    try:
        ts_code = (request.args.get('ts_code') or '').strip()
        freq_param = (request.args.get('freq') or '').strip().upper()
        column = (request.args.get('column') or '').strip()
        if not ts_code or freq_param not in ('15MIN', '60MIN', 'D') or column not in ETF_BIAS_COLUMN_MAP:
            return jsonify({'error': '缺少或无效的 ts_code/freq/column'}), 400
        if freq_param == 'D' and column != '10MA':
            return jsonify({'error': '日线 D 仅支持 column=10MA'}), 400
        series_tail = _resolve_etf_bias_mcd_series_limit(freq_param, request.args.get('limit'), BIAS_SERIES_LIMIT)
        per_col_ema = ETF_BIAS_PAIR_EMA_COL.get(column) if freq_param != 'D' else None
        cache_key = (ts_code, freq_param, column, int(series_tail))
        now_ck = time.time()
        with _cache_lock:
            hit = _etf_bias_series_cache.get(cache_key)
            if hit is not None and now_ck < hit[0]:
                _etf_bias_series_cache.move_to_end(cache_key)
                return jsonify(hit[1])
        try:
            conn = pymysql.connect(**DB_CONFIG_TUSHARE)
        except Exception as e:
            logger.exception("连接 stock_tushare 失败: %s", e)
            return jsonify({'error': '数据库连接失败'}), 500
        df = None
        # 分钟弹窗优先走“库内 per 列”快路径：只取展示窗口+预热，避免每次强制拉 PER_BIAS_WINDOW(1250+)。
        # 若后续发现缺列/异常会由 _bias_per_from_db_or_roll 回退计算，不改变口径语义。
        _bias_fetch_bars = max(series_tail + MCD_WARMUP_BARS, 360) if freq_param != 'D' else max(series_tail, PER_BIAS_WINDOW)
        try:
            if freq_param == 'D':
                df = _load_daily_bias_modal_df(conn, ts_code, _bias_fetch_bars)
            else:
                df = _load_minute_bias_series_modal_df(conn, ts_code, freq_param, _bias_fetch_bars)
                if df is None or df.empty:
                    df = _load_merged_minute_close_df(conn, ts_code, freq_param, _bias_fetch_bars)
        finally:
            conn.close()
        if df is None or df.empty:
            out = {
                'times': [], 'close': [], 'per_bias': [], 'over_2sigma': [],
                'ts_code': ts_code, 'freq': freq_param, 'column': column,
            }
            if per_col_ema:
                out['per_bias_ema'] = []
            return jsonify(out)

        # bias：前复权 close 向量 rolling（与 daemon 公式一致）；per：库内列完整则跳过滚动百分位重算
        close_px = pd.to_numeric(df["close"], errors="coerce")
        ma10 = close_px.rolling(window=10, min_periods=10).mean()
        bias10ma = (close_px - ma10) / ma10 * 100.0

        if freq_param == "D":
            per_bias10ma = _bias_per_from_db_or_roll(
                bias10ma, df["per_bias10ma"] if "per_bias10ma" in df.columns else None
            )
            bias_main = bias10ma
            per_main = per_bias10ma
            std_t = _expanding_std_no_lookahead(bias_main, BIAS_SERIES_MIN_PERIODS_STD)
            over_2sigma_full = (
                (std_t.notna() & (std_t > 0) & (bias_main.abs() >= 2 * std_t))
                .astype(int)
                .tolist()
            )
            tail_n = min(series_tail, len(df))
            sl = slice(-tail_n, None)
            df_tail = df.iloc[sl]
            per_main_t = per_main.iloc[sl]
            over_2sigma = over_2sigma_full[sl]
            times = [t.isoformat() if hasattr(t, "isoformat") else str(t) for t in df_tail["trade_time"]]
            payload = {
                "times": times,
                "close": _series_to_json_floats(df_tail["close"]),
                "per_bias": _series_to_json_floats(per_main_t),
                "over_2sigma": over_2sigma,
                "ts_code": ts_code,
                "freq": freq_param,
                "column": column,
            }
        else:
            ma20 = close_px.rolling(window=20, min_periods=20).mean()
            ema20 = close_px.ewm(span=20, adjust=False).mean()
            bias20ma = (close_px - ma20) / ma20 * 100.0
            bias20ema = (close_px - ema20) / ema20 * 100.0

            pc10 = df["per_bias10ma"] if "per_bias10ma" in df.columns else None
            pc20m = df["per_bias20ma"] if "per_bias20ma" in df.columns else None
            pc20e = df["per_bias20ema"] if "per_bias20ema" in df.columns else None
            per_bias10ma = _bias_per_from_db_or_roll(bias10ma, pc10)
            per_bias20ma = _bias_per_from_db_or_roll(bias20ma, pc20m)
            per_bias20ema = _bias_per_from_db_or_roll(bias20ema, pc20e)

            if column == "10MA":
                bias_main = bias10ma
                per_main = per_bias10ma
            elif column == "20MA":
                bias_main = bias20ma
                per_main = per_bias20ma
            else:
                bias_main = bias20ema
                per_main = per_bias20ema

            std_t = _expanding_std_no_lookahead(bias_main, BIAS_SERIES_MIN_PERIODS_STD)
            over_2sigma_full = (
                (std_t.notna() & (std_t > 0) & (bias_main.abs() >= 2 * std_t))
                .astype(int)
                .tolist()
            )

            tail_n = min(series_tail, len(df))
            sl = slice(-tail_n, None)
            df_tail = df.iloc[sl]
            per_main_t = per_main.iloc[sl]
            over_2sigma = over_2sigma_full[sl]

            times_m = [t.isoformat() if hasattr(t, "isoformat") else str(t) for t in df_tail["trade_time"]]

            payload = {
                "times": times_m,
                "close": _series_to_json_floats(df_tail["close"]),
                "per_bias": _series_to_json_floats(per_main_t),
                "over_2sigma": over_2sigma,
                "ts_code": ts_code,
                "freq": freq_param,
                "column": column,
            }
            if per_col_ema:
                payload["per_bias_ema"] = _series_to_json_floats(per_bias20ema.iloc[sl])

        with _cache_lock:
            while len(_etf_bias_series_cache) >= ETF_BIAS_SERIES_CACHE_MAX:
                _etf_bias_series_cache.popitem(last=False)
            _etf_bias_series_cache[cache_key] = (time.time() + ETF_BIAS_SERIES_CACHE_TTL, payload)
            _etf_bias_series_cache.move_to_end(cache_key)
        logger.info(
            "api_etf_bias_series ok ts_code=%s freq=%s column=%s limit=%s rows=%s elapsed=%.3fs",
            ts_code, freq_param, column, series_tail, len(payload.get("times") or []), time.perf_counter() - t0
        )
        return jsonify(payload)
    except Exception as e:
        logger.exception("etf_bias_series 失败: %s", e)
        return jsonify({'error': str(e)}), 500


def get_etf_mcd_series():
    """返回指定 (ts_code,freq) 最近聚集度 per_mcd 序列（优先读已落库 per，保证与数据表一致）。
    参数 limit 可选，默认 300；上限按周期 15MIN≤5000、60MIN≤2500、D≤1300。"""
    t0 = time.perf_counter()
    try:
        ts_code = (request.args.get("ts_code") or "").strip()
        freq_param = (request.args.get("freq") or "").strip().upper()
        if not ts_code or freq_param not in ("15MIN", "60MIN", "D"):
            return jsonify({"error": "缺少或无效的 ts_code/freq（仅支持 15MIN、60MIN、D）"}), 400

        series_tail = _resolve_etf_bias_mcd_series_limit(freq_param, request.args.get("limit"), BIAS_SERIES_LIMIT)
        cache_key = (ts_code, freq_param, int(series_tail))
        hit = _etf_mcd_series_cache_get(cache_key)
        if hit is not None:
            return jsonify(hit)
        # MCD 弹窗直接消费库内 per_mcd，不需要按 PER_BIAS_WINDOW 强制扩大取数；保留最小预热用于 2σ 稳定显示。
        fetch_bars = max(series_tail + MCD_WARMUP_BARS, 240)
        try:
            conn = pymysql.connect(**DB_CONFIG_TUSHARE)
        except Exception as e:
            logger.exception("连接 stock_tushare 失败: %s", e)
            return jsonify({"error": "数据库连接失败"}), 500

        try:
            if freq_param == "D":
                sql_daily = (
                    "SELECT trade_date AS trade_time, COALESCE(close_qfq, close) AS close, per_mcd "
                    "FROM stock_tushare.etf_daily "
                    "WHERE ts_code=%s AND close IS NOT NULL AND close>0 "
                    "ORDER BY trade_date DESC LIMIT %s"
                )
                with conn.cursor() as cur:
                    cur.execute(sql_daily, (ts_code, fetch_bars))
                    rows = cur.fetchall()
                rows_asc = list(reversed(rows)) if rows else []
                sql_rt = (
                    "SELECT trade_time, close, per_mcd FROM stock_tushare.etf_bias_realtime "
                    "WHERE ts_code=%s AND freq='D' AND trade_time IS NOT NULL "
                    "AND DATE(trade_time)=CURDATE() LIMIT 1"
                )
                with conn.cursor() as cur:
                    cur.execute(sql_rt, (ts_code,))
                    row_rt = cur.fetchone()
                if row_rt:
                    rows_asc.append(row_rt)
                if not rows_asc:
                    df = pd.DataFrame(columns=["trade_time", "close", "per_mcd"])
                else:
                    df = pd.DataFrame(rows_asc, columns=["trade_time", "close", "per_mcd"])
                    df["trade_time"] = pd.to_datetime(df["trade_time"], errors="coerce")
                    df["close"] = pd.to_numeric(df["close"], errors="coerce")
                    df["per_mcd"] = pd.to_numeric(df["per_mcd"], errors="coerce")
                    df = df.dropna(subset=["trade_time"])
                    df = df.sort_values("trade_time").drop_duplicates(subset=["trade_time"], keep="last")
                    if len(df) > fetch_bars:
                        df = df.iloc[-fetch_bars:].copy()
            else:
                freq_db = freq_param.lower()
                sql_today = (
                    "SELECT trade_time, close, per_mcd FROM stock_tushare.etf_rt_min "
                    "WHERE ts_code=%s AND freq=%s AND DATE(trade_time)=CURDATE() "
                    "AND close IS NOT NULL AND close>0 "
                    "ORDER BY trade_time"
                )
                with conn.cursor() as cur:
                    cur.execute(sql_today, (ts_code, freq_param))
                    rows_today = cur.fetchall()
                need = max(fetch_bars - len(rows_today), 1)
                sql_hist = (
                    "SELECT trade_time, close_qfq AS close, per_mcd FROM stock_tushare.etf_stk_mins_ma "
                    "WHERE ts_code=%s AND freq=%s AND trade_time<CURDATE() "
                    "ORDER BY trade_time DESC LIMIT %s"
                )
                with conn.cursor() as cur:
                    cur.execute(sql_hist, (ts_code, freq_db, need))
                    rows_hist = cur.fetchall()
                rows_asc = list(reversed(rows_hist)) + list(rows_today)
                # 与 BIAS 弹窗一致：允许最新实时虚拟K覆盖同名 trade_time（仅在 fresh 窗口内）
                sql_rt = (
                    "SELECT trade_time, close, per_mcd FROM stock_tushare.etf_bias_realtime "
                    "WHERE ts_code=%s AND freq=%s AND trade_time IS NOT NULL "
                    "AND close IS NOT NULL AND close>0 "
                    "AND updated_at >= DATE_SUB(NOW(), INTERVAL %s SECOND)"
                )
                with conn.cursor() as cur:
                    cur.execute(sql_rt, (ts_code, freq_param, ETF_BIAS_REALTIME_MAX_AGE))
                    row_rt = cur.fetchone()
                if row_rt:
                    rows_asc.append(row_rt)
                if not rows_asc:
                    df = pd.DataFrame(columns=["trade_time", "close", "per_mcd"])
                else:
                    df = pd.DataFrame(rows_asc, columns=["trade_time", "close", "per_mcd"])
                    df["trade_time"] = pd.to_datetime(df["trade_time"], errors="coerce")
                    df["close"] = pd.to_numeric(df["close"], errors="coerce")
                    df["per_mcd"] = pd.to_numeric(df["per_mcd"], errors="coerce")
                    df = df.dropna(subset=["trade_time"])
                    df = df.sort_values("trade_time").drop_duplicates(subset=["trade_time"], keep="last")
                    df = _drop_open_anchor_bar(df, freq_param)
                    if len(df) > fetch_bars:
                        df = df.iloc[-fetch_bars:].copy()
        finally:
            conn.close()

        if df is None or df.empty:
            return jsonify({
                "times": [], "close": [], "per_bias": [], "over_2sigma": [],
                "ts_code": ts_code, "freq": freq_param, "column": "MCD",
            })

        per_mcd = pd.to_numeric(df["per_mcd"], errors="coerce")
        std_t = _expanding_std_no_lookahead(per_mcd, BIAS_SERIES_MIN_PERIODS_STD)
        over_2sigma_full = (
            (std_t.notna() & (std_t > 0) & ((per_mcd - 50.0).abs() >= 2 * std_t))
            .astype(int)
            .tolist()
        )

        tail_n = min(series_tail, len(df))
        sl = slice(-tail_n, None)
        df_tail = df.iloc[sl]
        per_tail = per_mcd.iloc[sl]
        over_2sigma = over_2sigma_full[sl]
        times = [t.isoformat() if hasattr(t, "isoformat") else str(t) for t in df_tail["trade_time"]]

        payload = {
            "times": times,
            "close": _series_to_json_floats(df_tail["close"]),
            "per_bias": _series_to_json_floats(per_tail),
            "over_2sigma": over_2sigma,
            "ts_code": ts_code,
            "freq": freq_param,
            "column": "MCD",
        }
        _etf_mcd_series_cache_put(cache_key, payload)
        logger.info(
            "api_etf_mcd_series ok ts_code=%s freq=%s limit=%s rows=%s elapsed=%.3fs",
            ts_code, freq_param, series_tail, len(payload.get("times") or []), time.perf_counter() - t0
        )
        return jsonify(payload)
    except Exception as e:
        logger.exception("etf_mcd_series 失败: %s", e)
        return jsonify({"error": str(e)}), 500


def get_up_num_ma5_series():
    """超跌序列：marketinfo.up_num_ma5 与 index_daily(399317.SZ).close 按 trade_date 对齐。
    查询参数 limit：默认 300，最大 1300（约 5 年交易日），与融资占比弹窗一致。
    盘中拼接当日：399317.SZ 的 close 取 rt_idx_k；up_num_ma5 用 _read_up_num_ma5_rt（与涨跌家数面板一致）。
    若历史末交易日等于锚定日则覆盖末点，否则在末点后追加，再截断为最多 limit 点。"""
    try:
        series_limit = int(request.args.get('limit', '300'))
    except (TypeError, ValueError):
        series_limit = 300
    series_limit = max(1, min(series_limit, 1300))
    cache_key = ('up_num_ma5_series', int(series_limit))
    hit = popupSeriesCacheGet(cache_key)
    if hit is not None:
        return jsonify(hit)
    hist_rows: list[tuple] = []
    rt_idx_row: tuple | None = None
    try:
        conn_ts = pymysql.connect(**DB_CONFIG_TUSHARE)
    except Exception as e:
        logger.exception("连接 stock_tushare 失败: %s", e)
        return jsonify({'error': '数据库连接失败'}), 500

    try:
        sql = """
        SELECT m.trade_date, m.up_num_ma5, i.close
        FROM stock_tushare.marketinfo m
        INNER JOIN stock_tushare.index_daily i
          ON i.trade_date = m.trade_date
         AND i.ts_code = '399317.SZ'
        WHERE m.up_num_ma5 IS NOT NULL
          AND i.close IS NOT NULL
        ORDER BY m.trade_date DESC
        LIMIT %s
        """
        with conn_ts.cursor() as cur:
            cur.execute(sql, (series_limit,))
            hist_rows = cur.fetchall() or []
        with conn_ts.cursor() as cur:
            cur.execute(
                "SELECT trade_time, close FROM stock_tushare.rt_idx_k WHERE ts_code = %s LIMIT 1",
                ('399317.SZ',),
            )
            rt_idx_row = cur.fetchone()
    except Exception as e:
        logger.exception("up_num_ma5_series 读历史/rt_idx_k 失败: %s", e)
        return jsonify({'error': str(e)}), 500
    finally:
        conn_ts.close()

    rows_asc = list(reversed(hist_rows))
    last_td: date | None = None
    if rows_asc:
        last_td = _sql_date_to_py_date(rows_asc[-1][0])

    times: list[str] = []
    up_num_ma5: list[float | None] = []
    close: list[float | None] = []
    for td, up_v, close_v in rows_asc:
        t = pd.to_datetime(td, errors='coerce')
        if pd.isna(t):
            continue
        times.append(t.isoformat())
        up_num_ma5.append(None if pd.isna(up_v) else float(up_v))
        close.append(None if pd.isna(close_v) else float(close_v))

    anchor_date: date | None = None
    up_rt_str: str | None = None
    try:
        conn_wind = pymysql.connect(**DB_CONFIG)
        try:
            with conn_wind.cursor() as cur:
                cur.execute(
                    "SELECT DATE(MAX(trade_time)) FROM stock_wind.rt_k WHERE trade_time IS NOT NULL"
                )
                r = cur.fetchone()
                if r and r[0] is not None:
                    anchor_date = _sql_date_to_py_date(r[0])
            up_rt_str = _read_up_num_ma5_rt(conn_wind)
        finally:
            conn_wind.close()
    except Exception as e:
        logger.warning("up_num_ma5_series 实时拼接跳过: %s", e)

    def _slice_tail_max() -> None:
        nonlocal times, up_num_ma5, close
        if len(times) > series_limit:
            times = times[-series_limit:]
            up_num_ma5 = up_num_ma5[-series_limit:]
            close = close[-series_limit:]

    if (
        anchor_date is not None
        and up_rt_str is not None
        and rt_idx_row is not None
        and rt_idx_row[1] is not None
    ):
        try:
            rt_close = float(rt_idx_row[1])
            up_rt = float(up_rt_str)
            t_raw = rt_idx_row[0]
            t_dt = pd.to_datetime(t_raw, errors='coerce')
            if pd.isna(t_dt):
                t_dt = pd.Timestamp(anchor_date)
            rt_iso = t_dt.isoformat()

            if last_td == anchor_date:
                if times:
                    times[-1] = rt_iso
                    up_num_ma5[-1] = up_rt
                    close[-1] = rt_close
                else:
                    times.append(rt_iso)
                    up_num_ma5.append(up_rt)
                    close.append(rt_close)
            elif last_td is None or last_td < anchor_date:
                times.append(rt_iso)
                up_num_ma5.append(up_rt)
                close.append(rt_close)
            _slice_tail_max()
        except (TypeError, ValueError) as e:
            logger.warning("up_num_ma5_series 实时点解析失败: %s", e)

    payload = {
        'times': times,
        'up_num_ma5': up_num_ma5,
        'close': close,
        'series_kind': 'up_num_ma5',
        'ts_code': '399317.SZ',
        'limit': series_limit,
    }
    popupSeriesCachePut(cache_key, payload)
    return jsonify(payload)


def get_amount_top5_pct_series():
    """成交额 Top5%_pct 序列：marketinfo.amount_top5_pct 与 399317.SZ close 对齐，并拼接当日实时点。
    - start_date=YYYY-MM-DD：自该日起升序取全量（上限 AMOUNT_TOP5_PCT_MODAL_SERIES_CAP），供弹窗 pan/zoom 浏览全历史。
    - 无 start_date：仅 limit（默认 300，最大 AMOUNT_TOP5_PCT_MODAL_LIMIT_TAIL）取最近 N 个交易日（超跌弹窗同款）。"""
    start_date = _parse_amount_top5_series_start_date(request.args.get('start_date'))
    if start_date is not None:
        try:
            tail_cap = int(request.args.get('limit', str(AMOUNT_TOP5_PCT_MODAL_SERIES_CAP)))
        except (TypeError, ValueError):
            tail_cap = AMOUNT_TOP5_PCT_MODAL_SERIES_CAP
        tail_cap = max(1, min(tail_cap, AMOUNT_TOP5_PCT_MODAL_SERIES_CAP))
        cache_key = ('amount_top5_pct_series', 'from', start_date.isoformat(), int(tail_cap))
    else:
        try:
            tail_cap = int(request.args.get('limit', '300'))
        except (TypeError, ValueError):
            tail_cap = 300
        tail_cap = max(1, min(tail_cap, AMOUNT_TOP5_PCT_MODAL_LIMIT_TAIL))
        cache_key = ('amount_top5_pct_series', 'lim', int(tail_cap))

    hit = popupSeriesCacheGet(cache_key)
    if hit is not None:
        return jsonify(hit)

    hist_sql_asc = """
                SELECT m.trade_date, m.amount_top5_pct, i.close
                FROM stock_tushare.marketinfo m
                INNER JOIN stock_tushare.index_daily i
                  ON i.trade_date = m.trade_date
                 AND i.ts_code = '399317.SZ'
                WHERE m.amount_top5_pct IS NOT NULL
                  AND i.close IS NOT NULL
                  AND m.trade_date >= %s
                ORDER BY m.trade_date ASC
                LIMIT %s
                """
    hist_sql_desc = """
                SELECT m.trade_date, m.amount_top5_pct, i.close
                FROM stock_tushare.marketinfo m
                INNER JOIN stock_tushare.index_daily i
                  ON i.trade_date = m.trade_date
                 AND i.ts_code = '399317.SZ'
                WHERE m.amount_top5_pct IS NOT NULL
                  AND i.close IS NOT NULL
                ORDER BY m.trade_date DESC
                LIMIT %s
                """

    try:
        conn_ts = pymysql.connect(**DB_CONFIG_TUSHARE)
    except Exception as e:
        logger.exception("连接 stock_tushare 失败: %s", e)
        return jsonify({'error': '数据库连接失败'}), 500

    hist_rows: list[tuple] = []
    rt_idx_row: tuple | None = None
    try:
        with conn_ts.cursor() as cur:
            if start_date is not None:
                cur.execute(hist_sql_asc, (start_date, tail_cap))
            else:
                cur.execute(hist_sql_desc, (tail_cap,))
            hist_rows = cur.fetchall() or []
        with conn_ts.cursor() as cur:
            cur.execute(
                "SELECT trade_time, close FROM stock_tushare.rt_idx_k WHERE ts_code = %s LIMIT 1",
                ('399317.SZ',),
            )
            rt_idx_row = cur.fetchone()
    except Exception as e:
        logger.exception("amount_top5_pct_series 读历史/rt_idx_k 失败: %s", e)
        return jsonify({'error': str(e)}), 500
    finally:
        conn_ts.close()

    rows_asc = hist_rows if start_date is not None else list(reversed(hist_rows))
    last_td: date | None = None
    if rows_asc:
        last_td = _sql_date_to_py_date(rows_asc[-1][0])

    trade_dates_py: list[date] = []
    times: list[str] = []
    amount_top5_pct: list[float | None] = []
    close: list[float | None] = []
    for td, pct_v, close_v in rows_asc:
        d_py = _sql_date_to_py_date(td)
        if d_py is None:
            continue
        t = pd.to_datetime(td, errors='coerce')
        if pd.isna(t):
            continue
        trade_dates_py.append(d_py)
        times.append(t.isoformat())
        amount_top5_pct.append(None if pd.isna(pct_v) else float(pct_v))
        close.append(None if pd.isna(close_v) else float(close_v))

    anchor_date: date | None = None
    pct_rt_str: str | None = None
    try:
        conn_wind = pymysql.connect(**DB_CONFIG)
        try:
            anchor_date = _read_rt_k_anchor_date(conn_wind)
            pct_rt_str = _compute_stockstat_amount_top5_pct(conn_wind)
        finally:
            conn_wind.close()
    except Exception as e:
        logger.warning("amount_top5_pct_series 实时拼接跳过: %s", e)

    def _slice_tail_max() -> None:
        nonlocal times, amount_top5_pct, close, trade_dates_py
        if len(times) > tail_cap:
            times = times[-tail_cap:]
            amount_top5_pct = amount_top5_pct[-tail_cap:]
            close = close[-tail_cap:]
            trade_dates_py = trade_dates_py[-tail_cap:]

    if (
        anchor_date is not None
        and pct_rt_str is not None
        and rt_idx_row is not None
        and rt_idx_row[1] is not None
    ):
        try:
            rt_close = float(rt_idx_row[1])
            pct_rt = float(pct_rt_str)
            t_dt = pd.to_datetime(rt_idx_row[0], errors='coerce')
            if pd.isna(t_dt):
                t_dt = pd.Timestamp(anchor_date)
            rt_iso = t_dt.isoformat()

            if last_td == anchor_date:
                if times:
                    times[-1] = rt_iso
                    amount_top5_pct[-1] = pct_rt
                    close[-1] = rt_close
                    trade_dates_py[-1] = anchor_date
                else:
                    times.append(rt_iso)
                    amount_top5_pct.append(pct_rt)
                    close.append(rt_close)
                    trade_dates_py.append(anchor_date)
            elif last_td is None or last_td < anchor_date:
                times.append(rt_iso)
                amount_top5_pct.append(pct_rt)
                close.append(rt_close)
                trade_dates_py.append(anchor_date)
            _slice_tail_max()
        except (TypeError, ValueError) as e:
            logger.warning("amount_top5_pct_series 实时点解析失败: %s", e)

    sigma2_low, sigma2_high = _compute_amount_top5_pct_rolling_sigma2_series(
        trade_dates_py,
        amount_top5_pct,
    )

    payload = {
        'times': times,
        'amount_top5_pct': amount_top5_pct,
        'close': close,
        'sigma2_low': sigma2_low,
        'sigma2_high': sigma2_high,
        'series_kind': 'amount_top5_pct',
        'ts_code': '399317.SZ',
        'limit': tail_cap,
        'start_date': start_date.isoformat() if start_date else None,
    }
    popupSeriesCachePut(cache_key, payload, 60.0)
    return jsonify(payload)


def get_etf_bias_table():
    """ETF BIAS(Per) 表：直接读 etf_bias_realtime 的 per（1250 根口径）；缺失时用≥1250 根 K 本地重算。TTL 缓存降低高频前端轮询时的 DB 压力。"""
    global etf_bias_cache, etf_bias_last_read_ts
    now_ts = time.time()
    with _cache_lock:
        if etf_bias_cache is not None and (now_ts - etf_bias_last_read_ts) < ETF_BIAS_CACHE_TTL:
            return jsonify(etf_bias_cache)
    try:
        rows = read_etf_bias_table()
        payload = {
            'rows': rows,
            'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        with _cache_lock:
            etf_bias_cache = payload
            etf_bias_last_read_ts = now_ts
        return jsonify(payload)
    except Exception as e:
        logger.exception("获取 ETF BIAS 表失败: %s", e)
        with _cache_lock:
            cached = etf_bias_cache if isinstance(etf_bias_cache, dict) else None
        if cached and isinstance(cached.get('rows'), list) and cached.get('rows'):
            stale_payload = dict(cached)
            stale_payload['stale'] = True
            stale_payload['error'] = str(e)
            return jsonify(stale_payload)
        return jsonify({'rows': [], 'last_update': '--', 'error': str(e)})


def get_etf_ma_signal_table():
    """九只 ETF：rt_k 基本行情、区间涨跌幅（T-1 日线；T-5/20/60/120 Wind 拼 rt_k 期末）、MA 信号、聚集度 Per。"""
    global etf_ma_signal_cache, etf_ma_signal_last_read_ts, etf_ma_signal_refreshing
    try:
        now = time.time()
        with _cache_lock:
            cached_rows = etf_ma_signal_cache or []
            age = (now - etf_ma_signal_last_read_ts) if etf_ma_signal_last_read_ts else None
            refreshing = bool(etf_ma_signal_refreshing)

        signal_updated_at = (
            datetime.fromtimestamp(etf_ma_signal_last_read_ts).strftime('%Y-%m-%d %H:%M:%S')
            if etf_ma_signal_last_read_ts else '--'
        )

        def _with_realtime(base_rows: list) -> tuple[list, str | None]:
            try:
                quotes = _fetch_rt_k_etf_quotes(list(ETF_MA_SIGNAL_ORDER))
            except Exception as quote_e:
                logger.warning("MA 信号表：实时字段覆盖失败: %s", quote_e)
                quotes = {}
            return _overlay_ma_signal_realtime_rows(base_rows, quotes)

        # 热缓存：直接返回，避免阻塞首屏
        if cached_rows and age is not None and age < ETF_MA_SIGNAL_CACHE_TTL:
            response_rows, quote_observed_at = _with_realtime(cached_rows)
            response_rows = _safe_attach_etf_ma_metric_percentiles(response_rows)
            return jsonify({
                'rows': response_rows,
                'last_update': quote_observed_at or signal_updated_at,
                'quote_observed_at': quote_observed_at or '--',
                'signal_updated_at': signal_updated_at,
                'warming': False,
                'stale': False,
            })

        # 缓存过期：后台刷新，当前请求先返回旧快照（不清空 UI）
        if cached_rows:
            if not refreshing:
                _kickoff_ma_signal_table_refresh_async(force=False)
            response_rows, quote_observed_at = _with_realtime(cached_rows)
            response_rows = _safe_attach_etf_ma_metric_percentiles(response_rows)
            return jsonify({
                'rows': response_rows,
                'last_update': quote_observed_at or signal_updated_at,
                'quote_observed_at': quote_observed_at or '--',
                'signal_updated_at': signal_updated_at,
                'warming': False,
                'stale': True,
            })

        # 无缓存：异步拉取 + 快速兜底行，避免“MA/EMA 数据加载中...”长期悬挂
        if not refreshing:
            _kickoff_ma_signal_table_refresh_async(force=True)
        fallback_rows = _safe_attach_etf_ma_metric_percentiles(_build_ma_signal_fallback_rows())
        quote_observed_at = max(
            (str(r.get('etf_observed_at')) for r in fallback_rows if r.get('etf_observed_at')),
            default=None,
        )
        return jsonify({
            'rows': fallback_rows,
            'last_update': quote_observed_at or '--',
            'quote_observed_at': quote_observed_at or '--',
            'signal_updated_at': '--',
            'warming': True,
            'stale': True,
        })
    except Exception as e:
        logger.exception("获取 ETF MA 信号表失败: %s", e)
        with _cache_lock:
            rows = etf_ma_signal_cache or []
        # 失败时优先回退上次成功结果，避免前端误判为“算法没更新”
        return jsonify({'rows': rows, 'last_update': '--', 'error': str(e), 'warming': (len(rows) == 0), 'stale': True})


def _normalize_ma_sr_row(val) -> dict:
    """将单只 ETF 的 s1/r1/s2/r2 规范为 float 或 None。"""
    row = {'s1': None, 'r1': None, 's2': None, 'r2': None}
    if not isinstance(val, dict):
        return row
    for k in ('s1', 'r1', 's2', 'r2'):
        x = val.get(k)
        if x is None or x == '':
            row[k] = None
            continue
        try:
            fv = float(x)
            if fv != fv:  # NaN
                row[k] = None
            else:
                row[k] = fv
        except (TypeError, ValueError):
            row[k] = None
    return row


def _normalize_ma_sr_payload(obj) -> dict:
    """整表载荷：仅保留 str 键，值规范为 {s1,r1,s2,r2}。"""
    out = {}
    if not isinstance(obj, dict):
        return out
    for key, val in obj.items():
        if not isinstance(key, str):
            continue
        code = key.strip()
        if not code:
            continue
        out[code] = _normalize_ma_sr_row(val)
    return out


def _read_ma_sr_levels_dict() -> dict:
    try:
        if not os.path.isfile(_MA_SR_LEVELS_PATH):
            return {}
        with open(_MA_SR_LEVELS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return _normalize_ma_sr_payload(data)
    except Exception as e:
        logger.warning("读取 ma_sr_levels.json 失败，返回空: %s", e)
        return {}


def _write_ma_sr_levels_dict(payload: dict) -> None:
    normalized = _normalize_ma_sr_payload(payload)
    tmp_path = _MA_SR_LEVELS_PATH + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _MA_SR_LEVELS_PATH)


def api_ma_sr_levels():
    """MA 表支撑/阻力价位：GET 读 JSON 文件；POST 保存（局域网多机与 localhost 同源 localStorage 共用）。"""
    if request.method == 'GET':
        return jsonify(_read_ma_sr_levels_dict())
    try:
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            return jsonify({'error': '请求体须为 JSON 对象'}), 400
        _write_ma_sr_levels_dict(body)
        saved = _read_ma_sr_levels_dict()
        return jsonify({'ok': True, 'levels': saved})
    except Exception as e:
        logger.exception("保存 ma_sr_levels 失败: %s", e)
        return jsonify({'error': str(e)}), 500


def get_etf_ma_signal_series():
    """内嵌图：指定 ts_code、freq=60MIN|D，返回最近 300 根 close 及 20/60 组每根信号 1/-1/0（pandas 从 close 重算 MA/EMA/REF）。"""
    t0 = time.perf_counter()
    try:
        ts_code = (request.args.get('ts_code') or '').strip()
        freq_param = (request.args.get('freq') or '').strip().upper()
        if not ts_code or freq_param not in ('60MIN', 'D'):
            return jsonify({'error': '缺少或无效的 ts_code/freq（仅支持 60MIN、D）'}), 400
        cache_key = (ts_code, freq_param)
        payload = _small_ttl_cache_get(_ma_signal_series_cache, cache_key, time.time())
        if payload is not None:
            return jsonify(payload)
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
        try:
            if freq_param == 'D':
                df = _load_daily_close_df(conn, ts_code, MA_SIGNAL_SERIES_LIMIT)
            else:
                df = _load_merged_minute_close_df(conn, ts_code, freq_param, MA_SIGNAL_SERIES_LIMIT)
        finally:
            conn.close()
        if df is None or len(df) == 0:
            return jsonify({
                'times': [], 'close': [], 'signal_20': [], 'signal_60': [],
                'ts_code': ts_code, 'freq': freq_param,
            })
        sigs = _compute_ma_ref_signals(df['close'], ETF_MA_SIGNAL_PERIODS)
        times = [t.isoformat() if hasattr(t, 'isoformat') else str(t) for t in df['trade_time']]

        def _safe_close(s):
            return [None if pd.isna(x) else float(x) for x in s]

        payload = {
            'times': times,
            'close': _safe_close(df['close']),
            'signal_20': _signal_series_to_json(sigs[20]),
            'signal_60': _signal_series_to_json(sigs[60]),
            'ts_code': ts_code,
            'freq': freq_param,
        }
        _small_ttl_cache_put(
            _ma_signal_series_cache,
            cache_key,
            payload,
            MA_SIGNAL_SERIES_CACHE_TTL,
            MA_SIGNAL_SERIES_CACHE_MAX,
        )
        _log_slow_api(
            'etf_ma_signal_series',
            t0,
            f"ts_code={ts_code} freq={freq_param} rows={len(payload.get('times') or [])}",
        )
        return jsonify(payload)
    except Exception as e:
        logger.exception("etf_ma_signal_series 失败: %s", e)
        return jsonify({'error': str(e)}), 500


# 启动入口见 rt_monitor.app_factory.run_server（由 rt_monitor_app.py __main__ 调用）


# 代码更新时间: 2026-03-05 新增 /api/etf_bias_series（300 周期 + 2σ 标记）、前端 BIAS 点击弹窗折线图
# 代码更新时间: 2026-03-17 16:35 分钟差值图改为从 rt_idx_min 取最新交易日作基准，再从 idx_mins 取其上一交易日数据进行差值计算
# 代码更新时间: 2026-03-19 新增 MA/EMA/REF 共振信号表（60MIN+日线，20/60 组）、/api/etf_ma_signal_*、内嵌图（无弹窗）
# 代码更新时间: 2026-03-19 database_pool 改为可选导入，缺失时使用 pymysql 直连 + 内置 retry，避免单独复制脚本时 ModuleNotFoundError
# 代码更新时间: 2026-03-23 BIAS(Per) 表 15MIN/60MIN 优先读 etf_bias_realtime（updated_at 在 ETF_BIAS_REALTIME_MAX_AGE 内），否则 qfq 重算
# 代码更新时间: 2026-03-23 ETF_BIAS_REALTIME_MAX_AGE 60→120：对齐 etf_rt_min 命中 bar 后约 50s+jitter 的写入间隔，避免 3 秒轮询时在实时表与重算间抖动
# 代码更新时间: 2026-03-23 15/60 分钟 BIAS：历史 stk_mins+当日 rt_min+etf_bias_realtime 虚拟K 时序拼接后 qfq；etf_bias_series 分钟分支与此一致
# 代码更新时间: 2026-03-23 BIAS 表展示优先 etf_bias_realtime 的 per 列（与 daemon 1250 根口径一致）；仅用 300 根重算会与库不一致、看起来像不刷新
# 代码更新时间: 2026-03-23 BIAS 表/序列：表仅读 etf_bias_realtime；回退与 etf_bias_series 计算用≥PER_BIAS_WINDOW 根，序列 JSON 仍返回尾部 BIAS_SERIES_LIMIT 点
# 代码更新时间: 2026-03-23 limit_stats 新高/低六项：直连 rt_k+stk_factor_ma 计数，不依赖 stockstat.windcodes_high_* 是否已写入
# 代码更新时间: 2026-03-23 10:36 新高/低换算：stk_factor_ma 无未复权 high/low/close，LEFT JOIN stk_factor_pro 取 pro_high/pro_low/pro_close
# 代码更新时间: 2026-03-23 16:43 连板涨停数值支持点击弹窗明细（代码/名称/成交额亿/涨跌幅/申万行业），口径与 cnt_lb_zt 严格一致
# 代码更新时间: 2026-03-23 涨跌家数面板：新高/低、一字涨跌停、涨停跌停、连板跌停、T-1 等可点，统一 /api/limit_stats/detail?kind=
# 代码更新时间: 2026-03-24 12:00 涨跌家数面板恢复涨停T-5/跌停T-5 数值；不设明细 kind、表格格不可点
# 代码更新时间: 2026-03-24 12:30 rt_monitor.js：强缓存旧 HTML 时把 T-1 右侧灰色「—」升级为 zt_t5/dt_t5 格，确保能写入接口数值
# 代码更新时间: 2026-03-24 15:30 涨停T-5/跌停T-5：limit_list_ths distinct 交易日倒序第 5 日再 COUNT（不使用 trade_cal）
# 代码更新时间: 2026-03-24 16:30 zt_t5/dt_t5：limit_list_ths 全表最新 5 个 distinct 交易日，每日 COUNT 涨停池/跌停池（不含 ST）后算术均值 int(round)；表头涨停T-5均/跌停T-5均
# 代码更新时间: 2026-03-24 17:10 T-5均：与 latest_td 解耦；不足 5 个 distinct 日时按实际天数均分，避免恒为 null
# 代码更新时间: 2026-03-24 18:45 T-5均：去掉大段聚合 SQL，改回与 zt_t1 相同 COUNT 的 Python 循环求整数均值；T-5 单独 try 失败不拖累 T-1
# 代码更新时间: 2026-03-24 12:38 T-5均：独立 cur_t5 游标执行；RT_MONITOR_STATIC_VER 20260324-13；HTML 为 zt_t5/dt_t5 格加 id，JS 用 getElementById 写入防漏刷
# 代码更新时间: 2026-03-24 12:45 /api/limit_stats：若响应无 zt_t5/dt_t5 键则打 ERROR 并设 X-Limit-Stats-Panel:legacy-missing-t5（根因多为未重启进程）；RT_MONITOR_STATIC_VER 20260324-14
# 代码更新时间: 2026-03-24 15:42 启动防双开：新增 .rt_monitor_app.lock 单实例锁（PID 存活即拒绝二次启动，僵尸锁自动清理）
# 代码更新时间: 2026-03-24 15:56 单实例锁支持环境变量 RT_MONITOR_SINGLE_INSTANCE（默认1；设0可关闭，便于 PyCharm 多配置调试）
# 代码更新时间: 2026-03-24 22:18 涨跌家数表头文案调整：涨停T-5均/跌停T-5均 改为 涨停T-5/跌停T-5；RT_MONITOR_STATIC_VER 20260324-15
# 代码更新时间: 2026-03-28 22:11 MA 共振表左上角表头改为「Ma/Ema」与「方向」两行居中；rt_monitor.css 允许该格换行；RT_MONITOR_STATIC_VER 20260328-1
# 代码更新时间: 2026-03-30 涨跌家数：涨停T-1/T-5、跌停T-1/T-5 平移至连板涨停/连板跌停右侧（9 列）；RT_MONITOR_STATIC_VER 20260330-1
# 代码更新时间: 2026-03-23 前端连板涨停明细改请求 /api/limit_stats/detail/lb_zt；旧进程无 /api/limit_stats/detail 时避免 404 非 JSON 导致「网络请求失败」
# 代码更新时间: 2026-03-23 新增 /api/limit_stats/detail/<kind> 路径路由，前端统一走路径；与 /detail/lb_zt 同级，避免 ?kind= 在部分部署下 404
# 代码更新时间: 2026-03-23 新增扁平 /api/limit_stats_detail?kind= ；前端除 cnt_lb_zt 外走此接口（连板涨停仍走 /detail/lb_zt）
# 代码更新时间: 2026-03-23 启动日志打印 limit_stats 相关路由；前端明细请求多 URL 依次回退
# 代码更新时间: 2026-03-23 明细首选 GET /api/limit_stats?detail=1&kind= 与面板同源，避免独立 URL 404
# 代码更新时间: 2026-03-23 涨跌家数 API 禁止缓存 + 前端校验 rows 为数组，避免误用面板 JSON 致连板涨停弹窗空表
# 代码更新时间: 2026-03-23 前端明细请求顺序：先 /api/limit_stats_detail 等与面板不同路径，同路径 ?detail=1 放最后（防路径级误缓存）
# 代码更新时间: 2026-03-30 涨跌家数新高/低：5/10/20/60/120/200/250/850/历史 共十八项；stockstat 增 windcodes_*_5..200；RT_MONITOR_STATIC_VER 20260330-3
# 代码更新时间: 2026-03-30 涨跌家数表增 HL_C 行（9 列百分点差，rt 收盘前复权 vs ma.high/low）；RT_MONITOR_STATIC_VER 20260330-4
# 代码更新时间: 2026-03-30 涨跌家数：新低行与 HL_C 行上下对调；HL_C 数值正红负绿；RT_MONITOR_STATIC_VER 20260330-5
# 代码更新时间: 2026-03-30 19:05 MA/Ema 方向表新增聚集度Per(15MIN/60MIN/日线)，按历史+当日拼接后 qfq 序列计算；RT_MONITOR_STATIC_VER 20260330-6
# 代码更新时间: 2026-03-30 19:09 MA/Ema 表聚集度Per着色改为与 BIAS Per 一致（2σ黄/3σ红，含 hover）；RT_MONITOR_STATIC_VER 20260330-7
# 代码更新时间: 2026-03-30 19:13 MA/Ema 表方向三角形列显式排除 2σ/3σ 着色，仅聚集度Per三列参与；RT_MONITOR_STATIC_VER 20260330-8
# 代码更新时间: 2026-03-30 19:18 聚集度Per三列支持点击弹窗（复用BIAS弹窗样式），新增 /api/etf_mcd_series 返回 close+per_mcd 序列；RT_MONITOR_STATIC_VER 20260330-9
# 代码更新时间: 2026-03-30 19:24 修复聚集度Per弹窗前段异常：接口取数增加 mcd 预热窗口（ma120 预热）后再截取300点展示；RT_MONITOR_STATIC_VER 20260330-10
# 代码更新时间: 2026-03-30 20:02 聚集度Per弹窗改为优先读取已落库 per_mcd（历史 etf_stk_mins_ma/etf_daily + 当日 etf_rt_min），与表值严格对齐；RT_MONITOR_STATIC_VER 20260330-11
# 代码更新时间: 2026-03-30 20:12 主表聚集度Per改为优先读取已落库 per_mcd（分钟 rt_min→mins_ma，日线 realtime(D)→daily），与弹窗/数据库统一；RT_MONITOR_STATIC_VER 20260330-12
# 代码更新时间: 2026-03-30 20:20 MA信号表接口增加3秒缓存与失败回退；前端请求失败时给表格红框状态提示，降低超时导致的“未刷新”误判；RT_MONITOR_STATIC_VER 20260330-13
# 代码更新时间: 2026-03-30 14:20 stock_tushare 连接放宽 read/write_timeout（默认 180s）；MA 信号表遇 2013 自动重连重试一次
# 代码更新时间: 2026-03-30 分钟历史段 SQL：ma 子查询先 ORDER BY trade_time DESC LIMIT innerCap 再 JOIN etf_stk_mins，降低大表 JOIN 排序耗时
# 代码更新时间: 2026-03-31 limit_stats 新高/低/HL_C/明细：rt_k 增加 DATE(trade_time)=全表最新日，剔除旧 trade_time 残留
# 代码更新时间: 2026-03-31 rt_k(trade_time) 索引幂等创建；新鲜度条件改为 MAX(trade_time) 自然日范围（配合索引）
# 代码更新时间: 2026-03-31 read_limit_stats_panel 文档明确 pct 与 HL 口径差异；修正 load_initial_cache except 缩进
# 代码更新时间: 2026-03-31 rt_k 新鲜度与 ensure 索引内联本文件（与盘中实时统计.py 双份同步）
# 代码更新时间: 2026-03-31 涨跌家数：超跌 up_num_ma5_rt（近5日上涨占比，当日 rt_k 含ST）；RT_MONITOR_STATIC_VER 20260331-1
# 代码更新时间: 2026-03-31 涨跌家数：超跌Per up_num_ma5_rt_per（marketinfo.up_num_ma5 近500条百分位）；RT_MONITOR_STATIC_VER 20260331-2
# 代码更新时间: 2026-03-31 涨跌家数：超跌与超跌Per 同列上下对齐（去掉第11列）；RT_MONITOR_STATIC_VER 20260331-3
# 代码更新时间: 2026-03-31 超跌/超跌Per 着色改 BIAS 同款 2σ/3σ 底色、默认黑字；RT_MONITOR_STATIC_VER 20260331-4
# 代码更新时间: 2026-03-31 超跌值支持点击弹窗：marketinfo.up_num_ma5 + index_daily(399317.SZ).close 最近300窗口（trade_date匹配）；RT_MONITOR_STATIC_VER 20260331-5
# 代码更新时间: 2026-03-31 up_num_ma5_series 盘中拼接 rt_idx_k+_read_up_num_ma5_rt；RT_MONITOR_STATIC_VER 20260331-6
# 代码更新时间: 2026-03-31 18:57 P0: 变量名 pd→prev_d 消除 pandas 覆盖
# 代码更新时间: 2026-03-31 18:57 P1: stockstat 12次COUNT→1条条件聚合；T-5均值10次COUNT→1条GROUP BY；BIAS表+limit_stats面板加3秒TTL缓存
# 代码更新时间: 2026-04-02 单实例锁：Windows 用命名互斥体替代「PID 存活」判断（避免 PID 复用/误判）；POSIX 用 flock；load_initial_cache 仅在锁成功后执行
# 代码更新时间: 2026-04-03 涨跌家数表单独成行：置于首行四表之下、BIAS|MA 之上；RT_MONITOR_STATIC_VER 20260403-1
# 代码更新时间: 2026-04-03 TEMPLATES_AUTO_RELOAD=True（否则 debug=False 时改 HTML 不重启不生效）；RT_MONITOR_STATIC_VER 20260403-2
# 代码更新时间: 2026-04-03 .limit-stats-row CSS 加固：display:block !important + clear:both + width:100%；RT_MONITOR_STATIC_VER 20260403-3
# 代码更新时间: 2026-04-03 布局规则内联到 HTML <style> 块（随 no-cache HTML 下发，彻底绕过外链 CSS 缓存）；RT_MONITOR_STATIC_VER 20260403-4
# 代码更新时间: 2026-04-03 统一所有区块间距为 3px（与首行四表 quad-gap 一致）：limit-stats/bias-ma/图表区 margin-top=0，各区块 margin-bottom=3px；RT_MONITOR_STATIC_VER 20260403-5
# 代码更新时间: 2026-04-03 涨跌家数表 myuiux：格线 #f0f0f0、标签行 600、数值 .limit-stats-num 600；RT_MONITOR_STATIC_VER 20260403-6
# 代码更新时间: 2026-04-03 BIAS/MA/涨跌家数单元格统一 --rt-monitor-padding-bias-ma-row:4px 10px（首行四表仍为 6px）；RT_MONITOR_STATIC_VER 20260403-7
# 代码更新时间: 2026-04-03 --rt-monitor-padding-bias-ma-row 改为 3px 10px（更紧一行）；RT_MONITOR_STATIC_VER 20260403-8
# 代码更新时间: 2026-04-03 3px 10px 仅涨跌家数；BIAS/MA 恢复 6px 10px；变量改名 --rt-monitor-padding-limit-stats；RT_MONITOR_STATIC_VER 20260403-9
# 代码更新时间: 2026-04-03 国证A指四图两两并排（左当日/右前五日）：rt_monitor.html + rt_monitor.css；RT_MONITOR_STATIC_VER 20260403-10
# 代码更新时间: 2026-04-03 冲高回落 Top30 下新增融资余额/净买入额表（margin_daily 最近300日）、/api/margin_daily_recent；RT_MONITOR_STATIC_VER 20260403-11
# 代码更新时间: 2026-04-03 融资余额区块改为双轴图（净买入柱+融资余额紫线），对齐分钟成交额差值图样式；RT_MONITOR_STATIC_VER 20260403-12
# 代码更新时间: 2026-04-03 margin_daily 查询改为 FROM margin_daily（随连接库），前端 fetch 增加 HTTP/业务错误提示；RT_MONITOR_STATIC_VER 20260403-13
# 代码更新时间: 2026-04-03 margin_daily：rzye_sum/netbuy 在 SQL 中 ROUND(元/1e8,2) 为亿，与 margin_sum 元口径一致；RT_MONITOR_STATIC_VER 20260403-14
# 代码更新时间: 2026-04-03 单实例锁失败时 stderr 说明；启动日志打印 margin_daily 路由；after_request 禁止缓存该 API；RT_MONITOR_STATIC_VER 20260403-15
# 代码更新时间: 2026-04-03 web_dashboard 同步注册 /api/margin_daily_recent（与 rt_monitor 共用 /rt-monitor 模板时不再 404）；RT_MONITOR_STATIC_VER 20260403-16
# 代码更新时间: 2026-04-03 启动前自动结束其它 rt_monitor_app.py 进程（RT_MONITOR_AUTO_KILL_PREVIOUS）；RT_MONITOR_PORT；RT_MONITOR_STATIC_VER 20260403-17
# 代码更新时间: 2026-04-03 /api/margin_rzmre_idx_series：rzmre_pct + index_daily 399317.SZ close；并排双折线图；RT_MONITOR_STATIC_VER 20260403-18
# 代码更新时间: 2026-04-03 margin_rzmre_idx_series 增加 rzmre_pct_per；表头与 Tooltip 展示 Per；RT_MONITOR_STATIC_VER 20260403-19
# 代码更新时间: 2026-04-03 融资买入额占比折线改 Material Blue 600；RT_MONITOR_STATIC_VER 20260403-20
# 代码更新时间: 2026-04-03 /api/margin_daily_recent 增加 netbuy_pct；融资融券图 Tooltip 显示净买入%；RT_MONITOR_STATIC_VER 20260403-21
# 代码更新时间: 2026-04-03 margin_rzmre_idx_series limit 上限 1300；融资占比图双击弹窗约5年+平移缩放；RT_MONITOR_STATIC_VER 20260403-22
# 代码更新时间: 2026-04-03 融资占比弹窗高度 -20%；X 轴跨年两行显示年份+月日；RT_MONITOR_STATIC_VER 20260403-23
# 代码更新时间: 2026-04-03 融资占比弹窗 CSS resize 可调宽高；ResizeObserver 触发 Chart.resize；RT_MONITOR_STATIC_VER 20260403-24
# 代码更新时间: 2026-04-03 融资占比弹窗去掉说明段落；RT_MONITOR_STATIC_VER 20260403-25
# 代码更新时间: 2026-04-03 BIAS/聚集度Per 弹窗折线配色与融资占比弹窗 myuiux 对齐（收盘紫、Per Material 蓝），取消 Per 线按 over_2sigma 分段黄/蓝；RT_MONITOR_STATIC_VER 20260403-26
# 代码更新时间: 2026-04-03 融资买入额占比弹窗：localStorage 保存用户调整的窗体宽高与位置，下次打开恢复；RT_MONITOR_STATIC_VER 20260403-27
# 代码更新时间: 2026-04-03 融资买入额占比主图标题旁去掉「（双击放大）」文案；RT_MONITOR_STATIC_VER 20260403-28
# 代码更新时间: 2026-04-03 净买入/融资余额主图双击弹窗（约5年+zoom）；margin_daily_recent limit 上限 1300；RT_MONITOR_STATIC_VER 20260403-29
# 代码更新时间: 2026-04-03 行情监控区块间距迭代：先统一 6px 再收紧为 3px（section-v / chart-pair / BIAS|MA 横缝）；图卡 padding 6px；RT_MONITOR_STATIC_VER 20260403-31
# 代码更新时间: 2026-04-03 Top30 双表：#top-moves-row flex+gap 消灭 inline-block 空白缝；去 top-moves-table min-width:100%；RT_MONITOR_STATIC_VER 20260403-32
# 代码更新时间: 2026-04-03 超跌弹窗独立：可拖动/resize/localStorage；/api/up_num_ma5_series 支持 limit≤1300；与融资占比弹窗同款 zoom；RT_MONITOR_STATIC_VER 20260403-33
# 代码更新时间: 2026-04-03 超跌弹窗：category X 显式 min/max 最近300点 + limits 0..n-1 + pan/wheel modifierKey null，修复无法横向拖动浏览；RT_MONITOR_STATIC_VER 20260403-34
# 代码更新时间: 2026-04-03 BIAS/聚集度Per 弹窗对齐标准大图：可 resize、localStorage、zoom；etf_bias_series/etf_mcd_series 支持 limit（15m≤5000/60m≤2500/D≤1300）；RT_MONITOR_STATIC_VER 20260403-35
# 代码更新时间: 2026-04-03 etf_mcd 日线去重+按 fetch_bars 截断；etf_bias 日线同样按 trade_time 去重后截断；共用 _series_to_json_floats；前端弹窗列长度对齐校验
# 代码更新时间: 2026-04-03 日频融资融券双图移出自动刷新间隔；顶栏「刷新日频数据」+ 配置面板内同名按钮；RT_MONITOR_STATIC_VER 20260403-36
# 代码更新时间: 2026-04-03 顶栏文案「刷新日频」；配置内去日频区；BIAS 消息区下移；日频完成右下 toast；RT_MONITOR_STATIC_VER 20260403-37
# 代码更新时间: 2026-04-03 方案 B：顶栏配置+刷新日频横排；BIAS 条移入 container 顶文档流；详情层改父级 absolute；RT_MONITOR_STATIC_VER 20260403-38
# 代码更新时间: 2026-04-03 body 顶栏占位 56px + 顶栏内 config-trigger static !important，消除与 BIAS/双钮重叠；RT_MONITOR_STATIC_VER 20260403-39
# 代码更新时间: 2026-04-03 首行 flex：四表 + 右侧工具列（配置/日频/BIAS），顶边与表头对齐；配置面板改 config-wrap 内 absolute；RT_MONITOR_STATIC_VER 20260403-40
# 代码更新时间: 2026-04-03 MA/Ema 表：rt_k 基本行情列 + 可配置支撑阻力（localStorage）；突破/破位浅底色；RT_MONITOR_STATIC_VER 20260403-41
# 代码更新时间: 2026-04-05 配置面板加宽 + 支撑阻力输入列 min-width，便于录入价位；RT_MONITOR_STATIC_VER 20260403-42
# 代码更新时间: 2026-04-05 MA 表支撑/阻力格 title 悬停显示设置价位；RT_MONITOR_STATIC_VER 20260403-43
# 代码更新时间: 2026-04-05 MA 支撑/阻力：三位小数 + 自定义快显浮层（80ms）+ myuiux 样式；RT_MONITOR_STATIC_VER 20260403-44
# 代码更新时间: 2026-04-05 MA 浮层百分比红涨绿跌、延迟 60ms、规范写入 myuiux；RT_MONITOR_STATIC_VER 20260403-45
# 代码更新时间: 2026-04-05 MA 表 Amp 列固定黑色（振幅非涨跌色）；RT_MONITOR_STATIC_VER 20260403-46
# 代码更新时间: 2026-04-05 21:58 MA/Ema 表基本行情：最新价左侧增成交额（亿），rt_k.amount 元/1e8；RT_MONITOR_STATIC_VER 20260403-47
# 代码更新时间: 2026-04-05 22:08 MA/Ema 表头：成交额→AMT、最新价→Last；AMT 列数值居中；RT_MONITOR_STATIC_VER 20260403-48
# 代码更新时间: 2026-04-05 当日成交额 Top30：amount_top30；stk_factor_pro 昨成交额(千元→亿)+rt_k 自由换手%；RT_MONITOR_STATIC_VER 20260405-1
# 代码更新时间: 2026-04-05 Top30 行布局：子卡 flex:0 0 auto+min-content，行 overflow-x:auto（恢复前两表紧凑版式、第三表可横向滚动）；RT_MONITOR_STATIC_VER 20260405-2
# 代码更新时间: 2026-04-05 当日成交额 Top30 表头：成交额加权涨跌幅 amount_top_weighted_pct；RT_MONITOR_STATIC_VER 20260405-3
# 代码更新时间: 2026-04-06 MA/Ema 表：基本行情后增区间涨跌幅 T-1/T-5/T-20/T-60/T-120（etf_daily qfq 链）；RT_MONITOR_STATIC_VER 20260405-4
# 代码更新时间: 2026-04-06 _read_etf_daily_interval_range_pct 注释：核对拼接见 verify_etf_daily_interval_pct.py
# 代码更新时间: 2026-04-06 MA 区间涨跌幅改 Wind（含首尾 N 日、基期=起始日前收盘）；期末 rt_k+历史 etf_daily；RT_MONITOR_STATIC_VER 20260406-1
# 代码更新时间：2026-04-06 22:40 涨跌幅(%)表增区间 T-1..T-120（index_daily+rt_idx_k；932000/899050 不拼 rt）；RT_MONITOR_STATIC_VER 20260406-3
# 代码更新时间：2026-04-06 22:45 /api/rt_median 禁止缓存（避免旧 JSON 无 range 列）；表头去「基本行情/区间涨跌幅」分组行；RT_MONITOR_STATIC_VER 20260406-4
# 代码更新时间：2026-04-06 22:58 MA/Ema 表：支撑与阻力列移至聚集度Per之后；RT_MONITOR_STATIC_VER 20260406-5
# 代码更新时间：2026-04-06 23:07 监控页 UI：终端浅灰底、卡片 token、focus-visible/skip/main、aria-label、静态资源 RT_MONITOR_STATIC_VER 20260406-6
# 代码更新时间：2026-04-06 23:22 监控页按钮层级/hover/active、tap 高亮、可点格键盘 Enter/空格、配置项 name/autocomplete、RT_MONITOR_STATIC_VER 20260406-7
# 代码更新时间：2026-04-06 23:27 涨跌家数表：修复 td font-weight 压过数值 600；body 显式 font-family；RT_MONITOR_STATIC_VER 20260406-8
# 代码更新时间：2026-04-06 23:31 涨跌家数标签行与 BIAS/MA 表头对齐：去 table 叠层阴影、标签行 padding 6px+tabular-nums；RT_MONITOR_STATIC_VER 20260406-9
# 代码更新时间：2026-04-06 23:34 涨跌家数可点格去 filter:hover、与 BIAS 统一下划线；median-table-num / limit-stats-num 补 font-feature tnum；RT_MONITOR_STATIC_VER 20260406-10
# 代码更新时间：2026-04-06 23:37 涨跌家数标签行 th：.median-table.limit-stats-table th 格线+tabular-nums；标签行 padding 兼容 th/td；RT_MONITOR_STATIC_VER 20260406-11
# 代码更新时间：2026-04-06 23:40 涨跌家数标签行取消加粗：.median-table.limit-stats-table th font-weight 400；RT_MONITOR_STATIC_VER 20260406-12
# 代码更新时间：2026-04-08 14:13 前五日分钟图标题右侧文案去前缀；RT_MONITOR_STATIC_VER 20260408-1
# 代码更新时间：2026-04-09 10:47 涨跌家数：昨涨Pct/昨跌Pct（初版误用 rt_k.vol，已改为 rt_k.amount）；超跌/超跌Per 与 His高/低 同行；RT_MONITOR_STATIC_VER 20260409-1
# 代码更新时间：2026-04-09 10:49 昨涨Pct/昨跌Pct 权重改为 rt_k.amount；Top30 加权涨跌注释明确用 stockstat.amount；RT_MONITOR_STATIC_VER 20260409-2
# 代码更新时间：2026-04-09 11:01 涨跌家数：超跌/超跌Per 移至昨涨Pct/昨跌Pct 右侧；His 行恢复 9 列；RT_MONITOR_STATIC_VER 20260409-3
# 代码更新时间：2026-04-09 11:36 分钟成交额差值表头新增 marketinfo 最近60窗口最大 amount（含交易日）与今日预测成交额比值%；RT_MONITOR_STATIC_VER 20260409-4
# 代码更新时间：2026-04-09 11:41 表头文案「近60日峰值」改为「T-60峰值」；RT_MONITOR_STATIC_VER 20260409-5
# 代码更新时间：2026-04-09 11:42 T-60峰值金额与今日预测成交额同用 idx-predict-value 黄色；RT_MONITOR_STATIC_VER 20260409-6
# 代码更新时间：2026-04-09 11:48 表头「今日预测成交额」改为「今日预测」；RT_MONITOR_STATIC_VER 20260409-7
# 代码更新时间：2026-04-09 11:57 成交额（亿）表增 index_daily 近60交易日峰值（MAX T-60）与峰值日；RT_MONITOR_STATIC_VER 20260409-8
# 代码更新时间：2026-04-09 12:10 全部A股 MAX T-60 用 marketinfo.amount；成交额表峰值两列居中；RT_MONITOR_STATIC_VER 20260409-9
# 代码更新时间：2026-04-09 12:25 成交额表移至涨跌幅下对齐；与涨跌家数同排（先成交额后涨跌家数）；RT_MONITOR_STATIC_VER 20260409-10
# 代码更新时间：2026-04-09 首块改 flex+左纵叠右 2×2（myuiux 单缝 3px）；成交额峰值列居中 !important；RT_MONITOR_STATIC_VER 20260409-11
# 代码更新时间：2026-04-09 首块改上下两行+上行三张横排；#median-quad-row 内 .median-table-wrap margin:0 消双缝；RT_MONITOR_STATIC_VER 20260409-12
# 代码更新时间：2026-04-09 涨跌家数昨涨Pct/昨跌Pct：不显示±号，正红负绿；RT_MONITOR_STATIC_VER 20260409-13
# 代码更新时间：2026-04-09 12:38 分钟成交额差值表头 T-60 峰值改读 index_daily（399317.SZ），不再用 marketinfo；JSON 键名未变
# 代码更新时间：2026-04-09 12:44 成交额（亿）表峰值日右侧增「缩量%」= T-1/MAX T-60×100（前端）；表头/数值居中；RT_MONITOR_STATIC_VER 20260409-14
# 代码更新时间：2026-04-09 12:46 缩量% 列数值去掉尾部「%」符号（表头已含%）；RT_MONITOR_STATIC_VER 20260409-15
# 代码更新时间：2026-04-10 06:50 涨跌家数炸板/开板：与涨停/跌停同口径可点明细（stockstat high_limit_lifting/low_limit_lifting）；RT_MONITOR_STATIC_VER 20260410-1
# 代码更新时间：2026-04-10 06:53 昨涨Pct/昨跌Pct：正值红无+、负值绿显负号；RT_MONITOR_STATIC_VER 20260410-2
# 代码更新时间：2026-04-11 15:44 /api/ma_sr_levels + ma_sr_levels.json，支撑阻力与 localhost/IP 访问同源共用；RT_MONITOR_STATIC_VER 20260411-1
# 代码更新时间：2026-04-11 16:04 首行 flex nowrap+工具列 shrink:0，BIAS|MA 行 nowrap+横向滚动，防局域网窄宽下控件换行插入表间；RT_MONITOR_STATIC_VER 20260411-2
# 代码更新时间：2026-04-12 分钟差值标题「分钟差值」、Max(60)、index_daily Ma(20) 与（预测/Ma(20)）；RT_MONITOR_STATIC_VER 20260412-1
# 代码更新时间：2026-04-12 Ma(20) 移至「分钟差值vs 前五均值」表头；抽取 _compute_idx_predict_today_amounts；RT_MONITOR_STATIC_VER 20260412-2
# 代码更新时间：2026-04-13 index_member_all_sw 索引 idx_member_sw_ts_indate + 申万 JOIN 改 ROW_NUMBER；load_initial_cache 幂等建索引；RT_MONITOR_STATIC_VER 20260413-4
# 代码更新时间：2026-04-13 etf_bias_series 弹窗：分钟单表读 per_bias*、日线读 per_bias10ma，缺列回退 JOIN+rolling；RT_MONITOR_STATIC_VER 20260413-5
# 代码更新时间：2026-04-13 margin_rzmre_idx_series：先 LIMIT margin_daily 再 JOIN index_daily；融资弹窗 JS 预取+缓存；RT_MONITOR_STATIC_VER 20260413-6
# 代码更新时间：2026-04-13 get_rt_median：指数区间/T-60 仅 TTL 刷新时查库；index_daily MAX T-60 批量窗口；MA 信号表分钟 close 走 mcd 单表路径；RT_MONITOR_STATIC_VER 20260413-7
# 代码更新时间：2026-04-13 etf_bias_series：NumPy 滚动百分位替代 rolling.apply；4s LRU 响应缓存；RT_MONITOR_STATIC_VER 20260413-8
# 代码更新时间：2026-04-13 12:59 涨跌幅%%表/MA 表 Wind 区间：单次拉 prior 算 T-5/20/60/120；rt_median 单行异常不拖垮整表；RT_MONITOR_STATIC_VER 20260413-9
# 代码更新时间：2026-04-13 14:01 idx_mins 前9日/前五日均值批量 IN；rt_idx_k 与 index_daily 区间共用连接；MA 信号表逐行容错；RT_MONITOR_STATIC_VER 20260413-10
# 代码更新时间：2026-04-13 14:10 rt_monitor.js：首行四表/BIAS/MA 就地更新单元格+requestAnimationFrame 轮询，减轻刷新闪动；RT_MONITOR_STATIC_VER 20260413-11
# 代码更新时间：2026-04-13 14:31 弹窗接口体验修复：前端请求超时+同类中止，后端 margin_daily/margin_rzmre/up_num 弹窗短 TTL 缓存削峰；RT_MONITOR_STATIC_VER 20260413-12
# 代码更新时间：2026-04-13 15:14 弹窗性能二次修复：limit_stats_detail/top_moves 短TTL缓存+慢请求告警；修复弹窗请求竞态清理；RT_MONITOR_STATIC_VER 20260413-13
# 代码更新时间：2026-04-13 21:28 应收账款周转天数筛选表与方案保存：新增 /api/fina_arturn_days_table、前端筛选区块与 localStorage 方案；RT_MONITOR_STATIC_VER 20260413-23
# 代码更新时间：2026-04-14 06:32 应收账款筛选方案改齿轮弹窗；RT_MONITOR_STATIC_VER 20260413-24
# 代码更新时间：2026-04-14 应收账款周转天表：紧凑表头（列名与筛选钮左对齐小缝）+ 12px 表体 + 申万列省略与 title；RT_MONITOR_STATIC_VER 20260414-1
# 代码更新时间：2026-04-14 应收账款表：报告期迁入齿轮弹窗；列头年份展示；指标名置于四期数列正上方；方案弹窗按钮收窄；RT_MONITOR_STATIC_VER 20260414-2
# 代码更新时间：2026-04-14 应收账款表顶栏：齿轮|数据已更新|当前报告期同一行；RT_MONITOR_STATIC_VER 20260414-3
# 代码更新时间：2026-04-14 筛选方案弹窗：.bias-series-modal.fina-arturn 覆盖 1320px，收窄至 ~288px；RT_MONITOR_STATIC_VER 20260414-4
# 代码更新时间：2026-04-14 应收账款表：每列表头左侧升/降序按钮与排序逻辑；RT_MONITOR_STATIC_VER 20260414-5
# 代码更新时间：2026-04-14 应收账款表：双行表头（周转天/毛利率分组居中）+ 毛利率列仅显示年份；RT_MONITOR_STATIC_VER 20260414-25
# 代码更新时间：2026-04-14 应收账款毛利率 COALESCE(q_gsprofit_margin)+表头单行与顶栏三列栅格对齐；RT_MONITOR_STATIC_VER 20260414-26
# 代码更新时间：2026-04-14 应收账款指标分组回到 thead 双行（colspan 与年份列对齐）；RT_MONITOR_STATIC_VER 20260414-27
# 代码更新时间：2026-04-14 毛利率独立选行 fi_gpm+组间竖线；RT_MONITOR_STATIC_VER 20260414-28
# 代码更新时间：2026-04-14 应收账款 thead：状态条与分组同行、代码列与年份同行+齿轮委托；RT_MONITOR_STATIC_VER 20260414-29
# 代码更新时间：2026-04-14 应收账款表增 ROE（扣非）roe_dt 四列+SQL 参数对齐；RT_MONITOR_STATIC_VER 20260414-30
# 代码更新时间：2026-04-14 19:38 应收账款表新增 ocf_to_opincome/ocf_to_or 两组四期列；RT_MONITOR_STATIC_VER 20260414-33
# 代码更新时间：2026-04-14 应收账款表排序：单列三态按钮（无→升→降→无）；RT_MONITOR_STATIC_VER 20260414-6
# 代码更新时间：2026-04-14 应收账款表：股票代码/名称列去掉排序钮且不参与排序；RT_MONITOR_STATIC_VER 20260414-7
# 代码更新时间：2026-04-14 应收账款顶栏文案缩短：更新/报告期；RT_MONITOR_STATIC_VER 20260414-8
# 代码更新时间：2026-04-14 应收账款：齿轮+状态与指标名同一灰底横条，表头仅列名行；RT_MONITOR_STATIC_VER 20260414-9
# 代码更新时间：2026-04-14 应收账款顶栏：更新仅日期、报告期与条数换行；RT_MONITOR_STATIC_VER 20260414-10
# 代码更新时间：2026-04-14 应收账款顶栏：更新与报告期/条数同一行；RT_MONITOR_STATIC_VER 20260414-11
# 代码更新时间：2026-04-14 08:04 应收账款四期列：数值居中、表头排序区居中、列左右 padding 收紧；RT_MONITOR_STATIC_VER 20260414-12
# 代码更新时间：2026-04-14 08:10 应收账款四期列左右 padding 0.5px；RT_MONITOR_STATIC_VER 20260414-13
# 代码更新时间：2026-04-14 08:11 应收账款四期列左右 padding 0；RT_MONITOR_STATIC_VER 20260414-14
# 代码更新时间：2026-04-14 08:17 应收账款表：代码/名称/申万与首年列之间列距 1px；RT_MONITOR_STATIC_VER 20260414-15
# 代码更新时间：2026-04-14 08:22 应收账款表：max-content 消摊宽假缝、表头 gap 收紧、首列左 inset 4px；RT_MONITOR_STATIC_VER 20260414-16
# 代码更新时间：2026-04-14 08:26 底部 Top30 三表：列距与应收账款同思路 + 申万列省略 title；RT_MONITOR_STATIC_VER 20260414-17
# 代码更新时间：2026-04-14 08:32 Top30：代码|名称|申万 分界单侧 1px 缝，申万列左不再叠 1px；RT_MONITOR_STATIC_VER 20260414-18
# 代码更新时间：2026-04-14 08:36 Top30：代码列右 8px 与名称分隔（--rt-monitor-top-moves-code-name-h-gap）；RT_MONITOR_STATIC_VER 20260414-19
# 代码更新时间：2026-04-14 08:40 Top30：回落回升%列右 8px 与申万分隔（--rt-monitor-top-moves-metric-industry-h-gap）；RT_MONITOR_STATIC_VER 20260414-20
# 代码更新时间：2026-04-14 08:59 涨跌家数表紧凑：12px、行高1.35、padding 收紧；RT_MONITOR_STATIC_VER 20260414-21
# 代码更新时间：2026-04-14 09:01 涨跌家数行高与成交额（亿）median 表对齐；RT_MONITOR_STATIC_VER 20260414-22
# 代码更新时间：2026-04-14 销售毛利率表：fina_indicator.grossprofit_margin 当期+前三年，/api/fina_grossprofit_margin_table；RT_MONITOR_STATIC_VER 20260414-23
# 代码更新时间：2026-04-14 销售毛利率并入应收账款表右侧四列（g0–g3），去掉独立毛利率表与接口；RT_MONITOR_STATIC_VER 20260414-24
# 代码更新时间：2026-04-13 15:35 BIAS/MA 弹窗三次优化：etf_ma_signal_series 增 5s LRU 缓存；启动幂等补齐 ETF 热点索引（mins/realtime）；RT_MONITOR_STATIC_VER 20260413-14
# 代码更新时间：2026-04-13 15:50 BIAS/MCD 弹窗改“快速窗口先出图+全窗口后台补全”；MA 信号表增加启动异步预热，降低首开等待；RT_MONITOR_STATIC_VER 20260413-15
# 代码更新时间：2026-04-13 15:57 BIAS/MCD 弹窗二次修复：快速请求超时不直接失败，自动回退全量重试并放宽专用超时；RT_MONITOR_STATIC_VER 20260413-16
# 代码更新时间：2026-04-13 16:19 前端弹窗提速：BIAS 主表空闲批量预取+首屏耗时埋点；RT_MONITOR_STATIC_VER 20260413-18
# 代码更新时间：2026-04-13 16:30 BIAS 回退修复：禁用主表批量预取；全量超时自动二次重试；放宽专用超时；RT_MONITOR_STATIC_VER 20260413-19
# 代码更新时间：2026-04-13 19:15 MA/EMA 首载卡住修复：/api/etf_ma_signal_table 改为异步刷新+过期快照先回；无缓存时返回基础行情兜底行，避免“数据加载中”长时间悬挂；RT_MONITOR_STATIC_VER 20260413-22
# 代码更新时间：2026-04-14 21:35 应收账款表左侧新增公告日期（ann_date）四期列；RT_MONITOR_STATIC_VER 20260414-35
# 代码更新时间：2026-04-14 22:29 合同负债新增 Chg%（当期/历史8季度均值-1）列并左置于当期；RT_MONITOR_STATIC_VER 20260414-40
# 代码更新时间：2026-04-14 22:32 Chg% 负值显示绿色；RT_MONITOR_STATIC_VER 20260414-41
# 代码更新时间：2026-04-15 09:09 筛选方案弹窗增加「重置筛选」清空列条件并重置排序；RT_MONITOR_STATIC_VER 20260414-42
# 代码更新时间：2026-04-15 09:22 应收账款表左四列横向冻结（sticky+列宽）；RT_MONITOR_STATIC_VER 20260414-43
# 代码更新时间：2026-04-15 10:26 应收账款财务表 SQL：ROW_NUMBER 排序增加 report_type=1（合并报表）次于 update_flag=1、先于 ann_date
# 代码更新时间：2026-04-15 11:41 合同负债（亿）：balancesheet contract_liab + adv_receipts 同报告期再 /1e8
# 代码更新时间：2026-04-15 11:50 修复报告期下拉为空：缺列兼容+接口主表失败仍 200；前端先写 end_dates；RT_MONITOR_STATIC_VER 20260414-44
# 代码更新时间：2026-04-15 12:21 应收占比(TTM)：balancesheet 应收 / income 四单季营业总收入×100；列置于 ROE 左侧；RT_MONITOR_STATIC_VER 20260415-45
# 代码更新时间：2026-04-15 12:26 应收账款表头：分组行「TTM」/ 明细「应收%」互换；RT_MONITOR_STATIC_VER 20260415-46
# 代码更新时间：2026-04-15 12:35 应收账款表 CSS：border-collapse separate + 表体分组竖线与表头对齐；RT_MONITOR_STATIC_VER 20260415-47
# 代码更新时间：2026-04-15 12:49 经营现金流净额/营业收入四列：ocf_to_or 库内小数×100 输出；表头加(%)；RT_MONITOR_STATIC_VER 20260415-48
# 代码更新时间：2026-04-15 12:55 ocf_to_or：去掉 SQL 先 ROUND；×100 保留 4 位；前端 o 列 toFixed(4)；RT_MONITOR_STATIC_VER 20260415-49
# 代码更新时间：2026-04-15 13:05 应收账款表：经营现金流列去尾随 0；表体列间浅竖线；RT_MONITOR_STATIC_VER 20260415-50
# 代码更新时间：2026-04-15 13:02 ocf_to_or：库内四位小数比×100 后接口与页面均保留两位小数；RT_MONITOR_STATIC_VER 20260415-51
# 代码更新时间：2026-04-15 13:09 ocf_to_or：Decimal×100 再 quantize 两位，避免 float 误差；RT_MONITOR_STATIC_VER 20260415-52
# 代码更新时间：2026-04-15 13:27 应收账款表：股票代码/名称列居中；RT_MONITOR_STATIC_VER 20260415-53
# 代码更新时间：2026-04-15 13:46 rt_median 前端：合并上一帧区间/T-60 稳定列，避免轮询闪 --；RT_MONITOR_STATIC_VER 20260415-54
# 代码更新时间：2026-04-15 13:53 应收账款表：股票代码/名称冻结列宽收紧（--fina-freeze-c1/c2）；RT_MONITOR_STATIC_VER 20260415-55
# 代码更新时间：2026-04-15 17:23 应收账款表新增 q_gr_yoy/q_op_yoy 九季度列（置于合同负债左右）；RT_MONITOR_STATIC_VER 20260415-56
# 代码更新时间：2026-04-15 17:48 应收表新增同比列缺列降级：q_gr_yoy/q_op_yoy 不存在时返回空列避免整表失败；RT_MONITOR_STATIC_VER 20260415-56
# 代码更新时间：2026-04-15 20:26 应收表同比列改为主查询后补齐，修复主 SQL 超时导致整表不显示；RT_MONITOR_STATIC_VER 20260415-57
# 代码更新时间：2026-04-15 20:39 应收表分组顺序调整：合同负债与单季度营业利润同比互换位置；RT_MONITOR_STATIC_VER 20260415-58
# 代码更新时间：2026-04-15 21:09 应收表分组再调整：合同负债与营业利润同比列组对调；RT_MONITOR_STATIC_VER 20260415-59
# 代码更新时间：2026-04-15 21:25 应收表新增顶部同步滚动条并隐藏底部滚动条；RT_MONITOR_STATIC_VER 20260415-60
# 代码更新时间：2026-04-15 21:46 应收表容器改为 block 防止被内容撑宽，顶部滚动条可见；RT_MONITOR_STATIC_VER 20260415-61
# 代码更新时间：2026-04-15 22:09 应收表首行（更新/报告期）高度压缩，减少留白；RT_MONITOR_STATIC_VER 20260415-62
# 代码更新时间：2026-04-15 22:34 启动防误杀：自动清理仅终止比当前进程更早启动的 rt_monitor_app 旧进程
# 代码更新时间：2026-04-15 22:36 启动稳定性修复：RT_MONITOR_AUTO_KILL_PREVIOUS 默认关闭，避免并发启动互相误杀
# 代码更新时间：2026-04-26 18:06 单实例互斥体名改用 normcase 路径，修复 bash 小写盘符 vs bat 大写盘符导致两实例并存的 bug
# 代码更新时间：2026-04-15 22:45 图表降级修复：Chart.js 未加载时自动隐藏走势图区块并提示；RT_MONITOR_STATIC_VER 20260415-63
# 代码更新时间：2026-04-15 22:53 走势图区块默认隐藏，仅 Chart.js 可用时显示，彻底避免空白大框；RT_MONITOR_STATIC_VER 20260415-64
# 代码更新时间：2026-04-15 22:59 图表库加载改为本地优先+双 CDN 回退（失败仍降级隐藏），提升稳定显示；RT_MONITOR_STATIC_VER 20260415-65
# 代码更新时间：2026-04-15 23:42 修复空白大框残留：隐藏走势图行改为 display:none!important，覆盖布局脚本写入的 flex!important；RT_MONITOR_STATIC_VER 20260415-66
# 代码更新时间：2026-04-16 06:40 修复走势图整块消失：hideTrendChartRows 不得先于 Chart 判断执行 display:none（否则抵消 show）；RT_MONITOR_STATIC_VER 20260416-1
# 代码更新时间：2026-04-16 06:59 财报筛选独立页 /rt-monitor/fina；主监控移除内嵌应收表仅保留「财报」链接；RT_MONITOR_STATIC_VER 20260416-2
# 代码更新时间：2026-04-16 07:38 财报页去掉返回链接；主监控「财报」改为 target=_blank；RT_MONITOR_STATIC_VER 20260416-3
# 代码更新时间：2026-04-16 两融独立页 /rt-monitor/margin；主监控顶栏「财报|两融」新窗口；内嵌两融双图迁出；RT_MONITOR_STATIC_VER 20260416-4
# 代码更新时间：2026-04-16 08:07 rt-monitor 全页 body 四边留白 12px→3px；RT_MONITOR_STATIC_VER 20260416-5
# 代码更新时间：2026-04-16 11:54 财报表列筛选浮层 left 钳制防左缘裁切；RT_MONITOR_STATIC_VER 20260416-6
# 代码更新时间：2026-04-16 14:15 财报表新增固定资产（亿）列组与 Chg%（同合同负债口径）；RT_MONITOR_STATIC_VER 20260416-7
# 代码更新时间：2026-04-16 14:33 财报表悬停整行高亮改浅冰蓝（含冻结列一致高亮）；RT_MONITOR_STATIC_VER 20260416-8
# 代码更新时间：2026-04-16 14:36 财报表 hover 高亮再加深一档（更醒目）；RT_MONITOR_STATIC_VER 20260416-9
# 代码更新时间：2026-04-16 14:41 财报表上层去“公告日期”，下层改“公告日”，并收紧公告日列宽；RT_MONITOR_STATIC_VER 20260416-10
# 代码更新时间：2026-04-16 14:48 财报表上层恢复 TTM 对齐应收%，并补公告日/应收%分组竖线到顶；RT_MONITOR_STATIC_VER 20260416-11
# 代码更新时间：2026-04-16 14:58 财报表去掉“申万二级行业-公告日”顶部分割竖线；RT_MONITOR_STATIC_VER 20260416-12
# 代码更新时间：2026-04-16 16:43 财报宽表支持鼠标滚轮横向滚动（含 Shift+滚轮）；RT_MONITOR_STATIC_VER 20260416-13
# 代码更新时间：2026-04-16 17:19 财报页记忆报告期/筛选/排序（刷新后保留，重置后清空筛选排序）；RT_MONITOR_STATIC_VER 20260416-14
# 代码更新时间：2026-04-16 17:52 财报表新增“上市日”列（stock_basic.list_date，置于公告日与应收%之间，不冻结）；RT_MONITOR_STATIC_VER 20260416-15
# 代码更新时间：2026-04-16 17:54 修复上市日 DATE 兼容（去空串比较/聚合）；RT_MONITOR_STATIC_VER 20260416-16
# 代码更新时间：2026-04-16 18:35 财报表新增“单季度毛利率/净利率”各9季列（置于合同负债左侧），含后端补齐、前端筛选排序与分隔线；RT_MONITOR_STATIC_VER 20260416-17
# 代码更新时间：2026-04-16 18:39 财报表新增“单季度ROE(扣非)”9季列（q_dt_roe，置于合同负债左侧），含后端补齐与前端筛选排序；RT_MONITOR_STATIC_VER 20260416-18
# 代码更新时间：2026-04-16 23:30 财报方案调用新增“报告期应用方式（当前/方案/手动）+ 记住偏好”，并支持恢复排序；RT_MONITOR_STATIC_VER 20260416-19
# 代码更新时间：2026-04-17 06:30 财报表 TTM 增「营收」列（四单季营业总收入 TTM，亿元）；粗竖线 CSS nth-child 顺延；RT_MONITOR_STATIC_VER 20260417-1
# 代码更新时间：2026-04-17 06:47 财报表：总市值/自由市值/PE（stk_factor_pro+rt_k）；冻结 6 列；RT_MONITOR_STATIC_VER 20260417-2
# 代码更新时间：2026-04-17 07:00 财报表合同负债区 Chg% 右侧增「营收%」（k0/ttm_revenue_yi×100）；RT_MONITOR_STATIC_VER 20260417-3
# 代码更新时间：2026-04-17 09:09 补齐 static/vendor（Chart.js/Hammer/zoom）避免两融页与主监控首屏串行等 CDN；RT_MONITOR_STATIC_VER 20260417-4
# 代码更新时间：2026-04-17 09:25 chart-stack.umd.js 单文件+defer；两融页跳过 median-quad 布局；RT_MONITOR_STATIC_VER 20260417-5
# 代码更新时间：2026-04-17 09:40 财报独立页 rt_monitor.js 改 defer + 跳过 median-quad；RT_MONITOR_STATIC_VER 20260417-6
# 代码更新时间：2026-04-17 09:29 财报逻辑剥离为 static/js/rt_monitor_fina.js，主监控瘦身；RT_MONITOR_STATIC_VER 20260417-7
# 代码更新时间：2026-04-17 11:33 财报页固定资产当期值弹窗：固定资产(左轴)vs单季度营收(右轴)最近25季；RT_MONITOR_STATIC_VER 20260417-8
# 代码更新时间：2026-04-17 11:41 修复固定资产弹窗 bias-series-msg 相对遮罩层铺满导致整页/弹窗被挡；RT_MONITOR_STATIC_VER 20260417-9
# 代码更新时间：2026-04-17 12:05 固定资产序列 API：report_type 缺列时 MySQL1054 自动降级重试，避免 information_schema 误判
# 代码更新时间：2026-04-17 11:47 RT_MONITOR_STATIC_VER 20260417-10：rt_monitor_fina.js 补 tbody 变量修复整页不加载
# 代码更新时间：2026-04-17 12:02 固定资产弹窗序列：利润表累计营收拆单季；弹窗柱状图；RT_MONITOR_STATIC_VER 20260417-11
# 代码更新时间：2026-04-17 12:15 固定资产弹窗去说明文案、单 Y 轴；RT_MONITOR_STATIC_VER 20260417-12
# 代码更新时间：2026-04-17 12:28 固定资产弹窗标题行代码名称+输入切换；API 返回 sec_name；RT_MONITOR_STATIC_VER 20260417-13
# 代码更新时间：2026-04-17 12:20 可 resize 弹窗几何 localStorage；连板明细/固定资产/筛选方案；RT_MONITOR_STATIC_VER 20260417-14
# 代码更新时间：2026-04-17 12:29 固定资产弹窗版式；RT_MONITOR_STATIC_VER 20260417-15
# 代码更新时间：2026-04-17 12:35 表头合并文案+查询行紧凑；RT_MONITOR_STATIC_VER 20260417-16
# 代码更新时间：2026-04-17 12:42 表头与输入同行、移除加载按钮；RT_MONITOR_STATIC_VER 20260417-17
# 代码更新时间：2026-04-17 12:53 固定资产弹窗序列增合同负债(右轴，口径同应收表)+柱顶一位小数；RT_MONITOR_STATIC_VER 20260417-18
# 代码更新时间：2026-04-17 12:57 固定资产弹窗表头去「股票代码」标签、居中「输入框+名称+主题」不重复代码；RT_MONITOR_STATIC_VER 20260417-19
# 代码更新时间：2026-04-17 13:05 固定资产弹窗：合同负债改为折线+右轴，柱仅固定资产与单季营收；RT_MONITOR_STATIC_VER 20260417-20
# 代码更新时间：2026-04-17 13:18 固定资产弹窗：TTM 营收+PE（stk 最新日）+锚定期单季同比/毛利率/净利率/ROE(扣非)；RT_MONITOR_STATIC_VER 20260417-21
# 代码更新时间：2026-04-17 13:22 固定资产弹窗指标条：指定项负值绿色（与 fina-arturn-neg）；RT_MONITOR_STATIC_VER 20260417-22
# 代码更新时间：2026-04-17 13:33 财报表首屏 boot 提示、load try/catch；RT_MONITOR_STATIC_VER 20260417-23
# 代码更新时间：2026-04-17 13:35 固定资产弹窗指标条单行+与表头同字号字重；RT_MONITOR_STATIC_VER 20260417-24
# 代码更新时间：2026-04-17 13:40 固定资产弹窗指标条白盒内横向居中；RT_MONITOR_STATIC_VER 20260417-25
# 代码更新时间：2026-04-17 13:42 固定资产弹窗指标条：「当期报告期」改为「报告期」；RT_MONITOR_STATIC_VER 20260417-26
# 代码更新时间：2026-04-17 13:48 固定资产弹窗：按当前表顺序「上一个/下一个」切换；RT_MONITOR_STATIC_VER 20260417-27
# 代码更新时间：2026-04-17 13:57 固定资产指标条阈值高亮+ROE去百分号；RT_MONITOR_STATIC_VER 20260417-28
# 代码更新时间：2026-04-17 14:03 单季度营收第5根柱（右起）高亮；RT_MONITOR_STATIC_VER 20260417-29
# 代码更新时间：2026-04-17 14:05 第5根营收柱高亮作用域修复（右起计数）；RT_MONITOR_STATIC_VER 20260417-30
# 代码更新时间：2026-04-17 14:10 固定资产指标条追加行业与市值字段（口径同财报表）；RT_MONITOR_STATIC_VER 20260417-31
# 代码更新时间：2026-04-17 14:27 固定资产指标条字段值统一蓝色（标签黑色）；RT_MONITOR_STATIC_VER 20260417-32
# 代码更新时间：2026-04-17 20:42 固定资产弹窗追加主营构成Top3（收入/成本/利润及占比）；RT_MONITOR_STATIC_VER 20260417-33
# 代码更新时间：2026-04-17 21:00 固定资产弹窗主营构成新增行业(I)/地区(D) Top3；RT_MONITOR_STATIC_VER 20260417-34
# 代码更新时间：2026-04-17 21:06 主营构成表金额与占比去单位后缀（亿/%）；RT_MONITOR_STATIC_VER 20260417-35
# 代码更新时间：2026-04-17 21:17 主营构成表头改含单位并新增毛利率%列；RT_MONITOR_STATIC_VER 20260417-36
# 代码更新时间：2026-04-17 21:35 主营构成表在产品区块上方显示报告期；RT_MONITOR_STATIC_VER 20260417-37
# 代码更新时间：2026-04-17 21:42 报告期并入表头同一行并合并前两列；RT_MONITOR_STATIC_VER 20260417-38
# 代码更新时间：2026-04-17 22:00 主营构成取数改为<=请求日的最新年报期(12-31)；RT_MONITOR_STATIC_VER 20260417-39
# 代码更新时间：2026-04-17 22:21 主营构成新增次新年报并排展示（右侧），后端返回 current/previous 两套年报Top3；RT_MONITOR_STATIC_VER 20260417-40
# 代码更新时间：2026-04-17 22:28 主营构成双表排版优化（卡片化、间距、斑马纹、数字对齐、报告期高亮）；RT_MONITOR_STATIC_VER 20260417-41
# 代码更新时间：2026-04-17 22:32 回退主营构成双表排版优化，恢复上一版紧凑样式；RT_MONITOR_STATIC_VER 20260417-42
# 代码更新时间：2026-04-17 22:38 主营构成表列宽微调：主营构成列加宽并居中，产品/序号列收紧；RT_MONITOR_STATIC_VER 20260417-43
# 代码更新时间：2026-04-17 22:47 回退主营构成列宽微调（恢复左对齐与默认列宽）；RT_MONITOR_STATIC_VER 20260417-44
# 代码更新时间：2026-04-17 23:02 主营构成值改居中；双年报中间新增收入同比/利润同比/毛利变化（同名项对比，正红负绿）；RT_MONITOR_STATIC_VER 20260417-45
# 代码更新时间：2026-04-17 23:08 中间对照表去重：不再输出分类/名次/主营构成，仅保留同比与毛利变化三列并按最新年报行顺序对齐；RT_MONITOR_STATIC_VER 20260417-46
# 代码更新时间：2026-04-17 23:12 对照表列宽优化：三栏比例收紧中列，收入同比/利润同比/毛利变化强制单行并等宽列；RT_MONITOR_STATIC_VER 20260417-47
# 代码更新时间：2026-04-17 23:18 主营构成表头去“报告期”前缀，仅显示日期；收紧名次列并加宽主营构成列；RT_MONITOR_STATIC_VER 20260417-48
# 代码更新时间：2026-04-17 23:21 修复主营构成列挤压竖排：主表最小宽度上调、主营构成列加宽并禁止断字；RT_MONITOR_STATIC_VER 20260417-49
# 代码更新时间：2026-04-17 23:23 主营构成双表改用 colgroup 固定列宽（分类/序号/主营构成），避免 nth-child + colspan 导致错位挤压；RT_MONITOR_STATIC_VER 20260417-50
# 代码更新时间：2026-04-17 23:27 主营构成双表全列强制不换行（表头+数据），并进一步收紧分类/序号列、加宽主营构成列；RT_MONITOR_STATIC_VER 20260417-51
# 代码更新时间：2026-04-17 23:28 中间对照表进一步收紧（列宽/内边距），并统一 tbody 行高与主表逐行对齐；RT_MONITOR_STATIC_VER 20260417-52
# 代码更新时间：2026-04-17 23:31 同比三列并入最新年报表（移除中间比较表），保持右侧次新年报独立；主表新增 mainbz-has-compare 列宽策略；RT_MONITOR_STATIC_VER 20260417-53
# 代码更新时间：2026-04-17 23:34 修复双表混叠：改为 max-content 双列并给每表独立滚动容器，禁止跨列覆盖；RT_MONITOR_STATIC_VER 20260417-54
# 代码更新时间：2026-04-17 23:50 财报主表新增“单季度营业收入（亿）”9季列（s0..s8），并置于单季度营收同比增速旁；补齐后端 income 累计转单季与前端筛选排序渲染；RT_MONITOR_STATIC_VER 20260417-55
# 代码更新时间：2026-04-18 13:15 财报主查询 INNER JOIN stock_wind.rt_k，剔除无实时行情的退市/无效代码；RT_MONITOR_STATIC_VER 20260418-1
# 代码更新时间：2026-04-18 13:44 财报表 .fina-arturn-table-wrap overflow-y:clip 修复视口纵向 sticky；RT_MONITOR_STATIC_VER 20260418-2
# 代码更新时间：2026-04-18 13:51 财报表横向改视口滚动+JS 同步顶栏；去掉 wrap overflow-x；RT_MONITOR_STATIC_VER 20260418-3
# 代码更新时间：2026-04-18 14:03 财报表顶栏横向滚动条与 thead 留白收紧；RT_MONITOR_STATIC_VER 20260418-4
# 代码更新时间：2026-04-18 14:15 财报表移除顶部横向滚动条（保留视口横滚与滚轮）；RT_MONITOR_STATIC_VER 20260418-5
# 代码更新时间：2026-04-18 14:10 财报表斑马纹/分组竖线/卡片留白布局收紧；RT_MONITOR_STATIC_VER 20260418-6
# 代码更新时间：2026-04-18 14:14 应收表 thead 单色+去冻结区投影，减轻「两图层」观感；RT_MONITOR_STATIC_VER 20260418-7
# 代码更新时间：2026-04-18 14:19 应收表头恢复 #fafafa、消顶缝与 thead 投影；冻结 tbody 阴影改细线；RT_MONITOR_STATIC_VER 20260418-8
# 代码更新时间：2026-04-18 14:22 财报单页 body 顶留白 0；第一行表头压缩（齿轮/分组行 padding）；RT_MONITOR_STATIC_VER 20260418-9
# 代码更新时间：2026-04-18 14:25 财报表第一行再压缩 1px（齿轮 23px）；RT_MONITOR_STATIC_VER 20260418-10
# 代码更新时间：2026-04-18 14:27 财报表第一行再压缩 1px（齿轮 22px）；RT_MONITOR_STATIC_VER 20260418-11
# 代码更新时间：2026-04-18 14:30 财报表头方案入口改为「配置」文字+紧凑样式；RT_MONITOR_STATIC_VER 20260418-12
# 代码更新时间：2026-04-18 15:32 财报表卡片底部内边距置 0，去掉表下白托底叠层；RT_MONITOR_STATIC_VER 20260418-13
# 代码更新时间：2026-04-18 15:39 财报单页 html 滚动条与灰底统一；应收表末行底边与 overflow；RT_MONITOR_STATIC_VER 20260418-14
# 代码更新时间：2026-04-18 15:53 单季营收补齐：批量 income 键含拆 Q2–Q4 所需的上一季末（修复最旧列如 23Q4 全空）；RT_MONITOR_STATIC_VER 20260418-15
# 代码更新时间：2026-04-18 16:05 固定资产弹窗股票代码支持仅输入 6 位自动补 .SH/.SZ/.BJ；RT_MONITOR_STATIC_VER 20260418-16
# 代码更新时间：2026-04-18 16:25 固定资产代码：全角数字归一、沪 60xxxx 号段、Enter 事件捕获委托；RT_MONITOR_STATIC_VER 20260418-17
# 代码更新时间：2026-04-18 16:40 固定资产代码：带后缀时按号段纠正错误交易所（如 600000.SZ→.SH）；RT_MONITOR_STATIC_VER 20260418-18
# 代码更新时间：2026-04-18 16:59 财报表列筛选弹窗改 position:fixed+视口坐标，修复横滚与 sticky 表头下错位；RT_MONITOR_STATIC_VER 20260418-19
# 代码更新时间：2026-04-18 19:07 单季营收区新增「新高」列（s_high）：最新季为展示 9 季中最大则为 1；RT_MONITOR_STATIC_VER 20260418-20
# 代码更新时间：2026-04-18 19:12 新高 s_high：仅要求 s0 非空，与已有值的 s0..s8 比最大；RT_MONITOR_STATIC_VER 20260418-21
# 代码更新时间：2026-04-18 19:19 应收表分组粗竖线 nth-child 随「新高」列后移（41/50/…/97）；RT_MONITOR_STATIC_VER 20260418-22
# 代码更新时间：2026-04-18 19:23 应收表分组竖线改 2px+#6d7d8b，列间仍 1px 浅线；CSS 变量便于微调；RT_MONITOR_STATIC_VER 20260418-23
# 代码更新时间：2026-04-18 19:28 应收表 thead 第一行/第二行底横线与分组竖线同风格；RT_MONITOR_STATIC_VER 20260418-24
# 代码更新时间：2026-04-18 19:34 应收表第二行表头分组粗竖线：选择器补 .median-table 提高特异性，避免被 n+2 浅线覆盖；RT_MONITOR_STATIC_VER 20260418-25
# 代码更新时间：2026-04-18 19:38 应收表第二行浅竖线从分组起列拆分：thead 用 :not(8,11,…,97)，第一行粗竖线与列名行、表体对齐；RT_MONITOR_STATIC_VER 20260418-26
# 代码更新时间：2026-04-18 19:42 应收表第一行分组横线：metric 格压过 .median-table th 1px 底边，与右侧分组同为 2px；RT_MONITOR_STATIC_VER 20260418-27
# 代码更新时间：2026-04-18 19:59 合同负债区新增「新高」k_high（逻辑同 s_high，字段 k0..k8）；RT_MONITOR_STATIC_VER 20260418-28
# 代码更新时间：2026-04-18 20:09 财报表第一行空档拆为两列（公告日/上市日），与横向冻结至公告日及分组竖线对齐；RT_MONITOR_STATIC_VER 20260418-29
# 代码更新时间：2026-04-18 20:18 应收表公告日|上市日分界改为与其它大组一致的 2px 分组竖线（thead-group/第二行/表体第 7 列）；RT_MONITOR_STATIC_VER 20260418-30
# 代码更新时间：2026-04-18 20:26 应收表 CSS：撤回 tbody 上 :not(7)（避免浅线特异性压过全表粗线）；thead 浅线仍 :not(7)；RT_MONITOR_STATIC_VER 20260418-31
# 代码更新时间：2026-04-18 20:35 应收表：公告日|上市日 2px 分界改第 6 列 border-right（sticky），横滚时保持；表体第 7 列去左线防叠线；RT_MONITOR_STATIC_VER 20260418-32
# 代码更新时间：2026-04-18 21:23 固定资产主营构成：fina_mainbz 同名分项聚合；item_metrics 增 cost_yi 与合计；前端按收入 Top3 名称统一展示与同比；RT_MONITOR_STATIC_VER 20260418-33
# 代码更新时间：2026-04-18 21:27 主营构成表：成本/利润/占比不再按 Top3 行序与收入行交叉回填；同比仍仅同名分项；RT_MONITOR_STATIC_VER 20260418-34
# 代码更新时间：2026-04-18 21:31 主营构成同比：注释明确 P/I/D 内按 bz_item 名对齐、与两期排序无关；RT_MONITOR_STATIC_VER 20260418-35
# 代码更新时间：2026-04-19 财报表：单季经营活动净收益（亿）oi0..oi8+fina_indicator.q_opincome+oi_high；RT_MONITOR_STATIC_VER 20260419-1
# 代码更新时间：2026-04-19 财报表：单季扣非净利润（亿）dtp0..dtp8+q_dtprofit+dtp_high；RT_MONITOR_STATIC_VER 20260419-2
# 代码更新时间：2026-04-19 财报表：单季 ocs/oco 九列（q_ocf_to_sales、q_ocf_to_or 百分数）；RT_MONITOR_STATIC_VER 20260419-3
# 代码更新时间：2026-04-19 财报表 front：表头/表体/筛选/排序/145 列竖线/空表 colspan 接 ocs·oco；RT_MONITOR_STATIC_VER 20260419-4
# 代码更新时间：2026-04-19 单季 ocs/oco：q_ocf_to_sales、q_ocf_to_or 直接取库值，不再×100；RT_MONITOR_STATIC_VER 20260419-5
# 代码更新时间：2026-04-19 单季 OCF/营收% 增 ocs_high（同 dtp_high）；RT_MONITOR_STATIC_VER 20260419-6
# 代码更新时间：2026-04-19 单季 OCF/营收% 增 ocs_wqcv（加权变异系数，最新季指数权重）；RT_MONITOR_STATIC_VER 20260419-7
# 代码更新时间：2026-04-19 财报表增单季经营现金流净额（亿）cf0..cf8（cashflow.n_cashflow_act 累计拆解）；RT_MONITOR_STATIC_VER 20260419-8
# 代码更新时间：2026-04-19 单季经营现金流净额区增 cf_high（同 s_high）；RT_MONITOR_STATIC_VER 20260419-9
# 代码更新时间：2026-04-19 财报宽表固定资产组分界线 CSS：粗线 nth-child 应为 fchg 首列 148（勿用 f0=149）；RT_MONITOR_STATIC_VER 20260419-10
# 代码更新时间：2026-04-19 固定资产弹窗双轴图：顶部居中图例+layout 上内边距；RT_MONITOR_STATIC_VER 20260419-11
# 代码更新时间：2026-04-19 固定资产图：对照季改金边不误导读、折线数值下移、图例加大；RT_MONITOR_STATIC_VER 20260419-12
# 代码更新时间：2026-04-19 固定资产双轴图样式回退（对照季仍黄底、折线标注回点上）；RT_MONITOR_STATIC_VER 20260419-13
# 代码更新时间：2026-04-19 固定资产双轴图 layout.padding.top=0，图例上方不留白；RT_MONITOR_STATIC_VER 20260419-14
# 代码更新时间：2026-04-19 11:19 固定资产图：图例 padding/labels 收紧+#fina-fixed-assets-chart-wrap 内边距收紧；RT_MONITOR_STATIC_VER 20260419-15
# 代码更新时间：2026-04-19 11:23 固定资产对照季柱：浅黄改为 Amber600 底+深橙边，柱顶数字加深紫；RT_MONITOR_STATIC_VER 20260419-16
# 代码更新时间：2026-04-19 11:26 固定资产图：合同负债线加粗加色+加大拐点；柱/线数值白描边 11px 防重叠区读不清；RT_MONITOR_STATIC_VER 20260419-17
# 代码更新时间：2026-04-19 11:29 固定资产合同负债：beforeDatasetDraw 铺半透明宽描边「发光」底层；RT_MONITOR_STATIC_VER 20260419-18
# 代码更新时间：2026-04-19 11:33 固定资产双轴图统一配色 faPal（蓝/紫/青对照/橙折线），发光减弱；RT_MONITOR_STATIC_VER 20260419-19
# 代码更新时间：2026-04-19 11:46 /api/fina_fixed_assets_revenue_series 增单季现金流/经营净收益/毛利率/扣非ROE/现金流营收比；弹窗第二张双轴图；RT_MONITOR_STATIC_VER 20260419-20
# 代码更新时间：2026-04-19 11:56 固定资产双图：统一图例字体与共享色板（营收紫一致、负债玫红、橙专用于现金流/营收%）；RT_MONITOR_STATIC_VER 20260419-21
# 代码更新时间：2026-04-19 11:58 静态资源：固定资产图例去「单季度」；RT_MONITOR_STATIC_VER 20260419-22
# 代码更新时间：2026-04-19 12:52 固定资产弹窗第二张图下增加稿用宽表（列=季度、行=五项指标）；RT_MONITOR_STATIC_VER 20260419-23
# 代码更新时间：2026-04-19 13:03 稿表增现金流/营收%行、行内 min/max 着色、全表居中；RT_MONITOR_STATIC_VER 20260419-24
# 代码更新时间：2026-04-19 13:05 稿表极值配色对调（max 玫红 / min 青绿）；RT_MONITOR_STATIC_VER 20260419-25
# 代码更新时间：2026-04-19 13:10 固定资产弹窗去掉第二张组合图，仅保留下方经营数据宽表；RT_MONITOR_STATIC_VER 20260419-26
# 代码更新时间：2026-04-19 13:16 经营宽表每行标注中位数（琥珀黄底）；RT_MONITOR_STATIC_VER 20260419-27
# 代码更新时间：2026-04-19 13:19 经营宽表负值翡翠绿字，与既有三色底协调；RT_MONITOR_STATIC_VER 20260419-28
# 代码更新时间：2026-04-19 13:26 经营宽表前三行亿元口径、主营构成收入/成本/利润列统一一位小数；RT_MONITOR_STATIC_VER 20260419-29
# 代码更新时间：2026-04-19 13:27 固定资产弹窗指标条：营收TTM、总市值、自由流通市值 formatFinaArturnValue(,1)；RT_MONITOR_STATIC_VER 20260419-30
# 代码更新时间：2026-04-19 13:28 财报表主表总市值/自由市值/营收TTM列 formatFinaArturnValue(,1)；RT_MONITOR_STATIC_VER 20260419-31
# 代码更新时间：2026-04-19 13:43 财报表 TTM 下 PE/应收% 一位小数；p0-p3（应收账款周转天·四年）一位小数；RT_MONITOR_STATIC_VER 20260419-32
# 代码更新时间：2026-04-19 13:46 固定资产稿表 renderFinaFixedAssetsOperatingDataTable：极值与中位数各行仅标一列（并列取最左）；RT_MONITOR_STATIC_VER 20260419-33
# 代码更新时间：2026-04-19 15:10 固定资产序列行增 q_dtprofit_yi·q_netprofit_margin（fina_indicator 与财报表单季列同源）；稿表增扣非净利/净利率两行；RT_MONITOR_STATIC_VER 20260419-34
# 代码更新时间：2026-04-19 15:29 固定资产主营构成：序号 1 行浅黄底（mainbz-rank-1-row）；RT_MONITOR_STATIC_VER 20260419-35
# 代码更新时间：2026-04-19 15:34 主营构成序号1：P/I/D 三色底、类别列不着色；RT_MONITOR_STATIC_VER 20260419-36
# 代码更新时间：2026-04-19 15:58 固定资产稿表：q_ocf_to_or_pct 行改名「现金转化率（%）」并置底；RT_MONITOR_STATIC_VER 20260419-37
# 代码更新时间：2026-04-19 16:06 固定资产稿表「指标」列移至最右、sticky right；RT_MONITOR_STATIC_VER 20260419-38
# 代码更新时间：2026-04-19 16:20 _fina_fixed_assets_batch_fi_quarters 增 q_gr_yoy/q_op_yoy；稿表末两行营收·营业利润同比；RT_MONITOR_STATIC_VER 20260419-39
# 代码更新时间：2026-04-19 17:11 稿表同比行>30%：.fina-fa-opdata-yoy-over-30 浅绿强调；RT_MONITOR_STATIC_VER 20260419-40
# 代码更新时间：2026-04-19 17:15 稿表同比>30% 改浅蓝 #e3f2fd（与主营构成产品·序号1 一致）；RT_MONITOR_STATIC_VER 20260419-41
# 代码更新时间：2026-04-19 17:20 稿表 ROE(扣非)>4：.fina-fa-opdata-roep-over-4 浅黄 #fef3e2；RT_MONITOR_STATIC_VER 20260419-42
# 代码更新时间：2026-04-19 17:27 经营稿表极值三色与同比>30/ROE>4 共存（JS 非极值才加强调；CSS 极值双类）；RT_MONITOR_STATIC_VER 20260419-43
# 代码更新时间：2026-04-19 18:08 财报表 ocs/oco 列筛选弹窗标题去掉库字段说明；RT_MONITOR_STATIC_VER 20260419-44
# 代码更新时间：2026-04-19 18:39 固定资产弹窗遮罩透明（财报页后主表可读）；RT_MONITOR_STATIC_VER 20260419-45
# 代码更新时间：2026-04-19 18:46 固定资产遮罩穿透点击主表（pointer-events），弹窗内仍可操作；RT_MONITOR_STATIC_VER 20260419-46
# 代码更新时间：2026-04-19 20:00 财报表「配置」弹窗：去除北交所、去除 ST，存 viewState 并参与行筛选；RT_MONITOR_STATIC_VER 20260419-47
# 代码更新时间：2026-04-19 20:09 筛选方案遮罩 CSS 与固定资产弹窗一致（透明、穿透）；RT_MONITOR_STATIC_VER 20260419-48
# 代码更新时间：2026-04-19 20:58 财报表申万行业列：or 分隔多关键词 OR 匹配；RT_MONITOR_STATIC_VER 20260419-49
# 代码更新时间：2026-04-19 23:22 固定资产弹窗经营表增股东户数：stk_holdernumber.holder_num 按 end_date；RT_MONITOR_STATIC_VER 20260419-50
# 代码更新时间：2026-04-19 23:30 经营宽表「股东户数」行置底（仅 JS 行序）；RT_MONITOR_STATIC_VER 20260419-51
# 代码更新时间：2026-04-20 09:47 行情监控主页弹窗遮罩透明（rt_monitor.css body.rt-monitor-main），与财报页一致；RT_MONITOR_STATIC_VER 20260420-1
# 代码更新时间：2026-04-20 10:57 固定资产弹窗经营表「股东户数」下增「应收%」：balancesheet(accounts_receiv+oth_receiv)/单季营收；RT_MONITOR_STATIC_VER 20260420-2
# 代码更新时间：2026-04-20 10:59 recv_pct：核实 BS 为元、单季营收为亿元，计算处注释；RT_MONITOR_STATIC_VER 20260420-2
# 代码更新时间：2026-04-20 11:29 固定资产经营宽表股东户数：前端显示户/10000、两位小数，行头「股东户数(万户)」；RT_MONITOR_STATIC_VER 20260420-3
# 代码更新时间：2026-04-21 16:27 固定资产弹窗新增在建工程柱（balancesheet.cip_total/1e8），后端返回 cip_yi 并前端三柱联动显示
# 代码更新时间：2026-04-21 16:44 修复固定资产弹窗 SQL 拼接异常（unary + / 多余逗号），恢复 /api/fina_fixed_assets_revenue_series 正常返回
# 代码更新时间：2026-04-21 16:50 修复在建工程值透传链路（累计营收转单季函数补充 cip_yi），前端柱图可见
# 代码更新时间：2026-04-23 07:56 RT_MONITOR_STATIC_VER 20260423-1：固定资产弹窗顶栏「速览」+CSS 收紧表头与快照间距 1px
# 代码更新时间：2026-04-23 08:02 RT_MONITOR_STATIC_VER 20260423-2：经营宽表行序（JS renderFinaFixedAssetsOperatingDataTable specs）
# 代码更新时间：2026-04-23 08:10 RT_MONITOR_STATIC_VER 20260423-3：固定资产弹窗区块竖向间距 1px（rt_monitor.css）
# 代码更新时间：2026-04-23 08:18 RT_MONITOR_STATIC_VER 20260423-4：固定资产稿表「业务逻辑」列+模块A合并（rt_monitor_fina.js / rt_monitor.css）
# 代码更新时间：2026-04-23 08:23 RT_MONITOR_STATIC_VER 20260423-5：经营宽表前三行去掉「模块A·」前缀（rt_monitor_fina.js）
# 代码更新时间：2026-04-23 08:33 财报表后端：rt_k 代码缓存+快路径 WHERE IN；快路径 fi/bs collect 并行；wall≥4s WARNING；RT_MONITOR_STATIC_VER 20260423-7
# 代码更新时间：2026-04-23 08:36 rt_monitor_fina.js JSDoc「FCF*/」误闭合注释致整脚本失败；静态资源 RT_MONITOR_STATIC_VER 20260423-8
# 代码更新时间：2026-04-23 08:37 rt_monitor_fina.js 经营宽表 JSDoc 四行文案改为直接使用 FCF；RT_MONITOR_STATIC_VER 20260423-9
# 代码更新时间：2026-04-23 08:42 固定资产经营宽表「现金创造能力」四行粗线框（rt_monitor.css / rt_monitor_fina.js）；RT_MONITOR_STATIC_VER 20260423-10
# 代码更新时间：2026-04-23 09:11 经营宽表模块B/C 业务逻辑合并+黑色外框；RT_MONITOR_STATIC_VER 20260423-11
# 代码更新时间：2026-04-23 RT_MONITOR_STATIC_VER 20260423-12（静态资源缓存版本号对齐：经营宽表去掉指标列模块B/C前缀）
# 代码更新时间：2026-04-23 RT_MONITOR_STATIC_VER 20260423-13（经营宽表 ROIC 行指标列文案改为「ROIC」）
# 代码更新时间：2026-04-23 RT_MONITOR_STATIC_VER 20260423-14（经营宽表未分类行：业务逻辑五组+应收% 并入规模.损益+行序）
# 代码更新时间：2026-04-23 RT_MONITOR_STATIC_VER 20260423-15（应收占收入比并入营运资本·周转天数；规模.损益三行）
# 代码更新时间：2026-04-23 RT_MONITOR_STATIC_VER 20260423-16（经营宽表 CCC 行更名为「现金循环天数（天）」）
# 代码更新时间：2026-04-23 RT_MONITOR_STATIC_VER 20260423-17（经营宽表「应收占收入比」置于现金循环天数下一行）
# 代码更新时间：2026-04-23 RT_MONITOR_STATIC_VER 20260423-18（经营宽表「自由现金流FCF（亿）」去掉星号）
# 代码更新时间：2026-04-23 RT_MONITOR_STATIC_VER 20260423-19（经营宽表着色说明 P0 + 指标列字号 P1）
# 代码更新时间：2026-04-23 RT_MONITOR_STATIC_VER 20260423-20（去掉经营宽表顶部着色说明，保留指标列 12px/700）
# 代码更新时间：2026-04-23 RT_MONITOR_STATIC_VER 20260423-22（固定资产弹窗：顶栏快照上边距=1px，图表与底部经营表间距=1px）
# 代码更新时间：2026-04-23 RT_MONITOR_STATIC_VER 20260423-23（固定资产弹窗三处区块缝隙统一 1px）
# 代码更新时间：2026-04-23 RT_MONITOR_STATIC_VER 20260423-24（经营宽表按业务逻辑分组粗框恢复）
# 代码更新时间：2026-04-23 RT_MONITOR_STATIC_VER 20260423-25（固定资产弹窗内容区底部留白=1px）
# 代码更新时间：2026-04-23 RT_MONITOR_STATIC_VER 20260423-26（筛选弹窗打开卡顿优化 + 公告日/上市日日期筛选兼容 yyyy-mm-dd）
# 代码更新时间：2026-04-23 RT_MONITOR_STATIC_VER 20260423-27（经营宽表净债务/净现金负值=净现金，红色强调与顶栏一致）
# 代码更新时间：2026-04-23 RT_MONITOR_STATIC_VER 20260423-21（固定资产弹窗：主营行高+无纵裁切+分区间距+图表限高）
# 代码更新时间：2026-04-24 07:50 RT_MONITOR_STATIC_VER 20260423-49（经营宽表应收/存货天数相对行均值浅红低·浅绿高）
# 代码更新时间：2026-04-24 07:59 RT_MONITOR_STATIC_VER 20260423-50（应收/存货保留 max/min/median；均值色仅非极值格且底色更浅）
# 代码更新时间：2026-04-24 08:09 RT_MONITOR_STATIC_VER 20260423-51（均值偏离格字色黑色，背景不变）
# 代码更新时间：2026-04-24 RT_MONITOR_STATIC_VER 20260423-52（经营宽表季度列表头+数值格统一黑字 #111111；指标/业务逻辑不变）
# 代码更新时间：2026-04-24 RT_MONITOR_STATIC_VER 20260423-53（经营宽表应收占收入比：越小越好，极值/相对均值逻辑与应收天数一致）
# 代码更新时间：2026-04-24 RT_MONITOR_STATIC_VER 20260423-54（经营宽表 CSS：撤销数值格 color !important，恢复负值翡翠绿与 CCC/净债务红）
# 代码更新时间：2026-04-24 RT_MONITOR_STATIC_VER 20260423-55（经营宽表应收/存货/应收占收入比：浅色偏离改相对行中位数）
# 代码更新时间：2026-04-24 16:16
# 代码更新时间：2026-04-24 16:21
# 代码更新时间：2026-04-24 16:27
# 代码更新时间：2026-04-25 07:41
# 代码更新时间：2026-04-25 08:06
# 代码更新时间：2026-04-25 11:02 RT_MONITOR_STATIC_VER 20260425-3（现金转化率 UI 不带 % 符号）
# 代码更新时间：2026-04-25 13:37 RT_MONITOR_STATIC_VER 20260425-4（现金创造能力弹窗可拖拽+resize+布局记忆）
# 代码更新时间：2026-04-25 13:45 RT_MONITOR_STATIC_VER 20260425-5（现金图 FCF 负柱翡翠绿/正柱紫+标签色同步）
# 代码更新时间：2026-04-25 14:15 RT_MONITOR_STATIC_VER 20260425-6（现金创造图 B 档：OCF/资本开支负柱+零轴+转化点分色）
# 代码更新时间：2026-04-25 14:31 RT_MONITOR_STATIC_VER 20260425-7（固定资产弹窗指标条第二行：上市时间，stock_basic.list_date）
# 代码更新时间：2026-04-25 14:53 RT_MONITOR_STATIC_VER 20260425-8（上市时间后追加「已上市X年X个月」前端计算）
# 代码更新时间：2026-04-25 15:06 RT_MONITOR_STATIC_VER 20260425-9（营运资本周转天数二级弹窗：4线+右轴应收占收入比柱）
# 代码更新时间：2026-04-25 15:23 RT_MONITOR_STATIC_VER 20260425-10（营运资本图优化：应收/应付高对比配色+图上数值标签）
# 代码更新时间：2026-04-25 15:27 RT_MONITOR_STATIC_VER 20260425-11（营运资本图：应收占收入比柱状图轻量化，避免压过折线）
# 代码更新时间：2026-04-25 15:53 RT_MONITOR_STATIC_VER 20260425-12（固定资产弹窗证券代码输入框仅显示6位，无 .SH/.SZ/.BJ）
# 代码更新时间：2026-04-25 17:12 RT_MONITOR_STATIC_VER 20260425-13（固定资产弹窗新增「资本效率」二级弹窗：ROIC+ROE(扣非) 25季双折线）
# 代码更新时间：2026-04-25 17:18 RT_MONITOR_STATIC_VER 20260425-14（资本效率图首尾报告期数值标签强制显示）
# 代码更新时间：2026-04-25 17:42 RT_MONITOR_STATIC_VER 20260425-15（资本效率图标签防重叠升级+负值线/点/标签分色）
# 代码更新时间：2026-04-25 18:20 RT_MONITOR_STATIC_VER 20260425-16（净债务/净现金展示优先读取 fina_quarter_metrics.netdebt_opt，回退 netdebt）
# 代码更新时间：2026-04-25 19:13（修复运行时新增 netdebt_opt 后列缓存陈旧，固定资产弹窗净债务取值可即时命中新列）
# 代码更新时间：2026-04-25 20:41 RT_MONITOR_STATIC_VER 20260425-17（财报表新增市值/货币列：money_cap_opt/总市值×100，置于上市日右侧且不归入TTM）
# 代码更新时间：2026-04-25 20:45（修复市值/货币取值：fina_quarter_metrics.end_date 按 DATE_FORMAT 匹配报告期；money_cap_opt 元→亿后再与总市值相除）
# 代码更新时间：2026-04-26 12:17 RT_MONITOR_STATIC_VER 20260426-1（财报表新增存货（亿）12列：Chg%/营收%/新高+9季，位于合同负债左侧，口径对齐合同负债）
# 代码更新时间：2026-04-26 12:40 RT_MONITOR_STATIC_VER 20260426-2（固定资产弹窗主图新增存货（亿）柱状图，取 balancesheet.inventories 口径并优化柱宽/配色）
# 代码更新时间：2026-04-26 13:47 RT_MONITOR_STATIC_VER 20260426-3（固定资产弹窗主图四柱统一宽度；标签逐列避让与兜底堆叠，尽量不漏标且不重叠）
# 代码更新时间：2026-04-26 14:24 RT_MONITOR_STATIC_VER 20260426-4（固定资产弹窗成长性新增归母净利同比/环比，取 fina_indicator.q_netprofit_yoy/q_netprofit_qoq）
# 代码更新时间：2026-04-26 14:46 RT_MONITOR_STATIC_VER 20260426-5（固定资产弹窗新增成长性指标配置按钮，localStorage 保存六项同比/环比显示选择）
# 代码更新时间：2026-04-26 16:29 RT_MONITOR_STATIC_VER 20260426-6（固定资产序列接口返回 stock_basic.act_name、act_ent_type；前端申万行业文案）
# 代码更新时间：2026-04-26 16:40 RT_MONITOR_STATIC_VER 20260426-7（stock_basic 实控人字段改直查四列+1054 回退，避免 information_schema 误判缺列）
# 代码更新时间：2026-04-26 16:51 RT_MONITOR_STATIC_VER 20260426-8（/api/fina_fixed_assets_revenue_series 加入 no-store，防路径级缓存旧 JSON 无 act_*）
# 代码更新时间：2026-04-26 17:05 RT_MONITOR_STATIC_VER 20260426-9（no-store 按 path 后缀匹配反代前缀；固定资产 fetch 加 _cb；成功即清加载遮罩）
# 代码更新时间：2026-04-26 17:18 RT_MONITOR_STATIC_VER 20260426-10（财报表行批量挂 stock_basic.act_name/act_ent_type；缓存键 sb_act_1；弹窗 JS 回退读 arturnRow）
# 代码更新时间：2026-04-26 17:30 RT_MONITOR_STATIC_VER 20260426-11（_query_stock_basic_sec_list_act 集中查询+补查 act；/api/fina_stock_basic_act；前端缺字段补拉）
# 代码更新时间：2026-04-26 17:45 RT_MONITOR_STATIC_VER 20260426-12（固定资产弹窗 JS：act 接口与序列 Promise.all 并行合并）
# 代码更新时间：2026-04-26 18:05 RT_MONITOR_STATIC_VER 20260426-13（财报表主 SQL/fast_path 的 stock_basic 子查询与 list_date 同源带出 act_*；attach 失败不覆盖；缓存键 sb_act_2）
# 代码更新时间：2026-04-26 20:31 RT_MONITOR_STATIC_VER 20260426-14（财报筛选方案弹窗：已选方案展示列条件/排序/保存报告期摘要）
# 代码更新时间：2026-04-26 21:05 RT_MONITOR_STATIC_VER 20260426-15（方案摘要列名：补 p 应收周转天、r/u 同比及 Chg% 父指标；未知列键提示）
# 代码更新时间：2026-04-26 21:27 RT_MONITOR_STATIC_VER 20260426-16（财报表：fina_quarter_metrics 按表头报告期挂 fqm_debt_to_assets、fqm_interestdebt_ratio）
# 代码更新时间：2026-04-26 21:41 RT_MONITOR_STATIC_VER 20260426-17（固定资产弹窗顶栏证券代码输入框 text-align:center）
# 代码更新时间：2026-04-26 22:36 RT_MONITOR_STATIC_VER 20260426-18（财报表 v0..v8：单季 q_op_yoy>q_gr_yoy 时差值百分点；前端/CSS 列与分组竖线同步）
# 代码更新时间：2026-04-26 23:05 RT_MONITOR_STATIC_VER 20260426-19（v 列仅行内 q_op_yoy−q_gr_yoy，去掉 income.operate_profit 绝对额判断）
# 代码更新时间：2026-04-27 08:24 RT_MONITOR_STATIC_VER 20260426-20（财报表冻结列股票名称同 f0 打开固定资产速览；splitWrap 委托点击）
# 代码更新时间：2026-04-27 14:24 RT_MONITOR_STATIC_VER 20260427-21（单季度毛利率新增 m_high 新高与 m_qoq 环比连增 0..8）
# 代码更新时间：2026-04-27 14:35 RT_MONITOR_STATIC_VER 20260427-25（修复毛利率新高/环比 0 值显示与财报表分组竖线错位）
# 代码更新时间：2026-04-27 14:58（修复 fi_cands 快路径提前返回导致 m_high/m_qoq 始终为空；统一执行毛利率新高/环比派生）
# 代码更新时间：2026-04-27 17:37 RT_MONITOR_STATIC_VER 20260427-25（单季度净利率/ROE(扣非)新增 n_high/n_qoq、t_high/t_qoq）
# 代码更新时间：2026-04-27 17:52 RT_MONITOR_STATIC_VER 20260427-25（财报表头错位修正：空结果 colspan 与总列一致）
# 代码更新时间：2026-04-27 17:56 RT_MONITOR_STATIC_VER 20260427-25（财报表分组边界索引修正）
# 代码更新时间：2026-04-27 22:24 RT_MONITOR_STATIC_VER 20260427-26（主监控新增申万行业分析入口与独立页面；消息窗口下移避免遮挡）
# 代码更新时间：2026-04-27 22:43 RT_MONITOR_STATIC_VER 20260427-27（申万行业页改实时9季单季营收聚合，服务端180秒缓存削峰）
# 代码更新时间：2026-04-28 10:28 RT_MONITOR_STATIC_VER 20260427-28（申万行业页增9季单季扣非净利汇总，源 fina_indicator.q_dtprofit；与财报表 dtp 列同源）
# 代码更新时间：2026-04-28 10:38 RT_MONITOR_STATIC_VER 20260427-29（申万行业页表头：行业（一级）、家数）
# 代码更新时间：2026-04-28 10:50 RT_MONITOR_STATIC_VER 20260427-30（申万行业页首行汇总：全部A股，家数与各季营收/扣非列按行业加总）
# 代码更新时间：2026-04-28 11:46 RT_MONITOR_STATIC_VER 20260428-32（申万行业页：负值绿字、sw-industry-table+动态粗竖线）
# 代码更新时间：2026-04-28 11:51 RT_MONITOR_STATIC_VER 20260428-33（申万页标题/行业列名；单季营收列展示一位小数）
# 代码更新时间：2026-04-28 12:01 RT_MONITOR_STATIC_VER 20260428-34（申万页：单季营收同比增速%，上年同季可比口径；扩展 income 键集）
# 代码更新时间：2026-04-28 12:14 RT_MONITOR_STATIC_VER 20260428-35（申万页：单季扣非净利同比增速%，fina_indicator 上年同季可比口径）
# 代码更新时间：2026-04-28 12:19 RT_MONITOR_STATIC_VER 20260428-36（申万页布局：块级卡片+横滚+列宽，对齐财报页防竖排字）
# 代码更新时间：2026-04-28 12:25 RT_MONITOR_STATIC_VER 20260428-37（申万页前三列 position:sticky 横向冻结）
# 代码更新时间：2026-04-28 12:27 RT_MONITOR_STATIC_VER 20260428-38（申万同比增速列单元格去掉 % 后缀，表头仍含（%））
# 代码更新时间：2026-04-28 12:46 RT_MONITOR_STATIC_VER 20260428-39（申万页：双表分栏 CSS 对齐财报；列收紧居中；去 sticky 叠层）
# 代码更新时间：2026-04-28 12:51 RT_MONITOR_STATIC_VER 20260428-40（申万扣非：行业汇总上年同季负→当季正标 dnk+前端「扭亏」红）
# 代码更新时间：2026-04-28 12:53 RT_MONITOR_STATIC_VER 20260428-41（申万页表头不换行 nowrap；冻结列宽微调）
# 代码更新时间：2026-04-28 12:57 RT_MONITOR_STATIC_VER 20260428-42（申万页单元格对称 padding + 表头居中强化）
# 代码更新时间：2026-04-28 13:04 RT_MONITOR_STATIC_VER 20260428-43（扭亏标在扣非同比增速列，亿元列仅数值）
# 代码更新时间：2026-04-28 13:13 RT_MONITOR_STATIC_VER 20260428-44（申万滚动表分割线 thead/tbody 对齐）
# 代码更新时间：2026-04-28 13:16 RT_MONITOR_STATIC_VER 20260428-45（申万 applySwIndustryVlines：分组 thead 第3/4 格 nth-child 修正）
# 代码更新时间：2026-04-28 13:20 RT_MONITOR_STATIC_VER 20260428-46（申万冻结表头第二行行业名称；与季度行对齐）
# 代码更新时间：2026-04-28 13:25 RT_MONITOR_STATIC_VER 20260428-47（申万标题冻结列上居中；汇总行首列「汇总」）
# 代码更新时间：2026-04-28 13:29 RT_MONITOR_STATIC_VER 20260428-48（申万冻结表去掉 thead 占位行）
# 代码更新时间：2026-04-28 13:35 RT_MONITOR_STATIC_VER 20260428-49（申万双表 thead 行高同步+resize 校正）
# 代码更新时间：2026-04-28 14:12 RT_MONITOR_STATIC_VER 20260428-50（申万冻结占位改为左右 thead 总高差同步，修复家数与26Q1错位）
# 代码更新时间：2026-04-28 14:25 RT_MONITOR_STATIC_VER 20260428-51（申万冻结占位行纯隐形，去边线避免视觉多一行）
# 代码更新时间：2026-04-28 14:40 RT_MONITOR_STATIC_VER 20260428-52（申万双表改固定双行表头结构，移除运行时高度测量与延时重算）
# 代码更新时间：2026-04-28 14:46 RT_MONITOR_STATIC_VER 20260428-53（申万左右 tbody 行高强制一致，消除逐行错位）
# 代码更新时间：2026-04-28 14:50 RT_MONITOR_STATIC_VER 20260428-54（申万左表头改上隐形占位+下正常列名，修复列名行过高）
# 代码更新时间：2026-04-28 14:56 RT_MONITOR_STATIC_VER 20260428-55（申万左侧caption承接首行高度并移除thead占位行，修复整体下沉）
# 代码更新时间：2026-04-28 15:16 RT_MONITOR_STATIC_VER 20260428-56（申万冻结区新增总市值/自由市值，口径对齐财报页）
# 代码更新时间：2026-04-28 15:24 RT_MONITOR_STATIC_VER 20260428-57（申万页新增列头排序+筛选弹层，覆盖总市值/自由市值及26Q1等后续列）
# 代码更新时间：2026-04-28 15:26 RT_MONITOR_STATIC_VER 20260428-58（申万冻结区总市值/自由市值显示改为整数）
# 代码更新时间：2026-04-28 15:31 RT_MONITOR_STATIC_VER 20260428-59（申万列头增加双击筛选与可见排序提示，提升可发现性）
# 代码更新时间：2026-04-28 15:35 RT_MONITOR_STATIC_VER 20260428-60（申万列头显式“⇅/筛”控件，点击筛按钮可直接弹筛选）
# 代码更新时间：2026-04-28 16:04 RT_MONITOR_STATIC_VER 20260428-61（申万列头改为财报同款结构：fina-sort-btn + fina-filter-btn）
# 代码更新时间：2026-04-28 16:09 RT_MONITOR_STATIC_VER 20260428-62（申万页营收同比/扣非同比显示精度改为两位小数）
# 代码更新时间：2026-04-28 16:10 RT_MONITOR_STATIC_VER 20260428-63（行业代码/行业名称/家数移除排序筛选，仅保留后续列）
# 代码更新时间：2026-04-28 16:20 RT_MONITOR_STATIC_VER 20260428-64（申万右侧四组季度列宽适度收紧）
# 代码更新时间：2026-04-28 16:36 RT_MONITOR_STATIC_VER 20260428-65（申万页新增一级/二级下拉切换，默认一级）
# 代码更新时间：2026-04-28 16:54 RT_MONITOR_STATIC_VER 20260428-66（申万页前三行 sticky：标题+两行表头）
# 代码更新时间：2026-04-28 17:12 RT_MONITOR_STATIC_VER 20260428-67（修复申万 sticky 未生效：容器 overflow 口径改为纵向 visible）
# 代码更新时间：2026-04-28 17:29 RT_MONITOR_STATIC_VER 20260428-68（申万三行 sticky 偏移层级修正）
# 代码更新时间：2026-04-28 17:43 RT_MONITOR_STATIC_VER 20260428-69（申万改容器内滚动+左右纵向同步，三行锁定稳定生效）
# 代码更新时间：2026-04-28 17:48 RT_MONITOR_STATIC_VER 20260428-70（申万表头/行高渲染后动态同步，修复缩放与按钮撑高导致的再次错位）
# 代码更新时间：2026-04-28 17:55 RT_MONITOR_STATIC_VER 20260428-71（申万右侧表头 sticky 偏移改为 0/row1，修复再次错位）
# 代码更新时间：2026-04-28 18:04 RT_MONITOR_STATIC_VER 20260428-72（申万新增一级行业筛选下拉，支持按一级过滤展示二级明细并回传对应汇总口径）
# 代码更新时间：2026-04-28 18:07 RT_MONITOR_STATIC_VER 20260428-73（选择一级行业时强制二级明细口径，修复仍显示一级聚合问题）
# 代码更新时间：2026-04-28 18:13 RT_MONITOR_STATIC_VER 20260428-74（新增二级行业筛选下拉，选择后自动切三级并输出l3明细）
# 代码更新时间：2026-04-28 18:19 RT_MONITOR_STATIC_VER 20260428-75（隐藏左侧冻结区滚动条，仅保留右侧主滚动条）
# 代码更新时间：2026-04-28 18:22 RT_MONITOR_STATIC_VER 20260428-76（四组季度列顺序调整为：营收同比→扣非同比→营收→扣非）
# 代码更新时间：2026-04-30 08:23 RT_MONITOR_STATIC_VER 20260428-77（申万扩展五组 nine 季指标：单季经营现金流/毛利率/ROE扣非/合同负债/固资；缓存 key x1；前端 swColumnMeta）
# 代码更新时间：2026-04-30 08:58 RT_MONITOR_STATIC_VER 20260428-78（申万 qgm/qroe 由算术平均改为行业中位数；API 缓存 key x2）
# 代码更新时间：2026-04-30 09:03 RT_MONITOR_STATIC_VER 20260428-79（申万页合同负债/固定资产列显示一位小数）
# 代码更新时间：2026-04-30 09:08 RT_MONITOR_STATIC_VER 20260428-80（申万表头：毛利率/ROE扣非列名标明中位数）
# 代码更新时间：2026-04-30 09:17 RT_MONITOR_STATIC_VER 20260428-81（申万毛利率/ROE中位数组：新高+环比+九季，口径对齐财报 m_high/m_qoq；缓存 x3）
# 代码更新时间：2026-04-30 18:35 RT_MONITOR_STATIC_VER 20260428-82（申万九组指标窗口由9季扩至13季；qgm/qroe新高环比序列同步扩展）
# 代码更新时间：2026-05-01 07:20 RT_MONITOR_STATIC_VER 20260428-83（申万经营现金流净额 ycf* 同比增速，口径对齐营收 y*；现金流批量键含同比锚点；缓存 x5）
# 代码更新时间：2026-05-01 07:27 RT_MONITOR_STATIC_VER 20260428-84（申万页 ycf 列组移至扣非同比右侧；列宽与表头/体同步）
# 代码更新时间：2026-05-01 07:39 RT_MONITOR_STATIC_VER 20260428-85（申万 ycf 空且行业现金流负转正时 ycfnk+前端「转正」；缓存 x6）
# 代码更新时间：2026-05-01 07:43 RT_MONITOR_STATIC_VER 20260428-86（申万 qcf 组 qcf_high/qcf_qoq 新高环比，口径同 qroe；缓存 x7）
# 代码更新时间：2026-05-01 07:53 RT_MONITOR_STATIC_VER 20260428-87（申万 s/dtp 组 s_high、s_qoq、dtp_high、dtp_qoq；缓存 x8）
# 代码更新时间：2026-05-01 10:14 RT_MONITOR_STATIC_VER 20260430-1（牛散持股页 /rt-monitor/niusan；侧栏入口）
# 代码更新时间：2026-05-01 10:15 RT_MONITOR_STATIC_VER 20260430-2（牛散侧栏链接样式 rt_monitor.css）
# 代码更新时间：2026-05-01 10:31 RT_MONITOR_STATIC_VER 20260430-3（牛散页 UI 对齐财报页 fina-arturn 卡片布局；去掉口径说明文案）
# 代码更新时间：2026-05-01 10:44 RT_MONITOR_STATIC_VER 20260430-4（牛散列：自由市值整数亿、sw_name、比例两位、增减红绿、金额/VWAP两位、报告期）
# 代码更新时间：2026-05-01 10:49 RT_MONITOR_STATIC_VER 20260430-5（牛散表恢复总市值(亿) 整数，与自由市值并列）
# 代码更新时间：2026-05-01 10:56 RT_MONITOR_STATIC_VER 20260430-6（牛散：20日涨跌%、相对VWAP%；rt_k 最新价 + stk_factor 历史）
# 代码更新时间：2026-05-01 11:07 RT_MONITOR_STATIC_VER 20260430-7（牛散：20日/相对VWAP 列正值红负值绿）
# 代码更新时间：2026-05-01 11:10 RT_MONITOR_STATIC_VER 20260430-8（牛散表列顺序：报告期置末等）
# 代码更新时间：2026-05-01 11:21 RT_MONITOR_STATIC_VER 20260430-9（牛散页 UI：层次、列宽、居中、渐变标题与表头）
# 代码更新时间：2026-05-01 11:28 RT_MONITOR_STATIC_VER 20260430-10（牛散：列宽收紧；涨跌色选择器压过 tbody 字色）
# 代码更新时间：2026-05-01 11:52 RT_MONITOR_STATIC_VER 20260430-11（牛散表头：章建平/关联人说明行 + 合计持仓金额亿）
# 代码更新时间：2026-05-01 12:03 RT_MONITOR_STATIC_VER 20260430-12（牛散持仓变化 + 展开历史报告期至新进；/api/niusan_floatholder_history）
# 代码更新时间：2026-05-01 12:31 RT_MONITOR_STATIC_VER 20260430-13（牛散展开钮移至报告期格右下角）
# 代码更新时间：2026-05-01 13:01 RT_MONITOR_STATIC_VER 20260430-14（报告期与+同一行、不增高行）
# 代码更新时间：2026-05-01 13:07 RT_MONITOR_STATIC_VER 20260430-15（牛散 VWAP：新进起按季边际 VWAP 与持仓变化 blend）
# 代码更新时间：2026-05-01 15:10 RT_MONITOR_STATIC_VER 20260501-1（牛散 VWAP 链：自然季回溯，缺季止、新进止后再 blend）
# 代码更新时间：2026-05-01 15:15 RT_MONITOR_STATIC_VER 20260501-2（牛散：公告日 ann_date，列在报告期左侧）
# 代码更新时间：2026-05-01 15:23 RT_MONITOR_STATIC_VER 20260501-3（牛散：公告日字体对齐报告期；金额/市值等列申万式排序）
# 代码更新时间：2026-05-01 15:34 RT_MONITOR_STATIC_VER 20260501-4（牛散方案 niusan_holder_config.json：方案名展示、关联人 OR 匹配；配置页）
# 代码更新时间：2026-05-01 15:53 RT_MONITOR_STATIC_VER 20260501-6（牛散页：方案配置弹窗左置；股票名点出关联人明细；/api/niusan_floatholder_detail）
# 代码更新时间：2026-05-01 16:09 RT_MONITOR_STATIC_VER 20260501-7（牛散：标题行方案配置；明细弹窗拖拽缩放；明细内各方案持股数/金额汇总）
# 代码更新时间：2026-05-01 16:20 RT_MONITOR_STATIC_VER 20260501-8（牛散：方案汇总条移至主表上方；明细接口预取缓存秒开）
# 代码更新时间：2026-05-01 16:37 RT_MONITOR_STATIC_VER 20260501-9（牛散：rt_k 过滤代码；方案条横排点击明细；配置下拉单方案编辑；/api/niusan_scheme_holdings）
# 代码更新时间：2026-05-01 16:58 RT_MONITOR_STATIC_VER 20260501-10（牛散：点击方案卡在主表区切换，不再弹窗；API 返回 scheme_name/related_join）
# 代码更新时间：2026-05-01 17:00 RT_MONITOR_STATIC_VER 20260501-11（牛散主表股票名称列：黑色字，保留下划线可点）
# 代码更新时间：2026-05-01 17:10 RT_MONITOR_STATIC_VER 20260501-12（牛散：空闲预取各方案持股 JSON 缓存切换秒开；表头说明行黑字）
# 代码更新时间：2026-05-01 17:17 RT_MONITOR_STATIC_VER 20260501-13（牛散：方案配置内嵌 JSON 首击秒开；预取延迟 500ms 减争用）
# 代码更新时间：2026-05-01 17:31 RT_MONITOR_STATIC_VER 20260501-14（牛散表：总市值/自由市值列收窄；申万二级行业列加宽）
# 代码更新时间：2026-05-01 18:11 RT_MONITOR_STATIC_VER 20260501-15（牛散关联人明细：top10_floatholders 按 holder_name+最新 ann_date 去重）
# 代码更新时间：2026-05-01 18:22 RT_MONITOR_STATIC_VER 20260501-16（牛散主表/方案条/批量季统计/历史链：聚合前同口径按股东去重，与明细一致）
# 代码更新时间：2026-05-02 19:59 RT_MONITOR_STATIC_VER 20260502-1（财报筛选 stock_company 省/市/网站；固定资产序列 website；fina 缓存键 sb_act_4）
# 代码更新时间：2026-05-03 09:15 fina_arturn 附加 top10_holders 前三持股合计/明细；缓存键 sb_act_5；RT_MONITOR_STATIC_VER 20260503-16
# 代码更新时间：2026-05-03 09:30 RT_MONITOR_STATIC_VER 20260503-17（前三持股明细弹窗表内与副标题居中）
# 代码更新时间：2026-05-03 10:12 FINA_ARTURN_TABLE_CACHE_MAX 48→50，旁注单期 payload 粗算体量
# 代码更新时间：2026-05-03 10:21 _warmup_fina_arturn_cache_async 预取前 N 个报告期（FINA_ARTURN_WARMUP_PREFETCH_N=3）
# 代码更新时间：2026-05-03 10:37 sw_industry_stats 进程内长期缓存+LRU(50)+no-store；cache_ttl_sec 响应用 0
# 代码更新时间：2026-05-03 10:41 _warmup_sw_industry_cache_async 默认 L1（test_client GET sw_industry_stats?level=l1）
# 代码更新时间：2026-05-03 11:05 fina_arturn 自由流通比 free_mv/total_mv×100；_fina_arturn_attach_free_float_ratio_pct；缓存键 sb_act_6；RT_MONITOR_STATIC_VER 20260503-18
# 代码更新时间：2026-05-03 13:11 fina_arturn 股东人数 hn0–hn8（stk_holdernumber）；_fina_arturn_attach_stk_holdernumber_quarters；缓存键 sb_act_7；RT_MONITOR_STATIC_VER 20260503-19
# 代码更新时间：2026-05-03 13:19 rt_monitor_niusan 表头点击：event.target 为 Text 时无 closest，致「+」整段抛错；归一化到父元素；RT_MONITOR_STATIC_VER 20260503-20
# 代码更新时间：2026-05-03 13:44 牛散展开历史/明细 API 按 scheme_id 解析关联人（与主表当前方案一致）；_niusan_resolve_holder_hw_hp；RT_MONITOR_STATIC_VER 20260503-21
# 代码更新时间：2026-06-13 06:48 市场状态页恢复路由+API；增超跌/涨跌停/封板率六列；RT_MONITOR_STATIC_VER 20260613-1
# 代码更新时间：2026-06-13 17:04 市场状态表：上涨比例/涨跌中位数/封板率数值去尾部%；RT_MONITOR_STATIC_VER 20260613-2
# 代码更新时间：2026-06-13 19:43 市场状态表跌停开右增 5/10/20/60/120/200/250/850/HisDHL%（HLx_HHLL，正红负绿）；RT_MONITOR_STATIC_VER 20260613-3
# 代码更新时间：2026-06-13 19:54 市场状态表 HisHL% 右增 5..850/HisDHLC%（HLx_CCCC，正红负绿）；RT_MONITOR_STATIC_VER 20260613-4
# 代码更新时间：2026-06-13 20:09 市场状态表 HHLL/CCCC 18 列表头去掉 %；RT_MONITOR_STATIC_VER 20260613-5
# 代码更新时间：2026-06-13 20:20 市场状态表成交额左增 总市值/流通市值/自由市值（total_mv/circ_mv/free_mv÷10000，两位小数）；RT_MONITOR_STATIC_VER 20260613-6
# 代码更新时间：2026-06-13 20:53 市场状态表中位数右增 振幅（amplitude_399317，两位小数）；中位数表头改名；RT_MONITOR_STATIC_VER 20260613-7
# 代码更新时间：2026-06-13 20:58 市场状态表成交额右增 5%_Pct（amount_top5_pct，两位小数）；RT_MONITOR_STATIC_VER 20260613-8
# 代码更新时间：2026-07-20 17:55 市场状态表跌停开右增 HisHC/HisLC（HLhis_CH/HLhis_CL）；RT_MONITOR_STATIC_VER 20260720-1
# 代码更新时间：2026-07-20 18:06 市场状态 HisHC 弹窗双轴（HisHC/HisLC + 399317）；RT_MONITOR_STATIC_VER 20260720-2
# 代码更新时间：2026-07-20 18:34 市场状态 120/200/250/850/HisHLC 各自弹窗+右轴 399317；RT_MONITOR_STATIC_VER 20260720-3
# 代码更新时间：2026-06-13 13:27 补 INDEX_MONITOR_CODES/_index_industry_stats_cache，修复 /rt-monitor/index 500
# 代码更新时间：2026-06-13 13:45 恢复 Top5%_pct/Per、成交额 MIN T-60/谷值日、涨平跌表头后端字段
# 代码更新时间：2026-06-13 13:51 HL_C 改用 rt_k 最新日的上一交易日 MA 阈值，避免 ma 含当日 K 线导致全 0
# 代码更新时间：2026-06-14 11:37 恢复申万行业页实时层：区间涨跌幅/成交额比%/新高新低分布(含收盘价口径)/融资余额占位 + 明细端点 api_sw_industry_limit_detail；补 timedelta 导入；cache_key x8→x9
# 代码更新时间：2026-06-14 12:01 申万行业页融资余额列组改回 stock_wind.margin_detail 个股明细聚合；cache_key x9→x10 并返回 margin_as_of
# 代码更新时间：2026-06-14 12:15 申万行业页融资余额列组前端改为两位小数显示；RT_MONITOR_STATIC_VER 20260614-2
# 代码更新时间：2026-06-14 12:19 修正申万行业页历史/融资基准日提示元素 id；RT_MONITOR_STATIC_VER 20260614-3
# 代码更新时间：2026-06-14 12:40 修复申万行业页 HL_C 收盘价新高新低列：实时阈值改用 rt_k 最新日前一交易日 MA；RT_MONITOR_STATIC_VER 20260614-4
# 代码更新时间：2026-06-14 12:51 申万行业页 HL_C 六列合并为一次 MA/MP 查询，避免重复 JOIN 导致接口超时
# 代码更新时间：2026-06-14 13:21 opt-t-board 标注下月/下季牛市价差与熊市价差买卖腿；RT_MONITOR_STATIC_VER 20260614-5
# 代码更新时间：2026-06-14 13:28 opt-t-board 牛差/熊差合并单列，买卖徽章分色；RT_MONITOR_STATIC_VER 20260614-6
# 代码更新时间：2026-06-14 13:31 牛差/熊差单列徽章配色定稿并 bump 静态版本；RT_MONITOR_STATIC_VER 20260614-7
# 代码更新时间：2026-06-14 15:02 opt-t-board 选腿改回测甜点+胜率+盈亏比综合打分；RT_MONITOR_STATIC_VER 20260614-8
# 代码更新时间：2026-06-14 15:18 铁律②仅1档③成本>0.6剔除⑤熊差当月+下季备选标注；RT_MONITOR_STATIC_VER 20260614-9
# 代码更新时间：2026-06-14 20:35 分标的配置(甜点/止盈/到期月)+页头弹性排序+588000警示；RT_MONITOR_STATIC_VER 20260614-10
# 代码更新时间：2026-06-14 21:15 opt-t-board 左侧固定展示垂直价差五条铁律；RT_MONITOR_STATIC_VER 20260614-11
# 代码更新时间：2026-06-14 21:16 opt-t-board 配置 JSON 改后端序列化，修复整页 500；RT_MONITOR_STATIC_VER 20260614-12
# 代码更新时间：2026-06-14 21:33 修复 opt-t-board 工作线程懒导入回测模块触发 signal 注册崩溃；RT_MONITOR_STATIC_VER 20260614-13
# 代码更新时间：2026-06-14 22:30 选腿成本>0.5剔除、平值整行高亮、行权价列显示到期日与剩余天数；RT_MONITOR_STATIC_VER 20260614-14
# 代码更新时间：2026-06-14 22:45 到期日与剩余天数改到分组标题行展示一次，行权价列仅保留价格；RT_MONITOR_STATIC_VER 20260614-15
# 代码更新时间：2026-06-15 13:00 申万页缓存命中时仍重查 margin_detail 刷新融资余额列；cache_key x10→x11
# 代码更新时间：2026-06-16 10:25 申万页缓存命中时刷新完整实时层（区间涨跌幅/成交额比%/新高低/融资）；cache_key x12；rt_sw_k 含 amount
# 代码更新时间：2026-06-16 23:12 申万新高/低明细：历史日与主表计数同源读归档；HL_C 明细单 kind 查询；弹窗加载态优化
# 代码更新时间：2026-06-17 06:56 申万新高/低明细秒开：主表刷新预热 flagged 集合+stock_basic 元数据，明细仅内存过滤
# 代码更新时间：2026-06-17 09:20 固定资产弹窗恢复 rows[].ann_date（fina_indicator 主选行）；RT_MONITOR_STATIC_VER 20260617-1
# 代码更新时间：2026-06-17 19:53 P4.4 补全 sw_index_amt_pctile_series/fina_top10/system_scheme API；Amt% 序列 300s TTL 缓存
# 代码更新时间：2026-06-17 19:58 Amt% 分位序列：预计算日比率 + bisect 窗口，避免 O(n²) 全表扫描
# 代码更新时间：2026-06-23 15:25 Top30 三表 INNER JOIN rt_k 排除 vol=0 且 amount=0（停牌/无成交）
# 代码更新时间：2026-06-18 09:11 top10_holders 索引 idx_top10_holders_ts_end_date + load_initial_cache 幂等创建
# 代码更新时间：2026-06-22 11:18 limit_stats HL_C 改读 stockstat 预写列，修复 ma JOIN 读超时拖死同连接致连板/T-1/超跌等闪 --
# 代码更新时间：2026-06-23 16:45 amount_top5_pct_series 支持 start_date 全历史（≤6000点）+ 滚动五年 2σ 序列；RT_MONITOR_STATIC_VER 20260623-1
# 代码更新时间：2026-07-09 09:41 after_request gzip 压缩大 JSON；财报表虚拟滚动见 rt_monitor_fina.js
# 代码更新时间：2026-07-14 13:46 成交额Top30自由换手：free_share 最新日为空时回退最近非空日
# 代码更新时间：2026-08-14 22:20 no-store 增 /api/opt-iv-board、/api/opt-sp-plan
# 代码更新时间：2026-09-02 10:50 amount_top5_pct 滚动 2σ 改 O(n) 滑窗，避免弹窗 12s 超时
# 代码更新时间：2026-09-07 15:01 top_moves：TTL 8s、singleflight、昨额 120s、保活让路、slow_api 20s 冷却
# 代码更新时间：2026-09-07 16:53 修复 _topMovesFactorExp UnboundLocalError；申万刷新期间同样让路
# 代码更新时间：2026-09-07 17:00 top_moves SWR：HTTP 只读内存，后台 2～8s 自适应刷首页 Top30
# 代码更新时间：2026-09-07 17:27 后台启动预热；无人看按 15s 停刷；间隔从本轮开始起算
# 代码更新时间：2026-09-07 18:38 LIMIT_STATS_CACHE_TTL 3→1s（涨跌家数贴近 1s）
# 代码更新时间：2026-09-09 07:28 放开中证2000/北证50 区间涨跌幅拼 rt_idx_k（TQ 写库）
# 代码更新时间：2026-10-01 12:45 成交额表 T-120 后增全历史 MAX / 峰值日（index_daily 与 marketinfo）
# 代码更新时间：2026-10-01 14:07 涨跌幅表 Gap 旁注全历史分位
# 代码更新时间：2026-10-01 15:21 涨跌幅表 Pct/Intra/Gap/Amp 全历史分位与尾部浅底
# 代码更新时间：2026-10-01 16:45 Ma/Ema 表 AMT/Pct/Intra/Gap/Amp 全历史分位
# 代码更新时间：2026-10-04 21:54 P5.1 成交额历史极值/均值 12 个只读查询迁至 services/amount_extremes（原名 re-export）
