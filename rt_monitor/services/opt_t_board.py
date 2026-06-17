"""期权 T 型报价板域。"""
from __future__ import annotations

from collections import defaultdict
import json
import os
import re
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pymysql
from flask import make_response, render_template, request
from logger_config import get_logger

from rt_monitor.core.config import DB_CONFIG_TUSHARE, RT_MONITOR_STATIC_VER

logger = get_logger(__name__)

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


def _fmt_opt_daily_qty(val: object) -> str:
    """opt_daily 成交量/持仓量；无效为 —。"""
    if val is None:
        return '—'
    try:
        x = float(val) if not isinstance(val, Decimal) else float(val)
        if x != x:
            return '—'
        if abs(x - round(x)) < 1e-6:
            return str(int(round(x)))
        s = f'{x:.4f}'.rstrip('0').rstrip('.')
        return s if s else '0'
    except (TypeError, ValueError):
        return '—'


def _opt_daily_close_float(q: dict[str, Any] | None) -> float | None:
    """最新价 close 转 float，用于价差。"""
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


def _opt_t_rank_cell_class(side: str, metric: str, rank: int | None) -> str:
    """side=call|put metric=vol|oi rank=1..3 → 附加 CSS 类名。"""
    if rank is None or rank < 1 or rank > 3:
        return ''
    return f'opt-t-rank-{side}-{metric}-{rank}'


def _fmt_spread_qty(val: float | None) -> str:
    """价差 ×10000 后的展示。"""
    if val is None:
        return '—'
    try:
        if val != val:
            return '—'
        ax = abs(val)
        if ax >= 1e6:
            return f'{val:,.2f}'
        if ax >= 1:
            return f'{val:,.4f}'.rstrip('0').rstrip('.')
        return f'{val:.6f}'.rstrip('0').rstrip('.')
    except (TypeError, ValueError):
        return '—'


def _fetch_opt_daily_latest_map(cur: Any, ts_codes: list[str]) -> dict[str, dict[str, Any]]:
    """
    各 ts_code 在 opt_daily 中 trade_date 最大的一行：pre_close、close、vol、oi、trade_date。
    ts_codes 较多时分批 IN，避免单条 SQL 过长。
    """
    out: dict[str, dict[str, Any]] = {}
    codes = [str(c).strip() for c in ts_codes if c and str(c).strip()]
    if not codes:
        return out
    chunk = 400
    for i in range(0, len(codes), chunk):
        part = codes[i : i + chunk]
        ph = ','.join(['%s'] * len(part))
        sql = (
            f'SELECT d.ts_code, d.trade_date, d.pre_close, d.close, d.vol, d.oi '
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
                'pre_close': row.get('pre_close'),
                'close': row.get('close'),
                'vol': row.get('vol'),
                'oi': row.get('oi'),
                'trade_date': row.get('trade_date'),
            }
    return out


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


def _opt_t_board_profiles_for_page() -> dict[str, Any]:
    try:
        from opt_spread_board_select import ELASTIC_ORDER_LABEL, profilesForBoardJson

        return {
            'symbolProfiles': profilesForBoardJson(),
            'elasticOrderLabel': ELASTIC_ORDER_LABEL,
        }
    except ImportError:
        return {'symbolProfiles': {}, 'elasticOrderLabel': ''}


def _opt_t_board_spread_role_label(expiry_kind: str, code6: str = '') -> str:
    """section 表头旁的策略角色说明（对齐回测铁律，510500 牛差含当季）。"""
    if expiry_kind == 'quarter':
        if code6 == '510500':
            return '当季 · 牛差主力（中证500）'
        return '当季 · 牛差'
    labels = {
        'near': '当月 · 熊差短线（时间效率最高）',
        'next': '下月 · 牛差/熊差主力',
        'next_quarter': '下季 · 趋势备选（长持）',
    }
    return labels.get(expiry_kind, '')


def _opt_t_board_expiry_kind_cn(expiry_kind: str) -> str:
    cn = {
        'near': '当月',
        'next': '下月',
        'quarter': '当季',
        'next_quarter': '下季备选',
    }
    return cn.get(expiry_kind, expiry_kind)


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


