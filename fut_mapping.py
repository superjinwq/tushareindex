#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口：fut_mapping（期货主力与连续合约映射）
描述：按品种主力连续码（如 JD.DCE、IF.CFX）拉取 mapping_ts_code，落库 stock_tushare.fut_mapping。
文档：https://tushare.pro/document/2?doc_id=189
限量：单次最大 2000 条，须 limit/offset 分页。

运行：
  python fut_mapping.py
  python fut_mapping.py --mode full
  python fut_mapping.py --exchange DCE
  python fut_mapping.py --code JD.DCE
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
from typing import Any, Callable, NamedTuple

import numpy as np
import pandas as pd
import tushare as ts
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from urllib.parse import quote_plus

LOG_FILE = "fut_mapping.log"
CAL_TABLE = "fut_trade_cal"


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
TABLE_NAME = "fut_mapping"
BATCH_SIZE = 500
REQUEST_INTERVAL = 0.2
PAGE_LIMIT = 2000
INCREMENTAL_LOOKBACK_OPEN_DAYS = 30
MAPPING_FIELDS = "ts_code,trade_date,mapping_ts_code"
RETRY_SECONDS = (2, 4, 8)
INSERT_COLS = ["ts_code", "trade_date", "mapping_ts_code"]

DEFAULT_EXCHANGES = ("CFFEX", "DCE", "CZCE", "SHFE", "INE", "GFEX")

# fut_basic.exchange 与 ts_code 后缀不一致（如 CFFEX → .CFX）
EXCHANGE_TS_SUFFIX: dict[str, str] = {
    "CFFEX": "CFX",
    "SHFE": "SHF",
    "CZCE": "ZCE",
    "GFEX": "GFE",
    "DCE": "DCE",
    "INE": "INE",
}

EXCHANGE_SUFFIX_CASE_SQL = """
        CASE exchange
          WHEN 'CFFEX' THEN 'CFX'
          WHEN 'SHFE' THEN 'SHF'
          WHEN 'CZCE' THEN 'ZCE'
          WHEN 'GFEX' THEN 'GFE'
          ELSE exchange
        END
"""

PRIMARY_CONT_SQL = f"""
    SELECT ts_code, exchange, fut_code
    FROM fut_basic
    WHERE fut_code IS NOT NULL
      AND ts_code NOT REGEXP '[0-9]'
      AND ts_code = CONCAT(fut_code, '.', {EXCHANGE_SUFFIX_CASE_SQL.strip()})
"""

_engine: Engine | None = None


class PrimaryCont(NamedTuple):
    tsCode: str
    exchange: str
    futCode: str


def exchangeToTsSuffix(exchange: str) -> str:
    ex = exchange.strip().upper()
    return EXCHANGE_TS_SUFFIX.get(ex, ex)


def buildMainContCode(futCode: str, exchange: str) -> str:
    """月合约 fut_code + exchange → 主力连续 ts_code（如 IF + CFFEX → IF.CFX）。"""
    return f"{futCode.strip()}.{exchangeToTsSuffix(exchange)}"


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
  `ts_code` varchar(20) NOT NULL COMMENT '连续/主力合约代码',
  `trade_date` date NOT NULL COMMENT '交易日期',
  `mapping_ts_code` varchar(20) NOT NULL COMMENT '对应月合约代码',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ts_trade` (`ts_code`,`trade_date`),
  KEY `idx_trade_date` (`trade_date`),
  KEY `idx_mapping` (`mapping_ts_code`,`trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='期货主力映射 fut_mapping';
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


def fetchMappingPage(
    pro: Any,
    *,
    tsCode: str,
    startDate: str,
    endDate: str,
    offset: int,
) -> pd.DataFrame:
    time.sleep(REQUEST_INTERVAL)
    df = pro.fut_mapping(
        ts_code=tsCode,
        start_date=startDate,
        end_date=endDate,
        fields=MAPPING_FIELDS,
        limit=PAGE_LIMIT,
        offset=offset,
    )
    return df if df is not None else pd.DataFrame()


