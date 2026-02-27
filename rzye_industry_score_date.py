# -*- coding: utf-8 -*-
"""
指定日期的融资融券「绝对金额+增幅」行业得分输出（与 rzye_top10_report 方案二一致）
用法：python rzye_industry_score_date.py 2026-02-10
"""
import os
import sys
import pymysql
import pandas as pd
import numpy as np
from pathlib import Path

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "123456"),
    "charset": "utf8mb4",
}
DB_WIND = {**DB_CONFIG, "database": "stock_wind"}
DB_TUSHARE = {**DB_CONFIG, "database": "stock_tushare"}

# 聚合后列名为 total_rzjmre, total_rzjmre_t3, ...
DIM_COLS = ["total_rzjmre", "total_rzjmre_t3", "total_rzjmre_t5", "total_rzjmre_t10", "total_rzjmre_t20", "total_rzjmre_t60"]
DIM_NAMES = ["1日", "3日", "5日", "10日", "20日", "60日"]
BASE_SCORE = {6: 10, 5: 7, 4: 4, 3: 2, 2: 2, 1: 1}
SHORT_DIMS = [0, 1, 2]  # 1/3/5日 近期共识


def get_industry_distribution(conn, trade_date: str) -> pd.DataFrame:
    """trade_date: YYYYMMDD 或 YYYY-MM-DD"""
    sql = """
    SELECT
        CASE
            WHEN s.industry_sw_2021_2 IS NOT NULL THEN s.industry_sw_2021_2
            WHEN fb.fund_name IS NOT NULL AND UPPER(fb.fund_name) LIKE '%%ETF%%' THEN 'ETF'
            ELSE '未分类'
        END AS industry,
        SUM(md.rzye) AS total_rzye,
        SUM(md.rzjmre) AS total_rzjmre,
        SUM(md.rzjmre_t3) AS total_rzjmre_t3,
        SUM(md.rzjmre_t5) AS total_rzjmre_t5,
        SUM(md.rzjmre_t10) AS total_rzjmre_t10,
        SUM(md.rzjmre_t20) AS total_rzjmre_t20,
        SUM(md.rzjmre_t60) AS total_rzjmre_t60
    FROM margin_detail md
    LEFT JOIN stockcodes s ON md.ts_code = s.windcodes
    LEFT JOIN fund_basic fb ON md.ts_code = fb.ts_code
    WHERE md.trade_date = %s AND md.rzye IS NOT NULL AND md.rzye > 0
    GROUP BY 1
    ORDER BY SUM(md.rzye) DESC
    """
    df = pd.read_sql(sql, conn, params=[trade_date])
    if df.empty:
        return df
    for c in ["total_rzye"] + DIM_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    df = df[~df["industry"].isin(["ETF", "未分类"])].copy()
    return df


def run_score(trade_date: str, silent: bool = False) -> pd.Series | None:
    """计算指定日期的绝对金额+增幅行业得分；返回 industry->[0,1] 的 Series（供校验用）。silent=True 时不打印。"""
    trade_ymd = trade_date.replace("-", "")
    df = None
    for cfg, name in [(DB_WIND, "stock_wind"), (DB_TUSHARE, "stock_tushare")]:
        try:
            conn = pymysql.connect(**cfg, connect_timeout=10)
            df = get_industry_distribution(conn, trade_ymd)
            conn.close()
            if df is not None and len(df) >= 2:
                break
            if trade_ymd != trade_date:
                conn2 = pymysql.connect(**cfg, connect_timeout=10)
                df = get_industry_distribution(conn2, trade_date[:10])
                conn2.close()
            if df is not None and len(df) >= 2:
                break
        except Exception as e:
            if not silent:
                print(f"  [{name}] 跳过: {e}", file=sys.stderr)
    if df is None or df.empty or len(df) < 2:
        if not silent:
            print(f"未获取到 {trade_date} 的行业分布数据（margin_detail 需含 rzjmre 系列字段）")
        return None

    for col in DIM_COLS:
        if col not in df.columns:
            df[col] = 0
    top10_abs = {}
    top10_ratio = {}
    for i, col in enumerate(DIM_COLS):
        top10_abs[DIM_NAMES[i]] = set(df.nlargest(10, col, keep="first")["industry"].tolist())
        df[f"ratio_{i}"] = np.where(df["total_rzye"] > 0, df[col] / df["total_rzye"] * 100, 0)
        top10_ratio[DIM_NAMES[i]] = set(df.nlargest(10, f"ratio_{i}", keep="first")["industry"].tolist())

    all_abs = set()
    all_ratio = set()
    for s in top10_abs.values():
        all_abs |= s
    for s in top10_ratio.values():
        all_ratio |= s
    count_abs = {ind: sum(1 for d in DIM_NAMES if ind in top10_abs.get(d, set())) for ind in all_abs}
    count_ratio = {ind: sum(1 for d in DIM_NAMES if ind in top10_ratio.get(d, set())) for ind in all_ratio}
    all_candidates = all_abs | all_ratio

    rows = []
    for ind in all_candidates:
        c_abs = count_abs.get(ind, 0)
        c_ratio = count_ratio.get(ind, 0)
        dim_max = max(c_abs, c_ratio)
        base = BASE_SCORE.get(dim_max, 1)
        bonus = 1 if c_ratio >= c_abs else 0
        n_short = sum(1 for j in SHORT_DIMS if ind in top10_abs.get(DIM_NAMES[j], set()) and ind in top10_ratio.get(DIM_NAMES[j], set()))
        recent_bonus = n_short * 0.5
        score = base + bonus + recent_bonus
        rows.append((ind, f"{dim_max}维", c_abs, c_ratio, bonus, n_short, recent_bonus, score))
    rows.sort(key=lambda x: (-x[7], -x[2], -x[3], x[0]))

    # 归一化到 [0,1]，与 vol_price_screener 口径一致
    ind_scores = pd.Series({ind: sc for ind, _, _, _, _, _, _, sc in rows})
    lo, hi = ind_scores.min(), ind_scores.max()
    if hi > lo:
        ind_scores_norm = (ind_scores - lo) / (hi - lo)
    else:
        ind_scores_norm = pd.Series(0.5, index=ind_scores.index)

    if not silent:
        print(f"\n统计日期: {trade_date}（融资融券 绝对金额+增幅 方案二 行业得分）")
        print("=" * 95)
        print("  行业           | 档位 | 绝对维度数 | 增幅维度数 | 增幅加分 | 近期双进数 | 近期加分 | 得分")
        print("-" * 95)
        for ind, tier, c_abs, c_ratio, bonus, n_short, recent_bonus, score in rows:
            print(f"  {ind[:14]:<14} | {tier:>4} | {c_abs:>10} | {c_ratio:>10} | +{bonus:>5} | {n_short:>10} | +{recent_bonus:>5} | {score}")
        print("=" * 95)
        print("说明：绝对维度数=该行业在 1/3/5/10/20/60 日「融资净买入合计」进前10的维度数；增幅=净买/余额×100% 进前10的维度数；近期双进=1/3/5日同时进绝对与增幅前10的维度数×0.5。")
    return ind_scores_norm


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else "2026-02-10"
    run_score(date_arg)
