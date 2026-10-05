"""ETF/中金所股指期权的隐含远期、Black-76隐含波动率和无前视历史分位数。"""

from __future__ import annotations

import logging
import math
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

logger = logging.getLogger(__name__)

# 中债国债 10Y：CSV 历史种子 + yc_cb 增量；失败回退 DEFAULT_RISK_FREE_RATE
DEFAULT_RISK_FREE_RATE = 0.015
_RF_LOCK = threading.Lock()
_RF_CACHE: dict[str, tuple[float, str]] = {}  # YYYY-MM-DD -> (rate, source)
_RF_SERIES: pd.Series | None = None  # DatetimeIndex -> rate(小数)
_RF_SERIES_META: str = ""


def defaultCnGb10yCsvPath() -> Path:
    """项目内历史种子：data/rates/cn_gb10y_yield.csv。"""
    return Path(__file__).resolve().parent / "data" / "rates" / "cn_gb10y_yield.csv"


# ETF 等：当月、下月、当季、下季（近月链最多 2）
TENOR_NAMES = (
    "current_month",
    "next_month",
    "current_quarter",
    "next_quarter",
)

# 中金所股指期权：近月链最多 3 + 季月最多 3
TENOR_NAMES_CFFEX = (
    "current_month",
    "next_month",
    "next_next_month",
    "current_quarter",
    "next_quarter",
    "next_next_quarter",
)

CFFEX_INDEX_UNDERLYINGS = frozenset({"000300.SH", "000016.SH", "000852.SH"})
CFFEX_INDEX_CODE6 = frozenset({"000300", "000016", "000852"})


def underlyingIsCffexIndex(underlying: str | None) -> bool:
    if not underlying:
        return False
    raw = str(underlying).strip().upper()
    if raw in CFFEX_INDEX_UNDERLYINGS:
        return True
    code6 = raw.split(".", 1)[0]
    return code6 in CFFEX_INDEX_CODE6


