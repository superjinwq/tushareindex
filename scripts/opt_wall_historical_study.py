"""期权墙历史事件研究（只读）。

默认研究 510050 的最近到期月：在每个交易日形成 OI Wall 快照，
然后沿同一 ``maturity_date`` 追踪未来 1/3/5 个交易日的墙位、集中度、
同合约 Flow 和 ETF 收盘表现。脚本只查询 MySQL，不执行写入。
"""
# 用途: 留档
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
import json
from math import isfinite
from pathlib import Path
import sys
from statistics import median
from typing import Any, cast

import pandas as pd

projectRoot = Path(__file__).resolve().parents[1]
if str(projectRoot) not in sys.path:
    sys.path.insert(0, str(projectRoot))

from opt_wall_510050 import _connection, resolveUnderlying  # noqa: E402


STANDARD_PER_UNIT = 10000.0
HORIZONS = (1, 3, 5)
SIDE_NAMES = {'C': 'Call', 'P': 'Put'}


@dataclass(frozen=True)
class WallMetric:
    totalOi: float
    wallOi: float | None
    wallStrikes: tuple[float, ...]
    top1Share: float | None
    top3Share: float | None


@dataclass(frozen=True)
class FlowMetric:
    totalDelta: float
    commonCount: int
    positiveTotal: float
    negativeTotal: float
    strikeDeltas: dict[float, float]
    positiveWall: tuple[float, float] | None
    negativeWall: tuple[float, float] | None


@dataclass
class StudyData:
    options: pd.DataFrame
    standard: pd.DataFrame
    prices: dict[date, float]
    commonDates: list[date]
    maturitiesByDate: dict[date, list[date]]
    quality: dict[str, Any]


