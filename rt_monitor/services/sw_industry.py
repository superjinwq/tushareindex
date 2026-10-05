"""申万行业 + 宽基指数行业统计（自 monolith P2 迁出）。"""
from __future__ import annotations

import bisect
import copy
import hashlib
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict, defaultdict, deque
from datetime import date, datetime, timedelta
from typing import Any

import pymysql
from flask import jsonify, request
from logger_config import get_logger

from rt_monitor.core.config import DB_CONFIG, DB_CONFIG_TUSHARE, DB_CONFIG_WIND_BULK
from rt_monitor.core.market_session import isAShareMarketActive
from rt_monitor.core.small_cache import smallTtlCacheGet, smallTtlCachePut
from first_qfq_new_high import EPS, BarRecord, SlidingMaxHigh, findFirstBreaksForModes, toFloat

from rt_monitor.services.fina_arturn_queries import (
    _derive_financial_periods,
    _derive_quarter_periods,
    _fina_arturn_attach_price_range_pct,
    _fina_batch_balancesheet_contract_fix_yi,
    _fina_batch_cashflow_n_cashflow_act,
    _fina_batch_fina_indicator_q_dtprofit,
    _fina_batch_fina_indicator_q_gs_margin_q_dt_roe,
    _fina_batch_income_total_revenue,
    _fina_income_date_keys_with_yoy_for_periods,
    _fina_periods_union_yoy_for_q_dtprofit,
    _fina_quarter_single_revenue,
    _fina_yq_from_anchor,
    _fina_yoy_anchor_end_date,
    _normalize_end_date_yyyymmdd,
    _sw_attach_high_qoq_pair,
    _sw_cf_industry_single_q_sum_yuan,
    _sw_cf_yoy_yoypct_for_codes,
    _sw_dtp_yoy_yoypct_for_codes,
    _sw_median_rounded,
    _sw_rev_yoy_yoypct_for_codes,
    _to_float_or_none,
    _tushare_column_exists,
)

logger = get_logger(__name__)

# Windows 上 MySQL 磁盘临时表偶发 ER_NO_SUCH_TABLE(1146,#sql*)；短重试可扛过竞态
_SW_MYSQL_TEMP_TABLE_RETRY_MAX = 3
_SW_MYSQL_TEMP_TABLE_RETRY_BASE_SEC = 0.35


def _isMysqlEphemeralTempTableError(exc: BaseException) -> bool:
    """识别 MySQL 临时表文件丢失类错误（1146 + #sql / tmpdir 路径）。"""
    args = getattr(exc, 'args', ()) or ()
    errno = args[0] if args else None
    msg = str(args[1] if len(args) > 1 else exc).lower()
    if errno == 1146 and ('#sql' in msg or 'tmpdir' in msg or '\\temp\\' in msg or '/temp/' in msg):
        return True
    if '#sql' in msg and ("doesn't exist" in msg or 'does not exist' in msg):
        return True
    return False


def _swCallWithMysqlTempTableRetry(label: str, fn):
    """对易触发磁盘临时表的查询做有限次重试。"""
    lastExc: BaseException | None = None
    for attempt in range(1, _SW_MYSQL_TEMP_TABLE_RETRY_MAX + 1):
        try:
            return fn()
        except Exception as e:
            lastExc = e
            if not _isMysqlEphemeralTempTableError(e) or attempt >= _SW_MYSQL_TEMP_TABLE_RETRY_MAX:
                raise
            sleepSec = _SW_MYSQL_TEMP_TABLE_RETRY_BASE_SEC * attempt
            logger.warning(
                '%s MySQL临时表竞态(1146/#sql)，重试 %s/%s sleep=%.2fs: %s',
                label,
                attempt,
                _SW_MYSQL_TEMP_TABLE_RETRY_MAX,
                sleepSec,
                e,
            )
            time.sleep(sleepSec)
    assert lastExc is not None
    raise lastExc


# ---------- limit_stats SQL 片段（申万新高/低明细与 HL_C 同源，与 monolith 保持同步）----------
SQL_RT_K_MAX_TRADE_TIME_SUBQUERY = (
    "(SELECT MAX(trade_time) FROM stock_wind.rt_k rt2 WHERE rt2.trade_time IS NOT NULL)"
)
SQL_RT_K_FRESH_WHERE_RT = (
    "rt.trade_time IS NOT NULL AND "
    "rt.trade_time >= DATE(" + SQL_RT_K_MAX_TRADE_TIME_SUBQUERY + ") "
    "AND rt.trade_time < DATE(" + SQL_RT_K_MAX_TRADE_TIME_SUBQUERY + ") + INTERVAL 1 DAY"
)
_LIMIT_STATS_RT_K_FRESH_TRADE_DATE = SQL_RT_K_FRESH_WHERE_RT
_LIMIT_STATS_RT_CLOSE_QFQ = (
    "(rt.close * COALESCE("
    "NULLIF(ma.close_qfq, 0) / NULLIF(ma.pro_close, 0), "
    "1))"
)
# ST 判定一律以 stock_wind.rt_k.name 为准：rt_k 每日实时同步，而 stockstat.sec_name 仅在
# 盘中实时统计.py 进程启动时刷新一次，长期常驻会停留在旧快照（曾出现摘帽股仍被当 ST 剔除）。
_LIMIT_STATS_SQL_NOT_ST_RT_K = (
    " AND (rt.name IS NULL OR ("
    " rt.name NOT LIKE '*ST%%' AND rt.name NOT LIKE 'ST%%' AND rt.name NOT LIKE 'S*ST%%'"
    "))"
)
# 纯 stockstat 查询（盘中标志位口径）本身无 rt_k，需显式 JOIN 才能按实时名称判定 ST；
# INNER JOIN 同时顺带剔除 stockstat 中已退市的残留代码。
_LIMIT_STATS_SQL_JOIN_RT_K_ON_S = " INNER JOIN stock_wind.rt_k rt ON rt.ts_code = s.windcodes "
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
TABLE_LIMIT_STATS_ARCHIVE_DETAIL = "limit_stats_archive_detail"
SLOW_API_WARN_SECONDS = 0.45

_swCacheLock = threading.Lock()

# 申万行业页区间涨跌幅/成交额比% 的基准：申万全 A（801003.SI）
SW_STATS_SUMMARY_ALL_A_SW_TS = '801003.SI'
# 成交额比% 分位的历史回看年数
SW_AMT_RATIO_PCTILE_LOOKBACK_YEARS = 5
# 新高/低分布（盘中标志位口径）：JSON 字段 → stockstat 标志列（涨跌家数不含 ST）
_SW_STOCKSTAT_T0_FLAG_SQL: dict[str, str] = {
    "high_250": "IFNULL(s.windcodes_high_250,0)=1",
    "high_850": "IFNULL(s.windcodes_high_850,0)=1",
    "h_high": "IFNULL(s.windcodes_h_high,0)=1",
    "low_250": "IFNULL(s.windcodes_low_250,0)=1",
    "low_850": "IFNULL(s.windcodes_low_850,0)=1",
    "h_low": "IFNULL(s.windcodes_h_low,0)=1",
}
# 申万页「新高、新低分布」六列：JSON 字段名 → stockstat 标志键
_SW_INDUSTRY_LIMITSTAT_T0_SPECS: tuple[tuple[str, str], ...] = (
    ("sw_h250_t0", "high_250"),
    ("sw_h850", "high_850"),
    ("sw_h_his", "h_high"),
    ("sw_l250", "low_250"),
    ("sw_l850", "low_850"),
    ("sw_l_his", "h_low"),
)
# 申万页「新高、新低分布（收盘价）」六列：JSON 字段名 → HL_C 收盘前复权 kind
_SW_INDUSTRY_HLC_SPECS: tuple[tuple[str, str], ...] = (
    ("sw_hlc_h250", "hlc_ch_250"),
    ("sw_hlc_l250", "hlc_cl_250"),
    ("sw_hlc_h850", "hlc_ch_850"),
    ("sw_hlc_l850", "hlc_cl_850"),
    ("sw_hlc_h_his", "hlc_ch_h"),
    ("sw_hlc_l_his", "hlc_cl_h"),
)
# 申万页新高/低分布列 → 明细弹窗（与统计计数同源；archive_kind 供历史模式）
_SW_LIMIT_DETAIL_COLUMN_META: dict[str, dict[str, str]] = {
    "sw_h250_t0": {"title": "250日新高", "fetch": "stat", "flag_key": "high_250", "archive_kind": "cnt_h250"},
    "sw_h850": {"title": "850日新高", "fetch": "stat", "flag_key": "high_850", "archive_kind": "cnt_h850"},
    "sw_h_his": {"title": "历史新高", "fetch": "stat", "flag_key": "h_high", "archive_kind": "cnt_h_high"},
    "sw_l250": {"title": "250日新低", "fetch": "stat", "flag_key": "low_250", "archive_kind": "cnt_l250"},
    "sw_l850": {"title": "850日新低", "fetch": "stat", "flag_key": "low_850", "archive_kind": "cnt_l850"},
    "sw_l_his": {"title": "历史新低", "fetch": "stat", "flag_key": "h_low", "archive_kind": "cnt_l_low"},
    "sw_hlc_h250": {"title": "250日新高（收盘价）", "fetch": "hlc", "hlc_kind": "hlc_ch_250", "archive_kind": "hlc_ch_250"},
    "sw_hlc_h850": {"title": "850日新高（收盘价）", "fetch": "hlc", "hlc_kind": "hlc_ch_850", "archive_kind": "hlc_ch_850"},
    "sw_hlc_h_his": {"title": "历史新高（收盘价）", "fetch": "hlc", "hlc_kind": "hlc_ch_h", "archive_kind": "hlc_ch_h"},
    "sw_hlc_l250": {"title": "250日新低（收盘价）", "fetch": "hlc", "hlc_kind": "hlc_cl_250", "archive_kind": "hlc_cl_250"},
    "sw_hlc_l850": {"title": "850日新低（收盘价）", "fetch": "hlc", "hlc_kind": "hlc_cl_850", "archive_kind": "hlc_cl_850"},
    "sw_hlc_l_his": {"title": "历史新低（收盘价）", "fetch": "hlc", "hlc_kind": "hlc_cl_h", "archive_kind": "hlc_cl_h"},
}
# 收盘价六列弹窗：break_mode + 方向（high=新高突破 / low=新低跌破）；his/850 高走 first_qfq，其余本地扫描
_SW_LIMIT_DETAIL_BREAK_SPEC_BY_COL: dict[str, tuple[str, str]] = {
    "sw_hlc_h250": ("250", "high"),
    "sw_hlc_h850": ("850", "high"),
    "sw_hlc_h_his": ("his", "high"),
    "sw_hlc_l250": ("250", "low"),
    "sw_hlc_l850": ("850", "low"),
    "sw_hlc_l_his": ("his", "low"),
}
_SW_LIMIT_DETAIL_BREAK_MODE_BY_COL: dict[str, str] = {
    k: spec[0] for k, spec in _SW_LIMIT_DETAIL_BREAK_SPEC_BY_COL.items()
}
# 六列明细均附带申万行业路径 + 突破类型（first/last）
_SW_LIMIT_DETAIL_SW_INDUSTRY_COLS: frozenset[str] = frozenset(
    _SW_LIMIT_DETAIL_BREAK_SPEC_BY_COL.keys()
)
_SW_LIMIT_DETAIL_LOOKBACK: dict[str, int | None] = {
    "his": None,
    "850": 850,
    "250": 250,
}
_sw_industry_limit_detail_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
SW_INDUSTRY_LIMIT_DETAIL_CACHE_TTL = 60
SW_INDUSTRY_LIMIT_DETAIL_CACHE_MAX = 512
_sw_industry_pct_t_detail_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
SW_INDUSTRY_PCT_T_DETAIL_CACHE_TTL = 20
SW_INDUSTRY_PCT_T_DETAIL_CACHE_MAX = 256
_sw_amt_pctile_series_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
SW_AMT_PCTILE_SERIES_CACHE_TTL = 300.0
SW_AMT_PCTILE_SERIES_CACHE_MAX = 64
# 申万页新高/低明细：主表刷新时预热的命中集合（明细点击秒开，避免重复 HL_C/stockstat 重型查询）
_sw_limit_flagged_pool_cache: OrderedDict[tuple, tuple[float, dict[str, set[str]], dict[str, tuple[str, str]]]] = OrderedDict()
# 与 SW_LIMIT_COUNT_MAPS_CACHE_TTL 对齐：主表计数与明细名单必须同源，禁止明细池先过期去「现查」
SW_LIMIT_FLAGGED_POOL_TTL = 120.0
SW_LIMIT_FLAGGED_POOL_MAX = 32
_sw_stock_basic_meta_cache: dict[str, tuple[float, dict]] = {}
SW_STOCK_BASIC_META_TTL = 86400
# 实时层 TTL：前端 2s 轮询；热路径只刷 rt_sw_k+短日线窗，成交额分位约 30s 重算一次。
# 新高低全扫另有 SW_LIMIT_COUNT_MAPS_CACHE_TTL=120。
SW_LIVE_MARKET_MAPS_CACHE_TTL = 2.0
# 休市时段实时层刷新间隔（行情不变，不必 2s 级重算）；=0 恢复全天 2s 旧行为
SW_LIVE_OFFHOURS_REFRESH_SEC = float(os.getenv('SW_LIVE_OFFHOURS_REFRESH_SEC', '300'))
SW_LIVE_HEAVY_REFRESH_SEC = 30.0
_swLiveHeavyRefreshMono = 0.0
_swLiveHeavyRefreshLock = threading.Lock()
# 申万行业指数单日涨跌幅不可能到 ±100；盘前 Tushare 常给 close=0 / pct=-100
SW_RT_QUOTE_MAX_ABS_PCT = 30.0
SW_LIVE_MARKET_MAPS_CACHE_MAX = 24
_swLiveMarketMapsCache: OrderedDict[tuple, tuple[float, tuple]] = OrderedDict()
# 新高低 12 列全市场扫描成本高（stockstat+HL_C，约 5k+ 成分）；同成分池跨 L1/L2 复用
SW_LIMIT_COUNT_MAPS_CACHE_TTL = 120.0
SW_LIMIT_COUNT_MAPS_CACHE_MAX = 8
_swLimitCountMapsCache: OrderedDict[tuple, tuple[float, tuple]] = OrderedDict()
# 缓存命中时实时层 Stale-While-Revalidate：未命中 live 缓存则后台刷新，接口秒返旧值
_swPayloadMutLock = threading.Lock()
_swLiveRefreshLock = threading.Lock()
_swLiveRefreshInFlight: set[tuple] = set()


def isSwIndustryRefreshing() -> bool:
    """实时层后台刷新或完整统计冷建进行中，供 top_moves 等热路径让路。"""
    with _swLiveRefreshLock:
        liveN = len(_swLiveRefreshInFlight)
    with _swFullComputeLock:
        fullN = len(_swFullComputeScheduled)
    return liveN > 0 or fullN > 0

SW_LIVE_BG_REFRESH_INTERVAL_SEC = 75.0
# 财报批量（按成分股池）：L1/L2 全市场同 ts_codes 可复用，避免重复 10~15s 打库
SW_FINA_BATCH_CACHE_MAX = 6
_swFinaBatchCache: OrderedDict[tuple, dict] = OrderedDict()
# index_sw_daily 面板：预热/HTTP/后台刷新禁止同窗并发打 7 年全表
SW_DAILY_PANEL_CACHE_TTL = 1800.0
SW_DAILY_PANEL_CACHE_MAX = 8
_swDailyPanelCache: OrderedDict[tuple, tuple[float, tuple]] = OrderedDict()
# 区间涨跌幅 T-120 只需约 170 个自然日；7 年面板仅成交额分位后台用
SW_RANGE_PANEL_LOOKBACK_DAYS = 220
SW_AMT_PANEL_LOOKBACK_DAYS = 366 * 7 + 30
# 冷路径 singleflight：同 key 只算一遍，其余等结果
SW_COMPUTE_GATE_WAIT_SEC = 180.0
_swComputeGates: dict[str, threading.Event] = {}
_swFullComputeLock = threading.Lock()
_swFullComputeScheduled: set[str] = set()
# index_member_all_sw 成分映射（日内稳定）
SW_MEMBER_ROWS_CACHE_TTL = 3600.0
_swMemberRowsCache: tuple[float, list] | None = None

# 申万行业页：财报/聚合重算成本高，进程内长期有效（重启失效）；缓存命中时仍刷新实时层（区间涨跌幅/成交额比%/新高低/融资余额）
SW_INDUSTRY_STATS_CACHE_TTL = float('inf')
SW_INDUSTRY_STATS_CACHE_MAX = 50
# 响应 JSON 的 cache_ttl_sec：不可为 inf；0 表示进程内缓存、与 fina 策略一致
SW_INDUSTRY_STATS_CACHE_TTL_API_SEC = 0
_sw_industry_stats_cache: OrderedDict[str, tuple[float, dict, dict, str]] = OrderedDict()
_index_industry_stats_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
# index_weight 最新一期成分（月频，启动预热会连打 10+ 次；命中后避免与 arturn 抢 8s 短超时连接）
_INDEX_WEIGHT_LATEST_TTL_SEC = 6 * 3600.0
_INDEX_WEIGHT_LATEST_CACHE_MAX = 32
_indexWeightLatestCache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
INDEX_MONITOR_CODES = [
    ("000510.SH", "A500"),
    ("000300.SH", "沪深300"),
    ("000905.SH", "中证500"),
    ("000852.SH", "中证1000"),
    ("932000.CSI", "中证2000"),
    ("899050.BJ", "北证50"),
    ("000016.SH", "上证50"),
    ("000680.SH", "科创综指"),
    ("000688.SH", "科创50"),
    ("399006.SZ", "创业板指"),
    ("399102.SZ", "创业板综"),
]
# 宽基总览汇总行 / 成交额比% 基准：国证 A 指（index_daily 千元 + rt_idx_k 元）
INDEX_STATS_SUMMARY_BENCH_TS = '399317.SZ'
# 区间涨跌幅默认拼 rt；空集=全部可拼（932000/899050 由 tdx_opt_live → rt_idx_k）
INDEX_PCT_NO_RT_SPLICE_TS = frozenset()
_INDEX_LIVE_PCT_KEYS = (
    'sw_pct', 'sw_pct_t1', 'sw_pct_t5', 'sw_pct_t20', 'sw_pct_t60', 'sw_pct_t120',
)
_INDEX_LIVE_AMT_KEYS = (
    'sw_amt_pctile_t0', 'sw_amt_pctile_t1', 'sw_amt_pctile_t5',
    'sw_amt_pctile_t20', 'sw_amt_pctile_t60', 'sw_amt_pctile_t120',
)


def _index_live_pct_null() -> dict[str, float | None]:
    return {k: None for k in _INDEX_LIVE_PCT_KEYS}


def _index_live_amt_null() -> dict[str, float | None]:
    return {k: None for k in _INDEX_LIVE_AMT_KEYS}


def _index_aggregate_hl_by_constituents(
    flagged: set[str],
    constituentsByGroup: dict[str, set[str]],
) -> dict[str, int]:
    """命中个股按指数成分计数；同一股票可计入多只宽基。"""
    flaggedU = {str(x or '').strip().upper() for x in flagged if x}
    out: dict[str, int] = {}
    for group, cons in constituentsByGroup.items():
        out[str(group)] = sum(1 for tc in cons if str(tc or '').strip().upper() in flaggedU)
    return out


def _parse_rt_idx_trade_time_to_date(tradeTime) -> date | None:
    """rt_idx_k.trade_time 可能为 str/datetime/date，统一为交易日 date。"""
    if tradeTime is None:
        return None
    if isinstance(tradeTime, datetime):
        return tradeTime.date()
    if isinstance(tradeTime, date):
        return tradeTime
    s = str(tradeTime).strip()
    if len(s) >= 10:
        try:
            return datetime.strptime(s[:10], '%Y-%m-%d').date()
        except ValueError:
            pass
    return None


def _sw_live_fingerprint_row(row: dict) -> str:
    """实时层字段摘要，供轮询 data_version 检测变更。"""
    keys = (
        'sw_pct', 'sw_pct_t1', 'sw_pct_t5', 'sw_pct_t20', 'sw_pct_t60', 'sw_pct_t120',
        'sw_amt_pctile_t0', 'sw_amt_pctile_t1', 'sw_amt_pctile_t5',
        'sw_amt_pctile_t20', 'sw_amt_pctile_t60', 'sw_amt_pctile_t120',
        'sw_h250_t0', 'sw_h850', 'sw_h_his', 'sw_l250', 'sw_l850', 'sw_l_his',
        'sw_hlc_h250', 'sw_hlc_h850', 'sw_hlc_h_his',
        'sw_hlc_l250', 'sw_hlc_l850', 'sw_hlc_l_his',
        'sw_margin_rzye', 'sw_margin_1d', 'sw_margin_3d', 'sw_margin_5d',
        'sw_margin_20d', 'sw_margin_60d',
        'total_mv_yi', 'free_mv_yi', 'weight_pct',
    )
    return '|'.join(f'{k}={row.get(k)}' for k in keys)


def _sw_build_structural_version(payload: dict) -> str:
    """表头/列结构/成分列表指纹；不含实时行情层。"""
    level = str(payload.get('level') or '')
    td = str(payload.get('trade_date') or '')
    count = int(payload.get('count') or 0)
    index_code = str(payload.get('index_code') or '')
    periods = payload.get('periods') or []
    period_sig = hashlib.md5(','.join(str(p) for p in periods).encode()).hexdigest()[:8]
    has_dtp = '1' if payload.get('has_q_dtprofit') else '0'
    has_ext = '1' if payload.get('has_sw_extend_metrics') else '0'
    codes = ','.join(
        sorted(str(r.get('l1_code') or '') for r in (payload.get('rows') or []) if isinstance(r, dict))
    )
    code_hash = hashlib.md5(codes.encode()).hexdigest()[:10]
    return (
        f'{level}|{td}|{count}|p{len(periods)}:{period_sig}'
        f'|d{has_dtp}e{has_ext}|idx:{index_code}|{code_hash}'
    )


def _sw_build_data_version(payload: dict) -> str:
    """含实时层 + 财报占位状态；pending 清掉或营收/市值补齐时必须变，否则前端永跳过重绘。"""
    struct = _sw_build_structural_version(payload)
    live_parts: list[str] = []
    fina_parts: list[str] = []
    for row in payload.get('rows') or []:
        if isinstance(row, dict):
            live_parts.append(_sw_live_fingerprint_row(row))
            fina_parts.append(
                f"{row.get('s0')}:{row.get('y0')}:{row.get('dtp0')}:"
                f"{row.get('total_mv_yi')}:{row.get('free_mv_yi')}"
            )
    summary = payload.get('summary_row')
    if isinstance(summary, dict):
        live_parts.append('S:' + _sw_live_fingerprint_row(summary))
        fina_parts.append(
            f"S:{summary.get('s0')}:{summary.get('total_mv_yi')}:{summary.get('free_mv_yi')}"
        )
    live_hash = hashlib.md5('\n'.join(live_parts).encode()).hexdigest()[:12]
    fina_hash = hashlib.md5('\n'.join(fina_parts).encode()).hexdigest()[:10]
    margin = str(payload.get('margin_as_of') or '')
    pending = '1' if payload.get('pending_fina') else '0'
    return f'{struct}|live:{live_hash}|mg:{margin}|pf:{pending}|fn:{fina_hash}'


def _sw_attach_payload_versions(payload: dict) -> None:
    payload['structural_version'] = _sw_build_structural_version(payload)
    payload['data_version'] = _sw_build_data_version(payload)


def _swWaitOrLeadCompute(gateName: str) -> tuple[bool, threading.Event | None]:
    """同 key 只让一个线程计算。返回 (是否主算, 主算 Event)；等待方为 (False, None)。"""
    with _swCacheLock:
        ev = _swComputeGates.get(gateName)
        if ev is None:
            ev = threading.Event()
            _swComputeGates[gateName] = ev
            return True, ev
        waiterEv = ev
    if waiterEv.wait(timeout=SW_COMPUTE_GATE_WAIT_SEC):
        return False, None
    with _swCacheLock:
        cur = _swComputeGates.get(gateName)
        if cur is waiterEv:
            ev2 = threading.Event()
            _swComputeGates[gateName] = ev2
            return True, ev2
    return False, None


def _swFinishCompute(gateName: str, ev: threading.Event | None) -> None:
    if ev is None:
        return
    with _swCacheLock:
        if _swComputeGates.get(gateName) is ev:
            _swComputeGates.pop(gateName, None)
    ev.set()


def _swEmptyFinaBundle() -> dict:
    return {
        'rev_nested': {},
        'dtp_nested': {},
        'has_q_dtprofit': True,
        'cf_nested_sw': {},
        'margin_nested_sw': {},
        'bs_cf_nested_sw': {},
        'has_sw_extend_metrics': True,
        'sfp_map': {},
        'close_map': {},
    }


def _swScheduleFullSwStatsCompute(
    level: str,
    parentL1: str,
    parentL2: str,
    tradeDateKey: str,
) -> None:
    """后台跑完整财报包（_full=1），不堵 HTTP 首屏。"""
    cacheKey = f'{level}|{parentL1}|{parentL2}|{tradeDateKey}|x12'
    with _swFullComputeLock:
        if cacheKey in _swFullComputeScheduled:
            return
        hitPayload, _, _ = _sw_industry_stats_cache_get(cacheKey, time.time())
        # 仅当已有「完整包」才跳过；pending_fina 占位必须继续后台补齐
        if hitPayload is not None and not hitPayload.get('pending_fina'):
            return
        _swFullComputeScheduled.add(cacheKey)

    def _worker() -> None:
        try:
            from rt_monitor.monolith import app

            q = f'/api/sw_industry_stats?level={level}&_full=1'
            if parentL1:
                q += f'&parent_l1_code={parentL1}'
            if parentL2:
                q += f'&parent_l2_code={parentL2}'
            if tradeDateKey:
                q += f'&trade_date={tradeDateKey}'
            # 直调视图，避免 test_client 嵌套 WSGI 在 SocketIO 下卡死
            with app.app_context():
                with app.test_request_context(q):
                    apiSwIndustryStats()
        except Exception as e:
            logger.warning('申万完整统计后台计算失败 key=%s: %s', cacheKey, e)
        finally:
            with _swFullComputeLock:
                _swFullComputeScheduled.discard(cacheKey)

    threading.Thread(
        target=_worker, daemon=True, name=f'sw-full-stats-{cacheKey[:16]}'
    ).start()


def _sw_industry_stats_cache_get(
    cache_key: str, now_ts: float
) -> tuple[dict | None, dict[str, tuple[str, str]] | None, str | None]:
    with _swCacheLock:
        hit = _sw_industry_stats_cache.get(cache_key)
        if hit is not None and now_ts < hit[0]:
            _sw_industry_stats_cache.move_to_end(cache_key)
            return hit[1], hit[2], hit[3]
    return None, None, None


def _sw_industry_stats_cache_put(
    cache_key: str,
    payload: dict,
    ts_to_ind: dict[str, tuple[str, str]] | None,
    level: str,
    ttl: float,
    max_items: int,
) -> None:
    with _swCacheLock:
        while len(_sw_industry_stats_cache) >= max_items:
            _sw_industry_stats_cache.popitem(last=False)
        _sw_industry_stats_cache[cache_key] = (
            time.time() + ttl,
            payload,
            ts_to_ind or {},
            level,
        )
        _sw_industry_stats_cache.move_to_end(cache_key)

def _sw_normalize_trade_date_td(td: Any) -> date | None:
    if td is None:
        return None
    if isinstance(td, datetime):
        return td.date()
    if isinstance(td, date):
        return td
    return None


def _sw_empirical_pct_leq(sorted_hist: list[float], r: float) -> float | None:
    """经验分位：100 * (#{x<=r} / n)，sorted_hist 升序。"""
    if not sorted_hist:
        return None
    import bisect
    n = len(sorted_hist)
    hi = bisect.bisect_right(sorted_hist, r + 1e-12)
    return round(100.0 * float(hi) / float(n), 2)


def _sw_empirical_pct_leq_values(vals: list[float], r: float) -> float | None:
    """经验分位（无需预排序，适合滑动窗口）。"""
    if not vals:
        return None
    hi = sum(1 for x in vals if x <= r + 1e-12)
    return round(100.0 * float(hi) / float(len(vals)), 2)


def _normalize_sw_industry_index_ts_code(group_code: str) -> str | None:
    """index_member 的 l1/l2 行业代码多为无后缀；index_sw_daily / rt_sw_k 使用 *.SI。"""
    s = str(group_code or "").strip().upper()
    if not s:
        return None
    if s.endswith(".SI"):
        return s
    if "." in s:
        return s
    return f"{s}.SI"


def _sw_build_rt_sw_quote(
    cl_raw: Any,
    tt_raw: Any,
    pc_raw: Any,
    amt_raw: Any,
    *,
    preCloseRaw: Any = None,
) -> dict | None:
    """标准化 rt_sw_k 报价，并在 pct_change 缺失时由 close/pre_close 反算。"""
    cl_f: float | None = None
    try:
        if cl_raw is not None:
            cl_f = float(cl_raw)
            if cl_f <= 0:
                cl_f = None
    except (TypeError, ValueError):
        cl_f = None
    if cl_f is None:
        return None
    rt_d = _parse_rt_idx_trade_time_to_date(tt_raw)
    pc_f: float | None = None
    try:
        if pc_raw is not None:
            pc_f = float(pc_raw)
            if abs(pc_f) >= SW_RT_QUOTE_MAX_ABS_PCT:
                pc_f = None
    except (TypeError, ValueError):
        pc_f = None
    if pc_f is None:
        try:
            pre_close_f = float(preCloseRaw) if preCloseRaw is not None else None
            if pre_close_f is not None and pre_close_f > 0:
                derived_pct = (cl_f / pre_close_f - 1.0) * 100.0
                if abs(derived_pct) < SW_RT_QUOTE_MAX_ABS_PCT:
                    pc_f = round(derived_pct, 4)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    amt_yuan: float | None = None
    try:
        if amt_raw is not None:
            ay = float(amt_raw)
            if ay > 0:
                amt_yuan = ay
    except (TypeError, ValueError):
        amt_yuan = None
    return {
        "etf_last": round(cl_f, 4),
        "etf_rt_date": rt_d,
        "pct_change": pc_f,
        "amount_yuan": amt_yuan,
    }


def _fetch_rt_sw_k_quotes(ts_codes: list[str], conn) -> dict[str, dict]:
    """申万行业 rt_sw_k：实时涨跌幅优先 pct_change，缺失时由 close/pre_close 反算。"""
    out: dict[str, dict] = {}
    if not ts_codes or conn is None:
        return out
    uniq = list(dict.fromkeys(str(c or "").strip().upper() for c in ts_codes if c))
    if not uniq:
        return out
    chunk = 400
    try:
        for i in range(0, len(uniq), chunk):
            part = uniq[i : i + chunk]
            ph = ",".join(["%s"] * len(part))
            sql = (
                "SELECT ts_code, close, pre_close, trade_time, pct_change, amount "
                f"FROM stock_tushare.rt_sw_k WHERE ts_code IN ({ph})"
            )
            with conn.cursor() as cur:
                cur.execute(sql, tuple(part))
                for row in cur.fetchall() or []:
                    tc = str(row[0] or "").strip().upper()
                    if not tc:
                        continue
                    quote = _sw_build_rt_sw_quote(
                        row[1], row[3], row[4], row[5], preCloseRaw=row[2]
                    )
                    if quote is None:
                        continue
                    out[tc] = quote
    except Exception as e:
        logger.warning("申万行业统计：读取 rt_sw_k 失败: %s", e)
    return out


def _load_index_sw_daily_panel_bulk(
    conn,
    ts_codes: list[str],
    min_trade_date: date,
) -> tuple[dict[str, list[tuple]], dict[str, dict[date, float]]]:
    """批量读取 index_sw_daily：descRows 为交易日降序；amtByDate 为万元成交额索引。"""
    desc_rows: dict[str, list[tuple]] = {}
    amt_by_date: dict[str, dict[date, float]] = {}
    uniq = list(dict.fromkeys(str(c or '').strip().upper() for c in ts_codes if c))
    if not uniq or conn is None:
        return desc_rows, amt_by_date
    cacheKey = (tuple(sorted(uniq)), min_trade_date)
    nowTs = time.time()
    with _swCacheLock:
        hit = _swDailyPanelCache.get(cacheKey)
        if hit is not None and nowTs < hit[0]:
            _swDailyPanelCache.move_to_end(cacheKey)
            return hit[1]
    gateName = f'panel|{hash(cacheKey)}'
    isLeader, gateEv = _swWaitOrLeadCompute(gateName)
    if not isLeader:
        with _swCacheLock:
            hit = _swDailyPanelCache.get(cacheKey)
            if hit is not None and time.time() < hit[0]:
                return hit[1]
    try:
        desc_rows, amt_by_date = _query_index_sw_daily_panel(conn, uniq, min_trade_date)
        with _swCacheLock:
            while len(_swDailyPanelCache) >= SW_DAILY_PANEL_CACHE_MAX:
                _swDailyPanelCache.popitem(last=False)
            _swDailyPanelCache[cacheKey] = (
                time.time() + SW_DAILY_PANEL_CACHE_TTL,
                (desc_rows, amt_by_date),
            )
            _swDailyPanelCache.move_to_end(cacheKey)
        return desc_rows, amt_by_date
    finally:
        if isLeader:
            _swFinishCompute(gateName, gateEv)


