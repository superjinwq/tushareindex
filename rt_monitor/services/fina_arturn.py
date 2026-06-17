"""财报筛选域：页面与 arturn / 固定资产序列 API。"""
from __future__ import annotations

import threading
import time

import pymysql
from logger_config import get_logger

from rt_monitor.core.config import DB_CONFIG_TUSHARE
from rt_monitor.core.config import DB_CONFIG
from rt_monitor.core.page_render import renderRtMonitorPageNoCache
from rt_monitor.services import fina_arturn_queries as finaQ

logger = get_logger(__name__)


def renderFinaPageNoCache():
    return renderRtMonitorPageNoCache('rt_monitor_fina.html')


def queryFinaArturnDaysTable(endDateRaw: str | None) -> tuple[dict, int]:
    """应收账款周转天数主表；返回 (payload, http_status)。"""
    from rt_monitor.monolith import _normalize_end_date_yyyymmdd

    t0 = time.perf_counter()
    try:
        requestedEndDate = _normalize_end_date_yyyymmdd(endDateRaw)
        endDates = finaQ._query_arturn_end_dates(limit_n=120)
        if not endDates:
            return {'rows': [], 'periods': [], 'end_dates': [], 'error': '未找到可用报告期'}, 200
        selectedEndDate = (
            requestedEndDate if requestedEndDate in endDates else endDates[0]
        )
        cacheKey = ('arturn_days', selectedEndDate, 'sb_act_7')
        payload = finaQ._small_ttl_cache_get(
            finaQ._fina_indicator_metric_table_cache, cacheKey, time.time()
        )
        if payload is None:
            payload = finaQ._query_fina_arturn_table(selectedEndDate)
            if not payload.get('error'):
                finaQ._small_ttl_cache_put(
                    finaQ._fina_indicator_metric_table_cache,
                    cacheKey,
                    payload,
                    finaQ.FINA_ARTURN_TABLE_CACHE_TTL,
                    finaQ.FINA_ARTURN_TABLE_CACHE_MAX,
                )
        payload['selected_end_date'] = selectedEndDate
        payload['end_dates'] = endDates
        finaQ._log_slow_api(
            'fina_arturn_days_table',
            t0,
            f"selected={selectedEndDate} rows={len(payload.get('rows') or [])}",
        )
        return payload, 200
    except Exception as e:
        logger.exception("fina_arturn_days_table 接口异常: %s", e)
        return {'rows': [], 'periods': [], 'end_dates': [], 'error': str(e)}, 500


def queryFinaFixedAssetsRevenueSeries(
    tsCode: str,
    endDateRaw: str | None,
    limitRaw: str | None,
) -> tuple[dict, int]:
    """财报固定资产弹窗序列。"""
    from rt_monitor.monolith import _normalize_end_date_yyyymmdd

    tsCode = str(tsCode or '').strip()
    if not tsCode:
        return {'rows': [], 'error': '缺少 ts_code'}, 400
    reqEnd = _normalize_end_date_yyyymmdd(endDateRaw)
    if not reqEnd:
        endDates = finaQ._query_arturn_end_dates(limit_n=120)
        if not endDates:
            return {'rows': [], 'error': '未找到可用报告期'}, 200
        reqEnd = endDates[0]
    try:
        reqLimit = int(limitRaw if limitRaw is not None else '25')
    except (TypeError, ValueError):
        reqLimit = 25
    payload = finaQ._query_fina_fixed_assets_revenue_series(tsCode, reqEnd, reqLimit)
    payload['selected_end_date'] = reqEnd
    if payload.get('error'):
        return payload, 500
    return payload, 200


def queryFinaStockBasicAct(tsCode: str) -> tuple[dict, int]:
    """补拉 stock_basic.act_name / act_ent_type。"""
    tsCode = str(tsCode or '').strip()
    if not tsCode:
        return {'error': '缺少 ts_code'}, 400
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    except Exception as e:
        logger.exception('连接 stock_tushare 失败（fina_stock_basic_act）: %s', e)
        return {'error': '数据库连接失败'}, 500
    try:
        sb = finaQ._query_stock_basic_sec_list_act(conn, tsCode)
        return {
            'ts_code': tsCode,
            'act_name': sb['act_name'] or None,
            'act_ent_type': sb['act_ent_type'] or None,
        }, 200
    finally:
        conn.close()


