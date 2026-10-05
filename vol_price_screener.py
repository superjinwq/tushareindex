# -*- coding: utf-8 -*-
"""
量价筛选器：100 分制综合得分，用于标的排序/筛选。

规则（见 .cursor/rules/ma-lookback.mdc）：计算 MA 时一律向前取数以满足 MA 窗口长度，
避免窗口内前 (N-1) 日 MA 为 NaN 导致漏判（如脉冲、均线等）。

权重分配（100 分；2026-07 因子对比后锁定：lv换手相关 + sync提权 / r2·趋势稳健降权；脉冲退出总分）
----------------------------------------------------------------------
项目                    权重    说明
量价质量                10      跌日缩量×8 + corr(pct,换手)×7（硬筛仍用 volume_ratio；涨日放量/不对称度为观察）
R²                       8      典型价~时间 调整R²×8（公式不变，权重自15下调）
波动率(IVOL)           22.5     沪深300残差EWMA；score=max(0,(1-ivol/VOL_IVOL_ZERO_SCORE)×22.5)；硬筛总波动≤VOL_PCT_MAX
起涨区                22.5     max(0,(1−回落幅%/20)×22.5)（日内高点参考；原20，承接脉冲退出的2.5）
中金量价同步_20d        15      (corr(pct,turn)+1)/2×15（趋势子集正向；原5，IC/权重偏高故提权）
中金量能领先_20d        15      近端加权Pearson(corr(turn_t,ret_t+1),hl=LEAD_EWMA_HALFLIFE)→(corr+1)/2×15
趋势稳健                 7      残差SE截面归一化×3.5 + 斜率一致性(5/10/25)×3.5（公式不变，权重自10下调）
合计                   100      —（观察不参与总分：脉冲放量缩量、涨日放量、量能不对称度）

流程：取交易日(最多400日) → 拉量价与均线 → 按标的预处理（筛选日=该标的最近交易日且该标的至少290个交易日，
得到含 stock_rank 的预处理表）→ 突破前高/打分/观察字段均基于预处理结果，解决停牌导致数据不足的一刀切问题。
筛选条件：均线（收盘价站上）→ 突破前高 → 量价打分 → 截面归一化(趋势稳健) → 按综合得分排序 → mergeIndustry → Top N。
均线条件：最新日 MA 链 (close>ma20>ma120 或 close>ma20>ma60) 且 EMA 链 (close>ema20>ema120 或 close>ema20>ema60)。
输出含观察字段（不参与打分）：首次创新高、筛选日相对250日新高涨幅%、行业名称、RPS。
"""
import os
import sys
import time
import warnings

import pymysql
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Optional

# 消除 pd.read_sql 使用 pymysql 连接时的 UserWarning（pandas 建议用 SQLAlchemy，此处保持 pymysql 不增加依赖）
warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*", category=UserWarning)

# ---------- 进度日志 & CSV 写入容错 ----------
_STEP_T0 = 0.0

def _logStep(msg: str, *, newline: bool = True) -> None:
    """打印带耗时的步骤日志，flush 确保 PyCharm/终端实时可见。"""
    global _STEP_T0
    now = time.time()
    elapsed = ""
    if _STEP_T0 > 0:
        elapsed = f"  ({time.time() - _STEP_T0:.1f}s)"
    prefix = f"[{time.strftime('%H:%M:%S')}]"
    end = "\n" if newline else ""
    print(f"{prefix} {msg}{elapsed}", end=end, flush=True)
    _STEP_T0 = now

def _safeCsvWrite(df: "pd.DataFrame", path: Path, **kwargs) -> Path:
    """写入 CSV，若文件被占用则自动追加时间戳写入备用路径。"""
    try:
        df.to_csv(path, **kwargs)
        return path
    except PermissionError:
        backup = path.parent / f"{path.stem}_{int(time.time())}{path.suffix}"
        df.to_csv(backup, **kwargs)
        print(f"  文件被占用: {path.name}，已写入备用: {backup}")
        return backup


# 数据库配置
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "charset": "utf8mb4",
}
DB_WIND = {**DB_CONFIG, "database": "stock_wind"}
DB_TUSHARE = {**DB_CONFIG, "database": "stock_tushare"}

# --------------- 手工量价筛选常量（依据对话日志） ---------------
WINDOW_LIST = [40]                    # 主筛选窗口日数（过滤、量价质量/R²/波动率/起涨区均用此窗口）
PRIOR_HIGH_DAYS = 250                 # 前复权突破前高：最近40日最高价需 > 该250日（第41~290日）最高价
# 首次创新高：最近 N 日内「当日最高=该日对应的250日最高」仅出现 1 次，且筛选日为该日；与主窗口 40 独立，仅作观察字段
FIRST_HIGH_LOOKBACK = 20
# 为减少停牌导致「不足290个交易日」空值：主流程多载入交易日数，取数后做一次按标的预处理，满足后续计算
NEED_DATES_EXTRA_FOR_250H = 400
# 预处理后每标的保留的交易日数（最近 N 个交易日）：突破前高需 40+250，打分窗口需 40+脉冲 lookback19
NEED_DAYS_PER_STOCK = max(WINDOW_LIST) + PRIOR_HIGH_DAYS  # 290
CICC_WINDOW_DAYS = 20                 # 中金量价同步、量能领先 单独使用最近 20 日计算
LEAD_EWMA_HALFLIFE = 7.0              # 量能领先：配对(turn_t,ret_t+1)指数加权相关半衰期；IC对比hl=5~7优，取7
UP_VOL_RATIO_MIN = 0.50               # 涨日放量 ≥ 50%（改回：40%时样本增但 score IC_20 降，保留高区分度）
DOWN_VOL_RATIO_MIN = 0.65             # 跌日缩量 ≥ 65%
# 比例推断需满足 success-failure 条件（np、n(1-p)≥5），故涨/跌日至少各 5 日才计算涨日放量/跌日缩量
MIN_UP_DAYS = 5                       # 上涨日至少 5 日，否则涨日放量比例不具统计意义
MIN_DOWN_DAYS = 5                      # 下跌日至少 5 日，否则跌日缩量比例不具统计意义
VOL_PRICE_CORR_MIN = 0.30             # 量价相关系数 ≥ 0.3
PERIOD_RETURN_MAX = 50.0              # 窗口涨幅 < 50%
BREAKOUT_STRENGTH_MAX = 0.30          # 突破前高强度上限：近40日高/前250日高-1 < 30%（IC负向，过滤追高暴涨）
BOLL_POS_MIN = 0.40                   # BOLL 位置 ≥ 40%
BOLL_POS_MAX = 0.85                   # 85%（IC 负向：高位短期收益差，过滤过度超买；原 99.9%）
RSI_MIN = 0.0                         # RSI 无下限
RSI_MAX = 75.0                        # RSI12 ≤ 75
# 均线条件：MA (close>ma20>ma120 或 close>ma20>ma60) 且 EMA (close>ema20>ema120 或 close>ema20>ema60)
AMOUNT_DAYS = 5                       # 近 5 日
# 成交额门槛：采用「筛选日最近 5 个交易日全 A 成交额中位数」的平均值的倍数，随市场冷热自适应；否则用固定值
AMOUNT_USE_MEDIAN_AVG = True          # True=动态(5日中位数均值×倍数)，False=固定 AMOUNT_MIN_YI
AMOUNT_MEDIAN_AVG_MULTIPLIER = 2.0    # 动态时：门槛 = 该倍数 × (最近5日全A成交额中位数的平均值)，单位亿元
AMOUNT_MIN_YI = 3.0                   # 日均成交额固定门槛(亿)，仅当 AMOUNT_USE_MEDIAN_AVG=False 或动态计算失败时使用
TOP_N = 30                            # 输出前 N 名
HOLD_PERIODS = [5, 10, 15]            # 持有期（交易日数）：计算入选日收盘→持有N日后收盘的涨跌幅
OUTPUT_DIR = Path(r"d:\test")          # CSV 输出目录
# 策略使用说明（回测样本 2024-01-02~2026-02-10 硬筛池；每次出报告打印/落盘）
# 成功=持有期收益>0；指标为逐日均值再平均；总分=现行权重（脉冲不计）
STRATEGY_USAGE_LINES = [
    "【本策略使用说明】样本 2024-01-02~2026-02-10 硬筛池 | 成功=持有期收益>0 | 逐日均值再平均",
    "------------------------------------------------------------------------",
    "一、多少分值得参与（主看 20 日期望，勿只盯胜率）",
    "  全池基线: wr20≈46.5%  mean20≈+2.15%",
    "  [推荐主仓] 综合评分 >= 62.5  → 日均约18只  wr20≈49.5%  mean20≈+3.30%  (相对全池 +3.0pp / +1.15pp)",
    "  [进取参考] 综合评分 >= 67.5  → 日均约9只   wr20≈50.0%  mean20≈+2.56%  (胜率略升、收益回落)",
    "  [不建议]   综合评分 >= 70    → 期望收益回落至约+1.9%，样本变稀，不宜作硬门槛",
    "  [回避]     当日截面得分 Q1~Q2 或 评分<55 → 20日成功率偏低、均值常为负",
    "  截面优先做当日 Q4~Q5；得分与收益正相关(IC20≈0.15)，与「是否盈利」相关偏弱(≈0.07)",
    "",
    "二、市值 / 流通市值 / 成交额 vs 成功率",
    "  总市值、流通市值: 正相关(截面 IC20≈0.13) — 池内偏大市值成功率与收益更高",
    "    流通市值五分位 wr20: Q1≈39% → Q5≈48%",
    "  成交额(当日/近5日均): 弱负或接近零(IC20≈-0.04) — 勿再抬成交额硬筛",
    "  配合建议: 主仓 score>=62.5 且 流通市值不低于当日入选池中位数；极高分小票可作卫星，勿只堆超大盘高分",
    "",
    "三、权重摘要(100分): 量价10 + R2·8 + 波动IVOL22.5 + 起涨区22.5 + 同步15 + 领先15 + 趋势稳健7",
    "  观察不计入总分: 脉冲放量缩量、涨日放量、量能不对称度",
    "------------------------------------------------------------------------",
]
SCORE_PARTICIPATE_MIN = 62.5          # 推荐主仓门槛（与 STRATEGY_USAGE_LINES 一致）
SCORE_PARTICIPATE_AGGRESSIVE = 67.5   # 进取参考门槛


def _safePrintText(s: str) -> None:
    """控制台打印，兼容 Windows 编码。"""
    try:
        print(s)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(s.encode(enc, errors="replace").decode(enc))


def emitStrategyUsageGuide(
    *,
    resultDf: Optional[pd.DataFrame] = None,
    topDf: Optional[pd.DataFrame] = None,
    outPath: Optional[Path] = None,
) -> None:
    """打印并可选落盘策略使用说明；附带当日达标家数快照。"""
    lines = list(STRATEGY_USAGE_LINES)
    snap: list[str] = []
    if resultDf is not None and "score" in resultDf.columns and len(resultDf):
        sc = pd.to_numeric(resultDf["score"], errors="coerce")
        nAll = int(sc.notna().sum())
        nMain = int((sc >= SCORE_PARTICIPATE_MIN).sum())
        nAgg = int((sc >= SCORE_PARTICIPATE_AGGRESSIVE).sum())
        snap.append(
            f"当日硬筛池: {nAll} 只 | >= {SCORE_PARTICIPATE_MIN:g}(主仓): {nMain} 只 | "
            f">= {SCORE_PARTICIPATE_AGGRESSIVE:g}(进取): {nAgg} 只"
        )
        if "circ_mv_yi" in resultDf.columns:
            circ = pd.to_numeric(resultDf["circ_mv_yi"], errors="coerce")
            med = circ.median()
            if pd.notna(med) and nMain > 0:
                maskMain = sc >= SCORE_PARTICIPATE_MIN
                nMainLarge = int((maskMain & (circ >= med)).sum())
                snap.append(
                    f"主仓且流通市值>=当日池中位({med:.1f}亿): {nMainLarge} 只"
                )
    if topDf is not None and "score" in topDf.columns and len(topDf):
        scTop = pd.to_numeric(topDf["score"], errors="coerce")
        snap.append(
            f"Top{len(topDf)} 中 >= {SCORE_PARTICIPATE_MIN:g}: "
            f"{int((scTop >= SCORE_PARTICIPATE_MIN).sum())} 只"
        )
    if snap:
        lines = lines + [""] + ["【本日快照】"] + [f"  {x}" for x in snap]

    _safePrintText("")
    for line in lines:
        _safePrintText(line)
    if outPath is not None:
        try:
            outPath.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
            _safePrintText(f"(使用说明已写入 {outPath})")
        except Exception as exc:
            _safePrintText(f"(使用说明落盘失败: {exc})")

