#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口：report_rc（卖方盈利预测数据）
描述：获取券商每天研报的盈利预测数据，2010 年起，每晚 19~22 点更新当日。
     单次最多 3000 条，须 offset 分页。
数据库：stock_tushare
数据表：report_rc
更新方式：空表全量（FULL_START_DATE 起按自然日分段）/ 有数据增量（MAX(report_date) 回滚 N 日）
文档：https://tushare.pro/document/2?doc_id=292

环境变量：TUSHARE_TOKEN、DB_HOST、DB_PORT、DB_USER、DB_PASSWORD、DB_NAME（可选）
"""

from __future__ import annotations

import argparse
import datetime
import decimal
import logging
import re
import os
import signal
import sys
import time
from logging import Formatter, StreamHandler, getLogger
from logging.handlers import TimedRotatingFileHandler
from urllib.parse import quote_plus

import numpy as np
import pandas as pd
import tushare as ts
from sqlalchemy import create_engine, text

# ============================================================
# 日志（与 py 文件同名，按天轮转，保留 3 天；Windows 安全轮转）
# ============================================================
_LOG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(_LOG_DIR, "report_rc.log")
logger = getLogger("report_rc")
logger.setLevel(logging.INFO)
_fmt = Formatter("%(asctime)s %(levelname)s %(message)s")


from windows_safe_log import RetryTimedRotatingFileHandler as WindowsSafeTimedRotatingFileHandler


if not logger.handlers:
    _fh = WindowsSafeTimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=3, encoding="utf-8")
    _fh.setFormatter(_fmt)
    logger.addHandler(_fh)
    _sh = StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    logger.addHandler(_sh)


def _on_signal(sig, frame) -> None:
    logger.warning("收到中断信号，退出")
    sys.exit(0)


signal.signal(signal.SIGINT, _on_signal)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, _on_signal)


# ============================================================
# 配置
# ============================================================
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

TABLE_NAME = "report_rc"
BATCH_SIZE = 500
REQUEST_INTERVAL = 0.2
PAGE_SIZE = 3000

# ===== 用户配置区 =====
# 全量起始日（YYYYMMDD）；官方数据自 2010 年起
FULL_START_DATE = "20100101"
# 全量按自然日分段长度（段内靠 offset 分页取完）
CHUNK_CALENDAR_DAYS = 60
# 增量：自 MAX(report_date) 向前回滚自然日，覆盖修订/补录
INCREMENTAL_LOOKBACK_DAYS = 7
# ===== 用户配置区结束 =====

# 接口全部输出字段（规则 B）
FIELDS = (
    "ts_code,name,report_date,report_title,report_type,classify,org_name,author_name,"
    "quarter,op_rt,op_pr,tp,np,eps,pe,rd,roe,ev_ebitda,rating,max_price,min_price,"
    "imp_dg,create_time"
)

# 规则 Q：varchar 字符上限（utf8mb4 字符数）
VARCHAR_MAX_LEN: dict[str, int] = {
    "ts_code": 20,
    "name": 128,
    "report_title": 512,
    "report_type": 64,
    "classify": 64,
    "org_name": 128,
    "author_name": 256,
    "quarter": 16,
    "rating": 32,
    "imp_dg": 64,
}

# 规则 Q：decimal(M,D) 上界
DECIMAL_SPECS: dict[str, tuple[int, int]] = {
    "op_rt": (24, 4),
    "op_pr": (24, 4),
    "tp": (24, 4),
    "np": (24, 4),
    "eps": (14, 4),
    "pe": (14, 4),
    "rd": (14, 4),
    "roe": (14, 4),
    "ev_ebitda": (14, 4),
    "max_price": (14, 4),
    "min_price": (14, 4),
}

FLOAT_COLS = tuple(DECIMAL_SPECS.keys())

UK_COLS = ("ts_code", "report_date", "org_name", "quarter", "report_title")

DATE_COLS = ("report_date",)
DATETIME_COLS = ("create_time",)

STR_COLS = (
    "ts_code",
    "name",
    "report_title",
    "report_type",
    "classify",
    "org_name",
    "author_name",
    "quarter",
    "rating",
    "imp_dg",
)

DATA_COLS_ORDER = [
    "ts_code",
    "name",
    "report_date",
    "report_title",
    "report_type",
    "classify",
    "org_name",
    "author_name",
    "quarter",
    "op_rt",
    "op_pr",
    "tp",
    "np",
    "eps",
    "pe",
    "rd",
    "roe",
    "ev_ebitda",
    "rating",
    "max_price",
    "min_price",
    "imp_dg",
    "create_time",
]

_engine = create_engine(
    f"mysql+pymysql://{DB_CONFIG['user']}:{quote_plus(DB_CONFIG['password'])}@"
    f"{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}?charset={DB_CONFIG['charset']}",
    pool_recycle=3600,
    pool_pre_ping=True,
)

_pro = None


def get_pro():
    global _pro
    if _pro is None:
        tok = (os.getenv("TUSHARE_TOKEN") or TUSHARE_TOKEN or "").strip()
        if not tok:
            raise RuntimeError("请配置 TUSHARE_TOKEN 或设置环境变量 TUSHARE_TOKEN")
        _pro = ts.pro_api(tok)
    return _pro


CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `ts_code` varchar(20) NOT NULL COMMENT '股票代码',
  `name` varchar(128) DEFAULT NULL COMMENT '股票名称',
  `report_date` date NOT NULL COMMENT '研报日期',
  `report_title` varchar(512) NOT NULL COMMENT '报告标题',
  `report_type` varchar(64) DEFAULT NULL COMMENT '报告类型',
  `classify` varchar(64) DEFAULT NULL COMMENT '报告分类',
  `org_name` varchar(128) NOT NULL COMMENT '机构名称',
  `author_name` varchar(256) DEFAULT NULL COMMENT '作者',
  `quarter` varchar(16) NOT NULL COMMENT '预测报告期(如2024Q4)',
  `op_rt` decimal(24,4) DEFAULT NULL COMMENT '预测营业收入(万元)',
  `op_pr` decimal(24,4) DEFAULT NULL COMMENT '预测营业利润(万元)',
  `tp` decimal(24,4) DEFAULT NULL COMMENT '预测利润总额(万元)',
  `np` decimal(24,4) DEFAULT NULL COMMENT '预测净利润(万元)',
  `eps` decimal(14,4) DEFAULT NULL COMMENT '预测每股收益(元)',
  `pe` decimal(14,4) DEFAULT NULL COMMENT '预测市盈率',
  `rd` decimal(14,4) DEFAULT NULL COMMENT '预测股息率',
  `roe` decimal(14,4) DEFAULT NULL COMMENT '预测净资产收益率',
  `ev_ebitda` decimal(14,4) DEFAULT NULL COMMENT '预测EV/EBITDA',
  `rating` varchar(32) DEFAULT NULL COMMENT '卖方评级',
  `max_price` decimal(14,4) DEFAULT NULL COMMENT '预测最高目标价',
  `min_price` decimal(14,4) DEFAULT NULL COMMENT '预测最低目标价',
  `imp_dg` varchar(64) DEFAULT NULL COMMENT '机构关注度',
  `create_time` datetime DEFAULT NULL COMMENT 'TS数据更新时间',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '首次入库时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_report_rc` (`ts_code`,`report_date`,`org_name`,`quarter`,`report_title`),
  KEY `idx_report_date` (`report_date`),
  KEY `idx_ts_code` (`ts_code`),
  KEY `idx_org_name` (`org_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='卖方盈利预测(report_rc)';
"""


