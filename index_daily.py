#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口：index_daily（指数日线行情）
描述：拉取指定指数全历史日线，入库 stock_tushare.index_daily，支持每日增量，并计算振幅。
更新方式：首次全量 + 后续增量（UPSERT）。
"""

from __future__ import annotations
import os

import argparse
import datetime as dt
import logging
import signal
import sys
import time
from decimal import Decimal, InvalidOperation
from logging.handlers import TimedRotatingFileHandler
from typing import Any
from urllib.parse import quote_plus

import numpy as np
import pandas as pd
import tushare as ts
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


from windows_safe_log import WindowsSafeTimedRotatingFileHandler


running = True


def handleSignal(signum: int, frame: Any) -> None:
    """收到中断信号后，让主流程尽快退出。"""
    global running
    running = False
    logging.warning("收到信号 %s，准备安全退出...", signum)


signal.signal(signal.SIGINT, handleSignal)
signal.signal(signal.SIGTERM, handleSignal)


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

TABLE_NAME = "index_daily"
LOG_FILE = "index_daily.log"
REQUEST_INTERVAL = 0.2
BATCH_SIZE = 500
PAGE_LIMIT = 8000
FULL_START_DATE = "19900101"
WINDOW_YEARS = 8
PER_WINDOW_SIZE = 1250
RETRY_SECONDS = (2, 4, 8)
FIELDS = "ts_code,trade_date,close,open,high,low,pre_close,change,pct_chg,vol,amount"

INDEX_CODE_NAME_MAP: dict[str, str] = {
    "000510.SH": "A500",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "932000.CSI": "中证2000",
    "899050.BJ": "北证50",
    "000016.SH": "上证50",
    "000680.SH": "科创综指",
    "000688.SH": "科创50",
    "399006.SZ": "创业板",
    "399102.SZ": "创业板综",
    "399317.SZ": "国证A指",
    "000001.SH": "上证指数",
    "399107.SZ": "深圳A指",
}

DEFAULT_FLOAT_SPECS: dict[str, tuple[int, int]] = {
    "close": (20, 6),
    "open": (20, 6),
    "high": (20, 6),
    "low": (20, 6),
    "pre_close": (20, 6),
    "change": (20, 6),
    "pct_chg": (12, 6),
    "vol": (20, 2),
    "amount": (20, 2),
    "amplitude": (12, 6),
    "amp_per": (6, 2),
    "amount_per": (6, 2),
}


def setupLogger() -> None:
    """初始化日志：按天轮转，保留 3 天，文件名与脚本同名。"""
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = WindowsSafeTimedRotatingFileHandler(
        LOG_FILE,
        when="D",
        interval=1,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler], force=True)


def makeEngine() -> Engine:
    """创建 SQLAlchemy Engine。"""
    dsn = (
        f"mysql+pymysql://{quote_plus(DB_CONFIG['user'])}:{quote_plus(DB_CONFIG['password'])}"
        f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}?charset={DB_CONFIG['charset']}"
    )
    return create_engine(dsn, pool_recycle=3600, pool_pre_ping=True, pool_size=5, max_overflow=10)


def decimalSpecFromSeries(series: pd.Series, safeIntExtra: int = 3, maxDec: int = 6) -> tuple[int, int]:
    """基于样本推断 decimal(M,D)，降低存储浪费。"""
    non_null = series.dropna()
    if non_null.empty:
        return 12, 4
    max_int_digits = 1
    max_dec_digits = 0
    for value in non_null:
        try:
            dec = Decimal(str(float(value)))
            _sign, digits, exponent = dec.as_tuple()
            total = len(digits)
            if exponent < 0:
                dec_part = abs(exponent)
                int_part = max(total - dec_part, 1)
            else:
                dec_part = 0
                int_part = total + exponent
            max_int_digits = max(max_int_digits, int_part)
            max_dec_digits = max(max_dec_digits, dec_part)
        except Exception:
            continue
    safe_int = max_int_digits + safeIntExtra
    safe_dec = min(max(max_dec_digits, 2), maxDec)
    return safe_int + safe_dec, safe_dec


def inferFloatSpecs(sample_df: pd.DataFrame) -> dict[str, tuple[int, int]]:
    """推断目标字段的 decimal 精度。"""
    specs = dict(DEFAULT_FLOAT_SPECS)
    for col in ("close", "open", "high", "low", "pre_close", "change", "pct_chg", "vol", "amount"):
        if col in sample_df.columns:
            specs[col] = decimalSpecFromSeries(sample_df[col])
    if "amplitude" in sample_df.columns:
        specs["amplitude"] = decimalSpecFromSeries(sample_df["amplitude"])
    return specs


def calcAmplitude(row: pd.Series) -> float | None:
    """振幅 = ((high-low)/pre_close)*100，分母无效时返回 None。"""
    try:
        high = Decimal(str(row.get("high")))
        low = Decimal(str(row.get("low")))
        pre_close = Decimal(str(row.get("pre_close")))
        if pre_close <= 0:
            return None
        amplitude = ((high - low) / pre_close) * Decimal("100")
        return float(amplitude)
    except (InvalidOperation, TypeError, ValueError):
        return None


def processDf(df: pd.DataFrame, ts_code: str) -> pd.DataFrame:
    """标准化字段、计算振幅、填充 index_name。"""
    if df.empty:
        return df
    data = df.copy()
    if "trade_date" in data.columns:
        data["trade_date"] = pd.to_datetime(data["trade_date"], format="%Y%m%d", errors="coerce")
        data["trade_date"] = data["trade_date"].dt.strftime("%Y-%m-%d")
    data["index_name"] = INDEX_CODE_NAME_MAP.get(ts_code, "")
    data["amplitude"] = data.apply(calcAmplitude, axis=1)
    data = data.where(pd.notna(data), None)
    data = data.replace({np.nan: None})
    return data


def retryRequest(requestFunc, **kwargs) -> pd.DataFrame:
    """请求失败后指数退避重试。"""
    for attempt, wait_sec in enumerate(RETRY_SECONDS, start=1):
        if not running:
            return pd.DataFrame()
        try:
            return requestFunc(**kwargs)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if attempt == len(RETRY_SECONDS):
                logging.error("[FAILED] 参数=%s, 错误=%s", kwargs, exc)
                return pd.DataFrame()
            logging.warning("请求失败，第 %s 次重试前等待 %ss：%s", attempt, wait_sec, exc)
            time.sleep(wait_sec)
    return pd.DataFrame()


def fetchWithPaging(pro, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """分页拉取单指数区间数据，防止 8000 行截断。"""
    frames: list[pd.DataFrame] = []
    offset = 0
    while running:
        page = retryRequest(
            pro.query,
            api_name="index_daily",
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            fields=FIELDS,
            limit=PAGE_LIMIT,
            offset=offset,
        )
        if page.empty:
            break
        frames.append(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        time.sleep(REQUEST_INTERVAL)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def buildCreateTableSql(float_specs: dict[str, tuple[int, int]]) -> str:
    """按推断精度生成建表 SQL。"""
    return f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `ts_code` varchar(20) NOT NULL COMMENT '指数代码',
  `index_name` varchar(50) DEFAULT NULL COMMENT '指数名称',
  `trade_date` date NOT NULL COMMENT '交易日期',
  `close` decimal({float_specs['close'][0]},{float_specs['close'][1]}) DEFAULT NULL COMMENT '收盘点位',
  `open` decimal({float_specs['open'][0]},{float_specs['open'][1]}) DEFAULT NULL COMMENT '开盘点位',
  `high` decimal({float_specs['high'][0]},{float_specs['high'][1]}) DEFAULT NULL COMMENT '最高点位',
  `low` decimal({float_specs['low'][0]},{float_specs['low'][1]}) DEFAULT NULL COMMENT '最低点位',
  `pre_close` decimal({float_specs['pre_close'][0]},{float_specs['pre_close'][1]}) DEFAULT NULL COMMENT '昨日收盘点',
  `change` decimal({float_specs['change'][0]},{float_specs['change'][1]}) DEFAULT NULL COMMENT '涨跌点',
  `pct_chg` decimal({float_specs['pct_chg'][0]},{float_specs['pct_chg'][1]}) DEFAULT NULL COMMENT '涨跌幅(%)',
  `vol` decimal({float_specs['vol'][0]},{float_specs['vol'][1]}) DEFAULT NULL COMMENT '成交量(手)',
  `amount` decimal({float_specs['amount'][0]},{float_specs['amount'][1]}) DEFAULT NULL COMMENT '成交额(千元)',
  `amplitude` decimal({float_specs['amplitude'][0]},{float_specs['amplitude'][1]}) DEFAULT NULL COMMENT '振幅(%)',
  `amp_per` decimal({float_specs['amp_per'][0]},{float_specs['amp_per'][1]}) DEFAULT NULL COMMENT '振幅PER(1250周期,%)',
  `amount_per` decimal({float_specs['amount_per'][0]},{float_specs['amount_per'][1]}) DEFAULT NULL COMMENT '成交额PER(1250周期,%)',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ts_trade` (`ts_code`, `trade_date`),
  KEY `idx_trade_date` (`trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='指数日线行情';
""".strip()


