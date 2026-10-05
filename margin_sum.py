#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口：margin（融资融券交易汇总）
描述：获取融资融券每日交易汇总数据（SSE/SZSE/BSE），入库 margin_sum；按日汇总三所累计值入库 margin_daily
数据库：stock_tushare（用户配置区可改）
数据表：margin_sum（分所明细）、margin_daily（按 trade_date 汇总；含 rzmre_pct、rzmre_pct_per、rzye_free_pct 等）
更新方式：首次全量 margin_sum 后全量计算 margin_daily；日常增量拉取 margin_sum 后对区间内日期增量汇总 margin_daily，且每次执行均重算库内最新交易日 margin_daily（防交易所更新时差）；聚合后按日更新 rzmre_pct_per（相对过去 850 个交易日非空样本的经验分位）

文档：https://tushare.pro/document/2?doc_id=58
限量：单次最大 4000 行，须配合 offset 分页。

仅补 margin_daily 最近 N 日：python margin_sum.py --margin-daily-recent-only [--margin-daily-recent-days 10]

仅补衍生列 rzye_free_pct / rzmre_pct_per（不拉 Tushare、不重算 margin_sum）：
  全量：python margin_sum.py --margin-daily-derived-only --derived-full
  增量：python margin_sum.py --margin-daily-derived-only --derived-recent-days 30
"""

from __future__ import annotations
import os

import argparse
import datetime as dt
import logging
import signal
import sys
import time
from decimal import Decimal
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Callable
from urllib.parse import quote_plus

import numpy as np
import pandas as pd
import tushare as ts
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Engine


# ============================================================
# 日志：与 py 同名，按天轮转，保留 3 天（Windows 安全轮转）
# ============================================================
LOG_FILE = "margin_sum.log"


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


running = True


def handleSignal(signum: int, frame: Any) -> None:
    global running
    running = False
    logging.warning("收到信号 %s，准备安全退出…", signum)


signal.signal(signal.SIGINT, handleSignal)
signal.signal(signal.SIGTERM, handleSignal)


# ============================================================
# 用户配置区
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

TABLE_NAME = "margin_sum"
TABLE_DAILY = "margin_daily"
# 国证 A 指日线，用于 margin_daily.rzmre_pct（成交额 amount 单位为千元，见 index_daily 口径）
TABLE_INDEX_DAILY = "index_daily"
RZMRE_PCT_INDEX_TS_CODE = "399317.SZ"
# 全市场自由流通市值（亿元），与 marketinfo_from_stk_factor_pro 中 free_mv 口径一致
TABLE_MARKETINFO = "marketinfo"
YI_YUAN_TO_YUAN = 100_000_000  # 1 亿元 = 1e8 元（rzye_sum 为元时与 free_mv 换算）
# margin_daily 四个汇总列精度：18 位总长度、2 位小数（足够两融汇总量级，省存储）
MARGIN_DAILY_SUM_PREC = 18
MARGIN_DAILY_SUM_SCALE = 2
# netbuy：当日与上一交易日 rzye_sum 差（元）
MARGIN_DAILY_NETBUY_PREC = 18
MARGIN_DAILY_NETBUY_SCALE = 2
BATCH_SIZE = 500
REQUEST_INTERVAL = 0.2
PAGE_LIMIT = 4000

# 全量起始日（两融汇总历史可按需前推；默认 20100101）
FULL_START_DATE = "20100101"

# 全量按日历分段长度：单段内理论最大行数约「交易日数×3 交易所」，保持远小于 4000
CHUNK_CALENDAR_DAYS = 1000

# 增量：从本地 MAX(trade_date) 向前回滚的自然日数，用于覆盖 SSE/SZSE/BSE 披露/入库时点差异，避免遗漏
INCREMENTAL_LOOKBACK_DAYS = 5

# 增量结束后按库内数据检查是否三类交易所均已落库（与接口约定一致）
EXPECTED_EXCHANGES = frozenset({"SSE", "SZSE", "BSE"})

# margin_daily 聚合门槛：沪深两所必须同时落库，否则该日汇总会腰斩（净买入随之出现假暴跌）。
# 不含 BSE —— 北交所两融自 2023-02-13 才有数据，早期 3000+ 交易日仅 SSE/SZSE，且其余额占比约 0.3%。
MARGIN_DAILY_REQUIRED_EXCHANGES = frozenset({"SSE", "SZSE"})

# 接口文档全部输出字段（显式传入，规则 B）
FIELDS = "trade_date,exchange_id,rzye,rzmre,rzche,rqye,rqmcl,rzrqye,rqyl"

# 一致性校验用字段（数值容差）
DATA_TOLERANCE = 0.001
CHECK_FIELDS = "trade_date,exchange_id,rzye,rzrqye,rqyl"

RETRY_SECONDS = (2, 4, 8)

# 推断失败时的默认 decimal(M,D)（金额元、量大）
DEFAULT_FLOAT_SPECS: dict[str, tuple[int, int]] = {
    "rzye": (26, 4),
    "rzmre": (26, 4),
    "rzche": (26, 4),
    "rqye": (26, 4),
    "rqmcl": (24, 4),
    "rzrqye": (26, 4),
    "rqyl": (24, 4),
}

FLOAT_COLS = (
    "rzye",
    "rzmre",
    "rzche",
    "rqye",
    "rqmcl",
    "rzrqye",
    "rqyl",
)

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


def decimalSpecFromSeries(series: pd.Series, safeIntExtra: int = 3, maxDec: int = 6) -> tuple[int, int]:
    """从样本推断 decimal(M,D)。"""
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
    specs = dict(DEFAULT_FLOAT_SPECS)
    for col in FLOAT_COLS:
        if col in sample_df.columns:
            specs[col] = decimalSpecFromSeries(sample_df[col])
    return specs


def retryRequest(requestFunc: Callable[..., pd.DataFrame], **kwargs: Any) -> pd.DataFrame:
    """失败则指数退避重试最多 3 次。"""
    for attempt, wait_sec in enumerate(RETRY_SECONDS, start=1):
        if not running:
            return pd.DataFrame()
        try:
            return requestFunc(**kwargs)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if attempt == len(RETRY_SECONDS):
                logging.error("[FAILED] 参数=%s 错误=%s", kwargs, exc)
                return pd.DataFrame()
            logging.warning("请求失败，第 %s 次重试前等待 %ss：%s", attempt, wait_sec, exc)
            time.sleep(wait_sec)
    return pd.DataFrame()


def _queryMargin(
    pro: Any,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    trade_date: str | None = None,
    limit: int = PAGE_LIMIT,
    offset: int = 0,
) -> pd.DataFrame:
    """统一走 pro.query(api_name, fields, **kwargs)，便于 limit/offset 分页。单日勿与 start/end 混用。"""
    if trade_date:
        df = pro.query(
            "margin",
            FIELDS,
            trade_date=trade_date,
            limit=limit,
            offset=offset,
        )
    elif start_date and end_date:
        df = pro.query(
            "margin",
            FIELDS,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )
    else:
        return pd.DataFrame()
    return df if df is not None else pd.DataFrame()


def fetchRangeWithPaging(
    pro: Any, start_date: str, end_date: str, label: str = ""
) -> pd.DataFrame:
    """
    按区间拉取 margin，遇 4000 行满页则 offset 续拉，直至取完（规则 A：防截断）。
    """
    frames: list[pd.DataFrame] = []
    offset = 0
    while running:
        page = retryRequest(
            lambda **kw: _queryMargin(pro, **kw),
            start_date=start_date,
            end_date=end_date,
            limit=PAGE_LIMIT,
            offset=offset,
        )
        if page.empty:
            break
        frames.append(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        logging.info(
            "%s 分段 %s~%s 分页 offset=%s 已取 %s 行，继续…",
            label,
            start_date,
            end_date,
            offset,
            len(page),
        )
        time.sleep(REQUEST_INTERVAL)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def processDf(df: pd.DataFrame) -> pd.DataFrame:
    """日期转 date 字符串、数值 NaN 转 None。"""
    if df.empty:
        return df
    out = df.copy()
    if "trade_date" in out.columns:
        out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce")
        out["trade_date"] = out["trade_date"].dt.strftime("%Y-%m-%d")
        out["trade_date"] = out["trade_date"].where(out["trade_date"].notna(), None)
    if "exchange_id" in out.columns:
        out["exchange_id"] = out["exchange_id"].astype(str).str.strip()
    out = out.where(pd.notna(out), None)
    out = out.replace({np.nan: None})
    return out


def buildCreateTableSql(specs: dict[str, tuple[int, int]]) -> str:
    """生成建表 DDL（含注释与唯一键）。"""
    parts = [
        f"`rzye` decimal({specs['rzye'][0]},{specs['rzye'][1]}) DEFAULT NULL COMMENT '融资余额(元)'",
        f"`rzmre` decimal({specs['rzmre'][0]},{specs['rzmre'][1]}) DEFAULT NULL COMMENT '融资买入额(元)'",
        f"`rzche` decimal({specs['rzche'][0]},{specs['rzche'][1]}) DEFAULT NULL COMMENT '融资偿还额(元)'",
        f"`rqye` decimal({specs['rqye'][0]},{specs['rqye'][1]}) DEFAULT NULL COMMENT '融券余额(元)'",
        f"`rqmcl` decimal({specs['rqmcl'][0]},{specs['rqmcl'][1]}) DEFAULT NULL COMMENT '融券卖出量(股/份/手)'",
        f"`rzrqye` decimal({specs['rzrqye'][0]},{specs['rzrqye'][1]}) DEFAULT NULL COMMENT '融资融券余额(元)'",
        f"`rqyl` decimal({specs['rqyl'][0]},{specs['rqyl'][1]}) DEFAULT NULL COMMENT '融券余量(股/份/手)'",
    ]
    col_sql = ",\n  ".join(parts)
    return f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `trade_date` date NOT NULL COMMENT '交易日期',
  `exchange_id` varchar(10) NOT NULL COMMENT '交易所代码(SSE/SZSE/BSE)',
  {col_sql},
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_trade_exchange` (`trade_date`,`exchange_id`),
  KEY `idx_trade_date` (`trade_date`),
  KEY `idx_exchange` (`exchange_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='融资融券每日交易汇总(margin)';
""".strip()


