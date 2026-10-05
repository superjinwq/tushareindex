#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口：opt_basic（期权合约信息）
描述：按交易所分页全量拉取期权合约基础信息，落库 stock_tushare.opt_basic；六所默认每所一线程并行拉 API（全局节拍限流），写库/修剪经全局锁串行，避免同表并行 UPSERT 触发 InnoDB 1213。
文档：https://tushare.pro/document/2?doc_id=158
限量：单次返回可能达数千行，须 limit/offset 分页直至末页不满页（规则 A）。

运行：
  python opt_basic.py                    # 全量：六所并行拉取 + 每所修剪 + 一致性校验
  python opt_basic.py --mode full        # 同 auto（保留参数）
  python opt_basic.py --mode incremental # 已等同全量，仅打日志提示
  python opt_basic.py --check-only       # 仅做各所全量条数/ts_code 集合校验，不写入
  python opt_basic.py --exchange DCE     # 只处理单个交易所

TUSHARE_TOKEN 硬编码于下方配置区（与 margin_sum / stock_company 一致）。数据库连接仍可用 DB_HOST、DB_PORT、DB_USER、DB_PASSWORD、DB_NAME 环境变量覆盖。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import random
import signal
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from decimal import Decimal, InvalidOperation
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Callable

import numpy as np
import pandas as pd
import tushare as ts
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from urllib.parse import quote_plus

LOG_FILE = "opt_basic.log"


from windows_safe_log import WindowsSafeTimedRotatingFileHandler


def setupLogger() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = WindowsSafeTimedRotatingFileHandler(
        LOG_FILE, when="D", interval=1, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, ch], force=True)


setupLogger()
logger = logging.getLogger(__name__)

running = True


def handleSignal(signum: int, frame: Any) -> None:
    global running
    running = False
    logger.warning("收到信号 %s，准备安全退出…", signum)


signal.signal(signal.SIGINT, handleSignal)
signal.signal(signal.SIGTERM, handleSignal)

# ===== 用户配置区 =====
# 与 margin_sum / stock_company 等一致
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "").strip()
if not TUSHARE_TOKEN:
    raise SystemExit("缺少环境变量 TUSHARE_TOKEN")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "stock_tushare"),
    "charset": "utf8mb4",
}
TABLE_NAME = "opt_basic"
BATCH_SIZE = 500
REQUEST_INTERVAL = 0.1
# 六所并行拉 API，写库经全局锁串行（同表 UPSERT 避免 1213 死锁）；遇锁仍指数退避重试
DB_LOCK_RETRY_MAX = 10
DB_LOCK_RETRY_BASE_SEC = 0.08
DB_LOCK_WARN_FROM_ATTEMPT = 3
_dbWriteLock = threading.Lock()


def _isRetryableMysqlLockError(exc: BaseException) -> bool:
    """识别 MySQL 层可整事务重试的锁冲突（1213 死锁 / 1205 锁等待超时）。"""
    retryCodes = frozenset((1213, 1205))
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        e = stack.pop()
        eid = id(e)
        if eid in seen:
            continue
        seen.add(eid)
        args = getattr(e, "args", ()) or ()
        if args and isinstance(args[0], int) and args[0] in retryCodes:
            return True
        if args and isinstance(args[0], str):
            msg = args[0]
            for code in retryCodes:
                if f"({code}," in msg:
                    return True
        for attr in ("orig", "__cause__", "__context__"):
            nxt = getattr(e, attr, None)
            if isinstance(nxt, BaseException):
                stack.append(nxt)
    return False


def _lockRetryDelaySec(attempt: int) -> float:
    """指数退避 + 抖动，避免多线程同时重试再次碰撞。"""
    base = DB_LOCK_RETRY_BASE_SEC * (2 ** (attempt - 1))
    return base + random.uniform(0, DB_LOCK_RETRY_BASE_SEC)


def _logLockRetry(
    *,
    context: str,
    batchIdx: int,
    batchTotal: int,
    attempt: int,
    exc: BaseException,
) -> None:
    code = getattr(exc, "args", (None,))[0]
    msg = f"{context} 批 {batchIdx}/{batchTotal} 锁冲突({code}) 重试 {attempt}/{DB_LOCK_RETRY_MAX}"
    if attempt >= DB_LOCK_WARN_FROM_ATTEMPT:
        logger.warning("%s", msg)
    else:
        logger.debug("%s", msg)

