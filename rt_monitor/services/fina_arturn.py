"""财报筛选域：页面与 arturn / 固定资产序列 API。"""
from __future__ import annotations

import threading
import time

import pymysql
from logger_config import get_logger

from rt_monitor.core.config import DB_CONFIG_TUSHARE
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

# 代码更新时间：2026-06-17 17:10
