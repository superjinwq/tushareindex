#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tushare fund_adj（基金复权因子）入库 stock_tushare.etf_fund_adj。
接口文档: https://tushare.pro/document/2?doc_id=199
限量: 单次最多 2000 行，需 offset 分页。

用法:
  python fund_adj.py                  # 增量（每代码从本地 MAX(trade_date)+1 拉取）
  python fund_adj.py --full           # 全量（自 FULL_START_DATE）
  python fund_adj.py --full --codes 510300.SH,510500.SH

前复权锚定最新因子（与 Tushare 说明一致）:
  close_qfq(d) = close_bfq(d) * adj_factor(d) / adj_factor(最新交易日)
若某日缺因子行，merge_asof 向后对齐后仍缺则按 1.0 处理。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
import threading
from datetime import date, datetime, timedelta
from logging import Formatter, StreamHandler, getLogger, INFO
from logging.handlers import TimedRotatingFileHandler
from typing import Callable, List, Optional

import numpy as np
import pandas as pd
import pymysql
import tushare as ts

# ============ 配置 ============
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "stock_tushare") or "stock_tushare",
    "charset": "utf8mb4",
    "connect_timeout": 10,
}
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "").strip()
if not TUSHARE_TOKEN:
    raise SystemExit("缺少环境变量 TUSHARE_TOKEN")
TABLE_FUND_ADJ = "etf_fund_adj"
FIELDS_FUND_ADJ = "ts_code,trade_date,adj_factor"
FULL_START_DATE = "20100101"
REQUEST_INTERVAL = 0.2
MAX_RETRY = 3
API_LIMIT = 2000

# 默认与宽基脚本一致的 9 只；可用参数覆盖
DEFAULT_ETF_CODES: List[str] = [
    "588000.SH", "510500.SH", "510050.SH", "510300.SH", "588080.SH",
    "159915.SZ", "159919.SZ", "159922.SZ", "159901.SZ",
]

_log_dir = os.path.dirname(os.path.abspath(__file__))
_log_path = os.path.join(_log_dir, "fund_adj.log")
logger = getLogger("fund_adj")
logger.setLevel(INFO)
_fmt = Formatter("%(asctime)s [%(levelname)s] %(message)s")
if not logger.handlers:
    class WindowsSafeTimedRotatingFileHandler(TimedRotatingFileHandler):
        def doRollover(self) -> None:
            for delay in (0, 0.25, 0.5):
                if delay:
                    time.sleep(delay)
                try:
                    super().doRollover()
                    return
                except OSError:
                    continue

    _fh = WindowsSafeTimedRotatingFileHandler(_log_path, when="midnight", backupCount=3, encoding="utf-8")
    _fh.setFormatter(_fmt)
    logger.addHandler(_fh)
    _sh = StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    logger.addHandler(_sh)

_stop = False


def _on_sig(signum, frame) -> None:
    global _stop
    _stop = True
    logger.info("收到中断信号(%s)，本批完成后退出", signum)


def _register_signal_handlers() -> None:
    """仅在主线程注册 signal，避免被 Web 框架子线程 import 时抛 ValueError。"""
    if threading.current_thread() is not threading.main_thread():
        return
    signal.signal(signal.SIGINT, _on_sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_sig)


_register_signal_handlers()


def get_connection():
    return pymysql.connect(**DB_CONFIG)