def ensureExtraColumns(engine: Engine) -> None:
    """为已存在旧表补齐新增字段。"""
    alter_sql_list = [
        (
            "amp_per",
            "ALTER TABLE `index_daily` ADD COLUMN `amp_per` DECIMAL(6,2) NULL COMMENT '振幅PER(1250周期,%)' AFTER `amplitude`",
        ),
        (
            "amount_per",
            "ALTER TABLE `index_daily` ADD COLUMN `amount_per` DECIMAL(6,2) NULL COMMENT '成交额PER(1250周期,%)' AFTER `amp_per`",
        ),
    ]
    with engine.begin() as conn:
        for col_name, alter_sql in alter_sql_list:
            exists = conn.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_schema = :schema_name "
                    "AND table_name = :table_name "
                    "AND column_name = :col_name"
                ),
                {
                    "schema_name": DB_CONFIG["database"],
                    "table_name": TABLE_NAME,
                    "col_name": col_name,
                },
            ).scalar()
            if not exists:
                conn.execute(text(alter_sql))
                logging.info("已补齐字段：%s", col_name)


def createTable(engine: Engine, pro) -> None:
    """建表：先抽样推断小数精度，再执行 DDL。"""
    sample_code = next(iter(INDEX_CODE_NAME_MAP))
    sample_df = retryRequest(
        pro.query,
        api_name="index_daily",
        ts_code=sample_code,
        start_date="20240101",
        end_date=dt.date.today().strftime("%Y%m%d"),
        fields=FIELDS,
        limit=200,
        offset=0,
    )
    if not sample_df.empty:
        sample_df = processDf(sample_df, sample_code)
    float_specs = inferFloatSpecs(sample_df) if not sample_df.empty else DEFAULT_FLOAT_SPECS
    ddl = buildCreateTableSql(float_specs)
    with engine.begin() as conn:
        conn.execute(text(ddl))
    ensureExtraColumns(engine)
    logging.info("数据表 %s 已就绪。", TABLE_NAME)


