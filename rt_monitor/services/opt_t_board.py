"""期权 T 型报价板域。"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
import os
import re
import threading
import time
from datetime import date, datetime, time as dtTime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import pymysql
from flask import make_response, render_template, request
from logger_config import get_logger

from rt_monitor.core.config import DB_CONFIG_TUSHARE, RT_MONITOR_STATIC_VER
from rt_monitor.services import opt_iv_windows as ivWindows

logger = get_logger(__name__)

# 中金所股指期权 6 位代码（与 opt_spread_board_select.CFFEX_INDEX_OPT_CODES 对齐）
_CFFEX_INDEX_OPT_CODES = frozenset({'000300', '000016', '000852'})
DAQMT_QUOTE_SNAPSHOT_URL = os.getenv(
    'DAQMT_QUOTE_SNAPSHOT_URL',
    'http://127.0.0.1:15580/snapshot',
)
DAQMT_QUOTE_FETCH_TIMEOUT_SEC = 1.0
DAQMT_QUOTE_DOWN_COOLDOWN_SEC = 3.0
DAQMT_QUOTE_MEMO_SEC = 0.5
_daqmtDownUntil = 0.0
_daqmtSnapMemoLock = threading.Lock()
_daqmtSnapMemo: tuple[float, dict[str, Any] | None] = (0.0, None)


def _isCffexIndexOpt(code6: str) -> bool:
    return str(code6 or '').strip() in _CFFEX_INDEX_OPT_CODES


def _optTBoardPerUnit(code6: str) -> float:
    try:
        from opt_spread_board_select import getSymbolProfile

        return float(getSymbolProfile(code6).get('perUnit') or 10000.0)
    except Exception:
        return 100.0 if _isCffexIndexOpt(code6) else 10000.0


def _optTBoardSpreadMult(code6: str) -> float:
    try:
        from opt_spread_board_select import getSymbolProfile

        return float(getSymbolProfile(code6).get('spreadMult') or 10000.0)
    except Exception:
        return 100.0 if _isCffexIndexOpt(code6) else 10000.0


def _opt_t_board_norm_strike(exercise_price: object) -> float | None:
    """行权价转 float；无效则 None。"""
    if exercise_price is None:
        return None
    try:
        if isinstance(exercise_price, Decimal):
            return float(exercise_price)
        return float(exercise_price)
    except (TypeError, ValueError):
        return None


def _fmt_opt_daily_px(val: object) -> str:
    """opt_daily 价格字段格式化为短字符串；无效为 —。"""
    if val is None:
        return '—'
    try:
        x = float(val) if not isinstance(val, Decimal) else float(val)
        if x != x:  # NaN
            return '—'
        s = f'{x:.4f}'.rstrip('0').rstrip('.')
        return s if s else '0'
    except (TypeError, ValueError):
        return '—'


def _fmt_opt_last_px(val: object) -> str:
    """T 型价格固定四位小数（最新 / 时值 / 有效价）。"""
    if val is None:
        return '—'
    try:
        x = float(val) if not isinstance(val, Decimal) else float(val)
        if x != x:
            return '—'
        return f'{x:.4f}'
    except (TypeError, ValueError):
        return '—'


def _fmt_opt_daily_qty(val: object) -> str:
    """成交量/持仓量：≥1 万显示一位小数「x.x万」，否则整数；无效为 —。

    增列后量仓格变窄，6 位整数会顶满；万单位把 108377 → 10.8万。排序/top3
    仍走原始浮点，不解析这串展示文本。
    """
    if val is None:
        return '—'
    try:
        x = float(val) if not isinstance(val, Decimal) else float(val)
        if x != x:
            return '—'
        if abs(x) >= 10000.0:
            return f'{x / 10000.0:.1f}万'
        if abs(x - round(x)) < 1e-6:
            return str(int(round(x)))
        s = f'{x:.4f}'.rstrip('0').rstrip('.')
        return s if s else '0'
    except (TypeError, ValueError):
        return '—'


def _opt_daily_close_float(q: dict[str, Any] | None) -> float | None:
    """收盘价/最新 close 转 float（兼容旧口径与 IV）。"""
    if not q:
        return None
    v = q.get('close')
    if v is None:
        return None
    try:
        x = float(v) if not isinstance(v, Decimal) else float(v)
        if x != x:
            return None
        return x
    except (TypeError, ValueError):
        return None


def _optQuotePxPositive(value: object) -> float | None:
    try:
        x = float(value) if not isinstance(value, Decimal) else float(value)
    except (TypeError, ValueError):
        return None
    if x != x or x <= 0:
        return None
    return x


def _optQuoteSellPx(q: dict[str, Any] | None) -> float | None:
    """卖方立刻成交价：买一 → last → close。"""
    if not q:
        return None
    for key in ('bid1', 'last', 'close'):
        px = _optQuotePxPositive(q.get(key))
        if px is not None:
            return px
    return None


def _optQuoteBuyPx(q: dict[str, Any] | None) -> float | None:
    """买方立刻成交价：卖一 → last → close。"""
    if not q:
        return None
    for key in ('ask1', 'last', 'close'):
        px = _optQuotePxPositive(q.get(key))
        if px is not None:
            return px
    return None


def _optQuoteLastPx(q: dict[str, Any] | None) -> float | None:
    """T 型「最新」列：可信最新成交 → 日线昨收 → close；不与买一/卖一混用。"""
    if not q:
        return None
    for key in ('last', 'daily_close', 'close'):
        px = _optQuotePxPositive(q.get(key))
        if px is not None:
            return px
    return None


def _optLastScaleAbsurd(
    last: float | None,
    bid1: float | None,
    ask1: float | None,
) -> bool:
    """仅拦截数量级错价（如历史 ×10）；成交后 last 略出买卖一属正常，不拦。"""
    if last is None or last <= 0:
        return True
    refs = [x for x in (bid1, ask1) if x is not None and x > 0]
    if not refs:
        return False
    mid = sum(refs) / len(refs)
    if mid <= 0:
        return False
    return last > mid * 5.0 or last < mid / 5.0


def _fmt_opt_pct(val: float | None, *, digits: int = 1) -> str:
    """百分比展示；无效为 —。"""
    if val is None:
        return '—'
    try:
        if val != val:
            return '—'
        return f'{val:.{digits}f}'
    except (TypeError, ValueError):
        return '—'


def _fmt_opt_iv_pct(val: float | None) -> str:
    """T 型 IV%：<100 一位小数（99.9），≥100 取整（176），把列宽让给时值。"""
    if val is None:
        return '—'
    try:
        pct = float(val)
        if pct != pct:
            return '—'
        if abs(pct) >= 100.0:
            return f'{pct:.0f}'
        return f'{pct:.1f}'
    except (TypeError, ValueError):
        return '—'


def _opt_t_disc_cls(discText: str) -> str:
    """
    折溢%着色：正=卖方口径有利（Put折价接货/Call溢价卖出），负=不利。
    阈值 |x|≤0.05 视为近零，避免噪声抢色。
    """
    if not discText or discText == '—':
        return ''
    try:
        v = float(discText)
    except (TypeError, ValueError):
        return ''
    if v > 0.05:
        return 'opt-t-disc-pos'
    if v < -0.05:
        return 'opt-t-disc-neg'
    return 'opt-t-disc-flat'


def _opt_t_prem_cls(premText: str) -> str:
    """
    权利金% 阶梯着色（卖方核心列）：越高越醒目。
    分档：&lt;3 灰；≥3 / ≥5 / ≥8 / ≥10 / ≥20。
    """
    if not premText or premText == '—':
        return ''
    try:
        v = float(premText)
    except (TypeError, ValueError):
        return ''
    if v >= 20.0:
        return 'opt-t-prem-t5'
    if v >= 10.0:
        return 'opt-t-prem-t4'
    if v >= 8.0:
        return 'opt-t-prem-t3'
    if v >= 5.0:
        return 'opt-t-prem-t2'
    if v >= 3.0:
        return 'opt-t-prem-t1'
    return 'opt-t-prem-lo'


def _opt_t_tv_ann_cls(tvAnnText: str) -> str:
    """
    时年化 阶梯着色（卖方核心列）：时间密度越高越醒目。
    分档：&lt;15 灰；≥15 / ≥20 / ≥30 / ≥50 / ≥100。
    """
    if not tvAnnText or tvAnnText == '—':
        return ''
    try:
        v = float(tvAnnText)
    except (TypeError, ValueError):
        return ''
    if v >= 100.0:
        return 'opt-t-tvann-t5'
    if v >= 50.0:
        return 'opt-t-tvann-t4'
    if v >= 30.0:
        return 'opt-t-tvann-t3'
    if v >= 20.0:
        return 'opt-t-tvann-t2'
    if v >= 15.0:
        return 'opt-t-tvann-t1'
    return 'opt-t-tvann-lo'


def _opt_t_delta_cls(deltaText: str) -> str:
    """|Δ| 分档：偏低灰、中档醒目、偏高（易被指派）更醒目。"""
    if not deltaText or deltaText == '—':
        return ''
    try:
        v = abs(float(deltaText))
    except (TypeError, ValueError):
        return ''
    if v >= 0.45:
        return 'opt-t-delta-hi'
    if v >= 0.25:
        return 'opt-t-delta-mid'
    if v >= 0.15:
        return 'opt-t-delta-lo'
    return 'opt-t-delta-flat'


def _opt_t_ivr_cls_from_value(v: float | None) -> str:
    """分位值 → 色阶：偏高=卖权环境更「贵」。三列 Per 共用。"""
    if v is None:
        return ''
    if v >= 70.0:
        return 'opt-t-ivr-hi'
    if v >= 50.0:
        return 'opt-t-ivr-mid'
    if v >= 30.0:
        return 'opt-t-ivr-lo'
    return 'opt-t-ivr-flat'


def _opt_t_delta_abs(deltaText: str) -> float | None:
    if not deltaText or deltaText == '—':
        return None
    try:
        return abs(float(deltaText))
    except (TypeError, ValueError):
        return None


_OPT_T_EXPIRY_KIND_TO_TENOR = {
    'near': 'current_month',
    'next': 'next_month',
    'next_next': 'next_next_month',
    'quarter': 'current_quarter',
    'next_quarter': 'next_quarter',
    'next_next_quarter': 'next_next_quarter',
}

_OPT_T_IV_PANEL_TS_BY_CODE6 = {
    '588000': '588000.SH',
    '159915': '159915.SZ',
    '510050': '510050.SH',
    '510300': '510300.SH',
    '510500': '510500.SH',
    '000016': '000016.SH',
    '000300': '000300.SH',
    '000852': '000852.SH',
}


def _opt_t_board_iv_panel_ts(opt_underlying: str) -> str | None:
    code6 = str(opt_underlying or '').strip()
    return _OPT_T_IV_PANEL_TS_BY_CODE6.get(code6)


def _opt_t_board_fetch_iv_hist(
    cur: Any,
    *,
    underlyingTs: str,
    tenor: str,
    callPut: str,
    asof: date | None,
) -> tuple[list[date], list[float]]:
    """
    同期限代表 IV 全历史（opt_iv_panel），仅取 asof 之前交易日，防前视。

    返回按交易日升序的 (日期, 小数 IV)。Per A 与 Per C 需要全样本，故不再截断
    （最长标的 510050 也只 2823 天，走 uk_opt_iv_panel 索引实测 5.6ms）。
    """
    if not underlyingTs or not tenor or callPut not in ('C', 'P') or asof is None:
        return [], []
    try:
        cur.execute(
            """
            SELECT trade_date, representative_iv
            FROM opt_iv_panel
            WHERE underlying = %s
              AND tenor = %s
              AND call_put = %s
              AND trade_date < %s
              AND representative_iv IS NOT NULL
            ORDER BY trade_date
            """,
            (underlyingTs, tenor, callPut, asof.isoformat()),
        )
        rows = cur.fetchall() or []
    except Exception:
        return [], []
    dates: list[date] = []
    vals: list[float] = []
    for row in rows:
        try:
            x = float(row.get('representative_iv'))
        except (TypeError, ValueError, AttributeError):
            continue
        d = _opt_t_board_parse_date(row.get('trade_date'))
        if d is None or x != x or x <= 0:
            continue
        dates.append(d)
        vals.append(x)
    return dates, vals


# T 型三列分位：展示顺序为「紧邻 IV 的窗口最短」，向外依次拉长
_OPT_T_PER_KINDS = ('per1y', 'perc', 'pera')


def _opt_t_per_cells(
    windows: ivWindows.PerWindows,
    currentIv: float | None,
    deltaText: str,
    *,
    lo: float = 0.10,
    hi: float = 0.90,
) -> dict[str, Any]:
    """
    本合约 IV 相对同期限代表 IV 历史的三窗口分位：Per 1y / Per C / Per A。

    仅在 |Δ|∈[lo, hi] 出数——极端翼部的 IV 由反解噪声主导，分位没有意义。
    除展示文本外还回 ``*_raw`` 浮点：义务仓优选的星标与排序直接用它，不再去解析
    展示文本（此前靠 split('/') 反解 ``65/72``，格式一变就静默失效）。
    展示精度与 IV 看板一致（一位小数），两处对同一标的应给出同一个数。
    """
    absD = _opt_t_delta_abs(deltaText)
    inRange = absD is not None and lo <= absD <= hi
    values = windows.evaluate(currentIv) if inRange else {}
    out: dict[str, Any] = {}
    for kind in _OPT_T_PER_KINDS:
        raw = values.get(kind)
        out[f'{kind}_raw'] = raw
        if raw is None:
            out[kind] = '—'
            out[f'{kind}_cls'] = ''
            # 区间外不给 title：门控原因写在表头，逐格重复只会挡住相邻列
            out[f'{kind}_title'] = '' if not inRange else ivWindows.perTitle(kind, windows)
            continue
        out[kind] = f'{raw:.1f}'
        out[f'{kind}_cls'] = _opt_t_ivr_cls_from_value(raw)
        out[f'{kind}_title'] = ivWindows.perTitle(kind, windows)
    return out


def _optTIvYearFrac(daysToExpiry: int | None) -> float | None:
    """IV/希腊值用的年化剩余期限。到期日当天仍交易，按 1/365；日期未知才放弃。"""
    if daysToExpiry is None or daysToExpiry < 0:
        return None
    return max(int(daysToExpiry), 1) / 365.0


def _opt_t_board_seller_metrics(
    *,
    closePx: float | None = None,
    askPx: float | None = None,
    sellPx: float | None = None,
    buyPx: float | None = None,
    strike: float,
    spot: float | None,
    forward: float | None,
    callPut: str,
    daysToExpiry: int | None,
    rate: float,
) -> dict[str, Any]:
    """
    单腿评估字段（认购/认沽对称）；与「最新」列分离——最新=成交价，本函数用买卖档评估。

    1) 卖出价 sell=买一→last→close；买进价 buy=卖一→last→close
    2) 模式（关键）：
       · 仅当 buy 时值&lt;0（按卖一买入仍折价）→ 买方评估，C=buy
       · 否则 → 卖方评估，C=sell（买一）；含「买一略折、卖一已有正时值」的夹心区
    3) 权利金%：Call 备兑 C/(S−C)；Put 净接货 C/(K−C)
    4) 有效价：Call=K+C；Put=K−C
    5) 折溢%：Call=(有效价−S)/S；Put=(S−有效价)/S（与时值同 C）
    6) 时年化：仅正时值；(时值/K)×365/DTE×100
    """
    out: dict[str, Any] = {
        'prem_pct': '—',
        'prem_cls': '',
        'time_val': '—',
        'time_cls': '',
        'tv_ann': '—',
        'tv_ann_cls': '',
        'eff_px': '—',
        'disc_prem_pct': '—',
        'disc_cls': '',
        'iv': '—',
        'delta': '—',
        'delta_cls': '',
        'gamma': '—',
        'iv_raw': None,
        'action_mode': 'sell',
        'action_px': None,
        'px_label': '卖价',
        'px_title': '卖价(买一→最新→昨收)',
    }
    sell = sellPx if sellPx is not None else closePx
    buy = buyPx if buyPx is not None else askPx
    if sell is not None and (sell != sell or sell < 0):
        sell = None
    if buy is not None and (buy != buy or buy < 0):
        buy = None
    kind = str(callPut or '').strip().upper()
    if kind not in ('C', 'P'):
        return out

    actionMode = 'sell'
    px = sell
    if spot is not None and spot > 0:
        intrinsicProbe = (
            max(spot - strike, 0.0) if kind == 'C' else max(strike - spot, 0.0)
        )
        # 买方模式：必须「按卖一买进」仍为负时值，才算真折价可买
        buyTv = (buy - intrinsicProbe) if buy is not None else None
        if buyTv is not None and buyTv < 0:
            px = buy
            actionMode = 'buy'
        elif sell is not None:
            px = sell
            actionMode = 'sell'
        elif buy is not None:
            px = buy
            actionMode = 'buy'
    elif sell is None and buy is not None:
        px = buy
        actionMode = 'buy'
    if px is None:
        return out

    out['action_mode'] = actionMode
    out['action_px'] = float(px)
    if actionMode == 'buy':
        out['px_label'] = '买价'
        out['px_title'] = '买价(卖一)·按卖一买入仍折价'
    else:
        out['px_label'] = '卖价'
        out['px_title'] = '卖价(买一)·卖方立刻成交'

    effPx: float | None = None
    if kind == 'P' and strike > 0:
        effPx = strike - px
        out['eff_px'] = _fmt_opt_last_px(effPx)
    elif kind == 'C' and strike > 0:
        effPx = strike + px
        out['eff_px'] = _fmt_opt_last_px(effPx)

    premPct: float | None = None
    if kind == 'P' and strike > 0:
        netPut = strike - px
        if netPut > 1e-12:
            premPct = px / netPut * 100.0
    elif kind == 'C' and spot is not None and spot > 0:
        netCall = spot - px
        if netCall > 1e-12:
            premPct = px / netCall * 100.0
    if premPct is not None:
        out['prem_pct'] = _fmt_opt_pct(premPct, digits=2)
        out['prem_cls'] = _opt_t_prem_cls(out['prem_pct'])

    if spot is not None and spot > 0:
        intrinsic = max(spot - strike, 0.0) if kind == 'C' else max(strike - spot, 0.0)
        timeVal = px - intrinsic
        out['time_val'] = _fmt_opt_last_px(timeVal)
        if timeVal < 0:
            out['time_cls'] = 'opt-t-tv-neg'
        elif (
            timeVal > 0
            and strike > 0
            and daysToExpiry is not None
            and daysToExpiry > 0
        ):
            out['tv_ann'] = _fmt_opt_pct(
                (timeVal / strike) * 100.0 * (365.0 / daysToExpiry),
                digits=2,
            )
            out['tv_ann_cls'] = _opt_t_tv_ann_cls(out['tv_ann'])
        if effPx is not None:
            if kind == 'P':
                out['disc_prem_pct'] = _fmt_opt_pct(
                    (spot - effPx) / spot * 100.0, digits=2
                )
            else:
                out['disc_prem_pct'] = _fmt_opt_pct(
                    (effPx - spot) / spot * 100.0, digits=2
                )
            out['disc_cls'] = _opt_t_disc_cls(out['disc_prem_pct'])
        fwd = forward if forward is not None and forward > 0 else spot
        tExp = _optTIvYearFrac(daysToExpiry)
        if px > 0 and tExp is not None and fwd > 0:
            try:
                from option_iv import black76_delta, black76_gamma, implied_volatility

                iv = implied_volatility(
                    price=float(px),
                    forward=float(fwd),
                    strike=float(strike),
                    time_to_expiry=tExp,
                    rate=float(rate),
                    call_put=kind,
                )
                if iv == iv:
                    out['iv'] = _fmt_opt_iv_pct(iv * 100.0)
                    out['iv_raw'] = float(iv)
                    try:
                        dlt = black76_delta(
                            forward=float(fwd),
                            strike=float(strike),
                            time_to_expiry=tExp,
                            rate=float(rate),
                            volatility=float(iv),
                            call_put=kind,
                        )
                        if dlt == dlt:
                            out['delta'] = f'{dlt:.3f}'
                            out['delta_cls'] = _opt_t_delta_cls(out['delta'])
                    except Exception:
                        pass
                    try:
                        gma = black76_gamma(
                            forward=float(fwd),
                            strike=float(strike),
                            time_to_expiry=tExp,
                            volatility=float(iv),
                            spot=float(spot) if spot is not None and spot > 0 else float(fwd),
                        )
                        if gma == gma:
                            out['gamma'] = f'{gma:.2f}'
                    except Exception:
                        pass
            except Exception:
                pass
    return out


_OPT_T_SELECTOR_EXPIRY_KINDS = ('near', 'next', 'quarter', 'next_quarter')
_OPT_T_SELECTOR_EXPIRY_LABELS = {
    'near': '当月',
    'next': '下月',
    'quarter': '当季',
    'next_quarter': '下季',
}
_OPT_T_SELECTOR_BUCKETS_BY_SIDE = {
    'put': ('虚五', '虚四', '虚三', '虚二', '虚一', '平值', '实一', '实二'),
    'call': ('实二', '实一', '平值', '虚一', '虚二', '虚三', '虚四', '虚五'),
}
# 权利仓波动型档位：实三→虚四（义务仓仍用上表虚五→实二）
_OPT_RIGHTS_VOL_BUCKETS_BY_SIDE = {
    'put': ('虚四', '虚三', '虚二', '虚一', '平值', '实一', '实二', '实三'),
    'call': ('实三', '实二', '实一', '平值', '虚一', '虚二', '虚三', '虚四'),
}
_OPT_T_SELECTOR_BID_MAX_AGE_SEC = 180.0


def _opt_t_board_selector_number(value: object) -> float | None:
    """选择器展示值转数值；只接受有限实数。"""
    if value is None or str(value).strip() in ('', '—'):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n == n else None


def _opt_t_board_selector_bucket(relative: int) -> str | None:
    """相对平值档位映射；正数是本方向的虚值，负数是实值。"""
    if relative == 0:
        return '平值'
    if 1 <= relative <= 5:
        return f'虚{("一", "二", "三", "四", "五")[relative - 1]}'
    if -3 <= relative <= -1:
        return f'实{("一", "二", "三")[-relative - 1]}'
    return None


def _opt_t_board_selector_bucket_cls(bucket: str) -> str:
    """档位标签用虚/平/实三种醒目色，不改变虚实%本身的涨跌方向配色。"""
    if str(bucket).startswith('虚'):
        return 'opt-t-selector-bucket-otm'
    if str(bucket).startswith('实'):
        return 'opt-t-selector-bucket-itm'
    return 'opt-t-selector-bucket-atm'


def _opt_t_board_selector_strike_key(row: dict[str, Any]) -> str | None:
    """标准合约的行权价匹配键，避免 Decimal/float 表示差异造成错配。"""
    strike = _opt_t_board_selector_number(row.get('strike'))
    return f'{strike:.8f}' if strike is not None else None


def _opt_t_board_selector_sell_price(
    quote: dict[str, Any] | None,
    *,
    bookAgeSec: float | None,
    inMarketSession: bool,
) -> tuple[float | None, str]:
    """选择器卖方估值：盘中三分钟内买一，否则最新价→收盘价。"""
    q = quote if isinstance(quote, dict) else {}
    bid1 = _optQuotePxPositive(q.get('bid1'))
    ask1 = _optQuotePxPositive(q.get('ask1'))
    freshBook = (
        inMarketSession
        and q.get('book_live')
        and bid1 is not None
        and ask1 is not None
        and bid1 <= ask1
        and bookAgeSec is not None
        and 0.0 <= bookAgeSec <= _OPT_T_SELECTOR_BID_MAX_AGE_SEC
    )
    if freshBook:
        return bid1, 'bid1'
    for key in ('last', 'close'):
        px = _optQuotePxPositive(q.get(key))
        if px is not None:
            return px, key
    return None, ''


# 缺档/空格的三列占位，避免在三处重复写同一组空值
_OPT_T_SELECTOR_PER_BLANK: dict[str, Any] = {
    f'{kind}{suffix}': '' if suffix else '—'
    for kind in _OPT_T_PER_KINDS
    for suffix in ('', '_cls', '_title')
}


def _opt_t_selector_per_cells(row: dict[str, Any], side: str) -> dict[str, Any]:
    """从 T 表行取三列分位的展示字段，口径与主表完全一致（同一批计算结果）。"""
    out: dict[str, Any] = {}
    for kind in _OPT_T_PER_KINDS:
        out[kind] = row.get(f'{side}_{kind}') or '—'
        out[f'{kind}_cls'] = row.get(f'{side}_{kind}_cls') or ''
        out[f'{kind}_title'] = row.get(f'{side}_{kind}_title') or ''
    return out


def _opt_t_board_build_obligation_selector(sections: list[dict[str, Any]]) -> dict[str, Any]:
    """按标准合约 K 构建四期限义务仓矩阵，并同步写回原 T 表的左右标记。

    当月优先作为标准 K 网格，缺失时回退至最早可用期限。其余期限必须匹配
    完全相同的 K；没有该标准合约即标记缺档。☆ 仅陈述时年化最高；有效价、
    Delta 与流动性完整展示，不把未设风险目标的多维取舍伪装成唯一候选。盘中
    只采用三分钟内买一；其他时段用最新价、收盘价估算。
    """
    byKind: dict[str, dict[str, Any]] = {}
    for sec in sections:
        kind = str(sec.get('expiry_kind') or '')
        if kind in _OPT_T_SELECTOR_EXPIRY_KINDS and kind not in byKind:
            byKind[kind] = sec
        for row in sec.get('rows') or []:
            if not isinstance(row, dict):
                continue
            for side in ('call', 'put'):
                row[f'{side}_selector_yield_star'] = False

    columns: list[dict[str, str]] = []
    for kind in _OPT_T_SELECTOR_EXPIRY_KINDS:
        label = _OPT_T_SELECTOR_EXPIRY_LABELS[kind]
        days = (byKind.get(kind) or {}).get('selector_days_to_expiry')
        tradeDays = (byKind.get(kind) or {}).get('selector_trade_days_to_expiry')
        try:
            daysInt = int(days) if days is not None else None
        except (TypeError, ValueError):
            daysInt = None
        try:
            tradeInt = int(tradeDays) if tradeDays is not None else None
        except (TypeError, ValueError):
            tradeInt = None
        daysText = _opt_t_board_selector_dte_text(daysInt, tradeInt)
        expiryHead = f'{label}（{daysText}）' if daysText else label
        columns.append({
            'kind': kind,
            'label': f'{expiryHead}（年/Δ/有效价 ┊ Per 1y/C/A）',
            'head_expiry': expiryHead,
            'head_left': '年/Δ/有效价',
            'head_right': 'Per 1y/C/A',
        })

    standardRows: list[dict[str, Any]] = []
    standardAtmIndex: int | None = None
    for kind in _OPT_T_SELECTOR_EXPIRY_KINDS:
        sec = byKind.get(kind)
        if not sec:
            continue
        rows = sorted(
            (
                row for row in (sec.get('rows') or [])
                if isinstance(row, dict) and not row.get('is_adj')
            ),
            key=lambda row: float(row.get('strike') or 0),
        )
        atmIndex = next((i for i, row in enumerate(rows) if row.get('is_atm')), None)
        if atmIndex is not None:
            standardRows = rows
            standardAtmIndex = atmIndex
            break

    standardBySide: dict[str, dict[str, dict[str, Any]]] = {
        side: {} for side in ('call', 'put')
    }
    if standardAtmIndex is not None:
        for index, row in enumerate(standardRows):
            for side, direction in (('call', 1), ('put', -1)):
                bucket = _opt_t_board_selector_bucket(direction * (index - standardAtmIndex))
                if bucket in _OPT_T_SELECTOR_BUCKETS_BY_SIDE[side]:
                    standardBySide[side][bucket] = row

    rowsByKindAndStrike: dict[str, dict[str, dict[str, Any]]] = {}
    for kind, sec in byKind.items():
        rowsByKindAndStrike[kind] = {
            key: row
            for row in (sec.get('rows') or [])
            if isinstance(row, dict)
            and not row.get('is_adj')
            and (key := _opt_t_board_selector_strike_key(row)) is not None
        }

    grids: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
        side: {bucket: {} for bucket in buckets}
        for side, buckets in _OPT_T_SELECTOR_BUCKETS_BY_SIDE.items()
    }
    for side, bucketRows in standardBySide.items():
        for bucket, standardRow in bucketRows.items():
            strikeKey = _opt_t_board_selector_strike_key(standardRow)
            strikeDisplay = standardRow.get('strike_display') or '—'
            if strikeKey is None:
                continue
            for kind in _OPT_T_SELECTOR_EXPIRY_KINDS:
                row = rowsByKindAndStrike.get(kind, {}).get(strikeKey)
                if row is None:
                    grids[side][bucket][kind] = {
                        'status': 'missing',
                        'strike_display': strikeDisplay,
                        'tv_ann': '—',
                        'delta': '—',
                        'eff_px': '—',
                        **_OPT_T_SELECTOR_PER_BLANK,
                        'yield_star': False,
                    }
                    continue
                selectorStatus = row.get(f'{side}_selector_status')
                ready = (
                    selectorStatus == 'priced'
                    if selectorStatus is not None
                    else bool(row.get(f'{side}_strategy_ready'))
                )
                grids[side][bucket][kind] = {
                    'status': 'ready' if ready else 'empty',
                    'strike_display': strikeDisplay,
                    'tv_ann': row.get(f'{side}_selector_tv_ann', row.get(f'{side}_tv_ann')) or '—',
                    'delta': row.get(f'{side}_selector_delta', row.get(f'{side}_delta')) or '—',
                    'eff_px': row.get(f'{side}_selector_eff_px', row.get(f'{side}_eff_px')) or '—',
                    **_opt_t_selector_per_cells(row, side),
                    'price_source': row.get(f'{side}_selector_price_source') or '',
                    'yield_star': False,
                    '_tv': _opt_t_board_selector_number(
                        row.get(f'{side}_selector_tv_ann', row.get(f'{side}_tv_ann'))
                    ),
                    '_eff': _opt_t_board_selector_number(
                        row.get(f'{side}_selector_eff_px', row.get(f'{side}_eff_px'))
                    ),
                    '_liq': (
                        _opt_t_board_selector_number(row.get(f'{side}_oi_n')) or 0.0
                    ) + (
                        _opt_t_board_selector_number(row.get(f'{side}_vol_n')) or 0.0
                    ),
                    '_row': row,
                }

    for side, bucketMap in grids.items():
        for bucket, kindMap in bucketMap.items():
            cells = [
                cell for cell in kindMap.values()
                if cell['status'] == 'ready' and cell['_tv'] is not None and cell['_tv'] > 0
            ]
            if cells:
                bestYield = max(cell['_tv'] for cell in cells)
                for cell in cells:
                    if abs(cell['_tv'] - bestYield) < 1e-9:
                        cell['yield_star'] = True
                        cell['_row'][f'{side}_selector_yield_star'] = True
            # 分位列不标星：远月分位系统性偏高，四期限里的「最高」几乎恒为下季，
            # 标出来没有区分度（2026-09-20 实测 call/put 两侧 8 行全部落在下季）。
            # 跨期限贵不贵由三列数值自行比较，☆ 只保留时年化一个含义。

    tabs: dict[str, dict[str, Any]] = {}
    for side in ('put', 'call'):
        rows: list[dict[str, Any]] = []
        for bucket in _OPT_T_SELECTOR_BUCKETS_BY_SIDE[side]:
            standardRow = standardBySide[side].get(bucket)
            strikeDisplay = standardRow.get('strike_display') if standardRow else '—'
            cells: list[dict[str, Any]] = []
            for kind in _OPT_T_SELECTOR_EXPIRY_KINDS:
                raw = grids[side][bucket].get(kind)
                if raw is None:
                    cells.append({
                        'status': 'missing', 'strike_display': strikeDisplay,
                        'tv_ann': '—', 'delta': '—', 'eff_px': '—',
                        **_OPT_T_SELECTOR_PER_BLANK,
                        'price_source': '',
                        'yield_star': False,
                    })
                    continue
                cells.append({key: value for key, value in raw.items() if not key.startswith('_')})
            # 四期限都未入选则不出这一档；全空时只留表头，与权利仓观察一致。
            if not any(cell.get('status') == 'ready' for cell in cells):
                continue
            rows.append({
                'label': bucket,
                'strike_display': strikeDisplay,
                'moneyness_pct': (standardRow or {}).get('moneyness_pct') or '—',
                'moneyness_cls': (standardRow or {}).get('moneyness_cls') or '',
                'bucket_cls': _opt_t_board_selector_bucket_cls(bucket),
                'cells': cells,
            })
        tabs[side] = {'label': 'Put' if side == 'put' else 'Call', 'rows': rows}

    return {
        'columns': columns,
        'put': tabs['put'],
        'call': tabs['call'],
    }


# 权利仓观察：期限键与 IV 看板 tenor 对齐
_OPT_RIGHTS_IV_TENOR = {
    'near': 'current_month',
    'next': 'next_month',
    'quarter': 'current_quarter',
    'next_quarter': 'next_quarter',
}
_OPT_RIGHTS_NEAR_MIN_DTE = 15
_OPT_RIGHTS_DELTA_MIN = 0.20
_OPT_RIGHTS_SKEW_CALL_VETO = -2.0
_OPT_RIGHTS_SKEW_PUT_VETO = 2.5
_OPT_RIGHTS_REPAIR_HV_PER = 70.0
_OPT_RIGHTS_REPAIR_IV_HV = -5.0
_OPT_RIGHTS_PARITY_DELTA = 0.80
_OPT_RIGHTS_PARITY_MNY = 10.0
_OPT_RIGHTS_PARITY_DISC = -2.0


def _optRightsPerHard(
    per1y: float | None,
    perc: float | None,
    pera: float | None,
) -> bool:
    """三列皆有数，且至少两列 <10、三列最大值 <15。"""
    vals = (per1y, perc, pera)
    if any(v is None for v in vals):
        return False
    return sum(v < 10.0 for v in vals) >= 2 and max(vals) < 15.0


def _optRightsIsRepair(hvPer: float | None, ivHv: float | None) -> bool:
    """HV 仍高且 IV−HV 大负：刚爆完在修，波动型不进。"""
    return (
        hvPer is not None
        and hvPer >= _OPT_RIGHTS_REPAIR_HV_PER
        and ivHv is not None
        and ivHv <= _OPT_RIGHTS_REPAIR_IV_HV
    )


def _optRightsSkewBlocks(side: str, skew: float | None) -> bool:
    """缺斜或该侧过贵则否决。Call：斜≤−2；Put：斜≥+2.5。"""
    if skew is None:
        return True
    if side == 'call':
        return skew <= _OPT_RIGHTS_SKEW_CALL_VETO
    return skew >= _OPT_RIGHTS_SKEW_PUT_VETO


def _optRightsVolTenorOk(
    kind: str,
    dte: int | None,
    side: str,
    axis: dict[str, Any] | None,
) -> bool:
    """期限×边环境门：当月仅 DTE>15；斜否决 / 非修复。Per 改由该档自判。"""
    if kind == 'near' and (dte is None or dte <= _OPT_RIGHTS_NEAR_MIN_DTE):
        return False
    if not axis:
        return False
    if _optRightsIsRepair(axis.get('hv_per'), axis.get('iv_hv')):
        return False
    if _optRightsSkewBlocks(side, axis.get('skew')):
        return False
    return True


def _optRightsAxisPerHard(side: str, axis: dict[str, Any] | None) -> bool:
    """IV 看板中轴 Per HARD；仅作翼部提示，不再挡显示。"""
    if not axis:
        return False
    pers = axis.get('per_call' if side == 'call' else 'per_put')
    if not isinstance(pers, tuple) or len(pers) != 3:
        return False
    return _optRightsPerHard(pers[0], pers[1], pers[2])


def _optRightsRowPerHard(row: dict[str, Any], side: str) -> bool:
    """T 型该档 Per 1y/C/A 的 HARD。"""
    return _optRightsPerHard(
        _opt_t_board_selector_number(row.get(f'{side}_per1y')),
        _opt_t_board_selector_number(row.get(f'{side}_perc')),
        _opt_t_board_selector_number(row.get(f'{side}_pera')),
    )


def _optRightsHasLiquidity(row: dict[str, Any], side: str) -> bool:
    vol = _opt_t_board_selector_number(row.get(f'{side}_vol_n')) or 0.0
    oi = _opt_t_board_selector_number(row.get(f'{side}_oi_n')) or 0.0
    return vol > 0.0 or oi > 0.0


def _optRightsParityHit(row: dict[str, Any], side: str) -> bool:
    """折价 β：深实、折溢≤−2%、负时值、卖一仍折价、有量或仓。"""
    if row.get(f'{side}_action_mode') != 'buy':
        return False
    tv = _opt_t_board_selector_number(row.get(f'{side}_time_val'))
    disc = _opt_t_board_selector_number(row.get(f'{side}_disc_prem_pct'))
    if tv is None or tv >= 0.0 or disc is None or disc > _OPT_RIGHTS_PARITY_DISC:
        return False
    if not _optRightsHasLiquidity(row, side):
        return False
    delta = _opt_t_board_selector_number(row.get(f'{side}_delta'))
    mny = _opt_t_board_selector_number(row.get('moneyness_pct'))
    if side == 'call':
        return (delta is not None and delta >= _OPT_RIGHTS_PARITY_DELTA) or (
            mny is not None and mny <= -_OPT_RIGHTS_PARITY_MNY
        )
    return (delta is not None and delta <= -_OPT_RIGHTS_PARITY_DELTA) or (
        mny is not None and mny >= _OPT_RIGHTS_PARITY_MNY
    )


_OPT_T_TERM_LABELS = (
    ('near', '当月'),
    ('next', '下月'),
    ('quarter', '当季'),
    ('next_quarter', '下季'),
)
# |当月中轴 − 下月中轴| 不超过该百分点视为平坦，避免 0.1pp 噪声来回翻状态
_OPT_T_TERM_FLAT_PP = 0.5


def _optTPos(value: object) -> float | None:
    """正的有限实数；预期波动与中轴 IV 都不接受 0 或空。"""
    n = _opt_t_board_selector_number(value)
    if n is None or n <= 0.0:
        return None
    return n


def quoteMidPx(bidPx: object, askPx: object, lastPx: object) -> float | None:
    """买卖都在且未倒挂时用中间价，否则用最新价。"""
    bid = _optTPos(bidPx)
    ask = _optTPos(askPx)
    if bid is not None and ask is not None and ask + 1e-9 >= bid:
        return (bid + ask) / 2.0
    return _optTPos(lastPx)


# 与卖方读盘、权利仓「在修」同一套门槛，不另起阈值
_OPT_T_TERM_PER_RICH = 70.0
_OPT_T_TERM_PER_MID = 50.0
_OPT_T_TERM_PER_CHEAP = 30.0
_OPT_T_TERM_HV_HIGH = 70.0
_OPT_T_TERM_PREM_FLAT = 1.0
_OPT_T_TERM_REPAIR_PREM = -5.0
# 权利仓可看：溢要比「近零」更负，且 HV 分位也在低位，避免 −1.4 就提示去买
_OPT_T_TERM_CHEAP_PREM = -3.0
_OPT_T_TERM_CHEAP_HV = 50.0
_OPT_T_TERM_LIGHT_CODES = frozenset({'588000', '159915'})
# 判词后面直接写出的用法。条件仍放在悬停，不铺到格子上。
_OPT_T_TERM_ACTION = {
    '真贵': '义务仓可卖',
    '偏贵': '义务仓可看',
    '溢价不强': '只宜轻',
    '热区有溢价': '降档',
    '双高': '别因分位卖',
    '低位收租': '可收租',
    '低位轻仓': '轻仓',
    '在修': '不卖不买',
    '溢为负': '不卖不买',
    '分位偏低': '权利仓可看',
    '中性': '观望',
    '溢近零': '观望',
    '溢为正': '缺分位',
    '淡化': '不参与',
    '缺溢': '不参与',
}
# 用法色块：stop=不做，warn=收着做，go=可以看，info=曲线说明
_OPT_T_TERM_ACT_TONE = {
    '义务仓可卖': 'go',
    '义务仓可看': 'go',
    '可收租': 'go',
    '权利仓可看': 'go',
    '降档': 'warn',
    '只宜轻': 'warn',
    '轻仓': 'warn',
    '卖当月要降档': 'warn',
    '不卖不买': 'stop',
    '别因分位卖': 'stop',
    '观望': 'stop',
    '不参与': 'stop',
    '缺分位': 'stop',
    '下月不因 IV 更高去卖': 'stop',
    '别按事件肥去卖': 'stop',
    '选月看溢，远月没有事件肥': 'info',
    '选月看溢，不看曲线': 'info',
}


def _termPer1y(raw: object) -> float | None:
    if not isinstance(raw, (tuple, list)) or not raw:
        return None
    return _opt_t_board_selector_number(raw[0])


def _termPer1yMid(axis: dict[str, Any]) -> tuple[float | None, str]:
    """认购/认沽近一年分位。两边差 ≥8 分位时分开写，避免平均掉方向差异。"""
    callPer = _termPer1y(axis.get('per_call'))
    putPer = _termPer1y(axis.get('per_put'))
    vals = [v for v in (callPer, putPer) if v is not None]
    if not vals:
        return None, ''
    mid = sum(vals) / len(vals)
    if callPer is not None and putPer is not None and abs(callPer - putPer) >= 8.0:
        return mid, f'购{callPer:.0f}/沽{putPer:.0f}'
    return mid, f'{mid:.0f}'


def _termSlopeDepth(absPp: float) -> str:
    if absPp < 1.5:
        return '浅'
    if absPp < 3.0:
        return '中度'
    return '陡'


def termTenorTag(
    per1y: float | None,
    hvPer: float | None,
    prem: float | None,
    *,
    muted: bool,
) -> tuple[str, str, str]:
    """单期限判词。顺序按页内交叉规则：在修先于「溢为负所以买」。"""
    if muted:
        return '淡化', 'mute', '当月剩余不足 10 个自然日，HV 噪声大，这格不参与判断'
    if prem is None:
        return '缺溢', 'mute', '没有中轴溢'
    if hvPer is not None and hvPer >= _OPT_T_TERM_HV_HIGH and prem <= _OPT_T_TERM_REPAIR_PREM:
        return (
            '在修',
            'repair',
            'HV 分位≥70 且溢≤−5：刚爆完、报价已往下修。不卖；溢为负也不是买波动',
        )
    if per1y is None or hvPer is None:
        if prem > _OPT_T_TERM_PREM_FLAT:
            return '溢为正', 'soft', '缺分位，只知道报价高于刚实现'
        if prem < -_OPT_T_TERM_PREM_FLAT:
            return '溢为负', 'repair', '缺分位。报价低于刚实现。不因溢为负去买'
        return '溢近零', 'flat', '溢在 ±1pp 内'
    if (
        per1y >= _OPT_T_TERM_PER_RICH
        and prem > _OPT_T_TERM_PREM_FLAT
        and hvPer < _OPT_T_TERM_PER_MID
    ):
        return '真贵', 'rich', '近一年分位≥70、HV 分位<50、溢>1：贵在历史上，刚实现并不热。义务仓可卖'
    if per1y >= _OPT_T_TERM_PER_RICH and hvPer >= _OPT_T_TERM_HV_HIGH and abs(prem) <= 2.0:
        return '双高', 'hot', 'IV 与 HV 分位都高、溢接近 0：高波动段，别因分位高就卖'
    if per1y < _OPT_T_TERM_PER_MID and hvPer < _OPT_T_TERM_PER_MID and 0.0 < prem <= 5.0:
        return '低位收租', 'rent', 'IV、HV 分位都低于 50，溢小幅为正'
    if prem > _OPT_T_TERM_PREM_FLAT and per1y < _OPT_T_TERM_PER_MID:
        return '溢价不强', 'soft', '溢为正，但近一年分位<50：收的是相对刚实现的价，不是历史高位。义务仓只宜轻'
    if prem > _OPT_T_TERM_PREM_FLAT and hvPer >= _OPT_T_TERM_HV_HIGH:
        return '热区有溢价', 'hot', '溢为正且 IV 分位≥50，但 HV 已经≥70：义务仓降档'
    if prem > _OPT_T_TERM_PREM_FLAT and per1y >= _OPT_T_TERM_PER_MID:
        return '偏贵', 'rich', '溢为正，近一年分位≥50，HV 还没到 70。义务仓可看'
    if (
        prem <= _OPT_T_TERM_CHEAP_PREM
        and per1y < _OPT_T_TERM_PER_CHEAP
        and hvPer < _OPT_T_TERM_CHEAP_HV
    ):
        return (
            '分位偏低',
            'cheap',
            '近一年分位<30、溢≤−3、HV 分位<50。权利仓可以看，先对斜和 1σ',
        )
    if prem < -_OPT_T_TERM_PREM_FLAT:
        return '溢为负', 'repair', '报价低于刚实现。不卖，也不当成买波动'
    return '中性', 'flat', '溢接近 0，分位未到极端'


def _termPremBand(prem: float | None) -> str:
    """与芯片同一道 ±1pp。带内不算溢为正或溢为负。"""
    if prem is None:
        return 'none'
    if prem > _OPT_T_TERM_PREM_FLAT:
        return 'pos'
    if prem < -_OPT_T_TERM_PREM_FLAT:
        return 'neg'
    return 'flat'


def _termActTone(action: str) -> str:
    return _OPT_T_TERM_ACT_TONE.get(action, 'stop')


def _termCurveNote(
    state: str,
    nearPrem: float | None,
    nextPrem: float | None,
) -> tuple[str, str]:
    """曲线解释和用法。溢的正负与第二行芯片用同一道 ±1pp。"""
    nearBand = _termPremBand(nearPrem)
    nextBand = _termPremBand(nextPrem)
    if state == '平坦':
        return '当月与下月 IV 几乎一样', '选月看溢，不看曲线'
    if state == '正挂':
        if nearBand == 'pos' and nextBand == 'neg':
            return (
                '下月 IV 更高，但溢为负。贵的是报价，不是相对于刚实现的溢价',
                '下月不因 IV 更高去卖',
            )
        if nearBand == 'pos' and nextBand == 'pos':
            return '下月 IV 更高，两边溢都为正', '选月看溢，远月没有事件肥'
        return '下月 IV 更高。没有近月事件倒挂', ''
    if nearBand == 'pos':
        return '近月更贵且溢为正。事件或拥挤', '卖当月要降档'
    if nearBand == 'neg':
        return '近月 IV 更高，但溢已不为正', '别按事件肥去卖'
    return '近月 IV 更高', ''


def _termOneSide(
    name: str,
    per1y: float | None,
    hvPer: float | None,
    prem: float | None,
    *,
    muted: bool,
    lightBook: bool,
) -> dict[str, str]:
    tag, tone, reason = termTenorTag(per1y, hvPer, prem, muted=muted)
    if lightBook and tag == '低位收租':
        tag = '低位轻仓'
        reason += '。588000/159915 不按 50 的仓位收租'
    action = _OPT_T_TERM_ACTION.get(tag, '')
    return {
        'name': name,
        'tag': tag,
        'action': action,
        'tone': tone,
        'act_tone': _termActTone(action),
        'title': reason,
    }


def _termSideText(side: dict[str, str]) -> str:
    head = f"{side['name']} " if side.get('name') else ''
    tail = f"{side['tag']} · {side['action']}" if side.get('action') else side['tag']
    return head + tail


def _termReadChip(label: str, axis: dict[str, Any], *, lightBook: bool) -> dict[str, Any] | None:
    muted = bool(axis.get('muted'))
    prem = _opt_t_board_selector_number(axis.get('iv_hv'))
    hvPer = _opt_t_board_selector_number(axis.get('hv_per'))
    callPer = _termPer1y(axis.get('per_call'))
    putPer = _termPer1y(axis.get('per_put'))
    per1y, perShown = _termPer1yMid(axis)
    if prem is None and not muted:
        return None
    split = callPer is not None and putPer is not None and abs(callPer - putPer) >= 8.0
    if split:
        putSide = _termOneSide('沽', putPer, hvPer, prem, muted=muted, lightBook=lightBook)
        callSide = _termOneSide('购', callPer, hvPer, prem, muted=muted, lightBook=lightBook)
        if putSide['tag'] == callSide['tag'] and putSide['action'] == callSide['action']:
            sides = [_termOneSide('', per1y, hvPer, prem, muted=muted, lightBook=lightBook)]
        else:
            sides = [putSide, callSide]
    else:
        sides = [_termOneSide('', per1y, hvPer, prem, muted=muted, lightBook=lightBook)]
    bits = [label]
    if prem is not None:
        bits.append(f'溢{prem:+.1f}')
    if perShown:
        bits.append(f'1y {perShown}')
    if hvPer is not None:
        bits.append(f'HV {hvPer:.0f}')
    metrics = ' · '.join(bits)
    primary = sides[0]
    return {
        'metrics': metrics,
        'text': f'{metrics} → ' + ' ｜ '.join(_termSideText(side) for side in sides),
        'title': primary['title'],
        'tone': primary['tone'],
        'tag': primary['tag'],
        'action': primary['action'],
        'sides': sides,
    }


def buildTermStructureGate(
    axisByKind: dict[str, dict[str, Any]],
    underlying: str = '',
) -> dict[str, Any] | None:
    """当月中轴 IV − 下月中轴 IV，并给每个期限一条溢/分位判词。"""
    nearIv = _optTPos((axisByKind.get('near') or {}).get('mid_iv'))
    nextIv = _optTPos((axisByKind.get('next') or {}).get('mid_iv'))
    if nearIv is None or nextIv is None:
        return None
    slopePp = (nearIv - nextIv) * 100.0
    if slopePp > _OPT_T_TERM_FLAT_PP:
        state, stateCls = '倒挂', 'opt-t-term--back'
    elif slopePp < -_OPT_T_TERM_FLAT_PP:
        state, stateCls = '正挂', 'opt-t-term--contango'
    else:
        state, stateCls = '平坦', 'opt-t-term--flat'
    rich: list[str] = []
    mutedNear = False
    reads: list[dict[str, str]] = []
    digits = ''.join(ch for ch in str(underlying or '') if ch.isdigit())
    lightBook = digits[:6] in _OPT_T_TERM_LIGHT_CODES
    for kind, label in _OPT_T_TERM_LABELS:
        axis = axisByKind.get(kind) or {}
        if not axis:
            continue
        if axis.get('muted'):
            if kind == 'near':
                mutedNear = True
        else:
            ivHv = _opt_t_board_selector_number(axis.get('iv_hv'))
            if ivHv is not None and ivHv > 0.0:
                rich.append(label)
        chip = _termReadChip(label, axis, lightBook=lightBook)
        if chip is not None:
            reads.append(chip)
    nearAxis = axisByKind.get('near') or {}
    nearPrem = None if nearAxis.get('muted') else _opt_t_board_selector_number(nearAxis.get('iv_hv'))
    nextPrem = _opt_t_board_selector_number((axisByKind.get('next') or {}).get('iv_hv'))
    curveExplain, curveAction = _termCurveNote(state, nearPrem, nextPrem)
    curve = curveExplain if not curveAction else f'{curveExplain} · {curveAction}'
    depth = '' if state == '平坦' else _termSlopeDepth(abs(slopePp))
    richText = '、'.join(rich) if rich else '无'
    text = (
        f'期限 {depth}{state} {slopePp:+.1f}pp'
        f'（当月 {nearIv * 100:.1f} → 下月 {nextIv * 100:.1f}）'
        f' · 溢仍为正：{richText}'
    )
    title = (
        '当月中轴IV − 下月中轴IV，与右侧看板「最新」同一套代表IV。'
        '倒挂=近月更贵（短期事件或拥挤）；正挂=远月更贵（常态）；'
        f'|差|≤{_OPT_T_TERM_FLAT_PP:.1f}pp 记平坦。'
        '第二行每个期限：溢、近一年分位、HV 分位 → 判词。'
        '在修=不卖也不买波动；溢价不强=义务仓只宜轻；真贵=义务仓可卖。'
        '当月剩余不足10个自然日时 HV 淡化，不计入「溢仍为正」。'
    )
    if mutedNear:
        title += ' 当月 HV 已淡化。'
    return {
        'text': text,
        'title': title,
        'state': state,
        'cls': stateCls,
        'slope_pp': slopePp,
        'rich': rich,
        'reads': reads,
        'curve': curve,
        'curve_explain': curveExplain,
        'curve_action': curveAction,
        'curve_act_tone': _termActTone(curveAction) if curveAction else '',
    }


def blendExpectedMove(
    straddle: float | None,
    wing1: float | None,
    wing2: float | None,
) -> float | None:
    """tastytrade 权重。缺档时只用仍在的腿并重归一；没有平值跨式则放弃。"""
    weighted: list[tuple[float, float]] = []
    for px, weight in ((straddle, 0.6), (wing1, 0.3), (wing2, 0.1)):
        n = _optTPos(px)
        if n is None:
            continue
        weighted.append((n, weight))
    if _optTPos(straddle) is None or not weighted:
        return None
    weightSum = sum(weight for _, weight in weighted)
    return sum(px * weight for px, weight in weighted) / weightSum


def expectedMoveFromIv(spot: object, iv: object, days: int | None) -> float | None:
    """1σ ≈ 现价 × IV × √(剩余自然日/365)，与 T 型 Black-76 的日历年化一致。"""
    spotN = _optTPos(spot)
    ivN = _optTPos(iv)
    if spotN is None or ivN is None or days is None or int(days) <= 0:
        return None
    return spotN * ivN * math.sqrt(int(days) / 365.0)


def rememberExpectedMoveLeg(
    book: dict[float, dict[str, float]],
    *,
    strike: float,
    isAdj: bool,
    bidCall: object,
    askCall: object,
    lastCall: object,
    bidPut: object,
    askPut: object,
    lastPut: object,
    callIv: object,
    putIv: object,
) -> None:
    """标准合约才参与跨式。调整合约行权价仍用同一条带标注。"""
    if isAdj:
        return
    slot = book.setdefault(float(strike), {})
    callMid = quoteMidPx(bidCall, askCall, lastCall)
    putMid = quoteMidPx(bidPut, askPut, lastPut)
    if callMid is not None:
        slot['call'] = callMid
    if putMid is not None:
        slot['put'] = putMid
    callIvN = _optTPos(callIv)
    putIvN = _optTPos(putIv)
    if callIvN is not None:
        slot['call_iv'] = callIvN
    if putIvN is not None:
        slot['put_iv'] = putIvN


def _optTPairPx(callPx: float | None, putPx: float | None) -> float | None:
    callN = _optTPos(callPx)
    putN = _optTPos(putPx)
    if callN is None or putN is None:
        return None
    return callN + putN


def _optTAtmMidIv(slot: dict[str, float]) -> float | None:
    ivs = [n for n in (_optTPos(slot.get('call_iv')), _optTPos(slot.get('put_iv'))) if n is not None]
    if not ivs:
        return None
    return sum(ivs) / len(ivs)


def buildSectionExpectedMove(
    *,
    spot: object,
    days: int | None,
    book: dict[float, dict[str, float]],
) -> dict[str, Any] | None:
    """有平值跨式和上下各两档宽跨时用市价权重，否则退回 IV 公式。"""
    spotN = _optTPos(spot)
    if spotN is None or not book:
        return None
    strikes = sorted(book)
    atm = min(strikes, key=lambda s: abs(s - spotN))
    idx = strikes.index(atm)

    def leg(offset: int, side: str) -> float | None:
        j = idx + offset
        if j < 0 or j >= len(strikes):
            return None
        return _optTPos(book[strikes[j]].get(side))

    straddle = _optTPairPx(leg(0, 'call'), leg(0, 'put'))
    wing1 = _optTPairPx(leg(1, 'call'), leg(-1, 'put'))
    wing2 = _optTPairPx(leg(2, 'call'), leg(-2, 'put'))
    move: float | None = None
    source = ''
    if straddle is not None and wing1 is not None and wing2 is not None:
        move = blendExpectedMove(straddle, wing1, wing2)
        source = '平值跨式×0.6 + 近一档宽跨×0.3 + 近二档宽跨×0.1；价用买卖中间价，缺一边用最新价'
    if move is None:
        move = expectedMoveFromIv(spotN, _optTAtmMidIv(book[atm]), days)
        source = '现价×平值中轴IV×√(剩余自然日/365)。跨式或宽跨不齐时用此式'
    if move is None or move <= 0.0:
        return None
    lo = spotN - move
    hi = spotN + move
    return {
        'move': move,
        'lo': lo,
        'hi': hi,
        'text': f'1σ ±{move:.3f} · {lo:.3f}–{hi:.3f}',
        'title': f'约 68% 价格带。{source}。行权价列底边=带内，左右边界另加左边条。',
        'source': source,
    }


def markExpectedMoveRows(
    rows: list[dict[str, Any]],
    spot: object,
    move: dict[str, Any] | None,
) -> None:
    """带内行权价加底边；带内最低、最高两档再加边界。"""
    for row in rows:
        row['em_cls'] = ''
    if not move or _optTPos(spot) is None:
        return
    lo = float(move['lo'])
    hi = float(move['hi'])
    inside = [
        i for i, row in enumerate(rows)
        if lo - 1e-9 <= float(row['strike']) <= hi + 1e-9
    ]
    edges = {inside[0], inside[-1]} if inside else set()
    for i, row in enumerate(rows):
        strike = float(row['strike'])
        inBand = lo - 1e-9 <= strike <= hi + 1e-9
        cls = 'opt-t-em-in' if inBand else ''
        if i in edges:
            cls = f'{cls} opt-t-em-edge'.strip()
        row['em_cls'] = cls
        base = str(row.get('strike_title') or '').strip()
        extra = '1σ带内' if inBand else '1σ带外'
        row['strike_title'] = f'{base} · {extra}' if base else extra


def _optRightsAxisByKind(optUnderlying: str) -> dict[str, dict[str, Any]]:
    """从 IV 看板缓存取当前标的四期限中轴。失败则波动型环境门全关。"""
    digits = ''.join(ch for ch in str(optUnderlying or '') if ch.isdigit())
    code6 = digits[:6]
    if len(code6) != 6:
        return {}
    try:
        from rt_monitor.services.opt_iv_board import midIvRaw, queryOptIvBoardPayload

        payload = queryOptIvBoardPayload()
    except Exception:
        logger.exception('权利仓观察读取 IV 中轴失败')
        return {}
    block = next(
        (
            item for item in (payload.get('blocks') or [])
            if str(item.get('code6') or '') == code6
        ),
        None,
    )
    if not isinstance(block, dict):
        return {}
    ivToKind = {iv: kind for kind, iv in _OPT_RIGHTS_IV_TENOR.items()}
    out: dict[str, dict[str, Any]] = {}
    for tenor in block.get('tenors') or []:
        if not isinstance(tenor, dict):
            continue
        kind = ivToKind.get(str(tenor.get('tenor') or ''))
        if not kind:
            continue
        call = tenor.get('call') if isinstance(tenor.get('call'), dict) else {}
        put = tenor.get('put') if isinstance(tenor.get('put'), dict) else {}
        axis = tenor.get('axis') if isinstance(tenor.get('axis'), dict) else {}
        out[kind] = {
            'per_call': tuple(
                _opt_t_board_selector_number(call.get(key))
                for key in _OPT_T_PER_KINDS
            ),
            'per_put': tuple(
                _opt_t_board_selector_number(put.get(key))
                for key in _OPT_T_PER_KINDS
            ),
            'hv_per': _opt_t_board_selector_number(axis.get('hv_per')),
            'iv_hv': _opt_t_board_selector_number(axis.get('delta')),
            'skew': _opt_t_board_selector_number(axis.get('skew')),
            'mid_iv': midIvRaw(call.get('current_raw'), put.get('current_raw')),
            'muted': bool(axis.get('muted')),
        }
    return out


def _optRightsBlankCell(status: str) -> dict[str, Any]:
    return {
        'status': status,
        'delta': '—',
        'disc': '—',
        'disc_cls': '',
        'time_val': '—',
        'time_cls': '',
        'per1y': '—',
        'per1y_cls': '',
        'perc': '—',
        'perc_cls': '',
        'pera': '—',
        'pera_cls': '',
        'vol': '—',
        'oi': '—',
        'show_per': False,
        'axis_note': '',
        'is_wing_pass': False,
    }


def _optRightsReadyCell(
    row: dict[str, Any],
    side: str,
    *,
    showPer: bool,
    axisNote: str = '',
) -> dict[str, Any]:
    """波动型分位直接抄 T 型该档的 Per 1y/C/A，不用期限中轴。"""
    perCells: dict[str, Any] = {}
    for key in _OPT_T_PER_KINDS:
        if showPer:
            perCells[key] = row.get(f'{side}_{key}') or '—'
            perCells[f'{key}_cls'] = row.get(f'{side}_{key}_cls') or ''
            perCells[f'{key}_title'] = row.get(f'{side}_{key}_title') or ''
        else:
            perCells[key] = '—'
            perCells[f'{key}_cls'] = ''
            perCells[f'{key}_title'] = ''
    note = str(axisNote or '')
    return {
        'status': 'ready',
        'delta': row.get(f'{side}_delta') or '—',
        'disc': row.get(f'{side}_disc_prem_pct') or '—',
        'disc_cls': row.get(f'{side}_disc_cls') or '',
        'time_val': row.get(f'{side}_time_val') or '—',
        'time_cls': row.get(f'{side}_time_cls') or '',
        'vol': row.get(f'{side}_vol') or '—',
        'oi': row.get(f'{side}_oi') or '—',
        'show_per': showPer,
        'axis_note': note,
        'is_wing_pass': bool(note),
        **perCells,
    }


def _optRightsDecideCell(
    row: dict[str, Any] | None,
    *,
    side: str,
    mode: str,
    kind: str,
    dte: int | None,
    axis: dict[str, Any] | None,
) -> dict[str, Any]:
    """单格 HARD：缺合约 / 未过门 / 列出。"""
    if row is None:
        return _optRightsBlankCell('missing')
    if mode == 'vol':
        if not _optRightsVolTenorOk(kind, dte, side, axis):
            return _optRightsBlankCell('empty')
        delta = _opt_t_board_selector_number(row.get(f'{side}_delta'))
        if delta is None or abs(delta) <= _OPT_RIGHTS_DELTA_MIN:
            return _optRightsBlankCell('empty')
        if not _optRightsRowPerHard(row, side):
            return _optRightsBlankCell('empty')
        axisNote = ''
        if not _optRightsAxisPerHard(side, axis):
            axisNote = '翼部过门、中轴未过'
        return _optRightsReadyCell(row, side, showPer=True, axisNote=axisNote)
    if not _optRightsParityHit(row, side):
        return _optRightsBlankCell('empty')
    return _optRightsReadyCell(row, side, showPer=False)


def _optRightsRowSpecs(
    mode: str,
    side: str,
    *,
    standardBySide: dict[str, dict[str, dict[str, Any]]],
    rowsByKindAndStrike: dict[str, dict[str, dict[str, Any]]],
    columns: list[dict[str, Any]],
) -> list[tuple[str, dict[str, Any] | None]]:
    """波动型候选档是实三–虚四；平价型列出任一期限过 HARD 的全部 K。
    波动型在组格后丢掉四期限都未入选的档。"""
    if mode == 'vol':
        return [
            (bucket, standardBySide[side].get(bucket))
            for bucket in _OPT_RIGHTS_VOL_BUCKETS_BY_SIDE[side]
        ]
    bucketByKey = {
        key: (bucket, row)
        for bucket, row in standardBySide[side].items()
        if (key := _opt_t_board_selector_strike_key(row)) is not None
    }
    chosen: dict[str, dict[str, Any]] = {}
    for kind in _OPT_T_SELECTOR_EXPIRY_KINDS:
        dte = next((col['dte'] for col in columns if col['kind'] == kind), None)
        for key, row in rowsByKindAndStrike.get(kind, {}).items():
            cell = _optRightsDecideCell(
                row, side=side, mode='parity', kind=kind, dte=dte, axis=None,
            )
            if cell['status'] == 'ready':
                chosen[key] = row
    specs: list[tuple[str, dict[str, Any] | None]] = []
    for key in sorted(
        chosen,
        key=lambda item: _opt_t_board_selector_number(
            chosen[item].get('strike')
        ) or 0.0,
    ):
        bucket, _standard = bucketByKey.get(key, ('实深', chosen[key]))
        specs.append((bucket, chosen[key]))
    return specs


def _opt_t_board_build_rights_selector(
    sections: list[dict[str, Any]],
    axisByKind: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """当前标的权利仓观察：波动型 / 平价型 × Call / Put，网格对齐义务仓。"""
    axisMap = axisByKind or {}
    byKind: dict[str, dict[str, Any]] = {}
    for sec in sections:
        kind = str(sec.get('expiry_kind') or '')
        if kind in _OPT_T_SELECTOR_EXPIRY_KINDS and kind not in byKind:
            byKind[kind] = sec

    columns: list[dict[str, Any]] = []
    for kind in _OPT_T_SELECTOR_EXPIRY_KINDS:
        label = _OPT_T_SELECTOR_EXPIRY_LABELS[kind]
        days = (byKind.get(kind) or {}).get('selector_days_to_expiry')
        tradeDays = (byKind.get(kind) or {}).get('selector_trade_days_to_expiry')
        try:
            daysInt = int(days) if days is not None else None
        except (TypeError, ValueError):
            daysInt = None
        try:
            tradeInt = int(tradeDays) if tradeDays is not None else None
        except (TypeError, ValueError):
            tradeInt = None
        daysText = _opt_t_board_selector_dte_text(daysInt, tradeInt)
        expiryHead = f'{label}（{daysText}）' if daysText else label
        columns.append({'kind': kind, 'head_expiry': expiryHead, 'dte': daysInt})

    standardRows: list[dict[str, Any]] = []
    standardAtmIndex: int | None = None
    for kind in _OPT_T_SELECTOR_EXPIRY_KINDS:
        sec = byKind.get(kind)
        if not sec:
            continue
        rows = sorted(
            (
                row for row in (sec.get('rows') or [])
                if isinstance(row, dict) and not row.get('is_adj')
            ),
            key=lambda row: float(row.get('strike') or 0),
        )
        atmIndex = next((i for i, row in enumerate(rows) if row.get('is_atm')), None)
        if atmIndex is not None:
            standardRows = rows
            standardAtmIndex = atmIndex
            break

    standardBySide: dict[str, dict[str, dict[str, Any]]] = {
        side: {} for side in ('call', 'put')
    }
    rightsBucketSets = {
        side: set(_OPT_RIGHTS_VOL_BUCKETS_BY_SIDE[side]) for side in ('call', 'put')
    }
    if standardAtmIndex is not None:
        for index, row in enumerate(standardRows):
            for side, direction in (('call', 1), ('put', -1)):
                bucket = _opt_t_board_selector_bucket(
                    direction * (index - standardAtmIndex)
                )
                if bucket in rightsBucketSets[side]:
                    standardBySide[side][bucket] = row

    rowsByKindAndStrike: dict[str, dict[str, dict[str, Any]]] = {}
    for kind, sec in byKind.items():
        rowsByKindAndStrike[kind] = {
            key: row
            for row in (sec.get('rows') or [])
            if isinstance(row, dict)
            and not row.get('is_adj')
            and (key := _opt_t_board_selector_strike_key(row)) is not None
        }

    panels: dict[str, dict[str, dict[str, Any]]] = {}
    for mode in ('vol', 'parity'):
        tabs: dict[str, dict[str, Any]] = {}
        for side in ('call', 'put'):
            gridRows: list[dict[str, Any]] = []
            specs = _optRightsRowSpecs(
                mode,
                side,
                standardBySide=standardBySide,
                rowsByKindAndStrike=rowsByKindAndStrike,
                columns=columns,
            )
            for bucket, standardRow in specs:
                strikeDisplay = (
                    standardRow.get('strike_display') if standardRow else '—'
                )
                cells: list[dict[str, Any]] = []
                for kind in _OPT_T_SELECTOR_EXPIRY_KINDS:
                    strikeKey = (
                        _opt_t_board_selector_strike_key(standardRow)
                        if standardRow else None
                    )
                    row = (
                        rowsByKindAndStrike.get(kind, {}).get(strikeKey)
                        if strikeKey else None
                    )
                    dte = next(
                        (col['dte'] for col in columns if col['kind'] == kind),
                        None,
                    )
                    cells.append(
                        _optRightsDecideCell(
                            row,
                            side=side,
                            mode=mode,
                            kind=kind,
                            dte=dte,
                            axis=axisMap.get(kind),
                        )
                    )
                # 四期限都未入选则不出这一档；全空时只留表头，与平价型一致。
                if not any(cell.get('status') == 'ready' for cell in cells):
                    continue
                gridRows.append({
                    'label': bucket,
                    'strike_display': strikeDisplay,
                    'moneyness_pct': (standardRow or {}).get('moneyness_pct') or '—',
                    'moneyness_cls': (standardRow or {}).get('moneyness_cls') or '',
                    'bucket_cls': _opt_t_board_selector_bucket_cls(bucket),
                    'cells': cells,
                })
            tabs[side] = {'rows': gridRows}
        panels[mode] = tabs

    return {
        'columns': columns,
        'vol': panels['vol'],
        'parity': panels['parity'],
    }


def _opt_t_board_moneyness_pct(strike: float, spot: float | None) -> tuple[str, str]:
    """
    虚实%：(行权价−标的价)/标的价×100，固定两位小数。
    返回 (展示文本, CSS类)：低于现价冷色、高于现价暖色、近平值中性。
    """
    if spot is None or spot <= 0 or strike != strike:
        return '—', ''
    pct = (strike - spot) / spot * 100.0
    text = _fmt_opt_pct(pct, digits=2)
    if pct < -0.5:
        return text, 'opt-t-mny-below'
    if pct > 0.5:
        return text, 'opt-t-mny-above'
    return text, 'opt-t-mny-atm'


def _opt_t_board_section_forward(
    *,
    strikes: list[float],
    calls: dict[float, dict],
    puts: dict[float, dict],
    quote_map: dict[str, dict[str, Any]],
    spot: float | None,
    daysToExpiry: int | None,
    rate: float,
) -> float | None:
    """段内近 ATM 认购/认沽平价估计隐含远期；失败则 None（调用方回退现货）。"""
    tExp = _optTIvYearFrac(daysToExpiry)
    if spot is None or spot <= 0 or tExp is None:
        return None
    try:
        from option_iv import estimate_implied_forward
    except Exception:
        return None
    rows: list[dict[str, float]] = []
    for stk in strikes:
        rc = calls.get(stk)
        rp = puts.get(stk)
        if not rc or not rp:
            continue
        qc = quote_map.get(str(rc.get('ts_code') or '').strip())
        qp = quote_map.get(str(rp.get('ts_code') or '').strip())
        cPx = _opt_daily_close_float(qc)
        pPx = _opt_daily_close_float(qp)
        if cPx is None or pPx is None or cPx <= 0 or pPx <= 0:
            continue
        rows.append(
            {
                'strike': float(stk),
                'call_price': float(cPx),
                'put_price': float(pPx),
                'volume': float(
                    (_opt_daily_vol_oi_float(qc, 'vol') or 0.0)
                    + (_opt_daily_vol_oi_float(qp, 'vol') or 0.0)
                ),
                'open_interest': float(
                    (_opt_daily_vol_oi_float(qc, 'oi') or 0.0)
                    + (_opt_daily_vol_oi_float(qp, 'oi') or 0.0)
                ),
            }
        )
    if len(rows) < 2:
        return None
    try:
        import pandas as pd

        est = estimate_implied_forward(
            pd.DataFrame(rows),
            spot=float(spot),
            time_to_expiry=tExp,
            rate=float(rate),
        )
        fwd = float(est.forward)
        return fwd if fwd > 0 else None
    except Exception:
        return None


def _opt_daily_vol_oi_float(q: dict[str, Any] | None, col: str) -> float | None:
    """opt_daily 成交量或持仓量转 float。"""
    if not q:
        return None
    v = q.get(col)
    if v is None:
        return None
    try:
        x = float(v) if not isinstance(v, Decimal) else float(v)
        if x != x:
            return None
        return x
    except (TypeError, ValueError):
        return None


def _opt_t_rank_top3_indices(values: list[float | None]) -> dict[int, int]:
    """按值降序取前三名，返回 行下标 -> 名次 1..3；同值时行序靠前者优先。"""
    pairs: list[tuple[int, float]] = []
    for i, v in enumerate(values):
        if v is None:
            continue
        try:
            fv = float(v) if not isinstance(v, Decimal) else float(v)
        except (TypeError, ValueError):
            continue
        if fv != fv:
            continue
        pairs.append((i, fv))
    pairs.sort(key=lambda t: (-t[1], t[0]))
    return {pairs[k][0]: k + 1 for k in range(min(3, len(pairs)))}


def _opt_t_rank_top3_indices_by_group(
    values: list[float | None],
    groups: list[Any],
) -> dict[int, int]:
    """各组内独立取前三，返回 行下标 -> 组内名次 1..3。"""
    byGroup: dict[Any, list[int]] = defaultdict(list)
    n = min(len(values), len(groups))
    for i in range(n):
        byGroup[groups[i]].append(i)
    out: dict[int, int] = {}
    for idxs in byGroup.values():
        local = _opt_t_rank_top3_indices([values[i] for i in idxs])
        for localI, rank in local.items():
            out[idxs[localI]] = rank
    return out


def _opt_t_rank_cell_class(side: str, metric: str, rank: int | None) -> str:
    """side=call|put metric=vol|oi rank=1..3 → 附加 CSS 类名。"""
    if rank is None or rank < 1 or rank > 3:
        return ''
    return f'opt-t-rank-{side}-{metric}-{rank}'


def _fmt_spread_qty(val: float | None) -> str:
    """价差×合约乘数后的展示。不加千分位：该列宽度只有 54px，
    极端值（如 -15000）带逗号会超列宽被裁切。"""
    if val is None:
        return '—'
    try:
        if val != val:
            return '—'
        ax = abs(val)
        if ax >= 1e6:
            return f'{val:.2f}'
        if ax >= 1:
            return f'{val:.4f}'.rstrip('0').rstrip('.')
        return f'{val:.6f}'.rstrip('0').rstrip('.')
    except (TypeError, ValueError):
        return '—'


def _fmt_strike_display(stk: float, code6: str = '', *, isAdj: bool = False) -> str:
    """行权价三位小数；调整合约（名称尾 A）在价后加 A。"""
    del code6  # 保留形参兼容旧调用
    try:
        text = f'{float(stk):.3f}'
    except (TypeError, ValueError):
        return '—'
    if isAdj:
        return text + 'A'
    return text

def _opt_daily_latest_by_group(cur: Any, codes: list[str]) -> dict[str, dict[str, Any]]:
    """按合约 MAX(trade_date) 取最新行（缺行情日对齐时的回退）。"""
    out: dict[str, dict[str, Any]] = {}
    chunk = 400
    for i in range(0, len(codes), chunk):
        part = codes[i : i + chunk]
        ph = ','.join(['%s'] * len(part))
        sql = (
            f'SELECT d.ts_code, d.trade_date, d.close, d.vol, d.oi '
            f'FROM opt_daily d '
            f'INNER JOIN ( '
            f'  SELECT ts_code, MAX(trade_date) AS mx '
            f'  FROM opt_daily WHERE ts_code IN ({ph}) GROUP BY ts_code '
            f') t ON d.ts_code = t.ts_code AND d.trade_date = t.mx'
        )
        cur.execute(sql, part)
        for row in cur.fetchall() or []:
            tc = str(row.get('ts_code') or '').strip()
            if not tc:
                continue
            out[tc] = {
                'close': row.get('close'),
                'daily_close': row.get('close'),
                'vol': row.get('vol'),
                'oi': row.get('oi'),
                'trade_date': row.get('trade_date'),
            }
    return out


def _fetch_opt_daily_latest_map(cur: Any, ts_codes: list[str]) -> dict[str, dict[str, Any]]:
    """
    各 ts_code 在 opt_daily 中 trade_date 最大的一行：close、vol、oi、trade_date。
    先按样本合约的最新交易日对齐批量取（opt_daily 两千万行时远快于逐码 GROUP BY），
    缺行情的合约再回退 MAX(trade_date)。
    """
    codes = [str(c).strip() for c in ts_codes if c and str(c).strip()]
    if not codes:
        return {}
    cur.execute(
        'SELECT MAX(trade_date) AS mx FROM opt_daily WHERE ts_code=%s',
        (codes[0],),
    )
    probe = cur.fetchone() or {}
    mx = probe.get('mx')
    if mx is None:
        return _opt_daily_latest_by_group(cur, codes)
    out: dict[str, dict[str, Any]] = {}
    chunk = 400
    mxParam = mx.isoformat() if hasattr(mx, 'isoformat') else mx
    for i in range(0, len(codes), chunk):
        part = codes[i : i + chunk]
        ph = ','.join(['%s'] * len(part))
        sql = (
            f'SELECT ts_code, trade_date, close, vol, oi '
            f'FROM opt_daily WHERE trade_date=%s AND ts_code IN ({ph})'
        )
        cur.execute(sql, [mxParam, *part])
        for row in cur.fetchall() or []:
            tc = str(row.get('ts_code') or '').strip()
            if not tc:
                continue
            out[tc] = {
                'close': row.get('close'),
                'daily_close': row.get('close'),
                'vol': row.get('vol'),
                'oi': row.get('oi'),
                'trade_date': row.get('trade_date'),
            }
    missing = [c for c in codes if c not in out]
    if missing:
        out.update(_opt_daily_latest_by_group(cur, missing))
    return out


def qmtOptToTsCode(qmtCode: str) -> str:
    """QMT .SHO/.SZO → Tushare ts_code；与大QMT实时行情.py 同口径。"""
    text = str(qmtCode or '').strip().upper()
    if text.endswith('.SHO'):
        return text[:-4] + '.SH'
    if text.endswith('.SZO'):
        return text[:-4] + '.SZ'
    return text


def _optMetaUndlMatches(metaUndl: str, code6: str) -> bool:
    raw = str(metaUndl or '').strip().upper()
    u = str(code6 or '').strip().upper()
    if not raw or not u:
        return False
    return raw.split('.', 1)[0] == u


def _overlayOptBelongsToUndl(
    qmtCode: str,
    tsCode: str,
    code6: str,
    metaMap: dict,
    quoteMap: dict,
) -> bool:
    """只叠本标的：有 optMeta.undl 则对齐六位代码；无要素则仅已在本表 quoteMap 的合约。"""
    meta = None
    if metaMap:
        meta = metaMap.get(qmtCode) or metaMap.get(str(qmtCode).upper())
    undl = meta.get('undl') if isinstance(meta, dict) else None
    if undl:
        return _optMetaUndlMatches(str(undl), code6)
    return tsCode in quoteMap


def qmtMetaToOptBasicRow(qmtCode: str, meta: dict[str, Any]) -> dict[str, Any] | None:
    """QMT 合约要素 → T 型 raw_rows 行（opt_basic 同字段）。"""
    tsCode = qmtOptToTsCode(qmtCode)
    if not tsCode or '.' not in tsCode:
        return None
    cp = str((meta or {}).get('cp') or '').strip().upper()
    if cp not in ('C', 'P'):
        return None
    strike = _opt_t_board_norm_strike((meta or {}).get('k'))
    expRaw = str((meta or {}).get('exp') or '').strip()
    md = _opt_t_board_parse_date(expRaw)
    sm = str((meta or {}).get('sm') or '').strip()
    if md is not None and len(sm) != 6:
        sm = f'{md.year:04d}{md.month:02d}'
    if strike is None or md is None or len(sm) != 6:
        return None
    return {
        'ts_code': tsCode,
        'call_put': cp,
        'exercise_price': strike,
        's_month': sm,
        'maturity_date': md,
        'per_unit': (meta or {}).get('unit'),
        'name': str((meta or {}).get('name') or ''),
    }


def mergeQmtChainGaps(
    rawRows: list[dict[str, Any]],
    listedTs: set[str],
    snapshot: dict[str, Any] | None,
    optUnderlying: str,
    perUnit: float,
) -> tuple[list[dict[str, Any]], int]:
    """QMT 有、opt_basic 没有的合约补行。已在库的 ts_code（含次日 Tushare 入库的新月）不补第二行。"""
    u = str(optUnderlying or '').strip()
    if _isCffexIndexOpt(u) or not isinstance(snapshot, dict):
        return rawRows, 0
    metaMap = snapshot.get('optMeta') if isinstance(snapshot.get('optMeta'), dict) else {}
    listed = {str(x).strip() for x in (listedTs or set()) if str(x).strip()}
    listed.update(str(r.get('ts_code') or '').strip() for r in rawRows)
    added: list[dict[str, Any]] = []
    for qmtCode, meta in metaMap.items():
        if not isinstance(meta, dict) or not meta:
            continue
        if not _optMetaUndlMatches(str(meta.get('undl') or ''), u):
            continue
        tsCode = qmtOptToTsCode(str(qmtCode))
        if tsCode in listed:
            continue
        row = qmtMetaToOptBasicRow(str(qmtCode), meta)
        if row is None:
            continue
        added.append(row)
        listed.add(tsCode)
    if not added:
        return rawRows, 0
    return list(rawRows) + added, len(added)


def decodeQuoteTickDate(t: Any) -> date | None:
    """QMT time/stime → 日期。毫秒/秒时间戳；过小的数忽略。"""
    if t is None or t == '':
        return None
    if isinstance(t, datetime):
        return t.date()
    if isinstance(t, date):
        return t
    if isinstance(t, str):
        text = t.strip()
        if len(text) >= 10 and text[4] == '-' and text[7] == '-':
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
        try:
            t = float(text)
        except ValueError:
            return None
    try:
        v = float(t)
    except (TypeError, ValueError):
        return None
    if v > 1e12:
        return datetime.fromtimestamp(v / 1000.0).date()
    if v > 1e11:
        s = str(int(v))
        if len(s) >= 8:
            try:
                return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
            except ValueError:
                return None
        return None
    if v > 1e9:
        return datetime.fromtimestamp(v).date()
    return None


def decodeQuoteTickAt(t: Any) -> datetime | None:
    """QMT/TDX 原始 tick 时钟转本地 datetime；无法证实的值不猜测。"""
    if t is None or t == '':
        return None
    if isinstance(t, datetime):
        return t
    if isinstance(t, date):
        return datetime.combine(t, dtTime.min)
    if isinstance(t, str):
        text = t.strip()
        if len(text) >= 19 and text[4] == '-' and text[7] == '-':
            try:
                return datetime.fromisoformat(text.replace('Z', '+00:00')).replace(tzinfo=None)
            except ValueError:
                return None
        compact = text.split('.', 1)[0]
        if len(compact) >= 14 and compact[:14].isdigit():
            try:
                return datetime.strptime(compact[:14], '%Y%m%d%H%M%S')
            except ValueError:
                return None
        try:
            t = float(text)
        except ValueError:
            return None
    try:
        v = float(t)
    except (TypeError, ValueError):
        return None
    try:
        if v > 1e12:
            return datetime.fromtimestamp(v / 1000.0)
        if v > 1e11:
            compact = str(int(v))
            if len(compact) >= 14:
                return datetime.strptime(compact[:14], '%Y%m%d%H%M%S')
            return None
        if v > 1e9:
            return datetime.fromtimestamp(v)
    except (OverflowError, OSError, ValueError):
        return None
    return None


def _optTBoardLastMarketEventAt(
    snap: dict[str, Any] | None,
    optUnderlying: str,
    asof: date | None = None,
) -> datetime | None:
    """本标的 ETF/期权原始 tick 中最后一个可验证的市场事件时间。

    `optWall`/`bookTime` 是本地读快照或合并的观测时钟，不能作为市场事件；仅采用
    t/time/stime，并限于页面当前行情日，避免旧缓存被标为实时。
    """
    if not isinstance(snap, dict):
        return None
    targetDay = asof or date.today()
    candidates: list[datetime] = []

    def addTickTimes(packed: Any) -> None:
        if not isinstance(packed, dict) or packed.get('fromDaily'):
            return
        for key in _OPT_TICK_COMMON_TIME_KEYS:
            at = decodeQuoteTickAt(packed.get(key))
            if at is not None and at.date() == targetDay:
                candidates.append(at)

    etfMap = snap.get('etf') if isinstance(snap.get('etf'), dict) else {}
    etfCode = _optUnderlyingToQmtEtf(optUnderlying)
    addTickTimes(etfMap.get(etfCode) or etfMap.get(etfCode.upper()))

    optMap = snap.get('opt') if isinstance(snap.get('opt'), dict) else {}
    metaMap = snap.get('optMeta') if isinstance(snap.get('optMeta'), dict) else {}
    for qmtCode, packed in optMap.items():
        meta = metaMap.get(qmtCode) or metaMap.get(str(qmtCode).upper())
        if not isinstance(meta, dict) or not _optMetaUndlMatches(meta.get('undl'), optUnderlying):
            continue
        addTickTimes(packed)
    return max(candidates) if candidates else None


def _optTickFloat(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if x != x:
        return None
    return x


_OPT_TICK_COMMON_TIME_KEYS = ('t', 'time', 'stime')
_OPT_TICK_LAST_TIME_KEYS = ('lastTime', 'last_time')
_OPT_TICK_QTY_TIME_KEYS = ('qtyTime', 'qty_time')
_OPT_TICK_OI_TIME_KEYS = ('oiTime', 'oi_time', 'qtyTime', 'qty_time')
_OPT_TICK_BOOK_TIME_KEYS = ('bookTime', 'book_time')
_OPT_TICK_LAST_CHANGED_TIME_KEYS = ('lastChangedTime', 'last_changed_time')
_OPT_TICK_QTY_CHANGED_TIME_KEYS = ('qtyChangedTime', 'qty_changed_time')
_OPT_TICK_OI_CHANGED_TIME_KEYS = ('oiChangedTime', 'oi_changed_time')
_OPT_TICK_BOOK_CHANGED_TIME_KEYS = ('bookChangedTime', 'book_changed_time')


def _optTickFieldDate(
    packed: dict[str, Any] | None,
    fieldTimeKeys: tuple[str, ...],
) -> date | None:
    """读取字段自己的行情日；显式字段时钟无效时不得回退 common time。"""
    if not isinstance(packed, dict):
        return None
    hasExplicitClock = any(key in packed for key in fieldTimeKeys)
    for key in fieldTimeKeys:
        if key not in packed:
            continue
        tickDay = decodeQuoteTickDate(packed.get(key))
        if tickDay is not None:
            return tickDay
    if hasExplicitClock:
        return None
    for key in _OPT_TICK_COMMON_TIME_KEYS:
        tickDay = decodeQuoteTickDate(packed.get(key))
        if tickDay is not None:
            return tickDay
    return None


def _optTickFieldIsAsof(
    packed: dict[str, Any] | None,
    asof: date,
    fieldTimeKeys: tuple[str, ...],
) -> bool:
    return _optTickFieldDate(packed, fieldTimeKeys) == asof


def isStubDaqmtEtfTick(packed: dict[str, Any] | None) -> bool:
    """QMT 今日无量占位柱：vol=0 或 OHLC 粘在同一价。不能当今日现货。"""
    if not isinstance(packed, dict):
        return True
    vol = packed.get('vol')
    if vol is None:
        vol = packed.get('volume')
    volF = _optTickFloat(vol)
    if volF is not None and volF <= 0:
        return True
    ohlc: list[float] = []
    for key in ('open', 'high', 'low'):
        px = _optTickFloat(packed.get(key))
        if px is None:
            return False
        ohlc.append(px)
    last = _optTickFloat(packed.get('last'))
    if last is None:
        last = _optTickFloat(packed.get('lastPrice'))
    if last is None:
        return True
    ohlc.append(last)
    return ohlc[0] == ohlc[1] == ohlc[2] == ohlc[3]


def isTodayOptLast(
    packed: dict[str, Any] | None,
    asof: date,
) -> bool:
    """当日 last 有效即可，不要求有成交量。供 IV 覆盖面使用。"""
    if not isinstance(packed, dict):
        return False
    last = _optTickFloat(packed.get('last'))
    if last is None:
        last = _optTickFloat(packed.get('lastPrice'))
    if last is None or last <= 0:
        return False
    return _optTickFieldIsAsof(packed, asof, _OPT_TICK_LAST_TIME_KEYS)


def isFreshDaqmtOptTick(
    packed: dict[str, Any] | None,
    asof: date,
    dailyClose: Any = None,
) -> bool:
    """当日有成交：last/累计成交量都必须有可解析的当日字段时钟。"""
    if not isinstance(packed, dict):
        return False
    last = _optTickFloat(packed.get('last'))
    if last is None:
        last = _optTickFloat(packed.get('lastPrice'))
    if last is None or last <= 0:
        return False
    vol = packed.get('vol')
    if vol is None:
        vol = packed.get('volume')
    volF = _optTickFloat(vol)
    if volF is None or volF <= 0:
        return False
    return _optTickFieldIsAsof(
        packed, asof, _OPT_TICK_LAST_TIME_KEYS
    ) and _optTickFieldIsAsof(packed, asof, _OPT_TICK_QTY_TIME_KEYS)


def blendSectionQuoteAsof(
    calls: dict[float, dict],
    puts: dict[float, dict],
    quoteMap: dict[str, dict[str, Any]],
    dailyAsof: str,
    liveClock: str,
) -> tuple[str, str]:
    """段头行情标签。返回 (文案, live|partial|daily)。

    无盘口/无成交叠价时标「无实时」；有 live（含 TDX 盘口）则用 liveClock。
    """
    n = 0
    live = 0
    for bucket in (calls, puts):
        for row in bucket.values():
            tc = str(row.get('ts_code') or '').strip()
            if not tc:
                continue
            n += 1
            q = quoteMap.get(tc) or {}
            if q.get('live') or q.get('has_book'):
                live += 1
    if live <= 0:
        day = str(dailyAsof or '').strip()
        if day:
            return f'无实时 · {day}', 'daily'
        return '无实时', 'daily'
    clock = liveClock or '实时'
    if n > 0 and live < n:
        return f'{clock} · {live}/{n}张', 'partial'
    return clock, 'live'


def _optTBoardLiveClockLabel(
    quoteSource: str,
    snap: dict[str, Any] | None,
    optUnderlying: str = '',
    *,
    asof: date | None = None,
) -> str:
    """段头时钟；传入标的后，明确区分本地观测与最后市场事件。"""
    src = str(quoteSource or '').strip().lower()
    clock = ''
    if isinstance(snap, dict):
        clock = str(snap.get('optWall') or snap.get('etfWall') or '')
    hhmmss = clock[11:19] if len(clock) >= 19 else ''
    eventAt = _optTBoardLastMarketEventAt(snap, optUnderlying, asof) if optUnderlying else None
    if optUnderlying:
        sourceLabel = {
            'tdx': 'TDX盘口',
            'mixed': '混合·TDX补',
            'daqmt': 'QMT',
        }.get(src, '')
        parts = []
        if hhmmss:
            parts.append(f'本地观测 {hhmmss}')
        if eventAt is not None:
            parts.append(f'最后市场事件 {eventAt.strftime("%H:%M:%S")}')
        if sourceLabel:
            parts.append(sourceLabel)
        return ' · '.join(parts)
    wallHm = clock[11:16] if len(clock) >= 16 else ''
    # 粗判盘后：墙钟晚于 15:00（或无墙钟但源为纯 TDX 且本地已过 15:00）
    afterHours = False
    if wallHm:
        afterHours = wallHm > '15:00'
    else:
        afterHours = datetime.now().strftime('%H:%M') > '15:00'
    if src == 'tdx':
        if afterHours:
            return f'收盘盘口(TDX) {hhmmss}'.strip() if hhmmss else '收盘盘口(TDX)'
        return f'实时(TDX) {hhmmss}'.strip() if hhmmss else '实时(TDX)'
    if src == 'mixed':
        return f'实时·TDX补 {hhmmss}'.strip() if hhmmss else '实时·TDX补'
    if src == 'daqmt':
        return f'实时 {hhmmss}'.strip() if hhmmss else '实时'
    return ''


def _optUnderlyingToQmtEtf(code6: str) -> str:
    u = str(code6 or '').strip()
    if u == '159915':
        return '159915.SZ'
    return f'{u}.SH'


def fetchDaqmtQuoteSnapshot() -> dict[str, Any] | None:
    """读大QMT实时行情.py 本地快照；失败冷却，避免盘后拖慢 T 型查询。"""
    global _daqmtDownUntil, _daqmtSnapMemo
    url = (DAQMT_QUOTE_SNAPSHOT_URL or '').strip()
    if not url:
        return None
    now = time.monotonic()
    with _daqmtSnapMemoLock:
        t0, cached = _daqmtSnapMemo
        if cached is not None and (now - t0) < DAQMT_QUOTE_MEMO_SEC:
            return cached
    if now < _daqmtDownUntil:
        return None
    try:
        req = Request(url, method='GET')
        with urlopen(req, timeout=DAQMT_QUOTE_FETCH_TIMEOUT_SEC) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
        data = json.loads(raw)
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        _daqmtDownUntil = time.monotonic() + DAQMT_QUOTE_DOWN_COOLDOWN_SEC
        return None
    except Exception:
        _daqmtDownUntil = time.monotonic() + DAQMT_QUOTE_DOWN_COOLDOWN_SEC
        return None
    if not isinstance(data, dict) or not data.get('ok'):
        return None
    with _daqmtSnapMemoLock:
        _daqmtSnapMemo = (time.monotonic(), data)
    return data


def overlayDaqmtQuotes(
    quoteMap: dict[str, dict[str, Any]],
    underlyingClose: float | None,
    optUnderlying: str,
    snapshot: dict[str, Any] | None,
    *,
    asof: date | None = None,
) -> tuple[dict[str, dict[str, Any]], float | None, int, str]:
    """
    持仓/成交/最新以 QMT/TDX 为准：当日 vol>0 则叠 last/vol/oi；并始终叠 bid1/ask1。
    无成交时最新=昨收、成交=0。只叠本标的。
    """
    u = str(optUnderlying or '').strip()
    if not snapshot or _isCffexIndexOpt(u):
        return quoteMap, underlyingClose, 0, 'opt_daily'
    optSnap = snapshot.get('opt') if isinstance(snapshot.get('opt'), dict) else {}
    etfSnap = snapshot.get('etf') if isinstance(snapshot.get('etf'), dict) else {}
    metaMap = snapshot.get('optMeta') if isinstance(snapshot.get('optMeta'), dict) else {}
    asofDate = asof or date.today()
    liveN = 0
    bookN = 0
    liveSources: set[str] = set()
    for qmtCode, packed in optSnap.items():
        if not isinstance(packed, dict):
            continue
        tsCode = qmtOptToTsCode(str(qmtCode))
        if not _overlayOptBelongsToUndl(str(qmtCode), tsCode, u, metaMap, quoteMap):
            continue
        old = quoteMap.get(tsCode) or {}
        last = packed.get('last')
        if last is None:
            last = packed.get('lastPrice')
        try:
            lastF = float(last) if last is not None else None
        except (TypeError, ValueError):
            lastF = None
        isLive = bool(
            lastF is not None
            and lastF == lastF
            and lastF > 0
            and isFreshDaqmtOptTick(packed, asofDate, dailyClose=old.get('close'))
        )
        vol = packed.get('vol')
        if vol is None:
            vol = packed.get('volume')
        oi = packed.get('oi')
        if oi is None:
            oi = packed.get('openInt')
        dailyClose = old.get('daily_close')
        if dailyClose is None:
            dailyClose = old.get('close')
        try:
            dailyCloseF = float(dailyClose) if dailyClose is not None else None
            if dailyCloseF is not None and (
                dailyCloseF != dailyCloseF or dailyCloseF <= 0
            ):
                dailyCloseF = None
        except (TypeError, ValueError):
            dailyCloseF = None
        rawBid1 = _optQuotePxPositive(packed.get('bid1'))
        if rawBid1 is None and isinstance(packed.get('bidPrice'), (list, tuple)) and packed.get('bidPrice'):
            rawBid1 = _optQuotePxPositive(packed['bidPrice'][0])
        rawAsk1 = _optQuotePxPositive(packed.get('ask1'))
        if rawAsk1 is None and isinstance(packed.get('askPrice'), (list, tuple)) and packed.get('askPrice'):
            rawAsk1 = _optQuotePxPositive(packed['askPrice'][0])
        lastClockLive = _optTickFieldIsAsof(
            packed, asofDate, _OPT_TICK_LAST_TIME_KEYS
        )
        qtyClockLive = _optTickFieldIsAsof(
            packed, asofDate, _OPT_TICK_QTY_TIME_KEYS
        )
        bookClockLive = _optTickFieldIsAsof(
            packed, asofDate, _OPT_TICK_BOOK_TIME_KEYS
        )
        oiClockLive = _optTickFieldIsAsof(
            packed, asofDate, _OPT_TICK_OI_TIME_KEYS
        )
        bookLive = bool((rawBid1 is not None or rawAsk1 is not None) and bookClockLive)
        bid1 = rawBid1 if bookLive else None
        ask1 = rawAsk1 if bookLive else None
        # 当日有成交（last+vol）：直接用源端最新/成交量。
        # 不要求 last∈买卖一——成交后盘口挪动极常见，旧规则会大面积误杀成昨收+量0。
        scaleBad = _optLastScaleAbsurd(lastF, bid1, ask1)
        tradeLive = bool(isLive and not scaleBad)
        fromTdx = bool(packed.get('fromTdx'))
        qtyFromQmt = bool(packed.get('qtyFromQmt'))
        defaultTradeSource = 'qmt' if qtyFromQmt or not fromTdx else 'tdx'
        lastSource = str(packed.get('lastSource') or defaultTradeSource).strip().lower()
        qtySource = str(packed.get('qtySource') or defaultTradeSource).strip().lower()
        oiSource = str(packed.get('oiSource') or qtySource).strip().lower()
        bookSource = str(
            packed.get('bookSource') or ('tdx' if fromTdx else 'qmt')
        ).strip().lower()
        # TDX 有档且源端 last>0：即使 Volume 滞后为 0，也不回退昨收
        #（量仍按 tradeLive；无量显示 0）。纯 QMT 无量占位不走此路。
        sourceLastOk = bool(
            fromTdx
            and bookLive
            and lastClockLive
            and lastF is not None
            and lastF == lastF
            and lastF > 0
            and not scaleBad
        )
        trustedLast = lastF if (tradeLive or sourceLastOk) else None
        lastLive = trustedLast is not None
        closeVal = trustedLast if trustedLast is not None else dailyCloseF
        try:
            volF = float(vol) if vol is not None else None
            if volF is not None and (volF != volF or volF < 0):
                volF = None
        except (TypeError, ValueError):
            volF = None
        oiF = _optTickFloat(oi)
        oiLive = bool(oiF is not None and oiF >= 0 and oiClockLive)
        anyLive = bool(lastLive or bookLive or oiLive)
        oldBookLive = bool(old.get('book_live'))
        quoteMap[tsCode] = {
            'close': closeVal,
            'daily_close': dailyCloseF,
            'last': trustedLast,
            'bid1': bid1 if bookLive else (old.get('bid1') if oldBookLive else None),
            'ask1': ask1 if bookLive else (old.get('ask1') if oldBookLive else None),
            # 有实时成交用源端量；无成交才 0（勿因 last 略出档口把真实量清掉）
            'vol': volF if tradeLive else 0,
            'oi': oiF if oiLive else old.get('oi'),
            'trade_date': asofDate if anyLive else old.get('trade_date'),
            # `live` 仅为兼容旧调用；字段语义必须读取下面四个状态。
            'live': anyLive,
            'last_live': lastLive,
            'trade_live': tradeLive,
            'book_live': bookLive,
            'oi_live': oiLive,
            'has_book': bookLive,
            'from_tdx': fromTdx,
            'last_source': lastSource if lastLive else '',
            'qty_source': qtySource if tradeLive else '',
            'oi_source': oiSource if oiLive else '',
            'book_source': bookSource if bookLive else '',
        }
        if anyLive:
            liveN += 1
            if lastLive and lastSource:
                liveSources.add(lastSource)
            if tradeLive and qtySource:
                liveSources.add(qtySource)
            if oiLive and oiSource:
                liveSources.add(oiSource)
            if bookLive and bookSource:
                liveSources.add(bookSource)
        if bookLive:
            bookN += 1
    etfPacked = etfSnap.get(_optUnderlyingToQmtEtf(u))
    if (
        isinstance(etfPacked, dict)
        and etfPacked.get('last') is not None
        and not isStubDaqmtEtfTick(etfPacked)
    ):
        try:
            spot = float(etfPacked['last'])
            if spot == spot and spot > 0:
                etfDay = decodeQuoteTickDate(
                    etfPacked.get('t')
                    or etfPacked.get('time')
                    or etfPacked.get('stime')
                )
                if etfDay is None or etfDay == asofDate:
                    underlyingClose = spot
        except (TypeError, ValueError):
            pass
    if liveN <= 0 and bookN <= 0:
        return quoteMap, underlyingClose, 0, 'opt_daily'
    if liveSources == {'tdx'}:
        src = 'tdx'
    elif 'tdx' in liveSources:
        src = 'mixed'
    else:
        src = 'daqmt'
    return quoteMap, underlyingClose, liveN, src


_OPT_T_BOARD_UNDERLYING_RE = re.compile(r'^[0-9]{6}$')


def _opt_t_board_symbol_profile(code6: str) -> dict[str, Any]:
    try:
        from opt_spread_board_select import getSymbolProfile

        return getSymbolProfile(code6)
    except ImportError:
        return {
            'name': '',
            'elasticRank': 99,
            'tpMult': 2.0,
            'sweetMin': 0.30,
            'sweetMax': 0.40,
            'caution': None,
        }


_optTBoardProfilesCache: dict[str, Any] | None = None


def _opt_t_board_profiles_for_page() -> dict[str, Any]:
    global _optTBoardProfilesCache
    if _optTBoardProfilesCache is not None:
        return _optTBoardProfilesCache
    try:
        from opt_spread_board_select import ELASTIC_ORDER_LABEL, profilesForBoardJson

        _optTBoardProfilesCache = {
            'symbolProfiles': profilesForBoardJson(),
            'elasticOrderLabel': ELASTIC_ORDER_LABEL,
        }
    except ImportError:
        _optTBoardProfilesCache = {'symbolProfiles': {}, 'elasticOrderLabel': ''}
    return _optTBoardProfilesCache


def _opt_t_board_expiry_kind_cn(expiry_kind: str) -> str:
    cn = {
        'near': '当月',
        'next': '下月',
        'next_next': '下下月',
        'quarter': '当季',
        'next_quarter': '下季备选',
        'next_next_quarter': '下下季',
    }
    return cn.get(expiry_kind, expiry_kind)


def _opt_t_board_expiry_display_label(expiry_kind: str) -> str:
    """T 型段头仅展示 ETF 四个标准期限桶，不影响策略文案。"""
    displayLabels = {
        'near': '当月',
        'next': '下月',
        'quarter': '当季',
        'next_quarter': '下季',
    }
    return displayLabels.get(expiry_kind, '')


def _opt_t_board_format_expiry_meta(
    expiryDateDisplay: str,
    daysToExpiryLabel: str | None,
    expiryKind: str,
) -> str:
    """段头保留原到期日/DTE；可识别期限才追加展示标签。"""
    if not expiryDateDisplay or not daysToExpiryLabel:
        return ''
    expiryMeta = f'{expiryDateDisplay} · {daysToExpiryLabel}'
    displayLabel = _opt_t_board_expiry_display_label(expiryKind)
    return f'{expiryMeta} · {displayLabel}' if displayLabel else expiryMeta


def _opt_t_board_parse_date(value: object) -> date | None:
    """解析 opt_basic 日期值，失败返回 None。"""
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


def _opt_t_board_days_to_expiry(
    maturity_d: date | None,
    *,
    asof: date | None = None,
) -> int | None:
    """
    距到期日剩余自然日；解析失败返回 None。
    asof：优先用该段 opt_daily 行情日；缺省才用今天（无行情时的兜底）。
    """
    if maturity_d is None:
        return None
    base = asof if asof is not None else date.today()
    return (maturity_d - base).days


def _opt_t_board_trade_days_to_expiry(
    maturity_d: date | None,
    *,
    asof: date | None = None,
    openDates: set[date] | None = None,
) -> int | None:
    """距到期剩余交易日：asof < 开市日 ≤ maturity。

    若 trade_cal 未覆盖到到期日，返回 None（禁止用截断日历低估，例如库只到年底而到期在次年）。
    """
    if maturity_d is None:
        return None
    base = asof if asof is not None else date.today()
    if maturity_d <= base:
        return 0
    if openDates is None:
        openDates = _opt_t_board_load_sse_open_dates(base, maturity_d)
    if not openDates:
        return None
    # 覆盖校验：集合内最远开市日必须 ≥ 到期日，否则说明库未铺到该期限
    if max(openDates) < maturity_d:
        return None
    return sum(1 for d in openDates if base < d <= maturity_d)


def _opt_t_board_load_sse_open_dates(start: date, end: date) -> set[date]:
    """加载 [start, end] 内 SSE 开市日；失败返回空集。"""
    if end < start:
        return set()
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    except Exception:
        logger.exception('trade_cal 连接失败')
        return set()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cal_date FROM trade_cal
                WHERE exchange=%s AND is_open=1
                  AND cal_date>=%s AND cal_date<=%s
                """,
                ('SSE', start.isoformat(), end.isoformat()),
            )
            out: set[date] = set()
            for (raw,) in cur.fetchall():
                d = _opt_t_board_parse_date(raw)
                if d is not None:
                    out.add(d)
            return out
    except Exception:
        logger.exception('trade_cal 开市日查询失败')
        return set()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _opt_t_board_dte_pair_text(
    calDays: int | None,
    tradeDays: int | None,
    *,
    prefix: str = '',
) -> str | None:
    """自然日[.交易日]；prefix 如「剩」。缺交易日则只写自然日。"""
    if calDays is None:
        return None
    if calDays <= 0:
        return '今日到期'
    body = f'{calDays}天' if tradeDays is None else f'{calDays}天.{tradeDays}'
    return f'{prefix}{body}'