def createTable(pro: Any, engine: Engine) -> None:
    """抽样推断精度后建表。"""
    sample = retryRequest(
        lambda **kw: _queryMargin(pro, **kw),
        start_date="20230101",
        end_date=dt.date.today().strftime("%Y%m%d"),
        limit=200,
        offset=0,
    )
    if not sample.empty:
        sample = processDf(sample)
    specs = inferFloatSpecs(sample) if not sample.empty else DEFAULT_FLOAT_SPECS
    ddl = buildCreateTableSql(specs)
    with engine.begin() as conn:
        conn.execute(text(ddl))
    logging.info("表 %s 已就绪（或已存在）。", TABLE_NAME)


def saveData(engine: Engine, df: pd.DataFrame) -> int:
    """INSERT ON DUPLICATE KEY UPDATE，批量写入。"""
    if df.empty:
        return 0
    cols = [c for c in df.columns if c in FLOAT_COLS or c in ("trade_date", "exchange_id")]
    col_names = ", ".join(f"`{c}`" for c in cols)
    placeholders = ", ".join(f":{c}" for c in cols)
    update_part = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in cols if c not in ("trade_date", "exchange_id"))
    sql = f"""
INSERT INTO `{TABLE_NAME}` ({col_names})
VALUES ({placeholders})
ON DUPLICATE KEY UPDATE {update_part}
"""
    records = df[cols].to_dict("records")
    total = 0
    with engine.begin() as conn:
        for i in range(0, len(records), BATCH_SIZE):
            if not running:
                break
            batch = records[i : i + BATCH_SIZE]
            for rec in batch:
                conn.execute(text(sql), rec)
            total += len(batch)
    return total


def tableIsEmpty(engine: Engine) -> bool:
    with engine.connect() as conn:
        n = conn.execute(text(f"SELECT COUNT(*) FROM `{TABLE_NAME}`")).scalar()
    return int(n or 0) == 0


def getLocalMaxTradeDateYmd(engine: Engine) -> str | None:
    """库内最大交易日 YYYYMMDD。"""
    with engine.connect() as conn:
        v = conn.execute(
            text(f"SELECT DATE_FORMAT(MAX(trade_date), '%Y%m%d') FROM `{TABLE_NAME}`")
        ).scalar()
    return str(v) if v else None


def ensureMarginDailySumColumnsPrecision(engine: Engine) -> None:
    """
    已存在旧表（如 decimal(28,4)）时，将四个 _sum 列幂等改为 DECIMAL(18,2)。
    新建表已用 18,2 时跳过。
    """
    sum_meta: dict[str, str] = {
        "rzye_sum": "该日各所 rzye 合计(元)",
        "rzmre_sum": "该日各所 rzmre 合计(元)",
        "rqye_sum": "该日各所 rqye 合计(元)",
        "rzrqye_sum": "该日各所 rzrqye 合计(元)",
    }
    with engine.begin() as conn:
        tbl_exists = conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = :db AND table_name = :tb"
            ),
            {"db": DB_CONFIG["database"], "tb": TABLE_DAILY},
        ).scalar()
        if not int(tbl_exists or 0):
            return
        for col, cmt in sum_meta.items():
            row = conn.execute(
                text(
                    "SELECT NUMERIC_PRECISION, NUMERIC_SCALE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :tb AND COLUMN_NAME = :col"
                ),
                {"db": DB_CONFIG["database"], "tb": TABLE_DAILY, "col": col},
            ).fetchone()
            if not row:
                continue
            prec, scale = int(row[0] or 0), int(row[1] or 0)
            if prec == MARGIN_DAILY_SUM_PREC and scale == MARGIN_DAILY_SUM_SCALE:
                continue
            logging.info(
                "升级 `%s`.`%s` → DECIMAL(%s,%s)（原 %s,%s）",
                TABLE_DAILY,
                col,
                MARGIN_DAILY_SUM_PREC,
                MARGIN_DAILY_SUM_SCALE,
                prec,
                scale,
            )
            cmt_esc = cmt.replace("\\", "\\\\").replace("'", "''")
            conn.execute(
                text(
                    f"ALTER TABLE `{TABLE_DAILY}` MODIFY COLUMN `{col}` "
                    f"DECIMAL({MARGIN_DAILY_SUM_PREC},{MARGIN_DAILY_SUM_SCALE}) "
                    f"DEFAULT NULL COMMENT '{cmt_esc}'"
                )
            )


