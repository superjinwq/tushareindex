#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口：fut_trade_cal（期货交易日历）
描述：拉取六所期货交易日历（含休市 is_open=0），落库 stock_tushare.fut_trade_cal。
文档：https://tushare.pro/document/2?doc_id=467

运行：
  python fut_trade_cal.py
  python fut_trade_cal.py --mode full
  python fut_trade_cal.py --exchange DCE
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import signal
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Callable

import numpy as np
import pandas as pd
import tushare as ts
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from urllib.parse import quote_plus

LOG_FILE = "fut_trade_cal.log"


from windows_safe_log import WindowsSafeTimedRotatingFileHandler


def setupLogger() -> None:
    # 被已配置日志的宿主导入时不得 force 覆盖宿主日志
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
TABLE_NAME = "fut_trade_cal"
BATCH_SIZE = 500
REQUEST_INTERVAL = 0.2
FULL_START_DATE = "19900101"
INCREMENTAL_CALENDAR_BACK_DAYS = 30

DEFAULT_EXCHANGES = ("CFFEX", "DCE", "CZCE", "SHFE", "INE", "GFEX")

FIELDS = "exchange,cal_date,is_open,pretrade_date"
RETRY_SECONDS = (2, 4, 8)

INSERT_COLS = ["exchange", "cal_date", "is_open", "pretrade_date"]

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
  `exchange` varchar(16) NOT NULL COMMENT '交易所',
  `cal_date` date NOT NULL COMMENT '日历日期',
  `is_open` tinyint NOT NULL COMMENT '是否交易 0休市 1交易',
  `pretrade_date` date DEFAULT NULL COMMENT '上一交易日',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ex_cal` (`exchange`,`cal_date`),
  KEY `idx_cal_date` (`cal_date`),
  KEY `idx_ex_open` (`exchange`,`is_open`,`cal_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='期货交易日历 fut_trade_cal';
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


def fetchCalChunk(pro: Any, *, exchange: str, startDate: str, endDate: str) -> pd.DataFrame:
    time.sleep(REQUEST_INTERVAL)
    df = pro.fut_trade_cal(
        exchange=exchange,
        start_date=startDate,
        end_date=endDate,
        fields=FIELDS,
    )
    return df if df is not None else pd.DataFrame()


def yearChunks(startYyyymmdd: str, endYyyymmdd: str) -> list[tuple[str, str]]:
    """按自然年切分 API 请求区间。"""
    start = dt.datetime.strptime(startYyyymmdd, "%Y%m%d").date()
    end = dt.datetime.strptime(endYyyymmdd, "%Y%m%d").date()
    chunks: list[tuple[str, str]] = []
    y = start.year
    while y <= end.year:
        chunkStart = dt.date(y, 1, 1)
        if chunkStart < start:
            chunkStart = start
        chunkEnd = dt.date(y, 12, 31)
        if chunkEnd > end:
            chunkEnd = end
        chunks.append((chunkStart.strftime("%Y%m%d"), chunkEnd.strftime("%Y%m%d")))
        y += 1
    return chunks


