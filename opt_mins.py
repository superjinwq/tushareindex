#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口：opt_mins（期权历史分钟行情）
描述：按 opt_basic 合约逐只拉取 60min，落库 stock_tushare.opt_mins；
      全量可中断续跑；增量 SQL 收窄宇宙、跳过已新鲜、节拍 0.13s。
文档：https://tushare.pro/document/2?doc_id=341
限量：单次最大 8000 行，须按时间切片直至不满页（规则 A）。必填 ts_code+freq。

安全：
  未加 --ts-code / --max-codes 时，默认 --mode auto 直接退出，
  避免盘中误跑全市场。盘后请显式指定：
      python opt_mins.py --mode full
      python opt_mins.py --mode incremental
  功能验证请用：
      python opt_mins.py --mode full --ts-code 10007976.SH,MO2609-C-6600.CFX

运行：
  python opt_mins.py --mode full
  python opt_mins.py
  python opt_mins.py --check-only
  python opt_mins.py --exchange SSE,SZSE --opt-type ETF期权
  python opt_mins.py --exchange CFFEX,DCE,SHFE,CZCE --mode incremental
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Callable
from urllib.parse import quote_plus

import numpy as np
import pandas as pd
import tushare as ts
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

LOG_FILE = "opt_mins.log"


from windows_safe_log import WindowsSafeTimedRotatingFileHandler


def setupLogger() -> None:
    # 被宿主进程（pytest、rt_monitor 等）导入时根日志器已有处理器：不得 force 覆盖宿主日志
    if logging.getLogger().handlers:
        return
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
TABLE_NAME = "opt_mins"
STATE_TABLE = "opt_mins_sync_state"
BATCH_SIZE = 500
REQUEST_INTERVAL = 0.2
# 增量仍须逐合约请求；0.13s ≈ 462次/分。硬顶 500，不顶满，留给重试和收尾校验
INCREMENTAL_REQUEST_INTERVAL = 0.13
PAGE_LIMIT = 8000
DEFAULT_WORKERS = 5
MAX_WORKERS_CAP = 16
DEFAULT_EXCHANGES = ("SSE", "SZSE", "CFFEX", "DCE", "SHFE", "CZCE")
# 商品夜盘可能从上市日前一晚 21:00 开始，窗口左端再提前一天
NIGHT_LEAD_HOURS = 8
FULL_START_DATE = "20100101"
FREQ = "60min"
INCREMENTAL_LOOKBACK_TRADING_DAYS = 5
FIELDS = "ts_code,trade_time,open,close,high,low,vol,amount,oi"
DATA_TOLERANCE = 0.001
CHECK_NUM_COLS = ("close", "vol", "amount", "oi")
CHECK_SAMPLE_CODES = 8
RETRY_SECONDS = (2, 4, 8)
PRICE_COLS = ("open", "high", "low", "close")
VARCHAR_MAX_LEN = {"ts_code": 20, "freq": 10}
# 规则 P 分层抽样（SSE/SZSE/CFFEX/DCE/SHFE/CZCE，含活跃金银与长寿命）后规则 3b：
# 五价强制 3a=(10,4)；amount 样本 max≈1.47e8 → 保守 (20,4)；oi 同 opt_daily
DECIMAL_SPECS: dict[str, tuple[int, int]] = {
    "open": (10, 4),
    "high": (10, 4),
    "low": (10, 4),
    "close": (10, 4),
    "amount": (20, 4),
    "oi": (20, 4),
}
INSERT_COLS = [
    "ts_code",
    "trade_time",
    "freq",
    "open",
    "high",
    "low",
    "close",
    "vol",
    "amount",
    "oi",
]

_engine: Engine | None = None
_dbWriteLock = threading.Lock()
_threadLocal = threading.local()
_progressLock = threading.Lock()
_progressDone = 0


class TushareApiPace:
    """多线程共享：相邻两次 opt_mins 调用起点间隔至少 REQUEST_INTERVAL 秒。"""

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

    def setInterval(self, interval: float) -> None:
        self._interval = max(0.12, float(interval))


_tusharePace = TushareApiPace(REQUEST_INTERVAL)


@dataclass(frozen=True)
class OptContract:
    tsCode: str
    exchange: str
    listDate: dt.date | None
    delistDate: dt.date | None


@dataclass(frozen=True)
class SyncState:
    tsCode: str
    maxTradeTime: dt.datetime | None
    rowCount: int
    status: str


def makeEngine() -> Engine:
    global _engine
    if _engine is None:
        dsn = (
            f"mysql+pymysql://{quote_plus(DB_CONFIG['user'])}:{quote_plus(DB_CONFIG['password'])}"
            f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
            f"?charset={DB_CONFIG['charset']}"
        )
        _engine = create_engine(
            dsn, pool_recycle=3600, pool_pre_ping=True, pool_size=8, max_overflow=16
        )
    return _engine


CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `ts_code` varchar(20) NOT NULL COMMENT 'TS代码',
  `trade_time` datetime NOT NULL COMMENT '交易时间',
  `freq` varchar(10) NOT NULL COMMENT '分钟频度，本脚本仅写 60min',
  `open` decimal(10,4) DEFAULT NULL COMMENT '开盘价',
  `high` decimal(10,4) DEFAULT NULL COMMENT '最高价',
  `low` decimal(10,4) DEFAULT NULL COMMENT '最低价',
  `close` decimal(10,4) DEFAULT NULL COMMENT '收盘价',
  `vol` bigint DEFAULT NULL COMMENT '成交量(手)',
  `amount` decimal(20,4) DEFAULT NULL COMMENT '成交金额(元)',
  `oi` decimal(20,4) DEFAULT NULL COMMENT '持仓量(手)',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ts_time_freq` (`ts_code`,`trade_time`,`freq`),
  KEY `idx_trade_time` (`trade_time`),
  KEY `idx_freq_time` (`freq`,`trade_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='期权分钟行情 opt_mins';
"""

CREATE_STATE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{STATE_TABLE}` (
  `ts_code` varchar(20) NOT NULL COMMENT 'TS代码',
  `freq` varchar(10) NOT NULL COMMENT '分钟频度',
  `max_trade_time` datetime DEFAULT NULL COMMENT '本地已覆盖的最大 trade_time',
  `row_count` bigint NOT NULL DEFAULT 0 COMMENT '累计写入行（含 UPSERT，仅进度参考）',
  `status` varchar(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/partial/done/empty/error',
  `last_error` varchar(255) DEFAULT NULL COMMENT '最近失败摘要',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`ts_code`,`freq`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='opt_mins 按合约续传状态';
"""


def createTable(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(CREATE_TABLE_SQL))
        conn.execute(text(CREATE_STATE_SQL))
    logger.info("建表检查完成：%s / %s", TABLE_NAME, STATE_TABLE)


def parseTsCodes(raw: str) -> list[str]:
    if not raw or not raw.strip():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        code = part.strip()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def parseExchanges(raw: str) -> tuple[str, ...]:
    if not raw or not raw.strip():
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        ex = part.strip().upper()
        if not ex or ex in seen:
            continue
        seen.add(ex)
        out.append(ex)
    return tuple(out)


def clampWorkers(n: int) -> int:
    return max(1, min(int(n), MAX_WORKERS_CAP))


def fmtApiDt(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def toDate(value: Any) -> dt.date | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).strip()
    if not s or s.lower() == "none":
        return None
    s8 = s.replace("-", "").replace("/", "")[:8]
    if len(s8) != 8 or not s8.isdigit():
        return None
    return dt.datetime.strptime(s8, "%Y%m%d").date()


def toDateTime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min)
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.to_pydatetime().replace(tzinfo=None)


def isCoverageComplete(maxTradeTime: dt.datetime | None, delistDate: dt.date | None) -> bool:
    """已摘牌且本地最大 bar 日期覆盖摘牌日，视为该合约 60min 拉完。"""
    if maxTradeTime is None or delistDate is None:
        return False
    return maxTradeTime.date() >= delistDate


def incrementalFreshCutoff(
    lastOpenDate: dt.date | None, now: dt.datetime | None = None
) -> dt.datetime | None:
    """增量「已新鲜」阈值：日盘 15:00；过了开市日 21:00（含周末）则要求夜盘起点。"""
    if lastOpenDate is None:
        return None
    now = now or dt.datetime.now()
    dayClose = dt.datetime.combine(lastOpenDate, dt.time(15, 0))
    nightOpen = dt.datetime.combine(lastOpenDate, dt.time(21, 0))
    if now >= nightOpen:
        return nightOpen
    return dayClose


def isFreshForIncremental(
    maxTradeTime: dt.datetime | None, freshCutoff: dt.datetime | None
) -> bool:
    if maxTradeTime is None or freshCutoff is None:
        return False
    return maxTradeTime >= freshCutoff


def buildFetchWindow(
    listDate: dt.date | None,
    delistDate: dt.date | None,
    localMax: dt.datetime | None,
    today: dt.date,
    lookbackDays: int,
) -> tuple[dt.datetime, dt.datetime] | None:
    """返回 [start, end]；商品夜盘左端提前一天。"""
    startDay = listDate or dt.datetime.strptime(FULL_START_DATE, "%Y%m%d").date()
    start = dt.datetime.combine(startDay, dt.time.min) - dt.timedelta(hours=NIGHT_LEAD_HOURS)
    endDay = min(delistDate or today, today)
    end = dt.datetime.combine(endDay, dt.time(23, 59, 59))
    if localMax is not None:
        lb = localMax - dt.timedelta(days=max(lookbackDays, 0))
        if lb > start:
            start = lb
    if start > end:
        return None
    return start, end


