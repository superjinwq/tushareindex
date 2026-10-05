#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口：opt_daily（期权日线行情）
描述：按 SSE 交易日历 × 六所交易所分页拉取期权日线，落库 stock_tushare.opt_daily；首次全量、增量带回看窗口、按所按日一致性校验与修复。
文档：https://tushare.pro/document/2?doc_id=159
限量：单次最大 15000 条，须 limit/offset 分页直至末页不满页（规则 A）。

运行：
  python opt_daily.py
  python opt_daily.py --mode full
  python opt_daily.py --check-only
  python opt_daily.py --exchange DCE

TUSHARE_TOKEN 硬编码于配置区（与 opt_basic 一致）。DB 可用 DB_HOST 等环境变量覆盖。
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

LOG_FILE = "opt_daily.log"


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
TABLE_NAME = "opt_daily"
BATCH_SIZE = 500
REQUEST_INTERVAL = 0.2
PAGE_LIMIT = 15000
DEFAULT_EXCHANGES = ("SSE", "SZSE", "CFFEX", "DCE", "SHFE", "CZCE")
# 全量起始：期权日线历史可按需前推；默认 2010 年起
FULL_START_DATE = "20100101"
# 增量时除新交易日外，再回看最近 N 个交易日（覆盖源端修订）
INCREMENTAL_LOOKBACK_TRADING_DAYS = 5

FIELDS = (
    "ts_code,trade_date,exchange,pre_settle,pre_close,open,high,low,close,settle,vol,amount,oi"
)

DATA_TOLERANCE = 0.001
CHECK_NUM_COLS = ("close", "vol", "amount", "oi")

RETRY_SECONDS = (2, 4, 8)

PRICE_COLS = ("pre_settle", "pre_close", "open", "high", "low", "close", "settle")
VARCHAR_MAX_LEN = {"ts_code": 20, "exchange": 16}
DECIMAL_SPECS: dict[str, tuple[int, int]] = {
    "pre_settle": (10, 4),
    "pre_close": (10, 4),
    "open": (10, 4),
    "high": (10, 4),
    "low": (10, 4),
    "close": (10, 4),
    "settle": (10, 4),
    "vol": (20, 4),
    "amount": (24, 6),
    "oi": (20, 4),
}

