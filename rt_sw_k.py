#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口：rt_sw_k（申万行业指数实时截面）
描述：盘中轮询拉取全市场申万指数最新行情，入库 stock_tushare.rt_sw_k（每指数一行，主键 ts_code）。
文档：https://tushare.pro/document/2?doc_id=417

更新方式：
  - 默认常驻：仅在 9:29～15:10 窗口内轮询；每次全量 rt_sw_k(fields=...) 后 REPLACE 写入
  - --once：单轮拉取写入后退出（便于手工/计划任务）
  - --create-only：仅建表后退出

注意：
  - 本接口为单独权限数据，频率以账号实际权限为准；可通过环境变量调间隔。
  - 全量 `rt_sw_k()` 当前约 180 个 `ts_code`；若数据源缩表，库内可能出现「已不再返回」的遗留行，需自行 TRUNCATE 或 DELETE 清理。
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from logging.handlers import TimedRotatingFileHandler
from typing import Any

import numpy as np
import pandas as pd
import pymysql
import tushare as ts

# ==================== 配置 ====================
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "").strip()
if not TUSHARE_TOKEN:
    raise SystemExit("缺少环境变量 TUSHARE_TOKEN")


DB_CONFIG: dict[str, Any] = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "stock_tushare"),
    "charset": "utf8mb4",
    "connect_timeout": 10,
    "read_timeout": 60,
    "write_timeout": 60,
}

TABLE_NAME = "rt_sw_k"
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rt_sw_k.log")

# 全量一次请求后，与通用 500 次/分兼容的保守间隔（秒）；权限更低时请调大
REQUEST_INTERVAL_SEC = float(os.getenv("RT_SW_K_REQUEST_INTERVAL", "0.2"))
# 交易窗口内：成功写入后的休眠（秒）+ 随机抖动上限，减轻无效请求
SLEEP_AFTER_SUCCESS_SEC = float(os.getenv("RT_SW_K_SLEEP_SUCCESS", "8"))
SLEEP_AFTER_SUCCESS_JITTER_MAX = float(os.getenv("RT_SW_K_SLEEP_JITTER", "4"))
SLEEP_AFTER_MISS_SEC = float(os.getenv("RT_SW_K_SLEEP_MISS", "5"))
SLEEP_NON_TRADING_SEC = float(os.getenv("RT_SW_K_SLEEP_NON_TRADING", "60"))

BATCH_SIZE = 500
MAX_RETRY = 3
RETRY_SLEEP = (2.0, 4.0, 8.0)

FIELDS = (
    "ts_code,name,trade_time,close,pre_close,high,open,low,vol,amount,pct_change"
)

VARCHAR_MAX_LEN = {"ts_code": 20, "name": 80}

