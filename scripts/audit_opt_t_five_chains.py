# -*- coding: utf-8 -*-
"""五链 T 型评估口径独立核查：盘口 → 手算 → 对照 seller_metrics / payload 展示。"""
from __future__ import annotations
# 用途: 标定
# 保留至: 2026-10-20

from pathlib import Path
import sys

# 允许从任意工作目录直接执行本脚本，避免依赖调用方设置 PYTHONPATH=.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from datetime import date
from typing import Any

import pymysql

from rt_monitor.core.config import DB_CONFIG_TUSHARE
from rt_monitor.services.opt_t_board import (
    _OPT_T_BOARD_ETF_CODES,
    _fetch_opt_daily_latest_map,
    _fmt_opt_last_px,
    _fmt_opt_pct,
    _optQuoteBuyPx,
    _optQuoteLastPx,
    _optQuoteSellPx,
    _optTBoardFillBuckets,
    _optTBoardFillDisplayBuckets,
    _optTBoardPerUnit,
    _optTBoardRowIsDisplay,
    _optTBoardSortDisplayKeys,
    _opt_t_board_days_to_expiry,
    _opt_t_board_expiry_kind_map,
    _opt_t_board_fetch_underlying_close,
    _opt_t_board_parse_date,
    _opt_t_board_pick_atm_by_adj,
    _opt_t_board_section_forward,
    _opt_t_board_section_quote_asof,
    _opt_t_board_seller_metrics,
    _query_opt_t_board_payload,
    fetchDaqmtQuoteSnapshot,
    mergeQmtChainGaps,
    overlayDaqmtQuotes,
)

CODES = list(_OPT_T_BOARD_ETF_CODES)


def handCalc(
    kind: str,
    sell: float | None,
    buy: float | None,
    strike: float,
    spot: float | None,
    dte: int | None,
) -> dict[str, Any] | None:
    """不调用 seller_metrics 的手算期望。"""
    if kind not in ('C', 'P') or spot is None or spot <= 0:
        return None
    intrinsic = max(spot - strike, 0.0) if kind == 'C' else max(strike - spot, 0.0)
    buyTv = (buy - intrinsic) if buy is not None else None
    if buyTv is not None and buyTv < 0:
        mode, px = 'buy', buy
    elif sell is not None:
        mode, px = 'sell', sell
    elif buy is not None:
        mode, px = 'buy', buy
    else:
        return None
    tv = px - intrinsic
    eff = (strike + px) if kind == 'C' else (strike - px)
    if kind == 'C':
        disc = (eff - spot) / spot * 100.0
        prem = (px / (spot - px) * 100.0) if (spot - px) > 1e-12 else None
    else:
        disc = (spot - eff) / spot * 100.0
        prem = (px / (strike - px) * 100.0) if (strike - px) > 1e-12 else None
    tvAnn = None
    if tv > 0 and strike > 0 and dte and dte > 0:
        tvAnn = (tv / strike) * 100.0 * (365.0 / dte)
    return {
        'mode': mode,
        'px': px,
        'tv_s': _fmt_opt_last_px(tv),
        'eff_s': _fmt_opt_last_px(eff),
        'disc_s': _fmt_opt_pct(disc, digits=2),
        'prem_s': _fmt_opt_pct(prem, digits=2) if prem is not None else '—',
        'tv_ann_s': _fmt_opt_pct(tvAnn, digits=2) if tvAnn is not None else '—',
    }


