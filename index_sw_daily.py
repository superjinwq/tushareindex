#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口：sw_daily（申万行业日线行情，默认申万 2021 版）
描述：按交易日拉取全市场行业指数日线，入库 stock_tushare.index_sw_daily。
文档：https://tushare.pro/document/2?doc_id=327

更新方式：
  - 表空或 --full：从 FULL_START_DATE 起按交易日全量 UPSERT（可选 --truncate 先清空表）
  - 否则：从本地 MAX(trade_date) 次一交易日起增量 UPSERT

三阶段（默认同次运行，可用 --no-phase23 关闭）：
  1）按交易日批量拉取写入
  2）最近 SAMPLE_CHECK_DATES 个交易日：本地 vs API 向量化对比
  3）对不一致的 ts_code 按代码 + 日期分段重拉全历史并 UPSERT
"""

from __future__ import annotations
import os

import argparse
import datetime as dt
import logging
import signal
import sys
import time
from decimal import ROUND_HALF_UP, Decimal
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Callable

import numpy as np
import pandas as pd
import tushare as ts
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from urllib.parse import quote_plus


from windows_safe_log import WindowsSafeTimedRotatingFileHandler


running = True


def handleSignal(signum: int, frame: Any) -> None:
    global running
    running = False
    logging.warning("收到信号 %s，准备安全退出...", signum)


signal.signal(signal.SIGINT, handleSignal)
signal.signal(signal.SIGTERM, handleSignal)

# ==================== 配置（按用户要求硬编码） ====================
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "").strip()
if not TUSHARE_TOKEN:
    raise SystemExit("缺少环境变量 TUSHARE_TOKEN")

DB_CONFIG = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": os.getenv("DB_PASSWORD", "").strip(),
    "database": "stock_tushare",
    "charset": "utf8mb4",
}

TABLE_NAME = "index_sw_daily"
LOG_FILE = "index_sw_daily.log"
REQUEST_INTERVAL = 0.2
BATCH_SIZE = 500
PAGE_LIMIT = 4000
FULL_START_DATE = "20100101"
FULL_UPSERT_BATCH_DAYS = 20
SAMPLE_CHECK_DATES = 10
DATA_TOLERANCE = 0.001
RETRY_SLEEP = (2, 4, 8)

# 显式字段（规则 B）
FIELDS = (
    "ts_code,trade_date,name,open,low,high,close,change,pct_change,vol,amount,pe,pb,float_mv,total_mv"
)

# 阶段 2 对比用数值列（与 API 字段一致）
CHECK_NUMERIC_COLS = ["open", "high", "low", "close", "vol", "amount"]

VARCHAR_MAX_LEN = {"ts_code": 20, "name": 128}

DECIMAL_SPECS: dict[str, tuple[int, int]] = {
    "open": (10, 4),
    "high": (10, 4),
    "low": (10, 4),
    "close": (10, 4),
    "change": (14, 4),
    "pct_change": (12, 6),
    "vol": (24, 4),
    "amount": (24, 4),
    "pe": (16, 6),
    "pb": (16, 6),
    "float_mv": (24, 4),
    "total_mv": (24, 4),
}

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `ts_code` varchar(20) NOT NULL COMMENT '申万行业指数代码',
  `trade_date` date NOT NULL COMMENT '交易日期',
  `name` varchar(128) DEFAULT NULL COMMENT '指数名称',
  `open` decimal(10,4) DEFAULT NULL COMMENT '开盘点位',
  `low` decimal(10,4) DEFAULT NULL COMMENT '最低点位',
  `high` decimal(10,4) DEFAULT NULL COMMENT '最高点位',
  `close` decimal(10,4) DEFAULT NULL COMMENT '收盘点位',
  `change` decimal(14,4) DEFAULT NULL COMMENT '涨跌点位',
  `pct_change` decimal(12,6) DEFAULT NULL COMMENT '涨跌幅',
  `vol` decimal(24,4) DEFAULT NULL COMMENT '成交量（万股）',
  `amount` decimal(24,4) DEFAULT NULL COMMENT '成交额（万元）',
  `pe` decimal(16,6) DEFAULT NULL COMMENT '市盈率',
  `pb` decimal(16,6) DEFAULT NULL COMMENT '市净率',
  `float_mv` decimal(24,4) DEFAULT NULL COMMENT '流通市值（万元）',
  `total_mv` decimal(24,4) DEFAULT NULL COMMENT '总市值（万元）',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ts_trade` (`ts_code`,`trade_date`),
  KEY `idx_trade_date` (`trade_date`),
  KEY `idx_ts_trade` (`ts_code`,`trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='申万行业日线行情（sw_daily）';
""".strip()