def _queryRows(underlyingCode: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    config = resolveUnderlying(underlyingCode)
    optionSql = """
        SELECT od.trade_date, od.ts_code, ob.call_put, ob.exercise_price,
               ob.maturity_date, ob.per_unit, od.oi
        FROM opt_daily od
        INNER JOIN opt_basic ob ON ob.ts_code = od.ts_code
        WHERE ob.opt_code = %s
          AND ob.call_put IN ('C', 'P')
          AND ob.maturity_date >= od.trade_date
          AND (ob.list_date IS NULL OR ob.list_date <= od.trade_date)
          AND (ob.delist_date IS NULL OR ob.delist_date >= od.trade_date)
        ORDER BY od.trade_date, ob.maturity_date, ob.exercise_price,
                 ob.call_put, od.ts_code
    """
    priceSql = """
        SELECT trade_date, close_qfq
        FROM etf_daily
        WHERE ts_code = %s AND close_qfq IS NOT NULL
        ORDER BY trade_date
    """
    conn = _connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(optionSql, (config.optionRoot,))
            optionRows = list(cursor.fetchall())
            cursor.execute(priceSql, (config.code,))
            priceRows = list(cursor.fetchall())
    finally:
        conn.close()
    return optionRows, priceRows


def loadStudyData(underlyingCode: str = '510050') -> StudyData:
    optionRows, priceRows = _queryRows(underlyingCode)
    if not optionRows:
        raise RuntimeError('没有查询到期权历史数据')
    options = pd.DataFrame(optionRows)
    options['trade_date'] = pd.to_datetime(options['trade_date']).dt.date
    options['maturity_date'] = pd.to_datetime(options['maturity_date']).dt.date
    options['exercise_price_num'] = pd.to_numeric(
        options['exercise_price'], errors='coerce'
    )
    options['per_unit_num'] = pd.to_numeric(options['per_unit'], errors='coerce')
    options['oi_num'] = pd.to_numeric(options['oi'], errors='coerce')
    rawDates = set(options['trade_date'])
    prices = {
        pd.Timestamp(row['trade_date']).date(): float(row['close_qfq'])
        for row in priceRows
    }
    commonDates = sorted(rawDates & set(prices))
    if not commonDates:
        raise RuntimeError('期权与 ETF 没有共同 EOD 数据日')
    options = options[options['trade_date'].isin(commonDates)].copy()
    standard = options[
        options['per_unit_num'].notna()
        & ((options['per_unit_num'] - STANDARD_PER_UNIT).abs() < 1e-6)
        & options['exercise_price_num'].notna()
        & (options['exercise_price_num'] > 0)
        & options['oi_num'].notna()
        & (options['oi_num'] >= 0)
    ].copy()
    standard['oi_num'] = standard['oi_num'].astype(float)
    maturitiesByDate = {
        day: sorted(set(group['maturity_date']))[:4]
        for day, group in options.groupby('trade_date', sort=False)
    }
    duplicateRows = int(
        standard.duplicated(['trade_date', 'ts_code'], keep=False).sum()
    )
    quality = {
        'rawOptionRows': len(optionRows),
        'standardValidRows': len(standard),
        'adjustedRows': int(
            options['per_unit_num'].notna()
            .mul((options['per_unit_num'] - STANDARD_PER_UNIT).abs() >= 1e-6)
            .sum()
        ),
        'oiNullRows': int(options['oi_num'].isna().sum()),
        'duplicateDateContractRows': duplicateRows,
        'rawOptionDateCount': len(rawDates),
        'commonEodDateCount': len(commonDates),
        'rawOptionMinDate': min(rawDates).isoformat(),
        'rawOptionMaxDate': max(rawDates).isoformat(),
        'commonEodMinDate': commonDates[0].isoformat(),
        'commonEodMaxDate': commonDates[-1].isoformat(),
        'pageRecentDateLimit': 120,
    }
    return StudyData(options, standard, prices, commonDates, maturitiesByDate, quality)


def summarizeStrikeOis(strikeOis: dict[float, float]) -> WallMetric:
    if not strikeOis:
        return WallMetric(0.0, None, (), None, None)
    ranked = sorted(strikeOis.items(), key=lambda item: (-item[1], item[0]))
    totalOi = sum(float(value) for _, value in ranked)
    wallOi = float(ranked[0][1])
    wallStrikes = tuple(
        sorted(float(strike) for strike, value in ranked if value == wallOi)
    )
    top3Oi = sum(float(value) for _, value in ranked[:3])
    return WallMetric(
        totalOi=totalOi,
        wallOi=wallOi,
        wallStrikes=wallStrikes,
        top1Share=wallOi / totalOi if totalOi else None,
        top3Share=top3Oi / totalOi if totalOi else None,
    )


def buildWallMetrics(
    standard: pd.DataFrame,
) -> dict[tuple[date, date, str], WallMetric]:
    grouped = (
        standard.groupby(
            ['trade_date', 'maturity_date', 'call_put', 'exercise_price_num'],
            as_index=False,
            sort=False,
        )['oi_num']
        .sum()
    )
    metrics: dict[tuple[date, date, str], WallMetric] = {}
    for (day, maturity, side), group in grouped.groupby(
        ['trade_date', 'maturity_date', 'call_put'], sort=False
    ):
        strikeOis = {
            float(row.exercise_price_num): float(row.oi_num)
            for row in group.itertuples(index=False)
        }
        metrics[(day, maturity, side)] = summarizeStrikeOis(strikeOis)
    return metrics


def buildContractMaps(standard: pd.DataFrame) -> dict[tuple[date, date, str], dict[str, tuple[float, float]]]:
    maps: defaultdict[tuple[date, date, str], dict[str, tuple[float, float]]] = defaultdict(dict)
    for row in standard.itertuples(index=False):
        maps[(row.trade_date, row.maturity_date, row.call_put)][str(row.ts_code)] = (
            float(row.exercise_price_num),
            float(row.oi_num),
        )
    return dict(maps)


def calculateFlow(
    contractMaps: dict[tuple[date, date, str], dict[str, tuple[float, float]]],
    currentDate: date,
    baselineDate: date,
    maturity: date,
    side: str,
) -> FlowMetric:
    current = contractMaps.get((currentDate, maturity, side), {})
    baseline = contractMaps.get((baselineDate, maturity, side), {})
    commonCodes = current.keys() & baseline.keys()
    positive: defaultdict[float, float] = defaultdict(float)
    negative: defaultdict[float, float] = defaultdict(float)
    strikeDeltas: dict[float, float] = defaultdict(float)
    totalDelta = 0.0
    for tsCode in commonCodes:
        strike, currentOi = current[tsCode]
        _, baselineOi = baseline[tsCode]
        delta = currentOi - baselineOi
        totalDelta += delta
        strikeDeltas[strike] += delta
        if delta > 0:
            positive[strike] += delta
        elif delta < 0:
            negative[strike] += delta
    positiveWall = (
        sorted(positive.items(), key=lambda item: (-item[1], item[0]))[0]
        if positive
        else None
    )
    negativeWall = (
        sorted(negative.items(), key=lambda item: (item[1], item[0]))[0]
        if negative
        else None
    )
    return FlowMetric(
        totalDelta,
        len(commonCodes),
        sum(positive.values()),
        sum(negative.values()),
        dict(strikeDeltas),
        positiveWall,
        negativeWall,
    )


def buildFrontEvents(data: StudyData) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    metrics = buildWallMetrics(data.standard)
    contractMaps = buildContractMaps(data.standard)
    wallEvents: list[dict[str, Any]] = []
    dateEvents: list[dict[str, Any]] = []
    for index, day in enumerate(data.commonDates):
        maturities = data.maturitiesByDate.get(day, [])
        if not maturities:
            continue
        maturity = maturities[0]
        sideEvents: dict[str, dict[str, Any]] = {}
        for side in ('C', 'P'):
            metric = metrics.get((day, maturity, side))
            if metric is None or not metric.wallStrikes:
                continue
            event: dict[str, Any] = {
                'date': day,
                'maturity': maturity,
                'side': side,
                'wall': metric.wallStrikes[0],
                'wallStrikes': metric.wallStrikes,
                'distancePct': metric.wallStrikes[0] / data.prices[day] * 100 - 100,
                'top1': metric.top1Share,
                'top3': metric.top3Share,
                'totalOi': metric.totalOi,
            }
            if index > 0:
                flow = calculateFlow(
                    contractMaps,
                    day,
                    data.commonDates[index - 1],
                    maturity,
                    side,
                )
                direction = 1 if flow.totalDelta > 0 else -1 if flow.totalDelta < 0 else 0
                event['flowDelta1d'] = flow.totalDelta
                event['flowDirection'] = direction
                event['flowWall'] = (
                    flow.positiveWall[0]
                    if direction > 0 and flow.positiveWall
                    else flow.negativeWall[0]
                    if direction < 0 and flow.negativeWall
                    else None
                )
                event['positiveFlowTotal'] = flow.positiveTotal
                event['negativeFlowTotal'] = flow.negativeTotal
                event['positiveFlowWall'] = (
                    flow.positiveWall[0] if flow.positiveWall else None
                )
                event['negativeFlowWall'] = (
                    flow.negativeWall[0] if flow.negativeWall else None
                )
            for horizon in HORIZONS:
                futureIndex = index + horizon
                if futureIndex >= len(data.commonDates):
                    continue
                futureDate = data.commonDates[futureIndex]
                futureMetric = metrics.get((futureDate, maturity, side))
                if futureMetric is None or not futureMetric.wallStrikes:
                    continue
                event[f'wallSame{horizon}d'] = event['wall'] in futureMetric.wallStrikes
                event[f'top1Change{horizon}d'] = (
                    futureMetric.top1Share - event['top1']
                    if futureMetric.top1Share is not None and event['top1'] is not None
                    else None
                )
                event[f'top3Change{horizon}d'] = (
                    futureMetric.top3Share - event['top3']
                    if futureMetric.top3Share is not None and event['top3'] is not None
                    else None
                )
                event[f'return{horizon}d'] = (
                    data.prices[futureDate] / data.prices[day] - 1
                )
                if 'flowDelta1d' in event:
                    futureFlow = calculateFlow(
                        contractMaps, futureDate, day, maturity, side
                    )
                    direction = event['flowDirection']
                    futureWall = (
                        futureFlow.positiveWall[0]
                        if direction > 0 and futureFlow.positiveWall
                        else futureFlow.negativeWall[0]
                        if direction < 0 and futureFlow.negativeWall
                        else None
                    )
                    event[f'flowSameSign{horizon}d'] = (
                        direction != 0 and direction * futureFlow.totalDelta > 0
                    )
                    event[f'futureFlowDelta{horizon}d'] = futureFlow.totalDelta
                    event[f'flowWallSame{horizon}d'] = (
                        direction != 0
                        and event.get('flowWall') is not None
                        and futureWall == event['flowWall']
                    )
                    event[f'positiveNetSame{horizon}d'] = futureFlow.totalDelta > 0
                    event[f'negativeNetSame{horizon}d'] = futureFlow.totalDelta < 0
                    event[f'positiveFutureNetDelta{horizon}d'] = futureFlow.totalDelta
                    event[f'negativeFutureNetDelta{horizon}d'] = futureFlow.totalDelta
                    event[f'positiveFlowWallSame{horizon}d'] = (
                        event.get('positiveFlowWall') is not None
                        and futureFlow.positiveWall is not None
                        and futureFlow.positiveWall[0] == event['positiveFlowWall']
                    )
                    event[f'negativeFlowWallSame{horizon}d'] = (
                        event.get('negativeFlowWall') is not None
                        and futureFlow.negativeWall is not None
                        and futureFlow.negativeWall[0] == event['negativeFlowWall']
                    )
                    positiveWall = event.get('positiveFlowWall')
                    negativeWall = event.get('negativeFlowWall')
                    event[f'positiveFlowAtSameWallSame{horizon}d'] = (
                        positiveWall is not None
                        and futureFlow.strikeDeltas.get(cast(float, positiveWall), 0) > 0
                    )
                    event[f'negativeFlowAtSameWallSame{horizon}d'] = (
                        negativeWall is not None
                        and futureFlow.strikeDeltas.get(cast(float, negativeWall), 0) < 0
                    )
            sideEvents[side] = event
            wallEvents.append(event)
        if len(sideEvents) == 2:
            dateEvents.append(
                {
                    'date': day,
                    'maturity': maturity,
                    'price': data.prices[day],
                    'call': sideEvents['C'],
                    'put': sideEvents['P'],
                }
            )
    return wallEvents, dateEvents


def _cleanValues(values: list[float | None]) -> list[float]:
    return [float(value) for value in values if value is not None and isfinite(float(value))]


def summarizeValues(values: list[float | None], percent: bool = False) -> dict[str, Any]:
    clean = sorted(_cleanValues(values))
    if not clean:
        return {'n': 0}
    scale = 100 if percent else 1
    count = len(clean)
    return {
        'n': count,
        'mean': round(sum(clean) / count * scale, 3),
        'median': round(median(clean) * scale, 3),
        'positiveRate': round(sum(value > 0 for value in clean) / count * 100, 2),
        'p25': round(clean[int((count - 1) * 0.25)] * scale, 3),
        'p75': round(clean[int((count - 1) * 0.75)] * scale, 3),
    }


def _periodEvents(events: list[dict[str, Any]], since: date | None) -> list[dict[str, Any]]:
    return events if since is None else [event for event in events if event['date'] >= since]


def _wallSummary(events: list[dict[str, Any]], side: str) -> dict[str, Any]:
    scoped = [event for event in events if event['side'] == side]
    result: dict[str, Any] = {}
    for horizon in HORIZONS:
        sameKey = f'wallSame{horizon}d'
        valid = [event for event in scoped if sameKey in event]
        top1Changes = [event.get(f'top1Change{horizon}d') for event in valid]
        top3Changes = [event.get(f'top3Change{horizon}d') for event in valid]
        top1Valid = _cleanValues(top1Changes)
        top3Valid = _cleanValues(top3Changes)
        result[f'{horizon}D'] = {
            'n': len(valid),
            'wallSameRate': round(
                sum(bool(event[sameKey]) for event in valid) / len(valid) * 100, 2
            ) if valid else None,
            'top1ChangePp': summarizeValues(top1Changes, percent=True),
            'top3ChangePp': summarizeValues(top3Changes, percent=True),
            'top1NotLowerRate': round(
                sum(value >= 0 for value in top1Valid) / len(top1Valid) * 100, 2
            ) if top1Valid else None,
            'top3NotLowerRate': round(
                sum(value >= 0 for value in top3Valid) / len(top3Valid) * 100, 2
            ) if top3Valid else None,
        }
    return result


def _flowSummary(events: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for side in ('C', 'P'):
        for direction, totalKey, samePrefix, wallPrefix in (
            (1, 'positiveFlowTotal', 'positiveNetSame', 'positiveFlowWallSame'),
            (-1, 'negativeFlowTotal', 'negativeNetSame', 'negativeFlowWallSame'),
        ):
            scoped = [
                event for event in events
                if event['side'] == side and event.get(totalKey, 0) * direction > 0
            ]
            key = f'{SIDE_NAMES[side]}_{"+" if direction > 0 else "-"}'
            item: dict[str, Any] = {'n': len(scoped)}
            for horizon in HORIZONS:
                sameKey = f'{samePrefix}{horizon}d'
                valid = [event for event in scoped if sameKey in event]
                futureTotalKey = (
                    f'positiveFutureNetDelta{horizon}d'
                    if direction > 0
                    else f'negativeFutureNetDelta{horizon}d'
                )
                deltaValues = [event.get(futureTotalKey) for event in valid]
                wallKey = f'{wallPrefix}{horizon}d'
                wallValid = [event for event in scoped if wallKey in event]
                sameWallDirectionKey = (
                    f'positiveFlowAtSameWallSame{horizon}d'
                    if direction > 0
                    else f'negativeFlowAtSameWallSame{horizon}d'
                )
                sameWallDirectionValid = [
                    event for event in scoped if sameWallDirectionKey in event
                ]
                item[f'{horizon}D'] = {
                    'n': len(valid),
                    'sameSignRate': round(
                        sum(bool(event[sameKey]) for event in valid) / len(valid) * 100, 2
                    ) if valid else None,
                    'futureDeltaWanMedian': round(median(_cleanValues(deltaValues)) / 10000, 3)
                    if _cleanValues(deltaValues) else None,
                    'sameFlowWallStrikeRate': round(
                        sum(bool(event[wallKey]) for event in wallValid)
                        / len(wallValid) * 100,
                        2,
                    ) if wallValid else None,
                    'sameInitialFlowWallDirectionRate': round(
                        sum(bool(event[sameWallDirectionKey]) for event in sameWallDirectionValid)
                        / len(sameWallDirectionValid) * 100,
                        2,
                    ) if sameWallDirectionValid else None,
                }
            result[key] = item
    return result


def _returnSummary(events: list[dict[str, Any]], side: str | None = None) -> dict[str, Any]:
    scoped: list[dict[str, Any]] = (
        [event['call'] for event in events]
        if side is None
        else [event[side] for event in events]
    )
    return {
        f'{horizon}D': summarizeValues(
            [event.get(f'return{horizon}d') for event in scoped], percent=True
        )
        for horizon in HORIZONS
    }


def _percentileRank(values: list[float | None], value: float | None) -> float | None:
    clean = _cleanValues(values)
    if value is None or not clean:
        return None
    return round(sum(item <= value for item in clean) / len(clean) * 100, 1)


def buildReport(data: StudyData, recentYears: int = 3) -> dict[str, Any]:
    metrics = buildWallMetrics(data.standard)
    contractMaps = buildContractMaps(data.standard)
    wallEvents, dateEvents = buildFrontEvents(data)
    latestDate = data.commonDates[-1]
    latestMaturity = data.maturitiesByDate[latestDate][0]
    latestCall = metrics[(latestDate, latestMaturity, 'C')]
    latestPut = metrics[(latestDate, latestMaturity, 'P')]
    latestFlows: dict[str, Any] = {}
    for side in ('C', 'P'):
        flow = calculateFlow(
            contractMaps, latestDate, data.commonDates[-2], latestMaturity, side
        )
        direction = 1 if flow.totalDelta > 0 else -1 if flow.totalDelta < 0 else 0
        latestFlows[SIDE_NAMES[side]] = {
            'deltaOi': round(flow.totalDelta, 2),
            'direction': 'positive' if direction > 0 else 'negative' if direction < 0 else 'flat',
            'wall': flow.positiveWall if direction > 0 else flow.negativeWall if direction < 0 else None,
            'positiveFlowTotalOi': round(flow.positiveTotal, 2),
            'positiveFlowWall': flow.positiveWall,
            'negativeFlowTotalOi': round(flow.negativeTotal, 2),
            'negativeFlowWall': flow.negativeWall,
            'commonContracts': flow.commonCount,
        }
    latest = {
        'date': latestDate.isoformat(),
        'maturity': latestMaturity.isoformat(),
        'spot': data.prices[latestDate],
        'call': latestCall.__dict__,
        'put': latestPut.__dict__,
        'callDistancePct': round(latestCall.wallStrikes[0] / data.prices[latestDate] * 100 - 100, 3),
        'putDistancePct': round(latestPut.wallStrikes[0] / data.prices[latestDate] * 100 - 100, 3),
        'callPutRatio': round(latestCall.totalOi / latestPut.totalOi, 4) if latestPut.totalOi else None,
        'flow1d': latestFlows,
    }
    recentSince = latestDate.replace(year=latestDate.year - recentYears)
    recentWallEvents = _periodEvents(wallEvents, recentSince)
    recentDateEvents = _periodEvents(dateEvents, recentSince)
    currentLike = []
    for event in dateEvents:
        if event['date'] == latestDate:
            continue
        call, put = event['call'], event['put']
        if call.get('positiveFlowTotal', 0) <= 0 or put.get('positiveFlowTotal', 0) <= 0:
            continue
        if not (0 <= call['distancePct'] <= 8 and -6 <= put['distancePct'] <= 6):
            continue
        if call.get('top3') is None or put.get('top3') is None:
            continue
        if call['top3'] < 0.60 or put['top3'] < 0.50:
            continue
        currentLike.append(event)
    currentLikeDates = {event['date'] for event in currentLike}
    currentLikeWallEvents = [
        event for event in wallEvents if event['date'] in currentLikeDates
    ]
    callDateEvents = [event['call'] for event in dateEvents]
    putDateEvents = [event['put'] for event in dateEvents]
    callDistanceValues = [
        cast(float | None, event.get('distancePct')) for event in callDateEvents
    ]
    putDistanceValues = [
        cast(float | None, event.get('distancePct')) for event in putDateEvents
    ]
    callTop1Values = [cast(float | None, event.get('top1')) for event in callDateEvents]
    callTop3Values = [cast(float | None, event.get('top3')) for event in callDateEvents]
    putTop1Values = [cast(float | None, event.get('top1')) for event in putDateEvents]
    putTop3Values = [cast(float | None, event.get('top3')) for event in putDateEvents]
    currentPercentile = {
        'callDistancePct': _percentileRank(
            callDistanceValues, cast(float, latest['callDistancePct'])
        ),
        'putDistancePct': _percentileRank(
            putDistanceValues, cast(float, latest['putDistancePct'])
        ),
        'callTop1': _percentileRank(
            callTop1Values, latestCall.top1Share
        ),
        'callTop3': _percentileRank(
            callTop3Values, latestCall.top3Share
        ),
        'putTop1': _percentileRank(
            putTop1Values, latestPut.top1Share
        ),
        'putTop3': _percentileRank(
            putTop3Values, latestPut.top3Share
        ),
        'callPutRatio': _percentileRank(
            [
                event['call']['totalOi'] / event['put']['totalOi']
                if event['put']['totalOi']
                else None
                for event in dateEvents
            ],
            cast(float, latest['callPutRatio']),
        ),
    }
    return {
        'method': {
            'event': '每日最近到期月 EOD 快照；未来追踪同一 maturity_date，避免期限滚动混入墙位变化',
            'wall': '同一到期日、同一侧按行权价汇总 OI 后取 arg max；Top1=Wall OI/同侧总OI，Top3=前三行权价OI/同侧总OI',
        'flow': '同一合约 OI(current)-OI(baseline)，缺少基线合约不补零；正/负 Flow Wall 分别按未来净 Flow 同号、原墙位同号和墙位留存追踪',
            'price': 'etf_daily.close_qfq 从事件日收盘到未来 1/3/5 个共同交易日收盘的收益',
        'currentLike': 'Call Wall 距现货 0~8%，Put Wall 距现货 -6~6%，Call/Put Top3 至少 60%/50%，且两侧均有 1D +Flow',
        },
        'quality': data.quality,
        'latestFront': latest,
        'latestPercentile': currentPercentile,
        'samples': {
            'fullFrontDates': len(dateEvents),
            'recentYears': recentYears,
            'recentFrontDates': len(recentDateEvents),
            'fullSideWallEvents': len(wallEvents),
            'recentSideWallEvents': len(recentWallEvents),
            'currentLikeDates': len(currentLike),
        },
        'wallFull': {
            'Call': _wallSummary(wallEvents, 'C'),
            'Put': _wallSummary(wallEvents, 'P'),
        },
        'wallRecent': {
            'Call': _wallSummary(recentWallEvents, 'C'),
            'Put': _wallSummary(recentWallEvents, 'P'),
        },
        'flowFull': _flowSummary(wallEvents),
        'flowRecent': _flowSummary(recentWallEvents),
        'wallCurrentLike': {
            'Call': _wallSummary(currentLikeWallEvents, 'C'),
            'Put': _wallSummary(currentLikeWallEvents, 'P'),
        },
        'flowCurrentLike': _flowSummary(currentLikeWallEvents),
        'priceFull': _returnSummary(dateEvents),
        'priceRecent': _returnSummary(recentDateEvents),
        'priceCurrentLike': _returnSummary(currentLike),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='只读运行期权墙历史事件研究')
    parser.add_argument('--underlying', default='510050', choices=('510050',))
    parser.add_argument('--recent-years', type=int, default=3)
    args = parser.parse_args()
    if args.recent_years <= 0:
        parser.error('--recent-years 必须大于 0')
    report = buildReport(loadStudyData(args.underlying), args.recent_years)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())


# 代码更新时间：2026-09-25 16:50
