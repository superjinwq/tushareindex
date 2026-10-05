#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 stk_factor_pro 查找「前复权收盘价首次突破前高」的日期、价格及辅助指标。

模式（--mode）：
- his：首次突破全历史前高；
- 850：首次突破 850 周期前高（前 850 根 bar，不含信号日）；
- both：一次扫描同时输出上述两份结果（各写独立 txt/csv，语义互不干扰）。

运行示例：
  python first_qfq_new_high.py
      # 默认：stk_factor_pro 最新交易日 + mode=both（his/850 各一套 txt/csv）
  python first_qfq_new_high.py --code 688146.SH
  python first_qfq_new_high.py --mode his --from-date 2025-01-01 --to-date 2026-05-28
  python first_qfq_new_high.py --from-date 2026-06-03 --to-date 2026-06-03 --analyze
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import statistics
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from logging.handlers import TimedRotatingFileHandler
from typing import Any

import pymysql

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "stock_tushare"),
    "charset": "utf8mb4",
}

EPS = 1e-9
FORWARD_END_DEFAULT = date(2026, 5, 28)
LOOKBACK_850 = 850
BreakMode = str  # "his" | "850" | "both"


@dataclass
class BarRecord:
    tradeDate: date
    highQfq: float
    lowQfq: float
    closeQfq: float
    pctChg: float | None = None
    vol: float = 0.0
    amount: float = 0.0
    freeShare: float | None = None


@dataclass
class FirstBreakResult:
    tsCode: str
    name: str = ""
    breakDate: date | None = None
    closeQfq: float | None = None
    highQfq: float | None = None
    priorMaxHighQfq: float = 0.0
    priorMaxDate: date | None = None
    baselineDate: date | None = None
    baselineHighQfq: float = 0.0
    hasBreak: bool = False
    breakPct: float | None = None
    upperShadowPct: float | None = None
    pre5DayRetPct: float | None = None
    breakDayPctChg: float | None = None
    pre120RetPct: float | None = None
    peakAgeDays: int | None = None
    pre120RangePct: float | None = None
    ma120BiasPct: float | None = None
    volRatio20: float | None = None
    listAgeDays: int | None = None
    board: str = ""
    tag: str = ""
    r5: float | None = None
    r10: float | None = None
    mdd20: float | None = None
    maxUp20: float | None = None
    breakMode: str = "his"
    swName: str = ""


def setupLogger() -> logging.Logger:
    logger = logging.getLogger("first_qfq_new_high")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fileHandler = TimedRotatingFileHandler(
        "first_qfq_new_high.log",
        when="D",
        interval=1,
        backupCount=3,
        encoding="utf-8",
    )
    fileHandler.setFormatter(fmt)
    consoleHandler = logging.StreamHandler()
    consoleHandler.setFormatter(fmt)
    logger.addHandler(fileHandler)
    logger.addHandler(consoleHandler)
    return logger


logger = setupLogger()


