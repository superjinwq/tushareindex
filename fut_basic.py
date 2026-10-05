#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口：fut_basic（期货合约信息）
描述：按交易所分页全量拉取期货合约基础信息（含普通交割月与主力/连续），落库 stock_tushare.fut_basic；
      六所默认每所一线程并行，所有 pro.fut_basic 经全局节拍串行（避免多线程打满 500 次/分）。
文档：https://tushare.pro/document/2?doc_id=135
限量：单次最大 10000 行，须 limit/offset 分页直至末页不满页（规则 A）。

运行：
  python fut_basic.py                    # 全量：六所并行拉取 + 一致性校验
  python fut_basic.py --mode incremental # 已等同全量，仅打日志提示
  python fut_basic.py --check-only       # 仅做各所全量条数/ts_code 集合校验，不写入
  python fut_basic.py --exchange DCE     # 只处理单个交易所

TUSHARE_TOKEN 硬编码于下方配置区。数据库连接可用 DB_HOST、DB_PORT、DB_USER、DB_PASSWORD、DB_NAME 环境变量覆盖。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
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

LOG_FILE = "fut_basic.log"


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
TABLE_NAME = "fut_basic"
BATCH_SIZE = 500
REQUEST_INTERVAL = 0.2
# 六所并行拉 API；写库串行，避免同表并行 UPSERT 触发 InnoDB 1213。仍保留锁冲突退避重试。
DB_LOCK_RETRY_MAX = 10
DB_LOCK_RETRY_BASE_SEC = 0.08
PAGE_LIMIT = 5000
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


class _TushareApiPace:
    """多线程共享：相邻两次 fut_basic 调用起点间隔至少 REQUEST_INTERVAL 秒。"""

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

DEFAULT_EXCHANGES = ("CFFEX", "DCE", "CZCE", "SHFE", "INE", "GFEX")

FIELDS = (
    "ts_code,symbol,exchange,name,fut_code,multiplier,trade_unit,per_unit,"
    "quote_unit,quote_unit_desc,d_mode_desc,list_date,delist_date,d_month,"
    "last_ddate,trade_time_desc"
)

DATA_TOLERANCE = 0.001
CHECK_FLOAT_COLS = ("multiplier", "per_unit")
RETRY_SECONDS = (2, 4, 8)
# 六所并行 UPSERT 时 InnoDB 偶发死锁(1213)/锁等待超时(1205)：每批短事务 + 指数退避重试
DB_LOCK_RETRY_MAX = 10
DB_LOCK_RETRY_BASE_SEC = 0.08


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


VARCHAR_MAX_LEN: dict[str, int] = {
    "ts_code": 20,
    "symbol": 20,
    "exchange": 16,
    "name": 64,
    "fut_code": 16,
    "trade_unit": 32,
    "quote_unit": 32,
    "quote_unit_desc": 64,
    "d_mode_desc": 32,
    "d_month": 16,
    "trade_time_desc": 128,
}

DECIMAL_SPECS: dict[str, tuple[int, int]] = {
    "multiplier": (14, 6),
    "per_unit": (14, 6),
}

DATE_COLS = ("list_date", "delist_date", "last_ddate")

STR_COLS = (
    "symbol",
    "fut_code",
    "trade_unit",
    "quote_unit",
    "quote_unit_desc",
    "d_mode_desc",
    "d_month",
    "trade_time_desc",
)