def create_table_fund_adj(conn) -> None:
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{TABLE_FUND_ADJ}` (
      `ts_code`     VARCHAR(20)   NOT NULL COMMENT 'TS基金代码',
      `trade_date`  DATE          NOT NULL COMMENT '交易日期',
      `adj_factor`  DECIMAL(16,8) NOT NULL COMMENT '复权因子',
      `created_at`  DATETIME      DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
      `updated_at`  DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
      PRIMARY KEY (`ts_code`, `trade_date`),
      KEY `idx_trade_date` (`trade_date`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ETF/基金复权因子 fund_adj';
    """
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()
    logger.info("表 %s 已就绪", TABLE_FUND_ADJ)


def _process_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce")
    out["adj_factor"] = pd.to_numeric(out["adj_factor"], errors="coerce")
    out = out.replace({np.nan: None, pd.NaT: None})
    out = out[out["trade_date"].notna() & out["adj_factor"].notna()].copy()
    return out


def save_fund_adj_rows(conn, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    df = _process_df(df)
    if df.empty:
        return 0
    rows = []
    for _, r in df.iterrows():
        td = r["trade_date"]
        td_sql = td.strftime("%Y-%m-%d") if hasattr(td, "strftime") else str(td)[:10]
        rows.append((r["ts_code"], td_sql, float(r["adj_factor"])))
    sql = (
        f"INSERT INTO `{TABLE_FUND_ADJ}` (`ts_code`,`trade_date`,`adj_factor`) VALUES (%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE `adj_factor`=VALUES(`adj_factor`)"
    )
    n = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), 500):
            if _stop:
                break
            batch = rows[i : i + 500]
            cur.executemany(sql, batch)
            n += len(batch)
    conn.commit()
    return n


def get_max_trade_date_adj(conn, ts_code: str) -> Optional[date]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT MAX(trade_date) FROM `{TABLE_FUND_ADJ}` WHERE ts_code=%s", (ts_code,))
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    d = row[0]
    return d if isinstance(d, date) else datetime.strptime(str(d)[:10], "%Y-%m-%d").date()


def _fetch_fund_adj_page(
    pro,
    ts_code: str,
    start_date: str,
    end_date: str,
    offset: int,
) -> Optional[pd.DataFrame]:
    for attempt in range(MAX_RETRY):
        try:
            df = pro.fund_adj(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
                fields=FIELDS_FUND_ADJ,
                limit=str(API_LIMIT),
                offset=str(offset),
            )
            time.sleep(REQUEST_INTERVAL)
            return df
        except Exception as e:
            if attempt == MAX_RETRY - 1:
                logger.error("fund_adj 失败 ts_code=%s offset=%s: %s", ts_code, offset, e)
                return None
            time.sleep(2 ** (attempt + 1))
    return None


def run_import_fund_adj(
    conn,
    etf_codes: List[str],
    full_import: bool = False,
    stop_check: Optional[Callable[[], bool]] = None,
) -> int:
    """
    在已有 conn 上执行：确保表存在，按代码增量或全量拉取 fund_adj 并 UPSERT。
    stop_check: 可选，返回 True 时中止（用于宿主脚本 _stop_flag）。
    """
    create_table_fund_adj(conn)
    pro = ts.pro_api(TUSHARE_TOKEN)
    end_s = date.today().strftime("%Y%m%d")
    start_full = FULL_START_DATE
    grand = 0
    for ts_code in etf_codes:
        if _stop or (stop_check and stop_check()):
            break
        if full_import:
            start_s = start_full
        else:
            max_d = get_max_trade_date_adj(conn, ts_code)
            if max_d is None:
                start_s = start_full
            else:
                start_s = (max_d + timedelta(days=1)).strftime("%Y%m%d")
        if start_s > end_s:
            continue
        offset = 0
        while not _stop and not (stop_check and stop_check()):
            df = _fetch_fund_adj_page(pro, ts_code, start_s, end_s, offset)
            if df is None or df.empty:
                break
            grand += save_fund_adj_rows(conn, df)
            if len(df) < API_LIMIT:
                break
            offset += len(df)
        logger.info("fund_adj %s %s~%s 累计写入(含更新)本段分页", ts_code, start_s, end_s)
    return grand


def _merge_asof_day_key(series: pd.Series) -> pd.Series:
    """pandas merge_asof 要求 on 列 datetime 精度一致（否则 M8[us] vs M8[s] 报错）。"""
    return pd.to_datetime(series, errors="coerce").dt.normalize().astype("datetime64[ns]")


def load_adj_dataframe(conn, ts_code: str) -> pd.DataFrame:
    """升序 trade_date 的复权因子表，用于 merge_asof。"""
    sql = f"SELECT trade_date, adj_factor FROM `{TABLE_FUND_ADJ}` WHERE ts_code=%s ORDER BY trade_date"
    df = pd.read_sql(sql, conn, params=(ts_code,))
    if df.empty:
        return df
    df["trade_date"] = _merge_asof_day_key(df["trade_date"])
    df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce")
    return df.dropna(subset=["trade_date", "adj_factor"])


def adj_latest_value(adj_df: pd.DataFrame) -> Optional[float]:
    if adj_df is None or adj_df.empty:
        return None
    v = float(adj_df["adj_factor"].iloc[-1])
    return v if v and not np.isnan(v) else None


def attach_close_qfq_series(
    df: pd.DataFrame,
    time_col: str,
    raw_close_col: str,
    adj_df: pd.DataFrame,
) -> pd.Series:
    """
    对每行取 time_col 的日历日，按当日复权因子与最新因子算前复权收盘价序列。
    返回与 df 原始行顺序对齐的 close_qfq；无因子时退化为 raw close。
    """
    if df is None or df.empty:
        return pd.Series(dtype=float)
    close_raw = pd.to_numeric(df[raw_close_col], errors="coerce")
    if adj_df is None or adj_df.empty:
        return close_raw
    adj_latest = adj_latest_value(adj_df)
    if adj_latest is None or adj_latest == 0:
        return close_raw
    work = pd.DataFrame(
        {
            "_ord": np.arange(len(df), dtype=np.int64),
            "_day": _merge_asof_day_key(df[time_col]),
            "_close": close_raw.values,
        },
        index=df.index,
    )
    work = work.sort_values("_day")
    right = adj_df.sort_values("trade_date").rename(columns={"trade_date": "_day"})
    right["_day"] = _merge_asof_day_key(right["_day"])
    merged = pd.merge_asof(
        work[["_ord", "_day", "_close"]],
        right,
        on="_day",
        direction="backward",
    )
    merged["adj_factor"] = pd.to_numeric(merged["adj_factor"], errors="coerce").fillna(1.0)
    merged["close_qfq"] = merged["_close"].astype(float) * merged["adj_factor"].astype(float) / float(adj_latest)
    merged = merged.sort_values("_ord")
    out = pd.Series(merged["close_qfq"].values, index=df.index, dtype=float)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="fund_adj -> etf_fund_adj 入库")
    parser.add_argument("--full", action="store_true", help="全量自 FULL_START_DATE")
    parser.add_argument(
        "--codes",
        type=str,
        default="",
        help="逗号分隔 ts_code，默认 9 只宽基 ETF",
    )
    args = parser.parse_args()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else list(DEFAULT_ETF_CODES)
    logger.info("fund_adj 入库 | 全量=%s | 代码数=%d", args.full, len(codes))
    try:
        conn = get_connection()
    except Exception as e:
        logger.exception("数据库连接失败: %s", e)
        sys.exit(1)
    try:
        n = run_import_fund_adj(conn, codes, full_import=args.full)
        logger.info("完成，写入/更新约 %d 行", n)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

# 代码更新时间：2026-03-20 13:22 merge_asof 统一 datetime64[ns] 避免与 MySQL 读入的 s/us 精度冲突
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