def _opt_t_board_days_to_expiry_label(
    days: int | None,
    tradeDays: int | None = None,
) -> str | None:
    """剩余天数展示文案（相对行情日或今天）；可附交易日。"""
    return _opt_t_board_dte_pair_text(days, tradeDays, prefix='剩')


def _opt_t_board_selector_dte_text(
    calDays: int | None,
    tradeDays: int | None = None,
) -> str | None:
    """义务仓/权利仓表头括号内天数：36天.25 / 今日到期。"""
    return _opt_t_board_dte_pair_text(calDays, tradeDays, prefix='')


def _opt_t_board_section_quote_asof(
    calls: dict[float, dict],
    puts: dict[float, dict],
    quote_map: dict[str, dict[str, Any]],
) -> str:
    """分区内合约 opt_daily 最新 trade_date 的最大值（YYYY-MM-DD）；无行情则空串。"""
    asofDates: list[date] = []
    for bucket in (calls, puts):
        for row in bucket.values():
            tc = str(row.get('ts_code') or '').strip()
            if not tc:
                continue
            q = quote_map.get(tc)
            if not q:
                continue
            d = _opt_t_board_parse_date(q.get('trade_date'))
            if d is not None:
                asofDates.append(d)
    if not asofDates:
        return ''
    return max(asofDates).isoformat()


