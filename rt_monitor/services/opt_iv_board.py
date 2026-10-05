"""opt-t-board 右侧固定隐含波动率看板：读 opt_iv_panel，全样本统计。"""
from __future__ import annotations

import copy
import math
import threading
import time
from datetime import date, datetime
from typing import Any

import numpy as np
import pymysql
from flask import render_template
from logger_config import get_logger

from rt_monitor.core.config import DB_CONFIG_TUSHARE
from rt_monitor.services import opt_iv_windows as ivWindows

logger = get_logger(__name__)

# 与 opt_iv_panel_to_mysql.BOARD_UNDERLYINGS 顺序一致（五 ETF + 三中金所股指期权）
OPT_IV_BOARD_UNDERLYINGS: list[tuple[str, str]] = [
    ('588000.SH', '588000'),
    ('159915.SZ', '159915'),
    ('510050.SH', '510050'),
    ('000016.SH', '上证50指'),
    ('510300.SH', '510300'),
    ('000300.SH', '沪深300指数'),
    ('510500.SH', '510500'),
    ('000852.SH', '中证1000指'),
]

# ETF：当月/下月/当季/下季（仅 4 档，不展示空的下下月/下下季）
TENOR_ORDER_ETF = (
    'current_month',
    'next_month',
    'current_quarter',
    'next_quarter',
)
# 中金所股指期权：近月最多 3 + 季月最多 3
TENOR_ORDER_CFFEX = (
    'current_month',
    'next_month',
    'next_next_month',
    'current_quarter',
    'next_quarter',
    'next_next_quarter',
)
TENOR_CN = {
    'current_month': '当月',
    'next_month': '下月',
    'next_next_month': '下下月',
    'current_quarter': '当季',
    'next_quarter': '下季',
    'next_next_quarter': '下下季',
}
_CFFEX_IV_UNDERLYINGS = frozenset({'000300.SH', '000016.SH', '000852.SH'})
_INDEX_SPOT_UNDERLYINGS = frozenset({'000300.SH', '000016.SH', '000852.SH'})
HV_N_MIN = 5
HV_N_MAX = 120
HV_MUTE_DTE = 10
_ETF_IV_LIVE_UNDERLYINGS = frozenset(
    {'588000.SH', '159915.SZ', '510050.SH', '510300.SH', '510500.SH'}
)
_LIVE_CHAIN_TTL_SEC = 300.0
_IV_BOARD_LIVE_CACHE_TTL_SEC = 2.0
_liveChainCache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_liveChainLock = threading.Lock()


def tenorOrderForUnderlying(underlying: str) -> tuple[str, ...]:
    """按标的类型返回期限档：ETF 4 档，中金所股指期权 6 档。"""
    raw = str(underlying or '').strip().upper()
    if raw in _CFFEX_IV_UNDERLYINGS or raw.split('.', 1)[0] in {
        '000300',
        '000016',
        '000852',
    }:
        return TENOR_ORDER_CFFEX
    return TENOR_ORDER_ETF