# 趋势稳健：斜率一致性按 5/10/25 日三段（合计 40 日），与主窗口一致；满分随权重包下调
TREND_ROBUST_SEGMENTS = (5, 10, 25)   # 三段长度（从早到晚）
TREND_ROBUST_SCORE_MAX = 7.0          # 趋势稳健满分 7（原10；SE 3.5 + 斜率 3.5）
TREND_ROBUST_W_SE = 3.5               # 残差标准差贡献上限
TREND_ROBUST_W_SLOPE = 3.5            # 斜率一致性贡献上限
# R² 回归用价：close=仅收盘，typical=(H+L+2C)/4 更抗单日收盘噪声；效果与有效性优先用 typical
R2_PRICE_SOURCE = "typical"           # "close" | "typical"
R2_USE_ADJUSTED = True                # True=用调整 R²（小样本更有效），False=用原始 R²
SCORE_R2_MAX = 8.0                    # R² 满分（原15，权重包下调）
SCORE_SYNC_MAX = 15.0                 # 量价同步满分（原5，IC效率高故提权）
SCORE_VOL_MAX = 22.5                  # 波动率(IVOL)满分（原20；脉冲退出总分后均分+2.5）
SCORE_DRAWDOWN_MAX = 22.5             # 起涨区满分（原20；脉冲退出总分后均分+2.5）
# 波动率双轨（2026-07 面板复盘锁定）：
#   打分 = 特质波动 IVOL（对沪深300 单因子回归残差的 EWMA std）
#   硬筛 = 总波动 EWMA std ≤ VOL_PCT_MAX（尾部风控，口径不变）
VOL_IVOL_BENCHMARK = "000300.SH"   # IVOL 市场基准：沪深300（中证800≈等价，国证A指略差，已对比锁定）
VOL_IVOL_ZERO_SCORE = 11.2         # IVOL(%)≥此值则 score_vol=0；按 2024-01~2026-02 面板 P90 对齐标定
VOL_PCT_ZERO_SCORE = VOL_IVOL_ZERO_SCORE  # 兼容旧名，等同 VOL_IVOL_ZERO_SCORE
# 总波动硬筛选：EWMA日波动率(%)超过此值则直接排除；None=不启用硬筛选（仅打分）
VOL_PCT_MAX = 15.0
# EWMA波动率：半衰期（交易日）；每向前推 HALFLIFE 日权重减半，近期权重更高，等效窗口约14日
VOL_EWMA_HALFLIFE = 10
# 量能不对称度满分：涨日平均量比/跌日平均量比，[0.5,2.0]线性映射到[0,7]
VOL_ASYM_SCORE_MAX = 7.0
# 规律性脉冲放量缩量整理（仅观察，不计入总分）：放量日 vol>MA(vol,20)*1.5 且涨幅≥7%；
# 调整期（至下一脉冲日前，最多10日）内无单日跌幅≥6%；脉冲越少观察分越高
PULSE_VOL_MA_WINDOW = 20
PULSE_VOL_MA_RATIO_MIN = 1.5      # 放量日 vol 至少为 MA(vol,20) 的倍数
PULSE_RETURN_MIN = 7.0              # 放量日涨幅 ≥ 7%
PULSE_ADJ_MAX_DROP = -6.0           # 调整期单日跌幅不超 6%（即 pct_chg > -6）
PULSE_ADJ_LOOKAHEAD_MAX = 10        # 调整期最多看 10 个交易日
PULSE_ADJ_SCORE_MAX = 5.0           # 观察分满分 5（0→5…5+→0）；2026-07 复盘后退出总分，权重并入 vol/dd


# --------------- 各子项满分（权重见 docstring） ---------------
# 量价质量 10：跌日缩量×8 + corr(pct,换手)×7（内部0-15→缩放到10；硬筛仍用 volume_ratio 相关≥0.3）
# R² 8：典型价+调整R²，截断[0,1]×SCORE_R2_MAX
# 波动率(IVOL) SCORE_VOL_MAX：对 VOL_IVOL_BENCHMARK 回归残差 EWMA std；硬筛用总波动
# 起涨区 SCORE_DRAWDOWN_MAX：max(0,(1-回落幅%/20)×满分)；日内高点为参考
# 中金量价同步 SCORE_SYNC_MAX：(corr+1)/2×满分（趋势子集正向）
# 中金量能领先 15：近端加权 corr(turn_t,ret_t+1)→(corr+1)/2×15（LEAD_EWMA_HALFLIFE）
# 趋势稳健 TREND_ROBUST_SCORE_MAX：SE截面归一化 + 斜率一致性(5/10/25)
# 脉冲 5：脉冲越少得分越高（待下一轮复盘）
# 观察字段：score_up_vol、score_vol_div/vol_asym
# 2026-07：lv 相关改换手 IC20 0.045→0.082；sync 提权、r2/趋势稳健降权（公式对比无更优替代）。


def computeAmountThresholdQianyuan(
    conn, trade_dates: list[str], n_days: int = 5, multiplier: float = None
) -> Optional[float]:
    """
    计算成交额动态门槛（千元），用于与近 n 日日均成交额(千元)比较。
    门槛 = multiplier × (最近 n 个交易日「全 A 成交额中位数」的平均值)，单位先转为亿元再乘回千元。
    amount 在库中为千元，1 亿 = 1e5 千元。
    trade_dates 按日期降序（最近在前）。不足 n 日或计算失败时返回 None，调用方用固定 AMOUNT_MIN_YI。
    """
    if multiplier is None:
        multiplier = AMOUNT_MEDIAN_AVG_MULTIPLIER
    if not trade_dates or len(trade_dates) < n_days:
        return None
    last_n = trade_dates[:n_days]
    try:
        placeholders = ",".join(["%s"] * len(last_n))
        sql = f"SELECT trade_date, amount FROM stk_factor_pro WHERE trade_date IN ({placeholders}) AND amount IS NOT NULL"
        df = pd.read_sql(sql, conn, params=last_n)
        if df.empty:
            return None
        df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "", regex=False)
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        medians_yi = []
        for d in last_n:
            day_vals = df.loc[df["trade_date"] == d, "amount"].dropna()
            if day_vals.empty:
                return None
            medians_yi.append(float(day_vals.median()) / 1e5)
        if len(medians_yi) < n_days:
            return None
        avg_yi = sum(medians_yi) / len(medians_yi)
        return multiplier * avg_yi * 1e5
    except Exception:
        return None


def ewmaStdPct(pct: np.ndarray, halfLife: float = VOL_EWMA_HALFLIFE) -> float:
    """pct_chg(%) 序列的 EWMA 标准差（%）。"""
    x = np.asarray(pct, dtype=float)
    n = len(x)
    if n < 5:
        return float("nan")
    lam = 2.0 ** (-1.0 / halfLife)
    w = np.array([lam ** i for i in range(n - 1, -1, -1)], dtype=float)
    mask = np.isfinite(x)
    if mask.sum() < 5:
        return float("nan")
    ww = np.where(mask, w, 0.0)
    sw = ww.sum()
    if sw <= 0:
        return float("nan")
    ww = ww / sw
    xx = np.where(mask, x, 0.0)
    mean = float(np.dot(ww, xx))
    var = float(np.dot(ww, (xx - mean) ** 2))
    return float(np.sqrt(max(0.0, var)))


def ewmaPearsonCorr(x: np.ndarray, y: np.ndarray, halfLife: float) -> float:
    """
    指数加权 Pearson 相关：序列末尾为最新，半衰期 halfLife（交易日）。
    用于量能领先：x=turn[:-1], y=pct[1:]。方差过小或有效点不足时返回 0。
    """
    a = np.asarray(x, dtype=float)
    b = np.asarray(y, dtype=float)
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = a[-n:], b[-n:]
    mask = np.isfinite(a) & np.isfinite(b)
    if int(mask.sum()) < 3:
        return 0.0
    ages = np.arange(n - 1, -1, -1, dtype=float)  # 0=最新
    w = np.power(0.5, ages / max(float(halfLife), 1e-6))
    w = np.where(mask, w, 0.0)
    sw = float(w.sum())
    if sw <= 0:
        return 0.0
    w = w / sw
    aa = np.where(mask, a, 0.0)
    bb = np.where(mask, b, 0.0)
    xBar = float(np.dot(w, aa))
    yBar = float(np.dot(w, bb))
    cov = float(np.dot(w, (aa - xBar) * (bb - yBar)))
    sx = float(np.sqrt(max(0.0, np.dot(w, (aa - xBar) ** 2))))
    sy = float(np.sqrt(max(0.0, np.dot(w, (bb - yBar) ** 2))))
    if sx < 1e-12 or sy < 1e-12:
        return 0.0
    c = cov / (sx * sy)
    if not np.isfinite(c):
        return 0.0
    return float(max(-1.0, min(1.0, c)))


def idioEwmaPct(pct: np.ndarray, mktPct: np.ndarray, halfLife: float = VOL_EWMA_HALFLIFE) -> float:
    """对市场收益做单因子 OLS，残差再 EWMA 标准差（%）= IVOL。"""
    y = np.asarray(pct, dtype=float)
    x = np.asarray(mktPct, dtype=float)
    mask = np.isfinite(y) & np.isfinite(x)
    if mask.sum() < 10:
        return float("nan")
    y, x = y[mask], x[mask]
    xMean, yMean = float(x.mean()), float(y.mean())
    varX = float(np.sum((x - xMean) ** 2))
    if varX <= 1e-12:
        resid = y - yMean
    else:
        beta = float(np.sum((x - xMean) * (y - yMean)) / varX)
        alpha = yMean - beta * xMean
        resid = y - (alpha + beta * x)
    return ewmaStdPct(resid, halfLife=halfLife)


def fetchIndexPctMap(conn, tsCode: str, trade_dates: list[str]) -> dict[str, float]:
    """
    拉取指数日涨跌幅，返回 {YYYYMMDD: pct_chg}。
    trade_dates 可为 YYYYMMDD 或 YYYY-MM-DD。
    """
    if not trade_dates:
        return {}
    datesYmd = [str(d).replace("-", "") for d in trade_dates]
    # index_daily.trade_date 为 DATE，用区间查再过滤，避免 IN 格式不一致
    dMin, dMax = min(datesYmd), max(datesYmd)
    dMinSql = f"{dMin[:4]}-{dMin[4:6]}-{dMin[6:]}"
    dMaxSql = f"{dMax[:4]}-{dMax[4:6]}-{dMax[6:]}"
    try:
        df = pd.read_sql(
            """
            SELECT trade_date, pct_chg FROM index_daily
            WHERE ts_code=%s AND trade_date>=%s AND trade_date<=%s
            """,
            conn,
            params=[tsCode, dMinSql, dMaxSql],
        )
    except Exception:
        return {}
    if df is None or df.empty:
        return {}
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "", regex=False)
    df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce")
    want = set(datesYmd)
    out: dict[str, float] = {}
    for _, row in df.iterrows():
        k = str(row["trade_date"])
        if k in want and pd.notna(row["pct_chg"]):
            out[k] = float(row["pct_chg"])
    return out