def _opt_t_board_pick_atm_strike(strikes: list[float], spot: float | None) -> float | None:
    """按 |行权价-标的价| 最小取平值行权价。"""
    if spot is None or not strikes:
        return None
    return min(strikes, key=lambda s: abs(s - spot))


def _opt_t_board_pick_atm_by_adj(
    rowKeys: list[tuple[float, float, int]],
    spot: float | None,
) -> dict[int, float | None]:
    """标准(0)与调整(1)各自取平值行权价，互不混用。"""
    return {
        0: _opt_t_board_pick_atm_strike([k[0] for k in rowKeys if k[2] == 0], spot),
        1: _opt_t_board_pick_atm_strike([k[0] for k in rowKeys if k[2] == 1], spot),
    }


def _opt_t_board_spot_ts_code(opt_underlying: str) -> tuple[str, str]:
    """返回 (表名, ts_code)：ETF→etf_daily；中金所→index_daily。"""
    code6 = str(opt_underlying or '').strip()
    if _isCffexIndexOpt(code6):
        spotTs = f'{code6}.SH'
        try:
            from opt_spread_board_select import getSymbolProfile

            spotTs = str(getSymbolProfile(code6).get('spotTsCode') or spotTs)
        except Exception:
            pass
        return 'index_daily', spotTs
    if code6 == '159915':
        return 'etf_daily', '159915.SZ'
    return 'etf_daily', f'{code6}.SH'