def _query_index_sw_daily_panel(
    conn,
    uniq: list[str],
    min_trade_date: date,
) -> tuple[dict[str, list[tuple]], dict[str, dict[date, float]]]:
    """index_sw_daily 实际查询（无缓存）。"""
    desc_rows: dict[str, list[tuple]] = {}
    amt_by_date: dict[str, dict[date, float]] = {}
    idx_sw = 'stock_tushare.index_sw_daily'
    sql = (
        f'SELECT d.ts_code, d.trade_date, d.pct_change, d.`change`, d.close, d.amount '
        f'FROM {idx_sw} AS d '
        'WHERE d.ts_code IN ({ph}) AND d.trade_date >= %s '
        'AND d.close IS NOT NULL AND d.close > 0 '
        'ORDER BY d.ts_code ASC, d.trade_date DESC'
    )
    chunk = 80
    for i in range(0, len(uniq), chunk):
        part = uniq[i : i + chunk]
        if not part:
            continue
        ph = ', '.join(['%s'] * len(part))
        try:
            with conn.cursor() as cur:
                cur.execute(sql.format(ph=ph), (*part, min_trade_date))
                for r in cur.fetchall() or []:
                    if not r or not r[0]:
                        continue
                    tc = str(r[0]).strip().upper()
                    dd = _sw_normalize_trade_date_td(r[1])
                    if dd is None:
                        continue
                    try:
                        cq = float(r[4])
                        if cq <= 0:
                            continue
                    except (TypeError, ValueError):
                        continue
                    pc_raw, ch_raw, amt_raw = r[2], r[3], r[5]
                    try:
                        pre_close = float(cq) - float(ch_raw or 0)
                    except (TypeError, ValueError):
                        pre_close = cq
                    row_t = (dd, pc_raw, pre_close, cq, cq)
                    desc_rows.setdefault(tc, []).append(row_t)
                    if amt_raw is not None:
                        try:
                            af = float(amt_raw)
                            if af > 0:
                                amt_by_date.setdefault(tc, {})[dd] = af
                        except (TypeError, ValueError):
                            pass
        except Exception as e:
            logger.warning('申万 index_sw_daily 批量读取失败 chunk=%s: %s', i, e)
    return desc_rows, amt_by_date


def _sw_resolve_end_close_from_rt_or_rows(
    rows_desc: list[tuple],
    rt_quote: dict | None,
) -> tuple[date | None, float | None]:
    """期末交易日与收盘价：优先 rt_sw_k，否则取日线最近一日。"""
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
    if (end_close is None or end_trade_date is None) and rows_desc:
        try:
            td0, _, _, c0, _ = rows_desc[0]
            end_trade_date = _sw_normalize_trade_date_td(td0)
            end_close = float(c0)
        except (TypeError, ValueError, IndexError):
            pass
    return end_trade_date, end_close


def _sw_prior_cq_rows_before(
    conn,
    ts_code: str,
    end_trade_date: date,
    limit_n: int,
    *,
    rows_desc: list[tuple] | None = None,
) -> list[tuple]:
    """严格早于 end_trade_date 的 (trade_date, cq) 降序；优先用已加载日线，不足再查库。"""
    out: list[tuple] = []
    if rows_desc:
        for row in rows_desc:
            dd = _sw_normalize_trade_date_td(row[0])
            if dd is None or dd >= end_trade_date:
                continue
            try:
                cq = float(row[4])
                if cq > 0:
                    out.append((dd, cq))
            except (TypeError, ValueError, IndexError):
                continue
            if len(out) >= limit_n:
                return out
    if len(out) >= limit_n or conn is None:
        return out
    idx_sw = 'stock_tushare.index_sw_daily'
    prior_sql = (
        f'SELECT d.trade_date, d.close AS cq FROM {idx_sw} AS d '
        'WHERE d.ts_code = %s AND d.trade_date < %s '
        'AND d.close IS NOT NULL AND d.close > 0 '
        'ORDER BY d.trade_date DESC LIMIT %s'
    )
    try:
        with conn.cursor() as cur:
            cur.execute(prior_sql, (ts_code, end_trade_date, limit_n))
            for r in cur.fetchall() or []:
                dd = _sw_normalize_trade_date_td(r[0])
                if dd is None or r[1] is None:
                    continue
                try:
                    cq = float(r[1])
                    if cq > 0:
                        out.append((dd, cq))
                except (TypeError, ValueError):
                    continue
    except Exception as e:
        logger.warning('申万行业统计：prior cq 读取失败 %s: %s', ts_code, e)
    return out[:limit_n]


def _sw_index_sw_daily_ed_and_prior_dates(
    conn,
    ts_code: str,
    rt_quote: dict | None,
    *,
    rows_desc: list[tuple] | None = None,
) -> tuple[date | None, list[date]]:
    """期末 trade_date 与严格早于该日的交易日降序链（成交额比%锚定）。"""
    tc = str(ts_code or '').strip().upper()
    if not tc:
        return None, []
    rows = rows_desc
    if rows is None:
        if conn is None:
            return None, []
        idx_sw = 'stock_tushare.index_sw_daily'
        sql = (
            f'SELECT d.trade_date, d.pct_change, '
            f'(d.close - IFNULL(d.`change`, 0)), d.close, d.close '
            f'FROM {idx_sw} AS d '
            'WHERE d.ts_code = %s AND d.close IS NOT NULL AND d.close > 0 '
            'ORDER BY d.trade_date DESC LIMIT %s'
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (tc, 125))
                rows = cur.fetchall() or []
        except Exception as e:
            logger.warning('申万行业统计：ed/prior 日线读取失败 %s: %s', tc, e)
            return None, []
    if not rows:
        return None, []
    end_trade_date, _ = _sw_resolve_end_close_from_rt_or_rows(rows, rt_quote)
    if end_trade_date is None:
        return None, []
    prior_dates: list[date] = []
    for row in rows:
        dd = _sw_normalize_trade_date_td(row[0])
        if dd is None or dd >= end_trade_date:
            continue
        prior_dates.append(dd)
        if len(prior_dates) >= 130:
            break
    if len(prior_dates) < 130 and conn is not None:
        idx_sw = 'stock_tushare.index_sw_daily'
        prior_sql = (
            f'SELECT d.trade_date FROM {idx_sw} AS d '
            'WHERE d.ts_code = %s AND d.trade_date < %s '
            'ORDER BY d.trade_date DESC LIMIT %s'
        )
        try:
            with conn.cursor() as cur:
                cur.execute(prior_sql, (tc, end_trade_date, 130))
                prior_dates = []
                for r in cur.fetchall() or []:
                    dd = _sw_normalize_trade_date_td(r[0])
                    if dd:
                        prior_dates.append(dd)
        except Exception as e:
            logger.warning('申万行业统计：prior trade_date 读取失败 %s: %s', tc, e)
    return end_trade_date, prior_dates


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


def _read_index_sw_daily_interval_range_pct(
    conn,
    ts_code: str,
    rt_quote: dict | None,
    *,
    rows_desc: list[tuple] | None = None,
) -> dict:
    """申万行业指数：index_sw_daily 历史收盘 + rt_sw_k 期末，Wind 式 T-1/T-5/T-20/T-60/T-120（同 MA/指数涨跌幅表）。"""
    empty = {
        "range_pct_t1": None,
        "range_pct_t5": None,
        "range_pct_t20": None,
        "range_pct_t60": None,
        "range_pct_t120": None,
    }
    tc = str(ts_code or '').strip().upper()
    rows = rows_desc
    if rows is None:
        if not tc or conn is None:
            return empty
        idx_sw = 'stock_tushare.index_sw_daily'
        sql = (
            f'SELECT trade_date, pct_change AS pct_chg, '
            f'(close - IFNULL(`change`, 0)) AS pre_close, close, close AS cq '
            f'FROM {idx_sw} '
            'WHERE ts_code = %s AND close IS NOT NULL AND close > 0 '
            'ORDER BY trade_date DESC LIMIT %s'
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (tc, 125))
                rows = cur.fetchall() or []
        except Exception as e:
            logger.warning('申万行业统计：index_sw_daily 区间读取失败 %s: %s', ts_code, e)
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

    q = rt_quote or {}
    end_trade_date, end_close = _sw_resolve_end_close_from_rt_or_rows(rows, q)

    if end_close is not None and end_trade_date is not None:
        prior_rows = _sw_prior_cq_rows_before(
            conn, tc, end_trade_date, 130, rows_desc=rows
        )
        out['range_pct_t5'] = _wind_pct_from_prior_desc_rows(prior_rows, 5, end_close)
        out['range_pct_t20'] = _wind_pct_from_prior_desc_rows(prior_rows, 20, end_close)
        out['range_pct_t60'] = _wind_pct_from_prior_desc_rows(prior_rows, 60, end_close)
        out['range_pct_t120'] = _wind_pct_from_prior_desc_rows(prior_rows, 120, end_close)
    return out


def _load_index_sw_daily_amount_by_date(
    conn, ts_code: str, min_trade_date: date
) -> dict[date, float]:
    """index_sw_daily.amount（万元）按交易日索引，供成交额比%分位。"""
    out: dict[date, float] = {}
    tc = str(ts_code or "").strip().upper()
    if not tc or conn is None:
        return out
    idx_sw = "stock_tushare.index_sw_daily"
    sql = (
        f"SELECT d.trade_date, d.amount FROM {idx_sw} AS d "
        "WHERE d.ts_code = %s AND d.trade_date >= %s "
        "AND d.amount IS NOT NULL AND d.amount > 0 "
        "ORDER BY d.trade_date ASC"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (tc, min_trade_date))
            for r in cur.fetchall() or []:
                dd = _sw_normalize_trade_date_td(r[0])
                if dd is None or r[1] is None:
                    continue
                try:
                    af = float(r[1])
                    if af > 0:
                        out[dd] = af
                except (TypeError, ValueError):
                    continue
    except Exception as e:
        logger.warning("申万 index_sw_daily amount 读取失败 %s: %s", tc, e)
    return out


def _load_index_sw_daily_close_amount_maps(
    conn, ts_code: str, min_trade_date: date
) -> tuple[dict[date, float], dict[date, float]]:
    """index_sw_daily：按交易日索引 close、amount（万元）；仅 close>0 且 amount>0。"""
    closeBy: dict[date, float] = {}
    amtBy: dict[date, float] = {}
    tc = str(ts_code or '').strip().upper()
    if not tc or conn is None:
        return closeBy, amtBy
    idxSw = 'stock_tushare.index_sw_daily'
    sql = (
        f'SELECT d.trade_date, d.close, d.amount FROM {idxSw} AS d '
        'WHERE d.ts_code = %s AND d.trade_date >= %s '
        'AND d.close IS NOT NULL AND d.close > 0 '
        'AND d.amount IS NOT NULL AND d.amount > 0 '
        'ORDER BY d.trade_date ASC'
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (tc, min_trade_date))
            for r in cur.fetchall() or []:
                dd = _sw_normalize_trade_date_td(r[0])
                if dd is None:
                    continue
                try:
                    cf = float(r[1])
                    af = float(r[2])
                except (TypeError, ValueError):
                    continue
                if cf > 0 and af > 0:
                    closeBy[dd] = cf
                    amtBy[dd] = af
    except Exception as e:
        logger.warning('申万 index_sw_daily close+amount 读取失败 %s: %s', tc, e)
    return closeBy, amtBy


def _compute_sw_index_close_amt_pctile_series(
    conn, row_ts: str, years: int
) -> dict[str, Any]:
    """近 years 年：逐交易日 close + 成交额比%分位（与 Amt%(当日) 一致）。"""
    rowU = str(row_ts or '').strip().upper()
    benchU = str(SW_STATS_SUMMARY_ALL_A_SW_TS or '801003.SI').strip().upper()
    y = max(1, min(5, int(years)))
    empty: dict[str, Any] = {
        'success': True,
        'ts_code': rowU,
        'bench_ts': benchU,
        'years': y,
        'trade_date': [],
        'close': [],
        'amt_pctile': [],
        'error': None,
    }
    if not rowU or not benchU or rowU == benchU or conn is None:
        empty['success'] = False
        empty['error'] = '无效或与基准相同的 ts_code'
        return empty
    minD = date.today() - timedelta(days=int(y * 366) + 45)
    loPad = int(SW_AMT_RATIO_PCTILE_LOOKBACK_YEARS * 366 + 45)
    rowClose, rowAmt = _load_index_sw_daily_close_amount_maps(conn, rowU, minD)
    _, benchAmt = _load_index_sw_daily_close_amount_maps(conn, benchU, minD)
    dates = sorted(set(rowAmt.keys()) & set(benchAmt.keys()))
    if len(dates) < 2:
        empty['success'] = False
        empty['error'] = '有效交易日不足'
        return empty
    tradeOut: list[str] = []
    closeOut: list[float | None] = []
    pctOut: list[float | None] = []
    import bisect

    dailyRatio: list[float | None] = []
    for d in dates:
        rA = rowAmt.get(d)
        bA = benchAmt.get(d)
        rD: float | None = None
        if rA is not None and bA is not None:
            try:
                rf = float(rA)
                bf = float(bA)
            except (TypeError, ValueError):
                rf = bf = 0.0
            if bf > 0 and rf > 0:
                rD = 100.0 * rf / bf
        dailyRatio.append(rD)

    for i, d in enumerate(dates):
        tradeOut.append(d.isoformat())
        closeOut.append(round(float(rowClose.get(d) or 0.0), 4) if rowClose.get(d) else None)
        if i == 0:
            pctOut.append(None)
            continue
        rD = dailyRatio[i]
        if rD is None:
            pctOut.append(None)
            continue
        dHi = dates[i - 1]
        lo0 = dHi - timedelta(days=loPad)
        start = bisect.bisect_left(dates, lo0)
        histRaw = [dailyRatio[j] for j in range(start, i) if dailyRatio[j] is not None]
        pctOut.append(_sw_empirical_pct_leq_values(histRaw, rD))
    try:
        if tradeOut:
            rtMap = _fetch_rt_sw_k_quotes([rowU, benchU], conn)
            rq = rtMap.get(rowU) or {}
            bq = rtMap.get(benchU) or {}
            edRt, priorRt = _sw_index_sw_daily_ed_and_prior_dates(conn, rowU, rq)
            if priorRt and rq.get('etf_last') is not None and rq.get('etf_rt_date') is not None:
                dHistHi0 = priorRt[0]
                lo0r = dHistHi0 - timedelta(days=loPad)
                histRt: list[float] = []
                for d, rv in rowAmt.items():
                    if d < lo0r or d > dHistHi0:
                        continue
                    bv = benchAmt.get(d)
                    if bv is None or rv is None:
                        continue
                    try:
                        bfv = float(bv)
                        rfv = float(rv)
                    except (TypeError, ValueError):
                        continue
                    if bfv <= 0 or rfv <= 0:
                        continue
                    histRt.append(100.0 * rfv / bfv)
                histRt.sort()
                rT0: float | None = None
                try:
                    ry = rq.get('amount_yuan')
                    by = bq.get('amount_yuan')
                    if ry is not None and by is not None:
                        fy = float(ry)
                        fb = float(by)
                        if fy > 0 and fb > 0:
                            rT0 = 100.0 * (fy / 10000.0) / (fb / 10000.0)
                except (TypeError, ValueError):
                    rT0 = None
                if rT0 is None and edRt is not None:
                    bAmt = benchAmt.get(edRt)
                    rAmt = rowAmt.get(edRt)
                    if bAmt is not None and rAmt is not None:
                        try:
                            bf = float(bAmt)
                            rf = float(rAmt)
                            if bf > 0 and rf > 0:
                                rT0 = 100.0 * rf / bf
                        except (TypeError, ValueError):
                            pass
                pctRt = (
                    _sw_empirical_pct_leq(histRt, rT0)
                    if (histRt and rT0 is not None)
                    else None
                )
                rtDRaw = rq.get('etf_rt_date')
                if isinstance(rtDRaw, datetime):
                    rtD = rtDRaw.date()
                elif isinstance(rtDRaw, date):
                    rtD = rtDRaw
                else:
                    rtD = None
                if rtD is not None:
                    try:
                        clRt = float(rq['etf_last'])
                    except (TypeError, ValueError, KeyError):
                        clRt = None
                    if clRt is not None and clRt > 0:
                        try:
                            lastD = date.fromisoformat(str(tradeOut[-1])[:10])
                        except ValueError:
                            lastD = None
                        clCell = round(float(clRt), 4)
                        if lastD is None or rtD > lastD:
                            tradeOut.append(rtD.isoformat())
                            closeOut.append(clCell)
                            pctOut.append(pctRt)
                        elif rtD == lastD:
                            closeOut[-1] = clCell
                            if pctRt is not None:
                                pctOut[-1] = pctRt
    except Exception as eRt:
        logger.debug('sw_index_amt_pctile_series 拼接 rt 末点失败: %s', eRt)
    return {
        'success': True,
        'ts_code': rowU,
        'bench_ts': benchU,
        'years': y,
        'trade_date': tradeOut,
        'close': closeOut,
        'amt_pctile': pctOut,
        'error': None,
    }


def _read_sw_amount_ratio_percentiles(
    conn,
    row_ts: str,
    rt_quote: dict | None,
    bench_ts: str,
    bench_map: dict[date, float],
    row_map: dict[date, float],
    *,
    bench_rt_quote: dict | None = None,
    lookback_years: int = SW_AMT_RATIO_PCTILE_LOOKBACK_YEARS,
    rows_desc: list[tuple] | None = None,
) -> dict[str, float | None]:
    """成交额比%分位：T-1..T-120 的 ratio=100*行万元/801003万元，锚定日=prior[k-1]；
    T-0：当日(锚定 session 日)优先 rt 量比×100，否则回退锚定日日线比；分位样本为对应窗口日线万元比。"""
    spec = (
        ("sw_amt_pctile_t1", 1),
        ("sw_amt_pctile_t5", 5),
        ("sw_amt_pctile_t20", 20),
        ("sw_amt_pctile_t60", 60),
        ("sw_amt_pctile_t120", 120),
    )
    out: dict[str, float | None] = {"sw_amt_pctile_t0": None}
    for k, _ in spec:
        out[k] = None
    rtu = str(row_ts or "").strip().upper()
    btu = str(bench_ts or "").strip().upper()
    if not rtu or not btu or rtu == btu or conn is None:
        return out
    _ed, prior = _sw_index_sw_daily_ed_and_prior_dates(
        conn, rtu, rt_quote, rows_desc=rows_desc
    )
    if not prior:
        return out
    lo_pad = int(lookback_years * 366 + 45)
    d_hist_hi = prior[0]
    lo0 = d_hist_hi - timedelta(days=lo_pad)
    hist0: list[float] = []
    for d, rv in row_map.items():
        if d < lo0 or d > d_hist_hi:
            continue
        bv = bench_map.get(d)
        if bv is None or rv is None:
            continue
        try:
            bfv = float(bv)
            rfv = float(rv)
        except (TypeError, ValueError):
            continue
        if bfv <= 0 or rfv <= 0:
            continue
        hist0.append(100.0 * rfv / bfv)
    hist0.sort()
    r_t0: float | None = None
    rq = rt_quote or {}
    bq = bench_rt_quote or {}
    try:
        ry = rq.get("amount_yuan")
        by = bq.get("amount_yuan")
        if ry is not None and by is not None:
            fy = float(ry)
            fb = float(by)
            if fy > 0 and fb > 0:
                r_t0 = 100.0 * (fy / 10000.0) / (fb / 10000.0)
    except (TypeError, ValueError):
        r_t0 = None
    if r_t0 is None and _ed is not None:
        b_amt = bench_map.get(_ed)
        r_amt = row_map.get(_ed)
        if b_amt is not None and r_amt is not None:
            try:
                bf = float(b_amt)
                rf = float(r_amt)
                if bf > 0 and rf > 0:
                    r_t0 = 100.0 * rf / bf
            except (TypeError, ValueError):
                pass
    if r_t0 is not None and hist0:
        out["sw_amt_pctile_t0"] = _sw_empirical_pct_leq(hist0, r_t0)
    for key, kk in spec:
        if kk > len(prior):
            continue
        d_anchor = prior[kk - 1]
        b_amt = bench_map.get(d_anchor)
        r_amt = row_map.get(d_anchor)
        if b_amt is None or r_amt is None:
            continue
        try:
            bf = float(b_amt)
            rf = float(r_amt)
        except (TypeError, ValueError):
            continue
        if bf <= 0 or rf <= 0:
            continue
        r_now = 100.0 * rf / bf
        lo = d_anchor - timedelta(days=lo_pad)
        hist_raw: list[float] = []
        for d, rv in row_map.items():
            if d < lo or d > d_anchor:
                continue
            bv = bench_map.get(d)
            if bv is None or rv is None:
                continue
            try:
                bfv = float(bv)
                rfv = float(rv)
            except (TypeError, ValueError):
                continue
            if bfv <= 0 or rfv <= 0:
                continue
            hist_raw.append(100.0 * rfv / bfv)
        hist_raw.sort()
        out[key] = _sw_empirical_pct_leq(hist_raw, r_now)
    return out


def _fetch_rt_idx_k_stats_quotes(ts_codes: list[str], conn) -> dict[str, dict]:
    """rt_idx_k：etf_last、etf_rt_date、pct_change（由 close/pre_close）、amount_yuan。"""
    out: dict[str, dict] = {}
    if not ts_codes or conn is None:
        return out
    uniq = list(dict.fromkeys(str(c or '').strip().upper() for c in ts_codes if c))
    if not uniq:
        return out
    chunk = 80
    try:
        for i in range(0, len(uniq), chunk):
            part = uniq[i:i + chunk]
            ph = ','.join(['%s'] * len(part))
            sql = (
                'SELECT ts_code, close, pre_close, trade_time, amount '
                f'FROM stock_tushare.rt_idx_k WHERE ts_code IN ({ph})'
            )
            with conn.cursor() as cur:
                cur.execute(sql, tuple(part))
                for row in cur.fetchall() or []:
                    tc = str(row[0] or '').strip().upper()
                    if not tc:
                        continue
                    clRaw, preRaw, ttRaw, amtRaw = row[1], row[2], row[3], row[4]
                    clF: float | None = None
                    try:
                        if clRaw is not None:
                            clF = float(clRaw)
                            if clF <= 0:
                                clF = None
                    except (TypeError, ValueError):
                        clF = None
                    pctF: float | None = None
                    try:
                        if clF is not None and preRaw is not None:
                            pf = float(preRaw)
                            if pf > 0:
                                pctF = (clF / pf - 1.0) * 100.0
                    except (TypeError, ValueError):
                        pctF = None
                    amtYuan: float | None = None
                    try:
                        if amtRaw is not None:
                            ay = float(amtRaw)
                            if ay > 0:
                                amtYuan = ay
                    except (TypeError, ValueError):
                        amtYuan = None
                    out[tc] = {
                        'etf_last': round(clF, 4) if clF is not None else None,
                        'etf_rt_date': _parse_rt_idx_trade_time_to_date(ttRaw),
                        'pct_change': pctF,
                        'amount_yuan': amtYuan,
                    }
    except Exception as e:
        logger.warning('宽基行业统计：读取 rt_idx_k 失败: %s', e)
    return out


def _load_index_daily_panel_bulk(
    conn,
    ts_codes: list[str],
    minTradeDate: date,
) -> tuple[dict[str, list[tuple]], dict[str, dict[date, float]]]:
    """批量读取 index_daily：descRows 交易日降序；amtByDate 为千元成交额。"""
    descRows: dict[str, list[tuple]] = {}
    amtByDate: dict[str, dict[date, float]] = {}
    uniq = list(dict.fromkeys(str(c or '').strip().upper() for c in ts_codes if c))
    if not uniq or conn is None:
        return descRows, amtByDate
    sql = (
        'SELECT d.ts_code, d.trade_date, d.pct_chg, d.pre_close, d.close, d.amount '
        'FROM stock_tushare.index_daily AS d '
        'WHERE d.ts_code IN ({ph}) AND d.trade_date >= %s '
        'AND d.close IS NOT NULL AND d.close > 0 '
        'ORDER BY d.ts_code ASC, d.trade_date DESC'
    )
    chunk = 20
    for i in range(0, len(uniq), chunk):
        part = uniq[i:i + chunk]
        if not part:
            continue
        ph = ', '.join(['%s'] * len(part))
        try:
            with conn.cursor() as cur:
                cur.execute(sql.format(ph=ph), (*part, minTradeDate))
                for r in cur.fetchall() or []:
                    if not r or not r[0]:
                        continue
                    tc = str(r[0]).strip().upper()
                    dd = _sw_normalize_trade_date_td(r[1])
                    if dd is None:
                        continue
                    try:
                        cq = float(r[4])
                        if cq <= 0:
                            continue
                    except (TypeError, ValueError):
                        continue
                    pcRaw, preRaw, amtRaw = r[2], r[3], r[5]
                    try:
                        preClose = float(preRaw) if preRaw is not None else cq
                    except (TypeError, ValueError):
                        preClose = cq
                    descRows.setdefault(tc, []).append((dd, pcRaw, preClose, cq, cq))
                    if amtRaw is not None:
                        try:
                            af = float(amtRaw)
                            if af > 0:
                                amtByDate.setdefault(tc, {})[dd] = af
                        except (TypeError, ValueError):
                            pass
        except Exception as e:
            logger.warning('宽基 index_daily 批量读取失败 chunk=%s: %s', i, e)
    return descRows, amtByDate


def _index_daily_ed_and_prior_dates(
    conn,
    ts_code: str,
    rt_quote: dict | None,
    *,
    rows_desc: list[tuple] | None = None,
) -> tuple[date | None, list[date]]:
    """index_daily 期末日与严格早于该日的交易日降序链。"""
    tc = str(ts_code or '').strip().upper()
    if not tc:
        return None, []
    rows = rows_desc
    if rows is None:
        if conn is None:
            return None, []
        sql = (
            'SELECT trade_date, pct_chg, pre_close, close, close '
            'FROM stock_tushare.index_daily '
            'WHERE ts_code = %s AND close IS NOT NULL AND close > 0 '
            'ORDER BY trade_date DESC LIMIT %s'
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (tc, 125))
                rows = cur.fetchall() or []
        except Exception as e:
            logger.warning('宽基行业统计：ed/prior 日线读取失败 %s: %s', ts_code, e)
            return None, []
    if not rows:
        return None, []
    endTradeDate, _ = _sw_resolve_end_close_from_rt_or_rows(rows, rt_quote)
    if endTradeDate is None:
        return None, []
    priorDates: list[date] = []
    for row in rows:
        dd = _sw_normalize_trade_date_td(row[0])
        if dd is None or dd >= endTradeDate:
            continue
        priorDates.append(dd)
        if len(priorDates) >= 130:
            break
    if len(priorDates) < 130 and conn is not None:
        priorSql = (
            'SELECT trade_date FROM stock_tushare.index_daily '
            'WHERE ts_code = %s AND trade_date < %s '
            'ORDER BY trade_date DESC LIMIT %s'
        )
        try:
            with conn.cursor() as cur:
                cur.execute(priorSql, (tc, endTradeDate, 130))
                priorDates = []
                for r in cur.fetchall() or []:
                    dd = _sw_normalize_trade_date_td(r[0])
                    if dd:
                        priorDates.append(dd)
        except Exception as e:
            logger.warning('宽基行业统计：prior trade_date 读取失败 %s: %s', ts_code, e)
    return endTradeDate, priorDates


def _index_daily_prior_cq_rows(
    conn,
    ts_code: str,
    endTradeDate: date,
    limitN: int,
    *,
    rows_desc: list[tuple] | None = None,
) -> list[tuple]:
    """严格早于 endTradeDate 的 (trade_date, cq) 降序。"""
    out: list[tuple] = []
    if rows_desc:
        for row in rows_desc:
            dd = _sw_normalize_trade_date_td(row[0])
            if dd is None or dd >= endTradeDate:
                continue
            try:
                cq = float(row[4])
                if cq > 0:
                    out.append((dd, cq))
            except (TypeError, ValueError, IndexError):
                continue
            if len(out) >= limitN:
                return out
    if len(out) >= limitN or conn is None:
        return out
    priorSql = (
        'SELECT trade_date, close AS cq FROM stock_tushare.index_daily '
        'WHERE ts_code = %s AND trade_date < %s '
        'AND close IS NOT NULL AND close > 0 '
        'ORDER BY trade_date DESC LIMIT %s'
    )
    try:
        with conn.cursor() as cur:
            cur.execute(priorSql, (ts_code, endTradeDate, limitN))
            for r in cur.fetchall() or []:
                dd = _sw_normalize_trade_date_td(r[0])
                if dd is None or r[1] is None:
                    continue
                try:
                    cq = float(r[1])
                    if cq > 0:
                        out.append((dd, cq))
                except (TypeError, ValueError):
                    continue
    except Exception as e:
        logger.warning('宽基行业统计：prior cq 读取失败 %s: %s', ts_code, e)
    return out[:limitN]


def _read_index_daily_interval_range_pct(
    conn,
    ts_code: str,
    rt_quote: dict | None,
    *,
    splice_rt: bool = True,
    rows_desc: list[tuple] | None = None,
) -> dict:
    """宽基区间涨跌幅：index_daily 历史收盘 + 可选 rt_idx_k 期末。"""
    empty = {
        'range_pct_t1': None,
        'range_pct_t5': None,
        'range_pct_t20': None,
        'range_pct_t60': None,
        'range_pct_t120': None,
    }
    tc = str(ts_code or '').strip().upper()
    rows = rows_desc
    if rows is None:
        if not tc or conn is None:
            return empty
        sql = (
            'SELECT trade_date, pct_chg, pre_close, close, close AS cq '
            'FROM stock_tushare.index_daily '
            'WHERE ts_code = %s AND close IS NOT NULL AND close > 0 '
            'ORDER BY trade_date DESC LIMIT %s'
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (tc, 125))
                rows = cur.fetchall() or []
        except Exception as e:
            logger.warning('宽基行业统计：index_daily 区间读取失败 %s: %s', ts_code, e)
            return empty
    if not rows:
        return empty

    def _cqAt(i: int):
        if i < 0 or i >= len(rows):
            return None
        try:
            f = float(rows[i][4]) if rows[i][4] is not None else None
            return f if f is not None and f > 0 else None
        except (TypeError, ValueError):
            return None

    def _pctRatio(off: int) -> float | None:
        c0 = _cqAt(0)
        ck = _cqAt(off)
        if c0 is None or ck is None or ck == 0:
            return None
        return round((c0 / ck - 1.0) * 100.0, 2)

    out = dict(empty)
    if len(rows) >= 2:
        out['range_pct_t1'] = _pctRatio(1)
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
    q = (rt_quote or {}) if splice_rt else {}
    endTradeDate, endClose = _sw_resolve_end_close_from_rt_or_rows(rows, q)
    if endClose is not None and endTradeDate is not None:
        priorRows = _index_daily_prior_cq_rows(
            conn, tc, endTradeDate, 130, rows_desc=rows
        )
        out['range_pct_t5'] = _wind_pct_from_prior_desc_rows(priorRows, 5, endClose)
        out['range_pct_t20'] = _wind_pct_from_prior_desc_rows(priorRows, 20, endClose)
        out['range_pct_t60'] = _wind_pct_from_prior_desc_rows(priorRows, 60, endClose)
        out['range_pct_t120'] = _wind_pct_from_prior_desc_rows(priorRows, 120, endClose)
    return out


