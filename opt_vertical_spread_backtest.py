#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETF 期权垂直价差回测
====================
用本地 stock_tushare 历史数据回测四个 ETF 期权的垂直价差，回答两个实盘问题：
  问题一：建仓初始净借记成本落在什么区间、价差扩大到多少倍平仓性价比最高？
  问题二：当月/下月/当季/下季合约怎么选？

策略：
  - 牛市认购价差(看多)：买低行权价 Call + 卖高行权价 Call（净借记）
  - 熊市认沽价差(看空)：买高行权价 Put + 卖低行权价 Put（净借记）

数据：opt_basic（合约）、opt_daily（日线 close/settle/vol/oi）、etf_daily（标的价/ma20）。
口径：成交价用 close + 固定滑点；无 IV/Greeks，用 moneyness 选腿。

运行：
  python opt_vertical_spread_backtest.py                 # 四标的, A+B, close
  python opt_vertical_spread_backtest.py --codes 510300.SH --mode A
  python opt_vertical_spread_backtest.py --price settle  # 敏感性
  python -m pytest tests/test_opt_vertical_spread.py -v  # 纯函数单测

设计/计划见 docs/plans/2026-06-14-opt-vertical-spread-backtest*.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import signal
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from urllib.parse import quote_plus

LOG_FILE = "opt_vertical_spread_backtest.log"


class WindowsSafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Windows 下 rename 可能失败：重试后仍失败则跳过本轮轮转，继续写原文件。"""

    def doRollover(self) -> None:  # noqa: N802
        for _ in range(3):
            try:
                super().doRollover()
                return
            except PermissionError:
                time.sleep(0.3)
        if self.stream:
            try:
                self.stream.close()
            except Exception:
                pass
        self.stream = self._open()


def setupLogger() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = WindowsSafeTimedRotatingFileHandler(
        LOG_FILE, when="D", interval=1, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, ch], force=True)


setupLogger()
logger = logging.getLogger(__name__)

running = True


def handleSignal(signum: int, frame: Any) -> None:
    global running
    running = False
    logger.warning("收到信号 %s，准备安全退出…", signum)


signal.signal(signal.SIGINT, handleSignal)
signal.signal(signal.SIGTERM, handleSignal)

# ===== 配置区 =====
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "123456"),
    "database": os.getenv("DB_NAME", "stock_tushare"),
    "charset": "utf8mb4",
}

TARGETS = ["510300.SH", "510500.SH", "588000.SH", "159915.SZ"]
PER_UNIT = 10000          # ETF 期权合约单位
TICK = 0.0001             # 最小变动价位
SLIP_TICKS = 1            # 每腿每边滑点档数
FEE_PER_LEG = 2.0         # 每腿每张手续费(元)
MIN_OI = 50               # 建仓流动性阈值(持仓量)
WIDTHS = (1, 2, 3)        # 价差宽度(档)
TP_MULTS = (1.3, 1.5, 1.8, 2.0, 2.5)  # 止盈倍数(价差现值/建仓净借记)
SL_MULT: float | None = 0.5           # 止损倍数; None 关闭
EXIT_BEFORE_EXPIRE_DAYS = 1           # 到期前 N 个交易日强平
SAMPLE_FREQ = "W-MON"     # 方案A采样频率
EXPIRY_KINDS = ("near", "next", "quarter", "next_quarter")
QUARTER_MONTHS = frozenset((3, 6, 9, 12))

_engine: Engine | None = None


def makeEngine() -> Engine:
    global _engine
    if _engine is None:
        dsn = (
            f"mysql+pymysql://{quote_plus(DB_CONFIG['user'])}:{quote_plus(DB_CONFIG['password'])}"
            f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}?charset={DB_CONFIG['charset']}"
        )
        _engine = create_engine(dsn, pool_recycle=3600, pool_pre_ping=True)
    return _engine


# ============================================================
# 数据加载
# ============================================================
def loadOptMeta(engine: Engine, target: str) -> pd.DataFrame:
    """该标的全部期权合约元信息。"""
    oc = "OP" + target
    sql = text(
        "SELECT ts_code, call_put, exercise_price, maturity_date, list_date, delist_date "
        "FROM opt_basic WHERE opt_code = :oc"
    )
    df = pd.read_sql(sql, engine, params={"oc": oc})
    for c in ("maturity_date", "list_date", "delist_date"):
        df[c] = pd.to_datetime(df[c], errors="coerce")
    df["exercise_price"] = pd.to_numeric(df["exercise_price"], errors="coerce")
    df["call_put"] = df["call_put"].astype(str).str.strip().str.upper()
    return df


def loadOptDaily(engine: Engine, target: str, price_col: str) -> dict[str, dict[str, pd.Series]]:
    """该标的全部期权日线，返回 {ts_code: {'px': Series, 'oi': Series, 'vol': Series}}（按日期索引）。"""
    oc = "OP" + target
    sql = text(
        f"SELECT d.ts_code, d.trade_date, d.{price_col} AS px, d.oi, d.vol "
        "FROM opt_daily d JOIN opt_basic b ON d.ts_code = b.ts_code "
        "WHERE b.opt_code = :oc"
    )
    df = pd.read_sql(sql, engine, params={"oc": oc})
    if df.empty:
        return {}
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for c in ("px", "oi", "vol"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values(["ts_code", "trade_date"])
    out: dict[str, dict[str, pd.Series]] = {}
    for ts, g in df.groupby("ts_code", sort=False):
        gi = g.set_index("trade_date")
        out[str(ts)] = {"px": gi["px"], "oi": gi["oi"], "vol": gi["vol"]}
    return out


def loadEtfDaily(engine: Engine, target: str) -> pd.DataFrame:
    """标的 ETF 日线（实际 close 用于 moneyness/方向，ma20 用于信号）。"""
    sql = text(
        "SELECT trade_date, close, close_qfq, ma20 FROM etf_daily WHERE ts_code = :c ORDER BY trade_date"
    )
    df = pd.read_sql(sql, engine, params={"c": target})
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for c in ("close", "close_qfq", "ma20"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["close"]).reset_index(drop=True)


# ============================================================
# 纯函数：到期月分类 / 选腿 / 单笔回测
# ============================================================
def classifyExpiries(t: dt.date, maturities: list) -> dict[str, Any]:
    """
    给定交易日 t 与该日在交易合约的到期日列表，分类为 当月/下月/当季/下季。
    - near=升序第0, next=第1
    - quarter=季月(3/6/9/12)升序第0, next_quarter=第1
    返回 dict，缺失为 None。允许 near 与 quarter 重叠。
    """
    def _d(x: Any) -> dt.date:
        if isinstance(x, dt.datetime):
            return x.date()
        if isinstance(x, dt.date):
            return x
        return pd.Timestamp(x).date()

    mats = sorted({_d(m) for m in maturities if m is not None and _d(m) >= t})
    quarters = [m for m in mats if m.month in QUARTER_MONTHS]
    return {
        "near": mats[0] if len(mats) >= 1 else None,
        "next": mats[1] if len(mats) >= 2 else None,
        "quarter": quarters[0] if len(quarters) >= 1 else None,
        "next_quarter": quarters[1] if len(quarters) >= 2 else None,
    }


def pickLegs(chain: pd.DataFrame, spot: float, side: str, width: int) -> dict[str, Any] | None:
    """
    从单一到期日、单一 call_put 的链中选两腿。
    chain 列: exercise_price, ts_code, px, oi, vol（当日值）。
    side: 'bull'(认购牛市) / 'bear'(认沽熊市)。买腿取近平值，卖腿移 width 档。
    返回 {buy_ts, sell_ts, buy_k, sell_k, buy_px, sell_px} 或 None（不可成交）。
    """
    if chain is None or chain.empty:
        return None
    ch = chain.dropna(subset=["exercise_price", "px"]).sort_values("exercise_price").reset_index(drop=True)
    if ch.empty:
        return None
    i = int((ch["exercise_price"] - spot).abs().idxmin())
    j = i + width if side == "bull" else i - width
    if j < 0 or j >= len(ch):
        return None
    buy = ch.iloc[i]
    sell = ch.iloc[j]
    for leg in (buy, sell):
        if pd.isna(leg["px"]) or pd.isna(leg["oi"]) or float(leg["oi"]) < MIN_OI or float(leg.get("vol", 0) or 0) <= 0:
            return None
    return {
        "buy_ts": str(buy["ts_code"]), "sell_ts": str(sell["ts_code"]),
        "buy_k": float(buy["exercise_price"]), "sell_k": float(sell["exercise_price"]),
        "buy_px": float(buy["px"]), "sell_px": float(sell["px"]),
    }


def roundtripCost() -> float:
    """建仓+平仓共 4 腿的滑点与手续费(元/张)。"""
    slip = 4 * SLIP_TICKS * TICK * PER_UNIT
    fee = 4 * FEE_PER_LEG
    return slip + fee


def simulateTrade(
    entryDebit: float,
    spreadSeries: pd.Series,
    kTp: float,
    kSl: float | None,
    exitCutoff: pd.Timestamp,
) -> dict[str, Any] | None:
    """
    单笔回测。spreadSeries: 建仓日之后逐日净价差(买腿px-卖腿px)，按日期索引升序。
    entryDebit: 建仓净借记(>0)。kTp/kSl: 止盈/止损倍数。exitCutoff: 最晚强平日。
    返回单笔结果 dict，或 None（无效）。
    """
    if entryDebit is None or entryDebit <= 0 or spreadSeries is None or spreadSeries.empty:
        return None
    s = spreadSeries[spreadSeries.index <= exitCutoff].dropna()
    if s.empty:
        return None
    tpLevel = entryDebit * kTp
    slLevel = entryDebit * kSl if kSl is not None else None
    exitSpread = float(s.iloc[-1])
    exitReason = "expire"
    exitDate = s.index[-1]
    for d, v in s.items():
        v = float(v)
        if v >= tpLevel:
            exitSpread, exitReason, exitDate = tpLevel, "tp", d
            break
        if slLevel is not None and v <= slLevel:
            exitSpread, exitReason, exitDate = v, "sl", d
            break
    cost = roundtripCost()
    pnl = (exitSpread - entryDebit) * PER_UNIT - cost
    ret = pnl / (entryDebit * PER_UNIT)
    holdDays = max(1, (pd.Timestamp(exitDate) - pd.Timestamp(s.index[0])).days)
    return {
        "entry_debit": entryDebit, "exit_spread": exitSpread, "exit_reason": exitReason,
        "pnl": pnl, "ret": ret, "hold_days": holdDays, "win": pnl > 0,
    }


# ============================================================
# 链构造辅助
# ============================================================
def buildChain(
    meta: pd.DataFrame, daily: dict[str, dict[str, pd.Series]],
    t: pd.Timestamp, cp: str, maturity: dt.date,
) -> pd.DataFrame:
    """当日 t、指定 call_put、指定到期日的在交易合约链（附当日 px/oi/vol）。"""
    mat_ts = pd.Timestamp(maturity)
    sub = meta[
        (meta["call_put"] == cp)
        & (meta["maturity_date"] == mat_ts)
        & (meta["list_date"] <= t)
        & (meta["delist_date"] >= t)
    ]
    rows = []
    for _, r in sub.iterrows():
        ts = str(r["ts_code"])
        d = daily.get(ts)
        if d is None:
            continue
        px = d["px"].get(t)
        if px is None or pd.isna(px):
            continue
        rows.append({
            "ts_code": ts, "exercise_price": float(r["exercise_price"]),
            "px": float(px), "oi": d["oi"].get(t), "vol": d["vol"].get(t),
        })
    return pd.DataFrame(rows)


def spreadSeriesFor(
    daily: dict[str, dict[str, pd.Series]], buy_ts: str, sell_ts: str,
    start: pd.Timestamp, end: pd.Timestamp,
) -> pd.Series:
    """买腿-卖腿 的逐日净价差序列，区间 (start, end]。"""
    b = daily[buy_ts]["px"]
    s = daily[sell_ts]["px"]
    sp = (b - s).dropna()
    return sp[(sp.index > start) & (sp.index <= end)]


def tradingExitCutoff(etfDates: np.ndarray, maturity: dt.date, daysBack: int) -> pd.Timestamp:
    """到期日前 daysBack 个交易日（以 etf 交易日历近似）。"""
    mat_ts = np.datetime64(pd.Timestamp(maturity))
    idx = int(np.searchsorted(etfDates, mat_ts, side="right")) - 1
    idx = max(0, idx - daysBack)
    return pd.Timestamp(etfDates[idx])


# ============================================================
# 方案 A：条件回测
# ============================================================
def runConditionalBacktest(
    target: str, meta: pd.DataFrame, daily: dict[str, dict[str, pd.Series]], etf: pd.DataFrame,
    freq: str = SAMPLE_FREQ,
) -> list[dict[str, Any]]:
    if not daily or meta.empty or etf.empty:
        return []
    etf = etf.set_index("trade_date")
    etfDates = np.array(etf.index.values, dtype="datetime64[ns]")
    sampleDays = pd.date_range(etf.index.min(), etf.index.max(), freq=freq)
    matAll = sorted(meta["maturity_date"].dropna().unique())
    results: list[dict[str, Any]] = []
    nrec = 0
    for raw_t in sampleDays:
        if not running:
            break
        # 对齐到 <= raw_t 的最近交易日
        pos = int(np.searchsorted(etfDates, np.datetime64(raw_t), side="right")) - 1
        if pos < 0:
            continue
        t = pd.Timestamp(etfDates[pos])
        spot = etf["close"].get(t)
        if spot is None or pd.isna(spot):
            continue
        spot = float(spot)
        tradable = [m for m in matAll if pd.Timestamp(m) >= t]
        kinds = classifyExpiries(t.date(), tradable)
        for direction, cp, side in (("bull", "C", "bull"), ("bear", "P", "bear")):
            for kind in EXPIRY_KINDS:
                mat = kinds.get(kind)
                if mat is None:
                    continue
                chain = buildChain(meta, daily, t, cp, mat)
                exitCut = tradingExitCutoff(etfDates, mat, EXIT_BEFORE_EXPIRE_DAYS)
                if exitCut <= t:
                    continue
                for w in WIDTHS:
                    legs = pickLegs(chain, spot, side, w)
                    if legs is None:
                        continue
                    if side == "bull":
                        entryDebit = legs["buy_px"] - legs["sell_px"]
                        kspread = legs["sell_k"] - legs["buy_k"]
                    else:
                        entryDebit = legs["buy_px"] - legs["sell_px"]
                        kspread = legs["buy_k"] - legs["sell_k"]
                    if entryDebit <= 0 or kspread <= 0:
                        continue
                    sp = spreadSeriesFor(daily, legs["buy_ts"], legs["sell_ts"], t, exitCut)
                    if sp.empty:
                        continue
                    # 方向是否正确：平仓基准日标的相对建仓日
                    exitSpot = etf["close"].get(pd.Timestamp(sp.index[-1]))
                    dirCorrect = None
                    if exitSpot is not None and not pd.isna(exitSpot):
                        dirCorrect = (float(exitSpot) > spot) if direction == "bull" else (float(exitSpot) < spot)
                    costRel = entryDebit / kspread  # 净借记 ÷ 价差宽度(行权价差)
                    for kTp in TP_MULTS:
                        r = simulateTrade(entryDebit, sp, kTp, SL_MULT, exitCut)
                        if r is None:
                            continue
                        results.append({
                            "code": target, "direction": direction, "expiry_kind": kind,
                            "width": w, "tp_mult": kTp, "cost_rel": costRel,
                            "entry_debit": entryDebit, "k_spread": kspread,
                            "pnl": r["pnl"], "ret": r["ret"], "hold_days": r["hold_days"],
                            "win": r["win"], "exit_reason": r["exit_reason"],
                            "dir_correct": dirCorrect, "entry_date": t.date(),
                        })
                        nrec += 1
    logger.info("[%s] 条件回测生成 %s 条结果", target, nrec)
    return results


# ============================================================
# 方案 B：均线信号回测
# ============================================================
def runSignalBacktest(
    target: str, meta: pd.DataFrame, daily: dict[str, dict[str, pd.Series]], etf: pd.DataFrame,
    kind: str = "next", width: int = 1, kTp: float = 1.5,
) -> list[dict[str, Any]]:
    if not daily or meta.empty or etf.empty:
        return []
    e = etf.dropna(subset=["close", "ma20"]).reset_index(drop=True)
    if len(e) < 2:
        return []
    e = e.set_index("trade_date")
    etfDates = np.array(e.index.values, dtype="datetime64[ns]")
    matAll = sorted(meta["maturity_date"].dropna().unique())
    closes = e["close"]
    ma = e["ma20"]
    prevAbove = None
    inPos = False
    posExit: pd.Timestamp | None = None
    results: list[dict[str, Any]] = []
    for t in e.index:
        if not running:
            break
        if inPos and posExit is not None and t < posExit:
            continue
        inPos = False
        above = bool(closes.get(t) > ma.get(t))
        if prevAbove is None:
            prevAbove = above
            continue
        sig = None
        if above and not prevAbove:
            sig = ("bull", "C", "bull")
        elif (not above) and prevAbove:
            sig = ("bear", "P", "bear")
        prevAbove = above
        if sig is None:
            continue
        direction, cp, side = sig
        spot = float(closes.get(t))
        tradable = [m for m in matAll if pd.Timestamp(m) >= t]
        mat = classifyExpiries(t.date(), tradable).get(kind)
        if mat is None:
            continue
        chain = buildChain(meta, daily, t, cp, mat)
        exitCut = tradingExitCutoff(etfDates, mat, EXIT_BEFORE_EXPIRE_DAYS)
        if exitCut <= t:
            continue
        legs = pickLegs(chain, spot, side, width)
        if legs is None:
            continue
        entryDebit = legs["buy_px"] - legs["sell_px"]
        if entryDebit <= 0:
            continue
        sp = spreadSeriesFor(daily, legs["buy_ts"], legs["sell_ts"], t, exitCut)
        r = simulateTrade(entryDebit, sp, kTp, SL_MULT, exitCut)
        if r is None:
            continue
        inPos = True
        posExit = pd.Timestamp(sp.index[-1])
        results.append({
            "code": target, "direction": direction, "entry_date": t.date(),
            "pnl": r["pnl"], "ret": r["ret"], "hold_days": r["hold_days"],
            "win": r["win"], "exit_reason": r["exit_reason"],
        })
    logger.info("[%s] 信号回测生成 %s 笔", target, len(results))
    return results


# ============================================================
# 统计与输出
# ============================================================
def _aggGroup(g: pd.DataFrame) -> pd.Series:
    n = len(g)
    wins = g[g["pnl"] > 0]["ret"]
    losses = g[g["pnl"] <= 0]["ret"]
    avgWin = wins.mean() if len(wins) else 0.0
    avgLoss = losses.mean() if len(losses) else 0.0
    pf = (wins.sum() / abs(losses.sum())) if losses.sum() != 0 else np.inf
    expRet = g["ret"].mean()
    avgHold = g["hold_days"].mean()
    return pd.Series({
        "n": n, "win_rate": (g["pnl"] > 0).mean(),
        "avg_ret": expRet, "avg_win": avgWin, "avg_loss": avgLoss,
        "profit_factor": pf, "avg_hold_days": avgHold,
        "ret_per_day": expRet / avgHold if avgHold else 0.0,
        "tp_hit_rate": (g["exit_reason"] == "tp").mean(),
    })


def summarize(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    return df.groupby(by, observed=True).apply(_aggGroup, include_groups=False).reset_index()


def concludeAndExport(condDf: pd.DataFrame, sigDf: pd.DataFrame, outDir: str, price_col: str) -> None:
    os.makedirs(outDir, exist_ok=True)
    tslabel = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    if not condDf.empty:
        gridAll = summarize(condDf, ["code", "direction", "expiry_kind", "width", "tp_mult"])
        gridAll.to_csv(os.path.join(outDir, f"opt_spread_grid_{price_col}_{tslabel}.csv"), index=False, encoding="utf-8-sig")
        cond = condDf[condDf["dir_correct"] == True]  # noqa: E712
        gridCond = summarize(cond, ["direction", "expiry_kind", "width", "tp_mult"])
        gridCond.to_csv(os.path.join(outDir, f"opt_spread_conditional_{price_col}_{tslabel}.csv"), index=False, encoding="utf-8-sig")
        # 三段结论表单独落 CSV（方向正确子样本），便于审计/无乱码呈现
        _bucketCostRel(cond).to_csv(os.path.join(outDir, f"concl_cost_{price_col}_{tslabel}.csv"), index=False, encoding="utf-8-sig")
        summarize(cond, ["direction", "tp_mult"]).to_csv(os.path.join(outDir, f"concl_tp_{price_col}_{tslabel}.csv"), index=False, encoding="utf-8-sig")
        summarize(cond, ["direction", "expiry_kind"]).to_csv(os.path.join(outDir, f"concl_expiry_{price_col}_{tslabel}.csv"), index=False, encoding="utf-8-sig")
        _printConclusions(condDf, cond)
    if not sigDf.empty:
        sigSum = summarize(sigDf, ["code", "direction"])
        sigSum.to_csv(os.path.join(outDir, f"opt_spread_signal_{price_col}_{tslabel}.csv"), index=False, encoding="utf-8-sig")
        logger.info("[信号回测B汇总]\n%s", sigSum.to_string(index=False))


def _bucketCostRel(df: pd.DataFrame) -> pd.DataFrame:
    bins = [0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 1.01]
    labels = ["<0.2", "0.2-0.3", "0.3-0.4", "0.4-0.5", "0.5-0.6", "0.6-0.7", ">0.7"]
    d = df.copy()
    d["cost_bucket"] = pd.cut(d["cost_rel"], bins=bins, labels=labels)
    return summarize(d, ["direction", "cost_bucket"])


def _printConclusions(condDf: pd.DataFrame, cond: pd.DataFrame) -> None:
    logger.info("=" * 70)
    logger.info("结论一 | 初始成本区间(净借记/行权价差) — 方向正确子样本, ret/持有天数")
    cb = _bucketCostRel(cond)
    logger.info("\n%s", cb.to_string(index=False))
    logger.info("=" * 70)
    logger.info("结论二 | 止盈倍数 — 方向正确子样本, 按 direction×tp_mult")
    tp = summarize(cond, ["direction", "tp_mult"])
    logger.info("\n%s", tp.to_string(index=False))
    logger.info("=" * 70)
    logger.info("结论三 | 到期月选择 — 方向正确子样本, 按 direction×expiry_kind")
    ek = summarize(cond, ["direction", "expiry_kind"])
    logger.info("\n%s", ek.to_string(index=False))
    logger.info("=" * 70)


# ============================================================
# 主流程
# ============================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="ETF 期权垂直价差回测")
    parser.add_argument("--codes", default="", help="逗号分隔标的, 默认全部四个")
    parser.add_argument("--mode", choices=("A", "B", "both"), default="both")
    parser.add_argument("--price", choices=("close", "settle"), default="close")
    parser.add_argument("--freq", default=SAMPLE_FREQ, help="方案A采样频率(默认 W-MON)")
    parser.add_argument("--out", default="out", help="输出目录")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or TARGETS
    engine = makeEngine()

    allCond: list[dict[str, Any]] = []
    allSig: list[dict[str, Any]] = []
    for target in codes:
        if not running:
            break
        logger.info("===== 加载 %s =====", target)
        meta = loadOptMeta(engine, target)
        daily = loadOptDaily(engine, target, args.price)
        etf = loadEtfDaily(engine, target)
        logger.info("[%s] 合约 %s, 有日线合约 %s, etf %s 行(%s ~ %s)",
                    target, len(meta), len(daily), len(etf),
                    etf["trade_date"].min().date() if not etf.empty else "-",
                    etf["trade_date"].max().date() if not etf.empty else "-")
        if args.mode in ("A", "both"):
            allCond.extend(runConditionalBacktest(target, meta, daily, etf, freq=args.freq))
        if args.mode in ("B", "both"):
            allSig.extend(runSignalBacktest(target, meta, daily, etf))

    condDf = pd.DataFrame(allCond)
    sigDf = pd.DataFrame(allSig)
    concludeAndExport(condDf, sigDf, args.out, args.price)
    logger.info("全部完成。条件回测 %s 条, 信号回测 %s 笔。", len(condDf), len(sigDf))


if __name__ == "__main__":
    main()

# 代码更新时间：2026-06-14 09:50