# 分页上限：官方未写明时取保守值，满页则继续 offset（规则 A）
PAGE_LIMIT = 5000


class _TushareApiPace:
    """多线程共享：相邻两次 opt_basic 调用起点间隔至少 REQUEST_INTERVAL 秒。"""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._nextStart = 0.0

    def run(self, fn: Callable[[], Any]) -> Any:
        with self._lock:
            now = time.monotonic()
            if now < self._nextStart:
                time.sleep(self._nextStart - now)
            start = time.monotonic()
            try:
                return fn()
            finally:
                self._nextStart = start + self._interval


_tusharePace = _TushareApiPace(REQUEST_INTERVAL)

# 按交易所批量拉取（文档交易所代码）
DEFAULT_EXCHANGES = ("SSE", "SZSE", "CFFEX", "DCE", "SHFE", "CZCE")

# 接口全部输出字段（显式传入，规则 B）
FIELDS = (
    "ts_code,exchange,name,per_unit,opt_code,opt_type,call_put,exercise_type,"
    "exercise_price,s_month,maturity_date,list_price,list_date,delist_date,"
    "last_edate,last_ddate,quote_unit,min_price_chg"
)

# 一致性：数值容差（校验用抽样字段）
DATA_TOLERANCE = 0.001
CHECK_FLOAT_COLS = ("exercise_price", "list_price")

RETRY_SECONDS = (2, 4, 8)

# 规则 Q：与 DDL 一致
VARCHAR_MAX_LEN: dict[str, int] = {
    "ts_code": 20,
    "exchange": 16,
    "name": 200,
    "per_unit": 64,
    "opt_code": 64,
    "opt_type": 32,
    "call_put": 8,
    "exercise_type": 16,
    "s_month": 32,
    "quote_unit": 64,
    "min_price_chg": 64,
}

DECIMAL_SPECS: dict[str, tuple[int, int]] = {
    "exercise_price": (18, 6),
    "list_price": (18, 6),
}

DATE_COLS = (
    "list_date",
    "delist_date",
    "maturity_date",
    "last_edate",
    "last_ddate",
)

INSERT_COLS = [
    "ts_code",
    "exchange",
    "name",
    "per_unit",
    "opt_code",
    "opt_type",
    "call_put",
    "exercise_type",
    "exercise_price",
    "s_month",
    "maturity_date",
    "list_price",
    "list_date",
    "delist_date",
    "last_edate",
    "last_ddate",
    "quote_unit",
    "min_price_chg",
]

_engine: Engine | None = None


def makeEngine() -> Engine:
    global _engine
    if _engine is None:
        dsn = (
            f"mysql+pymysql://{quote_plus(DB_CONFIG['user'])}:{quote_plus(DB_CONFIG['password'])}"
            f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}?charset={DB_CONFIG['charset']}"
        )
        _engine = create_engine(
            dsn, pool_recycle=3600, pool_pre_ping=True, pool_size=5, max_overflow=10
        )
    return _engine


CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `ts_code` varchar(20) NOT NULL COMMENT 'TS代码',
  `exchange` varchar(16) NOT NULL COMMENT '交易市场',
  `name` varchar(200) NOT NULL DEFAULT '' COMMENT '合约名称',
  `per_unit` varchar(64) DEFAULT NULL COMMENT '合约单位',
  `opt_code` varchar(64) DEFAULT NULL COMMENT '标的合约代码',
  `opt_type` varchar(32) DEFAULT NULL COMMENT '合约类型',
  `call_put` varchar(8) DEFAULT NULL COMMENT '期权类型',
  `exercise_type` varchar(16) DEFAULT NULL COMMENT '行权方式',
  `exercise_price` decimal(18,6) DEFAULT NULL COMMENT '行权价格',
  `s_month` varchar(32) DEFAULT NULL COMMENT '结算月',
  `maturity_date` date DEFAULT NULL COMMENT '到期日',
  `list_price` decimal(18,6) DEFAULT NULL COMMENT '挂牌基准价',
  `list_date` date DEFAULT NULL COMMENT '开始交易日期',
  `delist_date` date DEFAULT NULL COMMENT '最后交易日期',
  `last_edate` date DEFAULT NULL COMMENT '最后行权日期',
  `last_ddate` date DEFAULT NULL COMMENT '最后交割日期',
  `quote_unit` varchar(64) DEFAULT NULL COMMENT '报价单位',
  `min_price_chg` varchar(64) DEFAULT NULL COMMENT '最小价格波幅',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ts_code` (`ts_code`),
  KEY `idx_exchange` (`exchange`),
  KEY `idx_list_date` (`list_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='期权合约信息 opt_basic';
