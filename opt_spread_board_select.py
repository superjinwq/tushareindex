"""
opt-t-board 垂直价差选腿：枚举候选组合，按回测甜点(成本占比)、胜率、盈亏比综合打分。
统计表由 buildBoardStats() 从条件回测(方向正确)生成，落 data/opt_spread_board_stats.json。
分标的配置 SYMBOL_PROFILES 对齐 RESULTS.md 弹性排序与各品种差异。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from opt_vertical_spread_backtest import (
    COST_BINS,
    COST_LABELS,
    TARGETS,
    _addCostBucket,
    _aggGroup,
    loadEtfDaily,
    loadOptDaily,
    loadOptMeta,
    makeEngine,
    runConditionalBacktest,
)

STATS_PATH = Path(__file__).resolve().parent / "data" / "opt_spread_board_stats.json"
# 统计覆盖全部可标注到期月（含 510500 牛差当季）
BOARD_EXPIRY_KINDS = frozenset({"near", "next", "quarter", "next_quarter"})
MIN_STAT_N = 50
MAX_BUY_DIST_STRIKES = 3
WIDTH_CANDIDATES = (1,)
MAX_COST_REL = 0.5

# 分标的回测结论配置（弹性排序：159915 > 510500 ≈ 510300 > 588000）
SYMBOL_PROFILES: dict[str, dict[str, Any]] = {
    "159915": {
        "name": "创业板",
        "elasticRank": 1,
        "sweetMin": 0.30,
        "sweetMax": 0.40,
        "tpMult": 2.5,
        "bullExpiryKinds": frozenset({"next", "next_quarter"}),
        "bearExpiryKinds": frozenset({"near", "next", "next_quarter"}),
        "caution": None,
    },
    "510500": {
        "name": "中证500",
        "elasticRank": 2,
        "sweetMin": 0.20,
        "sweetMax": 0.40,
        "tpMult": 2.0,
        "bullExpiryKinds": frozenset({"next", "quarter", "next_quarter"}),
        "bearExpiryKinds": frozenset({"near", "next", "next_quarter"}),
        "caution": None,
    },
    "510300": {
        "name": "沪深300",
        "elasticRank": 3,
        "sweetMin": 0.30,
        "sweetMax": 0.40,
        "tpMult": 2.0,
        "bullExpiryKinds": frozenset({"next", "next_quarter"}),
        "bearExpiryKinds": frozenset({"near", "next", "next_quarter"}),
        "caution": None,
    },
    "588000": {
        "name": "科创50",
        "elasticRank": 4,
        "sweetMin": 0.30,
        "sweetMax": 0.50,
        "tpMult": 2.5,
        "bullExpiryKinds": frozenset({"next", "next_quarter"}),
        "bearExpiryKinds": frozenset({"near", "next", "next_quarter"}),
        "caution": None,
    },
    # 展示用档案：尚未纳入四标的弹性回测排序，chip 序号接在 #4 之后
    "510050": {
        "name": "上证50",
        "elasticRank": 5,
        "sweetMin": 0.30,
        "sweetMax": 0.40,
        "tpMult": 2.0,
        "bullExpiryKinds": frozenset({"next", "next_quarter"}),
        "bearExpiryKinds": frozenset({"near", "next", "next_quarter"}),
        "caution": None,
        "useBacktestStats": True,
        "assetKind": "etf",
        "perUnit": 10000.0,
        "spreadMult": 10000.0,
    },
    # 中金所股指期权：启发式价差提示，不引用 ETF 回测统计
    "000300": {
        "name": "沪深300指",
        "elasticRank": 11,
        "sweetMin": 0.25,
        "sweetMax": 0.45,
        "tpMult": 2.0,
        "bullExpiryKinds": frozenset({"next", "next_quarter"}),
        "bearExpiryKinds": frozenset({"near", "next", "next_quarter"}),
        "caution": "中金所股指期权·启发式选腿（非ETF回测统计）",
        "useBacktestStats": False,
        "assetKind": "cffex_index",
        "perUnit": 100.0,
        "spreadMult": 100.0,
        "spotTsCode": "000300.SH",
        "optCode": "OP000300.SH",
    },
    "000016": {
        "name": "上证50指",
        "elasticRank": 12,
        "sweetMin": 0.25,
        "sweetMax": 0.45,
        "tpMult": 2.0,
        "bullExpiryKinds": frozenset({"next", "next_quarter"}),
        "bearExpiryKinds": frozenset({"near", "next", "next_quarter"}),
        "caution": "中金所股指期权·启发式选腿（非ETF回测统计）",
        "useBacktestStats": False,
        "assetKind": "cffex_index",
        "perUnit": 100.0,
        "spreadMult": 100.0,
        "spotTsCode": "000016.SH",
        "optCode": "OP000016.SH",
    },
    "000852": {
        "name": "中证1000指",
        "elasticRank": 13,
        "sweetMin": 0.25,
        "sweetMax": 0.45,
        "tpMult": 2.0,
        "bullExpiryKinds": frozenset({"next", "next_quarter"}),
        "bearExpiryKinds": frozenset({"near", "next", "next_quarter"}),
        "caution": "中金所股指期权·启发式选腿（非ETF回测统计）",
        "useBacktestStats": False,
        "assetKind": "cffex_index",
        "perUnit": 100.0,
        "spreadMult": 100.0,
        "spotTsCode": "000852.SH",
        "optCode": "OP000852.SH",
    },
}

CFFEX_INDEX_OPT_CODES = frozenset({"000300", "000016", "000852"})

_DEFAULT_PROFILE: dict[str, Any] = {
    "name": "",
    "elasticRank": 99,
    "sweetMin": 0.30,
    "sweetMax": 0.40,
    "tpMult": 2.0,
    "bullExpiryKinds": frozenset({"next", "next_quarter"}),
    "bearExpiryKinds": frozenset({"near", "next", "next_quarter"}),
    "caution": None,
    "useBacktestStats": True,
    "assetKind": "etf",
    "perUnit": 10000.0,
    "spreadMult": 10000.0,
}

ELASTIC_ORDER_LABEL = (
    "ETF：159915创业板 > 510500中证500 ≈ 510300沪深300 > 588000科创50；"
    "中金所股指期权（启发式）：000300 IO > 000016 HO > 000852 MO"
)


def isCffexIndexOpt(code6: str) -> bool:
    return str(code6 or "").strip() in CFFEX_INDEX_OPT_CODES


def getSymbolProfile(code6: str) -> dict[str, Any]:
    return {**_DEFAULT_PROFILE, **SYMBOL_PROFILES.get(code6, {})}


def sortCodesByElasticRank(codes: list[str]) -> list[str]:
    return sorted(codes, key=lambda c: (getSymbolProfile(c).get("elasticRank", 99), c))


def profilesForBoardJson() -> dict[str, dict[str, Any]]:
    """供前端 Tab 展示：弹性序号、简称、警示。"""
    out: dict[str, dict[str, Any]] = {}
    for code6, p in SYMBOL_PROFILES.items():
        merged = getSymbolProfile(code6)
        out[code6] = {
            "name": merged["name"],
            "elasticRank": merged["elasticRank"],
            "tpMult": merged["tpMult"],
            "sweetMin": merged["sweetMin"],
            "sweetMax": merged["sweetMax"],
            "caution": merged.get("caution"),
            "assetKind": merged.get("assetKind", "etf"),
            "useBacktestStats": bool(merged.get("useBacktestStats", True)),
        }
    return out


def bullExpiryKindsFor(code6: str) -> frozenset[str]:
    return getSymbolProfile(code6)["bullExpiryKinds"]


def bearExpiryKindsFor(code6: str) -> frozenset[str]:
    return getSymbolProfile(code6)["bearExpiryKinds"]


def costBucket(costRel: float) -> str:
    s = pd.cut([costRel], bins=COST_BINS, labels=COST_LABELS)[0]
    return str(s)


def _statsKey(code6: str, direction: str, expiryKind: str, width: int, bucket: str) -> str:
    return f"{code6}|{direction}|{expiryKind}|{width}|{bucket}"


def loadBoardStats() -> dict[str, dict[str, Any]]:
    if not STATS_PATH.is_file():
        return {}
    with STATS_PATH.open(encoding="utf-8") as f:
        raw = json.load(f)
    return raw if isinstance(raw, dict) else {}


def _filterBySymbolTpMult(cond: pd.DataFrame) -> pd.DataFrame:
    """各标的仅保留其回测最优止盈倍数样本。"""
    if cond.empty or "tp_mult" not in cond.columns:
        return cond
    parts: list[pd.DataFrame] = []
    cond = cond.copy()
    cond["_code6"] = cond["code"].astype(str).str.split(".").str[0]
    for code6, g in cond.groupby("_code6", observed=True):
        tp = float(getSymbolProfile(str(code6))["tpMult"])
        hit = g[np.isclose(g["tp_mult"].astype(float), tp, rtol=0, atol=1e-6)]
        if not hit.empty:
            parts.append(hit)
    if not parts:
        return cond.iloc[0:0]
    out = pd.concat(parts, ignore_index=True)
    return out.drop(columns=["_code6"], errors="ignore")


def buildBoardStats(outPath: Path | None = None) -> dict[str, dict[str, Any]]:
    """四标的日频条件回测 → 方向正确 → 分标的 tp_mult → 聚合 lookup。"""
    outPath = outPath or STATS_PATH
    engine = makeEngine()
    rows: list[dict[str, Any]] = []
    for target in TARGETS:
        meta = loadOptMeta(engine, target)
        daily = loadOptDaily(engine, target, "close")
        etf = loadEtfDaily(engine, target)
        if meta.empty or not daily or etf.empty:
            continue
        rows.extend(runConditionalBacktest(target, meta, daily, etf, freq="D"))
    if not rows:
        lookup: dict[str, dict[str, Any]] = {}
    else:
        df = pd.DataFrame(rows)
        cond = df[df["dir_correct"] == True].copy()  # noqa: E712
        cond = cond[cond["expiry_kind"].isin(BOARD_EXPIRY_KINDS)]
        cond = _filterBySymbolTpMult(cond)
        cond = _addCostBucket(cond)
        cond["cost_bucket"] = cond["cost_bucket"].astype(str)
        lookup = {}
        keys = ["code", "direction", "expiry_kind", "width", "cost_bucket"]
        for key_tuple, g in cond.groupby(keys, observed=True):
            if len(g) < MIN_STAT_N:
                continue
            code = str(key_tuple[0]).split(".")[0]
            direction, expiryKind, width, bucket = key_tuple[1], key_tuple[2], int(key_tuple[3]), str(key_tuple[4])
            agg = _aggGroup(g)
            lookup[_statsKey(code, direction, expiryKind, width, bucket)] = {
                "n": int(agg["n"]),
                "win_rate": float(agg["win_rate"]),
                "profit_factor": float(min(agg["profit_factor"], 99.0)),
                "avg_ret": float(agg["avg_ret"]),
                "ret_per_day": float(agg["ret_per_day"]),
                "tp_mult": float(getSymbolProfile(code)["tpMult"]),
            }
        for key_tuple, g in cond.groupby(["code", "direction", "expiry_kind", "width"], observed=True):
            if len(g) < MIN_STAT_N:
                continue
            code = str(key_tuple[0]).split(".")[0]
            direction, expiryKind, width = key_tuple[1], key_tuple[2], int(key_tuple[3])
            agg = _aggGroup(g)
            lookup[_statsKey(code, direction, expiryKind, width, "*")] = {
                "n": int(agg["n"]),
                "win_rate": float(agg["win_rate"]),
                "profit_factor": float(min(agg["profit_factor"], 99.0)),
                "avg_ret": float(agg["avg_ret"]),
                "ret_per_day": float(agg["ret_per_day"]),
                "tp_mult": float(getSymbolProfile(code)["tpMult"]),
            }
    outPath.parent.mkdir(parents=True, exist_ok=True)
    with outPath.open("w", encoding="utf-8") as f:
        json.dump(lookup, f, ensure_ascii=False, indent=2)
    return lookup


def _lookupStats(
    lookup: dict[str, dict[str, Any]],
    code6: str,
    direction: str,
    expiryKind: str,
    width: int,
    bucket: str,
) -> dict[str, Any] | None:
    hit = lookup.get(_statsKey(code6, direction, expiryKind, width, bucket))
    if hit and hit.get("n", 0) >= MIN_STAT_N:
        return hit
    return lookup.get(_statsKey(code6, direction, expiryKind, width, "*"))


def _sweetCostScore(code6: str, direction: str, costRel: float) -> float:
    prof = getSymbolProfile(code6)
    lo = float(prof["sweetMin"])
    hi = float(prof["sweetMax"])
    if lo <= costRel <= hi:
        score = 1.0
    else:
        if costRel < lo:
            dist = lo - costRel
        else:
            dist = costRel - hi
        score = max(0.0, 1.0 - dist / 0.18)
    if costRel > 0.55:
        score *= 0.55
    if costRel < 0.12:
        score *= 0.75
    return score


def _enumerateCandidates(
    legs: dict[float, dict],
    quoteMap: dict[str, dict[str, Any]],
    spot: float,
    direction: str,
    closeFn: Any,
    validFn: Any,
    buyFn: Any = None,
    sellFn: Any = None,
) -> list[dict[str, Any]]:
    buyPxFn = buyFn or closeFn
    sellPxFn = sellFn or closeFn
    usable: list[tuple[float, str]] = []
    for strike, row in legs.items():
        tc = str(row.get("ts_code") or "").strip()
        if tc and validFn(quoteMap.get(tc)):
            usable.append((strike, tc))
    usable.sort(key=lambda x: x[0])
    if len(usable) < 2:
        return []
    strikes = [u[0] for u in usable]
    tcMap = {u[0]: u[1] for u in usable}
    atmIdx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    lo = max(0, atmIdx - MAX_BUY_DIST_STRIKES)
    hi = min(len(strikes), atmIdx + MAX_BUY_DIST_STRIKES + 1)
    out: list[dict[str, Any]] = []
    for buyIdx in range(lo, hi):
        for w in WIDTH_CANDIDATES:
            sellIdx = buyIdx + w if direction == "bull" else buyIdx - w
            if sellIdx < 0 or sellIdx >= len(strikes):
                continue
            buyK, sellK = strikes[buyIdx], strikes[sellIdx]
            if direction == "bull" and sellK <= buyK:
                continue
            if direction == "bear" and sellK >= buyK:
                continue
            buyTc, sellTc = tcMap[buyK], tcMap[sellK]
            buyPx = buyPxFn(quoteMap.get(buyTc))
            sellPx = sellPxFn(quoteMap.get(sellTc))
            if buyPx is None or sellPx is None:
                continue
            net = buyPx - sellPx
            kSpread = abs(sellK - buyK)
            if net <= 0 or kSpread <= 0:
                continue
            costRel = net / kSpread
            if costRel > MAX_COST_REL:
                continue
            out.append({
                "buy_strike": buyK,
                "sell_strike": sellK,
                "buy_ts": buyTc,
                "sell_ts": sellTc,
                "net": net,
                "width": w,
                "cost_rel": costRel,
                "cost_bucket": costBucket(costRel),
            })
    return out


def scoreCandidate(
    cand: dict[str, Any],
    code6: str,
    direction: str,
    expiryKind: str,
    lookup: dict[str, dict[str, Any]],
) -> tuple[float, dict[str, Any]]:
    bucket = cand["cost_bucket"]
    width = int(cand["width"])
    stats = _lookupStats(lookup, code6, direction, expiryKind, width, bucket)
    sweet = _sweetCostScore(code6, direction, float(cand["cost_rel"]))
    tpMult = float(getSymbolProfile(code6)["tpMult"])
    if stats:
        wr = float(stats["win_rate"])
        pf = float(stats["profit_factor"])
        pfNorm = min(pf / 6.0, 1.0)
        nBonus = min(int(stats["n"]) / 500.0, 1.0) * 0.05
        score = 0.30 * sweet + 0.38 * wr + 0.32 * pfNorm + nBonus
        detail = {
            "win_rate": wr,
            "profit_factor": pf,
            "avg_ret": stats.get("avg_ret"),
            "stat_n": stats.get("n"),
            "stat_level": "bucket",
            "sweet_score": sweet,
            "tp_mult": tpMult,
        }
    else:
        score = 0.55 * sweet + 0.20
        detail = {
            "win_rate": None,
            "profit_factor": None,
            "stat_n": 0,
            "stat_level": "sweet_only",
            "sweet_score": sweet,
            "tp_mult": tpMult,
        }
    return score, detail


def pickSpreadByBacktestScore(
    legs: dict[float, dict],
    quoteMap: dict[str, dict[str, Any]],
    spot: float | None,
    code6: str,
    direction: str,
    expiryKind: str,
    closeFn: Any,
    validFn: Any,
    lookup: dict[str, dict[str, Any]] | None = None,
    buyFn: Any = None,
    sellFn: Any = None,
) -> dict[str, Any] | None:
    if spot is None or not legs:
        return None
    lookup = lookup if lookup is not None else loadBoardStats()
    cands = _enumerateCandidates(
        legs, quoteMap, spot, direction, closeFn, validFn,
        buyFn=buyFn, sellFn=sellFn,
    )
    if not cands:
        return None
    best: dict[str, Any] | None = None
    bestScore = -1.0
    for c in cands:
        sc, detail = scoreCandidate(c, code6, direction, expiryKind, lookup)
        if sc > bestScore:
            bestScore = sc
            best = {**c, "score": sc, **detail}
    return best


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="生成 opt-t-board 选腿回测统计表")
    parser.add_argument("--out", default=str(STATS_PATH))
    args = parser.parse_args()
    lk = buildBoardStats(Path(args.out))
    print(f"写入 {args.out}，键数 {len(lk)}")

# 代码更新时间：2026-08-05 22:46
# 代码更新时间：2026-08-07 12:30 中金所 000300/000016/000852 启发式档案（非ETF回测统计）
# 代码更新时间：2026-09-02 14:04 去掉 588000「强信号再做」页头警示（易误导）
# 代码更新时间：2026-09-09 16:49 价差枚举可选 buyFn/sellFn（买腿 ask / 卖腿 bid）