def _toDate(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def _fmtIvPct(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return '—'
    return f'{value * 100:.2f}'


def _fmtIvPctMax(value: float | None) -> str:
    """看板分位/极值列：≥100% 一位小数（123.0），否则两位（29.48），同为 5 字符对齐。"""
    if value is None or not math.isfinite(value):
        return '—'
    pct = value * 100.0
    if abs(pct) >= 100.0:
        return f'{pct:.1f}'
    return f'{pct:.2f}'


def _fmtDate(value: date | None) -> str:
    """看板日期用 6 位紧凑格式省列宽（新增认购/认沽「最新」两列后表宽吃紧）；
    完整 YYYY-MM-DD 放 title。"""
    return value.strftime('%y%m%d') if value else '—'


def _fmtDateFull(value: date | None) -> str:
    return value.isoformat() if value else ''


def _fmtPer(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return '—'
    return f'{value * 100:.1f}'


def _perCls(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return ''
    if value <= 0.20:
        return 'opt-iv-per--low'
    if value >= 0.80:
        return 'opt-iv-per--high'
    if value >= 0.60:
        return 'opt-iv-per--warm'
    return 'opt-iv-per--mid'


def _normCdf(z: float) -> float:
    """标准正态 CDF（用 erf，避免页面路径硬依赖 scipy）。"""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _empSigmaRaw(values: np.ndarray, nSigma: float) -> float | None:
    """±σ 经验分位原始 IV（小数）；空样本返回 None。"""
    hist = values[np.isfinite(values)]
    if hist.size == 0:
        return None
    qPct = 100.0 * _normCdf(nSigma)
    qPct = min(100.0, max(0.0, qPct))
    return float(np.percentile(hist, qPct))


def _empSigmaBand(values: np.ndarray, nSigma: float) -> tuple[str, str, float | None]:
    """
    ±σ 列：取真实样本在 Φ(nσ) 处的经验分位，不做 mean±nσ 高斯外推。
    返回 (展示串, title, 原始 IV 小数或 None)。
    """
    raw = _empSigmaRaw(values, nSigma)
    if raw is None:
        return '—', '', None
    qPct = 100.0 * _normCdf(nSigma)
    qPct = min(100.0, max(0.0, qPct))
    title = f'样本经验分位 Φ({nSigma:g})={qPct:.3f}% → {raw * 100:.2f}%（非 mean±nσ）'
    return _fmtIvPctMax(raw), title, raw


def _sigmaBreachCls(
    current: float,
    bandRaw: float | None,
    kind: str,
) -> tuple[str, str]:
    """
    最新 IV 越过对应 σ 阈时给该列醒目 class，并在 title 追加提示。
    kind: 'hi3'|'hi2'|'lo2'|'lo3'
    """
    if bandRaw is None or not math.isfinite(current) or not math.isfinite(bandRaw):
        return '', ''
    breached = False
    if kind in ('hi3', 'hi2'):
        breached = current >= bandRaw
    elif kind in ('lo3', 'lo2'):
        breached = current <= bandRaw
    if not breached:
        return '', ''
    clsMap = {
        'hi3': 'opt-iv-sig--hi3',
        'hi2': 'opt-iv-sig--hi2',
        'lo2': 'opt-iv-sig--lo2',
        'lo3': 'opt-iv-sig--lo3',
    }
    tipMap = {
        'hi3': '最新 IV 已 ≥ +3σ 阈',
        'hi2': '最新 IV 已 ≥ +2σ 阈',
        'lo2': '最新 IV 已 ≤ −2σ 阈',
        'lo3': '最新 IV 已 ≤ −3σ 阈',
    }
    return clsMap[kind], tipMap[kind]


def _fullSamplePercentile(values: np.ndarray, current: float) -> float | None:
    hist = values[np.isfinite(values)]
    if hist.size == 0 or not math.isfinite(current):
        return None
    less = int(np.sum(hist < current))
    equal = int(np.sum(hist == current))
    return float((less + 0.5 * equal) / hist.size)


# 展示字段名与 opt_iv_windows 的窗口键、T 板字段名三者保持同名，避免同一列在两页叫两个名字
_PER_CELL_KEYS = ('pera', 'perc', 'per1y')


def _perCells(windows: ivWindows.PerWindows, current: float | None) -> dict[str, str]:
    """三窗口分位 → 展示文本 / 着色 / title。

    共享模块给 0~100，本页 _fmtPer/_perCls 沿用 0~1 口径，故此处折算一次。
    """
    values = windows.evaluate(current)
    out: dict[str, str] = {}
    for key in _PER_CELL_KEYS:
        raw = values.get(key)
        ratio = None if raw is None else raw / 100.0
        out[key] = _fmtPer(ratio)
        out[f'{key}_cls'] = _perCls(ratio)
        out[f'{key}_title'] = ivWindows.perTitle(key, windows)
    return out


def alignedHvWindowN(dte: int | float | None) -> int | None:
    """剩余自然日 → 对齐交易日窗口，夹在 [5, 120]。"""
    if dte is None:
        return None
    try:
        dteF = float(dte)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(dteF) or dteF <= 0:
        return None
    return max(HV_N_MIN, min(HV_N_MAX, int(round(dteF * 252.0 / 365.0))))


def isHvMuted(tenor: str, dte: int | float | None) -> bool:
    if str(tenor or '').strip() != 'current_month':
        return False
    try:
        dteF = float(dte) if dte is not None else None
    except (TypeError, ValueError):
        return False
    return dteF is not None and math.isfinite(dteF) and dteF < HV_MUTE_DTE


def midIvRaw(callIv: float | None, putIv: float | None) -> float | None:
    vals = [
        v
        for v in (callIv, putIv)
        if v is not None and math.isfinite(v) and v > 0
    ]
    if not vals:
        return None
    return float(sum(vals) / len(vals))


def axisSkewPp(callIv: float | None, putIv: float | None) -> float | None:
    """认沽 IV − 认购 IV，单位百分点。缺一边则无法算斜度。"""
    if callIv is None or putIv is None:
        return None
    if not (math.isfinite(callIv) and math.isfinite(putIv) and callIv > 0 and putIv > 0):
        return None
    return (putIv - callIv) * 100.0


def _skewCls(skewPp: float | None) -> str:
    if skewPp is None or not math.isfinite(skewPp):
        return ''
    if skewPp >= 2.5:
        return 'opt-iv-skew--fat'
    if skewPp >= 0.5:
        return 'opt-iv-skew--put'
    if skewPp <= -0.5:
        return 'opt-iv-skew--call'
    return ''


def attachAxisSkew(
    axis: dict[str, Any],
    callIv: float | None,
    putIv: float | None,
) -> dict[str, Any]:
    """中轴斜度不依赖 HV 窗口；HV 缺数时仍可展示。"""
    pp = axisSkewPp(callIv, putIv)
    if pp is None:
        axis['skew'] = '—'
        axis['skew_title'] = ''
        axis['skew_cls'] = ''
        return axis
    axis['skew'] = _fmtDeltaPp(pp)
    axis['skew_title'] = (
        f'认沽最新 − 认购最新 = {pp:+.1f} pp。'
        '正=认沽更贵；+0.5~+2.5 常见溢价；≥+2.5 偏肥尾，卖 put 降档'
    )
    axis['skew_cls'] = _skewCls(pp)
    return axis


def logReturnsFromCloses(closes: np.ndarray) -> np.ndarray:
    x = np.asarray(closes, dtype=float)
    out = np.full(x.size, np.nan)
    if x.size < 2:
        return out
    prev = x[:-1]
    cur = x[1:]
    ok = np.isfinite(prev) & np.isfinite(cur) & (prev > 0) & (cur > 0)
    with np.errstate(divide='ignore', invalid='ignore'):
        out[1:] = np.where(ok, np.log(cur / prev), np.nan)
    return out


def rollingAnnHv(logret: np.ndarray, n: int) -> np.ndarray:
    """收盘对数收益滚动年化 HV；下标 i 用含当日的近 n 个收益。"""
    x = np.asarray(logret, dtype=float)
    out = np.full(x.size, np.nan)
    if n < 2 or x.size < n:
        return out
    invN = 1.0 / n
    ann = math.sqrt(252.0)
    c1 = np.concatenate([[0.0], np.cumsum(np.where(np.isfinite(x), x, 0.0))])
    c2 = np.concatenate([[0.0], np.cumsum(np.where(np.isfinite(x), x * x, 0.0))])
    cf = np.concatenate([[0], np.cumsum(np.isfinite(x).astype(np.int32))])
    for i in range(n - 1, x.size):
        lo = i + 1 - n
        if int(cf[i + 1] - cf[lo]) < n:
            continue
        s1 = c1[i + 1] - c1[lo]
        s2 = c2[i + 1] - c2[lo]
        var = (s2 - s1 * s1 * invN) / (n - 1)
        if var > 0 and math.isfinite(var):
            out[i] = math.sqrt(var) * ann
    return out


def subsequentAnnRv(logret: np.ndarray, startIdx: int, n: int) -> float | None:
    """asof 下标之后 n 个收益的年化已实现波动。"""
    if n < 2 or startIdx < 0:
        return None
    lo = startIdx + 1
    hi = startIdx + 1 + n
    if hi > logret.size:
        return None
    sl = np.asarray(logret[lo:hi], dtype=float)
    if sl.size < n or not np.all(np.isfinite(sl)):
        return None
    return float(np.std(sl, ddof=1) * math.sqrt(252.0))


def summarizeVrp(ivs: np.ndarray, rvs: np.ndarray) -> dict[str, Any]:
    mask = np.isfinite(ivs) & np.isfinite(rvs)
    if int(mask.sum()) < 8:
        return {'n': 0, 'median': None, 'mean': None, 'win_rate': None}
    vrp = ivs[mask] - rvs[mask]
    return {
        'n': int(vrp.size),
        'median': float(np.median(vrp)),
        'mean': float(np.mean(vrp)),
        'win_rate': float(np.mean(vrp > 0)),
    }


def _fmtDeltaPp(delta: float | None) -> str:
    if delta is None or not math.isfinite(delta):
        return '—'
    sign = '+' if delta > 0 else ''
    return f'{sign}{delta:.1f}'


def _deltaCls(delta: float | None) -> str:
    if delta is None or not math.isfinite(delta):
        return ''
    if delta > 1.0:
        return 'opt-iv-delta--pos'
    if delta < -1.0:
        return 'opt-iv-delta--neg'
    return ''


def _asofIndex(dates: list[date], asof: date) -> int | None:
    for i in range(len(dates) - 1, -1, -1):
        if dates[i] <= asof:
            return i
    return None


def _latestDte(rows: list[dict[str, Any]]) -> int | None:
    bestDate: date | None = None
    best: int | None = None
    for r in rows:
        d = _toDate(r.get('trade_date'))
        if d is None:
            continue
        try:
            dte = int(r['dte']) if r.get('dte') is not None else None
        except (TypeError, ValueError):
            dte = None
        if dte is None:
            continue
        if bestDate is None or d > bestDate:
            bestDate = d
            best = dte
    return best


def _emptyAxis() -> dict[str, Any]:
    return {
        'hv': '—',
        'hv_title': '',
        'delta': '—',
        'delta_title': '',
        'delta_cls': '',
        'hv_per': '—',
        'hv_per_cls': '',
        'skew': '—',
        'skew_title': '',
        'skew_cls': '',
        'muted': False,
        'muted_cls': '',
        'n': 0,
        'dte': None,
        'hv_raw': None,
    }


def _buildAxisStats(
    closeDates: list[date],
    logret: np.ndarray,
    asof: date | None,
    dte: int | None,
    midIv: float | None,
    tenor: str,
) -> dict[str, Any]:
    empty = _emptyAxis()
    muted = isHvMuted(tenor, dte)
    empty['muted'] = muted
    empty['muted_cls'] = 'opt-iv-hv--muted' if muted else ''
    empty['dte'] = int(dte) if dte is not None else None
    n = alignedHvWindowN(dte)
    if n is None or asof is None or midIv is None or not closeDates:
        return empty
    empty['n'] = n
    idx = _asofIndex(closeDates, asof)
    if idx is None:
        return empty
    hvSeries = rollingAnnHv(logret, n)
    if idx >= hvSeries.size or not np.isfinite(hvSeries[idx]):
        return empty
    hv = float(hvSeries[idx])
    per = _fullSamplePercentile(hvSeries[: idx + 1], hv)
    delta = (midIv - hv) * 100.0
    muteNote = '当月剩余不足10个自然日，HV 噪声大，决策以下月为准' if muted else ''
    hvTitle = f'回看 HV（n={n} 交易日，DTE={int(dte)}）'
    if muteNote:
        hvTitle = hvTitle + ' · ' + muteNote
    return {
        'hv': _fmtIvPct(hv),
        'hv_title': hvTitle,
        'delta': _fmtDeltaPp(delta),
        'delta_title': f'中轴 IV − HV = {delta:+.1f} pp（n={n}）',
        'delta_cls': '' if muted else _deltaCls(delta),
        'hv_per': _fmtPer(per),
        'hv_per_cls': _perCls(per),
        'muted': muted,
        'muted_cls': 'opt-iv-hv--muted' if muted else '',
        'n': n,
        'dte': int(dte),
        'hv_raw': hv,
    }


def _fetchCloseMap(
    cur: Any,
    underlyings: list[str],
) -> dict[str, tuple[list[date], np.ndarray]]:
    """标的收盘 → (日期, 与日期等长的对数收益，首日为 nan)。"""
    etfCodes = [u for u in underlyings if u not in _INDEX_SPOT_UNDERLYINGS]
    idxCodes = [u for u in underlyings if u in _INDEX_SPOT_UNDERLYINGS]
    buckets: dict[str, list[tuple[date, float]]] = {u: [] for u in underlyings}

    def loadTable(table: str, codes: list[str]) -> None:
        if not codes:
            return
        placeholders = ','.join(['%s'] * len(codes))
        cur.execute(
            f'SELECT ts_code, trade_date, close FROM {table} '
            f'WHERE ts_code IN ({placeholders}) ORDER BY ts_code, trade_date',
            tuple(codes),
        )
        for r in cur.fetchall() or []:
            ts = str(r.get('ts_code') or '')
            d = _toDate(r.get('trade_date'))
            try:
                px = float(r.get('close'))
            except (TypeError, ValueError):
                continue
            if not ts or d is None or not math.isfinite(px) or px <= 0:
                continue
            buckets.setdefault(ts, []).append((d, px))

    loadTable('etf_daily', etfCodes)
    loadTable('index_daily', idxCodes)
    packed: dict[str, tuple[list[date], np.ndarray]] = {}
    for ts, pairs in buckets.items():
        if not pairs:
            packed[ts] = ([], np.asarray([], dtype=float))
            continue
        pairs.sort(key=lambda x: x[0])
        dates = [p[0] for p in pairs]
        closes = np.asarray([p[1] for p in pairs], dtype=float)
        packed[ts] = (dates, logReturnsFromCloses(closes))
    return packed


def _fmtVrpLine(nWindow: int, stats: dict[str, Any]) -> str:
    sample = int(stats.get('n') or 0)
    if sample <= 0:
        return ''
    med = stats.get('median')
    mean = stats.get('mean')
    win = stats.get('win_rate')
    medS = f'{med * 100:+.1f}pp' if med is not None else '—'
    meanS = f'{mean * 100:+.1f}pp' if mean is not None else '—'
    winS = f'{win * 100:.1f}%' if win is not None else '—'
    return (
        f'该期限历史 VRP（IV-随后RV，n={nWindow}日）：'
        f'中位 {medS} · 均值 {meanS} · IV>RV {winS} · 样本 {sample}'
    )


def _sideStats(
    rows: list[dict[str, Any]],
    *,
    underlyingTs: str = '',
    tenor: str = '',
    callPut: str = '',
) -> dict[str, Any]:
    """
    单侧（认购或认沽）全样本统计 + 最新日三窗口 Per。
    Min/Max/Med 取真实样本；±2σ/±3σ 取 Φ(±n) 对应经验分位（禁止 mean±nσ 外推）。
    Per A/C/1y 口径见 opt_iv_windows；窗口对象放模块级缓存，blocks 里只带键
    （blocks 每次 finalize 都会 deepcopy，把排序结果塞进去会被反复复制）。
    """
    empty = {
        'min_date': '',
        'min_date_full': '',
        'min': '—',
        'm3': '—',
        'm3_title': '',
        'm3_cls': '',
        'm2': '—',
        'm2_title': '',
        'm2_cls': '',
        'med': '—',
        'p2': '—',
        'p2_title': '',
        'p2_cls': '',
        'p3': '—',
        'p3_title': '',
        'p3_cls': '',
        'max': '—',
        'max_title': '',
        'max_date': '',
        'max_date_full': '',
        'pera': '—',
        'pera_cls': '',
        'pera_title': '',
        'per1y': '—',
        'per1y_cls': '',
        'per1y_title': '',
        'perc': '—',
        'perc_cls': '',
        'perc_title': '',
        'current': '—',
        'current_raw': None,
        'n': 0,
        'asof': '',
        '_hist_vals': [],
        '_per_key': None,
    }
    if not rows:
        return empty
    dated: list[tuple[date, float]] = []
    for r in rows:
        d = _toDate(r.get('trade_date'))
        iv = r.get('representative_iv')
        try:
            ivF = float(iv) if iv is not None else math.nan
        except (TypeError, ValueError):
            ivF = math.nan
        # 业务约束：代表 IV 必须为正；异常点跳过不进样本
        if d is None or not math.isfinite(ivF) or ivF <= 0:
            continue
        dated.append((d, ivF))
    if not dated:
        return empty
    dated.sort(key=lambda x: x[0])
    dates = [x[0] for x in dated]
    vals = np.asarray([x[1] for x in dated], dtype=float)
    imin = int(np.nanargmin(vals))
    imax = int(np.nanargmax(vals))
    med = float(np.nanmedian(vals))
    current = float(vals[-1])
    asof = dates[-1]
    # 本页比的是日频代表 IV 序列自己的最新点，故序列含 asof 当日
    perKey = ivWindows.cacheKey(
        underlyingTs, tenor, callPut, asof, scope=ivWindows.SCOPE_INCL_ASOF
    )
    windows = ivWindows.cachedPerWindows(
        perKey,
        lambda: ivWindows.buildPerWindows(dates, vals.tolist(), underlyingTs=underlyingTs),
    )
    perCells = _perCells(windows, current)
    m3, m3Title, m3Raw = _empSigmaBand(vals, -3.0)
    m2, m2Title, m2Raw = _empSigmaBand(vals, -2.0)
    p2, p2Title, p2Raw = _empSigmaBand(vals, 2.0)
    p3, p3Title, p3Raw = _empSigmaBand(vals, 3.0)
    # 最新越过阈 → 点亮对应 σ 列（±3 比 ±2 更深，便于扫极端）
    m3Cls, m3Tip = _sigmaBreachCls(current, m3Raw, 'lo3')
    m2Cls, m2Tip = _sigmaBreachCls(current, m2Raw, 'lo2')
    p2Cls, p2Tip = _sigmaBreachCls(current, p2Raw, 'hi2')
    p3Cls, p3Tip = _sigmaBreachCls(current, p3Raw, 'hi3')
    if m3Tip:
        m3Title = (m3Title + ' · ' if m3Title else '') + m3Tip
    if m2Tip:
        m2Title = (m2Title + ' · ' if m2Title else '') + m2Tip
    if p2Tip:
        p2Title = (p2Title + ' · ' if p2Title else '') + p2Tip
    if p3Tip:
        p3Title = (p3Title + ' · ' if p3Title else '') + p3Tip
    return {
        'min_date': _fmtDate(dates[imin]),
        'min_date_full': _fmtDateFull(dates[imin]),
        'min': _fmtIvPctMax(float(vals[imin])),
        'm3': m3,
        'm3_title': m3Title,
        'm3_cls': m3Cls,
        'm2': m2,
        'm2_title': m2Title,
        'm2_cls': m2Cls,
        'med': _fmtIvPctMax(med),
        'p2': p2,
        'p2_title': p2Title,
        'p2_cls': p2Cls,
        'p3': p3,
        'p3_title': p3Title,
        'p3_cls': p3Cls,
        'max': _fmtIvPctMax(float(vals[imax])),
        'max_title': _fmtIvPct(float(vals[imax])),
        'max_date': _fmtDate(dates[imax]),
        'max_date_full': _fmtDateFull(dates[imax]),
        **perCells,
        'current': _fmtIvPct(current),
        'current_raw': current,
        'n': int(vals.size),
        'asof': _fmtDateFull(asof),
        '_hist_vals': [float(x) for x in vals.tolist()],
        '_per_key': perKey,
    }


def _fetchPanelRows(cur: Any, underlying: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT trade_date, tenor, call_put, representative_iv, dte
        FROM opt_iv_panel
        WHERE underlying=%s
          AND representative_iv IS NOT NULL
        ORDER BY trade_date, tenor, call_put
        """,
        (underlying,),
    )
    return list(cur.fetchall() or [])


def _optUnderlyingToQmtEtf(tsCode: str) -> str:
    return str(tsCode or '').strip().upper()


def quoteMapFromDaqmtSnapshot(
    snapshot: dict[str, Any] | None,
    *,
    asof: date | None = None,
) -> dict[str, dict[str, Any]]:
    """QMT 快照 → ts_code 报价（close=last）。跳过非当日/中价冒充 tick。"""
    if not snapshot or not isinstance(snapshot.get('opt'), dict):
        return {}
    from rt_monitor.services.opt_t_board import isTodayOptLast, qmtOptToTsCode

    asofDate = asof or date.today()
    out: dict[str, dict[str, Any]] = {}
    for qmtCode, packed in snapshot['opt'].items():
        if not isinstance(packed, dict) or packed.get('last') is None:
            continue
        try:
            lastF = float(packed['last'])
        except (TypeError, ValueError):
            continue
        if lastF != lastF or lastF <= 0:
            continue
        if not isTodayOptLast(packed, asofDate):
            continue
        tsCode = qmtOptToTsCode(str(qmtCode))
        vol = packed.get('vol')
        oi = packed.get('oi')
        out[tsCode] = {
            'close': lastF,
            'vol': vol if vol is not None else 0.0,
            'oi': oi if oi is not None else 0.0,
        }
    return out


def _spotFromSnapshot(snapshot: dict[str, Any] | None, tsCode: str) -> float | None:
    if not snapshot or not isinstance(snapshot.get('etf'), dict):
        return None
    packed = snapshot['etf'].get(_optUnderlyingToQmtEtf(tsCode))
    if not isinstance(packed, dict) or packed.get('last') is None:
        return None
    from rt_monitor.services.opt_t_board import isStubDaqmtEtfTick

    if isStubDaqmtEtfTick(packed):
        return None
    try:
        spot = float(packed['last'])
    except (TypeError, ValueError):
        return None
    if spot != spot or spot <= 0:
        return None
    return spot


def liveRepIvByTenor(
    contracts: list[dict[str, Any]],
    quoteByTs: dict[str, dict[str, Any]],
    spot: float,
    rate: float,
    asof: date,
    atmBand: float = 0.15,
) -> dict[tuple[str, str], float]:
    """单日近 ATM 代表 IV：与 opt_iv_panel 同一套远期 + 量仓加权中位数。"""
    import pandas as pd
    from option_iv import (
        _weighted_median,
        calendarTenorMapForMaturities,
        estimate_implied_forward,
        implied_volatility,
    )

    matured: list[tuple[date, dict[str, Any]]] = []
    for row in contracts:
        md = _toDate(row.get('maturity_date'))
        if md is None or md < asof:
            continue
        matured.append((md, row))
    if not matured:
        return {}
    tenorByTs = calendarTenorMapForMaturities(
        [md for md, _ in matured],
        isCffex=False,
    )
    byTenor: dict[str, list[dict[str, Any]]] = {}
    for md, row in matured:
        tenor = tenorByTs.get(pd.Timestamp(md).normalize())
        if not tenor:
            continue
        byTenor.setdefault(str(tenor), []).append(row)
    out: dict[tuple[str, str], float] = {}
    for tenor, rows in byTenor.items():
        md0 = _toDate(rows[0].get('maturity_date'))
        dte = (md0 - asof).days if md0 else -1
        if dte < 0:
            continue
        tExp = max(dte, 1) / 365.0
        one = _repIvOneTenor(rows, quoteByTs, spot, rate, tExp, atmBand)
        for cp, iv in one.items():
            out[(tenor, cp)] = iv
    return out


def _repIvOneTenor(
    rows: list[dict[str, Any]],
    quoteByTs: dict[str, dict[str, Any]],
    spot: float,
    rate: float,
    timeToExpiry: float,
    atmBand: float,
) -> dict[str, float]:
    import pandas as pd
    from option_iv import _weighted_median, estimate_implied_forward, implied_volatility

    calls: dict[float, dict[str, float]] = {}
    puts: dict[float, dict[str, float]] = {}
    for row in rows:
        tsCode = str(row.get('ts_code') or '').strip()
        q = quoteByTs.get(tsCode)
        if not q:
            continue
        try:
            stk = float(row.get('exercise_price') or row.get('strike'))
            px = float(q['close'])
        except (TypeError, ValueError):
            continue
        if stk <= 0 or px <= 0:
            continue
        cp = str(row.get('call_put') or '').strip().upper()[:1]
        vol = float(q.get('vol') or 0.0)
        oi = float(q.get('oi') or 0.0)
        packed = {'price': px, 'vol': max(vol, 0.0), 'oi': max(oi, 0.0)}
        if cp == 'C':
            calls[stk] = packed
        elif cp == 'P':
            puts[stk] = packed
    pairRows = []
    for stk in set(calls) & set(puts):
        pairRows.append(
            {
                'strike': stk,
                'call_price': calls[stk]['price'],
                'put_price': puts[stk]['price'],
                'volume': min(calls[stk]['vol'], puts[stk]['vol']),
                'open_interest': min(calls[stk]['oi'], puts[stk]['oi']),
            }
        )
    fwd = float(spot)
    if len(pairRows) >= 2:
        try:
            est = estimate_implied_forward(
                pd.DataFrame(pairRows),
                spot=float(spot),
                time_to_expiry=timeToExpiry,
                rate=float(rate),
            )
            if est.forward > 0:
                fwd = float(est.forward)
        except Exception:
            pass
    result: dict[str, float] = {}
    for cp, bucket in (('C', calls), ('P', puts)):
        ivs: list[float] = []
        wts: list[float] = []
        for stk, packed in bucket.items():
            try:
                moneyness = abs(math.log(stk / fwd))
            except (ValueError, ZeroDivisionError):
                continue
            if moneyness > atmBand:
                continue
            iv = implied_volatility(
                price=packed['price'],
                forward=fwd,
                strike=stk,
                time_to_expiry=timeToExpiry,
                rate=float(rate),
                call_put=cp,
            )
            if iv is None or not math.isfinite(iv) or iv <= 0:
                continue
            ivs.append(float(iv))
            wts.append(math.sqrt(packed['vol'] + 1.0) * math.sqrt(packed['oi'] + 1.0))
        if not ivs:
            continue
        med = _weighted_median(np.asarray(ivs, dtype=float), np.asarray(wts, dtype=float))
        if math.isfinite(med) and med > 0:
            result[cp] = float(med)
    return result


def overlayLiveIvOnStats(stats: dict[str, Any], liveIv: float | None, asofLabel: str) -> None:
    """用盘中代表 IV 替换「最新」并重算 Per / σ 越阈；Min/Med/σ 分位仍用日频样本。"""
    if liveIv is None or not math.isfinite(liveIv) or liveIv <= 0:
        return
    hist = np.asarray(stats.get('_hist_vals') or [], dtype=float)
    hist = hist[np.isfinite(hist) & (hist > 0)]
    current = float(liveIv)
    stats['current'] = _fmtIvPct(current)
    stats['current_raw'] = current
    stats['asof'] = asofLabel
    if hist.size == 0:
        return
    # 盘中 IV 覆盖后三窗口必须同步重算，否则 Per 列还挂在昨收上
    perKey = stats.get('_per_key')
    windows = ivWindows.peekCached(tuple(perKey)) if perKey else None
    if windows is not None:
        stats.update(_perCells(windows, current))
    m3, m3Title, m3Raw = _empSigmaBand(hist, -3.0)
    m2, m2Title, m2Raw = _empSigmaBand(hist, -2.0)
    p2, p2Title, p2Raw = _empSigmaBand(hist, 2.0)
    p3, p3Title, p3Raw = _empSigmaBand(hist, 3.0)
    m3Cls, m3Tip = _sigmaBreachCls(current, m3Raw, 'lo3')
    m2Cls, m2Tip = _sigmaBreachCls(current, m2Raw, 'lo2')
    p2Cls, p2Tip = _sigmaBreachCls(current, p2Raw, 'hi2')
    p3Cls, p3Tip = _sigmaBreachCls(current, p3Raw, 'hi3')
    stats['m3'], stats['m3_title'], stats['m3_cls'] = m3, (m3Title + (' · ' + m3Tip if m3Tip else '')), m3Cls
    stats['m2'], stats['m2_title'], stats['m2_cls'] = m2, (m2Title + (' · ' + m2Tip if m2Tip else '')), m2Cls
    stats['p2'], stats['p2_title'], stats['p2_cls'] = p2, (p2Title + (' · ' + p2Tip if p2Tip else '')), p2Cls
    stats['p3'], stats['p3_title'], stats['p3_cls'] = p3, (p3Title + (' · ' + p3Tip if p3Tip else '')), p3Cls


def _refreshAxisFromLive(axis: dict[str, Any], callIv: float | None, putIv: float | None) -> None:
    mid = midIvRaw(callIv, putIv)
    hv = axis.get('hv_raw')
    if mid is not None and hv is not None:
        try:
            hvF = float(hv)
        except (TypeError, ValueError):
            hvF = None
        if hvF is not None and math.isfinite(hvF) and hvF > 0:
            delta = (mid - hvF) * 100.0
            muted = bool(axis.get('muted'))
            axis['delta'] = _fmtDeltaPp(delta)
            axis['delta_title'] = f'中轴 IV − HV = {delta:+.1f} pp（盘中 IV）'
            axis['delta_cls'] = '' if muted else _deltaCls(delta)
    attachAxisSkew(axis, callIv, putIv)


def _stripIvHistVals(blocks: list[dict[str, Any]]) -> None:
    for block in blocks:
        for tenor in block.get('tenors') or []:
            for side in ('call', 'put'):
                stats = tenor.get(side)
                if isinstance(stats, dict):
                    stats.pop('_hist_vals', None)
                    stats.pop('_per_key', None)


def _fetchEtfLiveChain(cur: Any, code6: str) -> list[dict[str, Any]]:
    """在市 C/P 全链（含 A、任意乘数）；展示过滤 ≠ IV 宇宙。"""
    cur.execute(
        """
        SELECT ts_code, call_put, exercise_price, maturity_date
        FROM opt_basic
        WHERE opt_code LIKE %s
          AND delist_date IS NOT NULL AND delist_date >= CURDATE()
          AND UPPER(TRIM(COALESCE(call_put, ''))) IN ('C', 'P')
        """,
        (f'%{code6}%',),
    )
    return list(cur.fetchall() or [])


def _cachedEtfLiveChain(cur: Any, tsCode: str) -> list[dict[str, Any]]:
    code6 = tsCode.split('.', 1)[0]
    now = time.monotonic()
    with _liveChainLock:
        hit = _liveChainCache.get(tsCode)
        if hit is not None and (now - hit[0]) < _LIVE_CHAIN_TTL_SEC:
            return hit[1]
    rows = _fetchEtfLiveChain(cur, code6)
    with _liveChainLock:
        _liveChainCache[tsCode] = (time.monotonic(), rows)
    return rows


def applyLiveIvToBlocks(
    blocks: list[dict[str, Any]],
    snapshot: dict[str, Any] | None,
    chainByUnderlying: dict[str, list[dict[str, Any]]],
    *,
    asof: date,
    rate: float,
    asofLabel: str,
) -> int:
    """五只 ETF：用 QMT 最新价重算代表 IV，覆盖「最新」/Per/斜/溢。返回命中期限数。"""
    quoteByTs = quoteMapFromDaqmtSnapshot(snapshot, asof=asof)
    if not quoteByTs:
        return 0
    hitN = 0
    for block in blocks:
        tsCode = str(block.get('underlying') or '')
        if tsCode not in _ETF_IV_LIVE_UNDERLYINGS:
            continue
        spot = _spotFromSnapshot(snapshot, tsCode)
        if spot is None:
            continue
        contracts = chainByUnderlying.get(tsCode) or []
        if not contracts:
            continue
        liveMap = liveRepIvByTenor(contracts, quoteByTs, spot, rate, asof)
        if not liveMap:
            continue
        blockHit = 0
        for tenorRow in block.get('tenors') or []:
            tenor = str(tenorRow.get('tenor') or '')
            liveC = liveMap.get((tenor, 'C'))
            liveP = liveMap.get((tenor, 'P'))
            if liveC is None and liveP is None:
                continue
            overlayLiveIvOnStats(tenorRow.get('call') or {}, liveC, asofLabel)
            overlayLiveIvOnStats(tenorRow.get('put') or {}, liveP, asofLabel)
            _refreshAxisFromLive(
                tenorRow.get('axis') or {},
                tenorRow.get('call', {}).get('current_raw'),
                tenorRow.get('put', {}).get('current_raw'),
            )
            blockHit += 1
            hitN += 1
        if blockHit:
            block['asof'] = asofLabel
    return hitN


def _blocksFromPanelRows(
    rowsByUnderlying: dict[str, list[dict[str, Any]]],
    closeMap: dict[str, tuple[list[date], np.ndarray]] | None = None,
) -> tuple[list[dict[str, Any]], date | None]:
    blocks: list[dict[str, Any]] = []
    globalAsof: date | None = None
    closeMap = closeMap or {}
    for tsCode, nameCn in OPT_IV_BOARD_UNDERLYINGS:
        rows = rowsByUnderlying.get(tsCode) or []
        closeDates, logret = closeMap.get(tsCode, ([], np.asarray([], dtype=float)))
        byKey: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in rows:
            tenor = str(r.get('tenor') or '').strip()
            cp = str(r.get('call_put') or '').strip().upper()[:1]
            if tenor not in TENOR_CN or cp not in ('C', 'P'):
                continue
            byKey.setdefault((tenor, cp), []).append(r)
        tenorRows: list[dict[str, Any]] = []
        blockAsof = ''
        for tenor in tenorOrderForUnderlying(tsCode):
            callRows = byKey.get((tenor, 'C'), [])
            putRows = byKey.get((tenor, 'P'), [])
            callStats = _sideStats(callRows, underlyingTs=tsCode, tenor=tenor, callPut='C')
            putStats = _sideStats(putRows, underlyingTs=tsCode, tenor=tenor, callPut='P')
            if callStats.get('asof'):
                blockAsof = max(blockAsof, str(callStats['asof']))
            if putStats.get('asof'):
                blockAsof = max(blockAsof, str(putStats['asof']))
            asofD = _toDate(callStats.get('asof')) or _toDate(putStats.get('asof'))
            if callStats.get('asof') and putStats.get('asof'):
                asofD = max(
                    _toDate(callStats['asof']) or date.min,
                    _toDate(putStats['asof']) or date.min,
                )
                if asofD == date.min:
                    asofD = None
            axis = attachAxisSkew(
                _buildAxisStats(
                    closeDates,
                    logret,
                    asofD,
                    _latestDte(callRows + putRows),
                    midIvRaw(callStats.get('current_raw'), putStats.get('current_raw')),
                    tenor,
                ),
                callStats.get('current_raw'),
                putStats.get('current_raw'),
            )
            tenorRows.append(
                {
                    'tenor': tenor,
                    'tenor_cn': TENOR_CN[tenor],
                    'call': callStats,
                    'put': putStats,
                    'axis': axis,
                }
            )
        if blockAsof:
            d = _toDate(blockAsof)
            if d and (globalAsof is None or d > globalAsof):
                globalAsof = d
        blocks.append(
            {
                'underlying': tsCode,
                'code6': tsCode.split('.')[0],
                'name': nameCn,
                'asof': blockAsof,
                'tenors': tenorRows,
                'has_data': any((t['call']['n'] or t['put']['n']) for t in tenorRows),
            }
        )
    return blocks, globalAsof


_IV_BOARD_CACHE_TTL_SEC = 180.0
_ivBoardCacheLock = threading.Lock()
_ivBoardDailyCache: tuple[float, dict[str, Any]] | None = None
_ivBoardLiveCache: tuple[float, dict[str, Any]] | None = None
_ivBoardDailyRefreshing = False
_ivBoardLiveRefreshing = False
_ivBoardDailyComputeLock = threading.Lock()
_ivBoardLiveComputeLock = threading.Lock()


def _queryDailyIvBoardUncached() -> dict[str, Any]:
    """日频八块 + ETF 拉链；保留 _hist_vals 供盘中叠价。"""
    blocks: list[dict[str, Any]] = []
    err: str | None = None
    globalAsof: date | None = None
    chainByUnderlying: dict[str, list[dict[str, Any]]] = {}
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE, cursorclass=pymysql.cursors.DictCursor)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES LIKE 'opt_iv_panel'")
                if not cur.fetchone():
                    return {
                        'error': '尚未建表 opt_iv_panel，请先运行 opt_iv_panel_to_mysql.py',
                        'blocks': [],
                        'asof': '',
                        'rate_label': '',
                        'rf': 0.015,
                        'chains': {},
                    }
                rowsByUnderlying: dict[str, list[dict[str, Any]]] = {}
                for tsCode, _nameCn in OPT_IV_BOARD_UNDERLYINGS:
                    rowsByUnderlying[tsCode] = _fetchPanelRows(cur, tsCode)
                closeMap = _fetchCloseMap(
                    cur,
                    [ts for ts, _name in OPT_IV_BOARD_UNDERLYINGS],
                )
                blocks, globalAsof = _blocksFromPanelRows(rowsByUnderlying, closeMap)
                for tsCode, _name in OPT_IV_BOARD_UNDERLYINGS:
                    if tsCode in _ETF_IV_LIVE_UNDERLYINGS:
                        chainByUnderlying[tsCode] = _cachedEtfLiveChain(cur, tsCode)
        finally:
            conn.close()
    except Exception as e:
        logger.exception('opt_iv_board 查询失败: %s', e)
        err = str(e)
        globalAsof = None
    rateLabel = ''
    rf = 0.015
    try:
        from option_iv import getCnGb10yRate, getCnGb10yRateMeta

        rf, src = getCnGb10yRateMeta()
        if 'fallback' in str(src):
            rateLabel = f'r=fallback {rf * 100:.2f}%'
        else:
            rateLabel = f'r=中债10Y {rf * 100:.2f}%'
        rf = float(getCnGb10yRate(asof=date.today()))
    except Exception:
        rateLabel = rateLabel or ''
    return {
        'error': err,
        'blocks': blocks,
        'asof': globalAsof.isoformat() if globalAsof else '',
        'rate_label': rateLabel,
        'rf': float(rf),
        'chains': chainByUnderlying,
    }


def _storeDailyIvBoard(data: dict[str, Any]) -> dict[str, Any]:
    global _ivBoardDailyCache
    with _ivBoardCacheLock:
        _ivBoardDailyCache = (time.monotonic(), data)
    return data


def _refreshDailyIvBoardWorker() -> None:
    global _ivBoardDailyRefreshing
    try:
        _storeDailyIvBoard(_queryDailyIvBoardUncached())
    except Exception:
        logger.exception('IV 日频看板后台刷新失败')
    finally:
        with _ivBoardCacheLock:
            _ivBoardDailyRefreshing = False


def _getDailyIvBoard() -> dict[str, Any]:
    """日频八块：新鲜命中直接返；过期秒返旧值并后台重算；未命中才阻塞。"""
    global _ivBoardDailyRefreshing
    startBg = False
    with _ivBoardCacheLock:
        hit = _ivBoardDailyCache
        now = time.monotonic()
        if hit is not None and (now - hit[0]) < _IV_BOARD_CACHE_TTL_SEC:
            return hit[1]
        stale = hit[1] if hit is not None else None
        if stale is not None and not _ivBoardDailyRefreshing:
            _ivBoardDailyRefreshing = True
            startBg = True
    if stale is not None:
        if startBg:
            threading.Thread(
                target=_refreshDailyIvBoardWorker,
                daemon=True,
                name='opt-iv-daily-swr',
            ).start()
        return stale
    with _ivBoardDailyComputeLock:
        with _ivBoardCacheLock:
            hit = _ivBoardDailyCache
            now = time.monotonic()
            if hit is not None and (now - hit[0]) < _IV_BOARD_CACHE_TTL_SEC:
                return hit[1]
        return _storeDailyIvBoard(_queryDailyIvBoardUncached())


def _finalizeIvPayload(
    daily: dict[str, Any],
    snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    blocks = copy.deepcopy(daily.get('blocks') or [])
    asofTxt = str(daily.get('asof') or '')
    quoteSource = 'opt_iv_panel'
    chains = daily.get('chains') or {}
    if isinstance(snapshot, dict) and chains:
        clock = str(snapshot.get('optWall') or snapshot.get('etfWall') or '')
        liveLabel = f'实时 {clock[11:19]}' if len(clock) >= 19 else '实时'
        hitN = applyLiveIvToBlocks(
            blocks,
            snapshot,
            chains,
            asof=date.today(),
            rate=float(daily.get('rf') or 0.015),
            asofLabel=liveLabel,
        )
        if hitN > 0:
            quoteSource = 'daqmt'
            asofTxt = liveLabel
    _stripIvHistVals(blocks)
    return {
        'error': daily.get('error'),
        'blocks': blocks,
        'asof': asofTxt,
        'rate_label': daily.get('rate_label') or '',
        'quote_source': quoteSource,
    }


def _storeLiveIvBoard(data: dict[str, Any]) -> dict[str, Any]:
    global _ivBoardLiveCache
    with _ivBoardCacheLock:
        _ivBoardLiveCache = (time.monotonic(), data)
    return data


def clearOptIvBoardCache() -> None:
    """Per C 锚点变更后丢掉日频/直播看板，避免 304 旧分位。"""
    global _ivBoardDailyCache, _ivBoardLiveCache
    with _ivBoardCacheLock:
        _ivBoardDailyCache = None
        _ivBoardLiveCache = None


def _computeIvLivePayload() -> dict[str, Any]:
    from rt_monitor.services.opt_t_board import fetchDaqmtQuoteSnapshot

    daily = _getDailyIvBoard()
    snap = fetchDaqmtQuoteSnapshot()
    wantLive = bool(isinstance(snap, dict) and snap.get('opt'))
    return _storeLiveIvBoard(_finalizeIvPayload(daily, snap if wantLive else None))


def _refreshIvLiveWorker() -> None:
    global _ivBoardLiveRefreshing
    try:
        _computeIvLivePayload()
    except Exception:
        logger.exception('IV 直播看板后台刷新失败')
    finally:
        with _ivBoardCacheLock:
            _ivBoardLiveRefreshing = False


def peekOptIvBoardLiveHtml() -> str:
    """壳页秒开：已有直播缓存则直接取 HTML 片段，不触发重算。"""
    with _ivBoardCacheLock:
        hit = _ivBoardLiveCache
    if hit is None:
        return ''
    data = hit[1]
    if not isinstance(data, dict):
        return ''
    frag = data.get('_htmlFrag')
    if isinstance(frag, str) and frag:
        return frag
    try:
        return renderOptIvBoardFragment(data)
    except Exception:
        logger.exception('peek IV HTML 失败')
        return ''


def queryOptIvBoardPayload() -> dict[str, Any]:
    """IV 看板：日频 TTL 180s；直播 TTL 4s。过期秒返旧值，后台叠 QMT。"""
    global _ivBoardLiveRefreshing
    startBg = False
    with _ivBoardCacheLock:
        hit = _ivBoardLiveCache
        now = time.monotonic()
        if hit is not None and (now - hit[0]) < _IV_BOARD_LIVE_CACHE_TTL_SEC:
            return hit[1]
        stale = hit[1] if hit is not None else None
        if stale is not None and not _ivBoardLiveRefreshing:
            _ivBoardLiveRefreshing = True
            startBg = True
    if stale is not None:
        if startBg:
            threading.Thread(
                target=_refreshIvLiveWorker,
                daemon=True,
                name='opt-iv-live-swr',
            ).start()
        return stale
    with _ivBoardLiveComputeLock:
        with _ivBoardCacheLock:
            hit = _ivBoardLiveCache
            now = time.monotonic()
            if hit is not None and (now - hit[0]) < _IV_BOARD_LIVE_CACHE_TTL_SEC:
                return hit[1]
        return _computeIvLivePayload()


def renderOptIvBoardFragment(payload: dict[str, Any] | None = None) -> str:
    data = payload if payload is not None else queryOptIvBoardPayload()
    frag = data.get('_htmlFrag') if isinstance(data, dict) else None
    if isinstance(frag, str) and frag:
        return frag
    html = render_template(
        'rt_monitor_opt_iv_board.html',
        opt_iv_blocks=data.get('blocks') or [],
        opt_iv_error=data.get('error'),
        opt_iv_asof=data.get('asof') or '',
        opt_iv_rate_label=data.get('rate_label') or '',
    )
    if isinstance(data, dict):
        from rt_monitor.services.opt_t_board import optTBoardHtmlEtag

        data['_htmlFrag'] = html
        data['_htmlEtag'] = optTBoardHtmlEtag(html)
    return html


def _resolveIvUnderlying(raw: str | None) -> str | None:
    """接受 510050 / 510050.SH / 000300；仅允许看板八标的。"""
    s = (raw or '').strip().upper()
    if not s:
        return None
    if '.' not in s and len(s) == 6 and s.isdigit():
        for ts, _ in OPT_IV_BOARD_UNDERLYINGS:
            if ts.startswith(s + '.'):
                return ts
        return None
    allow = {ts for ts, _ in OPT_IV_BOARD_UNDERLYINGS}
    return s if s in allow else None


def queryOptIvSeries(
    underlyingRaw: str | None,
    tenor: str | None,
    callPut: str | None,
) -> dict[str, Any]:
    """
    单侧（认购/认沽 × 期限）全历史 IV + 现货 + 随后 RV。
    随后 RV 用该期限最新 DTE 对齐的固定 n，便于和 IV 同轴对比。
    """
    underlying = _resolveIvUnderlying(underlyingRaw)
    if not underlying:
        return {'ok': False, 'error': '标的无效或不在 IV 看板范围'}
    tenorKey = (tenor or '').strip()
    if tenorKey not in TENOR_CN:
        return {'ok': False, 'error': '期限无效'}
    cp = (callPut or '').strip().upper()[:1]
    if cp not in ('C', 'P'):
        return {'ok': False, 'error': 'call_put 须为 C 或 P'}
    nameCn = dict(OPT_IV_BOARD_UNDERLYINGS).get(underlying, underlying)
    sideCn = '认购' if cp == 'C' else '认沽'
    rowsOut: list[dict[str, Any]] = []
    vrpPayload: dict[str, Any] = {
        'n_window': None,
        'dte': None,
        'sample': 0,
        'median_pp': None,
        'mean_pp': None,
        'win_pct': None,
        'text': '',
    }
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE, cursorclass=pymysql.cursors.DictCursor)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES LIKE 'opt_iv_panel'")
                if not cur.fetchone():
                    return {'ok': False, 'error': '尚未建表 opt_iv_panel'}
                cur.execute(
                    """
                    SELECT trade_date, representative_iv, spot, dte
                    FROM opt_iv_panel
                    WHERE underlying=%s
                      AND tenor=%s
                      AND call_put=%s
                      AND representative_iv IS NOT NULL
                      AND representative_iv > 0
                    ORDER BY trade_date
                    """,
                    (underlying, tenorKey, cp),
                )
                rawRows: list[dict[str, Any]] = []
                for r in cur.fetchall() or []:
                    d = _toDate(r.get('trade_date'))
                    if d is None:
                        continue
                    try:
                        ivF = float(r.get('representative_iv'))
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(ivF) or ivF <= 0:
                        continue
                    spotV: float | None = None
                    try:
                        if r.get('spot') is not None:
                            spotF = float(r.get('spot'))
                            if math.isfinite(spotF) and spotF > 0:
                                spotV = spotF
                    except (TypeError, ValueError):
                        spotV = None
                    try:
                        dteV = int(r['dte']) if r.get('dte') is not None else None
                    except (TypeError, ValueError):
                        dteV = None
                    rawRows.append(
                        {
                            'date': d,
                            'iv': ivF,
                            'spot': spotV,
                            'dte': dteV,
                        }
                    )
                closeMap = _fetchCloseMap(cur, [underlying])
                dates, logret = closeMap.get(
                    underlying,
                    ([], np.asarray([], dtype=float)),
                )
                dateToIdx = {d: i for i, d in enumerate(dates)}
                latestDte = rawRows[-1]['dte'] if rawRows else None
                nWindow = alignedHvWindowN(latestDte)
                ivs: list[float] = []
                rvs: list[float] = []
                for item in rawRows:
                    rvF: float | None = None
                    if nWindow is not None:
                        idx = dateToIdx.get(item['date'])
                        if idx is not None:
                            rvF = subsequentAnnRv(logret, idx, nWindow)
                    rowsOut.append(
                        {
                            'trade_date': item['date'].isoformat(),
                            'iv': round(item['iv'] * 100.0, 4),
                            'spot': item['spot'],
                            'rv': None if rvF is None else round(rvF * 100.0, 4),
                        }
                    )
                    ivs.append(item['iv'])
                    rvs.append(math.nan if rvF is None else rvF)
                stats = summarizeVrp(np.asarray(ivs, dtype=float), np.asarray(rvs, dtype=float))
                if nWindow is not None:
                    vrpPayload = {
                        'n_window': nWindow,
                        'dte': latestDte,
                        'sample': int(stats['n']),
                        'median_pp': None
                        if stats['median'] is None
                        else round(stats['median'] * 100.0, 2),
                        'mean_pp': None
                        if stats['mean'] is None
                        else round(stats['mean'] * 100.0, 2),
                        'win_pct': None
                        if stats['win_rate'] is None
                        else round(stats['win_rate'] * 100.0, 1),
                        'text': _fmtVrpLine(nWindow, stats),
                    }
        finally:
            conn.close()
    except Exception as e:
        logger.exception('opt_iv_series 查询失败: %s', e)
        return {'ok': False, 'error': str(e)}
    return {
        'ok': True,
        'underlying': underlying,
        'code6': underlying.split('.')[0],
        'name': nameCn,
        'tenor': tenorKey,
        'tenor_cn': TENOR_CN[tenorKey],
        'call_put': cp,
        'side_cn': sideCn,
        'rows': rowsOut,
        'n': len(rowsOut),
        'vrp': vrpPayload,
    }


# 代码更新时间：2026-08-06 08:36
# 代码更新时间：2026-08-07 12:30 IV 看板扩至八标的（+沪深300指/上证50指/中证1000指）
# 代码更新时间：2026-08-07 12:47 ETF 五块显示名改为代码（510050/510300/510500/159915/588000）
# 代码更新时间：2026-08-07 12:59 IV 块顺序调整；000300 显示名「沪深300指数」
# 代码更新时间：2026-08-07 13:03 TENOR 扩至 6（下下月/下下季）；ETF 缺档显示 —
# 代码更新时间：2026-08-09 09:05 副标题展示中债10Y r（与 T 型 IV 共用）
# 代码更新时间：2026-08-09 10:21 与按日中债10Y面板重算对齐

# 代码更新时间：2026-08-07 17:11 IV 看板按标的类型输出期限档：ETF 4 / 中金所 6，不再给 ETF 填空档
# 代码更新时间：2026-08-14 22:20 全表一次拉取 + TTL 缓存
# 代码更新时间：2026-08-24 08:03 中轴 HV/Δ/HV Per；弹窗随后 RV 与历史 VRP
# 代码更新时间：2026-08-24 08:56 中轴斜度：认沽最新 − 认购最新
# 代码更新时间：2026-08-24 21:12 五只 ETF「最新」IV 叠 QMT 快照；中金所仍日频
# 代码更新时间：2026-08-25 14:52 实时 IV 跳过非当日/中价冒充 tick
# 代码更新时间：2026-08-25 18:27 实时 IV 拒 ETF 无量占位现货
# 代码更新时间：2026-08-25 20:41 活链不过滤 A/乘数；live 缓存命中不拉快照
# 代码更新时间：2026-08-26 06:17 活链含摘牌当日
# 代码更新时间：2026-08-27 14:10 直播 TTL 8s；HTML 片段缓存供 304
# 代码更新时间：2026-08-27 14:20 日频/直播 SWR：过期秒返，避免 25s 日频重算堵轮询
# 代码更新时间：2026-08-28 06:57 直播轮询与 TTL 8s 对齐，不再按 3s 打缓存
# 代码更新时间：2026-08-28 07:08 直播 TTL 4s，与 T 型同源 poll_ms
# 代码更新时间：2026-09-20 21:26 Per 拆 Per A/C/1y；键名统一为 pera；盘中覆盖同步重算三列
# 代码更新时间：2026-09-21 07:03 Per C 锚点保存时清空日频/直播缓存；≥100% 分位列一位小数
# 代码更新时间：2026-09-21 10:07 peekOptIvBoardLiveHtml 供壳页秒开内嵌
# 代码更新时间：2026-09-23 06:54 中轴 IV−HV 展示名改为「溢」