def _opt_t_board_fetch_underlying_close(cur: Any, opt_underlying: str) -> float | None:
    """读取标的最新 close：ETF→etf_daily；中金所股指期权→index_daily。"""
    table, spotTs = _opt_t_board_spot_ts_code(opt_underlying)
    cur.execute(
        f"""
        SELECT close
        FROM {table}
        WHERE ts_code = %s
          AND close IS NOT NULL
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (spotTs,),
    )
    row = cur.fetchone()
    if not row:
        return None
    try:
        x = float(row.get('close'))
        if x != x:
            return None
        return x
    except (TypeError, ValueError):
        return None


def buildTrendSma120(
    closes: list[float],
    *,
    spot: float | None,
    asof: date | None = None,
    source: str = 'etf_daily',
    window: int = 120,
) -> dict[str, Any]:
    """
    现价 vs SMA120 状态（只展示，不禁开）。
    closes：按交易日升序的收盘价；spot 优先用盘中现货，否则用末日收盘。
    """
    empty: dict[str, Any] = {
        'ok': False,
        'pass': None,
        'spot': None,
        'sma': None,
        'gap_pct': None,
        'gap_text': '',
        'label': '—',
        'asof': asof.isoformat() if asof else '',
        'source': source,
        'title': 'SMA120 样本不足（需 ≥120 交易日）',
    }
    if window < 1 or len(closes) < window:
        return empty
    series = [float(x) for x in closes if x is not None and float(x) == float(x) and float(x) > 0]
    if len(series) < window:
        return empty
    sma = sum(series[-window:]) / float(window)
    if sma <= 0:
        return empty
    px = float(spot) if spot is not None and spot == spot and spot > 0 else series[-1]
    gapPct = (px / sma - 1.0) * 100.0
    isPass = px >= sma
    gapText = f'{gapPct:+.2f}%'
    # 灯面只留 SMA120 + 缺口%；过/未过由绿/橙与正负号表达
    label = 'SMA120'
    title = (
        f'现价 {px:.4f} ≥ SMA120 {sma:.4f}（动量闸口径）；相对均线 {gapText}'
        if isPass
        else f'现价 {px:.4f} < SMA120 {sma:.4f}（动量闸口径）；相对均线 {gapText}'
    )
    return {
        'ok': True,
        'pass': isPass,
        'spot': round(px, 4),
        'sma': round(sma, 4),
        'gap_pct': round(gapPct, 2),
        'gap_text': gapText,
        'label': label,
        'asof': asof.isoformat() if asof else '',
        'source': source,
        'title': title,
    }


def _liveSpotFromDaqmtSnap(
    code6: str,
    snapshot: dict[str, Any] | None,
    *,
    asof: date | None = None,
) -> float | None:
    """从大 QMT 快照取 ETF/指数现货最新价；无则 None。"""
    if not snapshot or _isCffexIndexOpt(code6):
        return None
    etfSnap = snapshot.get('etf') if isinstance(snapshot.get('etf'), dict) else {}
    packed = etfSnap.get(_optUnderlyingToQmtEtf(code6))
    if not isinstance(packed, dict) or packed.get('last') is None:
        return None
    if isStubDaqmtEtfTick(packed):
        return None
    try:
        spot = float(packed['last'])
    except (TypeError, ValueError):
        return None
    if not (spot == spot and spot > 0):
        return None
    asofDate = asof or date.today()
    etfDay = decodeQuoteTickDate(
        packed.get('t') or packed.get('time') or packed.get('stime')
    )
    if etfDay is not None and etfDay != asofDate:
        return None
    return spot


def _fmtChipSpotLast(val: float) -> str:
    return f'{val:.3f}'


def _fmtChipSpotPct(pct: float) -> str:
    return f'{pct:+.2f}'


def _chipSpotIntraPp(last: float | None, openPx: float | None) -> float | None:
    """与主页 Ma/Ema 方向表 Intra 同式：(最新 − 开盘) / 开盘 × 100。"""
    if last is None or openPx is None or openPx == 0:
        return None
    return (last - openPx) / openPx * 100.0


def _chipSpotGapPp(openPx: float | None, lastClose: float | None) -> float | None:
    """与主页 Ma/Ema 方向表 Gap 同式：(开盘 − 昨收) / 昨收 × 100。"""
    if openPx is None or lastClose is None or lastClose == 0 or openPx <= 0:
        return None
    return (openPx - lastClose) / lastClose * 100.0


def _chipSpotAmpPp(
    highPx: float | None,
    lowPx: float | None,
    lastClose: float | None,
) -> float | None:
    """与主页 Ma/Ema 方向表 Amp 同式：(最高 − 最低) / 昨收 × 100。无涨跌方向。"""
    if highPx is None or lowPx is None or lastClose is None or lastClose == 0:
        return None
    if highPx < lowPx:
        return None
    return (highPx - lowPx) / lastClose * 100.0


def _fmtChipSpotAmp(amp: float) -> str:
    return f'{amp:.2f}'


def _chipGapPerLast(gaps: list[float]) -> float | None:
    """全样本 expanding 分位的最后一点，与 opt_sp_plan.expandingPctile 同一公式。"""
    from rt_monitor.services.opt_sp_plan import MIN_HIST

    finite = [float(g) for g in gaps if g is not None and g == g]
    if len(finite) < int(MIN_HIST):
        return None
    cur = finite[-1]
    less = 0
    eq = 0
    for x in finite:
        if x < cur:
            less += 1
        elif x == cur:
            eq += 1
    return (less + 0.5 * eq) / len(finite) * 100.0


def _fmtChipGapPer(per: float) -> str:
    return f'{per:.1f}'


def _chipRankPer(sample: list[float], cur: float | None) -> int | None:
    """与主页 Ma/Ema 旁注分位同式：100×（严格小于个数）/ n，四舍五入到整数。样本不含当日。"""
    if cur is None or cur != cur or not sample:
        return None
    lt = 0
    n = 0
    for v in sample:
        if v is None or v != v:
            continue
        n += 1
        if v < cur:
            lt += 1
    if n <= 0:
        return None
    raw = Decimal(lt) / Decimal(n) * Decimal(100)
    return int(raw.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


_chipGapHistLock = threading.Lock()
_chipGapHistMemo: dict[str, tuple[float, str, list[float]]] = {}
_CHIP_GAP_HIST_TTL_SEC = 60.0
_chipGapPerValLock = threading.Lock()
_chipGapPerValMemo: dict[str, tuple[str, float, float]] = {}
_chipDirHistLock = threading.Lock()
_chipDirHistMemo: dict[str, tuple[float, str, dict[str, list[float]]]] = {}


def _chipGapRowDate(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def _chipGapHistFor(code6: str) -> list[float]:
    """etf_daily/index_daily 历史 Gap，不含当日（当日用盘中开盘覆盖）。"""
    u = str(code6 or '').strip()
    if not _OPT_T_BOARD_UNDERLYING_RE.match(u):
        return []
    asof = date.today().isoformat()
    now = time.monotonic()
    with _chipGapHistLock:
        hit = _chipGapHistMemo.get(u)
        gapFresh = (
            hit is not None
            and (now - hit[0]) < _CHIP_GAP_HIST_TTL_SEC
            and hit[1] == asof
        )
        gapCopy = list(hit[2]) if gapFresh and hit is not None else None
    if gapCopy is not None:
        with _chipDirHistLock:
            dhit = _chipDirHistMemo.get(u)
            dirFresh = dhit is not None and dhit[1] == asof
        if dirFresh:
            return gapCopy
    table, spotTs = _opt_t_board_spot_ts_code(u)
    hist: list[float] = []
    pctHist: list[float] = []
    intraHist: list[float] = []
    ampHist: list[float] = []
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE, cursorclass=pymysql.cursors.DictCursor)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT trade_date, open, pre_close, close, pct_chg, high, low
                    FROM {table}
                    WHERE ts_code = %s
                    ORDER BY trade_date
                    """,
                    (spotTs,),
                )
                today = date.today()
                for row in cur.fetchall() or []:
                    d = _chipGapRowDate(row.get('trade_date'))
                    if d is None or d >= today:
                        continue
                    op = _optTickFloat(row.get('open'))
                    pre = _optTickFloat(row.get('pre_close'))
                    cl = _optTickFloat(row.get('close'))
                    gap = _chipSpotGapPp(op, pre)
                    if gap is not None:
                        hist.append(gap)
                    pctChg = _optTickFloat(row.get('pct_chg'))
                    if pctChg is None and cl is not None and pre not in (None, 0):
                        pctChg = (cl - pre) / pre * 100.0
                    if pctChg is not None:
                        pctHist.append(float(pctChg))
                    intra = _chipSpotIntraPp(cl, op)
                    if intra is not None:
                        intraHist.append(float(intra))
                    amp = _chipSpotAmpPp(
                        _optTickFloat(row.get('high')),
                        _optTickFloat(row.get('low')),
                        pre,
                    )
                    if amp is not None:
                        ampHist.append(float(amp))
        finally:
            conn.close()
    except Exception:
        hist = []
        pctHist = []
        intraHist = []
        ampHist = []
    stamped = time.monotonic()
    with _chipGapHistLock:
        _chipGapHistMemo[u] = (stamped, asof, list(hist))
    with _chipDirHistLock:
        _chipDirHistMemo[u] = (
            stamped,
            asof,
            {
                'pct': list(pctHist),
                'intra': list(intraHist),
                'gap': list(hist),
                'amp': list(ampHist),
            },
        )
    return hist