def fetchVolPriceData(conn, trade_dates: list[str]) -> Optional[pd.DataFrame]:
    """从 stk_factor_pro 拉取量价数据（含 amount、boll、rsi_qfq_12、turnover_rate_f 自由流通换手率）。"""
    if not trade_dates:
        return None
    placeholders = ",".join(["%s"] * len(trade_dates))
    sql = f"""
    SELECT ts_code, trade_date, pct_chg, vol, volume_ratio, turnover_rate_f, close_qfq, high_qfq, low_qfq, amount,
           boll_upper_qfq, boll_lower_qfq, rsi_qfq_12, total_mv, circ_mv
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
    num_cols = ["pct_chg", "vol", "volume_ratio", "turnover_rate_f", "close_qfq", "high_qfq", "low_qfq", "amount", "total_mv", "circ_mv"]
    for c in num_cols:
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


def preprocessVolPriceByStock(
    vol_price_df: pd.DataFrame,
    screening_date_ymd: str,
    min_days: int = None,
) -> pd.DataFrame:
    """
    按标的预处理：只保留「筛选日=该标的最近交易日」且「该标的至少有 min_days 个交易日」的标的，
    并为每标的保留最近 min_days 行，增加列 stock_rank（1=最近日, 2, ..., min_days）。
    后续突破前高、打分窗口、观察字段均基于此表，避免停牌导致一刀切数据不足。
    screening_date_ymd: 筛选日 YYYYMMDD。
    min_days: 每标的最少保留交易日数，默认 NEED_DAYS_PER_STOCK(290)。
    返回：含 stock_rank 的 DataFrame，每标的恰好 min_days 行；无合格标的时返回空 DataFrame。
    """
    if vol_price_df is None or vol_price_df.empty:
        return pd.DataFrame()
    if min_days is None:
        min_days = NEED_DAYS_PER_STOCK
    if "ts_code" not in vol_price_df.columns or "trade_date" not in vol_price_df.columns:
        return pd.DataFrame()
    screening_date_ymd = str(screening_date_ymd).replace("-", "")
    df = vol_price_df.sort_values(["ts_code", "trade_date"], ascending=[True, False]).copy()
    df["_td_norm"] = df["trade_date"].astype(str).str.replace("-", "", regex=False)
    # 组内排名（1=最近日）
    df["stock_rank"] = df.groupby("ts_code").cumcount() + 1
    # 条件1：该标的最近交易日 == 筛选日
    first_dates = df.loc[df["stock_rank"] == 1, ["ts_code", "_td_norm"]].set_index("ts_code")["_td_norm"]
    valid_codes = first_dates[first_dates == screening_date_ymd].index
    # 条件2：该标的至少 min_days 行
    code_counts = df.groupby("ts_code").size()
    enough_codes = code_counts[code_counts >= min_days].index
    keep_codes = valid_codes.intersection(enough_codes)
    if keep_codes.empty:
        return pd.DataFrame()
    out = df[df["ts_code"].isin(keep_codes) & (df["stock_rank"] <= min_days)].copy()
    out = out.drop(columns=["_td_norm"]).reset_index(drop=True)
    return out


def _preprocessed_code_col(df: pd.DataFrame) -> Optional[str]:
    """预处理表或量价表的代码列名，兼容 ts_code 或首列。"""
    if df is None or df.empty:
        return None
    return "ts_code" if "ts_code" in df.columns else (str(df.columns[0]) if len(df.columns) else None)


def _buildPreprocessedIndex(preprocessed_df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """将预处理结果按标的分组为 {ts_code_str: grp_df}，避免循环中逐次全表 filter。"""
    code_col = _preprocessed_code_col(preprocessed_df)
    if code_col is None or preprocessed_df.empty:
        return {}
    return {str(k): v for k, v in preprocessed_df.groupby(code_col)}


def filterBreakoutHigh(preprocessed_df: pd.DataFrame) -> pd.DataFrame:
    """
    保留「最近40日最高价 > 前250日(第41~290日)最高价」的标的。
    入参为预处理结果（含 stock_rank 列，每标的 290 行）。返回列 ts_code + breakout_strength(近40日高/前250日高-1)。
    """
    window_days = max(WINDOW_LIST)
    need = NEED_DAYS_PER_STOCK
    code_col = _preprocessed_code_col(preprocessed_df)
    if preprocessed_df.empty or "high_qfq" not in preprocessed_df.columns or "stock_rank" not in preprocessed_df.columns or code_col is None:
        return pd.DataFrame()
    # 向量化：按标的分别取 40 日最高价和前 250 日最高价
    h40 = preprocessed_df.loc[preprocessed_df["stock_rank"] <= window_days].groupby(code_col)["high_qfq"].max()
    h250 = preprocessed_df.loc[
        (preprocessed_df["stock_rank"] >= window_days + 1) & (preprocessed_df["stock_rank"] <= need)
    ].groupby(code_col)["high_qfq"].max()
    both = pd.DataFrame({"h40": h40, "h250": h250}).dropna()
    both = both[both["h250"] > 0]
    passed = both[both["h40"] > both["h250"]].copy()
    if passed.empty:
        return pd.DataFrame()
    passed["breakout_strength"] = (passed["h40"] / passed["h250"] - 1).round(4)
    # IC 测试：突破幅度越大未来收益越差，过滤追高暴涨（breakout_strength > 30%）
    passed = passed[passed["breakout_strength"] <= BREAKOUT_STRENGTH_MAX]
    if passed.empty:
        return pd.DataFrame()
    passed["ts_code"] = passed.index
    return passed[["ts_code", "breakout_strength"]].reset_index(drop=True)


def _windowAndLookbackFromPreprocessed(
    pp_index: dict[str, pd.DataFrame],
    ts_code: str,
    window_days: int,
    lookback_days: int,
) -> tuple[Optional[pd.DataFrame], Optional[np.ndarray]]:
    """从预建索引中取该标的的 40 日窗口与 19 日 vol lookback，供 calcStockScore 使用。"""
    grp_full = pp_index.get(str(ts_code).strip())
    if grp_full is None or "stock_rank" not in grp_full.columns:
        return None, None
    grp = grp_full[grp_full["stock_rank"] <= window_days]
    if len(grp) != window_days:
        return None, None
    win_df = grp.drop(columns=["stock_rank"], errors="ignore").sort_values("trade_date").reset_index(drop=True)
    vol_lookback = None
    if lookback_days > 0 and "vol" in grp_full.columns:
        lb = grp_full[
            (grp_full["stock_rank"] >= window_days + 1) & (grp_full["stock_rank"] <= window_days + lookback_days)
        ].sort_values("trade_date")["vol"].astype(float).values
        if len(lb) == lookback_days:
            vol_lookback = lb
    return win_df, vol_lookback


def computeFirstNewHighAndRetFrom250(
    pp_index: dict[str, pd.DataFrame], ts_codes: list[str]
) -> pd.DataFrame:
    """
    计算「首次创新高」与「筛选日相对250日新高(前41~290日)涨幅%」。仅作观察字段，不参与打分与硬筛选。
    入参为预建索引 {ts_code: grp_df}（含 stock_rank，每标的 290 行）。返回 ts_code, is_first_new_high(0/1), ret_from_250h_pct(%)。
    """
    need = NEED_DAYS_PER_STOCK
    if not pp_index:
        return pd.DataFrame(columns=["ts_code", "is_first_new_high", "ret_from_250h_pct"])
    n_lookback = min(FIRST_HIGH_LOOKBACK, need - 250)

    rows = []
    for ts_code in ts_codes:
        grp = pp_index.get(str(ts_code).strip())
        if grp is None or "high_qfq" not in grp.columns or "close_qfq" not in grp.columns:
            rows.append({"ts_code": ts_code, "is_first_new_high": 0, "ret_from_250h_pct": np.nan})
            continue
        grp = grp.sort_values("stock_rank")
        if len(grp) < need:
            rows.append({"ts_code": ts_code, "is_first_new_high": 0, "ret_from_250h_pct": np.nan})
            continue
        high_arr = pd.to_numeric(grp["high_qfq"], errors="coerce").values
        close_arr = pd.to_numeric(grp["close_qfq"], errors="coerce").values
        if np.any(np.isnan(high_arr)) or np.isnan(close_arr[0]):
            rows.append({"ts_code": ts_code, "is_first_new_high": 0, "ret_from_250h_pct": np.nan})
            continue
        high_250_prior = float(np.nanmax(high_arr[40:need]))
        if high_250_prior <= 0:
            rows.append({"ts_code": ts_code, "is_first_new_high": 0, "ret_from_250h_pct": np.nan})
            continue
        ret_from_250h_pct = round((float(close_arr[0]) - high_250_prior) / high_250_prior * 100, 4)
        count_new_high = 0
        screening_is_new_high = False
        for i in range(n_lookback):
            if i + 250 > len(high_arr):
                break
            window_250 = high_arr[i : i + 250]
            if np.isnan(window_250).any():
                continue
            is_ih = np.isclose(high_arr[i], np.nanmax(window_250), rtol=1e-9, atol=1e-6)
            if is_ih:
                count_new_high += 1
                if i == 0:
                    screening_is_new_high = True
        is_first_new_high = 1 if (count_new_high == 1 and screening_is_new_high) else 0
        rows.append({"ts_code": ts_code, "is_first_new_high": is_first_new_high, "ret_from_250h_pct": ret_from_250h_pct})
    return pd.DataFrame(rows)


def filterMaEmaLong(vol_price_df: pd.DataFrame, ma_df: pd.DataFrame, latest_date: str) -> pd.DataFrame:
    """
    保留收盘价站上均线（MA 与 EMA 均需满足）：
      MA:  close_qfq > ma20 > ma120  或  close_qfq > ma20 > ma60
      EMA: close_qfq > ema20 > ema120 或  close_qfq > ema20 > ema60
    """
    v = vol_price_df[vol_price_df["trade_date"].astype(str) == latest_date.replace("-", "")].copy()
    m = ma_df[ma_df["trade_date"].astype(str) == latest_date.replace("-", "")].copy()
    if v.empty or m.empty:
        return pd.DataFrame()
    v = v.merge(m, on=["ts_code", "trade_date"], how="inner")
    for col in ["ma20", "ma60", "ma120", "ema20", "ema60", "ema120", "close_qfq"]:
        if col not in v.columns:
            return pd.DataFrame()
    v["ma20"] = pd.to_numeric(v["ma20"], errors="coerce")
    v["ma60"] = pd.to_numeric(v["ma60"], errors="coerce")
    v["ma120"] = pd.to_numeric(v["ma120"], errors="coerce")
    v["ema20"] = pd.to_numeric(v["ema20"], errors="coerce")
    v["ema60"] = pd.to_numeric(v["ema60"], errors="coerce")
    v["ema120"] = pd.to_numeric(v["ema120"], errors="coerce")
    v["close_qfq"] = pd.to_numeric(v["close_qfq"], errors="coerce")
    v = v.dropna(subset=["ma20", "ma60", "ma120", "ema20", "ema60", "ema120", "close_qfq"])
    maOk = (
        ((v["close_qfq"] > v["ma20"]) & (v["ma20"] > v["ma120"]))
        | ((v["close_qfq"] > v["ma20"]) & (v["ma20"] > v["ma60"]))
    )
    emaOk = (
        ((v["close_qfq"] > v["ema20"]) & (v["ema20"] > v["ema120"]))
        | ((v["close_qfq"] > v["ema20"]) & (v["ema20"] > v["ema60"]))
    )
    v = v[maOk & emaOk]
    return v[["ts_code"]].drop_duplicates()


def calcStockScore(
    win_df: pd.DataFrame,
    vol_lookback: Optional[np.ndarray] = None,
    amount_min_qianyuan: Optional[float] = None,
    mktPctByDate: Optional[dict[str, float]] = None,
) -> Optional[dict]:
    """
    100 分制各子项计算（趋势稳健在 runScreener 中截面归一化后并入）。
    过滤：涨日放量≥50%、跌日缩量≥65%、量价相关≥0.3、窗口涨幅<PERIOD_RETURN_MAX、BOLL≥40%、RSI12≤75、近5日日均成交额≥门槛；
    若启用 VOL_PCT_MAX 则总波动 EWMA≤VOL_PCT_MAX。score_vol 用沪深300 IVOL（见 VOL_IVOL_BENCHMARK）。
    vol_lookback: 窗口前若干日的成交量序列（按日期升序），用于 MA(vol,20) 向前取数，使 40 日窗口内每一天都可参与脉冲判定。
    amount_min_qianyuan: 近5日日均成交额下限（千元）；None 时用 AMOUNT_MIN_YI*1e5。
    mktPctByDate: {YYYYMMDD: 指数pct_chg}，缺省或对齐不足时 IVOL 回退为总波动 EWMA。
    """
    if win_df is None or len(win_df) < 5:
        return None
    win_df = win_df.sort_values("trade_date").reset_index(drop=True)
    pct = win_df["pct_chg"].values
    vol_ratio = win_df["volume_ratio"].values
    # 中金两指标优先用换手率（与研报一致），缺失时用 volume_ratio
    if "turnover_rate_f" in win_df.columns:
        turn = np.asarray(win_df["turnover_rate_f"].values, dtype=float)
        turn = np.where(np.isnan(turn), np.nanmean(turn), turn) if np.any(np.isnan(turn)) else turn
    else:
        turn = vol_ratio.copy()
    up_days = pct > 0
    down_days = pct < 0
    n_up = up_days.sum()
    n_down = down_days.sum()
    # 涨/跌日不足最小样本量时直接排除（比例推断需 np、n(1-p)≥5）
    if n_up < MIN_UP_DAYS or n_down < MIN_DOWN_DAYS:
        return None
    up_vol_ratio = (vol_ratio[up_days] >= 1.0).sum() / n_up
    down_vol_ratio = (vol_ratio[down_days] < 1.0).sum() / n_down
    # 硬筛：volume_ratio 量价相关（门槛语义不变）
    corr = np.corrcoef(pct, vol_ratio)[0, 1]
    corr = 0.0 if np.isnan(corr) else max(-1.0, min(1.0, corr))
    if up_vol_ratio < UP_VOL_RATIO_MIN or down_vol_ratio < DOWN_VOL_RATIO_MIN or corr < VOL_PRICE_CORR_MIN:
        return None
    # 打分用换手相关（2026-07 对比 IC20 优于 volume_ratio；缺省已在 turn 回退量比）
    corr_turn = np.corrcoef(pct, turn)[0, 1]
    corr_turn = 0.0 if np.isnan(corr_turn) else max(-1.0, min(1.0, corr_turn))
    period_return = (1 + pct / 100).prod() - 1
    period_return_pct = period_return * 100
    if period_return_pct >= PERIOD_RETURN_MAX:
        return None
    last = win_df.iloc[-1]
    boll_upper = last.get("boll_upper_qfq")
    boll_lower = last.get("boll_lower_qfq")
    close_qfq = last.get("close_qfq")
    boll_pos_raw = None   # 布林带位置原始值（供IC测试观察）
    if pd.notna(boll_upper) and pd.notna(boll_lower) and boll_upper > boll_lower and pd.notna(close_qfq):
        boll_pos = (float(close_qfq) - float(boll_lower)) / (float(boll_upper) - float(boll_lower))
        boll_pos_raw = round(boll_pos, 4)
        if boll_pos < BOLL_POS_MIN or boll_pos > BOLL_POS_MAX:
            return None
    rsi12_raw = None      # RSI12 原始值（供IC测试观察）
    rsi12 = last.get("rsi_qfq_12")
    if pd.notna(rsi12):
        rsi12 = float(rsi12)
        rsi12_raw = round(rsi12, 2)
        if rsi12 < RSI_MIN or rsi12 > RSI_MAX:
            return None
    amount_5d_avg_yi = None  # 近5日日均成交额亿元（供IC测试观察）
    if "amount" in win_df.columns and len(win_df) >= AMOUNT_DAYS:
        amount_k = win_df["amount"].tail(AMOUNT_DAYS).mean()
        amount_5d_avg_yi = round(amount_k / 1e5, 4)
        threshold = amount_min_qianyuan if amount_min_qianyuan is not None else AMOUNT_MIN_YI * 1e5
        if amount_k < threshold:
            return None

    # --------------- 1. 量价质量 10：跌日缩量×8 + 换手相关×7；涨日放量仅观察 ---------------
    corr_norm = (corr_turn + 1) / 2
    score_up_vol = max(0.0, min(7.0, up_vol_ratio * 7.0))         # 涨日放量（观察字段，不参与总分）
    score_down_vol = max(0.0, min(8.0, down_vol_ratio * 8.0))     # 跌日缩量 满分 8
    score_corr_lv = max(0.0, min(7.0, corr_norm * 7.0))           # 换手-收益相关 满分 7
    scoreLv = score_down_vol + score_corr_lv                      # 原始量价质量 0-15 分，不再含 score_up_vol
    scoreLvRaw = scoreLv
    scoreLv = max(0.0, min(10.0, scoreLvRaw / 15.0 * 10.0))       # 线性缩放到 0-10

    # --------------- 2. R²：典型价+调整R² → ×SCORE_R2_MAX；并算 SE/斜率一致性供趋势稳健 ---------------
    close_vals = win_df["close_qfq"].astype(float).values
    if R2_PRICE_SOURCE == "typical" and "high_qfq" in win_df.columns and "low_qfq" in win_df.columns:
        h = win_df["high_qfq"].astype(float).values
        l = win_df["low_qfq"].astype(float).values
        y_vals = (h + l + 2.0 * close_vals) / 4.0  # 典型价 (H+L+2C)/4，抗单日收盘噪声
    else:
        y_vals = close_vals.copy()
    se_residual = np.nan
    slope_consist_ratio = 0.0
    if np.any(np.isnan(y_vals)) or len(y_vals) < 3:
        scoreR2 = 0.0
    else:
        n_vals = len(y_vals)
        x = np.arange(n_vals, dtype=float)
        x_mean = x.mean()
        y_mean = np.nanmean(y_vals)
        ss_tot = np.nansum((y_vals - y_mean) ** 2)
        if ss_tot <= 0:
            scoreR2 = 0.0
        else:
            coef = np.nansum((x - x_mean) * (y_vals - y_mean)) / (np.nansum((x - x_mean) ** 2) + 1e-20)
            intercept = y_mean - coef * x_mean
            fitted = intercept + coef * x
            ss_res = np.nansum((y_vals - fitted) ** 2)
            r2 = 1.0 - (ss_res / ss_tot)
            if R2_USE_ADJUSTED and n_vals > 2:
                # 调整 R² = 1 - (1-R²)*(n-1)/(n-p)，p=2；小样本下更有效、避免虚高
                r2 = 1.0 - (1.0 - r2) * (n_vals - 1) / (n_vals - 2)
            r2 = max(0.0, min(1.0, r2))
            scoreR2 = r2 * SCORE_R2_MAX
            # 残差标准差 SE = sqrt(SS_res/(n-2))，用于趋势稳健截面归一化
            if n_vals > 2:
                se_residual = float(np.sqrt(ss_res / (n_vals - 2)))
        # 斜率一致性：5/10/25 三段，每段 y~time 回归斜率符号，同号比例 [0,1]（与 R² 同价源）
        a, b, c = TREND_ROBUST_SEGMENTS[0], TREND_ROBUST_SEGMENTS[1], TREND_ROBUST_SEGMENTS[2]
        if n_vals >= a + b + c:  # 至少 40 点
            segs = [y_vals[0:a], y_vals[a : a + b], y_vals[a + b : a + b + c]]
            signs = []
            for seg in segs:
                if len(seg) < 2:
                    continue
                x_s = np.arange(len(seg), dtype=float)
                xm, ym = x_s.mean(), np.nanmean(seg)
                var_x = np.nansum((x_s - xm) ** 2)
                if var_x <= 0:
                    continue
                coef_s = np.nansum((x_s - xm) * (seg - ym)) / var_x
                signs.append(1 if coef_s > 0 else (-1 if coef_s < 0 else 0))
            if len(signs) == 3:
                # 三同号=1，两同号=2/3，否则 1/3 或 0
                s0, s1, s2 = signs[0], signs[1], signs[2]
                if s0 == s1 == s2 and s0 != 0:
                    slope_consist_ratio = 1.0
                elif (s0 == s1 and s0 != 0) or (s1 == s2 and s1 != 0) or (s0 == s2 and s0 != 0):
                    slope_consist_ratio = 2.0 / 3.0
                elif any(z != 0 for z in signs):
                    slope_consist_ratio = 1.0 / 3.0
                else:
                    slope_consist_ratio = 0.0
    scoreR2 = max(0.0, min(SCORE_R2_MAX, float(scoreR2)))

    # --------------- 3. 波动率双轨：总波动硬筛 + 沪深300 IVOL 打分 ---------------
    # 总波动：pct_chg EWMA std（半衰期 VOL_EWMA_HALFLIFE）→ VOL_PCT_MAX 硬筛
    # IVOL：对 VOL_IVOL_BENCHMARK 回归残差再 EWMA → score_vol（零分线 VOL_IVOL_ZERO_SCORE）
    vol_pct = ewmaStdPct(pct)  # 总波动%（硬筛/观察）
    if not np.isfinite(vol_pct):
        vol_pct = 0.0
    if VOL_PCT_MAX is not None and vol_pct > VOL_PCT_MAX:
        return None
    dateKeys = (
        win_df["trade_date"].astype(str).str.replace("-", "", regex=False).tolist()
    )
    if mktPctByDate:
        mktArr = np.array([mktPctByDate.get(d, np.nan) for d in dateKeys], dtype=float)
    else:
        mktArr = np.full(len(pct), np.nan)
    vol_idio = idioEwmaPct(pct, mktArr)
    if not np.isfinite(vol_idio):
        vol_idio = vol_pct  # 指数对齐不足时回退总波动，避免整票被静默丢分
    scoreVol = max(0.0, (1.0 - vol_idio / VOL_IVOL_ZERO_SCORE) * SCORE_VOL_MAX)

    # --------------- 4. 起涨区 SCORE_DRAWDOWN_MAX：得分 = max(0, (1 − 回落幅%/20)×满分) ---------------
    # 使用日内最高价（high_qfq）而非收盘价高点：趋势股突破时日内高点才是真正阻力位
    # 尝试过 A(收盘价高点)+B(波动率自适应阈值)+C(凸函数)，OOS IC_20日从0.088↓0.032，全面变差，回滚
    high = win_df["high_qfq"].max()
    close_last = win_df["close_qfq"].iloc[-1]
    drawdown = (1 - close_last / high) * 100 if high and high > 0 else 0.0
    scoreDrawdown = max(
        0.0,
        min(SCORE_DRAWDOWN_MAX, (1.0 - drawdown / 20.0) * SCORE_DRAWDOWN_MAX),
    )

    # --------------- 5/6. 中金量价同步、量能领先：单独用最近 CICC_WINDOW_DAYS 日（与主筛选 40 日窗口分离） ---------------
    win_20 = win_df.tail(CICC_WINDOW_DAYS)
    if len(win_20) >= 5:
        pct_20 = win_20["pct_chg"].values
        if "turnover_rate_f" in win_20.columns:
            turn_20 = np.asarray(win_20["turnover_rate_f"].values, dtype=float)
            turn_20 = np.where(np.isnan(turn_20), np.nanmean(turn_20), turn_20) if np.any(np.isnan(turn_20)) else turn_20
        else:
            turn_20 = win_20["volume_ratio"].values
        sync_corr = np.corrcoef(pct_20, turn_20)[0, 1]
        sync_corr = 0.0 if np.isnan(sync_corr) else max(-1, min(1, sync_corr))
        # 趋势股子集正向映射；满分 SCORE_SYNC_MAX（原5，IC/权重偏高故提至15）
        scoreSync = (sync_corr + 1) / 2 * SCORE_SYNC_MAX
        scoreSync = max(0.0, min(SCORE_SYNC_MAX, scoreSync))
        # 量能领先：换手率(t) 与 收益率(t+1) 近端加权 Pearson（LEAD_EWMA_HALFLIFE）
        # 等权/Δturn/残差领先对比见 _tmp_score_lead_alt_compare：EWMA(hl=5~7) IC20/Top30 最优
        if len(pct_20) >= 3:
            lead_corr = ewmaPearsonCorr(turn_20[:-1], pct_20[1:], LEAD_EWMA_HALFLIFE)
            scoreLead = (lead_corr + 1) / 2 * 15.0
            scoreLead = max(0.0, min(15.0, scoreLead))
        else:
            scoreLead = 0.0
    else:
        scoreSync = 0.0
        scoreLead = 0.0

    # --------------- 7. 量能不对称度（观察字段，不参与总分） ---------------
    # OOS IC_20日=-0.022，在趋势股子集中涨日过度放量反而是追涨信号，为负向因子，降为纯观察
    if n_up >= 3 and n_down >= 3:
        avg_vr_up = float(np.nanmean(vol_ratio[up_days]))
        avg_vr_down = float(np.nanmean(vol_ratio[down_days]))
        vol_asym = avg_vr_up / avg_vr_down if avg_vr_down > 0 else 1.0
        # [0.5,2.0]线性映射到[0,7]，供观察用
        score_vol_div = max(0.0, min(VOL_ASYM_SCORE_MAX, (vol_asym - 0.5) / 1.5 * VOL_ASYM_SCORE_MAX))
    else:
        vol_asym = 1.0
        score_vol_div = 0.0

    # 不含趋势稳健的 partial（满分 93 = 10+8+22.5+22.5+15+15）；趋势稳健 7 在 runScreener 并入 → 100
    scorePartial = scoreLv + scoreR2 + scoreVol + scoreDrawdown + scoreSync + scoreLead

    # 规律性脉冲放量缩量整理（仅观察）：放量日 vol>MA(vol,20)×1.5 且 涨幅≥7%；调整期无单日跌≥6%
    # MA(vol,20) 向前取数：用 vol_lookback + 窗口内 vol 拼接后算 rolling(20)，使 40 日窗口内每一天都可判定脉冲
    pulse_adj_valid_count = 0
    score_pulse_adj = 0.0
    if "vol" in win_df.columns and len(win_df) >= 1:
        vol_arr = np.asarray(win_df["vol"].astype(float).values, dtype=float)
        if vol_lookback is not None and len(vol_lookback) > 0:
            lookback = np.asarray(vol_lookback, dtype=float)
            vol_full = np.concatenate([lookback, vol_arr])
        else:
            vol_full = vol_arr
        vol_ma20_full = pd.Series(vol_full).rolling(PULSE_VOL_MA_WINDOW, min_periods=PULSE_VOL_MA_WINDOW).mean().values
        # 与窗口对齐：取 vol_ma20_full 的后 n_win 个，对应窗口内每一日
        n_win = len(vol_arr)
        vol_ma20 = vol_ma20_full[-n_win:] if len(vol_ma20_full) >= n_win else np.full(n_win, np.nan)
        pulse_mask = (vol_arr > vol_ma20 * PULSE_VOL_MA_RATIO_MIN) & (pct >= PULSE_RETURN_MIN)
        pulse_mask = np.where(np.isnan(vol_ma20), False, pulse_mask)
        pulse_idx = np.where(pulse_mask)[0]
        for k, i in enumerate(pulse_idx):
            end_adj = len(pct)
            if k + 1 < len(pulse_idx):
                end_adj = min(pulse_idx[k + 1], i + 1 + PULSE_ADJ_LOOKAHEAD_MAX)
            else:
                end_adj = min(len(pct), i + 1 + PULSE_ADJ_LOOKAHEAD_MAX)
            adj_start = i + 1
            if adj_start >= end_adj:
                pulse_adj_valid_count += 1
            else:
                adj_pct = pct[adj_start:end_adj]
                if np.all(adj_pct > PULSE_ADJ_MAX_DROP):
                    pulse_adj_valid_count += 1
        # 观察分：有效脉冲越少越高（0→5…5+→0）；不计入总分（与 vol 冗余、跨期不稳）
        score_pulse_adj = max(0.0, PULSE_ADJ_SCORE_MAX - float(pulse_adj_valid_count))

    # --------------- 8. 其它观察字段（不参与总分） ---------------

    # STR：换手率变异系数（std/mean），用 40 日完整窗口
    # 文献（东吴证券）全市场 ICIR=-2.968，换手率越稳定→机构型资金→趋势更持续
    # 期望方向：CV越低→后续收益越好（负向因子，IC应为负）
    str_cv = np.nan
    if "turnover_rate_f" in win_df.columns:
        turn_full = np.asarray(win_df["turnover_rate_f"].astype(float).values, dtype=float)
        turn_full_valid = turn_full[~np.isnan(turn_full)]
        if len(turn_full_valid) >= 5:
            t_mean = float(np.mean(turn_full_valid))
            t_std = float(np.std(turn_full_valid))
            str_cv = round(t_std / t_mean, 4) if t_mean > 0 else np.nan

    # DIR_MOM：方向动量（近20日）= 2×(正收益天数/总天数) - 1，结果 ∈[-1,1]
    # 西部证券 D-MOM 在均线多头子集 IC胜率86%，值越高→上涨方向越持续
    # 期望方向：正向（IC应为正）
    pct_window = pct[-min(20, len(pct)):]
    dir_mom_raw = float(np.sum(pct_window > 0)) / len(pct_window) if len(pct_window) > 0 else 0.5
    dir_mom = round(2.0 * dir_mom_raw - 1.0, 4)  # 归一化到[-1,1]

    return {
        "score_lv": round(scoreLv, 2),
        "score_up_vol": round(score_up_vol, 2),      # 观察字段，不参与总分
        "score_down_vol": round(score_down_vol, 2),
        "score_corr_lv": round(score_corr_lv, 2),
        "score_vol_div": round(score_vol_div, 2),     # 量能不对称度（替代涨日放量）
        "score_r2": round(scoreR2, 2),
        "score_vol": round(scoreVol, 2),
        "score_drawdown": round(scoreDrawdown, 2),
        "score_sync": round(scoreSync, 2),
        "score_lead": round(scoreLead, 2),
        "score_partial": round(scorePartial, 2),
        "se_residual": se_residual if not np.isnan(se_residual) else None,
        "slope_consist_ratio": round(slope_consist_ratio, 4),
        "up_vol_ratio": round(up_vol_ratio * 100, 1),
        "down_vol_ratio": round(down_vol_ratio * 100, 1),
        "vol_asym": round(vol_asym, 4),               # 量能不对称比原始值（供观察）
        "corr": round(corr, 4),
        "period_return_pct": round(period_return_pct, 2),
        "drawdown": round(drawdown, 2),
        "pulse_adj_valid_count": pulse_adj_valid_count,
        "score_pulse_adj": round(score_pulse_adj, 2),
        "str_cv": str_cv if not (isinstance(str_cv, float) and np.isnan(str_cv)) else None,  # 换手率稳定性（观察）
        "dir_mom": dir_mom,                                                                    # 方向动量（观察）
        # 硬筛选指标原始连续值（供IC测试验证合理性，通过硬筛后全部满足门槛）
        "boll_pos": boll_pos_raw,           # 布林带位置[0,1]（门槛≥0.40）
        "rsi12": rsi12_raw,                 # RSI12（门槛≤75）
        "amount_5d_avg_yi": amount_5d_avg_yi,  # 近5日日均成交额亿元（流动性门槛）
        "vol_pct": round(vol_pct, 4),           # 总波动 EWMA%（硬筛≤VOL_PCT_MAX，观察）
        "vol_idio_pct": round(float(vol_idio), 4),  # 沪深300 IVOL%（score_vol 用）
    }



def fetchReturnsByHoldingPeriods(
    conn, ts_codes: list, selection_date_ymd: str, hold_days: list[int] = None
) -> pd.DataFrame:
    """
    计算入选日收盘→持有 N 个交易日后收盘、以及→数据库最新日收盘的涨跌幅（前复权 close_qfq），单位 %。
    hold_days: 持有交易日数列表，如 [5, 10, 15]。
    返回 DataFrame: columns = [ts_code] + 可用的 [ret_5d, ret_10d, ...] + [ret_to_latest]。
    仅当入选日后实际交易日数 >= N 时才包含 ret_Nd 列；入选日 == 最新日时不输出 ret_to_latest。
    """
    if hold_days is None:
        hold_days = HOLD_PERIODS
    empty_cols = ["ts_code"] + [f"ret_{d}d" for d in hold_days] + ["ret_to_latest"]
    if not ts_codes:
        return pd.DataFrame(columns=empty_cols)
    # 查询入选日及之后的所有交易日（升序）
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT trade_date FROM stk_factor_pro WHERE trade_date >= %s ORDER BY trade_date ASC",
        (selection_date_ymd,),
    )
    all_dates = [str(r[0]).replace("-", "") for r in cur.fetchall()]
    cur.close()
    if not all_dates or all_dates[0] != selection_date_ymd:
        return pd.DataFrame(columns=empty_cols)
    # 确定各持有期目标日期：all_dates[0]=入选日(T+0)，all_dates[d]=T+d
    target_dates = {selection_date_ymd}
    hold_date_map: dict[int, str] = {}
    for d in hold_days:
        if d < len(all_dates):
            hold_date_map[d] = all_dates[d]
            target_dates.add(all_dates[d])
    latest_ymd = all_dates[-1]
    if latest_ymd != selection_date_ymd:
        target_dates.add(latest_ymd)
    # 一次 SQL 查询所有目标日期的收盘价
    target_list = sorted(target_dates)
    ph_dates = ",".join(["%s"] * len(target_list))
    ph_codes = ",".join(["%s"] * len(ts_codes))
    sql = f"SELECT ts_code, trade_date, close_qfq FROM stk_factor_pro WHERE trade_date IN ({ph_dates}) AND ts_code IN ({ph_codes})"
    try:
        df = pd.read_sql(sql, conn, params=target_list + list(ts_codes))
    except Exception:
        return pd.DataFrame(columns=empty_cols)
    if df.empty:
        return pd.DataFrame(columns=empty_cols)
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "", regex=False)
    df["close_qfq"] = pd.to_numeric(df["close_qfq"], errors="coerce")
    wide = df.pivot(index="ts_code", columns="trade_date", values="close_qfq")
    if selection_date_ymd not in wide.columns:
        return pd.DataFrame(columns=empty_cols)
    c_sel = wide[selection_date_ymd]
    result = pd.DataFrame(index=wide.index)
    # 各持有期收益：仅当目标日期存在时生成该列
    for d in hold_days:
        if d in hold_date_map and hold_date_map[d] in wide.columns:
            result[f"ret_{d}d"] = ((wide[hold_date_map[d]] - c_sel) / c_sel * 100).round(2)
    # 入选至最新日收益
    if latest_ymd != selection_date_ymd and latest_ymd in wide.columns:
        result["ret_to_latest"] = ((wide[latest_ymd] - c_sel) / c_sel * 100).round(2)
    result = result.reindex(ts_codes)
    result.index.name = "ts_code"
    return result.reset_index()


RPS_COLS = ["rps_5", "rps_10", "rps_20", "rps_60", "rps_120", "rps_200", "rps_250"]


def fetchRpsData(conn, ts_codes: list, trade_date_ymd: str) -> pd.DataFrame:
    """从 stk_rps 获取筛选日的 RPS 数据（观察字段，不参与打分）。"""
    if not ts_codes or not trade_date_ymd:
        return pd.DataFrame(columns=["ts_code"] + RPS_COLS)
    ph = ",".join(["%s"] * len(ts_codes))
    sql = f"SELECT ts_code, rps_5, rps_10, rps_20, rps_60, rps_120, rps_200, rps_250 FROM stk_rps WHERE trade_date = %s AND ts_code IN ({ph})"
    try:
        df = pd.read_sql(sql, conn, params=[trade_date_ymd] + list(ts_codes))
    except Exception:
        return pd.DataFrame(columns=["ts_code"] + RPS_COLS)
    for c in RPS_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def mergeIndustry(df: pd.DataFrame, conn_wind) -> pd.DataFrame:
    """按 ts_code 映射 industry_sw_2021_2（申万二级）、股票名称（sec_name）、行业名称。

    原取自 stock_wind.stockcodes（Wind 手工导出快照），该表数据截止 2026-02-12 后停更，
    导致新上市股票取不到名称/行业、且 ST 戴帽摘帽不反映。改用 Tushare 侧实时数据：
    名称取 stock_basic.name，申万二级取 index_member_all_sw 中每股 in_date 最新一行。
    """
    try:
        sc = pd.read_sql(
            """
            SELECT b.ts_code AS ts_code,
                   sw.l2_name AS industry_sw_2021_2,
                   b.name AS sec_name
            FROM stock_tushare.stock_basic b
            LEFT JOIN (
                SELECT ts_code, l2_name FROM (
                    SELECT ts_code, l2_name,
                           ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY in_date DESC, id DESC) AS rn
                    FROM stock_tushare.index_member_all_sw
                    WHERE is_new = 'Y' OR is_new IS NULL
                ) _sw WHERE _sw.rn = 1
            ) sw ON sw.ts_code = b.ts_code
            WHERE b.list_status = 'L'
            """,
            conn_wind,
        )
    except Exception:
        return df
    if sc.empty:
        return df
    # 行业名称：申万行业代码多数库中即中文名，统一用 industry_sw_2021_2 作为行业名称列输出
    sc["industry_name"] = sc["industry_sw_2021_2"]
    df = df.merge(sc[["ts_code", "industry_sw_2021_2", "sec_name", "industry_name"]], on="ts_code", how="left")
    return df



def runScreener(
    vol_price_df: pd.DataFrame,
    preprocessed_df: pd.DataFrame,
    ma_df: pd.DataFrame,
    trade_dates: list[str],
    conn_tushare,
    conn_wind,
    trade_date_str: str,
) -> pd.DataFrame:
    """过滤(均线多头→突破前高→量价门槛) + 逐标的打分 + 截面归一化(趋势稳健) → 按 score 降序。依赖预处理结果 preprocessed_df（按标的 290 日）。"""
    window_days = max(WINDOW_LIST)
    lookback_days = PULSE_VOL_MA_WINDOW - 1
    amount_min_qianyuan = None
    if AMOUNT_USE_MEDIAN_AVG:
        amount_min_qianyuan = computeAmountThresholdQianyuan(
            conn_tushare, trade_dates, n_days=AMOUNT_DAYS, multiplier=AMOUNT_MEDIAN_AVG_MULTIPLIER
        )
    long_codes = filterMaEmaLong(vol_price_df, ma_df, trade_date_str)
    if long_codes.empty:
        print(f"    均线多头: 0 只通过, 终止", flush=True)
        return pd.DataFrame()
    n_long = len(long_codes)
    breakout_codes = filterBreakoutHigh(preprocessed_df)
    if breakout_codes.empty:
        print(f"    均线多头: {n_long} 只 → 突破前高: 0 只通过, 终止", flush=True)
        return pd.DataFrame()
    long_codes = long_codes.merge(breakout_codes, on="ts_code", how="inner")
    if long_codes.empty:
        print(f"    均线多头: {n_long} 只 → 突破前高交集: 0 只, 终止", flush=True)
        return pd.DataFrame()
    n_candidate = len(long_codes)
    print(f"    均线多头: {n_long} 只 → 突破前高交集: {n_candidate} 只 → 开始逐标的打分...", flush=True)
    mktPctByDate = fetchIndexPctMap(conn_tushare, VOL_IVOL_BENCHMARK, trade_dates)
    if len(mktPctByDate) < window_days:
        print(
            f"    警告: {VOL_IVOL_BENCHMARK} 涨跌幅对齐不足({len(mktPctByDate)}/{window_days})，IVOL 将回退总波动",
            flush=True,
        )
    pp_index = _buildPreprocessedIndex(preprocessed_df)
    rows = []
    for ts_code in long_codes["ts_code"].unique():
        win_df, vol_lookback = _windowAndLookbackFromPreprocessed(
            pp_index, ts_code, window_days, lookback_days
        )
        if win_df is None:
            continue
        res = calcStockScore(
            win_df,
            vol_lookback=vol_lookback,
            amount_min_qianyuan=amount_min_qianyuan,
            mktPctByDate=mktPctByDate,
        )
        if res is None:
            continue
        rows.append({"ts_code": ts_code, **res})
    if not rows:
        print(f"    打分通过: 0 只", flush=True)
        return pd.DataFrame()
    print(f"    打分通过: {len(rows)} 只", flush=True)
    out = pd.DataFrame(rows)
    # 突破前高强度（观察字段）：近40日最高价/前250日最高价 - 1
    if "breakout_strength" in long_codes.columns:
        out = out.merge(long_codes[["ts_code", "breakout_strength"]], on="ts_code", how="left")
    # 均线多头强度（观察字段）：MA20/MA60 - 1，反映均线多头排列强弱（通过样本中该值均>0）
    _ma_last = ma_df[ma_df["trade_date"].astype(str) == trade_date_str.replace("-", "")].copy()
    if not _ma_last.empty and "ma20" in _ma_last.columns and "ma60" in _ma_last.columns:
        _ma_last["ma20"] = pd.to_numeric(_ma_last["ma20"], errors="coerce")
        _ma_last["ma60"] = pd.to_numeric(_ma_last["ma60"], errors="coerce")
        _ma_valid = _ma_last[["ts_code", "ma20", "ma60"]].dropna()
        _ma_valid = _ma_valid[_ma_valid["ma60"] > 0].copy()
        _ma_valid["ma20_ma60_ratio"] = (_ma_valid["ma20"] / _ma_valid["ma60"] - 1).round(4)
        out = out.merge(_ma_valid[["ts_code", "ma20_ma60_ratio"]], on="ts_code", how="left")
    out = mergeIndustry(out, conn_wind)
    # 趋势稳健：残差SE截面归一化×TREND_ROBUST_W_SE + 斜率一致性×TREND_ROBUST_W_SLOPE
    se_vals = out["se_residual"].astype(float)
    se_filled = se_vals.where(se_vals.notna(), se_vals.max() + 1.0 if se_vals.notna().any() else 1.0)
    norm_se = se_filled.rank(pct=True, ascending=True)  # 小 SE 得小 norm_se
    score_se = (1.0 - norm_se) * TREND_ROBUST_W_SE
    score_slope = (out["slope_consist_ratio"].astype(float) * TREND_ROBUST_W_SLOPE).round(4)
    out["score_trend_robust"] = (score_se + score_slope).clip(0.0, TREND_ROBUST_SCORE_MAX).round(2)
    # 100 分制：lv10 + r2·8 + vol22.5 + dd22.5 + sync15 + lead15 + trend7（脉冲仅观察）
    out["score"] = (
        out["score_lv"] + out["score_r2"] + out["score_vol"] + out["score_drawdown"]
        + out["score_sync"] + out["score_lead"]
        + out["score_trend_robust"]
    ).round(2)
    out = out.sort_values("score", ascending=False).reset_index(drop=True)
    # 首次创新高、筛选日相对250日新高涨幅%（仅观察；基于预建索引）
    df_extra = computeFirstNewHighAndRetFrom250(
        pp_index, out["ts_code"].astype(str).tolist()
    )
    if not df_extra.empty:
        out = out.merge(df_extra, on="ts_code", how="left")
        n_first = (out["is_first_new_high"] == 1).sum() if "is_first_new_high" in out.columns else 0
        n_ret_na = out["ret_from_250h_pct"].isna().sum() if "ret_from_250h_pct" in out.columns else 0
        if n_first > 0 or n_ret_na > 0:
            print("观察字段: 首次创250日新高=%d只, 筛选日相对250日新高涨幅%%为空=%d只(多为上市不足290日或数据缺失)" % (n_first, n_ret_na))
    if "is_first_new_high" not in out.columns:
        out["is_first_new_high"] = np.nan
    if "ret_from_250h_pct" not in out.columns:
        out["ret_from_250h_pct"] = np.nan
    # 列顺序：代码、名称、行业 优先，便于阅读
    lead_cols = ["ts_code", "sec_name", "industry_name"]
    rest = [c for c in out.columns if c not in lead_cols]
    out = out[[c for c in lead_cols if c in out.columns] + rest]
    return out


def rankDictFromResult(result_df: pd.DataFrame) -> dict[str, int]:
    """从筛选结果 DataFrame（已按 score 降序）得到 {ts_code: rank}，1-based。"""
    if result_df is None or result_df.empty or "ts_code" not in result_df.columns:
        return {}
    return {str(row["ts_code"]).strip(): (i + 1) for i, row in result_df.iterrows()}


def _checkCalcScoreConditions(
    win_df: pd.DataFrame, amount_min_qianyuan: Optional[float] = None
) -> tuple[bool, Optional[str]]:
    """
    与 calcStockScore 相同的门槛检查，返回 (是否通过, 未通过时的具体原因)。
    用于诊断某标的在指定截面日被排除的原因。
    amount_min_qianyuan: 近5日日均成交额下限（千元）；None 时用 AMOUNT_MIN_YI*1e5。
    """
    if win_df is None or len(win_df) < 5:
        return False, "窗口数据不足(需至少5个交易日)"
    win_df = win_df.sort_values("trade_date").reset_index(drop=True)
    pct = win_df["pct_chg"].values
    vol_ratio = win_df["volume_ratio"].values
    up_days = pct > 0
    down_days = pct < 0
    n_up = up_days.sum()
    n_down = down_days.sum()
    if n_up < MIN_UP_DAYS:
        return False, f"上涨日={n_up} (要求≥{MIN_UP_DAYS}日，比例推断需足够样本)"
    if n_down < MIN_DOWN_DAYS:
        return False, f"下跌日={n_down} (要求≥{MIN_DOWN_DAYS}日，比例推断需足够样本)"
    up_vol_ratio = (vol_ratio[up_days] >= 1.0).sum() / n_up
    down_vol_ratio = (vol_ratio[down_days] < 1.0).sum() / n_down
    corr = np.corrcoef(pct, vol_ratio)[0, 1]
    corr = 0.0 if np.isnan(corr) else max(-1.0, min(1.0, corr))
    if up_vol_ratio < UP_VOL_RATIO_MIN:
        return False, f"涨日放量={up_vol_ratio*100:.1f}% (要求≥{UP_VOL_RATIO_MIN*100:.0f}%)"
    if down_vol_ratio < DOWN_VOL_RATIO_MIN:
        return False, f"跌日缩量={down_vol_ratio*100:.1f}% (要求≥{DOWN_VOL_RATIO_MIN*100:.0f}%)"
    if corr < VOL_PRICE_CORR_MIN:
        return False, f"量价相关={corr:.3f} (要求≥{VOL_PRICE_CORR_MIN})"
    period_return = (1 + pct / 100).prod() - 1
    period_return_pct = period_return * 100
    if period_return_pct >= PERIOD_RETURN_MAX:
        return False, f"窗口涨幅={period_return_pct:.1f}% (要求<{PERIOD_RETURN_MAX}%)"
    last = win_df.iloc[-1]
    boll_upper = last.get("boll_upper_qfq")
    boll_lower = last.get("boll_lower_qfq")
    close_qfq = last.get("close_qfq")
    if pd.notna(boll_upper) and pd.notna(boll_lower) and boll_upper > boll_lower and pd.notna(close_qfq):
        boll_pos = (float(close_qfq) - float(boll_lower)) / (float(boll_upper) - float(boll_lower))
        if boll_pos < BOLL_POS_MIN:
            return False, f"BOLL位置={boll_pos*100:.1f}% (要求≥{BOLL_POS_MIN*100:.0f}%)"
        if boll_pos > BOLL_POS_MAX:
            return False, f"BOLL位置={boll_pos*100:.1f}% (要求≤{BOLL_POS_MAX}%)"
    rsi12 = last.get("rsi_qfq_12")
    if pd.notna(rsi12):
        rsi12 = float(rsi12)
        if rsi12 < RSI_MIN:
            return False, f"RSI12={rsi12:.1f} (要求≥{RSI_MIN})"
        if rsi12 > RSI_MAX:
            return False, f"RSI12={rsi12:.1f} (要求≤{RSI_MAX})"
    if "amount" in win_df.columns and len(win_df) >= AMOUNT_DAYS:
        amount_k = win_df["amount"].tail(AMOUNT_DAYS).mean()
        amount_yi = amount_k / 1e5  # 千元 -> 亿元
        threshold = amount_min_qianyuan if amount_min_qianyuan is not None else AMOUNT_MIN_YI * 1e5
        req_yi = threshold / 1e5
        if amount_k < threshold:
            return False, f"近{AMOUNT_DAYS}日日均成交额={amount_yi:.2f}亿 (要求≥{req_yi:.2f}亿)"
    vol_pct = ewmaStdPct(pct)
    if not np.isfinite(vol_pct):
        vol_pct = 0.0
    if VOL_PCT_MAX is not None and vol_pct > VOL_PCT_MAX:
        return False, f"总波动EWMA={vol_pct:.2f}% (要求≤{VOL_PCT_MAX}%)"
    return True, None


def diagnoseExclusion(
    conn_t, conn_w, ts_code: str, as_of_date: str
) -> list[tuple[str, bool, str]]:
    """
    诊断某标的在指定截面日被筛选排除的具体原因。
    as_of_date 格式 YYYY-MM-DD 或 YYYYMMDD。
    返回 [(步骤名, 是否通过, 说明), ...]，按执行顺序。
    """
    ts_code = str(ts_code).strip()
    as_of_ymd = as_of_date.replace("-", "")
    window_days = max(WINDOW_LIST)
    need_dates = max(window_days + PRIOR_HIGH_DAYS + 5, NEED_DATES_EXTRA_FOR_250H)
    cur = conn_t.cursor()
    cur.execute(
        "SELECT DISTINCT trade_date FROM stk_factor_pro WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT %s",
        (as_of_ymd, need_dates),
    )
    raw_dates = [str(r[0]) for r in cur.fetchall()]
    cur.close()
    if len(raw_dates) < window_days:
        return [("交易日", False, f"该日及之前交易日不足(需≥{window_days})")]
    trade_dates = [d.replace("-", "") for d in raw_dates]
    trade_date_str = f"{as_of_ymd[:4]}-{as_of_ymd[4:6]}-{as_of_ymd[6:]}"
    amount_min_qianyuan = None
    if AMOUNT_USE_MEDIAN_AVG:
        amount_min_qianyuan = computeAmountThresholdQianyuan(
            conn_t, trade_dates, n_days=AMOUNT_DAYS, multiplier=AMOUNT_MEDIAN_AVG_MULTIPLIER
        )
    vol_price_df = fetchVolPriceData(conn_t, trade_dates)
    ma_df = fetchMaData(conn_t, trade_dates)
    if vol_price_df is None or vol_price_df.empty:
        return [("量价数据", False, "无法拉取量价数据")]
    if ts_code not in vol_price_df["ts_code"].astype(str).values:
        return [("量价数据", False, "该日无此标的数据或 ts_code 不在库中")]
    if ma_df is None or ma_df.empty:
        return [("均线数据", False, "无法拉取均线数据")]
    out: list[tuple[str, bool, str]] = []
    long_codes = filterMaEmaLong(vol_price_df, ma_df, trade_date_str)
    if long_codes.empty:
        return [("均线多头", False, "当日无任何标的通过均线多头")]
    if ts_code not in long_codes["ts_code"].astype(str).values:
        return [("均线多头", False, "未通过：MA(close>ma20>ma120 或 close>ma20>ma60) 且 EMA(close>ema20>ema120 或 close>ema20>ema60) 不满足")]
    out.append(("均线多头", True, "通过"))
    preprocessed_df = preprocessVolPriceByStock(vol_price_df, as_of_ymd)
    if preprocessed_df.empty or ts_code not in preprocessed_df["ts_code"].astype(str).values:
        return out + [("预处理", False, "该标的未进入预处理结果（筛选日非该标的最近交易日或该标的不足290个交易日）")]
    breakout_codes = filterBreakoutHigh(preprocessed_df)
    merged = long_codes.merge(breakout_codes, on="ts_code", how="inner")
    if ts_code not in merged["ts_code"].astype(str).values:
        return out + [("突破前高", False, "未通过：按标的最近290个交易日，最近40日最高价未大于前250日最高价")]
    out.append(("突破前高", True, "通过"))
    pp_index = _buildPreprocessedIndex(preprocessed_df)
    lookback_days = PULSE_VOL_MA_WINDOW - 1
    win_df, _ = _windowAndLookbackFromPreprocessed(pp_index, ts_code, window_days, lookback_days)
    if win_df is None or len(win_df) < 5:
        return out + [("量价门槛与BOLL/RSI/成交额", False, "预处理窗口取数异常")]
    passed, reason = _checkCalcScoreConditions(win_df, amount_min_qianyuan=amount_min_qianyuan)
    if not passed:
        return out + [("量价门槛与BOLL/RSI/成交额", False, reason)]
    out.append(("量价门槛与BOLL/RSI/成交额", True, "通过"))
    return out + [("结果", True, "应进入全量结果，有排名")]


def main(trade_date_override: Optional[str] = None) -> int:
    """主流程：取最新交易日（或指定日期）、拉量价与均线、跑筛选、输出 CSV。trade_date_override 格式 YYYY-MM-DD。"""
    global _STEP_T0
    _STEP_T0 = time.time()
    t_main_start = _STEP_T0
    _logStep("="*50)
    _logStep(f"量价筛选启动  指定日期={trade_date_override or '最新'}")
    conn_t = pymysql.connect(**DB_TUSHARE, connect_timeout=10)
    conn_w = pymysql.connect(**DB_WIND, connect_timeout=10)
    try:
        window_days = max(WINDOW_LIST)
        cur = conn_t.cursor()
        need_dates = max(window_days + PRIOR_HIGH_DAYS + 5, NEED_DATES_EXTRA_FOR_250H)
        _logStep("[1/8] 获取交易日列表...")
        if trade_date_override:
            # 指定日期：取该日及之前的 need_dates 个交易日（含40日窗口+前250日）
            d = trade_date_override.replace("-", "")
            cur.execute(
                "SELECT DISTINCT trade_date FROM stk_factor_pro WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT %s",
                (d, need_dates),
            )
            raw_dates = [str(r[0]) for r in cur.fetchall()]
            if not raw_dates:
                print(f"指定日期 {trade_date_override} 无数据或之后无交易日")
                cur.close()
                return 1
            latest = raw_dates[0].replace("-", "")
            trade_date_str = f"{latest[:4]}-{latest[4:6]}-{latest[6:]}"
        else:
            cur.execute("SELECT DISTINCT trade_date FROM stk_factor_pro ORDER BY trade_date DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                print("未获取到最新交易日")
                cur.close()
                return 1
            latest = str(row[0]).replace("-", "")
            trade_date_str = f"{latest[:4]}-{latest[4:6]}-{latest[6:]}"
            cur.execute(
                "SELECT DISTINCT trade_date FROM stk_factor_pro ORDER BY trade_date DESC LIMIT %s",
                (need_dates,),
            )
            raw_dates = [str(r[0]) for r in cur.fetchall()]
        trade_dates = [d.replace("-", "") for d in raw_dates]
        cur.close()
        _logStep(f"  筛选日={trade_date_str}, 共获取 {len(trade_dates)} 个交易日")
        if len(trade_dates) < window_days:
            print("交易日不足")
            return 1
        _logStep("[2/8] 拉取量价数据...")
        vol_price_df = fetchVolPriceData(conn_t, trade_dates)
        _logStep(f"[3/8] 拉取均线数据...")
        ma_df = fetchMaData(conn_t, trade_dates)
        if vol_price_df is None or vol_price_df.empty or ma_df is None or ma_df.empty:
            print("量价或均线数据为空")
            return 1
        n_codes_vp = vol_price_df["ts_code"].nunique() if "ts_code" in vol_price_df.columns else "?"
        _logStep(f"  量价 {len(vol_price_df)} 行 / {n_codes_vp} 标的, 均线 {len(ma_df)} 行")
        _logStep("[4/8] 按标的预处理（筛选日有交易 & >=290日）...")
        preprocessed_df = preprocessVolPriceByStock(vol_price_df, trade_dates[0])
        if preprocessed_df.empty:
            print("预处理后无符合条件标的（需筛选日有交易且该标的至少290个交易日）")
            return 0
        n_pp = preprocessed_df["ts_code"].nunique() if "ts_code" in preprocessed_df.columns else len(preprocessed_df)
        _logStep(f"  预处理保留 {n_pp} 标的")
        _logStep(f"[5/8] 运行主筛选器（均线多头→突破前高→量价打分）...")
        result = runScreener(vol_price_df, preprocessed_df, ma_df, trade_dates, conn_t, conn_w, trade_date_str)
        if result.empty:
            _logStep(f"  {window_days}日窗口：未找到符合条件的股票")
            return 0
        _logStep(f"  筛选通过 {len(result)} 只")
        # 确保 result 有代码列（供后续 merge 与 fetchReturn 使用）；若缺 ts_code 则用首列兜底并统一为 ts_code
        result_code_col = "ts_code" if "ts_code" in result.columns else (result.columns[0] if len(result.columns) else None)
        if result_code_col and result_code_col != "ts_code":
            result = result.rename(columns={result_code_col: "ts_code"})
        _logStep("[6/8] 计算持有期收益 & RPS...")
        if "ts_code" in result.columns:
            ret_df = fetchReturnsByHoldingPeriods(conn_t, result["ts_code"].tolist(), latest, HOLD_PERIODS)
            if not ret_df.empty and "ts_code" in ret_df.columns:
                ret_cols = [c for c in ret_df.columns if c != "ts_code"]
                result = result.merge(ret_df, on="ts_code", how="left")
            else:
                ret_cols = []
        else:
            ret_cols = []
        # RPS 观察字段：筛选日的 rps_5/10/20/60/120/200/250（不参与打分）
        if "ts_code" in result.columns:
            rps_df = fetchRpsData(conn_t, result["ts_code"].tolist(), latest)
            if not rps_df.empty:
                result = result.merge(rps_df, on="ts_code", how="left")
        _logStep("  收益 & RPS 已合并")
        # 获取数据库最新日期（用于打印信息）
        cur = conn_t.cursor()
        cur.execute("SELECT MAX(trade_date) FROM stk_factor_pro")
        row = cur.fetchone()
        cur.close()
        db_latest_ymd = str(row[0]).replace("-", "") if row and row[0] else ""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        base_name = f"vol_price_{window_days}d_rule_{latest}"
        _safeCsvWrite(result, OUTPUT_DIR / f"{base_name}.csv", index=False, encoding="utf-8-sig")
        top = result.head(TOP_N).copy()
        # 近5日成交额(亿)、总市值(亿)、流通市值(亿)：从 vol_price_df 合并，amount 千元→亿 /1e5，total_mv/circ_mv 万元→亿 /10000
        last_5_dates = set(trade_dates[:5])
        vp_5 = vol_price_df[vol_price_df["trade_date"].astype(str).isin(last_5_dates)]
        if not vp_5.empty and "amount" in vp_5.columns:
            amount_5d = vp_5.groupby("ts_code")["amount"].agg(amount_mean="mean", amount_sum="sum").reset_index()
            amount_5d["amount_5d_avg_yi"] = (amount_5d["amount_mean"] / 1e5).round(4)
            top = top.merge(amount_5d[["ts_code", "amount_5d_avg_yi"]], on="ts_code", how="left")
        if "total_mv" in vol_price_df.columns and "circ_mv" in vol_price_df.columns:
            vp_latest = vol_price_df[vol_price_df["trade_date"].astype(str) == trade_dates[0]][["ts_code", "total_mv", "circ_mv"]].drop_duplicates("ts_code")
            vp_latest["total_mv_yi"] = (vp_latest["total_mv"].astype(float) / 10000).round(4)
            vp_latest["circ_mv_yi"] = (vp_latest["circ_mv"].astype(float) / 10000).round(4)
            # 全池也挂上市值，供使用说明「主仓+中位市值」快照
            result = result.merge(vp_latest[["ts_code", "total_mv_yi", "circ_mv_yi"]], on="ts_code", how="left")
            top = top.merge(vp_latest[["ts_code", "total_mv_yi", "circ_mv_yi"]], on="ts_code", how="left")
        _logStep("[7/8] 计算前1/3/5日排名（各重跑一次筛选）...")
        need_prev = window_days + PRIOR_HIGH_DAYS
        prev1_ymd = trade_dates[1] if len(trade_dates) > 1 else None
        prev3_ymd = trade_dates[3] if len(trade_dates) > 3 else None
        prev5_ymd = trade_dates[5] if len(trade_dates) > 5 else None
        rank_prev1, rank_prev3, rank_prev5 = {}, {}, {}
        def ymdToStr(ymd: str) -> str:
            return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}" if ymd else ""
        # 历史截面需排除筛选日之后的数据，否则 preprocessVolPriceByStock 的"最近交易日==筛选日"条件会失败
        def _filterVpByDates(dates_subset: list[str]) -> pd.DataFrame:
            return vol_price_df[vol_price_df["trade_date"].astype(str).isin(set(dates_subset))]
        def _filterMaByDates(dates_subset: list[str]) -> pd.DataFrame:
            return ma_df[ma_df["trade_date"].astype(str).isin(set(dates_subset))]
        if prev1_ymd and len(trade_dates) >= 1 + need_prev:
            _logStep(f"  前1日 ({ymdToStr(prev1_ymd)}) 筛选中...")
            td_prev1 = trade_dates[1:]
            vp_prev1, ma_prev1 = _filterVpByDates(td_prev1), _filterMaByDates(td_prev1)
            preprocessed_prev1 = preprocessVolPriceByStock(vp_prev1, prev1_ymd)
            result_prev1 = runScreener(vp_prev1, preprocessed_prev1, ma_prev1, td_prev1, conn_t, conn_w, ymdToStr(prev1_ymd))
            rank_prev1 = rankDictFromResult(result_prev1)
            _logStep(f"    前1日排名: {len(rank_prev1)} 只入选")
        else:
            _logStep(f"  前1日: 数据不足, 跳过")
        if prev3_ymd and len(trade_dates) >= 3 + need_prev:
            _logStep(f"  前3日 ({ymdToStr(prev3_ymd)}) 筛选中...")
            td_prev3 = trade_dates[3:]
            vp_prev3, ma_prev3 = _filterVpByDates(td_prev3), _filterMaByDates(td_prev3)
            preprocessed_prev3 = preprocessVolPriceByStock(vp_prev3, prev3_ymd)
            result_prev3 = runScreener(vp_prev3, preprocessed_prev3, ma_prev3, td_prev3, conn_t, conn_w, ymdToStr(prev3_ymd))
            rank_prev3 = rankDictFromResult(result_prev3)
            _logStep(f"    前3日排名: {len(rank_prev3)} 只入选")
        else:
            _logStep(f"  前3日: 数据不足, 跳过")
        if prev5_ymd and len(trade_dates) >= 5 + need_prev:
            _logStep(f"  前5日 ({ymdToStr(prev5_ymd)}) 筛选中...")
            td_prev5 = trade_dates[5:]
            vp_prev5, ma_prev5 = _filterVpByDates(td_prev5), _filterMaByDates(td_prev5)
            preprocessed_prev5 = preprocessVolPriceByStock(vp_prev5, prev5_ymd)
            result_prev5 = runScreener(vp_prev5, preprocessed_prev5, ma_prev5, td_prev5, conn_t, conn_w, ymdToStr(prev5_ymd))
            rank_prev5 = rankDictFromResult(result_prev5)
            _logStep(f"    前5日排名: {len(rank_prev5)} 只入选")
        else:
            _logStep(f"  前5日: 数据不足, 跳过")
        # 排名映射用「代码列」：merge 后可能只剩一侧的键列，用实际存在的列名兜底
        code_col = "ts_code" if "ts_code" in top.columns else (top.columns[0] if len(top.columns) else None)
        if code_col is None:
            raise RuntimeError("top 无可用列，无法生成排名")
        top["rank_today"] = range(1, len(top) + 1)
        top["rank_prev1"] = top[code_col].astype(str).map(rank_prev1).astype("Int64")
        top["rank_prev3"] = top[code_col].astype(str).map(rank_prev3).astype("Int64")
        top["rank_prev5"] = top[code_col].astype(str).map(rank_prev5).astype("Int64")
        top["prev1_date"] = f"{prev1_ymd[:4]}-{prev1_ymd[4:6]}-{prev1_ymd[6:]}" if prev1_ymd else ""
        top["prev3_date"] = f"{prev3_ymd[:4]}-{prev3_ymd[4:6]}-{prev3_ymd[6:]}" if prev3_ymd else ""
        top["prev5_date"] = f"{prev5_ymd[:4]}-{prev5_ymd[4:6]}-{prev5_ymd[6:]}" if prev5_ymd else ""

        def formatChg(r_today: int, r_prev: float) -> str:
            if pd.isna(r_prev):
                return "新"
            delta = r_today - int(r_prev)
            if delta < 0:
                return f"升{abs(delta)}"
            if delta > 0:
                return f"降{delta}"
            return "平"

        top["chg_prev1"] = [formatChg(top["rank_today"].iloc[i], top["rank_prev1"].iloc[i]) for i in range(len(top))]
        top["chg_prev3"] = [formatChg(top["rank_today"].iloc[i], top["rank_prev3"].iloc[i]) for i in range(len(top))]
        top["chg_prev5"] = [formatChg(top["rank_today"].iloc[i], top["rank_prev5"].iloc[i]) for i in range(len(top))]
        # 列顺序：代码/名称/行业/综合评分/涨跌幅 后紧跟 今日排名、前一(三/五)日排名及变化；去重列名避免 merge 产生重复列后选择丢失
        rank_cols = ["rank_today", "prev1_date", "rank_prev1", "chg_prev1", "prev3_date", "rank_prev3", "chg_prev3", "prev5_date", "rank_prev5", "chg_prev5"]
        others = [c for c in top.columns if c not in rank_cols]
        idx_score = others.index("score") if "score" in others else 0
        new_order = list(dict.fromkeys(
            [c for c in others[: idx_score + 1]] + rank_cols + [c for c in others[idx_score + 1 :] if c not in rank_cols]
        ))
        top = top[new_order]

        # 主表 CSV 全部使用中文列名
        HEADER_CN = {
            "ts_code": "股票代码", "sec_name": "股票名称", "industry_name": "行业", "industry_sw_2021_2": "行业代码(申万2021二级)",
            "score_partial": "部分总分(不含趋势稳健)",
            "score": "综合评分", "amount_5d_avg_yi": "近5日日均成交额(亿)",
            "total_mv_yi": "总市值(亿)", "circ_mv_yi": "流通市值(亿)",
            "ret_5d": "持有5日收益%", "ret_10d": "持有10日收益%", "ret_15d": "持有15日收益%",
            "ret_to_latest": "入选至今涨跌幅%",
            "rank_today": "今日排名", "prev1_date": "前一交易日", "rank_prev1": "前一交易日排名", "chg_prev1": "排名变化_前1日",
            "prev3_date": "前三交易日", "rank_prev3": "前三交易日排名", "chg_prev3": "排名变化_前3日",
            "prev5_date": "前五交易日", "rank_prev5": "前五交易日排名", "chg_prev5": "排名变化_前5日",
            "score_lv": "量价质量得分(换手相关)", "score_up_vol": "涨日放量得分(观察)", "score_down_vol": "跌日缩量得分", "score_corr_lv": "换手相关得分",
            "score_vol_div": "量能不对称度得分", "score_r2": "R²得分", "score_trend_robust": "趋势稳健得分",
            "score_vol": "波动率得分(沪深300-IVOL)", "score_drawdown": "起涨区得分", "score_sync": "量价同步得分", "score_lead": "量能领先得分(EWMA)",
            "score_pulse_adj": "脉冲放量缩量整理(观察)", "pulse_adj_valid_count": "脉冲有效次数(观察)",
            "str_cv": "换手率稳定性CV(观察)", "dir_mom": "方向动量[-1,1](观察)",
            "slope_consist_ratio": "斜率一致性(5/10/25)", "se_residual": "残差标准差",
            "up_vol_ratio": "涨日放量比例%", "down_vol_ratio": "跌日缩量比例%", "vol_asym": "量能不对称比", "corr": "量比相关(硬筛)",
            "period_return_pct": "窗口涨幅%", "drawdown": "回落幅%",
            "is_first_new_high": "首次创新高(1是0否)", "ret_from_250h_pct": "筛选日相对250日新高涨幅%",
            "boll_pos": "布林带位置[0,1](观察)", "rsi12": "RSI12(观察)",
            "amount_5d_avg_yi": "近5日日均成交额亿(观察)",
            "vol_pct": "总波动EWMA%(硬筛观察)", "vol_idio_pct": "沪深300-IVOL%(打分)",
            "breakout_strength": "突破前高强度(观察)", "ma20_ma60_ratio": "均线多头强度MA20/MA60-1(观察)",
            "rps_5": "RPS_5", "rps_10": "RPS_10", "rps_20": "RPS_20", "rps_60": "RPS_60",
            "rps_120": "RPS_120", "rps_200": "RPS_200", "rps_250": "RPS_250",
        }
        _logStep(f"[8/8] 输出 CSV（全量 + Top{TOP_N} + 得分明细）...")
        top_out = top.rename(columns={k: v for k, v in HEADER_CN.items() if k in top.columns})
        _safeCsvWrite(top_out, OUTPUT_DIR / f"{base_name}_top{TOP_N}.csv", index=False, encoding="utf-8-sig")
        # 各打分维度得分：单独写出并打印（满分见表头括号）
        score_cols = ["score_lv", "score_up_vol", "score_down_vol", "score_corr_lv", "score_vol_div",
                      "score_r2", "score_vol", "score_drawdown", "score_sync", "score_lead",
                      "score_trend_robust", "score_pulse_adj", "score"]
        score_cols_cn = ["量价质量(10)", "涨日放量(观察)", "跌日缩量(8)", "换手相关(7)", "量能不对称(观察)",
                         "R2(8)", "波动率IVOL(22.5)", "起涨区(22.5)", "量价同步(15)", "量能领先(15)",
                         "趋势稳健(7)", "脉冲放量缩量(观察)", "综合(100)"]
        cols_scores = [c for c in score_cols if c in top.columns]
        extra_scores = ["is_first_new_high", "ret_from_250h_pct"]
        cols_extra = [c for c in extra_scores if c in top.columns]
        code_col = "ts_code" if "ts_code" in top.columns else (top.columns[0] if len(top.columns) else None)
        name_col = "sec_name" if "sec_name" in top.columns else (top.columns[1] if len(top.columns) > 1 else None)
        industry_col = "industry_name" if "industry_name" in top.columns else None
        scores_path = None
        if cols_scores and code_col is not None:
            rank_1based = list(range(1, len(top) + 1))
            lead = [code_col] + ([name_col] if name_col and name_col != code_col else []) + ([industry_col] if industry_col else [])
            scores_df = top[[c for c in lead + cols_scores + cols_extra if c in top.columns]].copy()
            scores_df.insert(0, "排名", rank_1based)
            rename_scores = {code_col: "股票代码"}
            if name_col and name_col in scores_df.columns:
                rename_scores[name_col] = "股票名称"
            if industry_col and industry_col in scores_df.columns:
                rename_scores[industry_col] = "行业"
            for k, v in [("is_first_new_high", "首次创新高(1是0否)"), ("ret_from_250h_pct", "筛选日相对250日新高涨幅%")]:
                if k in scores_df.columns:
                    rename_scores[k] = v
            scores_df = scores_df.rename(columns=rename_scores)
            cn_map = dict(zip(score_cols, score_cols_cn))
            scores_df = scores_df.rename(columns={k: v for k, v in cn_map.items() if k in scores_df.columns})
            scores_path = OUTPUT_DIR / f"{base_name}_top{TOP_N}_scores.csv"
            _safeCsvWrite(scores_df, scores_path, index=False, encoding="utf-8-sig")
        db_latest_str = f"{db_latest_ymd[:4]}-{db_latest_ymd[4:6]}-{db_latest_ymd[6:]}" if db_latest_ymd else ""
        total_sec = time.time() - t_main_start
        _logStep(f"全部完成! 总耗时 {total_sec:.1f}s")
        print(f"入选日: {trade_date_str} | 库最新日: {db_latest_str} | {window_days}日窗口筛选出: {len(result)} 只 | Top{TOP_N} 已写入 {OUTPUT_DIR}")
        disp_cn = [v for k, v in HEADER_CN.items() if k in top.columns]
        def _safe_print(s: str) -> None:
            try:
                print(s)
            except UnicodeEncodeError:
                enc = getattr(sys.stdout, "encoding", None) or "utf-8"
                print(s.encode(enc, errors="replace").decode(enc))
        _safe_print(top_out[[c for c in disp_cn if c in top_out.columns]].to_string(index=False))
        if scores_path is not None:
            _safe_print("\n【各维度得分】(满分见表头括号，已写入 %s)" % scores_path)
            _safe_print(scores_df.to_string(index=False))
        # 策略使用说明：控制台 + 同目录落盘（与 CSV 一并作为筛选报告）
        usagePath = OUTPUT_DIR / f"{base_name}_策略使用说明.txt"
        emitStrategyUsageGuide(resultDf=result, topDf=top, outPath=usagePath)
        return 0
    finally:
        conn_t.close()
        conn_w.close()


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--diagnose":
        ts_code_diag = sys.argv[2].strip()
        date_diag = sys.argv[3].strip()
        try:
            conn_t = pymysql.connect(**DB_TUSHARE, connect_timeout=10)
            conn_w = pymysql.connect(**DB_WIND, connect_timeout=10)
            try:
                steps = diagnoseExclusion(conn_t, conn_w, ts_code_diag, date_diag)
                print(f"标的: {ts_code_diag}  截面日: {date_diag}")
                print("-" * 60)
                for name, passed, msg in steps:
                    status = "通过" if passed else "未通过"
                    print(f"  {name}: {status}  {msg}")
            finally:
                conn_t.close()
                conn_w.close()
            sys.exit(0)
        except Exception as e:
            print(f"诊断异常: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
    date_arg = None
    if len(sys.argv) >= 2:
        if sys.argv[1] == "--date" and len(sys.argv) >= 3:
            date_arg = sys.argv[2]
        elif sys.argv[1] not in ("-h", "--help"):
            date_arg = sys.argv[1]
    try:
        sys.exit(main(trade_date_override=date_arg))
    except KeyboardInterrupt:
        print("用户中断")
        sys.exit(130)
    except Exception as e:
        print(f"异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

# 代码更新时间：2026-08-04 18:46 名称/申万行业由停更的 stockcodes 改取 stock_basic + index_member_all_sw
# score_vol 锁定：沪深300 IVOL 打分 + 总波动 EWMA≤15% 硬筛；VOL_IVOL_ZERO_SCORE=11.2
# score_lead 锁定：近端加权 corr(turn_t,ret_t+1)，LEAD_EWMA_HALFLIFE=7
# score_lv：打分相关改 turnover；硬筛仍 volume_ratio
# 权重包：sync15 / r2·8 / trend7；脉冲退出总分，5 分均分给 vol/dd → 各 22.5
# 每次筛选报告末尾打印/落盘 STRATEGY_USAGE_LINES（参与门槛≥62.5、市值偏好等）
# 均线硬筛：MA(close>ma20>ma120 或 close>ma20>ma60) 且 EMA(同理)；不再要求 20>60>120 全链
# 代码更新时间：2026-08-04 19:02 名称/申万二级由停更的 stockcodes 改取 stock_basic + index_member_all_sw
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