def warmupFinaArturnCacheAsync() -> None:
    """启动后台预热财报 arturn 缓存（daemon 线程）。"""

    def worker() -> None:
        try:
            time.sleep(1.0)
            endDates = finaQ._query_arturn_end_dates(limit_n=120)
            if not endDates:
                return
            nowTs = time.time()
            n = max(0, int(finaQ.FINA_ARTURN_WARMUP_PREFETCH_N))
            for idx in range(min(n, len(endDates))):
                selected = endDates[idx]
                cacheKey = ('arturn_days', selected, 'sb_act_7')
                if (
                    finaQ._small_ttl_cache_get(
                        finaQ._fina_indicator_metric_table_cache, cacheKey, nowTs
                    )
                    is not None
                ):
                    continue
                payload = finaQ._query_fina_arturn_table(selected)
                if payload and not payload.get('error'):
                    finaQ._small_ttl_cache_put(
                        finaQ._fina_indicator_metric_table_cache,
                        cacheKey,
                        payload,
                        finaQ.FINA_ARTURN_TABLE_CACHE_TTL,
                        finaQ.FINA_ARTURN_TABLE_CACHE_MAX,
                    )
                    logger.info(
                        '财报 arturn 缓存已预热 [%d/%d]：end_date=%s rows=%d',
                        idx + 1,
                        min(n, len(endDates)),
                        selected,
                        len(payload.get('rows') or []),
                    )
        except Exception as e:
            logger.warning('财报 arturn 预热失败（忽略不影响启动）: %s', e)

    threading.Thread(target=worker, daemon=True, name='warmup-fina-arturn').start()


# 季度工具（供 rt_monitor_app / roic 脚本 re-export）
finaEndDateStrFromYq = finaQ._fina_end_date_str_from_yq
finaPrevYq = finaQ._fina_prev_yq
finaQuarterSingleRevenue = finaQ._fina_quarter_single_revenue


def _finaSqlDateFromYyyymmdd(endYyyymmdd: str) -> str | None:
    from rt_monitor.monolith import _normalize_end_date_yyyymmdd

    z = _normalize_end_date_yyyymmdd(endYyyymmdd)
    if not z or len(z) != 8:
        return None
    return f'{z[:4]}-{z[4:6]}-{z[6:8]}'


def _mysqlDateObjToYyyymmdd(d: object) -> str | None:
    if d is None:
        return None
    if hasattr(d, 'strftime'):
        try:
            return d.strftime('%Y%m%d')
        except Exception:
            pass
    s = str(d).strip().replace('-', '')
    return s[:8] if len(s) >= 8 else None


def _top10FloatholdersSumHoldRatio(rows: list[dict]) -> float:
    s = 0.0
    for r in rows:
        v = r.get('hold_ratio')
        if v is None:
            continue
        try:
            s += float(v)
        except (TypeError, ValueError):
            continue
    return s


def _top10FloatholdersFetchRows(
    conn, tsCode: str, endYyyymmdd: str
) -> tuple[list[dict], str | None]:
    dmysql = _finaSqlDateFromYyyymmdd(endYyyymmdd)
    code = (tsCode or '').strip()
    if not dmysql or not code:
        return [], '参数无效'
    sql = (
        'WITH ranked AS ( '
        '  SELECT holder_name, hold_amount, hold_ratio, hold_change, holder_type, '
        '    ROW_NUMBER() OVER ( '
        '      PARTITION BY holder_name '
        '      ORDER BY (ann_date IS NULL) ASC, ann_date DESC, hold_amount DESC '
        '    ) AS rn '
        '  FROM stock_tushare.top10_floatholders '
        '  WHERE ts_code = %s AND end_date = %s '
        ') '
        'SELECT holder_name, hold_amount, hold_ratio, hold_change, holder_type '
        'FROM ranked WHERE rn = 1 '
        'ORDER BY hold_amount DESC '
        'LIMIT 10'
    )
    out: list[dict] = []
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (code, dmysql))
            for row in cur.fetchall() or []:
                if not row:
                    continue
                hname, hamt, hratio, hchg, htype = row[0], row[1], row[2], row[3], row[4]
                out.append(
                    {
                        'holder_name': str(hname).strip() if hname is not None else '',
                        'hold_amount': float(hamt) if hamt is not None else None,
                        'hold_ratio': float(hratio) if hratio is not None else None,
                        'hold_change': float(hchg) if hchg is not None else None,
                        'holder_type': str(htype).strip() if htype is not None else '',
                    }
                )
    except Exception as e:
        logger.warning('fina_top10_floatholders 查询失败: %s', e)
        return [], str(e)
    return out, None


def _top10FloatholdersPrevEndDate(conn, tsCode: str, endMysql: str) -> object | None:
    code = (tsCode or '').strip()
    if not code or not endMysql:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT MAX(end_date) FROM stock_tushare.top10_floatholders '
                'WHERE ts_code = %s AND end_date < %s',
                (code, endMysql),
            )
            one = cur.fetchone()
            return one[0] if one and one[0] is not None else None
    except Exception as e:
        logger.warning('fina_top10_floatholders 次新报告期查询失败: %s', e)
        return None