def _chipDirPerOf(code6: str, kind: str, cur: float | None) -> int | None:
    """Pct / Intra / Gap / Amp 的主页同式分位。历史加载被测试替身替换时，样本为空则返回 None。"""
    if cur is None or cur != cur:
        return None
    u = str(code6 or '').strip()
    if not u:
        return None
    _chipGapHistFor(u)
    asof = date.today().isoformat()
    with _chipDirHistLock:
        hit = _chipDirHistMemo.get(u)
        sample = list((hit[2].get(kind) or [])) if hit is not None and hit[1] == asof else []
    if not sample:
        return None
    return _chipRankPer(sample, float(cur))


def _chipGapPerOf(code6: str, gap: float) -> float | None:
    u = str(code6 or '').strip()
    asof = date.today().isoformat()
    gapKey = round(float(gap), 4)
    with _chipGapPerValLock:
        hit = _chipGapPerValMemo.get(u)
        if hit is not None and hit[0] == asof and hit[1] == gapKey:
            return hit[2]
    per = _chipGapPerLast(_chipGapHistFor(u) + [float(gap)])
    if per is None:
        return None
    with _chipGapPerValLock:
        _chipGapPerValMemo[u] = (asof, gapKey, float(per))
    return float(per)


def _packChipSpotRow(
    last: float,
    lastClose: float | None,
    openPx: float | None,
    *,
    highPx: float | None = None,
    lowPx: float | None = None,
    pct: float | None = None,
    fresh: bool,
    code6: str | None = None,
    source: str = 'qmt',
    asof_text: str = '',
) -> dict[str, Any]:
    if pct is None and lastClose is not None and lastClose > 0:
        pct = (last / lastClose - 1.0) * 100.0
    intra = _chipSpotIntraPp(last, openPx)
    gap = _chipSpotGapPp(openPx, lastClose)
    amp = _chipSpotAmpPp(highPx, lowPx, lastClose)
    pctP = _chipDirPerOf(code6, 'pct', pct) if pct is not None and code6 else None
    intraP = _chipDirPerOf(code6, 'intra', intra) if intra is not None and code6 else None
    gapP = _chipDirPerOf(code6, 'gap', gap) if gap is not None and code6 else None
    ampP = _chipDirPerOf(code6, 'amp', amp) if amp is not None and code6 else None
    return {
        'last': round(last, 3),
        'last_text': _fmtChipSpotLast(last),
        'last_close': round(lastClose, 4) if lastClose is not None else None,
        'open': round(openPx, 4) if openPx is not None else None,
        'pct': None if pct is None else round(pct, 2),
        'pct_text': _fmtChipSpotPct(pct) if pct is not None else '—',
        'pct_p': pctP,
        'pct_p_text': '—' if pctP is None else str(pctP),
        'intra': None if intra is None else round(intra, 2),
        'intra_text': _fmtChipSpotPct(intra) if intra is not None else '—',
        'intra_p': intraP,
        'intra_p_text': '—' if intraP is None else str(intraP),
        'gap': None if gap is None else round(gap, 2),
        'gap_text': _fmtChipSpotPct(gap) if gap is not None else '—',
        'gap_p': gapP,
        'gap_p_text': '—' if gapP is None else str(gapP),
        'amp': None if amp is None else round(amp, 2),
        'amp_text': _fmtChipSpotAmp(amp) if amp is not None else '—',
        'amp_p': ampP,
        'amp_p_text': '—' if ampP is None else str(ampP),
        'fresh': fresh,
        'source': source,
        'asof_text': asof_text,
    }


def _chipRtkFallbackOpen(now: datetime) -> bool:
    """盘中只信 QMT。午休冷启动、盘前、15:00 后才允许 rt_k。"""
    t = now.time()
    if dtTime(11, 30) <= t < dtTime(13, 0):
        return True
    return not (dtTime(9, 15) <= t < dtTime(15, 0))


def _chipQmtPackedUsable(packed: dict[str, Any], today: date) -> bool:
    """无时钟的快照沿用现逻辑。有日期且不是今天的 last 不能当今日现货。"""
    tickDay = decodeQuoteTickDate(
        packed.get('t') or packed.get('time') or packed.get('stime')
    )
    return tickDay is None or tickDay == today


def _chipQmtAsofText(packed: dict[str, Any]) -> str:
    tickAt = decodeQuoteTickAt(packed.get('t') or packed.get('time') or packed.get('stime'))
    if tickAt is None:
        return ''
    return tickAt.strftime('%H:%M:%S')


def packChipSpotQuotes(
    snapshot: dict[str, Any] | None,
    *,
    extraCodes: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """顶栏 chip：今日 QMT →（非连续竞价）今日 rt_k → 日线最近收盘。

    09:15–11:30 与 13:00–15:00 不叠 rt_k。日线兜底带交易日，不把昨收摆成实时价。
    Pct=(最新−昨收)/昨收；Intra=(最新−开盘)/开盘；Gap=(开盘−昨收)/昨收；Amp=(最高−最低)/昨收。
    四个旁注分位与主页 Ma/Ema 同式（严格小于、整数）。计划区 gapP 仍是 expanding，不读这里。
    """
    wall = now or datetime.now()
    today = wall.date()
    etfSnap = snapshot.get('etf') if isinstance(snapshot, dict) and isinstance(snapshot.get('etf'), dict) else {}
    codes: list[str] = []
    seen: set[str] = set()
    for c in list(_opt_t_board_default_codes()) + list(extraCodes or []):
        u = str(c or '').strip()
        if _OPT_T_BOARD_UNDERLYING_RE.match(u) and u not in seen:
            seen.add(u)
            codes.append(u)
    for qmt in etfSnap:
        u = str(qmt).split('.', 1)[0]
        if _OPT_T_BOARD_UNDERLYING_RE.match(u) and u not in seen:
            seen.add(u)
            codes.append(u)
    out: dict[str, dict[str, Any]] = {}
    for code6 in codes:
        packed = etfSnap.get(_optUnderlyingToQmtEtf(code6))
        if not isinstance(packed, dict) or not _chipQmtPackedUsable(packed, today):
            continue
        last = _optTickFloat(packed.get('last'))
        if last is None:
            last = _optTickFloat(packed.get('lastPrice'))
        lastClose = _optTickFloat(packed.get('lastClose'))
        if lastClose is None:
            lastClose = _optTickFloat(packed.get('preClose'))
        if last is None or last <= 0:
            last = lastClose if lastClose is not None and lastClose > 0 else None
        if last is None or last <= 0:
            continue
        openPx = _optTickFloat(packed.get('open'))
        out[code6] = _packChipSpotRow(
            last,
            lastClose,
            openPx,
            highPx=_optTickFloat(packed.get('high')),
            lowPx=_optTickFloat(packed.get('low')),
            fresh=not isStubDaqmtEtfTick(packed),
            code6=code6,
            source='qmt',
            asof_text=_chipQmtAsofText(packed),
        )
    missing = [c for c in codes if c not in out]
    if missing and _chipRtkFallbackOpen(wall):
        for code6, row in _chipSpotRtkFallback(missing, today).items():
            out.setdefault(code6, row)
        missing = [c for c in codes if c not in out]
    if missing:
        daily = _chipSpotDailyFallback(missing)
        for code6, row in daily.items():
            out.setdefault(code6, row)
    return out


_chipSpotRtkMemoLock = threading.Lock()
_chipSpotRtkMemo: tuple[float, str, tuple[str, ...], dict[str, dict[str, Any]]] | None = None
_CHIP_SPOT_RTK_MEMO_SEC = 30.0


def _chipRtkAsofText(tradeTime: Any) -> str:
    if isinstance(tradeTime, datetime):
        return tradeTime.strftime('%m-%d %H:%M')
    text = str(tradeTime or '').strip()
    if len(text) >= 16 and text[4] == '-':
        return text[5:16].replace('T', ' ')
    return text[:16]


def _chipSpotRtkFallback(codes: list[str], today: date) -> dict[str, dict[str, Any]]:
    """stock_wind.rt_k 当日快照。trade_time 不是今天的行不用。30s 去重。"""
    global _chipSpotRtkMemo
    uniq = tuple(dict.fromkeys(c for c in codes if _OPT_T_BOARD_UNDERLYING_RE.match(c)))
    if not uniq:
        return {}
    asofKey = today.isoformat()
    now = time.monotonic()
    with _chipSpotRtkMemoLock:
        memo = _chipSpotRtkMemo
        if (
            memo is not None
            and (now - memo[0]) < _CHIP_SPOT_RTK_MEMO_SEC
            and memo[1] == asofKey
            and memo[2] == uniq
        ):
            return {k: dict(v) for k, v in memo[3].items()}
    tsList = [_optUnderlyingToQmtEtf(c) for c in uniq]
    codeByTs = {_optUnderlyingToQmtEtf(c): c for c in uniq}
    found: dict[str, dict[str, Any]] = {}
    cfg = dict(DB_CONFIG_TUSHARE)
    cfg['database'] = os.getenv('DB_NAME_WIND', 'stock_wind')
    try:
        conn = pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor)
        try:
            with conn.cursor() as cur:
                marks = ','.join(['%s'] * len(tsList))
                cur.execute(
                    'SELECT ts_code, pre_close, `open`, close, high, low, trade_time '
                    f'FROM rt_k WHERE ts_code IN ({marks})',
                    tsList,
                )
                for row in cur.fetchall() or []:
                    ts = str(row.get('ts_code') or '')
                    code6 = codeByTs.get(ts)
                    tradeDay = _chipGapRowDate(row.get('trade_time'))
                    if not code6 or tradeDay != today:
                        continue
                    last = _optTickFloat(row.get('close'))
                    if last is None or last <= 0:
                        continue
                    found[code6] = _packChipSpotRow(
                        last,
                        _optTickFloat(row.get('pre_close')),
                        _optTickFloat(row.get('open')),
                        highPx=_optTickFloat(row.get('high')),
                        lowPx=_optTickFloat(row.get('low')),
                        fresh=False,
                        code6=code6,
                        source='rt_k',
                        asof_text=_chipRtkAsofText(row.get('trade_time')),
                    )
        finally:
            conn.close()
    except Exception:
        logger.exception('顶栏 rt_k 兜底失败')
        found = {}
    with _chipSpotRtkMemoLock:
        _chipSpotRtkMemo = (time.monotonic(), asofKey, uniq, {k: dict(v) for k, v in found.items()})
    return found


_chipSpotDailyMemoLock = threading.Lock()
_chipSpotDailyMemo: tuple[float, tuple[str, ...], dict[str, dict[str, Any]]] | None = None
_CHIP_SPOT_DAILY_MEMO_SEC = 30.0


def _chipSpotDailyFallback(codes: list[str]) -> dict[str, dict[str, Any]]:
    """DAQMT 无现货时用 etf_daily/index_daily 最近收盘，30s 去重。"""
    global _chipSpotDailyMemo
    uniq = tuple(dict.fromkeys(c for c in codes if _OPT_T_BOARD_UNDERLYING_RE.match(c)))
    if not uniq:
        return {}
    now = time.monotonic()
    with _chipSpotDailyMemoLock:
        memo = _chipSpotDailyMemo
        if (
            memo is not None
            and (now - memo[0]) < _CHIP_SPOT_DAILY_MEMO_SEC
            and memo[1] == uniq
        ):
            return {k: dict(v) for k, v in memo[2].items()}
    byTable: dict[str, list[tuple[str, str]]] = {}
    for code6 in uniq:
        table, spotTs = _opt_t_board_spot_ts_code(code6)
        byTable.setdefault(table, []).append((code6, spotTs))
    found: dict[str, dict[str, Any]] = {}
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE, cursorclass=pymysql.cursors.DictCursor)
        try:
            with conn.cursor() as cur:
                for table, pairs in byTable.items():
                    tsList = [p[1] for p in pairs]
                    tsToCode = {p[1]: p[0] for p in pairs}
                    placeholders = ','.join(['%s'] * len(tsList))
                    cur.execute(
                        f"""
                        SELECT t.ts_code, t.trade_date, t.close, t.pre_close, t.pct_chg, t.open, t.high, t.low
                        FROM {table} t
                        INNER JOIN (
                            SELECT ts_code, MAX(trade_date) AS d
                            FROM {table}
                            WHERE ts_code IN ({placeholders})
                            GROUP BY ts_code
                        ) x ON t.ts_code = x.ts_code AND t.trade_date = x.d
                        """,
                        tsList,
                    )
                    for row in cur.fetchall() or []:
                        ts = str(row.get('ts_code') or '')
                        code6 = tsToCode.get(ts)
                        if not code6:
                            continue
                        last = _optTickFloat(row.get('close'))
                        lastClose = _optTickFloat(row.get('pre_close'))
                        pct = _optTickFloat(row.get('pct_chg'))
                        openPx = _optTickFloat(row.get('open'))
                        if last is None or last <= 0:
                            continue
                        tradeDay = _chipGapRowDate(row.get('trade_date'))
                        found[code6] = _packChipSpotRow(
                            last,
                            lastClose,
                            openPx,
                            highPx=_optTickFloat(row.get('high')),
                            lowPx=_optTickFloat(row.get('low')),
                            pct=pct,
                            fresh=False,
                            code6=code6,
                            source='daily',
                            asof_text=tradeDay.strftime('%m-%d') if tradeDay else '',
                        )
        finally:
            conn.close()
    except Exception:
        found = {}
    with _chipSpotDailyMemoLock:
        _chipSpotDailyMemo = (time.monotonic(), uniq, {k: dict(v) for k, v in found.items()})
    return found


_trendSma120MemoLock = threading.Lock()
_trendSma120Memo: tuple[float, tuple[str, ...], dict[str, dict[str, Any]]] | None = None
_TREND_SMA120_MEMO_SEC = 4.0


def queryTrendSma120ByCodes(
    codes: list[str] | None = None,
    *,
    snapshot: dict[str, Any] | None = None,
    autoFetchSnap: bool = True,
) -> dict[str, dict[str, Any]]:
    """
    多标的 SMA120 状态（默认五只 ETF + 调用方追加）。
    现货优先 QMT；历史收盘 etf_daily / index_daily。短 TTL 去重，供 chip 并排展示。
    """
    global _trendSma120Memo
    raw = [str(c or '').strip() for c in (codes or _opt_t_board_default_codes())]
    uniq = tuple(dict.fromkeys(c for c in raw if _OPT_T_BOARD_UNDERLYING_RE.match(c)))
    if not uniq:
        return {}
    snap = snapshot
    if snap is None and autoFetchSnap:
        snap = fetchDaqmtQuoteSnapshot()
    now = time.monotonic()
    with _trendSma120MemoLock:
        memo = _trendSma120Memo
        if (
            memo is not None
            and (now - memo[0]) < _TREND_SMA120_MEMO_SEC
            and memo[1] == uniq
        ):
            return {k: dict(v) for k, v in memo[2].items()}
    out: dict[str, dict[str, Any]] = {}
    asofToday = date.today()
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE, cursorclass=pymysql.cursors.DictCursor)
        try:
            with conn.cursor() as cur:
                for code6 in uniq:
                    table, spotTs = _opt_t_board_spot_ts_code(code6)
                    cur.execute(
                        f"""
                        SELECT trade_date, close
                        FROM {table}
                        WHERE ts_code = %s
                          AND close IS NOT NULL
                        ORDER BY trade_date DESC
                        LIMIT 130
                        """,
                        (spotTs,),
                    )
                    histRows = list(cur.fetchall() or [])
                    histRows.reverse()
                    closes: list[float] = []
                    smaAsof: date | None = None
                    for row in histRows:
                        try:
                            c = float(row.get('close'))
                        except (TypeError, ValueError):
                            continue
                        if c != c or c <= 0:
                            continue
                        closes.append(c)
                        smaAsof = _opt_t_board_parse_date(row.get('trade_date')) or smaAsof
                    liveSpot = _liveSpotFromDaqmtSnap(code6, snap, asof=asofToday)
                    out[code6] = buildTrendSma120(
                        closes,
                        spot=liveSpot,
                        asof=smaAsof,
                        source=table,
                    )
        finally:
            conn.close()
    except Exception as e:
        logger.exception('SMA120 多标的查询失败: %s', e)
        for code6 in uniq:
            out.setdefault(
                code6,
                buildTrendSma120([], spot=None, asof=None, source='etf_daily'),
            )
    with _trendSma120MemoLock:
        _trendSma120Memo = (time.monotonic(), uniq, {k: dict(v) for k, v in out.items()})
    return out


def _opt_t_board_expiry_kind_map(
    md_values: list[str],
    *,
    opt_underlying: str = '',
) -> dict[str, str]:
    """
    按日历规则标期限桶（非简单取前 N）。
    ETF：near/next/quarter/next_quarter；
    中金所股指期权：另加 next_next / next_next_quarter。
    """
    parsed: list[tuple[date, str]] = []
    today = date.today()
    for md_s in md_values:
        d = _opt_t_board_parse_date(md_s)
        if d is None or d < today:
            continue
        parsed.append((d, md_s))
    if not parsed:
        return {}
    from option_iv import calendarTenorMapForMaturities

    isCffex = _isCffexIndexOpt(str(opt_underlying or '').strip())
    tenorToKind = {
        'current_month': 'near',
        'next_month': 'next',
        'next_next_month': 'next_next',
        'current_quarter': 'quarter',
        'next_quarter': 'next_quarter',
        'next_next_quarter': 'next_next_quarter',
    }
    tenorMap = calendarTenorMapForMaturities(
        [d for d, _ in parsed],
        isCffex=isCffex,
    )
    tenorByDate = {
        (k.date() if hasattr(k, 'date') else k): v for k, v in tenorMap.items()
    }
    out: dict[str, str] = {}
    for d, md_s in parsed:
        tenor = tenorByDate.get(d)
        kind = tenorToKind.get(tenor or '', '')
        if kind:
            out[md_s] = kind
    return out


def _opt_t_board_spread_width(opt_underlying: str, direction: str, expiry_kind: str) -> int:
    """铁律固定 1 档；保留函数供兼容。"""
    return 1


def _opt_t_board_valid_quote(q: dict[str, Any] | None) -> bool:
    """有可执行卖价或买价即可参与价差枚举。"""
    if _optQuoteSellPx(q) is not None or _optQuoteBuyPx(q) is not None:
        return True
    return False


_opt_t_board_stats_lookup: dict[str, dict[str, Any]] | None = None
_opt_t_board_stats_mtime: float = 0.0


def _opt_t_board_stats_lookup_cached() -> dict[str, dict[str, Any]]:
    """懒加载回测统计表；stats 文件更新后自动重载。"""
    global _opt_t_board_stats_lookup, _opt_t_board_stats_mtime
    try:
        from opt_spread_board_select import STATS_PATH, loadBoardStats

        mtime = STATS_PATH.stat().st_mtime if STATS_PATH.is_file() else 0.0
        if _opt_t_board_stats_lookup is None or mtime != _opt_t_board_stats_mtime:
            _opt_t_board_stats_lookup = loadBoardStats()
            _opt_t_board_stats_mtime = mtime
    except Exception as e:
        logger.warning('opt-t-board 回测统计表加载失败，退化为仅成本甜点: %s', e)
        _opt_t_board_stats_lookup = {}
    return _opt_t_board_stats_lookup or {}


def _opt_t_board_pick_spread_marks(
    legs: dict[float, dict],
    quote_map: dict[str, dict[str, Any]],
    spot: float | None,
    opt_underlying: str,
    direction: str,
    expiry_kind: str,
) -> dict[str, Any] | None:
    """枚举候选价差；ETF 用回测统计打分，中金所用空 lookup→仅成本甜点启发式。"""
    if spot is None or not legs:
        return None
    try:
        from opt_spread_board_select import getSymbolProfile, pickSpreadByBacktestScore
    except ImportError as e:
        logger.warning('opt_spread_board_select 不可用: %s', e)
        return None
    prof = getSymbolProfile(opt_underlying)
    useStats = bool(prof.get('useBacktestStats', True))
    lookup = _opt_t_board_stats_lookup_cached() if useStats else {}
    return pickSpreadByBacktestScore(
        legs,
        quote_map,
        spot,
        opt_underlying,
        direction,
        expiry_kind,
        _opt_daily_close_float,
        _opt_t_board_valid_quote,
        lookup,
        buyFn=_optQuoteBuyPx,
        sellFn=_optQuoteSellPx,
    )


def _opt_t_board_spread_title(
    mark: dict[str, Any],
    role: str,
    expiry_kind: str,
    opt_underlying: str,
) -> str:
    side = '买腿' if role == 'buy' else '卖腿'
    ts_key = 'buy_ts' if role == 'buy' else 'sell_ts'
    ek_cn = _opt_t_board_expiry_kind_cn(expiry_kind)
    prof = _opt_t_board_symbol_profile(opt_underlying)
    tp_mult = float(mark.get('tp_mult') or prof.get('tpMult') or 2.0)
    parts = [
        f"{ek_cn} {side} {mark.get(ts_key, '')}",
        f"净成本={mark.get('net', 0):.4f}",
        f"成本占比={mark.get('cost_rel', 0):.2f}",
    ]
    sweet_lo = prof.get('sweetMin')
    sweet_hi = prof.get('sweetMax')
    if sweet_lo is not None and sweet_hi is not None:
        parts.append(f"成本甜点={sweet_lo:.2f}~{sweet_hi:.2f}")
    bucket = mark.get('cost_bucket')
    if bucket:
        parts.append(f"成本桶={bucket}")
    wr = mark.get('win_rate')
    pf = mark.get('profit_factor')
    if wr is not None and pf is not None:
        parts.append(f"回测胜率={wr * 100:.1f}%")
        parts.append(f"盈亏比={pf:.2f}")
        stat_n = mark.get('stat_n')
        if stat_n:
            parts.append(f"样本n={stat_n}")
    parts.append(f"宽度={mark.get('width', '')}档")
    parts.append(f"建议止盈={tp_mult:g}×净成本")
    score = mark.get('score')
    if score is not None:
        parts.append(f"综合分={score:.3f}")
    if prof.get('caution'):
        parts.append(str(prof['caution']))
    return '；'.join(parts)


def _opt_t_board_codes_from_env(value: str | None) -> list[str]:
    """从逗号/分号分隔环境变量解析 6 位标的代码列表，去重保序。"""
    if not value or not str(value).strip():
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in str(value).replace(';', ',').split(','):
        s = part.strip()
        if not _OPT_T_BOARD_UNDERLYING_RE.match(s):
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


# 五只 ETF 期权 chip 固定序：科创→创业→上证50→沪深300→中证500
_OPT_T_BOARD_ETF_CODES = ('588000', '159915', '510050', '510300', '510500')


