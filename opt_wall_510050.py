"""五 ETF 独立 OI Wall 教学页（只读、EOD）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import math
import os
from typing import Any, Callable, Iterable, cast

import pymysql
from flask import Flask, jsonify, render_template, request
from flask.json.provider import DefaultJSONProvider
from pymysql.cursors import DictCursor

from logger_config import get_logger
from option_iv import black76_gamma
from rt_monitor.core.config import DB_CONFIG_TUSHARE


logger = get_logger(__name__)

UNDERLYING = '510050.SH'
OPTION_ROOT = 'OP510050.SH'
DEFAULT_UNDERLYING_CODE = '510050'
SUPPORTED_UNDERLYINGS = ('510050', '510300', '510500', '588000', '159915')
STANDARD_PER_UNIT = 10000.0
EXPIRY_LABELS = ('当月', '下月', '当季', '下季')
FLOW_HORIZON_DAYS = {'1D': 1, '3D': 3, '5D': 5}
FLOW_ACCELERATION_RATIO_EPSILON = 1e-9
MAX_PAIN_FORMULA = (
    'Pain(K) = Σ CallOIᵢ × max(K − Kᵢ, 0) × per_unit '
    '+ Σ PutOIᵢ × max(Kᵢ − K, 0) × per_unit'
)
GAMMA_EXPOSURE_FORMULA = (
    'GammaExposure(K, side) = Σ Black76Γ(F, K, T, representative_iv, S) '
    '× OI × per_unit × S² × 1%'
)
GAMMA_IV_SOURCE = 'stock_tushare.opt_iv_panel.representative_iv'
GAMMA_ZONE_COVERAGE = 0.50
GAMMA_ZONE_FORMULA = (
    'GammaZone = 以 Gamma Wall 为中心的最小连续区间，'
    '使区间 Σ|GEX| ÷ 期限合计 Σ|GEX| ≥ 50%'
)
GAMMA_FLIP_FORMULA = (
    'NetGammaProxy(K) = Call |GEX|(K) − Put |GEX|(K)；'
    'GammaFlip = 相邻有效行权价之间 NetGammaProxy = 0 的线性插值'
)
GAMMA_FLIP_METHOD = 'call_minus_put_gex_balance_proxy'


@dataclass(frozen=True)
class UnderlyingConfig:
    """独立期权墙页面支持的 ETF 与期权根代码。"""

    code: str
    optionRoot: str
    label: str


UNDERLYING_CONFIGS = {
    '510050': UnderlyingConfig('510050.SH', 'OP510050.SH', '上证50ETF'),
    '510300': UnderlyingConfig('510300.SH', 'OP510300.SH', '沪深300ETF'),
    '510500': UnderlyingConfig('510500.SH', 'OP510500.SH', '中证500ETF'),
    '588000': UnderlyingConfig('588000.SH', 'OP588000.SH', '科创50ETF'),
    '159915': UnderlyingConfig('159915.SZ', 'OP159915.SZ', '创业板ETF'),
}


class DataUnavailableError(RuntimeError):
    """当前数据不足以生成 OI Wall。"""


def resolveUnderlying(value: object = DEFAULT_UNDERLYING_CODE) -> UnderlyingConfig:
    """把页面/API 输入限制到五个 ETF，避免把任意代码拼进 SQL。"""
    raw = str(value or DEFAULT_UNDERLYING_CODE).strip().upper()
    if raw.endswith('.SH') or raw.endswith('.SZ'):
        raw = raw[:-3]
    config = UNDERLYING_CONFIGS.get(raw)
    if config is None:
        raise ValueError(f'不支持的 ETF 品种：{raw}')
    return config


def underlyingOptions() -> list[dict[str, str]]:
    return [
        {
            'code': code,
            'tsCode': UNDERLYING_CONFIGS[code].code,
            'label': UNDERLYING_CONFIGS[code].label,
        }
        for code in SUPPORTED_UNDERLYINGS
    ]


def _toDate(value: object, fieldName: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        raw = value.strip()
        for fmt in ('%Y-%m-%d', '%Y%m%d'):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    raise ValueError(f'{fieldName} 不是有效日期')


def _finiteFloat(value: object, fieldName: str) -> float:
    if not isinstance(value, (int, float, Decimal, str)):
        raise ValueError(f'{fieldName} 不是有效数值')
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{fieldName} 不是有效数值') from exc
    if not math.isfinite(number):
        raise ValueError(f'{fieldName} 不是有限数值')
    return number


def _jsonNumber(value: float) -> int | float:
    if abs(value - round(value)) < 1e-9:
        return int(round(value))
    return round(value, 8)


def _sideSnapshot(
    strikeMap: dict[float, dict[str, Any]],
    *,
    spot: float,
) -> dict[str, Any]:
    if not strikeMap:
        return {
            'totalOi': 0,
            'wallOi': None,
            'wallStrikes': [],
            'top3Share': None,
            'strikes': [],
        }

    ranked = sorted(strikeMap.items(), key=lambda item: (-item[1]['oi'], item[0]))
    wallOi = ranked[0][1]['oi']
    wallStrikes = sorted(strike for strike, item in ranked if item['oi'] == wallOi)
    totalOi = sum(item['oi'] for item in strikeMap.values())
    top3Oi = sum(item['oi'] for _, item in ranked[:3])
    top3Share = None if totalOi == 0 else top3Oi / totalOi

    denseRanks: dict[float, int] = {}
    previousOi: float | None = None
    currentRank = 0
    for _, item in ranked:
        if previousOi is None or item['oi'] != previousOi:
            currentRank += 1
            previousOi = item['oi']
        denseRanks[item['oi']] = currentRank

    top3Strikes = {strike for strike, _ in ranked[:3]}
    strikes: list[dict[str, object]] = []
    for strike, item in sorted(strikeMap.items()):
        contracts = sorted(item['contracts'], key=lambda contract: contract['tsCode'])
        strikes.append(
            {
                'strike': _jsonNumber(strike),
                'oi': _jsonNumber(item['oi']),
                'distancePct': round((strike / spot - 1.0) * 100.0, 6),
                'rank': denseRanks[item['oi']],
                'isWall': strike in wallStrikes,
                'isTop3': strike in top3Strikes,
                'contracts': contracts,
            }
        )

    return {
        'totalOi': _jsonNumber(totalOi),
        'wallOi': _jsonNumber(wallOi),
        'wallStrikes': [_jsonNumber(strike) for strike in wallStrikes],
        'top3Share': None if top3Share is None else round(top3Share, 8),
        'strikes': strikes,
    }


def calculateMaxPain(
    callOiByStrike: dict[float, float],
    putOiByStrike: dict[float, float],
    *,
    spot: float,
    perUnit: float = STANDARD_PER_UNIT,
) -> dict[str, Any]:
    """扫描候选到期结算价，计算 Call/Put 合计内在价值最小点。

    输入应是已经按行权价聚合、且只保留有效标准合约 OI 的映射。
    `totalPainOiPrice` 的单位是 OI 张数 × 元/份；乘以标准合约乘数后得到元。
    """
    spotValue = _finiteFloat(spot, 'spot')
    perUnitValue = _finiteFloat(perUnit, 'per_unit')
    if spotValue <= 0:
        raise ValueError('spot 必须大于 0')
    if perUnitValue <= 0:
        raise ValueError('per_unit 必须大于 0')

    def normalizeOiMap(oiByStrike: dict[float, float], sideName: str) -> dict[float, float]:
        normalized: dict[float, float] = {}
        for rawStrike, rawOi in oiByStrike.items():
            strike = _finiteFloat(rawStrike, f'{sideName} strike')
            oi = _finiteFloat(rawOi, f'{sideName} OI')
            if strike <= 0:
                raise ValueError(f'{sideName} strike 必须大于 0')
            if oi < 0:
                raise ValueError(f'{sideName} OI 不能为负数')
            normalized[strike] = oi
        return normalized

    normalizedCall = normalizeOiMap(callOiByStrike, 'Call')
    normalizedPut = normalizeOiMap(putOiByStrike, 'Put')
    candidateStrikes = sorted(set(normalizedCall) | set(normalizedPut))
    emptyResult = {
        'available': False,
        'formula': MAX_PAIN_FORMULA,
        'candidateStrikes': [],
        'maxPainStrikes': [],
        'minPainOiPrice': None,
        'minPainYuan': None,
        'candidates': [],
    }
    if not candidateStrikes:
        return emptyResult

    candidates: list[dict[str, object]] = []
    for settlementStrike in candidateStrikes:
        callPainOiPrice = sum(
            oi * max(settlementStrike - strike, 0.0)
            for strike, oi in normalizedCall.items()
        )
        putPainOiPrice = sum(
            oi * max(strike - settlementStrike, 0.0)
            for strike, oi in normalizedPut.items()
        )
        totalPainOiPrice = callPainOiPrice + putPainOiPrice
        candidates.append(
            {
                'strike': _jsonNumber(settlementStrike),
                'distancePct': round((settlementStrike / spotValue - 1.0) * 100.0, 6),
                'callPainOiPrice': _jsonNumber(callPainOiPrice),
                'putPainOiPrice': _jsonNumber(putPainOiPrice),
                'totalPainOiPrice': _jsonNumber(totalPainOiPrice),
                'callPainYuan': _jsonNumber(round(callPainOiPrice * perUnitValue, 2)),
                'putPainYuan': _jsonNumber(round(putPainOiPrice * perUnitValue, 2)),
                'totalPainYuan': _jsonNumber(round(totalPainOiPrice * perUnitValue, 2)),
            }
        )

    candidatePainValues = [
        (
            _finiteFloat(item.get('totalPainOiPrice'), 'totalPainOiPrice'),
            _finiteFloat(item.get('strike'), 'strike'),
        )
        for item in candidates
    ]
    minPainOiPrice = min(value for value, _ in candidatePainValues)
    maxPainStrikes = [
        strike
        for value, strike in candidatePainValues
        if math.isclose(value, minPainOiPrice, abs_tol=1e-9, rel_tol=0.0)
    ]
    maxPainStrikeSet = set(maxPainStrikes)
    for item in candidates:
        item['isMaxPain'] = _finiteFloat(item.get('strike'), 'strike') in maxPainStrikeSet

    return {
        'available': True,
        'formula': MAX_PAIN_FORMULA,
        'candidateStrikes': [_jsonNumber(strike) for strike in candidateStrikes],
        'maxPainStrikes': [_jsonNumber(strike) for strike in maxPainStrikes],
        'minPainOiPrice': _jsonNumber(minPainOiPrice),
        'minPainYuan': _jsonNumber(round(minPainOiPrice * perUnitValue, 2)),
        'candidates': candidates,
    }


def calculateGammaExposure(
    *,
    forward: float,
    strike: float,
    timeToExpiry: float,
    representativeIv: float,
    spot: float,
    oi: float,
    perUnit: float,
) -> dict[str, Any]:
    """用 Black-76 现货 Gamma 计算单个合约的 1% 标的变动暴露。"""
    forwardValue = _finiteFloat(forward, 'forward')
    strikeValue = _finiteFloat(strike, 'strike')
    timeValue = _finiteFloat(timeToExpiry, 'time_to_expiry')
    ivValue = _finiteFloat(representativeIv, 'representative_iv')
    spotValue = _finiteFloat(spot, 'spot')
    oiValue = _finiteFloat(oi, 'oi')
    perUnitValue = _finiteFloat(perUnit, 'per_unit')
    if min(forwardValue, strikeValue, timeValue, ivValue, spotValue, perUnitValue) <= 0:
        raise ValueError('forward、strike、time_to_expiry、IV、spot 和 per_unit 必须大于 0')
    if oiValue < 0:
        raise ValueError('oi 不能为负数')

    gamma = black76_gamma(
        forward=forwardValue,
        strike=strikeValue,
        time_to_expiry=timeValue,
        volatility=ivValue,
        spot=spotValue,
    )
    gammaExposure = gamma * oiValue * perUnitValue * spotValue**2 * 0.01
    return {
        'available': True,
        'gamma': _jsonNumber(gamma),
        'gammaExposure': _jsonNumber(gammaExposure),
        'forward': _jsonNumber(forwardValue),
        'strike': _jsonNumber(strikeValue),
        'timeToExpiry': _jsonNumber(timeValue),
        'representativeIv': _jsonNumber(ivValue),
        'spot': _jsonNumber(spotValue),
        'oi': _jsonNumber(oiValue),
        'perUnit': _jsonNumber(perUnitValue),
    }


def _emptyGammaSideSnapshot() -> dict[str, Any]:
    return {
        'totalGammaExposure': None,
        'wallExposure': None,
        'wallStrikes': [],
        'top1Share': None,
        'strikes': [],
    }


def _gammaSideSnapshot(
    strikeMap: dict[float, dict[str, Any]],
    *,
    spot: float,
) -> dict[str, Any]:
    if not strikeMap:
        return _emptyGammaSideSnapshot()

    totalExposure = sum(float(item['gammaExposure']) for item in strikeMap.values())
    ranked = sorted(
        strikeMap.items(),
        key=lambda item: (-float(item[1]['gammaExposure']), item[0]),
    )
    wallExposure = float(ranked[0][1]['gammaExposure'])
    wallStrikes = (
        sorted(
            strike
            for strike, item in ranked
            if math.isclose(float(item['gammaExposure']), wallExposure, abs_tol=1e-9)
        )
        if totalExposure > 0
        else []
    )
    strikes: list[dict[str, Any]] = []
    for strike, item in sorted(strikeMap.items()):
        strikes.append(
            {
                'strike': _jsonNumber(strike),
                'distancePct': round((strike / spot - 1.0) * 100.0, 6),
                'gammaExposure': _jsonNumber(float(item['gammaExposure'])),
                'isWall': strike in wallStrikes,
                'contracts': sorted(
                    item['contracts'],
                    key=lambda contract: contract['tsCode'],
                ),
            }
        )
    return {
        'totalGammaExposure': _jsonNumber(totalExposure),
        'wallExposure': _jsonNumber(wallExposure) if totalExposure > 0 else 0,
        'wallStrikes': [_jsonNumber(strike) for strike in wallStrikes],
        'top1Share': (
            round(wallExposure / totalExposure, 8) if totalExposure > 0 else None
        ),
        'strikes': strikes,
    }


def _emptyGammaZoneSnapshot() -> dict[str, Any]:
    return {
        'available': False,
        'formula': GAMMA_ZONE_FORMULA,
        'coverageThreshold': GAMMA_ZONE_COVERAGE,
        'totalGammaExposure': None,
        'thresholdGammaExposure': None,
        'zoneGammaExposure': None,
        'zoneShare': None,
        'wallStrikes': [],
        'zoneStrikes': [],
        'lowerStrike': None,
        'upperStrike': None,
        'lowerDistancePct': None,
        'upperDistancePct': None,
        'reason': '没有可用的正数合计 |GEX|。',
    }


def calculateGammaZone(
    rows: Iterable[dict[str, object]],
    *,
    spot: float,
    coverageThreshold: float = GAMMA_ZONE_COVERAGE,
) -> dict[str, Any]:
    """寻找覆盖指定比例合计 |GEX| 的最小连续 Gamma 集中区。"""
    spotValue = _finiteFloat(spot, 'spot')
    if spotValue <= 0:
        raise ValueError('spot 必须大于 0')
    thresholdValue = _finiteFloat(coverageThreshold, 'coverageThreshold')
    if thresholdValue <= 0 or thresholdValue > 1:
        raise ValueError('coverageThreshold 必须大于 0 且不超过 1')

    parsedRows: list[dict[str, Any]] = []
    seenStrikes: set[float] = set()
    for row in rows:
        strike = _finiteFloat(row.get('strike'), 'strike')
        exposure = _finiteFloat(
            row.get('totalGammaExposure'),
            'totalGammaExposure',
        )
        if strike <= 0 or exposure < 0:
            raise ValueError('Gamma Zone 的行权价必须大于 0，合计 |GEX| 不能为负数')
        if strike in seenStrikes:
            raise ValueError(f'Gamma Zone 存在重复行权价：{strike}')
        seenStrikes.add(strike)
        parsedRows.append(
            {
                'strike': strike,
                'totalGammaExposure': exposure,
                'isWall': bool(row.get('isWall')),
            }
        )

    parsedRows.sort(key=lambda item: float(item['strike']))
    totalExposure = sum(float(item['totalGammaExposure']) for item in parsedRows)
    if not parsedRows or totalExposure <= 0:
        return _emptyGammaZoneSnapshot()

    maxExposure = max(float(item['totalGammaExposure']) for item in parsedRows)
    wallStrikes = [
        float(item['strike'])
        for item in parsedRows
        if item['isWall']
        or math.isclose(
            float(item['totalGammaExposure']),
            maxExposure,
            abs_tol=1e-9,
            rel_tol=0.0,
        )
    ]
    wallStrikes = sorted(set(wallStrikes))
    wallIndices = [
        index
        for index, item in enumerate(parsedRows)
        if float(item['strike']) in set(wallStrikes)
    ]
    if not wallIndices:
        return _emptyGammaZoneSnapshot()

    thresholdExposure = totalExposure * thresholdValue
    wallStart = min(wallIndices)
    wallEnd = max(wallIndices)
    prefix = [0.0]
    for item in parsedRows:
        prefix.append(prefix[-1] + float(item['totalGammaExposure']))

    bestCandidate: tuple[int, float, int, int, float] | None = None
    for start in range(0, wallStart + 1):
        for end in range(wallEnd, len(parsedRows)):
            zoneExposure = prefix[end + 1] - prefix[start]
            if zoneExposure + 1e-9 < thresholdExposure:
                continue
            zoneShare = zoneExposure / totalExposure
            candidate = (
                end - start + 1,
                -zoneShare,
                start,
                end,
                zoneExposure,
            )
            if bestCandidate is None or candidate < bestCandidate:
                bestCandidate = candidate

    if bestCandidate is None:
        return _emptyGammaZoneSnapshot()

    _, _, start, end, zoneExposure = bestCandidate
    zoneStrikes = [float(item['strike']) for item in parsedRows[start : end + 1]]
    lowerStrike = zoneStrikes[0]
    upperStrike = zoneStrikes[-1]
    return {
        'available': True,
        'formula': GAMMA_ZONE_FORMULA,
        'coverageThreshold': _jsonNumber(thresholdValue),
        'totalGammaExposure': _jsonNumber(totalExposure),
        'thresholdGammaExposure': _jsonNumber(thresholdExposure),
        'zoneGammaExposure': _jsonNumber(zoneExposure),
        'zoneShare': round(zoneExposure / totalExposure, 8),
        'wallStrikes': [_jsonNumber(strike) for strike in wallStrikes],
        'zoneStrikes': [_jsonNumber(strike) for strike in zoneStrikes],
        'lowerStrike': _jsonNumber(lowerStrike),
        'upperStrike': _jsonNumber(upperStrike),
        'lowerDistancePct': round((lowerStrike / spotValue - 1.0) * 100.0, 6),
        'upperDistancePct': round((upperStrike / spotValue - 1.0) * 100.0, 6),
        'reason': None,
    }


def _emptyGammaFlipSnapshot() -> dict[str, Any]:
    return {
        'available': False,
        'estimate': True,
        'scenario': True,
        'method': GAMMA_FLIP_METHOD,
        'formula': GAMMA_FLIP_FORMULA,
        'strike': None,
        'distancePct': None,
        'lowerStrike': None,
        'upperStrike': None,
        'lowerNetGammaProxy': None,
        'upperNetGammaProxy': None,
        'interpolationRatio': None,
        'exact': False,
        'validStrikeCount': 0,
        'candidates': [],
        'reason': '没有两个双侧有效行权价之间的 NetGammaProxy 变号。',
    }


def calculateGammaFlip(
    rows: Iterable[dict[str, object]],
    *,
    spot: float,
) -> dict[str, Any]:
    """计算 Call−Put GEX 平衡代理的零点，不把它解释为 Dealer 净 Gamma。"""
    spotValue = _finiteFloat(spot, 'spot')
    if spotValue <= 0:
        raise ValueError('spot 必须大于 0')

    parsedRows: list[dict[str, float]] = []
    seenStrikes: set[float] = set()
    for row in rows:
        strike = _finiteFloat(row.get('strike'), 'strike')
        if strike <= 0:
            raise ValueError('Gamma Flip 的行权价必须大于 0')
        if strike in seenStrikes:
            raise ValueError(f'Gamma Flip 存在重复行权价：{strike}')
        seenStrikes.add(strike)
        callRaw = row.get('callGammaExposure')
        putRaw = row.get('putGammaExposure')
        if callRaw is None or putRaw is None:
            continue
        callExposure = _finiteFloat(callRaw, 'callGammaExposure')
        putExposure = _finiteFloat(putRaw, 'putGammaExposure')
        if callExposure < 0 or putExposure < 0:
            raise ValueError('Call/Put Gamma 暴露不能为负数')
        if callExposure + putExposure <= 0:
            continue
        parsedRows.append(
            {
                'strike': strike,
                'net': callExposure - putExposure,
            }
        )

    parsedRows.sort(key=lambda item: item['strike'])
    if not parsedRows:
        return _emptyGammaFlipSnapshot()

    def makeCandidate(
        lower: dict[str, float],
        upper: dict[str, float],
        *,
        exact: bool = False,
    ) -> dict[str, Any]:
        lowerStrike = lower['strike']
        upperStrike = upper['strike']
        lowerNet = lower['net']
        upperNet = upper['net']
        if exact:
            flipStrike = lowerStrike
            ratio = 0.0
        else:
            denominator = upperNet - lowerNet
            if math.isclose(denominator, 0.0, abs_tol=1e-12, rel_tol=0.0):
                raise ValueError('Gamma Flip 插值分母不能为 0')
            ratio = -lowerNet / denominator
            flipStrike = lowerStrike + ratio * (upperStrike - lowerStrike)
        return {
            'strike': _jsonNumber(flipStrike),
            'distancePct': round((flipStrike / spotValue - 1.0) * 100.0, 6),
            'lowerStrike': _jsonNumber(lowerStrike),
            'upperStrike': _jsonNumber(upperStrike),
            'lowerNetGammaProxy': _jsonNumber(lowerNet),
            'upperNetGammaProxy': _jsonNumber(upperNet),
            'interpolationRatio': round(ratio, 8),
            'exact': exact,
        }

    candidates: list[dict[str, Any]] = []
    for item in parsedRows:
        if math.isclose(item['net'], 0.0, abs_tol=1e-9, rel_tol=0.0):
            candidates.append(makeCandidate(item, item, exact=True))
    for lower, upper in zip(parsedRows, parsedRows[1:]):
        if lower['net'] * upper['net'] < 0:
            candidates.append(makeCandidate(lower, upper))

    if not candidates:
        result = _emptyGammaFlipSnapshot()
        result['validStrikeCount'] = len(parsedRows)
        return result

    candidates.sort(
        key=lambda item: (
            abs(float(item['strike']) - spotValue),
            float(item['strike']),
        )
    )
    primary = candidates[0]
    return {
        'available': True,
        'estimate': True,
        'scenario': True,
        'method': GAMMA_FLIP_METHOD,
        'formula': GAMMA_FLIP_FORMULA,
        **primary,
        'validStrikeCount': len(parsedRows),
        'candidates': candidates,
        'reason': None,
    }


def _gammaAggregateSnapshot(
    callBuckets: dict[float, dict[str, Any]],
    putBuckets: dict[float, dict[str, Any]],
    *,
    spot: float,
) -> dict[str, Any]:
    strikeSet = sorted(set(callBuckets) | set(putBuckets))
    if not strikeSet:
        return {
            'totalGammaExposure': None,
            'wallExposure': None,
            'wallStrikes': [],
            'top1Share': None,
            'strikes': [],
            'zone': _emptyGammaZoneSnapshot(),
            'flip': _emptyGammaFlipSnapshot(),
        }

    totalExposure = 0.0
    rows: list[dict[str, Any]] = []
    for strike in strikeSet:
        callExposure = (
            None
            if strike not in callBuckets
            else float(callBuckets[strike]['gammaExposure'])
        )
        putExposure = (
            None
            if strike not in putBuckets
            else float(putBuckets[strike]['gammaExposure'])
        )
        totalStrikeExposure = (callExposure or 0.0) + (putExposure or 0.0)
        totalExposure += totalStrikeExposure
        rows.append(
            {
                'strike': _jsonNumber(strike),
                'distancePct': round((strike / spot - 1.0) * 100.0, 6),
                'callGammaExposure': (
                    None if callExposure is None else _jsonNumber(callExposure)
                ),
                'putGammaExposure': (
                    None if putExposure is None else _jsonNumber(putExposure)
                ),
                'totalGammaExposure': _jsonNumber(totalStrikeExposure),
                'netGammaProxy': (
                    None
                    if callExposure is None or putExposure is None
                    else _jsonNumber(callExposure - putExposure)
                ),
            }
        )

    wallExposure = max(float(item['totalGammaExposure']) for item in rows)
    wallStrikes = (
        [
            float(item['strike'])
            for item in rows
            if math.isclose(
                float(item['totalGammaExposure']),
                wallExposure,
                abs_tol=1e-9,
            )
        ]
        if totalExposure > 0
        else []
    )
    wallStrikeSet = set(wallStrikes)
    for item in rows:
        item['isWall'] = float(item['strike']) in wallStrikeSet
    zoneSnapshot = calculateGammaZone(rows, spot=spot)
    zoneStrikeSet = set(float(strike) for strike in zoneSnapshot['zoneStrikes'])
    for item in rows:
        item['isGammaZone'] = float(item['strike']) in zoneStrikeSet
    flipSnapshot = calculateGammaFlip(rows, spot=spot)
    flipBracketSet = {
        float(flipSnapshot['lowerStrike']),
        float(flipSnapshot['upperStrike']),
    } if flipSnapshot['available'] else set()
    for item in rows:
        item['isGammaFlipBracket'] = float(item['strike']) in flipBracketSet
    return {
        'totalGammaExposure': _jsonNumber(totalExposure),
        'wallExposure': _jsonNumber(wallExposure) if totalExposure > 0 else 0,
        'wallStrikes': [_jsonNumber(strike) for strike in wallStrikes],
        'top1Share': (
            round(wallExposure / totalExposure, 8) if totalExposure > 0 else None
        ),
        'strikes': rows,
        'zone': zoneSnapshot,
        'flip': flipSnapshot,
    }


def _gammaIvMap(
    ivRows: Iterable[dict[str, object]],
    *,
    dataDate: date,
) -> tuple[dict[tuple[date, str], dict[str, float]], int]:
    ivByKey: dict[tuple[date, str], dict[str, float]] = {}
    invalidRows = 0
    for row in ivRows:
        try:
            tradeDate = _toDate(row.get('trade_date'), 'trade_date')
            maturityDate = _toDate(row.get('maturity_date'), 'maturity_date')
            representativeIv = _finiteFloat(
                row.get('representative_iv'),
                'representative_iv',
            )
            forward = _finiteFloat(row.get('forward'), 'forward')
        except ValueError:
            invalidRows += 1
            continue
        callPut = str(row.get('call_put') or '').strip().upper()
        if tradeDate != dataDate or callPut not in {'C', 'P'}:
            invalidRows += 1
            continue
        if representativeIv <= 0 or forward <= 0:
            invalidRows += 1
            continue
        ivByKey[(maturityDate, callPut)] = {
            'representativeIv': representativeIv,
            'forward': forward,
        }
    return ivByKey, invalidRows


def buildGammaWallSnapshot(
    optionRows: Iterable[dict[str, object]],
    ivRows: Iterable[dict[str, object]],
    *,
    dataDate: date,
    spot: float,
    underlying: str = UNDERLYING,
) -> dict[str, Any]:
    """按期限与行权价聚合 EOD 代表 IV 近似的 Absolute Gamma Exposure。"""
    dataDate = _toDate(dataDate, 'dataDate')
    spotValue = _finiteFloat(spot, 'spot')
    if spotValue <= 0:
        raise ValueError('spot 必须大于 0')

    rows = list(optionRows)
    ivByKey, invalidIvRows = _gammaIvMap(ivRows, dataDate=dataDate)
    parsedRows: list[tuple[dict[str, object], date]] = []
    maturitySet: set[date] = set()
    expiredRows = 0
    for row in rows:
        rowTradeDate = _toDate(row.get('trade_date'), 'trade_date')
        if rowTradeDate != dataDate:
            raise ValueError('期权行与共同数据日不一致')
        maturityDate = _toDate(row.get('maturity_date'), 'maturity_date')
        if maturityDate < dataDate:
            expiredRows += 1
            continue
        parsedRows.append((row, maturityDate))
        maturitySet.add(maturityDate)

    selectedMaturities = sorted(maturitySet)[:4]
    selectedSet = set(selectedMaturities)
    buckets: dict[date, dict[str, dict[float, dict[str, Any]]]] = {
        maturity: {'C': {}, 'P': {}} for maturity in selectedMaturities
    }
    expiryQuality: dict[date, dict[str, int]] = {
        maturity: {
            'optionRows': 0,
            'validContracts': 0,
            'standardRows': 0,
            'adjustedRows': 0,
            'missingOiRows': 0,
            'missingIvRows': 0,
            'invalidRows': 0,
        }
        for maturity in selectedMaturities
    }
    standardRows = 0
    adjustedRows = 0
    missingOiRows = 0
    missingIvRows = 0
    invalidRows = 0
    validContracts = 0

    for row, maturityDate in parsedRows:
        if maturityDate not in selectedSet:
            continue
        maturityQuality = expiryQuality[maturityDate]
        maturityQuality['optionRows'] += 1
        try:
            perUnit = _finiteFloat(row.get('per_unit'), 'per_unit')
            strike = _finiteFloat(row.get('exercise_price'), 'exercise_price')
        except ValueError:
            invalidRows += 1
            maturityQuality['invalidRows'] += 1
            continue
        if perUnit <= 0 or strike <= 0:
            invalidRows += 1
            maturityQuality['invalidRows'] += 1
            continue
        tsCode = str(row.get('ts_code') or '').strip()
        callPut = str(row.get('call_put') or '').strip().upper()
        if not tsCode or callPut not in {'C', 'P'}:
            invalidRows += 1
            maturityQuality['invalidRows'] += 1
            continue
        if abs(perUnit - STANDARD_PER_UNIT) < 1e-6:
            standardRows += 1
            maturityQuality['standardRows'] += 1
            contractClass = 'standard'
        else:
            adjustedRows += 1
            maturityQuality['adjustedRows'] += 1
            contractClass = 'adjusted'

        oiRaw = row.get('oi')
        if oiRaw is None:
            missingOiRows += 1
            maturityQuality['missingOiRows'] += 1
            continue
        try:
            oi = _finiteFloat(oiRaw, 'oi')
        except ValueError:
            missingOiRows += 1
            maturityQuality['missingOiRows'] += 1
            continue
        if oi < 0:
            invalidRows += 1
            maturityQuality['invalidRows'] += 1
            continue

        ivInfo = ivByKey.get((maturityDate, callPut))
        if ivInfo is None:
            missingIvRows += 1
            maturityQuality['missingIvRows'] += 1
            continue
        timeToExpiry = max((maturityDate - dataDate).days, 1) / 365.0
        try:
            gammaResult = calculateGammaExposure(
                forward=ivInfo['forward'],
                strike=strike,
                timeToExpiry=timeToExpiry,
                representativeIv=ivInfo['representativeIv'],
                spot=spotValue,
                oi=oi,
                perUnit=perUnit,
            )
        except ValueError:
            invalidRows += 1
            maturityQuality['invalidRows'] += 1
            continue

        strikeBucket = buckets[maturityDate][callPut].setdefault(
            strike,
            {'gammaExposure': 0.0, 'contracts': []},
        )
        strikeBucket['gammaExposure'] += float(gammaResult['gammaExposure'])
        strikeBucket['contracts'].append(
            {
                'tsCode': tsCode,
                'oi': _jsonNumber(oi),
                'perUnit': _jsonNumber(perUnit),
                'contractClass': contractClass,
                'gamma': gammaResult['gamma'],
                'gammaExposure': gammaResult['gammaExposure'],
                'representativeIv': gammaResult['representativeIv'],
                'forward': gammaResult['forward'],
                'timeToExpiry': gammaResult['timeToExpiry'],
            }
        )
        validContracts += 1
        maturityQuality['validContracts'] += 1

    expiries: list[dict[str, Any]] = []
    for index, maturityDate in enumerate(selectedMaturities):
        callBuckets = buckets[maturityDate]['C']
        putBuckets = buckets[maturityDate]['P']
        callSnapshot = _gammaSideSnapshot(callBuckets, spot=spotValue)
        putSnapshot = _gammaSideSnapshot(putBuckets, spot=spotValue)
        aggregateSnapshot = _gammaAggregateSnapshot(
            callBuckets,
            putBuckets,
            spot=spotValue,
        )
        expiries.append(
            {
                'available': bool(callBuckets or putBuckets),
                'estimate': True,
                'formula': GAMMA_EXPOSURE_FORMULA,
                'ivSource': GAMMA_IV_SOURCE,
                'maturityDate': maturityDate.isoformat(),
                'expiryLabel': EXPIRY_LABELS[index],
                'dteCalendar': (maturityDate - dataDate).days,
                'iv': {
                    'call': (
                        None
                        if (maturityDate, 'C') not in ivByKey
                        else _jsonNumber(ivByKey[(maturityDate, 'C')]['representativeIv'])
                    ),
                    'put': (
                        None
                        if (maturityDate, 'P') not in ivByKey
                        else _jsonNumber(ivByKey[(maturityDate, 'P')]['representativeIv'])
                    ),
                    'forward': (
                        _jsonNumber(ivByKey[(maturityDate, 'C')]['forward'])
                        if (maturityDate, 'C') in ivByKey
                        else (
                            _jsonNumber(ivByKey[(maturityDate, 'P')]['forward'])
                            if (maturityDate, 'P') in ivByKey
                            else None
                        )
                    ),
                    'timeToExpiry': _jsonNumber(
                        max((maturityDate - dataDate).days, 1) / 365.0
                    ),
                },
                'quality': dict(expiryQuality[maturityDate]),
                'call': callSnapshot,
                'put': putSnapshot,
                'aggregate': aggregateSnapshot,
            }
        )

    warnings: list[str] = []
    if missingOiRows:
        warnings.append(f'{missingOiRows} 条 Gamma 合约 OI 缺失，已排除且未补零。')
    if missingIvRows:
        warnings.append(f'{missingIvRows} 条 Gamma 合约缺少同日代表 IV，已排除且未补值。')
    if adjustedRows:
        warnings.append(f'{adjustedRows} 条调整合约按自身 per_unit 纳入 Gamma 暴露。')
    if invalidIvRows:
        warnings.append(f'{invalidIvRows} 条代表 IV 记录无效，已排除。')
    if invalidRows:
        warnings.append(f'{invalidRows} 条 Gamma 合约字段无效，已排除。')
    if len(selectedMaturities) < 4:
        warnings.append(f'仅找到 {len(selectedMaturities)} 个有数据的未到期期限。')

    return {
        'available': bool(validContracts),
        'estimate': True,
        'formula': GAMMA_EXPOSURE_FORMULA,
        'ivSource': GAMMA_IV_SOURCE,
        'underlying': underlying,
        'dataDate': dataDate.isoformat(),
        'spot': _jsonNumber(spotValue),
        'expiries': expiries,
        'quality': {
            'optionRows': len(rows),
            'validContracts': validContracts,
            'standardRows': standardRows,
            'adjustedRows': adjustedRows,
            'missingOiRows': missingOiRows,
            'missingIvRows': missingIvRows,
            'invalidIvRows': invalidIvRows,
            'invalidRows': invalidRows,
            'expiredRows': expiredRows,
            'warnings': warnings,
        },
    }


def buildOiWallSnapshot(
    optionRows: Iterable[dict[str, object]],
    *,
    dataDate: date,
    spot: float,
    underlying: str = UNDERLYING,
    ivRows: Iterable[dict[str, object]] | None = None,
) -> dict[str, Any]:
    """把同一交易日的合约行转换为可追溯的四期限 OI Wall 快照。"""
    dataDate = _toDate(dataDate, 'dataDate')
    spotValue = _finiteFloat(spot, 'spot')
    if spotValue <= 0:
        raise ValueError('spot 必须大于 0')

    rows = list(optionRows)
    parsedRows: list[tuple[dict[str, object], date]] = []
    maturitySet: set[date] = set()
    expiredRows = 0
    for row in rows:
        rowTradeDate = _toDate(row.get('trade_date'), 'trade_date')
        if rowTradeDate != dataDate:
            raise ValueError('期权行与共同数据日不一致')
        maturityDate = _toDate(row.get('maturity_date'), 'maturity_date')
        if maturityDate < dataDate:
            expiredRows += 1
            continue
        parsedRows.append((row, maturityDate))
        maturitySet.add(maturityDate)

    selectedMaturities = sorted(maturitySet)[:4]
    selectedSet = set(selectedMaturities)
    buckets: dict[date, dict[str, dict[float, dict[str, Any]]]] = {
        maturity: {'C': {}, 'P': {}} for maturity in selectedMaturities
    }

    standardRows = 0
    adjustedRows = 0
    missingOiRows = 0
    invalidRows = 0
    adjustedContracts: list[dict[str, object]] = []

    for row, maturityDate in parsedRows:
        if maturityDate not in selectedSet:
            continue
        try:
            perUnit = _finiteFloat(row.get('per_unit'), 'per_unit')
            strike = _finiteFloat(row.get('exercise_price'), 'exercise_price')
        except ValueError:
            invalidRows += 1
            continue
        if perUnit <= 0 or strike <= 0:
            invalidRows += 1
            continue

        tsCode = str(row.get('ts_code') or '').strip()
        callPut = str(row.get('call_put') or '').strip().upper()
        if not tsCode or callPut not in {'C', 'P'}:
            invalidRows += 1
            continue

        oiRaw = row.get('oi')
        if abs(perUnit - STANDARD_PER_UNIT) >= 1e-6:
            adjustedRows += 1
            adjustedOi: float | None = None
            if oiRaw is not None:
                try:
                    adjustedOi = _finiteFloat(oiRaw, 'oi')
                except ValueError:
                    adjustedOi = None
            adjustedContracts.append(
                {
                    'tsCode': tsCode,
                    'maturityDate': maturityDate.isoformat(),
                    'callPut': callPut,
                    'strike': _jsonNumber(strike),
                    'oi': None if adjustedOi is None else _jsonNumber(adjustedOi),
                    'perUnit': _jsonNumber(perUnit),
                    'underlyingUnits': (
                        None if adjustedOi is None else _jsonNumber(adjustedOi * perUnit)
                    ),
                }
            )
            continue

        standardRows += 1
        if oiRaw is None:
            missingOiRows += 1
            continue
        try:
            oi = _finiteFloat(oiRaw, 'oi')
        except ValueError:
            missingOiRows += 1
            continue
        if oi < 0:
            invalidRows += 1
            continue

        strikeBucket = buckets[maturityDate][callPut].setdefault(
            strike,
            {'oi': 0.0, 'contracts': []},
        )
        strikeBucket['oi'] += oi
        strikeBucket['contracts'].append(
            {
                'tsCode': tsCode,
                'oi': _jsonNumber(oi),
                'perUnit': _jsonNumber(perUnit),
            }
        )

    gammaSnapshot = buildGammaWallSnapshot(
        rows,
        [] if ivRows is None else ivRows,
        dataDate=dataDate,
        spot=spotValue,
        underlying=underlying,
    )

    expiries: list[dict[str, object]] = []
    for index, maturityDate in enumerate(selectedMaturities):
        callSnapshot = _sideSnapshot(buckets[maturityDate]['C'], spot=spotValue)
        putSnapshot = _sideSnapshot(buckets[maturityDate]['P'], spot=spotValue)
        maxPain = calculateMaxPain(
            {
                strike: float(item['oi'])
                for strike, item in buckets[maturityDate]['C'].items()
            },
            {
                strike: float(item['oi'])
                for strike, item in buckets[maturityDate]['P'].items()
            },
            spot=spotValue,
        )
        expiries.append(
            {
                'maturityDate': maturityDate.isoformat(),
                'expiryLabel': EXPIRY_LABELS[index],
                'dteCalendar': (maturityDate - dataDate).days,
                'totalOi': callSnapshot['totalOi'] + putSnapshot['totalOi'],
                'call': callSnapshot,
                'put': putSnapshot,
                'maxPain': maxPain,
                'gamma': gammaSnapshot['expiries'][index],
            }
        )

    warnings: list[str] = []
    if missingOiRows:
        warnings.append(f'{missingOiRows} 条标准合约 OI 缺失，已排除且未补零。')
    if adjustedRows:
        warnings.append(f'{adjustedRows} 条调整合约未并入标准合约墙位。')
    if invalidRows:
        warnings.append(f'{invalidRows} 条合约字段无效，已排除。')
    if len(selectedMaturities) < 4:
        warnings.append(f'仅找到 {len(selectedMaturities)} 个有数据的未到期期限。')

    return {
        'underlying': underlying,
        'dataDate': dataDate.isoformat(),
        'spot': _jsonNumber(spotValue),
        'spotField': 'close_qfq',
        'expiries': expiries,
        'gammaQuality': gammaSnapshot['quality'],
        'quality': {
            'source': 'stock_tushare.opt_basic + opt_daily + etf_daily',
            'optionRows': len(rows),
            'standardRows': standardRows,
            'adjustedRows': adjustedRows,
            'missingOiRows': missingOiRows,
            'invalidRows': invalidRows,
            'expiredRows': expiredRows,
            'dateAligned': True,
            'adjustedContracts': adjustedContracts,
            'warnings': warnings,
        },
    }


def _flowSideSnapshot(
    strikeMap: dict[float, dict[str, Any]],
    *,
    spot: float,
    days: int,
) -> dict[str, Any]:
    if days <= 0:
        raise ValueError('Flow Velocity 的交易日数必须大于 0')
    if not strikeMap:
        return {
            'totalDeltaOi': 0,
            'positiveFlowTotalOi': 0,
            'positiveFlowVelocityOi': 0,
            'negativeFlowTotalOi': 0,
            'negativeFlowVelocityOi': 0,
            'positiveWallOi': None,
            'positiveWallStrikes': [],
            'positiveWallConcentration': None,
            'positiveWallVelocityOi': None,
            'negativeWallOi': None,
            'negativeWallStrikes': [],
            'negativeWallConcentration': None,
            'negativeWallVelocityOi': None,
            'strikes': [],
        }

    deltaValues = [float(item['deltaOi']) for item in strikeMap.values()]
    positiveValues = [value for value in deltaValues if value > 0]
    negativeValues = [value for value in deltaValues if value < 0]
    positiveFlowTotalOi = sum(positiveValues)
    negativeFlowTotalOi = sum(negativeValues)
    positiveWallOi = max(positiveValues) if positiveValues else None
    negativeWallOi = min(negativeValues) if negativeValues else None
    positiveWallConcentration = (
        None
        if positiveWallOi is None or positiveFlowTotalOi == 0
        else positiveWallOi / positiveFlowTotalOi
    )
    negativeWallConcentration = (
        None
        if negativeWallOi is None or negativeFlowTotalOi == 0
        else abs(negativeWallOi) / abs(negativeFlowTotalOi)
    )
    positiveWallVelocityOi = None if positiveWallOi is None else positiveWallOi / days
    negativeWallVelocityOi = None if negativeWallOi is None else negativeWallOi / days
    positiveWallStrikes = sorted(
        strike for strike, item in strikeMap.items()
        if positiveWallOi is not None and item['deltaOi'] == positiveWallOi
    )
    negativeWallStrikes = sorted(
        strike for strike, item in strikeMap.items()
        if negativeWallOi is not None and item['deltaOi'] == negativeWallOi
    )

    strikes: list[dict[str, object]] = []
    for strike, item in sorted(strikeMap.items()):
        contracts = sorted(item['contracts'], key=lambda contract: contract['tsCode'])
        deltaOi = float(item['deltaOi'])
        currentOi = float(item['currentOi'])
        baselineOi = float(item['baselineOi'])
        strikes.append(
            {
                'strike': _jsonNumber(strike),
                'currentOi': _jsonNumber(currentOi),
                'baselineOi': _jsonNumber(baselineOi),
                'deltaOi': _jsonNumber(deltaOi),
                'flowVelocityOi': _jsonNumber(deltaOi / days),
                'changeRate': None if baselineOi == 0 else round(deltaOi / baselineOi, 8),
                'distancePct': round((strike / spot - 1.0) * 100.0, 6),
                'isPositiveWall': strike in positiveWallStrikes,
                'isNegativeWall': strike in negativeWallStrikes,
                'contracts': contracts,
            }
        )

    return {
        'totalDeltaOi': _jsonNumber(sum(deltaValues)),
        'positiveFlowTotalOi': _jsonNumber(positiveFlowTotalOi),
        'positiveFlowVelocityOi': _jsonNumber(positiveFlowTotalOi / days),
        'negativeFlowTotalOi': _jsonNumber(negativeFlowTotalOi),
        'negativeFlowVelocityOi': _jsonNumber(negativeFlowTotalOi / days),
        'positiveWallOi': None if positiveWallOi is None else _jsonNumber(positiveWallOi),
        'positiveWallStrikes': [_jsonNumber(strike) for strike in positiveWallStrikes],
        'positiveWallConcentration': (
            None
            if positiveWallConcentration is None
            else round(positiveWallConcentration, 8)
        ),
        'positiveWallVelocityOi': (
            None
            if positiveWallVelocityOi is None
            else _jsonNumber(positiveWallVelocityOi)
        ),
        'negativeWallOi': None if negativeWallOi is None else _jsonNumber(negativeWallOi),
        'negativeWallStrikes': [_jsonNumber(strike) for strike in negativeWallStrikes],
        'negativeWallConcentration': (
            None
            if negativeWallConcentration is None
            else round(negativeWallConcentration, 8)
        ),
        'negativeWallVelocityOi': (
            None
            if negativeWallVelocityOi is None
            else _jsonNumber(negativeWallVelocityOi)
        ),
        'strikes': strikes,
    }


def _flowAccelerationSideSnapshot(
    oneDaySide: dict[str, Any],
    threeDaySide: dict[str, Any],
) -> tuple[dict[str, Any], int, int]:
    """比较同一到期日、同一方向的 1D 与 3D 日均 Flow 速度。"""
    oneDayItems = {
        float(item['strike']): item
        for item in oneDaySide.get('strikes', [])
    }
    threeDayItems = {
        float(item['strike']): item
        for item in threeDaySide.get('strikes', [])
    }
    commonStrikes = sorted(set(oneDayItems) & set(threeDayItems))
    missing3dRows = len(set(oneDayItems) - set(threeDayItems))
    missing1dRows = len(set(threeDayItems) - set(oneDayItems))

    accelerationItems: list[dict[str, object]] = []
    for strike in commonStrikes:
        oneDayItem = oneDayItems[strike]
        threeDayItem = threeDayItems[strike]
        oneDayVelocity = _finiteFloat(oneDayItem.get('flowVelocityOi'), '1D flowVelocityOi')
        threeDayVelocity = _finiteFloat(threeDayItem.get('flowVelocityOi'), '3D flowVelocityOi')
        accelerationOi = oneDayVelocity - threeDayVelocity
        accelerationRatio = (
            None
            if abs(threeDayVelocity) < FLOW_ACCELERATION_RATIO_EPSILON
            else oneDayVelocity / threeDayVelocity
        )
        accelerationItems.append(
            {
                'strike': _jsonNumber(strike),
                'flowVelocity1DOi': _jsonNumber(oneDayVelocity),
                'flowVelocity3DOi': _jsonNumber(threeDayVelocity),
                'flowAccelerationOi': _jsonNumber(accelerationOi),
                'flowAccelerationRatio': (
                    None
                    if accelerationRatio is None
                    else _jsonNumber(accelerationRatio)
                ),
            }
        )

    accelerationValues = [
        _finiteFloat(item.get('flowAccelerationOi'), 'flowAccelerationOi')
        for item in accelerationItems
    ]
    positiveValues = [value for value in accelerationValues if value > 0]
    negativeValues = [value for value in accelerationValues if value < 0]
    positiveWallOi = max(positiveValues) if positiveValues else None
    negativeWallOi = min(negativeValues) if negativeValues else None
    positiveWallStrikes = sorted(
        _finiteFloat(item.get('strike'), 'strike')
        for item in accelerationItems
        if positiveWallOi is not None
        and _finiteFloat(item.get('flowAccelerationOi'), 'flowAccelerationOi') == positiveWallOi
    )
    negativeWallStrikes = sorted(
        _finiteFloat(item.get('strike'), 'strike')
        for item in accelerationItems
        if negativeWallOi is not None
        and _finiteFloat(item.get('flowAccelerationOi'), 'flowAccelerationOi') == negativeWallOi
    )
    for item in accelerationItems:
        strike = _finiteFloat(item.get('strike'), 'strike')
        item['isPositiveAccelerationWall'] = strike in positiveWallStrikes
        item['isNegativeAccelerationWall'] = strike in negativeWallStrikes

    return (
        {
            'positiveAccelerationWallOi': (
                None if positiveWallOi is None else _jsonNumber(positiveWallOi)
            ),
            'positiveAccelerationWallStrikes': [
                _jsonNumber(strike) for strike in positiveWallStrikes
            ],
            'negativeAccelerationWallOi': (
                None if negativeWallOi is None else _jsonNumber(negativeWallOi)
            ),
            'negativeAccelerationWallStrikes': [
                _jsonNumber(strike) for strike in negativeWallStrikes
            ],
            'strikes': accelerationItems,
        },
        missing3dRows,
        missing1dRows,
    )


def _flowAccelerationSnapshot(
    horizons: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """生成 1D 对 3D 的 Flow Acceleration，不补齐缺失的跨周期行权价。"""
    oneDay = horizons.get('1D') or {}
    threeDay = horizons.get('3D') or {}
    baselineDates = {
        '1D': oneDay.get('baselineDate'),
        '3D': threeDay.get('baselineDate'),
    }
    quality: dict[str, Any] = {
        'pairedRows': 0,
        'missing3dRows': 0,
        'missing1dRows': 0,
        'warnings': [],
    }
    if not oneDay.get('available') or not threeDay.get('available'):
        quality['warnings'].append('Flow Acceleration 需要同时具备 1D 与 3D 的可用基线。')
        return {
            'available': False,
            'baselineDates': baselineDates,
            'quality': quality,
            'expiries': [],
        }

    threeDayByMaturity = {
        str(expiry['maturityDate']): expiry
        for expiry in threeDay.get('expiries', [])
    }
    expiries: list[dict[str, object]] = []
    for oneDayExpiry in oneDay.get('expiries', []):
        maturityDate = str(oneDayExpiry['maturityDate'])
        threeDayExpiry = threeDayByMaturity.get(maturityDate)
        if threeDayExpiry is None:
            quality['warnings'].append(f'{maturityDate} 缺少 3D Flow 期限数据，已排除。')
            continue
        sideSnapshots: dict[str, dict[str, Any]] = {}
        for sideName in ('call', 'put'):
            sideSnapshot, missing3dRows, missing1dRows = _flowAccelerationSideSnapshot(
                oneDayExpiry[sideName],
                threeDayExpiry[sideName],
            )
            sideSnapshots[sideName] = sideSnapshot
            quality['pairedRows'] += len(sideSnapshot['strikes'])
            quality['missing3dRows'] += missing3dRows
            quality['missing1dRows'] += missing1dRows
        expiries.append(
            {
                'maturityDate': maturityDate,
                'expiryLabel': oneDayExpiry.get('expiryLabel'),
                'dteCalendar': oneDayExpiry.get('dteCalendar'),
                **sideSnapshots,
            }
        )

    if quality['missing3dRows']:
        quality['warnings'].append(
            f"{quality['missing3dRows']} 个 1D 行权价缺少 3D 配对，未补零。"
        )
    if quality['missing1dRows']:
        quality['warnings'].append(
            f"{quality['missing1dRows']} 个 3D 行权价缺少 1D 配对，未补零。"
        )
    return {
        'available': True,
        'baselineDates': baselineDates,
        'formula': 'FlowAcceleration = FlowVelocity1D - FlowVelocity3D',
        'ratioFormula': 'FlowAccelerationRatio = FlowVelocity1D / FlowVelocity3D',
        'quality': quality,
        'expiries': expiries,
    }


def buildFlowWallSnapshot(
    optionRows: Iterable[dict[str, object]],
    *,
    dataDate: date,
    spot: float,
    baselineDates: dict[str, date | None],
) -> dict[str, Any]:
    """按同一合约对齐历史 OI，生成 1D/3D/5D signed Flow Wall。"""
    dataDate = _toDate(dataDate, 'dataDate')
    spotValue = _finiteFloat(spot, 'spot')
    if spotValue <= 0:
        raise ValueError('spot 必须大于 0')

    normalizedBaselineDates: dict[str, date | None] = {}
    for horizon in FLOW_HORIZON_DAYS:
        baseline = baselineDates.get(horizon)
        normalizedBaselineDates[horizon] = None if baseline is None else _toDate(baseline, f'{horizon} baselineDate')

    rowsByDate: dict[date, dict[str, dict[str, object]]] = {}
    for row in optionRows:
        rowTradeDate = _toDate(row.get('trade_date'), 'trade_date')
        tsCode = str(row.get('ts_code') or '').strip()
        if not tsCode:
            continue
        dateRows = rowsByDate.setdefault(rowTradeDate, {})
        if tsCode in dateRows:
            raise ValueError('Flow 输入存在重复合约日期')
        dateRows[tsCode] = row

    currentRows = rowsByDate.get(dataDate, {})
    maturitySet: set[date] = set()
    for row in currentRows.values():
        try:
            maturityDate = _toDate(row.get('maturity_date'), 'maturity_date')
        except ValueError:
            continue
        if maturityDate >= dataDate:
            maturitySet.add(maturityDate)
    selectedMaturities = sorted(maturitySet)[:4]
    selectedSet = set(selectedMaturities)

    currentCandidates: list[dict[str, object]] = []
    adjustedRows = 0
    missingCurrentOiRows = 0
    invalidRows = 0
    for row in currentRows.values():
        try:
            maturityDate = _toDate(row.get('maturity_date'), 'maturity_date')
            perUnit = _finiteFloat(row.get('per_unit'), 'per_unit')
            strike = _finiteFloat(row.get('exercise_price'), 'exercise_price')
        except ValueError:
            invalidRows += 1
            continue
        if maturityDate not in selectedSet:
            continue
        callPut = str(row.get('call_put') or '').strip().upper()
        tsCode = str(row.get('ts_code') or '').strip()
        if callPut not in {'C', 'P'} or not tsCode or perUnit <= 0 or strike <= 0:
            invalidRows += 1
            continue
        if abs(perUnit - STANDARD_PER_UNIT) >= 1e-6:
            adjustedRows += 1
            continue
        oiRaw = row.get('oi')
        if oiRaw is None:
            missingCurrentOiRows += 1
            continue
        try:
            currentOi = _finiteFloat(oiRaw, 'oi')
        except ValueError:
            missingCurrentOiRows += 1
            continue
        if currentOi < 0:
            invalidRows += 1
            continue
        currentCandidates.append(
            {
                'tsCode': tsCode,
                'callPut': callPut,
                'strike': strike,
                'maturityDate': maturityDate,
                'perUnit': perUnit,
                'currentOi': currentOi,
            }
        )

    horizons: dict[str, dict[str, Any]] = {}
    for horizon, days in FLOW_HORIZON_DAYS.items():
        baselineDate = normalizedBaselineDates[horizon]
        baselineRows = rowsByDate.get(baselineDate, {}) if baselineDate is not None else {}
        horizonQuality: dict[str, Any] = {
            'currentRows': len(currentRows),
            'baselineRows': len(baselineRows),
            'flowEligibleRows': 0,
            'missingBaselineRows': 0,
            'missingCurrentOiRows': missingCurrentOiRows,
            'adjustedRows': adjustedRows,
            'invalidRows': invalidRows,
            'warnings': [],
        }
        warnings: list[str] = horizonQuality['warnings']
        if baselineDate is None:
            warnings.append(f'{horizon} 缺少可用的第 {days} 个共同交易日，无法计算。')
        if adjustedRows:
            warnings.append(f'{adjustedRows} 条当前调整合约未并入 Flow Wall。')
        if missingCurrentOiRows:
            warnings.append(f'{missingCurrentOiRows} 条当前标准合约 OI 缺失，已排除且未补零。')
        if invalidRows:
            warnings.append(f'{invalidRows} 条当前合约字段无效，已排除。')

        if baselineDate is None:
            horizons[horizon] = {
                'days': days,
                'baselineDate': None,
                'available': False,
                'quality': horizonQuality,
                'expiries': [],
            }
            continue

        buckets: dict[date, dict[str, dict[float, dict[str, Any]]]] = {
            maturity: {'C': {}, 'P': {}} for maturity in selectedMaturities
        }
        for candidate in currentCandidates:
            tsCode = str(candidate['tsCode'])
            candidateMaturity = cast(date, candidate['maturityDate'])
            candidateStrike = cast(float, candidate['strike'])
            candidateCallPut = cast(str, candidate['callPut'])
            candidateCurrentOi = cast(float, candidate['currentOi'])
            baselineRow = baselineRows.get(tsCode)
            if baselineRow is None:
                horizonQuality['missingBaselineRows'] += 1
                continue
            try:
                baselineMaturity = _toDate(baselineRow.get('maturity_date'), 'maturity_date')
                baselinePerUnit = _finiteFloat(baselineRow.get('per_unit'), 'per_unit')
                baselineStrike = _finiteFloat(baselineRow.get('exercise_price'), 'exercise_price')
                baselineOi = _finiteFloat(baselineRow.get('oi'), 'oi')
            except ValueError:
                horizonQuality['missingBaselineRows'] += 1
                continue
            if (
                baselineMaturity != candidateMaturity
                or abs(baselinePerUnit - STANDARD_PER_UNIT) >= 1e-6
                or abs(baselineStrike - candidateStrike) >= 1e-9
                or baselineOi < 0
            ):
                horizonQuality['missingBaselineRows'] += 1
                continue

            deltaOi = candidateCurrentOi - baselineOi
            maturityDate = candidateMaturity
            callPut = candidateCallPut
            strike = candidateStrike
            strikeBucket = buckets[maturityDate][callPut].setdefault(
                strike,
                {
                    'currentOi': 0.0,
                    'baselineOi': 0.0,
                    'deltaOi': 0.0,
                    'contracts': [],
                },
            )
            strikeBucket['currentOi'] += candidateCurrentOi
            strikeBucket['baselineOi'] += baselineOi
            strikeBucket['deltaOi'] += deltaOi
            strikeBucket['contracts'].append(
                {
                    'tsCode': tsCode,
                    'currentOi': _jsonNumber(candidateCurrentOi),
                    'baselineOi': _jsonNumber(baselineOi),
                    'deltaOi': _jsonNumber(deltaOi),
                    'perUnit': _jsonNumber(STANDARD_PER_UNIT),
                }
            )
            horizonQuality['flowEligibleRows'] += 1

        expiries: list[dict[str, object]] = []
        for index, maturityDate in enumerate(selectedMaturities):
            callSnapshot = _flowSideSnapshot(
                buckets[maturityDate]['C'],
                spot=spotValue,
                days=days,
            )
            putSnapshot = _flowSideSnapshot(
                buckets[maturityDate]['P'],
                spot=spotValue,
                days=days,
            )
            expiries.append(
                {
                    'maturityDate': maturityDate.isoformat(),
                    'expiryLabel': EXPIRY_LABELS[index],
                    'dteCalendar': (maturityDate - dataDate).days,
                    'call': callSnapshot,
                    'put': putSnapshot,
                }
            )
        if horizonQuality['missingBaselineRows']:
            warnings.append(
                f"{horizonQuality['missingBaselineRows']} 条当前标准合约缺少 {baselineDate.isoformat()} 基线，已排除且未补零。"
            )
        if not selectedMaturities:
            warnings.append('当前数据日没有可用于 Flow 的未到期期限。')

        horizons[horizon] = {
            'days': days,
            'baselineDate': baselineDate.isoformat(),
            'available': True,
            'quality': horizonQuality,
            'expiries': expiries,
        }

    return {
        'dataDate': dataDate.isoformat(),
        'spot': _jsonNumber(spotValue),
        'horizons': horizons,
        'dynamics': {
            'acceleration1D3D': _flowAccelerationSnapshot(horizons),
        },
    }


def _connection() -> pymysql.connections.Connection:
    connectionConfig = dict(DB_CONFIG_TUSHARE)
    connectionConfig['cursorclass'] = DictCursor
    connectionConfig.setdefault('read_timeout', 20)
    connectionConfig.setdefault('write_timeout', 20)
    return pymysql.connect(**connectionConfig)


def _commonDataDates(
    conn: pymysql.connections.Connection,
    *,
    underlying: str = UNDERLYING,
    optionRoot: str = OPTION_ROOT,
    limit: int = 6,
) -> list[date]:
    if limit <= 0:
        raise ValueError('共同交易日数量必须大于 0')
    sql = """
        SELECT od.trade_date AS data_date
        FROM opt_daily od
        INNER JOIN opt_basic ob ON ob.ts_code = od.ts_code
        INNER JOIN etf_daily ed
            ON ed.ts_code = %s
           AND ed.trade_date = od.trade_date
        WHERE ob.opt_code = %s
          AND ob.maturity_date >= od.trade_date
          AND ed.close_qfq IS NOT NULL
        GROUP BY od.trade_date
        ORDER BY od.trade_date DESC
        LIMIT %s
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, (underlying, optionRoot, limit))
        rows = cursor.fetchall()
    dates = [
        _toDate(row['data_date'], 'data_date')
        for row in rows
        if row.get('data_date') is not None
    ]
    if not dates:
        raise DataUnavailableError('找不到期权与 ETF 的共同交易日')
    return dates