def _tusharePro(pro: object | None = None) -> object:
    if pro is not None:
        return pro
    import tushare as ts

    token = (os.environ.get("TUSHARE_TOKEN") or os.environ.get("TS_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("未设置 TUSHARE_TOKEN/TS_TOKEN")
    return ts.pro_api(token)


def _yieldPctToRate(y: float) -> float:
    """中债 yield 多为百分比（如 1.71）；偶发已是小数。"""
    rate = float(y) / 100.0 if float(y) > 0.5 else float(y)
    if rate <= 0 or rate > 0.2:
        raise ValueError(f"中债10Y异常值: {y}")
    return rate


def _fetchYcCb10yFrame(
    startDate: date,
    endDate: date,
    *,
    pro: object | None = None,
) -> pd.DataFrame:
    """按年分片拉取 yc_cb 10Y 到期（避开单次 2000 行截断）。"""
    if endDate < startDate:
        return pd.DataFrame(columns=["trade_date", "yield_pct"])
    api = _tusharePro(pro)
    chunks: list[pd.DataFrame] = []
    for year in range(startDate.year, endDate.year + 1):
        segStart = date(year, 1, 1)
        segEnd = date(year, 12, 31)
        if year == startDate.year:
            segStart = startDate
        if year == endDate.year:
            segEnd = endDate
        frame = api.yc_cb(
            ts_code="1001.CB",
            curve_type="0",
            curve_term=10,
            start_date=segStart.strftime("%Y%m%d"),
            end_date=segEnd.strftime("%Y%m%d"),
            fields="trade_date,yield",
        )
        if frame is not None and not frame.empty:
            chunks.append(frame)
    if not chunks:
        return pd.DataFrame(columns=["trade_date", "yield_pct"])
    out = pd.concat(chunks, ignore_index=True)
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce")
    out["yield_pct"] = pd.to_numeric(out["yield"], errors="coerce")
    out = out.dropna(subset=["trade_date", "yield_pct"]).drop_duplicates("trade_date")
    return out.loc[:, ["trade_date", "yield_pct"]].sort_values("trade_date")


def loadCnGb10yRateSeries(
    *,
    csvPath: str | Path | None = None,
    pro: object | None = None,
    refreshYcCb: bool = True,
    forceReload: bool = False,
) -> pd.Series:
    """
    加载中债 10Y 到期收益率序列（小数，DatetimeIndex）。
    历史：data/rates/cn_gb10y_yield.csv；增量：yc_cb 覆盖同日并续新。
    """
    global _RF_SERIES, _RF_SERIES_META
    with _RF_LOCK:
        if _RF_SERIES is not None and not forceReload:
            return _RF_SERIES.copy()

    path = Path(csvPath) if csvPath is not None else defaultCnGb10yCsvPath()
    frames: list[pd.DataFrame] = []
    csvRows = 0
    if path.is_file():
        seed = pd.read_csv(path)
        if {"trade_date", "yield_pct"}.issubset(seed.columns):
            seed = seed.loc[:, ["trade_date", "yield_pct"]].copy()
            seed["trade_date"] = pd.to_datetime(seed["trade_date"], errors="coerce")
            seed["yield_pct"] = pd.to_numeric(seed["yield_pct"], errors="coerce")
            seed = seed.dropna().drop_duplicates("trade_date")
            frames.append(seed)
            csvRows = len(seed)
        else:
            logger.warning("中债10Y CSV 缺列 trade_date/yield_pct: %s", path)
    else:
        logger.warning("中债10Y CSV 不存在: %s", path)

    ycRows = 0
    if refreshYcCb:
        try:
            today = date.today()
            if frames:
                csvMax = pd.Timestamp(frames[0]["trade_date"].max()).date()
                # 重叠回拉约 30 日便于覆盖修正，再以 yc_cb 覆盖同日
                ycStart = csvMax - timedelta(days=30)
            else:
                ycStart = date(2016, 6, 13)
            yc = _fetchYcCb10yFrame(ycStart, today, pro=pro)
            if not yc.empty:
                frames.append(yc)
                ycRows = len(yc)
        except Exception as exc:
            logger.warning("yc_cb 增量失败，仅用 CSV/回退: %s", exc)

    if not frames:
        idx = pd.DatetimeIndex([pd.Timestamp(date.today())])
        series = pd.Series([DEFAULT_RISK_FREE_RATE], index=idx, dtype=float)
        meta = f"fallback_only:{DEFAULT_RISK_FREE_RATE:.4f}"
    else:
        merged = pd.concat(frames, ignore_index=True)
        # 后写覆盖：yc_cb 在后 → 同日以 yc_cb 为准
        merged = merged.drop_duplicates("trade_date", keep="last").sort_values("trade_date")
        rates = merged["yield_pct"].map(_yieldPctToRate)
        series = pd.Series(
            rates.to_numpy(dtype=float),
            index=pd.DatetimeIndex(pd.to_datetime(merged["trade_date"]).dt.normalize()),
            dtype=float,
        )
        series = series.sort_index()
        meta = (
            f"csv_rows={csvRows};yc_rows={ycRows};"
            f"merged={len(series)};"
            f"{series.index.min().date()}~{series.index.max().date()}"
        )

    with _RF_LOCK:
        _RF_SERIES = series
        _RF_SERIES_META = meta
        _RF_CACHE.clear()
    logger.info("中债10Y序列已加载: %s", meta)
    return series.copy()


def lookupCnGb10yRate(
    asof: date | datetime | pd.Timestamp | str,
    *,
    series: pd.Series | None = None,
    fallback: float = DEFAULT_RISK_FREE_RATE,
) -> float:
    """按日取 rate：精确匹配，否则取 asof 当日及之前最近一期（ffill）。"""
    ser = series if series is not None else loadCnGb10yRateSeries()
    if ser.empty:
        return float(fallback)
    ts = pd.Timestamp(asof).normalize()
    if ts in ser.index:
        return float(ser.loc[ts])
    prev = ser.loc[:ts]
    if not prev.empty:
        return float(prev.iloc[-1])
    return float(fallback)


def getCnGb10yRate(
    *,
    asof: date | None = None,
    pro: object | None = None,
    fallback: float = DEFAULT_RISK_FREE_RATE,
) -> float:
    """
    取中债 10Y 到期收益率（小数）。
    数据源：本地 CSV 种子 + Tushare yc_cb 增量；按日 ffill；失败回退 fallback。
    """
    day = asof or date.today()
    key = day.isoformat()
    with _RF_LOCK:
        hit = _RF_CACHE.get(key)
        if hit is not None:
            return float(hit[0])

    try:
        series = loadCnGb10yRateSeries(pro=pro)
        rate = lookupCnGb10yRate(day, series=series, fallback=fallback)
        source = f"series:{_RF_SERIES_META}"
    except Exception as exc:
        logger.warning("获取中债10Y失败，使用 fallback=%.4f: %s", fallback, exc)
        rate = float(fallback)
        source = f"fallback:{fallback:.4f}"

    with _RF_LOCK:
        _RF_CACHE[key] = (rate, source)
    return float(rate)


def getCnGb10yRateMeta(*, asof: date | None = None) -> tuple[float, str]:
    """返回 (rate, source标签)。"""
    day = asof or date.today()
    rate = getCnGb10yRate(asof=day)
    with _RF_LOCK:
        cached = _RF_CACHE.get(day.isoformat())
        meta = _RF_SERIES_META
    if cached is None:
        return rate, f"fallback:{DEFAULT_RISK_FREE_RATE:.4f}"
    label = str(cached[1])
    if meta and not label.startswith("fallback"):
        label = f"中债10Y {rate * 100:.2f}% ({meta})"
    return float(cached[0]), label


def _toTimestampDate(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.normalize()


def _isConsecutiveCalendarMonth(earlier: pd.Timestamp, later: pd.Timestamp) -> bool:
    """later 是否恰为 earlier 的下一个自然月合约月。"""
    return (later.year - earlier.year) * 12 + (later.month - earlier.month) == 1


def calendarTenorMapForMaturities(
    maturities: Iterable[object],
    *,
    isCffex: bool,
) -> dict[pd.Timestamp, str]:
    """
    按日历规则给到期日打期限桶（非简单取前 N）。

    - 近月链：从最近到期日起，连续自然月最多 2（ETF）或 3（中金所）→ 当月/下月[/下下月]
    - 其余按到期日升序 → 当季/下季[/下下季]
    """
    mats = sorted({_toTimestampDate(m) for m in maturities})
    if not mats:
        return {}
    maxNear = 3 if isCffex else 2
    nearNames = (
        ["current_month", "next_month", "next_next_month"]
        if isCffex
        else ["current_month", "next_month"]
    )
    quarterNames = (
        ["current_quarter", "next_quarter", "next_next_quarter"]
        if isCffex
        else ["current_quarter", "next_quarter"]
    )
    chain: list[pd.Timestamp] = [mats[0]]
    for m in mats[1:]:
        if len(chain) >= maxNear:
            break
        if _isConsecutiveCalendarMonth(chain[-1], m):
            chain.append(m)
        else:
            break
    chainSet = set(chain)
    rest = [m for m in mats if m not in chainSet]
    out: dict[pd.Timestamp, str] = {}
    for i, m in enumerate(chain[:maxNear]):
        out[m] = nearNames[i]
    for i, m in enumerate(rest[: len(quarterNames)]):
        out[m] = quarterNames[i]
    return out


@dataclass(frozen=True)
class ForwardEstimate:
    forward: float
    pair_count: int
    used_fallback: bool


def black76_price(
    forward: float,
    strike: float,
    time_to_expiry: float,
    rate: float,
    volatility: float,
    call_put: str,
) -> float:
    """Black-76欧式期权价格，输入和输出均为每份标的的价格。"""
    if min(forward, strike, time_to_expiry, volatility) <= 0:
        raise ValueError("forward、strike、time_to_expiry和volatility必须为正")
    kind = call_put.upper()
    if kind not in {"C", "P"}:
        raise ValueError("call_put必须为C或P")
    discount = math.exp(-rate * time_to_expiry)
    sigma_root_t = volatility * math.sqrt(time_to_expiry)
    d1 = (math.log(forward / strike) + 0.5 * volatility**2 * time_to_expiry) / sigma_root_t
    d2 = d1 - sigma_root_t
    if kind == "C":
        return float(discount * (forward * norm.cdf(d1) - strike * norm.cdf(d2)))
    return float(discount * (strike * norm.cdf(-d2) - forward * norm.cdf(-d1)))


def black76_delta(
    forward: float,
    strike: float,
    time_to_expiry: float,
    rate: float,
    volatility: float,
    call_put: str,
) -> float:
    """
    Black-76 现货 Delta（假设 F∝S·e^{rT} 时 ∂/∂S）。
    Call≈N(d1)，Put≈−N(−d1)；量纲约 −1~1。
    """
    if min(forward, strike, time_to_expiry, volatility) <= 0:
        raise ValueError("forward、strike、time_to_expiry和volatility必须为正")
    kind = call_put.upper()
    if kind not in {"C", "P"}:
        raise ValueError("call_put必须为C或P")
    sigma_root_t = volatility * math.sqrt(time_to_expiry)
    d1 = (math.log(forward / strike) + 0.5 * volatility**2 * time_to_expiry) / sigma_root_t
    if kind == "C":
        return float(norm.cdf(d1))
    return float(-norm.cdf(-d1))


def black76_gamma(
    forward: float,
    strike: float,
    time_to_expiry: float,
    volatility: float,
    spot: float | None = None,
) -> float:
    """
    Black-76 现货 Gamma，与 black76_delta 配套（Call/Put 相同）。
    Γ = n(d1) / (S σ √T)；S 用现货，缺省用 forward。
    """
    if min(forward, strike, time_to_expiry, volatility) <= 0:
        raise ValueError("forward、strike、time_to_expiry和volatility必须为正")
    s = float(spot) if spot is not None and float(spot) > 0 else float(forward)
    if s <= 0:
        raise ValueError("spot 必须为正")
    sigmaRootT = volatility * math.sqrt(time_to_expiry)
    d1 = (math.log(forward / strike) + 0.5 * volatility**2 * time_to_expiry) / sigmaRootT
    return float(norm.pdf(d1) / (s * sigmaRootT))


def implied_volatility(
    *,
    price: float,
    forward: float,
    strike: float,
    time_to_expiry: float,
    rate: float,
    call_put: str,
    min_volatility: float = 0.01,
    max_volatility: float = 3.0,
) -> float:
    """反解Black-76 IV；不满足价格边界或无根时返回NaN。"""
    kind = call_put.upper()
    if (
        kind not in {"C", "P"}
        or not np.isfinite(price)
        or min(price, forward, strike, time_to_expiry) <= 0
        or min_volatility <= 0
        or max_volatility <= min_volatility
    ):
        return math.nan
    discount = math.exp(-rate * time_to_expiry)
    lower_bound = discount * max(
        forward - strike if kind == "C" else strike - forward, 0.0
    )
    upper_bound = discount * (forward if kind == "C" else strike)
    tolerance = 1e-12
    if price < lower_bound - tolerance or price > upper_bound + tolerance:
        return math.nan

    def objective(volatility: float) -> float:
        return black76_price(
            forward,
            strike,
            time_to_expiry,
            rate,
            volatility,
            kind,
        ) - price

    low_value = objective(min_volatility)
    high_value = objective(max_volatility)
    if abs(low_value) <= tolerance:
        return min_volatility
    if abs(high_value) <= tolerance:
        return max_volatility
    if low_value * high_value > 0:
        return math.nan
    try:
        return float(
            brentq(objective, min_volatility, max_volatility, xtol=1e-12, rtol=1e-12)
        )
    except (ValueError, RuntimeError):
        return math.nan


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    valid = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    clean_values = values[valid]
    clean_weights = weights[valid]
    if clean_values.size == 0:
        return math.nan
    order = np.argsort(clean_values)
    clean_values = clean_values[order]
    clean_weights = clean_weights[order]
    midpoint = 0.5 * clean_weights.sum()
    return float(clean_values[np.searchsorted(np.cumsum(clean_weights), midpoint, side="left")])


def estimate_implied_forward(
    pairs: pd.DataFrame,
    *,
    spot: float,
    time_to_expiry: float,
    rate: float,
    atm_band: float = 0.15,
    min_pairs: int = 2,
) -> ForwardEstimate:
    """用近ATM认购—认沽平价的加权中位数估计隐含远期。"""
    required = {"strike", "call_price", "put_price"}
    if required.difference(pairs.columns):
        raise ValueError(f"pairs缺少字段: {sorted(required.difference(pairs.columns))}")
    if spot <= 0 or time_to_expiry <= 0:
        raise ValueError("spot和time_to_expiry必须为正")
    frame = pairs.copy()
    for column in required:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.loc[
        (frame["strike"] > 0)
        & (frame["call_price"] > 0)
        & (frame["put_price"] > 0)
        & ((frame["strike"] / spot - 1.0).abs() <= atm_band)
    ].copy()
    if {"volume", "open_interest"}.issubset(frame.columns):
        weights = np.sqrt(
            frame["volume"].clip(lower=0).to_numpy(dtype=float) + 1.0
        ) * np.sqrt(frame["open_interest"].clip(lower=0).to_numpy(dtype=float) + 1.0)
    else:
        weights = np.ones(len(frame), dtype=float)
    candidates = frame["strike"].to_numpy(dtype=float) + math.exp(
        rate * time_to_expiry
    ) * (
        frame["call_price"].to_numpy(dtype=float)
        - frame["put_price"].to_numpy(dtype=float)
    )
    plausible = np.isfinite(candidates) & (candidates >= 0.75 * spot) & (
        candidates <= 1.25 * spot
    )
    if int(plausible.sum()) < min_pairs:
        return ForwardEstimate(
            forward=float(spot * math.exp(rate * time_to_expiry)),
            pair_count=int(plausible.sum()),
            used_fallback=True,
        )
    return ForwardEstimate(
        forward=_weighted_median(candidates[plausible], weights[plausible]),
        pair_count=int(plausible.sum()),
        used_fallback=False,
    )


def assign_tenor_buckets(
    frame: pd.DataFrame,
    *,
    underlying: str | None = None,
) -> pd.DataFrame:
    """按日历规则映射期限桶：ETF 近月≤2+季月≤2；中金所股指期权近月≤3+季月≤3。"""
    required = {"trade_date", "maturity_date"}
    if required.difference(frame.columns):
        raise ValueError(f"frame缺少字段: {sorted(required.difference(frame.columns))}")
    result = frame.copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"])
    result["maturity_date"] = pd.to_datetime(result["maturity_date"])
    unique_expiries = result[["trade_date", "maturity_date"]].drop_duplicates()
    unique_expiries = unique_expiries.loc[
        unique_expiries["maturity_date"] > unique_expiries["trade_date"]
    ]
    isCffex = underlyingIsCffexIndex(underlying)
    mapped_rows: list[dict[str, object]] = []
    for tradeDate, group in unique_expiries.groupby("trade_date", sort=False):
        tenorMap = calendarTenorMapForMaturities(
            group["maturity_date"].tolist(),
            isCffex=isCffex,
        )
        for md, tenor in tenorMap.items():
            mapped_rows.append(
                {
                    "trade_date": tradeDate,
                    "maturity_date": md,
                    "tenor": tenor,
                }
            )
    if not mapped_rows:
        result["tenor"] = pd.Series(dtype=object)
        return result
    tenorFrame = pd.DataFrame(mapped_rows)
    tenorFrame["trade_date"] = pd.to_datetime(tenorFrame["trade_date"])
    tenorFrame["maturity_date"] = pd.to_datetime(tenorFrame["maturity_date"])
    return result.merge(
        tenorFrame,
        on=["trade_date", "maturity_date"],
        how="left",
    )


def add_rolling_iv_percentile(
    frame: pd.DataFrame,
    *,
    window: int = 756,
    min_history: int = 120,
) -> pd.DataFrame:
    """按标的、期限、认购认沽分组计算只使用更早日期的中间秩分位数。"""
    required = {
        "underlying",
        "trade_date",
        "tenor",
        "call_put",
        "representative_iv",
    }
    if required.difference(frame.columns):
        raise ValueError(f"frame缺少字段: {sorted(required.difference(frame.columns))}")
    if window <= 0 or min_history <= 0:
        raise ValueError("window和min_history必须为正")
    result = frame.copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"])
    result = result.sort_values(
        ["underlying", "tenor", "call_put", "trade_date"]
    ).reset_index(drop=True)
    percentiles = np.full(len(result), np.nan, dtype=float)
    history_counts = np.zeros(len(result), dtype=int)
    group_columns = ["underlying", "tenor", "call_put"]
    for _, indices in result.groupby(group_columns, sort=False).groups.items():
        positions = np.asarray(list(indices), dtype=int)
        values = result.loc[positions, "representative_iv"].to_numpy(dtype=float)
        for offset, current in enumerate(values):
            history = values[max(0, offset - window) : offset]
            history = history[np.isfinite(history)]
            history_counts[positions[offset]] = len(history)
            if len(history) < min_history or not np.isfinite(current):
                continue
            less = int(np.sum(history < current))
            equal = int(np.sum(history == current))
            percentiles[positions[offset]] = (less + 0.5 * equal) / len(history)
    result["history_n"] = history_counts
    result["iv_percentile"] = percentiles
    result["history_reliable_252"] = result["history_n"] >= 252
    return result


def build_iv_surface_panel(
    *,
    underlying: str,
    underlying_prices: pd.DataFrame,
    basic: pd.DataFrame,
    daily: pd.DataFrame,
    rate: float | pd.Series | None = None,
    atm_band: float = 0.15,
    percentile_window: int = 756,
    percentile_min_history: int = 120,
) -> pd.DataFrame:
    """
    从认购/认沽日行情构造四期限、两权利类型的代表IV面板。
    rate=None：按交易日使用中债10Y（CSV+yc_cb）；float 为整段常数；Series 为按日序列。
    """
    basic_required = {"ts_code", "call_put", "strike", "maturity_date", "unit"}
    daily_required = {
        "ts_code",
        "trade_date",
        "close",
        "settle",
        "volume",
        "open_interest",
    }
    if basic_required.difference(basic.columns):
        raise ValueError(f"basic缺少字段: {sorted(basic_required.difference(basic.columns))}")
    if daily_required.difference(daily.columns):
        raise ValueError(f"daily缺少字段: {sorted(daily_required.difference(daily.columns))}")
    if {"trade_date", "close"}.difference(underlying_prices.columns):
        raise ValueError("underlying_prices必须包含trade_date和close")

    contracts = basic.loc[:, list(basic_required)].copy()
    contracts["maturity_date"] = pd.to_datetime(contracts["maturity_date"])
    quotes = daily.loc[:, list(daily_required)].copy()
    quotes["trade_date"] = pd.to_datetime(quotes["trade_date"])
    merged = quotes.merge(contracts, on="ts_code", how="inner")
    merged["premium"] = merged["close"].where(merged["close"] > 0, merged["settle"])
    spots = underlying_prices.loc[:, ["trade_date", "close"]].rename(
        columns={"close": "spot"}
    )
    spots["trade_date"] = pd.to_datetime(spots["trade_date"])
    merged = merged.merge(spots, on="trade_date", how="inner")
    merged = merged.loc[
        (merged["premium"] > 0)
        & (merged["spot"] > 0)
        & (merged["maturity_date"] > merged["trade_date"])
    ].copy()
    merged = assign_tenor_buckets(merged, underlying=underlying)
    merged = merged.loc[merged["tenor"].notna()]

    rateSeries: pd.Series | None
    scalarRate: float | None
    if rate is None:
        rateSeries = loadCnGb10yRateSeries()
        scalarRate = None
    elif isinstance(rate, pd.Series):
        rateSeries = rate.copy()
        rateSeries.index = pd.DatetimeIndex(pd.to_datetime(rateSeries.index)).normalize()
        rateSeries = rateSeries.sort_index()
        scalarRate = None
    else:
        rateSeries = None
        scalarRate = float(rate)

    rows: list[dict[str, object]] = []
    group_columns = ["trade_date", "maturity_date", "tenor", "spot"]
    for (trade_date, maturity_date, tenor, spot), group in merged.groupby(
        group_columns, sort=True
    ):
        time_to_expiry = (maturity_date - trade_date).days / 365.0
        dayRate = (
            float(scalarRate)
            if scalarRate is not None
            else lookupCnGb10yRate(trade_date, series=rateSeries)
        )
        calls = group.loc[group["call_put"] == "C"].rename(
            columns={
                "premium": "call_price",
                "volume": "call_volume",
                "open_interest": "call_open_interest",
            }
        )
        puts = group.loc[group["call_put"] == "P"].rename(
            columns={
                "premium": "put_price",
                "volume": "put_volume",
                "open_interest": "put_open_interest",
            }
        )
        pairs = calls.merge(
            puts,
            on=["strike", "unit"],
            how="inner",
            suffixes=("_call", "_put"),
        )
        if pairs.empty:
            parity = pd.DataFrame(columns=["strike", "call_price", "put_price"])
        else:
            parity = pd.DataFrame(
                {
                    "strike": pairs["strike"],
                    "call_price": pairs["call_price"],
                    "put_price": pairs["put_price"],
                    "volume": np.minimum(pairs["call_volume"], pairs["put_volume"]),
                    "open_interest": np.minimum(
                        pairs["call_open_interest"], pairs["put_open_interest"]
                    ),
                }
            )
        forward_estimate = estimate_implied_forward(
            parity,
            spot=float(spot),
            time_to_expiry=time_to_expiry,
            rate=dayRate,
        )
        for call_put in ("C", "P"):
            option_rows = group.loc[group["call_put"] == call_put].copy()
            option_rows = option_rows.loc[
                np.abs(np.log(option_rows["strike"] / forward_estimate.forward))
                <= atm_band
            ]
            option_rows["iv"] = [
                implied_volatility(
                    price=float(row.premium),
                    forward=forward_estimate.forward,
                    strike=float(row.strike),
                    time_to_expiry=time_to_expiry,
                    rate=dayRate,
                    call_put=call_put,
                )
                for row in option_rows.itertuples(index=False)
            ]
            valid = option_rows.loc[option_rows["iv"].notna()]
            weights = np.sqrt(valid["volume"].clip(lower=0).to_numpy(dtype=float) + 1.0)
            weights *= np.sqrt(
                valid["open_interest"].clip(lower=0).to_numpy(dtype=float) + 1.0
            )
            representative_iv = _weighted_median(
                valid["iv"].to_numpy(dtype=float), weights
            )
            rows.append(
                {
                    "underlying": underlying,
                    "trade_date": trade_date,
                    "maturity_date": maturity_date,
                    "tenor": tenor,
                    "call_put": call_put,
                    "representative_iv": representative_iv,
                    "valid_contracts": int(len(valid)),
                    "forward": forward_estimate.forward,
                    "pair_count": forward_estimate.pair_count,
                    "forward_fallback": forward_estimate.used_fallback,
                    "spot": float(spot),
                    "dte": int((maturity_date - trade_date).days),
                }
            )
    surface = pd.DataFrame(rows)
    if surface.empty:
        return surface
    return add_rolling_iv_percentile(
        surface,
        window=percentile_window,
        min_history=percentile_min_history,
    )


# 代码更新时间：2026-08-07 13:05 中金所股指期权期限桶扩至 6（日历近月链+季月，非简单取前 N）
# 代码更新时间：2026-08-09 09:05 增加 getCnGb10yRate（yc_cb 中债10Y，日缓存）
# 代码更新时间：2026-08-09 10:05 CSV种子+yc_cb增量；build_iv_surface_panel 支持按日 rate
# 代码更新时间：2026-08-10 23:00 新增 black76_delta（现货 Delta）
# 代码更新时间：2026-09-23 06:46 新增 black76_gamma（现货 Gamma，与 Δ 同 d1）
