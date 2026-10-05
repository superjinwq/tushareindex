# coding: utf-8
"""T 板新鲜度取证：区分「行情时间」与「拉取时间」，判定 liveN 是否真实。

用法：
    python -X utf8 scripts/audit_opt_freshness.py
    python -X utf8 scripts/audit_opt_freshness.py --undl 510050
    python -X utf8 scripts/audit_opt_freshness.py --slo

判定口径：
- 读 15580(/snapshot) 与 15581(/snapshot)，看每个合约的时间戳落在哪个交易日；
- 与 T 板 API 的 quote_source / quote_live_n 对照；
- 若 liveN>0 但时间戳全部落在非交易日，则判定为「伪实时」。
- `--slo` 额外读取轻量 quote-delta，按字段的最后观测时刻计算年龄；
  连续竞价内检查 OI：P0（ATM/高量高仓代理）P95 <=10s，整链 P95 <=45s、max <=60s。
  非连续竞价只报告年龄，不把自然静止误判为服务异常。
"""
from __future__ import annotations
# 用途: 标定
# 保留至: 2026-10-20

import argparse
import json
import math
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, time as clockTime

DAQMT = "http://127.0.0.1:15580/snapshot"
TDX = "http://127.0.0.1:15581/snapshot"
BOARD = "http://127.0.0.1:5000/api/opt-t-board?u={u}"
DELTA = "http://127.0.0.1:5000/api/opt-t-board/quote-delta?u={u}"
UNDLS = ("510050", "510300", "510500", "588000", "159915")
FIELD_LABELS = {"last": "最新", "vol": "成交量", "oi": "持仓量", "book": "买卖一"}


def getJson(url: str, timeout: float = 15.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def dayOf(raw) -> date | None:
    """把多种时间戳形态归一成日期；无法解析返回 None。"""
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    try:
        if val > 1e12:
            return datetime.fromtimestamp(val / 1000.0).date()
        if val > 1e9:
            return datetime.fromtimestamp(val).date()
    except (OSError, OverflowError, ValueError):
        return None
    return None


def fieldTime(rec: dict, keys: tuple) -> float:
    """按顺序取第一个可解析的时间键，返回毫秒。"""
    hasExplicit = any(k in rec for k in keys)
    for k in keys:
        raw = rec.get(k)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val > 1e12:
            return val
        if val > 1e9:
            return val * 1000.0
    if hasExplicit:
        return 0.0
    for k in ("time", "t"):
        raw = rec.get(k)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val > 1e12:
            return val
        if val > 1e9:
            return val * 1000.0
    return 0.0


def inContinuousAuction(now: datetime) -> bool:
    """仅连续竞价评价实时 SLA；集合竞价、午休和盘后不判失败。"""
    if now.weekday() >= 5:
        return False
    current = now.time()
    return (clockTime(9, 30) <= current <= clockTime(11, 30)) or (
        clockTime(13, 0) <= current <= clockTime(15, 0)
    )


def percentile(values: list[float], p: float) -> float | None:
    """nearest-rank 百分位；空样本必须显式返回 None。"""
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * p) - 1))
    return ordered[index]


def deltaFieldAgeSec(field: object, nowMs: float) -> float | None:
    """只用可信可替换字段的观测时钟，不能拿 changed_at 冒充新鲜度。"""
    if not isinstance(field, dict) or not field.get("replace"):
        return None
    try:
        observedMs = float(field.get("observed_ms"))
    except (TypeError, ValueError):
        return None
    if observedMs <= 0:
        return None
    return max(0.0, (nowMs - observedMs) / 1000.0)


def qmtToTsCode(qmtCode: object) -> str:
    raw = str(qmtCode or "").strip().upper()
    if raw.endswith(".SHO"):
        return raw[:-1]
    if raw.endswith(".SZO"):
        return raw[:-1]
    return raw