def _opt_t_board_default_codes() -> list[str]:
    codes = _opt_t_board_codes_from_env(os.getenv('OPT_T_BOARD_DEFAULT_CODES'))
    return codes if codes else list(_OPT_T_BOARD_ETF_CODES)


def _opt_t_board_allow_codes_set() -> frozenset[str] | None:
    """配置了 OPT_T_BOARD_ALLOW_CODES 时仅允许这些标的；未配置则仅校验六位数字格式。"""
    codes = _opt_t_board_codes_from_env(os.getenv('OPT_T_BOARD_ALLOW_CODES'))
    return frozenset(codes) if codes else None


def _opt_t_board_check_underlying(u: str) -> str | None:
    """合法返回 None；否则 'format' / 'allow'。"""
    if not _OPT_T_BOARD_UNDERLYING_RE.match(u):
        return 'format'
    allow = _opt_t_board_allow_codes_set()
    if allow is not None and u not in allow:
        return 'allow'
    return None


def _opt_t_board_resolve_for_page(u_raw: str | None) -> tuple[str, str | None]:
    defaults = _opt_t_board_default_codes()
    d0 = defaults[0]
    if u_raw is None or str(u_raw).strip() == '':
        return d0, None
    u = str(u_raw).strip()
    err = _opt_t_board_check_underlying(u)
    if err == 'format':
        return d0, '参数 u 须为6位数字，已使用默认标的。'
    if err == 'allow':
        return d0, '参数 u 不在 OPT_T_BOARD_ALLOW_CODES 白名单，已使用默认标的。'
    return u, None


def _opt_t_board_resolve_for_api(u_raw: str | None) -> tuple[str | None, str | None]:
    """缺省 u 时用默认第一个；显式非法则 (None, err)。"""
    defaults = _opt_t_board_default_codes()
    d0 = defaults[0]
    if u_raw is None or str(u_raw).strip() == '':
        return d0, None
    u = str(u_raw).strip()
    err = _opt_t_board_check_underlying(u)
    if err == 'format':
        return None, '标的须为6位数字'
    if err == 'allow':
        return None, '标的未在允许列表中'
    return u, None


def optTBoardHtmlEtag(html: str) -> str:
    return hashlib.sha1((html or '').encode('utf-8')).hexdigest()[:16]


def optTBoardNotModified(ifNoneMatch: str | None, etag: str | None) -> bool:
    got = str(ifNoneMatch or '').strip().strip('"')
    want = str(etag or '').strip().strip('"')
    return bool(got and want and got == want)


def _opt_t_board_render_body_fragment(payload: dict[str, Any]) -> str:
    """与 rt_monitor_opt_t_board_body.html 一致的表体 HTML。同 payload 复用，避免 3s 轮询反复 Jinja。"""
    frag = payload.get('_htmlFrag')
    if isinstance(frag, str) and frag:
        return frag
    html = render_template(
        'rt_monitor_opt_t_board_body.html',
        opt_t_sections=payload['sections'],
        opt_t_error=payload['error'],
        opt_t_row_count=int(payload.get('row_count') or 0),
        opt_t_obligation_selector=payload.get('obligation_selector'),
        opt_t_rights_selector=payload.get('rights_selector'),
        opt_t_term_gate=payload.get('term_gate'),
    )
    payload['_htmlFrag'] = html
    payload['_htmlEtag'] = optTBoardHtmlEtag(html)
    return html


_SNAP_UNSET = object()


def _optTBoardRowIsDisplay(r: dict[str, Any], perUnit: float) -> bool:
    """在市 C/P 全部上表，含名称尾 A、乘数≠标准的调整合约。"""
    del perUnit
    cp = str(r.get('call_put') or '').strip().upper()
    if cp:
        return cp in ('C', 'P')
    return True


def _optTBoardParseUnit(row: dict[str, Any] | None) -> float:
    raw = (row or {}).get('per_unit')
    if raw is None or raw == '':
        return 0.0
    try:
        return float(str(raw).replace(',', '').strip())
    except (TypeError, ValueError):
        return 0.0


def _optTBoardRowIsAdjName(row: dict[str, Any] | None) -> bool:
    return str((row or {}).get('name') or '').strip()[-1:] == 'A'


def _optTBoardDisplayKey(row: dict[str, Any]) -> tuple[float, float, int] | None:
    """同一 T 行 = 同行使价 + 同乘数 + 是否 A。标准与调整合约分行，避免量仓被盖掉。"""
    stk = _opt_t_board_norm_strike(row.get('exercise_price'))
    if stk is None:
        return None
    isA = 1 if _optTBoardRowIsAdjName(row) else 0
    return (stk, _optTBoardParseUnit(row), isA)


def _optTBoardSortDisplayKeys(keys: list[tuple[float, float, int]]) -> list[tuple[float, float, int]]:
    """先全部标准合约（非 A）行权价低→高，再全部 A 类低→高。"""
    return sorted(keys, key=lambda k: (k[2], k[0], k[1]))


def _optTBoardNextSameClassIdx(
    keys: list[tuple[float, float, int]],
    idx: int,
    step: int,
) -> int | None:
    """价差只在同一乘数、同一 A/非 A 序列上取邻档。"""
    if idx < 0 or idx >= len(keys):
        return None
    cur = keys[idx]
    j = idx + int(step)
    while 0 <= j < len(keys):
        other = keys[j]
        if other[1] == cur[1] and other[2] == cur[2]:
            return j
        j += int(step)
    return None


def _optTBoardFillDisplayBuckets(
    rows: list[dict],
    quoteMap: dict[str, dict[str, Any]] | None = None,
) -> dict[tuple[str, str], dict[str, dict]]:
    groups: dict[tuple[str, str], dict[str, dict]] = defaultdict(
        lambda: {'calls': {}, 'puts': {}}
    )
    for r in rows:
        key = _optTBoardDisplayKey(r)
        if key is None:
            continue
        sm = str(r.get('s_month') or '').strip()
        mdVal = r.get('maturity_date')
        mdS = mdVal.isoformat() if hasattr(mdVal, 'isoformat') else str(mdVal or '').strip()
        cp = str(r.get('call_put') or '').strip().upper()
        if cp not in ('C', 'P'):
            continue
        side = 'calls' if cp == 'C' else 'puts'
        bucket = groups[(sm, mdS)][side]
        old = bucket.get(key)
        bucket[key] = _optTBoardPickIvRow(old, r, quoteMap)
    return groups


def _optTBoardQuoteWeight(quoteMap: dict[str, dict[str, Any]] | None, row: dict) -> float:
    q = (quoteMap or {}).get(str(row.get('ts_code') or '').strip()) or {}
    vol = _opt_daily_vol_oi_float(q, 'vol') or 0.0
    oi = _opt_daily_vol_oi_float(q, 'oi') or 0.0
    return vol + oi


def _optTBoardPickIvRow(
    old: dict | None,
    new: dict,
    quoteMap: dict[str, dict[str, Any]] | None,
) -> dict:
    if old is None:
        return new
    wo = _optTBoardQuoteWeight(quoteMap, old)
    wn = _optTBoardQuoteWeight(quoteMap, new)
    if wn > wo:
        return new
    if wo > wn:
        return old
    oldA = str(old.get('name') or '').strip()[-1:] == 'A'
    newA = str(new.get('name') or '').strip()[-1:] == 'A'
    if oldA and not newA:
        return new
    return old


def _optTBoardFillBuckets(
    rows: list[dict],
    quoteMap: dict[str, dict[str, Any]] | None = None,
    *,
    preferLiquid: bool = False,
) -> dict[tuple[str, str], dict[str, dict]]:
    groups: dict[tuple[str, str], dict[str, dict]] = defaultdict(
        lambda: {'calls': {}, 'puts': {}}
    )
    for r in rows:
        sm = str(r.get('s_month') or '').strip()
        mdVal = r.get('maturity_date')
        mdS = mdVal.isoformat() if hasattr(mdVal, 'isoformat') else str(mdVal or '').strip()
        cp = str(r.get('call_put') or '').strip().upper()
        stk = _opt_t_board_norm_strike(r.get('exercise_price'))
        if stk is None or cp not in ('C', 'P'):
            continue
        side = 'calls' if cp == 'C' else 'puts'
        bucket = groups[(sm, mdS)][side]
        old = bucket.get(stk)
        bucket[stk] = (
            _optTBoardPickIvRow(old, r, quoteMap) if preferLiquid else r
        )
    return groups


_OPT_T_AGE_YOUNG_SEC = 30.0
_OPT_T_AGE_OLD_SEC = 300.0
# 阈值标定依据（2026-09-11 实测五链 422 张，见 scripts/tune_age_thresholds.py）：
#   系统投递节奏：中位 8.5s、P75 23.0s（订阅推送 hit≈375/422，约 10s 一轮）
#   10/60  → 正常仅 50.5%（一半行被涂灰，用户反馈「灰太多」）
#   15/120 → 62.6%
#   30/300 → 正常 91.0%、淡灰 19 张、深灰 19 张  ← 采用
# 关键教训：阈值必须**明显大于系统正常投递周期**，否则健康状态下也满屏告警。
# 调紧会放大噪声，调松会掩盖真故障（>300s 基本只会是源冻结/无成交）。
# 灰阶陈旧度只在**连续竞价时段**生效。
# 午休(11:30~13:00)/盘前/盘后行情本就冻结，此时按阈值着色会把整屏刷成灰，
# 属于噪声而非信息（2026-09-11 用户反馈）。
_OPT_T_AGE_WINDOWS = (
    (dtTime(9, 15), dtTime(11, 30)),
    (dtTime(13, 0), dtTime(15, 0)),
)
# 灰阶陈旧度：仅用灰阶，不引入新色相（表格红绿已用于涨跌/换手）。
_OPT_T_AGE_CLS = ('', 'opt-t-age-stale', 'opt-t-age-ancient')


def optTAgeColoringActive(now: datetime | None = None) -> bool:
    """当前是否处于连续竞价时段（决定是否给「最新/量/仓」加灰阶陈旧度）。"""
    wall = now or datetime.now()
    t = wall.time()
    return any(start <= t < end for start, end in _OPT_T_AGE_WINDOWS)


def _optTAgeClass(tsMs: float, nowMs: float) -> str:
    """按字段自身时间戳返回灰阶陈旧度 class（正常/淡灰>10s/深灰>60s）。"""
    try:
        t = float(tsMs)
    except (TypeError, ValueError):
        return ''
    if t <= 0:
        # 无字段时钟：无法证明新鲜，按陈旧处理（fail-closed 的展示侧）
        return _OPT_T_AGE_CLS[2]
    age = (float(nowMs) - t) / 1000.0
    if age > _OPT_T_AGE_OLD_SEC:
        return _OPT_T_AGE_CLS[2]
    if age > _OPT_T_AGE_YOUNG_SEC:
        return _OPT_T_AGE_CLS[1]
    return _OPT_T_AGE_CLS[0]


def _optTQuoteFieldTimes(
    rec: dict[str, Any] | None, nowMs: float, *, forceFresh: bool = False
) -> dict[str, str]:
    """一行合约的字段级灰阶 class：最新/成交量/持仓量 各自计时。

    forceFresh=True（非竞价时段）→ 全部返回空 class（正常色），
    因为午休/盘前/盘后行情本就不更新，着色没有信息量。
    """
    if forceFresh:
        return {'last': '', 'vol': '', 'oi': '', 'book': ''}
    packed = rec if isinstance(rec, dict) else {}
    common = packed.get('time') or packed.get('t') or packed.get('stime')

    def pick(keys: tuple) -> float:
        hasExplicit = any(k in packed for k in keys)
        for k in keys:
            v = packed.get(k)
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 1e12:
                return f
            if f > 1e9:
                return f * 1000.0
        if hasExplicit:
            return 0.0
        try:
            f = float(common)
        except (TypeError, ValueError):
            return 0.0
        if f > 1e12:
            return f
        if f > 1e9:
            return f * 1000.0
        return 0.0

    return {
        'last': _optTAgeClass(pick(_OPT_TICK_LAST_TIME_KEYS), nowMs),
        'vol': _optTAgeClass(pick(_OPT_TICK_QTY_TIME_KEYS), nowMs),
        'oi': _optTAgeClass(pick(_OPT_TICK_OI_TIME_KEYS), nowMs),
        'book': _optTAgeClass(pick(_OPT_TICK_BOOK_TIME_KEYS), nowMs),
    }


def _optTFieldClockMs(
    packed: dict[str, Any] | None,
    keys: tuple[str, ...],
    *,
    fallbackCommon: bool = True,
) -> float:
    """字段时钟归一到毫秒；显式无效键不能回退到 common time。"""
    rec = packed if isinstance(packed, dict) else {}
    hasExplicit = any(key in rec for key in keys)
    for key in keys:
        try:
            value = float(rec.get(key))
        except (TypeError, ValueError):
            continue
        if value > 1e12:
            return value
        if value > 1e9:
            return value * 1000.0
    if hasExplicit or not fallbackCommon:
        return 0.0
    for key in _OPT_TICK_COMMON_TIME_KEYS:
        try:
            value = float(rec.get(key))
        except (TypeError, ValueError):
            continue
        if value > 1e12:
            return value
        if value > 1e9:
            return value * 1000.0
    return 0.0


def _optTDeltaIso(ms: float) -> str | None:
    if ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0).isoformat(timespec='seconds')
    except (OSError, OverflowError, ValueError):
        return None


def _optTDeltaSource(packed: dict[str, Any], field: str) -> str | None:
    key = {
        'last': 'lastSource',
        'vol': 'qtySource',
        'oi': 'oiSource',
        'book': 'bookSource',
    }.get(field, '')
    source = str(packed.get(key) or '').strip().lower()
    if source in {'qmt', 'tdx'}:
        return source
    if field == 'book' and packed.get('fromTdx'):
        return 'tdx'
    # 旧快照或第三方载荷没给字段级来源时宁可披露未知，不能臆测为 QMT。
    return None


def _optTDeltaField(
    *,
    text: str,
    value: float | None,
    packed: dict[str, Any],
    observedKeys: tuple[str, ...],
    changedKeys: tuple[str, ...],
    fresh: bool,
    sourceField: str,
    sourceKnown: bool,
    nowMs: float,
    ageActive: bool,
) -> dict[str, Any]:
    observedMs = _optTFieldClockMs(packed, observedKeys)
    changedMs = _optTFieldClockMs(
        packed, changedKeys, fallbackCommon=False
    )
    return {
        'text': text,
        'value': value,
        'replace': bool(fresh),
        'fresh': bool(fresh),
        # 空字段不能借用整张快照的来源。尤其 TDX 快照没有可靠 OI，
        # 将空 OI 标为 TDX 会让页面误以为「TDX 给过持仓量」。
        'source': _optTDeltaSource(packed, sourceField) if sourceKnown else None,
        'observed_ms': observedMs or None,
        'observed_at': _optTDeltaIso(observedMs),
        # 缺失就明确 unknown，禁止拿观测时间冒充最后变动。
        'changed_ms': changedMs or None,
        'changed_at': _optTDeltaIso(changedMs),
        'age_class': '' if not ageActive else _optTAgeClass(observedMs, nowMs),
    }


def queryOptTBoardQuoteDelta(
    optUnderlying: str,
    *,
    snapshot: Any = _SNAP_UNSET,
    now: datetime | None = None,
) -> dict[str, Any]:
    """只读 DAQMT 快照的轻量行情增量；禁止 SQL、IV、Γ/Δ、HTML 重算。

    T 表期权 Δ/Γ 与 T 表 IV 走整表重算（约 5s），与本接口无关。
    右侧 IV 看板的「溢」是中轴 IV−HV，不是期权 Delta。

    `*Time` 表示该字段最后可信观测；`*ChangedTime` 仅表示值最后改变。
    当源端时钟跨日或缺失时，保留数字但 `replace=false`，前端不会把旧值涂成实时。
    """
    u = str(optUnderlying or '').strip()
    wall = now or datetime.now()
    snap = fetchDaqmtQuoteSnapshot() if snapshot is _SNAP_UNSET else snapshot
    observedAt = None
    if isinstance(snap, dict):
        rawWall = snap.get('optWall')
        observedAt = str(rawWall).strip() if rawWall else None
    if not observedAt:
        observedAt = wall.isoformat(timespec='seconds')
    base = {
        'ok': True,
        'u': u,
        'underlying': u,
        'ready': bool(isinstance(snap, dict) and snap.get('ok')),
        'observed_at': observedAt,
        'asof_ms': wall.timestamp() * 1000.0,
        'poll_ms': 1000,
        'quotes': {},
        'spots': packChipSpotQuotes(snap if isinstance(snap, dict) else None),
    }
    if not isinstance(snap, dict) or _isCffexIndexOpt(u):
        return base
    optSnap = snap.get('opt') if isinstance(snap.get('opt'), dict) else {}
    metaMap = snap.get('optMeta') if isinstance(snap.get('optMeta'), dict) else {}
    asof = wall.date()
    nowMs = wall.timestamp() * 1000.0
    ageActive = optTAgeColoringActive(wall)
    for qmtCode, packed in optSnap.items():
        if not isinstance(packed, dict):
            continue
        tsCode = qmtOptToTsCode(str(qmtCode))
        meta = metaMap.get(qmtCode) or metaMap.get(str(qmtCode).upper())
        metaUndl = meta.get('undl') if isinstance(meta, dict) else None
        if not tsCode or not _optMetaUndlMatches(metaUndl, u):
            continue
        rawBid1 = _optQuotePxPositive(packed.get('bid1'))
        if rawBid1 is None and isinstance(packed.get('bidPrice'), (list, tuple)) and packed.get('bidPrice'):
            rawBid1 = _optQuotePxPositive(packed['bidPrice'][0])
        rawAsk1 = _optQuotePxPositive(packed.get('ask1'))
        if rawAsk1 is None and isinstance(packed.get('askPrice'), (list, tuple)) and packed.get('askPrice'):
            rawAsk1 = _optQuotePxPositive(packed['askPrice'][0])
        last = _optTickFloat(packed.get('last'))
        if last is None:
            last = _optTickFloat(packed.get('lastPrice'))
        vol = _optTickFloat(packed.get('vol'))
        if vol is None:
            vol = _optTickFloat(packed.get('volume'))
        oi = _optTickFloat(packed.get('oi'))
        if oi is None:
            oi = _optTickFloat(packed.get('openInt'))
        if oi is None:
            oi = _optTickFloat(packed.get('openInterest'))
        lastFresh = bool(
            last is not None
            and last > 0
            and isFreshDaqmtOptTick(packed, asof)
            and not _optLastScaleAbsurd(last, rawBid1, rawAsk1)
        )
        volFresh = bool(
            vol is not None
            and vol >= 0
            and _optTickFieldIsAsof(packed, asof, _OPT_TICK_QTY_TIME_KEYS)
        )
        oiFresh = bool(
            oi is not None
            and oi >= 0
            and _optTickFieldIsAsof(packed, asof, _OPT_TICK_OI_TIME_KEYS)
        )
        bookFresh = bool(
            (rawBid1 is not None or rawAsk1 is not None)
            and _optTickFieldIsAsof(packed, asof, _OPT_TICK_BOOK_TIME_KEYS)
        )
        book = _optTDeltaField(
            text='',
            value=None,
            packed=packed,
            observedKeys=_OPT_TICK_BOOK_TIME_KEYS,
            changedKeys=_OPT_TICK_BOOK_CHANGED_TIME_KEYS,
            fresh=bookFresh,
            sourceField='book',
            sourceKnown=(rawBid1 is not None or rawAsk1 is not None),
            nowMs=nowMs,
            ageActive=ageActive,
        )
        book['bid1'] = _fmt_opt_last_px(rawBid1) if rawBid1 is not None else '—'
        book['ask1'] = _fmt_opt_last_px(rawAsk1) if rawAsk1 is not None else '—'
        base['quotes'][tsCode] = {
            'last': _optTDeltaField(
                text=_fmt_opt_last_px(last) if last is not None else '—',
                value=last,
                packed=packed,
                observedKeys=_OPT_TICK_LAST_TIME_KEYS,
                changedKeys=_OPT_TICK_LAST_CHANGED_TIME_KEYS,
                fresh=lastFresh,
                sourceField='last',
                sourceKnown=last is not None,
                nowMs=nowMs,
                ageActive=ageActive,
            ),
            'vol': _optTDeltaField(
                text=_fmt_opt_daily_qty(vol),
                value=vol,
                packed=packed,
                observedKeys=_OPT_TICK_QTY_TIME_KEYS,
                changedKeys=_OPT_TICK_QTY_CHANGED_TIME_KEYS,
                fresh=volFresh,
                sourceField='vol',
                sourceKnown=vol is not None,
                nowMs=nowMs,
                ageActive=ageActive,
            ),
            'oi': _optTDeltaField(
                text=_fmt_opt_daily_qty(oi),
                value=oi,
                packed=packed,
                observedKeys=_OPT_TICK_OI_TIME_KEYS,
                changedKeys=_OPT_TICK_OI_CHANGED_TIME_KEYS,
                fresh=oiFresh,
                sourceField='oi',
                sourceKnown=oi is not None,
                nowMs=nowMs,
                ageActive=ageActive,
            ),
            'book': book,
        }
    return base