def createMarginDailyTable(engine: Engine) -> None:
    """建表 margin_daily：按 trade_date 汇总三所；rzmre_pct 由国证A指；rzye_free_pct 由 marketinfo.free_mv（亿元）与 rzye_sum（元）换算。"""
    dm, ds = MARGIN_DAILY_SUM_PREC, MARGIN_DAILY_SUM_SCALE
    nb_m, nb_s = MARGIN_DAILY_NETBUY_PREC, MARGIN_DAILY_NETBUY_SCALE
    ddl = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_DAILY}` (
  `trade_date` date NOT NULL COMMENT '交易日期',
  `rzye_sum` decimal({dm},{ds}) DEFAULT NULL COMMENT '该日各所 rzye 合计(元)',
  `rzmre_sum` decimal({dm},{ds}) DEFAULT NULL COMMENT '该日各所 rzmre 合计(元)',
  `rqye_sum` decimal({dm},{ds}) DEFAULT NULL COMMENT '该日各所 rqye 合计(元)',
  `rzrqye_sum` decimal({dm},{ds}) DEFAULT NULL COMMENT '该日各所 rzrqye 合计(元)',
  `rzmre_pct` decimal(6,2) DEFAULT NULL COMMENT '融资买入额占国证A指成交额比(%)，rzmre_sum元/(399317.SZ amount千元×1000)×100',
  `rzmre_pct_per` decimal(6,2) DEFAULT NULL COMMENT 'rzmre_pct在过去850个交易日(非空样本)中的经验分位(%)，ECDF×100',
  `netbuy` decimal({nb_m},{nb_s}) DEFAULT NULL COMMENT '融资余额日变动(元)：当日rzye_sum-上一交易日rzye_sum',
  `netbuy_pct` decimal(6,2) DEFAULT NULL COMMENT '融资净买入占融资买入额比(%)：netbuy/rzmre_sum×100',
  `rzye_free_pct` decimal(6,2) DEFAULT NULL COMMENT '融资余额占自由流通市值比(%)：rzye_sum元/(marketinfo.free_mv亿元×10^8)×100',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`trade_date`),
  KEY `idx_trade_date` (`trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='融资融券按日汇总(由margin_sum聚合)';
""".strip()
    with engine.begin() as conn:
        conn.execute(text(ddl))
    ensureMarginDailySumColumnsPrecision(engine)
    ensureMarginDailyRzmrePctColumn(engine)
    ensureMarginDailyNetbuyColumn(engine)
    ensureMarginDailyNetbuyPctColumn(engine)
    ensureMarginDailyRzmrePctPerColumn(engine)
    ensureMarginDailyRzyeFreePctColumn(engine)
    logging.info("表 %s 已就绪（或已存在）。", TABLE_DAILY)


def ensureMarginDailyRzmrePctColumn(engine: Engine) -> None:
    """旧表补列：rzmre_pct = rzmre_sum(元) / (index_daily.amount(千元)*1000) * 100。"""
    with engine.begin() as conn:
        cnt = conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :tb AND COLUMN_NAME = 'rzmre_pct'"
            ),
            {"db": DB_CONFIG["database"], "tb": TABLE_DAILY},
        ).scalar()
        if int(cnt or 0):
            return
        conn.execute(
            text(
                f"ALTER TABLE `{TABLE_DAILY}` ADD COLUMN `rzmre_pct` DECIMAL(6,2) NULL "
                f"COMMENT '融资买入额占国证A指成交额比(%%)，rzmre_sum元/(amount千元×1000)×100' "
                f"AFTER `rzrqye_sum`"
            )
        )
        logging.info("已为 `%s` 增加列 rzmre_pct。", TABLE_DAILY)


def ensureMarginDailyNetbuyColumn(engine: Engine) -> None:
    """旧表补列：netbuy = 当日 rzye_sum - 上一交易日 rzye_sum（元）。"""
    nb_m, nb_s = MARGIN_DAILY_NETBUY_PREC, MARGIN_DAILY_NETBUY_SCALE
    with engine.begin() as conn:
        cnt = conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :tb AND COLUMN_NAME = 'netbuy'"
            ),
            {"db": DB_CONFIG["database"], "tb": TABLE_DAILY},
        ).scalar()
        if int(cnt or 0):
            return
        conn.execute(
            text(
                f"ALTER TABLE `{TABLE_DAILY}` ADD COLUMN `netbuy` DECIMAL({nb_m},{nb_s}) NULL "
                f"COMMENT '融资余额日变动(元)：当日rzye_sum-上一交易日rzye_sum' "
                f"AFTER `rzmre_pct`"
            )
        )
        logging.info("已为 `%s` 增加列 netbuy。", TABLE_DAILY)


def ensureMarginDailyNetbuyPctColumn(engine: Engine) -> None:
    """旧表补列：netbuy_pct = netbuy / rzmre_sum * 100（%），DECIMAL(6,2)。"""
    with engine.begin() as conn:
        cnt = conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :tb AND COLUMN_NAME = 'netbuy_pct'"
            ),
            {"db": DB_CONFIG["database"], "tb": TABLE_DAILY},
        ).scalar()
        if int(cnt or 0):
            return
        conn.execute(
            text(
                f"ALTER TABLE `{TABLE_DAILY}` ADD COLUMN `netbuy_pct` DECIMAL(6,2) NULL "
                f"COMMENT '融资净买入占融资买入额比(%%)：netbuy/rzmre_sum×100' "
                f"AFTER `netbuy`"
            )
        )
        logging.info("已为 `%s` 增加列 netbuy_pct。", TABLE_DAILY)


def ensureMarginDailyRzmrePctPerColumn(engine: Engine) -> None:
    """旧表补列：rzmre_pct 相对过去 850 个交易日（非空样本）的经验分位(%)。"""
    with engine.begin() as conn:
        cnt = conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :tb AND COLUMN_NAME = 'rzmre_pct_per'"
            ),
            {"db": DB_CONFIG["database"], "tb": TABLE_DAILY},
        ).scalar()
        if int(cnt or 0):
            return
        conn.execute(
            text(
                f"ALTER TABLE `{TABLE_DAILY}` ADD COLUMN `rzmre_pct_per` DECIMAL(6,2) NULL "
                f"COMMENT 'rzmre_pct在过去850个交易日(非空样本)中的经验分位(%%)，ECDF×100' "
                f"AFTER `rzmre_pct`"
            )
        )
        logging.info("已为 `%s` 增加列 rzmre_pct_per。", TABLE_DAILY)


def ensureMarginDailyRzyeFreePctColumn(engine: Engine) -> None:
    """旧表补列：融资余额占 marketinfo.free_mv（亿元）折算为元后的比例(%)。"""
    with engine.begin() as conn:
        cnt = conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :tb AND COLUMN_NAME = 'rzye_free_pct'"
            ),
            {"db": DB_CONFIG["database"], "tb": TABLE_DAILY},
        ).scalar()
        if int(cnt or 0):
            return
        conn.execute(
            text(
                f"ALTER TABLE `{TABLE_DAILY}` ADD COLUMN `rzye_free_pct` DECIMAL(6,2) NULL "
                f"COMMENT '融资余额占自由流通市值比(%%)：rzye_sum元/(marketinfo.free_mv亿元×10^8)×100' "
                f"AFTER `netbuy_pct`"
            )
        )
        logging.info("已为 `%s` 增加列 rzye_free_pct。", TABLE_DAILY)


def _sql_required_exchanges_conds() -> str:
    """HAVING 条件：MARGIN_DAILY_REQUIRED_EXCHANGES 中每个交易所当日至少一行。"""
    return " AND ".join(
        f"SUM(CASE WHEN `exchange_id` = '{ex}' THEN 1 ELSE 0 END) > 0"
        for ex in sorted(MARGIN_DAILY_REQUIRED_EXCHANGES)
    )


def _sql_margin_sum_daily_agg() -> str:
    """margin_sum 按日汇总的内层子查询（三处 margin_daily 聚合共用）。

    HAVING 只放行 MARGIN_DAILY_REQUIRED_EXCHANGES 均已落库的交易日：沪深披露有时差，
    早盘只拉到单边时该日不参与聚合，待另一所到齐后由下次运行自动补写。
    """
    conds = _sql_required_exchanges_conds()
    return f"""
            SELECT
              `trade_date`,
              SUM(COALESCE(`rzye`, 0)) AS `rzye_sum`,
              SUM(COALESCE(`rzmre`, 0)) AS `rzmre_sum`,
              SUM(COALESCE(`rqye`, 0)) AS `rqye_sum`,
              SUM(COALESCE(`rzrqye`, 0)) AS `rzrqye_sum`
            FROM `{TABLE_NAME}`
            GROUP BY `trade_date`
            HAVING {conds}