OUTPUT_COLUMNS = [
    "ts_code",
    "trade_date",
    "name",
    "open",
    "low",
    "high",
    "close",
    "change",
    "pct_change",
    "vol",
    "amount",
    "pe",
    "pb",
    "float_mv",
    "total_mv",
]


def setupLogger() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = WindowsSafeTimedRotatingFileHandler(
        LOG_FILE, when="D", interval=1, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, ch], force=True)


def makeEngine() -> Engine:
    dsn = (
        f"mysql+pymysql://{quote_plus(DB_CONFIG['user'])}:{quote_plus(DB_CONFIG['password'])}"
        f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}?charset={DB_CONFIG['charset']}"
    )
    return create_engine(dsn, pool_recycle=3600, pool_pre_ping=True, pool_size=5, max_overflow=10)


def createTable(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(CREATE_TABLE_SQL))
    logging.info("数据表 %s 已就绪", TABLE_NAME)


def retryRequest(callFn: Callable[..., pd.DataFrame], **kwargs: Any) -> pd.DataFrame:
    for attempt, wait_sec in enumerate(RETRY_SLEEP, start=1):
        if not running:
            return pd.DataFrame()
        try:
            time.sleep(REQUEST_INTERVAL)
            return callFn(**kwargs)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if attempt == len(RETRY_SLEEP):
                logging.error("[FAILED] sw_daily 参数=%s 错误=%s", kwargs, exc)
                return pd.DataFrame()
            logging.warning("请求失败，第 %s 次重试前等待 %ss：%s", attempt, wait_sec, exc)
            time.sleep(wait_sec)
    return pd.DataFrame()


def fetchSwDailyPaged(pro: Any, **api_kwargs: Any) -> pd.DataFrame:
    """分页拉取 sw_daily，防止单次 4000 行截断。"""
    frames: list[pd.DataFrame] = []
    offset = 0
    while running:
        df = retryRequest(
            pro.sw_daily,
            fields=FIELDS,
            limit=PAGE_LIMIT,
            offset=offset,
            **api_kwargs,
        )
        if df is None or df.empty:
            break
        frames.append(df)
        if len(df) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def getTradeCal(pro: Any, startYmd: str, endYmd: str) -> list[str]:
    """返回区间内开市日 cal_date 列表（YYYYMMDD），升序。"""
    df = retryRequest(
        pro.trade_cal,
        exchange="SSE",
        start_date=startYmd,
        end_date=endYmd,
        is_open="1",
    )
    if df is None or df.empty:
        return []
    if "is_open" in df.columns:
        df = df[df["is_open"] == 1]
    dates = sorted(df["cal_date"].astype(str).tolist())
    return dates


