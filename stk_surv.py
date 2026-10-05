#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口：stk_surv（上市公司机构调研记录）
描述：获取机构调研记录；单次最多 100 条，须 offset 分页。不导入 content 字段。
数据库：stock_tushare
数据表：stk_surv
更新方式：空表全量（FULL_START_DATE 起按自然日分段）/ 有数据增量（MAX(surv_date) 回滚 N 日）
文档：https://tushare.pro/document/2?doc_id=275

环境变量：TUSHARE_TOKEN、DB_HOST、DB_PORT、DB_USER、DB_PASSWORD、DB_NAME（可选）
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import re
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
LOG_FILE = os.path.join(_LOG_DIR, "stk_surv.log")
logger = getLogger("stk_surv")
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

TABLE_NAME = "stk_surv"
BATCH_SIZE = 500
REQUEST_INTERVAL = 0.2
PAGE_SIZE = 100

# ===== 用户配置区 =====
# 全量起始日（YYYYMMDD）；与 report_rc 等一致，2010 前多数分段无数据
FULL_START_DATE = "20100101"
# 全量按自然日分段长度（段内靠 offset 分页取完）
CHUNK_CALENDAR_DAYS = 30
# 增量：自 MAX(surv_date) 向前回滚自然日，覆盖补录
INCREMENTAL_LOOKBACK_DAYS = 7
# ===== 用户配置区结束 =====

# 接口输出字段（规则 B；不含 content）
FIELDS = (
    "ts_code,name,surv_date,fund_visitors,rece_place,rece_mode,rece_org,org_type,comp_rece"
)

# 规则 Q：varchar 字符上限（utf8mb4 字符数；抽样 + 冗余）
VARCHAR_MAX_LEN: dict[str, int] = {
    "ts_code": 20,
    "name": 128,
    "fund_visitors": 256,
    "rece_place": 512,
    "rece_mode": 512,
    "rece_org": 256,
    "org_type": 32,
}

# 长文本列（不入唯一索引前缀校验）
TEXT_MAX_LEN: dict[str, int] = {
    "comp_rece": 4000,
}

UK_COLS = ("ts_code", "surv_date", "fund_visitors", "rece_org", "rece_place", "rece_mode")
# 唯一键中允许为空串的列（如接待地点缺失）
UK_OPTIONAL_EMPTY_COLS = frozenset({"rece_place"})
UK_REQUIRED_COLS = tuple(c for c in UK_COLS if c not in UK_OPTIONAL_EMPTY_COLS)

DATE_COLS = ("surv_date",)

STR_COLS = (
    "ts_code",
    "name",
    "fund_visitors",
    "rece_place",
    "rece_mode",
    "rece_org",
    "org_type",
    "comp_rece",
)

DATA_COLS_ORDER = [
    "ts_code",
    "name",
    "surv_date",
    "fund_visitors",
    "rece_place",
    "rece_mode",
    "rece_org",
    "org_type",
    "comp_rece",
]

_engine = create_engine(
    f"mysql+pymysql://{DB_CONFIG['user']}:{quote_plus(DB_CONFIG['password'])}@"
    f"{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}?charset={DB_CONFIG['charset']}",
    pool_recycle=3600,
    pool_pre_ping=True,
)

_pro = None