def saveData(engine: Engine, df: pd.DataFrame, dry_run: bool = False) -> int:
    """批量 UPSERT。"""
    if df.empty:
        return 0
    cols = list(df.columns)
    col_names = ", ".join([f"`{c}`" for c in cols])
    placeholders = ", ".join([f":{c}" for c in cols])
    update_part = ", ".join([f"`{c}`=VALUES(`{c}`)" for c in cols if c not in ("ts_code", "trade_date")])
    sql = f"""
        INSERT INTO `{TABLE_NAME}` ({col_names})
        VALUES ({placeholders})
        ON DUPLICATE KEY UPDATE {update_part}
    """
    records = df.to_dict("records")
    if dry_run:
        return len(records)
    total = 0
    with engine.begin() as conn:
        for start in range(0, len(records), BATCH_SIZE):
            if not running:
                break
            batch = records[start : start + BATCH_SIZE]
            conn.execute(text(sql), batch)
            total += len(batch)
    return total


def rollingPerSeries(series: pd.Series, window_size: int = PER_WINDOW_SIZE) -> pd.Series:
    """计算滚动 PER 百分位（0-100），不足窗口长度时使用已有全部样本。"""

    def calc_last_percentile(window_values: pd.Series) -> float:
        if window_values.empty:
            return np.nan
        current_value = window_values.iloc[-1]
        if pd.isna(current_value):
            return np.nan
        valid_values = window_values.dropna()
        if valid_values.empty:
            return np.nan
        percentile = valid_values.rank(pct=True, method="average").iloc[-1] * 100
        return float(percentile)

    return series.rolling(window=window_size, min_periods=1).apply(calc_last_percentile, raw=False)