def toFloat(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def parseIsoDate(text: str) -> date:
    return datetime.strptime(text.strip(), "%Y-%m-%d").date()


def resolveBreakMode(mode: str) -> BreakMode:
    normalized = mode.strip().lower()
    if normalized in ("his", "history", "all", "h_high"):
        return "his"
    if normalized in ("850", "h850", "high_850"):
        return "850"
    if normalized in ("both", "all_modes", "dual"):
        return "both"
    raise ValueError(f"不支持的 --mode: {mode}，可选 his / 850 / both")


def activeScanModes(breakMode: BreakMode) -> list[str]:
    if breakMode == "both":
        return ["his", "850"]
    return [breakMode]


def lookbackBarsForMode(breakMode: BreakMode) -> int | None:
    return LOOKBACK_850 if breakMode == "850" else None


def modeLabel(breakMode: BreakMode) -> str:
    return "850周期前高" if breakMode == "850" else "历史前高"


def modeFileSuffix(breakMode: BreakMode) -> str:
    return "_850" if breakMode == "850" else ""


class SlidingMaxHigh:
    """滑动窗口内 high_qfq 最大值（单调队列，O(1) 均摊）。"""

    def __init__(self) -> None:
        self.queue: deque[tuple[int, float, date]] = deque()

    def add(self, idx: int, highQfq: float, tradeDate: date) -> None:
        while self.queue and self.queue[-1][1] <= highQfq:
            self.queue.pop()
        self.queue.append((idx, highQfq, tradeDate))

    def removeExpired(self, cutoffIdx: int) -> None:
        while self.queue and self.queue[0][0] <= cutoffIdx:
            self.queue.popleft()

    def priorMax(self) -> tuple[float, date | None]:
        if not self.queue:
            return 0.0, None
        return self.queue[0][1], self.queue[0][2]


def resolveBoard(tsCode: str) -> str:
    if tsCode.endswith(".BJ"):
        return "BJ"
    if tsCode.startswith("688"):
        return "KC"
    if tsCode.startswith("30"):
        return "CY"
    if tsCode.endswith(".SH"):
        return "SH"
    return "SZ"


def loadQfqBars(
    conn: pymysql.connections.Connection,
    tsCode: str | None,
    endDate: date,
    applySampleFilter: bool,
) -> dict[str, list[BarRecord]]:
    sampleClause = "AND vol > 0 AND amount > 0" if applySampleFilter else ""
    codeClause = "AND ts_code = %s" if tsCode else ""
    params: list[Any] = []
    if tsCode:
        params.append(tsCode)
    params.append(endDate)

    sql = f"""
        SELECT ts_code, trade_date, high_qfq, low_qfq, close_qfq, pct_chg, vol, amount, free_share
        FROM stk_factor_pro
        WHERE high_qfq IS NOT NULL
          AND low_qfq IS NOT NULL
          AND close_qfq IS NOT NULL
          {sampleClause}
          {codeClause}
          AND trade_date <= %s
        ORDER BY ts_code, trade_date
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    byCode: dict[str, list[BarRecord]] = defaultdict(list)
    for code, tradeDate, highQfq, lowQfq, closeQfq, pctChg, vol, amount, freeShare in rows:
        hq = toFloat(highQfq)
        lq = toFloat(lowQfq)
        cq = toFloat(closeQfq)
        if hq is None or lq is None or cq is None:
            continue
        byCode[code].append(
            BarRecord(
                tradeDate=tradeDate,
                highQfq=hq,
                lowQfq=lq,
                closeQfq=cq,
                pctChg=toFloat(pctChg),
                vol=float(vol) if vol else 0.0,
                amount=float(amount) if amount else 0.0,
                freeShare=toFloat(freeShare),
            )
        )
    return byCode


def calcPre5DayRetPct(bars: list[BarRecord], idx: int) -> float | None:
    """突破日前 5 个交易日累计涨幅：T-5…T-1 共 5 日，close[T-1]/close[T-6]-1（不含突破日）。"""
    if idx < 6:
        return None
    baseClose = bars[idx - 6].closeQfq
    prevClose = bars[idx - 1].closeQfq
    if baseClose <= 0:
        return None
    return (prevClose / baseClose - 1.0) * 100.0


def calcPre120RetPct(bars: list[BarRecord], idx: int) -> float | None:
    """突破日前 120 个交易日累计涨幅：T-120…T-1 共 120 日，close[T-1]/close[T-121]-1（不含突破日）。"""
    if idx < 121:
        return None
    baseClose = bars[idx - 121].closeQfq
    prevClose = bars[idx - 1].closeQfq
    if baseClose <= 0:
        return None
    return (prevClose / baseClose - 1.0) * 100.0


def calcPeakAgeDays(
    bars: list[BarRecord], idx: int, priorMaxDate: date | None
) -> int | None:
    """前高「年龄」：前高形成日至突破日之间的交易日数（不含前高日与突破日）。"""
    if priorMaxDate is None:
        return None
    peakIdx = None
    for i in range(idx):
        if bars[i].tradeDate == priorMaxDate:
            peakIdx = i
    if peakIdx is None:
        return None
    return idx - peakIdx - 1


def calcPre120RangePct(bars: list[BarRecord], idx: int) -> float | None:
    """前120日振幅 (high/low - 1)×100%，区间 T-120…T-1，不含信号日。"""
    if idx < 120:
        return None
    seg = bars[idx - 120 : idx]
    hi = max(b.highQfq for b in seg)
    lo = min(b.lowQfq for b in seg)
    if lo <= 0:
        return None
    return (hi / lo - 1.0) * 100.0


def calcMa120BiasPct(bars: list[BarRecord], idx: int) -> float | None:
    """信号日收盘相对 MA120 乖离率（MA120 含信号日共 120 根收盘）。"""
    if idx < 119:
        return None
    seg = bars[idx - 119 : idx + 1]
    ma120 = sum(b.closeQfq for b in seg) / 120.0
    if ma120 <= 0:
        return None
    return (bars[idx].closeQfq / ma120 - 1.0) * 100.0


def calcVolRatio20(bars: list[BarRecord], idx: int) -> float | None:
    """信号日成交量 / 前20交易日均量（均量不含信号日）。"""
    if idx < 20:
        return None
    avgVol = sum(bars[i].vol for i in range(idx - 20, idx)) / 20.0
    if avgVol <= 0:
        return None
    return bars[idx].vol / avgVol


def attachExtendedMetrics(
    result: FirstBreakResult, bars: list[BarRecord], idx: int
) -> None:
    """填充前120涨幅、前高年龄、前120振幅、MA120乖离、20日均量比。"""
    result.pre120RetPct = calcPre120RetPct(bars, idx)
    result.peakAgeDays = calcPeakAgeDays(bars, idx, result.priorMaxDate)
    result.pre120RangePct = calcPre120RangePct(bars, idx)
    result.ma120BiasPct = calcMa120BiasPct(bars, idx)
    result.volRatio20 = calcVolRatio20(bars, idx)


def calcBreakMetrics(
    bars: list[BarRecord],
    idx: int,
    priorMax: float,
) -> tuple[float, float, float | None, float | None]:
    bar = bars[idx]
    breakPct = (bar.closeQfq / priorMax - 1.0) * 100.0
    upperShadowPct = (bar.highQfq / bar.closeQfq - 1.0) * 100.0 if bar.closeQfq > 0 else 0.0
    pre5DayRetPct = calcPre5DayRetPct(bars, idx)
    breakDayPctChg = bar.pctChg
    return breakPct, upperShadowPct, pre5DayRetPct, breakDayPctChg


def attachForwardMetrics(result: FirstBreakResult, bars: list[BarRecord], idx: int) -> None:
    if not result.hasBreak or result.closeQfq is None:
        return
    fwd = bars[idx + 1 :]
    base = result.closeQfq
    for n, attr in ((5, "r5"), (10, "r10")):
        if len(fwd) >= n:
            setattr(result, attr, (fwd[n - 1].closeQfq / base - 1.0) * 100.0)
    window = fwd[:20]
    if window:
        result.mdd20 = min((b.closeQfq / base - 1.0) * 100.0 for b in window)
        result.maxUp20 = max((b.highQfq / base - 1.0) * 100.0 for b in window)


def classifyOutcome(result: FirstBreakResult) -> str:
    r = result.r10 if result.r10 is not None else result.r5
    if result.mdd20 is not None and result.mdd20 <= -10.0:
        return "fail"
    if r is not None and r <= -5.0:
        return "fail"
    if r is not None and r >= 8.0:
        return "win"
    if result.maxUp20 is not None and result.maxUp20 >= 12.0:
        return "win"
    return "neutral"


def buildBreakResult(
    tsCode: str,
    bars: list[BarRecord],
    idx: int,
    priorMax: float,
    priorMaxDate: date | None,
    name: str,
    listDate: date | None,
    breakMode: BreakMode,
    withForward: bool,
) -> FirstBreakResult:
    breakPct, upperShadowPct, pre5DayRetPct, breakDayPctChg = calcBreakMetrics(
        bars, idx, priorMax
    )
    bar = bars[idx]
    listAge = (bar.tradeDate - listDate).days if listDate else None
    result = FirstBreakResult(
        tsCode=tsCode,
        name=name,
        breakDate=bar.tradeDate,
        closeQfq=bar.closeQfq,
        highQfq=bar.highQfq,
        priorMaxHighQfq=priorMax,
        priorMaxDate=priorMaxDate,
        baselineDate=bars[0].tradeDate,
        baselineHighQfq=bars[0].highQfq,
        hasBreak=True,
        breakPct=breakPct,
        upperShadowPct=upperShadowPct,
        pre5DayRetPct=pre5DayRetPct,
        breakDayPctChg=breakDayPctChg,
        listAgeDays=listAge,
        board=resolveBoard(tsCode),
        breakMode=breakMode,
    )
    attachExtendedMetrics(result, bars, idx)
    if withForward:
        attachForwardMetrics(result, bars, idx)
        result.tag = classifyOutcome(result)
    return result


def scanStockFirstBreaks(
    tsCode: str,
    bars: list[BarRecord],
    fromDate: date | None,
    toDate: date | None,
    modes: list[str],
    name: str,
    listDate: date | None,
    withAnalyze: bool,
) -> dict[str, FirstBreakResult | None]:
    """单票顺序扫描：各 mode 独立记录全生命周期首次突破（批量时仅区间内落盘）。"""
    pending = set(modes)
    found: dict[str, FirstBreakResult | None] = {m: None for m in modes}
    if len(bars) < 2:
        return found

    runningMax = bars[0].highQfq
    runningMaxDate = bars[0].tradeDate
    sliding = SlidingMaxHigh()
    sliding.add(0, bars[0].highQfq, bars[0].tradeDate)
    track850 = "850" in pending

    for idx in range(1, len(bars)):
        if not pending:
            break
        bar = bars[idx]
        prior850, prior850Date = (0.0, None)
        if track850 and "850" in pending:
            sliding.removeExpired(idx - LOOKBACK_850 - 1)
            prior850, prior850Date = sliding.priorMax()

        if "his" in pending and bar.closeQfq > runningMax + EPS:
            result = buildBreakResult(
                tsCode, bars, idx, runningMax, runningMaxDate,
                name, listDate, "his", withAnalyze,
            )
            if fromDate is None or toDate is None or (fromDate <= bar.tradeDate <= toDate):
                found["his"] = result
            pending.discard("his")

        if "850" in pending and bar.closeQfq > prior850 + EPS:
            result = buildBreakResult(
                tsCode, bars, idx, prior850, prior850Date,
                name, listDate, "850", withAnalyze,
            )
            if fromDate is None or toDate is None or (fromDate <= bar.tradeDate <= toDate):
                found["850"] = result
            pending.discard("850")

        if "his" in pending and bar.highQfq > runningMax:
            runningMax = bar.highQfq
            runningMaxDate = bar.tradeDate
        if track850:
            sliding.add(idx, bar.highQfq, bar.tradeDate)

    return found


def findFirstBreaksForModes(
    tsCode: str,
    bars: list[BarRecord],
    modes: list[str],
    name: str = "",
    listDate: date | None = None,
    withForward: bool = False,
) -> dict[str, FirstBreakResult]:
    if not bars:
        raise ValueError(f"stk_factor_pro 无有效前复权数据: {tsCode}")

    found = scanStockFirstBreaks(
        tsCode, bars, None, None, modes, name, listDate, withForward,
    )
    baseline = bars[0]
    empty = FirstBreakResult(
        tsCode=tsCode,
        name=name,
        priorMaxHighQfq=baseline.highQfq,
        priorMaxDate=baseline.tradeDate,
        baselineDate=baseline.tradeDate,
        baselineHighQfq=baseline.highQfq,
        hasBreak=False,
        board=resolveBoard(tsCode),
    )
    out: dict[str, FirstBreakResult] = {}
    for mode in modes:
        item = found.get(mode)
        if item is not None:
            out[mode] = item
        else:
            stub = FirstBreakResult(
                tsCode=empty.tsCode,
                name=empty.name,
                priorMaxHighQfq=empty.priorMaxHighQfq,
                priorMaxDate=empty.priorMaxDate,
                baselineDate=empty.baselineDate,
                baselineHighQfq=empty.baselineHighQfq,
                hasBreak=False,
                board=empty.board,
                breakMode=mode,
            )
            out[mode] = stub
    return out


def findFirstBreakInBars(
    tsCode: str,
    bars: list[BarRecord],
    name: str = "",
    listDate: date | None = None,
    withForward: bool = False,
    breakMode: BreakMode = "his",
) -> FirstBreakResult:
    if not bars:
        raise ValueError(f"stk_factor_pro 无有效前复权数据: {tsCode}")

    lookback = lookbackBarsForMode(breakMode)
    baseline = bars[0]
    runningMax = baseline.highQfq
    runningMaxDate = baseline.tradeDate
    sliding = SlidingMaxHigh()
    sliding.add(0, baseline.highQfq, baseline.tradeDate)

    if len(bars) == 1:
        return FirstBreakResult(
            tsCode=tsCode,
            name=name,
            priorMaxHighQfq=baseline.highQfq,
            priorMaxDate=baseline.tradeDate,
            baselineDate=baseline.tradeDate,
            baselineHighQfq=baseline.highQfq,
            hasBreak=False,
            board=resolveBoard(tsCode),
            breakMode=breakMode,
        )

    for idx in range(1, len(bars)):
        bar = bars[idx]
        if lookback is not None:
            sliding.removeExpired(idx - lookback - 1)
            priorMax, priorMaxDate = sliding.priorMax()
        else:
            priorMax, priorMaxDate = runningMax, runningMaxDate

        if bar.closeQfq > priorMax + EPS:
            return buildBreakResult(
                tsCode,
                bars,
                idx,
                priorMax,
                priorMaxDate,
                name,
                listDate,
                breakMode,
                withForward,
            )

        if lookback is None:
            if bar.highQfq > runningMax:
                runningMax = bar.highQfq
                runningMaxDate = bar.tradeDate
        sliding.add(idx, bar.highQfq, bar.tradeDate)

    return FirstBreakResult(
        tsCode=tsCode,
        name=name,
        priorMaxHighQfq=runningMax if lookback is None else sliding.priorMax()[0],
        priorMaxDate=runningMaxDate if lookback is None else sliding.priorMax()[1],
        baselineDate=baseline.tradeDate,
        baselineHighQfq=baseline.highQfq,
        hasBreak=False,
        board=resolveBoard(tsCode),
        breakMode=breakMode,
    )


def findFirstQfqNewHigh(
    tsCode: str,
    applySampleFilter: bool = True,
    breakMode: BreakMode = "his",
) -> FirstBreakResult:
    conn = pymysql.connect(**DB_CONFIG, connect_timeout=10)
    try:
        byCode = loadQfqBars(conn, tsCode, date.today(), applySampleFilter)
        nameMap, listMap = loadBasicMaps(conn, [tsCode])
    finally:
        conn.close()
    bars = byCode.get(tsCode, [])
    return findFirstBreakInBars(
        tsCode,
        bars,
        name=nameMap.get(tsCode, ""),
        listDate=listMap.get(tsCode),
        breakMode=breakMode,
    )


def loadBasicMaps(
    conn: pymysql.connections.Connection,
    tsCodes: list[str] | None = None,
) -> tuple[dict[str, str], dict[str, date]]:
    nameMap: dict[str, str] = {}
    listMap: dict[str, date] = {}
    if tsCodes:
        ph = ",".join(["%s"] * len(tsCodes))
        sql = f"SELECT ts_code, name, list_date FROM stock_basic WHERE ts_code IN ({ph})"
        params: list[Any] = list(tsCodes)
    else:
        sql = "SELECT ts_code, name, list_date FROM stock_basic"
        params = []
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for ts, name, listDate in cur.fetchall():
            nameMap[ts] = name or ""
            listMap[ts] = listDate
    return nameMap, listMap


def loadSwNameMap(
    conn: pymysql.connections.Connection,
    tsCodes: list[str] | None = None,
) -> dict[str, str]:
    """每股最新申万行业名（与 rt_monitor 财报表 sw_name / 申万二级行业列同源）。

    来源：index_member_all_sw；is_new 为 Y 或 NULL；按 in_date 降序，同日多行取 sw_name 升序最小。
    """
    codeClause = ""
    params: list[Any] = []
    if tsCodes:
        ph = ",".join(["%s"] * len(tsCodes))
        codeClause = f"AND m.ts_code IN ({ph})"
        params = list(tsCodes)
    sql = f"""
        SELECT z.ts_code, z.sw_name
        FROM (
            SELECT m.ts_code, m.sw_name,
                ROW_NUMBER() OVER (
                    PARTITION BY m.ts_code
                    ORDER BY m.in_date DESC, m.sw_name ASC
                ) AS rn
            FROM index_member_all_sw m
            WHERE (m.is_new = 'Y' OR m.is_new IS NULL)
              {codeClause}
        ) z
        WHERE z.rn = 1
    """
    swMap: dict[str, str] = {}
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for ts, swName in cur.fetchall():
            swMap[ts] = (swName or "").strip()
    return swMap


def attachSwName(results: dict[str, FirstBreakResult | None], swMap: dict[str, str]) -> None:
    for result in results.values():
        if result is None:
            continue
        result.swName = swMap.get(result.tsCode, "")


def loadLatestTradeDate() -> date:
    conn = pymysql.connect(**DB_CONFIG, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(trade_date) FROM stk_factor_pro")
            row = cur.fetchone()
            if not row or row[0] is None:
                raise RuntimeError("stk_factor_pro 无交易日数据")
            latest = row[0]
            if isinstance(latest, date):
                return latest
            return parseIsoDate(str(latest)[:10])
    finally:
        conn.close()


def resolveBatchWindow(args: argparse.Namespace) -> tuple[date, date, date]:
    if args.from_date and args.to_date:
        fromDate = parseIsoDate(args.from_date)
        toDate = parseIsoDate(args.to_date)
    elif not args.from_date and not args.to_date:
        fromDate = toDate = loadLatestTradeDate()
        print(f"未指定日期，使用 stk_factor_pro 最新交易日: {fromDate}")
        logger.info("默认最新交易日 %s", fromDate)
    else:
        raise SystemExit("请同时指定 --from-date 与 --to-date，或两者均省略以使用最新交易日")

    if fromDate > toDate:
        raise SystemExit(f"--from-date({fromDate}) 不能晚于 --to-date({toDate})")

    if args.forward_end:
        forwardEnd = parseIsoDate(args.forward_end)
    else:
        forwardEnd = toDate
    if forwardEnd < toDate:
        forwardEnd = toDate
    return fromDate, toDate, forwardEnd


def loadTradeDays(conn: pymysql.connections.Connection, fromDate: date, toDate: date) -> list[date]:
    sql = """
        SELECT DISTINCT trade_date
        FROM stk_factor_pro
        WHERE trade_date BETWEEN %s AND %s
        ORDER BY trade_date
    """
    with conn.cursor() as cur:
        cur.execute(sql, (fromDate, toDate))
        return [row[0] for row in cur.fetchall()]


def scanRangeFirstBreaks(
    fromDate: date,
    toDate: date,
    forwardEnd: date,
    applySampleFilter: bool,
    withAnalyze: bool,
    breakMode: BreakMode = "his",
) -> tuple[list[date], dict[str, dict[date, list[FirstBreakResult]]]]:
    modes = activeScanModes(breakMode)
    conn = pymysql.connect(**DB_CONFIG, connect_timeout=30)
    try:
        byCode = loadQfqBars(conn, None, forwardEnd, applySampleFilter)
        nameMap, listMap = loadBasicMaps(conn)
        swMap = loadSwNameMap(conn)
        tradeDays = loadTradeDays(conn, fromDate, toDate)
    finally:
        conn.close()

    firstByMode: dict[str, dict[date, list[FirstBreakResult]]] = {
        m: defaultdict(list) for m in modes
    }
    for tsCode, bars in byCode.items():
        if len(bars) < 2:
            continue
        found = scanStockFirstBreaks(
            tsCode,
            bars,
            fromDate,
            toDate,
            modes,
            nameMap.get(tsCode, ""),
            listMap.get(tsCode),
            withAnalyze,
        )
        attachSwName(found, swMap)
        for mode in modes:
            result = found.get(mode)
            if result is not None and result.breakDate is not None:
                firstByMode[mode][result.breakDate].append(result)

    return tradeDays, firstByMode


def fmtPct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "NA"
    return f"{value:.{digits}f}"


def formatResult(result: FirstBreakResult) -> str:
    label = modeLabel(result.breakMode)
    lines = [f"{result.tsCode} {result.name} 前复权收盘价首次突破{label}"]
    lines.append(
        f"  上市 baseline: {result.baselineDate}  high_qfq={result.baselineHighQfq:.5f}"
    )
    if result.swName:
        lines.append(f"  申万行业 sw_name: {result.swName}")
    if result.hasBreak and result.breakDate is not None:
        lines.append(f"  突破日: {result.breakDate}")
        lines.append(f"  突破收盘价 close_qfq: {result.closeQfq:.5f}")
        lines.append(f"  当日最高价 high_qfq: {result.highQfq:.5f}")
        lines.append(
            f"  被突破前高: {result.priorMaxHighQfq:.5f} @ {result.priorMaxDate}"
        )
        lines.append(f"  突破幅度 break_pct: {fmtPct(result.breakPct)}%")
        lines.append(f"  上影线幅度 upper_shadow: {fmtPct(result.upperShadowPct)}%")
        lines.append(f"  前5日涨幅 pre5_ret(T-5…T-1): {fmtPct(result.pre5DayRetPct)}%")
        lines.append(f"  突破日涨跌幅 pct_chg: {fmtPct(result.breakDayPctChg)}%")
        lines.append(
            f"  前120日涨幅 pre120_ret(T-120…T-1): {fmtPct(result.pre120RetPct)}%"
        )
        lines.append(
            f"  前高年龄 peak_age(前高日→突破日间隔): "
            f"{result.peakAgeDays if result.peakAgeDays is not None else 'NA'} 日"
        )
        lines.append(f"  前120日振幅 pre120_range: {fmtPct(result.pre120RangePct)}%")
        lines.append(f"  MA120乖离 ma120_bias: {fmtPct(result.ma120BiasPct)}%")
        lines.append(
            f"  信号日/20日均量比 vol_ratio20: "
            f"{fmtPct(result.volRatio20, 2) if result.volRatio20 is not None else 'NA'}x"
        )
    else:
        noBreakMsg = (
            "  上市以来收盘价未突破850周期前高"
            if result.breakMode == "850"
            else "  上市以来收盘价未突破历史前高（仅 baseline）"
        )
        lines.append(noBreakMsg)
        lines.append(
            f"  当前参照前高 high_qfq: {result.priorMaxHighQfq:.5f} @ {result.priorMaxDate}"
        )
    return "\n".join(lines)


def formatEventLine(result: FirstBreakResult) -> str:
    volRatio = (
        f"{result.volRatio20:.2f}"
        if result.volRatio20 is not None
        else "NA"
    )
    peakAge = result.peakAgeDays if result.peakAgeDays is not None else "NA"
    swLabel = result.swName or "--"
    return (
        f"  {result.tsCode} {result.name} sw_name={swLabel} "
        f"close={result.closeQfq:.4f} high={result.highQfq:.4f} "
        f"prev_max={result.priorMaxHighQfq:.4f} "
        f"break_pct={fmtPct(result.breakPct)}% "
        f"upper_shadow={fmtPct(result.upperShadowPct)}% "
        f"pre5_ret={fmtPct(result.pre5DayRetPct)}% "
        f"pct_chg={fmtPct(result.breakDayPctChg)}% "
        f"pre120_ret={fmtPct(result.pre120RetPct)}% "
        f"peak_age={peakAge} "
        f"pre120_range={fmtPct(result.pre120RangePct)}% "
        f"ma120_bias={fmtPct(result.ma120BiasPct)}% "
        f"vol_ratio20={volRatio}x"
    )


def writeDailyText(
    path: str,
    tradeDays: list[date],
    firstByDate: dict[date, list[FirstBreakResult]],
) -> int:
    total = 0
    with open(path, "w", encoding="utf-8") as fp:
        for tradeDay in tradeDays:
            items = sorted(firstByDate.get(tradeDay, []), key=lambda x: x.tsCode)
            total += len(items)
            fp.write(f"=== {tradeDay} ({len(items)}只) ===\n")
            if not items:
                fp.write("  (无)\n")
            else:
                for item in items:
                    fp.write(formatEventLine(item) + "\n")
            fp.write("\n")
        fp.write(f"TOTAL: {total}\n")
    return total


def writeCsv(path: str, events: list[FirstBreakResult]) -> None:
    headers = [
        "trade_date", "ts_code", "name", "sw_name", "break_mode", "close_qfq", "high_qfq", "prev_max",
        "break_pct", "upper_shadow_pct", "pre5_ret_pct", "pct_chg",
        "pre120_ret_pct", "peak_age_days", "pre120_range_pct", "ma120_bias_pct",
        "vol_ratio20",
        "list_age_days", "board", "tag", "r5", "r10", "mdd20", "max_up20",
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=headers)
        writer.writeheader()
        for e in sorted(events, key=lambda x: (x.breakDate or date.min, x.tsCode)):
            writer.writerow(
                {
                    "trade_date": e.breakDate,
                    "ts_code": e.tsCode,
                    "name": e.name,
                    "sw_name": e.swName,
                    "break_mode": e.breakMode,
                    "close_qfq": f"{e.closeQfq:.4f}" if e.closeQfq is not None else "",
                    "high_qfq": f"{e.highQfq:.4f}" if e.highQfq is not None else "",
                    "prev_max": f"{e.priorMaxHighQfq:.4f}",
                    "break_pct": fmtPct(e.breakPct),
                    "upper_shadow_pct": fmtPct(e.upperShadowPct),
                    "pre5_ret_pct": fmtPct(e.pre5DayRetPct),
                    "pct_chg": fmtPct(e.breakDayPctChg),
                    "pre120_ret_pct": fmtPct(e.pre120RetPct),
                    "peak_age_days": e.peakAgeDays if e.peakAgeDays is not None else "",
                    "pre120_range_pct": fmtPct(e.pre120RangePct),
                    "ma120_bias_pct": fmtPct(e.ma120BiasPct),
                    "vol_ratio20": fmtPct(e.volRatio20, 2),
                    "list_age_days": e.listAgeDays if e.listAgeDays is not None else "",
                    "board": e.board,
                    "tag": e.tag,
                    "r5": fmtPct(e.r5),
                    "r10": fmtPct(e.r10),
                    "mdd20": fmtPct(e.mdd20),
                    "max_up20": fmtPct(e.maxUp20),
                }
            )


def metricStats(events: list[FirstBreakResult], key: str) -> tuple[float | None, float | None]:
    vals = [getattr(e, key) for e in events if getattr(e, key) is not None]
    if not vals:
        return None, None
    return statistics.median(vals), statistics.mean(vals)


def printAnalyzeReport(events: list[FirstBreakResult]) -> None:
    groups = {tag: [e for e in events if e.tag == tag] for tag in ("win", "fail", "neutral")}
    print(f"\n=== 后续表现分组 (N={len(events)}) ===")
    print(
        f"win={len(groups['win'])} fail={len(groups['fail'])} neutral={len(groups['neutral'])}"
    )
    print("说明：收益相关统计默认展示中位数，均值仅作参考。")
    metricKeys = (
        "breakPct", "upperShadowPct", "pre5DayRetPct", "breakDayPctChg",
        "pre120RetPct", "peakAgeDays", "pre120RangePct", "ma120BiasPct", "volRatio20",
        "listAgeDays", "r5", "r10", "mdd20", "maxUp20",
    )
    for tag in ("fail", "win", "neutral"):
        group = groups[tag]
        if not group:
            continue
        print(f"\n--- {tag} n={len(group)} ---")
        for key in metricKeys:
            medianVal, meanVal = metricStats(group, key)
            if medianVal is None:
                print(f"  {key}: NA")
            else:
                print(f"  {key}: median={medianVal:.2f} mean={meanVal:.2f}")
        print(f"  board: {dict(Counter(e.board for e in group))}")

    rules = [
        ("全体", lambda e: True),
        ("北交所", lambda e: e.board == "BJ"),
        ("突破日涨>=10%", lambda e: e.breakDayPctChg is not None and e.breakDayPctChg >= 10),
        ("突破日涨<10%", lambda e: e.breakDayPctChg is not None and e.breakDayPctChg < 10),
        ("突破幅度>=8%", lambda e: e.breakPct is not None and e.breakPct >= 8),
        ("前5日涨>=15%", lambda e: e.pre5DayRetPct is not None and e.pre5DayRetPct >= 15),
        ("前5日涨<15%", lambda e: e.pre5DayRetPct is not None and e.pre5DayRetPct < 15),
        ("前120涨>=100%", lambda e: e.pre120RetPct is not None and e.pre120RetPct >= 100),
        ("前120涨<60%", lambda e: e.pre120RetPct is not None and e.pre120RetPct < 60),
        ("前高年龄<=5日", lambda e: e.peakAgeDays is not None and e.peakAgeDays <= 5),
        ("前高年龄>=120日", lambda e: e.peakAgeDays is not None and e.peakAgeDays >= 120),
        ("前120振幅>=150%", lambda e: e.pre120RangePct is not None and e.pre120RangePct >= 150),
        ("MA120乖离>=80%", lambda e: e.ma120BiasPct is not None and e.ma120BiasPct >= 80),
        ("上影>=5%", lambda e: e.upperShadowPct is not None and e.upperShadowPct >= 5),
    ]
    print("\n=== 规则筛选 ===")
    for name, fn in rules:
        sub = [e for e in events if fn(e)]
        if not sub:
            continue
        failN = sum(1 for e in sub if e.tag == "fail")
        winN = sum(1 for e in sub if e.tag == "win")
        r10Med, r10Mean = metricStats(sub, "r10")
        mdd20Med, mdd20Mean = metricStats(sub, "mdd20")
        print(
            f"{name}: n={len(sub)} fail={failN}({failN/len(sub)*100:.0f}%) "
            f"win={winN}({winN/len(sub)*100:.0f}%)"
        )
        if r10Med is not None:
            print(f"  r10: median={r10Med:.2f}% mean={r10Mean:.2f}%")
        if mdd20Med is not None:
            print(f"  mdd20: median={mdd20Med:.2f}% mean={mdd20Mean:.2f}%")


def defaultBatchPaths(
    fromDate: date,
    toDate: date,
    mode: str,
    outTxt: str | None = None,
    outCsv: str | None = None,
) -> tuple[str, str]:
    suffix = modeFileSuffix(mode)
    txt = outTxt or f"first_qfq_new_high{suffix}_{fromDate}_{toDate}.txt"
    csvPath = outCsv or f"first_qfq_new_high{suffix}_{fromDate}_{toDate}.csv"
    return txt, csvPath


def summarizeBatchMode(
    mode: str,
    tradeDays: list[date],
    firstByDate: dict[date, list[FirstBreakResult]],
    outTxt: str,
    outCsv: str,
) -> int:
    total = writeDailyText(outTxt, tradeDays, firstByDate)
    events = [e for d in tradeDays for e in firstByDate.get(d, [])]
    writeCsv(outCsv, events)
    activeDays = sum(1 for d in tradeDays if firstByDate.get(d))
    label = modeLabel(mode)
    print(f"[{label}] 样本 {total} 只，有事件交易日 {activeDays}/{len(tradeDays)} 天")
    print(f"  txt: {outTxt}")
    print(f"  csv: {outCsv}")
    return total


def parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="查找前复权收盘价首次突破前高（全历史 / 850周期 / 同时）")
    parser.add_argument(
        "--mode",
        default="both",
        choices=["his", "850", "both"],
        help="his=全历史；850=850周期；both=同时输出两份（默认）",
    )
    parser.add_argument("--code", help="单票查询，如 688146.SH")
    parser.add_argument(
        "--from-date",
        help="批量起始日 YYYY-MM-DD；省略则与 --to-date 一并默认最新交易日",
    )
    parser.add_argument(
        "--to-date",
        help="批量结束日 YYYY-MM-DD；省略则与 --from-date 一并默认最新交易日",
    )
    parser.add_argument(
        "--forward-end",
        default=None,
        help="后续收益计算截止日；默认与 --to-date 相同",
    )
    parser.add_argument("--out", help="批量 his 模式 txt 路径（both 时仅作用于 his）")
    parser.add_argument("--csv", help="批量 his 模式 csv 路径（both 时仅作用于 his）")
    parser.add_argument("--out-850", help="批量 850 模式 txt（both 时默认自动生成）")
    parser.add_argument("--csv-850", help="批量 850 模式 csv（both 时默认自动生成）")
    parser.add_argument("--analyze", action="store_true", help="批量模式输出成败分组统计")
    parser.add_argument(
        "--no-filter",
        action="store_true",
        help="不过滤 vol/amount 为零的行",
    )
    return parser.parse_args()


def runBatch(args: argparse.Namespace) -> int:
    fromDate, toDate, forwardEnd = resolveBatchWindow(args)
    breakMode = resolveBreakMode(args.mode)

    logger.info(
        "批量扫描 mode=%s %s ~ %s，forwardEnd=%s",
        breakMode,
        fromDate,
        toDate,
        forwardEnd,
    )
    tradeDays, firstByMode = scanRangeFirstBreaks(
        fromDate,
        toDate,
        forwardEnd,
        applySampleFilter=not args.no_filter,
        withAnalyze=args.analyze,
        breakMode=breakMode,
    )

    modes = activeScanModes(breakMode)
    grandTotal = 0
    for mode in modes:
        if mode == "his":
            outTxt, outCsv = defaultBatchPaths(fromDate, toDate, mode, args.out, args.csv)
        else:
            outTxt, outCsv = defaultBatchPaths(
                fromDate, toDate, mode, args.out_850, args.csv_850,
            )
        total = summarizeBatchMode(
            mode, tradeDays, firstByMode[mode], outTxt, outCsv,
        )
        grandTotal += total
        logger.info("批量[%s] total=%d txt=%s csv=%s", mode, total, outTxt, outCsv)
        if args.analyze:
            events = [e for d in tradeDays for e in firstByMode[mode].get(d, [])]
            print(f"\n=== analyze: {modeLabel(mode)} ===")
            printAnalyzeReport(events)

    if breakMode == "both":
        print(f"\n完成(both): his+850 合计写入 {len(modes)} 套文件，事件总数 {grandTotal}")
    else:
        print(f"\n完成: 事件 {grandTotal} 条")
    return 0


def main() -> int:
    args = parseArgs()
    breakMode = resolveBreakMode(args.mode)
    if args.code:
        tsCode = args.code.strip().upper()
        modes = activeScanModes(breakMode)
        logger.info(
            "查询 %s mode=%s sampleFilter=%s",
            tsCode,
            breakMode,
            not args.no_filter,
        )
        conn = pymysql.connect(**DB_CONFIG, connect_timeout=10)
        try:
            byCode = loadQfqBars(conn, tsCode, date.today(), not args.no_filter)
            nameMap, listMap = loadBasicMaps(conn, [tsCode])
            swMap = loadSwNameMap(conn, [tsCode])
        finally:
            conn.close()
        bars = byCode.get(tsCode, [])
        results = findFirstBreaksForModes(
            tsCode,
            bars,
            modes,
            name=nameMap.get(tsCode, ""),
            listDate=listMap.get(tsCode),
        )
        for item in results.values():
            item.swName = swMap.get(tsCode, "")
        parts = [formatResult(results[m]) for m in modes]
        text = "\n\n".join(parts)
        print(text)
        logger.info("%s", text.replace("\n", " | "))
        return 0

    return runBatch(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("用户中断")
        sys.exit(130)
    except Exception as exc:
        logger.exception("执行失败: %s", exc)
        print(f"异常: {exc}")
        sys.exit(1)

# 代码更新时间：2026-06-19 21:43
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