""".strip()


def _sql_netbuy_lag_expr(ms_alias: str = "ms") -> str:
    """全历史按日 rzye_sum 序列上的日环比（首日为 NULL）。MySQL 8+ 窗口函数。"""
    nb_m, nb_s = MARGIN_DAILY_NETBUY_PREC, MARGIN_DAILY_NETBUY_SCALE
    return f"""
CAST(
  ROUND(
    {ms_alias}.`rzye_sum` - LAG({ms_alias}.`rzye_sum`) OVER (ORDER BY {ms_alias}.`trade_date`),
    {nb_s}
  ) AS DECIMAL({nb_m},{nb_s})
)""".strip()


def _sql_rzmre_pct_expr(agg_alias: str = "agg") -> str:
    """与 index_daily 399317.SZ 的 amount（千元）联算百分比。"""
    return f"""
  CASE
    WHEN idx.`amount` IS NULL OR idx.`amount` = 0 THEN NULL
    ELSE CAST(
      ROUND(
        {agg_alias}.`rzmre_sum` / (idx.`amount` * 1000) * 100,
        2
      ) AS DECIMAL(6,2)
    )
  END""".strip()


def _sql_netbuy_pct_expr(agg_alias: str = "d") -> str:
    """netbuy 与 rzmre_sum 相除乘 100，百分比保留两位；分母为 0 或 NULL 则为 NULL。"""
    a = agg_alias
    return f"""
  CASE
    WHEN {a}.`rzmre_sum` IS NULL OR {a}.`rzmre_sum` = 0 THEN NULL
    ELSE CAST(
      ROUND(
        {a}.`netbuy` / {a}.`rzmre_sum` * 100,
        2
      ) AS DECIMAL(6,2)
    )
  END""".strip()


def _sql_rzye_free_pct_expr(agg_alias: str = "d") -> str:
    """rzye_sum(元) 相对当日全市场自由流通市值：free_mv 为亿元，先 ×10^8 为元再除。"""
    a = agg_alias
    return f"""
  CASE
    WHEN mi.`free_mv` IS NULL OR mi.`free_mv` = 0 THEN NULL
    ELSE CAST(
      ROUND(
        {a}.`rzye_sum` / (mi.`free_mv` * {YI_YUAN_TO_YUAN}) * 100,
        2
      ) AS DECIMAL(6,2)
    )
  END""".strip()


def tableIsEmptyMarginDaily(engine: Engine) -> bool:
    with engine.connect() as conn:
        n = conn.execute(text(f"SELECT COUNT(*) FROM `{TABLE_DAILY}`")).scalar()
    return int(n or 0) == 0


def rebuildMarginDailyFull(engine: Engine) -> int:
    """
    margin_daily 全量：TRUNCATE 后按 margin_sum 全历史 GROUP BY 重算。
    首次上线或 margin_sum 全量导入后调用。
    """
    lag = _sql_netbuy_lag_expr("ms")
    pct = _sql_rzmre_pct_expr("d")
    nb_pct = _sql_netbuy_pct_expr("d")
    rf_pct = _sql_rzye_free_pct_expr("d")
    agg_inner = _sql_margin_sum_daily_agg()
    agg = text(
        f"""
        INSERT INTO `{TABLE_DAILY}` (
          `trade_date`, `rzye_sum`, `rzmre_sum`, `rqye_sum`, `rzrqye_sum`, `rzmre_pct`, `netbuy`, `netbuy_pct`, `rzye_free_pct`
        )
        SELECT
          d.`trade_date`,
          d.`rzye_sum`,
          d.`rzmre_sum`,
          d.`rqye_sum`,
          d.`rzrqye_sum`,
          {pct},
          d.`netbuy`,
          {nb_pct},
          {rf_pct}
        FROM (
          SELECT
            ms.`trade_date`,
            ms.`rzye_sum`,
            ms.`rzmre_sum`,
            ms.`rqye_sum`,
            ms.`rzrqye_sum`,
            {lag} AS `netbuy`
          FROM (
            {agg_inner}
          ) AS ms
        ) AS d
        LEFT JOIN `{TABLE_INDEX_DAILY}` idx
          ON idx.`trade_date` = d.`trade_date`
         AND idx.`ts_code` = :idx_code
        LEFT JOIN `{TABLE_MARKETINFO}` mi
          ON mi.`trade_date` = d.`trade_date`
        """
    )
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE `{TABLE_DAILY}`"))
        r = conn.execute(agg, {"idx_code": RZMRE_PCT_INDEX_TS_CODE})
        inserted = r.rowcount if r is not None else 0
    logging.info("margin_daily 全量重算完成，写入行数=%s。", inserted)
    return int(inserted or 0)


def refreshMarginDailyIncremental(engine: Engine, start_ymd: str, end_ymd: str) -> int:
    """
    margin_daily 增量：对 [start_ymd, end_ymd] 内有数据的 trade_date 重算汇总；
    并始终包含 margin_sum 中 MAX(trade_date) 当日（即使与区间重叠），避免交易所更新时差导致最新日未刷新。
    """
    d0 = dt.datetime.strptime(start_ymd, "%Y%m%d").date()
    d1 = dt.datetime.strptime(end_ymd, "%Y%m%d").date()
    lag = _sql_netbuy_lag_expr("ms")
    pct = _sql_rzmre_pct_expr("d")
    nb_pct = _sql_netbuy_pct_expr("d")
    rf_pct = _sql_rzye_free_pct_expr("d")
    agg_inner = _sql_margin_sum_daily_agg()
    upsert = text(
        f"""
        INSERT INTO `{TABLE_DAILY}` (
          `trade_date`, `rzye_sum`, `rzmre_sum`, `rqye_sum`, `rzrqye_sum`, `rzmre_pct`, `netbuy`, `netbuy_pct`, `rzye_free_pct`
        )
        SELECT
          d.`trade_date`,
          d.`rzye_sum`,
          d.`rzmre_sum`,
          d.`rqye_sum`,
          d.`rzrqye_sum`,
          {pct},
          d.`netbuy`,
          {nb_pct},
          {rf_pct}
        FROM (
          SELECT
            ms.`trade_date`,
            ms.`rzye_sum`,
            ms.`rzmre_sum`,
            ms.`rqye_sum`,
            ms.`rzrqye_sum`,
            {lag} AS `netbuy`
          FROM (
            {agg_inner}
          ) AS ms
        ) AS d
        LEFT JOIN `{TABLE_INDEX_DAILY}` idx
          ON idx.`trade_date` = d.`trade_date`
         AND idx.`ts_code` = :idx_code
        LEFT JOIN `{TABLE_MARKETINFO}` mi
          ON mi.`trade_date` = d.`trade_date`
        WHERE (d.`trade_date` >= :d0 AND d.`trade_date` <= :d1)
           OR d.`trade_date` = (SELECT MAX(`trade_date`) FROM `{TABLE_NAME}`)
        ON DUPLICATE KEY UPDATE
          `rzye_sum` = VALUES(`rzye_sum`),
          `rzmre_sum` = VALUES(`rzmre_sum`),
          `rqye_sum` = VALUES(`rqye_sum`),
          `rzrqye_sum` = VALUES(`rzrqye_sum`),
          `rzmre_pct` = VALUES(`rzmre_pct`),
          `netbuy` = VALUES(`netbuy`),
          `netbuy_pct` = VALUES(`netbuy_pct`),
          `rzye_free_pct` = VALUES(`rzye_free_pct`)
        """
    )
    with engine.begin() as conn:
        r = conn.execute(upsert, {"d0": d0, "d1": d1, "idx_code": RZMRE_PCT_INDEX_TS_CODE})
        n = r.rowcount if r is not None else 0
    purgeIncompleteMarginDailyRows(engine)
    max_d = getLocalMaxTradeDateYmd(engine)
    logging.info(
        "margin_daily 增量更新完成（区间含最新日重算），影响行约=%s；当前 margin_sum 最新交易日=%s。",
        n,
        max_d or "(无)",
    )
    return int(n or 0)


def refreshMarginDailyLastNDistinctTradeDates(engine: Engine, n: int) -> int:
    """
    从 margin_sum 取最近 n 个不同 trade_date（按日期倒序），聚合后写入 margin_daily（UPSERT）。
    不 TRUNCATE 整张表；仅更新这 n 个交易日对应行。
    """
    n = int(n)
    if n <= 0:
        return 0
    n = min(n, 5000)
    lag = _sql_netbuy_lag_expr("ms")
    pct = _sql_rzmre_pct_expr("d")
    nb_pct = _sql_netbuy_pct_expr("d")
    rf_pct = _sql_rzye_free_pct_expr("d")
    agg_inner = _sql_margin_sum_daily_agg()
    upsert = text(
        f"""
        INSERT INTO `{TABLE_DAILY}` (
          `trade_date`, `rzye_sum`, `rzmre_sum`, `rqye_sum`, `rzrqye_sum`, `rzmre_pct`, `netbuy`, `netbuy_pct`, `rzye_free_pct`
        )
        SELECT
          d.`trade_date`,
          d.`rzye_sum`,
          d.`rzmre_sum`,
          d.`rqye_sum`,
          d.`rzrqye_sum`,
          {pct},
          d.`netbuy`,
          {nb_pct},
          {rf_pct}
        FROM (
          SELECT
            ms.`trade_date`,
            ms.`rzye_sum`,
            ms.`rzmre_sum`,
            ms.`rqye_sum`,
            ms.`rzrqye_sum`,
            {lag} AS `netbuy`
          FROM (
            {agg_inner}
          ) AS ms
        ) AS d
        LEFT JOIN `{TABLE_INDEX_DAILY}` idx
          ON idx.`trade_date` = d.`trade_date`
         AND idx.`ts_code` = :idx_code
        LEFT JOIN `{TABLE_MARKETINFO}` mi
          ON mi.`trade_date` = d.`trade_date`
        WHERE d.`trade_date` IN (
          SELECT tt.`trade_date` FROM (
            SELECT `trade_date` FROM `{TABLE_NAME}` GROUP BY `trade_date` ORDER BY `trade_date` DESC LIMIT {n}
          ) AS tt
        )
        ON DUPLICATE KEY UPDATE
          `rzye_sum` = VALUES(`rzye_sum`),
          `rzmre_sum` = VALUES(`rzmre_sum`),
          `rqye_sum` = VALUES(`rqye_sum`),
          `rzrqye_sum` = VALUES(`rzrqye_sum`),
          `rzmre_pct` = VALUES(`rzmre_pct`),
          `netbuy` = VALUES(`netbuy`),
          `netbuy_pct` = VALUES(`netbuy_pct`),
          `rzye_free_pct` = VALUES(`rzye_free_pct`)
        """
    )
    with engine.begin() as conn:
        r = conn.execute(upsert, {"idx_code": RZMRE_PCT_INDEX_TS_CODE})
        affected = r.rowcount if r is not None else 0
    purgeIncompleteMarginDailyRows(engine)
    logging.info(
        "margin_daily 已按 margin_sum 最近 %s 个交易日聚合写入（UPSERT），影响行约=%s。",
        n,
        affected,
    )
    return int(affected or 0)


def purgeIncompleteMarginDailyRows(engine: Engine) -> int:
    """删除 margin_daily 中「margin_sum 已有该日、但沪深未齐」的残留行。

    聚合侧 HAVING 只能阻止新写入；若此前某次运行在单边数据下已写入腰斩行，
    该行不会再被 UPSERT 覆盖（已被 HAVING 排除），需在此清除，
    否则前端图上会一直留着一根假暴跌柱。数据到齐后由下次聚合自动补回。
    """
    conds = _sql_required_exchanges_conds()
    sel_incomplete = text(
        f"""
        SELECT d.`trade_date`
        FROM `{TABLE_DAILY}` d
        WHERE d.`trade_date` IN (
          SELECT t.`trade_date` FROM (
            SELECT `trade_date` FROM `{TABLE_NAME}`
            GROUP BY `trade_date`
            HAVING NOT ({conds})
          ) AS t
        )
        ORDER BY d.`trade_date`
        """
    )
    with engine.connect() as conn:
        bad_dates = [r[0] for r in conn.execute(sel_incomplete).fetchall()]
    if not bad_dates:
        return 0
    shown = ", ".join(
        d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10] for d in bad_dates[:10]
    )
    logging.warning(
        "margin_daily 清理不完整日 %s 条（沪深未集齐，聚合值会腰斩）：%s%s；待数据到齐后重跑本脚本自动补回。",
        len(bad_dates),
        shown,
        " ..." if len(bad_dates) > 10 else "",
    )
    with engine.begin() as conn:
        r = conn.execute(
            text(f"DELETE FROM `{TABLE_DAILY}` WHERE `trade_date` IN :ds").bindparams(
                bindparam("ds", expanding=True)
            ),
            {"ds": bad_dates},
        )
        deleted = r.rowcount if r is not None else 0
    return int(deleted or 0)


# rzmre_pct_per：滚动样本为「当前交易日之前、rzmre_pct 非空」的最近 N 条（按 trade_date 倒序取 N）
RZMRE_PCT_PER_WINDOW_TRADING_DAYS = 850


def lastNMarginSumTradeDates(engine: Engine, n: int) -> list[dt.date]:
    """与 refreshMarginDailyLastNDistinctTradeDates 子查询一致：margin_sum 最近 n 个不同 trade_date（升序）。"""
    n = int(n)
    if n <= 0:
        return []
    n = min(n, 5000)
    sql = text(
        f"""
        SELECT `trade_date` FROM `{TABLE_NAME}`
        GROUP BY `trade_date`
        ORDER BY `trade_date` DESC
        LIMIT :n
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"n": n}).fetchall()
    out: list[dt.date] = []
    for r in rows:
        x = r[0]
        if isinstance(x, dt.datetime):
            out.append(x.date())
        elif isinstance(x, dt.date):
            out.append(x)
        else:
            out.append(pd.Timestamp(x).date())
    return sorted(out)