def _read_index_amount_ratio_percentiles(
    conn,
    row_ts: str,
    rt_quote: dict | None,
    bench_ts: str,
    bench_map: dict[date, float],
    row_map: dict[date, float],
    *,
    bench_rt_quote: dict | None = None,
    lookback_years: int = SW_AMT_RATIO_PCTILE_LOOKBACK_YEARS,
    rows_desc: list[tuple] | None = None,
) -> dict[str, float | None]:
    """宽基成交额比%分位：历史 index_daily 千元比；T-0 用 rt_idx_k 元/元。"""
    spec = (
        ('sw_amt_pctile_t1', 1),
        ('sw_amt_pctile_t5', 5),
        ('sw_amt_pctile_t20', 20),
        ('sw_amt_pctile_t60', 60),
        ('sw_amt_pctile_t120', 120),
    )
    out: dict[str, float | None] = _index_live_amt_null()
    rtu = str(row_ts or '').strip().upper()
    btu = str(bench_ts or '').strip().upper()
    if not rtu or not btu or rtu == btu or conn is None:
        return out
    _ed, prior = _index_daily_ed_and_prior_dates(
        conn, rtu, rt_quote, rows_desc=rows_desc
    )
    if not prior:
        return out
    loPad = int(lookback_years * 366 + 45)
    dHistHi = prior[0]
    lo0 = dHistHi - timedelta(days=loPad)
    hist0: list[float] = []
    for d, rv in row_map.items():
        if d < lo0 or d > dHistHi:
            continue
        bv = bench_map.get(d)
        if bv is None or rv is None:
            continue
        try:
            bfv, rfv = float(bv), float(rv)
        except (TypeError, ValueError):
            continue
        if bfv <= 0 or rfv <= 0:
            continue
        hist0.append(100.0 * rfv / bfv)
    hist0.sort()
    rT0: float | None = None
    rq = rt_quote or {}
    bq = bench_rt_quote or {}
    try:
        ry, by = rq.get('amount_yuan'), bq.get('amount_yuan')
        if ry is not None and by is not None:
            fy, fb = float(ry), float(by)
            if fy > 0 and fb > 0:
                rT0 = 100.0 * fy / fb
    except (TypeError, ValueError):
        rT0 = None
    if rT0 is None and _ed is not None:
        bAmt, rAmt = bench_map.get(_ed), row_map.get(_ed)
        if bAmt is not None and rAmt is not None:
            try:
                bf, rf = float(bAmt), float(rAmt)
                if bf > 0 and rf > 0:
                    rT0 = 100.0 * rf / bf
            except (TypeError, ValueError):
                pass
    if rT0 is not None and hist0:
        out['sw_amt_pctile_t0'] = _sw_empirical_pct_leq(hist0, rT0)
    for key, kk in spec:
        if kk > len(prior):
            continue
        dAnchor = prior[kk - 1]
        bAmt, rAmt = bench_map.get(dAnchor), row_map.get(dAnchor)
        if bAmt is None or rAmt is None:
            continue
        try:
            bf, rf = float(bAmt), float(rAmt)
        except (TypeError, ValueError):
            continue
        if bf <= 0 or rf <= 0:
            continue
        rNow = 100.0 * rf / bf
        lo = dAnchor - timedelta(days=loPad)
        histRaw: list[float] = []
        for d, rv in row_map.items():
            if d < lo or d > dAnchor:
                continue
            bv = bench_map.get(d)
            if bv is None or rv is None:
                continue
            try:
                bfv, rfv = float(bv), float(rv)
            except (TypeError, ValueError):
                continue
            if bfv <= 0 or rfv <= 0:
                continue
            histRaw.append(100.0 * rfv / bfv)
        histRaw.sort()
        out[key] = _sw_empirical_pct_leq(histRaw, rNow)
    return out


def _summary_index_pct_bundle(conn, indexTs: str) -> dict[str, float | None]:
    """宽基汇总行：指定指数 Pct + Wind 区间；Amt% 恒为 null。"""
    empty = {**_index_live_pct_null(), **_index_live_amt_null()}
    idxTs = str(indexTs or '').strip().upper()
    if conn is None or not idxTs:
        return empty
    try:
        rtMap = _fetch_rt_idx_k_stats_quotes([idxTs], conn)
        rtQuote = rtMap.get(idxTs)
        splice = idxTs not in INDEX_PCT_NO_RT_SPLICE_TS
        rng = _read_index_daily_interval_range_pct(
            conn, idxTs, rtQuote if splice else None, splice_rt=splice
        )
        out = dict(empty)
        swPct = None
        if rtQuote and rtQuote.get('pct_change') is not None:
            try:
                swPct = round(float(rtQuote['pct_change']), 2)
            except (TypeError, ValueError):
                swPct = None
        if swPct is None:
            try:
                swPct = rng.get('range_pct_t1')
            except Exception:
                swPct = None
        out['sw_pct'] = swPct
        out['sw_pct_t1'] = rng.get('range_pct_t1')
        out['sw_pct_t5'] = rng.get('range_pct_t5')
        out['sw_pct_t20'] = rng.get('range_pct_t20')
        out['sw_pct_t60'] = rng.get('range_pct_t60')
        out['sw_pct_t120'] = rng.get('range_pct_t120')
        return out
    except Exception as e:
        logger.warning('宽基行业统计：汇总 %s 区间涨跌幅失败: %s', idxTs, e)
        return empty


def _index_collect_overview_live(
    conn,
    ind_meta: dict,
) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    """总览实时层：rt_idx_k 报价、区间涨跌幅、成交额比%分位。"""
    rtQuotes: dict[str, dict] = {}
    rangeCache: dict[str, dict] = {}
    amtCache: dict[str, dict] = {}
    benchTs = str(INDEX_STATS_SUMMARY_BENCH_TS or '399317.SZ').strip().upper()
    idxList: list[str] = []
    seen: set[str] = set()
    for idxCode, _name in INDEX_MONITOR_CODES:
        ic = str(idxCode or '').strip().upper()
        if ic in ind_meta and ic not in seen:
            seen.add(ic)
            idxList.append(ic)
    if benchTs and benchTs not in seen:
        seen.add(benchTs)
        idxList.append(benchTs)
    if not idxList or conn is None:
        return rtQuotes, rangeCache, amtCache
    rtQuotes = _fetch_rt_idx_k_stats_quotes(idxList, conn)
    minAmtD = date.today() - timedelta(days=366 * 7 + 30)
    descPanel, amtPanel = _load_index_daily_panel_bulk(conn, idxList, minAmtD)
    benchAmt = amtPanel.get(benchTs, {})
    benchRt = rtQuotes.get(benchTs)
    amtNull = _index_live_amt_null()
    for tsu in idxList:
        splice = tsu not in INDEX_PCT_NO_RT_SPLICE_TS
        rowsDesc = descPanel.get(tsu) or []
        rangeCache[tsu] = _read_index_daily_interval_range_pct(
            conn,
            tsu,
            rtQuotes.get(tsu) if splice else None,
            splice_rt=splice,
            rows_desc=rowsDesc or None,
        )
        if tsu == benchTs:
            amtCache[tsu] = dict(amtNull)
        else:
            amtCache[tsu] = _read_index_amount_ratio_percentiles(
                conn,
                tsu,
                rtQuotes.get(tsu),
                benchTs,
                benchAmt,
                amtPanel.get(tsu, {}),
                bench_rt_quote=benchRt,
                rows_desc=rowsDesc or None,
            )
    return rtQuotes, rangeCache, amtCache


def _index_collect_limit_hlc_maps(
    ts_codes: list[str],
    ts_to_ind: dict[str, tuple[str, str]],
    ts_by_l1: dict[str, list[str]],
    isOverview: bool,
    level: str,
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]], dict[str, int], dict[str, int]]:
    """新高/低 12 列：总览按成分多归属计数，下钻按 ts_to_ind；汇总按去重成分。"""
    emptyT0 = {jk: {} for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS}
    emptyHlc = {jk: {} for jk, _ in _SW_INDUSTRY_HLC_SPECS}
    zeroT0 = {jk: 0 for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS}
    zeroHlc = {jk: 0 for jk, _ in _SW_INDUSTRY_HLC_SPECS}
    if level == 'l3' or not ts_codes:
        return emptyT0, emptyHlc, zeroT0, zeroHlc
    scanMap = {tc: ('__scan__', '') for tc in ts_codes}
    try:
        _t0, _hlc, flagged = _sw_fetch_limit_count_maps_cached(scanMap, None)
    except Exception as e:
        logger.warning('index_industry_stats 新高/低分布计算失败: %s', e)
        return emptyT0, emptyHlc, zeroT0, zeroHlc
    universe = {str(t).strip().upper() for t in ts_codes if t}
    t0Sum = {
        jk: len({str(x).strip().upper() for x in (flagged.get(jk) or set()) if x} & universe)
        for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS
    }
    hlcSum = {
        jk: len({str(x).strip().upper() for x in (flagged.get(jk) or set()) if x} & universe)
        for jk, _ in _SW_INDUSTRY_HLC_SPECS
    }
    if isOverview:
        cons = {
            str(g): {str(t).strip().upper() for t in codes if t}
            for g, codes in ts_by_l1.items()
        }
        t0Maps = {
            jk: _index_aggregate_hl_by_constituents(flagged.get(jk) or set(), cons)
            for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS
        }
        hlcMaps = {
            jk: _index_aggregate_hl_by_constituents(flagged.get(jk) or set(), cons)
            for jk, _ in _SW_INDUSTRY_HLC_SPECS
        }
        return t0Maps, hlcMaps, t0Sum, hlcSum
    t0Maps, hlcMaps = _sw_aggregate_limit_maps_from_flagged(flagged, ts_to_ind)
    return t0Maps, hlcMaps, t0Sum, hlcSum


def _index_apply_pct_amt_to_row(
    row: dict,
    l1_code: str,
    *,
    isOverview: bool,
    level: str,
    rtQuotes: dict,
    rangeCache: dict,
    amtCache: dict,
) -> None:
    """写入区间涨跌幅与成交额比%分位。"""
    if level == 'l3':
        row.update(_index_live_pct_null())
        row.update(_index_live_amt_null())
        return
    idxTs = str(l1_code or '').strip().upper()
    if isOverview:
        lookup = idxTs
        rq = rtQuotes.get(lookup)
    else:
        lookup = _normalize_sw_industry_index_ts_code(l1_code)
        rq = rtQuotes.get(lookup) if lookup else None
    swPct = None
    if rq and rq.get('pct_change') is not None:
        try:
            swPct = round(float(rq['pct_change']), 2)
        except (TypeError, ValueError):
            swPct = None
    rd = rangeCache.get(lookup) if lookup else None
    if swPct is None and rd and rd.get('range_pct_t1') is not None:
        swPct = rd.get('range_pct_t1')
    row['sw_pct'] = swPct
    row['sw_pct_t1'] = (rd or {}).get('range_pct_t1')
    row['sw_pct_t5'] = (rd or {}).get('range_pct_t5')
    row['sw_pct_t20'] = (rd or {}).get('range_pct_t20')
    row['sw_pct_t60'] = (rd or {}).get('range_pct_t60')
    row['sw_pct_t120'] = (rd or {}).get('range_pct_t120')
    rdAmt = amtCache.get(lookup) if lookup else None
    row['sw_amt_pctile_t0'] = (rdAmt or {}).get('sw_amt_pctile_t0')
    row['sw_amt_pctile_t1'] = (rdAmt or {}).get('sw_amt_pctile_t1')
    row['sw_amt_pctile_t5'] = (rdAmt or {}).get('sw_amt_pctile_t5')
    row['sw_amt_pctile_t20'] = (rdAmt or {}).get('sw_amt_pctile_t20')
    row['sw_amt_pctile_t60'] = (rdAmt or {}).get('sw_amt_pctile_t60')
    row['sw_amt_pctile_t120'] = (rdAmt or {}).get('sw_amt_pctile_t120')


def _index_apply_hl_margin_to_row(
    row: dict,
    l1_code: str,
    *,
    level: str,
    t0Maps: dict,
    hlcMaps: dict,
    marginByGroup: dict,
) -> None:
    """写入新高低 12 列与融资余额。"""
    if level == 'l3':
        for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS:
            row[jk] = None
        for jk, _ in _SW_INDUSTRY_HLC_SPECS:
            row[jk] = None
        row.update(_sw_margin_empty_fields())
        return
    gc = str(l1_code or '').strip()
    for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS:
        row[jk] = int(t0Maps.get(jk, {}).get(gc, 0))
    for jk, _ in _SW_INDUSTRY_HLC_SPECS:
        row[jk] = int(hlcMaps.get(jk, {}).get(gc, 0))
    mf = marginByGroup.get(gc) or {}
    for mk in _sw_margin_empty_fields():
        row[mk] = mf.get(mk)


def _index_fill_summary_live(
    summary_row: dict,
    *,
    conn,
    isOverview: bool,
    indexCode: str,
    level: str,
    t0Summary: dict,
    hlcSummary: dict,
    marginSummary: dict,
) -> None:
    """汇总行：区间涨跌幅取国证A或所选宽基；Amt% 恒 null；新高低/融资按去重成分。"""
    if level == 'l3':
        summary_row.update(_index_live_pct_null())
        summary_row.update(_index_live_amt_null())
        for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS:
            summary_row[jk] = None
        for jk, _ in _SW_INDUSTRY_HLC_SPECS:
            summary_row[jk] = None
        summary_row.update(_sw_margin_empty_fields())
        return
    benchTs = (
        str(INDEX_STATS_SUMMARY_BENCH_TS or '').strip().upper()
        if isOverview
        else str(indexCode or '').strip().upper()
    )
    if benchTs:
        summary_row.update(_summary_index_pct_bundle(conn, benchTs))
    else:
        summary_row.update(_index_live_pct_null())
        summary_row.update(_index_live_amt_null())
    for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS:
        summary_row[jk] = int(t0Summary.get(jk, 0))
    for jk, _ in _SW_INDUSTRY_HLC_SPECS:
        summary_row[jk] = int(hlcSummary.get(jk, 0))
    summary_row.update(marginSummary)


def _sw_aggregate_h250_by_group(
    flagged_ts: set[str],
    ts_to_ind: dict[str, tuple[str, str]],
) -> dict[str, int]:
    """命中个股集合按当前 level 的 group_code 计数。"""
    counts: dict[str, int] = defaultdict(int)
    for ts in flagged_ts:
        info = ts_to_ind.get(ts)
        if not info:
            continue
        counts[str(info[0] or "").strip()] += 1
    return dict(counts)


def _sw_fetch_stockstat_t0_ts_codes_by_flag(
    conn_wind, ts_codes: list[str], flag_key: str
) -> set[str]:
    """与涨跌家数面板 stockstat 标志同源（不含 ST），再与 ts_codes 求交。flag_key 见 _SW_STOCKSTAT_T0_FLAG_SQL。"""
    wh = _SW_STOCKSTAT_T0_FLAG_SQL.get(flag_key)
    if not conn_wind or not ts_codes or not wh:
        return set()
    out: set[str] = set()
    chunk = 900
    for i in range(0, len(ts_codes), chunk):
        part = ts_codes[i : i + chunk]
        if not part:
            continue
        ph = ", ".join(["%s"] * len(part))
        sql = (
            "SELECT s.windcodes FROM stock_wind.stockstat s "
            + _LIMIT_STATS_SQL_JOIN_RT_K_ON_S
            + "WHERE " + wh + " "
            + _LIMIT_STATS_SQL_NOT_ST_RT_K
            + f" AND s.windcodes IN ({ph})"
        )
        with conn_wind.cursor() as cur:
            cur.execute(sql, tuple(part))
            for r in cur.fetchall() or []:
                if r and r[0]:
                    out.add(str(r[0]).strip())
    return out


def _sw_fetch_stockstat_t0_ts_codes_by_flag_all(
    conn_wind, flag_key: str
) -> set[str]:
    """全市场 stockstat 标志命中（不含 ST）；与申万成分求交后再聚合/明细。"""
    wh = _SW_STOCKSTAT_T0_FLAG_SQL.get(flag_key)
    if not conn_wind or not wh:
        return set()
    sql = (
        "SELECT s.windcodes FROM stock_wind.stockstat s "
        + _LIMIT_STATS_SQL_JOIN_RT_K_ON_S
        + "WHERE " + wh + " "
        + _LIMIT_STATS_SQL_NOT_ST_RT_K
    )
    out: set[str] = set()
    with conn_wind.cursor() as cur:
        cur.execute(sql)
        for r in cur.fetchall() or []:
            if r and r[0]:
                out.add(str(r[0]).strip())
    return out


def _sw_fetch_stockstat_t0_flag_sets_bulk(
    conn_wind, ts_codes: list[str]
) -> dict[str, set[str]]:
    """一次 SQL 拉取 6 个 stockstat 标志（仅申万成分股，避免全表扫描 6 次）。"""
    out: dict[str, set[str]] = {fk: set() for fk in _SW_STOCKSTAT_T0_FLAG_SQL}
    if not conn_wind or not ts_codes:
        return out
    flag_keys = list(_SW_STOCKSTAT_T0_FLAG_SQL.keys())
    select_sql = ", ".join(
        f"CASE WHEN {_SW_STOCKSTAT_T0_FLAG_SQL[fk]} THEN 1 ELSE 0 END AS {fk}"
        for fk in flag_keys
    )
    chunk = 5000
    for i in range(0, len(ts_codes), chunk):
        part = ts_codes[i : i + chunk]
        if not part:
            continue
        ph = ", ".join(["%s"] * len(part))
        sql = (
            f"SELECT s.windcodes, {select_sql} FROM stock_wind.stockstat s "
            + _LIMIT_STATS_SQL_JOIN_RT_K_ON_S
            + "WHERE 1=1 "
            + _LIMIT_STATS_SQL_NOT_ST_RT_K
            + f" AND s.windcodes IN ({ph})"
        )
        with conn_wind.cursor() as cur:
            cur.execute(sql, tuple(part))
            for r in cur.fetchall() or []:
                if not r or not r[0]:
                    continue
                ts = str(r[0]).strip()
                for offset, fk in enumerate(flag_keys, start=1):
                    if int(r[offset] or 0) != 0:
                        out[fk].add(ts)
    return out


def _sw_limit_flagged_pool_key(
    level: str,
    parent_l1_code: str,
    parent_l2_code: str,
    as_of_date: date | None,
) -> tuple[str, str, str, str]:
    return (
        str(level or "l1").strip().lower(),
        str(parent_l1_code or "").strip(),
        str(parent_l2_code or "").strip(),
        as_of_date.isoformat() if as_of_date else "",
    )


def _sw_limit_flagged_pool_get(
    pool_key: tuple[str, str, str, str],
    now_ts: float | None = None,
) -> tuple[dict[str, set[str]] | None, dict[str, tuple[str, str]] | None]:
    now_ts = time.time() if now_ts is None else now_ts
    with _swCacheLock:
        hit = _sw_limit_flagged_pool_cache.get(pool_key)
        if hit is not None and now_ts < hit[0]:
            _sw_limit_flagged_pool_cache.move_to_end(pool_key)
            return hit[1], hit[2]
    return None, None


def _sw_limit_flagged_pool_put(
    pool_key: tuple[str, str, str, str],
    flagged_by_col: dict[str, set[str]],
    ts_to_ind: dict[str, tuple[str, str]],
) -> None:
    if not flagged_by_col or not ts_to_ind:
        return
    with _swCacheLock:
        while len(_sw_limit_flagged_pool_cache) >= SW_LIMIT_FLAGGED_POOL_MAX:
            _sw_limit_flagged_pool_cache.popitem(last=False)
        _sw_limit_flagged_pool_cache[pool_key] = (
            time.time() + SW_LIMIT_FLAGGED_POOL_TTL,
            {k: set(v) for k, v in flagged_by_col.items()},
            dict(ts_to_ind),
        )
        _sw_limit_flagged_pool_cache.move_to_end(pool_key)


def _sw_stock_basic_meta_get(ts_code: str, now_ts: float | None = None) -> dict | None:
    now_ts = time.time() if now_ts is None else now_ts
    key = str(ts_code or "").strip()
    if not key:
        return None
    with _swCacheLock:
        hit = _sw_stock_basic_meta_cache.get(key)
        if hit is not None and now_ts < hit[0]:
            return dict(hit[1])
    return None


def _sw_warm_stock_basic_meta_cache(
    conn_tushare, ts_codes: set[str] | list[str]
) -> None:
    """批量预热 stock_basic 元数据，供明细弹窗秒开。"""
    if conn_tushare is None or not ts_codes:
        return
    now_ts = time.time()
    missing: list[str] = []
    for ts in ts_codes:
        key = str(ts or "").strip()
        if not key:
            continue
        with _swCacheLock:
            hit = _sw_stock_basic_meta_cache.get(key)
            if hit is None or now_ts >= hit[0]:
                missing.append(key)
    if not missing:
        return
    rows = _sw_fetch_stock_basic_limit_detail_rows(conn_tushare, missing)
    expiry = now_ts + SW_STOCK_BASIC_META_TTL
    with _swCacheLock:
        for row in rows:
            ts = str(row.get("ts_code") or "").strip()
            if ts:
                _sw_stock_basic_meta_cache[ts] = (expiry, dict(row))


