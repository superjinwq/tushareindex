#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口：fut_daily（期货日线行情）
描述：先同步/读取 fut_trade_cal，再按各所开市日 × exchange 分页拉取 fut_daily 落库 stock_tushare.fut_daily。
文档：https://tushare.pro/document/2?doc_id=138
限量：单次最大 2000 条，须 limit/offset 分页（规则 A）。

运行：
  python fut_daily.py
  python fut_daily.py --mode full
  python fut_daily.py --skip-cal-refresh
  python fut_daily.py --exchange DCE
  python fut_daily.py --skip-mapping-refresh
  python fut_daily.py --mapping-only
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import signal
import sys
import time
from decimal import Decimal, InvalidOperation
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Callable

import numpy as np
import pandas as pd
import tushare as ts
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from urllib.parse import quote_plus

import fut_mapping as fm
import fut_trade_cal as ftc

LOG_FILE = "fut_daily.log"


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
TABLE_NAME = "fut_daily"
CAL_TABLE = "fut_trade_cal"
BATCH_SIZE = 500
REQUEST_INTERVAL = 0.2
PAGE_LIMIT = 2000
INCREMENTAL_LOOKBACK_TRADING_DAYS = 5

DEFAULT_EXCHANGES = ftc.DEFAULT_EXCHANGES

FIELDS = (
    "ts_code,trade_date,pre_close,pre_settle,open,high,low,close,settle,"
    "change1,change2,vol,amount,oi,oi_chg,delv_settle"
)

DATA_TOLERANCE = 0.001
CHECK_NUM_COLS = ("close", "vol", "amount", "oi")
RETRY_SECONDS = (2, 4, 8)

PRICE_COLS = (
    "pre_close",
    "pre_settle",
    "open",
    "high",
    "low",
    "close",
    "settle",
    "change1",
    "change2",
    "delv_settle",
)

VARCHAR_MAX_LEN = {"ts_code": 20, "exchange": 16}
DECIMAL_SPECS: dict[str, tuple[int, int]] = {
    "pre_close": (10, 4),
    "pre_settle": (10, 4),
    "open": (10, 4),
    "high": (10, 4),
    "low": (10, 4),
    "close": (10, 4),
    "settle": (10, 4),
    "change1": (10, 4),
    "change2": (10, 4),
    "delv_settle": (10, 4),
    "vol": (20, 4),
    "amount": (24, 6),
    "oi": (20, 4),
    "oi_chg": (20, 4),
}