def distinctTradeDatesForIncrementalRzmrePctPer(
    engine: Engine, inc_start_ymd: str, inc_end_ymd: str
) -> list[dt.date]:
    """
    增量聚合后需重算 rzmre_pct_per 的交易日：区间内 margin_daily 已有行 + 强制包含 margin_sum 最新日（与 refreshMarginDailyIncremental 的 WHERE 一致）。
    """
    d0 = dt.datetime.strptime(inc_start_ymd, "%Y%m%d").date()
    d1 = dt.datetime.strptime(inc_end_ymd, "%Y%m%d").date()
    sql = text(
        f"""
        SELECT DISTINCT d.`trade_date`
        FROM `{TABLE_DAILY}` AS d
        WHERE (d.`trade_date` >= :d0 AND d.`trade_date` <= :d1)
           OR d.`trade_date` = (SELECT MAX(`trade_date`) FROM `{TABLE_NAME}`)
        ORDER BY d.`trade_date` ASC
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"d0": d0, "d1": d1}).fetchall()
    out: list[dt.date] = []
    for r in rows:
        x = r[0]
        if isinstance(x, dt.datetime):
            out.append(x.date())
        elif isinstance(x, dt.date):
            out.append(x)
        else:
            out.append(pd.Timestamp(x).date())
    return sorted(set(out))


def updateMarginDailyRzyeFreePctForDates(engine: Engine, dates: list[dt.date] | None) -> int:
    """
    从 stock_tushare.marketinfo 回填 margin_daily.rzye_free_pct（UPDATE JOIN，不重新聚合 margin_sum）。
    适用于 marketinfo 晚于 margin_daily 入库、或列新增后需一次性补数。
    dates 为 None 时更新全表；否则仅更新指定 trade_date（可批量 IN）。
    """
    case_set = (
        f"d.`rzye_free_pct` = CASE "
        f"WHEN mi.`free_mv` IS NULL OR mi.`free_mv` = 0 THEN NULL "
        f"ELSE CAST(ROUND(d.`rzye_sum` / (mi.`free_mv` * {YI_YUAN_TO_YUAN}) * 100, 2) AS DECIMAL(6,2)) "
        f"END"
    )
    total = 0
    if dates is None:
        stmt = text(
            f"""
            UPDATE `{TABLE_DAILY}` d
            LEFT JOIN `{TABLE_MARKETINFO}` mi ON mi.`trade_date` = d.`trade_date`
            SET {case_set}
            """
        )
        with engine.begin() as conn:
            r = conn.execute(stmt)
            total = int(r.rowcount or 0)
        logging.info("rzye_free_pct：全表从 marketinfo 回填完成，影响行约 %s。", total)
        return total
    if not dates:
        logging.info("rzye_free_pct：无指定日期，跳过。")
        return 0
    chunk_size = 800
    for i in range(0, len(dates), chunk_size):
        part = dates[i : i + chunk_size]
        ph = ",".join([f":d{j}" for j in range(len(part))])
        params: dict[str, object] = {f"d{j}": part[j] for j in range(len(part))}
        stmt = text(
            f"""
            UPDATE `{TABLE_DAILY}` d
            LEFT JOIN `{TABLE_MARKETINFO}` mi ON mi.`trade_date` = d.`trade_date`
            SET {case_set}
            WHERE d.`trade_date` IN ({ph})
            """
        )
        with engine.begin() as conn:
            r = conn.execute(stmt, params)
            total += int(r.rowcount or 0)
    logging.info(
        "rzye_free_pct：已按 %s 个交易日从 marketinfo 回填，累计影响行约 %s。",
        len(dates),
        total,
    )
    return total


def updateMarginDailyRzmrePctPerForDates(
    engine: Engine, dates: list[dt.date] | None
) -> int:
    """
    对指定 trade_date（或全表 rzmre_pct 非空行）写入 rzmre_pct_per。
    经验分位（ECDF×100）：在「当前日之前」取 rzmre_pct 非空的最近 N 个交易日（N=RZMRE_PCT_PER_WINDOW_TRADING_DAYS），
    再数其中 <= 当前 rzmre_pct 的占比×100；不含当日，样本量不足则跳过。
    """
    n_win = RZMRE_PCT_PER_WINDOW_TRADING_DAYS
    hist_sql = text(
        f"""
        SELECT `rzmre_pct` FROM `{TABLE_DAILY}`
        WHERE `rzmre_pct` IS NOT NULL
          AND `trade_date` < :t_cur
        ORDER BY `trade_date` DESC
        LIMIT {n_win}
        """
    )
    cur_sql = text(
        f"SELECT `rzmre_pct` FROM `{TABLE_DAILY}` WHERE `trade_date` = :td LIMIT 1"
    )
    upd_sql = text(
        f"UPDATE `{TABLE_DAILY}` SET `rzmre_pct_per` = :pv WHERE `trade_date` = :td"
    )
    if dates is None:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT `trade_date` FROM `{TABLE_DAILY}`
                    WHERE `rzmre_pct` IS NOT NULL
                    ORDER BY `trade_date` ASC
                    """
                )
            ).fetchall()
        date_list = []
        for r in rows:
            x = r[0]
            if isinstance(x, dt.datetime):
                date_list.append(x.date())
            elif isinstance(x, dt.date):
                date_list.append(x)
            else:
                date_list.append(pd.Timestamp(x).date())
    else:
        date_list = sorted(set(dates))
    if not date_list:
        logging.info("rzmre_pct_per：无待处理交易日，跳过。")
        return 0
    logging.info(
        "rzmre_pct_per：开始计算 %s 个交易日（全量模式=%s）。",
        len(date_list),
        dates is None,
    )
    updated = 0
    log_every = 100
    processed = 0
    conn = engine.connect()
    try:
        for trade_date in date_list:
            processed += 1
            cur_row = conn.execute(cur_sql, {"td": trade_date}).fetchone()
            if not cur_row or cur_row[0] is None:
                continue
            current_val = cur_row[0]
            try:
                current_f = float(current_val)
            except (TypeError, ValueError):
                logging.warning("rzmre_pct_per：%s 当前 rzmre_pct 非数值，跳过。", trade_date)
                continue
            if np.isnan(current_f):
                continue
            hist_rows = conn.execute(hist_sql, {"t_cur": trade_date}).fetchall()
            historical_values = []
            for hr in hist_rows:
                if hr[0] is None:
                    continue
                try:
                    hf = float(hr[0])
                except (TypeError, ValueError):
                    continue
                if not np.isnan(hf):
                    historical_values.append(hf)
            if len(historical_values) == 0:
                if processed <= 3 or processed % log_every == 0:
                    logging.debug(
                        "rzmre_pct_per：%s 历史样本为空，跳过（需早于当日的非空样本）。",
                        trade_date,
                    )
                continue
            count_le = sum(1 for v in historical_values if v <= current_f)
            percentile_rank = (count_le / len(historical_values)) * 100.0
            pv = round(percentile_rank, 2)
            conn.execute(upd_sql, {"pv": pv, "td": trade_date})
            conn.commit()
            updated += 1
            if updated % log_every == 0:
                logging.info(
                    "rzmre_pct_per：已更新 %s 条（已扫描 %s 个交易日）。",
                    updated,
                    processed,
                )
    finally:
        conn.close()
    logging.info(
        "rzmre_pct_per：完成，共写入 %s 条（扫描 %s 个交易日）。",
        updated,
        len(date_list),
    )
    return updated