def auditOne(u: str, snap: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        'u': u,
        'ok': True,
        'spot_daily': None,
        'spot_display': None,
        'quote_source': None,
        'live_n': 0,
        'book_n': 0,
        'rows': 0,
        'checked': 0,
        'mismatches': [],
        'mode_counts': {'buy': 0, 'sell': 0, 'none': 0},
        'sections': [],
        'samples': [],
        'err': None,
        'gap_n': 0,
        'crossing_ok': 0,
        'discount_ok': 0,
    }
    # 与板面同一快照，避免两次拉盘口造成现货漂移误报
    if snap is None:
        snap = fetchDaqmtQuoteSnapshot()
    try:
        payload = _query_opt_t_board_payload(u, snapshot=snap)
    except Exception as e:
        out['ok'] = False
        out['err'] = f'payload:{e}'
        return out

    raw_rows: list[dict] = []
    iv_rows: list[dict] = []
    listedTs: set[str] = set()
    quote_map: dict[str, dict[str, Any]] = {}
    underlying_close = None
    perUnit = _optTBoardPerUnit(u)
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE, cursorclass=pymysql.cursors.DictCursor)
        try:
            with conn.cursor() as cur:
                underlying_close = _opt_t_board_fetch_underlying_close(cur, u)
                out['spot_daily'] = underlying_close
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
                    (f'%{u}%',),
                )
                allCp = list(cur.fetchall() or [])
                raw_rows = [r for r in allCp if _optTBoardRowIsDisplay(r, perUnit)]
                iv_rows = list(allCp)
                ts_list = sorted(
                    {str(r['ts_code']).strip() for r in iv_rows if r.get('ts_code')}
                )
                quote_map = _fetch_opt_daily_latest_map(cur, ts_list)
                cur.execute(
                    """
                    SELECT ts_code FROM opt_basic
                    WHERE opt_code LIKE %s
                      AND delist_date IS NOT NULL AND delist_date >= CURDATE()
                    """,
                    (f'%{u}%',),
                )
                listedTs = {
                    str(r['ts_code']).strip()
                    for r in (cur.fetchall() or [])
                    if r.get('ts_code')
                }
        finally:
            conn.close()
    except Exception as e:
        out['ok'] = False
        out['err'] = f'db:{e}'
        return out

    raw_rows, gapN = mergeQmtChainGaps(raw_rows, listedTs, snap, u, perUnit)
    quote_map, underlying_close, liveN, quoteSource = overlayDaqmtQuotes(
        quote_map,
        underlying_close,
        u,
        snap,
        asof=date.today(),
    )
    out['spot_display'] = underlying_close
    out['quote_source'] = quoteSource
    out['live_n'] = liveN
    out['gap_n'] = gapN
    out['book_n'] = sum(1 for q in quote_map.values() if q.get('bid1') or q.get('ask1'))

    displaySpot = underlying_close
    dailySpot = out['spot_daily']
    groups = _optTBoardFillDisplayBuckets(raw_rows, quote_map)
    ivGroups = _optTBoardFillBuckets(iv_rows, quote_map, preferLiquid=True)
    expiry_kind_by_md = _opt_t_board_expiry_kind_map(
        [k[1] for k in groups.keys()],
        opt_underlying=u,
    )
    payload_secs = list(payload.get('sections') or [])
    sec_idx = 0

    for sm, md_s in sorted(groups.keys(), key=lambda k: (k[0], k[1])):
        g = groups[(sm, md_s)]
        calls, puts = g['calls'], g['puts']
        rowKeys = _optTBoardSortDisplayKeys(list(set(calls.keys()) | set(puts.keys())))
        quote_asof = _opt_t_board_section_quote_asof(calls, puts, quote_map)
        quoteAsofDate = _opt_t_board_parse_date(quote_asof) if quote_asof else None
        try:
            from option_iv import getCnGb10yRate

            rate = float(getCnGb10yRate(asof=quoteAsofDate))
        except Exception:
            rate = 0.015
        md_date = _opt_t_board_parse_date(md_s)
        dteDays = _opt_t_board_days_to_expiry(md_date, asof=quoteAsofDate)
        ivG = ivGroups.get((sm, md_s)) or {'calls': {}, 'puts': {}}
        sectionHasLive = any(
            (quote_map.get(str(row.get('ts_code') or '').strip()) or {}).get('live')
            for bucket in (ivG['calls'], ivG['puts'])
            for row in bucket.values()
        )
        fwdSpot = displaySpot if sectionHasLive else dailySpot
        sectionForward = _opt_t_board_section_forward(
            strikes=sorted(set(ivG['calls'].keys()) | set(ivG['puts'].keys())),
            calls=ivG['calls'],
            puts=ivG['puts'],
            quote_map=quote_map,
            spot=fwdSpot,
            daysToExpiry=dteDays,
            rate=rate,
        )
        atmByAdj = _opt_t_board_pick_atm_by_adj(rowKeys, displaySpot)
        psec = payload_secs[sec_idx] if sec_idx < len(payload_secs) else None
        sec_idx += 1
        prow_by_strike: dict[float, dict] = {}
        if psec:
            for pr in psec.get('rows') or []:
                prow_by_strike[float(pr['strike'])] = pr

        buy_n = sell_n = 0
        sec_mism = 0
        atm_samples: list[dict] = []
        for dKey in rowKeys:
            stk, _rowUnit, isAdjFlag = dKey
            rc, rp = calls.get(dKey), puts.get(dKey)
            qc = quote_map.get(str(rc['ts_code']).strip()) if rc else None
            qp = quote_map.get(str(rp['ts_code']).strip()) if rp else None
            out['rows'] += 1
            pr = prow_by_strike.get(float(stk))

            for side, q, kind, prefix in (
                (rc, qc, 'C', 'call'),
                (rp, qp, 'P', 'put'),
            ):
                if not side or not q:
                    continue
                # 与板面一致：仅 live 用叠价现货，灰字用日线现货
                spotUse = displaySpot if q.get('live') else dailySpot
                sell = _optQuoteSellPx(q)
                buy = _optQuoteBuyPx(q)
                last = _optQuoteLastPx(q)
                exp = handCalc(kind, sell, buy, float(stk), spotUse, dteDays)
                if exp is None:
                    out['mode_counts']['none'] += 1
                    continue
                out['mode_counts'][exp['mode']] += 1
                m = _opt_t_board_seller_metrics(
                    sellPx=sell,
                    buyPx=buy,
                    strike=float(stk),
                    spot=spotUse,
                    forward=sectionForward,
                    callPut=kind,
                    daysToExpiry=dteDays,
                    rate=rate,
                )
                out['checked'] += 1
                diffs: list[str] = []
                if m['action_mode'] != exp['mode'] or abs(
                    (m['action_px'] or 0) - exp['px']
                ) > 1e-9:
                    diffs.append(
                        f"mode/px {m['action_mode']}:{m['action_px']} "
                        f"vs {exp['mode']}:{exp['px']}"
                    )
                if m['time_val'] != exp['tv_s']:
                    diffs.append(f"tv {m['time_val']}!={exp['tv_s']}")
                if m['eff_px'] != exp['eff_s']:
                    diffs.append(f"eff {m['eff_px']}!={exp['eff_s']}")
                if m['disc_prem_pct'] != exp['disc_s']:
                    diffs.append(f"disc {m['disc_prem_pct']}!={exp['disc_s']}")
                if m['prem_pct'] != exp['prem_s']:
                    diffs.append(f"prem {m['prem_pct']}!={exp['prem_s']}")
                if m['tv_ann'] != exp['tv_ann_s']:
                    diffs.append(f"tvAnn {m['tv_ann']}!={exp['tv_ann_s']}")

                if pr is not None:
                    lastDisp = pr.get(f'{prefix}_close')
                    lastExp = _fmt_opt_last_px(last) if last is not None else '—'
                    if lastDisp != lastExp:
                        diffs.append(
                            f"latestCol {lastDisp}!={lastExp} (bid={sell} ask={buy})"
                        )
                    for fld, key in (
                        ('time_val', f'{prefix}_time_val'),
                        ('eff_px', f'{prefix}_eff_px'),
                        ('disc_prem_pct', f'{prefix}_disc_prem_pct'),
                        ('prem_pct', f'{prefix}_prem_pct'),
                        ('tv_ann', f'{prefix}_tv_ann'),
                    ):
                        if pr.get(key) != m[fld]:
                            diffs.append(
                                f"payload {key}={pr.get(key)} != metrics {m[fld]}"
                            )
                    if pr.get(f'{prefix}_action_mode') != m['action_mode']:
                        diffs.append(
                            f"payload mode {pr.get(f'{prefix}_action_mode')}!"
                            f"={m['action_mode']}"
                        )

                if sell is not None and buy is not None and spotUse:
                    intrinsic = (
                        max(spotUse - float(stk), 0.0)
                        if kind == 'C'
                        else max(float(stk) - spotUse, 0.0)
                    )
                    if (sell - intrinsic) < 0 <= (buy - intrinsic):
                        if exp['mode'] != 'sell':
                            diffs.append('CROSSING_ZONE_MUST_SELL')
                        else:
                            out['crossing_ok'] += 1
                    if (buy - intrinsic) < 0:
                        if exp['mode'] != 'buy':
                            diffs.append('TRUE_DISCOUNT_MUST_BUY')
                        else:
                            out['discount_ok'] += 1

                if diffs:
                    out['ok'] = False
                    sec_mism += 1
                    if len(out['mismatches']) < 40:
                        out['mismatches'].append(
                            {
                                'u': u,
                                'md': md_s,
                                'K': stk,
                                'cp': kind,
                                'bid': sell,
                                'ask': buy,
                                'last': last,
                                'spot': spotUse,
                                'diffs': diffs,
                            }
                        )

                atm = atmByAdj[1 if isAdjFlag else 0]
                if atm is not None and abs(stk - atm) < 1e-9 and len(atm_samples) < 4:
                    atm_samples.append(
                        {
                            'cp': kind,
                            'K': stk,
                            'mode': exp['mode'],
                            'px': exp['px'],
                            'tv': exp['tv_s'],
                            'eff': exp['eff_s'],
                            'disc': exp['disc_s'],
                            'prem': exp['prem_s'],
                            'last': _fmt_opt_last_px(last) if last else None,
                            'bid': sell,
                            'ask': buy,
                        }
                    )
                if exp['mode'] == 'buy':
                    buy_n += 1
                else:
                    sell_n += 1

        out['sections'].append(
            {
                'md': md_s,
                'kind': expiry_kind_by_md.get(md_s, ''),
                'dte': dteDays,
                'rows': len(rowKeys),
                'buy': buy_n,
                'sell': sell_n,
                'mism': sec_mism,
                'asof': quote_asof,
                'fwd': sectionForward,
            }
        )
        if atm_samples and len(out['samples']) < 3:
            out['samples'].append(
                {'md': md_s, 'spot': displaySpot, 'legs': atm_samples}
            )

    return out