def _latestCommonDataDate(
    conn: pymysql.connections.Connection,
    *,
    underlying: str = UNDERLYING,
    optionRoot: str = OPTION_ROOT,
) -> date:
    return _commonDataDates(
        conn,
        underlying=underlying,
        optionRoot=optionRoot,
        limit=1,
    )[0]


def _spotOnDate(
    conn: pymysql.connections.Connection,
    dataDate: date,
    *,
    underlying: str = UNDERLYING,
) -> float:
    sql = """
        SELECT close_qfq
        FROM etf_daily
        WHERE ts_code = %s AND trade_date = %s
        LIMIT 1
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, (underlying, dataDate))
        row = cursor.fetchone()
    if not row or row.get('close_qfq') is None:
        raise DataUnavailableError(f'共同交易日缺少 {underlying} close_qfq')
    spot = _finiteFloat(row['close_qfq'], 'close_qfq')
    if spot <= 0:
        raise DataUnavailableError(f'{underlying} close_qfq 无效')
    return spot


def _optionRowsOnDate(
    conn: pymysql.connections.Connection,
    dataDate: date,
    *,
    optionRoot: str = OPTION_ROOT,
) -> list[dict[str, object]]:
    maturitySql = """
        SELECT DISTINCT ob.maturity_date
        FROM opt_basic ob
        INNER JOIN opt_daily od
            ON od.ts_code = ob.ts_code
           AND od.trade_date = %s
        WHERE ob.opt_code = %s
          AND ob.call_put IN ('C', 'P')
          AND ob.maturity_date >= %s
          AND (ob.list_date IS NULL OR ob.list_date <= %s)
          AND (ob.delist_date IS NULL OR ob.delist_date >= %s)
        ORDER BY ob.maturity_date
        LIMIT 4
    """
    with conn.cursor() as cursor:
        cursor.execute(maturitySql, (dataDate, optionRoot, dataDate, dataDate, dataDate))
        maturityRows = cursor.fetchall()
    maturities = [row['maturity_date'] for row in maturityRows if row.get('maturity_date')]
    if not maturities:
        raise DataUnavailableError(f'共同交易日没有未到期的 {optionRoot} 合约')

    placeholders = ','.join(['%s'] * len(maturities))
    optionSql = f"""
        SELECT
            ob.ts_code,
            ob.call_put,
            ob.exercise_price,
            ob.maturity_date,
            ob.per_unit,
            od.trade_date,
            od.oi,
            od.vol,
            od.close,
            od.settle
        FROM opt_basic ob
        INNER JOIN opt_daily od
            ON od.ts_code = ob.ts_code
           AND od.trade_date = %s
        WHERE ob.opt_code = %s
          AND ob.call_put IN ('C', 'P')
          AND ob.maturity_date IN ({placeholders})
          AND (ob.list_date IS NULL OR ob.list_date <= %s)
          AND (ob.delist_date IS NULL OR ob.delist_date >= %s)
        ORDER BY ob.maturity_date, ob.exercise_price, ob.call_put, ob.ts_code
    """
    params: list[object] = [dataDate, optionRoot, *maturities, dataDate, dataDate]
    with conn.cursor() as cursor:
        cursor.execute(optionSql, params)
        return list(cursor.fetchall())


def _ivRowsOnDate(
    conn: pymysql.connections.Connection,
    dataDate: date,
    maturityDates: Iterable[date],
    *,
    underlying: str = UNDERLYING,
) -> list[dict[str, object]]:
    maturityValues = [_toDate(value, 'maturityDate') for value in maturityDates]
    if not maturityValues:
        return []
    placeholders = ','.join(['%s'] * len(maturityValues))
    sql = f"""
        SELECT
            underlying,
            trade_date,
            maturity_date,
            tenor,
            call_put,
            representative_iv,
            forward,
            spot,
            dte
        FROM opt_iv_panel
        WHERE underlying = %s
          AND trade_date = %s
          AND maturity_date IN ({placeholders})
          AND call_put IN ('C', 'P')
    """
    params: list[object] = [underlying, dataDate, *maturityValues]
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return list(cursor.fetchall())


def _flowRowsOnDates(
    conn: pymysql.connections.Connection,
    dataDates: Iterable[date],
    maturityDates: Iterable[date],
    *,
    optionRoot: str = OPTION_ROOT,
) -> list[dict[str, object]]:
    dateValues = [_toDate(value, 'dataDate') for value in dataDates]
    maturityValues = [_toDate(value, 'maturityDate') for value in maturityDates]
    if not dateValues or not maturityValues:
        return []
    datePlaceholders = ','.join(['%s'] * len(dateValues))
    maturityPlaceholders = ','.join(['%s'] * len(maturityValues))
    optionSql = f"""
        SELECT
            ob.ts_code,
            ob.call_put,
            ob.exercise_price,
            ob.maturity_date,
            ob.per_unit,
            od.trade_date,
            od.oi
        FROM opt_basic ob
        INNER JOIN opt_daily od
            ON od.ts_code = ob.ts_code
           AND od.trade_date IN ({datePlaceholders})
        WHERE ob.opt_code = %s
          AND ob.call_put IN ('C', 'P')
          AND ob.maturity_date IN ({maturityPlaceholders})
          AND (ob.list_date IS NULL OR ob.list_date <= od.trade_date)
          AND (ob.delist_date IS NULL OR ob.delist_date >= od.trade_date)
        ORDER BY od.trade_date, ob.maturity_date, ob.exercise_price, ob.call_put, ob.ts_code
    """
    params: list[object] = [*dateValues, optionRoot, *maturityValues]
    with conn.cursor() as cursor:
        cursor.execute(optionSql, params)
        return list(cursor.fetchall())


def loadOiWallSnapshot(
    underlyingCode: str = DEFAULT_UNDERLYING_CODE,
    connectionFactory: Callable[[], pymysql.connections.Connection] = _connection,
) -> dict[str, Any]:
    underlyingConfig = resolveUnderlying(underlyingCode)
    conn = connectionFactory()
    try:
        commonDates = _commonDataDates(
            conn,
            underlying=underlyingConfig.code,
            optionRoot=underlyingConfig.optionRoot,
            limit=6,
        )
        dataDate = commonDates[0]
        spot = _spotOnDate(conn, dataDate, underlying=underlyingConfig.code)
        optionRows = _optionRowsOnDate(
            conn,
            dataDate,
            optionRoot=underlyingConfig.optionRoot,
        )
        maturityDates = sorted(
            {
                _toDate(row.get('maturity_date'), 'maturity_date')
                for row in optionRows
                if _toDate(row.get('maturity_date'), 'maturity_date') >= dataDate
            }
        )[:4]
        ivRows = _ivRowsOnDate(
            conn,
            dataDate,
            maturityDates,
            underlying=underlyingConfig.code,
        )
        snapshot = buildOiWallSnapshot(
            optionRows,
            dataDate=dataDate,
            spot=spot,
            underlying=underlyingConfig.code,
            ivRows=ivRows,
        )
        snapshot['underlyingCode'] = underlyingConfig.code.rsplit('.', maxsplit=1)[0]
        snapshot['underlyingLabel'] = underlyingConfig.label
        snapshot['optionRoot'] = underlyingConfig.optionRoot
        maturityDates = [
            _toDate(expiry['maturityDate'], 'maturityDate')
            for expiry in snapshot['expiries']
        ]
        flowRows = _flowRowsOnDates(
            conn,
            commonDates,
            maturityDates,
            optionRoot=underlyingConfig.optionRoot,
        )
        baselineDates = {
            horizon: commonDates[days] if len(commonDates) > days else None
            for horizon, days in FLOW_HORIZON_DAYS.items()
        }
        snapshot['flow'] = buildFlowWallSnapshot(
            flowRows,
            dataDate=dataDate,
            spot=spot,
            baselineDates=baselineDates,
        )
        return snapshot
    finally:
        conn.close()


def loadHealth(
    connectionFactory: Callable[[], pymysql.connections.Connection] = _connection,
) -> dict[str, Any]:
    conn = connectionFactory()
    try:
        with conn.cursor() as cursor:
            cursor.execute('SELECT 1 AS ok')
            row = cursor.fetchone()
        return {
            'ok': True,
            'service': 'opt-wall-five-underlyings',
            'database': 'ok' if row else 'empty',
        }
    finally:
        conn.close()


def createApp(
    *,
    snapshotLoader: Callable[[], dict[str, Any]] | None = None,
    snapshotLoaderByUnderlying: Callable[[str], dict[str, Any]] | None = None,
    healthLoader: Callable[[], dict[str, Any]] | None = None,
) -> Flask:
    app = Flask(__name__)
    if isinstance(app.json, DefaultJSONProvider):
        app.json.ensure_ascii = False
    if snapshotLoaderByUnderlying is not None:
        snapshotFn: Callable[[str], dict[str, Any]] = snapshotLoaderByUnderlying
    elif snapshotLoader is not None:
        def loadInjectedSnapshot(_underlyingCode: str) -> dict[str, Any]:
            return snapshotLoader()

        snapshotFn = loadInjectedSnapshot
    else:
        snapshotFn = loadOiWallSnapshot
    healthFn = healthLoader or loadHealth

    @app.get('/')
    def index() -> str:
        try:
            selectedConfig = resolveUnderlying(request.args.get('underlying'))
        except ValueError:
            selectedConfig = resolveUnderlying()
        selectedCode = selectedConfig.code.rsplit('.', maxsplit=1)[0]
        return render_template(
            'opt_wall_510050.html',
            underlyingOptions=underlyingOptions(),
            selectedUnderlying=selectedCode,
            selectedUnderlyingLabel=selectedConfig.label,
        )

    @app.get('/api/oi-wall')
    def apiOiWall():
        try:
            selectedConfig = resolveUnderlying(request.args.get('underlying'))
        except ValueError:
            rawCode = str(request.args.get('underlying') or DEFAULT_UNDERLYING_CODE).strip().upper()
            if rawCode.endswith('.SH') or rawCode.endswith('.SZ'):
                rawCode = rawCode[:-3]
            return jsonify(
                {
                    'ok': False,
                    'error': f'不支持的 ETF 品种：{rawCode}',
                    'errorCode': 'INVALID_UNDERLYING',
                }
            ), 400

        try:
            return jsonify({'ok': True, 'snapshot': snapshotFn(selectedConfig.code)})
        except DataUnavailableError as exc:
            return jsonify({'ok': False, 'error': str(exc), 'errorCode': 'DATA_UNAVAILABLE'}), 422
        except Exception as exc:
            logger.error(
                '%s OI Wall 快照加载失败 error_type=%s',
                selectedConfig.code,
                type(exc).__name__,
            )
            return jsonify(
                {
                    'ok': False,
                    'error': 'OI Wall 数据暂时不可用，请查看服务日志。',
                    'errorCode': 'SNAPSHOT_LOAD_FAILED',
                }
            ), 503

    @app.get('/health')
    def health():
        try:
            return jsonify(healthFn())
        except Exception as exc:
            logger.error('510050 OI Wall 健康检查失败 error_type=%s', type(exc).__name__)
            return jsonify(
                {
                    'ok': False,
                    'service': 'opt-wall-five-underlyings',
                    'database': 'unavailable',
                }
            ), 503

    return app


app = createApp()


if __name__ == '__main__':
    host = os.getenv('OPT_WALL_HOST', '127.0.0.1')
    port = int(os.getenv('OPT_WALL_PORT', '5011'))
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
