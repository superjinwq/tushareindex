#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口：limit_list_ths（涨跌停榜单·同花顺）
描述：获取同花顺每日涨跌停榜单；历史自 20231101 起，增量约每日 16:00 后更新。
     单次最多 4000 条，需 offset 分页。五类池：涨停池、连扳池、冲刺涨停、炸板池、跌停池。
数据库：stock_tushare
数据表：limit_list_ths（业务数据）、limit_list_ths_pool_done（按日+池拉取完成标记，用于全量续传含空池）
更新方式：全量（自 FULL_START_DATE 起按交易日×五池）/ 增量（新交易日×五池）
三阶段：增量或全量 → 一致性校验 → 不一致则按日全量修复
文档：https://tushare.pro/document/2?doc_id=355
"""

from __future__ import annotations

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
LOG_FILE = os.path.join(_LOG_DIR, "limit_list_ths.log")
logger = getLogger("limit_list_ths")
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
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": os.getenv("DB_PASSWORD", "").strip(),
    "database": "stock_tushare",
    "charset": "utf8mb4",
}
TABLE_NAME = "limit_list_ths"
POOL_DONE_TABLE = "limit_list_ths_pool_done"
BATCH_SIZE = 500
REQUEST_INTERVAL = 0.2
# 文档：单次最大 4000 条
PAGE_SIZE = 4000

# ===== 用户配置区 =====
# 全量起始日（YYYYMMDD）。官方数据自 20231101 起；本需求从 2026-01-01 起拉取。
FULL_START_DATE = "20260101"
# ===== 用户配置区结束 =====

# 五类池（与接口文档一致，请求参数 limit_type）
LIMIT_TYPE_LIST = ("涨停池", "连扳池", "冲刺涨停", "炸板池", "跌停池")

# 接口全部输出字段（显式传入，避免默认显示=N 的字段缺失）
FIELDS = (
    "trade_date,ts_code,name,price,pct_chg,open_num,lu_desc,limit_type,tag,status,"
    "first_lu_time,last_lu_time,first_ld_time,last_ld_time,limit_order,limit_amount,"
    "turnover_rate,free_float,lu_limit_order,limit_up_suc_rate,turnover,rise_rate,sum_float,market_type"
)

# 一致性校验（规则 G）
DATA_TOLERANCE = 0.001
CHECK_FIELDS = "trade_date,ts_code,limit_type,price,pct_chg,turnover"

_engine = create_engine(
    f"mysql+pymysql://{DB_CONFIG['user']}:{quote_plus(DB_CONFIG['password'])}@"
    f"{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}?charset={DB_CONFIG['charset']}",
    pool_recycle=3600,
    pool_pre_ping=True,
)

pro = None  # main 中初始化


# ============================================================
# 建表 DDL（decimal 按业务量级设定；name/status 为 API 原名）
# ============================================================
CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `trade_date` date NOT NULL COMMENT '交易日期',
  `ts_code` varchar(20) NOT NULL COMMENT '股票代码',
  `name` varchar(128) DEFAULT NULL COMMENT '股票名称',
  `price` decimal(16,4) DEFAULT NULL COMMENT '收盘价(元)',
  `pct_chg` decimal(14,4) DEFAULT NULL COMMENT '涨跌幅%',
  `open_num` bigint DEFAULT NULL COMMENT '打开次数',
  `lu_desc` varchar(2048) DEFAULT NULL COMMENT '涨停原因',
  `limit_type` varchar(32) NOT NULL COMMENT '板单类别',
  `tag` varchar(128) DEFAULT NULL COMMENT '涨停标签',
  `status` varchar(64) DEFAULT NULL COMMENT '涨停状态（N连板、一字板）',
  `first_lu_time` datetime DEFAULT NULL COMMENT '首次涨停时间（与 trade_date 组合为当日时刻）',
  `last_lu_time` datetime DEFAULT NULL COMMENT '最后涨停时间',
  `first_ld_time` datetime DEFAULT NULL COMMENT '首次跌停时间',
  `last_ld_time` datetime DEFAULT NULL COMMENT '最后跌停时间',
  `limit_order` decimal(24,4) DEFAULT NULL COMMENT '封单量',
  `limit_amount` decimal(24,4) DEFAULT NULL COMMENT '封单额',
  `turnover_rate` decimal(14,4) DEFAULT NULL COMMENT '换手率%',
  `free_float` decimal(24,4) DEFAULT NULL COMMENT '实际流通',
  `lu_limit_order` decimal(24,4) DEFAULT NULL COMMENT '最大封单',
  `limit_up_suc_rate` decimal(14,4) DEFAULT NULL COMMENT '近一年涨停封板率',
  `turnover` decimal(24,4) DEFAULT NULL COMMENT '成交额',
  `rise_rate` decimal(14,4) DEFAULT NULL COMMENT '涨速',
  `sum_float` decimal(18,4) DEFAULT NULL COMMENT '总市值（亿元）',
  `market_type` varchar(32) DEFAULT NULL COMMENT '股票类型',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_date_code_ltype` (`trade_date`,`ts_code`,`limit_type`),
  KEY `idx_trade_date` (`trade_date`),
  KEY `idx_limit_type` (`limit_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='同花顺涨跌停榜单';
"""