def main() -> int:
    print('asof', date.today().isoformat(), 'codes', CODES)
    snap = fetchDaqmtQuoteSnapshot()
    if snap is None:
        print('snap None')
    else:
        print(
            'snap',
            f"keys={len(snap)} opt={len(snap.get('opt') or {})} "
            f"etf={len(snap.get('etf') or {})}",
        )
    results = []
    for u in CODES:
        r = auditOne(u, snap=snap)
        results.append(r)
        flag = 'PASS' if r['ok'] else 'FAIL'
        print(
            f"\n=== {u} [{flag}] src={r.get('quote_source')} live={r.get('live_n')} "
            f"book={r.get('book_n')} checked={r.get('checked')} "
            f"modes={r.get('mode_counts')} crossOK={r.get('crossing_ok')} "
            f"discOK={r.get('discount_ok')} spotD={r.get('spot_daily')} "
            f"spotX={r.get('spot_display')}"
        )
        if r.get('err'):
            print(' ERR', r['err'])
        for s in r.get('sections') or []:
            print(
                f"  sec {s['md']} {s['kind']} dte={s['dte']} rows={s['rows']} "
                f"sell={s['sell']} buy={s['buy']} mism={s['mism']} asof={s['asof']}"
            )
        for sm in r.get('samples') or []:
            print(f"  ATM@{sm['md']} spot={sm['spot']}")
            for leg in sm['legs']:
                print(
                    f"    {leg['cp']} K={leg['K']} mode={leg['mode']} C={leg['px']} "
                    f"tv={leg['tv']} eff={leg['eff']} disc={leg['disc']} "
                    f"prem={leg['prem']} last={leg['last']} "
                    f"bid={leg['bid']} ask={leg['ask']}"
                )
        for m in (r.get('mismatches') or [])[:8]:
            print('  MISMATCH', m)

    nFail = sum(1 for r in results if not r['ok'])
    print(
        f"\nSUMMARY chains={len(results)} fail={nFail} "
        f"checked={sum(r['checked'] for r in results)}"
    )
    return 0 if nFail == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())

# 代码更新时间：2026-09-16 11:04