def create_table() -> None:
    with _engine.begin() as conn:
        conn.execute(text(CREATE_TABLE_SQL))
    logger.info("数据表 %s 已就绪", TABLE_NAME)


def get_row_count() -> int:
    with _engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM `{TABLE_NAME}`")).scalar() or 0)


def get_max_report_date() -> str | None:
    with _engine.connect() as conn:
        row = conn.execute(text(f"SELECT MAX(`report_date`) FROM `{TABLE_NAME}`")).fetchone()
    if not row or row[0] is None:
        return None
    d = row[0]
    if hasattr(d, "strftime"):
        return d.strftime("%Y%m%d")
    return str(d).replace("-", "")[:8]


def iter_date_chunks(start_ymd: str, end_ymd: str) -> list[tuple[str, str]]:
    """将 [start,end] 按 CHUNK_CALENDAR_DAYS 切成多段（自然日）。"""
    start_d = datetime.datetime.strptime(start_ymd, "%Y%m%d").date()
    end_d = datetime.datetime.strptime(end_ymd, "%Y%m%d").date()
    out: list[tuple[str, str]] = []
    cur = start_d
    while cur <= end_d:
        chunk_end = min(cur + datetime.timedelta(days=CHUNK_CALENDAR_DAYS - 1), end_d)
        out.append((cur.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")))
        cur = chunk_end + datetime.timedelta(days=1)
    return out


def _process_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in DATE_COLS:
        if col in df.columns:
            s = pd.Series(df[col]).astype(str).str.replace(r"\D", "", regex=True)
            parsed = pd.to_datetime(s, format="%Y%m%d", errors="coerce")
            mask_bad = parsed.isna() & df[col].notna()
            if mask_bad.any():
                parsed2 = pd.to_datetime(df.loc[mask_bad, col], errors="coerce")
                parsed = parsed.where(~mask_bad, parsed2)
            df[col] = parsed.dt.strftime("%Y-%m-%d")
            df[col] = df[col].where(df[col].notna(), None)

    for col in DATETIME_COLS:
        if col in df.columns:
            parsed = pd.to_datetime(df[col], errors="coerce")
            df[col] = parsed.dt.strftime("%Y-%m-%d %H:%M:%S")
            df[col] = df[col].where(parsed.notna(), None)

    for col in STR_COLS:
        if col in df.columns:
            df[col] = df[col].apply(
                lambda x: None if x is None or (isinstance(x, float) and np.isnan(x)) else str(x).strip() or None
            )

    for col in FLOAT_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.replace({np.nan: None})
    return df


def _drop_incomplete_uk_rows(df: pd.DataFrame) -> pd.DataFrame:
    """丢弃唯一键字段为空的行（接口偶发缺 quarter 等），避免整批失败。"""
    if df.empty:
        return df
    out = df.copy()
    for col in UK_COLS:
        if col not in out.columns:
            out[col] = None
    mask_ok = pd.Series(True, index=out.index)
    for col in UK_COLS:
        s = out[col]
        bad = s.isna() | (s.astype(str).str.strip() == "")
        mask_ok &= ~bad
    n_drop = int((~mask_ok).sum())
    if n_drop:
        sample = out.loc[~mask_ok, ["ts_code", "report_date", "org_name", "quarter", "report_title"]].head(5)
        logger.warning(
            "丢弃唯一键不完整行 %s 条（示例前 5 行）：\n%s",
            n_drop,
            sample.to_string(index=False),
        )
    return out.loc[mask_ok].reset_index(drop=True)


def validate_before_save(df: pd.DataFrame) -> None:
    """规则 Q：唯一键非空、varchar 长度、decimal 边界。"""
    if df.empty:
        return

    for col in UK_COLS:
        if col not in df.columns:
            raise ValueError(f"缺少唯一键列 {col}")

    for col, maxlen in VARCHAR_MAX_LEN.items():
        if col not in df.columns:
            continue
        for idx, val in df[col].items():
            if val is None:
                continue
            if len(str(val)) > maxlen:
                logger.error(
                    "validate_before_save: 列 %s 超长 row=%s ts_code=%s len=%d max=%d",
                    col,
                    idx,
                    df.loc[idx].get("ts_code"),
                    len(str(val)),
                    maxlen,
                )
                raise ValueError(f"列 {col} 超出 VARCHAR({maxlen})")

    for col, (m, d) in DECIMAL_SPECS.items():
        if col not in df.columns:
            continue
        ctx = decimal.Context(prec=max(m + 2, 28))
        max_abs = decimal.Decimal("9" * (m - d) + "." + "9" * d)
        for idx, val in df[col].items():
            if val is None:
                continue
            try:
                q = ctx.create_decimal(str(val))
            except Exception as e:
                logger.error(
                    "validate_before_save: 列 %s 非数值 row=%s ts_code=%s val=%r",
                    col,
                    idx,
                    df.loc[idx].get("ts_code"),
                    val,
                )
                raise ValueError(f"列 {col} 非有效数值") from e
            if abs(q) > max_abs:
                logger.error(
                    "validate_before_save: 列 %s 超界 row=%s ts_code=%s val=%s",
                    col,
                    idx,
                    df.loc[idx].get("ts_code"),
                    val,
                )
                raise ValueError(f"列 {col} 超出 decimal({m},{d})")


def save_data(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    df = _process_df(df)
    df = _drop_incomplete_uk_rows(df)
    if df.empty:
        return 0
    validate_before_save(df)

    cols = [c for c in DATA_COLS_ORDER if c in df.columns]
    for c in DATA_COLS_ORDER:
        if c not in df.columns:
            df[c] = None
    df = df[DATA_COLS_ORDER]

    col_names = ", ".join([f"`{c}`" for c in DATA_COLS_ORDER])
    placeholders = ", ".join([f":{c}" for c in DATA_COLS_ORDER])
    update_parts = ", ".join([f"`{c}` = VALUES(`{c}`)" for c in DATA_COLS_ORDER])
    sql = f"""
        INSERT INTO `{TABLE_NAME}` ({col_names})
        VALUES ({placeholders})
        ON DUPLICATE KEY UPDATE {update_parts}
    """
    records = df.to_dict("records")
    total = 0
    with _engine.begin() as conn:
        for i in range(0, len(records), BATCH_SIZE):
            batch = records[i : i + BATCH_SIZE]
            for row in batch:
                for k, v in list(row.items()):
                    if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
                        row[k] = None
            conn.execute(text(sql), batch)
            total += len(batch)
    return total


# ============================================================
# API：重试 + 分页
# ============================================================
def _retry_request(func, *args, max_retries: int = 3, **kwargs) -> pd.DataFrame | None:
    for attempt in range(1, max_retries + 1):
        try:
            df = func(*args, **kwargs)
            if df is None:
                return pd.DataFrame()
            return df
        except KeyboardInterrupt:
            raise
        except Exception as e:
            wait = 2**attempt
            if attempt < max_retries:
                logger.warning("请求失败（第 %s/%s 次），%ss 后重试：%s", attempt, max_retries, wait, e)
                time.sleep(wait)
            else:
                logger.error(
                    "[FAILED] func=%s kwargs=%s ，已重试 %s 次：%s",
                    getattr(func, "__name__", str(func)),
                    kwargs,
                    max_retries,
                    e,
                )
    return None


def fetch_report_rc_range(start_date: str, end_date: str) -> pd.DataFrame | None:
    """
    按研报日期区间拉取 report_rc；offset 分页直至行数 < PAGE_SIZE。
    start_date/end_date: YYYYMMDD
    """
    pro = get_pro()
    frames: list[pd.DataFrame] = []
    offset = 0
    while True:
        df = _retry_request(
            pro.report_rc,
            start_date=start_date,
            end_date=end_date,
            offset=offset,
            limit=PAGE_SIZE,
            fields=FIELDS,
        )
        if df is None:
            return None
        if df.empty:
            break
        frames.append(df)
        if len(df) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        logger.info(
            "分段 %s~%s 分页 offset=%s 本页 %s 行，继续…",
            start_date,
            end_date,
            offset,
            len(df),
        )
        time.sleep(REQUEST_INTERVAL)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def import_date_range(start_ymd: str, end_ymd: str, label: str = "") -> int:
    """拉取并写入一个日期区间（可跨多段调用）。"""
    chunks = iter_date_chunks(start_ymd, end_ymd)
    total_written = 0
    for idx, (s, e) in enumerate(chunks, start=1):
        logger.info("%s [%s/%s] 拉取 %s ~ %s", label, idx, len(chunks), s, e)
        df = fetch_report_rc_range(s, e)
        time.sleep(REQUEST_INTERVAL)
        if df is None:
            logger.error("%s 分段 %s~%s 请求失败，跳过该段", label, s, e)
            continue
        if df.empty:
            logger.info("%s 分段 %s~%s 无数据", label, s, e)
            continue
        n = save_data(df)
        total_written += n
        logger.info("%s 分段 %s~%s 获取=%s 写入=%s", label, s, e, len(df), n)
    return total_written


def full_import() -> None:
    today = datetime.date.today().strftime("%Y%m%d")
    logger.info("全量导入：%s ~ %s", FULL_START_DATE, today)
    n = import_date_range(FULL_START_DATE, today, label="全量")
    logger.info("全量导入完成，累计写入约 %s 行", n)


def incremental_import() -> None:
    max_d = get_max_report_date()
    if not max_d:
        logger.info("业务表为空，切换全量导入")
        full_import()
        return
    max_dt = datetime.datetime.strptime(max_d, "%Y%m%d").date()
    start_dt = max_dt - datetime.timedelta(days=INCREMENTAL_LOOKBACK_DAYS)
    start_ymd = start_dt.strftime("%Y%m%d")
    today = datetime.date.today().strftime("%Y%m%d")
    logger.info(
        "增量导入：本地 MAX(report_date)=%s，回滚 %s 日，区间 %s ~ %s",
        max_d,
        INCREMENTAL_LOOKBACK_DAYS,
        start_ymd,
        today,
    )
    n = import_date_range(start_ymd, today, label="增量")
    logger.info("增量导入完成，累计写入约 %s 行", n)


def _parse_ymd(s: str) -> str:
    """校验并规范为 YYYYMMDD。"""
    digits = re.sub(r"\D", "", s.strip())
    if len(digits) != 8:
        raise argparse.ArgumentTypeError(f"日期须为 YYYYMMDD，收到: {s!r}")
    datetime.datetime.strptime(digits, "%Y%m%d")
    return digits


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="卖方盈利预测 report_rc 入库")
    p.add_argument(
        "--from-date",
        type=_parse_ymd,
        metavar="YYYYMMDD",
        help="指定区间起点（与 --to-date 合用，跳过自动全量/增量判断）",
    )
    p.add_argument(
        "--to-date",
        type=_parse_ymd,
        metavar="YYYYMMDD",
        help="指定区间终点（含当日）",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    create_table()
    logger.info("=" * 60)

    if args.from_date or args.to_date:
        if not args.from_date or not args.to_date:
            logger.error("指定区间须同时提供 --from-date 与 --to-date")
            sys.exit(1)
        if args.from_date > args.to_date:
            logger.error("from-date 不能晚于 to-date")
            sys.exit(1)
        logger.info(
            "【指定区间】%s ~ %s（当前表行数=%s）",
            args.from_date,
            args.to_date,
            get_row_count(),
        )
        n = import_date_range(args.from_date, args.to_date, label="区间")
        logger.info("区间导入完成，本段写入约 %s 行", n)
    elif get_row_count() == 0:
        logger.info("业务表为空，执行全量导入")
        full_import()
    else:
        logger.info("业务表已有 %s 行，执行增量导入", get_row_count())
        incremental_import()

    logger.info("=" * 60)
    logger.info("report_rc 流程结束；当前行数=%s", get_row_count())


if __name__ == "__main__":
    main()

# 代码更新时间：2026-05-21 05:54
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