def _query_opt_t_board_payload(opt_underlying: str, snapshot: Any = _SNAP_UNSET) -> dict:
    """
    从 stock_tushare.opt_basic 组装 T 型报价：标的 opt_code 含指定 6 位片段、delist_date 非空且 >= 今日
    （含到期/摘牌当日，当日仍交易；> 会把当月整段打成无行情空行）、仅 C/P。
    标准合约与调整合约（名称尾 A、乘数≠10000/100）分行展示；同行使价+同乘数+同 A 标记的购沽对齐。
    按 (s_month, maturity_date) 分组，组内先标准后调整、行权价由低到高。
    列序：认购 oi/vol/收盘/权利金%/IV/Per/Γ/Δ/时年化/时值/有效价/折溢%/价差
    | 行权价/虚实% | 认沽 价差/有效价/折溢%/时值/时年化/Δ/Γ/Per/IV/权利金%/收盘/vol/oi。
    行情 close/vol/oi 来自 QMT：当日有成交用最新价与今日量仓，否则最新=昨收、成交=0。
    QMT 链上已有、opt_basic 尚未入库的新挂牌/新行权价，用快照 optMeta 补行。
    段内隐含远期 F 用全部在市 C/P（含 A、任意乘数，按量仓取代表）。
    ETF 现货只要快照当日新鲜就叠，不依赖期权 liveN。灰字合约的 IV/权利金%仍用日线现货，避免昨收权利金配今日现货。
    段头仅展示行情日与到期日/剩余天数，不展示策略角色标签。
    权利金%分边：Call=C/(S−C)，Put=C/(K−C)；
    时年化=(时值/K)×365/DTE（时值≤0 为 —；DTE 相对行情日）；
    IV 用段内平价隐含远期 + 中债10Y（yc_cb，失败回退 1.5%）；
    Delta=Black-76 现货Δ；Gamma=现货 Γ（标的每涨 1 元，Δ 的变动，两位小数，Call/Put 同值）；
    IVR/分位相对同期限 opt_iv_panel 代表IV近252日（无年化权利金%列）。
    认购价差（低行权价行）：该行认购 close − 下一档同类认购 close，×该行乘数；最高档为 —。
    认沽价差（高行权价行）：该行认沽 close − 上一档同类认沽 close，×该行乘数；最低档为 —。
    每个分组内认购/认沽的成交量、持仓量前三、平值行：标准与 A 类各自独立标记，互不混排。
    """
    err: str | None = None
    raw_rows: list[dict] = []
    iv_rows: list[dict] = []
    listedTs: set[str] = set()
    quote_map: dict[str, dict[str, Any]] = {}
    underlying_close: float | None = None
    perUnit = _optTBoardPerUnit(opt_underlying)
    spreadMult = _optTBoardSpreadMult(opt_underlying)
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE, cursorclass=pymysql.cursors.DictCursor)
        try:
            with conn.cursor() as cur:
                underlying_close = _opt_t_board_fetch_underlying_close(cur, opt_underlying)
                cur.execute(
                    """
                    SELECT ts_code, call_put, exercise_price, s_month, maturity_date,
                           name, per_unit
                    FROM opt_basic
                    WHERE opt_code LIKE %s
                      AND delist_date IS NOT NULL AND delist_date >= CURDATE()
                      AND UPPER(TRIM(COALESCE(call_put, ''))) IN ('C', 'P')
                    ORDER BY s_month, maturity_date, exercise_price ASC
                    """,
                    (f'%{opt_underlying}%',),
                )
                allCp = list(cur.fetchall() or [])
                raw_rows = [r for r in allCp if _optTBoardRowIsDisplay(r, perUnit)]
                iv_rows = list(allCp)
                ts_list = sorted({str(r['ts_code']).strip() for r in iv_rows if r.get('ts_code')})
                quote_map = _fetch_opt_daily_latest_map(cur, ts_list)
                cur.execute(
                    """
                    SELECT ts_code FROM opt_basic
                    WHERE opt_code LIKE %s
                      AND delist_date IS NOT NULL AND delist_date >= CURDATE()
                    """,
                    (f'%{opt_underlying}%',),
                )
                listedTs = {
                    str(r['ts_code']).strip()
                    for r in (cur.fetchall() or [])
                    if r.get('ts_code')
                }
        finally:
            conn.close()
    except Exception as e:
        logger.exception('期权T型报价查询 opt_basic/opt_daily 失败: %s', e)
        err = str(e)

    dailySpot = underlying_close
    snap = fetchDaqmtQuoteSnapshot() if snapshot is _SNAP_UNSET else snapshot
    raw_rows, gapN = mergeQmtChainGaps(
        raw_rows, listedTs, snap, opt_underlying, perUnit
    )
    if gapN:
        logger.info('T型补 QMT 缺口 u=%s n=%s', opt_underlying, gapN)
        haveIv = {str(r.get('ts_code') or '').strip() for r in iv_rows}
        for r in raw_rows:
            tc = str(r.get('ts_code') or '').strip()
            if tc and tc not in haveIv:
                iv_rows.append(r)
                haveIv.add(tc)
    quote_map, underlying_close, liveN, quoteSource = overlayDaqmtQuotes(
        quote_map,
        underlying_close,
        opt_underlying,
        snap,
        asof=date.today(),
    )
    displaySpot = underlying_close
    liveClock = _optTBoardLiveClockLabel(
        quoteSource,
        snap if isinstance(snap, dict) else None,
        opt_underlying,
        asof=date.today(),
    )

    # 字段级灰阶陈旧度来源：overlay 后的 quote_map 只带状态位、不带时间戳，
    # 因此直接从快照按 ts_code 建「字段年龄 class」映射（最新/量/仓/盘口各自计时）。
    # 仅在连续竞价时段启用；午休/盘前/盘后行情本就冻结，着色是噪声。
    ageNowMs = datetime.now().timestamp() * 1000.0
    ageActive = optTAgeColoringActive()
    fieldAgeByTs: dict[str, dict[str, str]] = {}
    bookAgeSecByTs: dict[str, float | None] = {}
    if ageActive and isinstance(snap, dict):
        snapOpt = snap.get('opt') if isinstance(snap.get('opt'), dict) else {}
        for qmtCode, packed in snapOpt.items():
            if not isinstance(packed, dict):
                continue
            tsCode = qmtOptToTsCode(str(qmtCode))
            if not tsCode:
                continue
            fieldAgeByTs[tsCode] = _optTQuoteFieldTimes(packed, ageNowMs)
            bookClockMs = _optTFieldClockMs(packed, _OPT_TICK_BOOK_TIME_KEYS)
            ageSec = (ageNowMs - bookClockMs) / 1000.0 if bookClockMs > 0 else None
            # 源端时钟明显领先本机时钟时不把盘口当作可证明的新鲜报价。
            bookAgeSecByTs[tsCode] = ageSec if ageSec is not None and ageSec >= -5.0 else None

    trendCodes = list(
        dict.fromkeys([*_opt_t_board_default_codes(), str(opt_underlying or '').strip()])
    )
    trendSma120ByCode = queryTrendSma120ByCodes(
        trendCodes,
        snapshot=snap if isinstance(snap, dict) else None,
        autoFetchSnap=False,
    )
    trendSma120 = trendSma120ByCode.get(str(opt_underlying or '').strip()) or buildTrendSma120(
        [],
        spot=displaySpot,
    )

    groups = _optTBoardFillDisplayBuckets(raw_rows, quote_map)
    ivGroups = _optTBoardFillBuckets(iv_rows, quote_map, preferLiquid=True)

    sections: list[dict] = []
    # 字段级灰阶陈旧度：年龄基准（字段时间戳由 fieldAgeByTs 提供）
    ageNowMs = datetime.now().timestamp() * 1000.0
    expiry_kind_by_md = _opt_t_board_expiry_kind_map(
        [k[1] for k in groups.keys()],
        opt_underlying=opt_underlying,
    )
    maturityDates = [
        d
        for d in (_opt_t_board_parse_date(md) for _, md in groups.keys())
        if d is not None
    ]
    openEnd = max(maturityDates) if maturityDates else date.today()
    openDates = _opt_t_board_load_sse_open_dates(
        date.today() - timedelta(days=14),
        openEnd,
    )
    ivPanelTs = _opt_t_board_iv_panel_ts(opt_underlying)
    ivConn = None
    ivCur: Any = None
    if ivPanelTs and groups:
        try:
            ivConn = pymysql.connect(**DB_CONFIG_TUSHARE, cursorclass=pymysql.cursors.DictCursor)
            ivCur = ivConn.cursor()
        except Exception as e:
            logger.warning('opt_iv_panel 连接失败，Per 列将为空: %s', e)
            ivConn = None
            ivCur = None

    ivPatchLegs: list[dict[str, Any]] = []

    def _cachedPerWindows(tenor: str, callPut: str, asof: date | None) -> ivWindows.PerWindows:
        """三窗口按 (标的, 期限, 方向, asof) 走模块级缓存。

        窗口构建要排序全样本；逐行查分位每次重算一遍会把 2.4ms 乘上数百次。
        缓存跨请求存活，换交易日由 asof 进键自然失效。
        """
        if not ivPanelTs or not tenor or asof is None:
            return ivWindows.emptyWindows()
        key = ivWindows.cacheKey(
            ivPanelTs, tenor, callPut, asof, scope=ivWindows.SCOPE_EXCL_ASOF
        )
        cached = ivWindows.peekCached(key)
        if cached is not None:
            return cached
        if ivCur is None:
            return ivWindows.emptyWindows()
        dates, vals = _opt_t_board_fetch_iv_hist(
            ivCur,
            underlyingTs=ivPanelTs,
            tenor=tenor,
            callPut=callPut,
            asof=asof,
        )
        if not vals:
            # 查不到历史时不落缓存：否则连接恢复后仍会一直命中空窗口
            return ivWindows.emptyWindows()
        return ivWindows.cachedPerWindows(
            key,
            lambda: ivWindows.buildPerWindows(dates, vals, underlyingTs=ivPanelTs),
        )

    try:
        for (sm, md_s) in sorted(groups.keys(), key=lambda k: (k[0], k[1])):
            g = groups[(sm, md_s)]
            calls: dict = g['calls']
            puts: dict = g['puts']
            expiry_kind = expiry_kind_by_md.get(md_s, '')
            section_alt = expiry_kind in ('next_quarter', 'next_next_quarter')
            rowKeys = _optTBoardSortDisplayKeys(list(set(calls.keys()) | set(puts.keys())))
            quote_asof = _opt_t_board_section_quote_asof(calls, puts, quote_map)
            quoteAsofDate = _opt_t_board_parse_date(quote_asof) if quote_asof else None
            tenorKey = _OPT_T_EXPIRY_KIND_TO_TENOR.get(expiry_kind, '')
            perWinCall = _cachedPerWindows(tenorKey, 'C', quoteAsofDate)
            perWinPut = _cachedPerWindows(tenorKey, 'P', quoteAsofDate)
            try:
                from option_iv import getCnGb10yRate

                riskFreeRate = float(getCnGb10yRate(asof=quoteAsofDate))
            except Exception:
                riskFreeRate = 0.015
            md_date = _opt_t_board_parse_date(md_s)
            expiry_date_display = md_date.isoformat() if md_date else (md_s or '—')
            dteDays = _opt_t_board_days_to_expiry(md_date, asof=quoteAsofDate)
            tradeDaysMeta = _opt_t_board_trade_days_to_expiry(
                md_date, asof=quoteAsofDate, openDates=openDates,
            )
            days_to_expiry_label = _opt_t_board_days_to_expiry_label(
                dteDays, tradeDaysMeta,
            )
            expiry_meta = _opt_t_board_format_expiry_meta(
                expiry_date_display,
                days_to_expiry_label,
                expiry_kind,
            )
            atmByAdj = _opt_t_board_pick_atm_by_adj(rowKeys, displaySpot)
            ivG = ivGroups.get((sm, md_s)) or {'calls': {}, 'puts': {}}
            ivCalls: dict[float, dict] = ivG['calls']
            ivPuts: dict[float, dict] = ivG['puts']
            sectionHasLive = False
            for bucket in (ivCalls, ivPuts):
                for row in bucket.values():
                    tc = str(row.get('ts_code') or '').strip()
                    if (quote_map.get(tc) or {}).get('live'):
                        sectionHasLive = True
                        break
                if sectionHasLive:
                    break
            fwdSpot = displaySpot if sectionHasLive else dailySpot
            ivStrikes = sorted(set(ivCalls.keys()) | set(ivPuts.keys()))
            sectionForward = _opt_t_board_section_forward(
                strikes=ivStrikes,
                calls=ivCalls,
                puts=ivPuts,
                quote_map=quote_map,
                spot=fwdSpot,
                daysToExpiry=dteDays,
                rate=riskFreeRate,
            )
            rows_out: list[dict] = []
            emBook: dict[float, dict[str, float]] = {}
            for idx, dKey in enumerate(rowKeys):
                stk, rowUnit, isAdjFlag = dKey
                isAdj = bool(isAdjFlag)
                rowMult = rowUnit if rowUnit > 0 else spreadMult
                rc = calls.get(dKey)
                rp = puts.get(dKey)
                qc = quote_map.get(str(rc['ts_code']).strip()) if rc else None
                qp = quote_map.get(str(rp['ts_code']).strip()) if rp else None
                sd = _fmt_strike_display(stk, opt_underlying, isAdj=isAdj)
                strikeTitle = '调整合约' if isAdj else '标准合约'
                if rowUnit > 0:
                    strikeTitle = f'{strikeTitle} · 乘数 {int(rowUnit) if abs(rowUnit - int(rowUnit)) < 1e-6 else rowUnit}'
                call_spread_val: float | None = None
                nxt = _optTBoardNextSameClassIdx(rowKeys, idx, 1)
                if nxt is not None:
                    rc_hi = calls.get(rowKeys[nxt])
                    if rc and rc_hi:
                        q_lo = qc
                        q_hi = quote_map.get(str(rc_hi['ts_code']).strip())
                        # 牛市认购价差：买低行权 ask − 卖高行权 bid
                        c_lo = _optQuoteBuyPx(q_lo)
                        c_hi = _optQuoteSellPx(q_hi)
                        if c_lo is not None and c_hi is not None:
                            call_spread_val = (c_lo - c_hi) * rowMult
                put_spread_val: float | None = None
                prev = _optTBoardNextSameClassIdx(rowKeys, idx, -1)
                if prev is not None:
                    rp_lo = puts.get(rowKeys[prev])
                    if rp and rp_lo:
                        q_hi = qp
                        q_lo = quote_map.get(str(rp_lo['ts_code']).strip())
                        # 熊市认沽价差展示：卖高行权 bid − 买低行权 ask
                        c_hi = _optQuoteSellPx(q_hi)
                        c_lo = _optQuoteBuyPx(q_lo)
                        if c_hi is not None and c_lo is not None:
                            put_spread_val = (c_hi - c_lo) * rowMult
                callSell = _optQuoteSellPx(qc)
                putSell = _optQuoteSellPx(qp)
                callBuy = _optQuoteBuyPx(qc)
                putBuy = _optQuoteBuyPx(qp)
                callBookAgeSec = bookAgeSecByTs.get(str((rc or {}).get('ts_code') or '').strip())
                putBookAgeSec = bookAgeSecByTs.get(str((rp or {}).get('ts_code') or '').strip())
                callSelectorSell, callSelectorSource = _opt_t_board_selector_sell_price(
                    qc,
                    bookAgeSec=callBookAgeSec,
                    inMarketSession=ageActive,
                )
                putSelectorSell, putSelectorSource = _opt_t_board_selector_sell_price(
                    qp,
                    bookAgeSec=putBookAgeSec,
                    inMarketSession=ageActive,
                )
                callLast = _optQuoteLastPx(qc)
                putLast = _optQuoteLastPx(qp)
                # 「最新」= 源端当日成交 last；无成交才昨收。不因负时值/略出档口改写
                callTradeLive = bool((qc or {}).get('trade_live'))
                putTradeLive = bool((qp or {}).get('trade_live'))
                callM = _opt_t_board_seller_metrics(
                    sellPx=callSell,
                    buyPx=callBuy,
                    strike=stk,
                    spot=displaySpot if (qc or {}).get('live') else dailySpot,
                    forward=sectionForward,
                    callPut='C',
                    daysToExpiry=dteDays,
                    rate=riskFreeRate,
                )
                putM = _opt_t_board_seller_metrics(
                    sellPx=putSell,
                    buyPx=putBuy,
                    strike=stk,
                    spot=displaySpot if (qp or {}).get('live') else dailySpot,
                    forward=sectionForward,
                    callPut='P',
                    daysToExpiry=dteDays,
                    rate=riskFreeRate,
                )
                rememberExpectedMoveLeg(
                    emBook,
                    strike=stk,
                    isAdj=isAdj,
                    bidCall=callSell,
                    askCall=callBuy,
                    lastCall=callLast,
                    bidPut=putSell,
                    askPut=putBuy,
                    lastPut=putLast,
                    callIv=callM.get('iv_raw'),
                    putIv=putM.get('iv_raw'),
                )
                callSelectorM = _opt_t_board_seller_metrics(
                    sellPx=callSelectorSell,
                    strike=stk,
                    spot=displaySpot if (qc or {}).get('live') else dailySpot,
                    forward=sectionForward,
                    callPut='C',
                    daysToExpiry=dteDays,
                    rate=riskFreeRate,
                )
                putSelectorM = _opt_t_board_seller_metrics(
                    sellPx=putSelectorSell,
                    strike=stk,
                    spot=displaySpot if (qp or {}).get('live') else dailySpot,
                    forward=sectionForward,
                    callPut='P',
                    daysToExpiry=dteDays,
                    rate=riskFreeRate,
                )
                callPer = _opt_t_per_cells(
                    perWinCall,
                    callM.get('iv_raw') if isinstance(callM.get('iv_raw'), float) else None,
                    callM['delta'],
                )
                putPer = _opt_t_per_cells(
                    perWinPut,
                    putM.get('iv_raw') if isinstance(putM.get('iv_raw'), float) else None,
                    putM['delta'],
                )
                mnyText, mnyCls = _opt_t_board_moneyness_pct(stk, displaySpot)
                atm_strike = atmByAdj[1 if isAdj else 0]
                isAtm = atm_strike is not None and abs(stk - atm_strike) < 1e-9
                clsParts = []
                if isAtm:
                    clsParts.append('opt-t-row-atm')
                if isAdj:
                    clsParts.append('opt-t-row-adj')
                # 非竞价时段（午休/盘前/盘后）统一给空 class = 正常色；
                # 竞价时段内缺年龄映射才按「不可证明新鲜」给最深灰。
                if not ageActive:
                    callAge = _optTQuoteFieldTimes(None, ageNowMs, forceFresh=True)
                    putAge = _optTQuoteFieldTimes(None, ageNowMs, forceFresh=True)
                else:
                    callAge = fieldAgeByTs.get(str((rc or {}).get('ts_code') or '').strip()) or {}
                    putAge = fieldAgeByTs.get(str((rp or {}).get('ts_code') or '').strip()) or {}
                    if not callAge:
                        callAge = _optTQuoteFieldTimes(None, ageNowMs)
                    if not putAge:
                        putAge = _optTQuoteFieldTimes(None, ageNowMs)
                rows_out.append(
                    {
                        'strike': stk,
                        'strike_display': sd,
                        'strike_title': strikeTitle,
                        'is_adj': isAdj,
                        'row_cls': ' '.join(clsParts),
                        'moneyness_pct': mnyText,
                        'moneyness_cls': mnyCls,
                        'is_atm': isAtm,
                        # 灰阶陈旧度：正常=原色 / >10s 淡灰 / >60s 深灰（仅灰阶，不加色相）
                        'call_last_age_cls': callAge['last'],
                        'call_vol_age_cls': callAge['vol'],
                        'call_oi_age_cls': callAge['oi'],
                        'call_book_age_cls': callAge['book'],
                        'put_last_age_cls': putAge['last'],
                        'put_vol_age_cls': putAge['vol'],
                        'put_oi_age_cls': putAge['oi'],
                        'put_book_age_cls': putAge['book'],
                        'call_ts_code': str((rc or {}).get('ts_code') or '').strip(),
                        'put_ts_code': str((rp or {}).get('ts_code') or '').strip(),
                        'call_oi': _fmt_opt_daily_qty(qc['oi']) if qc else '—',
                        'call_vol': _fmt_opt_daily_qty(qc['vol']) if qc else '—',
                        'call_close': _fmt_opt_last_px(callLast) if callLast is not None else '—',
                        'call_live': callTradeLive,
                        'call_px_title': (
                            (
                                '最新成交'
                                if callTradeLive
                                else '无可信成交，最新=昨收'
                            )
                            + (
                                f'｜评估={callM.get("px_label")}(时值/折溢用'
                                f'{"卖一" if callM.get("action_mode") == "buy" else "买一"})'
                                if callM.get('action_px') is not None
                                else ''
                            )
                        ),
                        'call_action_mode': callM.get('action_mode') or 'sell',
                        'call_strategy_ready': False,
                        'call_selector_status': (
                            'priced' if callSelectorM.get('action_px') is not None else 'empty'
                        ),
                        'call_selector_price_source': callSelectorSource,
                        'call_selector_tv_ann': callSelectorM['tv_ann'],
                        'call_selector_delta': callSelectorM['delta'],
                        'call_selector_eff_px': callSelectorM['eff_px'],
                        'call_prem_pct': callM['prem_pct'],
                        'call_prem_cls': callM['prem_cls'],
                        'call_iv': callM['iv'],
                        **{f'call_{k}': v for k, v in callPer.items()},
                        'call_gamma': callM['gamma'],
                        'call_delta': callM['delta'],
                        'call_delta_cls': callM['delta_cls'],
                        'call_tv_ann': callM['tv_ann'],
                        'call_tv_ann_cls': callM['tv_ann_cls'],
                        'call_time_val': callM['time_val'],
                        'call_time_cls': callM['time_cls'],
                        'call_eff_px': callM['eff_px'],
                        'call_disc_prem_pct': callM['disc_prem_pct'],
                        'call_disc_cls': callM['disc_cls'],
                        'call_spread': _fmt_spread_qty(call_spread_val),
                        'put_spread': _fmt_spread_qty(put_spread_val),
                        'put_eff_px': putM['eff_px'],
                        'put_disc_prem_pct': putM['disc_prem_pct'],
                        'put_disc_cls': putM['disc_cls'],
                        'put_time_val': putM['time_val'],
                        'put_time_cls': putM['time_cls'],
                        'put_tv_ann': putM['tv_ann'],
                        'put_tv_ann_cls': putM['tv_ann_cls'],
                        'put_delta': putM['delta'],
                        'put_delta_cls': putM['delta_cls'],
                        'put_gamma': putM['gamma'],
                        **{f'put_{k}': v for k, v in putPer.items()},
                        'put_iv': putM['iv'],
                        'put_prem_pct': putM['prem_pct'],
                        'put_prem_cls': putM['prem_cls'],
                        'put_close': _fmt_opt_last_px(putLast) if putLast is not None else '—',
                        'put_live': putTradeLive,
                        'put_px_title': (
                            (
                                '最新成交'
                                if putTradeLive
                                else '无可信成交，最新=昨收'
                            )
                            + (
                                f'｜评估={putM.get("px_label")}(时值/折溢用'
                                f'{"卖一" if putM.get("action_mode") == "buy" else "买一"})'
                                if putM.get('action_px') is not None
                                else ''
                            )
                        ),
                        'put_action_mode': putM.get('action_mode') or 'sell',
                        'put_strategy_ready': False,
                        'put_selector_status': (
                            'priced' if putSelectorM.get('action_px') is not None else 'empty'
                        ),
                        'put_selector_price_source': putSelectorSource,
                        'put_selector_tv_ann': putSelectorM['tv_ann'],
                        'put_selector_delta': putSelectorM['delta'],
                        'put_selector_eff_px': putSelectorM['eff_px'],
                        'put_vol': _fmt_opt_daily_qty(qp['vol']) if qp else '—',
                        'put_oi': _fmt_opt_daily_qty(qp['oi']) if qp else '—',
                        '_call_vol_n': _opt_daily_vol_oi_float(qc, 'vol') if qc else None,
                        '_call_oi_n': _opt_daily_vol_oi_float(qc, 'oi') if qc else None,
                        '_put_vol_n': _opt_daily_vol_oi_float(qp, 'vol') if qp else None,
                        '_put_oi_n': _opt_daily_vol_oi_float(qp, 'oi') if qp else None,
                        'call_vol_n': _opt_daily_vol_oi_float(qc, 'vol') if qc else None,
                        'call_oi_n': _opt_daily_vol_oi_float(qc, 'oi') if qc else None,
                        'put_vol_n': _opt_daily_vol_oi_float(qp, 'vol') if qp else None,
                        'put_oi_n': _opt_daily_vol_oi_float(qp, 'oi') if qp else None,
                    }
                )
                for side, tsKey in (('C', 'call_ts_code'), ('P', 'put_ts_code')):
                    legTs = str(rows_out[-1].get(tsKey) or '').strip()
                    if legTs:
                        ivPatchLegs.append({
                            'ts': legTs,
                            'cp': side,
                            'strike': float(stk),
                            'dte': dteDays,
                            'tenor': tenorKey,
                            'asof': quoteAsofDate.isoformat() if quoteAsofDate else '',
                            'panel': ivPanelTs or '',
                        })
            cv = [r.get('_call_vol_n') for r in rows_out]
            co = [r.get('_call_oi_n') for r in rows_out]
            pv = [r.get('_put_vol_n') for r in rows_out]
            po = [r.get('_put_oi_n') for r in rows_out]
            adjFlags = [1 if r.get('is_adj') else 0 for r in rows_out]
            rcv = _opt_t_rank_top3_indices_by_group(cv, adjFlags)
            rco = _opt_t_rank_top3_indices_by_group(co, adjFlags)
            rpv = _opt_t_rank_top3_indices_by_group(pv, adjFlags)
            rpo = _opt_t_rank_top3_indices_by_group(po, adjFlags)
            for i, r in enumerate(rows_out):
                r['call_vol_cls'] = _opt_t_rank_cell_class('call', 'vol', rcv.get(i))
                r['call_oi_cls'] = _opt_t_rank_cell_class('call', 'oi', rco.get(i))
                r['put_vol_cls'] = _opt_t_rank_cell_class('put', 'vol', rpv.get(i))
                r['put_oi_cls'] = _opt_t_rank_cell_class('put', 'oi', rpo.get(i))
                for k in ('_call_vol_n', '_call_oi_n', '_put_vol_n', '_put_oi_n'):
                    r.pop(k, None)
            displayAsof, asofKind = blendSectionQuoteAsof(
                calls, puts, quote_map, quote_asof, liveClock
            )
            expectedMove = buildSectionExpectedMove(
                spot=displaySpot,
                days=dteDays,
                book=emBook,
            )
            markExpectedMoveRows(rows_out, displaySpot, expectedMove)
            sections.append(
                {
                    's_month': sm or '--',
                    'maturity_date': md_s or '--',
                    'selector_days_to_expiry': _opt_t_board_days_to_expiry(md_date),
                    'selector_trade_days_to_expiry': _opt_t_board_trade_days_to_expiry(
                        md_date, openDates=openDates,
                    ),
                    'caption': f"结算月 {sm or '--'} · 到期日 {md_s or '--'}",
                    'expiry_kind': expiry_kind,
                    'expiry_meta': expiry_meta,
                    'expected_move': expectedMove,
                    'quote_asof': displayAsof,
                    'quote_asof_kind': asofKind,
                    'spread_alt': section_alt,
                    'rows': rows_out,
                }
            )
    finally:
        if ivCur is not None:
            try:
                ivCur.close()
            except Exception:
                pass
        if ivConn is not None:
            try:
                ivConn.close()
            except Exception:
                pass

    obligationSelector = _opt_t_board_build_obligation_selector(sections)
    axisByKind = _optRightsAxisByKind(opt_underlying)
    try:
        rightsSelector = _opt_t_board_build_rights_selector(sections, axisByKind)
    except Exception:
        logger.exception('权利仓观察构建失败')
        rightsSelector = None
    termGate = buildTermStructureGate(axisByKind, opt_underlying)
    for sec in sections:
        for row in sec.get('rows') or []:
            if not isinstance(row, dict):
                continue
            for key in ('call_vol_n', 'call_oi_n', 'put_vol_n', 'put_oi_n'):
                row.pop(key, None)

    return {
        'error': err,
        'sections': sections,
        'obligation_selector': obligationSelector,
        'rights_selector': rightsSelector,
        'term_gate': termGate,
        'row_count': len(raw_rows),
        'quote_source': quoteSource,
        'quote_live_n': liveN,
        'trend_sma120': trendSma120,
        'trend_sma120_by_code': trendSma120ByCode,
        '_ivPatchLegs': ivPatchLegs,
    }


_T_BOARD_CACHE_TTL_SEC = 90.0
# 直播轮询目标；缓存是否失效由目标标的行情指纹决定。
_T_BOARD_LIVE_CACHE_TTL_SEC = float(os.getenv('OPT_T_BOARD_LIVE_TTL_SEC', '0.8'))
_T_BOARD_LIVE_STABLE_CACHE_TTL_SEC = float(
    os.getenv('OPT_T_BOARD_LIVE_STABLE_TTL_SEC', '30')
)
_T_BOARD_IDLE_POLL_MS = 10000
# 高频重算复用桶（秒）：同一桶内复用 payload，避免"内容每变必同步全量重算"。
# 注意：桶必须 **显著大于单次重算耗时**，否则每个请求都落到新桶、缓存永不命中，
# 形成「重算慢 → 每请求必重算 → 更慢」的自我维持循环（2026-09-11 实测踩到）。
# 实测：0=~2.4s、3s(≈重算耗时)无效、10s 可把 API 压回数百 ms。
# 用户确认取 5s（更实时，API 略升到数百 ms 量级仍远好于 4s）。
# 代价是至多该秒数的额外展示滞后；字段级灰阶陈旧度仍按真实字段时间着色。
# 设 0 关闭（退回"内容一变就重算"）。
_T_BOARD_REUSE_BUCKET_SEC = float(
    os.getenv('OPT_T_BOARD_REUSE_BUCKET_SEC', '5')
)
_tBoardCacheLock = threading.Lock()
_tBoardCache: dict[str, tuple[float, dict[str, Any], float]] = {}
_tBoardComputeLocks: dict[str, threading.Lock] = {}
_tBoardComputeLocksGuard = threading.Lock()
_tBoardRefreshing: set[str] = set()


def _daqmtTickDisplaySignature(packed: dict[str, Any] | None) -> list[Any]:
    """只保留 T 型实际消费的行情字段；忽略仅推进的 tick 时间。"""
    rec = packed if isinstance(packed, dict) else {}
    last = _optTickFloat(rec.get('last'))
    if last is None:
        last = _optTickFloat(rec.get('lastPrice'))
    vol = _optTickFloat(rec.get('vol'))
    if vol is None:
        vol = _optTickFloat(rec.get('volume'))
    oi = _optTickFloat(rec.get('oi'))
    if oi is None:
        oi = _optTickFloat(rec.get('openInt'))
    if oi is None:
        oi = _optTickFloat(rec.get('openInterest'))
    bid1 = _optQuotePxPositive(rec.get('bid1'))
    if bid1 is None and isinstance(rec.get('bidPrice'), (list, tuple)):
        bid = rec.get('bidPrice') or []
        bid1 = _optQuotePxPositive(bid[0]) if bid else None
    ask1 = _optQuotePxPositive(rec.get('ask1'))
    if ask1 is None and isinstance(rec.get('askPrice'), (list, tuple)):
        ask = rec.get('askPrice') or []
        ask1 = _optQuotePxPositive(ask[0]) if ask else None
    return [last, vol, oi, bid1, ask1, bool(rec.get('fromTdx'))]


def _daqmtEtfDisplaySignature(packed: dict[str, Any] | None) -> list[Any]:
    """ETF 只取影响现货有效性/价格的字段；时间仅保留交易日。"""
    rec = packed if isinstance(packed, dict) else {}
    day = decodeQuoteTickDate(
        rec.get('t') or rec.get('time') or rec.get('stime')
    )
    return [
        *_daqmtTickDisplaySignature(rec)[:2],
        _optTickFloat(rec.get('open')),
        _optTickFloat(rec.get('high')),
        _optTickFloat(rec.get('low')),
        day.isoformat() if day is not None else '',
    ]


def _daqmtMetaDisplaySignature(meta: dict[str, Any] | None) -> list[Any]:
    """QMT 补链时会影响分组、行权价和 A/非 A 展示的合约要素。"""
    rec = meta if isinstance(meta, dict) else {}
    unit = _optTickFloat(rec.get('unit'))
    if unit is None:
        unit = _optTickFloat(rec.get('per_unit'))
    return [
        _opt_t_board_norm_strike(rec.get('k')),
        str(rec.get('exp') or ''),
        str(rec.get('sm') or ''),
        str(rec.get('cp') or '').upper(),
        unit,
        str(rec.get('undl') or '').upper(),
        str(rec.get('name') or ''),
    ]


def _daqmtSnapWallKey(
    snap: dict[str, Any] | None,
    optUnderlying: str | None = None,
) -> str:
    """目标标的行情内容键；无关链和仅推进的 wall/time 不使缓存失效。"""
    if not isinstance(snap, dict):
        return ''
    u = str(optUnderlying or '').strip()
    if not u:
        return f"{snap.get('optWall') or ''}|{snap.get('etfWall') or ''}"
    optSnapRaw = snap.get('opt')
    metaRaw = snap.get('optMeta')
    etfRaw = snap.get('etf')
    optSnap = optSnapRaw if isinstance(optSnapRaw, dict) else {}
    metaMap = metaRaw if isinstance(metaRaw, dict) else {}
    etfMap = etfRaw if isinstance(etfRaw, dict) else {}
    optByCode = {str(k).upper(): v for k, v in optSnap.items()}
    metaByCode = {str(k).upper(): v for k, v in metaMap.items()}
    rows: list[list[Any]] = []
    for qmtCode in sorted(set(optByCode) | set(metaByCode)):
        meta = metaByCode.get(qmtCode)
        metaUndl = meta.get('undl') if isinstance(meta, dict) else ''
        if metaUndl:
            belongs = _optMetaUndlMatches(str(metaUndl), u)
        elif qmtCode.endswith('.SZO'):
            belongs = u == '159915'
        else:
            # SHO 合约号本身不含标的；meta 缺失时宁可多失效，不能漏掉真变化。
            belongs = True
        if not belongs:
            continue
        rows.append(
            [
                qmtCode,
                _daqmtMetaDisplaySignature(meta),
                _daqmtTickDisplaySignature(optByCode.get(qmtCode)),
            ]
        )
    etfCode = _optUnderlyingToQmtEtf(u).upper()
    etfByCode = {str(k).upper(): v for k, v in etfMap.items()}
    signature = {
        'u': u,
        'etf': _daqmtEtfDisplaySignature(etfByCode.get(etfCode)),
        'opt': rows,
    }
    raw = json.dumps(
        signature,
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    )
    digest = hashlib.sha1(raw.encode('utf-8')).hexdigest()[:20]
    # 复用桶（2026-09-11）：逐笔内容每 0.5~5s 就真变，若把内容摘要放进键，
    # 缓存永远不命中 —— 每个请求都同步全量重算，API 被压到 3~5s
    # （单次重算实为 ~0.5s，其余是反复重算 + 单飞锁排队）。
    # 因此桶内**只用桶号作键**：同一桶内所有请求复用同一份 payload；
    # 新桶的第一个请求自然会重算并拿到最新数据。
    # 代价是至多该秒数的额外展示滞后（字段级灰阶陈旧度仍按真实字段时间着色）。
    # 设 0 关闭（退回"内容一变就重算"）。
    if _T_BOARD_REUSE_BUCKET_SEC > 0:
        return f"b{int(time.time() // _T_BOARD_REUSE_BUCKET_SEC)}"
    return digest


