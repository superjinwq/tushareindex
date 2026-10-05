"""牛散持股域：配置 JSON + top10_floatholder 查询。"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections import OrderedDict, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import pymysql
from flask import make_response, render_template
from logger_config import get_logger

from rt_monitor.core.config import (
    DB_CONFIG,
    DB_CONFIG_TUSHARE,
    NIUSAN_HOLDER_CONFIG_PATH,
    RT_MONITOR_STATIC_VER,
)

logger = get_logger(__name__)

_NIUSAN_CFG_LOCK = threading.Lock()
_niusan_holder_cfg_cached: dict | None = None
_DEFAULT_NIUSAN_HOLDER_CFG: dict = {
    'activeId': 'default',
    'schemes': [
        {
            'id': 'default',
            'schemeName': '章建平',
            'holders': ['章建平', '方章乐', '方文艳', '方德基'],
        },
    ],
}

# 持股主表按 scheme_id 缓存（查询约 6s）；配置变更时整表失效
NIUSAN_HOLDINGS_CACHE_TTL = 180.0
NIUSAN_HOLDINGS_CACHE_MAX = 24
_niusanHoldingsCacheLock = threading.Lock()
_niusanHoldingsCache: OrderedDict[str, tuple[float, dict]] = OrderedDict()


def _niusan_holder_cfg_normalize(cfg: dict | None) -> dict:
    """规范化方案列表；无效时回落内置默认一套。"""
    if not isinstance(cfg, dict):
        cfg = {}
    schemes_out: list[dict] = []
    raw_list = cfg.get('schemes')
    if isinstance(raw_list, list):
        for s in raw_list[:32]:
            if not isinstance(s, dict):
                continue
            sid = str(s.get('id') or '').strip()
            if not sid:
                sid = uuid.uuid4().hex[:12]
            scheme_name = str(s.get('schemeName') or '').strip()
            holders_raw = s.get('holders')
            holders: list[str] = []
            if isinstance(holders_raw, list):
                seen_k: set[str] = set()
                for h in holders_raw[:64]:
                    if not isinstance(h, str):
                        continue
                    t = h.strip()
                    if not t:
                        continue
                    k = t.casefold()
                    if k in seen_k:
                        continue
                    seen_k.add(k)
                    holders.append(t)
            if not holders:
                continue
            schemes_out.append({'id': sid, 'schemeName': scheme_name, 'holders': holders})
    if not schemes_out:
        base = _DEFAULT_NIUSAN_HOLDER_CFG['schemes'][0]
        schemes_out = [
            {
                'id': base['id'],
                'schemeName': base['schemeName'],
                'holders': list(base['holders']),
            },
        ]
    active_id = str(cfg.get('activeId') or '').strip()
    ids_ok = {str(x['id']) for x in schemes_out}
    if active_id not in ids_ok:
        active_id = schemes_out[0]['id']
    return {'activeId': active_id, 'schemes': schemes_out}


def _niusan_holder_cfg_write_atomic_inner(cfg: dict) -> None:
    """原子写入 JSON（调用方已持锁）。"""
    tmp = NIUSAN_HOLDER_CONFIG_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, NIUSAN_HOLDER_CONFIG_PATH)


def _niusan_holder_cfg_load() -> dict:
    """读取 niusan_holder_config.json；缺失或损坏时落地内置默认。"""
    global _niusan_holder_cfg_cached
    if _niusan_holder_cfg_cached is not None:
        return _niusan_holder_cfg_cached
    with _NIUSAN_CFG_LOCK:
        if _niusan_holder_cfg_cached is not None:
            return _niusan_holder_cfg_cached
        cfg: dict
        if os.path.isfile(NIUSAN_HOLDER_CONFIG_PATH):
            try:
                with open(NIUSAN_HOLDER_CONFIG_PATH, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                cfg = _niusan_holder_cfg_normalize(raw)
            except Exception as e:
                logger.warning('读取 niusan_holder_config.json 失败: %s，使用内置默认', e)
                cfg = _niusan_holder_cfg_normalize(dict(_DEFAULT_NIUSAN_HOLDER_CFG))
        else:
            cfg = _niusan_holder_cfg_normalize(dict(_DEFAULT_NIUSAN_HOLDER_CFG))
            try:
                _niusan_holder_cfg_write_atomic_inner(cfg)
            except Exception as e:
                logger.warning('写入默认 niusan_holder_config.json 失败: %s', e)
        _niusan_holder_cfg_cached = cfg
        return cfg


def _niusan_holdings_cache_invalidate(schemeId: str | None = None) -> None:
    """schemeId 为空则清空全部持股缓存。"""
    with _niusanHoldingsCacheLock:
        if schemeId is None:
            _niusanHoldingsCache.clear()
            return
        sid = str(schemeId or '').strip()
        if sid:
            _niusanHoldingsCache.pop(sid, None)


def _niusan_holdings_cache_get(schemeId: str) -> dict | None:
    sid = str(schemeId or '').strip()
    if not sid:
        return None
    nowTs = time.time()
    with _niusanHoldingsCacheLock:
        hit = _niusanHoldingsCache.get(sid)
        if hit is None or nowTs >= hit[0]:
            if hit is not None:
                _niusanHoldingsCache.pop(sid, None)
            return None
        _niusanHoldingsCache.move_to_end(sid)
        return hit[1]


def _niusan_holdings_cache_put(schemeId: str, payload: dict) -> None:
    sid = str(schemeId or '').strip()
    if not sid or not isinstance(payload, dict):
        return
    with _niusanHoldingsCacheLock:
        while len(_niusanHoldingsCache) >= NIUSAN_HOLDINGS_CACHE_MAX:
            _niusanHoldingsCache.popitem(last=False)
        _niusanHoldingsCache[sid] = (time.time() + NIUSAN_HOLDINGS_CACHE_TTL, payload)
        _niusanHoldingsCache.move_to_end(sid)


def _niusan_resolve_scheme_id_for_query(forSchemeId: str | None) -> str:
    sid = str(forSchemeId or '').strip()
    if sid:
        return sid
    sch = _get_active_niusan_scheme()
    return str(sch.get('id') or '').strip() if isinstance(sch, dict) else ''


def queryNiusanHoldingsCached(forSchemeId: str | None = None) -> dict:
    """持股主表查询（带 TTL）；供页面 API / 启动预热共用。"""
    sid = _niusan_resolve_scheme_id_for_query(forSchemeId)
    if sid:
        hit = _niusan_holdings_cache_get(sid)
        if hit is not None:
            return hit
    payload = _query_niusan_holdings_page_rows(for_scheme_id=forSchemeId or sid or None)
    err = payload.get('error')
    if sid and not err:
        _niusan_holdings_cache_put(sid, payload)
    return payload


def _niusan_holder_cfg_save(cfg: dict) -> dict:
    """校验并保存方案；方案名不参与检索，关联人至少一名。"""
    global _niusan_holder_cfg_cached
    normalized = _niusan_holder_cfg_normalize(cfg)
    if not normalized.get('schemes'):
        raise ValueError('至少保留一套方案')
    for s in normalized['schemes']:
        if not s.get('holders'):
            raise ValueError('每套方案至少填写一名关联人')
    with _NIUSAN_CFG_LOCK:
        _niusan_holder_cfg_write_atomic_inner(normalized)
        _niusan_holder_cfg_cached = normalized
    _niusan_holdings_cache_invalidate(None)
    return normalized


def _get_active_niusan_scheme() -> dict:
    cfg = _niusan_holder_cfg_load()
    aid = str(cfg.get('activeId') or '').strip()
    for s in cfg.get('schemes') or []:
        if isinstance(s, dict) and str(s.get('id')) == aid:
            return s
    schemes = cfg.get('schemes') or []
    if schemes:
        return schemes[0]
    return dict(_DEFAULT_NIUSAN_HOLDER_CFG['schemes'][0])


def _niusan_scheme_display_meta_by_id(scheme_id: str) -> tuple[str, str]:
    """方案展示名 + 关联人文案（与主页 thead 相同分隔符）。"""
    sid = str(scheme_id or '').strip()
    if not sid:
        return ('--', '')
    cfg = _niusan_holder_cfg_load()
    for s in cfg.get('schemes') or []:
        if isinstance(s, dict) and str(s.get('id')) == sid:
            sn = str(s.get('schemeName') or '').strip() or '--'
            hol = s.get('holders')
            if isinstance(hol, list):
                rj = ' / '.join(str(h).strip() for h in hol if isinstance(h, str) and h.strip())
            else:
                rj = ''
            return (sn, rj)
    return ('--', '')


def _active_niusan_holder_like_params() -> tuple[str, ...]:
    """当前启用方案：关联人 LIKE 参数（方案名不参与匹配）。"""
    sch = _get_active_niusan_scheme()
    holders = sch.get('holders') if isinstance(sch, dict) else None
    out: list[str] = []
    if isinstance(holders, list):
        for h in holders:
            if isinstance(h, str) and h.strip():
                out.append('%' + h.strip() + '%')
    if not out:
        for h in _DEFAULT_NIUSAN_HOLDER_CFG['schemes'][0]['holders']:
            out.append('%' + h + '%')
    return tuple(out)


def _niusan_holder_where_clause(alias: str = 't') -> str:
    """关联人 OR LIKE；占位个数与 _niusan_holder_params() 一致。"""
    n = len(_active_niusan_holder_like_params())
    if n < 1:
        n = 1
    a = alias + '.' if alias else ''
    parts = [f'{a}holder_name LIKE %s' for _ in range(n)]
    return '(' + ' OR '.join(parts) + ')'


def _niusan_holder_params() -> tuple[str, ...]:
    return _active_niusan_holder_like_params()


def _niusan_holder_like_tuple_from_list(holders: list | None) -> tuple[str, ...]:
    """方案内关联人 → LIKE 参数元组（空则返回空元组）。"""
    out: list[str] = []
    if isinstance(holders, list):
        for h in holders:
            if isinstance(h, str) and h.strip():
                out.append('%' + h.strip() + '%')
    return tuple(out)


def _niusan_holder_where_clause_n(alias: str, n_likes: int) -> str:
    """固定 n 个 LIKE 占位（与 _niusan_holder_like_tuple_from_list 长度一致）。"""
    if n_likes < 1:
        n_likes = 1
    a = alias + '.' if alias else ''
    parts = [f'{a}holder_name LIKE %s' for _ in range(n_likes)]
    return '(' + ' OR '.join(parts) + ')'


def _niusan_resolve_holder_hw_hp(for_scheme_id: str | None) -> dict:
    """
    关联人 WHERE 子句与 LIKE 参数。
    for_scheme_id 为空：当前启用方案（niusan_holder_config activeId）。
    非空：该方案 id（与主表 /api/niusan_scheme_holdings 一致）。
    成功：{'ok': True, 'hw': str, 'hp': tuple}
    失败：{'ok': False, 'error': 'scheme_not_found' | 'scheme_no_holders'}
    """
    sid = str(for_scheme_id or '').strip()
    if sid:
        cfg_pick = _niusan_holder_cfg_load()
        sch_pick = None
        for s in cfg_pick.get('schemes') or []:
            if isinstance(s, dict) and str(s.get('id')) == sid:
                sch_pick = s
                break
        if not sch_pick:
            return {'ok': False, 'error': 'scheme_not_found'}
        hol_pick = sch_pick.get('holders')
        hol_list_pick: list = hol_pick if isinstance(hol_pick, list) else []
        hp = _niusan_holder_like_tuple_from_list(hol_list_pick)
        if not hp:
            return {'ok': False, 'error': 'scheme_no_holders'}
        hw = _niusan_holder_where_clause_n('t', len(hp))
        return {'ok': True, 'hw': hw, 'hp': hp}
    hw = _niusan_holder_where_clause('t')
    hp = _niusan_holder_params()
    return {'ok': True, 'hw': hw, 'hp': hp}


def _niusan_ts_codes_present_in_rt_k(ts_codes: list[str]) -> set[str]:
    """stock_wind.rt_k 中出现的 ts_code（用于剔除无实时行情的代码如部分北交所）。"""
    if not ts_codes:
        return set()
    out: set[str] = set()
    conn = None
    try:
        conn = pymysql.connect(**DB_CONFIG)
        chunk_sz = 400
        for i in range(0, len(ts_codes), chunk_sz):
            part = ts_codes[i : i + chunk_sz]
            ph = ','.join(['%s'] * len(part))
            with conn.cursor() as cur:
                cur.execute(f'SELECT DISTINCT ts_code FROM rt_k WHERE ts_code IN ({ph})', part)
                for row in cur.fetchall() or []:
                    if row and row[0]:
                        out.add(str(row[0]).strip())
    except Exception as e:
        logger.warning('牛散持股 rt_k 过滤查询失败，暂不剔除代码：%s', e)
        return set(ts_codes)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return out


def _niusan_fetch_holdings_agg_at_stock_latest(
    cur, hw: str, hp: tuple
) -> list[tuple]:
    """每股：仅当关联人仍出现在该股 top10_floatholders「全市场最新已披露报告期」时纳入。

    口径：stock_latest = MAX(end_date)（该 ts_code、不限股东）；若关联人在该期仍有行则显示，
    报告期即该最新期；若该期已披露但关联人已不在前十大，则不显示。

    返回行：(ts_code, end_date, hold_sum, ratio_sum, n_chg_lines, sum_hold_change, ann_date_max)。
    """
    if not hp:
        return []
    # 1) 关联人出现过的股票 → 2) 各股全市场最新报告期 → 3) 仅保留关联人仍在最新期的聚合
    cur.execute(
        f'''
        SELECT DISTINCT t.ts_code
        FROM top10_floatholders t
        WHERE {hw}
        ''',
        hp,
    )
    ts_codes = [str(r[0]).strip() for r in (cur.fetchall() or []) if r and r[0]]
    if not ts_codes:
        return []
    ph = ','.join(['%s'] * len(ts_codes))
    cur.execute(
        f'''
        SELECT ts_code, MAX(end_date) AS mx_end
        FROM top10_floatholders
        WHERE ts_code IN ({ph})
        GROUP BY ts_code
        ''',
        tuple(ts_codes),
    )
    stock_latest = {
        str(r[0]).strip(): r[1]
        for r in (cur.fetchall() or [])
        if r and r[0] and r[1] is not None
    }
    if not stock_latest:
        return []
    # (ts_code, end_date) 成对过滤：关联人在该股最新期
    pair_sql = ' OR '.join(['(t.ts_code = %s AND t.end_date = %s)'] * len(stock_latest))
    pair_params: list = []
    for ts, ed in stock_latest.items():
        pair_params.extend([ts, ed])
    cur.execute(
        f'''
        SELECT d.ts_code,
               d.end_date,
               SUM(d.hold_amount) AS hold_sum,
               SUM(d.hold_float_ratio) AS ratio_sum,
               SUM(CASE WHEN d.hold_change IS NOT NULL THEN 1 ELSE 0 END) AS n_chg_lines,
               SUM(d.hold_change) AS sum_hold_change,
               MAX(d.ann_date) AS ann_date_max
        FROM (
            SELECT t.ts_code, t.end_date, t.hold_amount, t.hold_float_ratio, t.hold_change, t.ann_date,
                ROW_NUMBER() OVER (
                    PARTITION BY t.ts_code, t.holder_name
                    ORDER BY t.ann_date IS NULL ASC, t.ann_date DESC, t.hold_amount DESC
                ) AS rn
            FROM top10_floatholders t
            WHERE ({pair_sql}) AND {hw}
        ) d
        WHERE d.rn = 1
        GROUP BY d.ts_code, d.end_date
        ORDER BY d.ts_code
        ''',
        tuple(pair_params) + hp,
    )
    return list(cur.fetchall() or [])


def _niusan_ref_close_map_by_ts_end(
    cur, pairs: list[tuple[str, date]]
) -> dict[str, Decimal]:
    """ts_code → 报告期末及之前最近收盘价（stk_factor_pro.close）。"""
    out: dict[str, Decimal] = {}
    if not pairs:
        return out
    chunk = 80
    for i in range(0, len(pairs), chunk):
        part = pairs[i : i + chunk]
        unions = ' UNION ALL '.join(['SELECT %s AS ts_code, %s AS end_d'] * len(part))
        params: list = []
        for ts, ed in part:
            params.extend([ts, ed])
        cur.execute(
            f'''
            SELECT s.ts_code, s.close
            FROM stk_factor_pro s
            INNER JOIN (
                SELECT p.ts_code, MAX(p.trade_date) AS mx
                FROM stk_factor_pro p
                INNER JOIN ({unions}) e
                    ON p.ts_code = e.ts_code AND p.trade_date <= e.end_d
                GROUP BY p.ts_code
            ) m ON s.ts_code = m.ts_code AND s.trade_date = m.mx
            ''',
            tuple(params),
        )
        for x in cur.fetchall() or []:
            ts = str(x[0] or '').strip()
            if ts and x[1] is not None:
                out[ts] = x[1] if isinstance(x[1], Decimal) else Decimal(str(x[1]))
    return out


def _niusan_scheme_stock_count_and_amt_yi(cur, holders: list | None) -> tuple[int, str]:
    """单方案：每股最新已披露前十大口径下的去重股票只数 + 持股金额(亿)合计（同主表）。"""
    hp = _niusan_holder_like_tuple_from_list(holders)
    if not hp:
        return (0, '0.00')
    hw = _niusan_holder_where_clause_n('t', len(hp))
    agg = _niusan_fetch_holdings_agg_at_stock_latest(cur, hw, hp)
    if not agg:
        return (0, '0.00')
    rk_ok = _niusan_ts_codes_present_in_rt_k([r[0] for r in agg])
    agg = [r for r in agg if r[0] in rk_ok]
    if not agg:
        return (0, '0.00')
    pairs = [(str(r[0]).strip(), r[1]) for r in agg if r[0] and r[1] is not None]
    ref_close = _niusan_ref_close_map_by_ts_end(cur, pairs)
    amt_sum = Decimal('0')
    for r in agg:
        ts_code, _end_d, hold_sum = r[0], r[1], r[2]
        if hold_sum is None:
            continue
        rc = ref_close.get(str(ts_code).strip())
        if rc is None:
            continue
        try:
            amt_sum += Decimal(str(hold_sum)) * Decimal(str(rc)) / Decimal('100000000')
        except Exception:
            pass
    return (len(agg), f'{float(amt_sum):.2f}')


def _niusan_scheme_summaries_for_report(conn) -> list[dict[str, object]]:
    """配置中各方案：各自按「每股最新已披露前十大」统计持股只数与金额（亿）。"""
    scheme_summaries: list[dict[str, object]] = []
    cfg_page = _niusan_holder_cfg_load()
    schemes = [s for s in (cfg_page.get('schemes') or []) if isinstance(s, dict)]

    def _one(sch: dict) -> dict[str, object]:
        sn = str(sch.get('schemeName') or '').strip() or '--'
        hol = sch.get('holders')
        hol_list: list = hol if isinstance(hol, list) else []
        # 每方案独立连接，便于线程池并行
        c = pymysql.connect(**DB_CONFIG_TUSHARE)
        try:
            with c.cursor() as cur_sm:
                cnt, amt_s = _niusan_scheme_stock_count_and_amt_yi(cur_sm, hol_list)
        finally:
            try:
                c.close()
            except Exception:
                pass
        return {
            'scheme_id': str(sch.get('id') or ''),
            'scheme_name': sn,
            'stock_count': cnt,
            'amt_yi_total': amt_s,
        }

    if len(schemes) <= 1:
        for sch in schemes:
            scheme_summaries.append(_one(sch))
        return scheme_summaries

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=min(6, len(schemes))) as pool:
        futs = {pool.submit(_one, sch): sch for sch in schemes}
        by_id: dict[str, dict[str, object]] = {}
        for fut in as_completed(futs):
            item = fut.result()
            by_id[str(item.get('scheme_id') or '')] = item
    for sch in schemes:
        sid = str(sch.get('id') or '')
        if sid in by_id:
            scheme_summaries.append(by_id[sid])
    return scheme_summaries


def _prev_quarter_report_end(end_d: date) -> date:
    """自然季报告期末的上一季报告期末（3/31、6/30、9/30、12/31 链）。"""
    if end_d.month == 3 and end_d.day == 31:
        return date(end_d.year - 1, 12, 31)
    if end_d.month == 6 and end_d.day == 30:
        return date(end_d.year, 3, 31)
    if end_d.month == 9 and end_d.day == 30:
        return date(end_d.year, 6, 30)
    if end_d.month == 12 and end_d.day == 31:
        return date(end_d.year, 9, 30)
    q = (end_d.month - 1) // 3
    if q == 0:
        return date(end_d.year - 1, 12, 31)
    if q == 1:
        return date(end_d.year, 3, 31)
    if q == 2:
        return date(end_d.year, 6, 30)
    return date(end_d.year, 9, 30)


def _niusan_quarter_ends_backward_list(anchor: date, max_steps: int = 48) -> list[date]:
    """从 anchor 起连续往前推 max_steps 个季末，供批量拉 top10_floatholders。"""
    out: list[date] = [anchor]
    q = anchor
    for _ in range(max_steps):
        q = _prev_quarter_report_end(q)
        out.append(q)
    return out


def _niusan_fetch_hold_stats_batch(
    cur,
    ts_codes: list[str],
    quarter_dates: list[date],
    hw: str,
    hp: tuple[str, ...],
) -> dict[tuple[str, date], tuple[Decimal | None, int]]:
    """(ts_code, end_date) -> (SUM(hold_amount), n_chg_lines)；仅含该期确有行的组合。"""
    if not ts_codes or not quarter_dates:
        return {}
    ph_ts = ','.join(['%s'] * len(ts_codes))
    ph_d = ','.join(['%s'] * len(quarter_dates))
    cur.execute(
        f'''
        SELECT d.ts_code, d.end_date,
               SUM(d.hold_amount) AS hold_sum,
               SUM(CASE WHEN d.hold_change IS NOT NULL THEN 1 ELSE 0 END) AS n_chg_lines
        FROM (
            SELECT t.ts_code, t.end_date, t.hold_amount, t.hold_change,
                ROW_NUMBER() OVER (
                    PARTITION BY t.ts_code, t.end_date, t.holder_name
                    ORDER BY t.ann_date IS NULL ASC, t.ann_date DESC, t.hold_amount DESC
                ) AS rn
            FROM top10_floatholders t
            WHERE t.ts_code IN ({ph_ts}) AND t.end_date IN ({ph_d}) AND {hw}
        ) d
        WHERE d.rn = 1
        GROUP BY d.ts_code, d.end_date
        ''',
        tuple(ts_codes) + tuple(quarter_dates) + hp,
    )
    out: dict[tuple[str, date], tuple[Decimal | None, int]] = {}
    for row in cur.fetchall() or []:
        ts_c = row[0]
        ed = row[1]
        hs = row[2]
        nchg = int(row[3] or 0)
        if ts_c is None or ed is None:
            continue
        out[(str(ts_c).strip(), ed)] = (hs, nchg)
    return out


def _niusan_hold_chain_quarter_walk(
    ts_code: str,
    anchor: date,
    hold_stats: dict[tuple[str, date], tuple[Decimal | None, int]],
) -> list[tuple]:
    """从 anchor 按自然季往前：缺季即止；遇到新进季纳入后不再往前。返回 [(end_date, hold_sum), ...] 升序。"""
    ts_key = str(ts_code).strip()
    anchor_key = (ts_key, anchor)
    if anchor_key not in hold_stats:
        return []
    rev: list[tuple[date, Decimal | None]] = []
    h_anchor, _ = hold_stats[anchor_key]
    rev.append((anchor, h_anchor))
    q = _prev_quarter_report_end(anchor)
    for _ in range(48):
        k = (ts_key, q)
        if k not in hold_stats:
            break
        h_sum, n_chg_lines = hold_stats[k]
        rev.append((q, h_sum))
        if n_chg_lines == 0:
            break
        q = _prev_quarter_report_end(q)
    rev.reverse()
    return rev


def _niusan_quarter_trade_window(end_d: date) -> tuple[date, date]:
    """报告期末 end_date 对应季度的自然区间（含首尾），供 stk_factor_pro VWAP 聚合。"""
    y = end_d.year
    if end_d.month == 3 and end_d.day == 31:
        return date(y, 1, 1), end_d
    if end_d.month == 6 and end_d.day == 30:
        return date(y, 4, 1), end_d
    if end_d.month == 9 and end_d.day == 30:
        return date(y, 7, 1), end_d
    if end_d.month == 12 and end_d.day == 31:
        return date(y, 10, 1), end_d
    q = (end_d.month - 1) // 3
    starts = (date(y, 1, 1), date(y, 4, 1), date(y, 7, 1), date(y, 10, 1))
    return starts[q], end_d


def _niusan_quarter_vwap_decimal(cur, ts_code: str, end_d: date) -> Decimal | None:
    """当季 stk_factor_pro 量价加权 VWAP（股），供持仓 blend 的边际价格。"""
    q0, q1 = _niusan_quarter_trade_window(end_d)
    cur.execute(
        '''
        SELECT CASE WHEN SUM(vol) > 0 THEN SUM(close * vol) / SUM(vol) ELSE NULL END
        FROM stk_factor_pro
        WHERE ts_code = %s AND trade_date >= %s AND trade_date <= %s
          AND vol IS NOT NULL AND vol > 0
        ''',
        (ts_code, q0, q1),
    )
    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return Decimal(str(row[0]))


def _niusan_blended_vwap_series_decimal(
    cur,
    ts_code: str,
    chain: list[tuple],
    quarter_vwap_memo: dict[tuple[str, date], Decimal | None] | None = None,
) -> list[Decimal | None]:
    """多季连续持有时：从链上最早一季起按持仓数量变化 blend 至各报告期末的持仓加权均价。

    首季：blend = 当季量价 VWAP；后续季 delta>0 时
    blend = (H_prev*blend + delta*当季VWAP)/H_curr；delta<0（减持）时 blend 不变。
    chain: [(end_date, hold_sum), ...] 必须按 end_date 升序；hold_sum 为 top10 合并持股数（股）。
    链由 _niusan_hold_chain_quarter_walk 生成：锚定季起按自然季往前，缺季即止、新进季即止。
    """
    memo = quarter_vwap_memo if quarter_vwap_memo is not None else {}
    out: list[Decimal | None] = []
    blended: Decimal | None = None
    h_prev: Decimal | None = None
    for i, row in enumerate(chain):
        end_d = row[0]
        h_raw = row[1]
        key = (ts_code, end_d)
        if key not in memo:
            memo[key] = _niusan_quarter_vwap_decimal(cur, ts_code, end_d)
        vq = memo[key]
        if h_raw is None:
            out.append(blended)
            continue
        h_curr = Decimal(str(h_raw))
        if i == 0:
            blended = vq
            h_prev = h_curr
            out.append(blended)
            continue
        delta = h_curr - (h_prev if h_prev is not None else Decimal('0'))
        if blended is None:
            blended = vq
        elif vq is not None:
            if delta > 0 and h_curr != 0:
                blended = (h_prev * blended + delta * vq) / h_curr
            elif delta < 0:
                pass
        h_prev = h_curr
        out.append(blended)
    return out


def _niusan_rt_k_latest_close_map(ts_codes: list[str]) -> dict[str, tuple[Decimal, date]]:
    """每股 rt_k 最新一笔行情：收盘价 + 自然日（DATE(trade_time)）。读 stock_wind。"""
    if not ts_codes:
        return {}
    ph = ','.join(['%s'] * len(ts_codes))
    out: dict[str, tuple[Decimal, date]] = {}
    conn = None
    try:
        conn = pymysql.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            cur.execute(
                f'''
                SELECT r.ts_code, r.close, r.trade_time
                FROM stock_wind.rt_k r
                INNER JOIN (
                    SELECT ts_code, MAX(trade_time) AS mx
                    FROM stock_wind.rt_k
                    WHERE ts_code IN ({ph})
                    GROUP BY ts_code
                ) m ON r.ts_code = m.ts_code AND r.trade_time = m.mx
                ''',
                ts_codes,
            )
            for x in cur.fetchall() or []:
                tt = x[2]
                d = tt.date() if hasattr(tt, 'date') else tt
                out[x[0]] = (x[1], d)
    except Exception as e:
        logger.warning('牛散持股读取 rt_k 最新价失败: %s', e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return out


def _niusan_pct_display_cls(pct_str: str) -> str:
    """涨跌幅字符串着色：正 rise（红）、负 fall（绿）；0/-- 无色。"""
    if pct_str in ('', '--'):
        return ''
    try:
        v = float(pct_str)
        if v > 0:
            return 'rise'
        if v < 0:
            return 'fall'
    except Exception:
        pass
    return ''


def _query_niusan_holdings_page_rows(for_scheme_id: str | None = None):
    """牛散持股页：关联人合并为每股一行；读 stock_tushare，最新价另读 stock_wind.rt_k。

    纳入口径（每股独立）：仅当关联人仍出现在该股 top10_floatholders 最新已披露报告期；
    各自行报告期可为不同季（如一股 2026-06-30、另一股仍 2026-03-31）。
    for_scheme_id 非空时针对该配置方案；为空则用当前启用方案。
    """
    res = _niusan_resolve_holder_hw_hp(for_scheme_id)
    if not res.get('ok'):
        err = str(res.get('error') or '')
        return {
            'report_end_date': None,
            'rows': [],
            'error': err,
            'amt_yi_total': '0.00',
            'scheme_summaries': [],
        }
    hw = res['hw']
    hp = res['hp']
    conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    rows_out: list[dict[str, str]] = []
    report_end: date | None = None
    err_msg: str | None = None
    scheme_summaries_out: list[dict[str, object]] = []
    end_date_by_ts: dict[str, date] = {}
    try:
        with conn.cursor() as cur:
            agg = _niusan_fetch_holdings_agg_at_stock_latest(cur, hw, hp)
        if agg:
            rk_allow = _niusan_ts_codes_present_in_rt_k([r[0] for r in agg])
            agg = [r for r in agg if r[0] in rk_allow]
        if not agg:
            scheme_summaries_out = _niusan_scheme_summaries_for_report(conn)
            return {
                'report_end_date': None,
                'rows': [],
                'error': None,
                'amt_yi_total': '0.00',
                'scheme_summaries': scheme_summaries_out,
            }

        # agg: ts_code, end_date, hold_sum, ratio_sum, n_chg_lines, sum_hold_change, ann_date_max
        for r in agg:
            ts = str(r[0] or '').strip()
            ed = r[1]
            if ts and ed is not None:
                end_date_by_ts[ts] = ed
        report_end = max(end_date_by_ts.values()) if end_date_by_ts else None

        ts_list = [r[0] for r in agg]
        ph = ','.join(['%s'] * len(ts_list))

        sb_map: dict[str, tuple] = {}
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT ts_code, COALESCE(name,""), list_date FROM stock_basic WHERE ts_code IN ({ph})',
                ts_list,
            )
            for x in cur.fetchall() or []:
                sb_map[x[0]] = (x[1], x[2])

        sw_map: dict[str, str] = {}
        with conn.cursor() as cur:
            cur.execute(
                f'''
                SELECT z.ts_code, z.sw_name
                FROM (
                    SELECT m.ts_code, m.sw_name,
                        ROW_NUMBER() OVER (
                            PARTITION BY m.ts_code
                            ORDER BY m.in_date DESC, m.sw_name ASC
                        ) AS rn
                    FROM stock_tushare.index_member_all_sw m
                    WHERE (m.is_new = 'Y' OR m.is_new IS NULL)
                      AND m.sw_name IS NOT NULL AND m.sw_name <> ''
                      AND m.ts_code IN ({ph})
                ) z
                WHERE z.rn = 1
                ''',
                ts_list,
            )
            for x in cur.fetchall() or []:
                sw_map[x[0]] = x[1] or ''

        pairs_ts_end = [(str(r[0]).strip(), r[1]) for r in agg if r[0] and r[1] is not None]
        with conn.cursor() as cur:
            ref_close = _niusan_ref_close_map_by_ts_end(cur, pairs_ts_end)

        latest_bar: dict[str, tuple] = {}
        with conn.cursor() as cur:
            cur.execute(
                f'''
                SELECT s.ts_code, s.total_mv, s.circ_mv, s.free_share, s.close, s.trade_date
                FROM stk_factor_pro s
                INNER JOIN (
                    SELECT ts_code, MAX(trade_date) AS mx
                    FROM stk_factor_pro
                    WHERE ts_code IN ({ph})
                    GROUP BY ts_code
                ) m ON s.ts_code = m.ts_code AND s.trade_date = m.mx
                ''',
                ts_list,
            )
            for x in cur.fetchall() or []:
                latest_bar[x[0]] = (x[1], x[2], x[3], x[4], x[5])

        # 每股：以该股纳入报告期为锚，按自然季往前链再 blend VWAP
        anchor_for_quarters = report_end or max(end_date_by_ts.values())
        quarter_date_list = _niusan_quarter_ends_backward_list(anchor_for_quarters, 48)
        hold_stats_page: dict[tuple[str, date], tuple[Decimal | None, int]] = {}
        with conn.cursor() as cur:
            hold_stats_page = _niusan_fetch_hold_stats_batch(cur, ts_list, quarter_date_list, hw, hp)
        chain_by_ts: defaultdict[str, list[tuple]] = defaultdict(list)
        for ts_item in ts_list:
            ts_key = str(ts_item).strip()
            anchor = end_date_by_ts.get(ts_key)
            if anchor is None:
                continue
            chain_by_ts[ts_item] = _niusan_hold_chain_quarter_walk(ts_item, anchor, hold_stats_page)

        quarter_vwap_memo: dict[tuple[str, date], Decimal | None] = {}
        blended_last_by_ts: dict[str, Decimal | None] = {}
        with conn.cursor() as cur:
            for ts_item in ts_list:
                ch = chain_by_ts.get(ts_item, [])
                ser = _niusan_blended_vwap_series_decimal(cur, ts_item, ch, quarter_vwap_memo)
                blended_last_by_ts[ts_item] = ser[-1] if ser else None

        rt_latest_map = _niusan_rt_k_latest_close_map(ts_list)
        close_20d_map: dict[str, Decimal | None] = {}
        with conn.cursor() as cur:
            for ts in ts_list:
                rtl = rt_latest_map.get(ts)
                if rtl is None:
                    close_20d_map[ts] = None
                    continue
                _c_rt, d_ref = rtl
                cur.execute(
                    '''
                    SELECT close FROM stk_factor_pro
                    WHERE ts_code = %s AND trade_date <= %s
                    ORDER BY trade_date DESC
                    LIMIT 1 OFFSET 20
                    ''',
                    (ts, d_ref),
                )
                row20 = cur.fetchone()
                close_20d_map[ts] = row20[0] if row20 and row20[0] is not None else None

        for r in agg:
            ts_code = r[0]
            row_end = r[1]
            hold_sum = r[2]
            ratio_sum = r[3]
            n_chg_lines = int(r[4] or 0)
            sum_chg = r[5]
            ann_date_mx = r[6]

            name, list_dt = sb_map.get(ts_code, ('', None))
            sw_nm = sw_map.get(ts_code, '')

            bar = latest_bar.get(ts_code)
            total_mv_yi = ''
            free_mv_yi = ''
            if bar:
                total_mv, circ_mv, free_share, cls_latest, _td = bar
                if total_mv is not None:
                    try:
                        total_mv_yi = str(int(round(float(Decimal(str(total_mv)) / Decimal('10000')))))
                    except Exception:
                        total_mv_yi = ''
                _mv_ok = False
                if free_share is not None and cls_latest is not None:
                    try:
                        yi_val = float(Decimal(str(free_share)) * Decimal(str(cls_latest)) / Decimal('10000'))
                        free_mv_yi = str(int(round(yi_val)))
                        _mv_ok = True
                    except Exception:
                        pass
                if not _mv_ok and circ_mv is not None:
                    try:
                        yi_val = float(Decimal(str(circ_mv)) / Decimal('10000'))
                        free_mv_yi = str(int(round(yi_val)))
                    except Exception:
                        free_mv_yi = ''

            try:
                hold_wan = float(Decimal(str(hold_sum)) / Decimal('10000'))
                hold_wan_s = f'{hold_wan:.2f}'
            except Exception:
                hold_wan_s = ''

            try:
                ratio_s = f'{float(ratio_sum):.2f}' if ratio_sum is not None else ''
            except Exception:
                ratio_s = ''

            chg_cls = ''
            if n_chg_lines == 0:
                chg_disp = '新进'
            else:
                try:
                    chg_wan = float(Decimal(str(sum_chg or 0)) / Decimal('10000'))
                    chg_disp = f'{chg_wan:.2f}'
                    if chg_wan > 0:
                        chg_cls = 'rise'
                    elif chg_wan < 0:
                        chg_cls = 'fall'
                except Exception:
                    chg_disp = '--'

            rc = ref_close.get(str(ts_code).strip())
            amt_yi = ''
            if hold_sum is not None and rc is not None:
                try:
                    amt_yi = f'{float(Decimal(str(hold_sum)) * rc / Decimal("100000000")):.2f}'
                except Exception:
                    amt_yi = ''

            vw = blended_last_by_ts.get(ts_code)
            vwap_s = f'{float(vw):.2f}' if vw is not None else '--'

            pct_20d_s = '--'
            pct_vs_vwap_s = '--'
            rtl_pair = rt_latest_map.get(ts_code)
            if rtl_pair is not None:
                p_now, _dr = rtl_pair
                try:
                    base20 = close_20d_map.get(ts_code)
                    if base20 is not None and Decimal(str(base20)) != 0:
                        p20 = (
                            float(Decimal(str(p_now)) / Decimal(str(base20)) - Decimal('1'))
                            * 100.0
                        )
                        pct_20d_s = f'{p20:+.2f}'
                except Exception:
                    pct_20d_s = '--'
                try:
                    if vw is not None and Decimal(str(vw)) != 0:
                        pv = (
                            float(Decimal(str(p_now)) / Decimal(str(vw)) - Decimal('1'))
                            * 100.0
                        )
                        pct_vs_vwap_s = f'{pv:+.2f}'
                except Exception:
                    pct_vs_vwap_s = '--'

            list_s = str(list_dt) if list_dt is not None else ''
            period_s = str(row_end) if row_end is not None else '--'
            ann_s = '--'
            if ann_date_mx is not None:
                try:
                    ann_s = ann_date_mx.strftime('%Y-%m-%d')
                except Exception:
                    ann_s = str(ann_date_mx)
                if ann_s == 'None':
                    ann_s = '--'

            rows_out.append(
                {
                    'ts_code': ts_code,
                    'name': name or '--',
                    'total_mv_yi': total_mv_yi or '--',
                    'free_mv_yi': free_mv_yi or '--',
                    'sw_name': sw_nm or '--',
                    'list_date': list_s or '--',
                    'ann_date': ann_s,
                    'report_period': period_s,
                    'hold_wan': hold_wan_s or '--',
                    'hold_ratio': ratio_s or '--',
                    'hold_change': chg_disp,
                    'hold_change_cls': chg_cls,
                    'amt_yi': amt_yi or '--',
                    'vwap': vwap_s,
                    'pct_20d': pct_20d_s,
                    'pct_vs_vwap': pct_vs_vwap_s,
                    'pct_20d_cls': _niusan_pct_display_cls(pct_20d_s),
                    'pct_vs_vwap_cls': _niusan_pct_display_cls(pct_vs_vwap_s),
                }
            )
        scheme_summaries_out = _niusan_scheme_summaries_for_report(conn)
    except Exception as e:
        logger.exception('牛散持股页查询失败: %s', e)
        err_msg = str(e)
        rows_out = []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    amt_sum_dec = Decimal('0')
    for _row in rows_out:
        _s = (_row.get('amt_yi') or '').strip()
        if not _s or _s == '--':
            continue
        try:
            amt_sum_dec += Decimal(str(_s))
        except Exception:
            pass
    amt_yi_total_s = f'{float(amt_sum_dec):.2f}'
    return {
        'report_end_date': report_end,
        'rows': rows_out,
        'error': err_msg,
        'amt_yi_total': amt_yi_total_s,
        'scheme_summaries': scheme_summaries_out,
    }


def _niusan_stock_latest_anchor_for_holders(
    cur, ts_code: str, hw: str, hp: tuple
) -> date | None:
    """该股全市场最新已披露 end_date；仅当关联人仍在该期前十大时返回该日期，否则 None。"""
    ts_norm = str(ts_code or '').strip()
    if not ts_norm or not hp:
        return None
    cur.execute(
        'SELECT MAX(end_date) FROM top10_floatholders WHERE ts_code = %s',
        (ts_norm,),
    )
    mx = cur.fetchone()
    stock_latest = mx[0] if mx and mx[0] is not None else None
    if stock_latest is None:
        return None
    cur.execute(
        f'''
        SELECT 1 FROM top10_floatholders t
        WHERE t.ts_code = %s AND t.end_date = %s AND {hw}
        LIMIT 1
        ''',
        (ts_norm, stock_latest) + hp,
    )
    if cur.fetchone() is None:
        return None
    return stock_latest


def _query_niusan_floatholder_history(ts_code: str, for_scheme_id: str | None = None) -> dict:
    """单只股票：锚定该股最新已披露且关联人仍在前十大的报告期，列出更早各期合并持仓。
    for_scheme_id 与主表当前选中方案一致；空则使用启用方案（兼容旧请求）。"""
    ts_norm = (ts_code or '').strip().upper()
    if not re.match(r'^\d{6}\.(SH|SZ|BJ)$', ts_norm):
        return {'ok': False, 'error': 'invalid ts_code', 'anchor_end_date': None, 'periods': []}

    res = _niusan_resolve_holder_hw_hp(for_scheme_id)
    if not res.get('ok'):
        err = str(res.get('error') or '')
        return {'ok': False, 'error': err, 'anchor_end_date': None, 'periods': []}
    hw = res['hw']
    hp = res['hp']
    conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    periods_out: list[dict[str, str]] = []
    anchor: date | None = None
    err_msg: str | None = None
    try:
        with conn.cursor() as cur:
            anchor = _niusan_stock_latest_anchor_for_holders(cur, ts_norm, hw, hp)
        if anchor is None:
            return {'ok': True, 'error': None, 'anchor_end_date': None, 'periods': []}

        with conn.cursor() as cur:
            cur.execute(
                f'''
                SELECT d.end_date,
                       SUM(d.hold_amount) AS hold_sum,
                       SUM(d.hold_float_ratio) AS ratio_sum,
                       SUM(CASE WHEN d.hold_change IS NOT NULL THEN 1 ELSE 0 END) AS n_chg_lines,
                       SUM(d.hold_change) AS sum_hold_change,
                       MAX(d.ann_date) AS ann_date_max
                FROM (
                    SELECT t.end_date, t.hold_amount, t.hold_float_ratio, t.hold_change, t.ann_date,
                        ROW_NUMBER() OVER (
                            PARTITION BY t.ts_code, t.end_date, t.holder_name
                            ORDER BY t.ann_date IS NULL ASC, t.ann_date DESC, t.hold_amount DESC
                        ) AS rn
                    FROM top10_floatholders t
                    WHERE t.ts_code = %s AND t.end_date < %s AND {hw}
                ) d
                WHERE d.rn = 1
                GROUP BY d.end_date
                ORDER BY d.end_date DESC
                ''',
                (ts_norm, anchor) + hp,
            )
            hist = cur.fetchall() or []

        name_s = '--'
        list_s = '--'
        sw_nm = '--'
        with conn.cursor() as cur:
            cur.execute(
                'SELECT COALESCE(name,""), list_date FROM stock_basic WHERE ts_code = %s LIMIT 1',
                (ts_norm,),
            )
            sb = cur.fetchone()
            if sb:
                name_s = sb[0] or '--'
                list_s = str(sb[1]) if sb[1] is not None else '--'
            cur.execute(
                '''
                SELECT z.sw_name
                FROM (
                    SELECT m.ts_code, m.sw_name,
                        ROW_NUMBER() OVER (
                            PARTITION BY m.ts_code
                            ORDER BY m.in_date DESC, m.sw_name ASC
                        ) AS rn
                    FROM stock_tushare.index_member_all_sw m
                    WHERE (m.is_new = 'Y' OR m.is_new IS NULL)
                      AND m.sw_name IS NOT NULL AND m.sw_name <> ''
                      AND m.ts_code = %s
                ) z
                WHERE z.rn = 1
                ''',
                (ts_norm,),
            )
            swr = cur.fetchone()
            if swr and swr[0]:
                sw_nm = swr[0]

        vwap_blend_at_date: dict[date, Decimal | None] = {}
        quarter_date_hist = _niusan_quarter_ends_backward_list(anchor, 48)
        hold_stats_hist: dict[tuple[str, date], tuple[Decimal | None, int]] = {}
        with conn.cursor() as cur:
            hold_stats_hist = _niusan_fetch_hold_stats_batch(cur, [ts_norm], quarter_date_hist, hw, hp)
            memo_h: dict[tuple[str, date], Decimal | None] = {}
            for r in hist:
                end_d_hist = r[0]
                chain_hist = _niusan_hold_chain_quarter_walk(ts_norm, end_d_hist, hold_stats_hist)
                ser_hist = _niusan_blended_vwap_series_decimal(cur, ts_norm, chain_hist, memo_h)
                if chain_hist and ser_hist:
                    vwap_blend_at_date[end_d_hist] = ser_hist[-1]

        with conn.cursor() as cur:
            for r in hist:
                end_d = r[0]
                hold_sum = r[1]
                ratio_sum = r[2]
                n_chg_lines = int(r[3] or 0)
                sum_chg = r[4]
                ann_hist = r[5]
                ann_hist_s = '--'
                if ann_hist is not None:
                    try:
                        ann_hist_s = ann_hist.strftime('%Y-%m-%d')
                    except Exception:
                        ann_hist_s = str(ann_hist)
                    if ann_hist_s == 'None':
                        ann_hist_s = '--'

                cur.execute(
                    '''
                    SELECT close FROM stk_factor_pro
                    WHERE ts_code = %s AND trade_date <= %s
                    ORDER BY trade_date DESC LIMIT 1
                    ''',
                    (ts_norm, end_d),
                )
                rc_row = cur.fetchone()
                rc = rc_row[0] if rc_row and rc_row[0] is not None else None

                try:
                    hold_wan = float(Decimal(str(hold_sum)) / Decimal('10000'))
                    hold_wan_s = f'{hold_wan:.2f}'
                except Exception:
                    hold_wan_s = ''

                try:
                    ratio_s = f'{float(ratio_sum):.2f}' if ratio_sum is not None else ''
                except Exception:
                    ratio_s = ''

                chg_cls = ''
                if n_chg_lines == 0:
                    chg_disp = '新进'
                else:
                    try:
                        chg_wan = float(Decimal(str(sum_chg or 0)) / Decimal('10000'))
                        chg_disp = f'{chg_wan:.2f}'
                        if chg_wan > 0:
                            chg_cls = 'rise'
                        elif chg_wan < 0:
                            chg_cls = 'fall'
                    except Exception:
                        chg_disp = '--'

                amt_yi = ''
                if hold_sum is not None and rc is not None:
                    try:
                        amt_yi = f'{float(Decimal(str(hold_sum)) * Decimal(str(rc)) / Decimal("100000000")):.2f}'
                    except Exception:
                        amt_yi = ''

                vw_bl = vwap_blend_at_date.get(end_d) if end_d is not None else None
                vwap_s = f'{float(vw_bl):.2f}' if vw_bl is not None else '--'

                period_s = str(end_d) if end_d is not None else '--'

                periods_out.append(
                    {
                        'ts_code': ts_norm,
                        'name': name_s,
                        'list_date': list_s,
                        'total_mv_yi': '--',
                        'free_mv_yi': '--',
                        'sw_name': sw_nm or '--',
                        'hold_wan': hold_wan_s or '--',
                        'hold_ratio': ratio_s or '--',
                        'hold_change': chg_disp,
                        'hold_change_cls': chg_cls,
                        'amt_yi': amt_yi or '--',
                        'vwap': vwap_s,
                        'pct_20d': '--',
                        'pct_vs_vwap': '--',
                        'pct_20d_cls': '',
                        'pct_vs_vwap_cls': '',
                        'ann_date': ann_hist_s,
                        'report_period': period_s,
                    }
                )
    except Exception as e:
        logger.exception('牛散历史持仓查询失败: %s', e)
        err_msg = str(e)
        periods_out = []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if err_msg:
        return {
            'ok': False,
            'error': err_msg,
            'anchor_end_date': str(anchor) if anchor else None,
            'periods': [],
        }
    return {
        'ok': True,
        'error': None,
        'anchor_end_date': str(anchor) if anchor else None,
        'periods': periods_out,
    }


def _query_niusan_floatholder_detail(ts_code: str, for_scheme_id: str | None = None) -> dict:
    """该股最新已披露且关联人仍在前十大的报告期下：各关联人明细 + 合并口径 VWAP。
    for_scheme_id 与当前主表方案一致；空则启用方案。"""
    ts_norm = (ts_code or '').strip().upper()
    if not re.match(r'^\d{6}\.(SH|SZ|BJ)$', ts_norm):
        return {'ok': False, 'error': 'invalid ts_code'}
    res = _niusan_resolve_holder_hw_hp(for_scheme_id)
    if not res.get('ok'):
        err = str(res.get('error') or '')
        return {'ok': False, 'error': err}
    hw = res['hw']
    hp = res['hp']
    conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    rows_out: list[dict[str, str]] = []
    err_msg: str | None = None
    try:
        with conn.cursor() as cur:
            report_end = _niusan_stock_latest_anchor_for_holders(cur, ts_norm, hw, hp)
        if report_end is None:
            return {'ok': False, 'error': 'no_anchor'}

        quarter_date_list = _niusan_quarter_ends_backward_list(report_end, 48)
        vwap_s = '--'
        name_s = '--'
        with conn.cursor() as cur:
            hold_stats = _niusan_fetch_hold_stats_batch(cur, [ts_norm], quarter_date_list, hw, hp)
            chain = _niusan_hold_chain_quarter_walk(ts_norm, report_end, hold_stats)
            memo_v: dict[tuple[str, date], Decimal | None] = {}
            ser = _niusan_blended_vwap_series_decimal(cur, ts_norm, chain, memo_v)
            vw_last = ser[-1] if ser else None
            if vw_last is not None:
                vwap_s = f'{float(vw_last):.2f}'

            # 同一报告期、同一股东名可能多条（不同 ann_date 修订/重复入库）；明细弹窗按股东去重，保留最新公告日一行
            cur.execute(
                f'''
                SELECT holder_name, hold_amount, hold_float_ratio, hold_change
                FROM (
                    SELECT holder_name, hold_amount, hold_float_ratio, hold_change,
                        ROW_NUMBER() OVER (
                            PARTITION BY holder_name
                            ORDER BY ann_date IS NULL ASC, ann_date DESC, hold_amount DESC
                        ) AS rn
                    FROM top10_floatholders t
                    WHERE t.ts_code = %s AND t.end_date = %s AND {hw}
                ) z
                WHERE z.rn = 1
                ORDER BY holder_name ASC
                ''',
                (ts_norm, report_end) + hp,
            )
            detail_rows = cur.fetchall() or []

            cur.execute(
                '''
                SELECT close FROM stk_factor_pro
                WHERE ts_code = %s AND trade_date <= %s
                ORDER BY trade_date DESC LIMIT 1
                ''',
                (ts_norm, report_end),
            )
            rc_row = cur.fetchone()
            rc = rc_row[0] if rc_row and rc_row[0] is not None else None

            cur.execute(
                'SELECT COALESCE(name,"") FROM stock_basic WHERE ts_code = %s LIMIT 1',
                (ts_norm,),
            )
            nm_row = cur.fetchone()
            if nm_row and nm_row[0]:
                name_s = nm_row[0] or '--'

        scheme_summaries = _niusan_scheme_summaries_for_report(conn)

        for dr in detail_rows:
            hn = dr[0] if dr[0] is not None else '--'
            ham = dr[1]
            hfr = dr[2]
            hc = dr[3]
            try:
                hold_wan_s = (
                    f'{float(Decimal(str(ham)) / Decimal("10000")):.2f}'
                    if ham is not None
                    else '--'
                )
            except Exception:
                hold_wan_s = '--'
            try:
                ratio_s = f'{float(hfr):.2f}' if hfr is not None else '--'
            except Exception:
                ratio_s = '--'
            chg_cls = ''
            if hc is None:
                chg_disp = '新进'
            else:
                try:
                    chg_wan = float(Decimal(str(hc)) / Decimal('10000'))
                    chg_disp = f'{chg_wan:.2f}'
                    if chg_wan > 0:
                        chg_cls = 'rise'
                    elif chg_wan < 0:
                        chg_cls = 'fall'
                except Exception:
                    chg_disp = '--'
            amt_yi = '--'
            if ham is not None and rc is not None:
                try:
                    amt_yi = f'{float(Decimal(str(ham)) * Decimal(str(rc)) / Decimal("100000000")):.2f}'
                except Exception:
                    amt_yi = '--'
            rows_out.append(
                {
                    'holder_name': str(hn),
                    'hold_wan': hold_wan_s,
                    'hold_ratio': ratio_s,
                    'hold_change': chg_disp,
                    'hold_change_cls': chg_cls,
                    'amt_yi': amt_yi,
                    'vwap': vwap_s,
                }
            )

        return {
            'ok': True,
            'error': None,
            'ts_code': ts_norm,
            'name': name_s,
            'report_end': str(report_end),
            'vwap_note': '合并口径持仓加权均价（该股关联人持股合计与主表一致）',
            'scheme_summaries': scheme_summaries,
            'rows': rows_out,
        }
    except Exception as e:
        logger.exception('牛散持股明细查询失败: %s', e)
        err_msg = str(e)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {'ok': False, 'error': err_msg or 'unknown', 'rows': [], 'scheme_summaries': []}


def _render_rt_monitor_niusan_no_cache():
    """牛散页壳：不在 SSR 打重查询；主表/方案条由前端拉 /api/niusan_scheme_holdings。"""
    sch = _get_active_niusan_scheme()
    scheme_title = str(sch.get('schemeName') or '').strip() or '--'
    holders_for_display = sch.get('holders') if isinstance(sch, dict) else None
    if isinstance(holders_for_display, list):
        related_join = ' / '.join(
            str(h).strip() for h in holders_for_display if isinstance(h, str) and h.strip()
        )
    else:
        related_join = ''
    active_scheme_id = str(sch.get('id') or '') if isinstance(sch, dict) else ''
    holder_cfg_embed = _niusan_holder_cfg_load()
    resp = make_response(
        render_template(
            'rt_monitor_niusan.html',
            niusan_rows=[],
            niusan_error=None,
            niusan_amt_yi_total='--',
            niusan_scheme_title=scheme_title,
            niusan_related_join=related_join,
            niusan_scheme_summaries=[],
            niusan_active_scheme_id=active_scheme_id,
            niusan_holder_cfg_embed=holder_cfg_embed,
            niusan_async_boot=True,
        )
    )
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['X-RT-Monitor-Build'] = RT_MONITOR_STATIC_VER
    resp.headers['X-RT-Monitor-App'] = 'rt_monitor_app'
    return resp


def warmupNiusanCacheAsync() -> None:
    """启动后台预热各方案持股缓存（先 active，再其余）。"""

    def _worker() -> None:
        try:
            time.sleep(2.0)
            cfg = _niusan_holder_cfg_load()
            schemes = [
                s for s in (cfg.get('schemes') or []) if isinstance(s, dict) and s.get('id')
            ]
            activeId = str(cfg.get('activeId') or '').strip()
            ordered: list[str] = []
            if activeId:
                ordered.append(activeId)
            for s in schemes:
                sid = str(s.get('id') or '').strip()
                if sid and sid not in ordered:
                    ordered.append(sid)
            for sid in ordered:
                t0 = time.perf_counter()
                payload = queryNiusanHoldingsCached(sid)
                n = len(payload.get('rows') or [])
                err = payload.get('error')
                logger.info(
                    '牛散持股缓存已预热：scheme=%s rows=%s err=%s elapsed=%.2fs',
                    sid,
                    n,
                    err or '-',
                    time.perf_counter() - t0,
                )
        except Exception as e:
            logger.warning('牛散持股预热失败（忽略不影响启动）: %s', e)

    threading.Thread(target=_worker, daemon=True, name='warmup-niusan-holdings').start()


# 代码更新时间：2026-06-23 06:01
# 代码更新时间：2026-07-30 16:28 主表/方案卡按每股最新已披露前十大纳入（多报告期并存）
# 代码更新时间：2026-07-30 16:32 方案汇总并行 + 聚合 SQL 拆步加速
# 代码更新时间：2026-08-06 22:41 页面出壳+持股 API TTL 缓存/启动预热
