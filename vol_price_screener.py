# -*- coding: utf-8 -*-
"""
量价筛选器：100 分制综合得分，用于标的排序/筛选。

权重分配（100 分）
-----------------
项目                    权重    说明
量价质量                40      0.3×涨放量 + 0.4×跌缩量 + 0.3×量价相关（手工规则：涨日放量×40+跌日缩量×40+量价相关×20）
R²                      15      前复权收盘对时间回归 R²，截断到 [0,1]
波动率                  10      得分 = max(0, 1 − 波动率%/10)
起涨区                  10      得分 = 1 − 回落幅%/20
融资融券                15      行业打分（绝对金额+增幅）按 industry_sw_2021_2 映射
中金量价同步_20d         5      同步越低得分越高，(1−(corr+1)/2)
中金量能领先_20d         5      领先越高得分越高，(corr+1)/2
合计                   100      —

筛选条件（依据对话日志恢复）：均线多头 → 涨日放量≥50%、跌日缩量≥60%、量价相关≥0.3、窗口涨幅<30%、
BOLL≥40%、RSI12≤75、近5日日均成交额≥3亿 → 按综合得分排序 → mergeIndustry + mergeMarginScore → Top N。
"""
import os
import sys
import pymysql
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Optional

# 数据库配置（与 rzye_industry_score_date 一致）
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "123456"),
    "charset": "utf8mb4",
}
DB_WIND = {**DB_CONFIG, "database": "stock_wind"}
DB_TUSHARE = {**DB_CONFIG, "database": "stock_tushare"}

# 融资融券方案二常量（与 rzye_industry_score_date 一致）
_MARGIN_DIM_COLS = ["rzjmre", "rzjmre_t3", "rzjmre_t5", "rzjmre_t10", "rzjmre_t20", "rzjmre_t60"]
_MARGIN_DIM_NAMES = ["1日", "3日", "5日", "10日", "20日", "60日"]
_MARGIN_BASE_SCORE = {6: 10, 5: 7, 4: 4, 3: 2, 2: 2, 1: 1}
_MARGIN_SHORT_DIMS = [0, 1, 2]  # 1/3/5 日近期共识

# --------------- 手工量价筛选常量（依据对话日志） ---------------
WINDOW_LIST = [20]                    # 窗口日数，如 [20] 或 [30]
UP_VOL_RATIO_MIN = 0.50               # 涨日放量 ≥ 50%
DOWN_VOL_RATIO_MIN = 0.60             # 跌日缩量 ≥ 60%
VOL_PRICE_CORR_MIN = 0.30             # 量价相关系数 ≥ 0.3
PERIOD_RETURN_MAX = 30.0              # 窗口涨幅 < 30%
BOLL_POS_MIN = 0.40                   # BOLL 位置 ≥ 40%
BOLL_POS_MAX = 99.9                   # 无上限（突破上轨保留）
RSI_MIN = 0.0                         # RSI 无下限
RSI_MAX = 75.0                        # RSI12 ≤ 75
AMOUNT_DAYS = 5                       # 近 5 日
AMOUNT_MIN_YI = 3.0                   # 日均成交额 ≥ 3 亿
TOP_N = 20                            # 输出前 N 名
OUTPUT_DIR = Path(r"d:\test")          # CSV 输出目录