def recalculatePerForCode(engine: Engine, ts_code: str, dry_run: bool = False) -> int:
    """按单个指数最近 PER_WINDOW_SIZE 条记录重算 amp_per/amount_per。"""
    with engine.connect() as conn:
        df = pd.read_sql(
            text(
                f"SELECT ts_code, trade_date, amplitude, amount "
                f"FROM ("
                f"  SELECT ts_code, trade_date, amplitude, amount "
                f"  FROM `{TABLE_NAME}` "
                f"  WHERE ts_code = :ts_code "
                f"  ORDER BY trade_date DESC "
                f"  LIMIT :limit_rows"
                f") t "
                f"ORDER BY trade_date ASC"
            ),
            conn,
            params={"ts_code": ts_code, "limit_rows": PER_WINDOW_SIZE},
        )
    if df.empty:
        return 0
    df["amp_per"] = rollingPerSeries(df["amplitude"]).round(2)
    df["amount_per"] = rollingPerSeries(df["amount"]).round(2)
    df = df.where(pd.notna(df), None)
    records = df[["ts_code", "trade_date", "amp_per", "amount_per"]].to_dict("records")
    if dry_run:
        return len(records)
    sql = text(
        f"UPDATE `{TABLE_NAME}` "
        f"SET amp_per = :amp_per, amount_per = :amount_per "
        f"WHERE ts_code = :ts_code AND trade_date = :trade_date"
    )
    updated = 0
    with engine.begin() as conn:
        for start in range(0, len(records), BATCH_SIZE):
            if not running:
                break
            batch = records[start : start + BATCH_SIZE]
            conn.execute(sql, batch)
            updated += len(batch)
    return updated


def getPerNullCount(engine: Engine) -> int:
    """统计待回填的 PER 空值数量。"""
    with engine.connect() as conn:
        value = conn.execute(
            text(
                f"SELECT COUNT(*) FROM `{TABLE_NAME}` "
                f"WHERE amp_per IS NULL OR amount_per IS NULL"
            )
        ).scalar()
    return int(value or 0)


def recalculatePerForCodes(engine: Engine, ts_codes: set[str], dry_run: bool = False) -> None:
    """对给定指数集合批量重算 PER。"""
    if not ts_codes:
        return
    total = 0
    for idx, ts_code in enumerate(sorted(ts_codes), start=1):
        if not running:
            break
        count = recalculatePerForCode(engine, ts_code, dry_run=dry_run)
        total += count
        logging.info("PER重算进度 [%s/%s] %s 更新=%s", idx, len(ts_codes), ts_code, count)
    logging.info("PER重算完成，累计更新=%s", total)


def getMaxTradeDateByCode(engine: Engine, ts_code: str) -> str | None:
    """读取某指数当前最大交易日（YYYYMMDD）。"""
    with engine.connect() as conn:
        value = conn.execute(
            text(
                f"SELECT DATE_FORMAT(MAX(trade_date), '%Y%m%d') "
                f"FROM `{TABLE_NAME}` WHERE ts_code = :ts_code"
            ),
            {"ts_code": ts_code},
        ).scalar()
    return value


def iterateWindows(start_date: str, end_date: str, years_per_window: int = WINDOW_YEARS) -> list[tuple[str, str]]:
    """将大时间段切成多个窗口，避免单次数据过大。"""
    start_dt = dt.datetime.strptime(start_date, "%Y%m%d").date()
    end_dt = dt.datetime.strptime(end_date, "%Y%m%d").date()
    windows: list[tuple[str, str]] = []
    cursor = start_dt
    while cursor <= end_dt:
        next_end = min(dt.date(cursor.year + years_per_window - 1, 12, 31), end_dt)
        windows.append((cursor.strftime("%Y%m%d"), next_end.strftime("%Y%m%d")))
        cursor = next_end + dt.timedelta(days=1)
    return windows


def importOneCode(pro, engine: Engine, ts_code: str, start_date: str, end_date: str, dry_run: bool) -> int:
    """导入单个指数在给定区间的全部数据。"""
    total_rows = 0
    windows = iterateWindows(start_date, end_date)
    for idx, (win_start, win_end) in enumerate(windows, start=1):
        if not running:
            break
        raw_df = fetchWithPaging(pro, ts_code=ts_code, start_date=win_start, end_date=win_end)
        cooked_df = processDf(raw_df, ts_code)
        written = saveData(engine, cooked_df, dry_run=dry_run)
        total_rows += written
        logging.info(
            "[%s] 窗口[%s/%s] %s~%s 获取=%s 写入=%s",
            ts_code,
            idx,
            len(windows),
            win_start,
            win_end,
            len(raw_df),
            written,
        )
        time.sleep(REQUEST_INTERVAL)
    return total_rows