def fetchExchangeCalendar(pro: Any, exchange: str, startDate: str, endDate: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for s, e in yearChunks(startDate, endDate):
        if not running:
            break
        page = retryRequest(fetchCalChunk, pro=pro, exchange=exchange, startDate=s, endDate=e)
        if page.empty:
            continue
        frames.append(page)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["exchange"] = out["exchange"].fillna(exchange).astype(str).str.strip()
    out = out.drop_duplicates(subset=["exchange", "cal_date"], keep="last")
    return out


def _processDf(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for col in ("cal_date", "pretrade_date"):
        if col not in out.columns:
            continue
        s = out[col].astype(str).str.replace(r"\D", "", regex=True)
        out[col] = pd.to_datetime(s, format="%Y%m%d", errors="coerce")
        out[col] = out[col].dt.strftime("%Y-%m-%d")
        out[col] = out[col].where(out[col].notna(), None)
    out["exchange"] = out["exchange"].astype(str).str.strip()
    out["is_open"] = pd.to_numeric(out["is_open"], errors="coerce").fillna(0).astype(int)
    out = out.replace({np.nan: None})
    return out


def validate_before_save(df: pd.DataFrame) -> None:
    if df.empty:
        return
    for idx, val in df["exchange"].items():
        if val is None or len(str(val)) > 16:
            logger.error("exchange 非法 行=%s val=%s", idx, val)
            raise ValueError("exchange invalid")
    for idx, val in df["is_open"].items():
        if val not in (0, 1):
            logger.error("is_open 非法 exchange=%s cal_date=%s val=%s", df.at[idx, "exchange"], df.at[idx, "cal_date"], val)
            raise ValueError("is_open invalid")


def saveData(engine: Engine, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    df2 = _processDf(df)
    validate_before_save(df2)
    placeholders = ", ".join([f":{c}" for c in INSERT_COLS])
    updateParts = [f"`{c}`=VALUES(`{c}`)" for c in INSERT_COLS if c not in ("exchange", "cal_date")]
    updateParts.append("`updated_at`=CURRENT_TIMESTAMP")
    sql = (
        f"INSERT INTO `{TABLE_NAME}` (`{'`, `'.join(INSERT_COLS)}`) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE " + ", ".join(updateParts)
    )
    rows = df2[INSERT_COLS].to_dict("records")
    n = 0
    with engine.begin() as conn:
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            conn.execute(text(sql), batch)
            n += len(batch)
    return n


def maxCalDateYyyymmdd(engine: Engine, exchange: str) -> str | None:
    with engine.connect() as conn:
        r = conn.execute(
            text(f"SELECT MAX(`cal_date`) AS m FROM `{TABLE_NAME}` WHERE `exchange` = :ex"),
            {"ex": exchange},
        )
        row = r.fetchone()
    if not row or row[0] is None:
        return None
    d = row[0]
    if hasattr(d, "strftime"):
        return d.strftime("%Y%m%d")
    s = str(d).replace("-", "")[:8]
    return s if len(s) == 8 else None


def tableRowCount(engine: Engine) -> int:
    with engine.connect() as conn:
        r = conn.execute(text(f"SELECT COUNT(*) AS c FROM `{TABLE_NAME}`"))
        row = r.mappings().first()
        return int(row["c"]) if row else 0


def getCalendarSyncEndDate(pro: Any, exchange: str) -> str:
    """日历拉取结束日：max(当年12-31, Tushare 该所 cal_date 上限)，不截断到 today。"""
    today = dt.date.today()
    floorEnd = f"{today.year}1231"
    probeEnd = f"{today.year + 1}1231"
    df = retryRequest(
        fetchCalChunk,
        pro=pro,
        exchange=exchange,
        startDate=f"{today.year}0101",
        endDate=probeEnd,
    )
    if df.empty or "cal_date" not in df.columns:
        logger.info("[%s] 结束日回退 %s", exchange, floorEnd)
        return floorEnd
    mx = df["cal_date"].astype(str).str.replace(r"\D", "", regex=True).max()
    if not mx or len(mx) != 8:
        logger.info("[%s] 结束日回退 %s", exchange, floorEnd)
        return floorEnd
    endDate = max(mx, floorEnd)
    logger.info("[%s] 日历结束日 %s", exchange, endDate)
    return endDate


def syncOneExchange(pro: Any, engine: Engine, exchange: str, startDate: str, endDate: str) -> int:
    logger.info("[%s] 拉取日历 %s ~ %s", exchange, startDate, endDate)
    raw = fetchExchangeCalendar(pro, exchange, startDate, endDate)
    if raw.empty:
        logger.warning("[%s] 日历 API 无数据", exchange)
        return 0
    n = saveData(engine, raw)
    logger.info("[%s] UPSERT %s 行", exchange, n)
    return n


def syncTradeCal(
    engine: Engine,
    pro: Any,
    exchanges: tuple[str, ...],
    *,
    mode: str = "auto",
) -> None:
    """供 fut_daily 调用：同步期货交易日历。"""
    createTable(engine)
    isEmpty = tableRowCount(engine) == 0

    if mode == "full" or isEmpty:
        for ex in exchanges:
            if not running:
                break
            endDate = getCalendarSyncEndDate(pro, ex)
            syncOneExchange(pro, engine, ex, FULL_START_DATE, endDate)
        return

    for ex in exchanges:
        if not running:
            break
        endDate = getCalendarSyncEndDate(pro, ex)
        maxD = maxCalDateYyyymmdd(engine, ex)
        if not maxD:
            syncOneExchange(pro, engine, ex, FULL_START_DATE, endDate)
            continue
        if maxD >= endDate:
            logger.info("[%s] 本地日历已至 %s，无需更新", ex, maxD)
            continue
        startDt = dt.datetime.strptime(maxD, "%Y%m%d").date() - dt.timedelta(days=INCREMENTAL_CALENDAR_BACK_DAYS)
        startStr = max(startDt.strftime("%Y%m%d"), FULL_START_DATE)
        syncOneExchange(pro, engine, ex, startStr, endDate)


def main() -> None:
    parser = argparse.ArgumentParser(description="fut_trade_cal 期货交易日历入库")
    parser.add_argument("--mode", choices=("auto", "full", "incremental"), default="auto")
    parser.add_argument("--exchange", default="", help="仅单所，如 DCE")
    args = parser.parse_args()

    exchanges: tuple[str, ...] = (
        (args.exchange.strip().upper(),) if args.exchange.strip() else DEFAULT_EXCHANGES
    )
    mode = "full" if args.mode == "full" else "auto"
    if args.mode == "incremental":
        mode = "auto"

    engine = makeEngine()
    pro = ts.pro_api(TUSHARE_TOKEN)
    syncTradeCal(engine, pro, exchanges, mode=mode)
    logger.info("日历同步完成")


if __name__ == "__main__":
    main()

# 代码更新时间：2026-05-31 16:12
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-04 21:04 被导入时不再 force 覆盖宿主日志（单独运行不变）
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