def backfillMarginDailyRzmrePctPerNulls(engine: Engine) -> int:
    """
    补算 rzmre_pct_per 为空但 rzmre_pct 已有值的行（列上线或历史漏算后使用）。
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT `trade_date` FROM `{TABLE_DAILY}`
                WHERE `rzmre_pct_per` IS NULL AND `rzmre_pct` IS NOT NULL
                ORDER BY `trade_date` ASC
                """
            )
        ).fetchall()
    dates: list[dt.date] = []
    for r in rows:
        x = r[0]
        if isinstance(x, dt.datetime):
            dates.append(x.date())
        elif isinstance(x, dt.date):
            dates.append(x)
        else:
            dates.append(pd.Timestamp(x).date())
    if not dates:
        logging.info("rzmre_pct_per 补算：无 rzmre_pct_per 为空且 rzmre_pct 非空行。")
        return 0
    logging.info(
        "rzmre_pct_per 补算：共 %s 条需补算。",
        len(dates),
    )
    return updateMarginDailyRzmrePctPerForDates(engine, dates)


def iterDateChunks(start_ymd: str, end_ymd: str) -> list[tuple[str, str]]:
    """将 [start,end] 按 CHUNK_CALENDAR_DAYS 切成多段。"""
    start_d = dt.datetime.strptime(start_ymd, "%Y%m%d").date()
    end_d = dt.datetime.strptime(end_ymd, "%Y%m%d").date()
    out: list[tuple[str, str]] = []
    cur = start_d
    while cur <= end_d:
        chunk_end = min(cur + dt.timedelta(days=CHUNK_CALENDAR_DAYS - 1), end_d)
        out.append((cur.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")))
        cur = chunk_end + dt.timedelta(days=1)
    return out


def fullImport(pro: Any, engine: Engine) -> int:
    """全量：从 FULL_START_DATE 至今日，分段 + 分页。"""
    today = dt.date.today().strftime("%Y%m%d")
    chunks = iterDateChunks(FULL_START_DATE, today)
    total_written = 0
    for idx, (s, e) in enumerate(chunks, start=1):
        if not running:
            break
        logging.info("全量分段 [%s/%s] %s ~ %s", idx, len(chunks), s, e)
        raw = fetchRangeWithPaging(pro, s, e, label="全量")
        cooked = processDf(raw)
        n = saveData(engine, cooked)
        total_written += n
        logging.info("全量分段 [%s/%s] 获取=%s 写入=%s", idx, len(chunks), len(raw), n)
        time.sleep(REQUEST_INTERVAL)
    logging.info("全量导入结束，累计写入=%s", total_written)
    return total_written


def reportThreeExchangeCoverage(engine: Engine, start_ymd: str, end_ymd: str) -> None:
    """
    读库确认 [start_ymd, end_ymd] 内每个已有数据的 trade_date 是否同时具备 SSE/SZSE/BSE。
    用于增量跑完后人工判断是否需再执行一次脚本。
    """
    start_d = dt.datetime.strptime(start_ymd, "%Y%m%d").date()
    end_d = dt.datetime.strptime(end_ymd, "%Y%m%d").date()
    sql = text(
        f"""
        SELECT `trade_date`, `exchange_id`
        FROM `{TABLE_NAME}`
        WHERE `trade_date` >= :d0 AND `trade_date` <= :d1
        ORDER BY `trade_date`, `exchange_id`
        """
    )
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"d0": start_d, "d1": end_d})
    if df.empty:
        logging.warning(
            "【三所检查】区间 %s ~ %s 库内无记录，请确认接口或稍后手工重跑。",
            start_ymd,
            end_ymd,
        )
        return
    problem_rows: list[tuple[str, set[str], set[str]]] = []
    complete_n = 0
    for tday, g in df.groupby("trade_date"):
        t_str = tday.strftime("%Y-%m-%d") if hasattr(tday, "strftime") else str(tday)[:10]
        actual = set(g["exchange_id"].astype(str).str.strip().tolist())
        missing = set(EXPECTED_EXCHANGES) - actual
        if missing:
            problem_rows.append((t_str, missing, actual))
        else:
            complete_n += 1
    for t_str, missing, actual in problem_rows:
        logging.warning(
            "【三所检查】%s 未集齐三所：缺少 %s；当前仅有 %s",
            t_str,
            " / ".join(sorted(missing)),
            " / ".join(sorted(actual)) if actual else "(无)",
        )
    if not problem_rows:
        logging.info(
            "【三所检查】区间 %s ~ %s 内共 %s 个交易日有数据，均已包含 SSE、SZSE、BSE。",
            start_ymd,
            end_ymd,
            complete_n,
        )
    else:
        logging.warning(
            "【三所检查】汇总：%s 个交易日未集齐三所（共检查到有数据的交易日 %s 个），请手工再执行一次本脚本或待晚到数据披露后重试。",
            len(problem_rows),
            complete_n + len(problem_rows),
        )