def queryFinaTop10Floatholders(tsCode: str, endDateRaw: str | None) -> tuple[dict, int]:
    """流通股东前十弹窗。"""
    from rt_monitor.monolith import _normalize_end_date_yyyymmdd
    from rt_monitor.services.niusan import _niusan_rt_k_latest_close_map

    t0 = time.perf_counter()
    tsCode = str(tsCode or '').strip()
    endNorm = _normalize_end_date_yyyymmdd(endDateRaw)
    if not tsCode or not endNorm:
        return {'rows': [], 'error': '缺少 ts_code 或 end_date'}, 400
    dmysql = _finaSqlDateFromYyyymmdd(endNorm)
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    except Exception as e:
        logger.exception('连接 stock_tushare 失败（fina_top10_floatholders）: %s', e)
        return {'rows': [], 'error': '数据库连接失败'}, 500
    try:
        rows, err = _top10FloatholdersFetchRows(conn, tsCode, endNorm)
        payload: dict = {
            'rows': rows,
            'summary_ratio_sum': None,
            'summary_prev_end_date': '',
            'summary_ratio_sum_prev': None,
            'summary_ratio_delta_pp': None,
        }
        if err:
            payload['error'] = err
            return payload, 200

        closeVal: float | None = None
        rkMap = _niusan_rt_k_latest_close_map([tsCode])
        tup = rkMap.get(tsCode)
        if tup is not None and tup[0] is not None:
            try:
                closeVal = float(tup[0])
            except (TypeError, ValueError):
                closeVal = None

        for r in rows:
            ha = r.get('hold_amount')
            if closeVal is not None and ha is not None:
                try:
                    r['hold_mkt_val_yi'] = round(float(closeVal) * float(ha) / 1.0e8, 2)
                except (TypeError, ValueError):
                    r['hold_mkt_val_yi'] = None
            else:
                r['hold_mkt_val_yi'] = None

        sumCurr = _top10FloatholdersSumHoldRatio(rows)
        payload['summary_ratio_sum'] = round(sumCurr, 2)

        prevEnd = _top10FloatholdersPrevEndDate(conn, tsCode, dmysql) if dmysql else None
        prevY = _mysqlDateObjToYyyymmdd(prevEnd)
        if prevY:
            payload['summary_prev_end_date'] = prevY
            rowsPrev, errP = _top10FloatholdersFetchRows(conn, tsCode, prevY)
            if not errP:
                sumPrev = _top10FloatholdersSumHoldRatio(rowsPrev)
                payload['summary_ratio_sum_prev'] = round(sumPrev, 2)
                payload['summary_ratio_delta_pp'] = round(sumCurr - sumPrev, 2)

        finaQ._log_slow_api(
            'fina_top10_floatholders',
            t0,
            f'ts={tsCode} end={endNorm} n={len(rows)} prev={payload.get("summary_prev_end_date") or "-"}',
        )
        return payload, 200
    except Exception as e:
        logger.exception('fina_top10_floatholders 接口异常: %s', e)
        return {'rows': [], 'error': str(e)}, 500
    finally:
        conn.close()


def queryFinaSystemSchemeCodes(
    schemeRaw: str | None, indexCodeRaw: str | None
) -> tuple[dict, int]:
    """财报系统方案代码池（index_weight 最新成分）。"""
    from rt_monitor.monolith import INDEX_MONITOR_CODES

    scheme = str(schemeRaw or '').strip().lower()
    if scheme != 'index_weight_latest':
        return {'ok': False, 'error': f'未知系统方案: {scheme}', 'ts_codes': []}, 400

    allowed = {code for code, _ in INDEX_MONITOR_CODES}
    indexCode = str(indexCodeRaw or '').strip().upper()
    if not indexCode or indexCode not in allowed:
        return {'ok': False, 'error': 'index_code 无效或未在监控宽基列表内', 'ts_codes': []}, 400

    try:
        conn = pymysql.connect(**DB_CONFIG)
    except Exception as e:
        logger.exception('连接 stock_wind 失败（fina_system_scheme_codes）: %s', e)
        return {'ok': False, 'error': '数据库连接失败', 'ts_codes': []}, 500
    try:
        sql = (
            'SELECT iw.index_code, iw.trade_date, iw.con_code '
            'FROM stock_wind.index_weight iw '
            'INNER JOIN ('
            '  SELECT index_code, MAX(trade_date) AS md '
            '  FROM stock_wind.index_weight '
            '  WHERE index_code = %s '
            '  GROUP BY index_code'
            ') x ON x.index_code = iw.index_code AND x.md = iw.trade_date '
            'WHERE iw.index_code = %s'
        )
        with conn.cursor() as cur:
            cur.execute(sql, (indexCode, indexCode))
            rows = cur.fetchall() or []

        tradeDateStr: str | None = None
        tsCodes: list[str] = []
        for row in rows:
            conCode = str(row[2] or '').strip().upper()
            if not conCode:
                continue
            if tradeDateStr is None:
                tradeDateStr = _mysqlDateObjToYyyymmdd(row[1]) or str(row[1])
            tsCodes.append(conCode)

        tsCodes = sorted(set(tsCodes))
        return {
            'ok': True,
            'scheme': scheme,
            'index_code': indexCode,
            'source': 'index_weight',
            'trade_date': tradeDateStr,
            'ts_codes': tsCodes,
            'count': len(tsCodes),
        }, 200
    except Exception as e:
        logger.exception('读取财报系统方案代码池失败(%s): %s', scheme, e)
        return {'ok': False, 'error': str(e), 'ts_codes': []}, 500
    finally:
        conn.close()


# 代码更新时间：2026-06-17 19:35