INSERT_COLS = [
    "ts_code",
    "symbol",
    "exchange",
    "name",
    "fut_code",
    "multiplier",
    "trade_unit",
    "per_unit",
    "quote_unit",
    "quote_unit_desc",
    "d_mode_desc",
    "list_date",
    "delist_date",
    "d_month",
    "last_ddate",
    "trade_time_desc",
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
  `ts_code` varchar(20) NOT NULL COMMENT '合约代码',
  `symbol` varchar(20) NOT NULL DEFAULT '' COMMENT '交易标识',
  `exchange` varchar(16) NOT NULL COMMENT '交易市场',
  `name` varchar(64) NOT NULL DEFAULT '' COMMENT '中文简称',
  `fut_code` varchar(16) DEFAULT NULL COMMENT '合约产品代码',
  `multiplier` decimal(14,6) DEFAULT NULL COMMENT '合约乘数(国债/指数期货)',
  `trade_unit` varchar(32) DEFAULT NULL COMMENT '交易计量单位',
  `per_unit` decimal(14,6) DEFAULT NULL COMMENT '交易单位(每手)',
  `quote_unit` varchar(32) DEFAULT NULL COMMENT '报价单位',
  `quote_unit_desc` varchar(64) DEFAULT NULL COMMENT '最小报价单位说明',
  `d_mode_desc` varchar(32) DEFAULT NULL COMMENT '交割方式说明',
  `list_date` date DEFAULT NULL COMMENT '上市日期',
  `delist_date` date DEFAULT NULL COMMENT '最后交易日期',
  `d_month` varchar(16) DEFAULT NULL COMMENT '交割月份',
  `last_ddate` date DEFAULT NULL COMMENT '最后交割日',
  `trade_time_desc` varchar(128) DEFAULT NULL COMMENT '交易时间说明',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ts_code` (`ts_code`),
  KEY `idx_exchange` (`exchange`),
  KEY `idx_list_date` (`list_date`),
  KEY `idx_delist_date` (`delist_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='期货合约信息 fut_basic';
"""


def createTable(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(CREATE_TABLE_SQL))
    logger.info("建表检查完成：%s", TABLE_NAME)


def retryRequest(requestFunc: Callable[..., pd.DataFrame], **kwargs: Any) -> pd.DataFrame:
    for attempt, waitSec in enumerate(RETRY_SECONDS, start=1):
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
            logger.warning("请求失败，第 %s 次重试前等待 %ss：%s", attempt, waitSec, exc)
            time.sleep(waitSec)
    return pd.DataFrame()


def fetchFutBasicPage(pro: Any, *, exchange: str, offset: int) -> pd.DataFrame:
    """单页；全局节拍内调用 pro.fut_basic。"""

    def _call() -> pd.DataFrame:
        kwargs: dict[str, Any] = {
            "exchange": exchange,
            "fields": FIELDS,
            "limit": PAGE_LIMIT,
            "offset": offset,
        }
        df = pro.fut_basic(**kwargs)
        return df if df is not None else pd.DataFrame()

    return _tusharePace.run(_call)


def fetchExchangeAll(pro: Any, exchange: str) -> pd.DataFrame:
    """单交易所分页拉取直至末页不满 PAGE_LIMIT（规则 A）。"""
    frames: list[pd.DataFrame] = []
    offset = 0
    while running:
        page = retryRequest(
            fetchFutBasicPage,
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
        logger.info("[%s] 分页 offset=%s 本页 %s 行，继续…", exchange, offset, len(page))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _stripOrNone(val: Any) -> str | None:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    s = str(val).strip()
    return s or None


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
    for col in ("multiplier", "per_unit"):
        if col not in out.columns:
            continue
        out[col] = pd.to_numeric(out[col], errors="coerce")
        out[col] = out[col].round(6)
    if "exchange" in out.columns:
        out["exchange"] = out["exchange"].astype(str).str.strip()
    if "ts_code" in out.columns:
        out["ts_code"] = out["ts_code"].astype(str).str.strip()
    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].apply(
            lambda x: "" if _stripOrNone(x) is None else _stripOrNone(x) or ""
        )
    if "name" in out.columns:
        out["name"] = out["name"].apply(
            lambda x: "" if _stripOrNone(x) is None else _stripOrNone(x) or ""
        )
    for col in STR_COLS:
        if col not in out.columns:
            continue
        out[col] = out[col].apply(_stripOrNone)
    out = out.replace({np.nan: None})
    return out


def validate_before_save(df: pd.DataFrame) -> None:
    if df.empty:
        return
    for col, spec in DECIMAL_SPECS.items():
        if col not in df.columns:
            continue
        mi, di = spec
        maxAbs = Decimal(10) ** (mi - di) - Decimal(10) ** (-di)
        q = Decimal(10) ** (-di)
        for idx, val in df[col].items():
            if val is None or (isinstance(val, float) and np.isnan(val)):
                continue
            try:
                dec = Decimal(str(val)).quantize(q)
            except (InvalidOperation, ValueError):
                logger.error("列 %s 非数值 ts_code=%s 行索引=%s", col, df.at[idx, "ts_code"], idx)
                raise
            if abs(dec) > maxAbs:
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
    for col, maxLen in VARCHAR_MAX_LEN.items():
        if col not in df.columns:
            continue
        for idx, val in df[col].items():
            if val is None:
                continue
            s = str(val)
            if len(s) > maxLen:
                logger.error(
                    "字符串超长 表=%s 列=%s ts_code=%s CHAR_LEN=%s 上限=%s 摘要=%s…",
                    TABLE_NAME,
                    col,
                    df.at[idx, "ts_code"],
                    len(s),
                    maxLen,
                    s[:80],
                )
                raise ValueError("varchar too long")


def saveData(engine: Engine, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    df2 = _processDf(df)
    validate_before_save(df2)
    placeholders = ", ".join([f":{c}" for c in INSERT_COLS])
    updateParts = [f"`{c}`=VALUES(`{c}`)" for c in INSERT_COLS if c != "ts_code"]
    updateParts.append("`updated_at`=CURRENT_TIMESTAMP")
    sql = (
        f"INSERT INTO `{TABLE_NAME}` (`{'`, `'.join(INSERT_COLS)}`) "
        f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE " + ", ".join(updateParts)
    )
    rows = df2[INSERT_COLS].to_dict("records")
    for rec in rows:
        for k, v in list(rec.items()):
            if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
                rec[k] = None
    n = 0
    nBatches = (len(rows) + BATCH_SIZE - 1) // BATCH_SIZE
    with _dbWriteLock:
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            batchIdx = i // BATCH_SIZE + 1
            for dbAttempt in range(1, DB_LOCK_RETRY_MAX + 1):
                try:
                    with engine.begin() as conn:
                        conn.execute(text(sql), batch)
                    n += len(batch)
                    break
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    if not _isRetryableMysqlLockError(exc) or dbAttempt >= DB_LOCK_RETRY_MAX:
                        raise
                    delay = DB_LOCK_RETRY_BASE_SEC * (2 ** (dbAttempt - 1))
                    logger.debug(
                        "表 `%s` 批 %s/%s 锁冲突，%.2fs 后重试（%s/%s）",
                        TABLE_NAME,
                        batchIdx,
                        nBatches,
                        delay,
                        dbAttempt,
                        DB_LOCK_RETRY_MAX,
                    )
                    time.sleep(delay)
    return n


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


def syncOneExchange(pro: Any, engine: Engine, exchange: str, *, doWrite: bool) -> int:
    """拉取单所并 UPSERT。不修剪孤儿行（保留历史退市合约）。"""
    logger.info("[%s] 开始拉取…", exchange)
    raw = fetchExchangeAll(pro, exchange)
    if raw.empty:
        logger.warning("[%s] API 无数据", exchange)
        return 0
    if not doWrite:
        logger.info("[%s] 预览 %s 条（未写入）", exchange, len(raw))
        return 0
    n = saveData(engine, raw)
    logger.info("[%s] UPSERT 完成，本批处理 %s 行", exchange, n)
    return n


def consistencyCheck(pro: Any, engine: Engine, exchanges: tuple[str, ...]) -> list[str]:
    """各所：本地行数与 API 全量行数、ts_code 集合哈希比对。"""
    bad: list[str] = []
    for ex in exchanges:
        if not running:
            break
        try:
            apiDf = fetchExchangeAll(pro, ex)
        except Exception as exc:
            logger.warning("一致性校验拉取失败 %s：%s，跳过该所", ex, exc)
            continue
        if apiDf.empty and countByExchange(engine, ex) == 0:
            continue
        apiN = len(apiDf)
        dbN = countByExchange(engine, ex)
        apiCodes = set(apiDf["ts_code"].astype(str).str.strip()) if not apiDf.empty else set()
        dbCodes = tsCodeSetFromDb(engine, ex)
        hApi = hashSortedCodes(apiCodes)
        hDb = hashSortedCodes(dbCodes)
        if apiN != dbN or hApi != hDb:
            logger.error(
                "[%s] 不一致：API 行数=%s 本地行数=%s md5(api)=%s md5(db)=%s",
                ex,
                apiN,
                dbN,
                hApi,
                hDb,
            )
            bad.append(ex)
        else:
            logger.info("[%s] 一致性通过（行数=%s）", ex, apiN)
        if not apiDf.empty:
            _spotCheckFloats(engine, ex, apiDf)
    return bad


def _spotCheckFloats(engine: Engine, exchange: str, apiDf: pd.DataFrame) -> None:
    """抽样比对 multiplier / per_unit。"""
    sample = apiDf.head(min(20, len(apiDf)))
    if sample.empty:
        return
    with engine.connect() as conn:
        for _, row in sample.iterrows():
            tsCode = str(row["ts_code"]).strip()
            r = conn.execute(
                text(
                    f"SELECT multiplier, per_unit FROM `{TABLE_NAME}` WHERE ts_code = :c LIMIT 1"
                ),
                {"c": tsCode},
            )
            dbRow = r.mappings().first()
            if not dbRow:
                continue
            for col in CHECK_FLOAT_COLS:
                av = row.get(col)
                dv = dbRow.get(col)
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
                        tsCode,
                        col,
                        a,
                        d,
                    )


def _syncOneExchangeJob(exchange: str, engine: Engine, *, doWrite: bool) -> int:
    proLocal = ts.pro_api(TUSHARE_TOKEN)
    return syncOneExchange(proLocal, engine, exchange, doWrite=doWrite)


def runParallelByExchange(engine: Engine, exchanges: tuple[str, ...], *, doWrite: bool) -> int:
    """每所一线程并行全量同步；Tushare 调用经 _tusharePace 全局串行。"""
    if not exchanges:
        return 0
    if len(exchanges) == 1:
        pro = ts.pro_api(TUSHARE_TOKEN)
        return syncOneExchange(pro, engine, exchanges[0], doWrite=doWrite)
    totalUpsert = 0
    futureToEx: dict[Any, str] = {}
    with ThreadPoolExecutor(max_workers=len(exchanges)) as pool:
        for ex in exchanges:
            fut = pool.submit(_syncOneExchangeJob, ex, engine, doWrite=doWrite)
            futureToEx[fut] = ex
        pending = set(futureToEx.keys())
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
                ex = futureToEx[fut]
                try:
                    if fut.cancelled():
                        continue
                    totalUpsert += fut.result()
                except BaseException:
                    logger.exception("[%s] 并行同步失败", ex)
                    raise
    return totalUpsert


def repairExchanges(engine: Engine, exchanges: list[str]) -> None:
    runParallelByExchange(engine, tuple(exchanges), doWrite=True)


def fullImport(engine: Engine, exchanges: tuple[str, ...], *, doWrite: bool) -> None:
    totalUpsert = runParallelByExchange(engine, exchanges, doWrite=doWrite)
    if doWrite:
        logger.info("全量阶段完成：累计 UPSERT 批次行 %s（含重复键更新）", totalUpsert)


def main() -> None:
    parser = argparse.ArgumentParser(description="fut_basic 期货合约入库")
    parser.add_argument(
        "--mode",
        choices=("auto", "full", "incremental"),
        default="auto",
        help="auto/full/incremental 均全量：六所并行拉取+UPSERT；incremental 仅保留兼容并打日志。",
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

    logger.info("全量导入：每所一线程并行，全局 API 节拍限流")

    createTable(engine)
    fullImport(engine, exchanges, doWrite=True)

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

# 代码更新时间：2026-06-02 22:21
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