def getPro():
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
  `surv_date` date NOT NULL COMMENT '调研日期',
  `fund_visitors` varchar(256) NOT NULL COMMENT '机构参与人员',
  `rece_place` varchar(512) NOT NULL DEFAULT '' COMMENT '接待地点',
  `rece_mode` varchar(512) NOT NULL COMMENT '接待方式',
  `rece_org` varchar(256) NOT NULL COMMENT '接待机构',
  `org_type` varchar(32) DEFAULT NULL COMMENT '接待公司类型',
  `comp_rece` text COMMENT '上市公司接待人员',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '首次入库时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_stk_surv` (`ts_code`,`surv_date`,`fund_visitors`,`rece_org`,`rece_place`(64),`rece_mode`(64)),
  KEY `idx_surv_date` (`surv_date`),
  KEY `idx_ts_code` (`ts_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='机构调研(stk_surv)';
"""


def createTable() -> None:
    with _engine.begin() as conn:
        conn.execute(text(CREATE_TABLE_SQL))
        # 先删唯一索引再扩列，否则 utf8mb4 下宽列会触发 1071
        idxRow = conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = :tn AND index_name = 'uk_stk_surv'"
            ),
            {"tn": TABLE_NAME},
        ).scalar()
        if idxRow:
            conn.execute(text(f"ALTER TABLE `{TABLE_NAME}` DROP INDEX `uk_stk_surv`"))
        migrateSql = [
            "MODIFY COLUMN `fund_visitors` varchar(256) NOT NULL COMMENT '机构参与人员'",
            "MODIFY COLUMN `rece_org` varchar(256) NOT NULL COMMENT '接待机构'",
            "MODIFY COLUMN `rece_place` varchar(512) NOT NULL DEFAULT '' COMMENT '接待地点'",
            "MODIFY COLUMN `rece_mode` varchar(512) NOT NULL COMMENT '接待方式'",
            "MODIFY COLUMN `comp_rece` text COMMENT '上市公司接待人员'",
        ]
        for stmt in migrateSql:
            conn.execute(text(f"ALTER TABLE `{TABLE_NAME}` {stmt}"))
        conn.execute(
            text(
                f"ALTER TABLE `{TABLE_NAME}` ADD UNIQUE KEY `uk_stk_surv` "
                "(`ts_code`,`surv_date`,`fund_visitors`,`rece_org`,`rece_place`(64),`rece_mode`(64))"
            )
        )
    logger.info("数据表 %s 已就绪", TABLE_NAME)


def getRowCount() -> int:
    with _engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM `{TABLE_NAME}`")).scalar() or 0)


def getMaxSurvDate() -> str | None:
    with _engine.connect() as conn:
        row = conn.execute(text(f"SELECT MAX(`surv_date`) FROM `{TABLE_NAME}`")).fetchone()
    if not row or row[0] is None:
        return None
    d = row[0]
    if hasattr(d, "strftime"):
        return d.strftime("%Y%m%d")
    return str(d).replace("-", "")[:8]


def iterDateChunks(startYmd: str, endYmd: str) -> list[tuple[str, str]]:
    """将 [start,end] 按 CHUNK_CALENDAR_DAYS 切成多段（自然日）。"""
    startD = datetime.datetime.strptime(startYmd, "%Y%m%d").date()
    endD = datetime.datetime.strptime(endYmd, "%Y%m%d").date()
    out: list[tuple[str, str]] = []
    cur = startD
    while cur <= endD:
        chunkEnd = min(cur + datetime.timedelta(days=CHUNK_CALENDAR_DAYS - 1), endD)
        out.append((cur.strftime("%Y%m%d"), chunkEnd.strftime("%Y%m%d")))
        cur = chunkEnd + datetime.timedelta(days=1)
    return out


def _processDf(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in DATE_COLS:
        if col in df.columns:
            s = pd.Series(df[col]).astype(str).str.replace(r"\D", "", regex=True)
            parsed = pd.to_datetime(s, format="%Y%m%d", errors="coerce")
            maskBad = parsed.isna() & df[col].notna()
            if maskBad.any():
                parsed2 = pd.to_datetime(df.loc[maskBad, col], errors="coerce")
                parsed = parsed.where(~maskBad, parsed2)
            df[col] = parsed.dt.strftime("%Y-%m-%d")
            df[col] = df[col].where(df[col].notna(), None)

    for col in STR_COLS:
        if col in df.columns:
            df[col] = df[col].apply(
                lambda x: None if x is None or (isinstance(x, float) and np.isnan(x)) else str(x).strip() or None
            )

    # 唯一键字符串列空值置 ''，避免 UNIQUE 对 NULL 允许多行重复（surv_date 除外）
    for col in UK_COLS:
        if col not in df.columns or col == "surv_date":
            continue
        df[col] = df[col].apply(
            lambda x: ""
            if x is None or (isinstance(x, float) and np.isnan(x))
            else str(x).strip() if x is not None else ""
        )

    df = df.replace({np.nan: None})
    if "rece_place" in df.columns:
        df["rece_place"] = df["rece_place"].apply(lambda x: "" if x is None else x)
    return df


def _dropIncompleteUkRows(df: pd.DataFrame) -> pd.DataFrame:
    """丢弃必填唯一键字段为空的行；rece_place 等允许空串。"""
    if df.empty:
        return df
    out = df.copy()
    for col in UK_COLS:
        if col not in out.columns:
            out[col] = ""
    maskOk = pd.Series(True, index=out.index)
    for col in UK_REQUIRED_COLS:
        s = out[col]
        bad = s.isna() | (s.astype(str).str.strip() == "")
        maskOk &= ~bad
    nDrop = int((~maskOk).sum())
    if nDrop:
        sampleCols = [c for c in UK_COLS if c in out.columns]
        sample = out.loc[~maskOk, sampleCols].head(5)
        logger.warning(
            "丢弃唯一键不完整行 %s 条（示例前 5 行）：\n%s",
            nDrop,
            sample.to_string(index=False),
        )
    return out.loc[maskOk].reset_index(drop=True)


def validateBeforeSave(df: pd.DataFrame) -> None:
    """规则 Q：唯一键非空、varchar 长度。"""
    if df.empty:
        return

    for col in UK_REQUIRED_COLS:
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
                    "validateBeforeSave: 列 %s 超长 row=%s ts_code=%s len=%d max=%d",
                    col,
                    idx,
                    df.loc[idx].get("ts_code"),
                    len(str(val)),
                    maxlen,
                )
                raise ValueError(f"列 {col} 超出 VARCHAR({maxlen})")

    for col, maxlen in TEXT_MAX_LEN.items():
        if col not in df.columns:
            continue
        for idx, val in df[col].items():
            if val is None:
                continue
            if len(str(val)) > maxlen:
                logger.error(
                    "validateBeforeSave: 列 %s 超长 row=%s ts_code=%s len=%d max=%d",
                    col,
                    idx,
                    df.loc[idx].get("ts_code"),
                    len(str(val)),
                    maxlen,
                )
                raise ValueError(f"列 {col} 超出 TEXT 上限 {maxlen}")


def saveData(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    df = _processDf(df)
    df = _dropIncompleteUkRows(df)
    if df.empty:
        return 0
    validateBeforeSave(df)

    for c in DATA_COLS_ORDER:
        if c not in df.columns:
            df[c] = None
    df = df[DATA_COLS_ORDER]

    colNames = ", ".join([f"`{c}`" for c in DATA_COLS_ORDER])
    placeholders = ", ".join([f":{c}" for c in DATA_COLS_ORDER])
    updateParts = ", ".join([f"`{c}` = VALUES(`{c}`)" for c in DATA_COLS_ORDER])
    sql = f"""
        INSERT INTO `{TABLE_NAME}` ({colNames})
        VALUES ({placeholders})
        ON DUPLICATE KEY UPDATE {updateParts}
    """
    records = df.to_dict("records")
    total = 0
    with _engine.begin() as conn:
        for i in range(0, len(records), BATCH_SIZE):
            batch = records[i : i + BATCH_SIZE]
            for row in batch:
                for k, v in list(row.items()):
                    if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
                        row[k] = "" if k in UK_OPTIONAL_EMPTY_COLS else None
                    elif v is None and k in UK_OPTIONAL_EMPTY_COLS:
                        row[k] = ""
            conn.execute(text(sql), batch)
            total += len(batch)
    return total


def _retryRequest(func, *args, maxRetries: int = 3, **kwargs) -> pd.DataFrame | None:
    for attempt in range(1, maxRetries + 1):
        try:
            df = func(*args, **kwargs)
            if df is None:
                return pd.DataFrame()
            return df
        except KeyboardInterrupt:
            raise
        except Exception as e:
            wait = 2**attempt
            if attempt < maxRetries:
                logger.warning("请求失败（第 %s/%s 次），%ss 后重试：%s", attempt, maxRetries, wait, e)
                time.sleep(wait)
            else:
                logger.error(
                    "[FAILED] func=%s kwargs=%s ，已重试 %s 次：%s",
                    getattr(func, "__name__", str(func)),
                    kwargs,
                    maxRetries,
                    e,
                )
    return None


def fetchStkSurvRange(startDate: str, endDate: str) -> pd.DataFrame | None:
    """
    按调研日期区间拉取 stk_surv；offset 分页直至行数 < PAGE_SIZE。
    startDate/endDate: YYYYMMDD
    """
    pro = getPro()
    frames: list[pd.DataFrame] = []
    offset = 0
    while True:
        df = _retryRequest(
            pro.stk_surv,
            start_date=startDate,
            end_date=endDate,
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
            startDate,
            endDate,
            offset,
            len(df),
        )
        time.sleep(REQUEST_INTERVAL)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def importDateRange(startYmd: str, endYmd: str, label: str = "") -> int:
    """拉取并写入一个日期区间（可跨多段调用）。"""
    chunks = iterDateChunks(startYmd, endYmd)
    totalWritten = 0
    for idx, (s, e) in enumerate(chunks, start=1):
        logger.info("%s [%s/%s] 拉取 %s ~ %s", label, idx, len(chunks), s, e)
        df = fetchStkSurvRange(s, e)
        time.sleep(REQUEST_INTERVAL)
        if df is None:
            logger.error("%s 分段 %s~%s 请求失败，跳过该段", label, s, e)
            continue
        if df.empty:
            logger.info("%s 分段 %s~%s 无数据", label, s, e)
            continue
        n = saveData(df)
        totalWritten += n
        logger.info("%s 分段 %s~%s 获取=%s 写入=%s", label, s, e, len(df), n)
    return totalWritten


def fullImport() -> None:
    today = datetime.date.today().strftime("%Y%m%d")
    logger.info("全量导入：%s ~ %s", FULL_START_DATE, today)
    n = importDateRange(FULL_START_DATE, today, label="全量")
    logger.info("全量导入完成，累计写入约 %s 行", n)


def incrementalImport() -> None:
    maxD = getMaxSurvDate()
    if not maxD:
        logger.info("业务表为空，切换全量导入")
        fullImport()
        return
    maxDt = datetime.datetime.strptime(maxD, "%Y%m%d").date()
    startDt = maxDt - datetime.timedelta(days=INCREMENTAL_LOOKBACK_DAYS)
    startYmd = startDt.strftime("%Y%m%d")
    today = datetime.date.today().strftime("%Y%m%d")
    logger.info(
        "增量导入：本地 MAX(surv_date)=%s，回滚 %s 日，区间 %s ~ %s",
        maxD,
        INCREMENTAL_LOOKBACK_DAYS,
        startYmd,
        today,
    )
    n = importDateRange(startYmd, today, label="增量")
    logger.info("增量导入完成，累计写入约 %s 行", n)


def _parseYmd(s: str) -> str:
    digits = re.sub(r"\D", "", s.strip())
    if len(digits) != 8:
        raise argparse.ArgumentTypeError(f"日期须为 YYYYMMDD，收到: {s!r}")
    datetime.datetime.strptime(digits, "%Y%m%d")
    return digits


def parseArgs() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="机构调研 stk_surv 入库")
    p.add_argument(
        "--full",
        action="store_true",
        help="强制全量（忽略表内已有数据）",
    )
    p.add_argument(
        "--from-date",
        type=_parseYmd,
        metavar="YYYYMMDD",
        help="指定区间起点（与 --to-date 合用）",
    )
    p.add_argument(
        "--to-date",
        type=_parseYmd,
        metavar="YYYYMMDD",
        help="指定区间终点（含当日）",
    )
    return p.parse_args()


def main() -> None:
    args = parseArgs()
    createTable()
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
            getRowCount(),
        )
        n = importDateRange(args.from_date, args.to_date, label="区间")
        logger.info("区间导入完成，本段写入约 %s 行", n)
    elif args.full:
        logger.info("【--full】强制全量（当前表行数=%s）", getRowCount())
        fullImport()
    elif getRowCount() == 0:
        logger.info("业务表为空，执行全量导入")
        fullImport()
    else:
        logger.info("业务表已有 %s 行，执行增量导入", getRowCount())
        incrementalImport()

    logger.info("=" * 60)
    logger.info("stk_surv 流程结束；当前行数=%s", getRowCount())


if __name__ == "__main__":
    main()

# 代码更新时间：2026-05-21 06:38
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
