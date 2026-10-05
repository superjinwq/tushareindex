#!/usr/bin/env python3
"""P4.2：将 monolith 中 fina 查询块迁入 services/fina_arturn_queries.py。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONO = ROOT / 'rt_monitor' / 'monolith.py'
OUT = ROOT / 'rt_monitor' / 'services' / 'fina_arturn_queries.py'

HEADER = '''"""财报 arturn / 固定资产序列查询（P4.2 自 monolith 迁出）。"""
from __future__ import annotations
# 用途: 留档
# 保留至: 2026-10-20

import os
import re
import statistics
import time
import threading
import concurrent.futures
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal
from datetime import date, datetime, timedelta
from typing import Any

import pymysql
import numpy as np

from logger_config import get_logger
from rt_monitor.core.config import DB_CONFIG, DB_CONFIG_TUSHARE
from rt_monitor.core.numbers import toFloatOrNone

logger = get_logger(__name__)

_to_float_or_none = toFloatOrNone
SLOW_API_WARN_SECONDS = 0.45

_finaCacheLock = threading.Lock()

try:
    from database_pool import get_db_connection as _poolGetDbConnection
except ModuleNotFoundError:

    @contextmanager
    def _poolGetDbConnection(pool_name='default', timeout=30):
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

get_db_connection = _poolGetDbConnection

_TUSHARE_COLUMN_EXISTS_CACHE: dict = {}

_RT_K_TS_CODES_CACHE_ROW: tuple[float, tuple[str, ...]] | None = None
_RT_K_TS_CODES_CACHE_TTL_SEC = 180.0


def _get_cached_rt_k_ts_codes_tuple(conn) -> tuple[str, ...] | None:
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


def _limit_stats_sw_join_sql(on_ts_code_expr: str) -> str:
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


def _normalize_end_date_yyyymmdd(raw_val) -> str | None:
    from rt_monitor.monolith import _normalize_end_date_yyyymmdd as fn

    return fn(raw_val)


def _ocf_to_or_display_pct(raw) -> float | None:
    from rt_monitor.monolith import _ocf_to_or_display_pct as fn

    return fn(raw)


def _log_slow_api(api_name: str, started_at: float, extra: str = '') -> None:
    elapsed = time.perf_counter() - started_at
    if elapsed >= SLOW_API_WARN_SECONDS:
        logger.warning("slow_api %s elapsed=%.3fs %s", api_name, elapsed, extra)


def _small_ttl_cache_get(cache_obj: OrderedDict, cache_key, now_ts: float):
    with _finaCacheLock:
        hit = cache_obj.get(cache_key)
        if hit is not None and now_ts < hit[0]:
            cache_obj.move_to_end(cache_key)
            return hit[1]
    return None


def _small_ttl_cache_put(cache_obj: OrderedDict, cache_key, payload: dict, ttl: float, max_items: int):
    with _finaCacheLock:
        while len(cache_obj) >= max_items:
            cache_obj.popitem(last=False)
        cache_obj[cache_key] = (time.time() + ttl, payload)
        cache_obj.move_to_end(cache_key)


'''

IMPORT_BLOCK = '''
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

'''


def findLine(lines: list[str], prefix: str) -> int:
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            return i
    raise RuntimeError(f'not found: {prefix!r}')


def main() -> None:
    lines = MONO.read_text(encoding='utf-8').splitlines(keepends=True)
    iFinaTtl = findLine(lines, 'FINA_ARTURN_TABLE_CACHE_TTL = ')
    iAfterFinaCache = iFinaTtl
    while iAfterFinaCache < len(lines) and not lines[iAfterFinaCache].startswith('SLOW_API_WARN_SECONDS'):
        iAfterFinaCache += 1
    finaConsts = ''.join(lines[iFinaTtl:iAfterFinaCache])
    linesNoConsts = lines[:iFinaTtl] + lines[iAfterFinaCache:]

    iStart = findLine(linesNoConsts, 'def _derive_financial_periods(')
    iEnd = findLine(linesNoConsts, 'ETF_BIAS_COLUMN_MAP = {')
    body = ''.join(linesNoConsts[iStart:iEnd])

    OUT.write_text(
        HEADER + finaConsts + '\n' + body + '\n# 代码更新时间：2026-06-17 17:10\n',
        encoding='utf-8',
    )

    newLines = linesNoConsts[:iStart] + [IMPORT_BLOCK] + linesNoConsts[iEnd:]
    MONO.write_text(''.join(newLines), encoding='utf-8')
    print(f'extracted {iEnd - iStart} lines -> {OUT.name}')


if __name__ == '__main__':
    main()