INSERT_COLS = [
    "ts_code",
    "trade_date",
    "exchange",
    "pre_settle",
    "pre_close",
    "open",
    "high",
    "low",
    "close",
    "settle",
    "vol",
    "amount",
    "oi",
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
  `trade_date` date NOT NULL COMMENT '交易日期',
  `exchange` varchar(16) NOT NULL COMMENT '交易市场',
  `pre_settle` decimal(10,4) DEFAULT NULL COMMENT '昨结算价',
  `pre_close` decimal(10,4) DEFAULT NULL COMMENT '前收盘价',
  `open` decimal(10,4) DEFAULT NULL COMMENT '开盘价',
  `high` decimal(10,4) DEFAULT NULL COMMENT '最高价',
  `low` decimal(10,4) DEFAULT NULL COMMENT '最低价',
  `close` decimal(10,4) DEFAULT NULL COMMENT '收盘价',
  `settle` decimal(10,4) DEFAULT NULL COMMENT '结算价',
  `vol` decimal(20,4) DEFAULT NULL COMMENT '成交量(手)',
  `amount` decimal(24,6) DEFAULT NULL COMMENT '成交金额(万元)',
  `oi` decimal(20,4) DEFAULT NULL COMMENT '持仓量(手)',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ts_trade` (`ts_code`,`trade_date`),
  KEY `idx_trade_date` (`trade_date`),
  KEY `idx_ex_td` (`exchange`,`trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='期权日线行情 opt_daily';
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


def fetchSseOpenDates(pro: Any, start_yyyymmdd: str, end_yyyymmdd: str) -> list[str]:
    """SSE 交易日历 is_open=1，返回排序后的 YYYYMMDD 列表。"""
    df = retryRequest(
        pro.trade_cal,
        exchange="SSE",
        start_date=start_yyyymmdd,
        end_date=end_yyyymmdd,
        is_open="1",
    )
    if df is None or df.empty:
        return []
    s = df["cal_date"].astype(str).str.replace(r"\D", "", regex=True)
    out = sorted({x for x in s.tolist() if len(x) == 8})
    return out


def fetchOptDailyPage(
    pro: Any, *, exchange: str, trade_date: str, offset: int
) -> pd.DataFrame:
    time.sleep(REQUEST_INTERVAL)
    df = pro.opt_daily(
        exchange=exchange,
        trade_date=trade_date,
        fields=FIELDS,
        limit=PAGE_LIMIT,
        offset=offset,
    )
    return df if df is not None else pd.DataFrame()


def fetchOneDayExchange(pro: Any, exchange: str, trade_yyyymmdd: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    offset = 0
    while running:
        page = retryRequest(
            fetchOptDailyPage,
            pro=pro,
            exchange=exchange,
            trade_date=trade_yyyymmdd,
            offset=offset,
        )
        if page.empty:
            break
        frames.append(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        logger.info(
            "[%s %s] 分页 offset=%s 本页 %s 行",
            trade_yyyymmdd,
            exchange,
            offset,
            len(page),
        )
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


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
    if "vol" in out.columns:
        out["vol"] = pd.to_numeric(out["vol"], errors="coerce").round(4)
    if "oi" in out.columns:
        out["oi"] = pd.to_numeric(out["oi"], errors="coerce").round(4)
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
        max_abs = Decimal(10) ** (mi - di) - Decimal(10) ** (-di)
        q = Decimal(10) ** (-di)
        for idx, val in df[col].items():
            if val is None or (isinstance(val, float) and np.isnan(val)):
                continue
            try:
                dec = Decimal(str(val)).quantize(q)
            except (InvalidOperation, ValueError):
                logger.error("decimal 非法 列=%s ts_code=%s", col, df.at[idx, "ts_code"])
                raise
            if abs(dec) > max_abs:
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
    for col, max_len in VARCHAR_MAX_LEN.items():
        if col not in df.columns:
            continue
        for idx, val in df[col].items():
            if val is None:
                continue
            s = str(val)
            if len(s) > max_len:
                logger.error(
                    "字符串超长 列=%s ts_code=%s len=%s 上限=%s",
                    col,
                    df.at[idx, "ts_code"],
                    len(s),
                    max_len,
                )
                raise ValueError("varchar too long")


def saveData(engine: Engine, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    df2 = _processDf(df)
    validate_before_save(df2)
    cols = [c for c in INSERT_COLS if c in df2.columns]
    placeholders = ", ".join([f":{c}" for c in cols])
    update_parts = [f"`{c}`=VALUES(`{c}`)" for c in cols if c not in ("ts_code", "trade_date")]
    update_parts.append("`updated_at`=CURRENT_TIMESTAMP")
    sql = (
        f"INSERT INTO `{TABLE_NAME}` (`{'`, `'.join(cols)}`) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE " + ", ".join(update_parts)
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
    engine: Engine, trade_yyyymmdd: str, exchange: str, keep_ts_codes: set[str]
) -> int:
    y = f"{trade_yyyymmdd[:4]}-{trade_yyyymmdd[4:6]}-{trade_yyyymmdd[6:8]}"
    with engine.connect() as conn:
        r = conn.execute(
            text(
                f"SELECT `ts_code` FROM `{TABLE_NAME}` WHERE `trade_date` = :td AND `exchange` = :ex"
            ),
            {"td": y, "ex": exchange},
        )
        db_codes = {str(row[0]) for row in r.fetchall()}
    stale = list(db_codes - keep_ts_codes)
    if not stale:
        return 0
    deleted = 0
    chunk = 500
    with engine.begin() as conn:
        for i in range(0, len(stale), chunk):
            part = stale[i : i + chunk]
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


def maxTradeDateYyyymmdd(engine: Engine) -> str | None:
    with engine.connect() as conn:
        r = conn.execute(text(f"SELECT MAX(`trade_date`) AS m FROM `{TABLE_NAME}`"))
        row = r.fetchone()
    if not row or row[0] is None:
        return None
    d = row[0]
    if hasattr(d, "strftime"):
        return d.strftime("%Y%m%d")
    s = str(d).replace("-", "")[:8]
    return s if len(s) == 8 else None


def countDayExchange(engine: Engine, trade_yyyymmdd: str, exchange: str) -> int:
    y = f"{trade_yyyymmdd[:4]}-{trade_yyyymmdd[4:6]}-{trade_yyyymmdd[6:8]}"
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
    pro: Any, engine: Engine, trade_yyyymmdd: str, exchange: str, *, do_write: bool
) -> int:
    raw = fetchOneDayExchange(pro, exchange, trade_yyyymmdd)
    if raw.empty:
        return 0
    api_codes = set(raw["ts_code"].astype(str).str.strip())
    if not do_write:
        return len(raw)
    n = saveData(engine, raw)
    pruned = pruneStaleForDayExchange(engine, trade_yyyymmdd, exchange, api_codes)
    if pruned:
        logger.info("[%s %s] 修剪孤儿 %s 条", trade_yyyymmdd, exchange, pruned)
    return n


def tradeDatesForIncremental(pro: Any, max_d: str) -> list[str]:
    """新交易日 + 最近 INCREMENTAL_LOOKBACK_TRADING_DAYS 个交易日（含 max_d）。"""
    today = dt.date.today().strftime("%Y%m%d")
    all_open = fetchSseOpenDates(pro, FULL_START_DATE, today)
    if not all_open:
        return []
    future = [d for d in all_open if d > max_d]
    try:
        idx = max(i for i, d in enumerate(all_open) if d <= max_d)
    except ValueError:
        return future
    lb = max(0, idx - (INCREMENTAL_LOOKBACK_TRADING_DAYS - 1))
    window = all_open[lb : idx + 1]
    return sorted(set(window + future))


def importDatesExchanges(
    pro: Any,
    engine: Engine,
    dates: list[str],
    exchanges: tuple[str, ...],
    *,
    do_write: bool,
    label: str,
) -> int:
    total = 0
    n_slots = len(dates) * len(exchanges)
    k = 0
    for td in dates:
        if not running:
            break
        for ex in exchanges:
            if not running:
                break
            k += 1
            n = syncOneDayExchange(pro, engine, td, ex, do_write=do_write)
            total += n
            if k % 50 == 0 or n > 0:
                logger.info("[%s] [%s/%s] %s %s 写入约 %s 行", label, k, n_slots, td, ex, n)
    return total


def consistencyCheck(
    pro: Any, engine: Engine, exchanges: tuple[str, ...]
) -> list[tuple[str, str]]:
    """以本地最新 trade_date 为基准，按所比对条数与抽样数值。"""
    bad_set: set[tuple[str, str]] = set()
    max_d = maxTradeDateYyyymmdd(engine)
    if not max_d:
        return []
    y = f"{max_d[:4]}-{max_d[4:6]}-{max_d[6:8]}"
    for ex in exchanges:
        if not running:
            break
        api_df = fetchOneDayExchange(pro, ex, max_d)
        api_n = len(api_df)
        db_n = countDayExchange(engine, max_d, ex)
        if api_n != db_n:
            logger.error(
                "不一致 %s %s：API=%s 本地=%s",
                max_d,
                ex,
                api_n,
                db_n,
            )
            bad_set.add((max_d, ex))
            continue
        if api_df.empty:
            continue
        sample = api_df.head(min(30, len(api_df)))
        row_bad = False
        with engine.connect() as conn:
            for _, row in sample.iterrows():
                ts_code = str(row["ts_code"]).strip()
                r = conn.execute(
                    text(
                        f"SELECT close, vol, amount, oi FROM `{TABLE_NAME}` "
                        f"WHERE ts_code=:c AND trade_date=:td LIMIT 1"
                    ),
                    {"c": ts_code, "td": y},
                )
                db_row = r.mappings().first()
                if not db_row:
                    row_bad = True
                    break
                for col in CHECK_NUM_COLS:
                    av = row.get(col)
                    dv = db_row.get(col)
                    if av is None or dv is None:
                        continue
                    try:
                        a = float(av)
                        d = float(dv)
                    except (TypeError, ValueError):
                        continue
                    denom = abs(a) + 1e-8
                    if abs(a - d) / denom > DATA_TOLERANCE:
                        logger.error(
                            "数值不一致 %s %s %s API=%s DB=%s",
                            max_d,
                            ts_code,
                            col,
                            a,
                            d,
                        )
                        row_bad = True
                        break
                if row_bad:
                    break
        if row_bad:
            bad_set.add((max_d, ex))
        else:
            logger.info("一致性通过 %s %s 条数=%s", max_d, ex, api_n)
    return sorted(bad_set)


def repairPairs(pro: Any, engine: Engine, pairs: list[tuple[str, str]]) -> None:
    for td, ex in pairs:
        if not running:
            break
        logger.warning("修复同步 %s %s", td, ex)
        syncOneDayExchange(pro, engine, td, ex, do_write=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="opt_daily 期权日线入库")
    parser.add_argument("--mode", choices=("auto", "full", "incremental"), default="auto")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--exchange", default="", help="仅单所，如 DCE")
    parser.add_argument("--skip-check", action="store_true")
    args = parser.parse_args()

    exchanges: tuple[str, ...] = (
        (args.exchange.strip().upper(),)
        if args.exchange.strip()
        else DEFAULT_EXCHANGES
    )

    engine = makeEngine()
    pro = ts.pro_api(TUSHARE_TOKEN)

    if args.check_only:
        bad = consistencyCheck(pro, engine, exchanges)
        if bad:
            logger.error("校验未通过：%s", bad)
            sys.exit(1)
        logger.info("校验通过")
        return

    createTable(engine)
    today = dt.date.today().strftime("%Y%m%d")
    is_empty = tableRowCount(engine) == 0

    if args.mode == "full" or is_empty:
        if is_empty:
            logger.info("表为空，执行全量（SSE 交易日 × 所选交易所）")
        else:
            logger.info("--mode full：全量重拉 SSE 交易日 × 所选交易所")
        dates = fetchSseOpenDates(pro, FULL_START_DATE, today)
        if not dates:
            logger.error("交易日历为空，终止")
            sys.exit(1)
        importDatesExchanges(pro, engine, dates, exchanges, do_write=True, label="全量")
    elif args.mode in ("auto", "incremental"):
        max_d = maxTradeDateYyyymmdd(engine)
        if not max_d:
            dates = fetchSseOpenDates(pro, FULL_START_DATE, today)
        else:
            dates = tradeDatesForIncremental(pro, max_d)
        if not dates:
            logger.info("无待同步交易日")
        else:
            logger.info(
                "增量：共 %s 个交易日（含回看最近 %s 个交易日）",
                len(dates),
                INCREMENTAL_LOOKBACK_TRADING_DAYS,
            )
            importDatesExchanges(pro, engine, dates, exchanges, do_write=True, label="增量")
    else:
        logger.warning("未知 mode=%s，跳过阶段1", args.mode)

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

# 代码更新时间：2026-05-11 19:43
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