def fetchIndustryMarginScores(
    conn_tushare,
    conn_wind,
    trade_date: str,
) -> Optional[pd.Series]:
    """
    按指定交易日计算行业融资融券得分（绝对金额+增幅 方案二），归一化到 [0,1]。
    数据来源：margin_detail 先查 conn_tushare，无数据再查 conn_wind；行业来自 conn_wind.stockcodes。
    返回：index=行业(industry_sw_2021_2)，value=归一化得分 [0,1]，不含 ETF/未分类。
    """
    trade_ymd = trade_date.replace("-", "")
    # 1) 取 margin_detail：先 tushare 再 wind
    margin_sql = """
    SELECT ts_code, rzye, rzjmre, rzjmre_t3, rzjmre_t5, rzjmre_t10, rzjmre_t20, rzjmre_t60
    FROM margin_detail
    WHERE trade_date = %s AND rzye IS NOT NULL AND rzye > 0
    """
    md_df = None
    for conn, name in [(conn_tushare, "tushare"), (conn_wind, "wind")]:
        try:
            md_df = pd.read_sql(margin_sql, conn, params=[trade_ymd])
            if md_df is not None and len(md_df) >= 2:
                break
            # 尝试 YYYY-MM-DD
            md_df = pd.read_sql(margin_sql, conn, params=[trade_date[:10]])
            if md_df is not None and len(md_df) >= 2:
                break
        except Exception:
            continue
    if md_df is None or md_df.empty or len(md_df) < 2:
        return None

    for c in ["rzye"] + _MARGIN_DIM_COLS:
        if c in md_df.columns:
            md_df[c] = pd.to_numeric(md_df[c], errors="coerce").fillna(0)

    # 2) 取 stockcodes 行业映射（仅 wind）
    try:
        sc_df = pd.read_sql(
            "SELECT windcodes AS ts_code, industry_sw_2021_2 FROM stockcodes WHERE industry_sw_2021_2 IS NOT NULL",
            conn_wind,
        )
    except Exception:
        return None
    if sc_df.empty:
        return None

    # 3) 合并：仅保留有行业的标的，ETF/未分类在 stockcodes 中无 industry_sw_2021_2 则被 LEFT JOIN 掉
    df = md_df.merge(sc_df, on="ts_code", how="inner")
    if df.empty or len(df) < 2:
        return None

    # 4) 按行业聚合
    agg = {"rzye": "sum"}
    for c in _MARGIN_DIM_COLS:
        if c in df.columns:
            agg[c] = "sum"
    df = df.groupby("industry_sw_2021_2", as_index=False).agg(agg)
    df = df.rename(columns={"industry_sw_2021_2": "industry"})
    # 列名统一为 total_*，与 rzye_industry_score_date 一致
    df["total_rzye"] = df["rzye"].astype(float)
    df["total_rzjmre"] = df["rzjmre"].astype(float) if "rzjmre" in df.columns else 0
    df["total_rzjmre_t3"] = df["rzjmre_t3"].astype(float) if "rzjmre_t3" in df.columns else 0
    df["total_rzjmre_t5"] = df["rzjmre_t5"].astype(float) if "rzjmre_t5" in df.columns else 0
    df["total_rzjmre_t10"] = df["rzjmre_t10"].astype(float) if "rzjmre_t10" in df.columns else 0
    df["total_rzjmre_t20"] = df["rzjmre_t20"].astype(float) if "rzjmre_t20" in df.columns else 0
    df["total_rzjmre_t60"] = df["rzjmre_t60"].astype(float) if "rzjmre_t60" in df.columns else 0
    dim_cols = ["total_rzjmre", "total_rzjmre_t3", "total_rzjmre_t5", "total_rzjmre_t10", "total_rzjmre_t20", "total_rzjmre_t60"]

    # 5) 方案二：各维度 top10 绝对 / top10 增幅，BASE_SCORE + 增幅加分 + 近期双进加分
    top10_abs = {}
    top10_ratio = {}
    for i, col in enumerate(dim_cols):
        if col not in df.columns:
            df[col] = 0
        top10_abs[_MARGIN_DIM_NAMES[i]] = set(df.nlargest(10, col, keep="first")["industry"].tolist())
        df[f"ratio_{i}"] = np.where(df["total_rzye"] > 0, df[col] / df["total_rzye"] * 100, 0)
        top10_ratio[_MARGIN_DIM_NAMES[i]] = set(df.nlargest(10, f"ratio_{i}", keep="first")["industry"].tolist())

    all_abs = set()
    all_ratio = set()
    for s in top10_abs.values():
        all_abs |= s
    for s in top10_ratio.values():
        all_ratio |= s
    count_abs = {ind: sum(1 for d in _MARGIN_DIM_NAMES if ind in top10_abs.get(d, set())) for ind in all_abs}
    count_ratio = {ind: sum(1 for d in _MARGIN_DIM_NAMES if ind in top10_ratio.get(d, set())) for ind in all_ratio}
    all_candidates = all_abs | all_ratio

    rows = []
    for ind in all_candidates:
        c_abs = count_abs.get(ind, 0)
        c_ratio = count_ratio.get(ind, 0)
        dim_max = max(c_abs, c_ratio)
        base = _MARGIN_BASE_SCORE.get(dim_max, 1)
        bonus = 1 if c_ratio >= c_abs else 0
        n_short = sum(
            1 for j in _MARGIN_SHORT_DIMS
            if ind in top10_abs.get(_MARGIN_DIM_NAMES[j], set()) and ind in top10_ratio.get(_MARGIN_DIM_NAMES[j], set())
        )
        recent_bonus = n_short * 0.5
        score = base + bonus + recent_bonus
        rows.append((ind, score))
    ind_scores = pd.Series({ind: sc for ind, sc in rows})

    # 6) 归一化到 [0,1]
    lo, hi = ind_scores.min(), ind_scores.max()
    if hi > lo:
        out = (ind_scores - lo) / (hi - lo)
    else:
        out = pd.Series(0.5, index=ind_scores.index)
    out.index.name = "industry_sw_2021_2"
    return out