DECIMAL_SPECS: dict[str, tuple[int, int]] = {
    "close": (10, 4),
    "pre_close": (10, 4),
    "high": (10, 4),
    "open": (10, 4),
    "low": (10, 4),
    "vol": (24, 0),
    "amount": (24, 2),
    "pct_change": (12, 4),
}

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
  `ts_code` varchar(20) NOT NULL COMMENT '指数代码',
  `name` varchar(80) DEFAULT NULL COMMENT '指数名称',
  `trade_time` datetime DEFAULT NULL COMMENT '交易时间',
  `close` decimal(10,4) DEFAULT NULL COMMENT '现价',
  `pre_close` decimal(10,4) DEFAULT NULL COMMENT '昨收',
  `high` decimal(10,4) DEFAULT NULL COMMENT '最高价',
  `open` decimal(10,4) DEFAULT NULL COMMENT '开盘价',
  `low` decimal(10,4) DEFAULT NULL COMMENT '最低价',
  `vol` decimal(24,0) DEFAULT NULL COMMENT '成交量（股）',
  `amount` decimal(24,2) DEFAULT NULL COMMENT '成交金额（元）',
  `pct_change` decimal(12,4) DEFAULT NULL COMMENT '涨跌幅',
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`ts_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='申万行业指数实时截面(rt_sw_k)';
"""


from windows_safe_log import WindowsSafeTimedRotatingFileHandler


running = True


def handleSignal(signum: int, frame: Any) -> None:
    global running
    running = False
    logging.warning("收到信号 %s，准备退出...", signum)


signal.signal(signal.SIGINT, handleSignal)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, handleSignal)


def setupLogging() -> None:
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = WindowsSafeTimedRotatingFileHandler(
        LOG_FILE, when="midnight", interval=1, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.suffix = "%Y%m%d"
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)


def isTradingTime9291510() -> bool:
    """轮询窗口 9:29～15:10（与 tushare-to-mysql 规则 I 一致）。"""
    now = datetime.now()
    t = now.time()
    return (
        t >= datetime.strptime("09:29", "%H:%M").time()
        and t <= datetime.strptime("15:10", "%H:%M").time()
    )


def sleepInterruptible(totalSec: float) -> None:
    """可中断休眠：便于 Ctrl+C 后尽快退出。
    remain 先算再 sleep，避免 while 与 sleep 之间时钟前移导致 ValueError。"""
    end = time.monotonic() + max(0.0, float(totalSec))
    while running:
        remain = end - time.monotonic()
        if remain <= 0:
            break
        time.sleep(min(1.0, remain))


def getConnection() -> pymysql.Connection:
    return pymysql.connect(**DB_CONFIG)


def createTableIfNotExists() -> None:
    conn = getConnection()
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        conn.commit()
        logging.info("表 %s 已就绪", TABLE_NAME)
    finally:
        conn.close()


def _decimalMaxAbs(m: int, d: int) -> Decimal:
    """非负上界：最大绝对值（对称用于有符号）。"""
    intDigits = m - d
    body = "9" * intDigits + ("." + "9" * d if d else "")
    return Decimal(body)


def dropUnusableRtSwKRows(df: pd.DataFrame) -> pd.DataFrame:
    """丢弃 close 缺失或 <=0 的截面，避免盘前 0/-100 覆盖可用行情。"""
    if df is None or df.empty or "close" not in df.columns:
        return df
    closeNum = pd.to_numeric(df["close"], errors="coerce")
    keep = closeNum.notna() & (closeNum > 0)
    dropped = int((~keep).sum())
    if dropped:
        logging.warning("rt_sw_k 丢弃不可用行 %s（close 缺失或<=0）", dropped)
    return df.loc[keep].copy()


def processDf(df: pd.DataFrame) -> pd.DataFrame:
    """类型归一：时间、五价四位小数、量额与涨跌幅量化、NaN→None。"""
    if df is None or df.empty:
        return df
    out = df.copy()
    priceCols = ["close", "pre_close", "high", "open", "low"]
    for c in priceCols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(
                lambda x: (
                    None
                    if pd.isna(x)
                    else float(
                        Decimal(str(x)).quantize(
                            Decimal("0.0001"), rounding=ROUND_HALF_UP
                        )
                    )
                )
            )
    if "trade_time" in out.columns:
        out["trade_time"] = pd.to_datetime(out["trade_time"], errors="coerce").map(
            lambda x: x.to_pydatetime() if pd.notna(x) else None
        )
    if "vol" in out.columns:
        out["vol"] = pd.to_numeric(out["vol"], errors="coerce").map(
            lambda x: (
                None
                if pd.isna(x)
                else int(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            )
        )
    if "amount" in out.columns:
        out["amount"] = pd.to_numeric(out["amount"], errors="coerce").map(
            lambda x: (
                None
                if pd.isna(x)
                else float(
                    Decimal(str(x)).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                )
            )
        )
    if "pct_change" in out.columns:
        out["pct_change"] = pd.to_numeric(out["pct_change"], errors="coerce").map(
            lambda x: (
                None
                if pd.isna(x)
                else float(
                    Decimal(str(x)).quantize(
                        Decimal("0.0001"), rounding=ROUND_HALF_UP
                    )
                )
            )
        )
    out = out.replace({np.nan: None})
    return out


def validateBeforeSave(df: pd.DataFrame) -> None:
    """规则 Q：超长/超界拒绝写入。"""
    if df is None or df.empty:
        return

    def charLen(v: Any) -> int:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return 0
        return len(str(v))

    for col, maxlen in VARCHAR_MAX_LEN.items():
        if col not in df.columns:
            continue
        lens = df[col].map(charLen)
        mx = int(lens.max() or 0)
        if mx > maxlen:
            rowLabel = lens.idxmax()
            logging.error(
                "validateBeforeSave: 列 %s 超长 max_len=%s row_idx=%s sample=%r",
                col,
                maxlen,
                rowLabel,
                df.loc[rowLabel, col] if rowLabel in df.index else None,
            )
            raise ValueError(f"列 {col} 超过 varchar({maxlen})")

    for col, (m, d) in DECIMAL_SPECS.items():
        if col not in df.columns:
            continue
        bound = _decimalMaxAbs(m, d)
        for i, v in enumerate(df[col].tolist()):
            if v is None:
                continue
            try:
                dv = Decimal(str(v)).copy_abs()
            except Exception:
                logging.error("validateBeforeSave: 列 %s 行 %s 非数值 %r", col, i, v)
                raise
            if dv > bound:
                logging.error(
                    "validateBeforeSave: 列 %s 行 %s 超界 value=%s bound=%s",
                    col,
                    i,
                    v,
                    bound,
                )
                raise ValueError(f"列 {col} 超出 decimal({m},{d})")


def saveData(df: pd.DataFrame) -> int:
    """REPLACE 批量写入（主键 ts_code）。"""
    if df is None or df.empty:
        return 0
    cols = [
        "ts_code",
        "name",
        "trade_time",
        "close",
        "pre_close",
        "high",
        "open",
        "low",
        "vol",
        "amount",
        "pct_change",
    ]
    use = df.reindex(columns=cols).copy()
    placeholders = ",".join(["%s"] * len(cols))
    colsQuoted = ",".join(f"`{c}`" for c in cols)
    sql = f"REPLACE INTO `{TABLE_NAME}` ({colsQuoted}) VALUES ({placeholders})"
    rows = [tuple(use.loc[i, c] for c in cols) for i in use.index]
    conn = getConnection()
    n = 0
    try:
        with conn.cursor() as cur:
            for off in range(0, len(rows), BATCH_SIZE):
                chunk = rows[off : off + BATCH_SIZE]
                cur.executemany(sql, chunk)
                n += len(chunk)
        conn.commit()
        logging.info("写入 %s 完成 %s 行", TABLE_NAME, n)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return n


def fetchRtSwK(pro: ts.pro_api) -> pd.DataFrame | None:
    """单次全量拉取申万实时截面（显式 fields）。"""
    df = pro.rt_sw_k(fields=FIELDS)
    if df is None:
        return None
    if isinstance(df, pd.DataFrame) and df.empty:
        return df
    return df


def fetchWithRetry(pro: ts.pro_api) -> pd.DataFrame | None:
    last_err: Exception | None = None
    for attempt in range(MAX_RETRY):
        try:
            df = fetchRtSwK(pro)
            time.sleep(REQUEST_INTERVAL_SEC)
            return df
        except Exception as e:
            last_err = e
            wait = RETRY_SLEEP[min(attempt, len(RETRY_SLEEP) - 1)]
            logging.error(
                "rt_sw_k 请求失败 attempt=%s/%s: %s，%.1fs 后重试",
                attempt + 1,
                MAX_RETRY,
                e,
                wait,
            )
            sleepInterruptible(wait)
    logging.error("rt_sw_k 连续失败: %s", last_err)
    return None


def runOneCycle(pro: ts.pro_api) -> bool:
    """
    一轮：拉取 → 处理 → 校验 → 写入。
    成功写入至少一行返回 True；任一步失败记日志并返回 False，不向上抛异常（常驻不因单轮故障退出）。
    """
    try:
        df = fetchWithRetry(pro)
        if df is None:
            return False
        if df.empty:
            logging.warning("rt_sw_k 返回空表，跳过写入")
            return False
        processed = processDf(df)
        if "ts_code" not in processed.columns:
            logging.error("rt_sw_k 返回缺少 ts_code 列，跳过写入")
            return False
        processed = processed.dropna(subset=["ts_code"])
        processed = processed[
            processed["ts_code"].map(lambda x: str(x).strip() != "")
        ]
        processed = processed.drop_duplicates(subset=["ts_code"], keep="last")
        processed = dropUnusableRtSwKRows(processed)
        if processed.empty:
            logging.warning("清洗后无有效 ts_code（或均为 close<=0），跳过写入")
            return False
        validateBeforeSave(processed)
        n = saveData(processed)
        return n > 0
    except Exception:
        logging.exception("runOneCycle 单轮失败（已吞并，常驻继续）")
        return False


def main() -> None:
    setupLogging()
    parser = argparse.ArgumentParser(description="申万 rt_sw_k 实时入库")
    parser.add_argument(
        "--once",
        action="store_true",
        help="单轮拉取写入后退出",
    )
    parser.add_argument(
        "--create-only",
        action="store_true",
        help="仅创建表后退出",
    )
    args = parser.parse_args()

    createTableIfNotExists()
    if args.create_only:
        logging.info("--create-only，退出")
        return

    pro = ts.pro_api(TUSHARE_TOKEN)

    if args.once:
        ok = runOneCycle(pro)
        logging.info("单轮结束 ok=%s", ok)
        return

    logging.info(
        "常驻启动：交易窗口 09:29-15:10 内轮询；成功间隔约 %.1f+%.1f 秒抖动",
        SLEEP_AFTER_SUCCESS_SEC,
        SLEEP_AFTER_SUCCESS_JITTER_MAX,
    )
    while running:
        try:
            if not isTradingTime9291510():
                sleepInterruptible(SLEEP_NON_TRADING_SEC)
                continue
            ok = runOneCycle(pro)
            if ok:
                jitter = random.uniform(0, SLEEP_AFTER_SUCCESS_JITTER_MAX)
                sleepInterruptible(SLEEP_AFTER_SUCCESS_SEC + jitter)
            else:
                sleepInterruptible(SLEEP_AFTER_MISS_SEC)
        except Exception:
            logging.exception("一轮失败，退避后继续")
            sleepInterruptible(SLEEP_AFTER_MISS_SEC)


if __name__ == "__main__":
    main()

# 代码更新时间：2026-05-14 15:58
# 代码更新时间：2026-09-02 09:28 丢弃 close<=0 的盘前脏截面，避免写入 -100%
# 代码更新时间：2026-09-08 15:11 sleepInterruptible 防 sleep 负数竞态；改用 monotonic
# 代码更新时间：2026-09-09 06:54 主循环单轮异常退避续跑（勿与 tusharert ENABLE_RT_SW_K 双开）
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
