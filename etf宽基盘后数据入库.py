#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETF 宽基盘后数据入库：日线 + 分钟，先导入后计算。

一、日线：Tushare fund_daily -> etf_daily，再计算 MA/EMA/BIAS/per_bias 写回同一表。
  - 导入：增量（默认）/ 全量（--full-import）
  - 计算：增量（默认）/ 全量（--full-ma）
二、分钟：Tushare stk_mins -> etf_stk_mins（不改）；再算前复权 close_qfq 并以之为准计算 MA/EMA/BIAS 写入 etf_stk_mins_ma。
三、复权：Tushare fund_adj -> etf_fund_adj（9 只）；日线/分钟指标均用 close_qfq = close * adj/adj_最新。
    运行时若检测到某标的 adj_latest 与上次落库的 adj_state 不一致，会对该标的自动触发分钟/日线全量重算（无需手搓 --full-ma）；手工 --full-ma 仍用于强制全表重算或排障。
    - 周期：1min / 15min / 60min（1min 按代码、按自然日逐日拉取，单次行数最少、最安全）
    - 导入：增量（默认）/ 全量（--full-import 时按年循环；1min 为按日请求）
    - 计算：增量（默认）/ 全量（--full-ma）；可选 --mins-ma-workers 按 (ts_code,freq) 并行算 MA（写库单连接）
    - 分钟 MA/EMA（etf_stk_mins_ma）：仅经 compute_ma_ema_mins → _strip_open_anchor_bar_mins，无旁路。
      1min/15min/60min 在 rolling/EMA 前均剔除每日 09:30 这根 K，**结果表不写入 09:30**（15/60 与规则 K 一致；1min 按你的口径也不落库该时刻）。

共用 9 只 ETF 代码；单日志 etf宽基盘后数据入库.log，保留 3 天。
参数：--daily-only 仅日线，--mins-only 仅分钟；默认先日线后分钟。
      --skip-fetch 仅计算，--skip-ma 仅导入；--full-import / --full-ma 全量。
      --skip-fund-adj 跳过 fund_adj 同步；默认每次跑日线前增量拉 etf_fund_adj。
      --freq 仅导入指定分钟周期（可多次，如仅 1min：--freq 1min）；不传则 1min/15min/60min 全跑。
"""

import os
import sys
import time
import argparse
import signal
import logging
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, date, timedelta
from typing import List, Tuple, Dict, Optional, Any

import warnings
import numpy as np
import pandas as pd
import pymysql
import tushare as ts

warnings.filterwarnings("ignore", message=".*pandas only supports SQLAlchemy.*")

from fund_adj import (
    adj_latest_value,
    attach_close_qfq_series,
    create_table_fund_adj,
    load_adj_dataframe,
    run_import_fund_adj,
)

# ============ 共用配置 ============
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
REQUEST_INTERVAL = 0.2
MAX_RETRY = 3
ETF_CODES: List[str] = [
    "588000.SH", "510500.SH", "510050.SH", "510300.SH", "588080.SH",
    "159915.SZ", "159919.SZ", "159922.SZ", "159901.SZ",
]

# ---------- 日线专用 ----------
TABLE_DAILY = "etf_daily"
ADJ_STATE_TABLE_DAILY = "etf_daily_ma_adj_state"
FULL_START_DATE = "20100101"
CHUNK_TRADE_DAYS = 1000
FIELDS_DAILY = "ts_code,trade_date,pre_close,open,high,low,close,change,pct_chg,vol,amount"
MA_PERIODS_DAILY = [10, 20, 60, 120, 200, 250]
EMA_PERIODS_DAILY = [10, 20, 60, 120, 200, 250]
PER_BIAS_WINDOW_DAILY = 1250
LOOKBACK_BARS_DAILY = max(250, PER_BIAS_WINDOW_DAILY)
BATCH_SIZE_DAILY = 500

# ---------- 分钟专用 ----------
SOURCE_TABLE_MINS = "etf_stk_mins"
TARGET_TABLE_MINS = "etf_stk_mins_ma"
ADJ_STATE_TABLE_MINS = "etf_stk_mins_ma_adj_state"
FULL_START_YEAR = 2010
FREQ_LIST: List[str] = ["1min", "15min", "60min"]
MA_PERIODS_MINS = [10, 20, 60, 120]
EMA_PERIODS_MINS = [20, 60, 120]
BIAS_MA_PERIODS_MINS = [10, 20, 60]
BIAS_EMA_PERIODS_MINS = [20, 60]
PER_BIAS_WINDOW_MINS = 1250
LOOKBACK_BARS_MINS = max(120, PER_BIAS_WINDOW_MINS)
PER_MCD_WINDOW_15MIN = 850
PER_MCD_WINDOW_60MIN = 500
PER_MCD_WINDOW_DAILY = 250
BATCH_SIZE_MINS = 5000
# REPLACE 写库进度日志：至少每多少批打一条（批数少则自动提高频率）
MINS_REPLACE_LOG_MIN_BATCHES = 50
# 分钟 MA 并行：仅并行 pandas 计算，REPLACE 仍单线程单连接，避免多连接写同一表导致顺序/事务问题


def _mins_ma_workers_default() -> int:
    """默认不超过 8，避免内存与 GIL 争用过度。"""
    n = os.cpu_count() or 4
    return min(8, max(1, n))


def _resolve_mins_ma_workers(ma_workers: int) -> int:
    if ma_workers > 0:
        return max(1, ma_workers)
    env = os.getenv("ETF_MINS_MA_WORKERS", "").strip()
    if env.isdigit():
        return max(1, int(env))
    return _mins_ma_workers_default()

# ============ 日志 ============
# Windows 下午夜轮转 rename 易 PermissionError，与项目内其它脚本一致做有限次重试。


from windows_safe_log import WindowsSafeTimedRotatingFileHandler


_LOG_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_FILE = os.path.join(_LOG_DIR, "etf宽基盘后数据入库.log")
logger = logging.getLogger("etf_wide_post")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_fh = WindowsSafeTimedRotatingFileHandler(_LOG_FILE, when="midnight", backupCount=3, encoding="utf-8")
_fh.setFormatter(_fmt)
logger.addHandler(_fh)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
logger.addHandler(_sh)

_stop_flag = False


def _sig(signum, frame):
    global _stop_flag
    _stop_flag = True
    logger.info("收到中断信号(%s)，本批完成后退出", signum)


signal.signal(signal.SIGINT, _sig)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, _sig)


def log_phase(phase: str, detail: str = "") -> None:
    """
    统一「当前在干什么」的交互式日志，便于终端长时间运行时扫一眼即知阶段。
    phase：大步骤名；detail：可选补充说明（耗时、数量等）。
    """
    if detail:
        logger.info("[阶段] %s — %s", phase, detail)
    else:
        logger.info("[阶段] %s", phase)


def get_connection():
    return pymysql.connect(**DB_CONFIG)


def get_table_columns(conn, table_name: str) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            (DB_CONFIG["database"], table_name),
        )
        return [r[0] for r in cur.fetchall()]


# ======================= 日线：etf_daily 导入 + MA/EMA =======================

_EXTRA_COLUMNS_DAILY = (
    [(f"ma{p}", "DECIMAL(10,4)", f"{p}周期均线") for p in MA_PERIODS_DAILY]
    + [(f"ema{p}", "DECIMAL(10,4)", f"{p}周期指数均线") for p in EMA_PERIODS_DAILY]
    + [("mcd_pct", "DECIMAL(8,4)", "聚集度：六线标准差/价格×100（std(ddof=0)/price*100）")]
    + [("per_mcd", "DECIMAL(10,2)", "mcd_pct滚动百分位（D:250）")]
    + [(f"bias{p}ma", "DECIMAL(10,4)", f"相对{p}均线乖离") for p in MA_PERIODS_DAILY]
    + [(f"bias{p}ema", "DECIMAL(10,4)", f"相对{p}EMA乖离") for p in EMA_PERIODS_DAILY]
    + [(f"per_bias{p}ma", "DECIMAL(10,2)", f"相对{p}均线乖离百分位") for p in MA_PERIODS_DAILY]
    + [(f"per_bias{p}ema", "DECIMAL(10,2)", f"相对{p}EMA乖离百分位") for p in EMA_PERIODS_DAILY]
)


def create_table_daily(conn) -> None:
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{TABLE_DAILY}` (
      `ts_code`    varchar(20) NOT NULL COMMENT 'TS代码',
      `trade_date` date        NOT NULL COMMENT '交易日期',
      `pre_close`  decimal(10,4) DEFAULT NULL,
      `open`       decimal(10,4) DEFAULT NULL,
      `high`       decimal(10,4) DEFAULT NULL,
      `low`        decimal(10,4) DEFAULT NULL,
      `close`      decimal(10,4) DEFAULT NULL,
      `etf_change` decimal(10,4) DEFAULT NULL,
      `pct_chg`    decimal(10,4) DEFAULT NULL,
      `vol`        decimal(20,2) DEFAULT NULL,
      `amount`     decimal(20,2) DEFAULT NULL,
      PRIMARY KEY (`ts_code`,`trade_date`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ETF日线(fund_daily,仅9只)';
    """
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()
    logger.info("表 %s 已就绪", TABLE_DAILY)