# --------------- 以下为 100 分制其他子项占位，便于后续接数据 ---------------
# 量价质量 40：0.3×涨放量 + 0.4×跌缩量 + 0.3×量价相关（需日线/量价数据）
# R² 15：前复权收盘对时间回归 R²，截断 [0,1]（需日线前复权）
# 波动率 10：max(0, 1 − 波动率%/10)（需波动率字段或日线计算）
# 起涨区 10：1 − 回落幅%/20（可用 stockstat.decline_rate 或 rt 计算）
# 中金量价同步_20d 5：(1−(corr+1)/2)；中金量能领先_20d 5：(corr+1)/2（需 stk_factor_pro 或等价字段）


def fetchVolPriceData(conn, trade_dates: list[str]) -> Optional[pd.DataFrame]:
    """从 stk_factor_pro 拉取量价数据（含 amount 千元、boll、rsi_qfq_12）。"""
    if not trade_dates:
        return None
    placeholders = ",".join(["%s"] * len(trade_dates))
    sql = f"""
    SELECT ts_code, trade_date, pct_chg, volume_ratio, close_qfq, high_qfq, low_qfq, amount,
           boll_upper_qfq, boll_lower_qfq, rsi_qfq_12
    FROM stk_factor_pro
    WHERE trade_date IN ({placeholders}) AND amount IS NOT NULL
    """
    try:
        df = pd.read_sql(sql, conn, params=trade_dates)
    except Exception:
        return None
    if df.empty:
        return None
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "")
    for c in ["pct_chg", "volume_ratio", "close_qfq", "high_qfq", "low_qfq", "amount"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def fetchMaData(conn, trade_dates: list[str]) -> Optional[pd.DataFrame]:
    """从 stk_factor_ma 拉取 ma20/60/120, ema20/60/120。"""
    if not trade_dates:
        return None
    placeholders = ",".join(["%s"] * len(trade_dates))
    sql = f"""
    SELECT ts_code, trade_date, ma20, ma60, ma120, ema20, ema60, ema120
    FROM stk_factor_ma
    WHERE trade_date IN ({placeholders})
    """
    try:
        df = pd.read_sql(sql, conn, params=trade_dates)
    except Exception:
        return None
    if df.empty:
        return None
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "")
    for c in ["ma20", "ma60", "ma120", "ema20", "ema60", "ema120"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def filterMaEmaLong(vol_price_df: pd.DataFrame, ma_df: pd.DataFrame, latest_date: str) -> pd.DataFrame:
    """保留 MA+EMA 双重多头：最新日 close_qfq > ma20 > ma60 > ma120 且 close_qfq > ema20 > ema60 > ema120。"""
    v = vol_price_df[vol_price_df["trade_date"].astype(str) == latest_date.replace("-", "")].copy()
    m = ma_df[ma_df["trade_date"].astype(str) == latest_date.replace("-", "")].copy()
    if v.empty or m.empty:
        return pd.DataFrame()
    v = v.merge(m, on=["ts_code", "trade_date"], how="inner")
    for col in ["close_qfq", "ma20", "ma60", "ma120", "ema20", "ema60", "ema120"]:
        if col not in v.columns:
            return pd.DataFrame()
    v = v.dropna(subset=["close_qfq", "ma20", "ma60", "ma120", "ema20", "ema60", "ema120"])
    v = v[(v["close_qfq"] > v["ma20"]) & (v["ma20"] > v["ma60"]) & (v["ma60"] > v["ma120"])]
    v = v[(v["close_qfq"] > v["ema20"]) & (v["ema20"] > v["ema60"]) & (v["ema60"] > v["ema120"])]
    return v[["ts_code"]].drop_duplicates()


def calcStockScore(win_df: pd.DataFrame) -> Optional[dict]:
    """
    手工量价得分：涨日放量×40 + 跌日缩量×40 + 量价相关×20（满分100）。
    过滤：涨日放量≥50%、跌日缩量≥60%、量价相关≥0.3、窗口涨幅<30%、BOLL≥40%、RSI12≤75、近5日日均成交额≥3亿。
    """
    if win_df is None or len(win_df) < 5:
        return None
    win_df = win_df.sort_values("trade_date")
    pct = win_df["pct_chg"].values
    vol_ratio = win_df["volume_ratio"].values
    up_days = pct > 0
    down_days = pct < 0
    n_up = up_days.sum()
    n_down = down_days.sum()
    up_vol_ratio = (vol_ratio[up_days] >= 1.0).sum() / n_up if n_up > 0 else 0
    down_vol_ratio = (vol_ratio[down_days] < 1.0).sum() / n_down if n_down > 0 else 0
    if n_up < 2 or n_down < 2:
        corr = 0.0
    else:
        corr = np.corrcoef(pct, vol_ratio)[0, 1]
        corr = 0.0 if np.isnan(corr) else max(-1, min(1, corr))
    if up_vol_ratio < UP_VOL_RATIO_MIN or down_vol_ratio < DOWN_VOL_RATIO_MIN or corr < VOL_PRICE_CORR_MIN:
        return None
    period_return = (1 + pct / 100).prod() - 1
    period_return_pct = period_return * 100
    if period_return_pct >= PERIOD_RETURN_MAX:
        return None
    last = win_df.iloc[-1]
    boll_upper = last.get("boll_upper_qfq")
    boll_lower = last.get("boll_lower_qfq")
    close_qfq = last.get("close_qfq")
    if pd.notna(boll_upper) and pd.notna(boll_lower) and boll_upper > boll_lower and pd.notna(close_qfq):
        boll_pos = (float(close_qfq) - float(boll_lower)) / (float(boll_upper) - float(boll_lower))
        if boll_pos < BOLL_POS_MIN or boll_pos > BOLL_POS_MAX:
            return None
    rsi12 = last.get("rsi_qfq_12")
    if pd.notna(rsi12):
        rsi12 = float(rsi12)
        if rsi12 < RSI_MIN or rsi12 > RSI_MAX:
            return None
    if "amount" in win_df.columns and len(win_df) >= AMOUNT_DAYS:
        amount_k = win_df["amount"].tail(AMOUNT_DAYS).mean()
        if amount_k < AMOUNT_MIN_YI * 1e5:  # 3亿 = 300000 千元
            return None
    score = up_vol_ratio * 40 + down_vol_ratio * 40 + (corr + 1) / 2 * 20
    score = max(0, min(100, score))
    high = win_df["high_qfq"].max()
    low = win_df["low_qfq"].min()
    close_last = win_df["close_qfq"].iloc[-1]
    drawdown = (1 - close_last / high) * 100 if high and high > 0 else 0
    return {
        "score": round(score, 2),
        "up_vol_ratio": round(up_vol_ratio * 100, 1),
        "down_vol_ratio": round(down_vol_ratio * 100, 1),
        "corr": round(corr, 4),
        "period_return_pct": round(period_return_pct, 2),
        "drawdown": round(drawdown, 2),
    }


def mergeIndustry(df: pd.DataFrame, conn_wind) -> pd.DataFrame:
    """按 ts_code 映射 industry_sw_2021_2（stockcodes 在 wind，windcodes 对应 ts_code）。"""
    try:
        sc = pd.read_sql("SELECT windcodes AS ts_code, industry_sw_2021_2 FROM stockcodes", conn_wind)
    except Exception:
        return df
    return df.merge(sc, on="ts_code", how="left")


def mergeMarginScore(df: pd.DataFrame, industry_scores: Optional[pd.Series]) -> pd.DataFrame:
    """按 industry_sw_2021_2 映射行业得分（0~1）。"""
    if industry_scores is None or industry_scores.empty or "industry_sw_2021_2" not in df.columns:
        df["margin_score"] = np.nan
        return df
    df["margin_score"] = df["industry_sw_2021_2"].map(industry_scores)
    return df


def runScreener(
    vol_price_df: pd.DataFrame,
    ma_df: pd.DataFrame,
    trade_dates: list[str],
    conn_tushare,
    conn_wind,
    trade_date_str: str,
    industry_scores: Optional[pd.Series],
) -> pd.DataFrame:
    """对每个窗口过滤+打分，mergeIndustry + mergeMarginScore，按 score 降序，返回带 industry 与 margin_score 的 DataFrame。"""
    long_codes = filterMaEmaLong(vol_price_df, ma_df, trade_date_str)
    if long_codes.empty:
        return pd.DataFrame()
    window_days = max(WINDOW_LIST)
    window_dates_set = set(trade_dates[:window_days])
    win_df_all = vol_price_df[vol_price_df["trade_date"].astype(str).isin(window_dates_set)]
    rows = []
    for ts_code in long_codes["ts_code"].unique():
        win_df = win_df_all[win_df_all["ts_code"] == ts_code]
        res = calcStockScore(win_df)
        if res is None:
            continue
        rows.append({"ts_code": ts_code, **res})
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out = mergeIndustry(out, conn_wind)
    out = mergeMarginScore(out, industry_scores)
    out = out.sort_values("score", ascending=False).reset_index(drop=True)
    return out


def main() -> int:
    """主流程：取最新交易日、拉量价与均线、跑筛选、输出 CSV。"""
    conn_t = pymysql.connect(**DB_TUSHARE, connect_timeout=10)
    conn_w = pymysql.connect(**DB_WIND, connect_timeout=10)
    try:
        cur = conn_t.cursor()
        cur.execute("SELECT DISTINCT trade_date FROM stk_factor_pro ORDER BY trade_date DESC LIMIT 1")
        row = cur.fetchone()
        cur.close()
        if not row:
            print("未获取到最新交易日")
            return 1
        latest = str(row[0]).replace("-", "")
        trade_date_str = f"{latest[:4]}-{latest[4:6]}-{latest[6:]}"
        window_days = max(WINDOW_LIST)
        cur = conn_t.cursor()
        cur.execute(
            "SELECT DISTINCT trade_date FROM stk_factor_pro ORDER BY trade_date DESC LIMIT %s",
            (window_days + 5,),
        )
        raw_dates = [str(r[0]) for r in cur.fetchall()]
        trade_dates = [d.replace("-", "") for d in raw_dates]
        cur.close()
        if len(trade_dates) < window_days:
            print("交易日不足")
            return 1
        vol_price_df = fetchVolPriceData(conn_t, trade_dates)
        ma_df = fetchMaData(conn_t, trade_dates)
        if vol_price_df is None or vol_price_df.empty or ma_df is None or ma_df.empty:
            print("量价或均线数据为空")
            return 1
        industry_scores = fetchIndustryMarginScores(conn_t, conn_w, trade_date_str)
        result = runScreener(vol_price_df, ma_df, trade_dates, conn_t, conn_w, trade_date_str, industry_scores)
        if result.empty:
            print(f"{window_days}日窗口：未找到符合条件的股票")
            return 0
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        base_name = f"vol_price_{window_days}d_rule_{latest}"
        result.to_csv(OUTPUT_DIR / f"{base_name}.csv", index=False, encoding="utf-8-sig")
        top = result.head(TOP_N)
        top.to_csv(OUTPUT_DIR / f"{base_name}_top{TOP_N}.csv", index=False, encoding="utf-8-sig")
        print(f"最新交易日: {trade_date_str} | {window_days}日窗口筛选出: {len(result)} 只 | Top{TOP_N} 已写入 {OUTPUT_DIR}")
        print(top[["ts_code", "up_vol_ratio", "down_vol_ratio", "corr", "score", "period_return_pct", "drawdown"]].to_string())
        return 0
    finally:
        conn_t.close()
        conn_w.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("用户中断")
        sys.exit(130)
    except Exception as e:
        print(f"异常: {e}")
        sys.exit(1)