def yearChunks(startYyyymmdd: str, endYyyymmdd: str) -> list[tuple[str, str]]:
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


def fetchMappingRange(pro: Any, tsCode: str, startYyyymmdd: str, endYyyymmdd: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for s, e in yearChunks(startYyyymmdd, endYyyymmdd):
        if not running:
            break
        offset = 0
        while running:
            page = retryRequest(
                fetchMappingPage,
                pro=pro,
                tsCode=tsCode,
                startDate=s,
                endDate=e,
                offset=offset,
            )
            if page.empty:
                break
            frames.append(page)
            if len(page) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
    return out


def _processDf(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["ts_code"] = out["ts_code"].astype(str).str.strip()
    out["mapping_ts_code"] = out["mapping_ts_code"].astype(str).str.strip()
    s = out["trade_date"].astype(str).str.replace(r"\D", "", regex=True)
    out["trade_date"] = pd.to_datetime(s, format="%Y%m%d", errors="coerce")
    out["trade_date"] = out["trade_date"].dt.strftime("%Y-%m-%d")
    out["trade_date"] = out["trade_date"].where(out["trade_date"].notna(), None)
    out = out.replace({np.nan: None})
    out = out.dropna(subset=["ts_code", "trade_date", "mapping_ts_code"])
    return out


def saveMapping(engine: Engine, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    df2 = _processDf(df)
    if df2.empty:
        return 0
    placeholders = ", ".join([f":{c}" for c in INSERT_COLS])
    sql = (
        f"INSERT INTO `{TABLE_NAME}` (`{'`, `'.join(INSERT_COLS)}`) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE `mapping_ts_code`=VALUES(`mapping_ts_code`), "
        f"`updated_at`=CURRENT_TIMESTAMP"
    )
    rows = df2[INSERT_COLS].to_dict("records")
    n = 0
    with engine.begin() as conn:
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            conn.execute(text(sql), batch)
            n += len(batch)
    return n


def openDatesFromDb(
    engine: Engine,
    exchange: str,
    *,
    startYyyymmdd: str | None = None,
    endYyyymmdd: str | None = None,
) -> list[str]:
    sql = (
        f"SELECT `cal_date` FROM `{CAL_TABLE}` "
        f"WHERE `exchange` = :ex AND `is_open` = 1"
    )
    params: dict[str, Any] = {"ex": exchange}
    if startYyyymmdd:
        sql += " AND `cal_date` >= :sd"
        params["sd"] = f"{startYyyymmdd[:4]}-{startYyyymmdd[4:6]}-{startYyyymmdd[6:8]}"
    if endYyyymmdd:
        sql += " AND `cal_date` <= :ed"
        params["ed"] = f"{endYyyymmdd[:4]}-{endYyyymmdd[4:6]}-{endYyyymmdd[6:8]}"
    sql += " ORDER BY `cal_date`"
    with engine.connect() as conn:
        r = conn.execute(text(sql), params)
        out: list[str] = []
        for row in r.fetchall():
            d = row[0]
            if hasattr(d, "strftime"):
                out.append(d.strftime("%Y%m%d"))
            else:
                s = str(d).replace("-", "")[:8]
                if len(s) == 8:
                    out.append(s)
    return out


def loadPrimaryContCodes(
    engine: Engine,
    exchanges: tuple[str, ...] | None = None,
    singleCode: str | None = None,
) -> list[PrimaryCont]:
    sql = PRIMARY_CONT_SQL
    params: dict[str, Any] = {}
    if singleCode:
        sql += " AND ts_code = :code"
        params["code"] = singleCode.strip().upper()
    elif exchanges:
        ph = ", ".join([f":ex{i}" for i in range(len(exchanges))])
        sql += f" AND exchange IN ({ph})"
        for i, ex in enumerate(exchanges):
            params[f"ex{i}"] = ex
    sql += " ORDER BY exchange, ts_code"
    with engine.connect() as conn:
        r = conn.execute(text(sql), params)
        return [PrimaryCont(str(a).strip(), str(b).strip(), str(c).strip()) for a, b, c in r.fetchall()]


def maxTradeDateForCode(engine: Engine, tsCode: str) -> str | None:
    with engine.connect() as conn:
        r = conn.execute(
            text(f"SELECT MAX(`trade_date`) AS m FROM `{TABLE_NAME}` WHERE `ts_code` = :c"),
            {"c": tsCode},
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


def resolveDateRange(
    engine: Engine,
    cont: PrimaryCont,
    *,
    mode: str,
) -> tuple[str, str] | None:
    today = dt.date.today().strftime("%Y%m%d")
    allOpen = openDatesFromDb(engine, cont.exchange, endYyyymmdd=today)
    if not allOpen:
        logger.warning("[%s] fut_trade_cal 无开市日", cont.tsCode)
        return None
    if mode == "full":
        return allOpen[0], allOpen[-1]

    maxD = maxTradeDateForCode(engine, cont.tsCode)
    if not maxD:
        return allOpen[0], allOpen[-1]

    try:
        idx = max(i for i, d in enumerate(allOpen) if d <= maxD)
    except ValueError:
        return allOpen[0], allOpen[-1]

    lb = max(0, idx - (INCREMENTAL_LOOKBACK_OPEN_DAYS - 1))
    start = allOpen[lb]
    end = allOpen[-1]
    if start > end:
        return None
    return start, end


def syncOneCode(
    pro: Any,
    engine: Engine,
    cont: PrimaryCont,
    *,
    mode: str,
) -> int:
    dateRange = resolveDateRange(engine, cont, mode=mode)
    if not dateRange:
        return 0
    start, end = dateRange
    raw = fetchMappingRange(pro, cont.tsCode, start, end)
    if raw.empty:
        logger.warning("[%s] mapping API 无数据 %s~%s", cont.tsCode, start, end)
        return 0
    n = saveMapping(engine, raw)
    logger.info("[%s] UPSERT %s 行 (%s~%s)", cont.tsCode, n, start, end)
    return n


def syncMapping(
    engine: Engine,
    pro: Any,
    exchanges: tuple[str, ...],
    *,
    mode: str = "auto",
    singleCode: str | None = None,
) -> int:
    """供 fut_daily 调用：同步 fut_mapping。"""
    createTable(engine)
    codes = loadPrimaryContCodes(engine, exchanges if not singleCode else None, singleCode)
    if not codes:
        logger.warning("无主力连续码可同步")
        return 0

    isEmpty = tableRowCount(engine) == 0
    syncMode = "full" if mode == "full" or isEmpty else "incremental"
    if singleCode and mode != "full":
        syncMode = mode if mode in ("full", "incremental") else "incremental"

    logger.info(
        "fut_mapping 同步 mode=%s codes=%s",
        syncMode,
        len(codes),
    )
    total = 0
    for i, cont in enumerate(codes, start=1):
        if not running:
            break
        total += syncOneCode(pro, engine, cont, mode=syncMode)
        if i % 10 == 0 or i == len(codes):
            logger.info("mapping 进度 %s/%s", i, len(codes))
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="fut_mapping 期货主力映射入库")
    parser.add_argument("--mode", choices=("auto", "full", "incremental"), default="auto")
    parser.add_argument("--exchange", default="", help="仅单所，如 DCE")
    parser.add_argument("--code", default="", help="仅单连续码，如 JD.DCE")
    args = parser.parse_args()

    exchanges: tuple[str, ...] = (
        (args.exchange.strip().upper(),) if args.exchange.strip() else DEFAULT_EXCHANGES
    )
    mode = args.mode
    if mode == "auto":
        mode = "auto"
    elif mode == "incremental":
        mode = "incremental"

    engine = makeEngine()
    pro = ts.pro_api(TUSHARE_TOKEN)
    single = args.code.strip().upper() if args.code.strip() else None
    n = syncMapping(engine, pro, exchanges, mode=mode, singleCode=single)
    logger.info("fut_mapping 同步完成，共 UPSERT 约 %s 行", n)


if __name__ == "__main__":
    main()

# 代码更新时间：2026-06-01 13:05
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-04 21:04 被导入时不再 force 覆盖宿主日志（单独运行不变）
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