def _opt_t_board_days_to_expiry(maturity_d: date | None) -> int | None:
    """距到期日剩余自然日；解析失败返回 None。"""
    if maturity_d is None:
        return None
    return (maturity_d - date.today()).days


def _opt_t_board_days_to_expiry_label(days: int | None) -> str | None:
    """剩余天数展示文案。"""
    if days is None:
        return None
    if days <= 0:
        return '今日到期'
    return f'剩{days}天'


def _opt_t_board_pick_atm_strike(strikes: list[float], spot: float | None) -> float | None:
    """按 |行权价-标的价| 最小取平值行权价。"""
    if spot is None or not strikes:
        return None
    return min(strikes, key=lambda s: abs(s - spot))


def _opt_t_board_fetch_underlying_close(cur: Any, opt_underlying: str) -> float | None:
    """读取 ETF 最新 close，用于选择近平值买腿。"""
    cur.execute(
        """
        SELECT close
        FROM etf_daily
        WHERE ts_code LIKE %s
          AND close IS NOT NULL
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (f'{opt_underlying}.%',),
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


def _opt_t_board_expiry_kind_map(md_values: list[str]) -> dict[str, str]:
    """按当前可交易到期日排序，标出 near/next/quarter/next_quarter。"""
    parsed: list[tuple[date, str]] = []
    today = date.today()
    for md_s in md_values:
        d = _opt_t_board_parse_date(md_s)
        if d is None or d < today:
            continue
        parsed.append((d, md_s))
    parsed.sort(key=lambda x: x[0])
    kinds = ['near', 'next', 'quarter', 'next_quarter']
    return {md_s: kinds[i] for i, (_, md_s) in enumerate(parsed[: len(kinds)])}


def _opt_t_board_spread_width(opt_underlying: str, direction: str, expiry_kind: str) -> int:
    """铁律固定 1 档；保留函数供兼容。"""
    return 1


def _opt_t_board_valid_quote(q: dict[str, Any] | None) -> bool:
    close = _opt_daily_close_float(q)
    if close is None or close <= 0:
        return False
    vol = _opt_daily_vol_oi_float(q, 'vol')
    oi = _opt_daily_vol_oi_float(q, 'oi')
    return (vol or 0) > 0 and (oi or 0) > 0


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
    """枚举候选价差，按回测成本甜点+胜率+盈亏比综合打分选腿。"""
    if spot is None or not legs:
        return None
    try:
        from opt_spread_board_select import pickSpreadByBacktestScore
    except ImportError as e:
        logger.warning('opt_spread_board_select 不可用: %s', e)
        return None
    return pickSpreadByBacktestScore(
        legs,
        quote_map,
        spot,
        opt_underlying,
        direction,
        expiry_kind,
        _opt_daily_close_float,
        _opt_t_board_valid_quote,
        _opt_t_board_stats_lookup_cached(),
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


def _opt_t_board_default_codes() -> list[str]:
    codes = _opt_t_board_codes_from_env(os.getenv('OPT_T_BOARD_DEFAULT_CODES'))
    return codes if codes else ['588000']


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


def _opt_t_board_render_body_fragment(payload: dict[str, Any]) -> str:
    """与 rt_monitor_opt_t_board_body.html 一致的表体 HTML。"""
    return render_template(
        'rt_monitor_opt_t_board_body.html',
        opt_t_sections=payload['sections'],
        opt_t_error=payload['error'],
        opt_t_row_count=int(payload.get('row_count') or 0),
    )


def _query_opt_t_board_payload(opt_underlying: str) -> dict:
    """
    从 stock_tushare.opt_basic 组装 T 型报价：标的 opt_code 含指定 6 位片段、delist_date 非空且大于今日、
    per_unit 按数值等于 10000（兼容库内 10000.0 等字符串）、仅 C/P。
    按 (s_month, maturity_date) 分组，组内行权价由低到高。
    列序：认购 oi/vol/pre_close/close/价差 | 行权价 | 认沽 价差/close/pre_close/vol/oi；
    行情与量均为 opt_daily 各 ts_code 在 MAX(trade_date) 行。
    认购价差（低行权价行）：该行认购 close − 下一档认购 close，×10000；最高行权价行为 —。
    认沽价差（高行权价行）：该行认沽 close − 上一档认沽 close，×10000；最低行权价行为 —。
    每个分组内认购/认沽的成交量、持仓量各自前三名单元格加醒目底色（便于扫表）。
    """
    err: str | None = None
    raw_rows: list[dict] = []
    quote_map: dict[str, dict[str, Any]] = {}
    underlying_close: float | None = None
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE, cursorclass=pymysql.cursors.DictCursor)
        try:
            with conn.cursor() as cur:
                underlying_close = _opt_t_board_fetch_underlying_close(cur, opt_underlying)
                cur.execute(
                    """
                    SELECT ts_code, call_put, exercise_price, s_month, maturity_date
                    FROM opt_basic
                    WHERE opt_code LIKE %s
                      AND delist_date IS NOT NULL AND delist_date > CURDATE()
                      AND CAST(
                          NULLIF(TRIM(REPLACE(COALESCE(per_unit, ''), ',', '')), '') AS DECIMAL(18, 6)
                      ) = 10000
                      AND UPPER(TRIM(COALESCE(call_put, ''))) IN ('C', 'P')
                    ORDER BY s_month, maturity_date, exercise_price ASC
                    """,
                    (f'%{opt_underlying}%',),
                )
                raw_rows = list(cur.fetchall() or [])
                ts_list = sorted({str(r['ts_code']).strip() for r in raw_rows if r.get('ts_code')})
                quote_map = _fetch_opt_daily_latest_map(cur, ts_list)
        finally:
            conn.close()
    except Exception as e:
        logger.exception('期权T型报价查询 opt_basic/opt_daily 失败: %s', e)
        err = str(e)

    groups: dict[tuple[str, str], dict[str, dict]] = defaultdict(lambda: {'calls': {}, 'puts': {}})
    for r in raw_rows:
        sm = str(r.get('s_month') or '').strip()
        md_val = r.get('maturity_date')
        if hasattr(md_val, 'isoformat'):
            md_s = md_val.isoformat()
        else:
            md_s = str(md_val or '').strip()
        key = (sm, md_s)
        cp = str(r.get('call_put') or '').strip().upper()
        stk = _opt_t_board_norm_strike(r.get('exercise_price'))
        if stk is None:
            continue
        bucket = groups[key]
        if cp == 'C':
            bucket['calls'][stk] = r
        elif cp == 'P':
            bucket['puts'][stk] = r

    sections: list[dict] = []
    expiry_kind_by_md = _opt_t_board_expiry_kind_map([k[1] for k in groups.keys()])
    for (sm, md_s) in sorted(groups.keys(), key=lambda k: (k[0], k[1])):
        g = groups[(sm, md_s)]
        calls: dict[float, dict] = g['calls']
        puts: dict[float, dict] = g['puts']
        expiry_kind = expiry_kind_by_md.get(md_s, '')
        bull_mark: dict[str, Any] | None = None
        bear_mark: dict[str, Any] | None = None
        try:
            from opt_spread_board_select import bearExpiryKindsFor, bullExpiryKindsFor

            bull_kinds = bullExpiryKindsFor(opt_underlying)
            bear_kinds = bearExpiryKindsFor(opt_underlying)
        except ImportError:
            bull_kinds = frozenset({'next', 'next_quarter'})
            bear_kinds = frozenset({'near', 'next', 'next_quarter'})
        if expiry_kind in bull_kinds:
            bull_mark = _opt_t_board_pick_spread_marks(
                calls,
                quote_map,
                underlying_close,
                opt_underlying,
                'bull',
                expiry_kind,
            )
        if expiry_kind in bear_kinds:
            bear_mark = _opt_t_board_pick_spread_marks(
                puts,
                quote_map,
                underlying_close,
                opt_underlying,
                'bear',
                expiry_kind,
            )
        spread_role = _opt_t_board_spread_role_label(expiry_kind, opt_underlying)
        section_alt = expiry_kind == 'next_quarter'
        strikes = sorted(set(calls.keys()) | set(puts.keys()), reverse=False)
        md_date = _opt_t_board_parse_date(md_s)
        expiry_date_display = md_date.isoformat() if md_date else (md_s or '—')
        days_to_expiry_label = _opt_t_board_days_to_expiry_label(_opt_t_board_days_to_expiry(md_date))
        expiry_meta = (
            f'{expiry_date_display} · {days_to_expiry_label}'
            if expiry_date_display and days_to_expiry_label
            else ''
        )
        atm_strike = _opt_t_board_pick_atm_strike(strikes, underlying_close)
        rows_out: list[dict] = []
        n_st = len(strikes)
        for idx, stk in enumerate(strikes):
            rc = calls.get(stk)
            rp = puts.get(stk)
            if rc is None and rp is None:
                continue
            sd = f'{stk:.3f}'
            tc_c = str(rc['ts_code']).strip() if rc else ''
            tc_p = str(rp['ts_code']).strip() if rp else ''
            qc = quote_map.get(tc_c) if tc_c else None
            qp = quote_map.get(tc_p) if tc_p else None
            call_spread_val: float | None = None
            if idx < n_st - 1:
                stk_hi = strikes[idx + 1]
                rc_hi = calls.get(stk_hi)
                if rc and rc_hi:
                    q_lo = qc
                    q_hi = quote_map.get(str(rc_hi['ts_code']).strip())
                    c_lo = _opt_daily_close_float(q_lo)
                    c_hi = _opt_daily_close_float(q_hi)
                    if c_lo is not None and c_hi is not None:
                        call_spread_val = (c_lo - c_hi) * 10000.0
            put_spread_val: float | None = None
            if idx > 0:
                stk_lo = strikes[idx - 1]
                rp_lo = puts.get(stk_lo)
                if rp and rp_lo:
                    q_hi = qp
                    q_lo = quote_map.get(str(rp_lo['ts_code']).strip())
                    c_hi = _opt_daily_close_float(q_hi)
                    c_lo = _opt_daily_close_float(q_lo)
                    if c_hi is not None and c_lo is not None:
                        put_spread_val = (c_hi - c_lo) * 10000.0
            rows_out.append(
                {
                    'strike': stk,
                    'strike_display': sd,
                    'is_atm': atm_strike is not None and abs(stk - atm_strike) < 1e-9,
                    'call_oi': _fmt_opt_daily_qty(qc['oi']) if qc else '—',
                    'call_vol': _fmt_opt_daily_qty(qc['vol']) if qc else '—',
                    'call_pre_close': _fmt_opt_daily_px(qc['pre_close']) if qc else '—',
                    'call_close': _fmt_opt_daily_px(qc['close']) if qc else '—',
                    'call_spread': _fmt_spread_qty(call_spread_val),
                    'put_spread': _fmt_spread_qty(put_spread_val),
                    'put_close': _fmt_opt_daily_px(qp['close']) if qp else '—',
                    'put_pre_close': _fmt_opt_daily_px(qp['pre_close']) if qp else '—',
                    'put_vol': _fmt_opt_daily_qty(qp['vol']) if qp else '—',
                    'put_oi': _fmt_opt_daily_qty(qp['oi']) if qp else '—',
                    'bull_buy': '',
                    'bull_sell': '',
                    'bear_buy': '',
                    'bear_sell': '',
                    'bull_buy_title': '',
                    'bull_sell_title': '',
                    'bear_buy_title': '',
                    'bear_sell_title': '',
                    '_call_vol_n': _opt_daily_vol_oi_float(qc, 'vol') if qc else None,
                    '_call_oi_n': _opt_daily_vol_oi_float(qc, 'oi') if qc else None,
                    '_put_vol_n': _opt_daily_vol_oi_float(qp, 'vol') if qp else None,
                    '_put_oi_n': _opt_daily_vol_oi_float(qp, 'oi') if qp else None,
                }
            )
            row = rows_out[-1]
            if bull_mark:
                if abs(stk - float(bull_mark['buy_strike'])) < 1e-9:
                    row['bull_buy'] = '买'
                    row['bull_buy_title'] = _opt_t_board_spread_title(bull_mark, 'buy', expiry_kind, opt_underlying)
                if abs(stk - float(bull_mark['sell_strike'])) < 1e-9:
                    row['bull_sell'] = '卖'
                    row['bull_sell_title'] = _opt_t_board_spread_title(bull_mark, 'sell', expiry_kind, opt_underlying)
            if bear_mark:
                if abs(stk - float(bear_mark['buy_strike'])) < 1e-9:
                    row['bear_buy'] = '买'
                    row['bear_buy_title'] = _opt_t_board_spread_title(bear_mark, 'buy', expiry_kind, opt_underlying)
                if abs(stk - float(bear_mark['sell_strike'])) < 1e-9:
                    row['bear_sell'] = '卖'
                    row['bear_sell_title'] = _opt_t_board_spread_title(bear_mark, 'sell', expiry_kind, opt_underlying)
        cv = [r.get('_call_vol_n') for r in rows_out]
        co = [r.get('_call_oi_n') for r in rows_out]
        pv = [r.get('_put_vol_n') for r in rows_out]
        po = [r.get('_put_oi_n') for r in rows_out]
        rcv = _opt_t_rank_top3_indices(cv)
        rco = _opt_t_rank_top3_indices(co)
        rpv = _opt_t_rank_top3_indices(pv)
        rpo = _opt_t_rank_top3_indices(po)
        for i, r in enumerate(rows_out):
            r['call_vol_cls'] = _opt_t_rank_cell_class('call', 'vol', rcv.get(i))
            r['call_oi_cls'] = _opt_t_rank_cell_class('call', 'oi', rco.get(i))
            r['put_vol_cls'] = _opt_t_rank_cell_class('put', 'vol', rpv.get(i))
            r['put_oi_cls'] = _opt_t_rank_cell_class('put', 'oi', rpo.get(i))
            for k in ('_call_vol_n', '_call_oi_n', '_put_vol_n', '_put_oi_n'):
                r.pop(k, None)
        sections.append(
            {
                's_month': sm or '--',
                'maturity_date': md_s or '--',
                'caption': f"结算月 {sm or '--'} · 到期日 {md_s or '--'}",
                'expiry_kind': expiry_kind,
                'spread_role': spread_role,
                'expiry_meta': expiry_meta,
                'spread_alt': section_alt,
                'rows': rows_out,
            }
        )

    return {'error': err, 'sections': sections, 'row_count': len(raw_rows)}


def _render_rt_monitor_opt_t_board_no_cache():
    """期权 T 型：多标的（opt_code LIKE）；oi/vol/价/价差 | 行权价 | 价差/价/vol/oi（opt_daily 最新日）。"""
    u_raw = request.args.get('u')
    underlying, u_warn = _opt_t_board_resolve_for_page(u_raw)
    payload = _query_opt_t_board_payload(underlying)
    defaults = _opt_t_board_default_codes()
    try:
        from opt_spread_board_select import sortCodesByElasticRank

        defaults = sortCodesByElasticRank(defaults)
    except ImportError:
        pass
    allow_set = _opt_t_board_allow_codes_set()
    allow_sorted = sorted(allow_set) if allow_set else None
    board_profiles = _opt_t_board_profiles_for_page()
    active_prof = _opt_t_board_symbol_profile(underlying)
    opt_t_config_json = json.dumps(
        {
            'defaults': defaults,
            'allowRestrict': allow_set is not None,
            'allowCodes': allow_sorted or [],
            'symbolProfiles': board_profiles.get('symbolProfiles') or {},
            'elasticOrderLabel': board_profiles.get('elasticOrderLabel') or '',
        },
        ensure_ascii=False,
    )
    resp = make_response(
        render_template(
            'rt_monitor_opt_t_board.html',
            opt_t_underlying=underlying,
            opt_t_warn=u_warn,
            opt_t_default_codes=defaults,
            opt_t_allow_codes=allow_sorted,
            opt_t_allow_restrict=(allow_set is not None),
            opt_t_sections=payload['sections'],
            opt_t_error=payload['error'],
            opt_t_row_count=int(payload.get('row_count') or 0),
            opt_t_symbol_profiles=board_profiles.get('symbolProfiles') or {},
            opt_t_elastic_order_label=board_profiles.get('elasticOrderLabel') or '',
            opt_t_active_profile=active_prof,
            opt_t_config_json=opt_t_config_json,
        )
    )
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['X-RT-Monitor-Build'] = RT_MONITOR_STATIC_VER
    resp.headers['X-RT-Monitor-App'] = 'rt_monitor_app'
    return resp


# 代码更新时间：2026-06-17 15:48
