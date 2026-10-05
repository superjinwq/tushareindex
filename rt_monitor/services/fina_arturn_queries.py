"""财报 arturn / 固定资产序列查询（P4.2 自 monolith 迁出）。"""
from __future__ import annotations

import gzip
import json
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
from difflib import SequenceMatcher
from typing import Any

import pymysql
import numpy as np

from logger_config import get_logger
from rt_monitor.core.config import DB_CONFIG, DB_CONFIG_TUSHARE
from rt_monitor.core.market_session import isAShareMarketActive
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

# stock_wind.rt_k 有效代码全集：进程内短 TTL，减轻财报筛选重复扫 rt_k
_RT_K_TS_CODES_CACHE_ROW: tuple[float, tuple[str, ...]] | None = None
_RT_K_TS_CODES_CACHE_TTL_SEC = 180.0


def _get_cached_rt_k_ts_codes_tuple(conn) -> tuple[str, ...] | None:
    """返回 rt_k 中 ts_code 全集（排序元组）；失败时返回 None。"""
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
    """检测 stock_tushare 下某表是否存在列（缺列时避免 SQL 直接失败）。"""
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
    """LEFT JOIN：每股一行 sw_name（与 limit_stats / 财报表共用）。"""
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


# 报告期级宽表小时级以上才变；TTL 过短会导致预热后很快冷击穿（大盘期可达数十秒）
FINA_ARTURN_TABLE_CACHE_TTL = float(os.getenv('FINA_ARTURN_TABLE_CACHE_TTL', '1800'))
# 最多同时保留多少个「报告期」的整包 payload（每键一份全市场宽表 dict，与预热条数无必然相等；见下 FINA_ARTURN_WARMUP_*）
# 粗算单期内存：行数约 4k～5.5k × 每行百级字段，Python 侧常落在约 10～50MB/期；50 键为上限非必占满，超出 LRU 淘汰最久未用
FINA_ARTURN_TABLE_CACHE_MAX = 50
# 启动/保活预热：按 end_dates 顺序预取前 N 个报告期（与 MAX 独立；增大 N 会拉长启动阶段 DB 负载）
FINA_ARTURN_WARMUP_PREFETCH_N = int(os.getenv('FINA_ARTURN_WARMUP_PREFETCH_N', '6'))
# 周期保活：在 TTL 过期前重刷最近 N 期，避免切期冷建
FINA_ARTURN_KEEPALIVE_INTERVAL_SEC = float(os.getenv('FINA_ARTURN_KEEPALIVE_INTERVAL_SEC', '600'))
FINA_ARTURN_ATTACH_PARALLEL_WORKERS = max(
    1, min(6, int(os.getenv('FINA_ARTURN_ATTACH_PARALLEL_WORKERS', '4')))
)
# 休市冻结：休市时段宽表过期也继续送旧值、不保活重算；=0 恢复「全天按 TTL 重算」旧行为
FINA_ARTURN_OFFHOURS_FREEZE = os.getenv('FINA_ARTURN_OFFHOURS_FREEZE', '1') != '0'
# 每段收盘后顺延多久仍按交易时段处理，保证收盘快照（rt_k 收盘价）能进宽表
FINA_ARTURN_CLOSE_GRACE_SEC = float(os.getenv('FINA_ARTURN_CLOSE_GRACE_SEC', '300'))
# 开盘首轮保活完成前为 True：期间过期缓存照送旧值，避免开盘请求冷建 20～55s
_finaServeStaleUntilRefreshed = False
# 宽表 HTTP 热路径：31MB jsonify + gzip 每次约 3～4s；预编码后复用 gzip 字节
# 值为 (过期时间, (payload.last_update, gzip 字节))：同键宽表重建后 last_update 变化即重编码
_fina_arturn_wire_cache: OrderedDict[tuple, tuple[float, tuple[str, bytes]]] = OrderedDict()


def isFinaArturnMarketActive(now: datetime | None = None) -> bool:
    """宽表是否按交易时段保活；关闭休市冻结时恒为 True（旧行为）。"""
    if not FINA_ARTURN_OFFHOURS_FREEZE:
        return True
    return isAShareMarketActive(now, closeGraceSec=FINA_ARTURN_CLOSE_GRACE_SEC)


def setFinaServeStaleUntilRefreshed(flag: bool) -> None:
    global _finaServeStaleUntilRefreshed
    _finaServeStaleUntilRefreshed = bool(flag)


def _finaCacheGetAllowStale(cache_obj: OrderedDict, cache_key, now_ts: float):
    """未过期直接返回；休市或开盘首轮保活未完成时，已过期条目也返回（不冷建）。"""
    hit = _small_ttl_cache_get(cache_obj, cache_key, now_ts)
    if hit is not None:
        return hit
    if not _finaServeStaleUntilRefreshed and isFinaArturnMarketActive():
        return None
    with _finaCacheLock:
        entry = cache_obj.get(cache_key)
    return entry[1] if entry is not None else None


def expireFinaArturnTableCache() -> int:
    """把宽表缓存全部标记过期（条目保留可作旧值送出），供收盘定格重建。"""
    with _finaCacheLock:
        for k, (_, v) in list(_fina_indicator_metric_table_cache.items()):
            _fina_indicator_metric_table_cache[k] = (0.0, v)
        return len(_fina_indicator_metric_table_cache)


def _finaArturnJsonDefault(o: Any):
    if isinstance(o, Decimal):
        try:
            return float(o)
        except (TypeError, ValueError):
            return str(o)
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, datetime):
        return o.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, (bytes, bytearray)):
        return o.decode('utf-8', errors='replace')
    return str(o)


def _finaArturnWireCacheKey(payload: dict) -> tuple:
    ed = str(payload.get('selected_end_date') or '')
    dv = str(payload.get('data_version') or '')
    hint = str(payload.get('hint') or '')
    ends = payload.get('end_dates') or []
    endFp = ','.join(str(x) for x in ends[:12])
    return ('arturn_wire_gz', ed, dv, endFp, hint)


_finaArturnWireLocksGate = threading.Lock()
_finaArturnWireLocks: dict[tuple, threading.Lock] = {}


def _finaArturnWireLock(key: tuple) -> threading.Lock:
    with _finaArturnWireLocksGate:
        lock = _finaArturnWireLocks.get(key)
        if lock is None:
            if len(_finaArturnWireLocks) > 4 * FINA_ARTURN_TABLE_CACHE_MAX:
                _finaArturnWireLocks.clear()
            lock = _finaArturnWireLocks[key] = threading.Lock()
        return lock


def ensureFinaArturnWireGzip(payload: dict) -> bytes | None:
    """把宽表 payload 编成 gzip JSON，同口径只编一次（按口径加锁：并发首请求/预热不重复编码约 1.5s）。"""
    if not payload or payload.get('error'):
        return None
    key = _finaArturnWireCacheKey(payload)
    stamp = str(payload.get('last_update') or '')
    hit = _finaCacheGetAllowStale(_fina_arturn_wire_cache, key, time.time())
    if hit is not None and hit[0] == stamp:
        return hit[1]
    with _finaArturnWireLock(key):
        hit = _finaCacheGetAllowStale(_fina_arturn_wire_cache, key, time.time())
        if hit is not None and hit[0] == stamp:
            return hit[1]
        return _finaArturnEncodeWire(payload, key, stamp)


def _finaArturnEncodeWire(payload: dict, key: tuple, stamp: str) -> bytes:
    t0 = time.perf_counter()
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(',', ':'),
        default=_finaArturnJsonDefault,
        allow_nan=True,
    ).encode('utf-8')
    gz = gzip.compress(raw, compresslevel=6)
    _small_ttl_cache_put(
        _fina_arturn_wire_cache,
        key,
        (stamp, gz),
        FINA_ARTURN_TABLE_CACHE_TTL,
        FINA_ARTURN_TABLE_CACHE_MAX,
    )
    logger.info(
        'fina_arturn wire gzip encoded end=%s rows=%s raw=%d gz=%d elapsed=%.3fs',
        payload.get('selected_end_date'),
        len(payload.get('rows') or []),
        len(raw),
        len(gz),
        time.perf_counter() - t0,
    )
    return gz
_fina_indicator_metric_table_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
# 同报告期冷建去重：多请求并发只跑一次重查询
_finaArturnBuildGate = threading.Lock()
_finaArturnBuildWaiters: dict[str, threading.Event] = {}
_finaArturnBuildCount = 0
_FINA_ARTURN_BUILD_WAIT_TIMEOUT_SEC = 300.0


def isFinaArturnBuilding() -> bool:
    """保活/冷建宽表期间为 True，热路径可改送过期缓存避免抢库。"""
    with _finaArturnBuildGate:
        return _finaArturnBuildCount > 0


def _fina_arturn_table_cache_key(end_date: str) -> tuple:
    return ('arturn_days', str(end_date or '').strip(), 'sb_act_12')


def getOrBuildFinaArturnTable(end_date: str, allowStale: bool = True) -> dict:
    """取报告期宽表：命中缓存直接返回；未命中时同 end_date 并发请求合并为一次建表。

    allowStale：HTTP 路径为 True（休市/开盘首轮保活前送旧值）；保活重建传 False。
    """
    global _finaArturnBuildCount
    ed = str(end_date or '').strip()
    if not ed:
        return {'rows': [], 'periods': [], 'contract_periods': [], 'error': '缺少 end_date'}
    cacheKey = _fina_arturn_table_cache_key(ed)
    cacheGet = _finaCacheGetAllowStale if allowStale else _small_ttl_cache_get
    nowTs = time.time()
    hit = cacheGet(_fina_indicator_metric_table_cache, cacheKey, nowTs)
    if hit is not None:
        return hit

    waitEvent: threading.Event | None = None
    isBuilder = False
    with _finaArturnBuildGate:
        hit = cacheGet(
            _fina_indicator_metric_table_cache, cacheKey, time.time()
        )
        if hit is not None:
            return hit
        existing = _finaArturnBuildWaiters.get(ed)
        if existing is not None:
            waitEvent = existing
        else:
            waitEvent = threading.Event()
            _finaArturnBuildWaiters[ed] = waitEvent
            isBuilder = True

    if not isBuilder:
        waitEvent.wait(timeout=_FINA_ARTURN_BUILD_WAIT_TIMEOUT_SEC)
        hit2 = _small_ttl_cache_get(
            _fina_indicator_metric_table_cache, cacheKey, time.time()
        )
        if hit2 is not None:
            return hit2
        return {
            'rows': [],
            'periods': [],
            'contract_periods': [],
            'error': f'等待报告期 {ed} 建表超时或失败',
        }

    with _finaArturnBuildGate:
        _finaArturnBuildCount += 1
    try:
        payload = _query_fina_arturn_table(ed)
        if payload and not payload.get('error'):
            _small_ttl_cache_put(
                _fina_indicator_metric_table_cache,
                cacheKey,
                payload,
                FINA_ARTURN_TABLE_CACHE_TTL,
                FINA_ARTURN_TABLE_CACHE_MAX,
            )
        return payload
    finally:
        with _finaArturnBuildGate:
            _finaArturnBuildCount = max(0, _finaArturnBuildCount - 1)
            ev = _finaArturnBuildWaiters.pop(ed, None)
            if ev is not None:
                ev.set()


def _fina_arturn_build_data_version(selected_end_date: str, row_count: int) -> str:
    """同报告期+行数+口径标识；前端用于跳过全量 DOM 重建。"""
    return f'{selected_end_date}|sb_act_12|{row_count}'