def optQuoteLivePollMs(quoteSource: str) -> int:
    """前端轮询间隔：直播源对齐 TTL，避免连打未过期缓存。"""
    src = str(quoteSource or '').strip().lower()
    if src in ('daqmt', 'tdx', 'mixed'):
        return max(400, int(round(_T_BOARD_LIVE_CACHE_TTL_SEC * 1000.0)))
    return int(_T_BOARD_IDLE_POLL_MS)


def _tBoardComputeLock(u: str) -> threading.Lock:
    with _tBoardComputeLocksGuard:
        lock = _tBoardComputeLocks.get(u)
        if lock is None:
            lock = threading.Lock()
            _tBoardComputeLocks[u] = lock
        return lock


def peekOptTBoardPayload(optUnderlying: str) -> dict[str, Any] | None:
    """只读进程内 T 型缓存，不触发计算或后台刷新。"""
    u = str(optUnderlying or '').strip()
    if not u:
        return None
    with _tBoardCacheLock:
        hit = _tBoardCache.get(u)
        if hit is None:
            return None
        return hit[1]


_ivDeltaMemo: dict[str, tuple[float, dict[str, Any]]] = {}
_IV_DELTA_TTL_SEC = 1.5
_IV_DELTA_POLL_MS = 2000


def queryOptTBoardIvDelta(optUnderlying: str) -> dict[str, Any]:
    """可见链轻量 IV/Δ/Γ/Per。只读快照和已缓存的腿清单，不打 SQL、不重画整表。"""
    u = str(optUnderlying or '').strip()
    now = time.monotonic()
    hit = _ivDeltaMemo.get(u)
    if hit is not None and (now - hit[0]) < _IV_DELTA_TTL_SEC:
        return hit[1]
    payload = peekOptTBoardPayload(u)
    legs = (payload or {}).get('_ivPatchLegs') or []
    base: dict[str, Any] = {
        'ok': True,
        'ready': False,
        'u': u,
        'poll_ms': _IV_DELTA_POLL_MS,
        'greeks': {},
    }
    if not legs:
        return base
    snap = fetchDaqmtQuoteSnapshot()
    if not isinstance(snap, dict):
        return base
    asof = date.today()
    quotes: dict[str, dict[str, Any]] = {}
    optSnap = snap.get('opt') if isinstance(snap.get('opt'), dict) else {}
    for qmtCode, packed in optSnap.items():
        if not isTodayOptLast(packed, asof):
            continue
        ts = qmtOptToTsCode(str(qmtCode))
        if not ts:
            continue
        last = _optTickFloat(packed.get('last'))
        if last is None:
            last = _optTickFloat(packed.get('lastPrice'))
        if last is None or last <= 0:
            continue
        quotes[ts] = {'close': last}
    spot = None
    etfSnap = snap.get('etf') if isinstance(snap.get('etf'), dict) else {}
    etf = etfSnap.get(_optUnderlyingToQmtEtf(u))
    if isinstance(etf, dict) and not isStubDaqmtEtfTick(etf):
        spot = _optTickFloat(etf.get('last'))
    if spot is None or spot <= 0:
        return base
    try:
        from option_iv import getCnGb10yRate
        rate = float(getCnGb10yRate(asof=asof))
    except Exception:
        rate = 0.015
    byDte: dict[int, list[dict[str, Any]]] = {}
    for leg in legs:
        if not isinstance(leg, dict) or leg.get('ts') not in quotes:
            continue
        try:
            dte = int(leg.get('dte'))
        except (TypeError, ValueError):
            continue
        byDte.setdefault(dte, []).append(leg)
    greeks: dict[str, dict[str, Any]] = {}
    for dte, group in byDte.items():
        calls: dict[float, dict] = {}
        puts: dict[float, dict] = {}
        for leg in group:
            slot = calls if leg.get('cp') == 'C' else puts
            slot[float(leg['strike'])] = {'ts_code': leg['ts']}
        fwd = _opt_t_board_section_forward(
            strikes=sorted(set(calls) | set(puts)),
            calls=calls,
            puts=puts,
            quote_map=quotes,
            spot=spot,
            daysToExpiry=dte,
            rate=rate,
        )
        for leg in group:
            px = quotes[leg['ts']]['close']
            metrics = _opt_t_board_seller_metrics(
                closePx=px,
                strike=float(leg['strike']),
                spot=spot,
                forward=fwd,
                callPut=str(leg.get('cp') or 'C'),
                daysToExpiry=dte,
                rate=rate,
            )
            if metrics.get('iv') in (None, '—'):
                continue
            cell: dict[str, Any] = {
                'iv': {'text': metrics['iv']},
                'delta': {'text': metrics.get('delta') or '—'},
                'gamma': {'text': metrics.get('gamma') or '—'},
            }
            ivRaw = metrics.get('iv_raw')
            asofS = str(leg.get('asof') or '')
            try:
                asofD = date.fromisoformat(asofS) if asofS else None
            except ValueError:
                asofD = None
            key = ivWindows.cacheKey(
                str(leg.get('panel') or ''),
                str(leg.get('tenor') or ''),
                str(leg.get('cp') or ''),
                asofD,
                scope=ivWindows.SCOPE_EXCL_ASOF,
            )
            windows = ivWindows.peekCached(key) or ivWindows.emptyWindows()
            per = _opt_t_per_cells(
                windows,
                ivRaw if isinstance(ivRaw, float) else None,
                str(metrics.get('delta') or ''),
            )
            for kind in _OPT_T_PER_KINDS:
                cell[kind] = {'text': per.get(kind) or '—'}
            greeks[str(leg['ts'])] = cell
    out = {
        'ok': True,
        'ready': True,
        'u': u,
        'poll_ms': _IV_DELTA_POLL_MS,
        'greeks': greeks,
    }
    _ivDeltaMemo[u] = (time.monotonic(), out)
    return out


def clearOptTBoardCache() -> None:
    """Per C 锚点变更后丢掉整表 HTML/payload，避免 304 旧分位。"""
    with _tBoardCacheLock:
        _tBoardCache.clear()


def _refreshTBoardCacheWorker(u: str) -> None:
    """SWR 后台重算；与前台请求共享单飞锁。"""
    try:
        snap = fetchDaqmtQuoteSnapshot()
        wallKey = _daqmtSnapWallKey(snap, u)
        ttl = _T_BOARD_LIVE_CACHE_TTL_SEC if snap else _T_BOARD_CACHE_TTL_SEC
        lock = _tBoardComputeLock(u)
        with lock:
            now = time.monotonic()
            with _tBoardCacheLock:
                hit = _tBoardCache.get(u)
                if hit is not None:
                    cachedWall = ''
                    if isinstance(hit[1], dict):
                        cachedWall = str(hit[1].get('_snapWallKey') or '')
                    age = now - hit[0]
                    stableTtl = max(
                        hit[2], _T_BOARD_LIVE_STABLE_CACHE_TTL_SEC
                    )
                    if (not wallKey and age < hit[2]) or (
                        wallKey and cachedWall == wallKey and age < stableTtl
                    ):
                        return
            payload = _query_opt_t_board_payload(u, snapshot=snap)
            if isinstance(payload, dict):
                payload['_snapWallKey'] = wallKey
            with _tBoardCacheLock:
                _tBoardCache[u] = (time.monotonic(), payload, ttl)
    except Exception:
        logger.exception('T型后台刷新失败 u=%s', u)
    finally:
        with _tBoardCacheLock:
            _tBoardRefreshing.discard(u)


def _scheduleTBoardRefresh(u: str) -> None:
    with _tBoardCacheLock:
        if u in _tBoardRefreshing:
            return
        _tBoardRefreshing.add(u)
    threading.Thread(
        target=_refreshTBoardCacheWorker,
        args=(u,),
        daemon=True,
        name=f'opt-t-board-swr-{u}',
    ).start()


def queryOptTBoardPayloadCached(optUnderlying: str) -> dict[str, Any]:
    """T 型 payload：墙未变命中缓存；墙变/过期则 SWR 秒返旧值 + 后台重算。

    单次重算约 2~5s。若桶切换时同步重算，前端每 5s 整表轮询会被堵成「秒不开」。
    盘口/量仓已由 quote-delta 1s 补丁；IV/Per 最多滞后一个重算周期可接受。
    无任何缓存时才同步计算（冷启动）。
    """
    u = str(optUnderlying or '').strip()
    snap = fetchDaqmtQuoteSnapshot()
    wallKey = _daqmtSnapWallKey(snap, u)
    now = time.monotonic()
    with _tBoardCacheLock:
        hit = _tBoardCache.get(u)
        if hit is not None:
            t0, payload, cachedTtl = hit[0], hit[1], hit[2]
            cachedWall = ''
            if isinstance(payload, dict):
                cachedWall = str(payload.get('_snapWallKey') or '')
            if snap and wallKey:
                if cachedWall == wallKey and (now - t0) < max(
                    cachedTtl, _T_BOARD_LIVE_STABLE_CACHE_TTL_SEC
                ):
                    return payload
                # 桶/内容键变了：秒返旧表，后台重算；禁止同步堵请求
                stalePayload = payload
            elif (now - t0) < cachedTtl:
                return payload
            else:
                stalePayload = payload
        else:
            stalePayload = None
    if stalePayload is not None:
        _scheduleTBoardRefresh(u)
        return stalePayload
    ttl = _T_BOARD_LIVE_CACHE_TTL_SEC if snap else _T_BOARD_CACHE_TTL_SEC
    lock = _tBoardComputeLock(u)
    with lock:
        now2 = time.monotonic()
        with _tBoardCacheLock:
            hit = _tBoardCache.get(u)
            staleInside = None
            if hit is not None:
                t0, payload, cachedTtl = hit[0], hit[1], hit[2]
                cachedWall = ''
                if isinstance(payload, dict):
                    cachedWall = str(payload.get('_snapWallKey') or '')
                # 等待锁期间旁路已写入：新鲜同键直接用；否则秒返旧值
                if snap and wallKey and cachedWall == wallKey and (
                    now2 - t0
                ) < max(cachedTtl, _T_BOARD_LIVE_STABLE_CACHE_TTL_SEC):
                    return payload
                if (not snap or not wallKey) and (now2 - t0) < cachedTtl:
                    return payload
                staleInside = payload
        if staleInside is not None:
            _scheduleTBoardRefresh(u)
            return staleInside
        payload = _query_opt_t_board_payload(u, snapshot=snap)
        if isinstance(payload, dict):
            payload['_snapWallKey'] = wallKey
        with _tBoardCacheLock:
            _tBoardCache[u] = (time.monotonic(), payload, ttl)
        return payload


def _optTBoardWarmCodeList(primary: str | None = None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    u0 = str(primary or '').strip()
    if u0 and _OPT_T_BOARD_UNDERLYING_RE.match(u0):
        out.append(u0)
        seen.add(u0)
    for c in _opt_t_board_default_codes():
        if c not in seen:
            out.append(c)
            seen.add(c)
    return out


def _optTBoardNeedsWarm(underlying: str | None = None) -> bool:
    """壳页只在缺缓存时预热；已有五链则交给前端轮询，避免每次刷新打满重算。"""
    for u in _optTBoardWarmCodeList(underlying):
        if peekOptTBoardPayload(u) is None:
            return True
    return False


def _warmSpPlanCaches() -> None:
    """Sell Put 与 T 表解耦预热，避免五链重算挡住计划区首屏。"""
    try:
        from rt_monitor.services.opt_sp_plan import querySpPlan, spEnabledCodes

        for spU in spEnabledCodes():
            try:
                querySpPlan(spU, historyLimit=5)
            except Exception:
                logger.exception('预热 Sell Put 计划失败 u=%s', spU)
    except Exception:
        logger.exception('预热 Sell Put 计划失败')


def _warmOptTBoardCaches(underlying: str | None = None) -> None:
    """壳页/启动后预热五只 ETF 的 T 型 + IV + Sell Put；已命中的 T 型跳过。"""
    threading.Thread(
        target=_warmSpPlanCaches,
        daemon=True,
        name='opt-sp-plan-warm',
    ).start()
    try:
        _opt_t_board_profiles_for_page()
    except Exception:
        logger.exception('预热标的档案失败')
    for u in _optTBoardWarmCodeList(underlying):
        if peekOptTBoardPayload(u) is not None:
            continue
        try:
            queryOptTBoardPayloadCached(u)
        except Exception:
            logger.exception('预热 T 型报价失败 u=%s', u)
    try:
        from rt_monitor.services.opt_iv_board import queryOptIvBoardPayload

        queryOptIvBoardPayload()
    except Exception:
        logger.exception('预热 IV 看板失败')


def _render_rt_monitor_opt_t_board_no_cache():
    """期权 T 型壳页：轻量壳秒开；有热缓存则内嵌表体，否则前端并行拉 API。"""
    u_raw = request.args.get('u')
    underlying, u_warn = _opt_t_board_resolve_for_page(u_raw)
    defaults = _opt_t_board_default_codes()
    allow_set = _opt_t_board_allow_codes_set()
    allow_sorted = sorted(allow_set) if allow_set else None
    board_profiles = _opt_t_board_profiles_for_page()
    active_prof = _opt_t_board_symbol_profile(underlying)
    try:
        from rt_monitor.services.opt_sp_plan import spEnabledCodes, spEnabledLabel

        spCodes = list(spEnabledCodes())
        spLabel = spEnabledLabel()
    except Exception:
        spCodes = ['588000', '510500', '159915']
        spLabel = ' / '.join(spCodes)
    opt_t_config_json = json.dumps(
        {
            'defaults': defaults,
            'allowRestrict': allow_set is not None,
            'allowCodes': allow_sorted or [],
            'symbolProfiles': board_profiles.get('symbolProfiles') or {},
            'elasticOrderLabel': board_profiles.get('elasticOrderLabel') or '',
            'spCodes': spCodes,
            'spEnabledLabel': spLabel,
        },
        ensure_ascii=False,
    )
    try:
        from option_iv import getCnGb10yRate

        threading.Thread(
            target=getCnGb10yRate,
            kwargs={'asof': date.today()},
            daemon=True,
            name='opt-t-board-rf',
        ).start()
    except Exception:
        pass
    if _optTBoardNeedsWarm(underlying):
        threading.Thread(
            target=_warmOptTBoardCaches,
            args=(underlying,),
            daemon=True,
            name='opt-t-board-warm',
        ).start()

    # 热缓存内嵌：首屏不白等 /api/opt-t-board 的 2~5s 重算
    optTBodyHtml = ''
    optTDefer = True
    peeked = peekOptTBoardPayload(underlying) if underlying else None
    if isinstance(peeked, dict) and peeked.get('sections') is not None:
        try:
            optTBodyHtml = _opt_t_board_render_body_fragment(peeked)
            if optTBodyHtml:
                optTDefer = False
        except Exception:
            logger.exception('壳页内嵌 T 表体失败 u=%s', underlying)
            optTBodyHtml = ''
            optTDefer = True

    optIvBodyHtml = ''
    optIvDefer = True
    try:
        from rt_monitor.services.opt_iv_board import peekOptIvBoardLiveHtml

        ivHtml = peekOptIvBoardLiveHtml()
        if ivHtml:
            optIvBodyHtml = ivHtml
            optIvDefer = False
    except Exception:
        logger.exception('壳页内嵌 IV 看板失败')

    optSpPlanBoot = None
    try:
        from rt_monitor.services.opt_sp_plan import peekSpPlan

        peekedSp = peekSpPlan(underlying, historyLimit=5)
        if peekedSp is not None:
            optSpPlanBoot = json.loads(json.dumps(peekedSp, ensure_ascii=False, default=str))
    except Exception:
        logger.exception('壳页内嵌 Sell Put 计划失败')
        optSpPlanBoot = None

    optChipSpots: dict[str, Any] = {}
    try:
        optChipSpots = packChipSpotQuotes(fetchDaqmtQuoteSnapshot())
    except Exception:
        logger.exception('壳页内嵌顶栏现货失败')
        optChipSpots = {}
    optSma120Boot = {}
    if isinstance(peeked, dict) and isinstance(peeked.get('trend_sma120_by_code'), dict):
        optSma120Boot = peeked.get('trend_sma120_by_code') or {}

    resp = make_response(
        render_template(
            'rt_monitor_opt_t_board.html',
            opt_t_underlying=underlying,
            opt_t_warn=u_warn,
            opt_t_default_codes=defaults,
            opt_t_allow_codes=allow_sorted,
            opt_t_allow_restrict=(allow_set is not None),
            opt_t_sections=[],
            opt_t_error=None,
            opt_t_row_count=0,
            opt_t_defer_load=optTDefer,
            opt_t_body_html=optTBodyHtml,
            opt_t_symbol_profiles=board_profiles.get('symbolProfiles') or {},
            opt_t_elastic_order_label=board_profiles.get('elasticOrderLabel') or '',
            opt_t_active_profile=active_prof,
            opt_t_config_json=opt_t_config_json,
            opt_iv_blocks=[],
            opt_iv_error=None,
            opt_iv_asof='',
            opt_iv_rate_label='',
            opt_iv_defer_load=optIvDefer,
            opt_iv_body_html=optIvBodyHtml,
            opt_sp_plan_boot=optSpPlanBoot,
            opt_chip_spots=optChipSpots,
            opt_sma120_boot=optSma120Boot,
        )
    )
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['X-RT-Monitor-Build'] = RT_MONITOR_STATIC_VER
    resp.headers['X-RT-Monitor-App'] = 'rt_monitor_app'
    return resp


# 代码更新时间：2026-08-07 08:46
# 代码更新时间：2026-08-07 12:30 中金所股指期权：per_unit/现货/价差乘数分支；启发式价差（空 stats）
# 代码更新时间：2026-08-07 13:03 到期日桶改日历规则；中金所支持下下月/下下季
# 代码更新时间：2026-08-09 08:51 卖方扫表列：IV/权利金%(K)/年化%/时间价值/有效价/虚实%/涨跌%
# 代码更新时间：2026-08-09 09:05 IV 改段内平价远期 + 中债10Y rate
# 代码更新时间：2026-08-09 11:18 折溢%；去前收盘列；涨跌红绿；行权价3位；虚实%两位+几何着色
# 代码更新时间：2026-08-09 11:54 时值不截断允许负值；负时值 CSS 类
# 代码更新时间：2026-08-09 12:13 折溢%正/负/近零着色
# 代码更新时间：2026-08-09 12:56 年化/IV/利率：剩余天数与中债r对齐该段行情日
# 代码更新时间：2026-08-09 15:13 权利金%：Call=C/(S−C)，Put=C/(K−C)
# 代码更新时间：2026-08-10 16:07 去牛差/熊差标注；增时值年化=(时值/K)×365/DTE
# 代码更新时间：2026-08-10 16:18 去掉主列年化%；仅保留权利金%与时年化
# 代码更新时间：2026-08-10 22:09 权利金%/时年化阶梯着色（靛蓝/琥珀）
# 代码更新时间：2026-08-10 22:20 分档：权利金% 3/5/8/10/20；时年化 15/20/30/50/100
# 代码更新时间：2026-08-10 22:45 去段头策略标签；去涨跌%列；最新价改称收盘价
# 代码更新时间：2026-08-10 23:00 增 Black-76 Δ 与 IVR/分位（opt_iv_panel）；不加年化权利金%
# 代码更新时间：2026-08-10 23:39 IVR Delta 展示门槛
# 代码更新时间：2026-08-14 22:20 壳页秒开：T 型异步+TTL 缓存；opt_daily 按最新日对齐
# 代码更新时间：2026-08-19 10:59 Sell Put 预热扩到 510500
# 代码更新时间：2026-08-24 20:38 叠大QMT快照最新价；日频回退 opt_daily
# 代码更新时间：2026-08-25 14:52 拒非当日/中价冒充 last；段头按新鲜度标实时
# 代码更新时间：2026-08-25 15:04 last 冻住仍叠 QMT 当日成交量/持仓
# 代码更新时间：2026-08-25 15:19 无成交不标实时；僵链仍叠 ETF 现货
# 代码更新时间：2026-08-25 17:36 有成交用 QMT 最新/量/仓；无成交昨收且量 0
# 代码更新时间：2026-08-25 18:18 T 型最新固定四位小数
# 代码更新时间：2026-08-25 18:27 拒 QMT 无量占位柱当今日现货
# 代码更新时间：2026-08-25 18:47 拉快照 timeout 1s、失败冷却 3s
# 代码更新时间：2026-08-25 19:34 QMT 链缺口补行；无 opt_daily 也叠行情
# 代码更新时间：2026-08-25 20:41 overlay 只叠本标的；IV 远期含 A；缓存不预拉快照
# 代码更新时间：2026-08-26 06:17 摘牌日当天仍展示（delist_date>=今天）
# 代码更新时间：2026-08-27 10:51 补行 undl 按六位对齐，吃快照 588000.SH
# 代码更新时间：2026-08-27 11:50 T型 SWR 秒返过期缓存；默认/预热五只 ETF
# 代码更新时间：2026-08-27 13:22 壳页注入 spCodes；Sell Put 两标的一律预热
# 代码更新时间：2026-08-27 14:10 直播 TTL 8s；HTML 片段缓存；缺缓存才预热
# 代码更新时间：2026-08-27 14:57 补行不与已入库 opt_basic ts_code 重复
# 代码更新时间：2026-08-28 06:57 快照 memo 2s；optQuoteLivePollMs 对齐直播 TTL
# 代码更新时间：2026-08-28 07:08 直播 TTL/轮询 4s，memo 0.5s，盘后 10s
# 代码更新时间：2026-08-28 08:04 T型展示全部在市 C/P（含 A、非标准乘数）分行
# 代码更新时间：2026-08-28 09:28 T型行权价：先标准后 A，各自由低到高
# 代码更新时间：2026-08-29 14:58 同月标准/A 量仓前三与平值分开标记
# 代码更新时间：2026-09-02 07:49 Sell Put 预热扩到 159915
# 代码更新时间：2026-09-07 20:58 顶栏 SMA120 动量闸状态灯（trend_sma120，只展示不禁开）
# 代码更新时间：2026-09-08 22:25 SMA120 灯面去掉「已过/未过」，保留色与缺口%
# 代码更新时间：2026-09-08 22:31 五只 ETF chip 各自挂 SMA120（trend_sma120_by_code）
# 代码更新时间：2026-09-09 06:38 chip 去弹性#序；默认序 588000/159915/510050/510300/510500
# 代码更新时间：2026-09-09 10:50 SMA120 gap_text 改为两位小数（如 -4.90%）
# 代码更新时间：2026-09-09 16:49 卖价=买一/买价=卖一；折溢时值负用卖一；TDX 段头与无实时
# 代码更新时间：2026-09-09 17:49 时值&lt;0 切买方：权利金/时值/有效价/折溢/IV 全用卖一
# 代码更新时间：2026-09-09 18:01 「最新」=last；时值/折溢等用买一/卖一评估，命名去歧义
# 代码更新时间：2026-09-09 18:04 买方仅当卖一时值&lt;0；夹心区仍用买一（修 2.850）
# 代码更新时间：2026-09-10 10:31
# 最新/成交量：源端当日 last+vol 直出；仅拦×5 错价；TTL 2s
# 代码更新时间：2026-09-10 13:00 vol滞后为0时仍保留源端最新，不回退昨收
# 代码更新时间：2026-09-10 14:05 直播 TTL 0.8s；按 optWall 失效缓存；禁止墙变后 SWR 旧表
# 代码更新时间：2026-09-21 10:07 墙/桶变改 SWR 秒返+后台重算；壳页内嵌热缓存表体/IV（秒开）
# 代码更新时间：2026-09-10 15:20 按目标标的行情内容失效；忽略无关链/global wall/time
# 代码更新时间：2026-09-15 08:43 T 型段头追加当月/下月/当季/下季展示期限
# 代码更新时间：2026-09-15 18:26 IVR 仅 |Delta|∈[0.10,0.90] 显示
# 代码更新时间：2026-09-15 18:55 段头分离本地观测与原始tick最后市场事件
# 代码更新时间：2026-09-17 08:19 跨月义务仓选择器与左右行权价标记
# 代码更新时间：2026-09-17 08:52 标准 K 跨月对齐、缺档标示与 Delta 非硬门槛
# 代码更新时间：2026-09-20 21:26 IVR 单列改 Per 1y/C/A 三列；义务仓优选星标改用 per1y_raw 浮点
# 代码更新时间：2026-09-20 21:51 义务仓优选分位列去掉 ☆（远月分位恒最高，无区分度）
# 代码更新时间：2026-09-21 06:12 量仓 ≥1 万显示 x.x万（delta 同源）
# 代码更新时间：2026-09-21 06:31 Per C 锚点保存时清空 T 板缓存
# 代码更新时间：2026-09-21 07:12 T 型 IV≥100 取整；义务仓 ┊ 居中对齐
# 代码更新时间：2026-09-21 10:07 墙/桶变 SWR 秒返；壳页内嵌热缓存
# 代码更新时间：2026-09-22 13:00 权利仓分位改用逐档 Per；页签选中色；平价格横向排
# 代码更新时间：2026-09-22 13:28 权利仓页签去「便宜」；波动型档位实三–虚四
# 代码更新时间：2026-09-22 13:47 波动型 Per 改该档 T 型；中轴仅翼部提示
# 代码更新时间：2026-09-22 18:07 段头/表头剩余天数附 SSE 交易日
# 代码更新时间：2026-09-22 18:15 trade_cal 未覆盖到期日则不展示交易日（防低估）
# 代码更新时间：2026-09-23 05:09 Δ 展示由两位改为三位小数（义务仓/权利仓/T型同源）
# 代码更新时间：2026-09-23 06:12 chip 现货最新价三位 + 涨跌幅随 quote-delta
# 代码更新时间：2026-09-23 06:46 T 表现货 Γ 两位，与期权 Δ 同重算，不进 quote-delta
# 代码更新时间：2026-09-23 06:54 IV 看板中轴列展示名改为「溢」
# 代码更新时间：2026-09-23 07:09 chip 增 Intra，与主页 Ma/Ema 方向表同式
# 代码更新时间：2026-09-23 07:53 Sell Put 壳页内嵌 + 与 T 表并行预热
# 代码更新时间：2026-09-23 09:02 chip Pct/Intra 展示去掉百分号
# 代码更新时间：2026-09-23 09:31 chip 第二行 Gap + expanding Per 与 gapP 同源
# 代码更新时间：2026-09-23 09:53 到期日 DTE=0 仍反解 IV/Δ/Γ（时年化仍空）
# 代码更新时间：2026-09-23 10:16 壳页内嵌顶栏现货 spots + SMA120
# 代码更新时间：2026-09-23 13:33 权利仓波动型只保留至少一个期限入选的档
# 代码更新时间：2026-09-24 11:39 义务仓优选只保留至少一个期限入选的档
# 代码更新时间：2026-09-28 08:52 期限结构闸门；各到期 1σ 带落到行权价
# 代码更新时间：2026-09-28 09:13 期限判词拆成溢/1y/HV，分开报价高低和溢价
# 代码更新时间：2026-09-28 11:54 曲线解释并入首行；判词后直接写出用法
# 代码更新时间：2026-09-28 12:23 正挂两条补上竖线后的用法
# 代码更新时间：2026-09-29 09:10 溢门槛对齐；认沽认购分判；权利仓可看收紧；用法色块
# 代码更新时间：2026-10-01 07:15 顶栏现货：今日 QMT → 今日 rt_k → 日线并标日期
# 代码更新时间：2026-10-02 08:05 顶栏 Pct/Intra 旁注主页同式全历史分位
# 代码更新时间：2026-10-02 08:48 顶栏 Gap 分位改主页同式；Amp 跟在 Gap 后


