# -*- coding: utf-8 -*-
"""
五日期校验：对比 vol_price_screener 与 rzye_industry_score_date 两套实现的行业得分是否一致。
从 margin_detail 取 5 个有数据的交易日，分别用两套逻辑计算行业得分（归一化到 [0,1]），按行业对齐后比较。
用法：python validate_rzye_industry_scores.py
"""
import os
import sys
import logging
from datetime import datetime

import pymysql
import pandas as pd

# 与 rzye_industry_score_date 同目录，直接 import
from rzye_industry_score_date import run_score as rzye_run_score

# 日志：与脚本同名的 .log，保留 3 天
LOG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(LOG_DIR, "validate_rzye_industry_scores.log")

def _setup_logging():
    for h in list(logging.root.handlers):
        logging.root.removeHandler(h)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger(__name__)

logger = _setup_logging()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "123456"),
    "charset": "utf8mb4",
}
DB_WIND = {**DB_CONFIG, "database": "stock_wind"}
DB_TUSHARE = {**DB_CONFIG, "database": "stock_tushare"}

# 数值比较容差
TOL = 1e-5
# 校验使用的交易日数量
N_DATES = 5


def get_five_dates() -> list[str]:
    """从 margin_detail 取最近 N_DATES 个有数据的交易日，返回 YYYY-MM-DD 列表。"""
    dates = []
    for cfg in [DB_WIND, DB_TUSHARE]:
        try:
            conn = pymysql.connect(**cfg, connect_timeout=10)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT trade_date FROM margin_detail WHERE trade_date IS NOT NULL ORDER BY trade_date DESC LIMIT %s",
                    (N_DATES + 10,),
                )
                rows = cur.fetchall()
            conn.close()
            for (d,) in rows:
                s = str(d)
                if len(s) == 8 and s.isdigit():
                    dates.append(f"{s[:4]}-{s[4:6]}-{s[6:]}")
                else:
                    dates.append(s)
            if len(dates) >= N_DATES:
                return dates[:N_DATES]
        except Exception as e:
            logger.warning("获取交易日失败 %s: %s", cfg.get("database"), e)
    if not dates:
        # 兜底：固定 5 个日期
        return ["2026-02-10", "2026-02-07", "2026-02-06", "2026-02-05", "2026-02-04"]
    return dates[:N_DATES]


def fetch_screener_scores(trade_date: str, conn_tushare, conn_wind) -> pd.Series | None:
    """调用 vol_price_screener.fetchIndustryMarginScores(conn_tushare, conn_wind, trade_date)，返回 industry->[0,1] 的 Series。"""
    try:
        import vol_price_screener as vps
        if not hasattr(vps, "fetchIndustryMarginScores"):
            return None
        s = vps.fetchIndustryMarginScores(conn_tushare, conn_wind, trade_date)
        if s is not None and isinstance(s, pd.Series):
            return s
    except ImportError:
        pass
    except Exception as e:
        logger.warning("vol_price_screener 调用异常 %s: %s", trade_date, e)
    return None


def compare_series(sa: pd.Series, sb: pd.Series, tol: float = TOL) -> tuple[bool, pd.DataFrame]:
    """对齐两个 Series（按 index 并集），比较数值；返回 (是否一致, 差异明细 DataFrame)。"""
    common = sa.index.union(sb.index).unique()
    va = sa.reindex(common).fillna(0)
    vb = sb.reindex(common).fillna(0)
    diff = (va - vb).abs()
    ok = (diff <= tol).all()
    detail = pd.DataFrame({"rzye": va, "screener": vb, "diff": diff}, index=common)
    return ok, detail


def main():
    logger.info("开始五日期行业得分校验（vol_price_screener vs rzye_industry_score_date）")
    dates = get_five_dates()
    logger.info("校验日期: %s", dates)

    conn_tushare = None
    conn_wind = None
    try:
        conn_tushare = pymysql.connect(**DB_TUSHARE, connect_timeout=10)
        conn_wind = pymysql.connect(**DB_WIND, connect_timeout=10)
    except Exception as e:
        logger.warning("数据库连接失败，仅使用 rzye 单脚本结果: %s", e)

    has_screener = False
    try:
        import vol_price_screener  # noqa: F401
        has_screener = hasattr(vol_price_screener, "fetchIndustryMarginScores")
    except ImportError:
        pass

    if not has_screener:
        logger.info("未找到 vol_price_screener 或 fetchIndustryMarginScores，仅输出 rzye_industry_score_date 的 5 日得分供人工核对。")

    results = []
    for trade_date in dates:
        sa = rzye_run_score(trade_date, silent=True)
        if sa is None or sa.empty:
            logger.warning("日期 %s: rzye 无结果，跳过", trade_date)
            results.append({"date": trade_date, "ok": False, "msg": "rzye 无结果"})
            continue

        if has_screener and conn_tushare and conn_wind:
            sb = fetch_screener_scores(trade_date, conn_tushare, conn_wind)
            if sb is None or sb.empty:
                logger.warning("日期 %s: screener 无结果，仅记录 rzye", trade_date)
                results.append({"date": trade_date, "ok": False, "msg": "screener 无结果"})
                continue
            ok, detail = compare_series(sa, sb)
            results.append({"date": trade_date, "ok": ok, "detail": detail})
            if ok:
                logger.info("日期 %s: 一致", trade_date)
            else:
                bad = detail[detail["diff"] > TOL]
                logger.info("日期 %s: 不一致，行业数 %d，差异示例:\n%s", trade_date, len(bad), bad.head(10).to_string())
        else:
            results.append({"date": trade_date, "ok": True, "detail": pd.DataFrame({"rzye": sa})})
            logger.info("日期 %s: 仅 rzye 得分，行业数 %d", trade_date, len(sa))

    if conn_tushare:
        try:
            conn_tushare.close()
        except Exception:
            pass
    if conn_wind:
        try:
            conn_wind.close()
        except Exception:
            pass

    # 汇总
    n_ok = sum(1 for r in results if r.get("ok"))
    logger.info("汇总: 共 %d 个日期，一致/有效 %d", len(results), n_ok)
    if has_screener and n_ok < len(results):
        for r in results:
            if not r.get("ok"):
                logger.info("  %s: %s", r["date"], r.get("msg", "存在差异"))

    # 可选：将差异明细写入 CSV（仅第一个不一致日期的 detail）
    for r in results:
        if not r.get("ok") and "detail" in r and r["detail"] is not None and not r["detail"].empty:
            out_path = os.path.join(LOG_DIR, "validate_rzye_industry_scores_diff.csv")
            r["detail"].to_csv(out_path, encoding="utf-8-sig")
            logger.info("差异明细已写入 %s", out_path)
            break

    return 0 if (not has_screener or n_ok == len(results)) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.info("用户中断")
        sys.exit(130)
    except Exception as e:
        logger.exception("校验异常: %s", e)
        sys.exit(1)

# 代码更新时间: 2026-02-25（本地时间精确到分钟，由开发者维护）