def _process_df_daily(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    df = df.copy()
    if "trade_date" in df.columns:
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
        df["trade_date"] = df["trade_date"].dt.strftime("%Y-%m-%d")
        df["trade_date"] = df["trade_date"].where(df["trade_date"].notna(), None)
        df.loc[df["trade_date"].isin(["NaT", "nan", ""]), "trade_date"] = None
    if "change" in df.columns:
        df = df.rename(columns={"change": "etf_change"})
    df = df.replace({np.nan: None, pd.NaT: None})
    if "trade_date" in df.columns:
        df = df[df["trade_date"].notna()].copy()
    return df


def save_daily_data(conn, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    df = _process_df_daily(df)
    all_cols = ["ts_code", "trade_date", "pre_close", "open", "high", "low", "close", "etf_change", "pct_chg", "vol", "amount"]
    cols = [c for c in all_cols if c in df.columns]
    if not cols:
        return 0
    df = df[cols]
    rows = df.to_numpy().tolist()
    placeholders = ", ".join(["%s"] * len(cols))
    updates = ", ".join([f"`{c}` = VALUES(`{c}`)" for c in cols if c not in ("ts_code", "trade_date")])
    sql = f"INSERT INTO `{TABLE_DAILY}` ({', '.join('`' + c + '`' for c in cols)}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}"
    n = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH_SIZE_DAILY):
            if _stop_flag:
                break
            batch = rows[i : i + BATCH_SIZE_DAILY]
            cur.executemany(sql, batch)
            n += len(batch)
    conn.commit()
    return n


def _fetch_fund_daily_retry(pro, ts_code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    for attempt in range(MAX_RETRY):
        try:
            df = pro.fund_daily(ts_code=ts_code, start_date=start_date, end_date=end_date, fields=FIELDS_DAILY)
            return df if df is not None and not df.empty else None
        except Exception as e:
            if attempt == MAX_RETRY - 1:
                logger.error("fund_daily 失败 ts_code=%s %s~%s: %s", ts_code, start_date, end_date, e)
                return None
            time.sleep(2 ** (attempt + 1))
    return None


def _date_range_chunks(start: date, end: date, chunk_days: int):
    current = start
    while current <= end and not _stop_flag:
        chunk_end = min(current + timedelta(days=chunk_days), end)
        yield current.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")
        current = chunk_end + timedelta(days=1)


def get_max_trade_date_daily(conn, ts_code: str) -> Optional[date]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT MAX(trade_date) FROM `{TABLE_DAILY}` WHERE ts_code = %s", (ts_code,))
        row = cur.fetchone()
    if row and row[0] is not None:
        return row[0] if isinstance(row[0], date) else datetime.strptime(str(row[0])[:10], "%Y-%m-%d").date()
    return None


def run_daily_import(conn, full_import: bool = False) -> int:
    logger.info("---------- 日线导入（etf_daily）----------")
    log_phase("日线导入", "建表并逐标的拉取 fund_daily（增量/全量由参数决定）")
    create_table_daily(conn)
    pro = ts.pro_api(TUSHARE_TOKEN)
    end_date = date.today()
    full_start = datetime.strptime(FULL_START_DATE, "%Y%m%d").date()
    total = 0
    for ts_code in ETF_CODES:
        if _stop_flag:
            break
        if full_import:
            start_date = full_start
            logger.info("[日线 %s] 全量 %s ~ %s", ts_code, start_date, end_date)
        else:
            max_d = get_max_trade_date_daily(conn, ts_code)
            if max_d is None:
                start_date = full_start
                logger.info("[日线 %s] 无本地数据 %s ~ %s", ts_code, start_date, end_date)
            else:
                start_date = max_d + timedelta(days=1)
        if start_date > end_date:
            continue
        for s, e in _date_range_chunks(start_date, end_date, CHUNK_TRADE_DAYS * 2):
            if _stop_flag:
                break
            df = _fetch_fund_daily_retry(pro, ts_code, s, e)
            if df is not None and not df.empty:
                total += save_daily_data(conn, df)
                logger.info("[日线 %s] %s ~ %s 写入 %d 行", ts_code, s, e, len(df))
            time.sleep(REQUEST_INTERVAL)
    if total == 0:
        logger.info("日线导入完成：无新数据（已是最新）")
    else:
        logger.info("日线导入完成：本次共写入 %d 行", total)
    return total


def ensure_close_qfq_column_daily(conn) -> None:
    """etf_daily 增加 close_qfq（前复权收盘），与 etf_fund_adj 配套。"""
    existing = set(get_table_columns(conn, TABLE_DAILY))
    if "close_qfq" in existing:
        return
    sql = (
        f"ALTER TABLE `{TABLE_DAILY}` ADD COLUMN `close_qfq` DECIMAL(10,4) DEFAULT NULL "
        "COMMENT '前复权收盘(close*adj/adj最新)'"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    logger.info("已添加列 %s.close_qfq", TABLE_DAILY)


def run_daily_close_qfq(conn) -> None:
    """按 etf_fund_adj 回写 etf_daily.close_qfq（整只标的全历史重算，适配前复权锚定最新）。"""
    logger.info("---------- 日线 close_qfq 回写（etf_daily）----------")
    log_phase("日线 close_qfq", "按复权因子回写 etf_daily.close_qfq，共 {} 只标的".format(len(ETF_CODES)))
    ensure_close_qfq_column_daily(conn)
    for idx, ts_code in enumerate(ETF_CODES, start=1):
        if _stop_flag:
            break
        log_phase("日线 close_qfq", "{}/{} {}".format(idx, len(ETF_CODES), ts_code))
        adj = load_adj_dataframe(conn, ts_code)
        if adj.empty:
            logger.warning("[close_qfq] %s 无 etf_fund_adj 数据，跳过（可先跑 fund_adj.py）", ts_code)
            continue
        al = adj_latest_value(adj)
        if al is None or al == 0:
            logger.warning("[close_qfq] %s adj_latest 无效，跳过该标的回写", ts_code)
            continue
        sql = (
            f"SELECT trade_date, `close` FROM `{TABLE_DAILY}` "
            "WHERE ts_code = %s AND `close` IS NOT NULL AND `close` > 0 ORDER BY trade_date"
        )
        daily = pd.read_sql(sql, conn, params=(ts_code,))
        if daily.empty:
            continue
        daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce")
        daily = daily.dropna(subset=["trade_date"])
        m = daily.merge(adj, on="trade_date", how="left")
        m["adj_factor"] = pd.to_numeric(m["adj_factor"], errors="coerce").ffill().bfill().fillna(1.0)
        m["close_qfq"] = m["close"].astype(float) * m["adj_factor"].astype(float) / float(al)
        upd_sql = f"UPDATE `{TABLE_DAILY}` SET close_qfq = %s WHERE ts_code = %s AND trade_date = %s"
        tuples = []
        for _, r in m.iterrows():
            td = r["trade_date"]
            td_sql = td.strftime("%Y-%m-%d") if hasattr(td, "strftime") else str(td)[:10]
            tuples.append((round(float(r["close_qfq"]), 4), ts_code, td_sql))
        with conn.cursor() as cur:
            for i in range(0, len(tuples), BATCH_SIZE_DAILY):
                if _stop_flag:
                    break
                cur.executemany(upd_sql, tuples[i : i + BATCH_SIZE_DAILY])
        conn.commit()
        logger.info("[close_qfq] 日线 %s 更新 %d 行", ts_code, len(tuples))


def ensure_extra_columns_daily(conn) -> None:
    existing = set(get_table_columns(conn, TABLE_DAILY))
    for col_name, col_type, comment in _EXTRA_COLUMNS_DAILY:
        if col_name in existing:
            continue
        sql = f"ALTER TABLE `{TABLE_DAILY}` ADD COLUMN `{col_name}` {col_type} DEFAULT NULL COMMENT '{comment}'"
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        logger.info("已添加列 %s.%s", TABLE_DAILY, col_name)


def _rolling_percentile_series(series: pd.Series, window: int) -> pd.Series:
    """乖离百分位：窗口内「<= 当前值」占比×100，与全量/增量路径共用一套口径。"""

    def _pct(s):
        cur = s.iloc[-1]
        if pd.isna(cur):
            return float("nan")
        valid = s.dropna()
        if len(valid) == 0:
            return float("nan")
        return (valid <= cur).sum() / len(valid) * 100.0

    return series.rolling(window=window, min_periods=1).apply(_pct, raw=False)


def compute_ma_ema_bias_daily(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("trade_date").reset_index(drop=True)
    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    if close.empty:
        return pd.DataFrame()
    df = df.loc[close.index].copy()
    df["close"] = close.values
    for p in MA_PERIODS_DAILY:
        df[f"ma{p}"] = df["close"].rolling(window=p, min_periods=p).mean()
    for p in EMA_PERIODS_DAILY:
        df[f"ema{p}"] = df["close"].ewm(span=p, adjust=False).mean()
    # 日线 mcd_pct：六线标准差 / 价格 * 100（价格口径由 load_daily 控制：close_qfq 优先，缺失回退 close）
    six = df.loc[:, ["ma20", "ema20", "ma60", "ema60", "ma120", "ema120"]].apply(
        pd.to_numeric, errors="coerce"
    )
    all_ok = six.notna().all(axis=1)
    denom = pd.to_numeric(df["close"], errors="coerce")
    valid_denom = denom.notna() & (denom > 0)
    std = six.std(axis=1, ddof=0)
    # 为保证可复算一致性：先固定 mcd_pct 的落库精度（4位），再基于该序列计算 per_mcd
    # 这样后续从数据库中读取 mcd_pct 重算 per_mcd 可与落库结果保持一致。
    mcd_raw = ((std / denom) * 100.0).where(all_ok & valid_denom)
    df["mcd_pct"] = mcd_raw.round(4)
    df["per_mcd"] = _rolling_percentile_series(df["mcd_pct"], PER_MCD_WINDOW_DAILY)
    for p in MA_PERIODS_DAILY:
        c = f"ma{p}"
        df[f"bias{p}ma"] = np.where((df[c].notna()) & (df[c] != 0), (df["close"] - df[c]) / df[c] * 100.0, float("nan"))
    for p in EMA_PERIODS_DAILY:
        c = f"ema{p}"
        df[f"bias{p}ema"] = np.where((df[c].notna()) & (df[c] != 0), (df["close"] - df[c]) / df[c] * 100.0, float("nan"))
    bias_cols = [f"bias{p}ma" for p in MA_PERIODS_DAILY] + [f"bias{p}ema" for p in EMA_PERIODS_DAILY]
    for col in bias_cols:
        df[f"per_{col}"] = _rolling_percentile_series(df[col], PER_BIAS_WINDOW_DAILY)
    out_cols = ["ts_code", "trade_date"] + [name for (name, _t, _c) in _EXTRA_COLUMNS_DAILY]
    result = df[out_cols].copy()
    for c in result.columns:
        if c in ("ts_code", "trade_date"):
            continue
        result[c] = result[c].round(2) if c.startswith("per_") else result[c].round(4)
    return result


def load_daily(conn) -> pd.DataFrame:
    # 准确性：优先 close_qfq（与 run_daily_close_qfq 一致）；缺失时 COALESCE 回退 raw close，避免误用未回写列
    sql = (
        f"SELECT ts_code, trade_date, COALESCE(close_qfq, close) AS close FROM `{TABLE_DAILY}` "
        "WHERE close IS NOT NULL AND close > 0 ORDER BY ts_code, trade_date"
    )
    df = pd.read_sql(sql, conn)
    if not df.empty and "trade_date" in df.columns:
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    return df


def get_max_computed_date_daily(conn) -> Dict[str, str]:
    sql = f"SELECT ts_code, MAX(trade_date) AS max_d FROM `{TABLE_DAILY}` WHERE ma10 IS NOT NULL GROUP BY ts_code"
    df = pd.read_sql(sql, conn)
    if df.empty:
        return {}
    max_d_str = pd.to_datetime(df["max_d"], errors="coerce").dt.strftime("%Y-%m-%d")
    return dict(zip(df["ts_code"], max_d_str))


def ensure_adj_state_table_daily(conn) -> None:
    """保存各 ts_code 最近一次用于日线计算的 adj_latest，用于判断是否需全量重算。"""
    ddl = """
    CREATE TABLE IF NOT EXISTS `{}` (
      `ts_code` VARCHAR(20) NOT NULL,
      `adj_latest` DECIMAL(20,10) DEFAULT NULL,
      `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (`ts_code`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='日线MA复权因子状态';
    """.format(ADJ_STATE_TABLE_DAILY)
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()


def load_adj_state_map_daily(conn) -> Dict[str, float]:
    sql = "SELECT ts_code, adj_latest FROM `{}`".format(ADJ_STATE_TABLE_DAILY)
    df = pd.read_sql(sql, conn)
    if df.empty:
        return {}
    state: Dict[str, float] = {}
    for _, row in df.iterrows():
        ts_code = str(row["ts_code"])
        val = pd.to_numeric(row["adj_latest"], errors="coerce")
        if pd.notna(val):
            state[ts_code] = float(val)
    return state


def save_adj_state_map_daily(conn, state_map: Dict[str, float]) -> None:
    if not state_map:
        return
    sql = (
        "REPLACE INTO `{}` (ts_code, adj_latest, updated_at) "
        "VALUES (%s, %s, NOW())"
    ).format(ADJ_STATE_TABLE_DAILY)
    data = [(k, v) for k, v in state_map.items() if v is not None and not np.isnan(v)]
    if not data:
        return
    with conn.cursor() as cur:
        cur.executemany(sql, data)
    conn.commit()


def batch_update_daily(conn, df: pd.DataFrame, update_cols: List[str]) -> Tuple[int, bool]:
    """批量 UPDATE 日线指标列。返回 (行数, 是否因中断回滚)。"""
    if df.empty or not update_cols:
        return 0, False
    set_clause = ", ".join([f"`{c}` = %s" for c in update_cols])
    sql = f"UPDATE `{TABLE_DAILY}` SET {set_clause} WHERE ts_code = %s AND trade_date = %s"

    def row_to_tuple(row):
        vals = [None if (isinstance(v, float) and pd.isna(v)) else v for v in row[update_cols]]
        td = row["trade_date"]
        if hasattr(td, "strftime"):
            td = td.strftime("%Y-%m-%d")
        elif td is None or (isinstance(td, float) and pd.isna(td)) or str(td) in ("NaT", "nan", ""):
            td = None
        else:
            td = str(td)[:10]
        return tuple(vals) + (row["ts_code"], td)

    data = [row_to_tuple(row) for _, row in df.iterrows()]
    n = 0
    interrupted = False
    with conn.cursor() as cur:
        for i in range(0, len(data), BATCH_SIZE_DAILY):
            if _stop_flag:
                interrupted = True
                logger.warning("日线 MA 写库：收到中断，回滚未提交的 UPDATE 批次")
                break
            cur.executemany(sql, data[i : i + BATCH_SIZE_DAILY])
            n += len(data[i : i + BATCH_SIZE_DAILY])
    if interrupted:
        conn.rollback()
        return 0, True
    conn.commit()
    return n, False


def run_daily_ma_step(conn, full_ma: bool = False) -> None:
    logger.info("---------- 日线 MA/EMA 计算（写回 etf_daily）----------")
    ensure_adj_state_table_daily(conn)
    ensure_extra_columns_daily(conn)
    table_cols = get_table_columns(conn, TABLE_DAILY)
    update_cols = [c for c in [x[0] for x in _EXTRA_COLUMNS_DAILY] if c in table_cols]
    if not update_cols:
        logger.warning("日线表内无目标计算列")
        return
    max_computed = {} if full_ma else get_max_computed_date_daily(conn)
    if max_computed and not full_ma:
        logger.info("日线计算增量：%d 只已有结果", len(max_computed))
    else:
        logger.info("日线计算全量")
    prev_adj_state = load_adj_state_map_daily(conn)
    current_adj_state: Dict[str, float] = {}
    force_full_codes = set()
    force_full_reasons: Dict[str, str] = {}
    for ts_code in ETF_CODES:
        adj_df = load_adj_dataframe(conn, ts_code)
        cur_adj = adj_latest_value(adj_df)
        if cur_adj is None:
            continue
        current_adj_state[ts_code] = cur_adj
        prev_adj = prev_adj_state.get(ts_code)
        if prev_adj is not None and abs(cur_adj - prev_adj) > 1e-12:
            force_full_codes.add(ts_code)
            force_full_reasons[ts_code] = "adj_latest_changed"
    if force_full_codes:
        logger.info("检测到日线复权因子变化，以下标的执行日线全量重算：%s", ",".join(sorted(force_full_codes)))
    log_phase("日线 MA", "从 etf_daily 加载收盘价序列…")
    t_ld = time.perf_counter()
    df_all = load_daily(conn)
    log_phase("日线 MA", "加载完成 {} 行，耗时 {:.1f} 秒".format(len(df_all), time.perf_counter() - t_ld))
    if df_all.empty:
        logger.warning("日线 MA 完成：表内无有效 close，跳过计算")
        save_adj_state_map_daily(conn, current_adj_state)
        return
    results = []
    n_full, n_incr = 0, 0
    _dg = df_all.groupby("ts_code", sort=True)
    _n_daily = _dg.ngroups
    log_phase("日线 MA", "按标的计算指标，共 {} 只".format(_n_daily))
    for _di, (ts_code, group) in enumerate(_dg, start=1):
        if _stop_flag:
            break
        log_phase("日线 MA", "进度 {}/{}：{}（{} 行）开始".format(_di, _n_daily, ts_code, len(group)))
        try:
            group_force_full = ts_code in force_full_codes
            max_d_str = None if (full_ma or group_force_full) else max_computed.get(ts_code)
            if max_d_str is None:
                res = compute_ma_ema_bias_daily(group)
                if not res.empty:
                    results.append(res)
                    n_full += 1
            else:
                group_sorted = group.sort_values("trade_date").reset_index(drop=True)
                try:
                    max_d = pd.to_datetime(max_d_str)
                except Exception:
                    max_d = None
                if max_d is None or pd.isna(max_d):
                    res = compute_ma_ema_bias_daily(group)
                    if not res.empty:
                        results.append(res)
                        n_full += 1
                else:
                    lookback = group_sorted[group_sorted["trade_date"] <= max_d].tail(LOOKBACK_BARS_DAILY)
                    new_rows = group_sorted[group_sorted["trade_date"] > max_d]
                    if not new_rows.empty:
                        combined = (
                            pd.concat([lookback, new_rows], ignore_index=True)
                            .drop_duplicates(subset=["trade_date"])
                            .sort_values("trade_date")
                            .reset_index(drop=True)
                        )
                        res = compute_ma_ema_bias_daily(combined)
                        out = res[res["trade_date"] > max_d].copy()
                        if not out.empty:
                            results.append(out)
                            n_incr += 1
        finally:
            log_phase("日线 MA", "进度 {}/{}：{} 完成".format(_di, _n_daily, ts_code))
    if not results:
        logger.info("日线 MA 完成：无新行需更新（增量已是最新）")
        save_adj_state_map_daily(conn, current_adj_state)
        return
    df_out = pd.concat(results, ignore_index=True)
    df_out["trade_date"] = pd.to_datetime(df_out["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df_out = df_out[df_out["trade_date"].notna() & (df_out["trade_date"] != "NaT")].copy()
    if df_out.empty:
        logger.info("日线 MA 完成：无有效行可写")
        save_adj_state_map_daily(conn, current_adj_state)
        return
    log_phase("日线 MA", "UPDATE 写回 etf_daily，共 {} 行待更新".format(len(df_out)))
    n, daily_update_interrupted = batch_update_daily(conn, df_out, update_cols)
    if daily_update_interrupted:
        logger.warning("日线 MA：UPDATE 未完整提交，已跳过 adj_state 更新")
        return
    logger.info("日线 MA 完成：全量 %d 组、增量 %d 组，共更新 %d 行", n_full, n_incr, n)
    logger.info(
        "日线 MA 审计：force_full_codes=%d, reasons=%s, write_rows=%d",
        len(force_full_codes),
        ",".join("{}:{}".format(k, v) for k, v in sorted(force_full_reasons.items())) if force_full_reasons else "none",
        n,
    )
    save_adj_state_map_daily(conn, current_adj_state)


# ======================= 分钟：etf_stk_mins 导入 + etf_stk_mins_ma =======================

def create_table_mins_if_not_exists(conn) -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS `{}` (
      `ts_code`    VARCHAR(20)  NOT NULL COMMENT 'ETF代码',
      `trade_time` DATETIME     NOT NULL COMMENT '交易时间',
      `freq`       VARCHAR(10)  NOT NULL COMMENT '1min/15min/60min',
      `open`       DECIMAL(16,4) DEFAULT NULL,
      `close`      DECIMAL(16,4) DEFAULT NULL,
      `high`       DECIMAL(16,4) DEFAULT NULL,
      `low`        DECIMAL(16,4) DEFAULT NULL,
      `vol`        BIGINT        DEFAULT NULL,
      `amount`     DECIMAL(20,2) DEFAULT NULL,
      PRIMARY KEY (`ts_code`,`trade_time`,`freq`),
      KEY `idx_trade_time` (`trade_time`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ETF 1min/15min/60min 历史分钟';
    """.format(SOURCE_TABLE_MINS)
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()
    logger.info("表 %s 已就绪", SOURCE_TABLE_MINS)


def get_max_trade_time_mins(conn, ts_code: str, freq: str) -> Optional[datetime]:
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(trade_time) FROM `{}` WHERE ts_code = %s AND freq = %s".format(SOURCE_TABLE_MINS), (ts_code, freq))
        row = cur.fetchone()
    if row and row[0] is not None:
        val = row[0]
        return val if isinstance(val, datetime) else datetime.strptime(str(val)[:19], "%Y-%m-%d %H:%M:%S")
    return None


def save_minutes_rows(conn, rows: List[tuple]) -> None:
    if not rows:
        return
    sql = "INSERT IGNORE INTO `{}` (ts_code, trade_time, freq, open, close, high, low, vol, amount) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)".format(SOURCE_TABLE_MINS)
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    logger.info("分钟写入 %d 行到 %s", len(rows), SOURCE_TABLE_MINS)


def _df_to_rows_mins(ts_code: str, freq: str, df: pd.DataFrame) -> List[tuple]:
    if df is None or df.empty or "trade_time" not in df.columns:
        return []
    rows = []
    for _, row in df.iterrows():
        rows.append((
            ts_code, str(row["trade_time"]), freq,
            float(row["open"]) if row.get("open") is not None else None,
            float(row["close"]) if row.get("close") is not None else None,
            float(row["high"]) if row.get("high") is not None else None,
            float(row["low"]) if row.get("low") is not None else None,
            int(row["vol"]) if row.get("vol") is not None else None,
            float(row["amount"]) if row.get("amount") is not None else None,
        ))
    return rows


def get_pro():
    ts.set_token(TUSHARE_TOKEN)
    return ts.pro_api(TUSHARE_TOKEN)


def _iter_1min_daily_chunks(start_date: str, end_date: str):
    """1min：按自然日切片（每日约 09:00～19:00），按代码逐日请求，远低于 stk_mins 单次 8000 行上限。"""
    start = datetime.strptime(start_date[:19], "%Y-%m-%d %H:%M:%S")
    end = datetime.strptime(end_date[:19], "%Y-%m-%d %H:%M:%S")
    if start > end:
        return
    d = start.date()
    end_d = end.date()
    while d <= end_d:
        day_open = datetime(d.year, d.month, d.day, 9, 0, 0)
        day_close = datetime(d.year, d.month, d.day, 19, 0, 0)
        seg_lo = max(start, day_open)
        seg_hi = min(end, day_close)
        if seg_lo <= seg_hi:
            yield seg_lo.strftime("%Y-%m-%d %H:%M:%S"), seg_hi.strftime("%Y-%m-%d %H:%M:%S")
        d += timedelta(days=1)


def _fetch_stk_mins_to_rows(pro, ts_code: str, freq: str, start_date: str, end_date: str) -> Optional[List[tuple]]:
    """单次 stk_mins；失败返回 None；无数据返回 []。"""
    if _stop_flag:
        return None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            df = pro.stk_mins(ts_code=ts_code, freq=freq, start_date=start_date, end_date=end_date)
            time.sleep(REQUEST_INTERVAL)
            if df is None or df.empty:
                return []
            if "trade_time" not in df.columns:
                return []
            return _df_to_rows_mins(ts_code, freq, df)
        except Exception as e:
            logger.warning("stk_mins 失败 ts_code=%s freq=%s %s~%s 尝试 %d/%d: %s", ts_code, freq, start_date, end_date, attempt, MAX_RETRY, e)
            time.sleep(REQUEST_INTERVAL * attempt)
    return None


def fetch_etf_minutes_year(pro, ts_code: str, freq: str, year: int) -> Optional[List[tuple]]:
    if _stop_flag:
        return None
    start_date = f"{year}-01-01 09:00:00"
    end_date = f"{year}-12-31 19:00:00"
    if freq == "1min":
        acc: List[tuple] = []
        logger.info(
            "1min 拉取中 ts_code=%s year=%d（按自然日约 365 次/年，中间可能静默数分钟；每 30 天打一次进度）",
            ts_code,
            year,
        )
        for day_idx, (ds, de) in enumerate(_iter_1min_daily_chunks(start_date, end_date), start=1):
            if _stop_flag:
                return None
            part = _fetch_stk_mins_to_rows(pro, ts_code, freq, ds, de)
            if part is None:
                return None
            acc.extend(part)
            if day_idx % 30 == 0:
                logger.info("1min 进度 ts_code=%s year=%d 已处理 %d 个日分片 累计 %d 条", ts_code, year, day_idx, len(acc))
        logger.info("分钟 ts_code=%s freq=%s year=%d 获取 %d 条（按日分片）", ts_code, freq, year, len(acc))
        return acc
    rows = _fetch_stk_mins_to_rows(pro, ts_code, freq, start_date, end_date)
    if rows is not None:
        logger.info("分钟 ts_code=%s freq=%s year=%d 获取 %d 条", ts_code, freq, year, len(rows))
    return rows


def fetch_etf_minutes_range(pro, ts_code: str, freq: str, start_date: str, end_date: str) -> Optional[List[tuple]]:
    if _stop_flag:
        return None
    if freq == "1min":
        acc: List[tuple] = []
        for day_idx, (ds, de) in enumerate(_iter_1min_daily_chunks(start_date, end_date), start=1):
            if _stop_flag:
                return None
            part = _fetch_stk_mins_to_rows(pro, ts_code, freq, ds, de)
            if part is None:
                return None
            acc.extend(part)
            if day_idx % 30 == 0:
                logger.info("1min 进度(增量) ts_code=%s 已处理 %d 个日分片 累计 %d 条", ts_code, day_idx, len(acc))
        return acc
    return _fetch_stk_mins_to_rows(pro, ts_code, freq, start_date, end_date)


def run_mins_import(conn, full_import: bool = False, freqs: Optional[List[str]] = None) -> int:
    """分钟数据导入，返回本次写入总行数。freqs 默认 FREQ_LIST（1min/15min/60min）。"""
    use_freqs = freqs if freqs is not None else list(FREQ_LIST)
    logger.info("---------- 分钟导入（etf_stk_mins）周期: %s ----------", ",".join(use_freqs))
    log_phase("分钟导入", "标的×周期 双层循环，周期={}".format(",".join(use_freqs)))
    if full_import and "1min" in use_freqs:
        logger.info("提示：1min 全量按日请求，单标的单年约数百次接口调用，终端长时间无输出时请看同目录日志或下方「1min 进度」行")
    create_table_mins_if_not_exists(conn)
    pro = get_pro()
    current_year = datetime.now().year
    today_end = datetime.now().strftime("%Y-%m-%d 19:00:00")
    total_rows = 0
    for ts_code in ETF_CODES:
        if _stop_flag:
            break
        for freq in use_freqs:
            if _stop_flag:
                break
            if full_import:
                for year in range(FULL_START_YEAR, current_year + 1):
                    if _stop_flag:
                        break
                    rows = fetch_etf_minutes_year(pro, ts_code, freq, year)
                    if rows is None:
                        break
                    if rows:
                        save_minutes_rows(conn, rows)
                        total_rows += len(rows)
                continue
            max_t = get_max_trade_time_mins(conn, ts_code, freq)
            if max_t is not None:
                start_dt = max_t + timedelta(days=1)
                start_str = start_dt.strftime("%Y-%m-%d 09:00:00")
                if start_str <= today_end:
                    rows = fetch_etf_minutes_range(pro, ts_code, freq, start_str, today_end)
                    if rows is not None and rows:
                        save_minutes_rows(conn, rows)
                        total_rows += len(rows)
                continue
            for year in range(FULL_START_YEAR, current_year + 1):
                if _stop_flag:
                    break
                rows = fetch_etf_minutes_year(pro, ts_code, freq, year)
                if rows is None:
                    break
                if rows:
                    save_minutes_rows(conn, rows)
                    total_rows += len(rows)
    if total_rows == 0:
        logger.info("分钟导入完成：无新数据（已是最新）")
    else:
        logger.info("分钟导入完成：本次共写入 %d 行", total_rows)
    return total_rows


_OPTIONAL_COLUMNS_MINS = [
    ("close_qfq", "DECIMAL(16,4)", "前复权收盘(close*adj/adj最新)"),
    ("ma10", "DECIMAL(10,4)", "10周期均线"),
    ("bias10ma", "DECIMAL(10,4)", "相对10均线乖离"),
    ("per_bias10ma", "DECIMAL(10,2)", "相对10均线乖离百分位"),
    ("ma120", "DECIMAL(10,4)", "120周期均线"),
    ("ema120", "DECIMAL(10,4)", "120周期指数均线"),
    (
        "mcd_pct",
        "DECIMAL(8,4)",
        "聚集度：六线标准差/价格×100（std(ddof=0)/price*100）",
    ),
    (
        "per_mcd",
        "DECIMAL(10,2)",
        "mcd_pct滚动百分位（15min:850,60min:500）",
    ),
]


def ensure_close_qfq_column_mins_ma(conn) -> None:
    existing = set(get_table_columns(conn, TARGET_TABLE_MINS))
    if "close_qfq" in existing:
        return
    sql = (
        "ALTER TABLE `{}` ADD COLUMN `close_qfq` DECIMAL(16,4) DEFAULT NULL COMMENT '前复权收盘'".format(TARGET_TABLE_MINS)
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    logger.info("已添加列 %s.close_qfq", TARGET_TABLE_MINS)


def ensure_optional_columns_mins(conn) -> List[str]:
    existing = set(get_table_columns(conn, TARGET_TABLE_MINS))
    added_cols: List[str] = []
    for col_name, col_type, comment in _OPTIONAL_COLUMNS_MINS:
        if col_name in existing:
            continue
        sql = "ALTER TABLE `{}` ADD COLUMN `{}` {} DEFAULT NULL COMMENT '{}'".format(TARGET_TABLE_MINS, col_name, col_type, comment)
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        added_cols.append(col_name)
        logger.info("已添加列 %s.%s", TARGET_TABLE_MINS, col_name)
    return added_cols


def get_replace_columns_mins() -> List[str]:
    return (
        ["ts_code", "trade_time", "freq", "close_qfq"]
        + ["ma10", "ma20", "ma60", "ma120", "ema20", "ema60", "ema120"]
        + ["bias10ma", "bias20ma", "bias60ma", "bias20ema", "bias60ema"]
        + ["per_bias10ma", "per_bias20ma", "per_bias60ma", "per_bias20ema", "per_bias60ema"]
        + ["mcd_pct", "per_mcd"]
    )


def align_columns_to_table(df: pd.DataFrame, table_cols: List[str], expected: List[str]) -> Tuple[pd.DataFrame, List[str]]:
    table_lower = {c.lower(): c for c in table_cols}
    rename = {}
    for e in expected:
        if e in table_cols:
            continue
        if e.lower() in table_lower:
            rename[e] = table_lower[e.lower()]
    if rename:
        df = df.rename(columns=rename)
    write_cols = [c for c in table_cols if c in df.columns]
    return df[write_cols], write_cols


def get_max_trade_time_per_group_mins(conn) -> Dict[Tuple[str, str], datetime]:
    sql = "SELECT ts_code, freq, MAX(trade_time) AS max_time FROM `{}`.{} GROUP BY ts_code, freq".format(DB_CONFIG["database"], TARGET_TABLE_MINS)
    df = pd.read_sql(sql, conn)
    if df.empty or df["max_time"].isna().all():
        return {}
    return {(row["ts_code"], row["freq"]): row["max_time"] for _, row in df.iterrows() if pd.notna(row["max_time"])}


def ensure_adj_state_table_mins(conn) -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS `{}` (
      `ts_code` VARCHAR(20) NOT NULL,
      `adj_latest` DECIMAL(20,10) DEFAULT NULL,
      `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (`ts_code`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='分钟MA复权因子状态';
    """.format(ADJ_STATE_TABLE_MINS)
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()


def load_adj_state_map_mins(conn) -> Dict[str, float]:
    sql = "SELECT ts_code, adj_latest FROM `{}`".format(ADJ_STATE_TABLE_MINS)
    df = pd.read_sql(sql, conn)
    if df.empty:
        return {}
    state: Dict[str, float] = {}
    for _, row in df.iterrows():
        ts_code = str(row["ts_code"])
        val = pd.to_numeric(row["adj_latest"], errors="coerce")
        if pd.notna(val):
            state[ts_code] = float(val)
    return state


def save_adj_state_map_mins(conn, state_map: Dict[str, float]) -> None:
    if not state_map:
        return
    sql = (
        "REPLACE INTO `{}` (ts_code, adj_latest, updated_at) "
        "VALUES (%s, %s, NOW())"
    ).format(ADJ_STATE_TABLE_MINS)
    data = [(k, v) for k, v in state_map.items() if v is not None and not np.isnan(v)]
    if not data:
        return
    with conn.cursor() as cur:
        cur.executemany(sql, data)
    conn.commit()


def load_mins(conn) -> pd.DataFrame:
    sql = "SELECT ts_code, trade_time, freq, close FROM `{}`.{} WHERE close IS NOT NULL AND close > 0 ORDER BY ts_code, freq, trade_time".format(DB_CONFIG["database"], SOURCE_TABLE_MINS)
    return pd.read_sql(sql, conn)


def _strip_open_anchor_bar_mins(df: pd.DataFrame) -> pd.DataFrame:
    """
    写入 etf_stk_mins_ma 前：剔除每日 09:30 这根 K（1min/15min/60min 一律不进入结果表）。
    15/60min 同时满足规则 K（避免锚点参与 rolling 导致根数与 REF 错位）；1min 按业务要求也不落库 09:30。
    匹配规则：trade_time 的时=9、分=30（秒可为 0）。
    """
    if df.empty or "trade_time" not in df.columns:
        return df
    out = df.copy()
    ts = pd.to_datetime(out["trade_time"], errors="coerce")
    is0930 = (ts.dt.hour == 9) & (ts.dt.minute == 30)
    is0930 = is0930.fillna(False)
    if is0930.any():
        out = out.loc[~is0930].reset_index(drop=True)
    return out


def compute_ma_ema_mins(df: pd.DataFrame) -> pd.DataFrame:
    """以 close_qfq 为主序列算 MA/EMA/BIAS；若无有效 close_qfq 则退回 close。已剔除各周期 09:30，不写库。"""
    df = df.sort_values("trade_time").reset_index(drop=True)
    df = _strip_open_anchor_bar_mins(df)
    close_raw = pd.to_numeric(df["close"], errors="coerce")
    if "close_qfq" in df.columns:
        px = pd.to_numeric(df["close_qfq"], errors="coerce")
    else:
        px = pd.Series(np.nan, index=df.index)
    fallback_count = int((px.isna() & close_raw.notna()).sum())
    if px.isna().all():
        px = close_raw
        fallback_count = int(px.notna().sum())
    df = df.assign(_px=px)
    df = df[df["_px"].notna()].copy()
    if df.empty:
        return pd.DataFrame()
    # 过滤 df 之后，需要保证分母序列与当前 df 行索引对齐，避免对齐错位导致 mcd_pct 异常
    close_raw = close_raw.reindex(df.index)
    for p in MA_PERIODS_MINS:
        df["ma{}".format(p)] = df["_px"].rolling(window=p, min_periods=p).mean()
    for p in EMA_PERIODS_MINS:
        df["ema{}".format(p)] = df["_px"].ewm(span=p, adjust=False).mean()
    for p in BIAS_MA_PERIODS_MINS:
        col_ma = "ma{}".format(p)
        df["bias{}ma".format(p)] = np.where((df[col_ma].notna()) & (df[col_ma] != 0), (df["_px"] - df[col_ma]) / df[col_ma] * 100.0, float("nan"))
    for p in BIAS_EMA_PERIODS_MINS:
        col_ema = "ema{}".format(p)
        df["bias{}ema".format(p)] = np.where((df[col_ema].notna()) & (df[col_ema] != 0), (df["_px"] - df[col_ema]) / df[col_ema] * 100.0, float("nan"))
    bias_cols = ["bias{}ma".format(p) for p in BIAS_MA_PERIODS_MINS] + ["bias{}ema".format(p) for p in BIAS_EMA_PERIODS_MINS]
    for col in bias_cols:
        df["per_" + col] = _rolling_percentile_series(df[col], PER_BIAS_WINDOW_MINS)

    # ------------------------------
    # mcd_pct（聚集度）：仅计算 15min/60min
    # 六线 = ma20, ema20, ma60, ema60, ma120, ema120
    # mcd_pct = std(ddof=0) / price * 100
    # price 分母：close_qfq；不存在/<=0 则回退 close
    # 任一六线为空则 mcd_pct 输出为空（与回测 out.where(all_ok) 对齐）
    # ------------------------------
    freq_val = str(df["freq"].iloc[0]) if "freq" in df.columns and len(df) else ""
    if freq_val in {"15min", "60min"}:
        denom = df["close_qfq"] if "close_qfq" in df.columns else pd.Series(np.nan, index=df.index)
        valid_denom = denom.notna() & (denom > 0)
        denom = denom.where(valid_denom, close_raw)
        valid_denom2 = denom.notna() & (denom > 0)

        six = df.loc[:, ["ma20", "ema20", "ma60", "ema60", "ma120", "ema120"]].apply(
            pd.to_numeric, errors="coerce"
        )
        all_ok = six.notna().all(axis=1)
        std = six.std(axis=1, ddof=0)
        mcd_pct = (std / denom) * 100.0
        df["mcd_pct"] = mcd_pct.where(all_ok & valid_denom2)
    else:
        df["mcd_pct"] = float("nan")

    # per_mcd：按频率使用不同滚动窗口
    if freq_val == "15min":
        df["per_mcd"] = _rolling_percentile_series(df["mcd_pct"], PER_MCD_WINDOW_15MIN)
    elif freq_val == "60min":
        df["per_mcd"] = _rolling_percentile_series(df["mcd_pct"], PER_MCD_WINDOW_60MIN)
    else:
        df["per_mcd"] = float("nan")
    out_cols = (
        ["ts_code", "trade_time", "freq", "close_qfq"]
        + ["ma10", "ma20", "ma60", "ma120", "ema20", "ema60", "ema120"]
        + ["bias10ma", "bias20ma", "bias60ma", "bias20ema", "bias60ema"]
        + ["per_bias10ma", "per_bias20ma", "per_bias60ma", "per_bias20ema", "per_bias60ema"]
        + ["mcd_pct", "per_mcd"]
    )
    df["close_qfq"] = df["_px"]
    result = df[out_cols].copy()
    result.attrs["fallback_to_close_count"] = fallback_count
    for c in result.columns:
        if c in ("ts_code", "freq", "trade_time"):
            continue
        result[c] = result[c].round(2) if c.startswith("per_") else result[c].round(4)
    return result


def _per_mcd_window_for_freq(freq: str) -> Optional[int]:
    if freq == "15min":
        return PER_MCD_WINDOW_15MIN
    if freq == "60min":
        return PER_MCD_WINDOW_60MIN
    return None


def _batch_update_per_mcd(conn, rows: List[Tuple[Any, Any, Any, Any]]) -> int:
    if not rows:
        return 0
    sql = (
        "UPDATE `{}` SET per_mcd = %s "
        "WHERE ts_code = %s AND trade_time = %s AND freq = %s"
    ).format(TARGET_TABLE_MINS)
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH_SIZE_MINS):
            batch = rows[i : i + BATCH_SIZE_MINS]
            cur.executemany(sql, batch)
            total += len(batch)
    return total


def run_mins_per_mcd_step(conn, full_backfill: bool = False) -> None:
    """
    仅计算/回填 per_mcd（不重算 MA/EMA）：
    - full_backfill=True：全历史回填（一次性）
    - full_backfill=False：仅对 per_mcd 为空的组做增量回填
    """
    mode = "全量" if full_backfill else "增量"
    logger.info("---------- 分钟 per_mcd 计算（%s）----------", mode)

    groups: List[Tuple[str, str, Optional[Any]]]
    if full_backfill:
        sql_groups = (
            "SELECT DISTINCT ts_code, freq FROM `{}` "
            "WHERE freq IN ('15min','60min') ORDER BY ts_code, freq"
        ).format(TARGET_TABLE_MINS)
        df_groups = pd.read_sql(sql_groups, conn)
        groups = [(str(r["ts_code"]), str(r["freq"]), None) for _, r in df_groups.iterrows()]
    else:
        sql_groups = (
            "SELECT ts_code, freq, MIN(trade_time) AS min_null_time "
            "FROM `{}` WHERE freq IN ('15min','60min') AND per_mcd IS NULL "
            "GROUP BY ts_code, freq ORDER BY ts_code, freq"
        ).format(TARGET_TABLE_MINS)
        df_groups = pd.read_sql(sql_groups, conn)
        groups = [(str(r["ts_code"]), str(r["freq"]), r["min_null_time"]) for _, r in df_groups.iterrows()]

    if not groups:
        logger.info("分钟 per_mcd：无需处理（%s）", mode)
        return

    total_updated = 0
    for ts_code, freq, min_null_time in groups:
        if _stop_flag:
            break
        win = _per_mcd_window_for_freq(freq)
        if not win:
            continue
        sql_data = (
            "SELECT ts_code, trade_time, freq, mcd_pct FROM `{}` "
            "WHERE ts_code=%s AND freq=%s ORDER BY trade_time"
        ).format(TARGET_TABLE_MINS)
        df = pd.read_sql(sql_data, conn, params=(ts_code, freq))
        if df.empty:
            continue
        mcd = pd.to_numeric(df["mcd_pct"], errors="coerce")
        per = _rolling_percentile_series(mcd, win).round(2)
        if full_backfill:
            mask = pd.Series(True, index=df.index)
        else:
            mt = pd.to_datetime(min_null_time, errors="coerce")
            if pd.isna(mt):
                continue
            t = pd.to_datetime(df["trade_time"], errors="coerce")
            mask = t >= mt
        upd_rows: List[Tuple[Any, Any, Any, Any]] = []
        for i in df.index[mask]:
            v = per.iloc[i]
            val = None if pd.isna(v) else float(v)
            upd_rows.append((val, df.at[i, "ts_code"], df.at[i, "trade_time"], df.at[i, "freq"]))
        if not upd_rows:
            continue
        n = _batch_update_per_mcd(conn, upd_rows)
        total_updated += n
    conn.commit()
    logger.info("分钟 per_mcd 计算完成（%s）：更新 %d 行", mode, total_updated)


def _compute_mins_one_group(
    ts_code: str,
    freq: str,
    g: pd.DataFrame,
    adj_df: pd.DataFrame,
    max_t: Optional[Any],
) -> Tuple[str, Optional[pd.DataFrame], int]:
    """
    单组 (ts_code, freq) 分钟 MA 计算。
    max_t 为 None：全量输出；否则增量仅保留 trade_time > max_t，且拼接 LOOKBACK_BARS_MINS 根历史
    以满足 ma120 与 per_bias 窗口（与全量算法同一套 rolling 口径）。
    返回：(mode, df|None, fallback 行数)；mode ∈ full / incr / skip。
    """
    if _stop_flag:
        return ("skip", None, 0)
    if adj_df is None:
        adj_df = pd.DataFrame()
    g = g.copy()
    g["close_qfq"] = attach_close_qfq_series(g, "trade_time", "close", adj_df)

    if max_t is None:
        res = compute_ma_ema_mins(g)
        if res.empty:
            return ("skip", None, 0)
        fb = int(res.attrs.get("fallback_to_close_count", 0))
        return ("full", res, fb)

    group_sorted = g.sort_values("trade_time").reset_index(drop=True)
    lookback = group_sorted[group_sorted["trade_time"] <= max_t].tail(LOOKBACK_BARS_MINS)
    new_rows = group_sorted[group_sorted["trade_time"] > max_t]
    if new_rows.empty:
        return ("skip", None, 0)
    combined = pd.concat([lookback, new_rows], ignore_index=True).drop_duplicates(subset=["trade_time"]).sort_values("trade_time").reset_index(drop=True)
    combined["close_qfq"] = attach_close_qfq_series(combined, "trade_time", "close", adj_df)
    res = compute_ma_ema_mins(combined)
    out = res[res["trade_time"] > max_t].copy()
    if out.empty:
        return ("skip", None, 0)
    fb = int(res.attrs.get("fallback_to_close_count", 0))
    return ("incr", out, fb)


def batch_replace_mins(conn, df: pd.DataFrame, cols: List[str]) -> Tuple[int, bool]:
    """
    REPLACE 分批写入。返回 (已提交行数, 是否因中断而未完整提交)。
    中断时 rollback，避免半套数据配全套 adj_state。
    """
    if df.empty or not cols:
        return 0, False
    ph = ",".join(["%s"] * len(cols))
    sql = "REPLACE INTO `{}`.{} ({}) VALUES ({})".format(DB_CONFIG["database"], TARGET_TABLE_MINS, ",".join(cols), ph)

    def to_sql_val(v):
        return None if (v is None or (isinstance(v, float) and pd.isna(v))) else v

    n_rows = len(df)
    logger.info("分钟 MA 写库：组装 %d 行参数（大表需数分钟）…", n_rows)
    t_asm = time.perf_counter()
    sub = df.loc[:, cols]
    # itertuples 比 iterrows 快一个数量级，避免 700 万行长时间无输出
    data = [tuple(to_sql_val(x) for x in row) for row in sub.itertuples(index=False, name=None)]
    logger.info("分钟 MA 写库：参数组装完成，耗时 %.1f 秒", time.perf_counter() - t_asm)
    total = 0
    n_batches = (len(data) + BATCH_SIZE_MINS - 1) // BATCH_SIZE_MINS
    log_every = max(1, min(MINS_REPLACE_LOG_MIN_BATCHES, n_batches // 15 or 1))
    t_exec = time.perf_counter()
    interrupted = False
    with conn.cursor() as cur:
        for bi, i in enumerate(range(0, len(data), BATCH_SIZE_MINS)):
            if _stop_flag:
                interrupted = True
                logger.warning("分钟 MA 写库：收到中断，本批前停止写入并回滚未提交批次")
                break
            batch = data[i : i + BATCH_SIZE_MINS]
            cur.executemany(sql, batch)
            total += len(batch)
            nb = bi + 1
            if nb == 1 or nb == n_batches or nb % log_every == 0:
                logger.info(
                    "分钟 MA 写库 %d/%d 批 | %d/%d 行 | %.1fs",
                    nb,
                    n_batches,
                    total,
                    len(data),
                    time.perf_counter() - t_exec,
                )
    if interrupted:
        conn.rollback()
        return 0, True
    conn.commit()
    return total, False


def create_table_mins_ma_if_not_exists(conn) -> None:
    """创建 etf_stk_mins_ma 表（不存在时），与 REPLACE 列一致。"""
    ddl = """
    CREATE TABLE IF NOT EXISTS `{}` (
      `ts_code`    VARCHAR(20)  NOT NULL,
      `trade_time` DATETIME     NOT NULL,
      `freq`       VARCHAR(10)  NOT NULL,
      `close_qfq`  DECIMAL(16,4) DEFAULT NULL COMMENT '前复权收盘',
      `ma10`       DECIMAL(10,4) DEFAULT NULL,
      `ma20`       DECIMAL(10,4) DEFAULT NULL,
      `ma60`       DECIMAL(10,4) DEFAULT NULL,
      `ma120`      DECIMAL(10,4) DEFAULT NULL,
      `ema20`      DECIMAL(10,4) DEFAULT NULL,
      `ema60`      DECIMAL(10,4) DEFAULT NULL,
      `ema120`     DECIMAL(10,4) DEFAULT NULL,
      `bias10ma`   DECIMAL(10,4) DEFAULT NULL,
      `bias20ma`   DECIMAL(10,4) DEFAULT NULL,
      `bias60ma`   DECIMAL(10,4) DEFAULT NULL,
      `bias20ema`  DECIMAL(10,4) DEFAULT NULL,
      `bias60ema`  DECIMAL(10,4) DEFAULT NULL,
      `per_bias10ma` DECIMAL(10,2) DEFAULT NULL,
      `per_bias20ma` DECIMAL(10,2) DEFAULT NULL,
      `per_bias60ma` DECIMAL(10,2) DEFAULT NULL,
      `per_bias20ema` DECIMAL(10,2) DEFAULT NULL,
      `per_bias60ema` DECIMAL(10,2) DEFAULT NULL,
      `mcd_pct`    DECIMAL(8,4)  DEFAULT NULL COMMENT '聚集度：六线标准差/价格×100（std(ddof=0)/price*100）',
      `per_mcd`    DECIMAL(10,2) DEFAULT NULL COMMENT 'mcd_pct滚动百分位（15min:850,60min:500）',
      PRIMARY KEY (`ts_code`,`trade_time`,`freq`),
      KEY `idx_trade_time` (`trade_time`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ETF分钟MA/EMA/BIAS';
    """.format(TARGET_TABLE_MINS)
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()
    logger.info("表 %s 已就绪", TARGET_TABLE_MINS)


def run_mins_ma_step(conn, full_ma: bool = False, ma_workers: int = 0) -> None:
    logger.info("---------- 分钟 MA/EMA 计算（写入 etf_stk_mins_ma）----------")
    log_phase("分钟MA", "准备：建目标表、adj_state、补列、读取目标表列信息…")
    create_table_mins_ma_if_not_exists(conn)
    ensure_adj_state_table_mins(conn)
    ensure_close_qfq_column_mins_ma(conn)
    added_cols = ensure_optional_columns_mins(conn)
    expected = get_replace_columns_mins()
    table_cols = get_table_columns(conn, TARGET_TABLE_MINS)
    log_phase("分钟MA", "准备完成，目标表列数=%d" % len(table_cols))
    per_mcd_added = "per_mcd" in added_cols
    other_added_cols = [c for c in added_cols if c != "per_mcd"]
    should_force_full = full_ma or bool(other_added_cols)
    if not should_force_full:
        log_phase("分钟MA", "查询 etf_stk_mins_ma 各 (ts_code,freq) 的最新 trade_time…")
    max_time_map = {} if should_force_full else get_max_trade_time_per_group_mins(conn)
    if added_cols:
        logger.info("分钟表新增列：%s", ",".join(added_cols))
    if other_added_cols:
        logger.info("新增列含 MA 相关字段：%s；本次自动全量重算回填历史", ",".join(other_added_cols))
    elif per_mcd_added and not full_ma:
        logger.info("仅新增 per_mcd：跳过 MA 全量重算，改走 per_mcd 全历史回填")
    if max_time_map and not should_force_full:
        logger.info("分钟计算增量：%d 个 (ts_code,freq)", len(max_time_map))
    else:
        logger.info("分钟计算全量")
    t_load = time.perf_counter()
    log_phase("分钟MA", "从 {} 加载全部分钟 close（全表扫描+排序，可能较久）…".format(SOURCE_TABLE_MINS))
    df_all = load_mins(conn)
    logger.info("分钟源表加载完成：%d 行，耗时 %.1f 秒", len(df_all), time.perf_counter() - t_load)
    if df_all.empty:
        logger.warning("分钟 MA 完成：源表 %s 无有效 close，跳过计算", SOURCE_TABLE_MINS)
        if per_mcd_added:
            run_mins_per_mcd_step(conn, full_backfill=True)
        return
    t_adj = time.perf_counter()
    log_phase("分钟MA", "加载各标的 etf_fund_adj（{} 只）用于前复权…".format(len(ETF_CODES)))
    adj_by_ts = {code: load_adj_dataframe(conn, code) for code in ETF_CODES}
    log_phase("分钟MA", "复权表加载完成，耗时 {:.1f} 秒；读取分钟 MA 复权状态…".format(time.perf_counter() - t_adj))
    prev_adj_state = load_adj_state_map_mins(conn)
    current_adj_state: Dict[str, float] = {}
    force_full_codes = set()
    force_full_reasons: Dict[str, str] = {}
    for ts_code, adj_df in adj_by_ts.items():
        cur_adj = adj_latest_value(adj_df)
        if cur_adj is None:
            continue
        current_adj_state[ts_code] = cur_adj
        prev_adj = prev_adj_state.get(ts_code)
        if prev_adj is not None and abs(cur_adj - prev_adj) > 1e-12:
            force_full_codes.add(ts_code)
            force_full_reasons[ts_code] = "adj_changed"
    # 历史上曾用「缺 adj_state → 全量」兜底，但若上次已算完 MA 仅因崩溃未 save，会误触发 700 万行重算。
    # 缺状态只打日志，运行结束时统一 save_adj_state_map_mins 补写即可；真正全量仅复权变化或新增列。
    missing_adj_state: list[str] = []
    if not should_force_full and max_time_map:
        history_codes = {k[0] for k in max_time_map.keys()}
        for ts_code in sorted(history_codes):
            if ts_code not in prev_adj_state:
                missing_adj_state.append(ts_code)
    if missing_adj_state:
        logger.info(
            "分钟 adj_state 未记录（本次不触发全量，运行结束会补写当前复权因子）：%s",
            ",".join(missing_adj_state),
        )
    if force_full_codes:
        logger.info("检测到复权因子变化，以下标的执行分钟全量重算：%s", ",".join(sorted(force_full_codes)))
    t_grp = time.perf_counter()
    _mg = df_all.groupby(["ts_code", "freq"], sort=False)
    _n_mg = _mg.ngroups
    log_phase("分钟MA", "分组并复制 {} 个 (ts_code,freq) 子表（大表可能需数分钟，下方会报进度）…".format(_n_mg))
    tasks: List[Tuple[str, str, pd.DataFrame, pd.DataFrame, Optional[Any]]] = []
    _tick = max(1, _n_mg // 10)
    for _gi, ((ts_code, freq), group) in enumerate(_mg, start=1):
        if _stop_flag:
            break
        if _gi == 1 or _gi % _tick == 0 or _gi == _n_mg:
            log_phase("分钟MA", "分组复制进度 {}/{}：当前 {} {}（{} 行）".format(_gi, _n_mg, ts_code, freq, len(group)))
        adj_df = adj_by_ts.get(ts_code)
        if adj_df is None:
            adj_df = pd.DataFrame()
        group_force_full = ts_code in force_full_codes
        max_t = None if (should_force_full or group_force_full) else max_time_map.get((ts_code, freq))
        tasks.append((ts_code, freq, group.copy(), adj_df, max_t))
    logger.info("分钟 MA 任务就绪：%d 组，分组+复制耗时 %.1f 秒", len(tasks), time.perf_counter() - t_grp)

    n_workers = min(_resolve_mins_ma_workers(ma_workers), max(1, len(tasks)), 32)
    if n_workers > 1 and len(tasks) > 1:
        log_phase(
            "分钟MA",
            "pandas 指标计算：并行 workers=%d，任务数=%d（全量组单任务可能极慢，完成顺序不定）".format(n_workers, len(tasks)),
        )
    else:
        log_phase("分钟MA", "pandas 指标计算：串行，共 {} 组".format(len(tasks)))

    results: List[pd.DataFrame] = []
    n_full, n_incr = 0, 0
    fallback_total = 0

    def _collect(mode: str, res_df: Optional[pd.DataFrame], fb: int) -> None:
        nonlocal n_full, n_incr, fallback_total
        if mode == "skip" or res_df is None or res_df.empty:
            return
        results.append(res_df)
        fallback_total += fb
        if mode == "full":
            n_full += 1
        elif mode == "incr":
            n_incr += 1

    if n_workers <= 1 or len(tasks) <= 1:
        for idx, t in enumerate(tasks, start=1):
            if _stop_flag:
                break
            ts_code, freq = t[0], t[1]
            logger.info("分钟 MA %d/%d 开始 %s %s", idx, len(tasks), ts_code, freq)
            t0 = time.perf_counter()
            mode, res_df, fb = _compute_mins_one_group(*t)
            logger.info(
                "分钟 MA %d/%d 完成 %s %s | %s | 行=%d | %.1fs",
                idx,
                len(tasks),
                ts_code,
                freq,
                mode,
                0 if res_df is None else len(res_df),
                time.perf_counter() - t0,
            )
            _collect(mode, res_df, fb)
    else:
        prog_lock = threading.Lock()
        done_cnt = [0]

        def _log_progress(ts_code: str, freq: str, mode: str, n_out: int, elapsed: float) -> None:
            with prog_lock:
                done_cnt[0] += 1
                k = done_cnt[0]
            logger.info(
                "分钟 MA %d/%d 完成 %s %s | %s | 行=%d | %.1fs",
                k,
                len(tasks),
                ts_code,
                freq,
                mode,
                n_out,
                elapsed,
            )

        # 不用「with + as_completed」无限等：每 0.5s 看 _stop_flag，与项目常驻脚本规则一致，Ctrl+C 后更快停下等待循环
        ex = ThreadPoolExecutor(max_workers=n_workers)
        future_to_task = {ex.submit(_compute_mins_one_group, *t): t for t in tasks}
        pending = set(future_to_task.keys())
        try:
            while pending:
                if _stop_flag:
                    logger.warning("分钟 MA：并行收到中断，取消未开始任务；已在跑的任务稍后结束")
                    break
                done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for fut in done:
                    t = future_to_task[fut]
                    ts_code, freq = t[0], t[1]
                    t0 = time.perf_counter()
                    try:
                        mode, res_df, fb = fut.result()
                    except Exception:
                        logger.exception("分钟 MA 单组计算失败：%s %s", ts_code, freq)
                        raise
                    n_out = 0 if res_df is None else len(res_df)
                    _log_progress(ts_code, freq, mode, n_out, time.perf_counter() - t0)
                    _collect(mode, res_df, fb)
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    if not results:
        save_adj_state_map_mins(conn, current_adj_state)
        logger.info("分钟 MA 完成：无新数据需计算（增量已是最新），已补写 adj_state")
        if per_mcd_added:
            run_mins_per_mcd_step(conn, full_backfill=True)
        else:
            run_mins_per_mcd_step(conn, full_backfill=False)
        return
    logger.info("分钟 MA：正在合并 %d 组计算结果…", len(results))
    t_cat = time.perf_counter()
    df_out = pd.concat(results, ignore_index=True)
    logger.info("分钟 MA：合并完成，共 %d 行，耗时 %.1f 秒", len(df_out), time.perf_counter() - t_cat)
    log_phase("分钟MA", "与表 {} 做列对齐，准备 REPLACE 写入…".format(TARGET_TABLE_MINS))
    df_aligned, write_cols = align_columns_to_table(df_out, table_cols, expected)
    if not write_cols:
        save_adj_state_map_mins(conn, current_adj_state)
        logger.warning("分钟 MA：表列与输出无交集，未写入；已补写 adj_state")
        return
    n, mins_write_interrupted = batch_replace_mins(conn, df_aligned, write_cols)
    if mins_write_interrupted:
        logger.warning("分钟 MA：写库未完整提交，已跳过 adj_state 更新（避免数据与复权状态不一致）")
        return
    save_adj_state_map_mins(conn, current_adj_state)
    logger.info("分钟 MA 完成：全量 %d 组、增量 %d 组，共写入 %d 行", n_full, n_incr, n)
    logger.info(
        "分钟 MA 审计：force_full_codes=%d, reasons=%s, write_rows=%d, fallback_rows=%d",
        len(force_full_codes),
        ",".join("{}:{}".format(k, v) for k, v in sorted(force_full_reasons.items())) if force_full_reasons else "none",
        n,
        fallback_total,
    )
    if per_mcd_added:
        run_mins_per_mcd_step(conn, full_backfill=True)
    else:
        run_mins_per_mcd_step(conn, full_backfill=False)


# ======================= 主入口 =======================

def main() -> None:
    parser = argparse.ArgumentParser(description="ETF 宽基盘后数据入库：日线 + 分钟，先导入后计算")
    parser.add_argument("--daily-only", action="store_true", help="仅执行日线流程（etf_daily 导入+MA）")
    parser.add_argument("--mins-only", action="store_true", help="仅执行分钟流程（etf_stk_mins 导入+etf_stk_mins_ma）")
    parser.add_argument("--skip-fetch", action="store_true", help="跳过导入，仅执行 MA 计算")
    parser.add_argument("--skip-ma", action="store_true", help="仅执行导入，不计算 MA")
    parser.add_argument("--full-import", action="store_true", help="全量导入（日线/分钟均从起始日期拉取）")
    parser.add_argument("--full-ma", action="store_true", help="全量计算（日线/分钟 MA 均全部重算）")
    parser.add_argument(
        "--mins-ma-workers",
        type=int,
        default=0,
        metavar="N",
        help="分钟 MA 并行线程数（每组 ts_code+freq 独立计算）；0=自动 min(CPU,8)，也可用环境变量 ETF_MINS_MA_WORKERS；REPLACE 写入仍为单连接",
    )
    parser.add_argument("--skip-fund-adj", action="store_true", help="不拉取/更新 etf_fund_adj（一般每日默认会增量同步）")
    parser.add_argument(
        "--freq",
        action="append",
        dest="freq_list",
        metavar="FREQ",
        choices=["1min", "15min", "60min"],
        help="仅导入指定分钟周期，可重复指定多个（如 --freq 1min）。不传则导入 1min+15min+60min",
    )
    args = parser.parse_args()

    run_daily = not args.mins_only   # 未指定 --mins-only 则跑日线
    run_mins = not args.daily_only   # 未指定 --daily-only 则跑分钟
    mins_freqs = list(dict.fromkeys(args.freq_list)) if args.freq_list else None

    logger.info(
        "ETF 宽基盘后数据入库 | %s | 日线:%s 分钟:%s | 导入:%s 计算:%s | fund_adj:%s | 分钟周期:%s",
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        "是" if run_daily else "否",
        "是" if run_mins else "否",
        "全量" if args.full_import else "增量",
        "全量" if args.full_ma else "增量",
        "跳过" if args.skip_fund_adj else ("全量" if args.full_import else "增量"),
        ",".join(mins_freqs) if mins_freqs else "默认(1min,15min,60min)",
    )
    try:
        conn = get_connection()
    except Exception as e:
        logger.exception("数据库连接失败: %s", e)
        sys.exit(1)
    log_phase("总流程", "MySQL 已连接 {}".format(DB_CONFIG.get("database", "")))
    try:
        if (run_daily or run_mins) and not args.skip_fund_adj:
            log_phase("总流程", "同步 etf_fund_adj（{}）…".format("全量" if args.full_import else "增量"))
            create_table_fund_adj(conn)
            run_import_fund_adj(
                conn,
                ETF_CODES,
                full_import=args.full_import,
                stop_check=lambda: _stop_flag,
            )
            log_phase("总流程", "etf_fund_adj 同步结束")
        elif args.skip_fund_adj:
            log_phase("总流程", "已跳过 etf_fund_adj（--skip-fund-adj）")
        if _stop_flag:
            logger.info("已中断")
            return
        if run_daily:
            if not args.skip_fetch:
                log_phase("总流程", "日线：拉取 fund_daily → etf_daily")
                run_daily_import(conn, full_import=args.full_import)
                log_phase("总流程", "日线：导入段结束")
            else:
                log_phase("总流程", "日线：已跳过导入（--skip-fetch）")
            if _stop_flag:
                logger.info("已中断")
                return
            if not args.skip_ma:
                run_daily_close_qfq(conn)
                run_daily_ma_step(conn, full_ma=args.full_ma)
                log_phase("总流程", "日线：指标段结束")
            else:
                log_phase("总流程", "日线：已跳过 MA（--skip-ma）")
        if _stop_flag:
            logger.info("已中断")
            return
        if run_mins:
            if not args.skip_fetch:
                log_phase("总流程", "分钟：拉取 stk_mins → etf_stk_mins")
                run_mins_import(conn, full_import=args.full_import, freqs=mins_freqs)
                log_phase("总流程", "分钟：导入段结束")
            else:
                log_phase("总流程", "分钟：已跳过导入（--skip-fetch）")
            if _stop_flag:
                logger.info("已中断")
                return
            if not args.skip_ma:
                run_mins_ma_step(conn, full_ma=args.full_ma, ma_workers=args.mins_ma_workers)
                log_phase("总流程", "分钟：MA 段结束")
            else:
                log_phase("总流程", "分钟：已跳过 MA（--skip-ma）")
    finally:
        conn.close()
    log_phase("总流程", "全部完成，连接已关闭")
    logger.info("脚本结束")


if __name__ == "__main__":
    main()

# 代码更新时间：2026-03-28 12:36
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