def positiveFloat(value: object) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def numericFloat(value: object, default: float = -1.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def selectOiP0Proxy(
    underlying: str,
    quotes: dict,
    snapshot: dict,
) -> set[str]:
    """构造可离线复现的 P0 代理：最近 ATM C/P + 高成交/高持仓。

    生产计划器还会加本地 `opt_sp_position` 的开放仓位；审计器不读库，
    所以这里刻意称为 proxy，不能把它说成完整持仓优先集合。
    """
    optMeta = snapshot.get("optMeta") if isinstance(snapshot, dict) else {}
    etf = snapshot.get("etf") if isinstance(snapshot, dict) else {}
    if not isinstance(optMeta, dict) or not isinstance(etf, dict):
        return set()
    spot = None
    for code, rec in etf.items():
        if str(code).upper().startswith(underlying) and isinstance(rec, dict):
            spot = positiveFloat(rec.get("last")) or positiveFloat(rec.get("lastPrice"))
            if spot is not None:
                break
    rows = []
    for qmtCode, meta in optMeta.items():
        tsCode = qmtToTsCode(qmtCode)
        if tsCode not in quotes or not isinstance(meta, dict):
            continue
        if str(meta.get("undl") or "").split(".")[0] != underlying:
            continue
        strike = positiveFloat(meta.get("k"))
        rows.append((tsCode, strike, quotes.get(tsCode) or {}))
    selected: set[str] = set()
    strikes = sorted({strike for _, strike, _ in rows if strike is not None}, key=lambda v: (abs(v - spot) if spot is not None else v, v))
    for tsCode, strike, _ in rows:
        if strike in set(strikes[:1]):
            selected.add(tsCode)
    for field in ("vol", "oi"):
        ranked = sorted(
            rows,
            key=lambda row: -numericFloat(
                ((row[2].get(field) or {}).get("value")), default=-1.0
            ),
        )
        selected.update(tsCode for tsCode, _, _ in ranked[:3])
    return selected


def formatAgeStats(ages: list[float], expectedCount: int) -> str:
    p50 = percentile(ages, 0.50)
    p95 = percentile(ages, 0.95)
    if p50 is None or p95 is None:
        return f"valid=0/{expectedCount}"
    return (
        f"valid={len(ages)}/{expectedCount} p50={p50:.1f}s "
        f"p95={p95:.1f}s max={max(ages):.1f}s"
    )


def auditDeltaSlo(underlying: str, snapshot: dict, now: datetime) -> tuple[bool | None, str]:
    """返回连续竞价下的 OI SLA 结论；盘外返回 None（不评价）。"""
    try:
        payload = getJson(DELTA.format(u=underlying), timeout=10.0)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return False, f"quote-delta 失败 {type(exc).__name__}: {exc}"
    quotes = payload.get("quotes") if isinstance(payload, dict) else {}
    if not isinstance(payload, dict) or not payload.get("ok") or not isinstance(quotes, dict):
        return False, f"quote-delta 无有效载荷：{payload!r}"
    nowMs = now.timestamp() * 1000.0
    fieldAges: dict[str, list[float]] = {}
    for field in FIELD_LABELS:
        fieldAges[field] = [
            age
            for rec in quotes.values()
            if isinstance(rec, dict)
            for age in [deltaFieldAgeSec(rec.get(field), nowMs)]
            if age is not None
        ]
    p0Codes = selectOiP0Proxy(underlying, quotes, snapshot)
    p0OiAges = [
        age
        for tsCode in p0Codes
        for age in [deltaFieldAgeSec((quotes.get(tsCode) or {}).get("oi"), nowMs)]
        if age is not None
    ]
    print(f"\n[quote-delta {underlying}] n={len(quotes)} observed_at={payload.get('observed_at')}")
    for field, label in FIELD_LABELS.items():
        print(f"  {label:4s} {formatAgeStats(fieldAges[field], len(quotes))}")
    print(f"  OI P0代理（ATM+高量高仓，未含开放仓位） {formatAgeStats(p0OiAges, len(p0Codes))}")
    if not inContinuousAuction(now):
        return None, "NOT_EVALUATED_OFF_SESSION"
    p0P95 = percentile(p0OiAges, 0.95)
    p1P95 = percentile(fieldAges["oi"], 0.95)
    p1Max = max(fieldAges["oi"], default=float("inf"))
    passed = bool(
        p0P95 is not None
        and p0P95 <= 10.0
        and p1P95 is not None
        and p1P95 <= 45.0
        and p1Max <= 60.0
    )
    return passed, "PASS" if passed else "DEGRADED"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--undl", default="", help="只看某标的（六位代码）")
    ap.add_argument("--slo", action="store_true", help="审计 quote-delta 的字段年龄与 OI SLA")
    args = ap.parse_args()
    today = date.today()

    print(f"=== 取证时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
          f"（today={today}）===")

    try:
        tdx = getJson(TDX)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"TDX 15581 不可用：{type(exc).__name__}: {exc}")
        tdx = {}
    if tdx:
        tdxOpt = tdx.get("opt") or {}
        days = Counter()
        nonzero = 0
        for rec in tdxOpt.values():
            if not isinstance(rec, dict):
                continue
            d = dayOf(rec.get("t") or rec.get("time"))
            days[str(d)] += 1
            last = rec.get("lastPrice")
            if last is None:
                last = rec.get("last")
            try:
                if last is not None and float(last) > 0:
                    nonzero += 1
            except (TypeError, ValueError):
                pass
        print(f"\n[TDX 15581] optN={tdx.get('optN')} oiN={tdx.get('oiN')} "
              f"wall={tdx.get('wall')} pullSec={tdx.get('pullSec')}")
        print(f"  有正值 last 的合约：{nonzero}/{len(tdxOpt)}")
        print(f"  time/t 日期分布：{dict(days)}")
        print(f"  tdxFields 是否含持仓字段：" 
              f"{[f for f in (tdx.get('tdxFields') or []) if 'Vol' in f or 'Stock' in f]}")

    try:
        dq = getJson(DAQMT)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"\nDAQMT 15580 不可用：{type(exc).__name__}: {exc}")
        dq = {}
    if dq:
        opt = dq.get("opt") or {}
        cLast = Counter()
        cQty = Counter()
        cOi = Counter()
        cBook = Counter()
        for rec in opt.values():
            if not isinstance(rec, dict):
                continue
            cLast[str(dayOf(fieldTime(rec, ("lastTime", "last_time"))))] += 1
            cQty[str(dayOf(fieldTime(rec, ("qtyTime", "qty_time"))))] += 1
            cOi[str(dayOf(fieldTime(rec, ("oiTime", "oi_time"))))] += 1
            cBook[str(dayOf(fieldTime(rec, ("bookTime", "book_time"))))] += 1
        print(f"\n[DAQMT 15580] optN={dq.get('optN')} etfN={dq.get('etfN')} "
              f"optWall={dq.get('optWall')} etfWall={dq.get('etfWall')}")
        print(f"  字段级时钟日期分布（None/今天以外的都算不可信）：")
        print(f"    lastTime : {dict(cLast)}")
        print(f"    qtyTime  : {dict(cQty)}")
        print(f"    oiTime   : {dict(cOi)}")
        print(f"    bookTime : {dict(cBook)}")

    print("\n[T 板 API] 逐标的：")
    targets = [args.undl] if args.undl else list(UNDLS)
    for u in targets:
        try:
            d = getJson(BOARD.format(u=u), timeout=30.0)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            print(f"  {u}: 失败 {type(exc).__name__}: {exc}")
            continue
        print(f"  {u}: src={d.get('quote_source')} liveN={d.get('quote_live_n')} "
              f"poll_ms={d.get('poll_ms')} asof={d.get('quote_asof')}")
    if not args.slo:
        return 0
    print("\n[quote-delta OI SLA]")
    outcomes: list[bool] = []
    for u in targets:
        result, label = auditDeltaSlo(u, dq, datetime.now())
        print(f"  {u}: {label}")
        if result is not None:
            outcomes.append(result)
    if outcomes and not all(outcomes):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