def _fina_arturn_parallel_attaches(
    specs: list[tuple[str, Any, tuple, dict]],
    *,
    max_workers: int | None = None,
) -> list[tuple[str, float]]:
    """并行执行需独立 MySQL 连接的 attach；每个 fn 首参为 conn。"""
    if not specs:
        return []
    workers = max_workers if max_workers is not None else FINA_ARTURN_ATTACH_PARALLEL_WORKERS

    def _run_one(item: tuple[str, Any, tuple, dict]) -> tuple[str, float]:
        label, fn, args, kwargs = item
        t0 = time.perf_counter()
        c = pymysql.connect(**DB_CONFIG_TUSHARE)
        try:
            fn(c, *args, **kwargs)
        finally:
            c.close()
        return label, time.perf_counter() - t0

    if len(specs) == 1:
        return [_run_one(specs[0])]

    out: list[tuple[str, float]] = []
    pool_workers = min(workers, len(specs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=pool_workers) as pool:
        futs = [pool.submit(_run_one, s) for s in specs]
        for fut in concurrent.futures.as_completed(futs):
            out.append(fut.result())
    return out

def _derive_financial_periods(base_end_date: str) -> list[str]:
    """按需求生成当期+前三年：当期保留，历史三期固定为 YYYY1231。"""
    y0 = int(base_end_date[:4])
    return [
        base_end_date,
        f"{y0 - 1}1231",
        f"{y0 - 2}1231",
        f"{y0 - 3}1231",
    ]


def _derive_quarter_periods(base_end_date: str, n: int = 9) -> list[str]:
    """按季度倒推报告期列表（含当期），如 20260331 -> 20260331,20251231,20250930...。"""
    s = _normalize_end_date_yyyymmdd(base_end_date)
    if not s:
        return []
    try:
        y = int(s[:4])
        m = int(s[4:6])
    except Exception:
        return []
    quarter_months = [3, 6, 9, 12]
    if m not in quarter_months:
        m = 12
    out: list[str] = []
    lim = max(1, min(int(n), 40))
    for _ in range(lim):
        if m == 3:
            out.append(f"{y}0331")
            y -= 1
            m = 12
        elif m == 6:
            out.append(f"{y}0630")
            m = 3
        elif m == 9:
            out.append(f"{y}0930")
            m = 6
        else:
            out.append(f"{y}1231")
            m = 9
    return out


def _fina_end_date_str_from_yq(y: int, q: int) -> str:
    """季度末 YYYYMMDD（q=1..4）。"""
    mmdd = ('0331', '0630', '0930', '1231')[q - 1]
    return f'{y}{mmdd}'


def _fina_prev_yq(y: int, q: int) -> tuple[int, int]:
    if q > 1:
        return y, q - 1
    return y - 1, 4


def _fina_yq_from_anchor(anchor_yyyymmdd: str) -> tuple[int, int] | None:
    """报告期末锚点 → (年, 季度 1..4)；非季末则不可用。"""
    s = _normalize_end_date_yyyymmdd(anchor_yyyymmdd)
    if not s or len(s) != 8:
        return None
    try:
        m = int(s[4:6])
    except ValueError:
        return None
    qmap = {3: 1, 6: 2, 9: 3, 12: 4}
    if m not in qmap:
        return None
    return int(s[:4]), qmap[m]


def _fina_yoy_anchor_end_date(anchor_yyyymmdd: str) -> str | None:
    """上年同季财报期末 YYYYMMDD（同比锚点），如 20260331→20250331。"""
    yq = _fina_yq_from_anchor(anchor_yyyymmdd)
    if not yq:
        return None
    y, q = yq
    if y <= 1900:
        return None
    return _fina_end_date_str_from_yq(y - 1, q)


def _fina_ttm_income_date_keys(anchor_yyyymmdd: str) -> set[str]:
    """TTM 单季拆解所需的 income.end_date 键集合（YYYYMMDD）。"""
    yq = _fina_yq_from_anchor(anchor_yyyymmdd)
    if not yq:
        return set()
    cy, cq = yq
    needed: set[str] = set()
    for _ in range(4):
        needed.add(_fina_end_date_str_from_yq(cy, cq))
        if cq > 1:
            needed.add(_fina_end_date_str_from_yq(cy, cq - 1))
        cy, cq = _fina_prev_yq(cy, cq)
    return needed


def _fina_income_date_keys_for_single_quarter_revenue(period_yyyymmdd_list: list[str]) -> set[str]:
    """展示用各季末 income 键 + 拆单季营收所需的同年内上一季末。

    Q1：仅需当行累计；Q2–Q4：需「当季末累计 − 上一季末累计」，故必须把同年内上一季末一并拉取。
    仅拉 9 个展示期时，最旧一列若为 Q4（如 23Q4），缺 23Q3 会导致整列为空。"""
    out: set[str] = set()
    for raw in period_yyyymmdd_list:
        norm = _normalize_end_date_yyyymmdd(raw)
        if not norm:
            continue
        out.add(norm)
        yq = _fina_yq_from_anchor(norm)
        if not yq:
            continue
        y, q = yq
        if q > 1:
            out.add(_fina_end_date_str_from_yq(y, q - 1))
    return out


def _fina_income_date_keys_with_yoy_for_periods(period_yyyymmdd_list: list[str]) -> set[str]:
    """单季营收展示期对应的 income 键 + 各期「上年同季」拆单季营收所需键（用于同比增速）。"""
    out = _fina_income_date_keys_for_single_quarter_revenue(period_yyyymmdd_list)
    for raw in period_yyyymmdd_list:
        norm = _normalize_end_date_yyyymmdd(raw)
        if not norm:
            continue
        prev_ed = _fina_yoy_anchor_end_date(norm)
        if not prev_ed:
            continue
        out |= _fina_income_date_keys_for_single_quarter_revenue([prev_ed])
    return out


def _sw_median_rounded(values: list[float], ndigits: int = 2) -> float | None:
    """申万扩展列 qgm/qroe：对行业内（或全市场）有值个股的样本求中位数；空样本返回 None。"""
    if not values:
        return None
    return round(float(statistics.median(values)), ndigits)


def _sw_attach_high_qoq_pair(
    row_obj: dict,
    metric_prefix: str,
    high_key: str,
    qoq_key: str,
    series_len: int,
) -> None:
    """对齐财报页 _fina_arturn_attach_quarter_margins 内 _attach_high_qoq：prefix0 为最新季，序列内新高与环比连升次数。"""
    m0v = row_obj.get(f'{metric_prefix}0')
    if m0v is None:
        row_obj[high_key] = None
        row_obj[qoq_key] = None
        return
    m_present: list[float] = []
    for i in range(series_len):
        mv = row_obj.get(f'{metric_prefix}{i}')
        if mv is not None:
            m_present.append(float(mv))
    row_obj[high_key] = int(1 if m_present and float(m0v) == max(m_present) else 0)
    rise_count = 0
    prev = row_obj.get(f'{metric_prefix}0')
    if prev is None:
        row_obj[qoq_key] = None
        return
    for i in range(1, series_len):
        curr = row_obj.get(f'{metric_prefix}{i}')
        if curr is None:
            break
        if float(prev) > float(curr):
            rise_count += 1
            prev = curr
            continue
        break
    row_obj[qoq_key] = int(rise_count)


def _sw_rev_yoy_yoypct_for_codes(
    ts_list: list[str],
    rev_nested: dict[str, dict[str, float]],
    anchor_p: str,
) -> float | None:
    """单季营收同比增速(%)。

    可比口径（披露期）：仅以 anchor 季已能拆出单季营收的股票为样本；仅当上年同季亦能拆出单季营收时，
    将该股票的本季与上年同季收入同时计入分子分母之和，再 (sum_curr−sum_prev)/sum_prev×100。
    """
    prev_ed = _fina_yoy_anchor_end_date(anchor_p)
    if not prev_ed:
        return None
    yq_c = _fina_yq_from_anchor(anchor_p)
    yq_p = _fina_yq_from_anchor(prev_ed)
    if not yq_c or not yq_p:
        return None
    cy, cq = yq_c
    py, pq = yq_p
    sum_c = 0.0
    sum_p = 0.0
    for ts in ts_list:
        rm = rev_nested.get(ts)
        if not rm:
            continue
        sq_c = _fina_quarter_single_revenue(rm, cy, cq)
        if sq_c is None:
            continue
        sq_p = _fina_quarter_single_revenue(rm, py, pq)
        if sq_p is None:
            continue
        sum_c += float(sq_c)
        sum_p += float(sq_p)
    if sum_p <= 0:
        return None
    return (sum_c - sum_p) / sum_p * 100.0


def _sw_cf_yoy_yoypct_for_codes(
    ts_list: list[str],
    cf_nested: dict[str, dict[str, float]],
    anchor_p: str,
) -> float | None:
    """单季经营现金流净额同比增速(%)，口径对齐 _sw_rev_yoy_yoypct_for_codes（拆单季后行业成分股双期可算则累加）。"""
    prev_ed = _fina_yoy_anchor_end_date(anchor_p)
    if not prev_ed:
        return None
    yq_c = _fina_yq_from_anchor(anchor_p)
    yq_p = _fina_yq_from_anchor(prev_ed)
    if not yq_c or not yq_p:
        return None
    cy, cq = yq_c
    py, pq = yq_p
    sum_c = 0.0
    sum_p = 0.0
    for ts in ts_list:
        cm = cf_nested.get(ts)
        if not cm:
            continue
        sq_c = _fina_quarter_single_revenue(cm, cy, cq)
        if sq_c is None:
            continue
        sq_p = _fina_quarter_single_revenue(cm, py, pq)
        if sq_p is None:
            continue
        sum_c += float(sq_c)
        sum_p += float(sq_p)
    if sum_p <= 0:
        return None
    return (sum_c - sum_p) / sum_p * 100.0


def _sw_cf_industry_single_q_sum_yuan(
    ts_list: list[str],
    cf_nested: dict[str, dict[str, float]],
    anchor_p: str,
) -> float | None:
    """行业内单季经营现金流（元）合计：与 qcf 列同源（有单季拆解值的股票才计入）。"""
    yq = _fina_yq_from_anchor(anchor_p)
    if not yq:
        return None
    tot = 0.0
    any_v = False
    for ts in ts_list:
        cm = cf_nested.get(ts)
        if not cm:
            continue
        sq = _fina_quarter_single_revenue(cm, yq[0], yq[1])
        if sq is None:
            continue
        tot += float(sq)
        any_v = True
    return tot if any_v else None


def _fina_periods_union_yoy_for_q_dtprofit(period_yyyymmdd_list: list[str]) -> list[str]:
    """fina_indicator.q_dtprofit 批量拉取：展示期 ∪ 各期上年同季 end_date（用于扣非同比增速）。"""
    out: set[str] = set()
    for raw in period_yyyymmdd_list:
        norm = _normalize_end_date_yyyymmdd(raw)
        if not norm:
            continue
        out.add(norm)
        prev_ed = _fina_yoy_anchor_end_date(norm)
        if prev_ed:
            out.add(prev_ed)
    return sorted(out)


def _sw_dtp_yoy_yoypct_for_codes(
    ts_list: list[str],
    dtp_nested: dict[str, dict[str, float]],
    anchor_p: str,
) -> float | None:
    """单季扣非净利润同比增速(%)。

    可比口径与营收同比一致：仅以 anchor 报告期已存在 q_dtprofit 的股票为样本；
    仅当上年同季亦有 q_dtprofit 时，将两期金额（元）分别累加后算增速。
    """
    p = _normalize_end_date_yyyymmdd(anchor_p)
    if not p:
        return None
    p_prev = _fina_yoy_anchor_end_date(p)
    if not p_prev:
        return None
    sum_c = 0.0
    sum_p = 0.0
    for ts in ts_list:
        mp = dtp_nested.get(ts)
        if not mp:
            continue
        v_c = mp.get(p)
        if v_c is None:
            continue
        v_p = mp.get(p_prev)
        if v_p is None:
            continue
        sum_c += float(v_c)
        sum_p += float(v_p)
    if sum_p <= 0:
        return None
    return (sum_c - sum_p) / sum_p * 100.0


def _fina_parse_income_total_revenue(raw) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(',', '')
    if s == '':
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fina_quarter_single_revenue(rev_map: dict[str, float], y: int, q: int) -> float | None:
    """单季营业总收入：Q1 取当行；Q2–Q4 为累计差（A 股利润表口径）。"""
    ed = _fina_end_date_str_from_yq(y, q)
    r_cur = rev_map.get(ed)
    if r_cur is None:
        return None
    if q == 1:
        return float(r_cur)
    prev = _fina_end_date_str_from_yq(y, q - 1)
    r_prev = rev_map.get(prev)
    if r_prev is None:
        return None
    return float(r_cur) - float(r_prev)


def _fina_ttm_total_revenue(rev_map: dict[str, float], anchor_yyyymmdd: str) -> float | None:
    """锚定报告期末的最近四个单季营业总收入之和。"""
    yq = _fina_yq_from_anchor(anchor_yyyymmdd)
    if not yq:
        return None
    cy, cq = yq
    total = 0.0
    for _ in range(4):
        sq = _fina_quarter_single_revenue(rev_map, cy, cq)
        if sq is None:
            return None
        total += sq
        cy, cq = _fina_prev_yq(cy, cq)
    return total


def _fina_batch_cashflow_n_cashflow_act(
    conn,
    ts_codes: list[str],
    date_keys: set[str],
    has_cf_rt: bool,
) -> dict[str, dict[str, float]]:
    """ts_code -> { YYYYMMDD -> n_cashflow_act 累计值（元）}，选行与 income 常用披露行一致。"""
    if not ts_codes or not date_keys:
        return {}
    cf_rt = (
        '          CASE WHEN cf.report_type = 1 THEN 0 ELSE 1 END, '
        if has_cf_rt else
        ''
    )
    date_list = sorted(date_keys)
    date_objs = [datetime.strptime(d, '%Y%m%d').date() for d in date_list]
    out: dict[str, dict[str, float]] = defaultdict(dict)
    chunk = 450
    for i in range(0, len(ts_codes), chunk):
        part = ts_codes[i : i + chunk]
        ph_ts = ','.join(['%s'] * len(part))
        ph_dt = ','.join(['%s'] * len(date_objs))
        sql = (
            'SELECT cf.ts_code, cf.end_date, cf.n_cashflow_act '
            'FROM ( '
            '  SELECT cf.ts_code, cf.end_date, cf.n_cashflow_act, '
            '    ROW_NUMBER() OVER ( '
            '      PARTITION BY cf.ts_code, cf.end_date '
            '      ORDER BY '
            '        CASE WHEN cf.n_cashflow_act IS NOT NULL AND TRIM(CAST(cf.n_cashflow_act AS CHAR)) <> \'\' '
            '          THEN 0 ELSE 1 END, '
            '        CASE WHEN cf.update_flag = \'1\' THEN 0 ELSE 1 END, '
            + cf_rt +
            '        cf.ann_date DESC '
            '    ) AS rn '
            '  FROM cashflow cf '
            f'  WHERE cf.ts_code IN ({ph_ts}) AND cf.end_date IN ({ph_dt}) '
            ') cf '
            'WHERE cf.rn = 1'
        )
        params = tuple(part) + tuple(date_objs)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur.fetchall() or []:
                tc, ed, tv = row[0], row[1], row[2]
                k = _normalize_end_date_yyyymmdd(ed)
                if not k:
                    continue
                v = _fina_parse_income_total_revenue(tv)
                if v is not None:
                    out[tc][k] = v
    return {k: dict(v) for k, v in out.items()}


def _fina_batch_income_total_revenue(
    conn,
    ts_codes: list[str],
    date_keys: set[str],
    has_inc_rt: bool,
) -> dict[str, dict[str, float]]:
    """ts_code -> { YYYYMMDD -> total_revenue }，选行与 income 常用披露行一致。"""
    if not ts_codes or not date_keys:
        return {}
    fi_rt = (
        '          CASE WHEN inc.report_type = 1 THEN 0 ELSE 1 END, '
        if has_inc_rt else
        ''
    )
    date_list = sorted(date_keys)
    date_objs = [datetime.strptime(d, '%Y%m%d').date() for d in date_list]
    out: dict[str, dict[str, float]] = defaultdict(dict)
    chunk = 450
    for i in range(0, len(ts_codes), chunk):
        part = ts_codes[i: i + chunk]
        ph_ts = ','.join(['%s'] * len(part))
        ph_dt = ','.join(['%s'] * len(date_objs))
        sql = (
            'SELECT inc.ts_code, inc.end_date, inc.total_revenue '
            'FROM ( '
            '  SELECT inc.ts_code, inc.end_date, inc.total_revenue, '
            '    ROW_NUMBER() OVER ( '
            '      PARTITION BY inc.ts_code, inc.end_date '
            '      ORDER BY '
            '        CASE WHEN inc.total_revenue IS NOT NULL AND TRIM(CAST(inc.total_revenue AS CHAR)) <> \'\' '
            '          THEN 0 ELSE 1 END, '
            '        CASE WHEN inc.update_flag = \'1\' THEN 0 ELSE 1 END, '
            + fi_rt +
            '        inc.ann_date DESC '
            '    ) AS rn '
            '  FROM income inc '
            f'  WHERE inc.ts_code IN ({ph_ts}) AND inc.end_date IN ({ph_dt}) '
            ') inc '
            'WHERE inc.rn = 1'
        )
        params = tuple(part) + tuple(date_objs)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur.fetchall() or []:
                tc, ed, tv = row[0], row[1], row[2]
                k = _normalize_end_date_yyyymmdd(ed)
                if not k:
                    continue
                v = _fina_parse_income_total_revenue(tv)
                if v is not None:
                    out[tc][k] = v
    return {k: dict(v) for k, v in out.items()}


def _fina_batch_fina_indicator_q_dtprofit(
    conn,
    ts_codes: list[str],
    periods_yyyymmdd: list[str],
    has_fi_rt: bool,
) -> dict[str, dict[str, float]]:
    """ts_code -> { YYYYMMDD -> q_dtprofit 元 }，选行与财报表 _fina_arturn_attach_q_dtprofit_yi 一致。"""
    if not ts_codes or not periods_yyyymmdd:
        return {}
    fi_rt_ord = (
        '      CASE WHEN fi.report_type = 1 THEN 0 ELSE 1 END, '
        if has_fi_rt else
        ''
    )
    date_objs: list = []
    seen: set[str] = set()
    for d in periods_yyyymmdd:
        norm = _normalize_end_date_yyyymmdd(d)
        if norm and len(norm) == 8 and norm not in seen:
            seen.add(norm)
            date_objs.append(datetime.strptime(norm, '%Y%m%d').date())
    if not date_objs:
        return {}
    out: dict[str, dict[str, float]] = defaultdict(dict)
    chunk = 450
    for i in range(0, len(ts_codes), chunk):
        part = ts_codes[i : i + chunk]
        ph_ts = ','.join(['%s'] * len(part))
        ph_dt = ','.join(['%s'] * len(date_objs))
        sql = (
            'SELECT z.ts_code, z.end_date, z.q_dtprofit '
            'FROM ('
            '  SELECT fi.ts_code, fi.end_date, fi.q_dtprofit, '
            '    ROW_NUMBER() OVER ('
            '      PARTITION BY fi.ts_code, fi.end_date '
            '      ORDER BY '
            '        CASE WHEN fi.q_dtprofit IS NOT NULL THEN 0 ELSE 1 END, '
            '        CASE WHEN fi.update_flag = \'1\' THEN 0 ELSE 1 END, '
            f'        {fi_rt_ord}'
            '        fi.ann_date DESC'
            '    ) AS rn '
            '  FROM stock_tushare.fina_indicator fi '
            f'  WHERE fi.ts_code IN ({ph_ts}) '
            f'    AND fi.end_date IN ({ph_dt})'
            ') z '
            'WHERE z.rn = 1'
        )
        params = tuple(part) + tuple(date_objs)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur.fetchall() or []:
                tc = str(row[0]).strip() if row[0] else ''
                k = _normalize_end_date_yyyymmdd(row[1])
                qv = _to_float_or_none(row[2] if len(row) > 2 else None)
                if not tc or not k:
                    continue
                if qv is not None:
                    out[tc][k] = float(qv)
    return {k: dict(v) for k, v in out.items()}


def _fina_batch_fina_indicator_q_gs_margin_q_dt_roe(
    conn,
    ts_codes: list[str],
    periods_yyyymmdd: list[str],
    has_fi_rt: bool,
    has_q_gs: bool,
    has_q_dt_roe: bool,
) -> dict[str, dict[str, tuple[float | None, float | None]]]:
    """ts_code -> { end_date -> (q_gsprofit_margin, q_dt_roe) }，选行与财报页单季毛利率/ROE(扣非)一致。"""
    if not ts_codes or not periods_yyyymmdd or (not has_q_gs and not has_q_dt_roe):
        return {}
    fi_rt_ord = (
        '      CASE WHEN fi.report_type = 1 THEN 0 ELSE 1 END, '
        if has_fi_rt else
        ''
    )
    gm_sel = 'fi.q_gsprofit_margin' if has_q_gs else 'NULL AS q_gsprofit_margin'
    qroe_sel = 'fi.q_dt_roe' if has_q_dt_roe else 'NULL AS q_dt_roe'
    gm_ord = 'CASE WHEN fi.q_gsprofit_margin IS NOT NULL THEN 0 ELSE 1 END, ' if has_q_gs else ''
    qroe_ord = 'CASE WHEN fi.q_dt_roe IS NOT NULL THEN 0 ELSE 1 END, ' if has_q_dt_roe else ''
    date_objs: list = []
    seen: set[str] = set()
    for d in periods_yyyymmdd:
        norm = _normalize_end_date_yyyymmdd(d)
        if norm and len(norm) == 8 and norm not in seen:
            seen.add(norm)
            date_objs.append(datetime.strptime(norm, '%Y%m%d').date())
    if not date_objs:
        return {}
    out: dict[str, dict[str, tuple[float | None, float | None]]] = defaultdict(dict)
    chunk = 450
    for i in range(0, len(ts_codes), chunk):
        part = ts_codes[i : i + chunk]
        ph_ts = ','.join(['%s'] * len(part))
        ph_dt = ','.join(['%s'] * len(date_objs))
        sql = (
            'SELECT z.ts_code, z.end_date, z.q_gsprofit_margin, z.q_dt_roe '
            'FROM ('
            '  SELECT fi.ts_code, fi.end_date, '
            f'    {gm_sel}, '
            f'    {qroe_sel}, '
            '    ROW_NUMBER() OVER ('
            '      PARTITION BY fi.ts_code, fi.end_date '
            '      ORDER BY '
            f'        {gm_ord}'
            f'        {qroe_ord}'
            '        CASE WHEN fi.update_flag = \'1\' THEN 0 ELSE 1 END, '
            f'        {fi_rt_ord}'
            '        fi.ann_date DESC'
            '    ) AS rn '
            '  FROM stock_tushare.fina_indicator fi '
            f'  WHERE fi.ts_code IN ({ph_ts}) '
            f'    AND fi.end_date IN ({ph_dt})'
            ') z '
            'WHERE z.rn = 1'
        )
        params = tuple(part) + tuple(date_objs)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur.fetchall() or []:
                tc = str(row[0]).strip() if row[0] else ''
                ed = _normalize_end_date_yyyymmdd(row[1] if len(row) > 1 else None)
                gm = _to_float_or_none(row[2] if len(row) > 2 else None)
                qroe = _to_float_or_none(row[3] if len(row) > 3 else None)
                if not tc or not ed:
                    continue
                out[tc][ed] = (gm, qroe)
    return {k: dict(v) for k, v in out.items()}


def _fina_batch_balancesheet_contract_fix_yi(
    conn,
    ts_codes: list[str],
    periods_yyyymmdd: list[str],
    *,
    has_bs_rt: bool,
    has_adv: bool,
    has_contract: bool,
    has_fix: bool,
) -> dict[str, dict[str, tuple[float | None, float | None]]]:
    """ts_code -> { end_date -> (合同负债+预收亿, 固定资产亿) }，口径对齐财报页 balancesheet 选行。"""
    if not ts_codes or not periods_yyyymmdd or (not has_contract and not has_fix):
        return {}
    bs_rt = (
        '          CASE WHEN bs.report_type = 1 THEN 0 ELSE 1 END, '
        if has_bs_rt else
        ''
    )
    if has_contract and has_adv:
        cy_expr = (
            '(COALESCE(bs.contract_liab, 0) + COALESCE(bs.adv_receipts, 0)) / 100000000.0 AS contract_yi'
        )
        pref_where = (
            '(bs.contract_liab IS NOT NULL OR bs.adv_receipts IS NOT NULL OR bs.fix_assets_total IS NOT NULL)'
        )
        pref_ord = (
            'CASE WHEN (bs.contract_liab IS NOT NULL OR bs.adv_receipts IS NOT NULL) THEN 0 ELSE 1 END, '
        )
    elif has_contract:
        cy_expr = '(COALESCE(bs.contract_liab, 0) / 100000000.0) AS contract_yi'
        pref_where = '(bs.contract_liab IS NOT NULL OR bs.fix_assets_total IS NOT NULL)'
        pref_ord = 'CASE WHEN bs.contract_liab IS NOT NULL THEN 0 ELSE 1 END, '
    else:
        cy_expr = 'NULL AS contract_yi'
        pref_where = 'bs.fix_assets_total IS NOT NULL'
        pref_ord = 'CASE WHEN bs.fix_assets_total IS NOT NULL THEN 0 ELSE 1 END, '
    fx_expr = (
        '(COALESCE(bs.fix_assets_total, 0) / 100000000.0) AS fix_yi'
        if has_fix else
        'NULL AS fix_yi'
    )
    date_objs: list = []
    seen: set[str] = set()
    for d in periods_yyyymmdd:
        norm = _normalize_end_date_yyyymmdd(d)
        if norm and len(norm) == 8 and norm not in seen:
            seen.add(norm)
            date_objs.append(datetime.strptime(norm, '%Y%m%d').date())
    if not date_objs:
        return {}
    out: dict[str, dict[str, tuple[float | None, float | None]]] = defaultdict(dict)
    chunk = 450
    for i in range(0, len(ts_codes), chunk):
        part = ts_codes[i : i + chunk]
        ph_ts = ','.join(['%s'] * len(part))
        ph_dt = ','.join(['%s'] * len(date_objs))
        sql = (
            f'SELECT z.ts_code, z.end_date, z.contract_yi, z.fix_yi '
            f'FROM ( '
            f'  SELECT bs.ts_code, bs.end_date, {cy_expr}, {fx_expr}, '
            f'    ROW_NUMBER() OVER ( '
            f'      PARTITION BY bs.ts_code, bs.end_date '
            f'      ORDER BY '
            f'        {pref_ord}'
            f'        CASE WHEN bs.update_flag = \'1\' THEN 0 ELSE 1 END, '
            f'        {bs_rt}'
            f'        bs.ann_date DESC '
            f'    ) AS rn '
            f'  FROM stock_tushare.balancesheet bs '
            f'  WHERE bs.ts_code IN ({ph_ts}) AND bs.end_date IN ({ph_dt}) '
            f'    AND {pref_where} '
            f') z WHERE z.rn = 1'
        )
        params = tuple(part) + tuple(date_objs)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                for row in cur.fetchall() or []:
                    tc = str(row[0]).strip() if row[0] else ''
                    ed = _normalize_end_date_yyyymmdd(row[1] if len(row) > 1 else None)
                    cv = _to_float_or_none(row[2] if len(row) > 2 else None)
                    fv = _to_float_or_none(row[3] if len(row) > 3 else None)
                    if not tc or not ed:
                        continue
                    if not has_contract:
                        cv = None
                    if not has_fix:
                        fv = None
                    out[tc][ed] = (cv, fv)
        except Exception as e:
            logger.warning('balancesheet contract/fix 批量读取失败: %s', e)
            return {}
    return {k: dict(v) for k, v in out.items()}


def _fina_batch_accounts_receiv(
    conn,
    ts_codes: list[str],
    anchor_yyyymmdd: str,
    has_bs_rt: bool,
) -> dict[str, float]:
    """当期报告期末应收账款（与 balancesheet 合并报表选行一致）。"""
    if not ts_codes:
        return {}
    s = _normalize_end_date_yyyymmdd(anchor_yyyymmdd)
    if not s:
        return {}
    anchor_dt = datetime.strptime(s, '%Y%m%d').date()
    bs_rt = (
        '          CASE WHEN bs.report_type = 1 THEN 0 ELSE 1 END, '
        if has_bs_rt else
        ''
    )
    out: dict[str, float] = {}
    chunk = 450
    for i in range(0, len(ts_codes), chunk):
        part = ts_codes[i: i + chunk]
        ph = ','.join(['%s'] * len(part))
        sql = (
            'SELECT bs.ts_code, bs.accounts_receiv '
            'FROM ( '
            '  SELECT bs.ts_code, bs.accounts_receiv, '
            '    ROW_NUMBER() OVER ( '
            '      PARTITION BY bs.ts_code, bs.end_date '
            '      ORDER BY '
            '        CASE WHEN bs.accounts_receiv IS NOT NULL THEN 0 ELSE 1 END, '
            '        CASE WHEN bs.update_flag = \'1\' THEN 0 ELSE 1 END, '
            + bs_rt +
            '        bs.ann_date DESC '
            '    ) AS rn '
            '  FROM balancesheet bs '
            f'  WHERE bs.ts_code IN ({ph}) AND bs.end_date = %s '
            ') bs '
            'WHERE bs.rn = 1'
        )
        with conn.cursor() as cur:
            cur.execute(sql, tuple(part) + (anchor_dt,))
            for row in cur.fetchall() or []:
                tc, ar = row[0], row[1]
                if ar is None:
                    continue
                try:
                    out[str(tc)] = float(ar)
                except (TypeError, ValueError):
                    pass
    return out


def _fina_arturn_attach_stock_company_fields(conn, rows: list[dict]) -> None:
    """为财报表每行写入 stock_company.province、city、website（批量 IN；缺表或查询失败则跳过）。"""
    if not rows:
        return
    try:
        if not _tushare_column_exists(conn, 'stock_company', 'ts_code'):
            return
    except Exception:
        return
    codes_uniq: list[str] = []
    seen: set[str] = set()
    for r in rows:
        tc = str(r.get('ts_code') or '').strip()
        if tc and tc not in seen:
            seen.add(tc)
            codes_uniq.append(tc)
    if not codes_uniq:
        return
    ph = ','.join(['%s'] * len(codes_uniq))
    sql = (
        'SELECT ts_code, COALESCE(`province`,\'\'), COALESCE(`city`,\'\'), COALESCE(`website`,\'\') '
        f'FROM stock_tushare.stock_company WHERE ts_code IN ({ph})'
    )
    mp: dict[str, tuple[str, str, str]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(codes_uniq))
            for rr in cur.fetchall() or []:
                if not rr or rr[0] is None:
                    continue
                tsc = str(rr[0]).strip()
                pv = str(rr[1] or '').strip() if len(rr) > 1 else ''
                ct = str(rr[2] or '').strip() if len(rr) > 2 else ''
                wb = str(rr[3] or '').strip() if len(rr) > 3 else ''
                mp[tsc] = (pv, ct, wb)
    except Exception as e:
        logger.warning('fina_arturn attach stock_company 跳过: %s', e)
        return
    for r in rows:
        tc = str(r.get('ts_code') or '').strip()
        triple = mp.get(tc, ('', '', ''))
        r['province'] = triple[0]
        r['city'] = triple[1]
        r['website'] = triple[2]


_TOP10_HOLDERS_TOP3_SQL = (
    'WITH latest AS ( '
    '  SELECT ts_code, MAX(end_date) AS mx '
    '  FROM stock_tushare.top10_holders '
    '  WHERE ts_code IN ({ph}) '
    '  GROUP BY ts_code '
    '), ranked AS ( '
    '  SELECT h.ts_code, h.end_date, h.holder_name, h.holder_type, h.hold_amount, h.hold_ratio, '
    '    ROW_NUMBER() OVER (PARTITION BY h.ts_code ORDER BY h.hold_ratio DESC) AS rk '
    '  FROM stock_tushare.top10_holders h '
    '  INNER JOIN latest l ON l.ts_code = h.ts_code AND l.mx = h.end_date '
    ') '
    'SELECT ts_code, end_date, holder_name, holder_type, hold_amount, hold_ratio '
    'FROM ranked WHERE rk <= 3 '
    'ORDER BY ts_code, rk'
)
_TOP10_HOLDERS_TOP3_SQL_WITH_CHANGE = (
    'WITH latest AS ( '
    '  SELECT ts_code, MAX(end_date) AS mx '
    '  FROM stock_tushare.top10_holders '
    '  WHERE ts_code IN ({ph}) '
    '  GROUP BY ts_code '
    '), ranked AS ( '
    '  SELECT h.ts_code, h.end_date, h.holder_name, h.holder_type, h.hold_amount, h.hold_ratio, h.hold_change, '
    '    ROW_NUMBER() OVER (PARTITION BY h.ts_code ORDER BY h.hold_ratio DESC) AS rk '
    '  FROM stock_tushare.top10_holders h '
    '  INNER JOIN latest l ON l.ts_code = h.ts_code AND l.mx = h.end_date '
    ') '
    'SELECT ts_code, end_date, holder_name, holder_type, hold_amount, hold_ratio, hold_change '
    'FROM ranked WHERE rk <= 3 '
    'ORDER BY ts_code, rk'
)
# 全市场约 5.4k 码：有 idx_top10_holders_ts_end_date 时单次 IN 约 0.35–0.5s（2026-06-18 本机）
FINA_TOP10_HOLDERS_SINGLE_QUERY_MAX = int(os.getenv('FINA_TOP10_HOLDERS_SINGLE_QUERY_MAX', '8000'))
FINA_TOP10_HOLDERS_CHUNK_SIZE = int(os.getenv('FINA_TOP10_HOLDERS_CHUNK_SIZE', '2000'))
FINA_TOP10_HOLDERS_PARALLEL_WORKERS = max(
    1, min(4, int(os.getenv('FINA_TOP10_HOLDERS_PARALLEL_WORKERS', '4')))
)
# 全市场批量 top3 映射进程内缓存（与 arturn 表 TTL 对齐；预热多期时复用同一份 map）
FINA_TOP10_HOLDERS_MAP_CACHE_TTL = float(os.getenv('FINA_TOP10_HOLDERS_MAP_CACHE_TTL', '300'))
FINA_TOP10_HOLDERS_BULK_CACHE_MIN = int(os.getenv('FINA_TOP10_HOLDERS_BULK_CACHE_MIN', '4000'))
FINA_TOP10_HOLDERS_MAP_CACHE_MAX = 2
_top10_holders_top3_map_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()


def _group_top10_holders_top3_rows(raw_rows: list, hasHoldChange: bool = False) -> dict[str, list]:
    by_code: dict[str, list] = {}
    for rr in raw_rows or []:
        if not rr or rr[0] is None:
            continue
        tsc = str(rr[0]).strip()
        hchg = rr[6] if hasHoldChange and len(rr) > 6 else None
        by_code.setdefault(tsc, []).append(
            (rr[1], rr[2], rr[3], rr[4], rr[5], hchg),
        )
    return by_code


def _fetch_top10_holders_top3_sql(part: list[str], conn, hasHoldChange: bool = False) -> list:
    if not part:
        return []
    ph = ','.join(['%s'] * len(part))
    tpl = _TOP10_HOLDERS_TOP3_SQL_WITH_CHANGE if hasHoldChange else _TOP10_HOLDERS_TOP3_SQL
    with conn.cursor() as cur:
        cur.execute(tpl.format(ph=ph), tuple(part))
        return cur.fetchall() or []


def _fetch_top10_holders_top3_map(codes: list[str], conn=None) -> dict[str, list]:
    """批量取最新期末 top3 股东；默认单次 IN 查全市场，超阈值再分块并行。"""
    if not codes:
        return {}
    hasHoldChange = False
    probeConn = conn
    ownProbe = False
    if probeConn is None:
        probeConn = pymysql.connect(**DB_CONFIG_TUSHARE)
        ownProbe = True
    try:
        hasHoldChange = _tushare_column_exists(probeConn, 'top10_holders', 'hold_change')
    except Exception:
        hasHoldChange = False
    finally:
        if ownProbe:
            probeConn.close()

    bulkCache = len(codes) >= FINA_TOP10_HOLDERS_BULK_CACHE_MIN
    cacheKey = ('top10_top3_bulk', hasHoldChange) if bulkCache else None
    if cacheKey is not None:
        cached = _small_ttl_cache_get(_top10_holders_top3_map_cache, cacheKey, time.time())
        if cached is not None:
            return cached

    if len(codes) <= FINA_TOP10_HOLDERS_SINGLE_QUERY_MAX:
        ownConn = conn is None
        dbConn = conn if conn is not None else pymysql.connect(**DB_CONFIG_TUSHARE)
        try:
            by_code = _group_top10_holders_top3_rows(
                _fetch_top10_holders_top3_sql(codes, dbConn, hasHoldChange),
                hasHoldChange,
            )
        finally:
            if ownConn:
                dbConn.close()
        if cacheKey is not None:
            _small_ttl_cache_put(
                _top10_holders_top3_map_cache,
                cacheKey,
                by_code,
                FINA_TOP10_HOLDERS_MAP_CACHE_TTL,
                FINA_TOP10_HOLDERS_MAP_CACHE_MAX,
            )
        return by_code

    chunks = [
        codes[i : i + FINA_TOP10_HOLDERS_CHUNK_SIZE]
        for i in range(0, len(codes), FINA_TOP10_HOLDERS_CHUNK_SIZE)
    ]

    def _fetchChunk(part: list[str]) -> dict[str, list]:
        dbConn = pymysql.connect(**DB_CONFIG_TUSHARE)
        try:
            return _group_top10_holders_top3_rows(
                _fetch_top10_holders_top3_sql(part, dbConn, hasHoldChange),
                hasHoldChange,
            )
        finally:
            dbConn.close()

    by_code: dict[str, list] = {}
    workers = min(FINA_TOP10_HOLDERS_PARALLEL_WORKERS, len(chunks))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for partial in ex.map(_fetchChunk, chunks):
            by_code.update(partial)
    if cacheKey is not None:
        _small_ttl_cache_put(
            _top10_holders_top3_map_cache,
            cacheKey,
            by_code,
            FINA_TOP10_HOLDERS_MAP_CACHE_TTL,
            FINA_TOP10_HOLDERS_MAP_CACHE_MAX,
        )
    return by_code


def _fina_arturn_attach_top10_holders_top3(conn, rows: list[dict]) -> None:
    """就地写入 holders_top3_pct、holders_top3_detail、holders_top3_end_date。

    口径：top10_holders 按 ts_code 取最新 end_date，在该期末快照内按 hold_ratio 降序取前 3，合计占比（%）；
    明细含 holder_name、holder_type、hold_amount、hold_ratio、hold_change（若库有该列）、end_date。
    """
    if not rows:
        return
    try:
        if not _tushare_column_exists(conn, 'top10_holders', 'ts_code'):
            return
        if not _tushare_column_exists(conn, 'top10_holders', 'hold_ratio'):
            return
    except Exception:
        return

    for r in rows:
        r['holders_top3_pct'] = None
        r['holders_top3_detail'] = []
        r['holders_top3_end_date'] = ''

    codes_uniq: list[str] = []
    seen: set[str] = set()
    for r in rows:
        tc = str(r.get('ts_code') or '').strip()
        if tc and tc not in seen:
            seen.add(tc)
            codes_uniq.append(tc)
    if not codes_uniq:
        return

    def _dt_iso(v: object) -> str:
        if v is None:
            return ''
        if hasattr(v, 'strftime'):
            try:
                return v.strftime('%Y-%m-%d')
            except Exception:
                return str(v)[:10]
        s = str(v).strip()
        return s[:10] if len(s) >= 10 else s

    def _to_float(v: object) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    try:
        by_code = _fetch_top10_holders_top3_map(codes_uniq, conn=conn)
    except Exception as e:
        logger.warning('fina_arturn attach top10_holders top3 跳过: %s', e)
        return

    for r in rows:
        tc = str(r.get('ts_code') or '').strip()
        lst = by_code.get(tc)
        if not lst:
            continue
        details: list[dict] = []
        ratio_sum = 0.0
        end_iso = ''
        for end_dt, hname, htype, hamt, hratio, hchg in lst:
            if not end_iso:
                end_iso = _dt_iso(end_dt)
            rf = _to_float(hratio)
            if rf is not None:
                ratio_sum += rf
            details.append(
                {
                    'holder_name': str(hname or '').strip(),
                    'holder_type': str(htype or '').strip(),
                    'hold_amount': _to_float(hamt),
                    'hold_ratio': rf,
                    'hold_change': _to_float(hchg),
                    'end_date': _dt_iso(end_dt),
                }
            )
        r['holders_top3_pct'] = round(ratio_sum, 6) if ratio_sum else None
        r['holders_top3_detail'] = details
        r['holders_top3_end_date'] = end_iso


def _fina_arturn_attach_stk_holdernumber_quarters(
    conn, rows: list[dict], quarter_periods: list[str]
) -> None:
    """就地写入 hn0..hn8（户）：stk_holdernumber.holder_num，与 contractPeriods 九季 end_date 对齐；同季多行取 ann_date 最大。"""
    for row in rows:
        for i in range(9):
            row[f'hn{i}'] = None
    if not rows:
        return
    if not _tushare_column_exists(conn, 'stk_holdernumber', 'holder_num'):
        return
    norm_slots: list[str | None] = []
    for p in (quarter_periods or [])[:9]:
        norm_slots.append(_normalize_end_date_yyyymmdd(p))
    periods_u = sorted({x for x in norm_slots if x})
    if not periods_u:
        return
    codes_seen: list[str] = []
    seen: set[str] = set()
    for r in rows:
        tc = str(r.get('ts_code') or '').strip()
        if tc and tc not in seen:
            seen.add(tc)
            codes_seen.append(tc)
    if not codes_seen:
        return
    dfmt = '%Y%m%d'
    by_pair: dict[tuple[str, str], int | None] = {}
    chunk_size = 400
    try:
        for i0 in range(0, len(codes_seen), chunk_size):
            part = codes_seen[i0 : i0 + chunk_size]
            ts_ph = ', '.join(['%s'] * len(part))
            pd_ph = ', '.join(['%s'] * len(periods_u))
            sql = (
                'SELECT sh.ts_code, DATE_FORMAT(sh.end_date, %s) AS edk, sh.holder_num '
                'FROM stock_tushare.stk_holdernumber sh '
                'INNER JOIN ( '
                '  SELECT ts_code, end_date, MAX(ann_date) AS ma '
                '  FROM stock_tushare.stk_holdernumber '
                f'  WHERE ts_code IN ({ts_ph}) '
                f'    AND DATE_FORMAT(end_date, %s) IN ({pd_ph}) '
                '  GROUP BY ts_code, end_date '
                ') z ON sh.ts_code = z.ts_code AND sh.end_date = z.end_date AND sh.ann_date = z.ma'
            )
            params = (dfmt, *part, dfmt, *periods_u)
            with conn.cursor() as cur:
                cur.execute(sql, params)
                raw = cur.fetchall() or []
            for rr in raw:
                if not rr or len(rr) < 3:
                    continue
                tsc = str(rr[0] or '').strip()
                edk = str(rr[1] or '').strip()
                hn = rr[2]
                if not tsc or len(edk) != 8:
                    continue
                if hn is None:
                    by_pair[(tsc, edk)] = None
                else:
                    try:
                        by_pair[(tsc, edk)] = int(round(float(hn)))
                    except (TypeError, ValueError):
                        by_pair[(tsc, edk)] = None
    except Exception as e:
        logger.warning('fina_arturn attach stk_holdernumber 九季跳过: %s', e)
        return
    for row in rows:
        tc = str(row.get('ts_code') or '').strip()
        if not tc:
            continue
        for idx, pk in enumerate(norm_slots):
            if not pk:
                continue
            row[f'hn{idx}'] = by_pair.get((tc, pk))


def _fina_arturn_attach_hn_qoq_low(rows: list[dict]) -> None:
    """股东人数：hn_qoq=从 hn0 起连续「人数环比下降」的季数；hn_low=hn0 是否为九季已有值中的最小（0/1）。"""
    for row in rows:
        row['hn_qoq'] = None
        row['hn_low'] = None
        h0 = row.get('hn0')
        if h0 is None:
            continue
        try:
            h0f = float(h0)
        except (TypeError, ValueError):
            continue
        present: list[float] = []
        for i in range(9):
            v = row.get(f'hn{i}')
            if v is not None:
                try:
                    present.append(float(v))
                except (TypeError, ValueError):
                    pass
        if not present:
            row['hn_low'] = None
        else:
            row['hn_low'] = 1 if h0f == min(present) else 0
        decl = 0
        prev = h0f
        for i in range(1, 9):
            curr = row.get(f'hn{i}')
            if curr is None:
                break
            try:
                cf = float(curr)
            except (TypeError, ValueError):
                break
            if prev < cf:
                decl += 1
                prev = cf
                continue
            break
        row['hn_qoq'] = decl


def _fina_arturn_attach_stock_basic_act(conn, rows: list[dict]) -> None:
    """为财报表每行就地写入 stock_basic.act_name、act_ent_type（固定资产弹窗顶栏回显；与序列接口同源列）。

    批量 IN 查询；缺列或失败时**不覆盖**行上已有值（主 SQL / fast_path 已与 list_date 同源带出 act 时不得被空串冲掉）。
    """
    if not rows:
        return
    codes_uniq: list[str] = []
    seen: set[str] = set()
    for r in rows:
        tc = str(r.get('ts_code') or '').strip()
        if tc and tc not in seen:
            seen.add(tc)
            codes_uniq.append(tc)
    if not codes_uniq:
        return
    ph = ','.join(['%s'] * len(codes_uniq))
    sql = (
        'SELECT ts_code, COALESCE(`act_name`,\'\'), COALESCE(`act_ent_type`,\'\') '
        f'FROM stock_tushare.stock_basic WHERE ts_code IN ({ph})'
    )
    mp: dict[str, tuple[str, str]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(codes_uniq))
            for rr in cur.fetchall() or []:
                if not rr or rr[0] is None:
                    continue
                tsc = str(rr[0]).strip()
                an = str(rr[1] or '').strip() if len(rr) > 1 else ''
                et = str(rr[2] or '').strip() if len(rr) > 2 else ''
                mp[tsc] = (an, et)
    except Exception as e:
        logger.warning('fina_arturn attach stock_basic act 跳过（保留主 SQL 已带出的 act_*）: %s', e)
        return
    for r in rows:
        tc = str(r.get('ts_code') or '').strip()
        pair = mp.get(tc, ('', ''))
        r['act_name'] = pair[0]
        r['act_ent_type'] = pair[1]


def _fina_arturn_attach_ar_receivable_pct(conn, rows: list[dict], anchor_yyyymmdd: str) -> None:
    """就地写入 ar_pct：应收占比(%) = accounts_receiv / TTM 单季营业总收入 × 100。
    同时写入 ttm_revenue_yi：最近四单季营业总收入之和（元）/1e8，供页面「营收」列展示（不显示单位）。
    同时写入 k_revenue_pct：当期合同负债（亿，k0）/ 营收 TTM（亿）×100，供合同负债区「营收%」列。"""
    if not rows:
        return
    for row in rows:
        row['k_revenue_pct'] = None
    has_inc = _tushare_column_exists(conn, 'income', 'total_revenue')
    has_bs = _tushare_column_exists(conn, 'balancesheet', 'accounts_receiv')
    if not has_inc or not has_bs:
        for row in rows:
            row['ar_pct'] = None
            row['ttm_revenue_yi'] = None
        return
    has_inc_rt = _tushare_column_exists(conn, 'income', 'report_type')
    has_bs_rt = _tushare_column_exists(conn, 'balancesheet', 'report_type')
    dkeys = _fina_ttm_income_date_keys(anchor_yyyymmdd)
    if not dkeys:
        for row in rows:
            row['ar_pct'] = None
            row['ttm_revenue_yi'] = None
            row['k_revenue_pct'] = None
        return
    ts_codes = [str(r['ts_code']) for r in rows if r.get('ts_code')]
    ar_map = _fina_batch_accounts_receiv(conn, ts_codes, anchor_yyyymmdd, has_bs_rt)
    rev_nested = _fina_batch_income_total_revenue(conn, ts_codes, dkeys, has_inc_rt)
    for row in rows:
        tc = row.get('ts_code')
        if not tc:
            row['ar_pct'] = None
            row['ttm_revenue_yi'] = None
            row['k_revenue_pct'] = None
            continue
        tcs = str(tc)
        ar = ar_map.get(tcs)
        rm = rev_nested.get(tcs, {})
        ttm = _fina_ttm_total_revenue(rm, anchor_yyyymmdd)
        if ttm is None:
            row['ttm_revenue_yi'] = None
            row['ar_pct'] = None
            row['k_revenue_pct'] = None
            continue
        row['ttm_revenue_yi'] = round(float(ttm) / 100_000_000.0, 2)
        k0 = row.get('k0')
        if k0 is not None and row['ttm_revenue_yi'] is not None:
            try:
                den = float(row['ttm_revenue_yi'])
                if den != 0.0:
                    row['k_revenue_pct'] = round(float(k0) / den * 100.0, 2)
            except (TypeError, ValueError):
                row['k_revenue_pct'] = None
        if ar is None or ttm == 0:
            row['ar_pct'] = None
        else:
            row['ar_pct'] = round((float(ar) / float(ttm)) * 100.0, 2)


_STK_FACTOR_PRO_MAX_D_CACHE: dict = {}
_STK_FACTOR_PRO_MAX_D_TTL = 60.0


def _fina_arturn_attach_stk_factor_mv_pe(rows: list[dict], conn_ext=None) -> None:
    """就地写入 pe_ttm、total_mv_yi、free_mv_yi：stk_factor_pro 取全表最新 trade_date 一行；
    total_mv 为万元→亿(/10000)；free_mv=free_share(万股)×rt_k.close(元/股)→万元再/10000=亿。

    性能：
    - 若调用方传入 conn_ext（已建立的 stock_tushare 连接），直接复用；避免每次 attach 都 pymysql.connect，
      节省握手/鉴权/TLS 开销。
    - MAX(trade_date) 走 60s TTL 缓存：stk_factor_pro 的新交易日仅日级别变动，1 分钟粒度对展示无影响。
    """
    for row in rows:
        row['pe_ttm'] = None
        row['total_mv_yi'] = None
        row['free_mv_yi'] = None
    if not rows:
        return
    ts_codes = list({str(r['ts_code']).strip() for r in rows if r.get('ts_code')})
    if not ts_codes:
        return
    conn_ts = conn_ext
    owned_conn_ts = False
    conn_w = None
    has_pe = False
    has_tmv = False
    has_fs = False
    if conn_ts is None:
        try:
            conn_ts = pymysql.connect(**DB_CONFIG_TUSHARE)
            owned_conn_ts = True
        except Exception as e:
            logger.warning('fina_arturn stk_factor_pro 连接失败，跳过 PE/市值列: %s', e)
            return
    try:
        has_pe = _tushare_column_exists(conn_ts, 'stk_factor_pro', 'pe_ttm')
        has_tmv = _tushare_column_exists(conn_ts, 'stk_factor_pro', 'total_mv')
        has_fs = _tushare_column_exists(conn_ts, 'stk_factor_pro', 'free_share')
        if not has_pe and not has_tmv and not has_fs:
            return
        import time as _t
        now = _t.monotonic()
        cache_hit = _STK_FACTOR_PRO_MAX_D_CACHE.get('max_d')
        if cache_hit and (now - cache_hit[0]) < _STK_FACTOR_PRO_MAX_D_TTL:
            max_d = cache_hit[1]
        else:
            max_d = None
            with conn_ts.cursor() as cur:
                cur.execute('SELECT MAX(trade_date) FROM stk_factor_pro')
                mx = cur.fetchone()
                if mx and mx[0] is not None:
                    max_d = mx[0]
            if max_d is not None:
                _STK_FACTOR_PRO_MAX_D_CACHE['max_d'] = (now, max_d)
        if max_d is None:
            return

        sel_cols = ['ts_code']
        if has_pe:
            sel_cols.append('pe_ttm')
        if has_tmv:
            sel_cols.append('total_mv')
        if has_fs:
            sel_cols.append('free_share')
        col_sql = ', '.join(sel_cols)
        sfp_map: dict[str, tuple] = {}
        chunk = 480
        for i in range(0, len(ts_codes), chunk):
            part = ts_codes[i : i + chunk]
            ph = ', '.join(['%s'] * len(part))
            sql = (
                f'SELECT {col_sql} FROM stk_factor_pro '
                f'WHERE trade_date = %s AND ts_code IN ({ph})'
            )
            with conn_ts.cursor() as cur:
                cur.execute(sql, (max_d, *part))
                for raw in cur.fetchall() or []:
                    if not raw:
                        continue
                    tc = str(raw[0]).strip() if raw[0] is not None else ''
                    if not tc:
                        continue
                    sfp_map[tc] = raw
    finally:
        if owned_conn_ts and conn_ts is not None:
            try:
                conn_ts.close()
            except Exception:
                pass

    close_map: dict[str, float] = {}
    if has_fs:
        try:
            conn_w = pymysql.connect(**DB_CONFIG)
            chunk = 480
            for i in range(0, len(ts_codes), chunk):
                part = ts_codes[i : i + chunk]
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
                        if not r2 or r2[0] is None:
                            continue
                        tcc = str(r2[0]).strip()
                        cv = r2[1]
                        if cv is None:
                            continue
                        try:
                            close_map[tcc] = float(cv)
                        except (TypeError, ValueError):
                            pass
        except Exception as e:
            logger.warning('fina_arturn rt_k close 批量查询失败，自由市值将不完整: %s', e)
        finally:
            if conn_w:
                conn_w.close()

    for row in rows:
        tc = str(row.get('ts_code') or '').strip()
        if not tc:
            continue
        raw = sfp_map.get(tc)
        if not raw:
            continue
        idx = 1
        pe_v = None
        tmv_v = None
        fs_v = None
        if has_pe:
            pe_v = raw[idx] if len(raw) > idx else None
            idx += 1
        if has_tmv:
            tmv_v = raw[idx] if len(raw) > idx else None
            idx += 1
        if has_fs:
            fs_v = raw[idx] if len(raw) > idx else None
        if has_pe and pe_v is not None:
            try:
                pf = float(pe_v)
                if pf == pf and abs(pf) < 1e12:
                    row['pe_ttm'] = round(pf, 2)
            except (TypeError, ValueError):
                pass
        if has_tmv and tmv_v is not None:
            try:
                row['total_mv_yi'] = round(float(tmv_v) / 10000.0, 2)
            except (TypeError, ValueError):
                pass
        if has_fs and fs_v is not None:
            cl = close_map.get(tc)
            if cl is not None:
                try:
                    # 万股 × 元/股 = 万元；万元 / 10000 = 亿
                    row['free_mv_yi'] = round(float(fs_v) * float(cl) / 10000.0, 2)
                except (TypeError, ValueError):
                    pass


def _fina_arturn_attach_free_float_ratio_pct(rows: list[dict]) -> None:
    """就地写入自由流通比(%)：free_mv_yi / total_mv_yi ×100，保留两位小数；缺总市值或≤0 时为 None。"""
    for row in rows:
        row['free_float_ratio_pct'] = None
        tm = row.get('total_mv_yi')
        fm = row.get('free_mv_yi')
        try:
            tmv = float(tm) if tm is not None else None
            fmv = float(fm) if fm is not None else None
        except (TypeError, ValueError):
            continue
        if tmv is None or fmv is None or tmv <= 0:
            continue
        row['free_float_ratio_pct'] = round((fmv / tmv) * 100.0, 2)


def _fina_arturn_attach_money_cap_ratio(rows: list[dict], conn, selected_end_date: str) -> None:
    """就地写入市值/货币(%)：fina_quarter_metrics.money_cap_opt / total_mv_yi ×100。"""
    for row in rows:
        row['money_cap_ratio_pct'] = None
    if not rows:
        return
    end_date = str(selected_end_date or '').strip()
    if not end_date or not _tushare_column_exists(conn, 'fina_quarter_metrics', 'money_cap_opt'):
        return

    has_update_flag = _tushare_column_exists(conn, 'fina_quarter_metrics', 'update_flag')
    has_rt = _tushare_column_exists(conn, 'fina_quarter_metrics', 'report_type')
    has_ann_date = _tushare_column_exists(conn, 'fina_quarter_metrics', 'ann_date')
    rt_order_sql = "CASE WHEN fq.report_type = 1 THEN 0 ELSE 1 END, " if has_rt else ""
    uf_order_sql = "CASE WHEN fq.update_flag = '1' THEN 0 ELSE 1 END, " if has_update_flag else ""
    ann_order_sql = "fq.ann_date DESC" if has_ann_date else "fq.ts_code ASC"
    ts_codes = [str(r.get('ts_code') or '').strip() for r in rows if str(r.get('ts_code') or '').strip()]
    if not ts_codes:
        return
    ph = ','.join(['%s'] * len(ts_codes))
    sql = (
        "SELECT x.ts_code, x.money_cap_opt "
        "FROM ("
        "  SELECT fq.ts_code, fq.money_cap_opt, "
        "         ROW_NUMBER() OVER ("
        "           PARTITION BY fq.ts_code, fq.end_date "
        "           ORDER BY "
        "             CASE WHEN fq.money_cap_opt IS NOT NULL THEN 0 ELSE 1 END, "
        + uf_order_sql
        + rt_order_sql
        + "             "
        + ann_order_sql
        + "         ) AS rn "
        "  FROM stock_tushare.fina_quarter_metrics fq "
        f"  WHERE DATE_FORMAT(fq.end_date, '%%Y%%m%%d') = %s AND fq.ts_code IN ({ph})"
        ") x "
        "WHERE x.rn = 1"
    )
    params = (end_date, *ts_codes)
    money_cap_map: dict[str, float] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for tc, mc in cur.fetchall() or []:
                code = str(tc or '').strip()
                if not code or mc is None:
                    continue
                try:
                    money_cap_map[code] = float(mc)
                except (TypeError, ValueError):
                    continue
    except Exception as e:
        logger.debug('fina_arturn money_cap_opt batch: %s', e)
        return

    missing_codes = [c for c in ts_codes if c not in money_cap_map]
    if missing_codes and _tushare_column_exists(conn, 'balancesheet', 'money_cap'):
        has_bs_rt = _tushare_column_exists(conn, 'balancesheet', 'report_type')
        has_trad_asset = _tushare_column_exists(conn, 'balancesheet', 'trad_asset')
        bs_rt_order_sql = "CASE WHEN bs.report_type = 1 THEN 0 ELSE 1 END, " if has_bs_rt else ""
        bs_uf_order_sql = "CASE WHEN bs.update_flag = '1' THEN 0 ELSE 1 END, " if _tushare_column_exists(conn, 'balancesheet', 'update_flag') else ""
        bs_ann_order_sql = "bs.ann_date DESC" if _tushare_column_exists(conn, 'balancesheet', 'ann_date') else "bs.ts_code ASC"
        ph_miss = ','.join(['%s'] * len(missing_codes))
        trad_asset_expr = "COALESCE(bs.trad_asset, 0)" if has_trad_asset else "0"
        bs_sql = (
            "SELECT y.ts_code, y.money_cap_opt "
            "FROM ("
            "  SELECT bs.ts_code, (COALESCE(bs.money_cap, 0) + "
            + trad_asset_expr +
            ") AS money_cap_opt, "
            "         ROW_NUMBER() OVER ("
            "           PARTITION BY bs.ts_code, bs.end_date "
            "           ORDER BY "
            "             CASE WHEN (bs.money_cap IS NOT NULL "
            + ("OR bs.trad_asset IS NOT NULL" if has_trad_asset else "")
            + ") THEN 0 ELSE 1 END, "
            + bs_uf_order_sql
            + bs_rt_order_sql
            + "             " + bs_ann_order_sql
            + "         ) AS rn "
            "  FROM stock_tushare.balancesheet bs "
            f"  WHERE DATE_FORMAT(bs.end_date, '%%Y%%m%%d') = %s AND bs.ts_code IN ({ph_miss})"
            ") y "
            "WHERE y.rn = 1"
        )
        try:
            with conn.cursor() as cur:
                cur.execute(bs_sql, (end_date, *missing_codes))
                for tc, mc in cur.fetchall() or []:
                    code = str(tc or '').strip()
                    if not code or code in money_cap_map or mc is None:
                        continue
                    try:
                        money_cap_map[code] = float(mc)
                    except (TypeError, ValueError):
                        continue
        except Exception as e:
            logger.debug('fina_arturn money_cap_opt fallback balancesheet: %s', e)

    for row in rows:
        code = str(row.get('ts_code') or '').strip()
        if not code:
            continue
        mc_raw = money_cap_map.get(code)
        if mc_raw is None:
            continue
        mc_yi = mc_raw / 100000000.0  # money_cap_opt 为元，换算到亿元后再与 total_mv_yi 同口径相除
        try:
            tmv = float(row.get('total_mv_yi'))
        except (TypeError, ValueError):
            continue
        if not np.isfinite(tmv) or tmv == 0:
            continue
        row['money_cap_ratio_pct'] = round((mc_yi / tmv) * 100.0, 1)


def _fina_arturn_attach_fqm_debt_ratios(rows: list[dict], conn, selected_end_date: str) -> None:
    """就地写入 fina_quarter_metrics：资产负债率 debt_to_assets、带息负债率 interestdebt_ratio（锚定当前表头报告期）。"""
    for row in rows:
        row['fqm_debt_to_assets'] = None
        row['fqm_interestdebt_ratio'] = None
    if not rows:
        return
    end_date = str(selected_end_date or '').strip()
    if not end_date:
        return
    has_dta = _tushare_column_exists(conn, 'fina_quarter_metrics', 'debt_to_assets')
    has_idr = _tushare_column_exists(conn, 'fina_quarter_metrics', 'interestdebt_ratio')
    if not has_dta and not has_idr:
        return
    has_update_flag = _tushare_column_exists(conn, 'fina_quarter_metrics', 'update_flag')
    has_rt = _tushare_column_exists(conn, 'fina_quarter_metrics', 'report_type')
    has_ann_date = _tushare_column_exists(conn, 'fina_quarter_metrics', 'ann_date')
    rt_order_sql = "CASE WHEN fq.report_type = 1 THEN 0 ELSE 1 END, " if has_rt else ""
    uf_order_sql = "CASE WHEN fq.update_flag = '1' THEN 0 ELSE 1 END, " if has_update_flag else ""
    ann_order_sql = "fq.ann_date DESC" if has_ann_date else "fq.ts_code ASC"
    sel_dta = 'fq.debt_to_assets' if has_dta else 'NULL'
    sel_idr = 'fq.interestdebt_ratio' if has_idr else 'NULL'
    ts_codes = [str(r.get('ts_code') or '').strip() for r in rows if str(r.get('ts_code') or '').strip()]
    if not ts_codes:
        return
    ph = ','.join(['%s'] * len(ts_codes))
    sql = (
        "SELECT x.ts_code, x.debt_to_assets, x.interestdebt_ratio "
        "FROM ("
        "  SELECT fq.ts_code, "
        + sel_dta + " AS debt_to_assets, "
        + sel_idr + " AS interestdebt_ratio, "
        "         ROW_NUMBER() OVER ("
        "           PARTITION BY fq.ts_code, fq.end_date "
        "           ORDER BY "
        "             CASE WHEN ("
        + ("fq.debt_to_assets IS NOT NULL OR fq.interestdebt_ratio IS NOT NULL" if has_dta and has_idr else
           ("fq.debt_to_assets IS NOT NULL" if has_dta else "fq.interestdebt_ratio IS NOT NULL"))
        + ") THEN 0 ELSE 1 END, "
        + uf_order_sql
        + rt_order_sql
        + "             "
        + ann_order_sql
        + "         ) AS rn "
        "  FROM stock_tushare.fina_quarter_metrics fq "
        f"  WHERE DATE_FORMAT(fq.end_date, '%%Y%%m%%d') = %s AND fq.ts_code IN ({ph})"
        ") x "
        "WHERE x.rn = 1"
    )
    params = (end_date, *ts_codes)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            raw = cur.fetchall() or []
    except Exception as e:
        logger.debug('fina_arturn fqm debt ratios batch: %s', e)
        return
    dta_map: dict[str, float] = {}
    idr_map: dict[str, float] = {}
    for r in raw:
        if not r or len(r) < 1:
            continue
        code = str(r[0] or '').strip()
        if not code:
            continue
        if has_dta and len(r) > 1 and r[1] is not None:
            try:
                dta_map[code] = float(r[1])
            except (TypeError, ValueError):
                pass
        if has_idr and len(r) > 2 and r[2] is not None:
            try:
                idr_map[code] = float(r[2])
            except (TypeError, ValueError):
                pass
    for row in rows:
        code = str(row.get('ts_code') or '').strip()
        if not code:
            continue
        if code in dta_map:
            try:
                row['fqm_debt_to_assets'] = round(float(dta_map[code]), 2)
            except (TypeError, ValueError):
                pass
        if code in idr_map:
            try:
                row['fqm_interestdebt_ratio'] = round(float(idr_map[code]), 2)
            except (TypeError, ValueError):
                pass


def _fina_arturn_attach_qfq_close_break_first_mode(
    conn,
    rows: list[dict],
    *,
    break_mode: str,
    key_prefix: str,
) -> None:
    """就地写入 stk_qfq_close_break 首次突破（timing=first）。"""
    field_suffixes = (
        'break_date',
        'break_free_mv_yi',
        'prior_max_throwback_5d',
        'post5_ret_pct',
        'post10_ret_pct',
        'post20_ret_pct',
        'post60_ret_pct',
        'peak_age_days',
        'prior_max_qfq',
        'gap_to_peak_pct',
        'break_pct',
        'upper_shadow_pct',
        'pct_chg',
        'vol_ratio20',
        'ff_tn5_60_ratio',
        'pre5_ret_pct',
        'pre120_ret_pct',
        'pre120_range_pct',
        'ma120_bias_pct',
        'avg_amt_5_yi',
        'avg_amt_20_yi',
        'avg_amt_60_yi',
        'ff_tn5_pct',
        'ff_tn20_pct',
        'ff_tn60_pct',
        'pre120_up7_cnt',
        'pre120_dn7_cnt',
    )
    null_keys = tuple(f'{key_prefix}_{s}' for s in field_suffixes)
    for row in rows:
        for k in null_keys:
            row[k] = None
    if not rows:
        return
    try:
        if not _tushare_column_exists(conn, 'stk_qfq_close_break', 'ts_code'):
            return
    except Exception:
        return

    codes_uniq: list[str] = []
    seen: set[str] = set()
    for r in rows:
        tc = str(r.get('ts_code') or '').strip()
        if tc and tc not in seen:
            seen.add(tc)
            codes_uniq.append(tc)
    if not codes_uniq:
        return

    def _date_iso(v: object) -> str:
        if v is None:
            return ''
        if hasattr(v, 'strftime'):
            try:
                return v.strftime('%Y-%m-%d')
            except Exception:
                return str(v)[:10]
        s = str(v).strip()
        return s[:10] if len(s) >= 10 else s

    def _to_float(v: object) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _to_int(v: object) -> int | None:
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    hasSignalMetrics = _tushare_column_exists(conn, 'stk_qfq_close_break', 'peak_age_days')
    hasFfTn560 = _tushare_column_exists(conn, 'stk_qfq_close_break', 'ff_turnover_5_60_ratio')
    hasAmt60 = _tushare_column_exists(conn, 'stk_qfq_close_break', 'avg_amt_60_yi')
    hasFf60 = _tushare_column_exists(conn, 'stk_qfq_close_break', 'avg_ff_turnover_60_pct')
    hasPre120ExtremeCnt = _tushare_column_exists(conn, 'stk_qfq_close_break', 'pre120_up7_cnt')
    hasPostRet = _tushare_column_exists(conn, 'stk_qfq_close_break', 'post5_ret_pct')
    hasBreakFreeMv = _tushare_column_exists(conn, 'stk_qfq_close_break', 'break_free_mv_yi')
    hasPriorMaxTb5 = _tushare_column_exists(conn, 'stk_qfq_close_break', 'prior_max_throwback_5d')
    hasGapToPeak = _tushare_column_exists(conn, 'stk_qfq_close_break', 'gap_to_peak_pct')
    select_cols = ['ts_code', 'break_date']
    if hasBreakFreeMv:
        select_cols.append('break_free_mv_yi')
    if hasPriorMaxTb5:
        select_cols.append('prior_max_throwback_5d')
    if hasPostRet:
        select_cols.extend(['post5_ret_pct', 'post10_ret_pct', 'post20_ret_pct', 'post60_ret_pct'])
    if hasSignalMetrics:
        select_cols.extend([
            'peak_age_days',
            'prior_max_qfq',
        ])
        if hasGapToPeak:
            select_cols.append('gap_to_peak_pct')
        select_cols.extend([
            'break_pct',
            'upper_shadow_pct',
            'pct_chg',
        ])
    select_cols.append('vol_ratio20')
    if hasFfTn560:
        select_cols.append('ff_turnover_5_60_ratio')
    select_cols.extend([
        'pre5_ret_pct',
        'pre120_ret_pct',
    ])
    if hasSignalMetrics:
        select_cols.extend(['pre120_range_pct', 'ma120_bias_pct'])
    select_cols.extend([
        'avg_amt_5_yi',
        'avg_amt_20_yi',
    ])
    if hasAmt60:
        select_cols.append('avg_amt_60_yi')
    select_cols.extend(['avg_ff_turnover_5_pct', 'avg_ff_turnover_20_pct'])
    if hasFf60:
        select_cols.append('avg_ff_turnover_60_pct')
    if hasPre120ExtremeCnt:
        select_cols.extend(['pre120_up7_cnt', 'pre120_dn7_cnt'])
    col_to_row_key: dict[str, str] = {
        'break_date': f'{key_prefix}_break_date',
        'break_free_mv_yi': f'{key_prefix}_break_free_mv_yi',
        'prior_max_throwback_5d': f'{key_prefix}_prior_max_throwback_5d',
        'post5_ret_pct': f'{key_prefix}_post5_ret_pct',
        'post10_ret_pct': f'{key_prefix}_post10_ret_pct',
        'post20_ret_pct': f'{key_prefix}_post20_ret_pct',
        'post60_ret_pct': f'{key_prefix}_post60_ret_pct',
        'peak_age_days': f'{key_prefix}_peak_age_days',
        'prior_max_qfq': f'{key_prefix}_prior_max_qfq',
        'gap_to_peak_pct': f'{key_prefix}_gap_to_peak_pct',
        'break_pct': f'{key_prefix}_break_pct',
        'upper_shadow_pct': f'{key_prefix}_upper_shadow_pct',
        'pct_chg': f'{key_prefix}_pct_chg',
        'vol_ratio20': f'{key_prefix}_vol_ratio20',
        'ff_turnover_5_60_ratio': f'{key_prefix}_ff_tn5_60_ratio',
        'pre5_ret_pct': f'{key_prefix}_pre5_ret_pct',
        'pre120_ret_pct': f'{key_prefix}_pre120_ret_pct',
        'pre120_range_pct': f'{key_prefix}_pre120_range_pct',
        'ma120_bias_pct': f'{key_prefix}_ma120_bias_pct',
        'avg_amt_5_yi': f'{key_prefix}_avg_amt_5_yi',
        'avg_amt_20_yi': f'{key_prefix}_avg_amt_20_yi',
        'avg_amt_60_yi': f'{key_prefix}_avg_amt_60_yi',
        'avg_ff_turnover_5_pct': f'{key_prefix}_ff_tn5_pct',
        'avg_ff_turnover_20_pct': f'{key_prefix}_ff_tn20_pct',
        'avg_ff_turnover_60_pct': f'{key_prefix}_ff_tn60_pct',
        'pre120_up7_cnt': f'{key_prefix}_pre120_up7_cnt',
        'pre120_dn7_cnt': f'{key_prefix}_pre120_dn7_cnt',
    }
    by_code: dict[str, tuple] = {}
    chunk = 480
    sql = (
        'SELECT ' + ', '.join(select_cols)
        + ' FROM stk_qfq_close_break '
        "WHERE break_mode = %s AND timing = 'first' AND ts_code IN ({ph})"
    )
    try:
        with conn.cursor() as cur:
            for i in range(0, len(codes_uniq), chunk):
                part = codes_uniq[i : i + chunk]
                ph = ', '.join(['%s'] * len(part))
                cur.execute(sql.format(ph=ph), [break_mode, *part])
                for raw in cur.fetchall() or []:
                    if not raw or len(raw) < 1:
                        continue
                    tc = str(raw[0] or '').strip()
                    if tc:
                        by_code[tc] = raw
    except Exception as e:
        logger.warning(
            'fina_arturn attach stk_qfq_close_break %s/first 跳过: %s',
            break_mode,
            e,
        )
        return

    for row in rows:
        tc = str(row.get('ts_code') or '').strip()
        raw = by_code.get(tc)
        if not raw:
            continue
        for col_name in select_cols[1:]:
            row_key = col_to_row_key.get(col_name)
            if not row_key:
                continue
            col_idx = select_cols.index(col_name)
            val = raw[col_idx] if len(raw) > col_idx else None
            if col_name == 'break_date':
                row[row_key] = _date_iso(val)
            elif col_name in ('pre120_up7_cnt', 'pre120_dn7_cnt', 'peak_age_days', 'prior_max_throwback_5d'):
                row[row_key] = _to_int(val)
            else:
                row[row_key] = _to_float(val)


def _fina_arturn_attach_last_break_flag(
    conn,
    rows: list[dict],
    break_mode: str,
    high_key: str,
    peak_age_key: str,
) -> None:
    """就地写入 last_*_high / last_*_peak_age_days：指定 break_mode 的 last 最新 break_date 行。"""
    for row in rows:
        row[high_key] = 0
        row[peak_age_key] = None
    if not rows:
        return
    try:
        if not _tushare_column_exists(conn, 'stk_qfq_close_break', 'ts_code'):
            return
    except Exception:
        return

    def _to_int(v: object) -> int | None:
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(break_date)
                FROM stk_qfq_close_break
                WHERE break_mode = %s AND timing = 'last'
                """,
                (break_mode,),
            )
            mx_row = cur.fetchone()
            anchor = mx_row[0] if mx_row else None
            if anchor is None:
                return
            cur.execute(
                """
                SELECT ts_code, has_break, peak_age_days
                FROM stk_qfq_close_break
                WHERE break_mode = %s
                  AND timing = 'last'
                  AND break_date = %s
                """,
                (break_mode, anchor),
            )
            by_code: dict[str, tuple] = {}
            for r in cur.fetchall() or []:
                if not r or not r[0]:
                    continue
                by_code[str(r[0]).strip()] = r
        for row in rows:
            tc = str(row.get('ts_code') or '').strip()
            if not tc:
                continue
            hit = by_code.get(tc)
            if not hit:
                continue
            if int(hit[1] or 0) == 1:
                row[high_key] = 1
            row[peak_age_key] = _to_int(hit[2])
    except Exception as e:
        logger.warning('fina_arturn attach %s 跳过: %s', high_key, e)


def _fina_arturn_attach_last_his_high_flag(conn, rows: list[dict]) -> None:
    """就地写入 last_his_high / last_his_peak_age_days：his/last 最新 break_date 行。"""
    _fina_arturn_attach_last_break_flag(
        conn, rows, 'his', 'last_his_high', 'last_his_peak_age_days',
    )


def _fina_arturn_attach_last_850_high_flag(conn, rows: list[dict]) -> None:
    """就地写入 last_850_high / last_850_peak_age_days：850/last 最新 break_date 行。"""
    _fina_arturn_attach_last_break_flag(
        conn, rows, '850', 'last_850_high', 'last_850_peak_age_days',
    )


def _fina_arturn_attach_qfq_close_break_first_his(conn, rows: list[dict]) -> None:
    """就地写入 stk_qfq_close_break 历史首次突破（break_mode=his, timing=first）。"""
    _fina_arturn_attach_qfq_close_break_first_mode(
        conn, rows, break_mode='his', key_prefix='first_his',
    )


def _fina_arturn_attach_qfq_close_break_first_850(conn, rows: list[dict]) -> None:
    """就地写入 stk_qfq_close_break 850 首次突破（break_mode=850, timing=first）。"""
    _fina_arturn_attach_qfq_close_break_first_mode(
        conn, rows, break_mode='850', key_prefix='first_850',
    )


# 「当前价位置」分母列 → (row key, 口径)；顺序即前端列序（先三档最高、再三档最低）
# 口径 pct = (当前价/极值−1)×100；mult = 当前价/极值（倍数）。
# 历史最低走 mult：前复权历史低点对老股票是上市初期几毛钱，百分比会到数万（如 36824%），倍数（369x）才可读。
FINA_PRICE_RANGE_SPECS: tuple[tuple[str, str, str], ...] = (
    ('h_high', 'pct_vs_h_high', 'pct'),
    ('high_850', 'pct_vs_high_850', 'pct'),
    ('high_250', 'pct_vs_high_250', 'pct'),
    ('h_low', 'mult_vs_h_low', 'mult'),
    ('low_850', 'pct_vs_low_850', 'pct'),
    ('low_250', 'pct_vs_low_250', 'pct'),
)
_STK_FACTOR_MA_MAX_D_CACHE: dict = {}
_STK_FACTOR_MA_MAX_D_TTL = 60.0


def _fina_arturn_sfp_max_trade_date(conn):
    """stk_factor_pro 最新交易日（60s TTL）。该表有 trade_date 前导索引，MAX 走索引近乎零成本。"""
    import time as _t
    now = _t.monotonic()
    hit = _STK_FACTOR_PRO_MAX_D_CACHE.get('max_d')
    if hit and (now - hit[0]) < _STK_FACTOR_PRO_MAX_D_TTL:
        return hit[1]
    max_d = None
    with conn.cursor() as cur:
        cur.execute('SELECT MAX(trade_date) FROM stk_factor_pro')
        mx = cur.fetchone()
        if mx and mx[0] is not None:
            max_d = mx[0]
    if max_d is not None:
        _STK_FACTOR_PRO_MAX_D_CACHE['max_d'] = (now, max_d)
    return max_d


def _fina_arturn_ma_max_trade_date(conn):
    """stk_factor_ma 最新交易日（60s TTL）。仅在 ma 落后于 pro（maema 未跑完）时才调用：
    该表主键为 (ts_code, trade_date)，无 trade_date 前导索引，MAX 需全索引扫描（实测 ~1.8s）。"""
    import time as _t
    now = _t.monotonic()
    hit = _STK_FACTOR_MA_MAX_D_CACHE.get('max_d')
    if hit and (now - hit[0]) < _STK_FACTOR_MA_MAX_D_TTL:
        return hit[1]
    max_d = None
    with conn.cursor() as cur:
        cur.execute('SELECT MAX(trade_date) FROM stk_factor_ma')
        mx = cur.fetchone()
        if mx and mx[0] is not None:
            max_d = mx[0]
    if max_d is not None:
        _STK_FACTOR_MA_MAX_D_CACHE['max_d'] = (now, max_d)
    return max_d


def _fina_arturn_fetch_price_range_rows(conn, ts_codes: list[str], anchor) -> list:
    """按锚定交易日取 stk_factor_ma 极值 + stock_wind.rt_k 当前价。

    必须带 ts_code IN：stk_factor_ma 主键 (ts_code, trade_date)，带 IN 走主键（实测 5528 只 0.085s）；
    若退化为仅按 trade_date 过滤会走 skip scan 扫 187 万行（实测 43.9s）。
    DATE(trade_time) >= anchor 剔除 rt_k 里退市/长停标的的陈旧快照，避免算出虚假暴跌。
    """
    ph = ','.join(['%s'] * len(ts_codes))
    sql = (
        'SELECT m.ts_code, k.close, '
        'm.h_high, m.high_850, m.high_250, m.h_low, m.low_850, m.low_250 '
        'FROM stk_factor_ma m '
        'JOIN stock_wind.rt_k k ON k.ts_code = m.ts_code '
        'WHERE m.trade_date = %s AND DATE(k.trade_time) >= %s '
        f'AND m.ts_code IN ({ph})'
    )
    with conn.cursor() as cur:
        cur.execute(sql, [anchor, anchor, *ts_codes])
        return list(cur.fetchall() or [])


def _fina_arturn_attach_price_range_pct(conn, rows: list[dict]) -> None:
    """就地写入 cur_price 与 6 个「当前价距各档高低点%/倍数」列。

    分子：stock_wind.rt_k.close（未复权实时价；盘中为实时、盘后即当日收盘）。
    分母：stk_factor_ma 锚定日的 h_high/high_850/high_250/h_low/low_850/low_250
      （前复权盘中极值；250/850 为最近 N 条 rolling，非日历窗）。
    同基准：ma 的 qfq 以该表最新交易日为锚，该日 close_qfq == 未复权 close，故 rt_k.close
      可直接与极值相除，勿再乘 close_qfq/pre_close（那会把「当日涨幅」误算进距高点%）。
    """
    for row in rows:
        row['cur_price'] = None
        for _, key, _mode in FINA_PRICE_RANGE_SPECS:
            row[key] = None
    if not rows:
        return
    ts_codes = list({str(r['ts_code']).strip() for r in rows if r.get('ts_code')})
    if not ts_codes:
        return
    try:
        if not _tushare_column_exists(conn, 'stk_factor_ma', 'h_high'):
            return
    except Exception:
        return
    try:
        anchor = _fina_arturn_sfp_max_trade_date(conn)
        if anchor is None:
            return
        ma_rows = _fina_arturn_fetch_price_range_rows(conn, ts_codes, anchor)
        if not ma_rows:
            # ma 落后于 pro（maema 当日未跑完）：回退到 ma 自身最新日，避免整组列静默全空
            fallback = _fina_arturn_ma_max_trade_date(conn)
            if fallback is None or fallback == anchor:
                return
            logger.warning(
                'fina_arturn price_range: stk_factor_ma 落后于 stk_factor_pro（%s < %s），回退至 ma 最新日',
                fallback, anchor,
            )
            ma_rows = _fina_arturn_fetch_price_range_rows(conn, ts_codes, fallback)
            if not ma_rows:
                return

        by_code: dict[str, tuple] = {}
        for r in ma_rows:
            if not r or not r[0]:
                continue
            by_code[str(r[0]).strip()] = r

        for row in rows:
            tc = str(row.get('ts_code') or '').strip()
            hit = by_code.get(tc) if tc else None
            if not hit:
                continue
            cur_close = toFloatOrNone(hit[1])
            if cur_close is None or cur_close <= 0:
                continue
            row['cur_price'] = round(cur_close, 2)
            for idx, (_, key, mode) in enumerate(FINA_PRICE_RANGE_SPECS):
                denom = toFloatOrNone(hit[2 + idx])
                if denom is None or denom <= 0:
                    continue
                if mode == 'mult':
                    row[key] = round(cur_close / denom, 2)
                else:
                    row[key] = round((cur_close / denom - 1.0) * 100.0, 2)
    except Exception as e:
        logger.warning('fina_arturn attach price_range_pct 跳过: %s', e)


def _fina_arturn_normalize_ann_date_ymd(raw) -> str | None:
    """公告日规范为 YYYY-MM-DD；支持 YYYYMMDD / YYYY-MM-DD / date/datetime。"""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    s = str(raw).strip()
    if not s:
        return None
    digits = s.replace('-', '').replace('/', '')[:8]
    if len(digits) != 8 or not digits.isdigit():
        return None
    return f'{digits[:4]}-{digits[4:6]}-{digits[6:8]}'


def _fina_arturn_fetch_pre_ann_close_qfq(
    conn, pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], float]:
    """批量取 (ts_code, ann_ymd) → 公告日前最近交易日 close_qfq。

    按公告日分组：同日股票共用一次窗口查询（ROW_NUMBER），避免 5500 次相关子查询。
    先限定公告日前 FINA_PRE_ANN_WINDOW_DAYS 天（窗口内有行则最近一行必在窗口内，结果与无下限等价），
    窗口内无行的少数股票（长停牌/公告早于上市）再无下限补查；无下限时每批需排序全历史约 300 万行。
    """
    out: dict[tuple[str, str], float] = {}
    if not pairs:
        return out
    byAnn: dict[str, list[str]] = defaultdict(list)
    for ts_code, annYmd in pairs:
        byAnn[annYmd].append(ts_code)
    with conn.cursor() as cur:
        for annYmd, codes in byAnn.items():
            uniq = list(dict.fromkeys(codes))
            try:
                lo = (date.fromisoformat(annYmd) - timedelta(days=FINA_PRE_ANN_WINDOW_DAYS)).isoformat()
            except ValueError:
                lo = None
            _fina_arturn_pre_ann_close_qfq_chunks(cur, annYmd, uniq, lo, out)
            if lo is None:
                continue
            rest = [c for c in uniq if (c, annYmd) not in out]
            if rest:
                _fina_arturn_pre_ann_close_qfq_chunks(cur, annYmd, rest, None, out)
    return out


FINA_PRE_ANN_WINDOW_DAYS = 60


def _fina_arturn_pre_ann_close_qfq_chunks(
    cur, annYmd: str, codes: list[str], lo: str | None, out: dict[tuple[str, str], float],
) -> None:
    """按 800 只一批取 trade_date < annYmd（且 >= lo，若给定）的最近一行 close_qfq，写入 out。"""
    chunkSize = 800
    loSql = ' AND trade_date >= %s' if lo else ''
    for i in range(0, len(codes), chunkSize):
        chunk = codes[i:i + chunkSize]
        ph = ','.join(['%s'] * len(chunk))
        sql = (
            'SELECT ts_code, close_qfq FROM ('
            '  SELECT ts_code, close_qfq,'
            '    ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn'
            '  FROM stk_factor_pro'
            f'  WHERE trade_date < %s{loSql} AND close_qfq IS NOT NULL AND close_qfq > 0'
            f'    AND ts_code IN ({ph})'
            ') z WHERE rn = 1'
        )
        cur.execute(sql, [annYmd, *([lo] if lo else []), *chunk])
        for r in cur.fetchall() or ():
            if not r or not r[0]:
                continue
            cq = toFloatOrNone(r[1])
            if cq is None or cq <= 0:
                continue
            out[(str(r[0]).strip(), annYmd)] = float(cq)


def _fina_arturn_attach_pct_since_ann(conn, rows: list[dict]) -> None:
    """就地写入 pct_since_ann：所选报告期主行公告日 a0 前一交易日 close_qfq → rt_k 现价。

    公式：(rt_k.close / close_qfq_pre_ann − 1)×100。
    分子用未复权实时价；分母为前复权收盘。qfq 以最新交易日为锚时最新日 close_qfq==close，
    故与现价同基准；切换报告期时 a0 变化，基数日随之变化。
    """
    for row in rows:
        row['pct_since_ann'] = None
    if not rows:
        return
    pairs: list[tuple[str, str]] = []
    annByCode: dict[str, str] = {}
    for row in rows:
        tc = str(row.get('ts_code') or '').strip()
        if not tc:
            continue
        annYmd = _fina_arturn_normalize_ann_date_ymd(row.get('a0'))
        if not annYmd:
            continue
        annByCode[tc] = annYmd
        pairs.append((tc, annYmd))
    if not pairs:
        return
    try:
        anchor = _fina_arturn_sfp_max_trade_date(conn)
        if anchor is None:
            return
        codes = list(annByCode.keys())
        # 现价：优先已由 price_range 写入的 cur_price；并行时可能尚未写入，再查 rt_k
        curByCode: dict[str, float] = {}
        for row in rows:
            tc = str(row.get('ts_code') or '').strip()
            if tc not in annByCode:
                continue
            cp = toFloatOrNone(row.get('cur_price'))
            if cp is not None and cp > 0:
                curByCode[tc] = float(cp)
        missing = [c for c in codes if c not in curByCode]
        if missing:
            chunkSize = 800
            with conn.cursor() as cur:
                for i in range(0, len(missing), chunkSize):
                    chunk = missing[i:i + chunkSize]
                    ph = ','.join(['%s'] * len(chunk))
                    sql = (
                        'SELECT ts_code, close FROM stock_wind.rt_k '
                        f'WHERE DATE(trade_time) >= %s AND ts_code IN ({ph})'
                    )
                    cur.execute(sql, [anchor, *chunk])
                    for r in cur.fetchall() or ():
                        if not r or not r[0]:
                            continue
                        cl = toFloatOrNone(r[1])
                        if cl is None or cl <= 0:
                            continue
                        curByCode[str(r[0]).strip()] = float(cl)
        baseMap = _fina_arturn_fetch_pre_ann_close_qfq(conn, pairs)
        for row in rows:
            tc = str(row.get('ts_code') or '').strip()
            annYmd = annByCode.get(tc)
            if not annYmd:
                continue
            cur = curByCode.get(tc)
            base = baseMap.get((tc, annYmd))
            if cur is None or base is None or base <= 0:
                continue
            row['pct_since_ann'] = round((cur / base - 1.0) * 100.0, 2)
    except Exception as e:
        logger.warning('fina_arturn attach pct_since_ann 跳过: %s', e)


def _fina_arturn_collect_fi_main_candidates(
    conn,
    ts_codes: list[str] | None,
    quarter_periods: list[str],
    *,
    has_fi_rt: bool,
) -> dict:
    """一次扫完主 SQL 里原来 4 组 fi 子查询（fi_turn / fi_gpm / fi_ocf_opincome / fi_ocf_or）需要的候选行。

    业务列：arturn_days、roe_dt、grossprofit_margin、q_gsprofit_margin、ocf_to_opincome、ocf_to_or。
    扫描范围：`quarter_periods`（通常 4 期）× 可选 ts_codes 白名单。ts_codes 为 None 或空时不带
    ts_code 过滤（base 子查询尚未给出 ts_code 时的降级通道）。

    选行逻辑（对齐原 4 组 SQL 的 ORDER BY，以 _fina_pick_*_main 在 Python 侧实现）：
      fi_turn：优先 `arturn_days IS NOT NULL`，否则回退全池（金融股 p 可空、roe 仍可取），再按 update_flag='1' > report_type=1 > ann_date DESC
      fi_gpm：`COALESCE(grossprofit_margin, q_gsprofit_margin) IS NOT NULL` 优先，再 update_flag ...
      fi_ocf_opincome：`ocf_to_opincome IS NOT NULL` 优先，再 update_flag ...
      fi_ocf_or：`ocf_to_or IS NOT NULL` 优先，再 update_flag ...

    返回 {(ts_code, end_date): [dict, ...]}，按 ann_date DESC 预排。失败返回 {}。
    """
    if not quarter_periods:
        return {}
    sel_cols = [
        'ts_code', 'end_date', 'update_flag', 'ann_date',
        'arturn_days', 'roe_dt',
        'grossprofit_margin', 'q_gsprofit_margin',
        'ocf_to_opincome', 'ocf_to_or',
    ]
    if has_fi_rt:
        sel_cols.append('report_type')
    col_sql = ', '.join(f'fi.{c}' for c in sel_cols)
    pd_ph = ', '.join(['%s'] * len(quarter_periods))
    if ts_codes:
        ts_ph = ', '.join(['%s'] * len(ts_codes))
        sql = (
            f'SELECT {col_sql} '
            f'FROM stock_tushare.fina_indicator fi '
            f'WHERE fi.end_date IN ({pd_ph}) AND fi.ts_code IN ({ts_ph})'
        )
        params = tuple(list(quarter_periods) + list(ts_codes))
    else:
        sql = (
            f'SELECT {col_sql} '
            f'FROM stock_tushare.fina_indicator fi '
            f'WHERE fi.end_date IN ({pd_ph})'
        )
        params = tuple(quarter_periods)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            raw = cur.fetchall() or []
    except Exception as e:
        logger.warning('fi_main_candidates 合并扫描失败，降级为原主 SQL: %s', e)
        return {}
    cands: dict = {}
    for r in raw:
        if not r:
            continue
        d = dict(zip(sel_cols, r))
        tc_raw = d.get('ts_code')
        tc = str(tc_raw).strip() if tc_raw is not None else ''
        ed = _normalize_end_date_yyyymmdd(d.get('end_date'))
        if not tc or not ed:
            continue
        d['ts_code'] = tc
        d['end_date'] = ed
        uf = d.get('update_flag')
        d['update_flag'] = str(uf) if uf is not None else ''
        if has_fi_rt:
            rt_v = d.get('report_type')
            try:
                d['report_type'] = int(rt_v) if rt_v is not None else None
            except (TypeError, ValueError):
                d['report_type'] = None
        ann = d.get('ann_date')
        if ann is None:
            d['ann_date'] = ''
        else:
            s = str(ann)
            if '-' in s and len(s) >= 10:
                s = s[:10].replace('-', '')
            d['ann_date'] = s
        cands.setdefault((tc, ed), []).append(d)
    for key in cands:
        cands[key].sort(key=lambda x: x.get('ann_date') or '', reverse=True)
    return cands


def _fina_arturn_collect_bs_candidates(
    conn,
    ts_codes: list[str] | None,
    contract_periods: list[str],
    *,
    has_bs_rt: bool,
    has_adv: bool,
) -> dict:
    """一次扫完原主 SQL bs_contract 子查询（9 期 × balancesheet，contract_liab / fix_assets_total / adv_receipts）。

    选行优先级（对齐原 SQL）：
      1. `CASE WHEN contract_liab IS NOT NULL [OR adv_receipts IS NOT NULL] THEN 0 ELSE 1 END`
      2. update_flag='1'
      3. report_type=1（若 has_bs_rt）
      4. ann_date DESC

    返回 {(ts_code, end_date): [dict, ...]}；失败返回 {}。
    """
    if not contract_periods:
        return {}
    sel_cols = [
        'ts_code', 'end_date', 'update_flag', 'ann_date',
        'contract_liab', 'fix_assets_total',
    ]
    if has_adv:
        sel_cols.append('adv_receipts')
    if has_bs_rt:
        sel_cols.append('report_type')
    col_sql = ', '.join(f'bs.{c}' for c in sel_cols)
    pd_ph = ', '.join(['%s'] * len(contract_periods))
    where_tail = (
        '(bs.contract_liab IS NOT NULL OR bs.adv_receipts IS NOT NULL '
        'OR bs.fix_assets_total IS NOT NULL)'
    ) if has_adv else (
        '(bs.contract_liab IS NOT NULL OR bs.fix_assets_total IS NOT NULL)'
    )
    if ts_codes:
        ts_ph = ', '.join(['%s'] * len(ts_codes))
        sql = (
            f'SELECT {col_sql} '
            f'FROM stock_tushare.balancesheet bs '
            f'WHERE bs.end_date IN ({pd_ph}) AND bs.ts_code IN ({ts_ph}) '
            f'  AND {where_tail}'
        )
        params = tuple(list(contract_periods) + list(ts_codes))
    else:
        sql = (
            f'SELECT {col_sql} '
            f'FROM stock_tushare.balancesheet bs '
            f'WHERE bs.end_date IN ({pd_ph}) '
            f'  AND {where_tail}'
        )
        params = tuple(contract_periods)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            raw = cur.fetchall() or []
    except Exception as e:
        logger.warning('bs_candidates 合并扫描失败，降级为原主 SQL: %s', e)
        return {}
    cands: dict = {}
    for r in raw:
        if not r:
            continue
        d = dict(zip(sel_cols, r))
        tc_raw = d.get('ts_code')
        tc = str(tc_raw).strip() if tc_raw is not None else ''
        ed = _normalize_end_date_yyyymmdd(d.get('end_date'))
        if not tc or not ed:
            continue
        d['ts_code'] = tc
        d['end_date'] = ed
        uf = d.get('update_flag')
        d['update_flag'] = str(uf) if uf is not None else ''
        if has_bs_rt:
            rt_v = d.get('report_type')
            try:
                d['report_type'] = int(rt_v) if rt_v is not None else None
            except (TypeError, ValueError):
                d['report_type'] = None
        ann = d.get('ann_date')
        if ann is None:
            d['ann_date'] = ''
        else:
            s = str(ann)
            if '-' in s and len(s) >= 10:
                s = s[:10].replace('-', '')
            d['ann_date'] = s
        cands.setdefault((tc, ed), []).append(d)
    for key in cands:
        cands[key].sort(key=lambda x: x.get('ann_date') or '', reverse=True)
    return cands


def _fina_pick_with_pref_fn(cands_for_key: list[dict], pref_fn, has_rt: bool) -> dict | None:
    """与 _fina_pick_from_cands 同语义，但业务优先级由 `pref_fn(dict)->0/1` 提供，用于 COALESCE 口径。

    排序键：pref_fn -> update_flag='1' -> report_type=1（若 has_rt）-> ann_date DESC（预排保证）。
    """
    if not cands_for_key:
        return None

    def _k(d: dict) -> tuple:
        key = [pref_fn(d) if pref_fn is not None else 0]
        key.append(0 if d.get('update_flag') == '1' else 1)
        if has_rt:
            key.append(0 if d.get('report_type') == 1 else 1)
        return tuple(key)

    return min(cands_for_key, key=_k)


def _fina_arturn_fast_path(
    conn,
    selected_end_date: str,
    periods: list[str],
    contract_periods: list[str],
    *,
    has_fi_rt: bool,
    has_bs_rt: bool,
    has_adv: bool,
    sw_join_sql: str,
) -> list[dict] | None:
    """B1 主 SQL 瘦身的快速路径：base SQL 只查 ts_code/sec_name/sw_name/list_date，
    其余字段由 2 次合并 collect（fi 主 4 列 + bs 9 期）在 Python 侧填充。

    任意一步失败时：抛出异常让外层降级回原大 SQL；调用方负责 logger.warning 并切回旧路径。

    返回值：已填好 a0/roe0-3/p0-3/g0-3/c0-3/o0-3/k0-8/f0-8 并按 p0 ASC, ts_code ASC 排序的行列表。
    调用方随后照旧跑 `_fina_arturn_attach_*` 系列 attach 其他列（r0-8/u0-8 等）。
    """
    fi_rt15 = (
        '               CASE WHEN fi.report_type = 1 THEN 0 ELSE 1 END, '
        if has_fi_rt else ''
    )
    # 瘦身 base SQL：只保留 base / rt_k 筛选 / rt_name / sw_name / list_date
    rt_codes_tuple = _get_cached_rt_k_ts_codes_tuple(conn)
    if rt_codes_tuple is not None and len(rt_codes_tuple) == 0:
        return []
    join_rt_k = ""
    where_in_rt_k = ""
    base_extra_params: tuple = ()
    if rt_codes_tuple is not None:
        rt_ph = ','.join(['%s'] * len(rt_codes_tuple))
        where_in_rt_k = f"WHERE base.ts_code IN ({rt_ph}) "
        base_extra_params = tuple(rt_codes_tuple)
    else:
        join_rt_k = (
            "INNER JOIN (SELECT DISTINCT ts_code FROM stock_wind.rt_k) rt_k_codes "
            "  ON rt_k_codes.ts_code = base.ts_code "
        )
    base_sql = (
        "SELECT base.ts_code, "
        "  COALESCE(rt_name.name, '') AS sec_name, "
        "  COALESCE(rt_sw_map.sw_name, '') AS sw_name, "
        "  MAX(sb.list_date) AS list_date, "
        "  MAX(sb.act_name) AS act_name, "
        "  MAX(sb.act_ent_type) AS act_ent_type "
        "FROM ("
        "  SELECT DISTINCT x.ts_code "
        "  FROM ("
        "    SELECT fi.ts_code, fi.end_date, "
        "           ROW_NUMBER() OVER ("
        "             PARTITION BY fi.ts_code, fi.end_date "
        "             ORDER BY "
        "               CASE WHEN fi.update_flag = '1' THEN 0 ELSE 1 END, "
        + fi_rt15 +
        "               fi.ann_date DESC"
        "           ) AS rn "
        "    FROM stock_tushare.fina_indicator fi "
        "    WHERE fi.end_date = %s"
        "  ) x "
        "  WHERE x.rn = 1"
        ") base "
        + join_rt_k +
        "LEFT JOIN ("
        "  SELECT z.ts_code, z.name "
        "  FROM ("
        "    SELECT ts_code, name, "
        "      ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_time DESC) AS rn "
        "    FROM stock_wind.rt_k "
        "    WHERE name IS NOT NULL AND name <> ''"
        "  ) z "
        "  WHERE z.rn = 1"
        ") rt_name ON rt_name.ts_code = base.ts_code "
        + sw_join_sql +
        "LEFT JOIN ("
        "  SELECT x.ts_code, x.list_date, x.act_name, x.act_ent_type "
        "  FROM ("
        "    SELECT sb.ts_code, sb.list_date, "
        "      COALESCE(sb.act_name, '') AS act_name, "
        "      COALESCE(sb.act_ent_type, '') AS act_ent_type, "
        "      ROW_NUMBER() OVER ("
        "        PARTITION BY sb.ts_code "
        "        ORDER BY "
        "          CASE WHEN sb.list_date IS NOT NULL THEN 0 ELSE 1 END, "
        "          sb.list_date DESC"
        "      ) AS rn "
        "    FROM stock_tushare.stock_basic sb "
        "  ) x "
        "  WHERE x.rn = 1"
        ") sb ON sb.ts_code = base.ts_code "
        + where_in_rt_k +
        "GROUP BY base.ts_code, rt_name.name, rt_sw_map.sw_name"
    )
    base_params = (periods[0],) + base_extra_params
    with conn.cursor() as cur:
        cur.execute(base_sql, base_params)
        raw = cur.fetchall() or []
    if not raw:
        # 当期 fina_indicator 全空或 rt_k 为空：走"空结果"分支，不视为失败
        return []
    rows: list[dict] = []
    for r in raw:
        tc = str(r[0]).strip() if r and r[0] is not None else ''
        if not tc:
            continue
        row: dict = {
            'ts_code': tc,
            'sec_name': r[1] if len(r) > 1 and r[1] is not None else '',
            'sw_name': r[2] if len(r) > 2 and r[2] is not None else '',
            'list_date': str(r[3]).strip() if len(r) > 3 and r[3] is not None else '',
            'act_name': str(r[4]).strip() if len(r) > 4 and r[4] is not None else '',
            'act_ent_type': str(r[5]).strip() if len(r) > 5 and r[5] is not None else '',
        }
        # 初始化所有主 SQL 原本承担的展示列为 None / ''
        row['a0'] = ''
        for i in range(4):
            row[f'roe{i}'] = None
            row[f'p{i}'] = None
            row[f'g{i}'] = None
            row[f'c{i}'] = None
            row[f'o{i}'] = None
        for i in range(9):
            row[f'k{i}'] = None
            row[f'f{i}'] = None
            row[f'r{i}'] = None  # quarter_yoy 会填，但先占位防下游 KeyError
            row[f'u{i}'] = None
            row[f'v{i}'] = None
        rows.append(row)
    if not rows:
        return []

    ts_codes = [r['ts_code'] for r in rows]
    fi_periods_4 = [p for p in periods[:4] if p]
    bs_periods_9 = [p for p in contract_periods[:9] if p]

    def _fp_collect_fi_main():
        cn = pymysql.connect(**DB_CONFIG_TUSHARE)
        try:
            return _fina_arturn_collect_fi_main_candidates(
                cn, ts_codes, fi_periods_4, has_fi_rt=has_fi_rt,
            )
        finally:
            cn.close()

    def _fp_collect_bs():
        cn = pymysql.connect(**DB_CONFIG_TUSHARE)
        try:
            return _fina_arturn_collect_bs_candidates(
                cn, ts_codes, bs_periods_9,
                has_bs_rt=has_bs_rt, has_adv=has_adv,
            )
        finally:
            cn.close()

    # fi_main / bs 两次大表扫描互不依赖：并行各用独立连接，缩短快路径 wall 时间
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as _ex_fp:
        _fut_fi = _ex_fp.submit(_fp_collect_fi_main)
        _fut_bs = _ex_fp.submit(_fp_collect_bs)
        fi_main = _fut_fi.result()
        bs_cands = _fut_bs.result()
    if not fi_main:
        # 主字段一次扫完失败 → 触发外层降级，回退原 SQL
        raise RuntimeError('fi_main_candidates empty')

    if not bs_cands:
        # balancesheet 表里 9 期全空是极小概率事件；发生则外层降级
        raise RuntimeError('bs_candidates empty')

    # ---- 填 fi_turn（a0 + roe0-3 + p0-3）：优先 arturn 非空，否则仍取最优行填 roe/a0（p 可空）
    turn_pref = lambda d: 0  # noqa: E731 —— fi_turn 无业务列优先，纯看 update_flag/rt/ann
    # gpm：COALESCE 优先；ocf_opincome / ocf_or：各自列优先
    gpm_pref = lambda d: 0 if (  # noqa: E731
        d.get('grossprofit_margin') is not None or d.get('q_gsprofit_margin') is not None
    ) else 1
    coi_pref = lambda d: 0 if d.get('ocf_to_opincome') is not None else 1  # noqa: E731
    cor_pref = lambda d: 0 if d.get('ocf_to_or') is not None else 1  # noqa: E731

    p0_target = periods[0]
    for row in rows:
        tc = row['ts_code']
        for i, pd_ in enumerate(fi_periods_4):
            cs = fi_main.get((tc, pd_))
            if not cs:
                continue
            # fi_turn：优先 arturn_days 非空；金融股等无应收周转时回退全池（p 仍为空）
            pool_turn = [d for d in cs if d.get('arturn_days') is not None]
            best_turn = (
                _fina_pick_with_pref_fn(pool_turn, turn_pref, has_fi_rt)
                or _fina_pick_with_pref_fn(cs, turn_pref, has_fi_rt)
            )
            if best_turn:
                # 只在 periods[0]（当期）填 a0
                if pd_ == p0_target:
                    row['a0'] = str(best_turn.get('ann_date') or '').strip()
                av = _to_float_or_none(best_turn.get('arturn_days'))
                rv = _to_float_or_none(best_turn.get('roe_dt'))
                row[f'p{i}'] = round(av, 2) if av is not None else None
                row[f'roe{i}'] = round(rv, 2) if rv is not None else None
            # fi_gpm：全池选行（不再过滤）
            best_gpm = _fina_pick_with_pref_fn(cs, gpm_pref, has_fi_rt)
            if best_gpm is not None:
                gv = best_gpm.get('grossprofit_margin')
                if gv is None:
                    gv = best_gpm.get('q_gsprofit_margin')
                fv = _to_float_or_none(gv)
                row[f'g{i}'] = round(fv, 2) if fv is not None else None
            # fi_ocf_opincome
            best_coi = _fina_pick_with_pref_fn(cs, coi_pref, has_fi_rt)
            if best_coi is not None:
                cv = _to_float_or_none(best_coi.get('ocf_to_opincome'))
                row[f'c{i}'] = round(cv, 2) if cv is not None else None
            # fi_ocf_or：不做 ROUND，走 Decimal ×100
            best_cor = _fina_pick_with_pref_fn(cs, cor_pref, has_fi_rt)
            if best_cor is not None:
                row[f'o{i}'] = _ocf_to_or_display_pct(best_cor.get('ocf_to_or'))

    # ---- 填 bs_contract（k0-8 + f0-8）
    bs_pref = lambda d: 0 if (  # noqa: E731
        d.get('contract_liab') is not None or (has_adv and d.get('adv_receipts') is not None)
    ) else 1
    for row in rows:
        tc = row['ts_code']
        for i, pd_ in enumerate(bs_periods_9):
            cs = bs_cands.get((tc, pd_))
            if not cs:
                continue
            best = _fina_pick_with_pref_fn(cs, bs_pref, has_bs_rt)
            if not best:
                continue
            cl = best.get('contract_liab')
            adv = best.get('adv_receipts') if has_adv else None
            fa = best.get('fix_assets_total')
            try:
                cl_f = float(cl) if cl is not None else 0.0
            except (TypeError, ValueError):
                cl_f = 0.0
            try:
                adv_f = float(adv) if adv is not None else 0.0
            except (TypeError, ValueError):
                adv_f = 0.0
            try:
                fa_f = float(fa) if fa is not None else 0.0
            except (TypeError, ValueError):
                fa_f = 0.0
            row[f'k{i}'] = round((cl_f + adv_f) / 100000000.0, 2)
            row[f'f{i}'] = round(fa_f / 100000000.0, 2)

    # 原主 SQL 的 ORDER BY p0 ASC, ts_code ASC —— 在 Python 侧等价排序
    rows.sort(key=lambda d: (
        1 if d.get('p0') is None else 0,
        d.get('p0') if d.get('p0') is not None else 0.0,
        d.get('ts_code') or '',
    ))
    return rows


def _fina_arturn_collect_fi_candidates(
    conn,
    ts_codes: list[str],
    quarter_periods: list[str],
    *,
    has_fi_rt: bool,
    has_q_gr_yoy: bool,
    has_q_op_yoy: bool,
    has_q_opincome: bool,
    has_q_dtprofit: bool,
    has_q_ocf_to_sales: bool,
    has_q_ocf_to_or: bool,
    has_q_gsprofit_margin: bool,
    has_q_netprofit_margin: bool,
    has_q_dt_roe: bool,
) -> dict:
    """一次性从 fina_indicator 拉取 5 组 attach 共同需要的"候选版本行"。

    原来 5 个单季 attach 各自跑一次带 ROW_NUMBER() 的 window-function 扫描，重复读百万级 fina_indicator
    5 次。这里改为不做窗口函数、一次性取所有版本（每组 ts_code×end_date 通常 2-4 行），由 Python 侧按
    各 attach 的口径选行；严格保持原优先级（对应字段 IS NOT NULL 优先 > update_flag='1' > report_type=1
    > ann_date DESC），避免业务语义改变。

    返回 {(ts_code, end_date): [row_dict, ...], ...}，dict 含 update_flag、report_type（仅 has_fi_rt
    时）、ann_date（'YYYYMMDD' 字符串），以及 has_* 为 True 的业务列原值。每组已按 ann_date DESC 预排。
    失败时返回 {}，主流程自动降级走原 5 次 SQL 路径。
    """
    if not ts_codes or not quarter_periods:
        return {}
    col_map = [
        ('q_gr_yoy', has_q_gr_yoy),
        ('q_op_yoy', has_q_op_yoy),
        ('q_opincome', has_q_opincome),
        ('q_dtprofit', has_q_dtprofit),
        ('q_ocf_to_sales', has_q_ocf_to_sales),
        ('q_ocf_to_or', has_q_ocf_to_or),
        ('q_gsprofit_margin', has_q_gsprofit_margin),
        ('q_netprofit_margin', has_q_netprofit_margin),
        ('q_dt_roe', has_q_dt_roe),
    ]
    biz_cols = [name for name, ok in col_map if ok]
    if not biz_cols:
        return {}
    sel_cols = ['ts_code', 'end_date', 'update_flag', 'ann_date']
    if has_fi_rt:
        sel_cols.append('report_type')
    sel_cols.extend(biz_cols)
    col_sql = ', '.join(f'fi.{c}' for c in sel_cols)
    ts_ph = ', '.join(['%s'] * len(ts_codes))
    pd_ph = ', '.join(['%s'] * len(quarter_periods))
    sql = (
        f'SELECT {col_sql} '
        f'FROM stock_tushare.fina_indicator fi '
        f'WHERE fi.ts_code IN ({ts_ph}) '
        f'  AND fi.end_date IN ({pd_ph})'
    )
    params = tuple(list(ts_codes) + list(quarter_periods))
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            raw = cur.fetchall() or []
    except Exception as e:
        logger.warning('合并扫描 fina_indicator 候选行失败，降级为按列独立查询: %s', e)
        return {}
    cands: dict = {}
    for r in raw:
        if not r:
            continue
        d = dict(zip(sel_cols, r))
        tc_raw = d.get('ts_code')
        tc = str(tc_raw).strip() if tc_raw is not None else ''
        ed = _normalize_end_date_yyyymmdd(d.get('end_date'))
        if not tc or not ed:
            continue
        d['ts_code'] = tc
        d['end_date'] = ed
        uf = d.get('update_flag')
        d['update_flag'] = str(uf) if uf is not None else ''
        if has_fi_rt:
            rt_v = d.get('report_type')
            try:
                d['report_type'] = int(rt_v) if rt_v is not None else None
            except (TypeError, ValueError):
                d['report_type'] = None
        ann = d.get('ann_date')
        if ann is None:
            d['ann_date'] = ''
        else:
            s = str(ann)
            if '-' in s and len(s) >= 10:
                s = s[:10].replace('-', '')
            d['ann_date'] = s
        cands.setdefault((tc, ed), []).append(d)
    # 每组按 ann_date DESC 预排（稳定），后续 pick 只需再按 primary key asc 做 min 即可
    for key in cands:
        cands[key].sort(key=lambda x: x.get('ann_date') or '', reverse=True)
    return cands


def _fina_pick_from_cands(cands_for_key: list[dict], pref_cols: tuple, has_fi_rt: bool) -> dict | None:
    """从 `(ts_code, end_date)` 组的候选行中按原 SQL 的 ORDER BY 选出"最优"行。

    优先级（与原 SQL 一致）：
      1. pref_cols 里每个字段 IS NOT NULL 优先
      2. update_flag = '1' 优先
      3. 若 has_fi_rt，report_type = 1 优先
      4. ann_date DESC —— cands_for_key 已按 ann_date DESC 预排，min 的稳定性天然保留
    """
    if not cands_for_key:
        return None
    def _k(d: dict) -> tuple:
        key = []
        for col in pref_cols:
            key.append(0 if d.get(col) is not None else 1)
        key.append(0 if d.get('update_flag') == '1' else 1)
        if has_fi_rt:
            key.append(0 if d.get('report_type') == 1 else 1)
        return tuple(key)
    # min 对等键保留首个出现者，而输入已按 ann_date DESC 预排 → 等价于 ORDER BY ... , ann_date DESC
    return min(cands_for_key, key=_k)


def _fina_arturn_attach_quarter_yoy(
    conn,
    rows: list[dict],
    quarter_periods: list[str],
    has_q_gr_yoy: bool,
    has_q_op_yoy: bool,
    has_fi_rt: bool,
    fi_cands: dict | None = None,
) -> None:
    """就地写入单季度同比列（r0..r8, u0..u8），避免主查询过重导致超时。

    `fi_cands` 非 None 时直接在内存中选行；为 None 时走原独立 SQL 路径（向后兼容 + 降级兜底）。
    """
    if not rows:
        return
    for row in rows:
        for i in range(9):
            row[f'r{i}'] = None
            row[f'u{i}'] = None
    if (not has_q_gr_yoy and not has_q_op_yoy) or not quarter_periods:
        return

    ts_codes = [str(r.get('ts_code') or '').strip() for r in rows]
    ts_codes = [t for t in ts_codes if t]
    if not ts_codes:
        return

    periods = [str(p).strip() for p in quarter_periods[:9] if str(p).strip()]
    if not periods:
        return

    if fi_cands is not None:
        pref_cols: list[str] = []
        if has_q_gr_yoy:
            pref_cols.append('q_gr_yoy')
        if has_q_op_yoy:
            pref_cols.append('q_op_yoy')
        pref_tuple = tuple(pref_cols)
        for row in rows:
            tc = str(row.get('ts_code') or '').strip()
            if not tc:
                continue
            for i, p in enumerate(periods[:9]):
                cs = fi_cands.get((tc, p))
                if not cs:
                    continue
                best = _fina_pick_from_cands(cs, pref_tuple, has_fi_rt)
                if not best:
                    continue
                row[f'r{i}'] = _to_float_or_none(best.get('q_gr_yoy')) if has_q_gr_yoy else None
                row[f'u{i}'] = _to_float_or_none(best.get('q_op_yoy')) if has_q_op_yoy else None
        return

    fi_rt_ord = (
        "      CASE WHEN fi.report_type = 1 THEN 0 ELSE 1 END, "
        if has_fi_rt else
        ""
    )
    ts_ph = ", ".join(["%s"] * len(ts_codes))
    pd_ph = ", ".join(["%s"] * len(periods))
    gr_sel = "fi.q_gr_yoy" if has_q_gr_yoy else "NULL AS q_gr_yoy"
    op_sel = "fi.q_op_yoy" if has_q_op_yoy else "NULL AS q_op_yoy"
    gr_ord = "CASE WHEN fi.q_gr_yoy IS NOT NULL THEN 0 ELSE 1 END, " if has_q_gr_yoy else ""
    op_ord = "CASE WHEN fi.q_op_yoy IS NOT NULL THEN 0 ELSE 1 END, " if has_q_op_yoy else ""
    sql = (
        "SELECT z.ts_code, z.end_date, z.q_gr_yoy, z.q_op_yoy "
        "FROM ("
        "  SELECT fi.ts_code, fi.end_date, "
        f"    {gr_sel}, "
        f"    {op_sel}, "
        "    ROW_NUMBER() OVER ("
        "      PARTITION BY fi.ts_code, fi.end_date "
        "      ORDER BY "
        f"        {gr_ord}"
        f"        {op_ord}"
        "        CASE WHEN fi.update_flag = '1' THEN 0 ELSE 1 END, "
        f"        {fi_rt_ord}"
        "        fi.ann_date DESC"
        "    ) AS rn "
        "  FROM stock_tushare.fina_indicator fi "
        f"  WHERE fi.ts_code IN ({ts_ph}) "
        f"    AND fi.end_date IN ({pd_ph})"
        ") z "
        "WHERE z.rn = 1"
    )
    params = tuple(ts_codes + periods)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            raw = cur.fetchall() or []
    except Exception as e:
        logger.warning("单季度同比补齐失败（降级为空列）: %s", e)
        return

    mp: dict[str, dict[str, tuple[float | None, float | None]]] = {}
    for r in raw:
        tc = str(r[0]).strip() if len(r) > 0 and r[0] is not None else ""
        ed = _normalize_end_date_yyyymmdd(r[1] if len(r) > 1 else None)
        if not tc or not ed:
            continue
        gr = _to_float_or_none(r[2] if len(r) > 2 else None)
        op = _to_float_or_none(r[3] if len(r) > 3 else None)
        if tc not in mp:
            mp[tc] = {}
        mp[tc][ed] = (gr, op)

    for row in rows:
        tc = str(row.get('ts_code') or '').strip()
        if not tc:
            continue
        md = mp.get(tc, {})
        for i, p in enumerate(periods[:9]):
            v = md.get(p)
            if not v:
                continue
            row[f'r{i}'] = v[0]
            row[f'u{i}'] = v[1]


def _fina_arturn_attach_quarter_op_yoy_excess_gr_yoy(
    _conn,
    rows: list[dict],
    quarter_periods: list[str],
    _has_inc_rt: bool,
) -> None:
    """就地写入 v0..v8：与表内 r/u 同源，均为 fina_indicator 同比增速（百分点差）。

    v_i = q_op_yoy − q_gr_yoy，仅当 r_i、u_i 可解析为浮点数且 u_i > r_i 时写入（否则前端 --）。
    与 income 营业利润绝对额无关。_conn / _has_inc_rt 保留签名与调用方一致，本函数不使用。
    """
    for row in rows:
        for i in range(9):
            row[f'v{i}'] = None
    if not rows or not quarter_periods:
        return
    periods = [str(p).strip() for p in quarter_periods[:9] if str(p).strip()]
    if not periods:
        return
    for row in rows:
        for i, _p in enumerate(periods[:9]):
            rv = row.get(f'r{i}')
            uv = row.get(f'u{i}')
            if rv is None or uv is None:
                continue
            try:
                rf = float(rv)
                uf = float(uv)
            except (TypeError, ValueError):
                continue
            if uf > rf:
                row[f'v{i}'] = round(uf - rf, 2)


def _fina_arturn_attach_quarter_revenue_yi(
    conn,
    rows: list[dict],
    quarter_periods: list[str],
    has_inc_rt: bool,
) -> None:
    """就地写入单季度营业收入（亿）（s0..s8）及新高标记 s_high（s0 非空时，在已有值的 s0..s8 中取最大，s0 等于该最大则为 1）。"""
    if not rows:
        return
    for row in rows:
        for i in range(9):
            row[f's{i}'] = None
        row['s_high'] = None
    if not quarter_periods:
        return
    ts_codes = [str(r.get('ts_code') or '').strip() for r in rows]
    ts_codes = [t for t in ts_codes if t]
    if not ts_codes:
        return
    periods = [str(p).strip() for p in quarter_periods[:9] if str(p).strip()]
    if not periods:
        return
    try:
        dkeys = _fina_income_date_keys_for_single_quarter_revenue(periods)
        rev_nested = _fina_batch_income_total_revenue(conn, ts_codes, dkeys, has_inc_rt)
    except Exception as e:
        logger.warning("单季度营业收入补齐失败（降级为空列）: %s", e)
        return
    for row in rows:
        tc = str(row.get('ts_code') or '').strip()
        if not tc:
            continue
        rev_map = rev_nested.get(tc, {})
        if not rev_map:
            continue
        for i, p in enumerate(periods[:9]):
            yq = _fina_yq_from_anchor(p)
            if not yq:
                continue
            y, q = yq
            sq = _fina_quarter_single_revenue(rev_map, y, q)
            if sq is None:
                continue
            row[f's{i}'] = round(float(sq) / 100000000.0, 2)
        s0v = row.get('s0')
        if s0v is None:
            row['s_high'] = None
        else:
            present: list[float] = []
            for i in range(9):
                x = row.get(f's{i}')
                if x is not None:
                    present.append(float(x))
            if not present:
                row['s_high'] = None
            else:
                mx = max(present)
                row['s_high'] = 1 if float(s0v) == mx else 0


def _fina_arturn_attach_quarter_cashflow_act_yi(
    conn,
    rows: list[dict],
    quarter_periods: list[str],
    has_n_cashflow_act: bool,
    has_cf_rt: bool,
) -> None:
    """就地写入单季度经营现金流净额（亿）cf0..cf8 及 cf_high：cashflow.n_cashflow_act 为报告期累计值，单季拆解同单季营收；新高与 s_high 同逻辑（cf0 非空时在已有值的 cf0..cf8 中取最大）。"""
    if not rows:
        return
    for row in rows:
        for i in range(9):
            row[f'cf{i}'] = None
        row['cf_high'] = None
    if not has_n_cashflow_act or not quarter_periods:
        return
    ts_codes = [str(r.get('ts_code') or '').strip() for r in rows]
    ts_codes = [t for t in ts_codes if t]
    if not ts_codes:
        return
    periods = [str(p).strip() for p in quarter_periods[:9] if str(p).strip()]
    if not periods:
        return
    try:
        dkeys = _fina_income_date_keys_for_single_quarter_revenue(periods)
        cf_nested = _fina_batch_cashflow_n_cashflow_act(conn, ts_codes, dkeys, has_cf_rt)
    except Exception as e:
        logger.warning("单季度经营现金流净额（cashflow.n_cashflow_act）补齐失败（降级为空列）: %s", e)
        return
    for row in rows:
        tc = str(row.get('ts_code') or '').strip()
        if not tc:
            continue
        cf_map = cf_nested.get(tc, {})
        if not cf_map:
            continue
        for i, p in enumerate(periods[:9]):
            yq = _fina_yq_from_anchor(p)
            if not yq:
                continue
            y, q = yq
            sq = _fina_quarter_single_revenue(cf_map, y, q)
            if sq is None:
                continue
            row[f'cf{i}'] = round(float(sq) / 100000000.0, 2)
        c0v = row.get('cf0')
        if c0v is None:
            row['cf_high'] = None
        else:
            present: list[float] = []
            for i in range(9):
                x = row.get(f'cf{i}')
                if x is not None:
                    present.append(float(x))
            if not present:
                row['cf_high'] = None
            else:
                mx = max(present)
                row['cf_high'] = 1 if float(c0v) == mx else 0


def _fina_arturn_attach_q_opincome_yi(
    conn,
    rows: list[dict],
    quarter_periods: list[str],
    has_q_opincome: bool,
    has_fi_rt: bool,
    fi_cands: dict | None = None,
) -> None:
    """就地写入单季度经营活动净收益（亿）（oi0..oi8）及新高 oi_high；源自 fina_indicator.q_opincome（元 /1e8→亿）。"""
    if not rows:
        return
    for row in rows:
        for i in range(9):
            row[f'oi{i}'] = None
        row['oi_high'] = None
    if not has_q_opincome or not quarter_periods:
        return
    ts_codes = [str(r.get('ts_code') or '').strip() for r in rows]
    ts_codes = [t for t in ts_codes if t]
    if not ts_codes:
        return
    periods = [str(p).strip() for p in quarter_periods[:9] if str(p).strip()]
    if not periods:
        return
    if fi_cands is not None:
        pref_tuple = ('q_opincome',)
        for row in rows:
            tc = str(row.get('ts_code') or '').strip()
            if not tc:
                continue
            for i, p in enumerate(periods[:9]):
                cs = fi_cands.get((tc, p))
                if not cs:
                    continue
                best = _fina_pick_from_cands(cs, pref_tuple, has_fi_rt)
                if not best:
                    continue
                qv = _to_float_or_none(best.get('q_opincome'))
                if qv is not None:
                    row[f'oi{i}'] = round(float(qv) / 100000000.0, 2)
            oi0v = row.get('oi0')
            if oi0v is None:
                row['oi_high'] = None
                continue
            present: list[float] = []
            for j in range(9):
                x = row.get(f'oi{j}')
                if x is not None:
                    present.append(float(x))
            if not present:
                row['oi_high'] = None
            else:
                mx = max(present)
                row['oi_high'] = 1 if float(oi0v) == mx else 0
        return
    fi_rt_ord = (
        '      CASE WHEN fi.report_type = 1 THEN 0 ELSE 1 END, '
        if has_fi_rt else
        ''
    )
    ts_ph = ', '.join(['%s'] * len(ts_codes))
    pd_ph = ', '.join(['%s'] * len(periods))
    sql = (
        'SELECT z.ts_code, z.end_date, z.q_opincome '
        'FROM ('
        '  SELECT fi.ts_code, fi.end_date, fi.q_opincome, '
        '    ROW_NUMBER() OVER ('
        '      PARTITION BY fi.ts_code, fi.end_date '
        '      ORDER BY '
        '        CASE WHEN fi.q_opincome IS NOT NULL THEN 0 ELSE 1 END, '
        '        CASE WHEN fi.update_flag = \'1\' THEN 0 ELSE 1 END, '
        f'        {fi_rt_ord}'
        '        fi.ann_date DESC'
        '    ) AS rn '
        '  FROM stock_tushare.fina_indicator fi '
        f'  WHERE fi.ts_code IN ({ts_ph}) '
        f'    AND fi.end_date IN ({pd_ph})'
        ') z '
        'WHERE z.rn = 1'
    )
    params = tuple(ts_codes + periods)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            raw = cur.fetchall() or []
    except Exception as e:
        logger.warning('单季度经营活动净收益补齐失败（降级为空列）: %s', e)
        return
    mp: dict[str, dict[str, float | None]] = {}
    for r in raw:
        tc = str(r[0]).strip() if len(r) > 0 and r[0] is not None else ''
        ed = _normalize_end_date_yyyymmdd(r[1] if len(r) > 1 else None)
        if not tc or not ed:
            continue
        qv = _to_float_or_none(r[2] if len(r) > 2 else None)
        if tc not in mp:
            mp[tc] = {}
        mp[tc][ed] = qv
    for row in rows:
        tc = str(row.get('ts_code') or '').strip()
        if not tc:
            continue
        md = mp.get(tc, {})
        for i, p in enumerate(periods[:9]):
            qv = md.get(p)
            if qv is None:
                continue
            row[f'oi{i}'] = round(float(qv) / 100000000.0, 2)
        oi0v = row.get('oi0')
        if oi0v is None:
            row['oi_high'] = None
        else:
            present: list[float] = []
            for i in range(9):
                x = row.get(f'oi{i}')
                if x is not None:
                    present.append(float(x))
            if not present:
                row['oi_high'] = None
            else:
                mx = max(present)
                row['oi_high'] = 1 if float(oi0v) == mx else 0


def _fina_arturn_attach_q_dtprofit_yi(
    conn,
    rows: list[dict],
    quarter_periods: list[str],
    has_q_dtprofit: bool,
    has_fi_rt: bool,
    fi_cands: dict | None = None,
) -> None:
    """就地写入单季度扣非净利润（亿）（dtp0..dtp8）及新高 dtp_high；源自 fina_indicator.q_dtprofit（元 /1e8→亿）。"""
    if not rows:
        return
    for row in rows:
        for i in range(9):
            row[f'dtp{i}'] = None
        row['dtp_high'] = None
    if not has_q_dtprofit or not quarter_periods:
        return
    ts_codes = [str(r.get('ts_code') or '').strip() for r in rows]
    ts_codes = [t for t in ts_codes if t]
    if not ts_codes:
        return
    periods = [str(p).strip() for p in quarter_periods[:9] if str(p).strip()]
    if not periods:
        return
    if fi_cands is not None:
        pref_tuple = ('q_dtprofit',)
        for row in rows:
            tc = str(row.get('ts_code') or '').strip()
            if not tc:
                continue
            for i, p in enumerate(periods[:9]):
                cs = fi_cands.get((tc, p))
                if not cs:
                    continue
                best = _fina_pick_from_cands(cs, pref_tuple, has_fi_rt)
                if not best:
                    continue
                qv = _to_float_or_none(best.get('q_dtprofit'))
                if qv is not None:
                    row[f'dtp{i}'] = round(float(qv) / 100000000.0, 2)
            dtp0v = row.get('dtp0')
            if dtp0v is None:
                row['dtp_high'] = None
                continue
            present: list[float] = []
            for j in range(9):
                x = row.get(f'dtp{j}')
                if x is not None:
                    present.append(float(x))
            if not present:
                row['dtp_high'] = None
            else:
                mx = max(present)
                row['dtp_high'] = 1 if float(dtp0v) == mx else 0
        return
    fi_rt_ord = (
        '      CASE WHEN fi.report_type = 1 THEN 0 ELSE 1 END, '
        if has_fi_rt else
        ''
    )
    ts_ph = ', '.join(['%s'] * len(ts_codes))
    pd_ph = ', '.join(['%s'] * len(periods))
    sql = (
        'SELECT z.ts_code, z.end_date, z.q_dtprofit '
        'FROM ('
        '  SELECT fi.ts_code, fi.end_date, fi.q_dtprofit, '
        '    ROW_NUMBER() OVER ('
        '      PARTITION BY fi.ts_code, fi.end_date '
        '      ORDER BY '
        '        CASE WHEN fi.q_dtprofit IS NOT NULL THEN 0 ELSE 1 END, '
        '        CASE WHEN fi.update_flag = \'1\' THEN 0 ELSE 1 END, '
        f'        {fi_rt_ord}'
        '        fi.ann_date DESC'
        '    ) AS rn '
        '  FROM stock_tushare.fina_indicator fi '
        f'  WHERE fi.ts_code IN ({ts_ph}) '
        f'    AND fi.end_date IN ({pd_ph})'
        ') z '
        'WHERE z.rn = 1'
    )
    params = tuple(ts_codes + periods)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            raw = cur.fetchall() or []
    except Exception as e:
        logger.warning('单季度扣非净利润补齐失败（降级为空列）: %s', e)
        return
    mp: dict[str, dict[str, float | None]] = {}
    for r in raw:
        tc = str(r[0]).strip() if len(r) > 0 and r[0] is not None else ''
        ed = _normalize_end_date_yyyymmdd(r[1] if len(r) > 1 else None)
        if not tc or not ed:
            continue
        qv = _to_float_or_none(r[2] if len(r) > 2 else None)
        if tc not in mp:
            mp[tc] = {}
        mp[tc][ed] = qv
    for row in rows:
        tc = str(row.get('ts_code') or '').strip()
        if not tc:
            continue
        md = mp.get(tc, {})
        for i, p in enumerate(periods[:9]):
            qv = md.get(p)
            if qv is None:
                continue
            row[f'dtp{i}'] = round(float(qv) / 100000000.0, 2)
        dtp0v = row.get('dtp0')
        if dtp0v is None:
            row['dtp_high'] = None
        else:
            present: list[float] = []
            for i in range(9):
                x = row.get(f'dtp{i}')
                if x is not None:
                    present.append(float(x))
            if not present:
                row['dtp_high'] = None
            else:
                mx = max(present)
                row['dtp_high'] = 1 if float(dtp0v) == mx else 0


def _fina_arturn_attach_q_fcf_star_yi(
    conn,
    rows: list[dict],
    quarter_periods: list[str],
) -> None:
    """就地写入单季度 FCF*（亿）（fst0..fst8）及新高 fst_high；源自 fina_quarter_metrics.q_fcf_star（元 /1e8→亿）。"""
    if not rows:
        return
    for row in rows:
        for i in range(9):
            row[f'fst{i}'] = None
        row['fst_high'] = None
    if not quarter_periods:
        return
    ts_codes = [str(r.get('ts_code') or '').strip() for r in rows]
    ts_codes = [t for t in ts_codes if t]
    if not ts_codes:
        return
    periods = [str(p).strip() for p in quarter_periods[:9] if str(p).strip()]
    if not periods:
        return
    ts_ph = ', '.join(['%s'] * len(ts_codes))
    pd_ph = ', '.join(['%s'] * len(periods))
    sql = (
        'SELECT ts_code, end_date, q_fcf_star '
        'FROM stock_tushare.fina_quarter_metrics '
        f'WHERE ts_code IN ({ts_ph}) '
        f'  AND end_date IN ({pd_ph})'
    )
    params = tuple(ts_codes + periods)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            raw = cur.fetchall() or []
    except Exception as e:
        logger.warning('单季度 FCF*（q_fcf_star）补齐失败（降级为空列）: %s', e)
        return
    mp: dict[str, dict[str, float | None]] = {}
    for r in raw:
        tc = str(r[0]).strip() if len(r) > 0 and r[0] is not None else ''
        ed = _normalize_end_date_yyyymmdd(r[1] if len(r) > 1 else None)
        if not tc or not ed:
            continue
        qv = _to_float_or_none(r[2] if len(r) > 2 else None)
        if tc not in mp:
            mp[tc] = {}
        mp[tc][ed] = qv
    for row in rows:
        tc = str(row.get('ts_code') or '').strip()
        if not tc:
            continue
        md = mp.get(tc, {})
        for i, p in enumerate(periods[:9]):
            qv = md.get(p)
            if qv is None:
                continue
            row[f'fst{i}'] = round(float(qv) / 100000000.0, 2)
        fst0v = row.get('fst0')
        if fst0v is None:
            row['fst_high'] = None
        else:
            present: list[float] = []
            for i in range(9):
                x = row.get(f'fst{i}')
                if x is not None:
                    present.append(float(x))
            if not present:
                row['fst_high'] = None
            else:
                mx = max(present)
                row['fst_high'] = 1 if float(fst0v) == mx else 0


# 单季 OCF/营收九列加权离散：ocs0 为最近一季权重 1，每往前一季乘该因子（环境变量可覆写）。
FINA_ARTURN_OCS_WQCV_DECAY = float(os.getenv('FINA_ARTURN_OCS_WQCV_DECAY', '0.88'))


def _compute_ocs_wqcv(row: dict) -> float | None:
    """基于 ocs0..ocs8 的加权变异系数（WQCV）：指数衰减权重，最新季权重最大。

    仅在非空季数 ≥ 2 时计算。WQCV = 加权标准差 / (|加权均值| + ε)，**数值越小表示相对水平波动越小**；
    当 |加权均值| < 1e-6 时退化为仅输出加权标准差（与表内单位一致），避免除零爆炸。
    """
    xs: list[float] = []
    ws: list[float] = []
    decay = FINA_ARTURN_OCS_WQCV_DECAY
    if decay <= 0 or decay > 1.0:
        decay = 0.88
    for i in range(9):
        v = row.get(f'ocs{i}')
        if v is None:
            continue
        xs.append(float(v))
        ws.append(decay ** i)
    if len(xs) < 2:
        return None
    sw = sum(ws)
    if sw <= 0:
        return None
    wn = [w / sw for w in ws]
    mu = sum(w * x for w, x in zip(wn, xs))
    var = sum(w * (x - mu) ** 2 for w, x in zip(wn, xs))
    sd = float(var ** 0.5)
    denom = abs(mu) + 1e-9
    if abs(mu) < 1e-6:
        out = sd
    else:
        out = sd / denom
    return round(out, 4)


def _fina_arturn_attach_quarter_q_ocf_pct(
    conn,
    rows: list[dict],
    quarter_periods: list[str],
    has_q_ocf_to_sales: bool,
    has_q_ocf_to_or: bool,
    has_fi_rt: bool,
    fi_cands: dict | None = None,
) -> None:
    """就地写入单季经营现金流比率：ocs0..ocs8（q_ocf_to_sales）、oco0..oco8（q_ocf_to_or）。

    与年报 o0..o3（ocf_to_or×100）不同：此处直接取库表已落库数值，不做 ×100 或其它换算。
    ocs_high：与 dtp_high 同逻辑，ocs0 非空时在已有值的 ocs0..ocs8 中取最大，ocs0 等于该最大则为 1，否则 0。
    ocs_wqcv：ocs0..ocs8 的加权变异系数（最新季权重大），越小越稳定；有效季 < 2 时为 None。
    """
    if not rows:
        return
    for row in rows:
        for i in range(9):
            row[f'ocs{i}'] = None
            row[f'oco{i}'] = None
        row['ocs_high'] = None
        row['ocs_wqcv'] = None
    if (not has_q_ocf_to_sales and not has_q_ocf_to_or) or not quarter_periods:
        return
    ts_codes = [str(r.get('ts_code') or '').strip() for r in rows]
    ts_codes = [t for t in ts_codes if t]
    if not ts_codes:
        return
    periods = [str(p).strip() for p in quarter_periods[:9] if str(p).strip()]
    if not periods:
        return

    if fi_cands is not None:
        pref_cols: list[str] = []
        if has_q_ocf_to_sales:
            pref_cols.append('q_ocf_to_sales')
        if has_q_ocf_to_or:
            pref_cols.append('q_ocf_to_or')
        pref_tuple = tuple(pref_cols)
        for row in rows:
            tc = str(row.get('ts_code') or '').strip()
            if not tc:
                continue
            for i, p in enumerate(periods[:9]):
                cs = fi_cands.get((tc, p))
                if not cs:
                    continue
                best = _fina_pick_from_cands(cs, pref_tuple, has_fi_rt)
                if not best:
                    continue
                ps = _to_float_or_none(best.get('q_ocf_to_sales')) if has_q_ocf_to_sales else None
                po = _to_float_or_none(best.get('q_ocf_to_or')) if has_q_ocf_to_or else None
                if ps is not None:
                    row[f'ocs{i}'] = ps
                if po is not None:
                    row[f'oco{i}'] = po
            ocs0v = row.get('ocs0')
            if ocs0v is None:
                row['ocs_high'] = None
            else:
                present: list[float] = []
                for j in range(9):
                    x = row.get(f'ocs{j}')
                    if x is not None:
                        present.append(float(x))
                if not present:
                    row['ocs_high'] = None
                else:
                    mx = max(present)
                    row['ocs_high'] = 1 if float(ocs0v) == mx else 0
            row['ocs_wqcv'] = _compute_ocs_wqcv(row)
        return

    fi_rt_ord = (
        '      CASE WHEN fi.report_type = 1 THEN 0 ELSE 1 END, '
        if has_fi_rt else
        ''
    )
    ts_ph = ', '.join(['%s'] * len(ts_codes))
    pd_ph = ', '.join(['%s'] * len(periods))
    sales_sel = 'fi.q_ocf_to_sales' if has_q_ocf_to_sales else 'NULL AS q_ocf_to_sales'
    or_sel = 'fi.q_ocf_to_or' if has_q_ocf_to_or else 'NULL AS q_ocf_to_or'
    sales_ord = (
        'CASE WHEN fi.q_ocf_to_sales IS NOT NULL THEN 0 ELSE 1 END, '
        if has_q_ocf_to_sales else ''
    )
    or_ord = (
        'CASE WHEN fi.q_ocf_to_or IS NOT NULL THEN 0 ELSE 1 END, '
        if has_q_ocf_to_or else ''
    )
    sql = (
        'SELECT z.ts_code, z.end_date, z.q_ocf_to_sales, z.q_ocf_to_or '
        'FROM ('
        '  SELECT fi.ts_code, fi.end_date, '
        f'    {sales_sel}, '
        f'    {or_sel}, '
        '    ROW_NUMBER() OVER ('
        '      PARTITION BY fi.ts_code, fi.end_date '
        '      ORDER BY '
        f'        {sales_ord}'
        f'        {or_ord}'
        '        CASE WHEN fi.update_flag = \'1\' THEN 0 ELSE 1 END, '
        f'        {fi_rt_ord}'
        '        fi.ann_date DESC'
        '    ) AS rn '
        '  FROM stock_tushare.fina_indicator fi '
        f'  WHERE fi.ts_code IN ({ts_ph}) '
        f'    AND fi.end_date IN ({pd_ph})'
        ') z '
        'WHERE z.rn = 1'
    )
    params = tuple(ts_codes + periods)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            raw = cur.fetchall() or []
    except Exception as e:
        logger.warning('单季度经营现金流比率补齐失败（降级为空列）: %s', e)
        return

    mp: dict[str, dict[str, tuple[float | None, float | None]]] = {}
    for r in raw:
        tc = str(r[0]).strip() if len(r) > 0 and r[0] is not None else ''
        ed = _normalize_end_date_yyyymmdd(r[1] if len(r) > 1 else None)
        if not tc or not ed:
            continue
        vs = _to_float_or_none(r[2] if len(r) > 2 else None)
        vo = _to_float_or_none(r[3] if len(r) > 3 else None)
        if tc not in mp:
            mp[tc] = {}
        mp[tc][ed] = (vs, vo)

    for row in rows:
        tc = str(row.get('ts_code') or '').strip()
        if not tc:
            continue
        md = mp.get(tc, {})
        for i, p in enumerate(periods[:9]):
            tup = md.get(p)
            if tup is None:
                continue
            ps = _to_float_or_none(tup[0])
            po = _to_float_or_none(tup[1])
            if ps is not None:
                row[f'ocs{i}'] = ps
            if po is not None:
                row[f'oco{i}'] = po
        ocs0v = row.get('ocs0')
        if ocs0v is None:
            row['ocs_high'] = None
        else:
            present: list[float] = []
            for i in range(9):
                x = row.get(f'ocs{i}')
                if x is not None:
                    present.append(float(x))
            if not present:
                row['ocs_high'] = None
            else:
                mx = max(present)
                row['ocs_high'] = 1 if float(ocs0v) == mx else 0
        row['ocs_wqcv'] = _compute_ocs_wqcv(row)


def _fina_arturn_attach_contract_k_high(rows: list[dict]) -> None:
    """就地写入合同负债（亿）九季新高 k_high：与 s_high 同逻辑，字段为 k0..k8（k0 非空时在已有值的 k0..k8 中取最大，k0 等于该最大则为 1，否则 0）。"""
    for row in rows:
        k0v = row.get('k0')
        if k0v is None:
            row['k_high'] = None
            continue
        present: list[float] = []
        for i in range(9):
            x = row.get(f'k{i}')
            if x is not None:
                present.append(float(x))
        if not present:
            row['k_high'] = None
        else:
            mx = max(present)
            row['k_high'] = 1 if float(k0v) == mx else 0


def _fina_arturn_attach_inventory_metrics(
    conn,
    rows: list[dict],
    quarter_periods: list[str],
    has_bs_rt: bool,
) -> None:
    """就地写入存货（亿）九季 + 新高 + 营收%（逻辑对齐合同负债区）。

    - inv0..inv8: balancesheet.inventories / 1e8
    - inv_high: 最新季 inv0 是否为展示九季最大
    - inv_revenue_pct: inv0 / ttm_revenue_yi * 100（与 k_revenue_pct 同口径）
    """
    if not rows:
        return
    for row in rows:
        row['inv_high'] = None
        row['inv_revenue_pct'] = None
        for i in range(9):
            row[f'inv{i}'] = None

    if not quarter_periods or not _tushare_column_exists(conn, 'balancesheet', 'inventories'):
        return

    ts_codes = [str(r.get('ts_code') or '').strip() for r in rows if str(r.get('ts_code') or '').strip()]
    if not ts_codes:
        return
    periods = [str(p).strip() for p in quarter_periods[:9] if str(p).strip()]
    if not periods:
        return

    ts_ph = ', '.join(['%s'] * len(ts_codes))
    pd_ph = ', '.join(['%s'] * len(periods))
    rt_line = "CASE WHEN bs.report_type = 1 THEN 0 ELSE 1 END, " if has_bs_rt else ""
    has_update_flag = _tushare_column_exists(conn, 'balancesheet', 'update_flag')
    has_ann_date = _tushare_column_exists(conn, 'balancesheet', 'ann_date')
    uf_line = "CASE WHEN bs.update_flag = '1' THEN 0 ELSE 1 END, " if has_update_flag else ""
    ann_line = "bs.ann_date DESC" if has_ann_date else "bs.ts_code ASC"
    sql = (
        "SELECT x.ts_code, x.end_date, x.inv_yi "
        "FROM ("
        "  SELECT bs.ts_code, bs.end_date, (COALESCE(bs.inventories, 0) / 100000000.0) AS inv_yi, "
        "         ROW_NUMBER() OVER ("
        "           PARTITION BY bs.ts_code, bs.end_date "
        "           ORDER BY "
        "             CASE WHEN bs.inventories IS NOT NULL THEN 0 ELSE 1 END, "
        + uf_line
        + rt_line
        + "             " + ann_line
        + "         ) AS rn "
        "  FROM stock_tushare.balancesheet bs "
        f"  WHERE bs.ts_code IN ({ts_ph}) "
        f"    AND DATE_FORMAT(bs.end_date, '%%Y%%m%%d') IN ({pd_ph}) "
        ") x "
        "WHERE x.rn = 1"
    )
    params = tuple(ts_codes + periods)
    by_ts_period: dict[tuple[str, str], float] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for tc, ed, iv in cur.fetchall() or []:
                t = str(tc or '').strip()
                d = str(ed or '').replace('-', '')[:8]
                if not t or not d or iv is None:
                    continue
                try:
                    by_ts_period[(t, d)] = round(float(iv), 2)
                except (TypeError, ValueError):
                    continue
    except Exception as e:
        logger.debug('fina_arturn inventories batch: %s', e)
        return

    for row in rows:
        tc = str(row.get('ts_code') or '').strip()
        if not tc:
            continue
        for i, p in enumerate(periods):
            row[f'inv{i}'] = by_ts_period.get((tc, p))

        inv0v = row.get('inv0')
        if inv0v is not None and row.get('ttm_revenue_yi') is not None:
            try:
                den = float(row.get('ttm_revenue_yi'))
                if den != 0 and np.isfinite(den):
                    row['inv_revenue_pct'] = round(float(inv0v) / den * 100.0, 2)
            except (TypeError, ValueError):
                row['inv_revenue_pct'] = None

        if inv0v is None:
            row['inv_high'] = None
            continue
        present: list[float] = []
        for i in range(9):
            x = row.get(f'inv{i}')
            if x is not None:
                present.append(float(x))
        if not present:
            row['inv_high'] = None
        else:
            row['inv_high'] = 1 if float(inv0v) == max(present) else 0


def _fina_arturn_attach_quarter_margins(
    conn,
    rows: list[dict],
    quarter_periods: list[str],
    has_q_gsprofit_margin: bool,
    has_q_netprofit_margin: bool,
    has_q_dt_roe: bool,
    has_fi_rt: bool,
    fi_cands: dict | None = None,
) -> None:
    """就地写入单季度毛利率/净利率/ROE(扣非)（m0..m8, n0..n8, t0..t8）并补齐三组新高/环比连续上升次数。"""
    if not rows:
        return
    for row in rows:
        row['m_high'] = None
        row['m_qoq'] = None
        row['n_high'] = None
        row['n_qoq'] = None
        row['t_high'] = None
        row['t_qoq'] = None
        for i in range(9):
            row[f'm{i}'] = None
            row[f'n{i}'] = None
            row[f't{i}'] = None
    if (not has_q_gsprofit_margin and not has_q_netprofit_margin and not has_q_dt_roe) or not quarter_periods:
        return

    ts_codes = [str(r.get('ts_code') or '').strip() for r in rows]
    ts_codes = [t for t in ts_codes if t]
    if not ts_codes:
        return

    periods = [str(p).strip() for p in quarter_periods[:9] if str(p).strip()]
    if not periods:
        return

    if fi_cands is not None:
        pref_cols: list[str] = []
        if has_q_gsprofit_margin:
            pref_cols.append('q_gsprofit_margin')
        if has_q_netprofit_margin:
            pref_cols.append('q_netprofit_margin')
        if has_q_dt_roe:
            pref_cols.append('q_dt_roe')
        pref_tuple = tuple(pref_cols)
        for row in rows:
            tc = str(row.get('ts_code') or '').strip()
            if not tc:
                continue
            for i, p in enumerate(periods[:9]):
                cs = fi_cands.get((tc, p))
                if not cs:
                    continue
                best = _fina_pick_from_cands(cs, pref_tuple, has_fi_rt)
                if not best:
                    continue
                row[f'm{i}'] = _to_float_or_none(best.get('q_gsprofit_margin')) if has_q_gsprofit_margin else None
                row[f'n{i}'] = _to_float_or_none(best.get('q_netprofit_margin')) if has_q_netprofit_margin else None
                row[f't{i}'] = _to_float_or_none(best.get('q_dt_roe')) if has_q_dt_roe else None
    else:
        fi_rt_ord = (
            "      CASE WHEN fi.report_type = 1 THEN 0 ELSE 1 END, "
            if has_fi_rt else
            ""
        )
        ts_ph = ", ".join(["%s"] * len(ts_codes))
        pd_ph = ", ".join(["%s"] * len(periods))
        gm_sel = "fi.q_gsprofit_margin" if has_q_gsprofit_margin else "NULL AS q_gsprofit_margin"
        nm_sel = "fi.q_netprofit_margin" if has_q_netprofit_margin else "NULL AS q_netprofit_margin"
        gm_ord = "CASE WHEN fi.q_gsprofit_margin IS NOT NULL THEN 0 ELSE 1 END, " if has_q_gsprofit_margin else ""
        nm_ord = "CASE WHEN fi.q_netprofit_margin IS NOT NULL THEN 0 ELSE 1 END, " if has_q_netprofit_margin else ""
        qroe_sel = "fi.q_dt_roe" if has_q_dt_roe else "NULL AS q_dt_roe"
        qroe_ord = "CASE WHEN fi.q_dt_roe IS NOT NULL THEN 0 ELSE 1 END, " if has_q_dt_roe else ""
        sql = (
            "SELECT z.ts_code, z.end_date, z.q_gsprofit_margin, z.q_netprofit_margin, z.q_dt_roe "
            "FROM ("
            "  SELECT fi.ts_code, fi.end_date, "
            f"    {gm_sel}, "
            f"    {nm_sel}, "
            f"    {qroe_sel}, "
            "    ROW_NUMBER() OVER ("
            "      PARTITION BY fi.ts_code, fi.end_date "
            "      ORDER BY "
            f"        {gm_ord}"
            f"        {nm_ord}"
            f"        {qroe_ord}"
            "        CASE WHEN fi.update_flag = '1' THEN 0 ELSE 1 END, "
            f"        {fi_rt_ord}"
            "        fi.ann_date DESC"
            "    ) AS rn "
            "  FROM stock_tushare.fina_indicator fi "
            f"  WHERE fi.ts_code IN ({ts_ph}) "
            f"    AND fi.end_date IN ({pd_ph})"
            ") z "
            "WHERE z.rn = 1"
        )
        params = tuple(ts_codes + periods)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                raw = cur.fetchall() or []
        except Exception as e:
            logger.warning("单季度毛利率/净利率补齐失败（降级为空列）: %s", e)
            return

        mp: dict[str, dict[str, tuple[float | None, float | None, float | None]]] = {}
        for r in raw:
            tc = str(r[0]).strip() if len(r) > 0 and r[0] is not None else ""
            ed = _normalize_end_date_yyyymmdd(r[1] if len(r) > 1 else None)
            if not tc or not ed:
                continue
            gm = _to_float_or_none(r[2] if len(r) > 2 else None)
            nm = _to_float_or_none(r[3] if len(r) > 3 else None)
            qroe = _to_float_or_none(r[4] if len(r) > 4 else None)
            if tc not in mp:
                mp[tc] = {}
            mp[tc][ed] = (gm, nm, qroe)

        for row in rows:
            tc = str(row.get('ts_code') or '').strip()
            if not tc:
                continue
            md = mp.get(tc, {})
            for i, p in enumerate(periods[:9]):
                v = md.get(p)
                if not v:
                    continue
                row[f'm{i}'] = v[0]
                row[f'n{i}'] = v[1]
                row[f't{i}'] = v[2]
    def _attach_high_qoq(row_obj: dict, metric_prefix: str, high_key: str, qoq_key: str) -> None:
        m0v = row_obj.get(f'{metric_prefix}0')
        if m0v is None:
            row_obj[high_key] = None
            row_obj[qoq_key] = None
            return
        m_present: list[float] = []
        for i in range(9):
            mv = row_obj.get(f'{metric_prefix}{i}')
            if mv is not None:
                m_present.append(float(mv))
        row_obj[high_key] = 1 if m_present and float(m0v) == max(m_present) else 0
        rise_count = 0
        prev = row_obj.get(f'{metric_prefix}0')
        if prev is None:
            row_obj[qoq_key] = None
            return
        for i in range(1, 9):
            curr = row_obj.get(f'{metric_prefix}{i}')
            if curr is None:
                break
            if float(prev) > float(curr):
                rise_count += 1
                prev = curr
                continue
            break
        row_obj[qoq_key] = rise_count

    for row in rows:
        _attach_high_qoq(row, 'm', 'm_high', 'm_qoq')
        _attach_high_qoq(row, 'n', 'n_high', 'n_qoq')
        _attach_high_qoq(row, 't', 't_high', 't_qoq')


def _query_fina_metric_end_dates(metric_col: str, limit_n: int = 80) -> list[str]:
    """读取某指标列有值的报告期（降序），用于前端下拉。当前仅支持 arturn_days。"""
    if metric_col != 'arturn_days':
        return []
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    except Exception as e:
        logger.exception("连接 stock_tushare 失败（fina end_date）: %s", e)
        return []
    try:
        lim = max(1, min(int(limit_n), 500))
    except Exception:
        lim = 80
    has_fi_rt = _tushare_column_exists(conn, 'fina_indicator', 'report_type')
    fi_rt_line = (
        "             CASE WHEN fi.report_type = 1 THEN 0 ELSE 1 END, "
        if has_fi_rt else
        ""
    )
    sql = (
        "SELECT t.end_date "
        "FROM ("
        "  SELECT fi.end_date, "
        "         ROW_NUMBER() OVER ("
        "           PARTITION BY fi.ts_code, fi.end_date "
        "           ORDER BY "
        "             CASE WHEN fi.update_flag = '1' THEN 0 ELSE 1 END, "
        + fi_rt_line +
        "             fi.ann_date DESC"
        "         ) AS rn "
        "  FROM stock_tushare.fina_indicator fi "
        f"  WHERE fi.end_date IS NOT NULL AND fi.{metric_col} IS NOT NULL"
        ") t "
        "WHERE t.rn = 1 "
        "GROUP BY t.end_date "
        "ORDER BY t.end_date DESC "
        "LIMIT %s"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (lim,))
            raw = cur.fetchall() or []
        out = []
        for r in raw:
            ed = _normalize_end_date_yyyymmdd(r[0] if r else None)
            if ed:
                out.append(ed)
        return out
    except Exception as e:
        logger.exception("查询 fina end_date 失败: %s", e)
        return []
    finally:
        conn.close()


_ARTURN_END_DATES_CACHE: dict = {}
_ARTURN_END_DATES_CACHE_TTL = 300.0


def _query_arturn_end_dates(limit_n: int = 80) -> list[str]:
    """读取可用报告期（降序），用于应收账款周转天数表下拉。

    加 5 分钟 TTL 缓存：报告期一天至多新增 1 条，重复 fetch 没有意义；用户反复点"配置"或切
    换报告期时能命中缓存，避免 fina_indicator 再扫一遍。
    """
    try:
        lim_key = max(1, min(int(limit_n), 500))
    except Exception:
        lim_key = 80
    import time as _t
    now = _t.monotonic()
    hit = _ARTURN_END_DATES_CACHE.get(lim_key)
    if hit and (now - hit[0]) < _ARTURN_END_DATES_CACHE_TTL:
        return list(hit[1])
    out = _query_fina_metric_end_dates('arturn_days', lim_key)
    if out:
        _ARTURN_END_DATES_CACHE[lim_key] = (now, tuple(out))
    return out


def _query_fina_arturn_table(selected_end_date: str) -> dict:
    """ROE（扣非）+ 应收账款周转天数 + 毛利率 + 经营现金流 + 两项单季同比 + 合同负债 + 固定资产。

    入池：selected_end_date 上 fina_indicator 有行即可（不要求 arturn_days；银行等金融股可进表）。
    ROE/周转天：与 fi_turn 同源；选行优先 arturn_days 非空，否则仍取最优版本行（周转天可空、ROE 仍可带出）。
    毛利率：独立选行，不要求 arturn_days；优先 COALESCE(grossprofit_margin, q_gsprofit_margin) 非空。
    经营现金流净额/经营活动净收益、经营现金流净额/营业收入：各自独立选行，优先对应字段非空；其中 ocf_to_or 四列不在 SQL 中预 ROUND，接口为库内小数×100、保留 2 位百分数。
    选行优先级（fina_indicator / balancesheet 一致）：update_flag=1 > report_type=1（合并报表）> ann_date 最新。
    单季度同比：q_gr_yoy（营业收入同比增速）与 q_op_yoy（营业利润同比增长率），均取当季+往前 8 季，共 9 季。
    单季利润增速高于收入增速（百分点）：v0..v8，由行内 u（q_op_yoy）与 r（q_gr_yoy）算 u−r；仅 u>r 时写入，与 income 绝对额无关。
    合同负债（亿）：balancesheet 同一报告期 contract_liab + adv_receipts（若存在 adv_receipts 列；旧准则预收款项），再 /1e8；无该列时仅用 contract_liab。
    固定资产（亿）：balancesheet.fix_assets_total / 1e8。
    应收占比(%)（单列）：balancesheet.accounts_receiv ÷ income 最近四单季营业总收入（TTM，累计口径拆解）×100，锚定期为 periods[0]。
    营收（TTM，亿元，单列）：与 ar_pct 同源的 TTM 营业总收入（元）/1e8，保留 2 位小数；页面不显示「亿」单位。
    主查询仅保留 ts_code 存在于 stock_wind.rt_k 的标的（实时表无代码视为非研究对象：退市或无效代码）。
    合同负债区「营收%」：k0（当期合同负债，亿）/ ttm_revenue_yi（亿）×100，与应收占比同源 TTM 营收。
    PE（pe_ttm）、总市值（total_mv 万元→亿）、自由市值（free_share 万股×rt_k.close→亿）：stk_factor_pro 全表最新 trade_date 行批量补齐。
    单季度经营现金流净额（亿）：cashflow.n_cashflow_act 为累计值，单季拆解同单季营业总收入（Q1=当行、Q2–Q4=累计差）；cf0..cf8 元/1e8，与 contractPeriods 对齐；cf_high 与 s_high 同逻辑（cf0 非空时在已有值的 cf0..cf8 中取最大，cf0 等于该最大则为 1）。
    单季度经营活动净收益（亿）：fina_indicator.q_opincome 选行后 /1e8；oi0..oi8 与 contractPeriods 对齐，oi_high 与单季营收 s_high 同为最新季在展示季已有值中的最大标记。
    单季度扣非净利润（亿）：fina_indicator.q_dtprofit 选行后 /1e8；dtp0..dtp8 与 contractPeriods 对齐，dtp_high 同上。
    单季度 FCF（亿）：fina_quarter_metrics.q_fcf_star（FCF*=单季 OCF−单季 CAPEX）/1e8；fst0..fst8 与 contractPeriods 对齐，fst_high 与 dtp_high 同逻辑。
    单季经营现金流/营业收入(%) 与 单季经营现金流/经营活动净收益(%)：q_ocf_to_sales、q_ocf_to_or 选行后直接输出库内值（本地表已按业务口径算好），ocs0..8、oco0..8；不与年报 ocf_to_or 列混用×100。ocs_high 与 dtp_high 同逻辑（最新季 ocs0 为展示九季已有值中的最大则为 1）。ocs_wqcv 为九季加权变异系数（指数衰减、最新季权重大），越小表示相对波动越小。
    """
    periods = _derive_financial_periods(selected_end_date)
    contractPeriods = _derive_quarter_periods(selected_end_date, n=9)
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    except Exception as e:
        logger.exception("连接 stock_tushare 失败（fina arturn+gpm）: %s", e)
        return {'rows': [], 'periods': periods, 'contract_periods': contractPeriods, 'error': '数据库连接失败'}

    sw_join_sql = _limit_stats_sw_join_sql('base.ts_code')
    has_fi_rt = _tushare_column_exists(conn, 'fina_indicator', 'report_type')
    fi_rt15 = (
        '               CASE WHEN fi.report_type = 1 THEN 0 ELSE 1 END, '
        if has_fi_rt else
        ''
    )
    fi_rt10 = (
        '          CASE WHEN fi.report_type = 1 THEN 0 ELSE 1 END, '
        if has_fi_rt else
        ''
    )
    has_bs_rt = _tushare_column_exists(conn, 'balancesheet', 'report_type')
    bs_rt_line = (
        '          CASE WHEN bs.report_type = 1 THEN 0 ELSE 1 END, '
        if has_bs_rt else
        ''
    )
    has_adv = _tushare_column_exists(conn, 'balancesheet', 'adv_receipts')
    if has_adv:
        bs_yi_expr = (
            '      ((COALESCE(bs.contract_liab, 0) + COALESCE(bs.adv_receipts, 0)) '
            '/ 100000000.0) AS contract_liab_yi, '
            '      (COALESCE(bs.fix_assets_total, 0) / 100000000.0) AS fix_assets_yi, '
        )
        bs_where_tail = (
            '      AND (bs.contract_liab IS NOT NULL OR bs.adv_receipts IS NOT NULL '
            'OR bs.fix_assets_total IS NOT NULL)\n'
        )
        bs_ord_data = (
            '          CASE WHEN (bs.contract_liab IS NOT NULL OR bs.adv_receipts IS NOT NULL) '
            'THEN 0 ELSE 1 END, '
        )
    else:
        bs_yi_expr = (
            '      (COALESCE(bs.contract_liab, 0) / 100000000.0) AS contract_liab_yi, '
            '      (COALESCE(bs.fix_assets_total, 0) / 100000000.0) AS fix_assets_yi, '
        )
        bs_where_tail = '      AND (bs.contract_liab IS NOT NULL OR bs.fix_assets_total IS NOT NULL)\n'
        bs_ord_data = '          CASE WHEN bs.contract_liab IS NOT NULL THEN 0 ELSE 1 END, '
    has_q_gr_yoy = _tushare_column_exists(conn, 'fina_indicator', 'q_gr_yoy')
    has_q_op_yoy = _tushare_column_exists(conn, 'fina_indicator', 'q_op_yoy')
    has_q_gsprofit_margin = _tushare_column_exists(conn, 'fina_indicator', 'q_gsprofit_margin')
    has_q_netprofit_margin = _tushare_column_exists(conn, 'fina_indicator', 'q_netprofit_margin')
    has_q_dt_roe = _tushare_column_exists(conn, 'fina_indicator', 'q_dt_roe')
    has_q_opincome = _tushare_column_exists(conn, 'fina_indicator', 'q_opincome')
    has_q_dtprofit = _tushare_column_exists(conn, 'fina_indicator', 'q_dtprofit')
    has_q_ocf_to_sales = _tushare_column_exists(conn, 'fina_indicator', 'q_ocf_to_sales')
    has_q_ocf_to_or = _tushare_column_exists(conn, 'fina_indicator', 'q_ocf_to_or')
    has_inc_rt = _tushare_column_exists(conn, 'income', 'report_type')
    has_cf_rt = _tushare_column_exists(conn, 'cashflow', 'report_type')
    has_n_cashflow_act = _tushare_column_exists(conn, 'cashflow', 'n_cashflow_act')

    # 单季度同比两组改为主查询后补齐，避免主 SQL 过重导致超时
    qgr_select_sql = (
        "  NULL AS r0, NULL AS r1, NULL AS r2, NULL AS r3, NULL AS r4, "
        "  NULL AS r5, NULL AS r6, NULL AS r7, NULL AS r8, "
    )
    qgr_join_sql = ""
    qop_select_sql = (
        "  NULL AS u0, NULL AS u1, NULL AS u2, NULL AS u3, NULL AS u4, "
        "  NULL AS u5, NULL AS u6, NULL AS u7, NULL AS u8 "
    )
    qop_join_sql = ""

    sql = (
        "SELECT "
        "  base.ts_code, "
        "  COALESCE(rt_name.name, '') AS sec_name, "
        "  COALESCE(rt_sw_map.sw_name, '') AS sw_name, "
        "  MAX(CASE WHEN fi_turn.end_date = %s THEN fi_turn.ann_date END) AS a0, "
        "  ROUND(MAX(CASE WHEN fi_turn.end_date = %s THEN fi_turn.roe_dt END), 2) AS roe0, "
        "  ROUND(MAX(CASE WHEN fi_turn.end_date = %s THEN fi_turn.roe_dt END), 2) AS roe1, "
        "  ROUND(MAX(CASE WHEN fi_turn.end_date = %s THEN fi_turn.roe_dt END), 2) AS roe2, "
        "  ROUND(MAX(CASE WHEN fi_turn.end_date = %s THEN fi_turn.roe_dt END), 2) AS roe3, "
        "  ROUND(MAX(CASE WHEN fi_turn.end_date = %s THEN fi_turn.arturn_days END), 2) AS p0, "
        "  ROUND(MAX(CASE WHEN fi_turn.end_date = %s THEN fi_turn.arturn_days END), 2) AS p1, "
        "  ROUND(MAX(CASE WHEN fi_turn.end_date = %s THEN fi_turn.arturn_days END), 2) AS p2, "
        "  ROUND(MAX(CASE WHEN fi_turn.end_date = %s THEN fi_turn.arturn_days END), 2) AS p3, "
        "  ROUND(MAX(CASE WHEN fi_gpm.end_date = %s THEN fi_gpm.gpm_val END), 2) AS g0, "
        "  ROUND(MAX(CASE WHEN fi_gpm.end_date = %s THEN fi_gpm.gpm_val END), 2) AS g1, "
        "  ROUND(MAX(CASE WHEN fi_gpm.end_date = %s THEN fi_gpm.gpm_val END), 2) AS g2, "
        "  ROUND(MAX(CASE WHEN fi_gpm.end_date = %s THEN fi_gpm.gpm_val END), 2) AS g3, "
        "  ROUND(MAX(CASE WHEN fi_ocf_opincome.end_date = %s THEN fi_ocf_opincome.ocf_to_opincome END), 2) AS c0, "
        "  ROUND(MAX(CASE WHEN fi_ocf_opincome.end_date = %s THEN fi_ocf_opincome.ocf_to_opincome END), 2) AS c1, "
        "  ROUND(MAX(CASE WHEN fi_ocf_opincome.end_date = %s THEN fi_ocf_opincome.ocf_to_opincome END), 2) AS c2, "
        "  ROUND(MAX(CASE WHEN fi_ocf_opincome.end_date = %s THEN fi_ocf_opincome.ocf_to_opincome END), 2) AS c3, "
        "  MAX(CASE WHEN fi_ocf_or.end_date = %s THEN fi_ocf_or.ocf_to_or END) AS o0, "
        "  MAX(CASE WHEN fi_ocf_or.end_date = %s THEN fi_ocf_or.ocf_to_or END) AS o1, "
        "  MAX(CASE WHEN fi_ocf_or.end_date = %s THEN fi_ocf_or.ocf_to_or END) AS o2, "
        "  MAX(CASE WHEN fi_ocf_or.end_date = %s THEN fi_ocf_or.ocf_to_or END) AS o3, "
        + qgr_select_sql +
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.contract_liab_yi END), 2) AS k0, "
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.contract_liab_yi END), 2) AS k1, "
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.contract_liab_yi END), 2) AS k2, "
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.contract_liab_yi END), 2) AS k3, "
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.contract_liab_yi END), 2) AS k4, "
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.contract_liab_yi END), 2) AS k5, "
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.contract_liab_yi END), 2) AS k6, "
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.contract_liab_yi END), 2) AS k7, "
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.contract_liab_yi END), 2) AS k8, "
        + qop_select_sql +
        ", ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.fix_assets_yi END), 2) AS f0, "
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.fix_assets_yi END), 2) AS f1, "
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.fix_assets_yi END), 2) AS f2, "
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.fix_assets_yi END), 2) AS f3, "
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.fix_assets_yi END), 2) AS f4, "
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.fix_assets_yi END), 2) AS f5, "
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.fix_assets_yi END), 2) AS f6, "
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.fix_assets_yi END), 2) AS f7, "
        "  ROUND(MAX(CASE WHEN bs_contract.end_date = %s THEN bs_contract.fix_assets_yi END), 2) AS f8, "
        "  MAX(sb.list_date) AS list_date, "
        "  MAX(sb.act_name) AS act_name, "
        "  MAX(sb.act_ent_type) AS act_ent_type "
        "FROM ("
        "  SELECT DISTINCT x.ts_code "
        "  FROM ("
        "    SELECT fi.ts_code, fi.end_date, "
        "           ROW_NUMBER() OVER ("
        "             PARTITION BY fi.ts_code, fi.end_date "
        "             ORDER BY "
        "               CASE WHEN fi.update_flag = '1' THEN 0 ELSE 1 END, "
        + fi_rt15 +
        "               fi.ann_date DESC"
        "           ) AS rn "
        "    FROM stock_tushare.fina_indicator fi "
        "    WHERE fi.end_date = %s"
        "  ) x "
        "  WHERE x.rn = 1"
        ") base "
        "INNER JOIN (SELECT DISTINCT ts_code FROM stock_wind.rt_k) rt_k_codes ON rt_k_codes.ts_code = base.ts_code "
        "LEFT JOIN ("
        "  SELECT y.ts_code, y.end_date, y.ann_date, y.arturn_days, y.roe_dt "
        "  FROM ("
        "    SELECT fi.ts_code, fi.end_date, fi.ann_date, fi.arturn_days, fi.roe_dt, "
        "           ROW_NUMBER() OVER ("
        "             PARTITION BY fi.ts_code, fi.end_date "
        "             ORDER BY "
        "               CASE WHEN fi.arturn_days IS NOT NULL THEN 0 ELSE 1 END, "
        "               CASE WHEN fi.update_flag = '1' THEN 0 ELSE 1 END, "
        + fi_rt15 +
        "               fi.ann_date DESC"
        "           ) AS rn "
        "    FROM stock_tushare.fina_indicator fi "
        "    WHERE fi.end_date IN (%s, %s, %s, %s)"
        "  ) y "
        "  WHERE y.rn = 1"
        ") fi_turn "
        "  ON fi_turn.ts_code = base.ts_code "
        "LEFT JOIN ("
        "  SELECT gm.ts_code, gm.end_date, gm.gpm_val "
        "  FROM ("
        "    SELECT fi.ts_code, fi.end_date, "
        "      COALESCE(fi.grossprofit_margin, fi.q_gsprofit_margin) AS gpm_val, "
        "      ROW_NUMBER() OVER ("
        "        PARTITION BY fi.ts_code, fi.end_date "
        "        ORDER BY "
        "          CASE WHEN COALESCE(fi.grossprofit_margin, fi.q_gsprofit_margin) IS NOT NULL "
        "            THEN 0 ELSE 1 END, "
        "          CASE WHEN fi.update_flag = '1' THEN 0 ELSE 1 END, "
        + fi_rt10 +
        "          fi.ann_date DESC"
        "      ) AS rn "
        "    FROM stock_tushare.fina_indicator fi "
        "    WHERE fi.end_date IN (%s, %s, %s, %s)"
        "  ) gm "
        "  WHERE gm.rn = 1"
        ") fi_gpm "
        "  ON fi_gpm.ts_code = base.ts_code "
        "LEFT JOIN ("
        "  SELECT oc.ts_code, oc.end_date, oc.ocf_to_opincome "
        "  FROM ("
        "    SELECT fi.ts_code, fi.end_date, fi.ocf_to_opincome, "
        "      ROW_NUMBER() OVER ("
        "        PARTITION BY fi.ts_code, fi.end_date "
        "        ORDER BY "
        "          CASE WHEN fi.ocf_to_opincome IS NOT NULL THEN 0 ELSE 1 END, "
        "          CASE WHEN fi.update_flag = '1' THEN 0 ELSE 1 END, "
        + fi_rt10 +
        "          fi.ann_date DESC"
        "      ) AS rn "
        "    FROM stock_tushare.fina_indicator fi "
        "    WHERE fi.end_date IN (%s, %s, %s, %s)"
        "  ) oc "
        "  WHERE oc.rn = 1"
        ") fi_ocf_opincome "
        "  ON fi_ocf_opincome.ts_code = base.ts_code "
        "LEFT JOIN ("
        "  SELECT oo.ts_code, oo.end_date, oo.ocf_to_or "
        "  FROM ("
        "    SELECT fi.ts_code, fi.end_date, fi.ocf_to_or, "
        "      ROW_NUMBER() OVER ("
        "        PARTITION BY fi.ts_code, fi.end_date "
        "        ORDER BY "
        "          CASE WHEN fi.ocf_to_or IS NOT NULL THEN 0 ELSE 1 END, "
        "          CASE WHEN fi.update_flag = '1' THEN 0 ELSE 1 END, "
        + fi_rt10 +
        "          fi.ann_date DESC"
        "      ) AS rn "
        "    FROM stock_tushare.fina_indicator fi "
        "    WHERE fi.end_date IN (%s, %s, %s, %s)"
        "  ) oo "
        "  WHERE oo.rn = 1"
        ") fi_ocf_or "
        "  ON fi_ocf_or.ts_code = base.ts_code "
        + qgr_join_sql +
        "LEFT JOIN ("
        "  SELECT bq.ts_code, bq.end_date, bq.contract_liab_yi, bq.fix_assets_yi "
        "  FROM ("
        "    SELECT bs.ts_code, bs.end_date, "
        + bs_yi_expr +
        "      ROW_NUMBER() OVER ("
        "        PARTITION BY bs.ts_code, bs.end_date "
        "        ORDER BY "
        + bs_ord_data +
        "          CASE WHEN bs.update_flag = '1' THEN 0 ELSE 1 END, "
        + bs_rt_line +
        "          bs.ann_date DESC"
        "      ) AS rn "
        "    FROM stock_tushare.balancesheet bs "
        "    WHERE bs.end_date IN (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        + bs_where_tail +
        "  ) bq "
        "  WHERE bq.rn = 1"
        ") bs_contract "
        "  ON bs_contract.ts_code = base.ts_code "
        + qop_join_sql +
        "LEFT JOIN ("
        "  SELECT z.ts_code, z.name "
        "  FROM ("
        "    SELECT ts_code, name, "
        "      ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_time DESC) AS rn "
        "    FROM stock_wind.rt_k "
        "    WHERE name IS NOT NULL AND name <> ''"
        "  ) z "
        "  WHERE z.rn = 1"
        ") rt_name ON rt_name.ts_code = base.ts_code "
        + sw_join_sql +
        "LEFT JOIN ("
        "  SELECT x.ts_code, x.list_date, x.act_name, x.act_ent_type "
        "  FROM ("
        "    SELECT sb.ts_code, sb.list_date, "
        "      COALESCE(sb.act_name, '') AS act_name, "
        "      COALESCE(sb.act_ent_type, '') AS act_ent_type, "
        "      ROW_NUMBER() OVER ("
        "        PARTITION BY sb.ts_code "
        "        ORDER BY "
        "          CASE WHEN sb.list_date IS NOT NULL THEN 0 ELSE 1 END, "
        "          sb.list_date DESC"
        "      ) AS rn "
        "    FROM stock_tushare.stock_basic sb "
        "  ) x "
        "  WHERE x.rn = 1"
        ") sb ON sb.ts_code = base.ts_code "
        "GROUP BY base.ts_code, rt_name.name, rt_sw_map.sw_name "
        "ORDER BY (p0 IS NULL), p0 ASC, base.ts_code ASC"
    )
    params_list = [
        periods[0],
        periods[0], periods[1], periods[2], periods[3],
        periods[0], periods[1], periods[2], periods[3],
        periods[0], periods[1], periods[2], periods[3],
        periods[0], periods[1], periods[2], periods[3],
        periods[0], periods[1], periods[2], periods[3],
    ]
    params_list.extend([
        contractPeriods[0], contractPeriods[1], contractPeriods[2], contractPeriods[3],
        contractPeriods[4], contractPeriods[5], contractPeriods[6], contractPeriods[7], contractPeriods[8],
    ])
    params_list.extend([
        contractPeriods[0], contractPeriods[1], contractPeriods[2], contractPeriods[3],
        contractPeriods[4], contractPeriods[5], contractPeriods[6], contractPeriods[7], contractPeriods[8],
    ])
    params_list.extend([
        periods[0],
        periods[0], periods[1], periods[2], periods[3],
        periods[0], periods[1], periods[2], periods[3],
        periods[0], periods[1], periods[2], periods[3],
        periods[0], periods[1], periods[2], periods[3],
    ])
    params_list.extend([
        contractPeriods[0], contractPeriods[1], contractPeriods[2], contractPeriods[3],
        contractPeriods[4], contractPeriods[5], contractPeriods[6], contractPeriods[7], contractPeriods[8],
    ])
    params = tuple(params_list)
    _stage_times: list[tuple[str, float]] = []
    _t_stage = time.perf_counter()
    try:
        _wall_t0 = time.perf_counter()
        # ========== B1 快速路径：瘦身 base SQL + Python 填值 ==========
        # 成功：跳过重型主 SQL；任何异常：logger.warning 后降级回原路径。
        rows: list[dict] | None = None
        try:
            rows = _fina_arturn_fast_path(
                conn,
                selected_end_date,
                periods,
                contractPeriods,
                has_fi_rt=has_fi_rt,
                has_bs_rt=has_bs_rt,
                has_adv=has_adv,
                sw_join_sql=sw_join_sql,
            )
        except Exception as _fast_e:
            logger.warning('fina_arturn 快速路径失败，降级回原主 SQL: %s', _fast_e)
            rows = None

        if rows is not None:
            _stage_times.append(('fast_path', time.perf_counter() - _t_stage))
            _t_stage = time.perf_counter()
        else:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                raw = cur.fetchall() or []
            _stage_times.append(('main_sql', time.perf_counter() - _t_stage))
            _t_stage = time.perf_counter()
            rows = []
            for r in raw:
                rows.append({
                    'ts_code': r[0] if len(r) > 0 else None,
                    'sec_name': r[1] if len(r) > 1 else '',
                    'sw_name': r[2] if len(r) > 2 else '',
                    'a0': str(r[3]).strip() if len(r) > 3 and r[3] is not None else '',
                    'roe0': _to_float_or_none(r[4] if len(r) > 4 else None),
                    'roe1': _to_float_or_none(r[5] if len(r) > 5 else None),
                    'roe2': _to_float_or_none(r[6] if len(r) > 6 else None),
                    'roe3': _to_float_or_none(r[7] if len(r) > 7 else None),
                    'p0': _to_float_or_none(r[8] if len(r) > 8 else None),
                    'p1': _to_float_or_none(r[9] if len(r) > 9 else None),
                    'p2': _to_float_or_none(r[10] if len(r) > 10 else None),
                    'p3': _to_float_or_none(r[11] if len(r) > 11 else None),
                    'g0': _to_float_or_none(r[12] if len(r) > 12 else None),
                    'g1': _to_float_or_none(r[13] if len(r) > 13 else None),
                    'g2': _to_float_or_none(r[14] if len(r) > 14 else None),
                    'g3': _to_float_or_none(r[15] if len(r) > 15 else None),
                    'c0': _to_float_or_none(r[16] if len(r) > 16 else None),
                    'c1': _to_float_or_none(r[17] if len(r) > 17 else None),
                    'c2': _to_float_or_none(r[18] if len(r) > 18 else None),
                    'c3': _to_float_or_none(r[19] if len(r) > 19 else None),
                    'o0': _ocf_to_or_display_pct(r[20] if len(r) > 20 else None),
                    'o1': _ocf_to_or_display_pct(r[21] if len(r) > 21 else None),
                    'o2': _ocf_to_or_display_pct(r[22] if len(r) > 22 else None),
                    'o3': _ocf_to_or_display_pct(r[23] if len(r) > 23 else None),
                    'r0': _to_float_or_none(r[24] if len(r) > 24 else None),
                    'r1': _to_float_or_none(r[25] if len(r) > 25 else None),
                    'r2': _to_float_or_none(r[26] if len(r) > 26 else None),
                    'r3': _to_float_or_none(r[27] if len(r) > 27 else None),
                    'r4': _to_float_or_none(r[28] if len(r) > 28 else None),
                    'r5': _to_float_or_none(r[29] if len(r) > 29 else None),
                    'r6': _to_float_or_none(r[30] if len(r) > 30 else None),
                    'r7': _to_float_or_none(r[31] if len(r) > 31 else None),
                    'r8': _to_float_or_none(r[32] if len(r) > 32 else None),
                    'k0': _to_float_or_none(r[33] if len(r) > 33 else None),
                    'k1': _to_float_or_none(r[34] if len(r) > 34 else None),
                    'k2': _to_float_or_none(r[35] if len(r) > 35 else None),
                    'k3': _to_float_or_none(r[36] if len(r) > 36 else None),
                    'k4': _to_float_or_none(r[37] if len(r) > 37 else None),
                    'k5': _to_float_or_none(r[38] if len(r) > 38 else None),
                    'k6': _to_float_or_none(r[39] if len(r) > 39 else None),
                    'k7': _to_float_or_none(r[40] if len(r) > 40 else None),
                    'k8': _to_float_or_none(r[41] if len(r) > 41 else None),
                    'u0': _to_float_or_none(r[42] if len(r) > 42 else None),
                    'u1': _to_float_or_none(r[43] if len(r) > 43 else None),
                    'u2': _to_float_or_none(r[44] if len(r) > 44 else None),
                    'u3': _to_float_or_none(r[45] if len(r) > 45 else None),
                    'u4': _to_float_or_none(r[46] if len(r) > 46 else None),
                    'u5': _to_float_or_none(r[47] if len(r) > 47 else None),
                    'u6': _to_float_or_none(r[48] if len(r) > 48 else None),
                    'u7': _to_float_or_none(r[49] if len(r) > 49 else None),
                    'u8': _to_float_or_none(r[50] if len(r) > 50 else None),
                    'f0': _to_float_or_none(r[51] if len(r) > 51 else None),
                    'f1': _to_float_or_none(r[52] if len(r) > 52 else None),
                    'f2': _to_float_or_none(r[53] if len(r) > 53 else None),
                    'f3': _to_float_or_none(r[54] if len(r) > 54 else None),
                    'f4': _to_float_or_none(r[55] if len(r) > 55 else None),
                    'f5': _to_float_or_none(r[56] if len(r) > 56 else None),
                    'f6': _to_float_or_none(r[57] if len(r) > 57 else None),
                    'f7': _to_float_or_none(r[58] if len(r) > 58 else None),
                    'f8': _to_float_or_none(r[59] if len(r) > 59 else None),
                    'list_date': str(r[60]).strip() if len(r) > 60 and r[60] is not None else '',
                    'act_name': str(r[61]).strip() if len(r) > 61 and r[61] is not None else '',
                    'act_ent_type': str(r[62]).strip() if len(r) > 62 and r[62] is not None else '',
                })
            _stage_times.append(('parse_main_rows', time.perf_counter() - _t_stage))
            _t_stage = time.perf_counter()
        # 一次性捞全 5 组 attach 所需的 fina_indicator 候选版本行，后续各 attach 在 Python 侧按自己口径选行，
        # 把原先对同一张百万级表的 5 次 ROW_NUMBER() 扫描压缩为 1 次普通 SELECT；collect 失败自动降级为原 SQL 路径。
        fi_ts_codes = list({str(r.get('ts_code') or '').strip() for r in rows if r.get('ts_code')})
        fi_periods = [str(p).strip() for p in (contractPeriods or [])[:9] if str(p).strip()]
        fi_cands = _fina_arturn_collect_fi_candidates(
            conn,
            fi_ts_codes,
            fi_periods,
            has_fi_rt=has_fi_rt,
            has_q_gr_yoy=has_q_gr_yoy,
            has_q_op_yoy=has_q_op_yoy,
            has_q_opincome=has_q_opincome,
            has_q_dtprofit=has_q_dtprofit,
            has_q_ocf_to_sales=has_q_ocf_to_sales,
            has_q_ocf_to_or=has_q_ocf_to_or,
            has_q_gsprofit_margin=has_q_gsprofit_margin,
            has_q_netprofit_margin=has_q_netprofit_margin,
            has_q_dt_roe=has_q_dt_roe,
        ) or None
        _stage_times.append(('collect_fi_cands', time.perf_counter() - _t_stage))
        _pa_workers = FINA_ARTURN_ATTACH_PARALLEL_WORKERS
        # v 依赖行内 r/u（quarter_yoy），不可与 quarter_yoy 同批并行，否则易读到未写入的 None
        _stage_times.extend(_fina_arturn_parallel_attaches([
            (
                'quarter_yoy',
                _fina_arturn_attach_quarter_yoy,
                (rows, contractPeriods, has_q_gr_yoy, has_q_op_yoy, has_fi_rt),
                {'fi_cands': fi_cands},
            ),
            (
                'quarter_revenue',
                _fina_arturn_attach_quarter_revenue_yi,
                (rows, contractPeriods, has_inc_rt),
                {},
            ),
            (
                'quarter_cashflow',
                _fina_arturn_attach_quarter_cashflow_act_yi,
                (rows, contractPeriods, has_n_cashflow_act, has_cf_rt),
                {},
            ),
        ], max_workers=_pa_workers))
        _t_stage = time.perf_counter()
        _fina_arturn_attach_quarter_op_yoy_excess_gr_yoy(
            conn, rows, contractPeriods, has_inc_rt,
        )
        _stage_times.append(('op_yoy_excess_gr', time.perf_counter() - _t_stage))
        _stage_times.extend(_fina_arturn_parallel_attaches([
            (
                'q_opincome',
                _fina_arturn_attach_q_opincome_yi,
                (rows, contractPeriods, has_q_opincome, has_fi_rt),
                {'fi_cands': fi_cands},
            ),
            (
                'q_dtprofit',
                _fina_arturn_attach_q_dtprofit_yi,
                (rows, contractPeriods, has_q_dtprofit, has_fi_rt),
                {'fi_cands': fi_cands},
            ),
            (
                'q_ocf_pct',
                _fina_arturn_attach_quarter_q_ocf_pct,
                (rows, contractPeriods, has_q_ocf_to_sales, has_q_ocf_to_or, has_fi_rt),
                {'fi_cands': fi_cands},
            ),
            (
                'quarter_margins',
                _fina_arturn_attach_quarter_margins,
                (rows, contractPeriods, has_q_gsprofit_margin, has_q_netprofit_margin, has_q_dt_roe, has_fi_rt),
                {'fi_cands': fi_cands},
            ),
        ], max_workers=_pa_workers))
        _t_stage = time.perf_counter()
        if _tushare_column_exists(conn, 'fina_quarter_metrics', 'q_fcf_star'):
            _fina_arturn_attach_q_fcf_star_yi(conn, rows, contractPeriods)
        _stage_times.append(('q_fcf_star', time.perf_counter() - _t_stage))
        _stage_times.extend(_fina_arturn_parallel_attaches([
            (
                'ar_receivable_pct',
                _fina_arturn_attach_ar_receivable_pct,
                (rows, periods[0]),
                {},
            ),
            (
                'inventories',
                _fina_arturn_attach_inventory_metrics,
                (rows, contractPeriods, has_bs_rt),
                {},
            ),
            (
                'top10_holders_top3',
                _fina_arturn_attach_top10_holders_top3,
                (rows,),
                {},
            ),
            (
                'stk_holdernumber_q',
                _fina_arturn_attach_stk_holdernumber_quarters,
                (rows, contractPeriods),
                {},
            ),
        ], max_workers=_pa_workers))
        _stage_times.extend(_fina_arturn_parallel_attaches([
            (
                'last_his_high',
                _fina_arturn_attach_last_his_high_flag,
                (rows,),
                {},
            ),
            (
                'last_850_high',
                _fina_arturn_attach_last_850_high_flag,
                (rows,),
                {},
            ),
            (
                'qfq_close_break_first_his',
                _fina_arturn_attach_qfq_close_break_first_his,
                (rows,),
                {},
            ),
            (
                'qfq_close_break_first_850',
                _fina_arturn_attach_qfq_close_break_first_850,
                (rows,),
                {},
            ),
            (
                'price_range_pct',
                _fina_arturn_attach_price_range_pct,
                (rows,),
                {},
            ),
            (
                'pct_since_ann',
                _fina_arturn_attach_pct_since_ann,
                (rows,),
                {},
            ),
            (
                'stock_company_fields',
                _fina_arturn_attach_stock_company_fields,
                (rows,),
                {},
            ),
            (
                'stock_basic_act',
                _fina_arturn_attach_stock_basic_act,
                (rows,),
                {},
            ),
            (
                'fqm_debt_ratios',
                lambda conn, r, anchor: _fina_arturn_attach_fqm_debt_ratios(r, conn, anchor),
                (rows, periods[0]),
                {},
            ),
        ], max_workers=_pa_workers))
        _t_stage = time.perf_counter()
        _fina_arturn_attach_stk_factor_mv_pe(rows, conn_ext=conn)
        _stage_times.append(('stk_factor_mv_pe', time.perf_counter() - _t_stage))
        _t_stage = time.perf_counter()
        _fina_arturn_attach_money_cap_ratio(rows, conn, periods[0])
        _stage_times.append(('money_cap_ratio', time.perf_counter() - _t_stage))
        _t_stage = time.perf_counter()
        _fina_arturn_attach_free_float_ratio_pct(rows)
        _stage_times.append(('free_float_ratio_pct', time.perf_counter() - _t_stage))
        _t_stage = time.perf_counter()
        _fina_arturn_attach_contract_k_high(rows)
        _stage_times.append(('contract_k_high', time.perf_counter() - _t_stage))
        _t_stage = time.perf_counter()
        _fina_arturn_attach_hn_qoq_low(rows)
        _stage_times.append(('hn_qoq_low', time.perf_counter() - _t_stage))
        # 阶段耗时埋点：默认 DEBUG 级不输出，性能退化排查时把 rt_monitor 日志级改 DEBUG 即可。
        # 关键字：fina_arturn_stage_timing（配合 fast_path / main_sql 可一眼分辨走了新路径还是降级）。
        if logger.isEnabledFor(10):  # logging.DEBUG
            logger.debug(
                "fina_arturn_stage_timing end=%s rows=%d | %s",
                selected_end_date,
                len(rows),
                ' '.join(f'{name}={dt:.3f}s' for name, dt in _stage_times),
            )
        _wall_elapsed = time.perf_counter() - _wall_t0
        if _wall_elapsed >= 4.0:
            logger.warning(
                "fina_arturn 慢查询 wall=%.2fs end=%s rows=%d | %s",
                _wall_elapsed,
                selected_end_date,
                len(rows),
                ' '.join(f'{name}={dt:.3f}s' for name, dt in _stage_times),
            )
        dataVersion = _fina_arturn_build_data_version(selected_end_date, len(rows))
        return {
            'rows': rows,
            'periods': periods,
            'contract_periods': contractPeriods,
            'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'data_version': dataVersion,
        }
    except Exception as e:
        logger.exception("查询 fina 多财务指标表失败: %s", e)
        return {'rows': [], 'periods': periods, 'contract_periods': contractPeriods, 'error': str(e)}
    finally:
        conn.close()


def _fina_revenue_cumulative_to_single_quarter_yi(rows_chrono_asc: list[dict]) -> list[dict]:
    """利润表 total_revenue 为年内累计：转单季营收（亿元）。Q1=当季累计；Q2/Q3/Q4=本行累计减年内上一披露期累计。
    固定资产、存货、在建工程、合同负债均为报告期时点值，原样取自 fix_assets_yi / inventories_yi / cip_yi / contract_liab_yi。"""
    by_date: dict[str, float | None] = {}
    for r in rows_chrono_asc:
        ed = str(r.get('end_date') or '')[:10]
        if len(ed) == 10:
            by_date[ed] = _to_float_or_none(r.get('revenue_cum_yi'))
    out: list[dict] = []
    for r in rows_chrono_asc:
        ed = str(r.get('end_date') or '')[:10]
        fix_yi = _to_float_or_none(r.get('fix_assets_yi'))
        sq: float | None = None
        if len(ed) == 10:
            y, m = ed[:4], ed[5:7]
            cum = by_date.get(ed)
            if cum is not None:
                if m == '03':
                    sq = cum
                elif m == '06':
                    pv = by_date.get(f'{y}-03-31')
                    sq = (cum - pv) if pv is not None else None
                elif m == '09':
                    pv = by_date.get(f'{y}-06-30')
                    sq = (cum - pv) if pv is not None else None
                elif m == '12':
                    pv = by_date.get(f'{y}-09-30')
                    sq = (cum - pv) if pv is not None else None
        if sq is not None:
            sq = round(float(sq), 2)
        inv_yi = _to_float_or_none(r.get('inventories_yi'))
        cip_yi = _to_float_or_none(r.get('cip_yi'))
        cl_yi = _to_float_or_none(r.get('contract_liab_yi'))
        out.append({
            'end_date': ed if len(ed) == 10 else str(r.get('end_date')),
            'fix_assets_yi': fix_yi,
            'inventories_yi': inv_yi,
            'cip_yi': cip_yi,
            'q_revenue_yi': sq,
            'contract_liab_yi': cl_yi,
        })
    return out


def _fina_fixed_assets_cf_cum_yuan_by_iso(
    conn,
    ts: str,
    rows_chrono_asc: list[dict],
    has_cf_rt: bool,
) -> dict[str, float | None]:
    """各报告期经营现金流累计（元），键为 YYYY-MM-DD；无 cashflow 列时返回空 dict。"""
    if not _tushare_column_exists(conn, 'cashflow', 'n_cashflow_act'):
        return {}
    date_keys: set[str] = set()
    for r in rows_chrono_asc:
        ed = str(r.get('end_date') or '')[:10]
        if len(ed) == 10:
            date_keys.add(ed.replace('-', ''))
    if not date_keys:
        return {}
    cf_nested = _fina_batch_cashflow_n_cashflow_act(conn, [ts], date_keys, has_cf_rt)
    cf_ts = cf_nested.get(ts, {})
    out: dict[str, float | None] = {}
    for k8, v in cf_ts.items():
        ks = str(k8 or '')
        if len(ks) != 8 or not ks.isdigit():
            continue
        iso = f'{ks[:4]}-{ks[4:6]}-{ks[6:8]}'
        try:
            out[iso] = float(v) if v is not None else None
        except (TypeError, ValueError):
            out[iso] = None
    return out


def _fina_cf_cumulative_to_single_quarter_yi(
    rows_chrono_asc: list[dict],
    cum_yuan_by_iso: dict[str, float | None],
) -> dict[str, float | None]:
    """n_cashflow_act 元累计 → 单季亿元，键与 rows 的 end_date（YYYY-MM-DD）对齐。口径同单季营收。"""
    by_date: dict[str, float | None] = {}
    for k, v in (cum_yuan_by_iso or {}).items():
        by_date[str(k)[:10]] = v
    out: dict[str, float | None] = {}
    for r in rows_chrono_asc:
        ed = str(r.get('end_date') or '')[:10]
        sq_yuan: float | None = None
        if len(ed) == 10:
            y, m = ed[:4], ed[5:7]
            cum = by_date.get(ed)
            if cum is not None:
                if m == '03':
                    sq_yuan = cum
                elif m == '06':
                    pv = by_date.get(f'{y}-03-31')
                    sq_yuan = (cum - pv) if pv is not None else None
                elif m == '09':
                    pv = by_date.get(f'{y}-06-30')
                    sq_yuan = (cum - pv) if pv is not None else None
                elif m == '12':
                    pv = by_date.get(f'{y}-09-30')
                    sq_yuan = (cum - pv) if pv is not None else None
        yi = None if sq_yuan is None else round(float(sq_yuan) / 100_000_000.0, 2)
        if len(ed) == 10:
            out[ed] = yi
    return out


def _fina_fixed_assets_batch_fi_quarters(
    conn,
    ts: str,
    end_dates_iso: list[str],
) -> dict[str, dict[str, float | None]]:
    """按报告期取 fina_indicator：单季经营活动净收益(亿)、扣非净利润(亿,q_dtprofit/1e8)、毛利率%、净利率%、ROE 扣非%、q_gr_yoy/q_gr_qoq/q_op_yoy/q_op_qoq/q_netprofit_yoy/q_netprofit_qoq（与财报表成长性列同源）。键为 YYYY-MM-DD；选行口径与财报表 q_* 补齐一致。"""
    out: dict[str, dict[str, float | None]] = {}
    if not end_dates_iso:
        return out
    has_fi_rt = _tushare_column_exists(conn, 'fina_indicator', 'report_type')
    has_oi = _tushare_column_exists(conn, 'fina_indicator', 'q_opincome')
    has_gs = _tushare_column_exists(conn, 'fina_indicator', 'q_gsprofit_margin')
    has_roe = _tushare_column_exists(conn, 'fina_indicator', 'q_dt_roe')
    has_dtp = _tushare_column_exists(conn, 'fina_indicator', 'q_dtprofit')
    has_nm = _tushare_column_exists(conn, 'fina_indicator', 'q_netprofit_margin')
    has_q_gr_yoy = _tushare_column_exists(conn, 'fina_indicator', 'q_gr_yoy')
    has_q_gr_qoq = _tushare_column_exists(conn, 'fina_indicator', 'q_gr_qoq')
    has_q_op_yoy = _tushare_column_exists(conn, 'fina_indicator', 'q_op_yoy')
    has_q_op_qoq = _tushare_column_exists(conn, 'fina_indicator', 'q_op_qoq')
    has_q_np_yoy = _tushare_column_exists(conn, 'fina_indicator', 'q_netprofit_yoy')
    has_q_np_qoq = _tushare_column_exists(conn, 'fina_indicator', 'q_netprofit_qoq')
    if not (
        has_oi or
        has_gs or
        has_roe or
        has_dtp or
        has_nm or
        has_q_gr_yoy or
        has_q_gr_qoq or
        has_q_op_yoy or
        has_q_op_qoq or
        has_q_np_yoy or
        has_q_np_qoq
    ):
        return out
    date_objs: list = []
    seen: set[str] = set()
    for ed in end_dates_iso:
        e10 = str(ed)[:10]
        if len(e10) != 10 or e10 in seen:
            continue
        seen.add(e10)
        try:
            date_objs.append(datetime.strptime(e10, '%Y-%m-%d').date())
        except Exception:
            continue
    if not date_objs:
        return out
    fi_rt_line = (
        '      CASE WHEN fi.report_type = 1 THEN 0 ELSE 1 END, '
        if has_fi_rt else
        ''
    )
    ph = ','.join(['%s'] * len(date_objs))
    sel_oi = 'fi.q_opincome' if has_oi else 'NULL'
    sel_gs = 'fi.q_gsprofit_margin' if has_gs else 'NULL'
    sel_roe = 'fi.q_dt_roe' if has_roe else 'NULL'
    sel_dtp = 'fi.q_dtprofit' if has_dtp else 'NULL'
    sel_nm = 'fi.q_netprofit_margin' if has_nm else 'NULL'
    sel_gr = 'fi.q_gr_yoy' if has_q_gr_yoy else 'NULL'
    sel_gr_qoq = 'fi.q_gr_qoq' if has_q_gr_qoq else 'NULL'
    sel_op = 'fi.q_op_yoy' if has_q_op_yoy else 'NULL'
    sel_op_qoq = 'fi.q_op_qoq' if has_q_op_qoq else 'NULL'
    sel_np = 'fi.q_netprofit_yoy' if has_q_np_yoy else 'NULL'
    sel_np_qoq = 'fi.q_netprofit_qoq' if has_q_np_qoq else 'NULL'
    ord_oi = 'CASE WHEN fi.q_opincome IS NOT NULL THEN 0 ELSE 1 END, ' if has_oi else ''
    ord_gs = 'CASE WHEN fi.q_gsprofit_margin IS NOT NULL THEN 0 ELSE 1 END, ' if has_gs else ''
    ord_roe = 'CASE WHEN fi.q_dt_roe IS NOT NULL THEN 0 ELSE 1 END, ' if has_roe else ''
    ord_dtp = 'CASE WHEN fi.q_dtprofit IS NOT NULL THEN 0 ELSE 1 END, ' if has_dtp else ''
    ord_nm = 'CASE WHEN fi.q_netprofit_margin IS NOT NULL THEN 0 ELSE 1 END, ' if has_nm else ''
    ord_gr = 'CASE WHEN fi.q_gr_yoy IS NOT NULL THEN 0 ELSE 1 END, ' if has_q_gr_yoy else ''
    ord_gr_qoq = 'CASE WHEN fi.q_gr_qoq IS NOT NULL THEN 0 ELSE 1 END, ' if has_q_gr_qoq else ''
    ord_op = 'CASE WHEN fi.q_op_yoy IS NOT NULL THEN 0 ELSE 1 END, ' if has_q_op_yoy else ''
    ord_op_qoq = 'CASE WHEN fi.q_op_qoq IS NOT NULL THEN 0 ELSE 1 END, ' if has_q_op_qoq else ''
    ord_np = 'CASE WHEN fi.q_netprofit_yoy IS NOT NULL THEN 0 ELSE 1 END, ' if has_q_np_yoy else ''
    ord_np_qoq = 'CASE WHEN fi.q_netprofit_qoq IS NOT NULL THEN 0 ELSE 1 END, ' if has_q_np_qoq else ''
    sql = (
        'SELECT z.end_date, z.q_opincome, z.q_gsprofit_margin, z.q_dt_roe, z.q_dtprofit, z.q_netprofit_margin, '
        'z.q_gr_yoy, z.q_gr_qoq, z.q_op_yoy, z.q_op_qoq, z.q_netprofit_yoy, z.q_netprofit_qoq FROM ('
        '  SELECT fi.end_date, '
        f'    {sel_oi} AS q_opincome, {sel_gs} AS q_gsprofit_margin, {sel_roe} AS q_dt_roe, '
        f'    {sel_dtp} AS q_dtprofit, {sel_nm} AS q_netprofit_margin, '
        f'    {sel_gr} AS q_gr_yoy, {sel_gr_qoq} AS q_gr_qoq, {sel_op} AS q_op_yoy, {sel_op_qoq} AS q_op_qoq, '
        f'    {sel_np} AS q_netprofit_yoy, {sel_np_qoq} AS q_netprofit_qoq, '
        '    ROW_NUMBER() OVER ('
        '      PARTITION BY fi.end_date '
        '      ORDER BY '
        f'        {ord_oi}{ord_gs}{ord_roe}{ord_dtp}{ord_nm}{ord_gr}{ord_gr_qoq}{ord_op}{ord_op_qoq}{ord_np}{ord_np_qoq}'
        "        CASE WHEN fi.update_flag = '1' THEN 0 ELSE 1 END, "
        f'        {fi_rt_line}'
        '        fi.ann_date DESC'
        '    ) AS rn '
        '  FROM stock_tushare.fina_indicator fi '
        f'  WHERE fi.ts_code = %s AND fi.end_date IN ({ph})'
        ') z WHERE z.rn = 1'
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (ts,) + tuple(date_objs))
            raw = cur.fetchall() or []
    except Exception as e:
        logger.debug('fixed_assets batch fi quarters: %s', e)
        return out
    for row in raw:
        if not row or row[0] is None:
            continue
        ed0 = row[0]
        ed_s = ed0.isoformat()[:10] if hasattr(ed0, 'isoformat') else str(ed0)[:10]
        if len(ed_s) != 10:
            continue
        qoi = _to_float_or_none(row[1]) if len(row) > 1 else None
        qgs = _to_float_or_none(row[2]) if len(row) > 2 else None
        qro = _to_float_or_none(row[3]) if len(row) > 3 else None
        qdtp = _to_float_or_none(row[4]) if len(row) > 4 else None
        qnm = _to_float_or_none(row[5]) if len(row) > 5 else None
        qgry = _to_float_or_none(row[6]) if len(row) > 6 else None
        qgrq = _to_float_or_none(row[7]) if len(row) > 7 else None
        qopy = _to_float_or_none(row[8]) if len(row) > 8 else None
        qopq = _to_float_or_none(row[9]) if len(row) > 9 else None
        qnpy = _to_float_or_none(row[10]) if len(row) > 10 else None
        qnpq = _to_float_or_none(row[11]) if len(row) > 11 else None
        oi_yi = None if qoi is None else round(float(qoi) / 100_000_000.0, 2)
        dtp_yi = None if qdtp is None else round(float(qdtp) / 100_000_000.0, 2)
        gs_pct = None if qgs is None else round(float(qgs), 2)
        roe_pct = None if qro is None else round(float(qro), 2)
        nm_pct = None if qnm is None else round(float(qnm), 2)
        gr_yoy = None if qgry is None else round(float(qgry), 2)
        gr_qoq = None if qgrq is None else round(float(qgrq), 2)
        op_yoy = None if qopy is None else round(float(qopy), 2)
        op_qoq = None if qopq is None else round(float(qopq), 2)
        np_yoy = None if qnpy is None else round(float(qnpy), 2)
        np_qoq = None if qnpq is None else round(float(qnpq), 2)
        out[ed_s] = {
            'q_opincome_yi': oi_yi,
            'q_dtprofit_yi': dtp_yi,
            'q_gsprofit_margin': gs_pct,
            'q_netprofit_margin': nm_pct,
            'q_dt_roe': roe_pct,
            'q_gr_yoy': gr_yoy,
            'q_gr_qoq': gr_qoq,
            'q_op_yoy': op_yoy,
            'q_op_qoq': op_qoq,
            'q_netprofit_yoy': np_yoy,
            'q_netprofit_qoq': np_qoq,
        }
    return out


def _fina_fixed_assets_batch_fqm_quarters(
    conn,
    ts: str,
    end_dates_iso: list[str],
) -> dict[str, dict[str, float | None]]:
    """按报告期批量取 fina_quarter_metrics 的模块A/B/C/D字段（现金、周转、杠杆、ROIC）。"""
    out: dict[str, dict[str, float | None]] = {}
    if not end_dates_iso:
        return out
    has_q_ocf = _tushare_column_exists(conn, 'fina_quarter_metrics', 'q_ocf')
    has_q_capex = _tushare_column_exists(conn, 'fina_quarter_metrics', 'q_capex')
    has_q_fcf = _tushare_column_exists(conn, 'fina_quarter_metrics', 'q_fcf_star')
    has_ar_days = _tushare_column_exists(conn, 'fina_quarter_metrics', 'ar_days_q')
    has_inv_days = _tushare_column_exists(conn, 'fina_quarter_metrics', 'inv_days_q')
    has_ap_days = _tushare_column_exists(conn, 'fina_quarter_metrics', 'ap_days_q')
    has_ccc = _tushare_column_exists(conn, 'fina_quarter_metrics', 'ccc_q')
    has_debt_to_assets = _tushare_column_exists(conn, 'fina_quarter_metrics', 'debt_to_assets')
    has_interestdebt_ratio = _tushare_column_exists(conn, 'fina_quarter_metrics', 'interestdebt_ratio')
    has_netdebt = _tushare_column_exists(conn, 'fina_quarter_metrics', 'netdebt')
    # 运行时 DDL（新增 netdebt_opt）后，避免沿用进程内旧缓存导致误判缺列。
    _TUSHARE_COLUMN_EXISTS_CACHE.pop(
        ((DB_CONFIG_TUSHARE.get('database') or 'stock_tushare'), 'fina_quarter_metrics', 'netdebt_opt'),
        None,
    )
    has_netdebt_opt = _tushare_column_exists(conn, 'fina_quarter_metrics', 'netdebt_opt')
    has_roic_sq = _tushare_column_exists(conn, 'fina_quarter_metrics', 'roic_sq_pct_avg_ic')
    if not (
        has_q_ocf or has_q_capex or has_q_fcf or has_ar_days or has_inv_days or has_ap_days or has_ccc
        or has_debt_to_assets or has_interestdebt_ratio or has_netdebt or has_netdebt_opt or has_roic_sq
    ):
        return out
    date_objs: list = []
    seen: set[str] = set()
    for ed in end_dates_iso:
        e10 = str(ed)[:10]
        if len(e10) != 10 or e10 in seen:
            continue
        seen.add(e10)
        try:
            date_objs.append(datetime.strptime(e10, '%Y-%m-%d').date())
        except Exception:
            continue
    if not date_objs:
        return out
    ph = ','.join(['%s'] * len(date_objs))
    sel_q_ocf = 'q_ocf' if has_q_ocf else 'NULL'
    sel_q_capex = 'q_capex' if has_q_capex else 'NULL'
    sel_q_fcf = 'q_fcf_star' if has_q_fcf else 'NULL'
    sel_ar_days = 'ar_days_q' if has_ar_days else 'NULL'
    sel_inv_days = 'inv_days_q' if has_inv_days else 'NULL'
    sel_ap_days = 'ap_days_q' if has_ap_days else 'NULL'
    sel_ccc = 'ccc_q' if has_ccc else 'NULL'
    sel_debt_to_assets = 'debt_to_assets' if has_debt_to_assets else 'NULL'
    sel_interestdebt_ratio = 'interestdebt_ratio' if has_interestdebt_ratio else 'NULL'
    if has_netdebt_opt and has_netdebt:
        sel_netdebt = 'COALESCE(netdebt_opt, netdebt)'
    elif has_netdebt_opt:
        sel_netdebt = 'netdebt_opt'
    elif has_netdebt:
        sel_netdebt = 'netdebt'
    else:
        sel_netdebt = 'NULL'
    sel_roic_sq = 'roic_sq_pct_avg_ic' if has_roic_sq else 'NULL'
    sql = (
        f'SELECT end_date, {sel_q_ocf} AS q_ocf, {sel_q_capex} AS q_capex, {sel_q_fcf} AS q_fcf_star, '
        f'{sel_ar_days} AS ar_days_q, {sel_inv_days} AS inv_days_q, {sel_ap_days} AS ap_days_q, {sel_ccc} AS ccc_q, '
        f'{sel_debt_to_assets} AS debt_to_assets, {sel_interestdebt_ratio} AS interestdebt_ratio, {sel_netdebt} AS netdebt, '
        f'{sel_roic_sq} AS roic_sq_pct_avg_ic '
        'FROM stock_tushare.fina_quarter_metrics '
        f'WHERE ts_code = %s AND end_date IN ({ph})'
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (ts,) + tuple(date_objs))
            raw = cur.fetchall() or []
    except Exception as e:
        # 兼容服务进程内 information_schema 列缓存陈旧：
        # 运行时新增 netdebt_opt 可能触发 1054，降级为 netdebt 重试。
        msg = str(e)
        if 'Unknown column' in msg and 'netdebt_opt' in msg and has_netdebt:
            sql = (
                f'SELECT end_date, {sel_q_ocf} AS q_ocf, {sel_q_capex} AS q_capex, {sel_q_fcf} AS q_fcf_star, '
                f'{sel_ar_days} AS ar_days_q, {sel_inv_days} AS inv_days_q, {sel_ap_days} AS ap_days_q, {sel_ccc} AS ccc_q, '
                f'{sel_debt_to_assets} AS debt_to_assets, {sel_interestdebt_ratio} AS interestdebt_ratio, netdebt AS netdebt, '
                f'{sel_roic_sq} AS roic_sq_pct_avg_ic '
                'FROM stock_tushare.fina_quarter_metrics '
                f'WHERE ts_code = %s AND end_date IN ({ph})'
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, (ts,) + tuple(date_objs))
                    raw = cur.fetchall() or []
            except Exception as e2:
                logger.debug('fixed_assets batch fina_quarter_metrics fallback: %s', e2)
                return out
        else:
            logger.debug('fixed_assets batch fina_quarter_metrics: %s', e)
            return out

    def _to_yi(v):
        fv = _to_float_or_none(v)
        return None if fv is None else round(float(fv) / 100_000_000.0, 2)

    for row in raw:
        if not row or row[0] is None:
            continue
        ed0 = row[0]
        ed_s = ed0.isoformat()[:10] if hasattr(ed0, 'isoformat') else str(ed0)[:10]
        if len(ed_s) != 10:
            continue
        out[ed_s] = {
            'q_ocf_yi': _to_yi(row[1] if len(row) > 1 else None),
            'q_capex_yi': _to_yi(row[2] if len(row) > 2 else None),
            'q_fcf_star_yi': _to_yi(row[3] if len(row) > 3 else None),
            'ar_days_q': _to_float_or_none(row[4] if len(row) > 4 else None),
            'inv_days_q': _to_float_or_none(row[5] if len(row) > 5 else None),
            'ap_days_q': _to_float_or_none(row[6] if len(row) > 6 else None),
            'ccc_q': _to_float_or_none(row[7] if len(row) > 7 else None),
            'debt_to_assets': _to_float_or_none(row[8] if len(row) > 8 else None),
            'interestdebt_ratio': _to_float_or_none(row[9] if len(row) > 9 else None),
            'netdebt_yi': _to_yi(row[10] if len(row) > 10 else None),
            'roic_sq_pct_avg_ic': _to_float_or_none(row[11] if len(row) > 11 else None),
        }
    return out


def _sql_fina_fixed_assets_revenue_series(
    use_bs_report_type: bool,
    use_inc_report_type: bool,
    has_adv: bool,
    has_cip: bool,
    has_inventories: bool,
) -> str:
    """balancesheet+income 同报告期合并行；income.total_revenue 为累计口径（亿元），下游再拆单季。
    合同负债（亿）：与 fina_arturn_days_table 一致——有 adv_receipts 列为 contract_liab+adv_receipts，否则仅 contract_liab。"""
    bs_rt_ord = 'CASE WHEN b.report_type = 1 THEN 0 ELSE 1 END, ' if use_bs_report_type else ''
    inc_rt_ord = 'CASE WHEN i.report_type = 1 THEN 0 ELSE 1 END, ' if use_inc_report_type else ''
    if has_adv:
        cl_inner = (
            'ROUND((COALESCE(b.contract_liab, 0) + COALESCE(b.adv_receipts, 0)) / 100000000.0, 2) AS contract_liab_yi, '
        )
    else:
        cl_inner = 'ROUND(COALESCE(b.contract_liab, 0) / 100000000.0, 2) AS contract_liab_yi, '
    cip_select = (
        "ROUND(z.cip_total / 100000000.0, 2) AS cip_yi "
        if has_cip else
        "NULL AS cip_yi "
    )
    inv_select = (
        "ROUND(z.inventories / 100000000.0, 2) AS inventories_yi, "
        if has_inventories else
        "NULL AS inventories_yi, "
    )
    cip_inner = "b.cip_total, " if has_cip else "NULL AS cip_total, "
    inv_inner = "b.inventories, " if has_inventories else "NULL AS inventories, "
    cip_ord = "        CASE WHEN b.cip_total IS NOT NULL THEN 0 ELSE 1 END, " if has_cip else ""
    inv_ord = "        CASE WHEN b.inventories IS NOT NULL THEN 0 ELSE 1 END, " if has_inventories else ""
    cip_where = " OR b.cip_total IS NOT NULL" if has_cip else ""
    inv_where = " OR b.inventories IS NOT NULL" if has_inventories else ""
    return (
        "SELECT z.end_date, "
        "ROUND(z.fix_assets_total / 100000000.0, 2) AS fix_assets_yi, "
        "ROUND(z.total_revenue / 100000000.0, 2) AS revenue_cum_yi, "
        "z.contract_liab_yi, "
        + inv_select
        + cip_select
        + "FROM ("
        + "  SELECT "
        + "    b.end_date, b.fix_assets_total, i.total_revenue, "
        + inv_inner
        + cip_inner
        + cl_inner
        + "    ROW_NUMBER() OVER ("
        + "      PARTITION BY b.end_date "
        + "      ORDER BY "
        + "        CASE WHEN b.fix_assets_total IS NOT NULL THEN 0 ELSE 1 END, "
        + "        CASE WHEN i.total_revenue IS NOT NULL THEN 0 ELSE 1 END, "
        + inv_ord
        + cip_ord
        + "        CASE WHEN b.update_flag = '1' THEN 0 ELSE 1 END, "
        + bs_rt_ord
        + "        b.ann_date DESC, "
        + "        CASE WHEN i.update_flag = '1' THEN 0 ELSE 1 END, "
        + inc_rt_ord
        + "        i.ann_date DESC "
        + "    ) AS rn "
        + "  FROM stock_tushare.balancesheet b "
        + "  INNER JOIN stock_tushare.income i "
        + "    ON i.ts_code = b.ts_code "
        + "   AND i.end_date = b.end_date "
        + "  WHERE b.ts_code = %s "
        + "    AND b.end_date <= %s "
        + "    AND b.end_date IS NOT NULL "
        + "    AND (b.fix_assets_total IS NOT NULL OR i.total_revenue IS NOT NULL"
        + inv_where
        + cip_where
        + ")"
        + ") z "
        + "WHERE z.rn = 1 "
        + "ORDER BY z.end_date DESC "
        + "LIMIT %s"
    )


def _fina_fixed_assets_modal_fill_fqm(
    conn,
    ts: str,
    ed_obj,
    anchor_yyyymmdd: str,
    out: dict[str, float | None],
) -> None:
    """从 stock_tushare.fina_quarter_metrics 补缺：FCF*(TTM 亿)、净债务/净现金(亿)、资产负债率(%)、CCC。

    FCF* TTM：锚定日前连续四季 q_fcf_star（元）之和 ÷ 1e8；任一季缺失则整条 TTM 为空。
    锚定行为 end_date = 所选报告期末；净债务优先 netdebt_opt，缺失回退 netdebt；正偏净负债、负可理解为净现金（元÷1e8）。"""
    out.setdefault('fcf_star_ttm_yi', None)
    out.setdefault('netdebt_yi', None)
    out.setdefault('debt_to_assets', None)
    out.setdefault('ccc_q', None)
    if not _tushare_column_exists(conn, 'fina_quarter_metrics', 'q_fcf_star'):
        return
    qp = _derive_quarter_periods(anchor_yyyymmdd, n=4)
    if len(qp) < 4:
        return
    dates4 = qp[:4]
    date_objs = []
    try:
        for p in dates4:
            date_objs.append(datetime.strptime(p, '%Y%m%d').date())
    except Exception:
        return
    try:
        with conn.cursor() as cur:
            has_netdebt = _tushare_column_exists(conn, 'fina_quarter_metrics', 'netdebt')
            # 运行时 DDL（新增 netdebt_opt）后，避免沿用进程内旧缓存导致误判缺列。
            _TUSHARE_COLUMN_EXISTS_CACHE.pop(
                ((DB_CONFIG_TUSHARE.get('database') or 'stock_tushare'), 'fina_quarter_metrics', 'netdebt_opt'),
                None,
            )
            has_netdebt_opt = _tushare_column_exists(conn, 'fina_quarter_metrics', 'netdebt_opt')
            if has_netdebt_opt and has_netdebt:
                netdebt_expr = 'COALESCE(netdebt_opt, netdebt)'
            elif has_netdebt_opt:
                netdebt_expr = 'netdebt_opt'
            elif has_netdebt:
                netdebt_expr = 'netdebt'
            else:
                netdebt_expr = 'NULL'
            sql_anchor = (
                f'SELECT {netdebt_expr} AS netdebt_view, debt_to_assets, ccc_q FROM stock_tushare.fina_quarter_metrics '
                'WHERE ts_code = %s AND end_date = %s LIMIT 1'
            )
            try:
                cur.execute(sql_anchor, (ts, ed_obj))
            except Exception as e:
                # 兼容服务进程内列缓存陈旧：若 netdebt_opt 1054，自动降级 netdebt。
                msg = str(e)
                if 'Unknown column' in msg and 'netdebt_opt' in msg and has_netdebt:
                    cur.execute(
                        'SELECT netdebt AS netdebt_view, debt_to_assets, ccc_q FROM stock_tushare.fina_quarter_metrics '
                        'WHERE ts_code = %s AND end_date = %s LIMIT 1',
                        (ts, ed_obj),
                    )
                else:
                    raise
            mx = cur.fetchone()
            if mx and len(mx) >= 3:
                nv = _to_float_or_none(mx[0])
                dv = _to_float_or_none(mx[1])
                cv = _to_float_or_none(mx[2])
                if nv is not None:
                    try:
                        out['netdebt_yi'] = round(float(nv) / 100_000_000.0, 2)
                    except (TypeError, ValueError):
                        pass
                if dv is not None:
                    try:
                        out['debt_to_assets'] = round(float(dv), 2)
                    except (TypeError, ValueError):
                        pass
                if cv is not None:
                    try:
                        out['ccc_q'] = round(float(cv), 2)
                    except (TypeError, ValueError):
                        pass
    except Exception as e:
        logger.debug('fixed_assets modal fina_quarter_metrics anchor row: %s', e)

    ts_ph = ', '.join(['%s'] * len(date_objs))
    sql_ttm = (
        'SELECT end_date, q_fcf_star FROM stock_tushare.fina_quarter_metrics '
        f'WHERE ts_code = %s AND end_date IN ({ts_ph})'
    )
    params_ttm = tuple([ts] + date_objs)
    try:
        with conn.cursor() as cur:
            cur.execute(sql_ttm, params_ttm)
            raw = cur.fetchall() or []
    except Exception as e:
        logger.debug('fixed_assets modal fina_quarter_metrics TTM: %s', e)
        return

    def _ed_key(raw_ed) -> str | None:
        if raw_ed is None:
            return None
        if hasattr(raw_ed, 'isoformat'):
            return raw_ed.isoformat().replace('-', '')[:8]
        s0 = str(raw_ed).strip().replace('-', '')
        return s0[:8] if len(s0) >= 8 else None

    mp: dict[str, float | None] = {}
    for r in raw:
        ek = _ed_key(r[0] if len(r) > 0 else None)
        if not ek:
            continue
        mp[ek] = _to_float_or_none(r[1] if len(r) > 1 else None)

    total_yuan = 0.0
    for p in dates4:
        qv = mp.get(p)
        if qv is None:
            out['fcf_star_ttm_yi'] = None
            return
        total_yuan += float(qv)
    try:
        out['fcf_star_ttm_yi'] = round(total_yuan / 100_000_000.0, 2)
    except (TypeError, ValueError):
        out['fcf_star_ttm_yi'] = None


def _fina_fixed_assets_modal_extra_metrics(conn, ts_code: str, anchor_yyyymmdd: str) -> dict[str, float | None]:
    """固定资产弹窗补充：PE（stk_factor_pro 全表最新 trade_date 的 pe_ttm，与财报表 TTM 区一致）、
    营收 TTM（亿，与 ar_pct/合同负债营收%同源）、锚定期 fina_indicator 单季同比与利润率/ROE(扣非)，
    以及 fina_quarter_metrics 的 FCF* TTM、净债务、资产负债率、CCC。"""
    out: dict[str, float | None] = {
        'pe_ttm': None,
        'ttm_revenue_yi': None,
        'q_gr_yoy': None,
        'q_gr_qoq': None,
        'q_op_yoy': None,
        'q_op_qoq': None,
        'q_gsprofit_margin': None,
        'q_netprofit_margin': None,
        'q_dt_roe': None,
        'fcf_star_ttm_yi': None,
        'netdebt_yi': None,
        'debt_to_assets': None,
        'ccc_q': None,
    }
    ts = str(ts_code or '').strip()
    s = _normalize_end_date_yyyymmdd(anchor_yyyymmdd)
    if not ts or not s:
        return out
    try:
        ed_obj = datetime.strptime(s, '%Y%m%d').date()
    except Exception:
        return out

    if _tushare_column_exists(conn, 'stk_factor_pro', 'pe_ttm'):
        try:
            max_d = None
            with conn.cursor() as cur:
                cur.execute('SELECT MAX(trade_date) FROM stk_factor_pro')
                mx = cur.fetchone()
                if mx and mx[0] is not None:
                    max_d = mx[0]
            if max_d is not None:
                with conn.cursor() as cur:
                    cur.execute(
                        'SELECT pe_ttm FROM stk_factor_pro WHERE trade_date = %s AND ts_code = %s LIMIT 1',
                        (max_d, ts),
                    )
                    pr = cur.fetchone()
                    if pr and pr[0] is not None:
                        try:
                            pf = float(pr[0])
                            if pf == pf and abs(pf) < 1e12:
                                out['pe_ttm'] = round(pf, 2)
                        except (TypeError, ValueError):
                            pass
        except Exception as e:
            logger.debug('fixed_assets modal pe_ttm: %s', e)

    if _tushare_column_exists(conn, 'income', 'total_revenue'):
        has_inc_rt = _tushare_column_exists(conn, 'income', 'report_type')
        dkeys = _fina_ttm_income_date_keys(s)
        if dkeys:
            rev_nested = _fina_batch_income_total_revenue(conn, [ts], dkeys, has_inc_rt)
            rm = rev_nested.get(ts, {})
            ttm = _fina_ttm_total_revenue(rm, s)
            if ttm is not None:
                try:
                    out['ttm_revenue_yi'] = round(float(ttm) / 100_000_000.0, 2)
                except (TypeError, ValueError):
                    pass

    has_fi_rt = _tushare_column_exists(conn, 'fina_indicator', 'report_type')
    has_q_gr = _tushare_column_exists(conn, 'fina_indicator', 'q_gr_yoy')
    has_q_gr_qoq = _tushare_column_exists(conn, 'fina_indicator', 'q_gr_qoq')
    has_q_op = _tushare_column_exists(conn, 'fina_indicator', 'q_op_yoy')
    has_q_op_qoq = _tushare_column_exists(conn, 'fina_indicator', 'q_op_qoq')
    has_q_gs = _tushare_column_exists(conn, 'fina_indicator', 'q_gsprofit_margin')
    has_q_nm = _tushare_column_exists(conn, 'fina_indicator', 'q_netprofit_margin')
    has_q_roe = _tushare_column_exists(conn, 'fina_indicator', 'q_dt_roe')
    if not (has_q_gr or has_q_gr_qoq or has_q_op or has_q_op_qoq or has_q_gs or has_q_nm or has_q_roe):
        _fina_fixed_assets_modal_fill_fqm(conn, ts, ed_obj, s, out)
        return out

    sel_list: list[str] = []
    ord_list: list[str] = []
    if has_q_gr:
        sel_list.append('fi.q_gr_yoy AS q_gr_yoy')
        ord_list.append('CASE WHEN fi.q_gr_yoy IS NOT NULL THEN 0 ELSE 1 END, ')
    else:
        sel_list.append('NULL AS q_gr_yoy')
    if has_q_gr_qoq:
        sel_list.append('fi.q_gr_qoq AS q_gr_qoq')
        ord_list.append('CASE WHEN fi.q_gr_qoq IS NOT NULL THEN 0 ELSE 1 END, ')
    else:
        sel_list.append('NULL AS q_gr_qoq')
    if has_q_op:
        sel_list.append('fi.q_op_yoy AS q_op_yoy')
        ord_list.append('CASE WHEN fi.q_op_yoy IS NOT NULL THEN 0 ELSE 1 END, ')
    else:
        sel_list.append('NULL AS q_op_yoy')
    if has_q_op_qoq:
        sel_list.append('fi.q_op_qoq AS q_op_qoq')
        ord_list.append('CASE WHEN fi.q_op_qoq IS NOT NULL THEN 0 ELSE 1 END, ')
    else:
        sel_list.append('NULL AS q_op_qoq')
    if has_q_gs:
        sel_list.append('fi.q_gsprofit_margin AS q_gsprofit_margin')
        ord_list.append('CASE WHEN fi.q_gsprofit_margin IS NOT NULL THEN 0 ELSE 1 END, ')
    else:
        sel_list.append('NULL AS q_gsprofit_margin')
    if has_q_nm:
        sel_list.append('fi.q_netprofit_margin AS q_netprofit_margin')
        ord_list.append('CASE WHEN fi.q_netprofit_margin IS NOT NULL THEN 0 ELSE 1 END, ')
    else:
        sel_list.append('NULL AS q_netprofit_margin')
    if has_q_roe:
        sel_list.append('fi.q_dt_roe AS q_dt_roe')
        ord_list.append('CASE WHEN fi.q_dt_roe IS NOT NULL THEN 0 ELSE 1 END, ')
    else:
        sel_list.append('NULL AS q_dt_roe')

    fi_rt_line = (
        '      CASE WHEN fi.report_type = 1 THEN 0 ELSE 1 END, '
        if has_fi_rt else
        ''
    )
    ord_prefix = ''.join(ord_list)
    sel_sql = ', '.join(sel_list)
    sql_fi = (
        'SELECT z.q_gr_yoy, z.q_gr_qoq, z.q_op_yoy, z.q_op_qoq, z.q_gsprofit_margin, z.q_netprofit_margin, z.q_dt_roe '
        'FROM ('
        f'  SELECT {sel_sql}, '
        '    ROW_NUMBER() OVER ('
        '      PARTITION BY fi.ts_code, fi.end_date '
        '      ORDER BY '
        f'        {ord_prefix}'
        "        CASE WHEN fi.update_flag = '1' THEN 0 ELSE 1 END, "
        f'        {fi_rt_line}'
        '        fi.ann_date DESC'
        '    ) AS rn '
        '  FROM stock_tushare.fina_indicator fi '
        '  WHERE fi.ts_code = %s AND fi.end_date = %s'
        ') z '
        'WHERE z.rn = 1'
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql_fi, (ts, ed_obj))
            fr = cur.fetchone()
        if fr:
            out['q_gr_yoy'] = _to_float_or_none(fr[0]) if has_q_gr else None
            out['q_gr_qoq'] = _to_float_or_none(fr[1]) if has_q_gr_qoq else None
            out['q_op_yoy'] = _to_float_or_none(fr[2]) if has_q_op else None
            out['q_op_qoq'] = _to_float_or_none(fr[3]) if has_q_op_qoq else None
            out['q_gsprofit_margin'] = _to_float_or_none(fr[4]) if has_q_gs else None
            out['q_netprofit_margin'] = _to_float_or_none(fr[5]) if has_q_nm else None
            out['q_dt_roe'] = _to_float_or_none(fr[6]) if has_q_roe else None
            for k in ('q_gr_yoy', 'q_gr_qoq', 'q_op_yoy', 'q_op_qoq', 'q_gsprofit_margin', 'q_netprofit_margin', 'q_dt_roe'):
                v = out.get(k)
                if v is not None:
                    try:
                        out[k] = round(float(v), 2)
                    except (TypeError, ValueError):
                        out[k] = None
    except Exception as e:
        logger.debug('fixed_assets modal fina_indicator: %s', e)

    _fina_fixed_assets_modal_fill_fqm(conn, ts, ed_obj, s, out)
    return out


# fina_mainbz 分类汇总行：仅作占比分母，不进 Top3
_MAINBZ_SUMMARY_ITEMS = frozenset({'产品', '行业', '地区'})
_MAINBZ_SUMMARY_BY_TYPE = {'P': '产品', 'I': '行业', 'D': '地区'}
# 已知结构父项种子（即使源侧暂时缺「父：子」子行也排除）
_MAINBZ_STRUCTURAL_PARENT_ITEMS = frozenset({'持续经营业务', '终止经营业务', '终止经营'})
# 粗口径占位名：仅当同批还有「非其他业务」实质分项时排除（杜绝只剩其他业务）
_MAINBZ_COARSE_ITEMS = frozenset({'主营业务', '主营业务收入'})
# 「其他/其它业务」残差分项：不能作为踢掉主营业务的理由，Top 排序也降权
# 覆盖：其他业务、其他业务收入、其他收入、其他业务(行业) 等
_MAINBZ_OTHER_BUSINESS_RE = re.compile(
    r'^(其他|其它)(业务)?(收入)?$|'
    r'^(其他|其它)业务(收入)?([（(]|$)|'
    r'^(其他|其它)收入([（(]|$)'
)
# 层级分隔：仅全角/半角冒号，避免英文连字符误伤
_MAINBZ_HIER_SEPS = ('：', ':')
# 名称像合计/小计（非「合计特别调整」单独处理）
_MAINBZ_AGG_NAME_RE = re.compile(
    r'^(合计|总计|小计|汇总|全部|总额)$|'
    r'.+(合计|小计|总计|汇总)$'
)
# 合并抵销/分部间抵销等，不进 Top3（可为负）
_MAINBZ_ELIM_RE = re.compile(r'(抵销|抵消|分部间|内部抵|合并抵|板块间|版块间)')


def _aggregate_fina_mainbz_by_item(parsed_rows: list[dict]) -> list[dict]:
    """同名 bz_item 多行时按收入/成本/利润分别求和，避免 item_metrics 后行覆盖前行且与 Top3 口径不一致。"""
    buckets: dict[str, dict] = {}
    for x in parsed_rows:
        item = str(x.get('bz_item') or '').strip()
        if not item:
            continue
        if item not in buckets:
            buckets[item] = {'bz_item': item, 'bz_sales': None, 'bz_cost': None, 'bz_profit': None}
        b = buckets[item]
        for key in ('bz_sales', 'bz_cost', 'bz_profit'):
            v = x.get(key)
            if v is None:
                continue
            fv = float(v)
            b[key] = fv if b[key] is None else float(b[key]) + fv
    return [
        {'bz_item': k, 'bz_sales': d['bz_sales'], 'bz_cost': d['bz_cost'], 'bz_profit': d['bz_profit']}
        for k, d in buckets.items()
    ]


def _collect_mainbz_hierarchy_parents(names: list[str]) -> set[str]:
    """智能识别层级父项：若存在以「父名：」开头的其它分项，则父名为结构合计行。"""
    parents: set[str] = set(_MAINBZ_STRUCTURAL_PARENT_ITEMS)
    uniq = [n for n in dict.fromkeys(names) if n]
    for n in uniq:
        for sep in _MAINBZ_HIER_SEPS:
            pref = n + sep
            if any(o != n and o.startswith(pref) and len(o) > len(pref) for o in uniq):
                parents.add(n)
                break
    return parents


def _strip_mainbz_hierarchy_prefix(
    name: str,
    parentNames: set[str] | None = None,
) -> str:
    """去掉「父：子」层级前缀（父名来自本批智能识别或种子表）。"""
    s = str(name or '').strip()
    if not s:
        return ''
    parents = parentNames if parentNames is not None else _MAINBZ_STRUCTURAL_PARENT_ITEMS
    # 最长父名优先，避免短串误截
    for parent in sorted(parents, key=len, reverse=True):
        if not parent:
            continue
        for sep in _MAINBZ_HIER_SEPS:
            pref = parent + sep
            if s.startswith(pref) and len(s) > len(pref):
                return s[len(pref):].strip()
    return s


def _mainbz_display_name_score(name: str, parentNames: set[str] | None = None) -> float:
    """展示名优选：扁平短名优于「父：子」层级名，优于带(产品)/(行业)冗余后缀。"""
    s = str(name or '').strip()
    if not s:
        return -1e9
    score = 0.0
    parents = parentNames if parentNames is not None else _MAINBZ_STRUCTURAL_PARENT_ITEMS
    for sep in _MAINBZ_HIER_SEPS:
        if sep in s:
            left = s.split(sep, 1)[0].strip()
            if left in parents or left in _MAINBZ_STRUCTURAL_PARENT_ITEMS:
                score -= 20.0
            else:
                score -= 8.0
            break
    if re.search(r'[（(](产品|行业|地区)[）)]\s*$', s):
        score -= 5.0
    score -= len(s) * 0.01
    return score


def _normalize_mainbz_item_name(
    name: str,
    parentNames: set[str] | None = None,
) -> str:
    """近义比对用归一化：去层级前缀、括号说明、统一常见后缀/同义片段。"""
    s = _strip_mainbz_hierarchy_prefix(name, parentNames)
    if not s:
        return ''
    s = re.sub(r'[（(][^（）()]*[）)]', '', s)
    s = s.replace('电力电子转换', '电力转换')
    s = s.replace('建设开发', '开发').replace('投资开发', '开发')
    for suf in ('主营业务', '收入', '业务', '行业', '地区'):
        if len(s) > len(suf) and s.endswith(suf):
            s = s[: -len(suf)]
    return s.strip()


def _is_mainbz_elim_item(item: str) -> bool:
    """合并/分部间抵销类行。"""
    return bool(item) and bool(_MAINBZ_ELIM_RE.search(item))


def _is_mainbz_other_business_item(item: str) -> bool:
    """其他/其它业务残差行（含其他业务收入/其他收入/(行业)后缀等）。"""
    s = str(item or '').strip()
    if not s:
        return False
    bare = re.sub(r'[（(][^（）()]*[）)]', '', s).strip()
    if bare in (
        '其他业务', '其它业务', '其他', '其它',
        '其他业务收入', '其它业务收入', '其他收入', '其它收入',
    ):
        return True
    return bool(_MAINBZ_OTHER_BUSINESS_RE.match(s) or _MAINBZ_OTHER_BUSINESS_RE.match(bare))


def _is_mainbz_special_adjust_item(item: str) -> bool:
    """合计特别调整等非业务分项。"""
    s = str(item or '').strip()
    return bool(s) and ('合计' in s and '调整' in s)


def _unique_mainbz_sales_clusters(salesList: list[float], relTol: float = 0.02) -> list[float]:
    """按金额去重簇：并行口径同额重复（32.5级水泥 vs 建材…-35级水泥）只计一次。"""
    clusters: list[float] = []
    for s in salesList:
        if any(_mainbz_metric_close(s, c, relTol=relTol) for c in clusters):
            continue
        clusters.append(float(s))
    return clusters


def _is_mainbz_sum_of_siblings(item: str, row: dict, all_rows: list[dict]) -> bool:
    """金额≈其余≥2个实质分项之和 → 无冒号的结构父项（如持续经营业务=化学+测试+生物）。"""
    sales = row.get('bz_sales')
    if sales is None or float(sales) <= 0:
        return False
    otherSales: list[float] = []
    for r in all_rows:
        name = str(r.get('bz_item') or '').strip()
        if not name or name == item:
            continue
        if name in _MAINBZ_SUMMARY_ITEMS or _is_mainbz_elim_item(name):
            continue
        # 其他业务/特别调整是残差，不参与「是否为父项」的加总判定
        if _is_mainbz_other_business_item(name) or _is_mainbz_special_adjust_item(name):
            continue
        s = r.get('bz_sales')
        if s is None or float(s) <= 0:
            continue
        # 同额交替标签（新疆/疆内、居民生活用电/民用电工）不计入「子项」
        if _mainbz_metric_close(sales, s, relTol=0.02):
            continue
        otherSales.append(float(s))
    # 并行重复披露先按金额簇去重，避免虚高加总误杀最大主业
    otherSales = _unique_mainbz_sales_clusters(otherSales, relTol=0.02)
    if len(otherSales) < 2:
        return False
    return _mainbz_metric_close(float(sales), sum(otherSales), relTol=0.02)


def _has_mainbz_other_material(
    item: str,
    row: dict,
    all_rows: list[dict],
) -> bool:
    """同批是否存在可替代粗口径的实质分项。
    排除：其他业务/收入、合计特别调整、合计类名、另一粗口径占位——避免主营被踢后只剩残差。"""
    sales = row.get('bz_sales')
    for r in all_rows:
        name = str(r.get('bz_item') or '').strip()
        if not name or name == item:
            continue
        if name in _MAINBZ_SUMMARY_ITEMS or _is_mainbz_elim_item(name):
            continue
        if _is_mainbz_other_business_item(name) or _is_mainbz_special_adjust_item(name):
            continue
        if _MAINBZ_AGG_NAME_RE.match(name):
            continue
        bare = re.sub(r'[（(][^（）()]*[）)]', '', name).strip()
        if bare in _MAINBZ_COARSE_ITEMS:
            continue
        s = r.get('bz_sales')
        if s is None or float(s) <= 0:
            continue
        if sales is not None and _mainbz_items_near_synonym(item, name, sales, s):
            continue
        return True
    return False


def _is_mainbz_non_leaf_segment(
    item: str,
    row: dict,
    all_rows: list[dict],
    parents: set[str],
    summarySales: float | None = None,
) -> bool:
    """判断是否应排除出 Top3：层级父项、合计/抵销、粗口径占位、子项加总父项。"""
    _ = summarySales  # 保留参数兼容调用方；不再用「≈分类汇总」杀主业（主业常≥98%）
    if not item:
        return True
    if item in _MAINBZ_SUMMARY_ITEMS or item in parents:
        return True
    if _is_mainbz_special_adjust_item(item):
        return True
    if _MAINBZ_AGG_NAME_RE.match(item):
        return True
    if _is_mainbz_elim_item(item):
        return True
    # 粗口径「主营业务」：仅当另有实质分项时剔除
    bare = re.sub(r'[（(][^（）()]*[）)]', '', item).strip()
    if bare in _MAINBZ_COARSE_ITEMS and _has_mainbz_other_material(item, row, all_rows):
        return True
    # 金额≈其余分项之和 → 结构父项（不要求名称含「合计」）
    if _is_mainbz_sum_of_siblings(item, row, all_rows):
        return True
    return False


def _drop_mainbz_non_leaf_segments(
    parsed_rows: list[dict],
    summarySales: float | None = None,
) -> list[dict]:
    """过滤非叶子/伪合计行；供 Top3 与分项占比使用。"""
    names = [str(x.get('bz_item') or '').strip() for x in parsed_rows]
    parents = _collect_mainbz_hierarchy_parents(names)
    out: list[dict] = []
    for x in parsed_rows:
        item = str(x.get('bz_item') or '').strip()
        if _is_mainbz_non_leaf_segment(item, x, parsed_rows, parents, summarySales):
            continue
        out.append(x)
    return out


def _mainbz_metric_close(a, b, relTol: float = 0.10) -> bool:
    """金额是否接近（用于同口径近义去重；默认相对偏差≤10%）。"""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    fa = abs(float(a))
    fb = abs(float(b))
    scale = max(fa, fb, 1.0)
    return abs(fa - fb) / scale <= relTol


def _mainbz_items_near_synonym(
    nameA: str,
    nameB: str,
    salesA=None,
    salesB=None,
    parentNames: set[str] | None = None,
) -> bool:
    """判断两个 bz_item 是否为同口径近义写法。
    短串包含（如「海外」⊂「…及海外」）仅在收入几乎相等时启用，避免误合并。"""
    na = _normalize_mainbz_item_name(nameA, parentNames)
    nb = _normalize_mainbz_item_name(nameB, parentNames)
    if not na or not nb:
        return False
    if na == nb:
        return True
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) >= 4 and shorter in longer:
        return True
    # 金额几乎相同（≤1%）时允许短串包含，覆盖地区「海外/…及海外」类写法
    if (
        len(shorter) >= 2
        and shorter in longer
        and _mainbz_metric_close(salesA, salesB, relTol=0.01)
    ):
        return True
    ratio = SequenceMatcher(None, na, nb).ratio()
    if ratio >= 0.72:
        return True
    pref = 0
    for ca, cb in zip(na, nb):
        if ca != cb:
            break
        pref += 1
    return pref >= 3 and ratio >= 0.65


def _mainbz_amount_fingerprint_equal(a: dict, b: dict) -> bool:
    """收入/成本/利润三元组几乎完全一致：视为同口径重复落库（名称可完全不同）。"""
    hasAny = False
    for key in ('bz_sales', 'bz_cost', 'bz_profit'):
        va, vb = a.get(key), b.get(key)
        if va is None and vb is None:
            continue
        if va is None or vb is None:
            return False
        hasAny = True
        if abs(float(va) - float(vb)) > 0.05:  # 分币级容差
            return False
    return hasAny


def _dedupe_mainbz_near_synonyms(parsed_rows: list[dict]) -> list[dict]:
    """近义分项去重：名称近义且金额接近，或金额三元组一致时只保留一条（不求和以免双计）。"""
    names = [str(x.get('bz_item') or '').strip() for x in parsed_rows]
    parents = _collect_mainbz_hierarchy_parents(names)
    ordered = sorted(
        parsed_rows,
        key=lambda x: (
            float(x['bz_sales']) if x.get('bz_sales') is not None else -1.0,
            _mainbz_display_name_score(str(x.get('bz_item') or ''), parents),
        ),
        reverse=True,
    )
    kept: list[dict] = []
    for row in ordered:
        item = str(row.get('bz_item') or '').strip()
        if not item:
            continue
        isDup = False
        for k in kept:
            sameFp = _mainbz_amount_fingerprint_equal(row, k)
            nameNear = _mainbz_items_near_synonym(
                item,
                str(k.get('bz_item') or ''),
                salesA=row.get('bz_sales'),
                salesB=k.get('bz_sales'),
                parentNames=parents,
            )
            if not sameFp and not (
                nameNear and _mainbz_metric_close(row.get('bz_sales'), k.get('bz_sales'))
            ):
                continue
            isDup = True
            # 展示名取更「扁平/干净」者；金额沿用已保留行
            curName = str(k.get('bz_item') or '')
            if _mainbz_display_name_score(item, parents) > _mainbz_display_name_score(
                curName, parents
            ):
                k['bz_item'] = item
            aliases = k.setdefault('aliases', [])
            if item not in aliases:
                aliases.append(item)
            break
        if not isDup:
            kept.append({
                'bz_item': item,
                'bz_sales': row.get('bz_sales'),
                'bz_cost': row.get('bz_cost'),
                'bz_profit': row.get('bz_profit'),
                'aliases': [item],
            })
    return kept


def _fina_mainbz_section_totals(
    summary_row: dict | None,
    parsed_rows: list[dict],
) -> tuple[float, float, float]:
    """占比分母：优先用分类汇总行（产品/行业/地区）；否则用去重后分项合计。"""
    totSales = 0.0
    totCost = 0.0
    totProfit = 0.0
    hasSummarySales = summary_row is not None and summary_row.get('bz_sales') is not None
    hasSummaryCost = summary_row is not None and summary_row.get('bz_cost') is not None
    hasSummaryProfit = summary_row is not None and summary_row.get('bz_profit') is not None
    if hasSummarySales:
        totSales = float(summary_row['bz_sales'])
    if hasSummaryCost:
        totCost = float(summary_row['bz_cost'])
    if hasSummaryProfit:
        totProfit = float(summary_row['bz_profit'])
    if hasSummarySales and hasSummaryCost and hasSummaryProfit:
        return totSales, totCost, totProfit
    sumSales = 0.0
    sumCost = 0.0
    sumProfit = 0.0
    for x in parsed_rows:
        if x.get('bz_sales') is not None:
            sumSales += float(x['bz_sales'])
        if x.get('bz_cost') is not None:
            sumCost += float(x['bz_cost'])
        if x.get('bz_profit') is not None:
            sumProfit += float(x['bz_profit'])
    if not hasSummarySales:
        totSales = sumSales
    if not hasSummaryCost:
        totCost = sumCost
    if not hasSummaryProfit:
        totProfit = sumProfit
    return totSales, totCost, totProfit


def _fina_mainbz_empty_snapshot() -> dict:
    """主营构成双表/同比基期共用的空快照骨架（各 section 独立列表，避免浅拷贝串改）。"""
    def _empty_sec() -> dict:
        return {
            'end_date': '',
            'revenue_top3': [],
            'cost_top3': [],
            'profit_top3': [],
            'item_metrics': {},
        }
    return {
        'end_date': '',
        'sections': {
            'P': _empty_sec(),
            'I': _empty_sec(),
            'D': _empty_sec(),
        },
    }


def _fina_fixed_assets_modal_mainbz_top3(conn, ts_code: str, selected_end_date: str) -> dict:
    """固定资产弹窗补充：主营构成 Top3（产品P/行业I/地区D）。
    左右表：<= selected_end_date 且存在产品(P)分项的最近两个 end_date（含半年报/季报）；
    例：半年度披露后为 current=2026-06-30、previous=2025-12-31。
    同比三列（收入/利润/毛利变化）：对照 current 的上年同期同月日（yoy_base，如 2025-06-30），
    同 P/I/D 内按 bz_item / metrics_key（归一化名）匹配；缺同期或缺同名则前端显示 --。
    Top3 排除「产品/行业/地区」汇总行；近义写法且金额接近时去重。
    收入%/成本%/利润% 分母：优先该分类汇总行，否则为去重后分项合计。"""
    out = {
        'current': _fina_mainbz_empty_snapshot(),
        'previous': _fina_mainbz_empty_snapshot(),
        # 仅供左侧同比列，不单独出表
        'yoy_base': _fina_mainbz_empty_snapshot(),
    }
    ts = str(ts_code or '').strip()
    ed = _normalize_end_date_yyyymmdd(selected_end_date)
    if not ts or not ed:
        return out
    try:
        def _build_top3(
            key: str,
            total: float,
            parsed_rows: list[dict],
            parentNames: set[str] | None = None,
        ) -> list[dict]:
            vals = [x for x in parsed_rows if x.get(key) is not None]
            # 有非「其他业务」分项时，其他业务永不排 Top1（主营构成展示优先主业）
            hasNonOther = any(
                float(x.get(key) or 0.0) > 0
                and not _is_mainbz_other_business_item(str(x.get('bz_item') or ''))
                for x in vals
            )
            vals.sort(
                key=lambda x: (
                    1
                    if (
                        hasNonOther
                        and _is_mainbz_other_business_item(str(x.get('bz_item') or ''))
                    )
                    else 0,
                    -float(x.get(key) or 0.0),
                )
            )
            out_rows: list[dict] = []
            for x in vals[:3]:
                v = float(x.get(key) or 0.0)
                yi = round(v / 100_000_000.0, 2)
                pct = None if total in (0.0, -0.0) else round((v / total) * 100.0, 2)
                itemName = str(x.get('bz_item') or '')
                out_rows.append({
                    'bz_item': itemName,
                    'metrics_key': _normalize_mainbz_item_name(itemName, parentNames),
                    'amount_yi': yi,
                    'pct': pct,
                })
            return out_rows

        with conn.cursor() as cur:
            # 最近两期有产品主营构成的报告期（年报/半年报/季报均可）
            cur.execute(
                "SELECT DISTINCT end_date FROM stock_tushare.fina_mainbz "
                "WHERE ts_code=%s AND bz_code='P' AND end_date<=%s "
                "ORDER BY end_date DESC LIMIT 2",
                (ts, ed),
            )
            period_dates_raw = cur.fetchall() or []
        period_dates = [r[0] for r in period_dates_raw if r and r[0] is not None]
        if not period_dates:
            return out

        def _fill_snapshot(target_end_date, holder: dict) -> None:
            if target_end_date is None:
                return
            target_s = target_end_date.isoformat() if hasattr(target_end_date, 'isoformat') else str(target_end_date)
            holder['end_date'] = target_s
            sections = holder.get('sections') or {}
            for bz_code in ('P', 'I', 'D'):
                sec = sections.get(bz_code)
                if not sec:
                    continue
                sec['end_date'] = target_s
                sec['revenue_top3'] = []
                sec['cost_top3'] = []
                sec['profit_top3'] = []
                sec['item_metrics'] = {}
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT bz_item, bz_sales, bz_cost, bz_profit "
                        "FROM stock_tushare.fina_mainbz "
                        "WHERE ts_code=%s AND bz_code=%s AND end_date=%s",
                        (ts, bz_code, target_end_date),
                    )
                    rows = cur.fetchall() or []
                if not rows:
                    continue

                summary_label = _MAINBZ_SUMMARY_BY_TYPE.get(bz_code, '')
                summary_row: dict | None = None
                body_rows_raw: list[dict] = []
                for r in rows:
                    item = str(r[0] or '').strip()
                    if not item:
                        continue
                    sales = _to_float_or_none(r[1])
                    cost = _to_float_or_none(r[2])
                    profit = _to_float_or_none(r[3])
                    row_d = {
                        'bz_item': item,
                        'bz_sales': sales,
                        'bz_cost': cost,
                        'bz_profit': profit,
                    }
                    if item in _MAINBZ_SUMMARY_ITEMS:
                        # 仅采纳本分类对应汇总行作分母；其它字面汇总项直接丢弃
                        if item == summary_label:
                            if summary_row is None:
                                summary_row = row_d
                            else:
                                old_s = summary_row.get('bz_sales')
                                if sales is not None and (
                                    old_s is None or abs(float(sales)) >= abs(float(old_s))
                                ):
                                    summary_row = row_d
                        continue
                    body_rows_raw.append(row_d)

                summary_sales = (
                    float(summary_row['bz_sales'])
                    if summary_row is not None and summary_row.get('bz_sales') is not None
                    else None
                )
                # 智能剔除层级父项/合计类/伪合计 → 近义去重
                leaf_rows = _drop_mainbz_non_leaf_segments(
                    _aggregate_fina_mainbz_by_item(body_rows_raw),
                    summarySales=summary_sales,
                )
                parents = _collect_mainbz_hierarchy_parents(
                    [str(x.get('bz_item') or '').strip() for x in body_rows_raw]
                )
                parsed_rows = _dedupe_mainbz_near_synonyms(leaf_rows)
                if not parsed_rows and summary_row is None:
                    continue

                tot_sales, tot_cost, tot_profit = _fina_mainbz_section_totals(
                    summary_row, parsed_rows
                )

                sec['revenue_top3'] = _build_top3(
                    'bz_sales', tot_sales, parsed_rows, parents
                )
                sec['cost_top3'] = _build_top3(
                    'bz_cost', tot_cost, parsed_rows, parents
                )
                sec['profit_top3'] = _build_top3(
                    'bz_profit', tot_profit, parsed_rows, parents
                )
                sec['tot_revenue_yi'] = round(tot_sales / 100_000_000.0, 2)
                sec['tot_cost_yi'] = round(tot_cost / 100_000_000.0, 2)
                sec['tot_profit_yi'] = round(tot_profit / 100_000_000.0, 2)
                # 为前端展示与同比共用同一套「分项」口径（与聚合/去重后 Top3 一致）
                item_metrics: dict[str, dict] = {}
                for x in parsed_rows:
                    item = str(x.get('bz_item') or '').strip()
                    if not item:
                        continue
                    sales_raw = x.get('bz_sales')
                    cost_raw = x.get('bz_cost')
                    profit_raw = x.get('bz_profit')
                    sales_yi = None if sales_raw is None else round(float(sales_raw) / 100_000_000.0, 2)
                    cost_yi = None if cost_raw is None else round(float(cost_raw) / 100_000_000.0, 2)
                    profit_yi = None if profit_raw is None else round(float(profit_raw) / 100_000_000.0, 2)
                    gross_margin = None
                    if sales_raw not in (None, 0, 0.0) and profit_raw is not None:
                        gross_margin = round((float(profit_raw) / float(sales_raw)) * 100.0, 2)
                    metrics = {
                        'revenue_yi': sales_yi,
                        'cost_yi': cost_yi,
                        'profit_yi': profit_yi,
                        'gross_margin': gross_margin,
                    }
                    item_metrics[item] = metrics
                    for alias in (x.get('aliases') or []):
                        a = str(alias or '').strip()
                        if a:
                            item_metrics[a] = metrics
                    norm_key = _normalize_mainbz_item_name(item, parents)
                    if norm_key and norm_key not in item_metrics:
                        item_metrics[norm_key] = metrics
                sec['item_metrics'] = item_metrics

        _fill_snapshot(period_dates[0], out['current'])
        if len(period_dates) > 1:
            _fill_snapshot(period_dates[1], out['previous'])

        # 上年同期：与 current.end_date 同月日、年份-1；库中无该期产品分项则 yoy_base 保持空
        cur_end = period_dates[0]
        if hasattr(cur_end, 'year'):
            yoy_date = date(int(cur_end.year) - 1, int(cur_end.month), int(cur_end.day))
        else:
            cur_s = str(cur_end)[:10]
            cur_obj = datetime.strptime(cur_s, '%Y-%m-%d').date()
            yoy_date = date(cur_obj.year - 1, cur_obj.month, cur_obj.day)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM stock_tushare.fina_mainbz "
                "WHERE ts_code=%s AND bz_code='P' AND end_date=%s LIMIT 1",
                (ts, yoy_date),
            )
            hasYoy = cur.fetchone() is not None
        if hasYoy:
            _fill_snapshot(yoy_date, out['yoy_base'])

        # 兼容旧前端结构：保留 current 的平铺别名
        out['end_date'] = out['current'].get('end_date', '')
        out['sections'] = out['current'].get('sections', {})
        return out
    except Exception as e:
        logger.debug('fixed_assets modal mainbz: %s', e)
        return out


def _fina_fixed_assets_ann_date_map(
    conn,
    ts: str,
    end_dates_iso: list[str],
) -> dict[str, str | None]:
    """按报告期取 fina_indicator 公告日：选行口径与财报表 fi 主选行一致（update_flag、report_type、ann_date DESC）。"""
    out: dict[str, str | None] = {}
    if not end_dates_iso:
        return out
    if not _tushare_column_exists(conn, 'fina_indicator', 'ann_date'):
        return out
    has_fi_rt = _tushare_column_exists(conn, 'fina_indicator', 'report_type')
    date_objs: list = []
    seen: set[str] = set()
    for ed in end_dates_iso:
        e10 = str(ed)[:10]
        if len(e10) != 10 or e10 in seen:
            continue
        seen.add(e10)
        try:
            date_objs.append(datetime.strptime(e10, '%Y-%m-%d').date())
        except Exception:
            continue
    if not date_objs:
        return out
    fi_rt_ord = (
        'CASE WHEN fi.report_type = 1 THEN 0 ELSE 1 END, '
        if has_fi_rt else
        ''
    )
    ph = ','.join(['%s'] * len(date_objs))
    sql = (
        'SELECT z.end_date, z.ann_date FROM ('
        '  SELECT fi.end_date, fi.ann_date, '
        '    ROW_NUMBER() OVER ('
        '      PARTITION BY fi.end_date '
        '      ORDER BY '
        "        CASE WHEN fi.update_flag = '1' THEN 0 ELSE 1 END, "
        f'        {fi_rt_ord}'
        '        fi.ann_date DESC'
        '    ) AS rn '
        '  FROM stock_tushare.fina_indicator fi '
        f'  WHERE fi.ts_code = %s AND fi.end_date IN ({ph})'
        ') z WHERE z.rn = 1'
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (ts,) + tuple(date_objs))
            raw = cur.fetchall() or []
    except Exception as e:
        logger.debug('fixed_assets ann_date map: %s', e)
        return out
    for row in raw:
        if not row or row[0] is None:
            continue
        ed0 = row[0]
        ed_s = ed0.isoformat()[:10] if hasattr(ed0, 'isoformat') else str(ed0)[:10]
        if len(ed_s) != 10:
            continue
        ad0 = row[1] if len(row) > 1 else None
        if ad0 is None:
            out[ed_s] = None
            continue
        if hasattr(ad0, 'isoformat'):
            out[ed_s] = ad0.isoformat()[:10]
            continue
        s_ad = str(ad0).strip()
        if len(s_ad) >= 10 and s_ad[4] == '-' and s_ad[7] == '-':
            out[ed_s] = s_ad[:10]
        elif len(s_ad) == 8 and s_ad.isdigit():
            out[ed_s] = f'{s_ad[:4]}-{s_ad[4:6]}-{s_ad[6:8]}'
        else:
            out[ed_s] = s_ad[:10] if s_ad else None
    return out


def _fina_fixed_assets_stk_holder_num_map(
    conn,
    ts_code: str,
    end_dates_iso: list[str],
) -> dict[str, int | None]:
    """stk_holdernumber：按 ts_code + end_date 取 holder_num（户）；同报告期多条取 ann_date 最新。"""
    out: dict[str, int | None] = {}
    if not end_dates_iso:
        return out
    if not _tushare_column_exists(conn, 'stk_holdernumber', 'holder_num'):
        return out
    ts = str(ts_code or '').strip()
    if not ts:
        return out
    dates: list[str] = []
    for d in end_dates_iso:
        s = str(d or '').strip()[:10]
        if len(s) == 10 and s[4] == '-' and s[7] == '-':
            dates.append(s)
    if not dates:
        return out
    placeholders = ','.join(['%s'] * len(dates))
    sql = (
        'SELECT sh.end_date, sh.holder_num FROM stk_holdernumber sh '
        'INNER JOIN ( '
        '  SELECT end_date, MAX(ann_date) AS ma FROM stk_holdernumber '
        f'  WHERE ts_code=%s AND end_date IN ({placeholders}) GROUP BY end_date '
        ') z ON sh.ts_code=%s AND sh.end_date=z.end_date AND sh.ann_date=z.ma '
        'WHERE sh.ts_code=%s'
    )
    params = (ts, *dates, ts, ts)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            raw = cur.fetchall() or []
        for r in raw:
            ed = r[0]
            ed_s = ed.isoformat()[:10] if hasattr(ed, 'isoformat') else str(ed)[:10]
            hn = r[1]
            if hn is None:
                out[ed_s] = None
            else:
                try:
                    out[ed_s] = int(round(float(hn)))
                except (TypeError, ValueError):
                    out[ed_s] = None
    except Exception as e:
        logger.warning('stk_holdernumber 股东户数批量查询失败 ts=%s: %s', ts, e)
    return out


def _fina_fixed_assets_bs_ar_plus_oth_recv_yuan_map(
    conn,
    ts_code: str,
    end_dates_iso: list[str],
) -> dict[str, float | None]:
    """balancesheet：各报告期末应收账款+其它应收。
    Tushare 原文与入库字段均为「元」（与 `_sql_fina_fixed_assets_revenue_series` 中 income.total_revenue 一致；拆单季前的 revenue_cum_yi 为 total_revenue/1e8）。
    选行优先级与 `_fina_batch_accounts_receiv` / 固定资产序列 BS 侧一致。"""
    out: dict[str, float | None] = {}
    if not end_dates_iso:
        return out
    ts = str(ts_code or '').strip()
    if not ts:
        return out
    has_ar = _tushare_column_exists(conn, 'balancesheet', 'accounts_receiv')
    if not has_ar:
        return out
    has_oth = _tushare_column_exists(conn, 'balancesheet', 'oth_receiv')
    has_bs_rt = _tushare_column_exists(conn, 'balancesheet', 'report_type')
    if has_oth:
        sum_sel = '(COALESCE(bs.accounts_receiv, 0) + COALESCE(bs.oth_receiv, 0)) AS recv_sum_yuan'
        ord_pref = (
            'CASE WHEN bs.accounts_receiv IS NOT NULL OR bs.oth_receiv IS NOT NULL THEN 0 ELSE 1 END, '
        )
    else:
        sum_sel = 'COALESCE(bs.accounts_receiv, 0) AS recv_sum_yuan'
        ord_pref = 'CASE WHEN bs.accounts_receiv IS NOT NULL THEN 0 ELSE 1 END, '
    bs_rt_line = (
        '          CASE WHEN bs.report_type = 1 THEN 0 ELSE 1 END, '
        if has_bs_rt else
        ''
    )
    dates: list[str] = []
    seen_d: set[str] = set()
    for d in end_dates_iso:
        s = str(d or '').strip()[:10]
        if len(s) == 10 and s[4] == '-' and s[7] == '-' and s not in seen_d:
            seen_d.add(s)
            dates.append(s)
    if not dates:
        return out
    placeholders = ','.join(['%s'] * len(dates))
    sql = (
        'SELECT z.end_date, z.recv_sum_yuan FROM ( '
        '  SELECT bs.end_date, '
        f'    {sum_sel}, '
        '    ROW_NUMBER() OVER ( '
        '      PARTITION BY bs.end_date '
        '      ORDER BY '
        f'        {ord_pref}'
        "        CASE WHEN bs.update_flag = '1' THEN 0 ELSE 1 END, "
        f'{bs_rt_line}'
        '        bs.ann_date DESC '
        '    ) AS rn '
        '  FROM stock_tushare.balancesheet bs '
        f'  WHERE bs.ts_code = %s AND bs.end_date IN ({placeholders}) '
        ') z WHERE z.rn = 1'
    )
    params = (ts, *dates)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            raw = cur.fetchall() or []
        for row in raw:
            if not row or row[0] is None:
                continue
            ed0 = row[0]
            ed_s = ed0.isoformat()[:10] if hasattr(ed0, 'isoformat') else str(ed0)[:10]
            if len(ed_s) != 10:
                continue
            v = row[1]
            if v is None:
                out[ed_s] = None
            else:
                try:
                    out[ed_s] = float(v)
                except (TypeError, ValueError):
                    out[ed_s] = None
    except Exception as e:
        logger.warning('balancesheet 应收+其它应收批量查询失败 ts=%s: %s', ts, e)
    return out


def _query_stock_basic_sec_list_act(conn, ts_code: str) -> dict[str, str]:
    """读 stock_basic：简称、上市日、实控人、企业性质。

    优先四列一次查全；遇 1054/Unknown column 时先两列再单独补 act（列存在时），避免静默吞异常导致顶栏恒为 --。
    """
    ts = str(ts_code or '').strip()
    empty = {'sec_name': '', 'list_date': '', 'act_name': '', 'act_ent_type': ''}
    if not ts:
        return dict(empty)
    sql_full = (
        "SELECT COALESCE(`name`,''), `list_date`, COALESCE(`act_name`,''), COALESCE(`act_ent_type`,'') "
        "FROM stock_tushare.stock_basic WHERE ts_code=%s LIMIT 1"
    )
    sql_min = "SELECT COALESCE(`name`,''), `list_date` FROM stock_tushare.stock_basic WHERE ts_code=%s LIMIT 1"
    out = dict(empty)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(sql_full, (ts,))
            except Exception as e_sb:
                eargs = getattr(e_sb, 'args', None)
                errno = int(eargs[0]) if eargs and str(eargs[0]).isdigit() else None
                err_txt = str(e_sb)
                if errno == 1054 or '1054' in err_txt or 'Unknown column' in err_txt:
                    cur.execute(sql_min, (ts,))
                else:
                    raise
            nr = cur.fetchone()
            if not nr:
                return out
            out['sec_name'] = str(nr[0] or '').strip()
            ld = nr[1] if len(nr) > 1 else None
            if ld is not None:
                if hasattr(ld, 'isoformat'):
                    out['list_date'] = str(ld.isoformat())[:10]
                else:
                    ls = str(ld).strip()
                    if len(ls) >= 10 and ls[4] == '-' and ls[7] == '-':
                        out['list_date'] = ls[:10]
                    elif len(ls) == 8 and ls.isdigit():
                        out['list_date'] = f'{ls[:4]}-{ls[4:6]}-{ls[6:8]}'
                    else:
                        out['list_date'] = ls[:10] if len(ls) >= 10 else ls
            if len(nr) >= 4:
                out['act_name'] = str(nr[2] or '').strip()
                out['act_ent_type'] = str(nr[3] or '').strip()
            elif _tushare_column_exists(conn, 'stock_basic', 'act_name') and _tushare_column_exists(
                conn, 'stock_basic', 'act_ent_type'
            ):
                try:
                    cur.execute(
                        "SELECT COALESCE(`act_name`,''), COALESCE(`act_ent_type`,'') "
                        "FROM stock_tushare.stock_basic WHERE ts_code=%s LIMIT 1",
                        (ts,),
                    )
                    ar = cur.fetchone()
                    if ar and len(ar) >= 2:
                        out['act_name'] = str(ar[0] or '').strip()
                        out['act_ent_type'] = str(ar[1] or '').strip()
                except Exception as e2:
                    logger.warning('stock_basic 实控人/企业性质补查失败 ts=%s: %s', ts, e2)
    except Exception as e:
        logger.warning('stock_basic 简况查询失败 ts=%s: %s', ts, e)
        return dict(empty)
    return out


def _query_stock_company_website(conn, ts_code: str) -> str:
    """读 stock_company.website（公司主页）；缺表/缺列/缺行返回空串。"""
    ts = str(ts_code or '').strip()
    if not ts:
        return ''
    try:
        if not _tushare_column_exists(conn, 'stock_company', 'website'):
            return ''
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(`website`,'') FROM stock_tushare.stock_company WHERE ts_code=%s LIMIT 1",
                (ts,),
            )
            nr = cur.fetchone()
        if nr and nr[0] is not None:
            return str(nr[0]).strip()
    except Exception as e:
        logger.debug('stock_company website 查询失败 ts=%s: %s', ts, e)
    return ''


def _query_fina_fixed_assets_revenue_series(ts_code: str, selected_end_date: str, limit_n: int = 25) -> dict:
    """固定资产(亿)+存货(亿)+在建工程(亿)+单季度营收(亿)+合同负债(亿)序列：按 end_date 对齐，默认最近 25 季，少于 25 季取全部。"""
    try:
        lim = int(limit_n)
    except (TypeError, ValueError):
        lim = 25
    lim = max(1, min(lim, 25))
    ts = str(ts_code or '').strip()
    if not ts:
        return {'rows': [], 'error': 'ts_code 不能为空'}
    end_dt = _normalize_end_date_yyyymmdd(selected_end_date)
    if not end_dt:
        return {'rows': [], 'error': 'end_date 格式无效'}
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    except Exception as e:
        logger.exception("连接 stock_tushare 失败（fina fixed assets modal）: %s", e)
        return {'rows': [], 'error': '数据库连接失败'}
    try:
        has_bs_rt = _tushare_column_exists(conn, 'balancesheet', 'report_type')
        has_inc_rt = _tushare_column_exists(conn, 'income', 'report_type')
        has_adv = _tushare_column_exists(conn, 'balancesheet', 'adv_receipts')
        has_cip = _tushare_column_exists(conn, 'balancesheet', 'cip_total')
        has_inv = _tushare_column_exists(conn, 'balancesheet', 'inventories')
        # information_schema 与真实表偶发不一致时仍可能 1054，故按序降级重试
        candidates = [
            (has_bs_rt, has_inc_rt),
            (False, has_inc_rt),
            (False, False),
        ]
        seen: set[tuple[bool, bool]] = set()
        raw = None
        last_op_err = None
        # 多取若干季，保证年内 Q2/Q3/Q4 相减时能命中上一累计披露行
        sql_limit = min(lim + 25, 60)
        for ubs, uir in candidates:
            key = (ubs, uir)
            if key in seen:
                continue
            seen.add(key)
            sql = _sql_fina_fixed_assets_revenue_series(ubs, uir, has_adv, has_cip, has_inv)
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, (ts, end_dt, sql_limit))
                    raw = cur.fetchall() or []
                break
            except pymysql.err.OperationalError as oe:
                last_op_err = oe
                errno0 = getattr(oe, 'args', [None])[0]
                if errno0 != 1054:
                    logger.exception("查询固定资产/单季营收序列失败（非缺列）: %s", oe)
                    return {'rows': [], 'error': str(oe)}
                logger.warning(
                    "固定资产/单季营收序列：report_type 降级重试 ubs=%s uir=%s err=%s",
                    ubs,
                    uir,
                    oe,
                )
                continue
        if raw is None:
            err_msg = str(last_op_err) if last_op_err else '查询失败'
            logger.exception("查询固定资产/单季营收序列失败（已用尽降级）: %s", last_op_err)
            return {'rows': [], 'error': err_msg}
        rows_chrono: list[dict] = []
        for r in raw:
            ed = r[0]
            ed_s = ed.isoformat() if hasattr(ed, 'isoformat') else str(ed)
            rows_chrono.append({
                'end_date': ed_s[:10] if len(ed_s) >= 10 else ed_s,
                'fix_assets_yi': _to_float_or_none(r[1]),
                'revenue_cum_yi': _to_float_or_none(r[2]),
                'contract_liab_yi': _to_float_or_none(r[3]),
                'inventories_yi': _to_float_or_none(r[4]) if len(r) > 4 else None,
                'cip_yi': _to_float_or_none(r[5]) if len(r) > 5 else None,
            })
        rows_chrono.sort(key=lambda x: str(x.get('end_date') or ''))
        rows_sq = _fina_revenue_cumulative_to_single_quarter_yi(rows_chrono)
        # 第二张图：单季现金流、经营净收益、毛利率、扣非 ROE、现金流/营收%
        has_cf_rt = _tushare_column_exists(conn, 'cashflow', 'report_type')
        cum_cf_yuan = _fina_fixed_assets_cf_cum_yuan_by_iso(conn, ts, rows_chrono, has_cf_rt)
        cf_q_map = _fina_cf_cumulative_to_single_quarter_yi(rows_chrono, cum_cf_yuan)
        edi_sq = [str(r.get('end_date') or '')[:10] for r in rows_sq]
        fi_batch = _fina_fixed_assets_batch_fi_quarters(conn, ts, edi_sq)
        fqm_batch = _fina_fixed_assets_batch_fqm_quarters(conn, ts, edi_sq)
        for r in rows_sq:
            ed = str(r.get('end_date') or '')[:10]
            r['q_cashflow_yi'] = cf_q_map.get(ed)
            fb = fi_batch.get(ed, {})
            qb = fqm_batch.get(ed, {})
            r['q_ocf_yi'] = qb.get('q_ocf_yi')
            if r['q_ocf_yi'] is None:
                r['q_ocf_yi'] = r.get('q_cashflow_yi')
            r['q_capex_yi'] = qb.get('q_capex_yi')
            r['q_fcf_star_yi'] = qb.get('q_fcf_star_yi')
            r['ar_days_q'] = qb.get('ar_days_q')
            r['inv_days_q'] = qb.get('inv_days_q')
            r['ap_days_q'] = qb.get('ap_days_q')
            r['ccc_q'] = qb.get('ccc_q')
            r['debt_to_assets_q'] = qb.get('debt_to_assets')
            r['interestdebt_ratio_q'] = qb.get('interestdebt_ratio')
            r['netdebt_yi_q'] = qb.get('netdebt_yi')
            r['roic_sq_pct_avg_ic_q'] = qb.get('roic_sq_pct_avg_ic')
            r['q_opincome_yi'] = fb.get('q_opincome_yi')
            r['q_dtprofit_yi'] = fb.get('q_dtprofit_yi')
            r['q_gsprofit_margin'] = fb.get('q_gsprofit_margin')
            r['q_netprofit_margin'] = fb.get('q_netprofit_margin')
            r['q_dt_roe'] = fb.get('q_dt_roe')
            r['q_gr_yoy'] = fb.get('q_gr_yoy')
            r['q_gr_qoq'] = fb.get('q_gr_qoq')
            r['q_op_yoy'] = fb.get('q_op_yoy')
            r['q_op_qoq'] = fb.get('q_op_qoq')
            r['q_netprofit_yoy'] = fb.get('q_netprofit_yoy')
            r['q_netprofit_qoq'] = fb.get('q_netprofit_qoq')
            rev = _to_float_or_none(r.get('q_revenue_yi'))
            cf_yi = _to_float_or_none(r.get('q_cashflow_yi'))
            if rev is not None and cf_yi is not None and abs(float(rev)) > 1e-12:
                r['q_ocf_to_or_pct'] = round((float(cf_yi) / float(rev)) * 100.0, 2)
            else:
                r['q_ocf_to_or_pct'] = None
        rows = rows_sq[-lim:] if len(rows_sq) > lim else rows_sq
        edi_rows = [str(r.get('end_date') or '')[:10] for r in rows]
        holder_map = _fina_fixed_assets_stk_holder_num_map(conn, ts, edi_rows)
        recv_yuan_map = _fina_fixed_assets_bs_ar_plus_oth_recv_yuan_map(conn, ts, edi_rows)
        ann_date_map = _fina_fixed_assets_ann_date_map(conn, ts, edi_rows)
        for r in rows:
            edk = str(r.get('end_date') or '')[:10]
            r['holder_num'] = holder_map.get(edk)
            r['ann_date'] = ann_date_map.get(edk)
            ry = recv_yuan_map.get(edk)
            rev_yi = _to_float_or_none(r.get('q_revenue_yi'))
            if ry is not None and rev_yi is not None and abs(float(rev_yi)) > 1e-12:
                # ry：应收+其它应收（元）；rev_yi：单季营业收入（亿元），来自 income.total_revenue（元）累计拆解
                ar_yi = float(ry) / 100_000_000.0  # 元 → 亿元，与 q_revenue_yi 同量纲后再算占比
                r['recv_pct'] = round((ar_yi / float(rev_yi)) * 100.0, 2)
            else:
                r['recv_pct'] = None
        sb_head = _query_stock_basic_sec_list_act(conn, ts)
        sec_name = sb_head['sec_name']
        list_date_api = sb_head['list_date']
        act_name = sb_head['act_name']
        act_ent_type = sb_head['act_ent_type']
        website_sc = _query_stock_company_website(conn, ts)
        extras = _fina_fixed_assets_modal_extra_metrics(conn, ts, end_dt)
        mainbz_top3 = _fina_fixed_assets_modal_mainbz_top3(conn, ts, end_dt)
        anchor_iso = f'{end_dt[:4]}-{end_dt[4:6]}-{end_dt[6:8]}' if len(end_dt) == 8 else ''
        return {
            'rows': rows,
            'ts_code': ts,
            'limit': lim,
            'sec_name': sec_name,
            'list_date': list_date_api or None,
            'act_name': act_name or None,
            'act_ent_type': act_ent_type or None,
            'website': website_sc or None,
            'metrics_anchor_date': anchor_iso,
            'pe_ttm': extras.get('pe_ttm'),
            'ttm_revenue_yi': extras.get('ttm_revenue_yi'),
            'q_gr_yoy': extras.get('q_gr_yoy'),
            'q_gr_qoq': extras.get('q_gr_qoq'),
            'q_op_yoy': extras.get('q_op_yoy'),
            'q_op_qoq': extras.get('q_op_qoq'),
            'q_gsprofit_margin': extras.get('q_gsprofit_margin'),
            'q_netprofit_margin': extras.get('q_netprofit_margin'),
            'q_dt_roe': extras.get('q_dt_roe'),
            'fcf_star_ttm_yi': extras.get('fcf_star_ttm_yi'),
            'netdebt_yi': extras.get('netdebt_yi'),
            'debt_to_assets': extras.get('debt_to_assets'),
            'ccc_q': extras.get('ccc_q'),
            'mainbz_top3': mainbz_top3,
        }
    except Exception as e:
        logger.exception("查询固定资产/单季营收序列失败: %s", e)
        return {'rows': [], 'error': str(e)}
    finally:
        conn.close()



# 代码更新时间：2026-07-31 09:28 入池放宽：该期有 fina_indicator 即可（不要求 arturn；His新高可覆盖银行）
# 代码更新时间：2026-08-04 15:03 新增 _fina_arturn_attach_price_range_pct（stk_factor_ma 前复权极值 + rt_k 当前价，pre_close 除权折算）；缓存口径 sb_act_9
# 代码更新时间：2026-08-04 15:18 历史最低改倍数口径 mult_vs_h_low（百分比达数万不可读）；缓存口径 sb_act_10
# 代码更新时间：2026-08-05 06:00 距高低点%去掉错误的 close_qfq/pre_close 折算（曾把当日涨幅算成距250高）；缓存 sb_act_11
# 代码更新时间：2026-08-05 06:35 新增 pct_since_ann（自公告%：a0 前一交易日 close_qfq→rt_k）；缓存 sb_act_12
# 代码更新时间：2026-08-06 22:54 TTL 300→1800；getOrBuildFinaArturnTable 同报告期冷建去重；保活间隔 600s
# 代码更新时间：2026-08-08 23:33 主营构成：过滤持续经营业务父项、层级前缀归一、扁平展示名优选
# 代码更新时间：2026-08-08 23:39 主营构成智能识别非叶子：层级父项/合计名/伪合计，避免硬编码漏网
# 代码更新时间：2026-08-08 23:51 主营构成：废≈汇总误杀；改子项加总/抵销/粗口径；1000家复验
# 代码更新时间：2026-08-08 23:55 杜绝「主业误杀只剩其他业务」：粗口径/加总去重/Top降权
# 代码更新时间：2026-08-09 00:00 深核四类问题：扩其他收入/剔特别调整作实质项；全市场门禁PASS
# 代码更新时间：2026-09-03 08:52 宽表 HTTP 预编码 gzip（ensureFinaArturnWireGzip）
# 代码更新时间：2026-09-07 15:01 isFinaArturnBuilding：宽表冷建期间供 top_moves 让路
# 代码更新时间：2026-10-04 17:42 休市沿用旧缓存不冷建；wire 按 last_update 校验
# 代码更新时间：2026-10-04 21:42 自公告% 基数查询：公告日前 60 天窗口 + 窗口外无下限补查（输出逐项等价，冷建 pct_since_ann ~35s→~1-6s）
# 代码更新时间：2026-10-04 22:45 wire gzip 按口径加锁，并发首请求只编码一次
