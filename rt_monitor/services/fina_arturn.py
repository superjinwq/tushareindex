"""财报筛选域：页面与 arturn / 固定资产序列 API。"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Any

import pymysql
from logger_config import get_logger

from rt_monitor.core.config import DB_CONFIG_TUSHARE
from rt_monitor.core.config import DB_CONFIG_WIND_BULK
from rt_monitor.core.config import FINA_INTEREST_MARKS_PATH
from rt_monitor.core.page_render import renderRtMonitorPageNoCache
from rt_monitor.services import fina_arturn_queries as finaQ

logger = get_logger(__name__)

_FINA_TS_CODE_RE = re.compile(r'^(\d{6})\.(SH|SZ|BJ)$', re.IGNORECASE)
_FINA_SYMBOL_RE = re.compile(r'(?<!\d)(\d{6})(?!\d)')
_FINA_HEADER_SKIP = frozenset({'代码', '证券代码', '股票代码', 'code', 'ts_code', 'symbol'})
_finaInterestMarksLock = threading.Lock()


def renderFinaPageNoCache():
    return renderRtMonitorPageNoCache('rt_monitor_fina.html')


def queryFinaArturnDaysTable(endDateRaw: str | None) -> tuple[dict, int, bytes | None]:
    """应收账款周转天数主表；返回 (payload, http_status, wire_gzip)。

    最新报告期若尚无实质数据（如年报季报刚开窗、库内仅少量披露），
    直接作为默认会返回 0 行——本机若 localStorage 仍记着旧报告期则正常，
    局域网新开页无本地状态就会「无匹配数据」。此处对空结果自动回退到最近有数据的报告期。
    """
    from rt_monitor.monolith import _normalize_end_date_yyyymmdd

    t0 = time.perf_counter()
    try:
        requestedEndDate = _normalize_end_date_yyyymmdd(endDateRaw)
        endDates = finaQ._query_arturn_end_dates(limit_n=120)
        if not endDates:
            return {'rows': [], 'periods': [], 'end_dates': [], 'error': '未找到可用报告期'}, 200, None

        isExplicit = bool(requestedEndDate and requestedEndDate in endDates)
        if isExplicit:
            candidates = [requestedEndDate] + [d for d in endDates if d != requestedEndDate]
        else:
            candidates = list(endDates)

        selectedEndDate = candidates[0]
        payload: dict = {'rows': [], 'periods': [], 'contract_periods': []}
        autoSwitchedFrom: str | None = None

        cacheHit = False
        for ed in candidates:
            cacheKey = finaQ._fina_arturn_table_cache_key(ed)
            before = finaQ._finaCacheGetAllowStale(
                finaQ._fina_indicator_metric_table_cache, cacheKey, time.time()
            )
            hit = finaQ.getOrBuildFinaArturnTable(ed)
            cacheHit = before is not None
            payload = hit
            selectedEndDate = ed
            if payload.get('error'):
                break
            if payload.get('rows'):
                if isExplicit and requestedEndDate and ed != requestedEndDate:
                    autoSwitchedFrom = requestedEndDate
                elif (not isExplicit) and endDates and ed != endDates[0]:
                    autoSwitchedFrom = endDates[0]
                break

        # payload 是共享缓存里的同一个 dict：只在浅拷贝上写本次请求字段，避免并发请求/预热线程互相改写
        payload = dict(payload)
        payload['selected_end_date'] = selectedEndDate
        payload['end_dates'] = endDates
        payload['cache_hit'] = bool(cacheHit)
        if autoSwitchedFrom:
            payload['auto_switched_from'] = autoSwitchedFrom
            payload['hint'] = (
                f'报告期 {autoSwitchedFrom} 暂无可用数据，已自动切换至 {selectedEndDate}'
            )
        wire = None
        if not payload.get('error'):
            try:
                wire = finaQ.ensureFinaArturnWireGzip(payload)
            except Exception:
                logger.warning('fina_arturn wire gzip 编码失败，回退 jsonify', exc_info=True)
                wire = None
        finaQ._log_slow_api(
            'fina_arturn_days_table',
            t0,
            f"selected={selectedEndDate} rows={len(payload.get('rows') or [])}"
            f" cache_hit={cacheHit} wire={'1' if wire else '0'}"
            + (f' switched_from={autoSwitchedFrom}' if autoSwitchedFrom else ''),
        )
        return payload, 200, wire
    except Exception as e:
        logger.exception("fina_arturn_days_table 接口异常: %s", e)
        return {'rows': [], 'periods': [], 'end_dates': [], 'error': str(e)}, 500, None


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


def _finaWarmRecentEndDates(label: str) -> None:
    """预热/保活最近 N 个报告期宽表。"""
    endDates = finaQ._query_arturn_end_dates(limit_n=120)
    if not endDates:
        return
    n = max(0, int(finaQ.FINA_ARTURN_WARMUP_PREFETCH_N))
    for idx in range(min(n, len(endDates))):
        selected = endDates[idx]
        cacheKey = finaQ._fina_arturn_table_cache_key(selected)
        already = finaQ._small_ttl_cache_get(
            finaQ._fina_indicator_metric_table_cache, cacheKey, time.time()
        )
        if already is not None:
            continue
        t0 = time.perf_counter()
        payload = finaQ.getOrBuildFinaArturnTable(selected, allowStale=False)
        if payload and not payload.get('error'):
            payload = dict(payload, selected_end_date=selected, end_dates=endDates, cache_hit=True)
            try:
                finaQ.ensureFinaArturnWireGzip(payload)
            except Exception:
                logger.warning('财报 arturn wire 预编码失败 end=%s', selected, exc_info=True)
            logger.info(
                '财报 arturn 缓存已%s [%d/%d]：end_date=%s rows=%d elapsed=%.2fs',
                label,
                idx + 1,
                min(n, len(endDates)),
                selected,
                len(payload.get('rows') or []),
                time.perf_counter() - t0,
            )


_FINA_KEEPALIVE_POLL_SEC = 30.0


def _finaKeepaliveStep(wasActive: bool, isActive: bool, sinceLastRunSec: float, interval: float) -> str | None:
    """保活状态机：返回本轮动作 '开盘保活' / '收盘定格' / '保活' / None（休市或未到间隔）。"""
    if isActive and not wasActive:
        return '开盘保活'
    if wasActive and not isActive:
        return '收盘定格'
    if isActive and sinceLastRunSec >= interval:
        return '保活'
    return None


def _finaRunKeepaliveAction(action: str) -> None:
    if action == '开盘保活':
        # 首轮重建完成前，过期条目仍送旧值，开盘请求不冷建
        finaQ.setFinaServeStaleUntilRefreshed(True)
        try:
            _finaWarmRecentEndDates(action)
        finally:
            finaQ.setFinaServeStaleUntilRefreshed(False)
        return
    if action == '收盘定格':
        # 休市期不再保活：收盘后强制重建一次，把收盘快照定格进缓存（重建期间送旧值）
        finaQ.expireFinaArturnTableCache()
    _finaWarmRecentEndDates(action)


def warmupFinaArturnCacheAsync() -> None:
    """启动预热最近报告期；交易时段周期保活，休市时段冻结缓存不重算。"""

    def worker() -> None:
        try:
            time.sleep(1.0)
            _finaWarmRecentEndDates('预热')
        except Exception as e:
            logger.warning('财报 arturn 预热失败（忽略不影响启动）: %s', e)
        interval = max(60.0, float(finaQ.FINA_ARTURN_KEEPALIVE_INTERVAL_SEC))
        wasActive = finaQ.isFinaArturnMarketActive()
        lastRunMono = time.monotonic()
        while True:
            try:
                time.sleep(_FINA_KEEPALIVE_POLL_SEC)
                isActive = finaQ.isFinaArturnMarketActive()
                action = _finaKeepaliveStep(
                    wasActive, isActive, time.monotonic() - lastRunMono, interval
                )
                wasActive = isActive
                if action is None:
                    continue
                try:
                    _finaRunKeepaliveAction(action)
                finally:
                    lastRunMono = time.monotonic()
            except Exception as e:
                logger.warning('财报 arturn 保活失败（将继续）: %s', e)

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
    from rt_monitor.services.sw_industry import INDEX_MONITOR_CODES

    scheme = str(schemeRaw or '').strip().lower()
    if scheme != 'index_weight_latest':
        return {'ok': False, 'error': f'未知系统方案: {scheme}', 'ts_codes': []}, 400

    allowed = {code for code, _ in INDEX_MONITOR_CODES}
    indexCode = str(indexCodeRaw or '').strip().upper()
    if not indexCode or indexCode not in allowed:
        return {'ok': False, 'error': 'index_code 无效或未在监控宽基列表内', 'ts_codes': []}, 400

    try:
        conn = pymysql.connect(**DB_CONFIG_WIND_BULK)
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


def _finaExtractRawCodesFromText(text: str) -> list[str]:
    """从纯文本提取股票代码（支持 000001.SZ / 000001，跳过表头）。"""
    out: list[str] = []
    seen: set[str] = set()
    for line in str(text or '').splitlines():
        raw = line.strip().strip('\ufeff')
        if not raw or raw.lower() in _FINA_HEADER_SKIP:
            continue
        # 整行即 ts_code
        mFull = _FINA_TS_CODE_RE.match(raw.upper())
        if mFull:
            code = f'{mFull.group(1)}.{mFull.group(2).upper()}'
            if code not in seen:
                seen.add(code)
                out.append(code)
            continue
        # 行内提取全部 6 位或带后缀代码
        for m in re.finditer(r'\b(\d{6}\.(?:SH|SZ|BJ)|\d{6})\b', raw, flags=re.IGNORECASE):
            token = m.group(1).upper()
            if token not in seen:
                seen.add(token)
                out.append(token)
    return out


def _finaExtractRawCodesFromXlsx(fileBytes: bytes) -> list[str]:
    """从 xlsx 首个工作表提取代码（优先「代码」列，否则第一列）。"""
    try:
        from openpyxl import load_workbook
    except ImportError as e:
        raise RuntimeError('服务器未安装 openpyxl，无法解析 Excel') from e

    wb = load_workbook(filename=io.BytesIO(fileBytes), read_only=True, data_only=True)
    try:
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()
    if not rows:
        return []

    header = [str(c).strip() if c is not None else '' for c in (rows[0] or ())]
    colIdx = 0
    for i, h in enumerate(header):
        if h.lower() in _FINA_HEADER_SKIP or h in _FINA_HEADER_SKIP:
            colIdx = i
            break

    textLines: list[str] = []
    start = 1 if any(h.lower() in _FINA_HEADER_SKIP or h in _FINA_HEADER_SKIP for h in header) else 0
    for row in rows[start:]:
        if not row or colIdx >= len(row):
            continue
        cell = row[colIdx]
        if cell is None:
            continue
        if isinstance(cell, float) and cell == int(cell):
            cell = int(cell)
        s = str(cell).strip()
        if s.endswith('.0') and s[:-2].isdigit():
            s = s[:-2]
        if s.isdigit() and len(s) < 6:
            s = s.zfill(6)
        textLines.append(s)
    return _finaExtractRawCodesFromText('\n'.join(textLines))


def _finaResolveTsCodesFromRaw(
    conn, rawCodes: list[str]
) -> tuple[list[str], list[str]]:
    """将原始代码解析为 ts_code；返回 (resolved, unresolved)。"""
    direct: list[str] = []
    symbols: list[str] = []
    for raw in rawCodes:
        s = str(raw or '').strip().upper()
        if not s:
            continue
        m = _FINA_TS_CODE_RE.match(s)
        if m:
            direct.append(f'{m.group(1)}.{m.group(2).upper()}')
            continue
        m6 = re.fullmatch(r'\d{6}', s)
        if m6:
            symbols.append(s)
            continue
        # 兜底：抽 6 位
        hit = _FINA_SYMBOL_RE.search(s)
        if hit:
            symbols.append(hit.group(1))

    resolved: list[str] = []
    seen: set[str] = set()
    for code in direct:
        if code not in seen:
            seen.add(code)
            resolved.append(code)

    unresolved: list[str] = []
    uniqSym = sorted(set(symbols))
    if not uniqSym:
        return resolved, unresolved

    symToTs: dict[str, str] = {}
    chunk = 500
    for i in range(0, len(uniqSym), chunk):
        part = uniqSym[i : i + chunk]
        ph = ', '.join(['%s'] * len(part))
        sql = (
            f'SELECT symbol, ts_code FROM stock_tushare.stock_basic '
            f'WHERE symbol IN ({ph})'
        )
        with conn.cursor() as cur:
            cur.execute(sql, tuple(part))
            for sym, ts in cur.fetchall() or []:
                symS = str(sym or '').strip()
                tsS = str(ts or '').strip().upper()
                if symS and tsS and symS not in symToTs:
                    symToTs[symS] = tsS

    for sym in uniqSym:
        ts = symToTs.get(sym)
        if not ts:
            unresolved.append(sym)
            continue
        if ts not in seen:
            seen.add(ts)
            resolved.append(ts)
    return resolved, unresolved


def queryFinaCustomBoardResolve(
    *,
    fileStorage: Any = None,
    codesRaw: list[str] | None = None,
    nameHint: str | None = None,
) -> tuple[dict, int]:
    """解析自定义板块文件/代码列表为 ts_code（供 fina 配置弹窗导入）。"""
    t0 = time.perf_counter()
    rawCodes: list[str] = []
    source = 'codes'
    suggestedName = str(nameHint or '').strip()

    try:
        if fileStorage is not None and getattr(fileStorage, 'filename', None):
            fname = str(fileStorage.filename or '').strip()
            source = 'file'
            if not suggestedName:
                base = fname.rsplit('/', 1)[-1].rsplit('\\', 1)[-1]
                suggestedName = re.sub(r'\.(txt|csv|xlsx|xls)$', '', base, flags=re.I).strip()
            data = fileStorage.read()
            if not data:
                return {'ok': False, 'error': '文件为空', 'ts_codes': []}, 400
            lower = fname.lower()
            if lower.endswith(('.xlsx', '.xls')):
                if lower.endswith('.xls') and not lower.endswith('.xlsx'):
                    return {
                        'ok': False,
                        'error': '暂不支持旧版 .xls，请另存为 .xlsx / .txt / .csv',
                        'ts_codes': [],
                    }, 400
                rawCodes = _finaExtractRawCodesFromXlsx(data)
            else:
                # txt / csv / 其它文本
                text = None
                for enc in ('utf-8-sig', 'utf-8', 'gbk', 'gb18030'):
                    try:
                        text = data.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
                if text is None:
                    return {'ok': False, 'error': '无法解码文本文件（请用 UTF-8 或 GBK）', 'ts_codes': []}, 400
                rawCodes = _finaExtractRawCodesFromText(text)
        elif codesRaw:
            source = 'codes'
            rawCodes = [str(c).strip() for c in codesRaw if str(c).strip()]
        else:
            return {'ok': False, 'error': '请上传文件或提供 codes', 'ts_codes': []}, 400

        if not rawCodes:
            return {'ok': False, 'error': '未识别到股票代码', 'ts_codes': []}, 400

        try:
            conn = pymysql.connect(**DB_CONFIG_TUSHARE)
        except Exception as e:
            logger.exception('连接 stock_tushare 失败（fina_custom_board_resolve）: %s', e)
            return {'ok': False, 'error': '数据库连接失败', 'ts_codes': []}, 500
        try:
            tsCodes, unresolved = _finaResolveTsCodesFromRaw(conn, rawCodes)
        finally:
            conn.close()

        if not tsCodes:
            return {
                'ok': False,
                'error': '代码均无法在 stock_basic 中解析',
                'ts_codes': [],
                'unresolved': unresolved[:50],
                'input_count': len(rawCodes),
            }, 400

        finaQ._log_slow_api(
            'fina_custom_board_resolve',
            t0,
            f'source={source} in={len(rawCodes)} ok={len(tsCodes)} miss={len(unresolved)}',
        )
        return {
            'ok': True,
            'name': suggestedName or '自定义板块',
            'ts_codes': tsCodes,
            'count': len(tsCodes),
            'input_count': len(rawCodes),
            'unresolved': unresolved[:50],
            'unresolved_count': len(unresolved),
            'source': source,
        }, 200
    except Exception as e:
        logger.exception('fina_custom_board_resolve 失败: %s', e)
        return {'ok': False, 'error': str(e), 'ts_codes': []}, 500


def _normalizeFinaInterestTsCode(raw) -> str | None:
    tc = str(raw or '').strip().upper()
    if not tc or not _FINA_TS_CODE_RE.match(tc):
        return None
    return tc


def _normalizeFinaInterestCodes(codesRaw) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(codesRaw, (list, tuple, set)):
        return out
    for item in codesRaw:
        tc = _normalizeFinaInterestTsCode(item)
        if not tc or tc in seen:
            continue
        seen.add(tc)
        out.append(tc)
    out.sort()
    return out


def _readFinaInterestMarksPayload() -> dict:
    """读取工程目录 fina_interest_marks.json；缺失/损坏返回空列表。"""
    try:
        if not os.path.isfile(FINA_INTEREST_MARKS_PATH):
            return {'codes': [], 'updated_at': None}
        with open(FINA_INTEREST_MARKS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            codes = _normalizeFinaInterestCodes(data)
            return {'codes': codes, 'updated_at': None}
        if not isinstance(data, dict):
            return {'codes': [], 'updated_at': None}
        codes = _normalizeFinaInterestCodes(data.get('codes'))
        updatedAt = data.get('updated_at')
        return {
            'codes': codes,
            'updated_at': str(updatedAt) if updatedAt else None,
        }
    except Exception as e:
        logger.warning('读取 fina_interest_marks.json 失败，返回空: %s', e)
        return {'codes': [], 'updated_at': None}


def _writeFinaInterestMarksPayload(codes: list[str]) -> dict:
    normalized = _normalizeFinaInterestCodes(codes)
    payload = {
        'codes': normalized,
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    parent = os.path.dirname(FINA_INTEREST_MARKS_PATH) or '.'
    os.makedirs(parent, exist_ok=True)
    tmpPath = FINA_INTEREST_MARKS_PATH + '.tmp'
    with open(tmpPath, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmpPath, FINA_INTEREST_MARKS_PATH)
    return payload


def queryFinaInterestMarks() -> tuple[dict, int]:
    """GET：返回长期保存的感兴趣 ts_code 列表。"""
    with _finaInterestMarksLock:
        return _readFinaInterestMarksPayload(), 200


def saveFinaInterestMarks(body: dict | None) -> tuple[dict, int]:
    """POST：全量覆盖 codes，或按 ts_code 切换单票标记。

    - ``{"codes": [...]}``：覆盖写入
    - ``{"ts_code": "600000.SH"}``：切换该票；可选 ``marked`` 强制开/关
    """
    if not isinstance(body, dict):
        return {'error': '请求体须为 JSON 对象', 'codes': []}, 400
    with _finaInterestMarksLock:
        if 'codes' in body:
            payload = _writeFinaInterestMarksPayload(body.get('codes') or [])
            return {'ok': True, **payload}, 200
        tc = _normalizeFinaInterestTsCode(body.get('ts_code'))
        if not tc:
            return {'error': '缺少有效 ts_code 或 codes', 'codes': []}, 400
        cur = _readFinaInterestMarksPayload()
        codes = list(cur.get('codes') or [])
        codeSet = set(codes)
        if 'marked' in body:
            wantMarked = bool(body.get('marked'))
        else:
            wantMarked = tc not in codeSet
        if wantMarked:
            codeSet.add(tc)
        else:
            codeSet.discard(tc)
        payload = _writeFinaInterestMarksPayload(sorted(codeSet))
        return {
            'ok': True,
            'ts_code': tc,
            'marked': wantMarked,
            **payload,
        }, 200


# 代码更新时间：2026-07-19 22:26
# 代码更新时间：2026-08-04 15:03 缓存键口径 sb_act_8→sb_act_9（当前价位置 7 列）
# 代码更新时间：2026-08-04 15:18 缓存键口径 sb_act_9→sb_act_10（历史最低改倍数 mult_vs_h_low）
# 代码更新时间：2026-08-05 06:00 缓存键口径 sb_act_10→sb_act_11（距高低点%去掉错误 pre_close 折算）
# 代码更新时间：2026-08-05 06:35 缓存键口径 sb_act_11→sb_act_12（新增自公告% pct_since_ann）
# 代码更新时间：2026-08-05 09:11 感兴趣标记落盘 fina_interest_marks.json + GET/POST API
# 代码更新时间：2026-08-06 22:54 切期：getOrBuild 去重 + 周期保活；TTL 见 fina_arturn_queries
# 代码更新时间：2026-08-27 09:12 系统方案代码池改 WIND_BULK，避免 index_weight 8s 读超时
# 代码更新时间：2026-09-03 08:52 宽表 HTTP 预编码 gzip；预热一并编线
# 代码更新时间：2026-10-04 17:42 保活状态机：休市不重算、收盘定格、开盘保活
# 代码更新时间：2026-10-04 22:45 请求/预热只改浅拷贝，不再改写共享缓存 dict