def processDf(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    if "trade_date" in out.columns:
        out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce")
        out["trade_date"] = out["trade_date"].dt.strftime("%Y-%m-%d")
    for col in ("open", "high", "low", "close"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").round(4)
    for col in ("change", "pct_change", "vol", "amount", "pe", "pb", "float_mv", "total_mv"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    if "name" in out.columns:
        out["name"] = out["name"].astype(str).str.strip()
        out.loc[out["name"].isin(("", "nan", "None")), "name"] = None
    out = out[[c for c in OUTPUT_COLUMNS if c in out.columns]]
    out = out.replace({np.nan: None})
    out = out.where(pd.notna(out), None)
    return out


def _decimalFits(value: Any, m: int, d: int) -> bool:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return True
    try:
        dec = Decimal(str(float(value))).quantize(Decimal(10) ** -d, rounding=ROUND_HALF_UP)
        tup = dec.as_tuple()
        digits = tup.digits
        exp = tup.exponent
        if exp < 0:
            int_len = max(len(digits) + exp, 1)
            frac_len = -exp
        else:
            int_len = len(digits) + exp
            frac_len = 0
        return int_len + frac_len <= m and frac_len <= d
    except Exception:
        return False


def validateBeforeSave(df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    for col, nmax in VARCHAR_MAX_LEN.items():
        if col not in df.columns:
            continue
        for i, v in enumerate(df[col].tolist()):
            if v is None:
                continue
            s = str(v)
            if len(s) > nmax:
                logging.error(
                    "validate_before_save: 列 %s 超长 len=%s 上限=%s 行索引=%s 样本=%s",
                    col,
                    len(s),
                    nmax,
                    i,
                    s[:80],
                )
                raise ValueError(f"列 {col} 超过 varchar({nmax})")
    for col, spec in DECIMAL_SPECS.items():
        if col not in df.columns:
            continue
        m, d = spec
        for i, v in enumerate(df[col].tolist()):
            if v is None:
                continue
            if not _decimalFits(v, m, d):
                logging.error(
                    "validate_before_save: 列 %s 数值超 decimal(%s,%s) 行索引=%s 值=%s",
                    col,
                    m,
                    d,
                    i,
                    v,
                )
                raise ValueError(f"列 {col} 超出 decimal({m},{d})")


def saveData(engine: Engine, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    validateBeforeSave(df)
    cols = [c for c in OUTPUT_COLUMNS if c in df.columns]
    data = df[cols].copy()
    col_names = ", ".join(f"`{c}`" for c in cols)
    placeholders = ", ".join(f":{c}" for c in cols)
    update_cols = [c for c in cols if c not in ("ts_code", "trade_date")]
    update_part = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in update_cols)
    sql = f"""
        INSERT INTO `{TABLE_NAME}` ({col_names})
        VALUES ({placeholders})
        ON DUPLICATE KEY UPDATE {update_part}
    """
    records = data.to_dict("records")
    total = 0
    with engine.begin() as conn:
        for start in range(0, len(records), BATCH_SIZE):
            if not running:
                break
            batch = records[start : start + BATCH_SIZE]
            conn.execute(text(sql), batch)
            total += len(batch)
    return total


def getRowCount(engine: Engine) -> int:
    with engine.connect() as conn:
        n = conn.execute(text(f"SELECT COUNT(*) FROM `{TABLE_NAME}`")).scalar()
    return int(n or 0)


def getMaxTradeDate(engine: Engine) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(text(f"SELECT MAX(trade_date) AS d FROM `{TABLE_NAME}`")).mappings().first()
    if not row or row["d"] is None:
        return None
    d = row["d"]
    if hasattr(d, "strftime"):
        return d.strftime("%Y-%m-%d")
    return str(d)[:10]


def getRecentNDates(engine: Engine, n: int) -> list[str]:
    q = text(
        f"SELECT DISTINCT trade_date FROM `{TABLE_NAME}` ORDER BY trade_date DESC LIMIT :lim"
    )
    with engine.connect() as conn:
        df = pd.read_sql(q, conn, params={"lim": n})
    if df.empty:
        return []
    dates = [pd.to_datetime(x).strftime("%Y-%m-%d") for x in df["trade_date"].tolist()]
    return sorted(dates)


def getDataByDates(
    engine: Engine, startYmdDash: str, endYmdDash: str, fields: list[str]
) -> pd.DataFrame:
    cols = ", ".join(f"`{c}`" for c in fields)
    q = text(
        f"SELECT {cols} FROM `{TABLE_NAME}` "
        "WHERE trade_date BETWEEN :s AND :e ORDER BY trade_date, ts_code"
    )
    with engine.connect() as conn:
        df = pd.read_sql(q, conn, params={"s": startYmdDash, "e": endYmdDash})
    if df.empty:
        return df
    df.columns = [str(c).lower() for c in df.columns]
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return df


def normalizeFrame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    if "trade_date" in out.columns:
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return out


def batchCompare(
    df_local: pd.DataFrame, df_api: pd.DataFrame, numeric_cols: list[str]
) -> list[str]:
    """返回不一致的 ts_code 列表。"""
    df_local = normalizeFrame(df_local)
    df_api = normalizeFrame(df_api)
    if df_local.empty or df_api.empty:
        return []
    inconsistent: set[str] = set()

    local_top = (
        df_local.sort_values(["ts_code", "trade_date"], ascending=[True, False])
        .groupby("ts_code", group_keys=False, sort=False)
        .head(SAMPLE_CHECK_DATES)
    )
    api_top = (
        df_api.sort_values(["ts_code", "trade_date"], ascending=[True, False])
        .groupby("ts_code", group_keys=False, sort=False)
        .head(SAMPLE_CHECK_DATES)
    )

    local_cnt = local_top.groupby("ts_code").size()
    api_cnt = api_top.groupby("ts_code").size()
    all_codes = local_cnt.index.union(api_cnt.index)
    cnt_mismatch = (
        local_cnt.reindex(all_codes, fill_value=0) != api_cnt.reindex(all_codes, fill_value=0)
    )
    inconsistent.update(cnt_mismatch[cnt_mismatch].index.tolist())

    merged = local_top.merge(
        api_top,
        on=["ts_code", "trade_date"],
        how="inner",
        suffixes=("_local", "_api"),
    )
    if merged.empty:
        inconsistent.update(all_codes.tolist())
        return list(inconsistent)

    merged_cnt = merged.groupby("ts_code").size()
    for code in all_codes:
        lc, ac = local_cnt.get(code, 0), api_cnt.get(code, 0)
        min_exp = min(lc, ac) if (lc > 0 and ac > 0) else 0
        if min_exp > 0 and merged_cnt.get(code, 0) < min_exp:
            inconsistent.add(str(code))

    for field in numeric_cols:
        a, b = f"{field}_local", f"{field}_api"
        if a not in merged.columns or b not in merged.columns:
            continue
        locv = pd.to_numeric(merged[a], errors="coerce")
        apiv = pd.to_numeric(merged[b], errors="coerce")
        valid = locv.notna() & apiv.notna()
        if not valid.any():
            continue
        sub = merged.loc[valid].copy()
        sub["_locv"] = locv[valid]
        sub["_apiv"] = apiv[valid]
        denom = np.maximum(sub["_apiv"].abs(), 1e-8)
        sub["_diff_pct"] = (sub["_locv"] - sub["_apiv"]).abs() / denom
        bad = sub.groupby("ts_code", group_keys=False)["_diff_pct"].max()
        inconsistent.update(bad[bad > DATA_TOLERANCE].index.astype(str).tolist())

    return sorted(inconsistent)


def runPhase1ByDates(
    pro: Any, engine: Engine, trade_dates: list[str], label: str
) -> tuple[int, list[str]]:
    """按交易日列表拉取并分批写入。"""
    if not trade_dates:
        return 0, []
    total_written = 0
    buf: list[pd.DataFrame] = []
    imported_dates: list[str] = []
    for i, ymd in enumerate(trade_dates):
        if not running:
            break
        raw = fetchSwDailyPaged(pro, trade_date=ymd)
        if raw is not None and not raw.empty:
            buf.append(raw)
            imported_dates.append(ymd)
        if len(buf) >= FULL_UPSERT_BATCH_DAYS or (i == len(trade_dates) - 1 and buf):
            merged = pd.concat(buf, ignore_index=True)
            merged = processDf(merged)
            if not merged.empty:
                n = saveData(engine, merged)
                total_written += n
                logging.info("[%s] 已写入 %s 条（截至日历日 %s）", label, total_written, ymd)
            buf = []
    return total_written, imported_dates


def runIncrementalOrFull(
    pro: Any,
    engine: Engine,
    *,
    forceFull: bool,
    truncateFirst: bool,
) -> tuple[int, list[str]]:
    today = dt.date.today().strftime("%Y%m%d")
    if truncateFirst and forceFull:
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE `{TABLE_NAME}`"))
        logging.info("已 TRUNCATE 表 %s", TABLE_NAME)

    if forceFull:
        start_cal = FULL_START_DATE
        logging.info("全量模式：%s ~ %s", start_cal, today)
    else:
        local_max = getMaxTradeDate(engine)
        if not local_max:
            start_cal = FULL_START_DATE
            logging.info("本地无数据，从 %s 全量", start_cal)
        else:
            next_d = (pd.to_datetime(local_max) + pd.Timedelta(days=1)).strftime("%Y%m%d")
            if next_d > today:
                logging.info("本地已最新（%s），阶段1 无增量", local_max)
                return 0, []
            start_cal = next_d
            logging.info("增量：%s ~ %s", start_cal, today)

    trade_dates = getTradeCal(pro, start_cal, today)
    if not trade_dates:
        logging.warning("交易日历为空，跳过阶段1")
        return 0, []
    label = "全量" if forceFull else "增量"
    return runPhase1ByDates(pro, engine, trade_dates, label)


def runPhase2Phase3(pro: Any, engine: Engine) -> None:
    logging.info("【阶段2】批量数据校验（最近 %s 个交易日）", SAMPLE_CHECK_DATES)
    recent = getRecentNDates(engine, SAMPLE_CHECK_DATES)
    if len(recent) < 2:
        logging.warning("本地交易日不足，跳过校验")
        return
    start_d, end_d = recent[0], recent[-1]
    check_fields = ["ts_code", "trade_date"] + CHECK_NUMERIC_COLS
    df_local = getDataByDates(engine, start_d, end_d, check_fields)
    if df_local.empty:
        logging.warning("本地校验区间无数据")
        return

    api_parts: list[pd.DataFrame] = []
    for d in recent:
        if not running:
            break
        ymd = pd.to_datetime(d).strftime("%Y%m%d")
        raw = fetchSwDailyPaged(pro, trade_date=ymd)
        if raw is not None and not raw.empty:
            api_parts.append(raw)
    if not api_parts:
        logging.error("API 校验数据为空")
        return
    df_api = processDf(pd.concat(api_parts, ignore_index=True))
    df_api = df_api[[c for c in check_fields if c in df_api.columns]]

    inconsistent = batchCompare(df_local, df_api, CHECK_NUMERIC_COLS)
    logging.info("校验完成：不一致行业代码数 = %s", len(inconsistent))
    if inconsistent[:30]:
        logging.info("不一致示例（前30）：%s", inconsistent[:30])

    if not inconsistent:
        return

    logging.info("【阶段3】对 %s 个 ts_code 分段全量重拉", len(inconsistent))
    for j, code in enumerate(inconsistent, start=1):
        if not running:
            break
        n = fullUpdateOneTsCode(pro, engine, code)
        logging.info("阶段3 进度 [%s/%s] %s 写入=%s", j, len(inconsistent), code, n)


def fullUpdateOneTsCode(pro: Any, engine: Engine, tsCode: str) -> int:
    """单行业指数从 FULL_START_DATE 至今日分段拉取并 UPSERT。"""
    end_ymd = dt.date.today().strftime("%Y%m%d")
    cur = dt.datetime.strptime(FULL_START_DATE, "%Y%m%d")
    end_dt = dt.datetime.strptime(end_ymd, "%Y%m%d")
    total = 0
    segment_days = 800
    while cur <= end_dt and running:
        seg_end = min(cur + dt.timedelta(days=segment_days - 1), end_dt)
        s1 = cur.strftime("%Y%m%d")
        s2 = seg_end.strftime("%Y%m%d")
        raw = fetchSwDailyPaged(pro, ts_code=tsCode, start_date=s1, end_date=s2)
        if raw is not None and not raw.empty:
            merged = processDf(raw)
            if not merged.empty:
                total += saveData(engine, merged)
        cur = seg_end + dt.timedelta(days=1)
    return total


def parseArgs() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="申万行业日线 sw_daily 入库")
    p.add_argument(
        "--full",
        action="store_true",
        help=f"从 {FULL_START_DATE} 起按交易日全量 UPSERT（不依赖表是否为空）",
    )
    p.add_argument(
        "--truncate",
        action="store_true",
        help="与 --full 合用：导入前 TRUNCATE 表",
    )
    p.add_argument(
        "--no-phase23",
        action="store_true",
        help="跳过阶段2/3 一致性校验与修复",
    )
    return p.parse_args()


def main() -> None:
    setupLogger()
    args = parseArgs()
    engine = makeEngine()
    createTable(engine)

    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()

    row_count = getRowCount(engine)
    force_full = bool(args.full) or row_count == 0
    if args.truncate and not force_full:
        logging.warning("--truncate 已忽略（需配合全量：表空自动全量或加 --full）")

    n1, dates1 = runIncrementalOrFull(
        pro,
        engine,
        forceFull=force_full,
        truncateFirst=bool(args.truncate and force_full),
    )
    logging.info("【阶段1】完成，写入约 %s 条，覆盖交易日数=%s", n1, len(dates1))

    if args.no_phase23:
        logging.info("已跳过阶段2/3（--no-phase23）")
        engine.dispose()
        return

    if row_count == 0 and n1 == 0:
        logging.warning("表仍为空且阶段1 无写入，跳过阶段2/3")
        engine.dispose()
        return

    runPhase2Phase3(pro, engine)
    engine.dispose()
    logging.info("全部流程结束")


if __name__ == "__main__":
    main()

# 代码更新时间：2026-05-14 13:50
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