"""


def createTable(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(CREATE_TABLE_SQL))
    logger.info("建表检查完成：%s", TABLE_NAME)


def retryRequest(requestFunc: Callable[..., pd.DataFrame], **kwargs: Any) -> pd.DataFrame:
    for attempt, wait_sec in enumerate(RETRY_SECONDS, start=1):
        if not running:
            return pd.DataFrame()
        try:
            return requestFunc(**kwargs)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if attempt == len(RETRY_SECONDS):
                logger.error("[FAILED] 参数=%s 错误=%s", kwargs, exc)
                return pd.DataFrame()
            logger.warning("请求失败，第 %s 次重试前等待 %ss：%s", attempt, wait_sec, exc)
            time.sleep(wait_sec)
    return pd.DataFrame()


def fetchOptBasicPage(pro: Any, *, exchange: str, offset: int) -> pd.DataFrame:
    """单页；全局节拍内调用 pro.opt_basic。"""

    def _call() -> pd.DataFrame:
        kwargs: dict[str, Any] = {
            "exchange": exchange,
            "fields": FIELDS,
            "limit": PAGE_LIMIT,
            "offset": offset,
        }
        df = pro.opt_basic(**kwargs)
        return df if df is not None else pd.DataFrame()

    return _tusharePace.run(_call)


def concatPages(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """分页拼接：走 records，避开 pandas 对空/全 NA 列 concat 的 FutureWarning。"""
    rows: list[dict[str, Any]] = []
    for frame in frames:
        if frame is None or frame.empty:
            continue
        rows.extend(frame.to_dict("records"))
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame.from_records(rows)


def fetchExchangeAll(pro: Any, exchange: str) -> pd.DataFrame:
    """单交易所分页拉取直至末页不满 PAGE_LIMIT（规则 A）。"""
    frames: list[pd.DataFrame] = []
    offset = 0
    while running:
        page = retryRequest(
            fetchOptBasicPage,
            pro=pro,
            exchange=exchange,
            offset=offset,
        )
        if page.empty:
            break
        frames.append(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        logger.info(
            "[%s] 分页 offset=%s 本页 %s 行，继续…",
            exchange,
            offset,
            len(page),
        )
    return concatPages(frames)


def _processDf(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for col in DATE_COLS:
        if col not in out.columns:
            continue
        out[col] = pd.to_datetime(out[col], format="%Y%m%d", errors="coerce")
        out[col] = out[col].dt.strftime("%Y-%m-%d")
        out[col] = out[col].where(out[col].notna(), None)
    for col in ("exercise_price", "list_price"):
        if col not in out.columns:
            continue
        out[col] = pd.to_numeric(out[col], errors="coerce")
        out[col] = out[col].round(6)
    if "exchange" in out.columns:
        out["exchange"] = out["exchange"].astype(str).str.strip()
    if "ts_code" in out.columns:
        out["ts_code"] = out["ts_code"].astype(str).str.strip()
    if "name" in out.columns:
        out["name"] = out["name"].apply(
            lambda x: ""
            if x is None or (isinstance(x, float) and np.isnan(x)) or str(x).strip() == ""
            else str(x).strip()
        )
    for col in ("per_unit", "opt_code", "opt_type", "call_put", "exercise_type", "s_month", "quote_unit", "min_price_chg"):
        if col not in out.columns:
            continue
        out[col] = out[col].apply(lambda x: None if x is None or (isinstance(x, float) and np.isnan(x)) else str(x).strip() or None)
    out = out.replace({np.nan: None})
    return out


def validate_before_save(df: pd.DataFrame) -> None:
    if df.empty:
        return
    for col, spec in DECIMAL_SPECS.items():
        if col not in df.columns:
            continue
        mi, di = spec
        max_abs = Decimal(10) ** (mi - di) - Decimal(10) ** (-di)
        q = Decimal(10) ** (-di)
        for idx, val in df[col].items():
            if val is None or (isinstance(val, float) and np.isnan(val)):
                continue
            try:
                dec = Decimal(str(val)).quantize(q)
            except (InvalidOperation, ValueError):
                logger.error("列 %s 非数值 ts_code=%s 行索引=%s", col, df.at[idx, "ts_code"], idx)
                raise
            if abs(dec) > max_abs:
                logger.error(
                    "decimal 越界 表=%s 列=%s ts_code=%s 值=%s 允许(M,D)=(%s,%s)",
                    TABLE_NAME,
                    col,
                    df.at[idx, "ts_code"],
                    val,
                    mi,
                    di,
                )
                raise ValueError("decimal overflow")
    for col, max_len in VARCHAR_MAX_LEN.items():
        if col not in df.columns:
            continue
        for idx, val in df[col].items():
            if val is None:
                continue
            s = str(val)
            if len(s) > max_len:
                logger.error(
                    "字符串超长 表=%s 列=%s ts_code=%s CHAR_LEN=%s 上限=%s 摘要=%s…",
                    TABLE_NAME,
                    col,
                    df.at[idx, "ts_code"],
                    len(s),
                    max_len,
                    s[:80],
                )
                raise ValueError("varchar too long")


def saveData(engine: Engine, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    df2 = _processDf(df)
    validate_before_save(df2)
    placeholders = ", ".join([f":{c}" for c in INSERT_COLS])
    update_parts = [f"`{c}`=VALUES(`{c}`)" for c in INSERT_COLS if c != "ts_code"]
    update_parts.append("`updated_at`=CURRENT_TIMESTAMP")
    sql = (
        f"INSERT INTO `{TABLE_NAME}` (`{'`, `'.join(INSERT_COLS)}`) "
        f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE " + ", ".join(update_parts)
    )
    rows = df2[INSERT_COLS].to_dict("records")
    rows.sort(key=lambda r: str(r.get("ts_code") or ""))
    for rec in rows:
        for k, v in list(rec.items()):
            if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
                rec[k] = None
    n = 0
    lockRetryTotal = 0
    n_batches = (len(rows) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        batch_idx = i // BATCH_SIZE + 1
        for db_attempt in range(1, DB_LOCK_RETRY_MAX + 1):
            try:
                with _dbWriteLock:
                    with engine.begin() as conn:
                        conn.execute(text(sql), batch)
                n += len(batch)
                break
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if not _isRetryableMysqlLockError(exc) or db_attempt >= DB_LOCK_RETRY_MAX:
                    raise
                lockRetryTotal += 1
                delay = _lockRetryDelaySec(db_attempt)
                _logLockRetry(
                    context=f"表 `{TABLE_NAME}`",
                    batchIdx=batch_idx,
                    batchTotal=n_batches,
                    attempt=db_attempt,
                    exc=exc,
                )
                time.sleep(delay)
    if lockRetryTotal:
        logger.info("表 `%s` 写库完成，锁重试合计 %s 次", TABLE_NAME, lockRetryTotal)
    return n


def pruneStaleForExchange(engine: Engine, exchange: str, keep_ts_codes: set[str]) -> int:
    """删除该交易所下本地存在但本次 API 未返回的 ts_code（源端已下线等）。"""
    db_codes = tsCodeSetFromDb(engine, exchange)
    stale = list(db_codes - keep_ts_codes)
    if not stale:
        return 0
    deleted = 0
    lockRetryTotal = 0
    chunk = 500
    n_chunks = (len(stale) + chunk - 1) // chunk
    for i in range(0, len(stale), chunk):
        part = sorted(stale[i : i + chunk])
        chunk_idx = i // chunk + 1
        ph = ", ".join([f":p{j}" for j in range(len(part))])
        params = {f"p{j}": part[j] for j in range(len(part))}
        params["ex"] = exchange
        delete_sql = (
            f"DELETE FROM `{TABLE_NAME}` WHERE `exchange` = :ex AND `ts_code` IN ({ph})"
        )
        for db_attempt in range(1, DB_LOCK_RETRY_MAX + 1):
            try:
                with _dbWriteLock:
                    with engine.begin() as conn:
                        r = conn.execute(text(delete_sql), params)
                        deleted += r.rowcount or 0
                break
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if not _isRetryableMysqlLockError(exc) or db_attempt >= DB_LOCK_RETRY_MAX:
                    raise
                lockRetryTotal += 1
                delay = _lockRetryDelaySec(db_attempt)
                _logLockRetry(
                    context=f"[{exchange}] 修剪",
                    batchIdx=chunk_idx,
                    batchTotal=n_chunks,
                    attempt=db_attempt,
                    exc=exc,
                )
                time.sleep(delay)
    if lockRetryTotal:
        logger.info("[%s] 修剪完成，锁重试合计 %s 次", exchange, lockRetryTotal)
    return deleted


def countByExchange(engine: Engine, exchange: str) -> int:
    with engine.connect() as conn:
        r = conn.execute(
            text(f"SELECT COUNT(*) AS c FROM `{TABLE_NAME}` WHERE `exchange` = :ex"),
            {"ex": exchange},
        )
        row = r.mappings().first()
        return int(row["c"]) if row else 0


def tsCodeSetFromDb(engine: Engine, exchange: str) -> set[str]:
    with engine.connect() as conn:
        r = conn.execute(
            text(f"SELECT `ts_code` FROM `{TABLE_NAME}` WHERE `exchange` = :ex"),
            {"ex": exchange},
        )
        return {str(row[0]) for row in r.fetchall()}


def hashSortedCodes(codes: set[str]) -> str:
    s = "|".join(sorted(codes))
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def syncOneExchange(pro: Any, engine: Engine, exchange: str, *, do_write: bool) -> tuple[int, int]:
    """拉取单所、可选写入并修剪。返回 (写入行数, 删除孤儿行数)。"""
    logger.info("[%s] 开始拉取…", exchange)
    raw = fetchExchangeAll(pro, exchange)
    if raw.empty:
        logger.warning("[%s] API 无数据", exchange)
        return 0, 0
    api_codes = set(raw["ts_code"].astype(str).str.strip())
    if not do_write:
        logger.info("[%s] 预览 %s 条（未写入）", exchange, len(raw))
        return 0, 0
    n = saveData(engine, raw)
    pruned = pruneStaleForExchange(engine, exchange, api_codes)
    if pruned:
        logger.info("[%s] 修剪孤儿行 %s 条", exchange, pruned)
    logger.info("[%s] UPSERT 完成，本批处理 %s 行", exchange, n)
    return n, pruned


def consistencyCheck(pro: Any, engine: Engine, exchanges: tuple[str, ...]) -> list[str]:
    """
    各所：本地行数与 API 全量行数、ts_code 集合哈希比对。
    返回不一致的交易所列表（需从源重同步）。
    """
    bad: list[str] = []
    for ex in exchanges:
        if not running:
            break
        try:
            api_df = fetchExchangeAll(pro, ex)
        except Exception as exc:
            logger.warning("一致性校验拉取失败 %s：%s，跳过该所", ex, exc)
            continue
        if api_df.empty and countByExchange(engine, ex) == 0:
            continue
        api_n = len(api_df)
        db_n = countByExchange(engine, ex)
        api_codes = set(api_df["ts_code"].astype(str).str.strip()) if not api_df.empty else set()
        db_codes = tsCodeSetFromDb(engine, ex)
        h_api = hashSortedCodes(api_codes)
        h_db = hashSortedCodes(db_codes)
        if api_n != db_n or h_api != h_db:
            logger.error(
                "[%s] 不一致：API 行数=%s 本地行数=%s md5(api)=%s md5(db)=%s",
                ex,
                api_n,
                db_n,
                h_api,
                h_db,
            )
            bad.append(ex)
        else:
            logger.info("[%s] 一致性通过（行数=%s）", ex, api_n)
        if not api_df.empty:
            _spotCheckFloats(engine, ex, api_df)
    return bad


def _spotCheckFloats(engine: Engine, exchange: str, api_df: pd.DataFrame) -> None:
    """对抽样合约比对 exercise_price / list_price（容差 DATA_TOLERANCE）。"""
    sample = api_df.head(min(20, len(api_df)))
    if sample.empty:
        return
    with engine.connect() as conn:
        for _, row in sample.iterrows():
            ts_code = str(row["ts_code"]).strip()
            r = conn.execute(
                text(
                    f"SELECT exercise_price, list_price FROM `{TABLE_NAME}` WHERE ts_code = :c LIMIT 1"
                ),
                {"c": ts_code},
            )
            db_row = r.mappings().first()
            if not db_row:
                continue
            for col in CHECK_FLOAT_COLS:
                av = row.get(col)
                dv = db_row.get(col)
                if av is None or dv is None or (isinstance(av, float) and np.isnan(av)):
                    continue
                try:
                    a = float(av)
                    d = float(dv)
                except (TypeError, ValueError):
                    continue
                denom = abs(a) + 1e-8
                if abs(a - d) / denom > DATA_TOLERANCE:
                    logger.error(
                        "[%s] 抽样数值不一致 %s %s API=%s DB=%s",
                        exchange,
                        ts_code,
                        col,
                        a,
                        d,
                    )


def _syncOneExchangeJob(exchange: str, engine: Engine, *, do_write: bool) -> tuple[int, int]:
    """线程入口：每线程独立 pro_api，避免跨线程共享客户端。"""
    pro_local = ts.pro_api(TUSHARE_TOKEN)
    return syncOneExchange(pro_local, engine, exchange, do_write=do_write)


def runParallelByExchange(engine: Engine, exchanges: tuple[str, ...], *, do_write: bool) -> int:
    """
    每所一线程并行全量同步；Tushare 调用经 _tusharePace 全局串行。
    返回累计 UPSERT 行数（do_write=False 时为 0）。
    """
    if not exchanges:
        return 0
    if len(exchanges) == 1:
        pro = ts.pro_api(TUSHARE_TOKEN)
        u, _p = syncOneExchange(pro, engine, exchanges[0], do_write=do_write)
        return u
    total_upsert = 0
    future_to_ex: dict[Any, str] = {}
    max_workers = len(exchanges)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for ex in exchanges:
            fut = pool.submit(_syncOneExchangeJob, ex, engine, do_write=do_write)
            future_to_ex[fut] = ex
        pending = set(future_to_ex.keys())
        while pending:
            if not running:
                for fut in pending:
                    fut.cancel()
                logger.warning("用户中断，未完成的交易所任务已取消")
                break
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for fut in done:
                ex = future_to_ex[fut]
                try:
                    if fut.cancelled():
                        continue
                    u, _p = fut.result()
                    total_upsert += u
                except BaseException:
                    logger.exception("[%s] 并行同步失败", ex)
                    raise
    return total_upsert


def repairExchanges(engine: Engine, exchanges: list[str]) -> None:
    runParallelByExchange(engine, tuple(exchanges), do_write=True)


def fullImport(engine: Engine, exchanges: tuple[str, ...], *, do_write: bool) -> None:
    total_upsert = runParallelByExchange(engine, exchanges, do_write=do_write)
    if do_write:
        logger.info("全量阶段完成：累计 UPSERT 批次行 %s（含重复键更新）", total_upsert)


def main() -> None:
    parser = argparse.ArgumentParser(description="opt_basic 期权合约入库")
    parser.add_argument(
        "--mode",
        choices=("auto", "full", "incremental"),
        default="auto",
        help="auto/full/incremental 均全量：六所并行拉取+每所修剪；incremental 仅保留兼容并打日志。",
    )
    parser.add_argument("--check-only", action="store_true", help="仅一致性校验，不写库")
    parser.add_argument(
        "--exchange",
        type=str,
        default="",
        help="只处理单个交易所，如 DCE；默认处理全部六所",
    )
    parser.add_argument("--skip-check", action="store_true", help="跳过阶段2一致性校验")
    args = parser.parse_args()

    exchanges: tuple[str, ...]
    if args.exchange.strip():
        exchanges = (args.exchange.strip().upper(),)
    else:
        exchanges = DEFAULT_EXCHANGES

    engine = makeEngine()
    pro = ts.pro_api(TUSHARE_TOKEN)

    if args.check_only:
        bad = consistencyCheck(pro, engine, exchanges)
        if bad:
            logger.error("校验未通过交易所：%s", ",".join(bad))
            sys.exit(1)
        logger.info("校验全部通过")
        return

    if args.mode == "incremental":
        logger.warning("--mode incremental 已与全量合并：本次仍执行六所全量并行同步")

    if args.mode == "full":
        logger.info("--mode full：全量导入（每所一线程 + 全局 API 节拍）")
    else:
        logger.info("全量导入：每所一线程并行，全局 API 节拍限流")

    createTable(engine)
    fullImport(engine, exchanges, do_write=True)

    if args.skip_check:
        return

    bad = consistencyCheck(pro, engine, exchanges)
    if bad:
        repairExchanges(engine, bad)
        bad2 = consistencyCheck(pro, engine, exchanges)
        if bad2:
            logger.error("修复后仍有不一致：%s", bad2)
            sys.exit(1)
    logger.info("全部完成")


if __name__ == "__main__":
    main()

# 代码更新时间：2026-09-04 15:31
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