def shouldSkipContract(
    contract: OptContract,
    state: SyncState | None,
    today: dt.date,
    *,
    force: bool,
    freshCutoff: dt.datetime | None = None,
    skipFresh: bool = False,
) -> bool:
    if force:
        return False
    if state is None:
        return False
    if state.status == "done" or isCoverageComplete(state.maxTradeTime, contract.delistDate):
        return True
    if (
        state.status == "empty"
        and contract.delistDate is not None
        and contract.delistDate < today
    ):
        return True
    if skipFresh and isFreshForIncremental(state.maxTradeTime, freshCutoff):
        return True
    return False


def isSafeTestRun(tsCodes: list[str], maxCodes: int) -> bool:
    """仅 --ts-code / --max-codes 视为测试安全范围；单所全量仍须显式 mode。"""
    return bool(tsCodes) or maxCodes > 0


def shouldRefuseUnconstrainedAuto(mode: str, tsCodes: list[str], maxCodes: int) -> bool:
    """无测试范围的 auto 拒绝开跑，防止盘中误扫全市场。"""
    return mode == "auto" and not isSafeTestRun(tsCodes, maxCodes)


def yearSlices(start: dt.datetime, end: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
    out: list[tuple[dt.datetime, dt.datetime]] = []
    year = start.year
    while year <= end.year:
        s = max(start, dt.datetime(year, 1, 1, 0, 0, 0))
        e = min(end, dt.datetime(year, 12, 31, 23, 59, 59))
        if s <= e:
            out.append((s, e))
        year += 1
    return out


def splitDatetimeRange(
    start: dt.datetime, end: dt.datetime
) -> list[tuple[dt.datetime, dt.datetime]]:
    mid = start + (end - start) / 2
    mid = mid.replace(microsecond=0)
    if mid <= start or mid >= end:
        return [(start, end)]
    return [(start, mid), (mid, end)]


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


def fetchOptMinsOnce(
    pro: Any, tsCode: str, start: dt.datetime, end: dt.datetime
) -> pd.DataFrame:
    def _call() -> pd.DataFrame:
        df = pro.opt_mins(
            ts_code=tsCode,
            freq=FREQ,
            start_date=fmtApiDt(start),
            end_date=fmtApiDt(end),
            fields=FIELDS,
        )
        return df if df is not None else pd.DataFrame()

    return _tusharePace.run(_call)


def fetchOptMinsRange(pro: Any, tsCode: str, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    """按年切片；单段满 8000 再对半切（规则 A）。"""
    frames: list[pd.DataFrame] = []
    pending = yearSlices(start, end)
    while pending and running:
        s, e = pending.pop(0)
        page = retryRequest(fetchOptMinsOnce, pro=pro, tsCode=tsCode, start=s, end=e)
        if page.empty:
            continue
        if len(page) >= PAGE_LIMIT:
            halves = splitDatetimeRange(s, e)
            if len(halves) == 2 and halves != [(s, e)]:
                logger.info(
                    "[%s] 满页 %s，拆分 %s ~ %s",
                    tsCode,
                    len(page),
                    fmtApiDt(s),
                    fmtApiDt(e),
                )
                pending = halves + pending
                continue
        frames.append(page)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    if "trade_time" in out.columns:
        out = out.drop_duplicates(subset=["ts_code", "trade_time"], keep="last")
    return out


def _processDf(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    if "trade_time" in out.columns:
        out["trade_time"] = pd.to_datetime(out["trade_time"], errors="coerce")
        out["trade_time"] = out["trade_time"].dt.strftime("%Y-%m-%d %H:%M:%S")
        out["trade_time"] = out["trade_time"].where(out["trade_time"].notna(), None)
    if "freq" not in out.columns:
        out["freq"] = FREQ
    out["freq"] = out["freq"].astype(str).str.strip()
    if "ts_code" in out.columns:
        out["ts_code"] = out["ts_code"].astype(str).str.strip()
    for col in PRICE_COLS:
        if col not in out.columns:
            continue
        out[col] = pd.to_numeric(out[col], errors="coerce").round(4)
    if "amount" in out.columns:
        out["amount"] = pd.to_numeric(out["amount"], errors="coerce").round(4)
    if "oi" in out.columns:
        out["oi"] = pd.to_numeric(out["oi"], errors="coerce").round(4)
    if "vol" in out.columns:
        vol = pd.to_numeric(out["vol"], errors="coerce").round(0)
        out["vol"] = vol.where(vol.notna(), None)
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
                logger.error("decimal 非法 列=%s ts_code=%s", col, df.at[idx, "ts_code"])
                raise
            if abs(dec) > maxAbs:
                logger.error(
                    "decimal 越界 表=%s 列=%s ts_code=%s 值=%s (M,D)=(%s,%s)",
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
                    "字符串超长 列=%s ts_code=%s len=%s 上限=%s",
                    col,
                    df.at[idx, "ts_code"],
                    len(s),
                    maxLen,
                )
                raise ValueError("varchar too long")


def saveData(engine: Engine, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    df2 = _processDf(df)
    validate_before_save(df2)
    cols = [c for c in INSERT_COLS if c in df2.columns]
    placeholders = ", ".join([f":{c}" for c in cols])
    updateParts = [f"`{c}`=VALUES(`{c}`)" for c in cols if c not in ("ts_code", "trade_time", "freq")]
    updateParts.append("`updated_at`=CURRENT_TIMESTAMP")
    sql = (
        f"INSERT INTO `{TABLE_NAME}` (`{'`, `'.join(cols)}`) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE " + ", ".join(updateParts)
    )
    rows = df2[cols].to_dict("records")
    for rec in rows:
        for k, v in list(rec.items()):
            if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
                rec[k] = None
            if k == "vol" and v is not None:
                rec[k] = int(v)
    n = 0
    with engine.begin() as conn:
        for i in range(0, len(rows), BATCH_SIZE):
            conn.execute(text(sql), rows[i : i + BATCH_SIZE])
            n += len(rows[i : i + BATCH_SIZE])
    return n


def tableRowCount(engine: Engine) -> int:
    with engine.connect() as conn:
        r = conn.execute(text(f"SELECT COUNT(*) AS c FROM `{TABLE_NAME}`"))
        row = r.mappings().first()
        return int(row["c"]) if row else 0


def loadOptBasicContracts(
    engine: Engine,
    exchanges: tuple[str, ...],
    tsCodes: list[str] | None = None,
    optType: str | None = None,
) -> list[OptContract]:
    ph = ", ".join([f":e{i}" for i in range(len(exchanges))])
    params: dict[str, Any] = {f"e{i}": exchanges[i] for i in range(len(exchanges))}
    extra = ""
    if tsCodes:
        extra = " AND ts_code IN (" + ", ".join([f":c{i}" for i in range(len(tsCodes))]) + ")"
        params.update({f"c{i}": tsCodes[i] for i in range(len(tsCodes))})
    if optType:
        extra += " AND opt_type=:ot"
        params["ot"] = optType
    sql = (
        "SELECT ts_code, exchange, list_date, delist_date FROM opt_basic "
        f"WHERE exchange IN ({ph}){extra} ORDER BY ts_code"
    )
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    out = [
        OptContract(
            tsCode=str(r["ts_code"]).strip(),
            exchange=str(r["exchange"] or "").strip(),
            listDate=toDate(r["list_date"]),
            delistDate=toDate(r["delist_date"]),
        )
        for r in rows
        if r.get("ts_code")
    ]
    logger.info(
        "opt_basic 宇宙 %s 份（交易所=%s%s）",
        len(out),
        ",".join(exchanges),
        f" opt_type={optType}" if optType else "",
    )
    return out


def loadStateMap(engine: Engine) -> dict[str, SyncState]:
    sql = (
        f"SELECT ts_code, max_trade_time, row_count, status FROM `{STATE_TABLE}` "
        "WHERE freq=:f"
    )
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"f": FREQ}).mappings().all()
    out: dict[str, SyncState] = {}
    for r in rows:
        code = str(r["ts_code"]).strip()
        out[code] = SyncState(
            tsCode=code,
            maxTradeTime=toDateTime(r["max_trade_time"]),
            rowCount=int(r["row_count"] or 0),
            status=str(r["status"] or "pending"),
        )
    return out


def upsertState(
    engine: Engine,
    tsCode: str,
    *,
    maxTradeTime: dt.datetime | None,
    addRows: int,
    status: str,
    lastError: str | None,
) -> None:
    sql = (
        f"INSERT INTO `{STATE_TABLE}` "
        "(ts_code, freq, max_trade_time, row_count, status, last_error) "
        "VALUES (:c, :f, :mx, :n, :st, :err) "
        "ON DUPLICATE KEY UPDATE "
        "max_trade_time=GREATEST("
        "COALESCE(VALUES(max_trade_time), max_trade_time), "
        "COALESCE(max_trade_time, VALUES(max_trade_time))), "
        "row_count=row_count+VALUES(row_count), "
        "status=VALUES(status), last_error=VALUES(last_error), "
        "updated_at=CURRENT_TIMESTAMP"
    )
    with engine.begin() as conn:
        conn.execute(
            text(sql),
            {
                "c": tsCode,
                "f": FREQ,
                "mx": maxTradeTime,
                "n": addRows,
                "st": status,
                "err": (lastError or "")[:255] or None,
            },
        )


def fetchSseOpenDates(pro: Any, startYyyymmdd: str, endYyyymmdd: str) -> list[dt.date]:
    df = retryRequest(
        pro.trade_cal,
        exchange="SSE",
        start_date=startYyyymmdd,
        end_date=endYyyymmdd,
        is_open="1",
    )
    if df is None or df.empty:
        return []
    s = df["cal_date"].astype(str).str.replace(r"\D", "", regex=True)
    out: list[dt.date] = []
    for x in s.tolist():
        if len(x) == 8:
            out.append(dt.datetime.strptime(x, "%Y%m%d").date())
    return sorted(set(out))


def sseIncrementalBounds(pro: Any, today: dt.date, nDays: int) -> tuple[dt.date, dt.date | None]:
    """增量回看起点 + 最近开市日（用于跳过已新鲜）。"""
    start = (today - dt.timedelta(days=120)).strftime("%Y%m%d")
    opens = fetchSseOpenDates(pro, start, today.strftime("%Y%m%d"))
    if not opens:
        return today - dt.timedelta(days=nDays * 2), None
    recent = [d for d in opens if d <= today]
    if not recent:
        return today - dt.timedelta(days=nDays * 2), None
    lastOpen = recent[-1]
    if len(recent) <= nDays:
        return recent[0], lastOpen
    return recent[-nDays], lastOpen


def lookbackStartDate(pro: Any, today: dt.date, nDays: int) -> dt.date:
    lb, _ = sseIncrementalBounds(pro, today, nDays)
    return lb


def loadIncrementalUniverse(
    engine: Engine,
    exchanges: tuple[str, ...],
    lookbackStart: dt.date,
    today: dt.date,
    optType: str | None = None,
) -> tuple[list[OptContract], dict[str, SyncState]]:
    """只加载在市 / 近窗摘牌 / 未拉齐摘牌 / 无状态或 pending|error。"""
    ph = ", ".join([f":e{i}" for i in range(len(exchanges))])
    params: dict[str, Any] = {f"e{i}": exchanges[i] for i in range(len(exchanges))}
    params.update({"f": FREQ, "today": today, "lb": lookbackStart})
    typeSql = ""
    if optType:
        typeSql = " AND b.opt_type=:ot"
        params["ot"] = optType
    sql = (
        "SELECT b.ts_code, b.exchange, b.list_date, b.delist_date, "
        "s.max_trade_time, s.row_count, s.status "
        "FROM opt_basic b "
        f"LEFT JOIN `{STATE_TABLE}` s ON s.ts_code=b.ts_code AND s.freq=:f "
        f"WHERE b.exchange IN ({ph}){typeSql} AND ("
        "b.delist_date IS NULL OR b.delist_date>=:today "
        "OR b.delist_date>=:lb OR s.ts_code IS NULL "
        "OR s.status IN ('pending','error') "
        "OR (s.status='partial' AND b.delist_date IS NOT NULL "
        "AND b.delist_date<:today "
        "AND (s.max_trade_time IS NULL OR DATE(s.max_trade_time)<b.delist_date))"
        ") ORDER BY b.ts_code"
    )
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    contracts: list[OptContract] = []
    stateMap: dict[str, SyncState] = {}
    for r in rows:
        if not r.get("ts_code"):
            continue
        code = str(r["ts_code"]).strip()
        contracts.append(
            OptContract(
                tsCode=code,
                exchange=str(r["exchange"] or "").strip(),
                listDate=toDate(r["list_date"]),
                delistDate=toDate(r["delist_date"]),
            )
        )
        if r.get("status") is not None or r.get("max_trade_time") is not None:
            stateMap[code] = SyncState(
                tsCode=code,
                maxTradeTime=toDateTime(r["max_trade_time"]),
                rowCount=int(r["row_count"] or 0),
                status=str(r["status"] or "pending"),
            )
    logger.info(
        "增量 SQL 宇宙 %s 份（回看起点 %s，带状态 %s%s）",
        len(contracts),
        lookbackStart,
        len(stateMap),
        f" opt_type={optType}" if optType else "",
    )
    return contracts, stateMap


def selectIncrementalContracts(
    contracts: list[OptContract],
    stateMap: dict[str, SyncState],
    lookbackStart: dt.date,
    today: dt.date,
) -> list[OptContract]:
    picked: list[OptContract] = []
    for c in contracts:
        listed = c.delistDate is None or c.delistDate >= today
        recent = c.delistDate is not None and c.delistDate >= lookbackStart
        st = stateMap.get(c.tsCode)
        missing = st is None or st.status in ("pending", "error")
        incompleteDelist = (
            st is not None
            and st.status == "partial"
            and c.delistDate is not None
            and c.delistDate < today
            and (st.maxTradeTime is None or st.maxTradeTime.date() < c.delistDate)
        )
        if listed or recent or missing or incompleteDelist:
            picked.append(c)
    return picked


def sortContracts(contracts: list[OptContract], today: dt.date) -> list[OptContract]:
    """在市优先，便于中断后仍先有最新合约。"""

    def _key(c: OptContract) -> tuple[int, dt.date, str]:
        listed = 0 if (c.delistDate is None or c.delistDate >= today) else 1
        return listed, c.listDate or dt.date.min, c.tsCode

    return sorted(contracts, key=_key)


def decideStatus(
    contract: OptContract,
    maxTradeTime: dt.datetime | None,
    fetched: int,
    today: dt.date,
    err: str | None,
) -> str:
    if err:
        return "error"
    if fetched <= 0 and maxTradeTime is None:
        return "empty"
    if isCoverageComplete(maxTradeTime, contract.delistDate):
        return "done"
    if contract.delistDate is not None and contract.delistDate < today and maxTradeTime:
        return "partial"
    return "partial"


def getThreadPro() -> Any:
    pro = getattr(_threadLocal, "pro", None)
    if pro is None:
        pro = ts.pro_api(TUSHARE_TOKEN)
        _threadLocal.pro = pro
    return pro


def syncOneContract(
    engine: Engine,
    contract: OptContract,
    state: SyncState | None,
    *,
    today: dt.date,
    lookbackDays: int,
    force: bool,
    freshCutoff: dt.datetime | None = None,
    skipFresh: bool = False,
) -> int:
    if not running:
        return 0
    if shouldSkipContract(
        contract,
        state,
        today,
        force=force,
        freshCutoff=freshCutoff,
        skipFresh=skipFresh,
    ):
        return 0
    localMax = None if force else (state.maxTradeTime if state else None)
    window = buildFetchWindow(
        contract.listDate, contract.delistDate, localMax, today, lookbackDays
    )
    if window is None:
        return 0
    start, end = window
    pro = getThreadPro()
    raw = fetchOptMinsRange(pro, contract.tsCode, start, end)
    if raw.empty:
        with _dbWriteLock:
            upsertState(
                engine,
                contract.tsCode,
                maxTradeTime=localMax,
                addRows=0,
                status=decideStatus(contract, localMax, 0, today, None),
                lastError=None,
            )
        return 0
    raw = raw.copy()
    raw["freq"] = FREQ
    maxTs = toDateTime(pd.to_datetime(raw["trade_time"], errors="coerce").max())
    if localMax and maxTs:
        maxTs = max(localMax, maxTs)
    elif localMax:
        maxTs = localMax
    status = decideStatus(contract, maxTs, len(raw), today, None)
    with _dbWriteLock:
        n = saveData(engine, raw)
        upsertState(
            engine,
            contract.tsCode,
            maxTradeTime=maxTs,
            addRows=n,
            status=status,
            lastError=None,
        )
    return n


def _bumpProgress(total: int, tsCode: str, n: int) -> None:
    global _progressDone
    with _progressLock:
        _progressDone += 1
        done = _progressDone
    if done % 20 == 0 or n > 0:
        logger.info("[进度 %s/%s] %s 写入约 %s 行", done, total, tsCode, n)


def runWorkerPool(
    engine: Engine,
    contracts: list[OptContract],
    stateMap: dict[str, SyncState],
    *,
    today: dt.date,
    lookbackDays: int,
    workers: int,
    force: bool,
    freshCutoff: dt.datetime | None = None,
    skipFresh: bool = False,
) -> int:
    global _progressDone
    _progressDone = 0
    if not contracts:
        logger.info("无待同步合约")
        return 0
    total = len(contracts)
    logger.info("开始同步 %s 份合约，workers=%s", total, workers)
    totalRows = 0
    it = iter(contracts)

    def _job(c: OptContract) -> tuple[str, int]:
        n = syncOneContract(
            engine,
            c,
            stateMap.get(c.tsCode),
            today=today,
            lookbackDays=lookbackDays,
            force=force,
            freshCutoff=freshCutoff,
            skipFresh=skipFresh,
        )
        return c.tsCode, n

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending: set[Any] = set()
        while running:
            while running and len(pending) < workers * 4:
                try:
                    c = next(it)
                except StopIteration:
                    break
                pending.add(pool.submit(_job, c))
            if not pending:
                break
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            if not running:
                for fut in pending:
                    fut.cancel()
                logger.warning("用户中断，未完成任务已取消")
                break
            for fut in done:
                if fut.cancelled():
                    continue
                try:
                    code, n = fut.result()
                    totalRows += n
                    _bumpProgress(total, code, n)
                except Exception:
                    logger.exception("合约任务失败")
    logger.info("本轮写入约 %s 行（含 UPSERT）", totalRows)
    return totalRows


def countLocalBars(engine: Engine, tsCode: str, start: dt.datetime, end: dt.datetime) -> int:
    sql = (
        f"SELECT COUNT(*) AS c FROM `{TABLE_NAME}` "
        "WHERE ts_code=:c AND freq=:f AND trade_time>=:s AND trade_time<=:e"
    )
    with engine.connect() as conn:
        r = conn.execute(
            text(sql),
            {"c": tsCode, "f": FREQ, "s": start, "e": end},
        ).mappings().first()
    return int(r["c"]) if r else 0


def loadLocalBarMap(
    engine: Engine, tsCode: str, start: dt.datetime, end: dt.datetime
) -> dict[str, dict[str, Any]]:
    sql = (
        f"SELECT trade_time, close, vol, amount, oi FROM `{TABLE_NAME}` "
        "WHERE ts_code=:c AND freq=:f AND trade_time>=:s AND trade_time<=:e"
    )
    with engine.connect() as conn:
        rows = conn.execute(
            text(sql),
            {"c": tsCode, "f": FREQ, "s": start, "e": end},
        ).mappings().all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = fmtApiDt(toDateTime(r["trade_time"]) or dt.datetime.min)
        out[key] = dict(r)
    return out


def pickCheckCodes(
    contracts: list[OptContract], stateMap: dict[str, SyncState], explicit: list[str]
) -> list[str]:
    if explicit:
        return explicit
    byEx: dict[str, str] = {}
    for c in contracts:
        st = stateMap.get(c.tsCode)
        if not st or not st.maxTradeTime:
            continue
        if c.exchange not in byEx:
            byEx[c.exchange] = c.tsCode
        if len(byEx) >= len(DEFAULT_EXCHANGES):
            break
    extra = [s.tsCode for s in stateMap.values() if s.maxTradeTime][:CHECK_SAMPLE_CODES]
    out: list[str] = []
    seen: set[str] = set()
    for code in list(byEx.values()) + extra:
        if code in seen:
            continue
        seen.add(code)
        out.append(code)
        if len(out) >= CHECK_SAMPLE_CODES:
            break
    return out


def _numClose(a: Any, d: Any) -> bool:
    if a is None or d is None:
        return True
    try:
        av = float(a)
        dv = float(d)
    except (TypeError, ValueError):
        return True
    return abs(av - dv) / (abs(av) + 1e-8) <= DATA_TOLERANCE


def consistencyCheck(
    engine: Engine,
    contracts: list[OptContract],
    stateMap: dict[str, SyncState],
    explicit: list[str],
    today: dt.date,
) -> list[str]:
    codes = pickCheckCodes(contracts, stateMap, explicit)
    if not codes:
        logger.info("无可用抽样合约，跳过一致性校验")
        return []
    pro = ts.pro_api(TUSHARE_TOKEN)
    bad: list[str] = []
    cmap = {c.tsCode: c for c in contracts}
    for code in codes:
        if not running:
            break
        st = stateMap.get(code)
        endDt = st.maxTradeTime if st and st.maxTradeTime else dt.datetime.combine(today, dt.time(23, 59, 59))
        start = endDt - dt.timedelta(days=10)
        end = endDt + dt.timedelta(minutes=1)
        apiDf = fetchOptMinsRange(pro, code, start, end)
        dbN = countLocalBars(engine, code, start, end)
        apiN = 0 if apiDf.empty else len(apiDf)
        if apiN != dbN:
            logger.error("不一致 %s：API=%s 本地=%s（近 10 日）", code, apiN, dbN)
            bad.append(code)
            continue
        if apiDf.empty:
            logger.info("一致性通过 %s（近 10 日空）", code)
            continue
        dbMap = loadLocalBarMap(engine, code, start, end)
        rowBad = False
        sample = apiDf.head(min(20, len(apiDf)))
        for _, row in sample.iterrows():
            key = fmtApiDt(toDateTime(row.get("trade_time")) or dt.datetime.min)
            dbRow = dbMap.get(key)
            if not dbRow:
                rowBad = True
                break
            for col in CHECK_NUM_COLS:
                if not _numClose(row.get(col), dbRow.get(col)):
                    logger.error("数值不一致 %s %s %s", code, key, col)
                    rowBad = True
                    break
            if rowBad:
                break
        if rowBad:
            bad.append(code)
        else:
            logger.info("一致性通过 %s 近10日=%s 所=%s", code, apiN, cmap.get(code).exchange if code in cmap else "")
    return bad


def applyTsCodeFilter(contracts: list[OptContract], tsCodes: list[str]) -> list[OptContract]:
    if not tsCodes:
        return contracts
    have = {c.tsCode: c for c in contracts}
    out: list[OptContract] = []
    for code in tsCodes:
        if code in have:
            out.append(have[code])
        else:
            logger.warning("opt_basic 无 %s，仍按宽窗口拉取", code)
            out.append(OptContract(tsCode=code, exchange="", listDate=None, delistDate=None))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="opt_mins 期权 60min 入库")
    parser.add_argument("--mode", choices=("auto", "full", "incremental"), default="auto")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument(
        "--exchange",
        default="",
        help="逗号分隔交易所，如 SSE,SZSE；空则默认六所",
    )
    parser.add_argument(
        "--opt-type",
        default="",
        help="按 opt_basic.opt_type 过滤，如 ETF期权",
    )
    parser.add_argument("--ts-code", default="", help="逗号分隔合约，供测试/补拉")
    parser.add_argument("--max-codes", type=int, default=0, help="最多处理 N 份，0 不限制")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--lookback-days", type=int, default=INCREMENTAL_LOOKBACK_TRADING_DAYS)
    parser.add_argument("--force", action="store_true", help="忽略 done/empty，重拉窗口")
    parser.add_argument(
        "--no-skip-fresh",
        action="store_true",
        help="增量不跳过已覆盖最近开市日的合约",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="API 节拍秒数；增量默认 0.13，全量默认 0.2",
    )
    parser.add_argument("--skip-check", action="store_true")
    args = parser.parse_args()

    exchanges: tuple[str, ...] = parseExchanges(args.exchange) or DEFAULT_EXCHANGES
    optType = args.opt_type.strip() or None
    tsCodes = parseTsCodes(args.ts_code)
    workers = clampWorkers(args.workers)
    lookbackDays = max(0, int(args.lookback_days))
    today = dt.date.today()
    engine = makeEngine()
    createTable(engine)
    tableRows = tableRowCount(engine)

    if shouldRefuseUnconstrainedAuto(args.mode, tsCodes, args.max_codes):
        logger.error(
            "未指定 --ts-code/--max-codes 时，默认 auto 拒绝开跑，以免盘中误扫全市场。"
            "测试：python opt_mins.py --ts-code 10007976.SH ；"
            "盘后全量：python opt_mins.py --mode full ；"
            "盘后增量：python opt_mins.py --mode incremental"
        )
        sys.exit(2)

    mode = args.mode
    if mode == "auto":
        mode = "full" if tableRows == 0 else "incremental"

    lastOpen: dt.date | None = None
    if args.check_only:
        contracts = applyTsCodeFilter(
            loadOptBasicContracts(engine, exchanges, tsCodes or None, optType), tsCodes
        )
        stateMap = loadStateMap(engine)
        bad = consistencyCheck(engine, contracts, stateMap, tsCodes, today)
        if bad:
            logger.error("校验未通过：%s", bad)
            sys.exit(1)
        logger.info("校验通过")
        return

    if mode == "incremental" and not tsCodes:
        pro = ts.pro_api(TUSHARE_TOKEN)
        lb, lastOpen = sseIncrementalBounds(pro, today, lookbackDays)
        contracts, stateMap = loadIncrementalUniverse(
            engine, exchanges, lb, today, optType
        )
    else:
        contracts = applyTsCodeFilter(
            loadOptBasicContracts(engine, exchanges, tsCodes or None, optType), tsCodes
        )
        stateMap = loadStateMap(engine)
        if mode == "incremental":
            pro = ts.pro_api(TUSHARE_TOKEN)
            lb, lastOpen = sseIncrementalBounds(pro, today, lookbackDays)
            contracts = selectIncrementalContracts(contracts, stateMap, lb, today)
            logger.info("增量宇宙 %s 份（回看起点 %s）", len(contracts), lb)
        elif not isSafeTestRun(tsCodes, args.max_codes):
            logger.warning(
                "全量模式：约 %s 份合约，60min 历史可能十余小时。盘中请勿跑全市场。",
                len(contracts),
            )

    if args.interval is not None:
        _tusharePace.setInterval(args.interval)
        paceSec = max(0.12, float(args.interval))
    elif mode == "incremental":
        _tusharePace.setInterval(INCREMENTAL_REQUEST_INTERVAL)
        paceSec = INCREMENTAL_REQUEST_INTERVAL
    else:
        paceSec = REQUEST_INTERVAL
    skipFresh = mode == "incremental" and not args.no_skip_fresh
    freshCutoff = incrementalFreshCutoff(lastOpen) if skipFresh else None
    if mode == "incremental":
        logger.info(
            "增量节拍 %ss；跳过已新鲜=%s 阈值=%s",
            paceSec,
            skipFresh,
            freshCutoff,
        )

    beforeSkip = len(contracts)
    contracts = [
        c
        for c in contracts
        if not shouldSkipContract(
            c,
            stateMap.get(c.tsCode),
            today,
            force=args.force,
            freshCutoff=freshCutoff,
            skipFresh=skipFresh,
        )
    ]
    logger.info("过滤已完成/已新鲜 %s 份，待拉取 %s 份", beforeSkip - len(contracts), len(contracts))
    contracts = sortContracts(contracts, today)
    if args.max_codes > 0:
        contracts = contracts[: args.max_codes]
        logger.info("--max-codes=%s，本轮只处理 %s 份", args.max_codes, len(contracts))

    runWorkerPool(
        engine,
        contracts,
        stateMap,
        today=today,
        lookbackDays=lookbackDays,
        workers=workers,
        force=args.force,
        freshCutoff=freshCutoff,
        skipFresh=skipFresh,
    )
    if args.skip_check:
        return
    stateMap = loadStateMap(engine)
    bad = consistencyCheck(engine, contracts, stateMap, tsCodes, today)
    if bad:
        logger.warning("校验失败，重拉 %s", bad)
        repair = applyTsCodeFilter(
            loadOptBasicContracts(engine, exchanges, bad, optType), bad
        )
        runWorkerPool(
            engine,
            repair,
            loadStateMap(engine),
            today=today,
            lookbackDays=lookbackDays,
            workers=min(workers, 3),
            force=True,
        )
        bad2 = consistencyCheck(engine, repair, loadStateMap(engine), bad, today)
        if bad2:
            logger.error("修复后仍不一致：%s", bad2)
            sys.exit(1)
    logger.info("全部完成")


if __name__ == "__main__":
    main()

# 代码更新时间：2026-09-06 12:28
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-04 21:04 被导入时不再 force 覆盖宿主日志（单独运行不变）
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