def fullImport(pro, engine: Engine, dry_run: bool) -> set[str]:
    """首次全量：从 FULL_START_DATE 拉到今天。"""
    today = dt.date.today().strftime("%Y%m%d")
    total = 0
    changed_codes: set[str] = set()
    for i, ts_code in enumerate(INDEX_CODE_NAME_MAP.keys(), start=1):
        if not running:
            break
        rows = importOneCode(pro, engine, ts_code, FULL_START_DATE, today, dry_run=dry_run)
        total += rows
        if rows > 0:
            changed_codes.add(ts_code)
        logging.info("全量进度 [%s/%s] %s 完成，累计写入=%s", i, len(INDEX_CODE_NAME_MAP), ts_code, total)
    logging.info("全量导入结束，累计写入=%s。", total)
    return changed_codes


def incrementalImport(pro, engine: Engine, dry_run: bool) -> set[str]:
    """每日增量：按每个指数各自最大交易日向后补齐。"""
    today = dt.date.today().strftime("%Y%m%d")
    total = 0
    changed_codes: set[str] = set()
    for i, ts_code in enumerate(INDEX_CODE_NAME_MAP.keys(), start=1):
        if not running:
            break
        max_date = getMaxTradeDateByCode(engine, ts_code)
        if not max_date:
            start_date = FULL_START_DATE
        else:
            start_date = (dt.datetime.strptime(max_date, "%Y%m%d").date() + dt.timedelta(days=1)).strftime("%Y%m%d")
        if start_date > today:
            logging.info("增量进度 [%s/%s] %s 无新数据。", i, len(INDEX_CODE_NAME_MAP), ts_code)
            continue
        rows = importOneCode(pro, engine, ts_code, start_date, today, dry_run=dry_run)
        total += rows
        if rows > 0:
            changed_codes.add(ts_code)
        logging.info("增量进度 [%s/%s] %s 完成，累计写入=%s", i, len(INDEX_CODE_NAME_MAP), ts_code, total)
    logging.info("增量导入结束，累计写入=%s。", total)
    return changed_codes


def parseArgs() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="index_daily 入库脚本（全量 + 增量 + 振幅）")
    parser.add_argument(
        "--mode",
        choices=["auto", "full", "incremental"],
        default="auto",
        help="导入模式：auto/full/incremental，默认 auto。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只请求和计算，不写入数据库。",
    )
    return parser.parse_args()


def main() -> None:
    """主入口：建表后按模式执行。"""
    setupLogger()
    args = parseArgs()
    engine = makeEngine()
    pro = ts.pro_api(TUSHARE_TOKEN)
    createTable(engine, pro)
    changed_codes: set[str] = set()
    if args.mode == "full":
        changed_codes = fullImport(pro, engine, dry_run=args.dry_run)
        recalculatePerForCodes(engine, changed_codes, dry_run=args.dry_run)
        return
    if args.mode == "incremental":
        changed_codes = incrementalImport(pro, engine, dry_run=args.dry_run)
        # 增量场景需保证 PER 可持续更新；无新数据时若历史尚未补算，触发一次全量补算。
        if not changed_codes and getPerNullCount(engine) > 0:
            changed_codes = set(INDEX_CODE_NAME_MAP.keys())
        recalculatePerForCodes(engine, changed_codes, dry_run=args.dry_run)
        return

    # auto 模式：空表走全量，否则走增量
    with engine.connect() as conn:
        row_count = conn.execute(text(f"SELECT COUNT(*) FROM `{TABLE_NAME}`")).scalar() or 0
    if row_count == 0:
        logging.info("auto 模式检测到空表，执行全量导入。")
        changed_codes = fullImport(pro, engine, dry_run=args.dry_run)
        recalculatePerForCodes(engine, changed_codes, dry_run=args.dry_run)
    else:
        logging.info("auto 模式检测到已有数据（%s），执行增量导入。", row_count)
        changed_codes = incrementalImport(pro, engine, dry_run=args.dry_run)
        if not changed_codes and getPerNullCount(engine) > 0:
            changed_codes = set(INDEX_CODE_NAME_MAP.keys())
        recalculatePerForCodes(engine, changed_codes, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

# 代码更新时间：2026-03-26 18:57
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