INSERT_COLS = [
    "ts_code",
    "trade_date",
    "exchange",
    "pre_close",
    "pre_settle",
    "open",
    "high",
    "low",
    "close",
    "settle",
    "change1",
    "change2",
    "vol",
    "amount",
    "oi",
    "oi_chg",
    "delv_settle",
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
  `ts_code` varchar(20) NOT NULL COMMENT 'TS合约代码',
  `trade_date` date NOT NULL COMMENT '交易日期',
  `exchange` varchar(16) NOT NULL COMMENT '交易所',
  `pre_close` decimal(10,4) DEFAULT NULL COMMENT '昨收盘价',
  `pre_settle` decimal(10,4) DEFAULT NULL COMMENT '昨结算价',
  `open` decimal(10,4) DEFAULT NULL COMMENT '开盘价',
  `high` decimal(10,4) DEFAULT NULL COMMENT '最高价',
  `low` decimal(10,4) DEFAULT NULL COMMENT '最低价',
  `close` decimal(10,4) DEFAULT NULL COMMENT '收盘价',
  `settle` decimal(10,4) DEFAULT NULL COMMENT '结算价',
  `change1` decimal(10,4) DEFAULT NULL COMMENT '涨跌1 收盘价-昨结算价',
  `change2` decimal(10,4) DEFAULT NULL COMMENT '涨跌2 结算价-昨结算价',
  `vol` decimal(20,4) DEFAULT NULL COMMENT '成交量(手)',
  `amount` decimal(24,6) DEFAULT NULL COMMENT '成交金额(万元)',
  `oi` decimal(20,4) DEFAULT NULL COMMENT '持仓量(手)',
  `oi_chg` decimal(20,4) DEFAULT NULL COMMENT '持仓量变化',
  `delv_settle` decimal(10,4) DEFAULT NULL COMMENT '交割结算价',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ts_trade` (`ts_code`,`trade_date`),
  KEY `idx_trade_date` (`trade_date`),
  KEY `idx_ex_td` (`exchange`,`trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='期货日线行情 fut_daily';
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


def fetchFutDailyPage(pro: Any, *, exchange: str, tradeDate: str, offset: int) -> pd.DataFrame:
    time.sleep(REQUEST_INTERVAL)
    df = pro.fut_daily(
        exchange=exchange,
        trade_date=tradeDate,
        fields=FIELDS,
        limit=PAGE_LIMIT,
        offset=offset,
    )
    return df if df is not None else pd.DataFrame()


def fetchOneDayExchange(pro: Any, exchange: str, tradeYyyymmdd: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    offset = 0
    while running:
        page = retryRequest(
            fetchFutDailyPage,
            pro=pro,
            exchange=exchange,
            tradeDate=tradeYyyymmdd,
            offset=offset,
        )
        if page.empty:
            break
        page = page.copy()
        page["exchange"] = exchange
        frames.append(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        logger.info("[%s %s] 分页 offset=%s 本页 %s 行", tradeYyyymmdd, exchange, offset, len(page))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


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


def _processDf(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    if "trade_date" in out.columns:
        s = out["trade_date"].astype(str).str.replace(r"\D", "", regex=True)
        out["trade_date"] = pd.to_datetime(s, format="%Y%m%d", errors="coerce")
        out["trade_date"] = out["trade_date"].dt.strftime("%Y-%m-%d")
        out["trade_date"] = out["trade_date"].where(out["trade_date"].notna(), None)
    for col in PRICE_COLS:
        if col not in out.columns:
            continue
        out[col] = pd.to_numeric(out[col], errors="coerce").round(4)
    for col in ("vol", "oi", "oi_chg"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").round(4)
    if "amount" in out.columns:
        out["amount"] = pd.to_numeric(out["amount"], errors="coerce").round(6)
    if "ts_code" in out.columns:
        out["ts_code"] = out["ts_code"].astype(str).str.strip()
    if "exchange" in out.columns:
        out["exchange"] = out["exchange"].astype(str).str.strip()
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
                    "decimal 越界 列=%s ts_code=%s 值=%s (M,D)=(%s,%s)",
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
            if len(str(val)) > maxLen:
                raise ValueError("varchar too long")


def saveData(engine: Engine, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    df2 = _processDf(df)
    validate_before_save(df2)
    cols = [c for c in INSERT_COLS if c in df2.columns]
    placeholders = ", ".join([f":{c}" for c in cols])
    updateParts = [f"`{c}`=VALUES(`{c}`)" for c in cols if c not in ("ts_code", "trade_date")]
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
    n = 0
    with engine.begin() as conn:
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            conn.execute(text(sql), batch)
            n += len(batch)
    return n


def pruneStaleForDayExchange(
    engine: Engine, tradeYyyymmdd: str, exchange: str, keepTsCodes: set[str]
) -> int:
    y = f"{tradeYyyymmdd[:4]}-{tradeYyyymmdd[4:6]}-{tradeYyyymmdd[6:8]}"
    with engine.connect() as conn:
        r = conn.execute(
            text(
                f"SELECT `ts_code` FROM `{TABLE_NAME}` WHERE `trade_date` = :td AND `exchange` = :ex"
            ),
            {"td": y, "ex": exchange},
        )
        dbCodes = {str(row[0]) for row in r.fetchall()}
    stale = list(dbCodes - keepTsCodes)
    if not stale:
        return 0
    deleted = 0
    with engine.begin() as conn:
        for i in range(0, len(stale), 500):
            part = stale[i : i + 500]
            ph = ", ".join([f":p{j}" for j in range(len(part))])
            params = {f"p{j}": part[j] for j in range(len(part))}
            params["td"] = y
            params["ex"] = exchange
            rr = conn.execute(
                text(
                    f"DELETE FROM `{TABLE_NAME}` WHERE `trade_date` = :td AND `exchange` = :ex "
                    f"AND `ts_code` IN ({ph})"
                ),
                params,
            )
            deleted += rr.rowcount or 0
    return deleted


def tableRowCount(engine: Engine) -> int:
    with engine.connect() as conn:
        r = conn.execute(text(f"SELECT COUNT(*) AS c FROM `{TABLE_NAME}`"))
        row = r.mappings().first()
        return int(row["c"]) if row else 0


def maxTradeDateForExchange(engine: Engine, exchange: str) -> str | None:
    with engine.connect() as conn:
        r = conn.execute(
            text(f"SELECT MAX(`trade_date`) AS m FROM `{TABLE_NAME}` WHERE `exchange` = :ex"),
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


def countDayExchange(engine: Engine, tradeYyyymmdd: str, exchange: str) -> int:
    y = f"{tradeYyyymmdd[:4]}-{tradeYyyymmdd[4:6]}-{tradeYyyymmdd[6:8]}"
    with engine.connect() as conn:
        r = conn.execute(
            text(
                f"SELECT COUNT(*) AS c FROM `{TABLE_NAME}` WHERE `trade_date` = :td AND `exchange` = :ex"
            ),
            {"td": y, "ex": exchange},
        )
        row = r.mappings().first()
        return int(row["c"]) if row else 0


def syncOneDayExchange(
    pro: Any, engine: Engine, tradeYyyymmdd: str, exchange: str, *, doWrite: bool
) -> int:
    raw = fetchOneDayExchange(pro, exchange, tradeYyyymmdd)
    if raw.empty:
        return 0
    apiCodes = set(raw["ts_code"].astype(str).str.strip())
    if not doWrite:
        return len(raw)
    n = saveData(engine, raw)
    pruned = pruneStaleForDayExchange(engine, tradeYyyymmdd, exchange, apiCodes)
    if pruned:
        logger.info("[%s %s] 修剪孤儿 %s 条", tradeYyyymmdd, exchange, pruned)
    return n


def datesForIncremental(engine: Engine, exchange: str) -> list[str]:
    """该所：新开市日 + 最近 N 个开市日回看。"""
    today = dt.date.today().strftime("%Y%m%d")
    allOpen = openDatesFromDb(engine, exchange, endYyyymmdd=today)
    if not allOpen:
        return []
    maxD = maxTradeDateForExchange(engine, exchange)
    if not maxD:
        return allOpen
    future = [d for d in allOpen if d > maxD]
    try:
        idx = max(i for i, d in enumerate(allOpen) if d <= maxD)
    except ValueError:
        return future
    lb = max(0, idx - (INCREMENTAL_LOOKBACK_TRADING_DAYS - 1))
    window = allOpen[lb : idx + 1]
    return sorted(set(window + future))


def importExchangeDates(
    pro: Any,
    engine: Engine,
    exchange: str,
    dates: list[str],
    *,
    doWrite: bool,
    label: str,
) -> int:
    total = 0
    n = len(dates)
    for i, td in enumerate(dates, start=1):
        if not running:
            break
        u = syncOneDayExchange(pro, engine, td, exchange, doWrite=doWrite)
        total += u
        if i % 100 == 0 or u > 0:
            logger.info("[%s] [%s/%s] %s %s 约 %s 行", label, i, n, td, exchange, u)
    return total


def consistencyCheck(pro: Any, engine: Engine, exchanges: tuple[str, ...]) -> list[tuple[str, str]]:
    bad: set[tuple[str, str]] = set()
    for ex in exchanges:
        if not running:
            break
        maxD = maxTradeDateForExchange(engine, ex)
        if not maxD:
            continue
        apiDf = fetchOneDayExchange(pro, ex, maxD)
        apiN = len(apiDf)
        dbN = countDayExchange(engine, maxD, ex)
        if apiN != dbN:
            logger.error("不一致 %s %s：API=%s 本地=%s", maxD, ex, apiN, dbN)
            bad.add((maxD, ex))
            continue
        if apiDf.empty:
            logger.info("一致性通过 %s %s（无成交数据）", maxD, ex)
            continue
        y = f"{maxD[:4]}-{maxD[4:6]}-{maxD[6:8]}"
        sample = apiDf.head(min(30, len(apiDf)))
        rowBad = False
        with engine.connect() as conn:
            for _, row in sample.iterrows():
                tsCode = str(row["ts_code"]).strip()
                r = conn.execute(
                    text(
                        f"SELECT close, vol, amount, oi FROM `{TABLE_NAME}` "
                        f"WHERE ts_code=:c AND trade_date=:td LIMIT 1"
                    ),
                    {"c": tsCode, "td": y},
                )
                dbRow = r.mappings().first()
                if not dbRow:
                    rowBad = True
                    break
                for col in CHECK_NUM_COLS:
                    av = row.get(col)
                    dv = dbRow.get(col)
                    if av is None or dv is None:
                        continue
                    try:
                        a = float(av)
                        d = float(dv)
                    except (TypeError, ValueError):
                        continue
                    if abs(a - d) / (abs(a) + 1e-8) > DATA_TOLERANCE:
                        rowBad = True
                        break
                if rowBad:
                    break
        if rowBad:
            bad.add((maxD, ex))
        else:
            logger.info("一致性通过 %s %s 条数=%s", maxD, ex, apiN)
    return sorted(bad)


def repairPairs(pro: Any, engine: Engine, pairs: list[tuple[str, str]]) -> None:
    for td, ex in pairs:
        if not running:
            break
        logger.warning("修复同步 %s %s", td, ex)
        syncOneDayExchange(pro, engine, td, ex, doWrite=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="fut_daily 期货日线入库")
    parser.add_argument("--mode", choices=("auto", "full", "incremental"), default="auto")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--exchange", default="", help="仅单所，如 DCE")
    parser.add_argument("--skip-check", action="store_true")
    parser.add_argument("--skip-cal-refresh", action="store_true", help="跳过启动时日历刷新")
    parser.add_argument("--skip-mapping-refresh", action="store_true", help="跳过 fut_mapping 同步")
    parser.add_argument("--mapping-only", action="store_true", help="仅同步 fut_mapping")
    args = parser.parse_args()

    exchanges: tuple[str, ...] = (
        (args.exchange.strip().upper(),) if args.exchange.strip() else DEFAULT_EXCHANGES
    )

    engine = makeEngine()
    pro = ts.pro_api(TUSHARE_TOKEN)

    if not args.skip_cal_refresh:
        calMode = "full" if args.mode == "full" else "auto"
        logger.info("刷新 fut_trade_cal（mode=%s）…", calMode)
        ftc.syncTradeCal(engine, pro, exchanges, mode=calMode)

    if args.mapping_only:
        mapMode = "full" if args.mode == "full" else "auto"
        logger.info("仅同步 fut_mapping（mode=%s）…", mapMode)
        fm.syncMapping(engine, pro, exchanges, mode=mapMode)
        logger.info("fut_mapping 完成")
        return

    if args.check_only:
        bad = consistencyCheck(pro, engine, exchanges)
        if bad:
            logger.error("校验未通过：%s", bad)
            sys.exit(1)
        logger.info("校验通过")
        return

    createTable(engine)
    isEmpty = tableRowCount(engine) == 0

    if args.mode == "full" or isEmpty:
        if isEmpty:
            logger.info("fut_daily 为空，按各所 fut_trade_cal 开市日全量")
        else:
            logger.info("--mode full：按日历全量重拉")
        for ex in exchanges:
            if not running:
                break
            dates = openDatesFromDb(engine, ex)
            if not dates:
                logger.warning("[%s] fut_trade_cal 无开市日，跳过", ex)
                continue
            logger.info("[%s] 全量 %s 个交易日", ex, len(dates))
            importExchangeDates(pro, engine, ex, dates, doWrite=True, label="全量")
    else:
        for ex in exchanges:
            if not running:
                break
            dates = datesForIncremental(engine, ex)
            if not dates:
                logger.info("[%s] 无待同步交易日", ex)
                continue
            logger.info(
                "[%s] 增量 %s 个交易日（含回看 %s 日）",
                ex,
                len(dates),
                INCREMENTAL_LOOKBACK_TRADING_DAYS,
            )
            importExchangeDates(pro, engine, ex, dates, doWrite=True, label="增量")

    if not args.skip_mapping_refresh:
        mapMode = "full" if args.mode == "full" else "auto"
        logger.info("同步 fut_mapping（mode=%s）…", mapMode)
        fm.syncMapping(engine, pro, exchanges, mode=mapMode)

    if args.skip_check:
        return

    bad = consistencyCheck(pro, engine, exchanges)
    if bad:
        repairPairs(pro, engine, bad)
        bad2 = consistencyCheck(pro, engine, exchanges)
        if bad2:
            logger.error("修复后仍不一致：%s", bad2)
            sys.exit(1)
    logger.info("全部完成")


if __name__ == "__main__":
    main()

# 代码更新时间：2026-06-01 12:45
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