def incrementalImport(pro: Any, engine: Engine) -> tuple[int, str | None, str | None, bool]:
    """
    增量：从 MAX(trade_date) 向前回滚 INCREMENTAL_LOOKBACK_DAYS 个自然日到今日，全部 UPSERT。
    这样即使某日某交易所晚于其他所披露，后续重跑也会补齐。

    Returns:
        (写入行数, 增量起始 YYYYMMDD, 结束 YYYYMMDD, 是否内部已改走 margin_sum 全量)
    """
    today = dt.date.today().strftime("%Y%m%d")
    max_ymd = getLocalMaxTradeDateYmd(engine)
    if not max_ymd:
        logging.info("本地无数据，改走全量。")
        n = fullImport(pro, engine)
        return n, None, None, True
    anchor = dt.datetime.strptime(max_ymd, "%Y%m%d").date()
    start_d = anchor - dt.timedelta(days=INCREMENTAL_LOOKBACK_DAYS)
    start_ymd = start_d.strftime("%Y%m%d")
    logging.info(
        "增量区间 %s ~ %s（自最大日 %s 回滚 %s 天）",
        start_ymd,
        today,
        max_ymd,
        INCREMENTAL_LOOKBACK_DAYS,
    )
    raw = fetchRangeWithPaging(pro, start_ymd, today, label="增量")
    cooked = processDf(raw)
    n = saveData(engine, cooked)
    logging.info("增量完成，获取=%s 写入=%s", len(raw), n)
    reportThreeExchangeCoverage(engine, start_ymd, today)
    return n, start_ymd, today, False


def consistencyCheck(pro: Any, engine: Engine) -> None:
    """
    阶段2：以本地最新 trade_date 为锚，对比 API 同日数据（失败仅警告，不阻断主流程）。
    """
    max_ymd = getLocalMaxTradeDateYmd(engine)
    if not max_ymd:
        logging.info("一致性校验跳过：本地无数据。")
        return
    try:
        chk_fields = [x.strip() for x in CHECK_FIELDS.split(",") if x.strip()]
        d_obj = dt.datetime.strptime(max_ymd, "%Y%m%d").date()
        sel = ", ".join(f"`{c}`" for c in chk_fields)
        local_sql = f"SELECT {sel} FROM `{TABLE_NAME}` WHERE trade_date = :d"
        with engine.connect() as conn:
            local_df = pd.read_sql(text(local_sql), conn, params={"d": d_obj})
        if not local_df.empty and "trade_date" in local_df.columns:
            local_df["trade_date"] = pd.to_datetime(local_df["trade_date"]).dt.strftime("%Y-%m-%d")
        api_df = retryRequest(
            lambda **kw: _queryMargin(pro, **kw),
            trade_date=max_ymd,
            limit=PAGE_LIMIT,
            offset=0,
        )
        if api_df.empty:
            logging.warning("一致性校验：API 最新日 %s 无返回，跳过数值比对。", max_ymd)
            return
        api_df = processDf(api_df)
        api_df = api_df[[c for c in chk_fields if c in api_df.columns]]
        if local_df.empty:
            logging.warning("一致性校验：本地最新日 %s 无记录。", max_ymd)
            return
        merged = local_df.merge(
            api_df,
            on=["trade_date", "exchange_id"],
            how="outer",
            suffixes=("_loc", "_api"),
        )
        for col in ("rzye", "rzrqye", "rqyl"):
            lc, ac = f"{col}_loc", f"{col}_api"
            if lc not in merged.columns or ac not in merged.columns:
                continue
            for _, row in merged.iterrows():
                a, b = row.get(lc), row.get(ac)
                if a is None and b is None:
                    continue
                try:
                    va, vb = float(a or 0), float(b or 0)
                    denom = abs(vb) + 1e-8
                    if abs(va - vb) / denom > DATA_TOLERANCE:
                        logging.warning(
                            "一致性：%s %s %s 本地=%s API=%s",
                            max_ymd,
                            row.get("exchange_id"),
                            col,
                            a,
                            b,
                        )
                except Exception:
                    pass
        logging.info("一致性校验完成（最新日=%s）。", max_ymd)
    except Exception as exc:
        logging.warning("一致性校验异常（已忽略）：%s", exc)


def previewMarginDailyRecent(engine: Engine, n: int) -> None:
    """
    试算/展示 margin_daily 最近 n 个交易日（按 trade_date 倒序取 n 条，再按日期正序打印便于阅读）。
    """
    if n <= 0:
        return
    sql = text(
        f"""
        SELECT `trade_date`, `rzye_sum`, `rzmre_sum`, `rqye_sum`, `rzrqye_sum`, `rzmre_pct`, `rzmre_pct_per`, `netbuy`, `netbuy_pct`, `rzye_free_pct`
        FROM `{TABLE_DAILY}`
        ORDER BY `trade_date` DESC
        LIMIT :lim
        """
    )
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"lim": n})
    if df.empty:
        logging.info("【margin_daily 预览】表中无数据（请先跑入库生成 margin_daily）。")
        return
    df = df.sort_values("trade_date", ascending=True).reset_index(drop=True)
    lines = df.to_string(index=False)
    logging.info("【margin_daily 预览】最近 %s 个交易日：\n%s", len(df), lines)
    print("【margin_daily 预览】最近 %s 个交易日：\n%s" % (len(df), lines))