def _sw_normalize_date_for_compare(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    if len(s) >= 10 and s[4] == '-':
        try:
            return datetime.strptime(s[:10], '%Y-%m-%d').date()
        except ValueError:
            pass
    digits = s.replace('-', '')[:8]
    if len(digits) == 8 and digits.isdigit():
        try:
            return datetime.strptime(digits, '%Y%m%d').date()
        except ValueError:
            pass
    return None


def _sw_fetch_rt_k_latest_trade_date(conn_wind) -> date | None:
    if conn_wind is None:
        return None
    try:
        with conn_wind.cursor() as cur:
            cur.execute(
                "SELECT DATE(MAX(trade_time)) FROM stock_wind.rt_k WHERE trade_time IS NOT NULL"
            )
            row = cur.fetchone()
        return _sw_normalize_date_for_compare(row[0] if row else None)
    except Exception:
        return None


def _sw_fetch_member_industry_paths(
    conn_tushare, ts_codes: list[str]
) -> dict[str, str]:
    """ts_code → 一级--二级--三级（缺失级省略）。"""
    if not ts_codes or conn_tushare is None:
        return {}
    ordered = sorted({str(c).strip() for c in ts_codes if c})
    out: dict[str, str] = {}
    chunk = 500
    for i in range(0, len(ordered), chunk):
        part = ordered[i : i + chunk]
        if not part:
            continue
        ph = ', '.join(['%s'] * len(part))
        sql = (
            'SELECT z.ts_code, z.l1_name, z.l2_name, z.l3_name '
            'FROM ( '
            '  SELECT m.ts_code, m.l1_name, m.l2_name, m.l3_name, '
            '    ROW_NUMBER() OVER (PARTITION BY m.ts_code ORDER BY m.in_date DESC, '
            '      m.l1_code ASC, m.l2_code ASC) AS rn '
            '  FROM stock_tushare.index_member_all_sw m '
            "  WHERE (m.is_new = 'Y' OR m.is_new IS NULL) "
            f'    AND m.ts_code IN ({ph}) '
            ') z WHERE z.rn = 1'
        )
        with conn_tushare.cursor() as cur:
            cur.execute(sql, tuple(part))
            rows = cur.fetchall() or []
        for r in rows:
            ts = str(r[0] or '').strip()
            if not ts:
                continue
            parts = [
                str(r[j] or '').strip()
                for j in (1, 2, 3)
                if r[j] and str(r[j]).strip()
            ]
            out[ts] = '--'.join(parts) if parts else '--'
    return out


def _sw_load_qfq_bars_batch(
    conn_tushare,
    ts_codes: list[str],
    before_date: date,
) -> dict[str, list[BarRecord]]:
    """批量加载 signal 日之前的前复权 K 线（供收盘价 HLC 弹窗 first/last 判定）。"""
    out: dict[str, list[BarRecord]] = defaultdict(list)
    if not ts_codes or before_date is None:
        return out
    ordered = sorted({str(c).strip() for c in ts_codes if c})
    chunk = 400
    for i in range(0, len(ordered), chunk):
        part = ordered[i : i + chunk]
        ph = ', '.join(['%s'] * len(part))
        sql = f"""
            SELECT ts_code, trade_date, high_qfq, low_qfq, close_qfq
            FROM stock_tushare.stk_factor_pro
            WHERE high_qfq IS NOT NULL
              AND low_qfq IS NOT NULL
              AND close_qfq IS NOT NULL
              AND trade_date < %s
              AND ts_code IN ({ph})
            ORDER BY ts_code, trade_date
        """
        with conn_tushare.cursor() as cur:
            cur.execute(sql, (before_date, *part))
            rows = cur.fetchall() or []
        for code, tradeDate, highQfq, lowQfq, closeQfq in rows:
            hq = toFloat(highQfq)
            lq = toFloat(lowQfq)
            cq = toFloat(closeQfq)
            if hq is None or lq is None or cq is None:
                continue
            out[str(code).strip()].append(
                BarRecord(
                    tradeDate=tradeDate,
                    highQfq=hq,
                    lowQfq=lq,
                    closeQfq=cq,
                )
            )
    return out


class _SlidingMinLow:
    """滑动窗口内 low_qfq 最小值（单调队列，对称 SlidingMaxHigh）。"""

    def __init__(self) -> None:
        self.queue: deque[tuple[int, float, date]] = deque()

    def add(self, idx: int, lowQfq: float, tradeDate: date) -> None:
        while self.queue and self.queue[-1][1] >= lowQfq:
            self.queue.pop()
        self.queue.append((idx, lowQfq, tradeDate))

    def removeExpired(self, cutoffIdx: int) -> None:
        while self.queue and self.queue[0][0] <= cutoffIdx:
            self.queue.popleft()

    def priorMin(self) -> tuple[float, date | None]:
        if not self.queue:
            return 0.0, None
        return self.queue[0][1], self.queue[0][2]


def _sw_hist_has_close_extremum_break(
    hist_bars: list[BarRecord],
    break_mode: str,
    direction: str,
) -> bool:
    """历史 bar 是否已出现至少一次收盘价突破（高）或跌破（低）。"""
    if len(hist_bars) < 2:
        return False
    lookback = _SW_LIMIT_DETAIL_LOOKBACK.get(break_mode)
    if break_mode not in _SW_LIMIT_DETAIL_LOOKBACK:
        return False
    is_high = direction == 'high'
    if lookback is None:
        if is_high:
            running = hist_bars[0].highQfq
            for idx in range(1, len(hist_bars)):
                bar = hist_bars[idx]
                if bar.closeQfq > running + EPS:
                    return True
                if bar.highQfq > running:
                    running = bar.highQfq
        else:
            running = hist_bars[0].lowQfq
            for idx in range(1, len(hist_bars)):
                bar = hist_bars[idx]
                if bar.closeQfq < running - EPS:
                    return True
                if bar.lowQfq < running:
                    running = bar.lowQfq
        return False

    if is_high:
        sliding = SlidingMaxHigh()
        sliding.add(0, hist_bars[0].highQfq, hist_bars[0].tradeDate)
        for idx in range(1, len(hist_bars)):
            bar = hist_bars[idx]
            sliding.removeExpired(idx - lookback - 1)
            priorMax, _ = sliding.priorMax()
            if bar.closeQfq > priorMax + EPS:
                return True
            sliding.add(idx, bar.highQfq, bar.tradeDate)
    else:
        slidingLo = _SlidingMinLow()
        slidingLo.add(0, hist_bars[0].lowQfq, hist_bars[0].tradeDate)
        for idx in range(1, len(hist_bars)):
            bar = hist_bars[idx]
            slidingLo.removeExpired(idx - lookback - 1)
            priorMin, _ = slidingLo.priorMin()
            if bar.closeQfq < priorMin - EPS:
                return True
            slidingLo.add(idx, bar.lowQfq, bar.tradeDate)
    return False


def _sw_classify_realtime_close_break_timing(
    ts_code: str,
    hist_bars: list[BarRecord],
    break_mode: str,
    signal_date: date,
    direction: str = 'high',
) -> str:
    """对照历史 bar：无先例→first，曾有先例且今日再破/再跌→last。"""
    if len(hist_bars) < 2:
        return 'first'
    # his/850 新高：与 first_qfq 扫描严格对齐，避免口径漂移
    if direction == 'high' and break_mode in ('his', '850'):
        try:
            found = findFirstBreaksForModes(
                ts_code,
                hist_bars,
                [break_mode],
                withForward=False,
            )
        except ValueError:
            return '--'
        result = found.get(break_mode)
        if result is None or not result.hasBreak:
            return 'first'
        if result.breakDate is not None and result.breakDate < signal_date:
            return 'last'
        return 'first'
    if _sw_hist_has_close_extremum_break(hist_bars, break_mode, direction):
        return 'last'
    return 'first'


def _sw_fetch_realtime_close_break_timing_map(
    conn_tushare,
    conn_wind,
    ts_codes: list[str],
    break_mode: str,
    signal_date: date | None,
    direction: str = 'high',
) -> dict[str, str]:
    """收盘价六列弹窗：按 first_qfq / 对称新低算法实时判定 first/last。"""
    default = {str(c).strip(): '--' for c in ts_codes if c}
    if (
        not default
        or break_mode not in ('his', '850', '250')
        or direction not in ('high', 'low')
        or conn_tushare is None
    ):
        return default

    today = signal_date or _sw_fetch_rt_k_latest_trade_date(conn_wind)
    if today is None:
        return default

    ordered = sorted(default.keys())
    bars_by_code = _sw_load_qfq_bars_batch(conn_tushare, ordered, before_date=today)
    out: dict[str, str] = {}
    for ts in ordered:
        out[ts] = _sw_classify_realtime_close_break_timing(
            ts,
            bars_by_code.get(ts) or [],
            break_mode,
            today,
            direction,
        )
    return out


def _sw_build_limit_detail_rows_from_ts_codes(
    conn_tushare,
    matched_ts: list[str],
    *,
    column_key: str = '',
    as_of_date: date | None = None,
    conn_wind=None,
) -> list[dict]:
    rows: list[dict] = []
    missing: list[str] = []
    now_ts = time.time()
    for ts in matched_ts:
        meta = _sw_stock_basic_meta_get(ts, now_ts)
        if meta:
            rows.append(meta)
        else:
            missing.append(ts)
    if missing and conn_tushare is not None:
        fetched = _sw_fetch_stock_basic_limit_detail_rows(conn_tushare, missing)
        expiry = now_ts + SW_STOCK_BASIC_META_TTL
        with _swCacheLock:
            for row in fetched:
                ts = str(row.get("ts_code") or "").strip()
                if ts:
                    _sw_stock_basic_meta_cache[ts] = (expiry, dict(row))
        rows.extend(fetched)
    by_ts = {str(r.get("ts_code") or "").strip(): r for r in rows}
    ordered_rows: list[dict] = []
    for ts in matched_ts:
        key = str(ts or "").strip()
        if not key:
            continue
        if key in by_ts:
            ordered_rows.append(by_ts[key])
        else:
            # 缺 stock_basic 时仍占位，避免「主表 N、明细 <N」
            ordered_rows.append(
                {
                    "ts_code": key,
                    "sec_name": "--",
                    "list_date": "--",
                }
            )

    col_key = str(column_key or '').strip()
    need_sw = col_key in _SW_LIMIT_DETAIL_SW_INDUSTRY_COLS
    break_spec = _SW_LIMIT_DETAIL_BREAK_SPEC_BY_COL.get(col_key)
    if need_sw and ordered_rows and conn_tushare is not None:
        sw_paths = _sw_fetch_member_industry_paths(conn_tushare, matched_ts)
        timing_map: dict[str, str] = {}
        if break_spec:
            break_mode, direction = break_spec
            ref_date = as_of_date or _sw_fetch_rt_k_latest_trade_date(conn_wind)
            timing_map = _sw_fetch_realtime_close_break_timing_map(
                conn_tushare, conn_wind, matched_ts, break_mode, ref_date, direction
            )
        for row in ordered_rows:
            ts = str(row.get('ts_code') or '').strip()
            row['sw_industry'] = sw_paths.get(ts, '--')
            if break_spec:
                row['break_timing'] = timing_map.get(ts, '--')
    return ordered_rows


def _sw_fetch_hlc_ts_code_sets(conn_wind, ts_codes: list[str]) -> dict[str, set[str]]:
    """一次性取回 6 个 HL_C 收盘价口径命中集合，避免申万页重复执行 MA/MP 重型 JOIN。"""
    result: dict[str, set[str]] = {kind: set() for _, kind in _SW_INDUSTRY_HLC_SPECS}
    if not conn_wind or not ts_codes:
        return result

    c = _LIMIT_STATS_RT_CLOSE_QFQ
    select_flags = (
        f"CASE WHEN mp.high_250 IS NOT NULL AND mp.low_250 IS NOT NULL AND ({c}) > mp.high_250 THEN 1 ELSE 0 END AS hlc_ch_250, "
        f"CASE WHEN mp.high_250 IS NOT NULL AND mp.low_250 IS NOT NULL AND ({c}) < mp.low_250 THEN 1 ELSE 0 END AS hlc_cl_250, "
        f"CASE WHEN mp.high_850 IS NOT NULL AND mp.low_850 IS NOT NULL AND ({c}) > mp.high_850 THEN 1 ELSE 0 END AS hlc_ch_850, "
        f"CASE WHEN mp.high_850 IS NOT NULL AND mp.low_850 IS NOT NULL AND ({c}) < mp.low_850 THEN 1 ELSE 0 END AS hlc_cl_850, "
        f"CASE WHEN mp.h_high IS NOT NULL AND mp.h_low IS NOT NULL AND ({c}) > mp.h_high THEN 1 ELSE 0 END AS hlc_ch_h, "
        f"CASE WHEN mp.h_high IS NOT NULL AND mp.h_low IS NOT NULL AND ({c}) < mp.h_low THEN 1 ELSE 0 END AS hlc_cl_h"
    )
    ordered_kinds = ["hlc_ch_250", "hlc_cl_250", "hlc_ch_850", "hlc_cl_850", "hlc_ch_h", "hlc_cl_h"]
    chunk = 5000
    for i in range(0, len(ts_codes), chunk):
        part = ts_codes[i : i + chunk]
        if not part:
            continue
        ph = ", ".join(["%s"] * len(part))
        sql = (
            """
WITH d AS (
    SELECT DATE(MAX(trade_time)) AS trade_date
    FROM stock_wind.rt_k
    WHERE trade_time IS NOT NULL
),
ma AS (
    SELECT m.ts_code, m.high_qfq, m.low_qfq, m.close_qfq,
           p.high AS pro_high, p.low AS pro_low, p.close AS pro_close
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
    SELECT m.ts_code, m.high_250, m.high_850, m.h_high, m.low_250, m.low_850, m.h_low
    FROM stock_tushare.stk_factor_ma m
    INNER JOIN (
        SELECT m2.ts_code, MAX(m2.trade_date) AS md
        FROM stock_tushare.stk_factor_ma m2
        INNER JOIN d ON m2.trade_date < d.trade_date
        GROUP BY m2.ts_code
    ) mx ON m.ts_code = mx.ts_code AND m.trade_date = mx.md
)
SELECT s1.windcodes, """
            + select_flags
            + """
FROM stock_wind.stockstat s1
"""
            "INNER JOIN stock_wind.rt_k rt ON s1.windcodes = rt.ts_code "
            "INNER JOIN ma ON ma.ts_code = rt.ts_code "
            "INNER JOIN mp ON mp.ts_code = rt.ts_code "
            + "WHERE NOT (rt.vol = 0 AND rt.amount = 0) AND "
            + _LIMIT_STATS_RT_K_FRESH_TRADE_DATE
            + " "
            + _LIMIT_STATS_SQL_NOT_ST_RT_K
            + f" AND s1.windcodes IN ({ph})"
        )
        with conn_wind.cursor() as cur:
            cur.execute(sql, tuple(part))
            for r in cur.fetchall() or []:
                if r and r[0]:
                    ts = str(r[0]).strip()
                    for offset, kind in enumerate(ordered_kinds, start=1):
                        if int(r[offset] or 0) != 0:
                            result[kind].add(ts)
    return result


def _sw_fetch_hlc_ts_codes_by_kind(
    conn_wind, ts_codes: list[str], kind: str
) -> set[str]:
    """涨跌家数 HL_C 明细同源：收盘前复权价突破/跌破上一交易日 MA 阈值（不含 ST）。"""
    if kind not in _LIMIT_STATS_HLC_DETAIL_WHERE or not conn_wind or not ts_codes:
        return set()
    cond = _LIMIT_STATS_HLC_DETAIL_WHERE[kind]
    out: set[str] = set()
    chunk = 900
    for i in range(0, len(ts_codes), chunk):
        part = ts_codes[i : i + chunk]
        if not part:
            continue
        ph = ", ".join(["%s"] * len(part))
        sql = (
            """
WITH d AS (
    SELECT DATE(MAX(trade_time)) AS trade_date
    FROM stock_wind.rt_k
    WHERE trade_time IS NOT NULL
),
ma AS (
    SELECT m.ts_code, m.high_qfq, m.low_qfq, m.close_qfq,
           p.high AS pro_high, p.low AS pro_low, p.close AS pro_close
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
    SELECT m.ts_code, m.high_250, m.high_850, m.h_high, m.low_250, m.low_850, m.h_low
    FROM stock_tushare.stk_factor_ma m
    INNER JOIN (
        SELECT m2.ts_code, MAX(m2.trade_date) AS md
        FROM stock_tushare.stk_factor_ma m2
        INNER JOIN d ON m2.trade_date < d.trade_date
        GROUP BY m2.ts_code
    ) mx ON m.ts_code = mx.ts_code AND m.trade_date = mx.md
)
SELECT s1.windcodes
FROM stock_wind.stockstat s1
INNER JOIN stock_wind.rt_k rt ON s1.windcodes = rt.ts_code
INNER JOIN ma ON ma.ts_code = rt.ts_code
INNER JOIN mp ON mp.ts_code = rt.ts_code
WHERE """
            + cond
            + " AND "
            + _LIMIT_STATS_RT_K_FRESH_TRADE_DATE
            + " "
            + _LIMIT_STATS_SQL_NOT_ST_RT_K
            + f" AND s1.windcodes IN ({ph})"
        )
        with conn_wind.cursor() as cur:
            cur.execute(sql, tuple(part))
            for r in cur.fetchall() or []:
                if r and r[0]:
                    out.add(str(r[0]).strip())
    return out


def _sw_fetch_archive_ts_codes_by_kind(
    conn_tushare,
    ts_codes: list[str],
    trade_date: date,
    kind: str,
) -> set[str]:
    """limit_stats_archive_detail：指定交易日+kind 的个股集合（再与申万成分求交）。"""
    grouped = _sw_fetch_archive_ts_codes_grouped_by_kinds(
        conn_tushare, ts_codes, trade_date, [kind]
    )
    return grouped.get(kind, set())


def _sw_fetch_archive_ts_codes_grouped_by_kinds(
    conn_tushare,
    ts_codes: list[str],
    trade_date: date,
    kinds: list[str],
) -> dict[str, set[str]]:
    """一次查询拉取多个 kind 的归档个股集合（申万页历史模式计数/明细同源）。"""
    ordered_kinds = [str(k).strip() for k in kinds if str(k or "").strip()]
    result: dict[str, set[str]] = {k: set() for k in ordered_kinds}
    if conn_tushare is None or not ts_codes or not ordered_kinds or trade_date is None:
        return result
    chunk = 900
    tbl = TABLE_LIMIT_STATS_ARCHIVE_DETAIL
    kind_ph = ", ".join(["%s"] * len(ordered_kinds))
    for i in range(0, len(ts_codes), chunk):
        part = ts_codes[i : i + chunk]
        if not part:
            continue
        ph = ", ".join(["%s"] * len(part))
        sql = (
            f"SELECT d.kind, d.ts_code FROM stock_tushare.{tbl} d "
            f"WHERE d.trade_date = %s AND d.kind IN ({kind_ph}) AND d.ts_code IN ({ph})"
        )
        with conn_tushare.cursor() as cur:
            cur.execute(sql, (trade_date, *ordered_kinds, *part))
            for r in cur.fetchall() or []:
                if not r or not r[0] or not r[1]:
                    continue
                kind = str(r[0]).strip()
                if kind in result:
                    result[kind].add(str(r[1]).strip())
    return result


def _sw_fetch_limit_count_maps(
    ts_to_ind: dict[str, tuple[str, str]],
    selected_trade_date: date | None,
) -> tuple[
    dict[str, dict[str, int]],
    dict[str, dict[str, int]],
    dict[str, set[str]],
]:
    """申万页新高/低 12 列：盘中 stockstat+HL_C，或历史日 limit_stats_archive_detail。"""
    sw_t0_count_maps: dict[str, dict[str, int]] = {}
    sw_hlc_count_maps: dict[str, dict[str, int]] = {}
    flagged_by_col: dict[str, set[str]] = {}
    if not ts_to_ind:
        return sw_t0_count_maps, sw_hlc_count_maps, flagged_by_col
    ts_codes = sorted(ts_to_ind.keys())
    ts_set = set(ts_codes)
    if selected_trade_date is not None:
        stat_kinds = [
            str(_SW_LIMIT_DETAIL_COLUMN_META[jk].get("archive_kind") or "").strip()
            for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS
        ]
        hlc_kinds = [
            str(_SW_LIMIT_DETAIL_COLUMN_META[jk].get("archive_kind") or "").strip()
            for jk, _ in _SW_INDUSTRY_HLC_SPECS
        ]
        conn_t = None
        try:
            conn_t = pymysql.connect(**DB_CONFIG_TUSHARE)
            stat_grouped = _sw_fetch_archive_ts_codes_grouped_by_kinds(
                conn_t, ts_codes, selected_trade_date, stat_kinds
            )
            hlc_grouped = _sw_fetch_archive_ts_codes_grouped_by_kinds(
                conn_t, ts_codes, selected_trade_date, hlc_kinds
            )
            for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS:
                arch_kind = str(_SW_LIMIT_DETAIL_COLUMN_META[jk].get("archive_kind") or "").strip()
                flagged = set(stat_grouped.get(arch_kind, set()))
                flagged_by_col[jk] = flagged
                sw_t0_count_maps[jk] = _sw_aggregate_h250_by_group(flagged, ts_to_ind)
            for jk, _ in _SW_INDUSTRY_HLC_SPECS:
                arch_kind = str(_SW_LIMIT_DETAIL_COLUMN_META[jk].get("archive_kind") or "").strip()
                flagged = set(hlc_grouped.get(arch_kind, set()))
                flagged_by_col[jk] = flagged
                sw_hlc_count_maps[jk] = _sw_aggregate_h250_by_group(flagged, ts_to_ind)
        finally:
            if conn_t is not None:
                try:
                    conn_t.close()
                except Exception:
                    pass
        return sw_t0_count_maps, sw_hlc_count_maps, flagged_by_col
    conn_w = None
    try:
        conn_w = pymysql.connect(**DB_CONFIG_WIND_BULK)
        stat_sets = _sw_fetch_stockstat_t0_flag_sets_bulk(conn_w, ts_codes)
        for jk, flag_key in _SW_INDUSTRY_LIMITSTAT_T0_SPECS:
            flagged = set(stat_sets.get(flag_key, set()))
            flagged_by_col[jk] = flagged
            sw_t0_count_maps[jk] = _sw_aggregate_h250_by_group(flagged, ts_to_ind)
        hlc_sets = _sw_fetch_hlc_ts_code_sets(conn_w, ts_codes)
        for jk, hlc_kind in _SW_INDUSTRY_HLC_SPECS:
            flagged = set(hlc_sets.get(hlc_kind, set()))
            flagged_by_col[jk] = flagged
            sw_hlc_count_maps[jk] = _sw_aggregate_h250_by_group(flagged, ts_to_ind)
    finally:
        if conn_w is not None:
            try:
                conn_w.close()
            except Exception:
                pass
    return sw_t0_count_maps, sw_hlc_count_maps, flagged_by_col


def _sw_fetch_live_flagged_by_col(conn_wind, ts_codes: list[str]) -> dict[str, set[str]]:
    """盘中 12 列新高/低命中集合（与主表统计同源 bulk 查询）。"""
    flagged_by_col: dict[str, set[str]] = {}
    if not conn_wind or not ts_codes:
        return flagged_by_col
    stat_sets = _sw_fetch_stockstat_t0_flag_sets_bulk(conn_wind, ts_codes)
    for jk, flag_key in _SW_INDUSTRY_LIMITSTAT_T0_SPECS:
        flagged_by_col[jk] = set(stat_sets.get(flag_key, set()))
    hlc_sets = _sw_fetch_hlc_ts_code_sets(conn_wind, ts_codes)
    for jk, hlc_kind in _SW_INDUSTRY_HLC_SPECS:
        flagged_by_col[jk] = set(hlc_sets.get(hlc_kind, set()))
    return flagged_by_col


def _sw_limit_count_maps_cache_key(
    ts_codes: list[str],
    selected_trade_date: date | None,
) -> tuple:
    """仅按成分池+交易日缓存；勿含 level——缓存内容应为个股命中集，再按调用方 ts_to_ind 聚合。"""
    td = selected_trade_date.isoformat() if selected_trade_date else 'live'
    # v2：旧缓存误存「按 level 聚合后的 maps」，L1/L2/L3 同成分池会互相污染成全 0
    return ('v2', td, len(ts_codes), hash(tuple(ts_codes)))


def _sw_aggregate_limit_maps_from_flagged(
    flagged_by_col: dict[str, set[str]],
    ts_to_ind: dict[str, tuple[str, str]],
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """用个股命中集按当前 level 的 group_code 重聚（L1/L2/L3 可共享同一 flagged 缓存）。"""
    sw_t0_count_maps: dict[str, dict[str, int]] = {}
    sw_hlc_count_maps: dict[str, dict[str, int]] = {}
    for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS:
        sw_t0_count_maps[jk] = _sw_aggregate_h250_by_group(
            flagged_by_col.get(jk) or set(), ts_to_ind
        )
    for jk, _ in _SW_INDUSTRY_HLC_SPECS:
        sw_hlc_count_maps[jk] = _sw_aggregate_h250_by_group(
            flagged_by_col.get(jk) or set(), ts_to_ind
        )
    return sw_t0_count_maps, sw_hlc_count_maps


def _sw_peek_limit_count_flagged(
    ts_to_ind: dict[str, tuple[str, str]],
    selected_trade_date: date | None,
) -> dict[str, set[str]] | None:
    """只读 limit_count 命中缓存；未命中返回 None（不触发重扫）。"""
    ts_codes = sorted(ts_to_ind.keys())
    if not ts_codes:
        return None
    cache_key = _sw_limit_count_maps_cache_key(ts_codes, selected_trade_date)
    now_ts = time.time()
    with _swCacheLock:
        hit = _swLimitCountMapsCache.get(cache_key)
        if hit is not None and now_ts < hit[0]:
            _swLimitCountMapsCache.move_to_end(cache_key)
            flagged = hit[1]
            return {k: set(v) for k, v in flagged.items()} if flagged else {}
    return None


def _sw_apply_limit_count_maps_to_payload(
    payload: dict,
    ts_to_ind: dict[str, tuple[str, str]],
    flagged_by_col: dict[str, set[str]],
    *,
    pool_key: tuple[str, str, str, str] | None = None,
) -> None:
    """将同源命中集写入主表各行/汇总，并续期明细池（保证点开名单与数字一致）。"""
    if not payload or not ts_to_ind or not flagged_by_col:
        return
    sw_t0_count_maps, sw_hlc_count_maps = _sw_aggregate_limit_maps_from_flagged(
        flagged_by_col, ts_to_ind
    )
    rows = payload.get('rows') or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        group_code = str(row.get('group_code') or row.get('l1_code') or '').strip()
        for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS:
            row[jk] = int(sw_t0_count_maps.get(jk, {}).get(group_code, 0))
        for jk, _ in _SW_INDUSTRY_HLC_SPECS:
            row[jk] = int(sw_hlc_count_maps.get(jk, {}).get(group_code, 0))
    summary = payload.get('summary_row')
    if isinstance(summary, dict):
        for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS:
            summary[jk] = sum(
                int(r.get(jk) or 0) for r in rows if isinstance(r, dict)
            )
        for jk, _ in _SW_INDUSTRY_HLC_SPECS:
            summary[jk] = sum(
                int(r.get(jk) or 0) for r in rows if isinstance(r, dict)
            )
    if pool_key:
        _sw_limit_flagged_pool_put(pool_key, flagged_by_col, ts_to_ind)


def _sw_fetch_limit_count_maps_cached(
    ts_to_ind: dict[str, tuple[str, str]],
    selected_trade_date: date | None,
) -> tuple[
    dict[str, dict[str, int]],
    dict[str, dict[str, int]],
    dict[str, set[str]],
]:
    ts_codes = sorted(ts_to_ind.keys())
    if not ts_codes:
        return {}, {}, {}
    cache_key = _sw_limit_count_maps_cache_key(ts_codes, selected_trade_date)
    now_ts = time.time()
    flagged_by_col: dict[str, set[str]] | None = None
    with _swCacheLock:
        hit = _swLimitCountMapsCache.get(cache_key)
        if hit is not None and now_ts < hit[0]:
            _swLimitCountMapsCache.move_to_end(cache_key)
            flagged_by_col = hit[1]
    if flagged_by_col is None:
        gateName = f'hlc|{hash(cache_key)}'
        isLeader, gateEv = _swWaitOrLeadCompute(gateName)
        if not isLeader:
            with _swCacheLock:
                hit = _swLimitCountMapsCache.get(cache_key)
                if hit is not None and time.time() < hit[0]:
                    flagged_by_col = hit[1]
        if flagged_by_col is None:
            try:
                _t0, _hlc, flagged_by_col = _swCallWithMysqlTempTableRetry(
                    'sw_limit_count_maps',
                    lambda: _sw_fetch_limit_count_maps(ts_to_ind, selected_trade_date),
                )
                with _swCacheLock:
                    while len(_swLimitCountMapsCache) >= SW_LIMIT_COUNT_MAPS_CACHE_MAX:
                        _swLimitCountMapsCache.popitem(last=False)
                    # 只缓存个股命中集；聚合结果随 level 的 ts_to_ind 变化，不能跨 L1/L2/L3 复用
                    _swLimitCountMapsCache[cache_key] = (
                        time.time() + SW_LIMIT_COUNT_MAPS_CACHE_TTL,
                        flagged_by_col,
                    )
                    _swLimitCountMapsCache.move_to_end(cache_key)
            finally:
                if isLeader:
                    _swFinishCompute(gateName, gateEv)
        elif isLeader:
            _swFinishCompute(gateName, gateEv)
    sw_t0_count_maps, sw_hlc_count_maps = _sw_aggregate_limit_maps_from_flagged(
        flagged_by_col, ts_to_ind
    )
    return sw_t0_count_maps, sw_hlc_count_maps, flagged_by_col


def _parse_sw_industry_trade_date_param(raw: str | None) -> date | None:
    """解析申万行业 API 的 trade_date=YYYY-MM-DD（或 YYYYMMDD）。"""
    s = str(raw or "").strip()
    if not s:
        return None
    if len(s) >= 10 and s[4] == "-":
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    digits = s.replace("-", "")[:8]
    if len(digits) == 8 and digits.isdigit():
        try:
            return datetime.strptime(digits, "%Y%m%d").date()
        except ValueError:
            return None
    return None


def _sw_fina_batch_cache_key(
    ts_codes: list[str],
    trade_date_key: str,
    periods: list[str],
) -> tuple:
    return (trade_date_key, hash(tuple(ts_codes)), tuple(periods))


def _sw_load_member_all_sw_rows(conn) -> list:
    """申万成分 latest 映射（进程内短缓存，避免每次冷启动重复扫 index_member_all_sw）。"""
    global _swMemberRowsCache
    now_ts = time.time()
    with _swCacheLock:
        if _swMemberRowsCache is not None and now_ts < _swMemberRowsCache[0]:
            return list(_swMemberRowsCache[1])
    sql_sw = (
        "SELECT z.ts_code, z.l1_code, z.l1_name, z.l2_code, z.l2_name, z.l3_code, z.l3_name "
        "FROM ( "
        "  SELECT m.ts_code, m.l1_code, m.l1_name, m.l2_code, m.l2_name, m.l3_code, m.l3_name, "
        "    ROW_NUMBER() OVER (PARTITION BY m.ts_code ORDER BY m.in_date DESC, m.l1_code ASC, m.l2_code ASC, m.l2_name ASC) AS rn "
        "  FROM stock_tushare.index_member_all_sw m "
        "  WHERE (m.is_new = 'Y' OR m.is_new IS NULL) "
        "    AND m.l1_code IS NOT NULL AND m.l1_code <> '' "
        "    AND m.l1_name IS NOT NULL AND m.l1_name <> '' "
        "    AND m.l2_code IS NOT NULL AND m.l2_code <> '' "
        "    AND m.l2_name IS NOT NULL AND m.l2_name <> '' "
        ") z "
        "WHERE z.rn = 1"
    )
    with conn.cursor() as cur:
        cur.execute(sql_sw)
        sw_rows = cur.fetchall() or []
    with _swCacheLock:
        _swMemberRowsCache = (now_ts + SW_MEMBER_ROWS_CACHE_TTL, list(sw_rows))
    return sw_rows


def _sw_fetch_fina_batch_bundle_impl(
    conn,
    ts_codes: list[str],
    periods: list[str],
) -> dict:
    """批量读取申万页财报底层（按个股），供 L1/L2 等多档位复用聚合。"""
    has_inc_rt = _tushare_column_exists(conn, 'income', 'report_type')
    date_keys = _fina_income_date_keys_with_yoy_for_periods(periods)
    rev_nested = _fina_batch_income_total_revenue(conn, ts_codes, date_keys, has_inc_rt)

    has_q_dtprofit = _tushare_column_exists(conn, 'fina_indicator', 'q_dtprofit')
    has_fi_rt = _tushare_column_exists(conn, 'fina_indicator', 'report_type')
    dtp_nested: dict[str, dict[str, float]] = {}
    if has_q_dtprofit:
        dtp_period_keys = _fina_periods_union_yoy_for_q_dtprofit(periods)
        dtp_nested = _fina_batch_fina_indicator_q_dtprofit(
            conn, ts_codes, dtp_period_keys, has_fi_rt
        )

    sfp_map: dict[str, tuple[float | None, float | None]] = {}
    close_map: dict[str, float] = {}
    has_tmv = _tushare_column_exists(conn, 'stk_factor_pro', 'total_mv')
    has_fs = _tushare_column_exists(conn, 'stk_factor_pro', 'free_share')
    if has_tmv or has_fs:
        max_d = None
        with conn.cursor() as cur:
            cur.execute('SELECT MAX(trade_date) FROM stock_tushare.stk_factor_pro')
            mx = cur.fetchone()
            if mx and mx[0] is not None:
                max_d = mx[0]
        if max_d is not None:
            sel_cols = ['ts_code']
            if has_tmv:
                sel_cols.append('total_mv')
            if has_fs:
                sel_cols.append('free_share')
            col_sql = ', '.join(sel_cols)
            chunk = 480
            for i in range(0, len(ts_codes), chunk):
                part = ts_codes[i : i + chunk]
                if not part:
                    continue
                ph = ', '.join(['%s'] * len(part))
                sql = (
                    f'SELECT {col_sql} FROM stock_tushare.stk_factor_pro '
                    f'WHERE trade_date = %s AND ts_code IN ({ph})'
                )
                with conn.cursor() as cur:
                    cur.execute(sql, (max_d, *part))
                    for raw in cur.fetchall() or []:
                        if not raw or raw[0] is None:
                            continue
                        tc = str(raw[0]).strip()
                        if not tc:
                            continue
                        idx = 1
                        tmv_v = None
                        fs_v = None
                        if has_tmv:
                            tmv_v = raw[idx] if len(raw) > idx else None
                            idx += 1
                        if has_fs:
                            fs_v = raw[idx] if len(raw) > idx else None
                        tmv_num = None
                        fs_num = None
                        try:
                            if tmv_v is not None:
                                tmv_num = float(tmv_v) / 10000.0
                        except (TypeError, ValueError):
                            tmv_num = None
                        try:
                            if fs_v is not None:
                                fs_num = float(fs_v)
                        except (TypeError, ValueError):
                            fs_num = None
                        sfp_map[tc] = (tmv_num, fs_num)

        if has_fs:
            try:
                conn_w = pymysql.connect(**DB_CONFIG)
                try:
                    chunk = 480
                    for i in range(0, len(ts_codes), chunk):
                        part = ts_codes[i : i + chunk]
                        if not part:
                            continue
                        ph = ', '.join(['%s'] * len(part))
                        sql = (
                            'SELECT ts_code, close FROM ('
                            '  SELECT ts_code, close, '
                            '    ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_time DESC) AS rn '
                            '  FROM rt_k WHERE ts_code IN (' + ph + ')'
                            ') x WHERE rn = 1'
                        )
                        with conn_w.cursor() as cur:
                            cur.execute(sql, tuple(part))
                            for r2 in cur.fetchall() or []:
                                if not r2 or r2[0] is None or r2[1] is None:
                                    continue
                                tc2 = str(r2[0]).strip()
                                if not tc2:
                                    continue
                                try:
                                    close_map[tc2] = float(r2[1])
                                except (TypeError, ValueError):
                                    continue
                finally:
                    conn_w.close()
            except Exception as e:
                logger.warning('sw_industry_stats 查询 rt_k close 失败，自由市值将不完整: %s', e)

    cf_nested_sw: dict[str, dict[str, float]] = {}
    margin_nested_sw: dict[str, dict[str, tuple[float | None, float | None]]] = {}
    bs_cf_nested_sw: dict[str, dict[str, tuple[float | None, float | None]]] = {}
    has_sw_extend_metrics = False
    if periods and ts_codes:
        has_n_cf_sw = _tushare_column_exists(conn, 'cashflow', 'n_cashflow_act')
        has_cf_rt_sw = _tushare_column_exists(conn, 'cashflow', 'report_type')
        has_q_gs_sw = _tushare_column_exists(conn, 'fina_indicator', 'q_gsprofit_margin')
        has_q_dt_roe_sw = _tushare_column_exists(conn, 'fina_indicator', 'q_dt_roe')
        has_bs_rt_sw = _tushare_column_exists(conn, 'balancesheet', 'report_type')
        has_adv_sw = _tushare_column_exists(conn, 'balancesheet', 'adv_receipts')
        has_cl_sw = _tushare_column_exists(conn, 'balancesheet', 'contract_liab')
        has_fa_sw = _tushare_column_exists(conn, 'balancesheet', 'fix_assets_total')
        cf_keys_sq = _fina_income_date_keys_with_yoy_for_periods(periods)
        if has_n_cf_sw:
            try:
                cf_nested_sw = _fina_batch_cashflow_n_cashflow_act(
                    conn, ts_codes, cf_keys_sq, has_cf_rt_sw
                )
                has_sw_extend_metrics = True
            except Exception as e:
                logger.warning('sw_industry_stats 经营现金流批量读取失败: %s', e)
        if has_q_gs_sw or has_q_dt_roe_sw:
            try:
                margin_nested_sw = _fina_batch_fina_indicator_q_gs_margin_q_dt_roe(
                    conn,
                    ts_codes,
                    periods,
                    has_fi_rt,
                    has_q_gs_sw,
                    has_q_dt_roe_sw,
                )
                has_sw_extend_metrics = True
            except Exception as e:
                logger.warning('sw_industry_stats 毛利率/ROE(扣非)批量读取失败: %s', e)
        if has_cl_sw or has_fa_sw:
            try:
                bs_cf_nested_sw = _fina_batch_balancesheet_contract_fix_yi(
                    conn,
                    ts_codes,
                    periods,
                    has_bs_rt=has_bs_rt_sw,
                    has_adv=has_adv_sw,
                    has_contract=has_cl_sw,
                    has_fix=has_fa_sw,
                )
                has_sw_extend_metrics = True
            except Exception as e:
                logger.warning('sw_industry_stats 合同负债/固定资产批量读取失败: %s', e)

    return {
        'rev_nested': rev_nested,
        'dtp_nested': dtp_nested,
        'has_q_dtprofit': has_q_dtprofit,
        'cf_nested_sw': cf_nested_sw,
        'margin_nested_sw': margin_nested_sw,
        'bs_cf_nested_sw': bs_cf_nested_sw,
        'has_sw_extend_metrics': has_sw_extend_metrics,
        'sfp_map': sfp_map,
        'close_map': close_map,
    }


def _sw_load_fina_batch_bundle(
    ts_codes: list[str],
    periods: list[str],
    trade_date_key: str,
) -> dict:
    """带进程内缓存的财报批量加载（同成分股池 L1/L2 共享）。"""
    if not ts_codes or not periods:
        return {
            'rev_nested': {},
            'dtp_nested': {},
            'has_q_dtprofit': False,
            'cf_nested_sw': {},
            'margin_nested_sw': {},
            'bs_cf_nested_sw': {},
            'has_sw_extend_metrics': False,
            'sfp_map': {},
            'close_map': {},
        }
    cache_key = _sw_fina_batch_cache_key(ts_codes, trade_date_key, periods)
    with _swCacheLock:
        hit = _swFinaBatchCache.get(cache_key)
        if hit is not None:
            _swFinaBatchCache.move_to_end(cache_key)
            return hit
    gateName = f'fina|{hash(cache_key)}'
    isLeader, gateEv = _swWaitOrLeadCompute(gateName)
    if not isLeader:
        with _swCacheLock:
            hit = _swFinaBatchCache.get(cache_key)
            if hit is not None:
                _swFinaBatchCache.move_to_end(cache_key)
                return hit
    conn = None
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
        bundle = _sw_fetch_fina_batch_bundle_impl(conn, ts_codes, periods)
        with _swCacheLock:
            while len(_swFinaBatchCache) >= SW_FINA_BATCH_CACHE_MAX:
                _swFinaBatchCache.popitem(last=False)
            _swFinaBatchCache[cache_key] = bundle
            _swFinaBatchCache.move_to_end(cache_key)
        return bundle
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if isLeader:
            _swFinishCompute(gateName, gateEv)


def _sw_margin_empty_fields() -> dict[str, float | None]:
    return {
        'sw_margin_rzye': None,
        'sw_margin_1d': None,
        'sw_margin_3d': None,
        'sw_margin_5d': None,
        'sw_margin_20d': None,
        'sw_margin_60d': None,
    }


def _sw_margin_yuan_to_yi(val) -> float | None:
    try:
        if val is None:
            return None
        return round(float(val) / 100000000.0, 2)
    except (TypeError, ValueError):
        return None


def _sw_resolve_margin_as_of(selected_trade_date: date | None) -> tuple[date | None, str | None]:
    """融资基准日：实时取 margin_detail 最新日；历史回放取行情日前一交易日。"""
    if selected_trade_date is not None:
        prev_dates = get_prev_n_trade_dates(selected_trade_date, 1)
        if not prev_dates:
            return None, 'prev_of_selected'
        return _parse_sw_industry_trade_date_param(prev_dates[0]), 'prev_of_selected'

    conn_w = None
    try:
        conn_w = pymysql.connect(**DB_CONFIG)
        with conn_w.cursor() as cur:
            cur.execute("SELECT MAX(trade_date) FROM stock_wind.margin_detail")
            row = cur.fetchone()
        if row and row[0] is not None:
            return _parse_sw_industry_trade_date_param(row[0]), 'latest'
    except Exception as e:
        logger.warning('sw_industry_stats 查询 margin_detail 最新日失败: %s', e)
    finally:
        if conn_w is not None:
            try:
                conn_w.close()
            except Exception:
                pass
    return None, None


def _sw_fetch_margin_metrics_by_group_lists(
    ts_by_group: dict[str, list[str]],
    selected_trade_date: date | None,
) -> tuple[dict[str, dict[str, float | None]], dict[str, float | None], date | None, str | None]:
    """按分组聚合 margin_detail；同一股票可计入多组，汇总按去重成分。"""
    empty_summary = _sw_margin_empty_fields()
    ts_to_groups: dict[str, list[str]] = defaultdict(list)
    for group_code, codes in ts_by_group.items():
        g = str(group_code or '').strip()
        if not g:
            continue
        for tc in codes:
            tcu = str(tc or '').strip().upper()
            if tcu:
                ts_to_groups[tcu].append(g)
    if not ts_to_groups:
        return {}, empty_summary, None, None

    margin_as_of, margin_mode = _sw_resolve_margin_as_of(selected_trade_date)
    if margin_as_of is None:
        logger.warning('sw_industry_stats 融资余额(sw_margin_*) 无可用 margin_as_of，置 None')
        return {}, empty_summary, None, margin_mode

    zeroKeys = (
        'sw_margin_rzye', 'sw_margin_1d', 'sw_margin_3d',
        'sw_margin_5d', 'sw_margin_20d', 'sw_margin_60d',
    )
    sums_by_group: dict[str, dict[str, float]] = defaultdict(
        lambda: {k: 0.0 for k in zeroKeys}
    )
    summary_sums = {k: 0.0 for k in zeroKeys}
    seen_any = {k: False for k in zeroKeys}
    ts_codes = sorted(ts_to_groups.keys())
    conn_w = None
    try:
        conn_w = pymysql.connect(**DB_CONFIG)
        chunk = 800
        for i in range(0, len(ts_codes), chunk):
            part = ts_codes[i:i + chunk]
            if not part:
                continue
            ph = ', '.join(['%s'] * len(part))
            sql = (
                "SELECT ts_code, rzye, rzjmre, rzjmre_t3, rzjmre_t5, rzjmre_t20, rzjmre_t60 "
                f"FROM stock_wind.margin_detail WHERE trade_date = %s AND ts_code IN ({ph})"
            )
            with conn_w.cursor() as cur:
                cur.execute(sql, (margin_as_of,) + tuple(part))
                rows = cur.fetchall() or []
            for r in rows:
                tc = str(r[0] or '').strip().upper()
                groups = ts_to_groups.get(tc)
                if not groups:
                    continue
                values = {
                    'sw_margin_rzye': r[1],
                    'sw_margin_1d': r[2],
                    'sw_margin_3d': r[3],
                    'sw_margin_5d': r[4],
                    'sw_margin_20d': r[5],
                    'sw_margin_60d': r[6],
                }
                for key, raw_val in values.items():
                    if raw_val is None:
                        continue
                    try:
                        fv = float(raw_val)
                    except (TypeError, ValueError):
                        continue
                    for group_code in groups:
                        sums_by_group[group_code][key] += fv
                    summary_sums[key] += fv
                    seen_any[key] = True
    except Exception as e:
        logger.warning('sw_industry_stats 聚合 stock_wind.margin_detail 失败: %s', e)
        return {}, empty_summary, margin_as_of, margin_mode
    finally:
        if conn_w is not None:
            try:
                conn_w.close()
            except Exception:
                pass

    out_by_group = {
        group_code: {key: _sw_margin_yuan_to_yi(val) for key, val in group_sums.items()}
        for group_code, group_sums in sums_by_group.items()
    }
    summary_fields = {
        key: _sw_margin_yuan_to_yi(val) if seen_any[key] else None
        for key, val in summary_sums.items()
    }
    return out_by_group, summary_fields, margin_as_of, margin_mode


def _sw_fetch_margin_metrics_by_group(
    ts_to_ind: dict[str, tuple[str, str]],
    selected_trade_date: date | None,
) -> tuple[dict[str, dict[str, float | None]], dict[str, float | None], date | None, str | None]:
    """按 ts→单组映射聚合融资余额（申万 L1/L2 无重叠）。"""
    ts_by_group: dict[str, list[str]] = defaultdict(list)
    for ts, info in ts_to_ind.items():
        ts_by_group[str(info[0] or '').strip()].append(ts)
    return _sw_fetch_margin_metrics_by_group_lists(dict(ts_by_group), selected_trade_date)


def _sw_build_idx_sw_quote_list(group_codes: list[str]) -> list[str]:
    """申万行业指数 ts_code 列表（含全 A 基准 801003.SI），供 rt_sw_k / index_sw_daily 预取。"""
    idx_sw_list: list[str] = []
    seen_sw_ts: set[str] = set()
    for gc in group_codes:
        tsu = _normalize_sw_industry_index_ts_code(str(gc))
        if tsu and tsu not in seen_sw_ts:
            seen_sw_ts.add(tsu)
            idx_sw_list.append(tsu)
    bench_ts = str(SW_STATS_SUMMARY_ALL_A_SW_TS or "").strip().upper()
    if bench_ts and bench_ts not in seen_sw_ts:
        seen_sw_ts.add(bench_ts)
        idx_sw_list.append(bench_ts)
    idx_sw_list.sort()
    return idx_sw_list


def _sw_apply_live_row_market_fields(
    row: dict,
    *,
    rt_sw_quotes: dict[str, dict],
    range_cache_sw: dict[str, dict],
    amt_pctile_cache_sw: dict[str, dict],
    sw_t0_count_maps: dict[str, dict[str, int]],
    sw_hlc_count_maps: dict[str, dict[str, int]],
) -> None:
    """将实时层字段写入单行（与 api_sw_industry_stats 冷路径一致）。"""
    group_code = str(row.get('group_code') or row.get('l1_code') or '').strip()
    idx_ts = _normalize_sw_industry_index_ts_code(group_code)
    rq = rt_sw_quotes.get(idx_ts) if idx_ts else None
    sw_pct_val = None
    if (
        rq
        and rq.get('etf_last') is not None
        and rq.get('pct_change') is not None
    ):
        try:
            sw_pct_val = round(float(rq['pct_change']), 2)
            if abs(sw_pct_val) >= SW_RT_QUOTE_MAX_ABS_PCT:
                sw_pct_val = None
        except (TypeError, ValueError):
            sw_pct_val = None
    rd_sw = range_cache_sw.get(idx_ts) if idx_ts else None
    row['sw_pct'] = sw_pct_val
    row['sw_pct_t1'] = (rd_sw or {}).get('range_pct_t1')
    row['sw_pct_t5'] = (rd_sw or {}).get('range_pct_t5')
    row['sw_pct_t20'] = (rd_sw or {}).get('range_pct_t20')
    row['sw_pct_t60'] = (rd_sw or {}).get('range_pct_t60')
    row['sw_pct_t120'] = (rd_sw or {}).get('range_pct_t120')
    # 轻量刷新可能不带分位/新高低；空 map 勿把已有列刷成 None/0
    if amt_pctile_cache_sw:
        rd_amt = amt_pctile_cache_sw.get(idx_ts) if idx_ts else None
        row['sw_amt_pctile_t0'] = (rd_amt or {}).get('sw_amt_pctile_t0')
        row['sw_amt_pctile_t1'] = (rd_amt or {}).get('sw_amt_pctile_t1')
        row['sw_amt_pctile_t5'] = (rd_amt or {}).get('sw_amt_pctile_t5')
        row['sw_amt_pctile_t20'] = (rd_amt or {}).get('sw_amt_pctile_t20')
        row['sw_amt_pctile_t60'] = (rd_amt or {}).get('sw_amt_pctile_t60')
        row['sw_amt_pctile_t120'] = (rd_amt or {}).get('sw_amt_pctile_t120')
    if sw_t0_count_maps:
        for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS:
            row[jk] = int(sw_t0_count_maps.get(jk, {}).get(group_code, 0))
    if sw_hlc_count_maps:
        for jk, _ in _SW_INDUSTRY_HLC_SPECS:
            row[jk] = int(sw_hlc_count_maps.get(jk, {}).get(group_code, 0))


def _sw_live_market_maps_cache_key(
    idx_sw_list: list[str],
    selected_trade_date: date | None,
    pool_key: tuple[str, str, str, str] | None,
) -> tuple:
    td = selected_trade_date.isoformat() if selected_trade_date else ''
    pk = pool_key or ('', '', '', '')
    # v2：与 limit_count flagged 缓存配套，作废曾写入错误聚合（全 0）的 live maps
    return ('v2', td, pk, tuple(sorted(idx_sw_list)))


def _sw_live_maps_missing_heavy(maps: tuple | None) -> bool:
    """轻量首屏会写入空成交额分位；此类缓存不得短路 waitLimitScan=True 的重算。"""
    if not maps or len(maps) < 3:
        return True
    amt = maps[2] or {}
    return not bool(amt)


def _sw_fetch_live_market_maps(
    ts_to_ind: dict[str, tuple[str, str]],
    selected_trade_date: date | None = None,
    pool_key: tuple[str, str, str, str] | None = None,
    waitLimitScan: bool = True,
) -> tuple[
    dict[str, dict],
    dict[str, dict],
    dict[str, dict],
    dict[str, dict[str, int]],
    dict[str, dict[str, int]],
]:
    """实时层批量预取：rt_sw_k 区间涨跌幅、成交额比%、stockstat 新高低、HL_C 收盘价新高低。

    waitLimitScan=False：新高低仅读缓存，不阻塞 HTTP 首屏（14s~120s 扫描改后台）；
    日线面板只拉约 220 天算 T-1..T-120，成交额 7 年分位改后台。
    """
    empty: tuple = ({}, {}, {}, {}, {})
    if not ts_to_ind:
        return empty
    group_codes = sorted({info[0] for info in ts_to_ind.values()})
    idx_sw_list = _sw_build_idx_sw_quote_list(group_codes)
    cache_key = _sw_live_market_maps_cache_key(idx_sw_list, selected_trade_date, pool_key)
    now_ts = time.time()
    with _swCacheLock:
        hit = _swLiveMarketMapsCache.get(cache_key)
        if hit is not None and now_ts < hit[0]:
            cached = hit[1]
            # 重路径要成交额分位；空 amt 的轻量包必须穿透重算
            if waitLimitScan and _sw_live_maps_missing_heavy(cached):
                pass
            else:
                _swLiveMarketMapsCache.move_to_end(cache_key)
                return cached
    ts_codes = sorted(ts_to_ind.keys())
    rt_sw_quotes: dict[str, dict] = {}
    range_cache_sw: dict[str, dict] = {}
    amt_pctile_cache_sw: dict[str, dict] = {}
    sw_t0_count_maps: dict[str, dict[str, int]] = {}
    sw_hlc_count_maps: dict[str, dict[str, int]] = {}
    conn_ts = None
    try:
        conn_ts = pymysql.connect(**DB_CONFIG_TUSHARE)
        rt_sw_quotes = _fetch_rt_sw_k_quotes(idx_sw_list, conn_ts)
        lookbackDays = (
            SW_AMT_PANEL_LOOKBACK_DAYS
            if waitLimitScan
            else SW_RANGE_PANEL_LOOKBACK_DAYS
        )
        min_amt_d = date.today() - timedelta(days=lookbackDays)
        panel_codes = list(
            dict.fromkeys(
                idx_sw_list + [str(SW_STATS_SUMMARY_ALL_A_SW_TS or '').strip().upper()]
            )
        )
        desc_panel, amt_panel = _load_index_sw_daily_panel_bulk(conn_ts, panel_codes, min_amt_d)
        bench_ts_amt = SW_STATS_SUMMARY_ALL_A_SW_TS
        bench_amt_map = amt_panel.get(str(bench_ts_amt or '').strip().upper(), {})
        bench_rt_q = rt_sw_quotes.get(str(bench_ts_amt or '').strip().upper())
        for tsu in idx_sw_list:
            rows_desc = desc_panel.get(tsu, [])
            range_cache_sw[tsu] = _read_index_sw_daily_interval_range_pct(
                conn_ts, tsu, rt_sw_quotes.get(tsu), rows_desc=rows_desc or None
            )
            if not waitLimitScan:
                continue
            row_amt = amt_panel.get(tsu, {})
            amt_pctile_cache_sw[tsu] = _read_sw_amount_ratio_percentiles(
                conn_ts,
                tsu,
                rt_sw_quotes.get(tsu),
                bench_ts_amt,
                bench_amt_map,
                row_amt,
                bench_rt_quote=bench_rt_q,
                rows_desc=rows_desc or None,
            )
    except Exception as e:
        logger.warning('sw_industry_stats 实时层预取(Tushare)失败: %s', e)
    finally:
        if conn_ts is not None:
            try:
                conn_ts.close()
            except Exception:
                pass
    flagged_by_col: dict[str, set[str]] = {}
    hlcFromCache = False
    try:
        if waitLimitScan:
            sw_t0_count_maps, sw_hlc_count_maps, flagged_by_col = (
                _sw_fetch_limit_count_maps_cached(ts_to_ind, selected_trade_date)
            )
        else:
            flaggedWarm = _sw_peek_limit_count_flagged(ts_to_ind, selected_trade_date)
            if flaggedWarm is not None:
                hlcFromCache = True
                sw_t0_count_maps, sw_hlc_count_maps = _sw_aggregate_limit_maps_from_flagged(
                    flaggedWarm, ts_to_ind
                )
                flagged_by_col = flaggedWarm
    except Exception as e:
        logger.warning('sw_industry_stats 实时层预取(Wind新高低)失败: %s', e)
    else:
        if pool_key and flagged_by_col:
            _sw_limit_flagged_pool_put(pool_key, flagged_by_col, ts_to_ind)
            if waitLimitScan:
                union_ts: set[str] = set()
                for flagged in flagged_by_col.values():
                    union_ts |= flagged
                if union_ts:
                    conn_meta = None
                    try:
                        conn_meta = pymysql.connect(**DB_CONFIG_TUSHARE)
                        _sw_warm_stock_basic_meta_cache(conn_meta, union_ts)
                    except Exception as e:
                        logger.warning('sw_industry_stats 明细元数据预热失败: %s', e)
                    finally:
                        if conn_meta is not None:
                            try:
                                conn_meta.close()
                            except Exception:
                                pass
    if not rt_sw_quotes and not sw_t0_count_maps and not sw_hlc_count_maps:
        return empty
    # 轻量路径合并上一包的成交额分位/新高低，避免 2s 热缓存冲掉重列
    if not waitLimitScan:
        with _swCacheLock:
            oldHit = _swLiveMarketMapsCache.get(cache_key)
            oldMaps = oldHit[1] if oldHit is not None else None
        if oldMaps:
            _oRt, _oRg, oAmt, oT0, oHlc = oldMaps
            if not amt_pctile_cache_sw and oAmt:
                amt_pctile_cache_sw = oAmt
            if not sw_t0_count_maps and oT0:
                sw_t0_count_maps = oT0
            if not sw_hlc_count_maps and oHlc:
                sw_hlc_count_maps = oHlc
                hlcFromCache = True
    result = (
        rt_sw_quotes,
        range_cache_sw,
        amt_pctile_cache_sw,
        sw_t0_count_maps,
        sw_hlc_count_maps,
    )
    # 有行情即可写入 2s 热缓存；轻量包已合并旧分位/新高低，apply 侧空 map 不冲重列
    if waitLimitScan or hlcFromCache or rt_sw_quotes:
        with _swCacheLock:
            while len(_swLiveMarketMapsCache) >= SW_LIVE_MARKET_MAPS_CACHE_MAX:
                _swLiveMarketMapsCache.popitem(last=False)
            _swLiveMarketMapsCache[cache_key] = (
                time.time() + SW_LIVE_MARKET_MAPS_CACHE_TTL,
                result,
            )
            _swLiveMarketMapsCache.move_to_end(cache_key)
    return result


def _sw_live_market_maps_is_fresh(
    ts_to_ind: dict[str, tuple[str, str]],
    selected_trade_date: date | None,
    pool_key: tuple[str, str, str, str] | None,
) -> bool:
    """实时层 maps 是否可跳过后台补齐：TTL 内且已含成交额分位（非轻量空包）。"""
    if not ts_to_ind:
        return True
    group_codes = sorted({info[0] for info in ts_to_ind.values()})
    idx_sw_list = _sw_build_idx_sw_quote_list(group_codes)
    cache_key = _sw_live_market_maps_cache_key(idx_sw_list, selected_trade_date, pool_key)
    now_ts = time.time()
    extraSec = _swLiveMapsOffhoursExtraSec()
    with _swCacheLock:
        hit = _swLiveMarketMapsCache.get(cache_key)
        if hit is None or now_ts >= hit[0] + extraSec:
            return False
        return not _sw_live_maps_missing_heavy(hit[1])


def _swLiveMapsOffhoursExtraSec(now: datetime | None = None) -> float:
    """交易时段 0（原 TTL）；休市时段把 2s TTL 放宽到 SW_LIVE_OFFHOURS_REFRESH_SEC（盘前 09:15 起恢复）。"""
    if SW_LIVE_OFFHOURS_REFRESH_SEC <= 0 or isAShareMarketActive(now):
        return 0.0
    return max(0.0, SW_LIVE_OFFHOURS_REFRESH_SEC - SW_LIVE_MARKET_MAPS_CACHE_TTL)


def _sw_schedule_live_market_refresh(
    statsCacheKey: str,
    ts_to_ind: dict[str, tuple[str, str]],
    selected_trade_date: date | None,
    level: str,
    pool_key: tuple[str, str, str, str] | None,
) -> None:
    """后台刷新长缓存 payload 的实时层（新高低/涨跌/分位/融资），不阻塞当前 HTTP。"""
    jobKey = (statsCacheKey, pool_key or ('', '', '', ''))
    with _swLiveRefreshLock:
        if jobKey in _swLiveRefreshInFlight:
            return
        _swLiveRefreshInFlight.add(jobKey)

    def _worker() -> None:
        t0 = time.perf_counter()
        try:
            payload, tsInd, lvl = _sw_industry_stats_cache_get(statsCacheKey, time.time())
            if payload is None:
                return
            useInd = ts_to_ind or tsInd or {}
            useLvl = lvl or level
            _sw_patch_live_market_fields_in_payload(
                payload,
                useInd,
                selected_trade_date,
                useLvl,
                pool_key=pool_key,
            )
            with _swPayloadMutLock:
                _sw_attach_payload_versions(payload)
            logger.info(
                'sw_industry_stats 实时层后台刷新完成 key=%s elapsed=%.2fs',
                statsCacheKey,
                time.perf_counter() - t0,
            )
        except Exception as e:
            logger.warning(
                'sw_industry_stats 实时层后台刷新失败 key=%s: %s',
                statsCacheKey,
                e,
            )
        finally:
            with _swLiveRefreshLock:
                _swLiveRefreshInFlight.discard(jobKey)

    threading.Thread(
        target=_worker, daemon=True, name=f'sw-live-refresh-{statsCacheKey[:12]}'
    ).start()


def _sw_serve_cached_stats_payload(
    hit_payload: dict,
    hit_ts_to_ind: dict[str, tuple[str, str]] | None,
    hit_level: str | None,
    selected_trade_date: date | None,
    level: str,
    statsCacheKey: str,
    pool_key: tuple[str, str, str, str] | None,
):
    """长缓存命中：HTTP 只 deepcopy 秒返；实时层/财报补齐一律后台。

    禁止在请求线程里同步 patch（margin + 日线面板偶发数秒，前端会 abort）。
    新高低 12 列若 count-maps 仍热，同步贴同源计数，避免与明细名单分叉。
    """
    tsInd = hit_ts_to_ind or {}
    useLvl = hit_level or level
    if hit_payload.get('pending_fina'):
        parts = str(statsCacheKey or '').split('|')
        if len(parts) >= 4:
            _swScheduleFullSwStatsCompute(parts[0], parts[1], parts[2], parts[3])
    if useLvl != 'l3' and tsInd:
        flaggedWarm = _sw_peek_limit_count_flagged(tsInd, selected_trade_date)
        if flaggedWarm is not None:
            _sw_apply_limit_count_maps_to_payload(
                hit_payload,
                tsInd,
                flaggedWarm,
                pool_key=pool_key,
            )
    if not _sw_live_market_maps_is_fresh(tsInd, selected_trade_date, pool_key):
        _sw_schedule_live_market_refresh(
            statsCacheKey,
            tsInd,
            selected_trade_date,
            useLvl,
            pool_key,
        )
        liveMode = 'async'
    else:
        liveMode = 'cached'
    with _swPayloadMutLock:
        out = copy.deepcopy(hit_payload)
    _sw_attach_payload_versions(out)
    out['live_refresh'] = liveMode
    return jsonify(out)


def _sw_patch_margin_fields_in_payload(
    payload: dict,
    ts_to_ind: dict[str, tuple[str, str]],
    selected_trade_date: date | None,
    level: str,
) -> None:
    """命中申万页长缓存时仍刷新融资余额列（margin_detail 日更或同日落库修正）。"""
    if level == 'l3' or not ts_to_ind or not payload:
        return
    sw_margin_by_group, sw_margin_summary, margin_as_of, margin_mode = (
        _sw_fetch_margin_metrics_by_group(ts_to_ind, selected_trade_date)
    )
    margin_keys = tuple(_sw_margin_empty_fields().keys())
    for row in payload.get('rows') or []:
        if not isinstance(row, dict):
            continue
        group_code = str(row.get('l1_code') or '').strip()
        margin_fields = sw_margin_by_group.get(group_code) or {}
        for mk in margin_keys:
            row[mk] = margin_fields.get(mk)
    summary = payload.get('summary_row')
    if isinstance(summary, dict):
        for mk in margin_keys:
            summary[mk] = sw_margin_summary.get(mk)
    payload['margin_as_of'] = margin_as_of.isoformat() if margin_as_of else None
    payload['margin_as_of_mode'] = margin_mode


def _sw_should_heavy_live_refresh() -> bool:
    """成交额分位等重列约 30s 一次；涨跌幅走 2s 轻量路径。"""
    global _swLiveHeavyRefreshMono
    now = time.time()
    with _swLiveHeavyRefreshLock:
        if (now - float(_swLiveHeavyRefreshMono or 0.0)) < float(SW_LIVE_HEAVY_REFRESH_SEC):
            return False
        _swLiveHeavyRefreshMono = now
        return True


def _sw_patch_live_market_fields_in_payload(
    payload: dict,
    ts_to_ind: dict[str, tuple[str, str]],
    selected_trade_date: date | None,
    level: str,
    pool_key: tuple[str, str, str, str] | None = None,
    *,
    allowHeavy: bool = True,
) -> None:
    """命中申万页长缓存时刷新实时层：区间涨跌幅、成交额比%、新高/低 12 列及融资余额。

    allowHeavy=False：HTTP 热路径禁止新高低全扫/7 年成交额面板（改后台）。
    """
    _sw_patch_margin_fields_in_payload(payload, ts_to_ind, selected_trade_date, level)
    if level == 'l3' or not ts_to_ind or not payload:
        return
    rows = payload.get('rows') or []
    if not rows:
        return
    # 首屏空成交额必须立刻重算，不受 30s 节流；节流只用于已有分位后的周期刷新
    needHeavyFill = any(
        isinstance(r, dict) and r.get('sw_amt_pctile_t0') is None for r in rows
    )
    heavy = bool(allowHeavy) and (
        needHeavyFill or _sw_should_heavy_live_refresh()
    )
    (
        rt_sw_quotes,
        range_cache_sw,
        amt_pctile_cache_sw,
        sw_t0_count_maps,
        sw_hlc_count_maps,
    ) = _sw_fetch_live_market_maps(
        ts_to_ind,
        selected_trade_date,
        pool_key=pool_key,
        waitLimitScan=heavy,
    )
    if not rt_sw_quotes and not sw_t0_count_maps and not sw_hlc_count_maps:
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        _sw_apply_live_row_market_fields(
            row,
            rt_sw_quotes=rt_sw_quotes,
            range_cache_sw=range_cache_sw,
            amt_pctile_cache_sw=amt_pctile_cache_sw,
            sw_t0_count_maps=sw_t0_count_maps,
            sw_hlc_count_maps=sw_hlc_count_maps,
        )
    summary = payload.get('summary_row')
    if isinstance(summary, dict):
        for k in (
            'sw_pct', 'sw_pct_t1', 'sw_pct_t5', 'sw_pct_t20', 'sw_pct_t60', 'sw_pct_t120',
            'sw_amt_pctile_t0', 'sw_amt_pctile_t1', 'sw_amt_pctile_t5',
            'sw_amt_pctile_t20', 'sw_amt_pctile_t60', 'sw_amt_pctile_t120',
        ):
            summary[k] = None
        if sw_t0_count_maps:
            for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS:
                summary[jk] = sum(int(r.get(jk) or 0) for r in rows if isinstance(r, dict))
        if sw_hlc_count_maps:
            for jk, _ in _SW_INDUSTRY_HLC_SPECS:
                summary[jk] = sum(int(r.get(jk) or 0) for r in rows if isinstance(r, dict))


def _sw_format_list_date_for_detail(raw) -> str:
    if raw is None:
        return "--"
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    s = str(raw).strip()
    if not s:
        return "--"
    if len(s) >= 10 and s[4] == "-":
        return s[:10]
    digits = s.replace("-", "")[:8]
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return s


def _sw_fetch_stock_basic_limit_detail_rows(
    conn_tushare, ts_codes: list[str]
) -> list[dict]:
    if not ts_codes or conn_tushare is None:
        return []
    out: list[dict] = []
    chunk = 500
    ordered = sorted({str(c).strip() for c in ts_codes if c})
    for i in range(0, len(ordered), chunk):
        part = ordered[i : i + chunk]
        ph = ", ".join(["%s"] * len(part))
        sql = (
            "SELECT ts_code, COALESCE(name, ''), list_date "
            "FROM stock_tushare.stock_basic "
            f"WHERE ts_code IN ({ph}) ORDER BY ts_code ASC"
        )
        with conn_tushare.cursor() as cur:
            cur.execute(sql, tuple(part))
            rows = cur.fetchall() or []
        for r in rows:
            out.append(
                {
                    "ts_code": str(r[0] or "").strip(),
                    "sec_name": str(r[1] or "").strip() or "--",
                    "list_date": _sw_format_list_date_for_detail(
                        r[2] if len(r) > 2 else None
                    ),
                }
            )
    return out


def _sw_query_member_ts_map(
    conn,
    level: str,
    parent_l1_code: str,
    parent_l2_code: str,
) -> tuple[dict[str, tuple[str, str]], dict[str, list[str]]]:
    """与 api_sw_industry_stats 相同的申万成分映射（group_code → 个股列表）。"""
    sql_sw = (
        "SELECT z.ts_code, z.l1_code, z.l1_name, z.l2_code, z.l2_name, z.l3_code, z.l3_name "
        "FROM ( "
        "  SELECT m.ts_code, m.l1_code, m.l1_name, m.l2_code, m.l2_name, m.l3_code, m.l3_name, "
        "    ROW_NUMBER() OVER (PARTITION BY m.ts_code ORDER BY m.in_date DESC, m.l1_code ASC, m.l2_code ASC, m.l2_name ASC) AS rn "
        "  FROM stock_tushare.index_member_all_sw m "
        "  WHERE (m.is_new = 'Y' OR m.is_new IS NULL) "
        "    AND m.l1_code IS NOT NULL AND m.l1_code <> '' "
        "    AND m.l1_name IS NOT NULL AND m.l1_name <> '' "
        "    AND m.l2_code IS NOT NULL AND m.l2_code <> '' "
        "    AND m.l2_name IS NOT NULL AND m.l2_name <> '' "
        ") z "
        "WHERE z.rn = 1"
    )
    with conn.cursor() as cur:
        cur.execute(sql_sw)
        sw_rows = cur.fetchall() or []
    ts_to_ind: dict[str, tuple[str, str]] = {}
    ts_by_group: dict[str, list[str]] = defaultdict(list)
    for row in sw_rows:
        ts_code = str(row[0] or "").strip()
        l1_code = str(row[1] or "").strip()
        l2_code = str(row[3] or "").strip()
        l3_code = str(row[5] or "").strip()
        l3_name = str(row[6] or "").strip()
        l2_name = str(row[4] or "").strip()
        l1_name = str(row[2] or "").strip()
        if not ts_code or not l1_code or not l2_code:
            continue
        if parent_l1_code and l1_code != parent_l1_code:
            continue
        if parent_l2_code and l2_code != parent_l2_code:
            continue
        if level == "l2":
            group_code = l2_code
            group_name = l2_name
        elif level == "l3":
            if not l3_code or not l3_name:
                continue
            group_code = l3_code
            group_name = l3_name
        else:
            group_code = l1_code
            group_name = l1_name
        ts_to_ind[ts_code] = (group_code, group_name)
        ts_by_group[group_code].append(ts_code)
    for gc in list(ts_by_group.keys()):
        ts_by_group[gc] = sorted(set(ts_by_group[gc]))
    return ts_to_ind, dict(ts_by_group)


def read_sw_industry_limit_detail(
    group_code: str,
    column_key: str,
    level: str,
    parent_l1_code: str,
    parent_l2_code: str,
    as_of_date: date | None,
    limit: int = 2000,
) -> dict:
    """申万行业行内新高/低分布单元格明细：代码、名称、上市日期（stock_basic.list_date）。"""
    col = str(column_key or "").strip()
    meta = _SW_LIMIT_DETAIL_COLUMN_META.get(col)
    result: dict = {
        "ok": True,
        "rows": [],
        "error": None,
        "column": col,
        "group_code": str(group_code or "").strip(),
        "trade_date": as_of_date.isoformat() if as_of_date else None,
        "title": meta.get("title") if meta else col,
        "count": 0,
    }
    if not meta:
        result["ok"] = False
        result["error"] = "不支持的 column"
        return result
    if level not in ("l1", "l2", "l3"):
        level = "l1"
    try:
        limit_n = int(limit)
    except Exception:
        limit_n = 2000
    limit_n = max(1, min(limit_n, 5000))
    pool_key = _sw_limit_flagged_pool_key(level, parent_l1_code, parent_l2_code, as_of_date)
    flagged_by_col, ts_to_ind_pool = _sw_limit_flagged_pool_get(pool_key)
    if flagged_by_col is not None and col in flagged_by_col and ts_to_ind_pool:
        flagged = flagged_by_col.get(col) or set()
        gc = str(group_code or "").strip()
        if gc:
            scope = {ts for ts, (g, _) in ts_to_ind_pool.items() if g == gc}
            matched = sorted(flagged & scope)
        else:
            matched = sorted(flagged)
        total_matched = len(matched)
        if len(matched) > limit_n:
            matched = matched[:limit_n]
        conn_t = None
        conn_w = None
        try:
            conn_t = pymysql.connect(**DB_CONFIG_TUSHARE)
            conn_w = pymysql.connect(**DB_CONFIG_WIND_BULK)
            rows = _sw_build_limit_detail_rows_from_ts_codes(
                conn_t,
                matched,
                column_key=col,
                as_of_date=as_of_date,
                conn_wind=conn_w,
            )
            result["rows"] = rows
            result["count"] = len(rows)
            result["from_pool"] = True
            if col in _SW_LIMIT_DETAIL_SW_INDUSTRY_COLS:
                result["detail_extra"] = True
                result["has_sw_industry"] = True
            if break_mode := _SW_LIMIT_DETAIL_BREAK_MODE_BY_COL.get(col):
                result["break_mode"] = break_mode
                result["has_break_timing"] = True
                if spec := _SW_LIMIT_DETAIL_BREAK_SPEC_BY_COL.get(col):
                    result["break_direction"] = spec[1]
            if total_matched > limit_n:
                result["truncated"] = True
                result["total_matched"] = total_matched
            if as_of_date is not None and not rows and ts_to_ind_pool:
                result["hint"] = (
                    f"{as_of_date.isoformat()} 暂无归档明细；"
                    "仅收市并完成归档的交易日可查，或点「恢复今日」查看盘中"
                )
            return result
        finally:
            for c in (conn_w, conn_t):
                if c is not None:
                    try:
                        c.close()
                    except Exception:
                        pass

    conn_t = None
    conn_w = None
    try:
        conn_t = pymysql.connect(**DB_CONFIG_TUSHARE)
        ts_to_ind_cached = ts_to_ind_pool
        if ts_to_ind_cached:
            _ts_to_ind = ts_to_ind_cached
            ts_by_group: dict[str, list[str]] = defaultdict(list)
            for ts, (g, _) in _ts_to_ind.items():
                ts_by_group[g].append(ts)
            for gk in list(ts_by_group.keys()):
                ts_by_group[gk] = sorted(set(ts_by_group[gk]))
        else:
            _ts_to_ind, ts_by_group = _sw_query_member_ts_map(
                conn_t, level, parent_l1_code, parent_l2_code
            )
        gc = str(group_code or "").strip()
        if gc:
            scope_ts = list(ts_by_group.get(gc, []))
        else:
            scope_ts = sorted(_ts_to_ind.keys())
        if not scope_ts:
            return result
        flagged: set[str] = set()
        if as_of_date is not None:
            arch_kind = str(meta.get("archive_kind") or "").strip()
            if not arch_kind:
                result["ok"] = False
                result["error"] = "历史归档 kind 未配置"
                return result
            flagged = _sw_fetch_archive_ts_codes_by_kind(
                conn_t, scope_ts, as_of_date, arch_kind
            )
        else:
            # 与主表计数同源：走 limit_count 缓存，禁止绕过现查导致「表 N / 明细 ≠N」
            _t0, _hlc, full_flagged = _sw_fetch_limit_count_maps_cached(
                _ts_to_ind, as_of_date
            )
            flagged = set(full_flagged.get(col, set()))
            if gc:
                flagged &= set(scope_ts)
            if full_flagged and _ts_to_ind:
                _sw_limit_flagged_pool_put(pool_key, full_flagged, _ts_to_ind)
        matched = sorted(flagged)
        total_matched = len(matched)
        if len(matched) > limit_n:
            matched = matched[:limit_n]
        # 收盘价六列突破类型需要 rt_k 最新交易日
        if (
            as_of_date is None
            and conn_w is None
            and col in _SW_LIMIT_DETAIL_SW_INDUSTRY_COLS
            and matched
        ):
            conn_w = pymysql.connect(**DB_CONFIG_WIND_BULK)
        rows = _sw_build_limit_detail_rows_from_ts_codes(
            conn_t,
            matched,
            column_key=col,
            as_of_date=as_of_date,
            conn_wind=conn_w,
        )
        result["rows"] = rows
        result["count"] = len(rows)
        if col in _SW_LIMIT_DETAIL_SW_INDUSTRY_COLS:
            result["detail_extra"] = True
            result["has_sw_industry"] = True
        if _SW_LIMIT_DETAIL_BREAK_MODE_BY_COL.get(col):
            result["break_mode"] = _SW_LIMIT_DETAIL_BREAK_MODE_BY_COL[col]
            result["has_break_timing"] = True
            if spec := _SW_LIMIT_DETAIL_BREAK_SPEC_BY_COL.get(col):
                result["break_direction"] = spec[1]
        if as_of_date is not None and not rows and scope_ts:
            result["hint"] = (
                f"{as_of_date.isoformat()} 暂无归档明细；"
                "仅收市并完成归档的交易日可查，或点「恢复今日」查看盘中"
            )
        if total_matched > limit_n:
            result["truncated"] = True
            result["total_matched"] = total_matched
        return result
    except Exception as e:
        logger.exception("sw_industry_limit_detail 查询失败: %s", e)
        result["ok"] = False
        result["error"] = str(e)
        return result
    finally:
        for c in (conn_w, conn_t):
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass


def apiSwIndustryLimitDetail():
    """申万行业页：新高/低分布列点击明细（代码、名称、上市日期；收盘价六列含申万行业+突破类型）。"""
    column_key = str(request.args.get('column') or request.args.get('col') or '').strip()
    group_code = str(
        request.args.get('group_code') or request.args.get('industry_code') or ''
    ).strip()
    level = str(request.args.get('level') or 'l1').strip().lower()
    parent_l1_code = str(request.args.get('parent_l1_code') or '').strip()
    parent_l2_code = str(request.args.get('parent_l2_code') or '').strip()
    as_of_date = _parse_sw_industry_trade_date_param(request.args.get('trade_date'))
    try:
        limit_n = int(request.args.get('limit') or 2000)
    except Exception:
        limit_n = 2000
    cache_key = (
        column_key,
        group_code,
        level,
        parent_l1_code,
        parent_l2_code,
        as_of_date.isoformat() if as_of_date else '',
        limit_n,
    )
    now_ts = time.time()
    hit = smallTtlCacheGet(_sw_industry_limit_detail_cache, cache_key, now_ts)
    if hit is not None:
        return jsonify(hit)
    payload = read_sw_industry_limit_detail(
        group_code,
        column_key,
        level,
        parent_l1_code,
        parent_l2_code,
        as_of_date,
        limit=limit_n,
    )
    if payload.get('ok'):
        smallTtlCachePut(
            _sw_industry_limit_detail_cache,
            cache_key,
            payload,
            SW_INDUSTRY_LIMIT_DETAIL_CACHE_TTL,
            SW_INDUSTRY_LIMIT_DETAIL_CACHE_MAX,
        )
        return jsonify(payload)
    return jsonify(payload), 400


def _sw_pct_t_detail_pack_row(
    tsCode: str,
    name: str | None,
    pctChg: float | None,
    amountYi: float | None,
    totalMvYi: float | None,
    freeMvYi: float | None,
    freeTurnoverPct: float | None,
) -> dict:
    return {
        'ts_code': tsCode,
        'name': (name or '').strip() or '--',
        'pct_chg': pctChg,
        'amount_yi': amountYi,
        'total_mv_yi': totalMvYi,
        'free_mv_yi': freeMvYi,
        'free_turnover_pct': freeTurnoverPct,
        # 与 fina「当前价位置」同源：rt_k.close ÷ stk_factor_ma 极值
        'pct_vs_h_high': None,
        'pct_vs_high_850': None,
        'pct_vs_high_250': None,
    }


def _sw_attach_pct_t_price_gaps(connT, rowLists: list[list[dict]]) -> None:
    """为 Top 列表就地写入距历史高/850高/250高%（复用 fina 口径）。"""
    byCode: dict[str, dict] = {}
    for rows in rowLists:
        for r in rows or []:
            tc = str(r.get('ts_code') or '').strip()
            if tc and tc not in byCode:
                byCode[tc] = r
    if not byCode:
        return
    try:
        _fina_arturn_attach_price_range_pct(connT, list(byCode.values()))
    except Exception as e:
        logger.warning('sw_pct_t_detail 附加当前价位置跳过: %s', e)


def _sw_fetch_pct_t_quotes_hist(
    connT, tsCodes: list[str], asOf: date
) -> dict[str, dict]:
    """历史日：stk_factor_pro 当日 pct/amount/市值/自由换手。amount 千元→亿。"""
    out: dict[str, dict] = {}
    if not tsCodes:
        return out
    hasTurnF = _tushare_column_exists(connT, 'stk_factor_pro', 'turnover_rate_f')
    hasFs = _tushare_column_exists(connT, 'stk_factor_pro', 'free_share')
    hasTmv = _tushare_column_exists(connT, 'stk_factor_pro', 'total_mv')
    sel = ['ts_code', 'pct_chg', 'amount', 'close']
    if hasTmv:
        sel.append('total_mv')
    if hasFs:
        sel.append('free_share')
    if hasTurnF:
        sel.append('turnover_rate_f')
    colSql = ', '.join(sel)
    chunk = 480
    for i in range(0, len(tsCodes), chunk):
        part = tsCodes[i : i + chunk]
        ph = ', '.join(['%s'] * len(part))
        sql = (
            f'SELECT {colSql} FROM stock_tushare.stk_factor_pro '
            f'WHERE trade_date = %s AND ts_code IN ({ph})'
        )
        with connT.cursor() as cur:
            cur.execute(sql, (asOf, *part))
            for raw in cur.fetchall() or []:
                if not raw or raw[0] is None:
                    continue
                tc = str(raw[0]).strip()
                if not tc:
                    continue
                pct = _to_float_or_none(raw[1])
                amtQian = _to_float_or_none(raw[2])
                closeV = _to_float_or_none(raw[3])
                idx = 4
                tmv = None
                fs = None
                turnF = None
                if hasTmv:
                    tmv = _to_float_or_none(raw[idx]) if len(raw) > idx else None
                    idx += 1
                if hasFs:
                    fs = _to_float_or_none(raw[idx]) if len(raw) > idx else None
                    idx += 1
                if hasTurnF:
                    turnF = _to_float_or_none(raw[idx]) if len(raw) > idx else None
                amountYi = round(amtQian / 1e5, 2) if amtQian is not None else None
                totalMvYi = round(tmv / 10000.0, 2) if tmv is not None else None
                freeMvYi = None
                if fs is not None and closeV is not None:
                    freeMvYi = round(fs * closeV / 10000.0, 2)
                freeTurn = round(turnF, 2) if turnF is not None else None
                out[tc] = {
                    'pct_chg': round(pct, 2) if pct is not None else None,
                    'amount_yi': amountYi,
                    'total_mv_yi': totalMvYi,
                    'free_mv_yi': freeMvYi,
                    'free_turnover_pct': freeTurn,
                    'name': None,
                }
    return out


def _sw_fetch_pct_t_quotes_live(
    connW, connT, tsCodes: list[str]
) -> dict[str, dict]:
    """盘中：rt_k 涨跌/成交额/量价 + stk_factor_pro 最新市值/自由股本。"""
    out: dict[str, dict] = {}
    if not tsCodes:
        return out
    chunk = 480
    for i in range(0, len(tsCodes), chunk):
        part = tsCodes[i : i + chunk]
        ph = ', '.join(['%s'] * len(part))
        sql = (
            'SELECT rt.ts_code, COALESCE(rt.name, ""), rt.close, rt.pre_close, rt.vol, rt.amount '
            'FROM stock_wind.rt_k rt '
            f'WHERE rt.ts_code IN ({ph}) AND {_LIMIT_STATS_RT_K_FRESH_TRADE_DATE} '
            'AND NOT (rt.vol = 0 AND rt.amount = 0)'
        )
        with connW.cursor() as cur:
            cur.execute(sql, tuple(part))
            for raw in cur.fetchall() or []:
                if not raw or raw[0] is None:
                    continue
                tc = str(raw[0]).strip()
                if not tc:
                    continue
                name = str(raw[1] or '').strip()
                closeV = _to_float_or_none(raw[2])
                preClose = _to_float_or_none(raw[3])
                volV = _to_float_or_none(raw[4])
                amtYuan = _to_float_or_none(raw[5])
                pct = None
                if closeV is not None and preClose is not None and preClose > 0:
                    pct = round((closeV / preClose - 1.0) * 100.0, 2)
                amountYi = round(amtYuan / 1e8, 2) if amtYuan is not None else None
                out[tc] = {
                    'name': name or None,
                    'pct_chg': pct,
                    'amount_yi': amountYi,
                    'close': closeV,
                    'vol': volV,
                    'total_mv_yi': None,
                    'free_mv_yi': None,
                    'free_turnover_pct': None,
                }
    if not out:
        return out
    codes = list(out.keys())
    hasFs = _tushare_column_exists(connT, 'stk_factor_pro', 'free_share')
    hasTmv = _tushare_column_exists(connT, 'stk_factor_pro', 'total_mv')
    if not (hasFs or hasTmv):
        return out
    for i in range(0, len(codes), chunk):
        part = codes[i : i + chunk]
        ph = ', '.join(['%s'] * len(part))
        sql = (
            'SELECT a.ts_code, a.total_mv, '
            'COALESCE(a.free_share, fs.free_share) AS free_share '
            'FROM ( '
            '  SELECT f.ts_code, f.total_mv, f.free_share '
            '  FROM stock_tushare.stk_factor_pro f '
            '  INNER JOIN ( '
            '    SELECT ts_code, MAX(trade_date) AS mx '
            '    FROM stock_tushare.stk_factor_pro '
            f'    WHERE ts_code IN ({ph}) GROUP BY ts_code '
            '  ) lm ON f.ts_code = lm.ts_code AND f.trade_date = lm.mx '
            ') a '
            'LEFT JOIN ( '
            '  SELECT f.ts_code, f.free_share '
            '  FROM stock_tushare.stk_factor_pro f '
            '  INNER JOIN ( '
            '    SELECT ts_code, MAX(trade_date) AS mx '
            '    FROM stock_tushare.stk_factor_pro '
            f'    WHERE ts_code IN ({ph}) '
            '      AND free_share IS NOT NULL AND free_share > 0 '
            '    GROUP BY ts_code '
            '  ) lm2 ON f.ts_code = lm2.ts_code AND f.trade_date = lm2.mx '
            ') fs ON a.ts_code = fs.ts_code'
        )
        with connT.cursor() as cur:
            cur.execute(sql, tuple(part) + tuple(part))
            for raw in cur.fetchall() or []:
                if not raw or raw[0] is None:
                    continue
                tc = str(raw[0]).strip()
                row = out.get(tc)
                if not row:
                    continue
                tmv = _to_float_or_none(raw[1]) if hasTmv else None
                fs = _to_float_or_none(raw[2]) if hasFs else None
                if tmv is not None:
                    row['total_mv_yi'] = round(tmv / 10000.0, 2)
                closeV = _to_float_or_none(row.get('close'))
                if fs is not None and closeV is not None:
                    row['free_mv_yi'] = round(fs * closeV / 10000.0, 2)
                volV = _to_float_or_none(row.get('vol'))
                if volV is not None and fs is not None and fs > 0:
                    row['free_turnover_pct'] = round(volV / (fs * 10000.0) * 100.0, 2)
    return out


def _sw_attach_pct_t_names(connT, rowsByCode: dict[str, dict]) -> None:
    missing = [tc for tc, r in rowsByCode.items() if not (r.get('name') or '').strip()]
    if not missing:
        return
    basics = _sw_fetch_stock_basic_limit_detail_rows(connT, missing)
    nameMap = {b['ts_code']: b.get('sec_name') for b in basics}
    for tc in missing:
        nm = nameMap.get(tc)
        if nm:
            rowsByCode[tc]['name'] = nm


def read_sw_industry_pct_t_detail(
    groupCode: str,
    level: str,
    parentL1Code: str,
    parentL2Code: str,
    asOfDate: date | None,
    topN: int = 20,
) -> dict:
    """申万行业「区间涨跌幅 T」点击：成分股涨幅 TopN + 成交额 TopN。"""
    result: dict = {
        'ok': True,
        'error': None,
        'group_code': str(groupCode or '').strip(),
        'industry_name': '',
        'trade_date': asOfDate.isoformat() if asOfDate else None,
        'mode': 'historical' if asOfDate else 'live',
        'member_count': 0,
        'top_n': 20,
        'pct_top20': [],
        'amount_top20': [],
        # 兼容旧前端键名
        'pct_top10': [],
        'amount_top10': [],
    }
    gc = str(groupCode or '').strip()
    if not gc:
        result['ok'] = False
        result['error'] = '缺少 group_code'
        return result
    if level not in ('l1', 'l2', 'l3'):
        level = 'l1'
    try:
        n = int(topN)
    except Exception:
        n = 20
    n = max(1, min(n, 50))
    result['top_n'] = n
    connT = None
    connW = None
    try:
        connT = pymysql.connect(**DB_CONFIG_TUSHARE)
        _tsToInd, tsByGroup = _sw_query_member_ts_map(
            connT, level, parentL1Code, parentL2Code
        )
        scopeTs = list(tsByGroup.get(gc, []))
        if not scopeTs:
            result['error'] = None
            result['hint'] = '该行业暂无成分股'
            return result
        indName = ''
        for ts in scopeTs:
            pair = _tsToInd.get(ts)
            if pair and pair[0] == gc:
                indName = str(pair[1] or '').strip()
                if indName:
                    break
        result['industry_name'] = indName
        result['member_count'] = len(scopeTs)
        if asOfDate is not None:
            quoteMap = _sw_fetch_pct_t_quotes_hist(connT, scopeTs, asOfDate)
        else:
            connW = pymysql.connect(**DB_CONFIG_WIND_BULK)
            quoteMap = _sw_fetch_pct_t_quotes_live(connW, connT, scopeTs)
        _sw_attach_pct_t_names(connT, quoteMap)
        packed: list[dict] = []
        for tc in scopeTs:
            q = quoteMap.get(tc) or {}
            packed.append(
                _sw_pct_t_detail_pack_row(
                    tc,
                    q.get('name'),
                    q.get('pct_chg'),
                    q.get('amount_yi'),
                    q.get('total_mv_yi'),
                    q.get('free_mv_yi'),
                    q.get('free_turnover_pct'),
                )
            )
        pctSorted = sorted(
            [r for r in packed if r.get('pct_chg') is not None],
            key=lambda r: float(r['pct_chg']),
            reverse=True,
        )[:n]
        amtSorted = sorted(
            [
                r
                for r in packed
                if r.get('amount_yi') is not None and float(r['amount_yi']) > 0
            ],
            key=lambda r: float(r['amount_yi']),
            reverse=True,
        )[:n]
        _sw_attach_pct_t_price_gaps(connT, [pctSorted, amtSorted])
        result['pct_top20'] = pctSorted
        result['amount_top20'] = amtSorted
        result['pct_top10'] = pctSorted
        result['amount_top10'] = amtSorted
        if asOfDate is not None and not packed:
            result['hint'] = f'{asOfDate.isoformat()} 无成分行情'
        return result
    except Exception as e:
        logger.exception('sw_industry_pct_t_detail 查询失败: %s', e)
        result['ok'] = False
        result['error'] = str(e)
        return result
    finally:
        for c in (connW, connT):
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass


def apiSwIndustryPctTDetail():
    """申万行业页：区间涨跌幅 T 点击 → 涨幅 Top20 + 成交额 Top20（含距高低点%）。"""
    groupCode = str(
        request.args.get('group_code') or request.args.get('industry_code') or ''
    ).strip()
    level = str(request.args.get('level') or 'l1').strip().lower()
    parentL1Code = str(request.args.get('parent_l1_code') or '').strip()
    parentL2Code = str(request.args.get('parent_l2_code') or '').strip()
    asOfDate = _parse_sw_industry_trade_date_param(request.args.get('trade_date'))
    try:
        topN = int(request.args.get('top_n') or request.args.get('limit') or 20)
    except Exception:
        topN = 20
    cacheKey = (
        groupCode,
        level,
        parentL1Code,
        parentL2Code,
        asOfDate.isoformat() if asOfDate else '',
        topN,
        'price_gap_v1',
    )
    nowTs = time.time()
    hit = smallTtlCacheGet(_sw_industry_pct_t_detail_cache, cacheKey, nowTs)
    if hit is not None:
        return jsonify(hit)
    payload = read_sw_industry_pct_t_detail(
        groupCode, level, parentL1Code, parentL2Code, asOfDate, topN=topN
    )
    if payload.get('ok'):
        smallTtlCachePut(
            _sw_industry_pct_t_detail_cache,
            cacheKey,
            payload,
            SW_INDUSTRY_PCT_T_DETAIL_CACHE_TTL,
            SW_INDUSTRY_PCT_T_DETAIL_CACHE_MAX,
        )
        return jsonify(payload)
    return jsonify(payload), 400


def apiSwIndexAmtPctileSeries():
    """申万行业指数：近 N 年收盘 + 日度成交额比%分位序列（Amt% 弹窗双轴图）。"""
    rawTs = str(request.args.get('ts_code') or '').strip()
    tsCode = _normalize_sw_industry_index_ts_code(rawTs) or rawTs.strip().upper()
    try:
        years = int(float(request.args.get('years') or 3))
    except (TypeError, ValueError):
        years = 3
    cacheKey = f'{tsCode}|{years}'
    nowTs = time.time()
    hit = smallTtlCacheGet(_sw_amt_pctile_series_cache, cacheKey, nowTs)
    if hit is not None:
        return jsonify(hit)
    conn = None
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
        payload = _compute_sw_index_close_amt_pctile_series(conn, tsCode, years)
        if payload.get('success'):
            smallTtlCachePut(
                _sw_amt_pctile_series_cache,
                cacheKey,
                payload,
                SW_AMT_PCTILE_SERIES_CACHE_TTL,
                SW_AMT_PCTILE_SERIES_CACHE_MAX,
            )
        return jsonify(payload)
    except Exception as e:
        logger.error('sw_index_amt_pctile_series 失败: %s', e)
        return jsonify(
            {
                'success': False,
                'error': str(e),
                'ts_code': tsCode,
                'bench_ts': str(SW_STATS_SUMMARY_ALL_A_SW_TS or '801003.SI'),
                'years': years,
                'trade_date': [],
                'close': [],
                'amt_pctile': [],
            }
        ), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def apiSwIndustryStats():
    """申万行业统计（支持 level=l1/l2/l3）：家数 + 最近13季单季营收（亿）+ 营收同比增速(%)
    + 单季扣非净利（亿）+ 扣非同比增速(%)；营收/扣非同比均为上年同季可比口径；
    s_high/s_qoq、dtp_high/dtp_qoq 为 s*/dtp* 各季序列的新高与环比连升次数（同 qcf_high/qcf_qoq 口径）；
    + dnk{idx}：行业全样本上年同季负、当季正；可选扩展 has_sw_extend_metrics 时每季另含
    ycf{idx} 单季经营现金流净额同比增速(%)（拆单季后与营收 y{idx} 同可比累加口径）、
    ycfnk{idx}：ycf 为空但行业单季现金流合计（与 qcf 同源）上年同季<0 且当季>0 时为真，前端显示「转正」（对齐扣非 dy/dnk）；
    qcf{idx} 单季经营现金流行业累计（亿）、qgm{idx} 单季毛利率行业中位数（%）、qroe{idx} 单季扣非ROE 行业中位数（%）、
    qcf_high/qcf_qoq、qgm_high/qgm_qoq、qroe_high/qroe_qoq：对上述现金流累计或中位数13季序列的新高与环比连升次数（口径同财报页 m_high/m_qoq）；
    cl{idx} 合同负债行业累计（亿）、fa{idx} 固定资产行业累计（亿）；取数与财报页 /rt-monitor/fina 单季逻辑一致；进程内 LRU 缓存。"""
    level = str(request.args.get('level') or 'l1').strip().lower()
    parent_l1_code = str(request.args.get('parent_l1_code') or '').strip()
    parent_l2_code = str(request.args.get('parent_l2_code') or '').strip()
    selected_trade_date = _parse_sw_industry_trade_date_param(request.args.get('trade_date'))
    if level not in ('l1', 'l2', 'l3'):
        level = 'l1'
    if parent_l2_code:
        level = 'l3'
    # 只要指定了一级行业筛选，则按二级行业明细返回（满足“选一级看二级明细”）
    elif parent_l1_code:
        level = 'l2'
    # x12：缓存命中时刷新实时层（区间涨跌幅/成交额比%/新高低/融资余额）；rt_sw_k 含 amount
    trade_date_key = selected_trade_date.isoformat() if selected_trade_date else ''
    cache_key = f'{level}|{parent_l1_code}|{parent_l2_code}|{trade_date_key}|x12'
    limit_pool_key = _sw_limit_flagged_pool_key(
        level, parent_l1_code, parent_l2_code, selected_trade_date
    )
    now_ts = time.time()
    wantFull = str(request.args.get('_full') or '').strip() == '1'
    hit_payload, hit_ts_to_ind, hit_level = _sw_industry_stats_cache_get(cache_key, now_ts)
    if hit_payload is not None:
        # 完整包请求不得被 pending 占位短路，否则后台永远补不齐财报
        if not (wantFull and hit_payload.get('pending_fina')):
            return _sw_serve_cached_stats_payload(
                hit_payload,
                hit_ts_to_ind,
                hit_level,
                selected_trade_date,
                level,
                cache_key,
                limit_pool_key,
            )

    statsGate = f'stats|{cache_key}'
    isStatsLeader = False
    statsGateEv = None
    if wantFull:
        # 最多抢两次主算：等待方醒来后若仍是 pending 占位，禁止直接当完成返回
        for _gateAttempt in range(2):
            isStatsLeader, statsGateEv = _swWaitOrLeadCompute(statsGate)
            if isStatsLeader:
                break
            hit_payload, hit_ts_to_ind, hit_level = _sw_industry_stats_cache_get(
                cache_key, time.time()
            )
            if hit_payload is not None and not hit_payload.get('pending_fina'):
                return _sw_serve_cached_stats_payload(
                    hit_payload,
                    hit_ts_to_ind,
                    hit_level,
                    selected_trade_date,
                    level,
                    cache_key,
                    limit_pool_key,
                )
            # 仍 pending：再抢一次；第二次仍失败则尽力返回当前缓存
        if not isStatsLeader:
            hit_payload, hit_ts_to_ind, hit_level = _sw_industry_stats_cache_get(
                cache_key, time.time()
            )
            if hit_payload is not None:
                return _sw_serve_cached_stats_payload(
                    hit_payload,
                    hit_ts_to_ind,
                    hit_level,
                    selected_trade_date,
                    level,
                    cache_key,
                    limit_pool_key,
                )

    conn = None
    try:
        has_sw_extend_metrics = False
        isPartial = False
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
        sw_rows = _sw_load_member_all_sw_rows(conn)
        if not sw_rows:
            payload = {
                'success': True,
                'rows': [],
                'summary_row': None,
                'periods': [],
                'has_q_dtprofit': False,
                'level': level,
                'selected_l1_code': parent_l1_code,
                'selected_l1_name': None,
                'selected_l2_code': parent_l2_code,
                'selected_l2_name': None,
                'l1_options': [],
                'l2_options': [],
                'count': 0,
                'has_sw_extend_metrics': False,
                'cache_ttl_sec': SW_INDUSTRY_STATS_CACHE_TTL_API_SEC,
            }
            _sw_industry_stats_cache_put(
                cache_key,
                payload,
                {},
                level,
                SW_INDUSTRY_STATS_CACHE_TTL,
                SW_INDUSTRY_STATS_CACHE_MAX,
            )
            _sw_attach_payload_versions(payload)
            return jsonify(payload)

        ts_to_ind: dict[str, tuple[str, str]] = {}
        ts_to_parent: dict[str, tuple[str, str]] = {}
        ind_meta: dict[str, dict] = {}
        l1_option_map: dict[str, str] = {}
        l2_option_map: dict[str, str] = {}
        for row in sw_rows:
            ts_code = str(row[0] or '').strip()
            l1_code = str(row[1] or '').strip()
            l1_name = str(row[2] or '').strip()
            l2_code = str(row[3] or '').strip()
            l2_name = str(row[4] or '').strip()
            l3_code = str(row[5] or '').strip()
            l3_name = str(row[6] or '').strip()
            if not ts_code or not l1_code or not l2_code:
                continue
            l1_option_map[l1_code] = l1_name
            if not parent_l1_code or l1_code == parent_l1_code:
                l2_option_map[l2_code] = l2_name
            if parent_l1_code and l1_code != parent_l1_code:
                continue
            if parent_l2_code and l2_code != parent_l2_code:
                continue
            if level == 'l2':
                group_code = l2_code
                group_name = l2_name
            elif level == 'l3':
                if not l3_code or not l3_name:
                    continue
                group_code = l3_code
                group_name = l3_name
            else:
                group_code = l1_code
                group_name = l1_name
            ts_to_ind[ts_code] = (group_code, group_name)
            ts_to_parent[ts_code] = (l1_code, l1_name)
            if group_code not in ind_meta:
                ind_meta[group_code] = {'l1_name': group_name, 'company_count': 0}
            ind_meta[group_code]['company_count'] += 1
        ts_by_l1: dict[str, list[str]] = defaultdict(list)
        for _ts, (_lc, _) in ts_to_ind.items():
            ts_by_l1[_lc].append(_ts)
        ts_codes = sorted(ts_to_ind.keys())
        if not ts_codes:
            payload = {
                'success': True,
                'rows': [],
                'summary_row': None,
                'periods': [],
                'has_q_dtprofit': False,
                'level': level,
                'selected_l1_code': parent_l1_code,
                'selected_l1_name': l1_option_map.get(parent_l1_code),
                'selected_l2_code': parent_l2_code,
                'selected_l2_name': l2_option_map.get(parent_l2_code),
                'l1_options': [{'l1_code': k, 'l1_name': v} for k, v in sorted(l1_option_map.items(), key=lambda x: x[1])],
                'l2_options': [{'l2_code': k, 'l2_name': v} for k, v in sorted(l2_option_map.items(), key=lambda x: x[1])],
                'count': 0,
                'has_sw_extend_metrics': False,
                'cache_ttl_sec': SW_INDUSTRY_STATS_CACHE_TTL_API_SEC,
            }
            _sw_industry_stats_cache_put(
                cache_key,
                payload,
                {},
                level,
                SW_INDUSTRY_STATS_CACHE_TTL,
                SW_INDUSTRY_STATS_CACHE_MAX,
            )
            _sw_attach_payload_versions(payload)
            return jsonify(payload)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT end_date FROM stock_tushare.income "
                "WHERE end_date IS NOT NULL "
                "ORDER BY end_date DESC LIMIT 13"
            )
            raw_periods = cur.fetchall() or []
        periods = [_normalize_end_date_yyyymmdd(r[0]) for r in raw_periods if r and r[0] is not None]
        periods = [p for p in periods if p]

        live_empty: tuple = ({}, {}, {}, {}, {})
        finaKey = _sw_fina_batch_cache_key(ts_codes, trade_date_key, periods)
        with _swCacheLock:
            finaReady = _swFinaBatchCache.get(finaKey) is not None
        isPartial = (not wantFull) and (not finaReady) and level != 'l3'
        if isPartial:
            _swScheduleFullSwStatsCompute(
                level, parent_l1_code, parent_l2_code, trade_date_key
            )
            fina_bundle = _swEmptyFinaBundle()
            try:
                live_maps = _sw_fetch_live_market_maps(
                    ts_to_ind, selected_trade_date, limit_pool_key, False
                )
            except Exception as e:
                logger.warning(
                    'sw_industry_stats 首屏实时层预取失败: %s', e
                )
                live_maps = live_empty
            sw_margin_by_group = {}
            sw_margin_summary = _sw_margin_empty_fields()
            margin_as_of = None
            margin_as_of_mode = None
        elif level != 'l3':
            with ThreadPoolExecutor(max_workers=2) as pool:
                fina_future = pool.submit(
                    _sw_load_fina_batch_bundle, ts_codes, periods, trade_date_key
                )
                live_future = pool.submit(
                    _sw_fetch_live_market_maps,
                    ts_to_ind,
                    selected_trade_date,
                    limit_pool_key,
                    False,
                )
                fina_bundle = fina_future.result()
                try:
                    live_maps = live_future.result()
                except Exception as e:
                    logger.warning(
                        'sw_industry_stats 区间涨跌幅/成交额比/新高低预取失败: %s', e
                    )
                    live_maps = live_empty
            sw_margin_by_group, sw_margin_summary, margin_as_of, margin_as_of_mode = (
                _sw_fetch_margin_metrics_by_group(ts_to_ind, selected_trade_date)
            )
        else:
            fina_bundle = _sw_load_fina_batch_bundle(ts_codes, periods, trade_date_key)
            live_maps = live_empty
            sw_margin_by_group = {}
            sw_margin_summary = _sw_margin_empty_fields()
            margin_as_of = None
            margin_as_of_mode = None

        rev_nested = fina_bundle['rev_nested']
        dtp_nested = fina_bundle['dtp_nested']
        has_q_dtprofit = fina_bundle['has_q_dtprofit']
        cf_nested_sw = fina_bundle['cf_nested_sw']
        margin_nested_sw = fina_bundle['margin_nested_sw']
        bs_cf_nested_sw = fina_bundle['bs_cf_nested_sw']
        has_sw_extend_metrics = fina_bundle['has_sw_extend_metrics']
        sfp_map = fina_bundle['sfp_map']
        close_map = fina_bundle['close_map']
        (
            rt_sw_quotes,
            range_cache_sw,
            amt_pctile_cache_sw,
            sw_t0_count_maps,
            sw_hlc_count_maps,
        ) = live_maps

        ind_rev: dict[str, dict[str, float]] = defaultdict(dict)
        for l1_code in ind_meta.keys():
            for p in periods:
                ind_rev[l1_code][p] = 0.0
        for ts_code, rev_map in rev_nested.items():
            ind_info = ts_to_ind.get(ts_code)
            if ind_info is None:
                continue
            l1_code = ind_info[0]
            for p in periods:
                yq = _fina_yq_from_anchor(p)
                if not yq:
                    continue
                sq = _fina_quarter_single_revenue(rev_map, yq[0], yq[1])
                if sq is not None:
                    ind_rev[l1_code][p] += float(sq)

        ind_total_mv: dict[str, float] = defaultdict(float)
        ind_free_mv: dict[str, float] = defaultdict(float)
        market_total_mv = 0.0
        market_free_mv = 0.0
        for tc in ts_codes:
            ind_info = ts_to_ind.get(tc)
            if ind_info is None:
                continue
            l1_code = ind_info[0]
            pair = sfp_map.get(tc)
            if not pair:
                continue
            tmv_yi, fs_wangu = pair
            if tmv_yi is not None:
                ind_total_mv[l1_code] += float(tmv_yi)
                market_total_mv += float(tmv_yi)
            if fs_wangu is not None:
                close_v = close_map.get(tc)
                if close_v is not None:
                    free_yi = float(fs_wangu) * float(close_v) / 10000.0
                    ind_free_mv[l1_code] += free_yi
                    market_free_mv += free_yi

        period_keys_for_dtp: set[str] = set()
        ind_dtp: dict[str, dict[str, float]] = defaultdict(dict)
        market_dtp_by_p: dict[str, float] = defaultdict(float)
        if has_q_dtprofit:
            period_keys_for_dtp = set(periods)
            for _p in periods:
                _pp = _fina_yoy_anchor_end_date(_p)
                if _pp:
                    period_keys_for_dtp.add(_pp)
            for l1_code in ind_meta.keys():
                for pk in period_keys_for_dtp:
                    ind_dtp[l1_code][pk] = 0.0
            for ts_code, ed_map in dtp_nested.items():
                ind_info = ts_to_ind.get(ts_code)
                if ind_info is None:
                    continue
                l1_code = ind_info[0]
                for pk in period_keys_for_dtp:
                    pv = ed_map.get(pk)
                    if pv is not None:
                        ind_dtp[l1_code][pk] += float(pv)
            for l1_code in ind_meta.keys():
                for pk in period_keys_for_dtp:
                    market_dtp_by_p[pk] += float(ind_dtp[l1_code][pk])

        rows = []
        for l1_code in sorted(ind_meta.keys()):
            row = {
                'l1_code': l1_code,
                'group_code': l1_code,
                'l1_name': ind_meta[l1_code].get('l1_name'),
                'company_count': int(ind_meta[l1_code].get('company_count') or 0),
                'total_mv_yi': round(float(ind_total_mv.get(l1_code, 0.0)), 2),
                'free_mv_yi': round(float(ind_free_mv.get(l1_code, 0.0)), 2),
            }
            for idx, p in enumerate(periods):
                row[f's{idx}'] = round((ind_rev.get(l1_code, {}).get(p, 0.0) / 100000000.0), 2)
            if has_q_dtprofit:
                for idx, p in enumerate(periods):
                    cur_raw = float(ind_dtp.get(l1_code, {}).get(p, 0.0))
                    row[f'dtp{idx}'] = round(cur_raw / 100000000.0, 2)
                    pp = _fina_yoy_anchor_end_date(p)
                    prev_raw = float(ind_dtp.get(l1_code, {}).get(pp, 0.0)) if pp else 0.0
                    row[f'dnk{idx}'] = bool(pp and prev_raw < 0 and cur_raw > 0)
            else:
                for idx in range(len(periods)):
                    row[f'dtp{idx}'] = None
            _sw_attach_high_qoq_pair(row, 's', 's_high', 's_qoq', len(periods))
            _sw_attach_high_qoq_pair(row, 'dtp', 'dtp_high', 'dtp_qoq', len(periods))
            for idx, p in enumerate(periods):
                yv = _sw_rev_yoy_yoypct_for_codes(ts_by_l1.get(l1_code, []), rev_nested, p)
                row[f'y{idx}'] = round(yv, 2) if yv is not None else None
            if has_q_dtprofit:
                for idx, p in enumerate(periods):
                    dyv = _sw_dtp_yoy_yoypct_for_codes(ts_by_l1.get(l1_code, []), dtp_nested, p)
                    row[f'dy{idx}'] = round(dyv, 2) if dyv is not None else None
            if has_sw_extend_metrics:
                ts_list = ts_by_l1.get(l1_code, [])
                for idx, p in enumerate(periods):
                    ycfv = _sw_cf_yoy_yoypct_for_codes(
                        ts_list, cf_nested_sw, p
                    )
                    row[f'ycf{idx}'] = (
                        round(ycfv, 2) if ycfv is not None else None
                    )
                    pp_y = _fina_yoy_anchor_end_date(p)
                    if pp_y:
                        cur_cf_y = _sw_cf_industry_single_q_sum_yuan(
                            ts_list, cf_nested_sw, p
                        )
                        prev_cf_y = _sw_cf_industry_single_q_sum_yuan(
                            ts_list, cf_nested_sw, pp_y
                        )
                        row[f'ycfnk{idx}'] = bool(
                            cur_cf_y is not None
                            and prev_cf_y is not None
                            and prev_cf_y < 0
                            and cur_cf_y > 0
                        )
                    else:
                        row[f'ycfnk{idx}'] = False
                for idx, p in enumerate(periods):
                    tot_yuan = 0.0
                    any_ocf = False
                    for tc in ts_list:
                        yq = _fina_yq_from_anchor(p)
                        if not yq:
                            continue
                        sq = _fina_quarter_single_revenue(
                            cf_nested_sw.get(tc, {}), yq[0], yq[1]
                        )
                        if sq is not None:
                            tot_yuan += float(sq)
                            any_ocf = True
                    row[f'qcf{idx}'] = (
                        round(tot_yuan / 100000000.0, 2) if any_ocf else None
                    )
                    gms: list[float] = []
                    roes: list[float] = []
                    for tc in ts_list:
                        tup_m = margin_nested_sw.get(tc, {}).get(p)
                        if not tup_m:
                            continue
                        if tup_m[0] is not None:
                            gms.append(float(tup_m[0]))
                        if tup_m[1] is not None:
                            roes.append(float(tup_m[1]))
                    row[f'qgm{idx}'] = _sw_median_rounded(gms)
                    row[f'qroe{idx}'] = _sw_median_rounded(roes)
                    cs = 0.0
                    fs = 0.0
                    any_cl = False
                    any_fa = False
                    for tc in ts_list:
                        tup_b = bs_cf_nested_sw.get(tc, {}).get(p)
                        if not tup_b:
                            continue
                        if tup_b[0] is not None:
                            cs += float(tup_b[0])
                            any_cl = True
                        if tup_b[1] is not None:
                            fs += float(tup_b[1])
                            any_fa = True
                    row[f'cl{idx}'] = round(cs, 2) if any_cl else None
                    row[f'fa{idx}'] = round(fs, 2) if any_fa else None
                _sw_attach_high_qoq_pair(
                    row, 'qcf', 'qcf_high', 'qcf_qoq', len(periods)
                )
                _sw_attach_high_qoq_pair(
                    row, 'qgm', 'qgm_high', 'qgm_qoq', len(periods)
                )
                _sw_attach_high_qoq_pair(
                    row, 'qroe', 'qroe_high', 'qroe_qoq', len(periods)
                )
            else:
                for idx in range(len(periods)):
                    row[f'ycf{idx}'] = None
                    row[f'ycfnk{idx}'] = None
                    row[f'qcf{idx}'] = None
                    row[f'qgm{idx}'] = None
                    row[f'qroe{idx}'] = None
                    row[f'cl{idx}'] = None
                    row[f'fa{idx}'] = None
                row['qcf_high'] = None
                row['qcf_qoq'] = None
                row['qgm_high'] = None
                row['qgm_qoq'] = None
                row['qroe_high'] = None
                row['qroe_qoq'] = None
            # ===== 实时/行情层逐行写入（l3 全部置 None，与指数页一致） =====
            if level == 'l3':
                for _k in (
                    'sw_pct', 'sw_pct_t1', 'sw_pct_t5', 'sw_pct_t20', 'sw_pct_t60', 'sw_pct_t120',
                    'sw_amt_pctile_t0', 'sw_amt_pctile_t1', 'sw_amt_pctile_t5',
                    'sw_amt_pctile_t20', 'sw_amt_pctile_t60', 'sw_amt_pctile_t120',
                    'sw_h250_t0', 'sw_h850', 'sw_h_his', 'sw_l250', 'sw_l850', 'sw_l_his',
                    'sw_hlc_h250', 'sw_hlc_h850', 'sw_hlc_h_his',
                    'sw_hlc_l250', 'sw_hlc_l850', 'sw_hlc_l_his',
                    'sw_margin_rzye', 'sw_margin_1d', 'sw_margin_3d',
                    'sw_margin_5d', 'sw_margin_20d', 'sw_margin_60d',
                ):
                    row[_k] = None
            else:
                _sw_apply_live_row_market_fields(
                    row,
                    rt_sw_quotes=rt_sw_quotes,
                    range_cache_sw=range_cache_sw,
                    amt_pctile_cache_sw=amt_pctile_cache_sw,
                    sw_t0_count_maps=sw_t0_count_maps,
                    sw_hlc_count_maps=sw_hlc_count_maps,
                )
                margin_fields = sw_margin_by_group.get(l1_code) or {}
                empty_margin_fields = _sw_margin_empty_fields()
                for _mk in empty_margin_fields.keys():
                    row[_mk] = margin_fields.get(_mk)
            rows.append(row)

        summary_row: dict | None = None
        if rows:
            selected_l1_name = l1_option_map.get(parent_l1_code) if parent_l1_code else None
            selected_l2_name = l2_option_map.get(parent_l2_code) if parent_l2_code else None
            summary_row = {
                'l1_code': '',
                'l1_name': selected_l2_name or selected_l1_name or '全部A股',
                'company_count': sum(int(r.get('company_count') or 0) for r in rows),
                'total_mv_yi': round(float(market_total_mv), 2),
                'free_mv_yi': round(float(market_free_mv), 2),
                'is_summary': True,
            }
            # 实时层：区间涨跌幅/成交额比%汇总置 None；新高新低分布按各行合计；融资按全市场合计。
            for _k in (
                'sw_pct', 'sw_pct_t1', 'sw_pct_t5', 'sw_pct_t20', 'sw_pct_t60', 'sw_pct_t120',
                'sw_amt_pctile_t0', 'sw_amt_pctile_t1', 'sw_amt_pctile_t5',
                'sw_amt_pctile_t20', 'sw_amt_pctile_t60', 'sw_amt_pctile_t120',
            ):
                summary_row[_k] = None
            summary_row.update(sw_margin_summary)
            for _jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS:
                summary_row[_jk] = sum(int(r.get(_jk) or 0) for r in rows)
            for _jk, _ in _SW_INDUSTRY_HLC_SPECS:
                summary_row[_jk] = sum(int(r.get(_jk) or 0) for r in rows)
            for idx in range(len(periods)):
                summary_row[f's{idx}'] = round(
                    sum(float(r.get(f's{idx}') or 0) for r in rows), 2
                )
            if has_q_dtprofit:
                for idx in range(len(periods)):
                    summary_row[f'dtp{idx}'] = round(
                        sum(float(r.get(f'dtp{idx}') or 0) for r in rows), 2
                    )
                for idx, p in enumerate(periods):
                    pp = _fina_yoy_anchor_end_date(p)
                    cur_m = float(market_dtp_by_p.get(p, 0.0))
                    prev_m = float(market_dtp_by_p.get(pp, 0.0)) if pp else 0.0
                    summary_row[f'dnk{idx}'] = bool(pp and prev_m < 0 and cur_m > 0)
            else:
                for idx in range(len(periods)):
                    summary_row[f'dtp{idx}'] = None
            for idx, p in enumerate(periods):
                ysum = _sw_rev_yoy_yoypct_for_codes(ts_codes, rev_nested, p)
                summary_row[f'y{idx}'] = round(ysum, 2) if ysum is not None else None
            if has_q_dtprofit:
                for idx, p in enumerate(periods):
                    dys = _sw_dtp_yoy_yoypct_for_codes(ts_codes, dtp_nested, p)
                    summary_row[f'dy{idx}'] = round(dys, 2) if dys is not None else None
            _sw_attach_high_qoq_pair(
                summary_row, 's', 's_high', 's_qoq', len(periods)
            )
            _sw_attach_high_qoq_pair(
                summary_row, 'dtp', 'dtp_high', 'dtp_qoq', len(periods)
            )
            if has_sw_extend_metrics:
                for idx, p in enumerate(periods):
                    ycfv_s = _sw_cf_yoy_yoypct_for_codes(
                        ts_codes, cf_nested_sw, p
                    )
                    summary_row[f'ycf{idx}'] = (
                        round(ycfv_s, 2) if ycfv_s is not None else None
                    )
                    pp_s = _fina_yoy_anchor_end_date(p)
                    if pp_s:
                        cur_cf_s = _sw_cf_industry_single_q_sum_yuan(
                            ts_codes, cf_nested_sw, p
                        )
                        prev_cf_s = _sw_cf_industry_single_q_sum_yuan(
                            ts_codes, cf_nested_sw, pp_s
                        )
                        summary_row[f'ycfnk{idx}'] = bool(
                            cur_cf_s is not None
                            and prev_cf_s is not None
                            and prev_cf_s < 0
                            and cur_cf_s > 0
                        )
                    else:
                        summary_row[f'ycfnk{idx}'] = False
                for idx, p in enumerate(periods):
                    tot_yuan = 0.0
                    any_ocf = False
                    for tc in ts_codes:
                        yq = _fina_yq_from_anchor(p)
                        if not yq:
                            continue
                        sq = _fina_quarter_single_revenue(
                            cf_nested_sw.get(tc, {}), yq[0], yq[1]
                        )
                        if sq is not None:
                            tot_yuan += float(sq)
                            any_ocf = True
                    summary_row[f'qcf{idx}'] = (
                        round(tot_yuan / 100000000.0, 2) if any_ocf else None
                    )
                    gms_m: list[float] = []
                    roes_m: list[float] = []
                    for tc in ts_codes:
                        tup_mm = margin_nested_sw.get(tc, {}).get(p)
                        if not tup_mm:
                            continue
                        if tup_mm[0] is not None:
                            gms_m.append(float(tup_mm[0]))
                        if tup_mm[1] is not None:
                            roes_m.append(float(tup_mm[1]))
                    summary_row[f'qgm{idx}'] = _sw_median_rounded(gms_m)
                    summary_row[f'qroe{idx}'] = _sw_median_rounded(roes_m)
                    cs_m = 0.0
                    fs_m = 0.0
                    any_cl_m = False
                    any_fa_m = False
                    for tc in ts_codes:
                        tup_bm = bs_cf_nested_sw.get(tc, {}).get(p)
                        if not tup_bm:
                            continue
                        if tup_bm[0] is not None:
                            cs_m += float(tup_bm[0])
                            any_cl_m = True
                        if tup_bm[1] is not None:
                            fs_m += float(tup_bm[1])
                            any_fa_m = True
                    summary_row[f'cl{idx}'] = round(cs_m, 2) if any_cl_m else None
                    summary_row[f'fa{idx}'] = round(fs_m, 2) if any_fa_m else None
                _sw_attach_high_qoq_pair(
                    summary_row, 'qcf', 'qcf_high', 'qcf_qoq', len(periods)
                )
                _sw_attach_high_qoq_pair(
                    summary_row, 'qgm', 'qgm_high', 'qgm_qoq', len(periods)
                )
                _sw_attach_high_qoq_pair(
                    summary_row, 'qroe', 'qroe_high', 'qroe_qoq', len(periods)
                )
            elif summary_row is not None:
                for idx in range(len(periods)):
                    summary_row[f'ycf{idx}'] = None
                    summary_row[f'ycfnk{idx}'] = None
                    summary_row[f'qcf{idx}'] = None
                    summary_row[f'qgm{idx}'] = None
                    summary_row[f'qroe{idx}'] = None
                    summary_row[f'cl{idx}'] = None
                    summary_row[f'fa{idx}'] = None
                summary_row['qcf_high'] = None
                summary_row['qcf_qoq'] = None
                summary_row['qgm_high'] = None
                summary_row['qgm_qoq'] = None
                summary_row['qroe_high'] = None
                summary_row['qroe_qoq'] = None

        payload = {
            'success': True,
            'rows': rows,
            'summary_row': summary_row,
            'periods': periods,
            'has_q_dtprofit': bool(has_q_dtprofit),
            'has_rev_yoy': len(periods) > 0,
            'has_dtp_yoy': bool(has_q_dtprofit and len(periods) > 0),
            'level': level,
            'selected_l1_code': parent_l1_code,
            'selected_l1_name': l1_option_map.get(parent_l1_code),
            'selected_l2_code': parent_l2_code,
            'selected_l2_name': l2_option_map.get(parent_l2_code),
            'trade_date': selected_trade_date.isoformat() if selected_trade_date else None,
            'is_historical': selected_trade_date is not None,
            'historical_hint': (
                '新高/低 12 列读 limit_stats_archive_detail 归档；区间涨跌幅等为所选日行情'
                if selected_trade_date is not None
                else None
            ),
            'margin_as_of': margin_as_of.isoformat() if margin_as_of else None,
            'margin_as_of_mode': margin_as_of_mode,
            'l1_options': [{'l1_code': k, 'l1_name': v} for k, v in sorted(l1_option_map.items(), key=lambda x: x[1])],
            'l2_options': [{'l2_code': k, 'l2_name': v} for k, v in sorted(l2_option_map.items(), key=lambda x: x[1])],
            'count': len(rows),
            'has_sw_extend_metrics': bool(
                has_sw_extend_metrics and len(periods) > 0
            ),
            'cache_ttl_sec': SW_INDUSTRY_STATS_CACHE_TTL_API_SEC,
            'pending_fina': bool(isPartial),
        }
        # 首屏 partial 也必须写入缓存，否则每 2s 轮询都冷建 ~0.8s+，且 _full 补齐前永不秒开
        _sw_industry_stats_cache_put(
            cache_key,
            payload,
            ts_to_ind,
            level,
            SW_INDUSTRY_STATS_CACHE_TTL,
            SW_INDUSTRY_STATS_CACHE_MAX,
        )
        # partial 轻量包常带空 amt；无论 maps「热」与否都调度后台重算
        if level != 'l3':
            _sw_schedule_live_market_refresh(
                cache_key,
                ts_to_ind,
                selected_trade_date,
                level,
                limit_pool_key,
            )
        _sw_attach_payload_versions(payload)
        return jsonify(payload)
    except Exception as e:
        logger.error('读取申万行业统计（含单季营收/扣非净利）失败: %s', e)
        return jsonify({'success': False, 'error': str(e), 'rows': []}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if isStatsLeader:
            _swFinishCompute(statsGate, statsGateEv)


def _fetchLatestIndexWeightRaw(indexCodes: list[str]) -> tuple[dict[str, dict[str, float]], bool]:
    """读取各指数最新一期成分权重。进程缓存命中则不打库；DB 失败时 loadOk=False。"""
    nowTs = time.time()
    out: dict[str, dict[str, float]] = {}
    missing: list[str] = []
    for code in indexCodes:
        hit = smallTtlCacheGet(_indexWeightLatestCache, code, nowTs)
        if hit is not None:
            out[code] = hit
        else:
            missing.append(code)
    if not missing:
        return out, True

    conn = None
    rows: list = []
    try:
        # 必须用 WIND_BULK：默认 DB_CONFIG.read_timeout=8，启动时与 arturn 抢库会 2013
        conn = pymysql.connect(**DB_CONFIG_WIND_BULK)
        placeholders = ', '.join(['%s'] * len(missing))
        sql = (
            "SELECT iw.index_code, iw.con_code, iw.weight "
            "FROM stock_wind.index_weight iw "
            "INNER JOIN ("
            "  SELECT index_code, MAX(trade_date) AS md "
            "  FROM stock_wind.index_weight "
            f"  WHERE index_code IN ({placeholders}) "
            "  GROUP BY index_code"
            ") x ON x.index_code = iw.index_code AND x.md = iw.trade_date "
            f"WHERE iw.index_code IN ({placeholders})"
        )
        t0 = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute(sql, tuple(missing + missing))
            rows = cur.fetchall() or []
        elapsed = time.perf_counter() - t0
        if elapsed >= 1.0:
            logger.info(
                'index_weight 最新成分查询 codes=%s rows=%s elapsed=%.2fs',
                len(missing), len(rows), elapsed,
            )
    except Exception as e:
        logger.warning("读取 index_weight 成分失败: %s", e)
        return out, False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    raw: dict[str, dict[str, float]] = defaultdict(dict)
    for row in rows:
        idxCode = str(row[0] or '').strip().upper()
        tsCode = str(row[1] or '').strip().upper()
        if not idxCode or not tsCode:
            continue
        try:
            w = float(row[2]) if row[2] is not None else 0.0
        except (TypeError, ValueError):
            w = 0.0
        if w <= 0:
            continue
        raw[idxCode][tsCode] = raw[idxCode].get(tsCode, 0.0) + w
    for code in missing:
        item = dict(raw.get(code) or {})
        smallTtlCachePut(
            _indexWeightLatestCache, code, item,
            _INDEX_WEIGHT_LATEST_TTL_SEC, _INDEX_WEIGHT_LATEST_CACHE_MAX,
        )
        out[code] = item
    return out, True


def _load_index_constituent_weights(
    index_code: str,
) -> tuple[dict[str, float], list[str], dict[str, dict[str, float]], bool]:
    """读取宽基指数成分权重（归一化到 100%）。

    Returns:
        (aggregate_pct_by_ts, selected_index_codes, per_index_pct_by_ts, loadOk)
    """
    all_codes = [code for code, _ in INDEX_MONITOR_CODES]
    selected_codes = [index_code] if index_code and index_code in all_codes else list(all_codes)
    if not selected_codes:
        return {}, [], {}, True

    per_index_raw, loadOk = _fetchLatestIndexWeightRaw(selected_codes)

    # 先在指数内归一，再跨指数等权平均，保证“全部宽基指数”总权重约 100%
    per_index_pct: dict[str, dict[str, float]] = {}
    for idx_code in selected_codes:
        item = per_index_raw.get(idx_code, {})
        total = sum(item.values())
        if total <= 0:
            continue
        per_index_pct[idx_code] = {ts_code: (raw_w / total) * 100.0 for ts_code, raw_w in item.items()}

    aggregate: dict[str, float] = defaultdict(float)
    valid_count = 0
    for idx_code in selected_codes:
        item = per_index_pct.get(idx_code, {})
        if not item:
            continue
        valid_count += 1
        for ts_code, pct_w in item.items():
            aggregate[ts_code] += pct_w

    if valid_count <= 0:
        return {}, selected_codes, per_index_pct, loadOk
    for ts_code in list(aggregate.keys()):
        aggregate[ts_code] = aggregate[ts_code] / valid_count
    return dict(aggregate), selected_codes, per_index_pct, loadOk


def apiIndexIndustryStats():
    """宽基指数行业统计：指数成分来自 index_weight，分组与13季口径对齐 sw_industry_stats。"""
    level = str(request.args.get('level') or 'l1').strip().lower()
    parent_l1_code = str(request.args.get('parent_l1_code') or '').strip()
    parent_l2_code = str(request.args.get('parent_l2_code') or '').strip()
    index_code = str(request.args.get('index_code') or '').strip().upper()
    if level not in ('l1', 'l2', 'l3'):
        level = 'l1'
    if parent_l2_code:
        level = 'l3'
    elif parent_l1_code:
        level = 'l2'
    cache_key = f'{index_code}|{level}|{parent_l1_code}|{parent_l2_code}|x14'
    now_ts = time.time()
    hit = smallTtlCacheGet(_index_industry_stats_cache, cache_key, now_ts)
    if hit is not None:
        return jsonify(hit)

    ts_weight_pct, selected_codes, per_index_pct, loadOk = _load_index_constituent_weights(index_code)
    is_index_overview = (not index_code) and (not parent_l1_code) and (not parent_l2_code)
    ts_universe = set(ts_weight_pct.keys())
    index_options = [{'index_code': code, 'index_name': name} for code, name in INDEX_MONITOR_CODES]
    if not ts_universe:
        payload = {
            'success': bool(loadOk), 'rows': [], 'summary_row': None, 'periods': [],
            'has_q_dtprofit': False, 'has_sw_extend_metrics': False,
            'level': level, 'selected_l1_code': parent_l1_code, 'selected_l2_code': parent_l2_code,
            'l1_options': [], 'l2_options': [], 'count': 0, 'cache_ttl_sec': SW_INDUSTRY_STATS_CACHE_TTL_API_SEC,
            'index_code': index_code, 'index_options': index_options, 'selected_index_codes': selected_codes,
        }
        # 读库失败不得写入 inf TTL，否则预热空结果会锁死到进程重启
        if loadOk:
            smallTtlCachePut(_index_industry_stats_cache, cache_key, payload, SW_INDUSTRY_STATS_CACHE_TTL, SW_INDUSTRY_STATS_CACHE_MAX)
        else:
            logger.warning(
                '宽基行业统计跳过空缓存：index_weight 读取失败 index_code=%s',
                index_code or '(all)',
            )
        _sw_attach_payload_versions(payload)
        return jsonify(payload)

    conn = None
    try:
        has_sw_extend_metrics = False
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
        sql_sw = (
            "SELECT z.ts_code, z.l1_code, z.l1_name, z.l2_code, z.l2_name, z.l3_code, z.l3_name "
            "FROM ( "
            "  SELECT m.ts_code, m.l1_code, m.l1_name, m.l2_code, m.l2_name, m.l3_code, m.l3_name, "
            "    ROW_NUMBER() OVER (PARTITION BY m.ts_code ORDER BY m.in_date DESC, m.l1_code ASC, m.l2_code ASC, m.l2_name ASC) AS rn "
            "  FROM stock_tushare.index_member_all_sw m "
            "  WHERE (m.is_new = 'Y' OR m.is_new IS NULL) "
            "    AND m.l1_code IS NOT NULL AND m.l1_code <> '' "
            "    AND m.l1_name IS NOT NULL AND m.l1_name <> '' "
            "    AND m.l2_code IS NOT NULL AND m.l2_code <> '' "
            "    AND m.l2_name IS NOT NULL AND m.l2_name <> '' "
            ") z WHERE z.rn = 1"
        )
        with conn.cursor() as cur:
            cur.execute(sql_sw)
            sw_rows = cur.fetchall() or []

        ts_to_ind: dict[str, tuple[str, str]] = {}
        ind_meta: dict[str, dict] = {}
        l1_option_map: dict[str, str] = {}
        l2_option_map: dict[str, str] = {}
        if is_index_overview:
            for idx_code, idx_name in INDEX_MONITOR_CODES:
                ts_map = per_index_pct.get(idx_code, {})
                if not ts_map:
                    continue
                for ts_code, pct_w in ts_map.items():
                    ts_code_u = str(ts_code or '').strip().upper()
                    if not ts_code_u:
                        continue
                    ts_to_ind[ts_code_u] = (idx_code, idx_name)
                    if idx_code not in ind_meta:
                        ind_meta[idx_code] = {'l1_name': idx_name, 'company_count': 0, 'weight_pct': 0.0}
                    ind_meta[idx_code]['company_count'] += 1
                    ind_meta[idx_code]['weight_pct'] += float(pct_w)
        else:
            for row in sw_rows:
                ts_code = str(row[0] or '').strip().upper()
                if ts_code not in ts_universe:
                    continue
                l1_code = str(row[1] or '').strip()
                l1_name = str(row[2] or '').strip()
                l2_code = str(row[3] or '').strip()
                l2_name = str(row[4] or '').strip()
                l3_code = str(row[5] or '').strip()
                l3_name = str(row[6] or '').strip()
                if not ts_code or not l1_code or not l2_code:
                    continue
                l1_option_map[l1_code] = l1_name
                if not parent_l1_code or l1_code == parent_l1_code:
                    l2_option_map[l2_code] = l2_name
                if parent_l1_code and l1_code != parent_l1_code:
                    continue
                if parent_l2_code and l2_code != parent_l2_code:
                    continue
                if level == 'l2':
                    group_code, group_name = l2_code, l2_name
                elif level == 'l3':
                    if not l3_code or not l3_name:
                        continue
                    group_code, group_name = l3_code, l3_name
                else:
                    group_code, group_name = l1_code, l1_name
                ts_to_ind[ts_code] = (group_code, group_name)
                if group_code not in ind_meta:
                    ind_meta[group_code] = {'l1_name': group_name, 'company_count': 0, 'weight_pct': 0.0}
                ind_meta[group_code]['company_count'] += 1
                ind_meta[group_code]['weight_pct'] += float(ts_weight_pct.get(ts_code, 0.0))

        ts_by_l1: dict[str, list[str]] = defaultdict(list)
        if is_index_overview:
            # 宽基总览：允许同一成分股同时属于多个指数，不能用 ts->单组映射覆盖。
            for idx_code, _idx_name in INDEX_MONITOR_CODES:
                ts_list = sorted((per_index_pct.get(idx_code) or {}).keys())
                if ts_list:
                    ts_by_l1[idx_code] = ts_list
            ts_codes = sorted({tc for arr in ts_by_l1.values() for tc in arr})
        else:
            ts_codes = sorted(ts_to_ind.keys())
            for _ts, (_lc, _) in ts_to_ind.items():
                ts_by_l1[_lc].append(_ts)

        if not ts_codes:
            payload = {
                'success': True, 'rows': [], 'summary_row': None, 'periods': [],
                'has_q_dtprofit': False, 'has_sw_extend_metrics': False, 'level': level,
                'selected_l1_code': parent_l1_code, 'selected_l2_code': parent_l2_code,
                'l1_options': [{'l1_code': k, 'l1_name': v} for k, v in sorted(l1_option_map.items(), key=lambda x: x[1])],
                'l2_options': [{'l2_code': k, 'l2_name': v} for k, v in sorted(l2_option_map.items(), key=lambda x: x[1])],
                'count': 0, 'cache_ttl_sec': SW_INDUSTRY_STATS_CACHE_TTL_API_SEC,
                'index_code': index_code, 'index_options': index_options, 'selected_index_codes': selected_codes,
            }
            smallTtlCachePut(_index_industry_stats_cache, cache_key, payload, SW_INDUSTRY_STATS_CACHE_TTL, SW_INDUSTRY_STATS_CACHE_MAX)
            _sw_attach_payload_versions(payload)
            return jsonify(payload)

        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT end_date FROM stock_tushare.income WHERE end_date IS NOT NULL ORDER BY end_date DESC LIMIT 13")
            raw_periods = cur.fetchall() or []
        periods = [_normalize_end_date_yyyymmdd(r[0]) for r in raw_periods if r and r[0] is not None]
        periods = [p for p in periods if p]

        has_inc_rt = _tushare_column_exists(conn, 'income', 'report_type')
        date_keys = _fina_income_date_keys_with_yoy_for_periods(periods)
        rev_nested = _fina_batch_income_total_revenue(conn, ts_codes, date_keys, has_inc_rt)

        has_q_dtprofit = _tushare_column_exists(conn, 'fina_indicator', 'q_dtprofit')
        has_fi_rt = _tushare_column_exists(conn, 'fina_indicator', 'report_type')
        dtp_nested: dict[str, dict[str, float]] = {}
        if has_q_dtprofit:
            dtp_period_keys = _fina_periods_union_yoy_for_q_dtprofit(periods)
            dtp_nested = _fina_batch_fina_indicator_q_dtprofit(conn, ts_codes, dtp_period_keys, has_fi_rt)

        ind_rev: dict[str, dict[str, float]] = defaultdict(dict)
        for l1_code in ind_meta.keys():
            for p in periods:
                ind_rev[l1_code][p] = 0.0
        if is_index_overview:
            for l1_code in ind_meta.keys():
                ts_list = ts_by_l1.get(l1_code, [])
                for ts_code in ts_list:
                    rev_map = rev_nested.get(ts_code, {})
                    if not rev_map:
                        continue
                    for p in periods:
                        yq = _fina_yq_from_anchor(p)
                        if not yq:
                            continue
                        sq = _fina_quarter_single_revenue(rev_map, yq[0], yq[1])
                        if sq is not None:
                            ind_rev[l1_code][p] += float(sq)
        else:
            for ts_code, rev_map in rev_nested.items():
                ind_info = ts_to_ind.get(ts_code)
                if ind_info is None:
                    continue
                l1_code = ind_info[0]
                for p in periods:
                    yq = _fina_yq_from_anchor(p)
                    if not yq:
                        continue
                    sq = _fina_quarter_single_revenue(rev_map, yq[0], yq[1])
                    if sq is not None:
                        ind_rev[l1_code][p] += float(sq)

        # 复用 sw 现有市值口径
        ind_total_mv: dict[str, float] = defaultdict(float)
        ind_free_mv: dict[str, float] = defaultdict(float)
        market_total_mv = 0.0
        market_free_mv = 0.0
        has_tmv = _tushare_column_exists(conn, 'stk_factor_pro', 'total_mv')
        has_fs = _tushare_column_exists(conn, 'stk_factor_pro', 'free_share')
        if has_tmv or has_fs:
            max_d = None
            with conn.cursor() as cur:
                cur.execute('SELECT MAX(trade_date) FROM stock_tushare.stk_factor_pro')
                mx = cur.fetchone()
                if mx and mx[0] is not None:
                    max_d = mx[0]
            sfp_map: dict[str, tuple[float | None, float | None]] = {}
            if max_d is not None:
                sel_cols = ['ts_code']
                if has_tmv:
                    sel_cols.append('total_mv')
                if has_fs:
                    sel_cols.append('free_share')
                col_sql = ', '.join(sel_cols)
                chunk = 480
                for i in range(0, len(ts_codes), chunk):
                    part = ts_codes[i:i + chunk]
                    if not part:
                        continue
                    ph = ', '.join(['%s'] * len(part))
                    sql = f'SELECT {col_sql} FROM stock_tushare.stk_factor_pro WHERE trade_date = %s AND ts_code IN ({ph})'
                    with conn.cursor() as cur:
                        cur.execute(sql, (max_d, *part))
                        for raw in cur.fetchall() or []:
                            if not raw or raw[0] is None:
                                continue
                            tc = str(raw[0]).strip()
                            if not tc:
                                continue
                            idx = 1
                            tmv_num = None
                            fs_num = None
                            if has_tmv:
                                try:
                                    tmv_num = float(raw[idx]) / 10000.0 if raw[idx] is not None else None
                                except (TypeError, ValueError):
                                    tmv_num = None
                                idx += 1
                            if has_fs:
                                try:
                                    fs_num = float(raw[idx]) if raw[idx] is not None else None
                                except (TypeError, ValueError):
                                    fs_num = None
                            sfp_map[tc] = (tmv_num, fs_num)

            close_map: dict[str, float] = {}
            if has_fs:
                try:
                    conn_w = pymysql.connect(**DB_CONFIG)
                    try:
                        chunk = 480
                        for i in range(0, len(ts_codes), chunk):
                            part = ts_codes[i:i + chunk]
                            if not part:
                                continue
                            ph = ', '.join(['%s'] * len(part))
                            sql = (
                                'SELECT ts_code, close FROM ('
                                '  SELECT ts_code, close, ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_time DESC) AS rn '
                                f'  FROM rt_k WHERE ts_code IN ({ph})'
                                ') x WHERE rn = 1'
                            )
                            with conn_w.cursor() as cur:
                                cur.execute(sql, tuple(part))
                                for r2 in cur.fetchall() or []:
                                    if not r2 or r2[0] is None or r2[1] is None:
                                        continue
                                    try:
                                        close_map[str(r2[0]).strip()] = float(r2[1])
                                    except (TypeError, ValueError):
                                        continue
                    finally:
                        conn_w.close()
                except Exception as e:
                    logger.warning('index_industry_stats 查询 rt_k close 失败: %s', e)

            if is_index_overview:
                market_tmv_seen: set[str] = set()
                market_fmv_seen: set[str] = set()
                for l1_code in ind_meta.keys():
                    for tc in ts_by_l1.get(l1_code, []):
                        pair = sfp_map.get(tc)
                        if not pair:
                            continue
                        tmv_yi, fs_wangu = pair
                        if tmv_yi is not None:
                            ind_total_mv[l1_code] += float(tmv_yi)
                            if tc not in market_tmv_seen:
                                market_total_mv += float(tmv_yi)
                                market_tmv_seen.add(tc)
                        if fs_wangu is not None and close_map.get(tc) is not None:
                            free_yi = float(fs_wangu) * float(close_map[tc]) / 10000.0
                            ind_free_mv[l1_code] += free_yi
                            if tc not in market_fmv_seen:
                                market_free_mv += free_yi
                                market_fmv_seen.add(tc)
            else:
                for tc in ts_codes:
                    ind_info = ts_to_ind.get(tc)
                    if ind_info is None:
                        continue
                    l1_code = ind_info[0]
                    pair = sfp_map.get(tc)
                    if not pair:
                        continue
                    tmv_yi, fs_wangu = pair
                    if tmv_yi is not None:
                        ind_total_mv[l1_code] += float(tmv_yi)
                        market_total_mv += float(tmv_yi)
                    if fs_wangu is not None and close_map.get(tc) is not None:
                        free_yi = float(fs_wangu) * float(close_map[tc]) / 10000.0
                        ind_free_mv[l1_code] += free_yi
                        market_free_mv += free_yi

        # 扣非/扩展口径复用 sw
        period_keys_for_dtp: set[str] = set()
        ind_dtp: dict[str, dict[str, float]] = defaultdict(dict)
        market_dtp_by_p: dict[str, float] = defaultdict(float)
        if has_q_dtprofit:
            period_keys_for_dtp = set(periods)
            for _p in periods:
                _pp = _fina_yoy_anchor_end_date(_p)
                if _pp:
                    period_keys_for_dtp.add(_pp)
            for l1_code in ind_meta.keys():
                for pk in period_keys_for_dtp:
                    ind_dtp[l1_code][pk] = 0.0
            if is_index_overview:
                for l1_code in ind_meta.keys():
                    for ts_code in ts_by_l1.get(l1_code, []):
                        ed_map = dtp_nested.get(ts_code, {})
                        if not ed_map:
                            continue
                        for pk in period_keys_for_dtp:
                            pv = ed_map.get(pk)
                            if pv is not None:
                                ind_dtp[l1_code][pk] += float(pv)
            else:
                for ts_code, ed_map in dtp_nested.items():
                    ind_info = ts_to_ind.get(ts_code)
                    if ind_info is None:
                        continue
                    l1_code = ind_info[0]
                    for pk in period_keys_for_dtp:
                        pv = ed_map.get(pk)
                        if pv is not None:
                            ind_dtp[l1_code][pk] += float(pv)
            for l1_code in ind_meta.keys():
                for pk in period_keys_for_dtp:
                    market_dtp_by_p[pk] += float(ind_dtp[l1_code][pk])

        cf_nested_sw: dict[str, dict[str, float]] = {}
        margin_nested_sw: dict[str, dict[str, tuple[float | None, float | None]]] = {}
        bs_cf_nested_sw: dict[str, dict[str, tuple[float | None, float | None]]] = {}
        if periods and ts_codes:
            has_n_cf_sw = _tushare_column_exists(conn, 'cashflow', 'n_cashflow_act')
            has_cf_rt_sw = _tushare_column_exists(conn, 'cashflow', 'report_type')
            has_q_gs_sw = _tushare_column_exists(conn, 'fina_indicator', 'q_gsprofit_margin')
            has_q_dt_roe_sw = _tushare_column_exists(conn, 'fina_indicator', 'q_dt_roe')
            has_bs_rt_sw = _tushare_column_exists(conn, 'balancesheet', 'report_type')
            has_adv_sw = _tushare_column_exists(conn, 'balancesheet', 'adv_receipts')
            has_cl_sw = _tushare_column_exists(conn, 'balancesheet', 'contract_liab')
            has_fa_sw = _tushare_column_exists(conn, 'balancesheet', 'fix_assets_total')
            cf_keys_sq = _fina_income_date_keys_with_yoy_for_periods(periods)
            if has_n_cf_sw:
                try:
                    cf_nested_sw = _fina_batch_cashflow_n_cashflow_act(conn, ts_codes, cf_keys_sq, has_cf_rt_sw)
                    has_sw_extend_metrics = True
                except Exception as e:
                    logger.warning('index_industry_stats 经营现金流批量读取失败: %s', e)
            if has_q_gs_sw or has_q_dt_roe_sw:
                try:
                    margin_nested_sw = _fina_batch_fina_indicator_q_gs_margin_q_dt_roe(conn, ts_codes, periods, has_fi_rt, has_q_gs_sw, has_q_dt_roe_sw)
                    has_sw_extend_metrics = True
                except Exception as e:
                    logger.warning('index_industry_stats 毛利率/ROE(扣非)批量读取失败: %s', e)
            if has_cl_sw or has_fa_sw:
                try:
                    bs_cf_nested_sw = _fina_batch_balancesheet_contract_fix_yi(conn, ts_codes, periods, has_bs_rt=has_bs_rt_sw, has_adv=has_adv_sw, has_contract=has_cl_sw, has_fix=has_fa_sw)
                    has_sw_extend_metrics = True
                except Exception as e:
                    logger.warning('index_industry_stats 合同负债/固定资产批量读取失败: %s', e)

        range_cache_sw: dict[str, dict] = {}
        rt_sw_quotes: dict[str, dict] = {}
        amt_pctile_cache_sw: dict[str, dict[str, float | None]] = {}
        if (not is_index_overview) and level != 'l3':
            idx_sw_list: list[str] = []
            seen_sw_ts: set[str] = set()
            for gc in ind_meta.keys():
                tsu = _normalize_sw_industry_index_ts_code(str(gc))
                if tsu and tsu not in seen_sw_ts:
                    seen_sw_ts.add(tsu)
                    idx_sw_list.append(tsu)
            idx_sw_list.sort()
            _bts_amt = str(SW_STATS_SUMMARY_ALL_A_SW_TS or "").strip().upper()
            if _bts_amt and _bts_amt not in seen_sw_ts:
                seen_sw_ts.add(_bts_amt)
                idx_sw_list.append(_bts_amt)
            idx_sw_list.sort()
            rt_sw_quotes = _fetch_rt_sw_k_quotes(idx_sw_list, conn)
            for tsu in idx_sw_list:
                range_cache_sw[tsu] = _read_index_sw_daily_interval_range_pct(
                    conn, tsu, rt_sw_quotes.get(tsu)
                )
            bench_ts_amt = SW_STATS_SUMMARY_ALL_A_SW_TS
            min_amt_d = date.today() - timedelta(days=366 * 7 + 30)
            bench_amt_map = _load_index_sw_daily_amount_by_date(
                conn, bench_ts_amt, min_amt_d
            )
            row_amt_maps: dict[str, dict[date, float]] = {}
            for tsu in idx_sw_list:
                row_amt_maps[tsu] = _load_index_sw_daily_amount_by_date(conn, tsu, min_amt_d)
            _bench_rt_q_sw = rt_sw_quotes.get(_bts_amt)
            for tsu in idx_sw_list:
                amt_pctile_cache_sw[tsu] = _read_sw_amount_ratio_percentiles(
                    conn,
                    tsu,
                    rt_sw_quotes.get(tsu),
                    bench_ts_amt,
                    bench_amt_map,
                    row_amt_maps[tsu],
                    bench_rt_quote=_bench_rt_q_sw,
                )
        elif is_index_overview and level != 'l3':
            try:
                rt_sw_quotes, range_cache_sw, amt_pctile_cache_sw = (
                    _index_collect_overview_live(conn, ind_meta)
                )
            except Exception as e:
                logger.warning('index_industry_stats 总览实时层失败: %s', e)

        sw_t0_count_maps: dict[str, dict[str, int]] = {
            jk: {} for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS
        }
        sw_hlc_count_maps: dict[str, dict[str, int]] = {
            jk: {} for jk, _ in _SW_INDUSTRY_HLC_SPECS
        }
        sw_t0_summary: dict[str, int] = {jk: 0 for jk, _ in _SW_INDUSTRY_LIMITSTAT_T0_SPECS}
        sw_hlc_summary: dict[str, int] = {jk: 0 for jk, _ in _SW_INDUSTRY_HLC_SPECS}
        sw_margin_by_group: dict[str, dict[str, float | None]] = {}
        sw_margin_summary = _sw_margin_empty_fields()
        margin_as_of = None
        margin_as_of_mode = None
        if level != 'l3':
            try:
                sw_t0_count_maps, sw_hlc_count_maps, sw_t0_summary, sw_hlc_summary = (
                    _index_collect_limit_hlc_maps(
                        ts_codes, ts_to_ind, ts_by_l1, is_index_overview, level
                    )
                )
            except Exception as e:
                logger.warning('index_industry_stats 新高/低分布失败: %s', e)
            try:
                if is_index_overview:
                    sw_margin_by_group, sw_margin_summary, margin_as_of, margin_as_of_mode = (
                        _sw_fetch_margin_metrics_by_group_lists(dict(ts_by_l1), None)
                    )
                else:
                    sw_margin_by_group, sw_margin_summary, margin_as_of, margin_as_of_mode = (
                        _sw_fetch_margin_metrics_by_group(ts_to_ind, None)
                    )
            except Exception as e:
                logger.warning('index_industry_stats 融资余额聚合失败: %s', e)

        rows = []
        for l1_code in sorted(ind_meta.keys()):
            row = {
                'l1_code': l1_code,
                'l1_name': ind_meta[l1_code].get('l1_name'),
                'company_count': int(ind_meta[l1_code].get('company_count') or 0),
                'weight_pct': round(float(ind_meta[l1_code].get('weight_pct') or 0.0), 2),
                'total_mv_yi': round(float(ind_total_mv.get(l1_code, 0.0)), 2),
                'free_mv_yi': round(float(ind_free_mv.get(l1_code, 0.0)), 2),
            }
            for idx, p in enumerate(periods):
                row[f's{idx}'] = round((ind_rev.get(l1_code, {}).get(p, 0.0) / 100000000.0), 2)
            if has_q_dtprofit:
                for idx, p in enumerate(periods):
                    cur_raw = float(ind_dtp.get(l1_code, {}).get(p, 0.0))
                    row[f'dtp{idx}'] = round(cur_raw / 100000000.0, 2)
                    pp = _fina_yoy_anchor_end_date(p)
                    prev_raw = float(ind_dtp.get(l1_code, {}).get(pp, 0.0)) if pp else 0.0
                    row[f'dnk{idx}'] = bool(pp and prev_raw < 0 and cur_raw > 0)
            else:
                for idx in range(len(periods)):
                    row[f'dtp{idx}'] = None
            _sw_attach_high_qoq_pair(row, 's', 's_high', 's_qoq', len(periods))
            _sw_attach_high_qoq_pair(row, 'dtp', 'dtp_high', 'dtp_qoq', len(periods))
            for idx, p in enumerate(periods):
                yv = _sw_rev_yoy_yoypct_for_codes(ts_by_l1.get(l1_code, []), rev_nested, p)
                row[f'y{idx}'] = round(yv, 2) if yv is not None else None
            if has_q_dtprofit:
                for idx, p in enumerate(periods):
                    dyv = _sw_dtp_yoy_yoypct_for_codes(ts_by_l1.get(l1_code, []), dtp_nested, p)
                    row[f'dy{idx}'] = round(dyv, 2) if dyv is not None else None
            if has_sw_extend_metrics:
                ts_list = ts_by_l1.get(l1_code, [])
                for idx, p in enumerate(periods):
                    ycfv = _sw_cf_yoy_yoypct_for_codes(ts_list, cf_nested_sw, p)
                    row[f'ycf{idx}'] = round(ycfv, 2) if ycfv is not None else None
                    pp_y = _fina_yoy_anchor_end_date(p)
                    if pp_y:
                        cur_cf_y = _sw_cf_industry_single_q_sum_yuan(ts_list, cf_nested_sw, p)
                        prev_cf_y = _sw_cf_industry_single_q_sum_yuan(ts_list, cf_nested_sw, pp_y)
                        row[f'ycfnk{idx}'] = bool(cur_cf_y is not None and prev_cf_y is not None and prev_cf_y < 0 and cur_cf_y > 0)
                    else:
                        row[f'ycfnk{idx}'] = False
                for idx, p in enumerate(periods):
                    tot_yuan = 0.0
                    any_ocf = False
                    for tc in ts_list:
                        yq = _fina_yq_from_anchor(p)
                        if not yq:
                            continue
                        sq = _fina_quarter_single_revenue(cf_nested_sw.get(tc, {}), yq[0], yq[1])
                        if sq is not None:
                            tot_yuan += float(sq)
                            any_ocf = True
                    row[f'qcf{idx}'] = round(tot_yuan / 100000000.0, 2) if any_ocf else None
                    gms: list[float] = []
                    roes: list[float] = []
                    for tc in ts_list:
                        tup_m = margin_nested_sw.get(tc, {}).get(p)
                        if not tup_m:
                            continue
                        if tup_m[0] is not None:
                            gms.append(float(tup_m[0]))
                        if tup_m[1] is not None:
                            roes.append(float(tup_m[1]))
                    row[f'qgm{idx}'] = _sw_median_rounded(gms)
                    row[f'qroe{idx}'] = _sw_median_rounded(roes)
                    cs = 0.0
                    fs = 0.0
                    any_cl = False
                    any_fa = False
                    for tc in ts_list:
                        tup_b = bs_cf_nested_sw.get(tc, {}).get(p)
                        if not tup_b:
                            continue
                        if tup_b[0] is not None:
                            cs += float(tup_b[0])
                            any_cl = True
                        if tup_b[1] is not None:
                            fs += float(tup_b[1])
                            any_fa = True
                    row[f'cl{idx}'] = round(cs, 2) if any_cl else None
                    row[f'fa{idx}'] = round(fs, 2) if any_fa else None
                _sw_attach_high_qoq_pair(row, 'qcf', 'qcf_high', 'qcf_qoq', len(periods))
                _sw_attach_high_qoq_pair(row, 'qgm', 'qgm_high', 'qgm_qoq', len(periods))
                _sw_attach_high_qoq_pair(row, 'qroe', 'qroe_high', 'qroe_qoq', len(periods))
            else:
                for idx in range(len(periods)):
                    row[f'ycf{idx}'] = None
                    row[f'ycfnk{idx}'] = None
                    row[f'qcf{idx}'] = None
                    row[f'qgm{idx}'] = None
                    row[f'qroe{idx}'] = None
                    row[f'cl{idx}'] = None
                    row[f'fa{idx}'] = None
                row['qcf_high'] = None
                row['qcf_qoq'] = None
                row['qgm_high'] = None
                row['qgm_qoq'] = None
                row['qroe_high'] = None
                row['qroe_qoq'] = None
            _index_apply_pct_amt_to_row(
                row,
                l1_code,
                isOverview=is_index_overview,
                level=level,
                rtQuotes=rt_sw_quotes,
                rangeCache=range_cache_sw,
                amtCache=amt_pctile_cache_sw,
            )
            _index_apply_hl_margin_to_row(
                row,
                l1_code,
                level=level,
                t0Maps=sw_t0_count_maps,
                hlcMaps=sw_hlc_count_maps,
                marginByGroup=sw_margin_by_group,
            )
            rows.append(row)

        summary_row: dict | None = None
        if rows:
            selected_l1_name = l1_option_map.get(parent_l1_code) if parent_l1_code else None
            selected_l2_name = l2_option_map.get(parent_l2_code) if parent_l2_code else None
            summary_row = {
                'l1_code': '',
                'l1_name': selected_l2_name or selected_l1_name or ('全部宽基指数' if not index_code else dict(INDEX_MONITOR_CODES).get(index_code, index_code)),
                'company_count': sum(int(r.get('company_count') or 0) for r in rows),
                'weight_pct': 100.0,
                'total_mv_yi': round(float(market_total_mv), 2),
                'free_mv_yi': round(float(market_free_mv), 2),
                'is_summary': True,
            }
            for idx in range(len(periods)):
                summary_row[f's{idx}'] = round(sum(float(r.get(f's{idx}') or 0) for r in rows), 2)
            if has_q_dtprofit:
                for idx in range(len(periods)):
                    summary_row[f'dtp{idx}'] = round(sum(float(r.get(f'dtp{idx}') or 0) for r in rows), 2)
                for idx, p in enumerate(periods):
                    pp = _fina_yoy_anchor_end_date(p)
                    cur_m = float(market_dtp_by_p.get(p, 0.0))
                    prev_m = float(market_dtp_by_p.get(pp, 0.0)) if pp else 0.0
                    summary_row[f'dnk{idx}'] = bool(pp and prev_m < 0 and cur_m > 0)
            else:
                for idx in range(len(periods)):
                    summary_row[f'dtp{idx}'] = None
            for idx, p in enumerate(periods):
                ysum = _sw_rev_yoy_yoypct_for_codes(ts_codes, rev_nested, p)
                summary_row[f'y{idx}'] = round(ysum, 2) if ysum is not None else None
            if has_q_dtprofit:
                for idx, p in enumerate(periods):
                    dys = _sw_dtp_yoy_yoypct_for_codes(ts_codes, dtp_nested, p)
                    summary_row[f'dy{idx}'] = round(dys, 2) if dys is not None else None
            _sw_attach_high_qoq_pair(summary_row, 's', 's_high', 's_qoq', len(periods))
            _sw_attach_high_qoq_pair(summary_row, 'dtp', 'dtp_high', 'dtp_qoq', len(periods))
            if has_sw_extend_metrics:
                for idx, p in enumerate(periods):
                    ycfv_s = _sw_cf_yoy_yoypct_for_codes(ts_codes, cf_nested_sw, p)
                    summary_row[f'ycf{idx}'] = round(ycfv_s, 2) if ycfv_s is not None else None
                    pp_s = _fina_yoy_anchor_end_date(p)
                    if pp_s:
                        cur_cf_s = _sw_cf_industry_single_q_sum_yuan(ts_codes, cf_nested_sw, p)
                        prev_cf_s = _sw_cf_industry_single_q_sum_yuan(ts_codes, cf_nested_sw, pp_s)
                        summary_row[f'ycfnk{idx}'] = bool(cur_cf_s is not None and prev_cf_s is not None and prev_cf_s < 0 and cur_cf_s > 0)
                    else:
                        summary_row[f'ycfnk{idx}'] = False
                for idx, p in enumerate(periods):
                    tot_yuan = 0.0
                    any_ocf = False
                    for tc in ts_codes:
                        yq = _fina_yq_from_anchor(p)
                        if not yq:
                            continue
                        sq = _fina_quarter_single_revenue(cf_nested_sw.get(tc, {}), yq[0], yq[1])
                        if sq is not None:
                            tot_yuan += float(sq)
                            any_ocf = True
                    summary_row[f'qcf{idx}'] = round(tot_yuan / 100000000.0, 2) if any_ocf else None
                    gms_m: list[float] = []
                    roes_m: list[float] = []
                    for tc in ts_codes:
                        tup_mm = margin_nested_sw.get(tc, {}).get(p)
                        if not tup_mm:
                            continue
                        if tup_mm[0] is not None:
                            gms_m.append(float(tup_mm[0]))
                        if tup_mm[1] is not None:
                            roes_m.append(float(tup_mm[1]))
                    summary_row[f'qgm{idx}'] = _sw_median_rounded(gms_m)
                    summary_row[f'qroe{idx}'] = _sw_median_rounded(roes_m)
                    cs_m = 0.0
                    fs_m = 0.0
                    any_cl_m = False
                    any_fa_m = False
                    for tc in ts_codes:
                        tup_bm = bs_cf_nested_sw.get(tc, {}).get(p)
                        if not tup_bm:
                            continue
                        if tup_bm[0] is not None:
                            cs_m += float(tup_bm[0])
                            any_cl_m = True
                        if tup_bm[1] is not None:
                            fs_m += float(tup_bm[1])
                            any_fa_m = True
                    summary_row[f'cl{idx}'] = round(cs_m, 2) if any_cl_m else None
                    summary_row[f'fa{idx}'] = round(fs_m, 2) if any_fa_m else None
                _sw_attach_high_qoq_pair(summary_row, 'qcf', 'qcf_high', 'qcf_qoq', len(periods))
                _sw_attach_high_qoq_pair(summary_row, 'qgm', 'qgm_high', 'qgm_qoq', len(periods))
                _sw_attach_high_qoq_pair(summary_row, 'qroe', 'qroe_high', 'qroe_qoq', len(periods))
            else:
                for idx in range(len(periods)):
                    summary_row[f'ycf{idx}'] = None
                    summary_row[f'ycfnk{idx}'] = None
                    summary_row[f'qcf{idx}'] = None
                    summary_row[f'qgm{idx}'] = None
                    summary_row[f'qroe{idx}'] = None
                    summary_row[f'cl{idx}'] = None
                    summary_row[f'fa{idx}'] = None
                summary_row['qcf_high'] = None
                summary_row['qcf_qoq'] = None
                summary_row['qgm_high'] = None
                summary_row['qgm_qoq'] = None
                summary_row['qroe_high'] = None
                summary_row['qroe_qoq'] = None

        if summary_row is not None:
            _index_fill_summary_live(
                summary_row,
                conn=conn,
                isOverview=is_index_overview,
                indexCode=index_code,
                level=level,
                t0Summary=sw_t0_summary,
                hlcSummary=sw_hlc_summary,
                marginSummary=sw_margin_summary,
            )

        payload = {
            'success': True,
            'rows': rows,
            'summary_row': summary_row,
            'periods': periods,
            'has_q_dtprofit': bool(has_q_dtprofit),
            'has_rev_yoy': len(periods) > 0,
            'has_dtp_yoy': bool(has_q_dtprofit and len(periods) > 0),
            'level': level,
            'selected_l1_code': parent_l1_code,
            'selected_l1_name': l1_option_map.get(parent_l1_code),
            'selected_l2_code': parent_l2_code,
            'selected_l2_name': l2_option_map.get(parent_l2_code),
            'l1_options': [{'l1_code': k, 'l1_name': v} for k, v in sorted(l1_option_map.items(), key=lambda x: x[1])],
            'l2_options': [{'l2_code': k, 'l2_name': v} for k, v in sorted(l2_option_map.items(), key=lambda x: x[1])],
            'count': len(rows),
            'has_sw_extend_metrics': bool(has_sw_extend_metrics and len(periods) > 0),
            'cache_ttl_sec': SW_INDUSTRY_STATS_CACHE_TTL_API_SEC,
            'index_code': index_code,
            'index_options': index_options,
            'selected_index_codes': selected_codes,
            'margin_as_of': margin_as_of.isoformat() if margin_as_of else None,
            'margin_as_of_mode': margin_as_of_mode,
        }
        smallTtlCachePut(_index_industry_stats_cache, cache_key, payload, SW_INDUSTRY_STATS_CACHE_TTL, SW_INDUSTRY_STATS_CACHE_MAX)
        _sw_attach_payload_versions(payload)
        return jsonify(payload)
    except Exception as e:
        logger.error('读取宽基指数行业统计失败: %s', e)
        return jsonify({'success': False, 'error': str(e), 'rows': []}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# monolith / compat 蛇形命名兼容
api_sw_industry_stats = apiSwIndustryStats
api_sw_industry_limit_detail = apiSwIndustryLimitDetail
api_sw_index_amt_pctile_series = apiSwIndexAmtPctileSeries
api_index_industry_stats = apiIndexIndustryStats


def _swWarmDefaultLevelsViaTestClient(app) -> None:
    """经 test_request_context 预热默认 L1/L2/L3 长缓存（无 trade_date）。

    已有完整包才跳过；pending_fina 占位必须继续跑 _full=1，否则启动后首屏
    一旦写入 partial，预热会被永久短路，市值/营收一直为 0。
    """
    for warmLevel in ('l1', 'l2', 'l3'):
        ck = f'{warmLevel}||||x12'
        hitExisting, _, _ = _sw_industry_stats_cache_get(ck, time.time())
        if hitExisting is not None and not hitExisting.get('pending_fina'):
            if warmLevel == 'l1':
                logger.debug('申万行业预热跳过：默认 L1 已是完整包')
            continue
        with app.app_context():
            with app.test_request_context(
                f'/api/sw_industry_stats?level={warmLevel}&_full=1'
            ):
                apiSwIndustryStats()
        hitPayload, _, _ = _sw_industry_stats_cache_get(ck, time.time())
        if hitPayload is None:
            logger.warning(
                '申万行业预热未写入缓存 level=%s（忽略不影响启动）',
                warmLevel,
            )
            continue
        logger.info(
            '申万行业统计缓存已预热：默认 %s rows=%s pending=%s',
            warmLevel.upper(),
            len(hitPayload.get('rows') or []),
            bool(hitPayload.get('pending_fina')),
        )


def _swRefreshDefaultLiveLayers() -> None:
    """定时刷新默认层级实时层，尽量让轮询走 sync 亚秒路径。"""
    for warmLevel in ('l1', 'l2', 'l3'):
        ck = f'{warmLevel}||||x12'
        payload, tsInd, lvl = _sw_industry_stats_cache_get(ck, time.time())
        if payload is None or not tsInd:
            continue
        useLvl = lvl or warmLevel
        poolKey = _sw_limit_flagged_pool_key(useLvl, '', '', None)
        if _sw_live_market_maps_is_fresh(tsInd, None, poolKey):
            continue
        _sw_schedule_live_market_refresh(ck, tsInd, None, useLvl, poolKey)


def _swWarmIndexIndustryViaTestClient(app) -> None:
    """预热宽基默认总览 + 常用指数 L1（与 INDEX_MONITOR_CODES 对齐）。"""
    warmPaths = [
        '/api/index_industry_stats',
        '/api/index_industry_stats?level=l1',
    ]
    # 冷路径最重的几个优先；其余指数顺带预热默认 L1
    priorityCodes = [
        '000300.SH',
        '000905.SH',
        '932000.CSI',
        '000510.SH',
        '399006.SZ',
    ]
    seen: set[str] = set()
    for code in priorityCodes + [c for c, _ in INDEX_MONITOR_CODES]:
        cu = str(code or '').strip().upper()
        if not cu or cu in seen:
            continue
        seen.add(cu)
        warmPaths.append(f'/api/index_industry_stats?index_code={cu}&level=l1')
    failedPaths: list[str] = []
    for path in warmPaths:
        # cache_key 形如 index|level|...|x14；空 index 总览用 ''|
        try:
            with app.app_context():
                with app.test_client() as tc:
                    rv = tc.get(path)
            if rv.status_code != 200:
                logger.warning(
                    '宽基行业预热 HTTP status=%s path=%s（忽略不影响启动）',
                    getattr(rv, 'status_code', '?'),
                    path,
                )
                failedPaths.append(path)
                continue
            data = rv.get_json(silent=True) or {}
            nRows = len(data.get('rows') or [])
            if nRows <= 0:
                failedPaths.append(path)
                logger.warning('宽基行业预热为空 path=%s', path)
            else:
                logger.info('宽基行业统计缓存已预热：path=%s rows=%s', path, nRows)
        except Exception as e:
            failedPaths.append(path)
            logger.warning('宽基行业预热失败 path=%s: %s', path, e)
    for path in failedPaths:
        try:
            with app.app_context():
                with app.test_client() as tc:
                    rv = tc.get(path)
            data = rv.get_json(silent=True) or {}
            nRows = len(data.get('rows') or [])
            if rv.status_code == 200 and nRows > 0:
                logger.info('宽基行业预热重试成功：path=%s rows=%s', path, nRows)
            else:
                logger.warning(
                    '宽基行业预热重试仍空 path=%s status=%s rows=%s',
                    path, getattr(rv, 'status_code', '?'), nRows,
                )
        except Exception as e:
            logger.warning('宽基行业预热重试失败 path=%s: %s', path, e)


def warmupSwIndustryCacheAsync() -> None:
    """启动预热默认申万 + 宽基视图，并周期刷新申万实时层（SWR 配套）。"""

    def _worker():
        try:
            from rt_monitor.monolith import app

            time.sleep(1.0)
            _swWarmDefaultLevelsViaTestClient(app)
            _swWarmIndexIndustryViaTestClient(app)
        except Exception as e:
            logger.warning('申万/宽基行业预热失败（忽略不影响启动）: %s', e)
        while True:
            try:
                time.sleep(SW_LIVE_BG_REFRESH_INTERVAL_SEC)
                _swRefreshDefaultLiveLayers()
            except Exception as e:
                logger.warning('申万实时层周期预热失败（将继续）: %s', e)

    t = threading.Thread(target=_worker, daemon=True, name='warmup-sw-industry')
    t.start()

# 代码更新时间：2026-07-20 22:17
# 代码更新时间：2026-07-27 11:54 收盘价六列明细弹窗统一申万行业+突破类型（250/新低对称判定）
# 代码更新时间：2026-08-01 21:29 Wind新高低预取对 MySQL 1146/#sql 临时表竞态短重试
# 代码更新时间：2026-08-04 17:39 ST 判定改以 rt_k.name 为准（stockstat.sec_name 仅启动时刷新会滞后）；三处标志位查询加 JOIN rt_k
# 代码更新时间：2026-08-06 10:15 区间涨跌幅 T 点击明细：成分涨幅/成交额 Top10（市值+自由换手）
# 代码更新时间：2026-08-06 11:06 Top20 + fina 当前价位置（距历史高/850高/250高）
# 代码更新时间：2026-08-06 18:12 新高低/实时层缓存 TTL 15→120/60，缓解 3s 轮询叠 14s 扫描
# 代码更新时间：2026-08-06 18:26 SWR：缓存命中实时层过期时秒返旧值+后台刷新；75s 周期预热；前端轮询 5s
# 代码更新时间：2026-08-06 18:50 修复 limit_count 缓存跨 L1/L2/L3 误复用聚合结果导致新高低全 0
# 代码更新时间：2026-08-06 22:41 宽基 index_industry_stats 启动预热（总览+常用指数 L1）

# 代码更新时间：2026-08-07 14:06 新高低主表与明细同源：池TTL=120；明细走 count-maps；SWR 热缓存同步贴计数
# 代码更新时间：2026-08-23 14:24 宽基总览恢复 index_daily/rt_idx_k 区间涨跌幅、成交额比%、新高低与融资余额
# 代码更新时间：2026-08-27 09:12 index_weight 改 WIND_BULK+成分缓存；失败不写 inf 空缓存
# 代码更新时间：2026-09-02 09:28 过滤 rt_sw_k 盘前 0/-100；首屏不阻塞新高低扫描
# 代码更新时间：2026-09-02 09:48 首屏日线 220 天；panel/fina/stats/hlc singleflight
# 代码更新时间：2026-09-02 09:57 首屏跳过财报重算，后台 _full=1 补齐
# 代码更新时间：2026-09-07 16:53 isSwIndustryRefreshing：实时层刷新/完整冷建供 top_moves 让路
# 代码更新时间：2026-09-07 18:19 实时层 TTL 60→10s，配合申万页 2s 轮询
# 代码更新时间：2026-09-07 18:27 涨跌幅 TTL=2s 轻量路径；成交额分位约 30s 重刷
# 代码更新时间：2026-09-09 07:28 放开 932000/899050 区间涨跌幅拼 rt_idx_k
# 代码更新时间：2026-09-21 10:20 partial 进长缓存；HTTP 禁 heavy；后台 _full 直调视图
# 代码更新时间：2026-09-21 11:20 pending 占位不跳过预热；空 amt 穿透；_full 勿返占位
# 代码更新时间：2026-10-04 17:42 申万实时层休市刷新放宽到 5 分钟