CREATE_POOL_DONE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{POOL_DONE_TABLE}` (
  `trade_date` date NOT NULL COMMENT '交易日期',
  `pool` varchar(32) NOT NULL COMMENT '请求的 limit_type 池名',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '标记时间',
  PRIMARY KEY (`trade_date`,`pool`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='limit_list_ths 按日+池拉取完成（含空表）';
"""


def create_table() -> None:
    with _engine.begin() as conn:
        conn.execute(text(CREATE_TABLE_SQL))
        conn.execute(text(CREATE_POOL_DONE_SQL))
    logger.info("数据表 %s / %s 已就绪", TABLE_NAME, POOL_DONE_TABLE)
    migrate_board_time_columns_varchar_to_datetime()


def _apply_board_time_datetime_alter() -> None:
    """将四类时间列改为 datetime（在数据已规范为可转换字符串之后调用）。"""
    specs = [
        ("first_lu_time", "首次涨停时间（与 trade_date 组合为当日时刻）"),
        ("last_lu_time", "最后涨停时间"),
        ("first_ld_time", "首次跌停时间"),
        ("last_ld_time", "最后跌停时间"),
    ]
    with _engine.begin() as conn:
        for cname, cmt in specs:
            # 注释为固定中文，无单引号；避免 :绑定 在部分驱动下对 DDL 不生效
            conn.execute(
                text(
                    f"ALTER TABLE `{TABLE_NAME}` MODIFY COLUMN `{cname}` datetime DEFAULT NULL COMMENT '{cmt}'"
                )
            )


def migrate_board_time_columns_varchar_to_datetime() -> None:
    """
    旧版脚本将 first/last lu/ld 建成 varchar；tushare-to-mysql 约定含 _time 应为 datetime。
    若检测到 varchar：先按行解析写回，再 ALTER 为 datetime。新表已是 datetime 则跳过。
    """
    with _engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :tb AND COLUMN_NAME = 'first_lu_time'
                """
            ),
            {"tb": TABLE_NAME},
        ).fetchone()
    if not row or str(row[0]).lower() != "varchar":
        return
    logger.info("检测到 %s 时间字段为 varchar，迁移为 datetime …", TABLE_NAME)
    with _engine.connect() as conn:
        df = pd.read_sql(
            text(
                f"SELECT `id`, `trade_date`, `first_lu_time`, `last_lu_time`, `first_ld_time`, `last_ld_time` "
                f"FROM `{TABLE_NAME}`"
            ),
            conn,
        )
    if df.empty:
        _apply_board_time_datetime_alter()
        logger.info("表为空，已执行 ALTER 为 datetime")
        return
    td = df["trade_date"].map(_trade_date_to_str)
    for c in DATETIME_COLS:
        df[c] = [_parse_board_time(df[c].iloc[i], td.iloc[i]) for i in range(len(df))]
    upd = text(
        f"""
        UPDATE `{TABLE_NAME}` SET
          `first_lu_time` = :a, `last_lu_time` = :b, `first_ld_time` = :c, `last_ld_time` = :d
        WHERE `id` = :id
        """
    )
    with _engine.begin() as conn:
        for i in range(0, len(df), BATCH_SIZE):
            chunk = df.iloc[i : i + BATCH_SIZE]
            for _, r in chunk.iterrows():
                conn.execute(
                    upd,
                    {
                        "a": r["first_lu_time"],
                        "b": r["last_lu_time"],
                        "c": r["first_ld_time"],
                        "d": r["last_ld_time"],
                        "id": int(r["id"]),
                    },
                )
    _apply_board_time_datetime_alter()
    logger.info("时间字段 varchar → datetime 迁移完成（若原字符串无法解析则为 NULL）")


# ============================================================
# DataFrame 处理
# ============================================================
DATE_COLS = ["trade_date"]
# 含 _time 的字段按 tushare-to-mysql / type-mapping：入库 datetime（见 DATETIME_COLS）
DATETIME_COLS = ["first_lu_time", "last_lu_time", "first_ld_time", "last_ld_time"]
# 文本列：入库前 strip；ts_code 同 skill 约定 varchar(20)，需去空格防唯一键异常
STR_COLS = [
    "ts_code",
    "name",
    "lu_desc",
    "limit_type",
    "tag",
    "status",
    "market_type",
]


def _trade_date_to_str(td: object) -> str | None:
    """将 trade_date 列元素转为 YYYY-MM-DD，供与时间字符串拼接。"""
    if td is None or (isinstance(td, float) and np.isnan(td)):
        return None
    if hasattr(td, "strftime"):
        return td.strftime("%Y-%m-%d")
    s = str(td).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def _parse_board_time(raw: object, trade_date_str: str | None) -> str | None:
    """
    接口可能返回：完整 datetime 串、HH:MM:SS、纯数字 HHMMSS/HHMM 等；
    与 trade_date 组合为当日 datetime，返回 MySQL 可写的 'YYYY-MM-DD HH:MM:SS' 或 None。
    """
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    # 完整日期时间（接口偶发带日期）
    if re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", s):
        try:
            dt = pd.to_datetime(s, errors="coerce")
            if pd.notna(dt):
                return pd.Timestamp(dt).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    if not trade_date_str:
        return None
    # 仅时间：HH:MM:SS
    m = re.match(r"^(\d{1,2}):(\d{1,2}):(\d{1,2})$", s)
    if m:
        return f"{trade_date_str} {int(m.group(1)):02d}:{int(m.group(2)):02d}:{int(m.group(3)):02d}"
    # 纯数字 HHMMSS / HHMM
    digits = re.sub(r"\D", "", s)
    if len(digits) == 6:
        return f"{trade_date_str} {digits[:2]}:{digits[2:4]}:{digits[4:6]}"
    if len(digits) == 4:
        return f"{trade_date_str} {digits[:2]}:{digits[2:4]}:00"
    return None


def _process_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in DATE_COLS:
        if col in df.columns:
            s = pd.Series(df[col]).astype(str).str.replace(r"\D", "", regex=True)
            df[col] = pd.to_datetime(s, format="%Y%m%d", errors="coerce")
            df[col] = df[col].dt.strftime("%Y-%m-%d")
            df[col] = df[col].where(df[col].notna(), None)
    if "open_num" in df.columns:
        df["open_num"] = pd.to_numeric(df["open_num"], errors="coerce")
        df["open_num"] = df["open_num"].apply(lambda x: int(x) if pd.notna(x) and x == x else None)
    # 涨跌停首次/末次时间 → datetime（依赖已规范化的 trade_date）
    if "trade_date" in df.columns:
        td_series = df["trade_date"].map(_trade_date_to_str)
        for col in DATETIME_COLS:
            if col in df.columns:
                df[col] = [
                    _parse_board_time(df[col].iloc[i], td_series.iloc[i])
                    for i in range(len(df))
                ]
    for col in STR_COLS:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: None if x is None or (isinstance(x, float) and np.isnan(x)) else str(x).strip() or None)
    df = df.replace({np.nan: None})
    return df


DATA_COLS_ORDER = [
    "trade_date",
    "ts_code",
    "name",
    "price",
    "pct_chg",
    "open_num",
    "lu_desc",
    "limit_type",
    "tag",
    "status",
    "first_lu_time",
    "last_lu_time",
    "first_ld_time",
    "last_ld_time",
    "limit_order",
    "limit_amount",
    "turnover_rate",
    "free_float",
    "lu_limit_order",
    "limit_up_suc_rate",
    "turnover",
    "rise_rate",
    "sum_float",
    "market_type",
]


def save_data(df: pd.DataFrame) -> int:
    """INSERT ... ON DUPLICATE KEY UPDATE，按 uk (trade_date, ts_code, limit_type)"""
    if df is None or df.empty:
        return 0
    df = _process_df(df)
    cols = [c for c in DATA_COLS_ORDER if c in df.columns]
    df = df[cols]
    col_names = ", ".join([f"`{c}`" for c in cols])
    placeholders = ", ".join([f":{c}" for c in cols])
    update_parts = ", ".join([f"`{c}` = VALUES(`{c}`)" for c in cols])
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
    """
    成功返回 DataFrame（可为空表）；重试耗尽返回 None，表示「请求失败」，
    与「接口正常返回 0 行」区分，避免误写入 pool_done。
    """
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


def fetch_one_pool_day(trade_date_yyyymmdd: str, limit_type_pool: str) -> pd.DataFrame | None:
    """
    拉取单个交易日、单个池的数据；支持 offset 分页至行数 < PAGE_SIZE。
    任一页请求失败则返回 None；成功但无数据返回空 DataFrame。
    trade_date_yyyymmdd: YYYYMMDD
    """
    frames: list[pd.DataFrame] = []
    offset = 0
    while True:
        df = _retry_request(
            pro.limit_list_ths,
            trade_date=trade_date_yyyymmdd,
            limit_type=limit_type_pool,
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
        time.sleep(REQUEST_INTERVAL)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    return out


def mark_pool_fetched(trade_date_yyyymmdd: str, pool: str) -> None:
    """空池也标记，便于全量续传。"""
    d = f"{trade_date_yyyymmdd[:4]}-{trade_date_yyyymmdd[4:6]}-{trade_date_yyyymmdd[6:8]}"
    with _engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO `{POOL_DONE_TABLE}` (`trade_date`, `pool`) VALUES (:dt, :p) "
                "ON DUPLICATE KEY UPDATE `pool` = VALUES(`pool`)"
            ),
            {"dt": d, "p": pool},
        )


def is_pool_fetched(trade_date_yyyymmdd: str, pool: str) -> bool:
    d = f"{trade_date_yyyymmdd[:4]}-{trade_date_yyyymmdd[4:6]}-{trade_date_yyyymmdd[6:8]}"
    with _engine.connect() as conn:
        r = conn.execute(
            text(f"SELECT 1 FROM `{POOL_DONE_TABLE}` WHERE `trade_date` = :dt AND `pool` = :p LIMIT 1"),
            {"dt": d, "p": pool},
        ).fetchone()
    return r is not None


def get_row_count() -> int:
    with _engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM `{TABLE_NAME}`")).scalar()


def get_max_date_from_db() -> str | None:
    with _engine.connect() as conn:
        row = conn.execute(text(f"SELECT MAX(`trade_date`) FROM `{TABLE_NAME}`")).fetchone()
    if not row or row[0] is None:
        return None
    d = row[0]
    if hasattr(d, "strftime"):
        return d.strftime("%Y%m%d")
    s = str(d)
    return s.replace("-", "")[:8]


# ============================================================
# 一致性校验（最新交易日：API 五池合并 vs 本地）
# ============================================================
def consistency_check() -> list[str]:
    if get_row_count() == 0:
        return []
    latest = get_max_date_from_db()
    if not latest:
        return []
    logger.info("一致性校验：最新交易日 %s", latest)
    frames = []
    for pool in LIMIT_TYPE_LIST:
        df = fetch_one_pool_day(latest, pool)
        time.sleep(REQUEST_INTERVAL)
        if df is None:
            logger.warning("一致性校验：%s 池 %s 请求失败，跳过校验", latest, pool)
            return []
        if not df.empty:
            frames.append(df)
    if not frames:
        logger.warning("API 未返回 %s 任何池数据，跳过校验", latest)
        return []
    df_api = pd.concat(frames, ignore_index=True)
    df_api["trade_date"] = pd.to_datetime(df_api["trade_date"], format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m-%d")
    df_api = df_api.drop_duplicates(subset=["trade_date", "ts_code", "limit_type"], keep="last")
    date_val = f"{latest[:4]}-{latest[4:6]}-{latest[6:8]}"
    chk_cols = ", ".join(f"`{c.strip()}`" for c in CHECK_FIELDS.split(","))
    with _engine.connect() as conn:
        df_local = pd.read_sql(
            text(f"SELECT {chk_cols} FROM `{TABLE_NAME}` WHERE `trade_date` = :dt"),
            conn,
            params={"dt": date_val},
        )
    if df_local.empty:
        logger.warning("本地无 %s 数据，跳过校验", latest)
        return []
    if len(df_local) != len(df_api):
        logger.warning("校验 %s 行数不一致 本地=%s API=%s", latest, len(df_local), len(df_api))
        return [latest]
    df_local["trade_date"] = pd.to_datetime(df_local["trade_date"]).dt.strftime("%Y-%m-%d")
    merged = df_local.merge(
        df_api,
        on=["trade_date", "ts_code", "limit_type"],
        how="inner",
        suffixes=("_l", "_a"),
    )
    if len(merged) != len(df_local):
        logger.warning("校验 %s 键合并后行数不足", latest)
        return [latest]
    for col in ["price", "pct_chg", "turnover"]:
        lc, ac = f"{col}_l", f"{col}_a"
        if lc not in merged.columns or ac not in merged.columns:
            continue
        a_val = pd.to_numeric(merged[ac], errors="coerce").replace(0, np.nan)
        diff_pct = (
            pd.to_numeric(merged[lc], errors="coerce") - pd.to_numeric(merged[ac], errors="coerce")
        ).abs() / (a_val.abs() + 1e-8)
        if diff_pct.max() > DATA_TOLERANCE:
            logger.warning("校验 %s 字段 %s 数值差异超过容差", latest, col)
            return [latest]
    logger.info("校验 %s 通过 本地=API=%s 条", latest, len(df_local))
    return []


def full_update_by_date(trade_date_yyyymmdd: str) -> int:
    """修复：先删该日业务数据，再拉五池并写入，并补 pool_done。"""
    dsql = f"{trade_date_yyyymmdd[:4]}-{trade_date_yyyymmdd[4:6]}-{trade_date_yyyymmdd[6:8]}"
    with _engine.begin() as conn:
        conn.execute(text(f"DELETE FROM `{TABLE_NAME}` WHERE `trade_date` = :dt"), {"dt": dsql})
        conn.execute(text(f"DELETE FROM `{POOL_DONE_TABLE}` WHERE `trade_date` = :dt"), {"dt": dsql})
    total = 0
    for pool in LIMIT_TYPE_LIST:
        df = fetch_one_pool_day(trade_date_yyyymmdd, pool)
        if df is None:
            logger.error("[全量修复] %s 池 %s 拉取失败，该日数据可能不完整，请重跑", trade_date_yyyymmdd, pool)
            time.sleep(REQUEST_INTERVAL)
            continue
        mark_pool_fetched(trade_date_yyyymmdd, pool)
        if not df.empty:
            total += save_data(df)
        time.sleep(REQUEST_INTERVAL)
    logger.info("[全量修复] %s 写入 %s 条", trade_date_yyyymmdd, total)
    return total


# ============================================================
# 全量 / 增量
# ============================================================
def full_import() -> None:
    """
    自 FULL_START_DATE 起每个交易日×五池；已标记 pool_done 的跳过（含空池）。

    注意：仅当 main() 判定业务表为空时才会调用本函数。若曾清空业务表但未清 pool_done，
    会导致 is_pool_fetched 全为 True、请求次数为 0。因此在业务表行数为 0 时先清空续传标记表。
    """
    with _engine.connect() as conn:
        n_main = conn.execute(text(f"SELECT COUNT(*) FROM `{TABLE_NAME}`")).scalar()
    if int(n_main or 0) == 0:
        with _engine.begin() as conn:
            conn.execute(text(f"DELETE FROM `{POOL_DONE_TABLE}`"))
        logger.info(
            "业务表为空，已清空续传表 `%s`，避免「表已空但 pool_done 仍有标记」导致全量跳过",
            POOL_DONE_TABLE,
        )

    today = datetime.date.today().strftime("%Y%m%d")
    df_cal = _retry_request(pro.trade_cal, exchange="SSE", start_date=FULL_START_DATE, end_date=today, is_open="1")
    if df_cal is None:
        logger.error("获取交易日历失败（网络/接口），终止全量")
        return
    if df_cal.empty:
        logger.error("交易日历为空，终止全量")
        return
    all_dates = df_cal["cal_date"].astype(str).str.replace(r"\D", "", regex=True).tolist()
    all_dates = [d for d in all_dates if len(d) == 8]
    if not all_dates:
        logger.error(
            "交易日历解析后无有效 YYYYMMDD（原始 %s 行），请检查 trade_cal 返回或 FULL_START_DATE=%s",
            len(df_cal),
            FULL_START_DATE,
        )
        return
    total_written = 0
    n_tasks = 0
    for td in all_dates:
        for pool in LIMIT_TYPE_LIST:
            if is_pool_fetched(td, pool):
                continue
            n_tasks += 1
            df = fetch_one_pool_day(td, pool)
            if df is None:
                logger.error("[全量] %s 池 %s 拉取失败，未写入 pool_done，下次将重试", td, pool)
                time.sleep(REQUEST_INTERVAL)
                continue
            mark_pool_fetched(td, pool)
            if not df.empty:
                n = save_data(df)
                total_written += n
            time.sleep(REQUEST_INTERVAL)
    logger.info("全量导入完成：新完成池请求约 %s 次，累计写入行 %s", n_tasks, total_written)


def incremental_import() -> None:
    max_date = get_max_date_from_db()
    if not max_date:
        logger.info("业务表为空，切换全量导入")
        full_import()
        return
    today = datetime.date.today().strftime("%Y%m%d")
    df_cal = _retry_request(pro.trade_cal, exchange="SSE", start_date=max_date, end_date=today, is_open="1")
    if df_cal is None:
        logger.warning("获取交易日历失败（网络/接口），跳过增量")
        return
    if df_cal.empty:
        logger.warning("交易日历为空，跳过增量")
        return
    cal_dates = df_cal["cal_date"].astype(str).str.replace(r"\D", "", regex=True)
    new_dates = [d for d in cal_dates.tolist() if len(d) == 8 and d > max_date]
    if not new_dates:
        logger.info("无新交易日")
        return
    logger.info("增量：%s 个新交易日 %s ~ %s", len(new_dates), new_dates[0], new_dates[-1])
    total_written = 0
    for td in new_dates:
        for pool in LIMIT_TYPE_LIST:
            df = fetch_one_pool_day(td, pool)
            if df is None:
                logger.error("[增量] %s 池 %s 拉取失败，未写入 pool_done", td, pool)
                time.sleep(REQUEST_INTERVAL)
                continue
            mark_pool_fetched(td, pool)
            if not df.empty:
                total_written += save_data(df)
            time.sleep(REQUEST_INTERVAL)
    logger.info("增量完成，写入约 %s 行", total_written)


def main() -> None:
    global pro
    pro = ts.pro_api(TUSHARE_TOKEN)
    create_table()

    logger.info("=" * 60)
    if get_row_count() == 0:
        logger.info("【阶段1】业务表为空，执行全量导入")
        full_import()
    else:
        logger.info("【阶段1】执行增量导入")
        incremental_import()

    logger.info("=" * 60)
    logger.info("【阶段2】一致性校验")
    bad = consistency_check()

    logger.info("=" * 60)
    if not bad:
        logger.info("【阶段3】无需修复")
    else:
        logger.info("【阶段3】按日修复 %s", bad)
        for td in bad:
            full_update_by_date(td)
            time.sleep(REQUEST_INTERVAL)

    logger.info("=" * 60)
    logger.info("全部流程结束")


if __name__ == "__main__":
    main()

# 代码更新时间：2026-03-22 11:41（本地时间）
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