def main() -> None:
    setupLogger()
    parser = argparse.ArgumentParser(description="融资融券交易汇总 margin → margin_sum / margin_daily")
    parser.add_argument(
        "--mode",
        choices=("auto", "full", "incremental"),
        default="auto",
        help="auto：margin_sum 表空则全量，否则增量",
    )
    parser.add_argument(
        "--preview-daily-only",
        action="store_true",
        help="仅连接库并打印 margin_daily 最近交易日，不调用 Tushare、不写 margin_sum",
    )
    parser.add_argument(
        "--preview-days",
        type=int,
        default=5,
        help="预览/试算最近几个交易日（默认 5），配合 --preview-daily-only 或 --show-daily-recent",
    )
    parser.add_argument(
        "--show-daily-recent",
        type=int,
        nargs="?",
        const=5,
        default=None,
        metavar="N",
        help="正常跑完流程后，再打印 margin_daily 最近 N 个交易日（默认 5）；仅写 --show-daily-recent 不传 N 亦为 5",
    )
    parser.add_argument(
        "--margin-daily-recent-only",
        action="store_true",
        help="不拉 Tushare：仅把 margin_sum 中最近若干交易日聚合写入 margin_daily（配合 --margin-daily-recent-days）",
    )
    parser.add_argument(
        "--margin-daily-recent-days",
        type=int,
        default=10,
        help="与 --margin-daily-recent-only 配合，聚合最近 N 个交易日（默认 10）",
    )
    parser.add_argument(
        "--margin-daily-derived-only",
        action="store_true",
        help="不拉 Tushare：仅补算 margin_daily 衍生列 rzye_free_pct（JOIN marketinfo）与 rzmre_pct_per；需配合 --derived-full 或 --derived-recent-days",
    )
    parser.add_argument(
        "--derived-full",
        action="store_true",
        help="与 --margin-daily-derived-only 配合：全表补算上述两列",
    )
    parser.add_argument(
        "--derived-recent-days",
        type=int,
        default=None,
        metavar="N",
        help="与 --margin-daily-derived-only 配合：仅对 margin_sum 最近 N 个交易日补算（增量）",
    )
    args = parser.parse_args()

    if args.margin_daily_derived_only:
        setupLogger()
        engine = makeEngine()
        createMarginDailyTable(engine)
        if tableIsEmptyMarginDaily(engine):
            logging.error("margin_daily 无数据，无法补算衍生列。请先跑入库生成 margin_daily。")
            return
        if args.derived_full and args.derived_recent_days is not None:
            logging.error("请勿同时指定 --derived-full 与 --derived-recent-days。")
            return
        if not args.derived_full and args.derived_recent_days is None:
            logging.error("请指定 --derived-full（全量补算）或 --derived-recent-days N（增量补算）。")
            return
        if args.derived_full:
            logging.info("衍生列补算：全量（rzye_free_pct → rzmre_pct_per）。")
            updateMarginDailyRzyeFreePctForDates(engine, None)
            updateMarginDailyRzmrePctPerForDates(engine, None)
            backfillMarginDailyRzmrePctPerNulls(engine)
        else:
            assert args.derived_recent_days is not None
            n = int(args.derived_recent_days)
            if n <= 0:
                logging.error("--derived-recent-days 须为正整数。")
                return
            ds = lastNMarginSumTradeDates(engine, n)
            logging.info("衍生列补算：最近 %s 个交易日，共 %s 个日期。", n, len(ds))
            updateMarginDailyRzyeFreePctForDates(engine, ds)
            updateMarginDailyRzmrePctPerForDates(engine, ds)
            backfillMarginDailyRzmrePctPerNulls(engine)
        previewMarginDailyRecent(engine, min(15, max(5, args.derived_recent_days or 10)))
        logging.info("全部流程结束（仅衍生列补算）。")
        return

    if args.margin_daily_recent_only:
        setupLogger()
        engine = makeEngine()
        createMarginDailyTable(engine)
        if tableIsEmpty(engine):
            logging.error("margin_sum 无数据，无法聚合 margin_daily。请先执行入库拉取 margin_sum。")
            return
        refreshMarginDailyLastNDistinctTradeDates(engine, args.margin_daily_recent_days)
        _ds = lastNMarginSumTradeDates(engine, args.margin_daily_recent_days)
        updateMarginDailyRzyeFreePctForDates(engine, _ds)
        updateMarginDailyRzmrePctPerForDates(engine, _ds)
        backfillMarginDailyRzmrePctPerNulls(engine)
        previewMarginDailyRecent(engine, args.margin_daily_recent_days)
        logging.info("全部流程结束（仅 margin_daily 最近 N 日）。")
        return

    if args.preview_daily_only:
        engine = makeEngine()
        createMarginDailyTable(engine)
        previewMarginDailyRecent(engine, args.preview_days)
        return

    pro = ts.pro_api(TUSHARE_TOKEN)
    engine = makeEngine()
    createTable(pro, engine)
    createMarginDailyTable(engine)

    used_full_margin_sum = False
    inc_start: str | None = None
    inc_end: str | None = None

    if args.mode == "full":
        fullImport(pro, engine)
        used_full_margin_sum = True
    elif args.mode == "incremental":
        _n, inc_start, inc_end, used_full_margin_sum = incrementalImport(pro, engine)
    else:
        if tableIsEmpty(engine):
            fullImport(pro, engine)
            used_full_margin_sum = True
        else:
            _n, inc_start, inc_end, used_full_margin_sum = incrementalImport(pro, engine)

    # margin_daily：全量 margin_sum 后全表聚合；否则按增量区间 + 强制最新交易日重算
    if used_full_margin_sum:
        rebuildMarginDailyFull(engine)
        updateMarginDailyRzyeFreePctForDates(engine, None)
        updateMarginDailyRzmrePctPerForDates(engine, None)
        backfillMarginDailyRzmrePctPerNulls(engine)
    elif tableIsEmptyMarginDaily(engine) and not tableIsEmpty(engine):
        logging.info("margin_daily 为空且 margin_sum 已有数据，执行 margin_daily 全量聚合。")
        rebuildMarginDailyFull(engine)
        updateMarginDailyRzyeFreePctForDates(engine, None)
        updateMarginDailyRzmrePctPerForDates(engine, None)
        backfillMarginDailyRzmrePctPerNulls(engine)
    elif inc_start and inc_end:
        refreshMarginDailyIncremental(engine, inc_start, inc_end)
        _inc_dates = distinctTradeDatesForIncrementalRzmrePctPer(engine, inc_start, inc_end)
        updateMarginDailyRzyeFreePctForDates(engine, _inc_dates)
        updateMarginDailyRzmrePctPerForDates(engine, _inc_dates)
        backfillMarginDailyRzmrePctPerNulls(engine)
    else:
        logging.warning("margin_daily 未更新：非全量且无增量区间（可能 margin_sum 无数据）。")

    consistencyCheck(pro, engine)

    if args.show_daily_recent is not None:
        previewMarginDailyRecent(engine, args.show_daily_recent)

    logging.info("全部流程结束。")


if __name__ == "__main__":
    main()

# 代码更新时间：2026-04-03 22:17（rzmre_pct_per：改为850个交易日滚动样本，非5×365日历窗）
# 代码更新时间：2026-07-30 09:14（margin_daily 聚合加沪深齐全门槛 + 清理不完整日残留，避免早盘单边数据造成假暴跌）
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
