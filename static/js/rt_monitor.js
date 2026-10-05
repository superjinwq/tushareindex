/**
 * 行情监控页：涨跌中位数展示，支持可配置的自动刷新与立即刷新
 *
 * 说明：财报筛选（应收账款宽表）已迁至独立脚本 static/js/rt_monitor_fina.js，本文件不再包含该段逻辑。
 *
 * 模块概览：
 * - 中位数/成交额/上涨比例/涨跌幅% 四张表：renderMedianTable / renderAmountTable / renderUppctTable / renderIdxPctTable
 * - BIAS 表与弹窗折线图：renderBiasTable / drawBiasSeriesChart（大图+zoom+localStorage）/ initBiasSeriesModal；聚集度 Per 同弹窗
 * - 国证A指分钟差值图与预测：renderIdxMinuteDiffChart；当日/昨日、当日/前五日均值累计图：renderIdxCumsumChart / renderPrev5CumsumChart
 * - 融资融券日度：renderMarginDailyChart（净买入柱 + 融资余额紫线）+ 双击弹窗；renderMarginRzmreIdxChart + 双击弹窗；约5年+chartjs-plugin-zoom 平移/滚轮缩放
 * - 超跌（五日上涨占比）+ 国证A指：独立弹窗（可拖动/resize/localStorage），/api/up_num_ma5_series?limit≤1300，与融资占比弹窗同款 zoom
 * - 成交额 Top5%_pct + 国证A指：点击表头打开独立弹窗，/api/amount_top5_pct_series?start_date=2005-01-01（全历史≤6000点），滚轮缩放+拖拽平移浏览
 * - 数据拉取：fetchRtMedian / fetchIdxMinuteDiff / fetchIdxMinuteDiffPrev5 / fetchBiasTable / fetchMaSignalTable / fetchLimitStatsPanel；MA 共振内嵌图：fetchMaSignalChartData
 * - 定时器由 startAutoRefresh / stopAutoRefresh 控制；日频融资融券双图仅首屏/顶栏「刷新日频」/「立即刷新」，不参与间隔轮询
 */

let refreshTimerId = null;

/** 表格默认分类行（12 行），与后端 table 顺序一致 */
var DEFAULT_CATEGORIES = ['全部A股', 'A500', '沪深300', '中证500', '中证1000', '中证2000', '北证50', '上证50', '科创综指', '科创50', '创业板指', '创业板综'];

/** 前端错误日志：统一前缀 [rt_monitor]，便于过滤；仅在有 console 时输出，避免无 console 环境报错 */
function logError(msg, detail) {
    if (typeof window !== 'undefined' && window.console && window.console.error) {
        if (detail !== undefined) window.console.error('[rt_monitor] ' + msg, detail);
        else window.console.error('[rt_monitor] ' + msg);
    }
}

/**
 * 将 /api/... 转为带前缀的绝对路径。模板注入 window.__RT_MONITOR_API_PREFIX__（SCRIPT_NAME 或 RT_MONITOR_API_PREFIX），
 * 反代子路径部署时与页面同源，避免 fetch 打到站点根路径导致弹窗长期「加载中」。
 */
function rtMonitorApiUrl(path) {
    if (path == null || path === '') return path;
    var p = String(path);
    if (p.charAt(0) !== '/') p = '/' + p;
    var pre = typeof window !== 'undefined' ? window.__RT_MONITOR_API_PREFIX__ : '';
    pre = pre != null && pre !== '' ? String(pre).replace(/\/+$/, '') : '';
    return pre ? pre + p : p;
}

// 弹窗请求守卫：同类新请求会中止旧请求，并提供统一超时，避免长期“加载中”。
var POPUP_FETCH_TIMEOUT_MS = 12000;
// BIAS/MCD 序列接口在高负载或冷缓存时可能慢于通用弹窗；单独放宽，避免误超时
var BIAS_SERIES_QUICK_TIMEOUT_MS = 20000;
var BIAS_SERIES_FULL_TIMEOUT_MS = 45000;
/** 与页面默认自动刷新间隔、后端 RT_MONITOR_POLL_TTL_SEC 对齐（秒→毫秒） */
var RT_MONITOR_POLL_TTL_MS = 2000;
var POPUP_PAYLOAD_CACHE_TTL_MS = RT_MONITOR_POLL_TTL_MS;
var BIAS_SERIES_PAYLOAD_CACHE_TTL_MS = RT_MONITOR_POLL_TTL_MS;
var popupFetchRegistry = {
    biasSeries: null,
    marginDaily: null,
    marginRzmre: null,
    upNumMa5: null,
    limitStatsDetail: null,
    maSignalSeries: null,
};
var popupPayloadCache = Object.create(null);
// 弹窗打开计数器：>0 时暂停 setInterval 轮询，释放浏览器连接池给弹窗数据请求
var _modalOpenCount = 0;
function _modalOpened() {
    _modalOpenCount++;
    if (_modalOpenCount === 1) stopAutoRefresh();
}
function _modalClosed() {
    _modalOpenCount = Math.max(0, _modalOpenCount - 1);
    if (_modalOpenCount === 0) startAutoRefresh();
}
// 预取去重：避免同一 quick url 重复后台拉取
var biasQuickPrefetchInFlight = Object.create(null);
// BIAS 表空闲预取节流：降低首点冷启动等待
var biasGridPrefetchCooldownMs = 9000;
var biasGridPrefetchMaxPerRun = 8;
var biasGridPrefetchLastAt = 0;
var biasGridPrefetchInFlight = Object.create(null);

function getPopupPayloadCache(cacheKey) {
    var rec = popupPayloadCache[cacheKey];
    if (!rec) return null;
    if (rec.expireAt <= Date.now()) {
        delete popupPayloadCache[cacheKey];
        return null;
    }
    return rec.payload;
}

function setPopupPayloadCache(cacheKey, payload, ttlMs) {
    popupPayloadCache[cacheKey] = {
        expireAt: Date.now() + Math.max(500, Number(ttlMs || POPUP_PAYLOAD_CACHE_TTL_MS)),
        payload: payload,
    };
}

function beginPopupFetch(reqKey) {
    var key = String(reqKey || '');
    var prev = popupFetchRegistry[key];
    if (prev && prev.controller && typeof prev.controller.abort === 'function') {
        try { prev.controller.abort(); } catch (e) { /* ignore */ }
    }
    var token = Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
    var controller = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    popupFetchRegistry[key] = { token: token, controller: controller };
    return popupFetchRegistry[key];
}

function isPopupFetchCurrent(reqKey, token) {
    var key = String(reqKey || '');
    var rec = popupFetchRegistry[key];
    return !!(rec && rec.token === token);
}

function endPopupFetch(reqKey, token) {
    var key = String(reqKey || '');
    if (!isPopupFetchCurrent(key, token)) return;
    popupFetchRegistry[key] = null;
}

function abortPopupFetch(reqKey) {
    var key = String(reqKey || '');
    var rec = popupFetchRegistry[key];
    if (!rec) return;
    if (rec.controller && typeof rec.controller.abort === 'function') {
        try { rec.controller.abort(); } catch (e) { /* ignore */ }
    }
    popupFetchRegistry[key] = null;
}

function fetchPopupJson(url, reqKey, timeoutMs) {
    var rec = beginPopupFetch(reqKey);
    var controller = rec.controller;
    var token = rec.token;
    var toMs = Math.max(1000, Number(timeoutMs || POPUP_FETCH_TIMEOUT_MS));
    var timerId = null;
    if (controller) {
        timerId = setTimeout(function () {
            try { controller.abort(); } catch (e) { /* ignore */ }
        }, toMs);
    }
    var opts = { cache: 'no-store' };
    if (controller) opts.signal = controller.signal;
    return fetch(url, opts)
        .then(function (res) {
            return res.json().then(function (data) {
                return { ok: res.ok, status: res.status, data: data, token: token };
            });
        })
        .catch(function (err) {
            if (err && typeof err === 'object') err.__popupToken = token;
            throw err;
        })
        .finally(function () {
            if (timerId) clearTimeout(timerId);
        });
}

function fetchJsonNoGuard(url, timeoutMs) {
    var controller = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var toMs = Math.max(1000, Number(timeoutMs || POPUP_FETCH_TIMEOUT_MS));
    var timerId = null;
    if (controller) {
        timerId = setTimeout(function () {
            try { controller.abort(); } catch (e) { /* ignore */ }
        }, toMs);
    }
    var opts = { cache: 'no-store' };
    if (controller) opts.signal = controller.signal;
    return fetch(url, opts)
        .then(function (res) {
            return res.json().then(function (data) {
                return { ok: res.ok, status: res.status, data: data };
            });
        })
        .finally(function () {
            if (timerId) clearTimeout(timerId);
        });
}

function runIdleTask(fn, timeoutMs) {
    try {
        if (typeof window !== 'undefined' && typeof window.requestIdleCallback === 'function') {
            window.requestIdleCallback(function () { fn(); }, { timeout: Math.max(200, Number(timeoutMs || 1200)) });
            return;
        }
    } catch (e) {
        /* ignore */
    }
    setTimeout(function () { fn(); }, 120);
}

function scheduleBiasGridQuickPrefetch() {
    // 回退：该批量预取在实盘下会放大并发，导致弹窗主请求被拖慢；先禁用，保留函数壳便于后续灰度恢复
    return;
    var now = Date.now();
    if (now - biasGridPrefetchLastAt < biasGridPrefetchCooldownMs) return;
    biasGridPrefetchLastAt = now;
    runIdleTask(function () {
        try {
            var nodes = document.querySelectorAll('#bias-table-body .bias-cell-clickable[data-ts-code][data-freq][data-col]');
            if (!nodes || !nodes.length) return;
            var reqs = [];
            var seen = Object.create(null);
            for (var i = 0; i < nodes.length; i++) {
                var el = nodes[i];
                var tsCode = (el.getAttribute('data-ts-code') || '').trim();
                var freq = (el.getAttribute('data-freq') || '').trim();
                var col = (el.getAttribute('data-col') || '').trim();
                if (!tsCode || !freq || !col) continue;
                var lim = getEtfBiasMcdQuickSeriesLimit(freq, getEtfBiasMcdModalSeriesLimit(freq));
                var url = rtMonitorApiUrl('/api/etf_bias_series?ts_code=' + encodeURIComponent(tsCode) +
                    '&freq=' + encodeURIComponent(freq) +
                    '&column=' + encodeURIComponent(col) +
                    '&limit=' + encodeURIComponent(String(lim)));
                var ck = 'biasSeries|' + url;
                if (seen[ck]) continue;
                seen[ck] = true;
                if (getPopupPayloadCache(ck) || biasGridPrefetchInFlight[ck]) continue;
                reqs.push({ url: url, cacheKey: ck });
                if (reqs.length >= biasGridPrefetchMaxPerRun) break;
            }
            if (!reqs.length) return;
            var idx = 0;
            var concurrency = 2;
            function worker() {
                if (idx >= reqs.length) return Promise.resolve();
                var cur = reqs[idx++];
                biasGridPrefetchInFlight[cur.cacheKey] = true;
                return fetchJsonNoGuard(cur.url, BIAS_SERIES_QUICK_TIMEOUT_MS)
                    .then(function (o) {
                        if (o && o.ok && o.data && !o.data.error) {
                            setPopupPayloadCache(cur.cacheKey, o.data, BIAS_SERIES_PAYLOAD_CACHE_TTL_MS);
                        }
                    })
                    .catch(function () { /* best effort */ })
                    .finally(function () {
                        delete biasGridPrefetchInFlight[cur.cacheKey];
                    })
                    .then(worker);
            }
            for (var w = 0; w < concurrency; w++) worker();
        } catch (e) {
            /* best effort */
        }
    }, 1000);
}

// 国证A指分钟差值图表实例
let idxMinuteDiffChartInstance = null;
// 当日/昨日成交额差额累计值图表实例
let idxCumsumChartInstance = null;
// 当日 vs 前五日均值：分钟差值图、累计值图
let idxPrev5MinuteDiffChartInstance = null;
let idxPrev5CumsumChartInstance = null;
/** 融资融券 margin_daily：净买入柱 + 融资余额线 */
let marginDailyChartInstance = null;
/** 净买入/融资余额弹窗大图（与主图同序列，约 5 年 + zoom） */
let marginDailyModalChartInstance = null;
/** 融资买入额占比 rzmre_pct + 国证A指收盘 + 分位 Per（rzmre_pct_per） */
let marginRzmreIdxChartInstance = null;
/** 弹窗大图：与 prepareMarginRzmreIdxSeriesData 一致；含 zoom/pan */
let marginRzmreIdxModalChartInstance = null;
/** 弹窗请求约 5 年交易日，与 /api/margin_rzmre_idx_series limit 上限一致 */
var MARGIN_RZMRE_IDX_MODAL_SERIES_LIMIT = 1300;
/** localStorage：融资买入额占比弹窗用户调整的宽高与位置（下次打开恢复） */
var MARGIN_RZMRE_MODAL_LAYOUT_STORAGE_KEY = 'rt_monitor_margin_rzmre_modal_layout_v1';
var marginRzmreModalLayoutOpenTs = 0;
var marginRzmreModalSaveTimer = null;
/** 弹窗拉取条数上限，与后端 margin_daily_recent limit 上限一致 */
var MARGIN_DAILY_MODAL_SERIES_LIMIT = 1300;
/** localStorage：净买入/融资余额弹窗几何（与融资占比弹窗独立） */
var MARGIN_DAILY_MODAL_LAYOUT_STORAGE_KEY = 'rt_monitor_margin_daily_modal_layout_v1';
var marginDailyModalLayoutOpenTs = 0;
var marginDailyModalSaveTimer = null;
/** 主图就绪后 debounce 预取弹窗全量序列（双击即用） */
var marginModalPrefetchTimer = null;
/** 清空融资融券弹窗预取缓存（日频手动刷新时调用） */
function clearMarginModalPrefetchCaches() {
    window.__rtMonitorMarginDailyModalRows = null;
    window.__rtMonitorMarginDailyModalLenCapped = undefined;
    window.__rtMonitorMarginRzmreModalRows = null;
    window.__rtMonitorMarginRzmreModalLenCapped = undefined;
}
function prefetchMarginDailyModalFullSeries() {
    var lim = MARGIN_DAILY_MODAL_SERIES_LIMIT;
    fetch(rtMonitorApiUrl('/api/margin_daily_recent?limit=' + encodeURIComponent(String(lim))), { cache: 'no-store' })
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (data) {
            if (data && !data.error && Array.isArray(data.rows) && data.rows.length) {
                window.__rtMonitorMarginDailyModalRows = data.rows;
                window.__rtMonitorMarginDailyModalLenCapped = data.rows.length >= lim;
            }
        })
        .catch(function () { /* 静默 */ });
}
function prefetchMarginRzmreModalFullSeries() {
    var lim = MARGIN_RZMRE_IDX_MODAL_SERIES_LIMIT;
    fetch(rtMonitorApiUrl('/api/margin_rzmre_idx_series?limit=' + encodeURIComponent(String(lim))), { cache: 'no-store' })
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (data) {
            if (data && !data.error && Array.isArray(data.rows) && data.rows.length) {
                window.__rtMonitorMarginRzmreModalRows = data.rows;
                window.__rtMonitorMarginRzmreModalLenCapped = data.rows.length >= lim;
            }
        })
        .catch(function () { /* 静默 */ });
}
function scheduleMarginModalPrefetchs() {
    if (marginModalPrefetchTimer) {
        clearTimeout(marginModalPrefetchTimer);
    }
    marginModalPrefetchTimer = setTimeout(function () {
        marginModalPrefetchTimer = null;
        prefetchMarginDailyModalFullSeries();
        prefetchMarginRzmreModalFullSeries();
    }, 600);
}
/** 超跌弹窗大图：与 /api/up_num_ma5_series limit 上限一致 */
let upNumMa5ModalChartInstance = null;
var UP_NUM_MA5_MODAL_SERIES_LIMIT = 1300;
var UP_NUM_MA5_MODAL_LAYOUT_STORAGE_KEY = 'rt_monitor_up_num_ma5_modal_layout_v1';
var upNumMa5ModalLayoutOpenTs = 0;
var upNumMa5ModalSaveTimer = null;
/** 成交额 Top5%_pct 弹窗：与超跌同款 */
let amountTop5PctModalChartInstance = null;
var AMOUNT_TOP5_PCT_MODAL_START_DATE = '2005-01-01';
/** 与后端 AMOUNT_TOP5_PCT_MODAL_SERIES_CAP 一致；start_date 模式一次拉全历史供 pan/zoom */
var AMOUNT_TOP5_PCT_MODAL_SERIES_CAP = 6000;
var AMOUNT_TOP5_PCT_MODAL_LAYOUT_STORAGE_KEY = 'rt_monitor_amount_top5_pct_modal_layout_v1';
var amountTop5PctModalLayoutOpenTs = 0;
var amountTop5PctModalSaveTimer = null;
/** BIAS / 聚集度Per 弹窗：localStorage 几何（与融资/超跌独立） */
var ETF_BIAS_MCD_MODAL_LAYOUT_STORAGE_KEY = 'rt_monitor_etf_bias_mcd_modal_layout_v1';
var biasSeriesModalLayoutOpenTs = 0;
var biasSeriesModalSaveTimer = null;
/** 连板涨停明细弹窗几何（独立键名） */
var LBZT_DETAIL_MODAL_LAYOUT_STORAGE_KEY = 'rt_monitor_lbzt_detail_modal_layout_v1';
var lbztDetailModalLayoutOpenTs = 0;
var lbztDetailModalSaveTimer = null;
var biasSeriesModalActiveToken = null;
/** 与 rt_monitor_app.ETF_BIAS_MCD_MODAL_LIMIT_CAP_* 一致，改后端时须同步此处 */
var ETF_BIAS_MCD_MODAL_LIMIT_15MIN = 2400;
var ETF_BIAS_MCD_MODAL_LIMIT_60MIN = 1200;
var ETF_BIAS_MCD_MODAL_LIMIT_D = 1300;
var ETF_BIAS_MCD_MODAL_LIMIT_DEFAULT = 300;
// 首屏优先小窗口：先快出图，再异步补全到全窗口，保持最终语义不变（同接口同公式，仅 limit 分阶段）。
var ETF_BIAS_MCD_MODAL_QUICK_LIMIT_15MIN = 900;
var ETF_BIAS_MCD_MODAL_QUICK_LIMIT_60MIN = 600;
var ETF_BIAS_MCD_MODAL_QUICK_LIMIT_D = 300;

/** 弹窗请求条数上限（与后端 _resolve_etf_bias_mcd_series_limit 封顶一致） */
function getEtfBiasMcdModalSeriesLimit(freq) {
    var f = String(freq || '').toUpperCase();
    if (f === '15MIN') return ETF_BIAS_MCD_MODAL_LIMIT_15MIN;
    if (f === '60MIN') return ETF_BIAS_MCD_MODAL_LIMIT_60MIN;
    if (f === 'D') return ETF_BIAS_MCD_MODAL_LIMIT_D;
    return ETF_BIAS_MCD_MODAL_LIMIT_DEFAULT;
}

function getEtfBiasMcdQuickSeriesLimit(freq, fullLimit) {
    var f = String(freq || '').toUpperCase();
    var full = Math.max(1, Number(fullLimit || ETF_BIAS_MCD_MODAL_LIMIT_DEFAULT));
    if (f === '15MIN') return Math.min(full, ETF_BIAS_MCD_MODAL_QUICK_LIMIT_15MIN);
    if (f === '60MIN') return Math.min(full, ETF_BIAS_MCD_MODAL_QUICK_LIMIT_60MIN);
    if (f === 'D') return Math.min(full, ETF_BIAS_MCD_MODAL_QUICK_LIMIT_D);
    return Math.min(full, ETF_BIAS_MCD_MODAL_LIMIT_DEFAULT);
}

/** 弹窗标题中的区间说明（用户心智：15m 约 1 年、60m 约 2 年、日线约 5 年） */
function getEtfBiasMcdModalRangeLabel(freq) {
    var f = String(freq || '').toUpperCase();
    if (f === '15MIN') return '最近约1年（15分钟）';
    if (f === '60MIN') return '最近约2年（60分钟）';
    if (f === 'D') return '最近约5年交易日（日线）';
    return '最近数据';
}

// 融资买入额占比折线（与国证A指紫线并排）：Material Blue 600 #1E88E5，与紫区分、不占用红涨语义
var MARGIN_RZMRE_PCT_LINE = 'rgba(30, 136, 229, 0.95)';
var MARGIN_RZMRE_PCT_FILL = 'rgba(30, 136, 229, 0.08)';

// 与「分钟成交额差值（亿）& 收盘价」表一致的柱色：正红负绿中性灰
function idxDiffBarColor(ctx) {
    var v = ctx.raw;
    var n = (typeof v === 'number') ? v : parseFloat(v);
    if (isNaN(n)) return 'rgba(189, 195, 199, 0.7)';
    return n >= 0 ? 'rgba(231, 76, 60, 0.7)' : 'rgba(46, 204, 113, 0.7)';
}

// 国证A指分钟图 X 轴：仅在这 9 个时间点显示刻度标签和竖线
var IDX_X_HALF_HOUR_LABELS = ['09:30', '10:00', '10:30', '11:00', '11:30', '13:30', '14:00', '14:30', '15:00'];
function idxXTickFilter(val) {
    if (val == null) return '';
    var s = String(val);
    var normalized = (s.length === 4 && s[1] === ':') ? '0' + s : s; // "9:30" -> "09:30"
    return IDX_X_HALF_HOUR_LABELS.indexOf(normalized) !== -1 ? (normalized.length === 5 ? normalized : val) : '';
}
// Chart.js 4 的 category 轴：仅在半小时刻度处绘制竖线（grid）
function idxXGridLineWidth(ctx) {
    if (!ctx.scale || !ctx.tick) return 0;
    var label = ctx.scale.getLabelForValue ? ctx.scale.getLabelForValue(ctx.tick.value) : '';
    var s = String(label);
    var normalized = (s.length === 4 && s[1] === ':') ? '0' + s : s;
    return IDX_X_HALF_HOUR_LABELS.indexOf(normalized) !== -1 ? 1 : 0;
}
function idxXGridColor(ctx) {
    return idxXGridLineWidth(ctx) ? 'rgba(0,0,0,0.12)' : 'transparent';
}
// 图表 Y 轴刻度：只显示整数，无小数、无千分位分隔符
function formatAxisInt(val) {
    if (val == null || (typeof val !== 'number' && isNaN(Number(val)))) return val;
    return String(Math.round(Number(val)));
}

/** 百分比类 Y 轴：保留 1 位小数，无千分位（融资买入额占比等） */
function formatAxisPct1(val) {
    if (val == null || (typeof val !== 'number' && isNaN(Number(val)))) return val;
    var n = Number(val);
    return (Math.round(n * 10) / 10).toFixed(1);
}

/** 将时间值格式化为 "HH:mm"（用于图表标签）；无效值返回字符串形式或索引兜底 */
function formatMinuteLabel(t, fallbackIndex) {
    try {
        var d = new Date(t);
        if (isNaN(d.getTime())) {
            var s = String(t);
            return s.length >= 16 ? s.slice(11, 16) : (fallbackIndex != null ? '' + fallbackIndex : s);
        }
        var h = d.getHours(), m = d.getMinutes();
        return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m;
    } catch (e) {
        var s2 = String(t);
        return s2.length >= 16 ? s2.slice(11, 16) : (fallbackIndex != null ? '' + fallbackIndex : s2);
    }
}

/** 设置“累计值最新”文案：金额 + 可选变化比例，整段按正红负绿着色（el 需有 class idx-cumsum-latest） */
function setCumsumLatestText(el, val, pct) {
    if (!el) return;
    if (val != null && !isNaN(val)) {
        var text = Math.round(val) + ' 亿';
        if (pct != null && !isNaN(pct)) {
            var pctStr = (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
            text += ' （' + pctStr + '）';
        }
        el.textContent = text;
        el.classList.remove('rise', 'fall');
        el.classList.add(val >= 0 ? 'rise' : 'fall');
    } else {
        el.textContent = '--';
        el.classList.remove('rise', 'fall');
    }
}

// 仅显示数值，不带 % 符号
function formatMedian(value) {
    if (value === null || value === undefined || (typeof value === 'number' && isNaN(value))) {
        return '--';
    }
    const num = parseFloat(value);
    return num.toFixed(2);
}

// 红涨绿跌：正数加 .rise（红），负数加 .fall（绿）
function applyMedianClass(el, value) {
    if (!el) return;
    el.classList.remove('rise', 'fall');
    if (value !== null && value !== undefined && !isNaN(parseFloat(value))) {
        const n = parseFloat(value);
        el.classList.add(n >= 0 ? 'rise' : 'fall');
    }
}

// rt_k 等元口径转亿：保留 2 位小数（与后端 etf_amount_yi 一致）
function formatAmountYi2(value) {
    if (value === null || value === undefined || (typeof value === 'number' && isNaN(value))) {
        return '--';
    }
    var n = parseFloat(value);
    return isNaN(n) ? '--' : n.toFixed(2);
}

// 成交额（亿）：整型，无小数，空显示为 -- 
function formatAmount(value) {
    if (value === null || value === undefined || (typeof value === 'number' && isNaN(value))) {
        return '--';
    }
    const n = parseInt(value, 10);
    return isNaN(n) ? '--' : String(n);
}

function formatAmountDate(value) {
    if (value === null || value === undefined || value === '') return '--';
    return String(value).slice(0, 10);
}

/** 成交额表：T-1(亿) / MAX T-60(亿) × 100，保留一位小数（表头「缩量%」已标明单位，数值不重复输出 %） */
function formatAmountShrinkPct(amountT1, max60Yi) {
    if (amountT1 === null || amountT1 === undefined || max60Yi === null || max60Yi === undefined) {
        return '--';
    }
    const m = Number(max60Yi);
    const t1 = Number(amountT1);
    if (!Number.isFinite(m) || !Number.isFinite(t1) || m <= 0) {
        return '--';
    }
    return (t1 / m * 100).toFixed(1);
}

/* amount-tone-start */
var AMOUNT_SHRINK_RISE_MIN = 60;
var AMOUNT_SHRINK_FALL_MAX = 20;

function amountDateKey(value) {
    if (value === null || value === undefined || value === '') return '';
    var s = String(value).slice(0, 10);
    return s === '--' ? '' : s;
}

/** 缩量%：≥60 红，≤20 绿，中间不染色。按展示用的一位小数比较。 */
function amountShrinkTone(amountT1, max60Yi) {
    var shown = formatAmountShrinkPct(amountT1, max60Yi);
    if (shown === '--') return '';
    var n = parseFloat(shown);
    if (!isFinite(n)) return '';
    if (n >= AMOUNT_SHRINK_RISE_MIN) return 'rise';
    if (n <= AMOUNT_SHRINK_FALL_MAX) return 'fall';
    return '';
}

/** 峰值日与峰值日(T-60) 同一天：近 60 日量峰即历史量峰，红色。 */
function amountPeakDateTone(histDate, t60Date) {
    var a = amountDateKey(histDate);
    var b = amountDateKey(t60Date);
    if (!a || !b) return '';
    return a === b ? 'rise' : '';
}

/** 谷值日(T-60) 等于 T-1 交易日：昨天就是近 60 日地量，绿色。 */
function amountValleyDateTone(valleyDate, t1Date) {
    var a = amountDateKey(valleyDate);
    var b = amountDateKey(t1Date);
    if (!a || !b) return '';
    return a === b ? 'fall' : '';
}

/** T 相对昨日全天：不低于为红（涨），低于为绿（跌）。 */
function amountTodayTone(amountT, amountT1) {
    if (amountT === null || amountT === undefined || amountT === '') return '';
    if (amountT1 === null || amountT1 === undefined || amountT1 === '') return '';
    var t = Number(amountT);
    var t1 = Number(amountT1);
    if (!isFinite(t) || !isFinite(t1)) return '';
    return t >= t1 ? 'rise' : 'fall';
}

function applyAmountTone(el, tone) {
    if (!el) return;
    el.classList.remove('rise', 'fall');
    if (tone === 'rise' || tone === 'fall') el.classList.add(tone);
}

function applyAmountRowTones(tr, row, t1Date) {
    if (!tr || !row || !tr.cells || tr.cells.length < 14) return;
    applyAmountTone(tr.cells[1], amountTodayTone(row.amount_t, row.amount_t1));
    applyAmountTone(tr.cells[8], amountPeakDateTone(row.idx_daily_max_all_date, row.idx_daily_max_t60_date));
    applyAmountTone(tr.cells[11], amountShrinkTone(row.amount_t1, row.idx_daily_max_t60));
    applyAmountTone(tr.cells[13], amountValleyDateTone(row.idx_daily_min_t60_date, t1Date));
}
/* amount-tone-end */

// 上涨比例（%）：保留 2 位小数，空显示为 --
function formatUppct(value) {
    if (value === null || value === undefined || (typeof value === 'number' && isNaN(value))) {
        return '--';
    }
    const num = parseFloat(value);
    return isNaN(num) ? '--' : num.toFixed(2);
}

// 上涨比例着色：>50 用 rise（红），<=50 用 fall（绿）
function applyUppctClass(el, value) {
    if (!el) return;
    el.classList.remove('rise', 'fall');
    if (value === null || value === undefined) return;
    const n = parseFloat(value);
    if (isNaN(n)) return;
    el.classList.add(n > 50 ? 'rise' : 'fall');
}

/** 首行四表固定 12 行：刷新时仅改单元格文本，避免 tbody.innerHTML 整表替换导致闪动 */
var MEDIAN_QUAD_ROWS = 12;
var AMOUNT_TABLE_TD = 14;
var UPPCT_TABLE_TD = 7;
var IDX_PCT_TABLE_TD = 10;
/** MA 信号表列数：与 renderMaSignalTable 生成 DOM 一致 */
var MA_SIGNAL_COLS = 23;
/** 高频轮询状态：in-flight 去重 + 成功快照，避免失败时闪白/清空 */
var pollInFlightState = { rtMedian: false, biasTable: false, maSignal: false, limitStats: false };
var rtMedianLastOk = null;
var biasTableLastOk = null;
var maSignalTableLastOk = null;
var maSrSyncStarted = false;

function beginPollFetch(key) {
    if (!key) return false;
    if (pollInFlightState[key]) return false;
    pollInFlightState[key] = true;
    return true;
}

function endPollFetch(key) {
    if (!key) return;
    pollInFlightState[key] = false;
}

/** 按 category 索引一行（rt_median 四表共用） */
function rtMedianRowsByCategory(rows) {
    var map = Object.create(null);
    if (!Array.isArray(rows)) return map;
    for (var i = 0; i < rows.length; i++) {
        var r = rows[i];
        if (!r || r.category == null) continue;
        map[String(r.category).trim()] = r;
    }
    return map;
}

/**
 * 盘中当日不变的列：指数区间 T-1..T-120、成交额全历史 MAX/峰值日、MAX/MIN T-60/峰值日/谷值日/缩量% 依赖的历史序列。
 * 若本帧接口因超时/单行异常返回 null，用上一成功帧补齐，避免轮询时单元格闪成 --。
 */
function mergeRtMedianStableFields(fresh, prev) {
    if (!fresh || typeof fresh !== 'object') return fresh;
    if (!prev || !Array.isArray(prev.table)) return fresh;
    var out = Object.assign({}, fresh);
    if (fresh.amount_top5_pct == null && prev.amount_top5_pct != null) {
        out.amount_top5_pct = prev.amount_top5_pct;
    }
    if (fresh.amount_top5_pct_per == null && prev.amount_top5_pct_per != null) {
        out.amount_top5_pct_per = prev.amount_top5_pct_per;
    }
    var prevMed = rtMedianRowsByCategory(prev.table);
    out.table = Array.isArray(fresh.table)
        ? fresh.table.map(function (r) {
            if (!r || r.category == null) return r;
            var cat = String(r.category).trim();
            var p = prevMed[cat];
            if (!p) return r;
            var m = Object.assign({}, r);
            if (m.idx_daily_max_all == null && p.idx_daily_max_all != null) m.idx_daily_max_all = p.idx_daily_max_all;
            if (
                (m.idx_daily_max_all_date == null || m.idx_daily_max_all_date === '') &&
                p.idx_daily_max_all_date != null &&
                p.idx_daily_max_all_date !== ''
            ) {
                m.idx_daily_max_all_date = p.idx_daily_max_all_date;
            }
            if (m.idx_daily_max_t60 == null && p.idx_daily_max_t60 != null) m.idx_daily_max_t60 = p.idx_daily_max_t60;
            if (
                (m.idx_daily_max_t60_date == null || m.idx_daily_max_t60_date === '') &&
                p.idx_daily_max_t60_date != null &&
                p.idx_daily_max_t60_date !== ''
            ) {
                m.idx_daily_max_t60_date = p.idx_daily_max_t60_date;
            }
            if (m.idx_daily_min_t60 == null && p.idx_daily_min_t60 != null) m.idx_daily_min_t60 = p.idx_daily_min_t60;
            if (
                (m.idx_daily_min_t60_date == null || m.idx_daily_min_t60_date === '') &&
                p.idx_daily_min_t60_date != null &&
                p.idx_daily_min_t60_date !== ''
            ) {
                m.idx_daily_min_t60_date = p.idx_daily_min_t60_date;
            }
            return m;
        })
        : fresh.table;
    var rk = ['range_pct_t1', 'range_pct_t5', 'range_pct_t20', 'range_pct_t60', 'range_pct_t120'];
    if (Array.isArray(fresh.table_idx) && Array.isArray(prev.table_idx)) {
        var prevIdx = rtMedianRowsByCategory(prev.table_idx);
        out.table_idx = fresh.table_idx.map(function (r) {
            if (!r || r.category == null) return r;
            var cat = String(r.category).trim();
            var p = prevIdx[cat];
            if (!p) return r;
            var m = Object.assign({}, r);
            for (var j = 0; j < rk.length; j++) {
                var k = rk[j];
                if (m[k] == null && p[k] != null) m[k] = p[k];
            }
            if (m.idx_gap_per == null && p.idx_gap_per != null) m.idx_gap_per = p.idx_gap_per;
            if (m.idx_pct_per == null && p.idx_pct_per != null) m.idx_pct_per = p.idx_pct_per;
            if (m.idx_intra_per == null && p.idx_intra_per != null) m.idx_intra_per = p.idx_intra_per;
            if (m.idx_amp_per == null && p.idx_amp_per != null) m.idx_amp_per = p.idx_amp_per;
            return m;
        });
    }
    return out;
}

function renderRtMedian(data) {
    const msgSection = document.getElementById('message-section');
    const msgText = document.getElementById('message-text');
    const lastUpdateEl = document.getElementById('last-update');

    if (data.error) {
        msgSection.style.display = 'block';
        msgText.textContent = data.error;
        if (lastUpdateEl) {
            var fallbackTs = rtMedianLastOk && rtMedianLastOk.last_update ? rtMedianLastOk.last_update : '--';
            lastUpdateEl.textContent = '最后刷新: ' + fallbackTs;
        }
        return;
    }

    msgSection.style.display = 'none';
    if (lastUpdateEl) {
        var suffix = data.__stale ? '（沿用上一帧）' : '';
        lastUpdateEl.textContent = '最后刷新: ' + (data.last_update || '--') + suffix;
    }
    const tableRows = Array.isArray(data.table) ? data.table : [];
    var amountT1Date = (Array.isArray(data.t5_dates) && data.t5_dates.length) ? data.t5_dates[0] : '';
    renderMedianTable(tableRows);
    renderAmountTable(tableRows, amountT1Date);
    renderUppctTable(tableRows);
    setAmountTop5HeaderMetrics(data.amount_top5_pct, data.amount_top5_pct_per);
    // 涨跌幅% 表单独用 table_idx（国证A指首行），其余三张表用 table（全部A股首行）
    const tableIdxRows = Array.isArray(data.table_idx) ? data.table_idx : tableRows;
    renderIdxPctTable(tableIdxRows);
}

function renderMedianTable(rows) {
    const tbody = document.getElementById('median-table-body');
    if (!tbody) return;
    const list = rows.length >= 12 ? rows : DEFAULT_CATEGORIES.map(function (cat) { return { category: cat, t: null, t1: null, t5: null, t20: null, t60: null, t120: null }; });
    const slice = list.slice(0, MEDIAN_QUAD_ROWS);
    if (tbody.rows.length === MEDIAN_QUAD_ROWS) {
        var ok = true;
        for (var pi = 0; pi < MEDIAN_QUAD_ROWS; pi++) {
            if (!tbody.rows[pi] || tbody.rows[pi].cells.length !== 7) { ok = false; break; }
        }
        if (ok) {
            for (var ri = 0; ri < MEDIAN_QUAD_ROWS; ri++) {
                var row = slice[ri];
                var tr = tbody.rows[ri];
                tr.className = 'median-table-row' + ((ri % 2 === 1) ? ' median-table-zebra' : '');
                tr.cells[0].textContent = row.category || '--';
                var vals = [row.t, row.t1, row.t5, row.t20, row.t60, row.t120];
                for (var cj = 0; cj < 6; cj++) {
                    var span = tr.cells[cj + 1].querySelector('.median-cell-value');
                    if (span) {
                        span.textContent = formatMedian(vals[cj]);
                        applyMedianClass(span, vals[cj]);
                    }
                }
            }
            return;
        }
    }
    tbody.innerHTML = slice.map(function (row, i) {
        var trClass = (i % 2 === 1) ? ' median-table-zebra' : '';
        return '<tr class="median-table-row' + trClass + '">' +
            '<td class="median-table-cat">' + (row.category || '--') + '</td>' +
            '<td class="median-table-num"><span class="median-cell-value" data-val="t">' + formatMedian(row.t) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value" data-val="t1">' + formatMedian(row.t1) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value" data-val="t5">' + formatMedian(row.t5) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value" data-val="t20">' + formatMedian(row.t20) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value" data-val="t60">' + formatMedian(row.t60) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value" data-val="t120">' + formatMedian(row.t120) + '</span></td>' +
            '</tr>';
    }).join('');
    slice.forEach(function (row, i) {
        var tr = tbody.rows[i];
        if (!tr) return;
        var vals = [row.t, row.t1, row.t5, row.t20, row.t60, row.t120];
        [].forEach.call(tr.querySelectorAll('.median-cell-value'), function (span, j) {
            applyMedianClass(span, vals[j]);
        });
    });
}

/** 成交额（亿）表头：Top5%_pct（stockstat 前5%家数占比）+ Per（marketinfo 五年分位） */
function setAmountTop5HeaderMetrics(pctVal, perVal) {
    var elPct = document.getElementById('amount-top5-pct-header');
    var elPer = document.getElementById('amount-top5-pct-per-header');
    if (elPct) {
        elPct.classList.add('bias-cell-clickable');
        elPct.setAttribute('title', 'stockstat 入样 amount>0：按成交额降序前 ceil(5%) 家合计占全市场比例；点击打开五年双轴图');
        var pctText = '--';
        if (pctVal != null && pctVal !== '' && !(typeof pctVal === 'number' && isNaN(pctVal))) {
            var pctNum = parseFloat(pctVal);
            if (!isNaN(pctNum)) pctText = pctNum.toFixed(2) + '%';
        }
        elPct.textContent = 'Top5%_pct：' + pctText;
    }
    if (elPer) {
        var perText = '--';
        if (perVal != null && perVal !== '' && !(typeof perVal === 'number' && isNaN(perVal))) {
            var perNum = parseFloat(perVal);
            if (!isNaN(perNum)) perText = perNum.toFixed(2);
        }
        elPer.textContent = 'Per：' + perText;
    }
}

function renderAmountTable(rows, t1Date) {
    const tbody = document.getElementById('amount-table-body');
    if (!tbody) return;
    var amountT1Date = t1Date || '';
    const list = rows.length >= 12 ? rows : DEFAULT_CATEGORIES.map(function (cat) {
        return { category: cat, amount_t: null, amount_t1: null, amount_t5: null, amount_t20: null, amount_t60: null, amount_t120: null, idx_daily_max_all: null, idx_daily_max_all_date: null, idx_daily_max_t60: null, idx_daily_max_t60_date: null, idx_daily_min_t60: null, idx_daily_min_t60_date: null };
    });
    const slice = list.slice(0, MEDIAN_QUAD_ROWS);
    if (tbody.rows.length === MEDIAN_QUAD_ROWS) {
        var okA = true;
        for (var ai = 0; ai < MEDIAN_QUAD_ROWS; ai++) {
            if (!tbody.rows[ai] || tbody.rows[ai].cells.length !== AMOUNT_TABLE_TD) { okA = false; break; }
        }
        if (okA) {
            for (var aj = 0; aj < MEDIAN_QUAD_ROWS; aj++) {
                var rowA = slice[aj];
                var trA = tbody.rows[aj];
                trA.className = 'median-table-row' + ((aj % 2 === 1) ? ' median-table-zebra' : '');
                trA.cells[0].textContent = rowA.category || '--';
                trA.cells[1].textContent = formatAmount(rowA.amount_t);
                trA.cells[2].textContent = formatAmount(rowA.amount_t1);
                trA.cells[3].textContent = formatAmount(rowA.amount_t5);
                trA.cells[4].textContent = formatAmount(rowA.amount_t20);
                trA.cells[5].textContent = formatAmount(rowA.amount_t60);
                trA.cells[6].textContent = formatAmount(rowA.amount_t120);
                trA.cells[7].textContent = formatAmount(rowA.idx_daily_max_all);
                trA.cells[8].textContent = formatAmountDate(rowA.idx_daily_max_all_date);
                trA.cells[9].textContent = formatAmount(rowA.idx_daily_max_t60);
                trA.cells[10].textContent = formatAmountDate(rowA.idx_daily_max_t60_date);
                trA.cells[11].textContent = formatAmountShrinkPct(rowA.amount_t1, rowA.idx_daily_max_t60);
                trA.cells[12].textContent = formatAmount(rowA.idx_daily_min_t60);
                trA.cells[13].textContent = formatAmountDate(rowA.idx_daily_min_t60_date);
                applyAmountRowTones(trA, rowA, amountT1Date);
            }
            return;
        }
    }
    tbody.innerHTML = slice.map(function (row, i) {
        var trClass = (i % 2 === 1) ? ' median-table-zebra' : '';
        var shrinkPctStr = formatAmountShrinkPct(row.amount_t1, row.idx_daily_max_t60);
        return '<tr class="median-table-row' + trClass + '">' +
            '<td class="median-table-cat">' + (row.category || '--') + '</td>' +
            '<td class="median-table-num">' + formatAmount(row.amount_t) + '</td>' +
            '<td class="median-table-num">' + formatAmount(row.amount_t1) + '</td>' +
            '<td class="median-table-num">' + formatAmount(row.amount_t5) + '</td>' +
            '<td class="median-table-num">' + formatAmount(row.amount_t20) + '</td>' +
            '<td class="median-table-num">' + formatAmount(row.amount_t60) + '</td>' +
            '<td class="median-table-num">' + formatAmount(row.amount_t120) + '</td>' +
            '<td class="median-table-num amount-peak-cell">' + formatAmount(row.idx_daily_max_all) + '</td>' +
            '<td class="median-table-num amount-peak-cell">' + formatAmountDate(row.idx_daily_max_all_date) + '</td>' +
            '<td class="median-table-num amount-peak-cell">' + formatAmount(row.idx_daily_max_t60) + '</td>' +
            '<td class="median-table-num amount-peak-cell">' + formatAmountDate(row.idx_daily_max_t60_date) + '</td>' +
            '<td class="median-table-num amount-shrink-pct-cell">' + shrinkPctStr + '</td>' +
            '<td class="median-table-num amount-valley-cell">' + formatAmount(row.idx_daily_min_t60) + '</td>' +
            '<td class="median-table-num amount-valley-cell">' + formatAmountDate(row.idx_daily_min_t60_date) + '</td>' +
            '</tr>';
    }).join('');
    for (var toneI = 0; toneI < slice.length && toneI < tbody.rows.length; toneI++) {
        applyAmountRowTones(tbody.rows[toneI], slice[toneI], amountT1Date);
    }
}

function renderUppctTable(rows) {
    const tbody = document.getElementById('uppct-table-body');
    if (!tbody) return;
    const list = rows.length >= 12 ? rows : DEFAULT_CATEGORIES.map(function (cat) {
        return { category: cat, uppct_t: null, uppct_t1: null, uppct_t5: null, uppct_t20: null, uppct_t60: null, uppct_t120: null };
    });
    const sliceU = list.slice(0, MEDIAN_QUAD_ROWS);
    if (tbody.rows.length === MEDIAN_QUAD_ROWS) {
        var okU = true;
        for (var ui = 0; ui < MEDIAN_QUAD_ROWS; ui++) {
            if (!tbody.rows[ui] || tbody.rows[ui].cells.length !== UPPCT_TABLE_TD) { okU = false; break; }
        }
        if (okU) {
            for (var uj = 0; uj < MEDIAN_QUAD_ROWS; uj++) {
                var rowU = sliceU[uj];
                var trU = tbody.rows[uj];
                trU.className = 'median-table-row' + ((uj % 2 === 1) ? ' median-table-zebra' : '');
                trU.cells[0].textContent = rowU.category || '--';
                var valsU = [rowU.uppct_t, rowU.uppct_t1, rowU.uppct_t5, rowU.uppct_t20, rowU.uppct_t60, rowU.uppct_t120];
                for (var uk = 0; uk < 6; uk++) {
                    var spU = trU.cells[uk + 1].querySelector('.uppct-cell-value');
                    if (spU) {
                        spU.textContent = formatUppct(valsU[uk]);
                        applyUppctClass(spU, valsU[uk]);
                    }
                }
            }
            return;
        }
    }
    tbody.innerHTML = sliceU.map(function (row, i) {
        var trClass = (i % 2 === 1) ? ' median-table-zebra' : '';
        return '<tr class="median-table-row' + trClass + '">' +
            '<td class="median-table-cat">' + (row.category || '--') + '</td>' +
            '<td class="median-table-num"><span class="median-cell-value uppct-cell-value">' + formatUppct(row.uppct_t) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value uppct-cell-value">' + formatUppct(row.uppct_t1) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value uppct-cell-value">' + formatUppct(row.uppct_t5) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value uppct-cell-value">' + formatUppct(row.uppct_t20) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value uppct-cell-value">' + formatUppct(row.uppct_t60) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value uppct-cell-value">' + formatUppct(row.uppct_t120) + '</span></td>' +
            '</tr>';
    }).join('');
    sliceU.forEach(function (row, i) {
        var tr = tbody.rows[i];
        if (!tr) return;
        var vals = [row.uppct_t, row.uppct_t1, row.uppct_t5, row.uppct_t20, row.uppct_t60, row.uppct_t120];
        [].forEach.call(tr.querySelectorAll('.uppct-cell-value'), function (span, j) {
            applyUppctClass(span, vals[j]);
        });
    });
}

// 涨跌幅% 表：Pct/Intra/Gap/Amp（index_stat）+ 区间 T-1..T-120（index_daily+rt_idx_k，与 MA 表 Wind 口径一致）
/* gap-per-tone-start */
function formatGapPer(value) {
    if (value === null || value === undefined || value === '') return '';
    var n = Number(value);
    if (!isFinite(n)) return '';
    var r = Math.round(n);
    if (r < 0) r = 0;
    if (r > 100) r = 100;
    return String(r);
}

/** Gap 旁注分位：≥95 红，≤5 绿，中间灰色。 */
function gapPerTone(value) {
    var shown = formatGapPer(value);
    if (shown === '') return '';
    var n = parseInt(shown, 10);
    if (n >= 95) return 'rise';
    if (n <= 5) return 'fall';
    return '';
}

function applyGapPerSpan(span, value) {
    if (!span) return;
    span.textContent = formatGapPer(value);
    span.classList.remove('rise', 'fall');
    var tone = gapPerTone(value);
    if (tone) span.classList.add(tone);
}

/** 分位 ≤5 / ≥95 时给当前值加浅底；Amp 无涨跌色，尾部同时改字色。 */
function paintIdxMetricTail(tr, row) {
    if (!tr || !row) return;
    var cols = [
        ['pct', 'idx_pct_per'],
        ['intra', 'idx_intra_per'],
        ['gap', 'idx_gap_per'],
        ['amp', 'idx_amp_per']
    ];
    for (var i = 0; i < cols.length; i++) {
        var col = cols[i][0];
        var perSpan = tr.querySelector('.idx-metric-per[data-per="' + col + '"]');
        applyGapPerSpan(perSpan, row[cols[i][1]]);
        var valueSpan = tr.querySelector('[data-col="' + col + '"]');
        if (!valueSpan) continue;
        valueSpan.classList.remove('tail-hi', 'tail-lo');
        var tone = gapPerTone(row[cols[i][1]]);
        if (tone === 'rise') valueSpan.classList.add('tail-hi');
        if (tone === 'fall') valueSpan.classList.add('tail-lo');
    }
}

function paintMaSignalMetricTail(tr, row) {
    if (!tr || !row) return;
    var cols = [
        ['amt', 'etf_amount_per'],
        ['pct', 'etf_pct_per'],
        ['intra', 'etf_intra_per'],
        ['gap', 'etf_gap_per'],
        ['amp', 'etf_amp_per']
    ];
    for (var i = 0; i < cols.length; i++) {
        var col = cols[i][0];
        applyGapPerSpan(tr.querySelector('.idx-metric-per[data-per="' + col + '"]'), row[cols[i][1]]);
        var valueSpan = tr.querySelector('[data-q="' + col + '"]');
        if (!valueSpan) continue;
        valueSpan.classList.remove('tail-hi', 'tail-lo');
        var tone = gapPerTone(row[cols[i][1]]);
        if (tone === 'rise') valueSpan.classList.add('tail-hi');
        if (tone === 'fall') valueSpan.classList.add('tail-lo');
    }
}
/* gap-per-tone-end */

function renderIdxPctTable(rows) {
    const tbody = document.getElementById('idx-pct-table-body');
    if (!tbody) return;
    const list = rows.length >= 12 ? rows : DEFAULT_CATEGORIES.map(function (cat) {
        return {
            category: cat,
            idx_pct_t: null,
            idx_intra_t: null,
            idx_gap_t: null,
            idx_amp_t: null,
            range_pct_t1: null,
            range_pct_t5: null,
            range_pct_t20: null,
            range_pct_t60: null,
            range_pct_t120: null,
        };
    });
    const sliceI = list.slice(0, MEDIAN_QUAD_ROWS);
    var rkMapI = { t1: 'range_pct_t1', t5: 'range_pct_t5', t20: 'range_pct_t20', t60: 'range_pct_t60', t120: 'range_pct_t120' };
    if (tbody.rows.length === MEDIAN_QUAD_ROWS) {
        var okI = true;
        for (var ii = 0; ii < MEDIAN_QUAD_ROWS; ii++) {
            if (!tbody.rows[ii] || tbody.rows[ii].cells.length !== IDX_PCT_TABLE_TD) { okI = false; break; }
        }
        if (okI) {
            for (var gapChk = 0; gapChk < MEDIAN_QUAD_ROWS; gapChk++) {
                if (!tbody.rows[gapChk] || !tbody.rows[gapChk].querySelector('.idx-metric-per[data-per="amp"]')) { okI = false; break; }
            }
        }
        if (okI) {
            for (var ij = 0; ij < MEDIAN_QUAD_ROWS; ij++) {
                var rowI = sliceI[ij];
                var trI = tbody.rows[ij];
                trI.className = 'median-table-row' + ((ij % 2 === 1) ? ' median-table-zebra' : '');
                trI.cells[0].textContent = rowI.category || '--';
                var valsIdx = [rowI.idx_pct_t, rowI.idx_intra_t, rowI.idx_gap_t];
                [].forEach.call(trI.querySelectorAll('.idx-pct-cell'), function (span, j) {
                    span.textContent = formatMedian(valsIdx[j]);
                    applyMedianClass(span, valsIdx[j]);
                });
                var ampSpanI = trI.querySelector('.idx-amp-cell');
                if (ampSpanI) {
                    ampSpanI.textContent = formatMedian(rowI.idx_amp_t);
                    ampSpanI.classList.remove('rise', 'fall');
                }
                paintIdxMetricTail(trI, rowI);
                Object.keys(rkMapI).forEach(function (rk) {
                    var spanR = trI.querySelector('.idx-pct-range-cell[data-rk="' + rk + '"]');
                    if (!spanR) return;
                    spanR.textContent = formatMedian(rowI[rkMapI[rk]]);
                    applyMedianClass(spanR, rowI[rkMapI[rk]]);
                });
            }
            return;
        }
    }
    tbody.innerHTML = sliceI.map(function (row, i) {
        var trClass = (i % 2 === 1) ? ' median-table-zebra' : '';
        return '<tr class="median-table-row' + trClass + '">' +
            '<td class="median-table-cat">' + (row.category || '--') + '</td>' +
            '<td class="median-table-num"><span class="median-cell-value idx-pct-cell" data-col="pct">' + formatMedian(row.idx_pct_t) + '</span><span class="idx-metric-per" data-per="pct" title="全历史分位"></span></td>' +
            '<td class="median-table-num"><span class="median-cell-value idx-pct-cell" data-col="intra">' + formatMedian(row.idx_intra_t) + '</span><span class="idx-metric-per" data-per="intra" title="全历史分位"></span></td>' +
            '<td class="median-table-num"><span class="median-cell-value idx-pct-cell" data-col="gap">' + formatMedian(row.idx_gap_t) + '</span><span class="idx-metric-per" data-per="gap" title="全历史分位"></span></td>' +
            '<td class="median-table-num"><span class="median-cell-value idx-amp-cell" data-col="amp">' + formatMedian(row.idx_amp_t) + '</span><span class="idx-metric-per" data-per="amp" title="全历史分位；对照完整交易日，盘中常偏低"></span></td>' +
            '<td class="median-table-num"><span class="median-cell-value idx-pct-range-cell" data-rk="t1">' + formatMedian(row.range_pct_t1) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value idx-pct-range-cell" data-rk="t5">' + formatMedian(row.range_pct_t5) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value idx-pct-range-cell" data-rk="t20">' + formatMedian(row.range_pct_t20) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value idx-pct-range-cell" data-rk="t60">' + formatMedian(row.range_pct_t60) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value idx-pct-range-cell" data-rk="t120">' + formatMedian(row.range_pct_t120) + '</span></td>' +
            '</tr>';
    }).join('');
    sliceI.forEach(function (row, i) {
        var tr = tbody.rows[i];
        if (!tr) return;
        var vals = [row.idx_pct_t, row.idx_intra_t, row.idx_gap_t];
        [].forEach.call(tr.querySelectorAll('.idx-pct-cell'), function (span, j) {
            applyMedianClass(span, vals[j]);
        });
        paintIdxMetricTail(tr, row);
        var rkMap = { t1: 'range_pct_t1', t5: 'range_pct_t5', t20: 'range_pct_t20', t60: 'range_pct_t60', t120: 'range_pct_t120' };
        Object.keys(rkMap).forEach(function (rk) {
            var span = tr.querySelector('.idx-pct-range-cell[data-rk="' + rk + '"]');
            if (!span) return;
            applyMedianClass(span, row[rkMap[rk]]);
        });
    });
}

function formatSignedPct(value) {
    if (value === null || value === undefined || (typeof value === 'number' && isNaN(value))) return '--';
    const n = parseFloat(value);
    if (isNaN(n)) return '--';
    return (n >= 0 ? '+' : '') + n.toFixed(2);
}

/** stockstat.amount 已按元在服务端换算为亿（amount_yi），此处仅格式化展示 */
function formatAmountYi(value) {
    if (value === null || value === undefined || (typeof value === 'number' && isNaN(value))) return '--';
    var n = parseFloat(value);
    return isNaN(n) ? '--' : n.toFixed(2);
}

function renderDeclineTopTable(rows) {
    const tbody = document.getElementById('decline-top-body');
    if (!tbody) return;
    const list = Array.isArray(rows) ? rows : [];
    tbody.innerHTML = list.map(function (row, i) {
        var trClass = (i % 2 === 1) ? ' median-table-zebra' : '';
        var v = row && row.decline_rate != null ? row.decline_rate : null;
        var swRaw = row && row.sw_name != null ? String(row.sw_name) : '';
        var swTitle = swRaw ? ' title="' + escapeHtmlAttr(swRaw) + '"' : '';
        return '<tr class="median-table-row' + trClass + '">' +
            '<td class="median-table-cat">' + (row.windcodes || '--') + '</td>' +
            '<td class="median-table-cat">' + (row.sec_name || '--') + '</td>' +
            '<td class="median-table-num top-moves-metric-cell">' + formatAmountYi(row && row.amount_yi) + '</td>' +
            '<td class="median-table-num top-moves-metric-cell"><span class="median-cell-value decline-cell">' + formatSignedPct(v) + '</span></td>' +
            '<td class="median-table-cat"' + swTitle + '>' + (swRaw || '--') + '</td>' +
            '</tr>';
    }).join('');
    [].forEach.call(tbody.querySelectorAll('.decline-cell'), function (span) {
        applyMedianClass(span, span && span.textContent ? parseFloat(span.textContent) : null);
    });
}

function renderRecoveryTopTable(rows) {
    const tbody = document.getElementById('recovery-top-body');
    if (!tbody) return;
    const list = Array.isArray(rows) ? rows : [];
    tbody.innerHTML = list.map(function (row, i) {
        var trClass = (i % 2 === 1) ? ' median-table-zebra' : '';
        var v = row && row.recovery_rate != null ? row.recovery_rate : null;
        var swRaw = row && row.sw_name != null ? String(row.sw_name) : '';
        var swTitle = swRaw ? ' title="' + escapeHtmlAttr(swRaw) + '"' : '';
        return '<tr class="median-table-row' + trClass + '">' +
            '<td class="median-table-cat">' + (row.windcodes || '--') + '</td>' +
            '<td class="median-table-cat">' + (row.sec_name || '--') + '</td>' +
            '<td class="median-table-num top-moves-metric-cell">' + formatAmountYi(row && row.amount_yi) + '</td>' +
            '<td class="median-table-num top-moves-metric-cell"><span class="median-cell-value recovery-cell">' + formatSignedPct(v) + '</span></td>' +
            '<td class="median-table-cat"' + swTitle + '>' + (swRaw || '--') + '</td>' +
            '</tr>';
    }).join('');
    [].forEach.call(tbody.querySelectorAll('.recovery-cell'), function (span) {
        applyMedianClass(span, span && span.textContent ? parseFloat(span.textContent) : null);
    });
}

/** 当日成交额 Top30：stockstat + 昨成交额(stk_factor_pro 千元→亿) + 自由换手%(rt_k.vol / free_share万股) */
function renderAmountTopTable(rows) {
    const tbody = document.getElementById('amount-top-body');
    if (!tbody) return;
    const list = Array.isArray(rows) ? rows : [];
    tbody.innerHTML = list.map(function (row, i) {
        var trClass = (i % 2 === 1) ? ' median-table-zebra' : '';
        var vPct = row && row.pctchange != null ? row.pctchange : null;
        var vFft = row && row.free_float_turnover_pct != null ? row.free_float_turnover_pct : null;
        var fftStr = '--';
        if (vFft != null && vFft !== '') {
            var nf = parseFloat(vFft);
            fftStr = isNaN(nf) ? '--' : nf.toFixed(2);
        }
        var swRawAmt = row && row.sw_name != null ? String(row.sw_name) : '';
        var swTitleAmt = swRawAmt ? ' title="' + escapeHtmlAttr(swRawAmt) + '"' : '';
        return '<tr class="median-table-row' + trClass + '">' +
            '<td class="median-table-cat">' + (row.windcodes || '--') + '</td>' +
            '<td class="median-table-cat">' + (row.sec_name || '--') + '</td>' +
            '<td class="median-table-num top-moves-metric-cell">' + formatAmountYi(row && row.amount_yi) + '</td>' +
            '<td class="median-table-num top-moves-metric-cell">' + formatAmountYi(row && row.prev_amount_yi) + '</td>' +
            '<td class="median-table-num top-moves-metric-cell"><span class="median-cell-value amount-pct-cell">' + formatSignedPct(vPct) + '</span></td>' +
            '<td class="median-table-num top-moves-metric-cell">' + fftStr + '</td>' +
            '<td class="median-table-cat"' + swTitleAmt + '>' + (swRawAmt || '--') + '</td>' +
            '</tr>';
    }).join('');
    [].forEach.call(tbody.querySelectorAll('.amount-pct-cell'), function (span) {
        applyMedianClass(span, span && span.textContent ? parseFloat(span.textContent) : null);
    });
}

/** Top30 成交额加权涨跌幅表头：Σ(pct×amount)/Σ(amount)，与 myuiux idx-minute-predict 红涨绿跌一致 */
function setAmountTopWeightedPctHeader(val) {
    var el = document.getElementById('amount-top-weighted-pct');
    if (!el) return;
    el.className = 'idx-minute-predict';
    el.textContent = '';
    var label = document.createElement('span');
    label.textContent = '加权涨跌 ';
    el.appendChild(label);
    var numSpan = document.createElement('span');
    if (val == null || val === '' || (typeof val === 'number' && isNaN(val))) {
        numSpan.textContent = '--';
    } else {
        var n = parseFloat(val);
        if (isNaN(n)) {
            numSpan.textContent = '--';
        } else {
            numSpan.textContent = (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
            if (n > 0) numSpan.className = 'rise';
            else if (n < 0) numSpan.className = 'fall';
        }
    }
    el.appendChild(numSpan);
}

function fetchTopMoves() {
    if (document.hidden) {
        return;
    }
    fetch(rtMonitorApiUrl('/api/top_moves?limit=30'))
        .then(function (res) { return res.json(); })
        .then(function (data) {
            if (data && data.error) {
                renderDeclineTopTable([]);
                renderRecoveryTopTable([]);
                renderAmountTopTable([]);
                setAmountTopWeightedPctHeader(null);
                return;
            }
            renderDeclineTopTable(Array.isArray(data.decline_top30) ? data.decline_top30 : []);
            renderRecoveryTopTable(Array.isArray(data.recovery_top30) ? data.recovery_top30 : []);
            renderAmountTopTable(Array.isArray(data.amount_top30) ? data.amount_top30 : []);
            setAmountTopWeightedPctHeader(data.amount_top_weighted_pct);
        })
        .catch(function () {
            renderDeclineTopTable([]);
            renderRecoveryTopTable([]);
            renderAmountTopTable([]);
            setAmountTopWeightedPctHeader(null);
        });
}

/** X 轴标签：YYYY-MM-DD → MM-DD，减少 300 日横轴占位 */
function formatMarginDailyXLabel(tradeDateStr) {
    if (!tradeDateStr || typeof tradeDateStr !== 'string') return '';
    var s = tradeDateStr.trim();
    return s.length >= 10 ? s.slice(5, 10) : s;
}

/**
 * 融资占比+国证A指图 X 轴刻度（多日/多年）：与前一交易日跨年则「年份」+ 换行 + MM-DD，否则仅 MM-DD，便于扫年度区间（myuiux：刻度 #555、与 Tooltip 完整日期互补）。
 */
function formatMarginRzmreIdxXAxisTick(fullDateStr, prevFullDateStr) {
    if (!fullDateStr || typeof fullDateStr !== 'string') return '';
    var s = fullDateStr.trim();
    var y = s.length >= 4 ? s.slice(0, 4) : '';
    var mmdd = s.length >= 10 ? s.slice(5, 10) : '';
    var prevY = '';
    if (prevFullDateStr && typeof prevFullDateStr === 'string' && prevFullDateStr.length >= 4) {
        prevY = prevFullDateStr.trim().slice(0, 4);
    }
    if (!prevY || prevY !== y) {
        return y + '\n' + mmdd;
    }
    return mmdd;
}

/**
 * 解析 margin_daily 净买入/融资余额序列（升序），供主图与弹窗共用。
 * @returns {object|null} labels、柱/线数据、轴域、tradeDateFull、last 等
 */
function prepareMarginDailySeriesData(rows) {
    var list = Array.isArray(rows) ? rows.slice() : [];
    list.sort(function (a, b) { return String(a.trade_date).localeCompare(String(b.trade_date)); });
    if (list.length === 0) return null;
    var labels = list.map(function (r) { return formatMarginDailyXLabel(r.trade_date); });
    var netbuy = list.map(function (r) {
        var v = r.netbuy_yi;
        if (v == null || v === '') return null;
        var n = parseFloat(v);
        return isNaN(n) ? null : n;
    });
    var rzye = list.map(function (r) {
        var v = r.rzye_sum_yi;
        if (v == null || v === '') return null;
        var n = parseFloat(v);
        return isNaN(n) ? null : n;
    });
    var netbuyPct = list.map(function (r) {
        var v = r.netbuy_pct;
        if (v == null || v === '') return null;
        var n = parseFloat(v);
        return isNaN(n) ? null : n;
    });
    var validRzye = rzye.filter(function (v) { return v != null && !isNaN(v); });
    var rzyeMin;
    var rzyeMax;
    if (validRzye.length > 0) {
        rzyeMin = Math.min.apply(null, validRzye);
        rzyeMax = Math.max.apply(null, validRzye);
        if (rzyeMin === rzyeMax) {
            var pad = rzyeMin * 0.01 || 1;
            rzyeMin -= pad;
            rzyeMax += pad;
        } else {
            var rng = rzyeMax - rzyeMin;
            rzyeMin -= rng * 0.05;
            rzyeMax += rng * 0.05;
        }
    }
    var tradeDateFull = list.map(function (r) { return r.trade_date || ''; });
    return {
        list: list,
        last: list[list.length - 1],
        labels: labels,
        netbuy: netbuy,
        rzye: rzye,
        netbuyPct: netbuyPct,
        tradeDateFull: tradeDateFull,
        validRzye: validRzye,
        rzyeMin: rzyeMin,
        rzyeMax: rzyeMax,
    };
}

/**
 * 融资融券日度双轴图：左柱净买入额（正红负绿 idxDiffBarColor）、右线融资余额（紫），对齐分钟成交额差值图风格。
 * 数据按 trade_date 升序；标题行展示最新一日摘要。
 */
function renderMarginDailyChart(rows) {
    var container = document.getElementById('margin-daily-chart');
    var latestEl = document.getElementById('margin-daily-latest');
    if (!container) return;
    if (!window.Chart) {
        logError('Chart.js 未加载，无法渲染融资融券图');
        return;
    }
    var P = prepareMarginDailySeriesData(rows);
    if (!P) {
        container.innerHTML = '<div class="rt-monitor-chart-empty">暂无融资融券日数据</div>';
        if (latestEl) latestEl.innerHTML = '--';
        if (marginDailyChartInstance) {
            marginDailyChartInstance.destroy();
            marginDailyChartInstance = null;
        }
        return;
    }
    var labels = P.labels;
    var netbuy = P.netbuy;
    var rzye = P.rzye;
    var netbuyPct = P.netbuyPct;
    var validRzye = P.validRzye;
    var rzyeMin = P.rzyeMin;
    var rzyeMax = P.rzyeMax;
    var tradeDateFull = P.tradeDateFull;
    var last = P.last;
    if (latestEl) {
        var td = last.trade_date || '--';
        var rz = last.rzye_sum_yi;
        var nb = last.netbuy_yi;
        var html = '最新 ' + td + ' 融资余额 ';
        if (rz != null && !isNaN(parseFloat(rz))) {
            html += '<span class="idx-predict-value">' + Math.round(parseFloat(rz)) + ' 亿</span>';
        } else {
            html += '--';
        }
        html += ' 净买入 ';
        if (nb != null && !isNaN(parseFloat(nb))) {
            var nbn = parseFloat(nb);
            html += '<span class="' + (nbn >= 0 ? 'rise' : 'fall') + '">' + (nbn >= 0 ? '+' : '') + nbn.toFixed(2) + ' 亿</span>';
        } else {
            html += '--';
        }
        latestEl.innerHTML = html;
    }
    var canvas;
    if (!marginDailyChartInstance) {
        canvas = document.createElement('canvas');
        container.innerHTML = '';
        container.appendChild(canvas);
        marginDailyChartInstance = new Chart(canvas, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    {
                        type: 'bar',
                        label: '净买入额（亿）',
                        data: netbuy,
                        yAxisID: 'yNetbuy',
                        backgroundColor: idxDiffBarColor,
                        borderWidth: 0,
                    },
                    {
                        type: 'line',
                        label: '融资余额（亿）',
                        data: rzye,
                        yAxisID: 'yRzye',
                        borderColor: 'rgba(155, 89, 182, 1)',
                        backgroundColor: 'rgba(155, 89, 182, 0.12)',
                        tension: 0.35,
                        fill: false,
                        pointRadius: 0,
                        pointHoverRadius: 3,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                animation: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        displayColors: false,
                        callbacks: {
                            title: function (items) {
                                if (!items || !items.length) return '';
                                var ch = items[0].chart;
                                var idx = items[0].dataIndex;
                                var dates = ch.$marginTradeDates;
                                var full = dates && dates[idx];
                                return full ? ('日期：' + full) : '';
                            },
                            label: function (ctx) {
                                var v = ctx.parsed.y;
                                if (v === null || v === undefined || isNaN(v)) return '';
                                if (ctx.datasetIndex === 0) {
                                    return '净买入额：' + v.toFixed(2) + ' 亿';
                                }
                                return '融资余额：' + v.toFixed(2) + ' 亿';
                            },
                            afterBody: function (items) {
                                if (!items || !items.length) return [];
                                var ch = items[0].chart;
                                var idx = items[0].dataIndex;
                                var arr = ch.$marginNetbuyPct;
                                var p = arr && arr[idx];
                                if (p === null || p === undefined || isNaN(p)) return [];
                                return ['净买入%：' + parseFloat(p).toFixed(2) + '%'];
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        display: true,
                        grid: { display: false },
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                            autoSkip: true,
                            maxRotation: 0,
                            maxTicksLimit: 14,
                        },
                    },
                    yNetbuy: {
                        type: 'linear',
                        position: 'left',
                        title: { display: false },
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                            callback: function (val) { return formatAxisInt(val); },
                        },
                        grid: {
                            color: function (ctx) {
                                var v = ctx.tick && ctx.tick.value;
                                return v === 0 ? '#b0bec5' : '#eceff1';
                            },
                            lineWidth: function (ctx) {
                                var v = ctx.tick && ctx.tick.value;
                                return v === 0 ? 1.2 : 0.5;
                            },
                        },
                    },
                    yRzye: {
                        type: 'linear',
                        position: 'right',
                        grid: { drawOnChartArea: false },
                        title: { display: false },
                        min: validRzye.length ? rzyeMin : undefined,
                        max: validRzye.length ? rzyeMax : undefined,
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                            callback: function (val) { return formatAxisInt(val); },
                        },
                    },
                },
            },
        });
        marginDailyChartInstance.$marginTradeDates = tradeDateFull;
        marginDailyChartInstance.$marginNetbuyPct = netbuyPct;
    } else {
        var chart = marginDailyChartInstance;
        chart.options.animation = false;
        chart.data.labels = labels;
        chart.$marginTradeDates = tradeDateFull;
        chart.$marginNetbuyPct = netbuyPct;
        if (chart.data.datasets && chart.data.datasets.length >= 2) {
            chart.data.datasets[0].data = netbuy;
            chart.data.datasets[1].data = rzye;
        }
        if (validRzye.length > 0 && chart.options.scales && chart.options.scales.yRzye) {
            chart.options.scales.yRzye.min = rzyeMin;
            chart.options.scales.yRzye.max = rzyeMax;
        } else if (chart.options.scales && chart.options.scales.yRzye) {
            delete chart.options.scales.yRzye.min;
            delete chart.options.scales.yRzye.max;
        }
        chart.update('none');
    }
}

/**
 * 解析融资买入额占比 + 国证A指序列，供主图与弹窗共用（不操作 DOM）。
 * @returns {object|null} labels / 数据集 / 轴域 / tradeDateFull 等
 */
function prepareMarginRzmreIdxSeriesData(rows) {
    var list = Array.isArray(rows) ? rows.slice() : [];
    list.sort(function (a, b) { return String(a.trade_date).localeCompare(String(b.trade_date)); });
    if (list.length === 0) return null;
    var labels = list.map(function (r) { return formatMarginDailyXLabel(r.trade_date); });
    var pctArr = list.map(function (r) {
        var v = r.rzmre_pct;
        if (v == null || v === '') return null;
        var n = parseFloat(v);
        return isNaN(n) ? null : n;
    });
    var closeArr = list.map(function (r) {
        var v = r.idx_close;
        if (v == null || v === '') return null;
        var n = parseFloat(v);
        return isNaN(n) ? null : n;
    });
    var perArr = list.map(function (r) {
        var v = r.rzmre_pct_per;
        if (v == null || v === '') return null;
        var n = parseFloat(v);
        return isNaN(n) ? null : n;
    });
    var validPct = pctArr.filter(function (v) { return v != null && !isNaN(v); });
    var validClose = closeArr.filter(function (v) { return v != null && !isNaN(v); });
    var pctMin;
    var pctMax;
    if (validPct.length > 0) {
        pctMin = Math.min.apply(null, validPct);
        pctMax = Math.max.apply(null, validPct);
        if (pctMin === pctMax) {
            var pp = pctMin * 0.05 || 0.1;
            pctMin -= pp;
            pctMax += pp;
        } else {
            var pr = pctMax - pctMin;
            pctMin -= pr * 0.05;
            pctMax += pr * 0.05;
        }
    }
    var closeMin;
    var closeMax;
    if (validClose.length > 0) {
        closeMin = Math.min.apply(null, validClose);
        closeMax = Math.max.apply(null, validClose);
        if (closeMin === closeMax) {
            var cp = closeMin * 0.002 || 1;
            closeMin -= cp;
            closeMax += cp;
        } else {
            var cr = closeMax - closeMin;
            closeMin -= cr * 0.05;
            closeMax += cr * 0.05;
        }
    }
    var tradeDateFull = list.map(function (r) { return r.trade_date || ''; });
    return {
        list: list,
        labels: labels,
        pctArr: pctArr,
        closeArr: closeArr,
        perArr: perArr,
        tradeDateFull: tradeDateFull,
        validPct: validPct,
        validClose: validClose,
        pctMin: pctMin,
        pctMax: pctMax,
        closeMin: closeMin,
        closeMax: closeMax,
    };
}

/**
 * 融资买入额占比（%）与国证A指收盘双折线：左轴 rzmre_pct，右轴 idx_close；表头/悬停展示 rzmre_pct_per（Per 分位）
 */
function renderMarginRzmreIdxChart(rows) {
    var container = document.getElementById('margin-rzmre-idx-chart');
    var latestEl = document.getElementById('margin-rzmre-idx-latest');
    if (!container) return;
    if (!window.Chart) {
        logError('Chart.js 未加载，无法渲染融资占比+国证A指图');
        return;
    }
    var P = prepareMarginRzmreIdxSeriesData(rows);
    if (!P) {
        container.innerHTML = '<div class="rt-monitor-chart-empty">暂无融资买入额占比与国证A指对齐数据</div>';
        if (latestEl) latestEl.innerHTML = '--';
        if (marginRzmreIdxChartInstance) {
            marginRzmreIdxChartInstance.destroy();
            marginRzmreIdxChartInstance = null;
        }
        return;
    }
    var list = P.list;
    var labels = P.labels;
    var pctArr = P.pctArr;
    var closeArr = P.closeArr;
    var perArr = P.perArr;
    var validPct = P.validPct;
    var validClose = P.validClose;
    var pctMin = P.pctMin;
    var pctMax = P.pctMax;
    var closeMin = P.closeMin;
    var closeMax = P.closeMax;
    var tradeDateFull = P.tradeDateFull;
    var last = list[list.length - 1];
    if (latestEl) {
        var td = last.trade_date || '--';
        var p = last.rzmre_pct;
        var perLast = last.rzmre_pct_per;
        var html = '最新 ' + td + ' 融资买入额占比 ';
        if (p != null && !isNaN(parseFloat(p))) {
            html += '<span class="idx-predict-value">' + parseFloat(p).toFixed(2) + '%</span>';
        } else {
            html += '--';
        }
        html += ' Per ';
        if (perLast != null && !isNaN(parseFloat(perLast))) {
            html += '<span class="idx-predict-value">' + parseFloat(perLast).toFixed(2) + '%</span>';
        } else {
            html += '--';
        }
        latestEl.innerHTML = html;
    }
    var canvas;
    if (!marginRzmreIdxChartInstance) {
        canvas = document.createElement('canvas');
        container.innerHTML = '';
        container.appendChild(canvas);
        marginRzmreIdxChartInstance = new Chart(canvas, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: '融资买入额占比',
                        data: pctArr,
                        yAxisID: 'yPct',
                        borderColor: MARGIN_RZMRE_PCT_LINE,
                        backgroundColor: MARGIN_RZMRE_PCT_FILL,
                        tension: 0.35,
                        fill: false,
                        pointRadius: 0,
                        pointHoverRadius: 3,
                        borderWidth: 1.5,
                    },
                    {
                        label: '国证A指收盘',
                        data: closeArr,
                        yAxisID: 'yIdx',
                        borderColor: 'rgba(155, 89, 182, 1)',
                        backgroundColor: 'rgba(155, 89, 182, 0.08)',
                        tension: 0.35,
                        fill: false,
                        pointRadius: 0,
                        pointHoverRadius: 3,
                        borderWidth: 1.5,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                animation: false,
                layout: {
                    padding: { bottom: 6 },
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        displayColors: false,
                        callbacks: {
                            title: function (items) {
                                if (!items || !items.length) return '';
                                var ch = items[0].chart;
                                var idx = items[0].dataIndex;
                                var dates = ch.$marginRzmreTradeDates;
                                var full = dates && dates[idx];
                                return full ? ('日期：' + full) : '';
                            },
                            label: function (ctx) {
                                var v = ctx.parsed.y;
                                if (v === null || v === undefined || isNaN(v)) return '';
                                if (ctx.datasetIndex === 0) {
                                    return '融资买入额占比：' + v.toFixed(2) + ' %';
                                }
                                return '国证A指收盘：' + formatAxisInt(v) + ' 点';
                            },
                            afterBody: function (items) {
                                if (!items || !items.length) return '';
                                var ch = items[0].chart;
                                var idx = items[0].dataIndex;
                                var arr = ch.$marginRzmrePctPer;
                                var pv = arr && arr[idx];
                                if (pv == null || isNaN(pv)) {
                                    return 'Per（分位）：--';
                                }
                                return 'Per（分位）：' + parseFloat(pv).toFixed(2) + ' %';
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        display: true,
                        grid: { display: false },
                        ticks: {
                            color: '#555',
                            font: {
                                size: RT_MONITOR_CHART_FONT_SIZE,
                                family: PAGE_FONT,
                                lineHeight: 1.35,
                            },
                            autoSkip: true,
                            maxRotation: 0,
                            maxTicksLimit: 14,
                            callback: function (tickVal) {
                                var idx = tickVal;
                                var chart = this.chart;
                                var dates = chart.$marginRzmreTradeDates;
                                if (!dates || dates[idx] == null || dates[idx] === '') return '';
                                var full = String(dates[idx]);
                                var prev = idx > 0 && dates[idx - 1] != null && dates[idx - 1] !== ''
                                    ? String(dates[idx - 1])
                                    : null;
                                return formatMarginRzmreIdxXAxisTick(full, prev);
                            },
                        },
                    },
                    yPct: {
                        type: 'linear',
                        position: 'left',
                        title: { display: false },
                        min: validPct.length ? pctMin : undefined,
                        max: validPct.length ? pctMax : undefined,
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                            callback: function (val) { return formatAxisPct1(val); },
                        },
                        grid: {
                            color: function (ctx) {
                                var v = ctx.tick && ctx.tick.value;
                                return v === 0 ? '#b0bec5' : '#eceff1';
                            },
                            lineWidth: function (ctx) {
                                var v = ctx.tick && ctx.tick.value;
                                return v === 0 ? 1.2 : 0.5;
                            },
                        },
                    },
                    yIdx: {
                        type: 'linear',
                        position: 'right',
                        grid: { drawOnChartArea: false },
                        title: { display: false },
                        min: validClose.length ? closeMin : undefined,
                        max: validClose.length ? closeMax : undefined,
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                            callback: function (val) { return formatAxisInt(val); },
                        },
                    },
                },
            },
        });
        marginRzmreIdxChartInstance.$marginRzmreTradeDates = tradeDateFull;
        marginRzmreIdxChartInstance.$marginRzmrePctPer = perArr;
    } else {
        var chart = marginRzmreIdxChartInstance;
        chart.options.animation = false;
        chart.data.labels = labels;
        chart.$marginRzmreTradeDates = tradeDateFull;
        chart.$marginRzmrePctPer = perArr;
        if (chart.data.datasets && chart.data.datasets.length >= 2) {
            chart.data.datasets[0].data = pctArr;
            chart.data.datasets[1].data = closeArr;
        }
        if (validPct.length > 0 && chart.options.scales && chart.options.scales.yPct) {
            chart.options.scales.yPct.min = pctMin;
            chart.options.scales.yPct.max = pctMax;
        } else if (chart.options.scales && chart.options.scales.yPct) {
            delete chart.options.scales.yPct.min;
            delete chart.options.scales.yPct.max;
        }
        if (validClose.length > 0 && chart.options.scales && chart.options.scales.yIdx) {
            chart.options.scales.yIdx.min = closeMin;
            chart.options.scales.yIdx.max = closeMax;
        } else if (chart.options.scales && chart.options.scales.yIdx) {
            delete chart.options.scales.yIdx.min;
            delete chart.options.scales.yIdx.max;
        }
        chart.update('none');
    }
}

/**
 * 弹窗大图：与主图相同双轴序列，启用 chartjs-plugin-zoom 横向平移与滚轮缩放；初始视窗为最近约 300 个交易日。
 */
function renderMarginRzmreIdxModalChart(rows) {
    var canvas = document.getElementById('margin-rzmre-idx-modal-chart');
    var msgEl = document.getElementById('margin-rzmre-idx-modal-msg');
    if (!canvas || !window.Chart) {
        logError('弹窗 canvas 或 Chart.js 不可用');
        return;
    }
    if (marginRzmreIdxModalChartInstance) {
        marginRzmreIdxModalChartInstance.destroy();
        marginRzmreIdxModalChartInstance = null;
    }
    if (msgEl) {
        msgEl.style.display = 'none';
        msgEl.textContent = '';
    }
    var P = prepareMarginRzmreIdxSeriesData(rows);
    if (!P) {
        if (msgEl) {
            msgEl.style.display = 'block';
            msgEl.textContent = '暂无对齐数据';
        }
        return;
    }
    var labels = P.labels;
    var pctArr = P.pctArr;
    var closeArr = P.closeArr;
    var perArr = P.perArr;
    var validPct = P.validPct;
    var validClose = P.validClose;
    var pctMin = P.pctMin;
    var pctMax = P.pctMax;
    var closeMin = P.closeMin;
    var closeMax = P.closeMax;
    var tradeDateFull = P.tradeDateFull;
    var zoomOpts = {
        limits: {
            x: { min: 'original', max: 'original', minRange: 15 },
        },
        pan: {
            enabled: true,
            mode: 'x',
        },
        zoom: {
            wheel: { enabled: true },
            pinch: { enabled: true },
            mode: 'x',
        },
    };
    if (typeof Hammer === 'undefined') {
        zoomOpts.pan.enabled = false;
        zoomOpts.zoom.pinch.enabled = false;
        logError('Hammer.js 未加载，弹窗仅支持滚轮缩放，平移已关闭');
    }
    marginRzmreIdxModalChartInstance = new Chart(canvas, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [
                {
                    label: '融资买入额占比',
                    data: pctArr,
                    yAxisID: 'yPct',
                    borderColor: MARGIN_RZMRE_PCT_LINE,
                    backgroundColor: MARGIN_RZMRE_PCT_FILL,
                    tension: 0.35,
                    fill: false,
                    pointRadius: 0,
                    pointHoverRadius: 3,
                    borderWidth: 1.5,
                },
                {
                    label: '国证A指收盘',
                    data: closeArr,
                    yAxisID: 'yIdx',
                    borderColor: 'rgba(155, 89, 182, 1)',
                    backgroundColor: 'rgba(155, 89, 182, 0.08)',
                    tension: 0.35,
                    fill: false,
                    pointRadius: 0,
                    pointHoverRadius: 3,
                    borderWidth: 1.5,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            animation: false,
            layout: {
                padding: { bottom: 14 },
            },
            plugins: {
                legend: { display: false },
                zoom: zoomOpts,
                tooltip: {
                    displayColors: false,
                    callbacks: {
                        title: function (items) {
                            if (!items || !items.length) return '';
                            var ch = items[0].chart;
                            var idx = items[0].dataIndex;
                            var dates = ch.$marginRzmreTradeDates;
                            var full = dates && dates[idx];
                            return full ? ('日期：' + full) : '';
                        },
                        label: function (ctx) {
                            var v = ctx.parsed.y;
                            if (v === null || v === undefined || isNaN(v)) return '';
                            if (ctx.datasetIndex === 0) {
                                return '融资买入额占比：' + v.toFixed(2) + ' %';
                            }
                            return '国证A指收盘：' + formatAxisInt(v) + ' 点';
                        },
                        afterBody: function (items) {
                            if (!items || !items.length) return '';
                            var ch = items[0].chart;
                            var idx = items[0].dataIndex;
                            var arr = ch.$marginRzmrePctPer;
                            var pv = arr && arr[idx];
                            if (pv == null || isNaN(pv)) {
                                return 'Per（分位）：--';
                            }
                            return 'Per（分位）：' + parseFloat(pv).toFixed(2) + ' %';
                        },
                    },
                },
            },
            scales: {
                x: {
                    display: true,
                    grid: { display: false },
                    ticks: {
                        color: '#555',
                        font: {
                            size: RT_MONITOR_CHART_FONT_SIZE,
                            family: PAGE_FONT,
                            lineHeight: 1.35,
                        },
                        autoSkip: true,
                        autoSkipPadding: 2,
                        maxRotation: 0,
                        maxTicksLimit: 42,
                        callback: function (tickVal) {
                            var idx = tickVal;
                            var chart = this.chart;
                            var dates = chart.$marginRzmreTradeDates;
                            if (!dates || dates[idx] == null || dates[idx] === '') return '';
                            var full = String(dates[idx]);
                            var prev = idx > 0 && dates[idx - 1] != null && dates[idx - 1] !== ''
                                ? String(dates[idx - 1])
                                : null;
                            return formatMarginRzmreIdxXAxisTick(full, prev);
                        },
                    },
                },
                yPct: {
                    type: 'linear',
                    position: 'left',
                    title: { display: false },
                    min: validPct.length ? pctMin : undefined,
                    max: validPct.length ? pctMax : undefined,
                    ticks: {
                        color: '#555',
                        font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                        callback: function (val) { return formatAxisPct1(val); },
                    },
                    grid: {
                        color: function (ctx) {
                            var v = ctx.tick && ctx.tick.value;
                            return v === 0 ? '#b0bec5' : '#eceff1';
                        },
                        lineWidth: function (ctx) {
                            var v = ctx.tick && ctx.tick.value;
                            return v === 0 ? 1.2 : 0.5;
                        },
                    },
                },
                yIdx: {
                    type: 'linear',
                    position: 'right',
                    grid: { drawOnChartArea: false },
                    title: { display: false },
                    min: validClose.length ? closeMin : undefined,
                    max: validClose.length ? closeMax : undefined,
                    ticks: {
                        color: '#555',
                        font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                        callback: function (val) { return formatAxisInt(val); },
                    },
                },
            },
        },
    });
    marginRzmreIdxModalChartInstance.$marginRzmreTradeDates = tradeDateFull;
    marginRzmreIdxModalChartInstance.$marginRzmrePctPer = perArr;
    var ch = marginRzmreIdxModalChartInstance;
    var n = labels.length;
    requestAnimationFrame(function () {
        if (!marginRzmreIdxModalChartInstance) return;
        if (n > 0 && typeof ch.zoomScale === 'function') {
            var win = Math.min(300, n);
            var zmin = Math.max(0, n - win);
            var zmax = n - 1;
            try {
                marginRzmreIdxModalChartInstance.zoomScale('x', { min: zmin, max: zmax }, 'default');
            } catch (e) {
                logError('zoomScale 初始视窗失败', e);
            }
        }
        try {
            marginRzmreIdxModalChartInstance.resize();
        } catch (e2) {
            logError('弹窗图 resize 失败', e2);
        }
    });
}

function loadUpNumMa5ModalLayout() {
    try {
        if (typeof localStorage === 'undefined') return null;
        var raw = localStorage.getItem(UP_NUM_MA5_MODAL_LAYOUT_STORAGE_KEY);
        if (!raw) return null;
        var o = JSON.parse(raw);
        if (typeof o.w !== 'number' || typeof o.h !== 'number') return null;
        if (o.w < 320 || o.h < 320) return null;
        return {
            w: o.w,
            h: o.h,
            l: typeof o.l === 'number' ? o.l : null,
            t: typeof o.t === 'number' ? o.t : null,
        };
    } catch (e) {
        return null;
    }
}

function saveUpNumMa5ModalLayout(modalEl) {
    try {
        if (!modalEl || typeof localStorage === 'undefined') return;
        var rect = modalEl.getBoundingClientRect();
        var w = Math.round(rect.width);
        var h = Math.round(rect.height);
        var l = Math.round(rect.left);
        var t = Math.round(rect.top);
        if (w < 320 || h < 320) return;
        localStorage.setItem(UP_NUM_MA5_MODAL_LAYOUT_STORAGE_KEY, JSON.stringify({ w: w, h: h, l: l, t: t }));
    } catch (e) {
        /* ignore */
    }
}

function applyUpNumMa5ModalSavedLayout() {
    var overlay = document.getElementById('up-num-ma5-modal-overlay');
    var modal = overlay && overlay.querySelector('.up-num-ma5-modal-shell');
    if (!modal) return;
    var lay = loadUpNumMa5ModalLayout();
    if (!lay) return;
    var vw = window.innerWidth || document.documentElement.clientWidth;
    var vh = window.innerHeight || document.documentElement.clientHeight;
    var minW = 320;
    var minH = 320;
    var w = Math.min(Math.max(lay.w, minW), Math.max(minW, vw - 4));
    var h = Math.min(Math.max(lay.h, minH), Math.max(minH, vh - 4));
    modal.style.width = w + 'px';
    modal.style.height = h + 'px';
    modal.style.transform = 'none';
    var l = lay.l != null ? lay.l : (vw - w) / 2;
    var t = lay.t != null ? lay.t : (vh - h) / 2;
    l = Math.max(0, Math.min(l, vw - w));
    t = Math.max(0, Math.min(t, vh - h));
    modal.style.left = l + 'px';
    modal.style.top = t + 'px';
}

function scheduleSaveUpNumMa5ModalLayout(modalEl) {
    if (!modalEl) return;
    clearTimeout(upNumMa5ModalSaveTimer);
    upNumMa5ModalSaveTimer = setTimeout(function () {
        if (Date.now() - upNumMa5ModalLayoutOpenTs < 700) return;
        saveUpNumMa5ModalLayout(modalEl);
    }, 400);
}

function closeUpNumMa5Modal() {
    var overlay = document.getElementById('up-num-ma5-modal-overlay');
    var modal = overlay && overlay.querySelector('.up-num-ma5-modal-shell');
    var wasOpen = overlay && overlay.classList.contains('is-open');
    if (wasOpen && modal) {
        saveUpNumMa5ModalLayout(modal);
    }
    if (overlay) overlay.classList.remove('is-open');
    if (upNumMa5ModalChartInstance) {
        upNumMa5ModalChartInstance.destroy();
        upNumMa5ModalChartInstance = null;
    }
    abortPopupFetch('upNumMa5');
    var ld = document.getElementById('up-num-ma5-modal-loading');
    if (ld) ld.style.display = 'none';
    if (wasOpen) _modalClosed();
}

/**
 * 超跌弹窗：双轴 + 20/25 参考线 + chartjs-plugin-zoom；初始 X 视窗最近约 300 个交易日。
 */
function renderUpNumMa5ModalChart(payload) {
    var canvas = document.getElementById('up-num-ma5-modal-chart');
    var msgEl = document.getElementById('up-num-ma5-modal-msg');
    if (!canvas || !window.Chart) {
        logError('超跌弹窗 canvas 或 Chart.js 不可用');
        return;
    }
    if (upNumMa5ModalChartInstance) {
        upNumMa5ModalChartInstance.destroy();
        upNumMa5ModalChartInstance = null;
    }
    if (msgEl) {
        msgEl.style.display = 'none';
        msgEl.textContent = '';
    }
    var times = payload.times || [];
    var upNumMa5 = payload.up_num_ma5 || [];
    var close = payload.close || [];
    var n = Math.min(times.length, upNumMa5.length, close.length);
    if (n === 0) {
        if (msgEl) {
            msgEl.style.display = 'block';
            msgEl.textContent = '暂无数据，请确认 marketinfo / index_daily 已有可匹配窗口';
        }
        return;
    }
    var labels = [];
    var tradeDateFull = [];
    for (var i = 0; i < n; i++) {
        var t = times[i];
        var full = '';
        if (t != null && typeof t === 'string' && t.length >= 10) {
            full = t.slice(0, 10);
        } else {
            try {
                var d = new Date(t);
                if (!isNaN(d.getTime())) {
                    var y = d.getFullYear();
                    var mo = d.getMonth() + 1;
                    var day = d.getDate();
                    full = y + '-' + (mo < 10 ? '0' : '') + mo + '-' + (day < 10 ? '0' : '') + day;
                }
            } catch (e1) { full = ''; }
        }
        tradeDateFull.push(full);
        labels.push(full.length >= 10 ? full.slice(5, 10) : String(i));
    }
    var closeSlice = close.slice(0, n);
    var upSlice = upNumMa5.slice(0, n);
    var validClose = closeSlice.filter(function (v) { return v != null && !isNaN(Number(v)); });
    var closeMin;
    var closeMax;
    if (validClose.length > 0) {
        closeMin = Math.min.apply(null, validClose.map(Number));
        closeMax = Math.max.apply(null, validClose.map(Number));
        if (closeMin === closeMax) {
            var padC = closeMin * 0.01 || 1;
            closeMin -= padC;
            closeMax += padC;
        } else {
            var rngC = closeMax - closeMin;
            closeMin -= rngC * 0.05;
            closeMax += rngC * 0.05;
        }
    }
    var line20 = { label: '20', data: Array(n).fill(20), yAxisID: 'yPer', borderColor: 'rgba(255, 193, 7, 0.9)', borderWidth: 1, borderDash: [4, 2], tension: 0, fill: false, pointRadius: 0 };
    var line25 = { label: '25', data: Array(n).fill(25), yAxisID: 'yPer', borderColor: 'rgba(255, 193, 7, 0.9)', borderWidth: 1, borderDash: [4, 2], tension: 0, fill: false, pointRadius: 0 };
    /* 初始只显示最近约 300 根；limits 用数值 0..n-1，避免「整段 5 年一次画满」时 original 与视窗一致导致无法平移（与融资占比弹窗行为对齐） */
    var viewWin = Math.min(300, n);
    var xIdxMin = n > 0 ? Math.max(0, n - viewWin) : 0;
    var xIdxMax = n > 0 ? n - 1 : 0;
    var xMaxBound = n > 0 ? n - 1 : 0;
    var zoomOpts = {
        limits: {
            x: { min: 0, max: xMaxBound, minRange: 15 },
        },
        pan: {
            enabled: true,
            mode: 'x',
            modifierKey: null,
            threshold: 5,
        },
        zoom: {
            wheel: { enabled: true, modifierKey: null },
            pinch: { enabled: true },
            mode: 'x',
        },
    };
    if (typeof Hammer === 'undefined') {
        zoomOpts.pan.enabled = false;
        zoomOpts.zoom.pinch.enabled = false;
        logError('Hammer.js 未加载，超跌弹窗仅支持滚轮缩放，平移已关闭');
    }
    try {
        upNumMa5ModalChartInstance = new Chart(canvas, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: '国证A指收盘',
                        data: closeSlice,
                        yAxisID: 'yClose',
                        borderColor: 'rgba(155, 89, 182, 1)',
                        backgroundColor: 'rgba(155, 89, 182, 0.08)',
                        tension: 0.35,
                        fill: false,
                        pointRadius: 0,
                        pointHoverRadius: 3,
                        borderWidth: 1.5,
                    },
                    {
                        label: '五日上涨占比(%)',
                        data: upSlice,
                        yAxisID: 'yPer',
                        borderColor: MARGIN_RZMRE_PCT_LINE,
                        backgroundColor: MARGIN_RZMRE_PCT_FILL,
                        tension: 0.35,
                        fill: false,
                        pointRadius: 0,
                        pointHoverRadius: 3,
                        borderWidth: 1.5,
                    },
                    line20,
                    line25,
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                animation: false,
                layout: { padding: { bottom: 14 } },
                plugins: {
                    legend: { display: false },
                    zoom: zoomOpts,
                    tooltip: {
                        displayColors: false,
                        callbacks: {
                            title: function (items) {
                                if (!items || !items.length) return '';
                                var ch = items[0].chart;
                                var idx = items[0].dataIndex;
                                var dates = ch.$upNumMa5TradeDates;
                                var full = dates && dates[idx];
                                return full ? ('日期：' + full) : '';
                            },
                            label: function (ctx) {
                                if (ctx.datasetIndex >= 2) return '';
                                var v = ctx.parsed.y;
                                if (v === null || v === undefined || isNaN(v)) return '';
                                if (ctx.datasetIndex === 0) {
                                    return '国证A指收盘：' + formatAxisInt(v) + ' 点';
                                }
                                return '五日上涨占比：' + v.toFixed(2) + ' %';
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        type: 'category',
                        min: xIdxMin,
                        max: xIdxMax,
                        display: true,
                        grid: { display: false },
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT, lineHeight: 1.35 },
                            autoSkip: true,
                            autoSkipPadding: 2,
                            maxRotation: 0,
                            maxTicksLimit: 42,
                            callback: function (tickVal) {
                                var idx = tickVal;
                                var chart = this.chart;
                                var dates = chart.$upNumMa5TradeDates;
                                if (!dates || dates[idx] == null || dates[idx] === '') return '';
                                var full = String(dates[idx]);
                                var prev = idx > 0 && dates[idx - 1] != null && dates[idx - 1] !== ''
                                    ? String(dates[idx - 1])
                                    : null;
                                return formatMarginRzmreIdxXAxisTick(full, prev);
                            },
                        },
                    },
                    yClose: {
                        type: 'linear',
                        position: 'left',
                        title: { display: false },
                        min: validClose.length ? closeMin : undefined,
                        max: validClose.length ? closeMax : undefined,
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                            callback: function (val) { return formatAxisInt(val); },
                        },
                        grid: {
                            color: function (ctx) {
                                var v = ctx.tick && ctx.tick.value;
                                return v === 0 ? '#b0bec5' : '#eceff1';
                            },
                            lineWidth: function (ctx) {
                                var v = ctx.tick && ctx.tick.value;
                                return v === 0 ? 1.2 : 0.5;
                            },
                        },
                    },
                    yPer: {
                        type: 'linear',
                        position: 'right',
                        title: { display: false },
                        min: 0,
                        max: 100,
                        grid: { drawOnChartArea: false },
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                            callback: function (val) { return formatAxisPct1(val); },
                        },
                    },
                },
            },
        });
    } catch (e) {
        if (msgEl) {
            msgEl.style.display = 'block';
            msgEl.textContent = '图表渲染失败: ' + (e && e.message ? e.message : '未知错误');
        }
        return;
    }
    upNumMa5ModalChartInstance.$upNumMa5TradeDates = tradeDateFull;
    requestAnimationFrame(function () {
        if (!upNumMa5ModalChartInstance) return;
        try {
            upNumMa5ModalChartInstance.resize();
        } catch (e2) {
            logError('超跌弹窗图 resize 失败', e2);
        }
        /* 再补一次 zoomScale，与 plugin 内部状态同步（category 轴已设 min/max，此处通常等价） */
        requestAnimationFrame(function () {
            if (!upNumMa5ModalChartInstance || typeof upNumMa5ModalChartInstance.zoomScale !== 'function') return;
            if (n <= 0) return;
            try {
                upNumMa5ModalChartInstance.zoomScale('x', { min: xIdxMin, max: xIdxMax }, 'default');
            } catch (ez) {
                logError('超跌弹窗 zoomScale 同步失败', ez);
            }
        });
    });
}

function openUpNumMa5ExpandModal() {
    var overlay = document.getElementById('up-num-ma5-modal-overlay');
    var loadingEl = document.getElementById('up-num-ma5-modal-loading');
    var msgEl = document.getElementById('up-num-ma5-modal-msg');
    if (!overlay) return;
    upNumMa5ModalLayoutOpenTs = Date.now();
    if (msgEl) {
        msgEl.style.display = 'none';
        msgEl.textContent = '';
    }
    overlay.classList.add('is-open'); _modalOpened();
    applyUpNumMa5ModalSavedLayout();
    if (loadingEl) loadingEl.style.display = 'flex';
    if (upNumMa5ModalChartInstance) {
        upNumMa5ModalChartInstance.destroy();
        upNumMa5ModalChartInstance = null;
    }
    var lim = UP_NUM_MA5_MODAL_SERIES_LIMIT;
    var reqUrl = rtMonitorApiUrl('/api/up_num_ma5_series?limit=' + encodeURIComponent(String(lim)));
    var reqToken = null;
    fetchPopupJson(reqUrl, 'upNumMa5')
        .then(function (o) {
            reqToken = o.token;
            if (!isPopupFetchCurrent('upNumMa5', o.token)) return;
            if (loadingEl) loadingEl.style.display = 'none';
            var data = o.data;
            if (!o.ok) {
                if (msgEl) {
                    msgEl.style.display = 'block';
                    msgEl.textContent = '加载失败：HTTP ' + o.status;
                }
                return;
            }
            if (!data || data.error) {
                if (msgEl) {
                    msgEl.style.display = 'block';
                    msgEl.textContent = data && data.error ? String(data.error) : '加载失败';
                }
                return;
            }
            renderUpNumMa5ModalChart(data);
        })
        .catch(function (err) {
            reqToken = err && err.__popupToken ? err.__popupToken : reqToken;
            if (!isPopupFetchCurrent('upNumMa5', err && err.__popupToken)) return;
            if (loadingEl) loadingEl.style.display = 'none';
            logError('up_num_ma5 弹窗数据失败', err);
            if (msgEl) {
                msgEl.style.display = 'block';
                msgEl.textContent = err && err.name === 'AbortError' ? '请求超时或已取消，请重试' : '请求失败，请检查网络或服务';
            }
        })
        .finally(function () {
            if (reqToken) endPopupFetch('upNumMa5', reqToken);
        });
}

function initUpNumMa5Modal() {
    var overlay = document.getElementById('up-num-ma5-modal-overlay');
    var modal = overlay && overlay.querySelector('.bias-series-modal');
    var header = overlay && overlay.querySelector('.bias-series-modal-header');
    var closeBtn = document.getElementById('up-num-ma5-modal-close');
    if (closeBtn) closeBtn.addEventListener('click', closeUpNumMa5Modal);
    if (overlay) {
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) closeUpNumMa5Modal();
        });
    }
    if (modal && header) {
        modal.style.position = 'fixed';
        modal.style.left = '50%';
        modal.style.top = '50%';
        modal.style.transform = 'translate(-50%, -50%)';
        var dragging = false;
        var startX = 0;
        var startY = 0;
        var startLeft = 0;
        var startTop = 0;
        header.style.cursor = 'move';
        header.addEventListener('mousedown', function (e) {
            if (e.button !== 0) return;
            dragging = true;
            startX = e.clientX;
            startY = e.clientY;
            var rect = modal.getBoundingClientRect();
            startLeft = rect.left;
            startTop = rect.top;
            modal.style.transform = '';
            e.preventDefault();
        });
        document.addEventListener('mousemove', function (e) {
            if (!dragging) return;
            var dx = e.clientX - startX;
            var dy = e.clientY - startY;
            var newLeft = startLeft + dx;
            var newTop = startTop + dy;
            var vw = window.innerWidth || document.documentElement.clientWidth;
            var vh = window.innerHeight || document.documentElement.clientHeight;
            var rect = modal.getBoundingClientRect();
            var maxLeft = vw - rect.width;
            var maxTop = vh - rect.height;
            if (newLeft < 0) newLeft = 0;
            if (newTop < 0) newTop = 0;
            if (newLeft > maxLeft) newLeft = maxLeft;
            if (newTop > maxTop) newTop = maxTop;
            modal.style.left = newLeft + 'px';
            modal.style.top = newTop + 'px';
        });
        document.addEventListener('mouseup', function () {
            if (dragging) {
                saveUpNumMa5ModalLayout(modal);
            }
            dragging = false;
        });
    }
    var modalChartWrap = document.getElementById('up-num-ma5-modal-chart-wrap');
    if (modalChartWrap && typeof ResizeObserver !== 'undefined') {
        var ro = new ResizeObserver(function () {
            if (upNumMa5ModalChartInstance && typeof upNumMa5ModalChartInstance.resize === 'function') {
                upNumMa5ModalChartInstance.resize();
            }
        });
        ro.observe(modalChartWrap);
    }
    if (modal && modal.classList && modal.classList.contains('up-num-ma5-modal-shell') && typeof ResizeObserver !== 'undefined') {
        var roShell = new ResizeObserver(function () {
            scheduleSaveUpNumMa5ModalLayout(modal);
        });
        roShell.observe(modal);
    }
}

function loadAmountTop5PctModalLayout() {
    try {
        if (typeof localStorage === 'undefined') return null;
        var raw = localStorage.getItem(AMOUNT_TOP5_PCT_MODAL_LAYOUT_STORAGE_KEY);
        if (!raw) return null;
        var o = JSON.parse(raw);
        if (typeof o.w !== 'number' || typeof o.h !== 'number') return null;
        if (o.w < 320 || o.h < 320) return null;
        return {
            w: o.w,
            h: o.h,
            l: typeof o.l === 'number' ? o.l : null,
            t: typeof o.t === 'number' ? o.t : null,
        };
    } catch (e) {
        return null;
    }
}

function saveAmountTop5PctModalLayout(modalEl) {
    try {
        if (!modalEl || typeof localStorage === 'undefined') return;
        var rect = modalEl.getBoundingClientRect();
        var w = Math.round(rect.width);
        var h = Math.round(rect.height);
        var l = Math.round(rect.left);
        var t = Math.round(rect.top);
        if (w < 320 || h < 320) return;
        localStorage.setItem(AMOUNT_TOP5_PCT_MODAL_LAYOUT_STORAGE_KEY, JSON.stringify({ w: w, h: h, l: l, t: t }));
    } catch (e) {
        /* ignore */
    }
}

function applyAmountTop5PctModalSavedLayout() {
    var overlay = document.getElementById('amount-top5-pct-modal-overlay');
    var modal = overlay && overlay.querySelector('.amount-top5-pct-modal-shell');
    if (!modal) return;
    var lay = loadAmountTop5PctModalLayout();
    if (!lay) return;
    var vw = window.innerWidth || document.documentElement.clientWidth;
    var vh = window.innerHeight || document.documentElement.clientHeight;
    modal.style.width = lay.w + 'px';
    modal.style.height = lay.h + 'px';
    modal.style.transform = '';
    var l = lay.l != null ? lay.l : Math.max(0, (vw - lay.w) / 2);
    var t = lay.t != null ? lay.t : Math.max(0, (vh - lay.h) / 2);
    l = Math.max(0, Math.min(l, vw - lay.w));
    t = Math.max(0, Math.min(t, vh - lay.h));
    modal.style.left = l + 'px';
    modal.style.top = t + 'px';
}

function scheduleSaveAmountTop5PctModalLayout(modalEl) {
    if (!modalEl) return;
    clearTimeout(amountTop5PctModalSaveTimer);
    amountTop5PctModalSaveTimer = setTimeout(function () {
        if (Date.now() - amountTop5PctModalLayoutOpenTs < 700) return;
        saveAmountTop5PctModalLayout(modalEl);
    }, 400);
}

function closeAmountTop5PctModal() {
    var overlay = document.getElementById('amount-top5-pct-modal-overlay');
    var modal = overlay && overlay.querySelector('.amount-top5-pct-modal-shell');
    var wasOpen = overlay && overlay.classList.contains('is-open');
    if (wasOpen && modal) {
        saveAmountTop5PctModalLayout(modal);
    }
    if (overlay) overlay.classList.remove('is-open');
    if (amountTop5PctModalChartInstance) {
        amountTop5PctModalChartInstance.destroy();
        amountTop5PctModalChartInstance = null;
    }
    abortPopupFetch('amountTop5Pct');
    var ld = document.getElementById('amount-top5-pct-modal-loading');
    if (ld) ld.style.display = 'none';
    if (wasOpen) _modalClosed();
}

/** 成交额 Top5%_pct 弹窗：视窗右缘索引（category 轴 zoom/pan 后 x.max）。 */
function getAmountTop5PctViewportRightIdx(chart) {
    if (!chart || !chart.scales || !chart.scales.x) return 0;
    var n = chart.data && chart.data.labels ? chart.data.labels.length : 0;
    if (n <= 0) return 0;
    var max = chart.scales.x.max;
    if (max == null || isNaN(Number(max))) return n - 1;
    return Math.min(n - 1, Math.max(0, Math.round(Number(max))));
}

/** 视窗右缘交易日对应的滚动 2σ 值（左轴黄字刻度用）。 */
function getAmountTop5PctSigma2NumsAtViewportRight(chart) {
    if (!chart) return { low: null, high: null };
    var idx = getAmountTop5PctViewportRightIdx(chart);
    var lowArr = chart.$amountTop5PctSigma2Low;
    var highArr = chart.$amountTop5PctSigma2High;
    var low = null;
    var high = null;
    if (lowArr && lowArr[idx] != null && !isNaN(Number(lowArr[idx]))) low = Number(lowArr[idx]);
    if (highArr && highArr[idx] != null && !isNaN(Number(highArr[idx]))) high = Number(highArr[idx]);
    return { low: low, high: high };
}

function normalizeAmountTop5Sigma2Series(raw, n) {
    var out = [];
    var i;
    if (!Array.isArray(raw)) {
        var scalar = raw != null && !isNaN(Number(raw)) ? Number(raw) : null;
        for (i = 0; i < n; i++) out.push(scalar);
        return out;
    }
    for (i = 0; i < n; i++) {
        var v = i < raw.length ? raw[i] : null;
        out.push(v == null || isNaN(Number(v)) ? null : Number(v));
    }
    return out;
}

/** 成交额 Top5%_pct 弹窗：左轴占比 + 滚动五年 2σ 黄虚线，右轴国证A指收盘；与超跌弹窗同款 zoom/pan。 */
function renderAmountTop5PctModalChart(payload) {
    var canvas = document.getElementById('amount-top5-pct-modal-chart');
    var msgEl = document.getElementById('amount-top5-pct-modal-msg');
    if (!canvas || !window.Chart) {
        logError('成交额 Top5%_pct 弹窗 canvas 或 Chart.js 不可用');
        return;
    }
    if (amountTop5PctModalChartInstance) {
        amountTop5PctModalChartInstance.destroy();
        amountTop5PctModalChartInstance = null;
    }
    if (msgEl) {
        msgEl.style.display = 'none';
        msgEl.textContent = '';
    }
    var times = payload.times || [];
    var pctArr = payload.amount_top5_pct || [];
    var closeArr = payload.close || [];
    var n = Math.min(times.length, pctArr.length, closeArr.length);
    if (n === 0) {
        if (msgEl) {
            msgEl.style.display = 'block';
            msgEl.textContent = '暂无数据，请确认 marketinfo / index_daily 已有可匹配窗口';
        }
        return;
    }
    var labels = [];
    var tradeDateFull = [];
    for (var i = 0; i < n; i++) {
        var t = times[i];
        var full = '';
        if (t != null && typeof t === 'string' && t.length >= 10) {
            full = t.slice(0, 10);
        } else {
            try {
                var d = new Date(t);
                if (!isNaN(d.getTime())) {
                    var y = d.getFullYear();
                    var mo = d.getMonth() + 1;
                    var day = d.getDate();
                    full = y + '-' + (mo < 10 ? '0' : '') + mo + '-' + (day < 10 ? '0' : '') + day;
                }
            } catch (e1) { full = ''; }
        }
        tradeDateFull.push(full);
        labels.push(full.length >= 10 ? full.slice(5, 10) : String(i));
    }
    var pctSlice = pctArr.slice(0, n);
    var closeSlice = closeArr.slice(0, n);
    var sigma2LowSeries = normalizeAmountTop5Sigma2Series(payload.sigma2_low, n);
    var sigma2HighSeries = normalizeAmountTop5Sigma2Series(payload.sigma2_high, n);
    var validPct = pctSlice.filter(function (v) { return v != null && !isNaN(Number(v)); });
    var validClose = closeSlice.filter(function (v) { return v != null && !isNaN(Number(v)); });
    var pctMin;
    var pctMax;
    if (validPct.length > 0) {
        pctMin = Math.min.apply(null, validPct.map(Number));
        pctMax = Math.max.apply(null, validPct.map(Number));
        sigma2LowSeries.forEach(function (v) {
            if (v != null && !isNaN(v)) {
                pctMin = Math.min(pctMin, v);
                pctMax = Math.max(pctMax, v);
            }
        });
        sigma2HighSeries.forEach(function (v) {
            if (v != null && !isNaN(v)) {
                pctMin = Math.min(pctMin, v);
                pctMax = Math.max(pctMax, v);
            }
        });
        if (pctMin === pctMax) {
            var padP = pctMin * 0.01 || 1;
            pctMin -= padP;
            pctMax += padP;
        } else {
            var rngP = pctMax - pctMin;
            pctMin -= rngP * 0.05;
            pctMax += rngP * 0.05;
        }
    }
    var closeMin;
    var closeMax;
    if (validClose.length > 0) {
        closeMin = Math.min.apply(null, validClose.map(Number));
        closeMax = Math.max.apply(null, validClose.map(Number));
        if (closeMin === closeMax) {
            var padC = closeMin * 0.01 || 1;
            closeMin -= padC;
            closeMax += padC;
        } else {
            var rngC = closeMax - closeMin;
            closeMin -= rngC * 0.05;
            closeMax += rngC * 0.05;
        }
    }
    var viewWin = Math.min(300, n);
    var xIdxMin = n > 0 ? Math.max(0, n - viewWin) : 0;
    var xIdxMax = n > 0 ? n - 1 : 0;
    var xMaxBound = n > 0 ? n - 1 : 0;
    function refreshAmountTop5PctSigma2Axis(chart) {
        if (chart) chart.update('none');
    }
    var zoomOpts = {
        limits: { x: { min: 0, max: xMaxBound, minRange: 15 } },
        pan: {
            enabled: true,
            mode: 'x',
            modifierKey: null,
            threshold: 5,
            onPanComplete: function (ctx) {
                if (ctx && ctx.chart) refreshAmountTop5PctSigma2Axis(ctx.chart);
            },
        },
        zoom: {
            wheel: { enabled: true, modifierKey: null },
            pinch: { enabled: true },
            mode: 'x',
            onZoomComplete: function (ctx) {
                if (ctx && ctx.chart) refreshAmountTop5PctSigma2Axis(ctx.chart);
            },
        },
    };
    if (typeof Hammer === 'undefined') {
        zoomOpts.pan.enabled = false;
        zoomOpts.zoom.pinch.enabled = false;
        logError('Hammer.js 未加载，成交额 Top5%_pct 弹窗仅支持滚轮缩放，平移已关闭');
    }
    var datasets = [
        {
            label: 'Top5%成交额占比',
            data: pctSlice,
            yAxisID: 'yPct',
            borderColor: '#dc2626',
            backgroundColor: 'rgba(220, 38, 38, 0.08)',
            tension: 0.35,
            fill: false,
            pointRadius: 0,
            pointHoverRadius: 3,
            borderWidth: 1.5,
        },
        {
            label: '国证A指收盘',
            data: closeSlice,
            yAxisID: 'yClose',
            borderColor: 'rgba(155, 89, 182, 1)',
            backgroundColor: 'rgba(155, 89, 182, 0.08)',
            tension: 0.35,
            fill: false,
            pointRadius: 0,
            pointHoverRadius: 3,
            borderWidth: 1.5,
        },
    ];
    var hasSigma2Low = sigma2LowSeries.some(function (v) { return v != null && !isNaN(v); });
    var hasSigma2High = sigma2HighSeries.some(function (v) { return v != null && !isNaN(v); });
    if (hasSigma2Low) {
        datasets.push({
            label: '2σ下',
            data: sigma2LowSeries,
            yAxisID: 'yPct',
            borderColor: 'rgba(255, 193, 7, 0.9)',
            borderWidth: 1,
            borderDash: [4, 2],
            tension: 0.35,
            spanGaps: false,
            fill: false,
            pointRadius: 0,
        });
    }
    if (hasSigma2High) {
        datasets.push({
            label: '2σ上',
            data: sigma2HighSeries,
            yAxisID: 'yPct',
            borderColor: 'rgba(255, 193, 7, 0.9)',
            borderWidth: 1,
            borderDash: [4, 2],
            tension: 0.35,
            spanGaps: false,
            fill: false,
            pointRadius: 0,
        });
    }
    try {
        amountTop5PctModalChartInstance = new Chart(canvas, {
            type: 'line',
            data: { labels: labels, datasets: datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                animation: false,
                layout: { padding: { bottom: 14 } },
                plugins: {
                    legend: { display: false },
                    zoom: zoomOpts,
                    tooltip: {
                        displayColors: false,
                        callbacks: {
                            title: function (items) {
                                if (!items || !items.length) return '';
                                var ch = items[0].chart;
                                var idx = items[0].dataIndex;
                                var dates = ch.$amountTop5PctTradeDates;
                                var full = dates && dates[idx];
                                return full ? ('日期：' + full) : '';
                            },
                            label: function (ctx) {
                                if (ctx.datasetIndex >= 2) return '';
                                var v = ctx.parsed.y;
                                if (v === null || v === undefined || isNaN(v)) return '';
                                if (ctx.datasetIndex === 0) {
                                    return 'Top5%成交额占比：' + v.toFixed(2) + ' %';
                                }
                                return '国证A指收盘：' + formatAxisInt(v) + ' 点';
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        type: 'category',
                        min: xIdxMin,
                        max: xIdxMax,
                        display: true,
                        grid: { display: false },
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT, lineHeight: 1.35 },
                            autoSkip: true,
                            autoSkipPadding: 2,
                            maxRotation: 0,
                            maxTicksLimit: 42,
                            callback: function (tickVal) {
                                var idx = tickVal;
                                var chart = this.chart;
                                var dates = chart.$amountTop5PctTradeDates;
                                if (!dates || dates[idx] == null || dates[idx] === '') return '';
                                var full = String(dates[idx]);
                                var prev = idx > 0 && dates[idx - 1] != null && dates[idx - 1] !== ''
                                    ? String(dates[idx - 1])
                                    : null;
                                return formatMarginRzmreIdxXAxisTick(full, prev);
                            },
                        },
                    },
                    yPct: {
                        type: 'linear',
                        position: 'left',
                        title: { display: false },
                        min: validPct.length ? pctMin : undefined,
                        max: validPct.length ? pctMax : undefined,
                        afterBuildTicks: function (axis) {
                            var chart = axis.chart;
                            var sig = getAmountTop5PctSigma2NumsAtViewportRight(chart);
                            var sigma2LowNum = sig.low;
                            var sigma2HighNum = sig.high;
                            if (sigma2LowNum == null && sigma2HighNum == null) return;
                            var seen = {};
                            var merged = [];
                            axis.ticks.forEach(function (t) {
                                var v = (typeof t === 'object' && t !== null) ? t.value : t;
                                if (v == null || isNaN(Number(v)) || seen[v]) return;
                                seen[v] = true;
                                merged.push({ value: Number(v) });
                            });
                            if (sigma2LowNum != null && !seen[sigma2LowNum]) {
                                merged.push({ value: sigma2LowNum });
                            }
                            if (sigma2HighNum != null && !seen[sigma2HighNum]) {
                                merged.push({ value: sigma2HighNum });
                            }
                            merged.sort(function (a, b) { return a.value - b.value; });
                            axis.ticks = merged;
                        },
                        ticks: {
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                            color: function (ctx) {
                                var chart = ctx.chart;
                                var sig = getAmountTop5PctSigma2NumsAtViewportRight(chart);
                                var val = ctx.tick && ctx.tick.value;
                                if (sig.low != null && Math.abs(val - sig.low) < 0.08) {
                                    return 'rgba(255, 193, 7, 1)';
                                }
                                if (sig.high != null && Math.abs(val - sig.high) < 0.08) {
                                    return 'rgba(255, 193, 7, 1)';
                                }
                                return '#555';
                            },
                            callback: function (val) {
                                var chart = this.chart;
                                var sig = getAmountTop5PctSigma2NumsAtViewportRight(chart);
                                if (sig.low != null && Math.abs(val - sig.low) < 0.08) {
                                    return sig.low.toFixed(2);
                                }
                                if (sig.high != null && Math.abs(val - sig.high) < 0.08) {
                                    return sig.high.toFixed(2);
                                }
                                return formatAxisPct1(val);
                            },
                        },
                        grid: {
                            color: function (ctx) {
                                var v = ctx.tick && ctx.tick.value;
                                return v === 0 ? '#b0bec5' : '#eceff1';
                            },
                            lineWidth: function (ctx) {
                                var v = ctx.tick && ctx.tick.value;
                                return v === 0 ? 1.2 : 0.5;
                            },
                        },
                    },
                    yClose: {
                        type: 'linear',
                        position: 'right',
                        title: { display: false },
                        min: validClose.length ? closeMin : undefined,
                        max: validClose.length ? closeMax : undefined,
                        grid: { drawOnChartArea: false },
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                            callback: function (val) { return formatAxisInt(val); },
                        },
                    },
                },
            },
        });
    } catch (e) {
        if (msgEl) {
            msgEl.style.display = 'block';
            msgEl.textContent = '图表渲染失败: ' + (e && e.message ? e.message : '未知错误');
        }
        return;
    }
    amountTop5PctModalChartInstance.$amountTop5PctTradeDates = tradeDateFull;
    amountTop5PctModalChartInstance.$amountTop5PctSigma2Low = sigma2LowSeries;
    amountTop5PctModalChartInstance.$amountTop5PctSigma2High = sigma2HighSeries;
    requestAnimationFrame(function () {
        if (!amountTop5PctModalChartInstance) return;
        try {
            amountTop5PctModalChartInstance.resize();
        } catch (e2) {
            logError('成交额 Top5%_pct 弹窗图 resize 失败', e2);
        }
        requestAnimationFrame(function () {
            if (!amountTop5PctModalChartInstance || typeof amountTop5PctModalChartInstance.zoomScale !== 'function') return;
            if (n <= 0) return;
            try {
                amountTop5PctModalChartInstance.zoomScale('x', { min: xIdxMin, max: xIdxMax }, 'default');
            } catch (ez) {
                logError('成交额 Top5%_pct 弹窗 zoomScale 同步失败', ez);
            }
        });
    });
}

function openAmountTop5PctExpandModal() {
    var overlay = document.getElementById('amount-top5-pct-modal-overlay');
    var loadingEl = document.getElementById('amount-top5-pct-modal-loading');
    var msgEl = document.getElementById('amount-top5-pct-modal-msg');
    if (!overlay) return;
    amountTop5PctModalLayoutOpenTs = Date.now();
    if (msgEl) {
        msgEl.style.display = 'none';
        msgEl.textContent = '';
    }
    overlay.classList.add('is-open'); _modalOpened();
    applyAmountTop5PctModalSavedLayout();
    if (loadingEl) loadingEl.style.display = 'flex';
    if (amountTop5PctModalChartInstance) {
        amountTop5PctModalChartInstance.destroy();
        amountTop5PctModalChartInstance = null;
    }
    var reqUrl = rtMonitorApiUrl(
        '/api/amount_top5_pct_series?start_date=' + encodeURIComponent(AMOUNT_TOP5_PCT_MODAL_START_DATE)
        + '&limit=' + encodeURIComponent(String(AMOUNT_TOP5_PCT_MODAL_SERIES_CAP))
    );
    var reqToken = null;
    fetchPopupJson(reqUrl, 'amountTop5Pct', 20000)
        .then(function (o) {
            reqToken = o.token;
            if (!isPopupFetchCurrent('amountTop5Pct', o.token)) return;
            if (loadingEl) loadingEl.style.display = 'none';
            var data = o.data;
            if (!o.ok) {
                if (msgEl) {
                    msgEl.style.display = 'block';
                    msgEl.textContent = '加载失败：HTTP ' + o.status;
                }
                return;
            }
            if (!data || data.error) {
                if (msgEl) {
                    msgEl.style.display = 'block';
                    msgEl.textContent = data && data.error ? String(data.error) : '加载失败';
                }
                return;
            }
            renderAmountTop5PctModalChart(data);
        })
        .catch(function (err) {
            reqToken = err && err.__popupToken ? err.__popupToken : reqToken;
            if (!isPopupFetchCurrent('amountTop5Pct', err && err.__popupToken)) return;
            if (loadingEl) loadingEl.style.display = 'none';
            logError('amount_top5_pct 弹窗数据失败', err);
            if (msgEl) {
                msgEl.style.display = 'block';
                msgEl.textContent = err && err.name === 'AbortError' ? '请求超时或已取消，请重试' : '请求失败，请检查网络或服务';
            }
        })
        .finally(function () {
            if (reqToken) endPopupFetch('amountTop5Pct', reqToken);
        });
}

function initAmountTop5PctModal() {
    var overlay = document.getElementById('amount-top5-pct-modal-overlay');
    var modal = overlay && overlay.querySelector('.amount-top5-pct-modal-shell');
    var header = overlay && overlay.querySelector('.bias-series-modal-header');
    var closeBtn = document.getElementById('amount-top5-pct-modal-close');
    var headerPct = document.getElementById('amount-top5-pct-header');
    if (closeBtn) closeBtn.addEventListener('click', closeAmountTop5PctModal);
    if (headerPct) {
        headerPct.addEventListener('click', function (e) {
            e.preventDefault();
            openAmountTop5PctExpandModal();
        });
    }
    if (overlay) {
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) closeAmountTop5PctModal();
        });
    }
    if (modal && header) {
        modal.style.position = 'fixed';
        modal.style.left = '50%';
        modal.style.top = '50%';
        modal.style.transform = 'translate(-50%, -50%)';
        var dragging = false;
        var startX = 0;
        var startY = 0;
        var startLeft = 0;
        var startTop = 0;
        header.style.cursor = 'move';
        header.addEventListener('mousedown', function (e) {
            if (e.button !== 0) return;
            dragging = true;
            startX = e.clientX;
            startY = e.clientY;
            var rect = modal.getBoundingClientRect();
            startLeft = rect.left;
            startTop = rect.top;
            modal.style.transform = '';
            e.preventDefault();
        });
        document.addEventListener('mousemove', function (e) {
            if (!dragging) return;
            var dx = e.clientX - startX;
            var dy = e.clientY - startY;
            var newLeft = startLeft + dx;
            var newTop = startTop + dy;
            var vw = window.innerWidth || document.documentElement.clientWidth;
            var vh = window.innerHeight || document.documentElement.clientHeight;
            var rect = modal.getBoundingClientRect();
            var maxLeft = vw - rect.width;
            var maxTop = vh - rect.height;
            if (newLeft < 0) newLeft = 0;
            if (newTop < 0) newTop = 0;
            if (newLeft > maxLeft) newLeft = maxLeft;
            if (newTop > maxTop) newTop = maxTop;
            modal.style.left = newLeft + 'px';
            modal.style.top = newTop + 'px';
        });
        document.addEventListener('mouseup', function () {
            if (dragging) {
                saveAmountTop5PctModalLayout(modal);
            }
            dragging = false;
        });
    }
    var modalChartWrap = document.getElementById('amount-top5-pct-modal-chart-wrap');
    if (modalChartWrap && typeof ResizeObserver !== 'undefined') {
        var ro = new ResizeObserver(function () {
            if (amountTop5PctModalChartInstance && typeof amountTop5PctModalChartInstance.resize === 'function') {
                amountTop5PctModalChartInstance.resize();
            }
        });
        ro.observe(modalChartWrap);
    }
    if (modal && modal.classList && modal.classList.contains('amount-top5-pct-modal-shell') && typeof ResizeObserver !== 'undefined') {
        var roShell = new ResizeObserver(function () {
            scheduleSaveAmountTop5PctModalLayout(modal);
        });
        roShell.observe(modal);
    }
}

/**
 * 净买入/融资余额弹窗：与主图同柱+线，chartjs-plugin-zoom 横向平移与滚轮缩放；初始视窗最近约 300 个交易日。
 */
function renderMarginDailyModalChart(rows) {
    var canvas = document.getElementById('margin-daily-modal-chart');
    var msgEl = document.getElementById('margin-daily-modal-msg');
    if (!canvas || !window.Chart) {
        logError('净买入弹窗 canvas 或 Chart.js 不可用');
        return;
    }
    if (marginDailyModalChartInstance) {
        marginDailyModalChartInstance.destroy();
        marginDailyModalChartInstance = null;
    }
    if (msgEl) {
        msgEl.style.display = 'none';
        msgEl.textContent = '';
    }
    var P = prepareMarginDailySeriesData(rows);
    if (!P) {
        if (msgEl) {
            msgEl.style.display = 'block';
            msgEl.textContent = '暂无融资融券日数据';
        }
        return;
    }
    var labels = P.labels;
    var netbuy = P.netbuy;
    var rzye = P.rzye;
    var netbuyPct = P.netbuyPct;
    var validRzye = P.validRzye;
    var rzyeMin = P.rzyeMin;
    var rzyeMax = P.rzyeMax;
    var tradeDateFull = P.tradeDateFull;
    var zoomOpts = {
        limits: {
            x: { min: 'original', max: 'original', minRange: 15 },
        },
        pan: {
            enabled: true,
            mode: 'x',
        },
        zoom: {
            wheel: { enabled: true },
            pinch: { enabled: true },
            mode: 'x',
        },
    };
    if (typeof Hammer === 'undefined') {
        zoomOpts.pan.enabled = false;
        zoomOpts.zoom.pinch.enabled = false;
        logError('Hammer.js 未加载，净买入弹窗仅支持滚轮缩放，平移已关闭');
    }
    marginDailyModalChartInstance = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [
                {
                    type: 'bar',
                    label: '净买入额（亿）',
                    data: netbuy,
                    yAxisID: 'yNetbuy',
                    backgroundColor: idxDiffBarColor,
                    borderWidth: 0,
                },
                {
                    type: 'line',
                    label: '融资余额（亿）',
                    data: rzye,
                    yAxisID: 'yRzye',
                    borderColor: 'rgba(155, 89, 182, 1)',
                    backgroundColor: 'rgba(155, 89, 182, 0.12)',
                    tension: 0.35,
                    fill: false,
                    pointRadius: 0,
                    pointHoverRadius: 3,
                    borderWidth: 2,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            animation: false,
            layout: {
                padding: { bottom: 14 },
            },
            plugins: {
                legend: { display: false },
                zoom: zoomOpts,
                tooltip: {
                    displayColors: false,
                    callbacks: {
                        title: function (items) {
                            if (!items || !items.length) return '';
                            var ch = items[0].chart;
                            var idx = items[0].dataIndex;
                            var dates = ch.$marginTradeDates;
                            var full = dates && dates[idx];
                            return full ? ('日期：' + full) : '';
                        },
                        label: function (ctx) {
                            var v = ctx.parsed.y;
                            if (v === null || v === undefined || isNaN(v)) return '';
                            if (ctx.datasetIndex === 0) {
                                return '净买入额：' + v.toFixed(2) + ' 亿';
                            }
                            return '融资余额：' + v.toFixed(2) + ' 亿';
                        },
                        afterBody: function (items) {
                            if (!items || !items.length) return [];
                            var ch = items[0].chart;
                            var idx = items[0].dataIndex;
                            var arr = ch.$marginNetbuyPct;
                            var p = arr && arr[idx];
                            if (p === null || p === undefined || isNaN(p)) return [];
                            return ['净买入%：' + parseFloat(p).toFixed(2) + '%'];
                        },
                    },
                },
            },
            scales: {
                x: {
                    display: true,
                    grid: { display: false },
                    ticks: {
                        color: '#555',
                        font: {
                            size: RT_MONITOR_CHART_FONT_SIZE,
                            family: PAGE_FONT,
                            lineHeight: 1.35,
                        },
                        autoSkip: true,
                        autoSkipPadding: 2,
                        maxRotation: 0,
                        maxTicksLimit: 42,
                        callback: function (tickVal) {
                            var idx = tickVal;
                            var chart = this.chart;
                            var dates = chart.$marginTradeDates;
                            if (!dates || dates[idx] == null || dates[idx] === '') return '';
                            var full = String(dates[idx]);
                            var prev = idx > 0 && dates[idx - 1] != null && dates[idx - 1] !== ''
                                ? String(dates[idx - 1])
                                : null;
                            return formatMarginRzmreIdxXAxisTick(full, prev);
                        },
                    },
                },
                yNetbuy: {
                    type: 'linear',
                    position: 'left',
                    title: { display: false },
                    ticks: {
                        color: '#555',
                        font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                        callback: function (val) { return formatAxisInt(val); },
                    },
                    grid: {
                        color: function (ctx) {
                            var v = ctx.tick && ctx.tick.value;
                            return v === 0 ? '#b0bec5' : '#eceff1';
                        },
                        lineWidth: function (ctx) {
                            var v = ctx.tick && ctx.tick.value;
                            return v === 0 ? 1.2 : 0.5;
                        },
                    },
                },
                yRzye: {
                    type: 'linear',
                    position: 'right',
                    grid: { drawOnChartArea: false },
                    title: { display: false },
                    min: validRzye.length ? rzyeMin : undefined,
                    max: validRzye.length ? rzyeMax : undefined,
                    ticks: {
                        color: '#555',
                        font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                        callback: function (val) { return formatAxisInt(val); },
                    },
                },
            },
        },
    });
    marginDailyModalChartInstance.$marginTradeDates = tradeDateFull;
    marginDailyModalChartInstance.$marginNetbuyPct = netbuyPct;
    var ch = marginDailyModalChartInstance;
    var n = labels.length;
    requestAnimationFrame(function () {
        if (!marginDailyModalChartInstance) return;
        if (n > 0 && typeof ch.zoomScale === 'function') {
            var win = Math.min(300, n);
            var zmin = Math.max(0, n - win);
            var zmax = n - 1;
            try {
                marginDailyModalChartInstance.zoomScale('x', { min: zmin, max: zmax }, 'default');
            } catch (e) {
                logError('净买入弹窗 zoomScale 初始视窗失败', e);
            }
        }
        try {
            marginDailyModalChartInstance.resize();
        } catch (e2) {
            logError('净买入弹窗图 resize 失败', e2);
        }
    });
}

/** 读取融资占比弹窗上次保存的宽高与位置 */
function loadMarginRzmreModalLayout() {
    try {
        if (typeof localStorage === 'undefined') return null;
        var raw = localStorage.getItem(MARGIN_RZMRE_MODAL_LAYOUT_STORAGE_KEY);
        if (!raw) return null;
        var o = JSON.parse(raw);
        if (typeof o.w !== 'number' || typeof o.h !== 'number') return null;
        if (o.w < 320 || o.h < 320) return null;
        return {
            w: o.w,
            h: o.h,
            l: typeof o.l === 'number' ? o.l : null,
            t: typeof o.t === 'number' ? o.t : null,
        };
    } catch (e) {
        return null;
    }
}

/** 持久化融资占比弹窗当前几何（关闭/拖拽结束/resize 防抖时调用） */
function saveMarginRzmreModalLayout(modalEl) {
    try {
        if (!modalEl || typeof localStorage === 'undefined') return;
        var rect = modalEl.getBoundingClientRect();
        var w = Math.round(rect.width);
        var h = Math.round(rect.height);
        var l = Math.round(rect.left);
        var t = Math.round(rect.top);
        if (w < 320 || h < 320) return;
        localStorage.setItem(MARGIN_RZMRE_MODAL_LAYOUT_STORAGE_KEY, JSON.stringify({ w: w, h: h, l: l, t: t }));
    } catch (e) {
        /* 配额、隐私模式 */
    }
}

/** 打开弹窗前应用上次保存的宽高与位置（与 CSS 初始居中二选一） */
function applyMarginRzmreModalSavedLayout() {
    var overlay = document.getElementById('margin-rzmre-idx-modal-overlay');
    var modal = overlay && overlay.querySelector('.margin-rzmre-idx-modal-shell');
    if (!modal) return;
    var lay = loadMarginRzmreModalLayout();
    if (!lay) return;
    var vw = window.innerWidth || document.documentElement.clientWidth;
    var vh = window.innerHeight || document.documentElement.clientHeight;
    var minW = 320;
    var minH = 320;
    var w = Math.min(Math.max(lay.w, minW), Math.max(minW, vw - 4));
    var h = Math.min(Math.max(lay.h, minH), Math.max(minH, vh - 4));
    modal.style.width = w + 'px';
    modal.style.height = h + 'px';
    modal.style.transform = 'none';
    var l = lay.l != null ? lay.l : (vw - w) / 2;
    var t = lay.t != null ? lay.t : (vh - h) / 2;
    l = Math.max(0, Math.min(l, vw - w));
    t = Math.max(0, Math.min(t, vh - h));
    modal.style.left = l + 'px';
    modal.style.top = t + 'px';
}

function scheduleSaveMarginRzmreModalLayout(modalEl) {
    if (!modalEl) return;
    clearTimeout(marginRzmreModalSaveTimer);
    marginRzmreModalSaveTimer = setTimeout(function () {
        if (Date.now() - marginRzmreModalLayoutOpenTs < 700) return;
        saveMarginRzmreModalLayout(modalEl);
    }, 400);
}

function loadMarginDailyModalLayout() {
    try {
        if (typeof localStorage === 'undefined') return null;
        var raw = localStorage.getItem(MARGIN_DAILY_MODAL_LAYOUT_STORAGE_KEY);
        if (!raw) return null;
        var o = JSON.parse(raw);
        if (typeof o.w !== 'number' || typeof o.h !== 'number') return null;
        if (o.w < 320 || o.h < 320) return null;
        return {
            w: o.w,
            h: o.h,
            l: typeof o.l === 'number' ? o.l : null,
            t: typeof o.t === 'number' ? o.t : null,
        };
    } catch (e) {
        return null;
    }
}

function saveMarginDailyModalLayout(modalEl) {
    try {
        if (!modalEl || typeof localStorage === 'undefined') return;
        var rect = modalEl.getBoundingClientRect();
        var w = Math.round(rect.width);
        var h = Math.round(rect.height);
        var l = Math.round(rect.left);
        var t = Math.round(rect.top);
        if (w < 320 || h < 320) return;
        localStorage.setItem(MARGIN_DAILY_MODAL_LAYOUT_STORAGE_KEY, JSON.stringify({ w: w, h: h, l: l, t: t }));
    } catch (e) {
        /* ignore */
    }
}

function applyMarginDailyModalSavedLayout() {
    var overlay = document.getElementById('margin-daily-modal-overlay');
    var modal = overlay && overlay.querySelector('.margin-daily-modal-shell');
    if (!modal) return;
    var lay = loadMarginDailyModalLayout();
    if (!lay) return;
    var vw = window.innerWidth || document.documentElement.clientWidth;
    var vh = window.innerHeight || document.documentElement.clientHeight;
    var minW = 320;
    var minH = 320;
    var w = Math.min(Math.max(lay.w, minW), Math.max(minW, vw - 4));
    var h = Math.min(Math.max(lay.h, minH), Math.max(minH, vh - 4));
    modal.style.width = w + 'px';
    modal.style.height = h + 'px';
    modal.style.transform = 'none';
    var l = lay.l != null ? lay.l : (vw - w) / 2;
    var t = lay.t != null ? lay.t : (vh - h) / 2;
    l = Math.max(0, Math.min(l, vw - w));
    t = Math.max(0, Math.min(t, vh - h));
    modal.style.left = l + 'px';
    modal.style.top = t + 'px';
}

function scheduleSaveMarginDailyModalLayout(modalEl) {
    if (!modalEl) return;
    clearTimeout(marginDailyModalSaveTimer);
    marginDailyModalSaveTimer = setTimeout(function () {
        if (Date.now() - marginDailyModalLayoutOpenTs < 700) return;
        saveMarginDailyModalLayout(modalEl);
    }, 400);
}

function closeMarginDailyModal() {
    var overlay = document.getElementById('margin-daily-modal-overlay');
    var modal = overlay && overlay.querySelector('.margin-daily-modal-shell');
    var wasOpen = overlay && overlay.classList.contains('is-open');
    if (wasOpen && modal) {
        saveMarginDailyModalLayout(modal);
    }
    if (overlay) overlay.classList.remove('is-open');
    if (marginDailyModalChartInstance) {
        marginDailyModalChartInstance.destroy();
        marginDailyModalChartInstance = null;
    }
    abortPopupFetch('marginDaily');
    var ld = document.getElementById('margin-daily-modal-loading');
    if (ld) ld.style.display = 'none';
    if (wasOpen) _modalClosed();
}

function openMarginDailyExpandModal() {
    var overlay = document.getElementById('margin-daily-modal-overlay');
    var loadingEl = document.getElementById('margin-daily-modal-loading');
    var msgEl = document.getElementById('margin-daily-modal-msg');
    if (!overlay) return;
    marginDailyModalLayoutOpenTs = Date.now();
    if (msgEl) {
        msgEl.style.display = 'none';
        msgEl.textContent = '';
    }
    overlay.classList.add('is-open'); _modalOpened();
    applyMarginDailyModalSavedLayout();
    if (loadingEl) loadingEl.style.display = 'flex';
    if (marginDailyModalChartInstance) {
        marginDailyModalChartInstance.destroy();
        marginDailyModalChartInstance = null;
    }
    var lim = MARGIN_DAILY_MODAL_SERIES_LIMIT;
    var cached = window.__rtMonitorMarginDailyModalRows;
    var dailyShort = window.__rtMonitorMarginDailyModalLenCapped === false;
    if (cached && Array.isArray(cached) && cached.length > 0 && (cached.length >= lim || dailyShort)) {
        if (loadingEl) loadingEl.style.display = 'none';
        renderMarginDailyModalChart(cached.slice());
        return;
    }
    var reqUrl = rtMonitorApiUrl('/api/margin_daily_recent?limit=' + encodeURIComponent(String(lim)));
    var reqToken = null;
    fetchPopupJson(reqUrl, 'marginDaily')
        .then(function (o) {
            reqToken = o.token;
            if (!isPopupFetchCurrent('marginDaily', o.token)) return;
            if (loadingEl) loadingEl.style.display = 'none';
            var data = o.data;
            if (!o.ok) {
                if (msgEl) {
                    msgEl.style.display = 'block';
                    msgEl.textContent = '加载失败：HTTP ' + o.status;
                }
                return;
            }
            if (!data || data.error) {
                if (msgEl) {
                    msgEl.style.display = 'block';
                    msgEl.textContent = data && data.error ? String(data.error) : '加载失败';
                }
                return;
            }
            var rows = Array.isArray(data.rows) ? data.rows : [];
            if (rows.length) {
                window.__rtMonitorMarginDailyModalRows = rows;
                window.__rtMonitorMarginDailyModalLenCapped = rows.length >= lim;
            } else {
                window.__rtMonitorMarginDailyModalRows = null;
                window.__rtMonitorMarginDailyModalLenCapped = undefined;
            }
            renderMarginDailyModalChart(rows);
        })
        .catch(function (err) {
            reqToken = err && err.__popupToken ? err.__popupToken : reqToken;
            if (!isPopupFetchCurrent('marginDaily', err && err.__popupToken)) return;
            if (loadingEl) loadingEl.style.display = 'none';
            logError('margin_daily 弹窗数据失败', err);
            if (msgEl) {
                msgEl.style.display = 'block';
                msgEl.textContent = err && err.name === 'AbortError' ? '请求超时或已取消，请重试' : '请求失败，请检查网络或服务';
            }
        })
        .finally(function () {
            if (reqToken) endPopupFetch('marginDaily', reqToken);
        });
}

function initMarginDailyModal() {
    var overlay = document.getElementById('margin-daily-modal-overlay');
    var modal = overlay && overlay.querySelector('.bias-series-modal');
    var header = overlay && overlay.querySelector('.bias-series-modal-header');
    var closeBtn = document.getElementById('margin-daily-modal-close');
    var chartBox = document.getElementById('margin-daily-chart');
    if (closeBtn) closeBtn.addEventListener('click', closeMarginDailyModal);
    if (overlay) {
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) closeMarginDailyModal();
        });
    }
    if (chartBox) {
        chartBox.addEventListener('dblclick', function (e) {
            e.preventDefault();
            openMarginDailyExpandModal();
        });
    }
    if (modal && header) {
        modal.style.position = 'fixed';
        modal.style.left = '50%';
        modal.style.top = '50%';
        modal.style.transform = 'translate(-50%, -50%)';
        var dragging = false;
        var startX = 0;
        var startY = 0;
        var startLeft = 0;
        var startTop = 0;
        header.style.cursor = 'move';
        header.addEventListener('mousedown', function (e) {
            if (e.button !== 0) return;
            dragging = true;
            startX = e.clientX;
            startY = e.clientY;
            var rect = modal.getBoundingClientRect();
            startLeft = rect.left;
            startTop = rect.top;
            modal.style.transform = '';
            e.preventDefault();
        });
        document.addEventListener('mousemove', function (e) {
            if (!dragging) return;
            var dx = e.clientX - startX;
            var dy = e.clientY - startY;
            var newLeft = startLeft + dx;
            var newTop = startTop + dy;
            var vw = window.innerWidth || document.documentElement.clientWidth;
            var vh = window.innerHeight || document.documentElement.clientHeight;
            var rect = modal.getBoundingClientRect();
            var maxLeft = vw - rect.width;
            var maxTop = vh - rect.height;
            if (newLeft < 0) newLeft = 0;
            if (newTop < 0) newTop = 0;
            if (newLeft > maxLeft) newLeft = maxLeft;
            if (newTop > maxTop) newTop = maxTop;
            modal.style.left = newLeft + 'px';
            modal.style.top = newTop + 'px';
        });
        document.addEventListener('mouseup', function () {
            if (dragging) {
                saveMarginDailyModalLayout(modal);
            }
            dragging = false;
        });
    }
    var modalChartWrap = document.getElementById('margin-daily-modal-chart-wrap');
    if (modalChartWrap && typeof ResizeObserver !== 'undefined') {
        var ro = new ResizeObserver(function () {
            if (marginDailyModalChartInstance && typeof marginDailyModalChartInstance.resize === 'function') {
                marginDailyModalChartInstance.resize();
            }
        });
        ro.observe(modalChartWrap);
    }
    if (modal && modal.classList && modal.classList.contains('margin-daily-modal-shell') && typeof ResizeObserver !== 'undefined') {
        var roShell = new ResizeObserver(function () {
            scheduleSaveMarginDailyModalLayout(modal);
        });
        roShell.observe(modal);
    }
}

function closeMarginRzmreIdxModal() {
    var overlay = document.getElementById('margin-rzmre-idx-modal-overlay');
    var modal = overlay && overlay.querySelector('.margin-rzmre-idx-modal-shell');
    var wasOpen = overlay && overlay.classList.contains('is-open');
    if (wasOpen && modal) {
        saveMarginRzmreModalLayout(modal);
    }
    if (overlay) overlay.classList.remove('is-open');
    if (marginRzmreIdxModalChartInstance) {
        marginRzmreIdxModalChartInstance.destroy();
        marginRzmreIdxModalChartInstance = null;
    }
    abortPopupFetch('marginRzmre');
    var ld = document.getElementById('margin-rzmre-idx-modal-loading');
    if (ld) ld.style.display = 'none';
    if (wasOpen) _modalClosed();
}

function openMarginRzmreIdxExpandModal() {
    var overlay = document.getElementById('margin-rzmre-idx-modal-overlay');
    var loadingEl = document.getElementById('margin-rzmre-idx-modal-loading');
    var msgEl = document.getElementById('margin-rzmre-idx-modal-msg');
    if (!overlay) return;
    marginRzmreModalLayoutOpenTs = Date.now();
    if (msgEl) {
        msgEl.style.display = 'none';
        msgEl.textContent = '';
    }
    overlay.classList.add('is-open'); _modalOpened();
    applyMarginRzmreModalSavedLayout();
    if (loadingEl) loadingEl.style.display = 'flex';
    if (marginRzmreIdxModalChartInstance) {
        marginRzmreIdxModalChartInstance.destroy();
        marginRzmreIdxModalChartInstance = null;
    }
    var lim = MARGIN_RZMRE_IDX_MODAL_SERIES_LIMIT;
    var cachedR = window.__rtMonitorMarginRzmreModalRows;
    var rzmreShort = window.__rtMonitorMarginRzmreModalLenCapped === false;
    if (cachedR && Array.isArray(cachedR) && cachedR.length > 0 && (cachedR.length >= lim || rzmreShort)) {
        if (loadingEl) loadingEl.style.display = 'none';
        renderMarginRzmreIdxModalChart(cachedR.slice());
        return;
    }
    var reqUrl = rtMonitorApiUrl('/api/margin_rzmre_idx_series?limit=' + encodeURIComponent(String(lim)));
    var reqToken = null;
    fetchPopupJson(reqUrl, 'marginRzmre')
        .then(function (o) {
            reqToken = o.token;
            if (!isPopupFetchCurrent('marginRzmre', o.token)) return;
            if (loadingEl) loadingEl.style.display = 'none';
            var data = o.data;
            if (!o.ok) {
                if (msgEl) {
                    msgEl.style.display = 'block';
                    msgEl.textContent = '加载失败：HTTP ' + o.status;
                }
                return;
            }
            if (!data || data.error) {
                if (msgEl) {
                    msgEl.style.display = 'block';
                    msgEl.textContent = data && data.error ? String(data.error) : '加载失败';
                }
                return;
            }
            var rows = Array.isArray(data.rows) ? data.rows : [];
            if (rows.length) {
                window.__rtMonitorMarginRzmreModalRows = rows;
                window.__rtMonitorMarginRzmreModalLenCapped = rows.length >= lim;
            } else {
                window.__rtMonitorMarginRzmreModalRows = null;
                window.__rtMonitorMarginRzmreModalLenCapped = undefined;
            }
            renderMarginRzmreIdxModalChart(rows);
        })
        .catch(function (err) {
            reqToken = err && err.__popupToken ? err.__popupToken : reqToken;
            if (!isPopupFetchCurrent('marginRzmre', err && err.__popupToken)) return;
            if (loadingEl) loadingEl.style.display = 'none';
            logError('margin_rzmre 弹窗数据失败', err);
            if (msgEl) {
                msgEl.style.display = 'block';
                msgEl.textContent = err && err.name === 'AbortError' ? '请求超时或已取消，请重试' : '请求失败，请检查网络或服务';
            }
        })
        .finally(function () {
            if (reqToken) endPopupFetch('marginRzmre', reqToken);
        });
}

function initMarginRzmreIdxModal() {
    var overlay = document.getElementById('margin-rzmre-idx-modal-overlay');
    var modal = overlay && overlay.querySelector('.bias-series-modal');
    var header = overlay && overlay.querySelector('.bias-series-modal-header');
    var closeBtn = document.getElementById('margin-rzmre-idx-modal-close');
    var chartBox = document.getElementById('margin-rzmre-idx-chart');
    if (closeBtn) closeBtn.addEventListener('click', closeMarginRzmreIdxModal);
    if (overlay) {
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) closeMarginRzmreIdxModal();
        });
    }
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        var biasOv = document.getElementById('bias-series-modal-overlay');
        if (biasOv && biasOv.classList.contains('is-open')) {
            closeBiasSeriesModal();
            return;
        }
        var dailyOv = document.getElementById('margin-daily-modal-overlay');
        if (dailyOv && dailyOv.classList.contains('is-open')) {
            closeMarginDailyModal();
            return;
        }
        var upMa5Ov = document.getElementById('up-num-ma5-modal-overlay');
        if (upMa5Ov && upMa5Ov.classList.contains('is-open')) {
            closeUpNumMa5Modal();
            return;
        }
        var amtTop5Ov = document.getElementById('amount-top5-pct-modal-overlay');
        if (amtTop5Ov && amtTop5Ov.classList.contains('is-open')) {
            closeAmountTop5PctModal();
            return;
        }
        if (overlay && overlay.classList.contains('is-open')) closeMarginRzmreIdxModal();
    });
    if (chartBox) {
        chartBox.addEventListener('dblclick', function (e) {
            e.preventDefault();
            openMarginRzmreIdxExpandModal();
        });
    }
    if (modal && header) {
        modal.style.position = 'fixed';
        modal.style.left = '50%';
        modal.style.top = '50%';
        modal.style.transform = 'translate(-50%, -50%)';
        var dragging = false;
        var startX = 0;
        var startY = 0;
        var startLeft = 0;
        var startTop = 0;
        header.style.cursor = 'move';
        header.addEventListener('mousedown', function (e) {
            if (e.button !== 0) return;
            dragging = true;
            startX = e.clientX;
            startY = e.clientY;
            var rect = modal.getBoundingClientRect();
            startLeft = rect.left;
            startTop = rect.top;
            modal.style.transform = '';
            e.preventDefault();
        });
        document.addEventListener('mousemove', function (e) {
            if (!dragging) return;
            var dx = e.clientX - startX;
            var dy = e.clientY - startY;
            var newLeft = startLeft + dx;
            var newTop = startTop + dy;
            var vw = window.innerWidth || document.documentElement.clientWidth;
            var vh = window.innerHeight || document.documentElement.clientHeight;
            var rect = modal.getBoundingClientRect();
            var maxLeft = vw - rect.width;
            var maxTop = vh - rect.height;
            if (newLeft < 0) newLeft = 0;
            if (newTop < 0) newTop = 0;
            if (newLeft > maxLeft) newLeft = maxLeft;
            if (newTop > maxTop) newTop = maxTop;
            modal.style.left = newLeft + 'px';
            modal.style.top = newTop + 'px';
        });
        document.addEventListener('mouseup', function () {
            if (dragging) {
                saveMarginRzmreModalLayout(modal);
            }
            dragging = false;
        });
    }
    var modalChartWrap = document.getElementById('margin-rzmre-idx-modal-chart-wrap');
    if (modalChartWrap && typeof ResizeObserver !== 'undefined') {
        var ro = new ResizeObserver(function () {
            if (marginRzmreIdxModalChartInstance && typeof marginRzmreIdxModalChartInstance.resize === 'function') {
                marginRzmreIdxModalChartInstance.resize();
            }
        });
        ro.observe(modalChartWrap);
    }
    // 仅监听弹窗外壳尺寸（用户拖动右下角 resize），避免图表内部重绘误触发保存
    if (modal && modal.classList && modal.classList.contains('margin-rzmre-idx-modal-shell') && typeof ResizeObserver !== 'undefined') {
        var roShell = new ResizeObserver(function () {
            scheduleSaveMarginRzmreModalLayout(modal);
        });
        roShell.observe(modal);
    }
}

function fetchMarginRzmreIdxSeries() {
    if (!window.Chart) return Promise.resolve({ ok: false });
    return fetch(rtMonitorApiUrl('/api/margin_rzmre_idx_series?limit=300'), { cache: 'no-store' })
        .then(function (res) {
            if (!res.ok) {
                logError('margin_rzmre_idx_series HTTP 失败', res.status);
                var container = document.getElementById('margin-rzmre-idx-chart');
                var latestEl = document.getElementById('margin-rzmre-idx-latest');
                if (latestEl) latestEl.innerHTML = '--';
                if (marginRzmreIdxChartInstance) {
                    marginRzmreIdxChartInstance.destroy();
                    marginRzmreIdxChartInstance = null;
                }
                var msg = res.status === 404
                    ? '接口 404：请重启 rt_monitor_app.py 或 web_dashboard.py 后 Ctrl+F5。'
                    : '接口 HTTP ' + res.status + '，请检查服务与网络。';
                if (container) {
                    container.innerHTML = '<div class="rt-monitor-chart-empty">' + msg + '</div>';
                }
                return null;
            }
            return res.json();
        })
        .then(function (data) {
            if (data === null) return { ok: false };
            if (!data) return { ok: false };
            if (data.error) {
                logError('margin_rzmre_idx_series 业务错误', data.error);
                var c = document.getElementById('margin-rzmre-idx-chart');
                var le = document.getElementById('margin-rzmre-idx-latest');
                if (le) le.innerHTML = '--';
                if (marginRzmreIdxChartInstance) {
                    marginRzmreIdxChartInstance.destroy();
                    marginRzmreIdxChartInstance = null;
                }
                if (c) {
                    c.innerHTML = '<div class="rt-monitor-chart-empty">数据加载失败：' + String(data.error) + '</div>';
                }
                return { ok: false };
            }
            renderMarginRzmreIdxChart(Array.isArray(data.rows) ? data.rows : []);
            scheduleMarginModalPrefetchs();
            return { ok: true };
        })
        .catch(function (err) {
            logError('margin_rzmre_idx_series 请求异常', err);
            var c2 = document.getElementById('margin-rzmre-idx-chart');
            if (c2 && c2.innerHTML.indexOf('rt-monitor-chart-empty') === -1) {
                c2.innerHTML = '<div class="rt-monitor-chart-empty">请求失败或响应非 JSON，请刷新重试。</div>';
            }
            return { ok: false };
        });
}

function fetchMarginDailyRecent() {
    if (!window.Chart) return Promise.resolve({ ok: false });
    return fetch(rtMonitorApiUrl('/api/margin_daily_recent?limit=300'), { cache: 'no-store' })
        .then(function (res) {
            if (!res.ok) {
                logError('margin_daily_recent HTTP 失败', res.status);
                var container = document.getElementById('margin-daily-chart');
                var latestEl = document.getElementById('margin-daily-latest');
                if (latestEl) latestEl.innerHTML = '--';
                if (marginDailyChartInstance) {
                    marginDailyChartInstance.destroy();
                    marginDailyChartInstance = null;
                }
                var msg = res.status === 404
                    ? '接口 404：当前进程未注册 /api/margin_daily_recent。请重启本机占用 5000 端口的进程（rt_monitor_app.py 或 web_dashboard.py）后 Ctrl+F5。'
                    : '接口 HTTP ' + res.status + '，请检查服务与网络。';
                if (container) {
                    container.innerHTML = '<div class="rt-monitor-chart-empty">' + msg + '</div>';
                }
                return null;
            }
            return res.json();
        })
        .then(function (data) {
            if (data === null) return { ok: false };
            if (!data) return { ok: false };
            if (data.error) {
                logError('margin_daily_recent 业务错误', data.error);
                var c = document.getElementById('margin-daily-chart');
                var le = document.getElementById('margin-daily-latest');
                if (le) le.innerHTML = '--';
                if (marginDailyChartInstance) {
                    marginDailyChartInstance.destroy();
                    marginDailyChartInstance = null;
                }
                if (c) {
                    c.innerHTML = '<div class="rt-monitor-chart-empty">数据加载失败：' + String(data.error) + '</div>';
                }
                return { ok: false };
            }
            renderMarginDailyChart(Array.isArray(data.rows) ? data.rows : []);
            scheduleMarginModalPrefetchs();
            return { ok: true };
        })
        .catch(function (err) {
            logError('margin_daily_recent 请求异常', err);
            var c2 = document.getElementById('margin-daily-chart');
            if (c2 && c2.innerHTML.indexOf('rt-monitor-chart-empty') === -1) {
                c2.innerHTML = '<div class="rt-monitor-chart-empty">请求失败或响应非 JSON，请刷新重试。</div>';
            }
            return { ok: false };
        });
}


function hideTrendChartRowsWhenChartUnavailable() {
    /* 必须先判断 Chart：若可用则不得先 display:none，否则会把上一行 showTrendChartRowsWhenChartReady 的效果清掉，导致六张走势图永久不可见 */
    if (window.Chart) return false;
    ['idx-minute-pair-row', 'idx-cumsum-pair-row', 'margin-daily-pair-row'].forEach(function (id) {
        var row = document.getElementById(id);
        if (!row) return;
        row.classList.remove('is-ready');
        row.style.setProperty('display', 'none', 'important');
    });
    var msgSection = document.getElementById('message-section');
    var msgText = document.getElementById('message-text');
    if (msgSection && msgText) {
        msgSection.style.display = 'block';
        msgText.textContent = '走势图组件未加载，已自动隐藏相关图表区块。请 Ctrl+F5 刷新；若仍存在，请检查 Chart.js 资源可访问性。';
    }
    return true;
}

function showTrendChartRowsWhenChartReady() {
    if (!window.Chart) return false;
    ['idx-minute-pair-row', 'idx-cumsum-pair-row', 'margin-daily-pair-row'].forEach(function (id) {
        var row = document.getElementById(id);
        if (!row) return;
        row.classList.add('is-ready');
        row.style.setProperty('display', 'flex', 'important');
    });
    return true;
}

/** 主监控或两融独立页是否内嵌两融主图 DOM（迁出后主监控为 false） */
function hasRtMonitorMarginMainCharts() {
    return !!document.getElementById('margin-daily-chart');
}

/**
 * 日频仅此两路：净买入&融资余额、融资买入额占比&国证A指；不参与自动刷新间隔，由顶栏「刷新日频」或首屏 load /「立即刷新」触发。
 */
function refreshDailyMarginCharts() {
    if (!hasRtMonitorMarginMainCharts()) return;
    clearMarginModalPrefetchCaches();
    fetchMarginDailyRecent();
    fetchMarginRzmreIdxSeries();
}

var dailyRefreshToastTimerId = null;
/** 本地完成时间格式化，用于日频刷新反馈 */
function formatDailyRefreshDoneTime(d) {
    var x = d instanceof Date ? d : new Date(d);
    if (isNaN(x.getTime())) return '--';
    var y = x.getFullYear();
    var m = x.getMonth() + 1;
    var day = x.getDate();
    var h = x.getHours();
    var min = x.getMinutes();
    var s = x.getSeconds();
    function z(n) { return n < 10 ? '0' + n : String(n); }
    return y + '-' + z(m) + '-' + z(day) + ' ' + z(h) + ':' + z(min) + ':' + z(s);
}

function showDailyRefreshToast(isOk, doneAt, detail) {
    var el = document.getElementById('daily-refresh-toast');
    if (!el) return;
    if (dailyRefreshToastTimerId) {
        clearTimeout(dailyRefreshToastTimerId);
        dailyRefreshToastTimerId = null;
    }
    el.hidden = false;
    el.classList.toggle('is-error', !isOk);
    var timeStr = formatDailyRefreshDoneTime(doneAt);
    if (isOk) {
        el.textContent = '日频数据已刷新。完成时间：' + timeStr + '（本地）';
    } else {
        el.textContent = '日频数据刷新未完成。' + (detail ? String(detail) + ' ' : '') + '完成时间：' + timeStr + '（本地）';
    }
    dailyRefreshToastTimerId = setTimeout(function () {
        el.hidden = true;
        dailyRefreshToastTimerId = null;
    }, 4500);
}

/** 用户点击顶栏「刷新日频」：两路并行，结束后弹出简要结果 */
function refreshDailyMarginChartsWithToast() {
    if (!hasRtMonitorMarginMainCharts()) return;
    Promise.all([fetchMarginDailyRecent(), fetchMarginRzmreIdxSeries()])
        .then(function (results) {
            var ok = results[0] && results[0].ok && results[1] && results[1].ok;
            showDailyRefreshToast(ok, new Date(), ok ? '' : '请查看图表区错误提示或稍后重试。');
        });
}

// BIAS 表：列顺序与表头一致（15MIN/60MIN 各 10MA,20MA,20EMA；日线仅 10MA）
// 10MA、20MA 为可点击链接（弹窗折线图：10MA 单线，20MA 同图 MA+EMA 双线）；20EMA 仅展示数值；日线 10MA 仅展示
var BIAS_COL_LABELS = ['10MA', '20MA', '20EMA'];
var BIAS_DAILY_COL_LABELS = ['10MA'];
var BIAS_COL_CLICKABLE = { '10MA': true, '20MA': true };
// 2σ 对应 per_bias 约 2.5～97.5，3σ 对应约 0.15～99.85（弹窗图中 2σ 上下界细线用）
var SIGMA_2_MIN = 2.5, SIGMA_2_MAX = 97.5, SIGMA_3_MIN = 0.15, SIGMA_3_MAX = 99.85;
var PAGE_FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif';
/** Chart.js 刻度/图例/Tooltip 字号（px），与 myuiux 一致；页面表格正文 13px 由 CSS --rt-monitor-font-data 控制 */
var RT_MONITOR_CHART_FONT_SIZE = 10;
// 会话内每个单元格第一次超出 2σ/3σ 的时间（key: ts_code|freq|col）
var biasFirstDetectedTimes = {};

function formatBiasValue(value) {
    if (value === null || value === undefined || (typeof value === 'number' && isNaN(value))) {
        return '--';
    }
    var num = parseFloat(value);
    return isNaN(num) ? '--' : num.toFixed(2);
}
// 仅显示几点几分，不显示日期
function formatBiasFirstDetectedTime(date) {
    if (!date) return '--';
    var d = date instanceof Date ? date : new Date(date);
    if (isNaN(d.getTime())) return '--';
    var h = d.getHours(), m = d.getMinutes();
    return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m;
}

function getBiasSigmaClass(value) {
    if (value === null || value === undefined) return '';
    var num = parseFloat(value);
    if (isNaN(num)) return '';
    if (num < SIGMA_3_MIN || num > SIGMA_3_MAX) return ' bias-sigma-3';
    if (num < SIGMA_2_MIN || num > SIGMA_2_MAX) return ' bias-sigma-2';
    return '';
}

function getBiasSigmaType(value) {
    if (value === null || value === undefined) return 0;
    var num = parseFloat(value);
    if (isNaN(num)) return 0;
    if (num < SIGMA_3_MIN || num > SIGMA_3_MAX) return 3;
    if (num < SIGMA_2_MIN || num > SIGMA_2_MAX) return 2;
    return 0;
}

/** 与原先 makeBiasCell 内联计数一致，供消息区/明细与 tbody 刷新解耦 */
function collectBiasDeviations(rows) {
    var count2 = 0, count3 = 0;
    var deviations = [];
    rows.forEach(function (row) {
        var tsCode = row.ts_code || '--';
        function pushSigma(k, v, freq) {
            var sigmaType = getBiasSigmaType(v);
            if (sigmaType === 3) {
                count3++;
                var key = tsCode + '|' + freq + '|' + k;
                if (!biasFirstDetectedTimes[key]) biasFirstDetectedTimes[key] = new Date();
                deviations.push({ ts_code: tsCode, freq: freq, col: k, value: v, sigma: 3, firstDetected: biasFirstDetectedTimes[key] });
            } else if (sigmaType === 2) {
                count2++;
                var key2 = tsCode + '|' + freq + '|' + k;
                if (!biasFirstDetectedTimes[key2]) biasFirstDetectedTimes[key2] = new Date();
                deviations.push({ ts_code: tsCode, freq: freq, col: k, value: v, sigma: 2, firstDetected: biasFirstDetectedTimes[key2] });
            }
        }
        BIAS_COL_LABELS.forEach(function (k) {
            var v = row['15MIN'] && row['15MIN'][k] !== undefined ? row['15MIN'][k] : null;
            pushSigma(k, v, '15MIN');
        });
        BIAS_COL_LABELS.forEach(function (k) {
            var v = row['60MIN'] && row['60MIN'][k] !== undefined ? row['60MIN'][k] : null;
            pushSigma(k, v, '60MIN');
        });
        BIAS_DAILY_COL_LABELS.forEach(function (k) {
            var v = row['D'] && row['D'][k] !== undefined ? row['D'][k] : null;
            pushSigma(k, v, 'D');
        });
    });
    return { count2: count2, count3: count3, deviations: deviations };
}

function makeBiasCellHtmlOnly(k, v, freq, tsCode) {
    var sigmaClass = getBiasSigmaClass(v);
    var isClickable = BIAS_COL_CLICKABLE[k];
    var spanClass = 'bias-cell-value' + (isClickable ? ' bias-cell-clickable' : '');
    var attrs = isClickable
        ? ' tabindex="0" role="button" data-ts-code="' + (tsCode || '').replace(/"/g, '&quot;') + '" data-freq="' + freq + '" data-col="' + (k || '').replace(/"/g, '&quot;') + '"'
        : '';
    return '<td class="median-table-num bias-cell' + sigmaClass + '"><span class="' + spanClass + '"' + attrs + '>' + formatBiasValue(v) + '</span></td>';
}

function patchBiasTableRow(tr, row, rowIndex) {
    var tsCode = row.ts_code || '--';
    tr.className = 'median-table-row' + ((rowIndex % 2 === 1) ? ' median-table-zebra' : '');
    tr.cells[0].textContent = tsCode;
    var ci = 1;
    function patchLabels(labels, freq) {
        labels.forEach(function (k) {
            var v = row[freq] && row[freq][k] !== undefined ? row[freq][k] : null;
            var sigmaClass = getBiasSigmaClass(v);
            var td = tr.cells[ci++];
            td.className = ('median-table-num bias-cell' + sigmaClass).trim();
            var span = td.querySelector('.bias-cell-value');
            if (!span) return;
            span.textContent = formatBiasValue(v);
            var isClickable = BIAS_COL_CLICKABLE[k];
            span.className = 'bias-cell-value' + (isClickable ? ' bias-cell-clickable' : '');
            if (isClickable) {
                span.setAttribute('tabindex', '0');
                span.setAttribute('role', 'button');
                span.setAttribute('data-ts-code', tsCode);
                span.setAttribute('data-freq', freq);
                span.setAttribute('data-col', k);
            } else {
                span.removeAttribute('tabindex');
                span.removeAttribute('role');
                span.removeAttribute('data-ts-code');
                span.removeAttribute('data-freq');
                span.removeAttribute('data-col');
            }
        });
    }
    patchLabels(BIAS_COL_LABELS, '15MIN');
    patchLabels(BIAS_COL_LABELS, '60MIN');
    patchLabels(BIAS_DAILY_COL_LABELS, 'D');
}

function paintBiasSigmaDetail(summary, msgEl, detailEl) {
    if (msgEl) {
        msgEl.textContent = '消息：' + (summary.count2 + summary.count3) + ' 个';
    }
    if (detailEl) {
        if (summary.deviations.length === 0) {
            detailEl.innerHTML = '<p class="bias-sigma-detail-empty">当前无超出 2σ/3σ 的单元格。</p>';
        } else {
            detailEl.innerHTML = '<table class="bias-sigma-detail-table"><thead><tr><th>ts_code</th><th>周期</th><th>指标</th><th>per_bias</th><th>时间</th></tr></thead><tbody>' +
                summary.deviations.map(function (d) {
                    return '<tr><td>' + d.ts_code + '</td><td>' + d.freq + '</td><td>' + d.col + '</td><td>' + formatBiasValue(d.value) + '</td><td>' + formatBiasFirstDetectedTime(d.firstDetected) + '</td></tr>';
                }).join('') +
                '</tbody></table>';
        }
    }
}

function renderBiasTable(data) {
    var tbody = document.getElementById('bias-table-body');
    var msgEl = document.getElementById('bias-sigma-msg');
    var detailEl = document.getElementById('bias-sigma-detail-list');
    if (!tbody) return;
    var rows = Array.isArray(data.rows) ? data.rows : [];
    if (rows.length === 0 && biasTableLastOk && Array.isArray(biasTableLastOk.rows) && biasTableLastOk.rows.length > 0) {
        rows = biasTableLastOk.rows;
    }
    var summary = collectBiasDeviations(rows);
    var BIAS_ROW_TD = 8;
    if (rows.length > 0 && tbody.rows.length === rows.length) {
        var canPatchBias = true;
        for (var bi = 0; bi < rows.length; bi++) {
            if (!tbody.rows[bi] || tbody.rows[bi].cells.length !== BIAS_ROW_TD) { canPatchBias = false; break; }
        }
        if (canPatchBias) {
            for (var bj = 0; bj < rows.length; bj++) {
                patchBiasTableRow(tbody.rows[bj], rows[bj], bj);
            }
            paintBiasSigmaDetail(summary, msgEl, detailEl);
            scheduleBiasGridQuickPrefetch();
            return;
        }
    }
    tbody.innerHTML = rows.map(function (row, i) {
        var trClass = (i % 2 === 1) ? ' median-table-zebra' : '';
        var tsCode = row.ts_code || '--';
        var cells15 = BIAS_COL_LABELS.map(function (k) {
            var v = row['15MIN'] && row['15MIN'][k] !== undefined ? row['15MIN'][k] : null;
            return makeBiasCellHtmlOnly(k, v, '15MIN', tsCode);
        }).join('');
        var cells60 = BIAS_COL_LABELS.map(function (k) {
            var v = row['60MIN'] && row['60MIN'][k] !== undefined ? row['60MIN'][k] : null;
            return makeBiasCellHtmlOnly(k, v, '60MIN', tsCode);
        }).join('');
        var cellsDaily = BIAS_DAILY_COL_LABELS.map(function (k) {
            var v = row['D'] && row['D'][k] !== undefined ? row['D'][k] : null;
            return makeBiasCellHtmlOnly(k, v, 'D', tsCode);
        }).join('');
        return '<tr class="median-table-row' + trClass + '">' +
            '<td class="median-table-cat">' + tsCode + '</td>' +
            cells15 + cells60 + cellsDaily +
            '</tr>';
    }).join('');
    paintBiasSigmaDetail(summary, msgEl, detailEl);
    scheduleBiasGridQuickPrefetch();
}

// BIAS 序列弹窗：当前图表实例，关闭时销毁
var biasSeriesChartInstance = null;

function openBiasSeriesModal(title) {
    var overlay = document.getElementById('bias-series-modal-overlay');
    var titleEl = document.getElementById('bias-series-modal-title');
    if (overlay) { overlay.classList.add('is-open'); _modalOpened(); }
    if (titleEl && title) titleEl.textContent = title;
}

function loadEtfBiasMcdModalLayout() {
    try {
        if (typeof localStorage === 'undefined') return null;
        var raw = localStorage.getItem(ETF_BIAS_MCD_MODAL_LAYOUT_STORAGE_KEY);
        if (!raw) return null;
        var o = JSON.parse(raw);
        if (typeof o.w !== 'number' || typeof o.h !== 'number') return null;
        if (o.w < 320 || o.h < 320) return null;
        return {
            w: o.w,
            h: o.h,
            l: typeof o.l === 'number' ? o.l : null,
            t: typeof o.t === 'number' ? o.t : null,
        };
    } catch (e) {
        return null;
    }
}

function saveEtfBiasMcdModalLayout(modalEl) {
    try {
        if (!modalEl || typeof localStorage === 'undefined') return;
        var rect = modalEl.getBoundingClientRect();
        var w = Math.round(rect.width);
        var h = Math.round(rect.height);
        var l = Math.round(rect.left);
        var t = Math.round(rect.top);
        if (w < 320 || h < 320) return;
        localStorage.setItem(ETF_BIAS_MCD_MODAL_LAYOUT_STORAGE_KEY, JSON.stringify({ w: w, h: h, l: l, t: t }));
    } catch (e) {
        /* ignore */
    }
}

function applyEtfBiasMcdModalSavedLayout() {
    var overlay = document.getElementById('bias-series-modal-overlay');
    var modal = overlay && overlay.querySelector('.etf-bias-mcd-modal-shell');
    if (!modal) return;
    var lay = loadEtfBiasMcdModalLayout();
    if (!lay) return;
    var vw = window.innerWidth || document.documentElement.clientWidth;
    var vh = window.innerHeight || document.documentElement.clientHeight;
    var minW = 320;
    var minH = 320;
    var w = Math.min(Math.max(lay.w, minW), Math.max(minW, vw - 4));
    var h = Math.min(Math.max(lay.h, minH), Math.max(minH, vh - 4));
    modal.style.width = w + 'px';
    modal.style.height = h + 'px';
    modal.style.transform = 'none';
    var l = lay.l != null ? lay.l : (vw - w) / 2;
    var t = lay.t != null ? lay.t : (vh - h) / 2;
    l = Math.max(0, Math.min(l, vw - w));
    t = Math.max(0, Math.min(t, vh - h));
    modal.style.left = l + 'px';
    modal.style.top = t + 'px';
}

function scheduleSaveEtfBiasMcdModalLayout(modalEl) {
    if (!modalEl) return;
    clearTimeout(biasSeriesModalSaveTimer);
    biasSeriesModalSaveTimer = setTimeout(function () {
        if (Date.now() - biasSeriesModalLayoutOpenTs < 700) return;
        saveEtfBiasMcdModalLayout(modalEl);
    }, 400);
}

function closeBiasSeriesModal() {
    var overlay = document.getElementById('bias-series-modal-overlay');
    var modal = overlay && overlay.querySelector('.etf-bias-mcd-modal-shell');
    var wasOpen = overlay && overlay.classList.contains('is-open');
    if (wasOpen && modal) {
        saveEtfBiasMcdModalLayout(modal);
    }
    if (overlay) overlay.classList.remove('is-open');
    if (biasSeriesChartInstance) {
        biasSeriesChartInstance.destroy();
        biasSeriesChartInstance = null;
    }
    biasSeriesModalActiveToken = null;
    abortPopupFetch('biasSeries');
    if (wasOpen) _modalClosed();
}

function showBiasSeriesMsg(text) {
    var el = document.getElementById('bias-series-msg');
    if (!el) return;
    el.textContent = text || '';
    el.style.display = text ? 'flex' : 'none';
}

/**
 * BIAS / 聚集度 Per 弹窗大图：双轴 + 2σ 参考线 + chartjs-plugin-zoom（与超跌弹窗同款 limits / pan / wheel）；
 * 日线 X 轴跨年刻度与融资占比弹窗一致；初始视窗最近约 300 根，可横向拖动浏览全量。
 */
function drawBiasSeriesChart(payload) {
    var loadingEl = document.getElementById('bias-series-loading');
    var canvas = document.getElementById('bias-series-chart');
    if (loadingEl) loadingEl.style.display = 'none';
    showBiasSeriesMsg('');
    if (!canvas) return;
    if (!window.Chart) {
        showBiasSeriesMsg('图表库未加载，请刷新页面重试');
        return;
    }
    if (biasSeriesChartInstance) {
        biasSeriesChartInstance.destroy();
        biasSeriesChartInstance = null;
    }
    var times = payload.times || [];
    var close = payload.close || [];
    var perBias = payload.per_bias || [];
    var perBiasEma = payload.per_bias_ema || [];
    if (times.length === 0) {
        showBiasSeriesMsg(payload.freq === 'D' ? '该品种日线暂无数据，请确认 etf_daily 表已有数据并已跑 etf_daily_ma_ema' : '该品种/周期暂无数据，请确认 etf_stk_mins_ma 等表已有历史');
        return;
    }
    /* 数据准确性：各列必须同索引对齐；若接口异常长度不一，截断到最短并打日志，避免折线与时间错配 */
    var nTime = times.length;
    var nClose = close.length;
    var nPer = perBias.length;
    var nEma = perBiasEma.length;
    var nAlign = Math.min(nTime, nClose, nPer);
    if (nEma > 0) {
        nAlign = Math.min(nAlign, nEma);
    }
    if (nAlign === 0) {
        showBiasSeriesMsg('返回序列为空或无法对齐');
        return;
    }
    if (nAlign !== nTime || nAlign !== nClose || nAlign !== nPer || (nEma > 0 && nAlign !== nEma)) {
        logError('BIAS/聚集度弹窗：times/close/per 长度不一致，已截断至 ' + nAlign + ' 点', { nTime: nTime, nClose: nClose, nPer: nPer, nEma: nEma });
        times = times.slice(0, nAlign);
        close = close.slice(0, nAlign);
        perBias = perBias.slice(0, nAlign);
        if (nEma > 0) {
            perBiasEma = perBiasEma.slice(0, nAlign);
        }
    }
    var hasEma = perBiasEma.length === times.length && perBiasEma.length > 0;
    var isDaily = payload.freq === 'D';
    var labels = [];
    var tooltipTitles = [];
    var tradeDateFull = [];
    var ti;
    for (ti = 0; ti < times.length; ti++) {
        var t = times[ti];
        var fullDay = '';
        if (t != null && typeof t === 'string' && t.length >= 10) {
            fullDay = t.slice(0, 10);
        } else {
            try {
                var dParse = new Date(t);
                if (!isNaN(dParse.getTime())) {
                    var y0 = dParse.getFullYear();
                    var mo0 = dParse.getMonth() + 1;
                    var day0 = dParse.getDate();
                    fullDay = y0 + '-' + (mo0 < 10 ? '0' : '') + mo0 + '-' + (day0 < 10 ? '0' : '') + day0;
                }
            } catch (e0) { fullDay = ''; }
        }
        tradeDateFull.push(fullDay);
        if (isDaily) {
            labels.push(fullDay.length >= 10 ? fullDay.slice(5, 10) : String(ti));
            tooltipTitles.push(fullDay ? ('日期：' + fullDay) : '');
        } else {
            try {
                var d = new Date(t);
                if (isNaN(d.getTime())) {
                    labels.push(String(ti));
                    tooltipTitles.push('');
                } else {
                    labels.push(
                        (d.getMonth() + 1) + '/' + d.getDate() + ' ' +
                        (d.getHours() < 10 ? '0' : '') + d.getHours() + ':' +
                        (d.getMinutes() < 10 ? '0' : '') + d.getMinutes()
                    );
                    var y2 = d.getFullYear();
                    var mo2 = d.getMonth() + 1;
                    var day2 = d.getDate();
                    var h2 = d.getHours();
                    var m2 = d.getMinutes();
                    tooltipTitles.push(
                        '时间：' + y2 + '-' + (mo2 < 10 ? '0' : '') + mo2 + '-' + (day2 < 10 ? '0' : '') + day2 + ' ' +
                        (h2 < 10 ? '0' : '') + h2 + ':' + (m2 < 10 ? '0' : '') + m2
                    );
                }
            } catch (e1) {
                labels.push(String(ti));
                tooltipTitles.push('');
            }
        }
    }
    var xTickIndicesByDate = [];
    if (!isDaily) {
        var lastDateKey = '';
        var tickIdxForDay = -1;
        for (var idx = 0; idx < times.length; idx++) {
            try {
                var dt = new Date(times[idx]);
                if (isNaN(dt.getTime())) continue;
                var dateKey = dt.getFullYear() + '-' + (dt.getMonth() + 1) + '-' + dt.getDate();
                var is930 = (dt.getHours() === 9 && dt.getMinutes() === 30);
                if (dateKey !== lastDateKey) {
                    if (lastDateKey !== '' && tickIdxForDay >= 0) xTickIndicesByDate.push(tickIdxForDay);
                    tickIdxForDay = idx;
                    if (is930) tickIdxForDay = idx;
                    lastDateKey = dateKey;
                } else if (is930) {
                    tickIdxForDay = idx;
                }
            } catch (e) { /* skip */ }
        }
        if (lastDateKey !== '' && tickIdxForDay >= 0) xTickIndicesByDate.push(tickIdxForDay);
    }
    var perLineLabel = payload.column === '10MA' ? '10MA' : (payload.column === '20MA' ? '20MA' : (payload.column || 'Per'));
    var perBiasDataset = {
        label: perLineLabel,
        data: perBias,
        yAxisID: 'yBias',
        borderColor: MARGIN_RZMRE_PCT_LINE,
        borderWidth: 2,
        backgroundColor: MARGIN_RZMRE_PCT_FILL,
        tension: 0.35,
        fill: false,
        pointRadius: 0,
        pointHoverRadius: 4,
        pointStyle: 'line',
    };
    var perBiasEmaDataset = null;
    if (hasEma) {
        perBiasEmaDataset = {
            label: '20EMA',
            data: perBiasEma,
            yAxisID: 'yBias',
            borderColor: 'rgba(255, 99, 132, 1)',
            borderWidth: 2,
            backgroundColor: 'rgba(255, 99, 132, 0.1)',
            tension: 0.35,
            fill: false,
            pointRadius: 0,
            pointHoverRadius: 4,
            pointStyle: 'line',
        };
    }
    var n = labels.length;
    var line2sigmaLow = { label: '2σ下', data: Array(n).fill(SIGMA_2_MIN), yAxisID: 'yBias', borderColor: 'rgba(255, 193, 7, 0.9)', borderWidth: 1, borderDash: [4, 2], tension: 0, fill: false, pointRadius: 0 };
    var line2sigmaHigh = { label: '2σ上', data: Array(n).fill(SIGMA_2_MAX), yAxisID: 'yBias', borderColor: 'rgba(255, 193, 7, 0.9)', borderWidth: 1, borderDash: [4, 2], tension: 0, fill: false, pointRadius: 0 };
    var datasets = [
        { label: 'close', data: close, yAxisID: 'yClose', borderColor: 'rgba(155, 89, 182, 1)', borderWidth: 2, backgroundColor: 'rgba(155, 89, 182, 0.12)', tension: 0.35, fill: false, pointRadius: 0, pointHoverRadius: 4, pointStyle: 'line' },
        perBiasDataset,
    ];
    if (perBiasEmaDataset) datasets.push(perBiasEmaDataset);
    datasets.push(line2sigmaLow, line2sigmaHigh);
    var tooltipDatasetLimit = hasEma ? 3 : 2;

    var viewWin = Math.min(300, n);
    var xIdxMin = n > 0 ? Math.max(0, n - viewWin) : 0;
    var xIdxMax = n > 0 ? n - 1 : 0;
    var xMaxBound = n > 0 ? n - 1 : 0;
    var zoomOpts = {
        limits: {
            x: { min: 0, max: xMaxBound, minRange: 15 },
        },
        pan: {
            enabled: true,
            mode: 'x',
            modifierKey: null,
            threshold: 5,
        },
        zoom: {
            wheel: { enabled: true, modifierKey: null },
            pinch: { enabled: true },
            mode: 'x',
        },
    };
    if (typeof Hammer === 'undefined') {
        zoomOpts.pan.enabled = false;
        zoomOpts.zoom.pinch.enabled = false;
        logError('Hammer.js 未加载，BIAS/聚集度弹窗仅支持滚轮缩放，平移已关闭');
    }

    var validClose = close.filter(function (v) { return v != null && !isNaN(Number(v)); });
    var closeMin;
    var closeMax;
    if (validClose.length > 0) {
        closeMin = Math.min.apply(null, validClose.map(Number));
        closeMax = Math.max.apply(null, validClose.map(Number));
        if (closeMin === closeMax) {
            var padC = closeMin * 0.01 || 1;
            closeMin -= padC;
            closeMax += padC;
        } else {
            var rngC = closeMax - closeMin;
            closeMin -= rngC * 0.05;
            closeMax += rngC * 0.05;
        }
    }

    var xScaleConfig;
    if (isDaily) {
        xScaleConfig = {
            type: 'category',
            min: xIdxMin,
            max: xIdxMax,
            display: true,
            grid: { display: false },
            ticks: {
                color: '#555',
                font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT, lineHeight: 1.35 },
                autoSkip: true,
                autoSkipPadding: 2,
                maxRotation: 0,
                maxTicksLimit: 42,
                callback: function (tickVal) {
                    var idx = tickVal;
                    var chart = this.chart;
                    var dates = chart.$biasTradeDateFull;
                    if (!dates || dates[idx] == null || dates[idx] === '') return '';
                    var full = String(dates[idx]);
                    var prev = idx > 0 && dates[idx - 1] != null && dates[idx - 1] !== ''
                        ? String(dates[idx - 1])
                        : null;
                    return formatMarginRzmreIdxXAxisTick(full, prev);
                },
            },
        };
    } else {
        xScaleConfig = {
            type: 'category',
            min: xIdxMin,
            max: xIdxMax,
            display: true,
            grid: { display: false },
            ticks: {
                color: '#555',
                font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                autoSkip: false,
                maxTicksLimit: 99999,
                callback: function (val, index) {
                    return xTickIndicesByDate.indexOf(index) >= 0 ? labels[index] : undefined;
                },
            },
        };
    }

    try {
        biasSeriesChartInstance = new Chart(canvas, {
            type: 'line',
            data: {
                labels: labels,
                datasets: datasets,
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                animation: false,
                layout: { padding: { bottom: 14 } },
                datasets: {
                    line: {
                        pointRadius: 0,
                        pointHoverRadius: 4,
                        tension: 0.35,
                        borderWidth: 2,
                    },
                },
                elements: {
                    point: { radius: 0 },
                },
                plugins: {
                    legend: { display: false },
                    zoom: zoomOpts,
                    tooltip: {
                        displayColors: false,
                        filter: function (item) { return item.datasetIndex < tooltipDatasetLimit; },
                        callbacks: {
                            title: function (items) {
                                if (!items || !items.length) return '';
                                var ch = items[0].chart;
                                var idx = items[0].dataIndex;
                                var arr = ch.$biasTooltipTitles;
                                return arr && arr[idx] ? arr[idx] : '';
                            },
                            label: function (ctx) {
                                if (ctx.datasetIndex >= tooltipDatasetLimit) return '';
                                var v = ctx.parsed.y;
                                if (v === null || v === undefined || isNaN(v)) return '';
                                if (ctx.datasetIndex === 0) {
                                    return '收盘：' + (typeof v === 'number' ? v.toFixed(2) : v);
                                }
                                return ctx.dataset.label + '：' + (typeof v === 'number' ? v.toFixed(2) : v) + ' %';
                            },
                        },
                    },
                },
                scales: {
                    x: xScaleConfig,
                    yClose: {
                        type: 'linear',
                        display: true,
                        position: 'left',
                        title: { display: false },
                        min: validClose.length ? closeMin : undefined,
                        max: validClose.length ? closeMax : undefined,
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                            callback: function (val) { return formatAxisInt(val); },
                        },
                        grid: {
                            color: function (ctx) {
                                var v = ctx.tick && ctx.tick.value;
                                return v === 0 ? '#b0bec5' : '#eceff1';
                            },
                            lineWidth: function (ctx) {
                                var v = ctx.tick && ctx.tick.value;
                                return v === 0 ? 1.2 : 0.5;
                            },
                        },
                    },
                    yBias: {
                        type: 'linear',
                        display: true,
                        position: 'right',
                        title: { display: false },
                        min: 0,
                        max: 100,
                        grid: { drawOnChartArea: false },
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                            callback: function (val) { return formatAxisPct1(val); },
                        },
                    },
                },
            },
        });
    } catch (err) {
        showBiasSeriesMsg('图表渲染失败: ' + (err && err.message ? err.message : '未知错误'));
        return;
    }
    biasSeriesChartInstance.$biasTooltipTitles = tooltipTitles;
    biasSeriesChartInstance.$biasTradeDateFull = tradeDateFull;
    requestAnimationFrame(function () {
        if (!biasSeriesChartInstance) return;
        try {
            biasSeriesChartInstance.resize();
        } catch (e2) {
            logError('BIAS/聚集度弹窗图 resize 失败', e2);
        }
        requestAnimationFrame(function () {
            if (!biasSeriesChartInstance || typeof biasSeriesChartInstance.zoomScale !== 'function') return;
            if (n <= 0) return;
            try {
                biasSeriesChartInstance.zoomScale('x', { min: xIdxMin, max: xIdxMax }, 'default');
            } catch (ez) {
                logError('BIAS/聚集度弹窗 zoomScale 同步失败', ez);
            }
        });
    });
}

function initBiasSigmaToggle() {
    var btn = document.getElementById('bias-sigma-toggle-detail');
    var list = document.getElementById('bias-sigma-detail-list');
    var box = document.getElementById('bias-sigma-msg-section');
    if (!btn || !list) return;
    function closePopover() {
        list.classList.remove('is-open');
        btn.setAttribute('aria-expanded', 'false');
        btn.textContent = '查看详情';
    }
    btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var isOpen = list.classList.contains('is-open');
        if (isOpen) {
            closePopover();
        } else {
            list.classList.add('is-open');
            btn.setAttribute('aria-expanded', 'true');
            btn.textContent = '收起详情';
        }
    });
    /* 方案 B：条在文档流内，点击条内区域不冒泡到 document，避免误关详情层 */
    if (box) {
        box.addEventListener('click', function (e) {
            e.stopPropagation();
        });
    }
    document.addEventListener('click', function () {
        if (list.classList.contains('is-open')) closePopover();
    });
    list.addEventListener('click', function (e) { e.stopPropagation(); });
}

function initBiasSeriesModal() {
    var overlay = document.getElementById('bias-series-modal-overlay');
    var modal = overlay && overlay.querySelector('.etf-bias-mcd-modal-shell');
    var header = overlay && overlay.querySelector('.bias-series-modal-header');
    var closeBtn = document.getElementById('bias-series-modal-close');
    if (closeBtn) closeBtn.addEventListener('click', closeBiasSeriesModal);
    if (overlay) {
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) closeBiasSeriesModal();
        });
    }
    if (modal && header) {
        modal.style.position = 'fixed';
        modal.style.left = '50%';
        modal.style.top = '50%';
        modal.style.transform = 'translate(-50%, -50%)';
        var dragging = false;
        var startX = 0;
        var startY = 0;
        var startLeft = 0;
        var startTop = 0;
        header.style.cursor = 'move';
        header.addEventListener('mousedown', function (e) {
            if (e.button !== 0) return;
            dragging = true;
            startX = e.clientX;
            startY = e.clientY;
            var rect = modal.getBoundingClientRect();
            startLeft = rect.left;
            startTop = rect.top;
            modal.style.transform = '';
            e.preventDefault();
        });
        document.addEventListener('mousemove', function (e) {
            if (!dragging) return;
            var dx = e.clientX - startX;
            var dy = e.clientY - startY;
            var newLeft = startLeft + dx;
            var newTop = startTop + dy;
            var vw = window.innerWidth || document.documentElement.clientWidth;
            var vh = window.innerHeight || document.documentElement.clientHeight;
            var rect = modal.getBoundingClientRect();
            var maxLeft = vw - rect.width;
            var maxTop = vh - rect.height;
            if (newLeft < 0) newLeft = 0;
            if (newTop < 0) newTop = 0;
            if (newLeft > maxLeft) newLeft = maxLeft;
            if (newTop > maxTop) newTop = maxTop;
            modal.style.left = newLeft + 'px';
            modal.style.top = newTop + 'px';
        });
        document.addEventListener('mouseup', function () {
            if (dragging) {
                saveEtfBiasMcdModalLayout(modal);
            }
            dragging = false;
        });
    }
    var modalChartWrap = document.getElementById('bias-series-chart-wrap');
    if (modalChartWrap && typeof ResizeObserver !== 'undefined') {
        var roChart = new ResizeObserver(function () {
            if (biasSeriesChartInstance && typeof biasSeriesChartInstance.resize === 'function') {
                biasSeriesChartInstance.resize();
            }
        });
        roChart.observe(modalChartWrap);
    }
    if (modal && modal.classList && modal.classList.contains('etf-bias-mcd-modal-shell') && typeof ResizeObserver !== 'undefined') {
        var roShell = new ResizeObserver(function () {
            scheduleSaveEtfBiasMcdModalLayout(modal);
        });
        roShell.observe(modal);
    }
    function scheduleSiblingBiasQuickPrefetch(tsCode, freq, col, kind, quickLim) {
        try {
            var selector = '.bias-cell-clickable[data-freq="' + freq + '"][data-col="' + col + '"]' + (kind === 'mcd' ? '[data-series-kind="mcd"]' : ':not([data-series-kind])');
            var nodes = document.querySelectorAll(selector);
            if (!nodes || nodes.length <= 1) return;
            var uniqTs = [];
            var seen = Object.create(null);
            for (var i = 0; i < nodes.length; i++) {
                var t = (nodes[i].getAttribute('data-ts-code') || '').trim();
                if (!t || t === tsCode || seen[t]) continue;
                seen[t] = true;
                uniqTs.push(t);
            }
            if (!uniqTs.length) return;
            // 控制并发，避免挤占当前可见请求
            var idx = 0;
            var concurrency = 2;
            function buildUrl(t) {
                if (kind === 'mcd') {
                    return rtMonitorApiUrl('/api/etf_mcd_series?ts_code=' + encodeURIComponent(t) + '&freq=' + encodeURIComponent(freq) + '&limit=' + encodeURIComponent(String(quickLim)));
                }
                return rtMonitorApiUrl('/api/etf_bias_series?ts_code=' + encodeURIComponent(t) + '&freq=' + encodeURIComponent(freq) + '&column=' + encodeURIComponent(col) + '&limit=' + encodeURIComponent(String(quickLim)));
            }
            function worker() {
                if (idx >= uniqTs.length) return Promise.resolve();
                var t = uniqTs[idx++];
                var url = buildUrl(t);
                var ck = 'biasSeries|' + url;
                if (getPopupPayloadCache(ck) || biasQuickPrefetchInFlight[ck]) {
                    return worker();
                }
                biasQuickPrefetchInFlight[ck] = true;
                return fetchJsonNoGuard(url, BIAS_SERIES_QUICK_TIMEOUT_MS)
                    .then(function (o) {
                        if (o && o.ok && o.data && !o.data.error) {
                            setPopupPayloadCache(ck, o.data, BIAS_SERIES_PAYLOAD_CACHE_TTL_MS);
                        }
                    })
                    .catch(function () { /* prefetch best effort */ })
                    .finally(function () {
                        delete biasQuickPrefetchInFlight[ck];
                    })
                    .then(worker);
            }
            for (var w = 0; w < concurrency; w++) {
                worker();
            }
        } catch (e) {
            /* prefetch best effort */
        }
    }

    function openBiasMcdSeriesFromCell(cell) {
        var tsCode = cell.getAttribute('data-ts-code');
        var freq = cell.getAttribute('data-freq');
        var col = cell.getAttribute('data-col');
        var kind = (cell.getAttribute('data-series-kind') || 'bias').toLowerCase();
        if (!tsCode || !freq || !col) return;
        if (biasSeriesChartInstance) {
            biasSeriesChartInstance.destroy();
            biasSeriesChartInstance = null;
        }
        var loadingEl = document.getElementById('bias-series-loading');
        if (loadingEl) loadingEl.style.display = 'flex';
        biasSeriesModalLayoutOpenTs = Date.now();
        applyEtfBiasMcdModalSavedLayout();
        var freqLabel = (freq === 'D') ? '日线' : freq;
        var rangeLbl = getEtfBiasMcdModalRangeLabel(freq);
        var chartTitle = (kind === 'mcd')
            ? (tsCode + ' ' + freqLabel + ' 聚集度Per — ' + rangeLbl)
            : ((col === '10MA') ? (tsCode + ' ' + freqLabel + ' 10MA — ' + rangeLbl) : (col === '20MA') ? (tsCode + ' ' + freqLabel + ' 20MA/20EMA — ' + rangeLbl) : (tsCode + ' ' + freqLabel + ' ' + col + ' — ' + rangeLbl));
        openBiasSeriesModal(chartTitle);
        var lim = getEtfBiasMcdModalSeriesLimit(freq);
        var quickLim = getEtfBiasMcdQuickSeriesLimit(freq, lim);
        var reqUrlFull = '';
        var reqUrlQuick = '';
        if (kind === 'mcd') {
            reqUrlFull = rtMonitorApiUrl('/api/etf_mcd_series?ts_code=' + encodeURIComponent(tsCode) + '&freq=' + encodeURIComponent(freq) + '&limit=' + encodeURIComponent(String(lim)));
            reqUrlQuick = rtMonitorApiUrl('/api/etf_mcd_series?ts_code=' + encodeURIComponent(tsCode) + '&freq=' + encodeURIComponent(freq) + '&limit=' + encodeURIComponent(String(quickLim)));
        } else {
            reqUrlFull = rtMonitorApiUrl('/api/etf_bias_series?ts_code=' + encodeURIComponent(tsCode) + '&freq=' + encodeURIComponent(freq) + '&column=' + encodeURIComponent(col) + '&limit=' + encodeURIComponent(String(lim)));
            reqUrlQuick = rtMonitorApiUrl('/api/etf_bias_series?ts_code=' + encodeURIComponent(tsCode) + '&freq=' + encodeURIComponent(freq) + '&column=' + encodeURIComponent(col) + '&limit=' + encodeURIComponent(String(quickLim)));
        }
        var cacheKeyFull = 'biasSeries|' + reqUrlFull;
        var cacheKeyQuick = 'biasSeries|' + reqUrlQuick;
        var openToken = Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 7);
        biasSeriesModalActiveToken = openToken;
        var modalOpenAt = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
        var firstPaintLogged = false;
        function logBiasModalFirstPaint(sourceTag) {
            if (firstPaintLogged) return;
            firstPaintLogged = true;
            var nowTs = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
            var costMs = Math.max(0, nowTs - modalOpenAt);
            if (typeof window !== 'undefined' && window.console && window.console.info) {
                window.console.info('[rt_monitor] BIAS/MCD 弹窗首屏耗时', {
                    tsCode: tsCode,
                    freq: freq,
                    col: col,
                    kind: kind,
                    source: sourceTag,
                    costMs: Number(costMs.toFixed ? costMs.toFixed(1) : costMs)
                });
            }
        }
        // 当前标的打开后，后台轻量预取同列同频其它标的，减少第二轮点击冷启动等待
        scheduleSiblingBiasQuickPrefetch(tsCode, freq, col, kind, quickLim);
        function isSameModalOpen() {
            return biasSeriesModalActiveToken === openToken;
        }
        function requestSeries(url, cacheKey, timeoutMs) {
            var reqToken = null;
            return fetchPopupJson(url, 'biasSeries', timeoutMs)
                .then(function (o) {
                    reqToken = o.token;
                    if (!isPopupFetchCurrent('biasSeries', o.token) || !isSameModalOpen()) return null;
                    if (!o.ok) {
                        return { __error: '加载失败: ' + (o.data && o.data.error ? o.data.error : 'HTTP ' + o.status) };
                    }
                    if (o.data && o.data.error) {
                        return { __error: o.data.error };
                    }
                    setPopupPayloadCache(cacheKey, o.data, BIAS_SERIES_PAYLOAD_CACHE_TTL_MS);
                    return o.data;
                })
                .catch(function (err) {
                    reqToken = err && err.__popupToken ? err.__popupToken : reqToken;
                    if (!isPopupFetchCurrent('biasSeries', reqToken) || !isSameModalOpen()) return null;
                    return { __abort: !!(err && err.name === 'AbortError'), __errObj: err };
                })
                .finally(function () {
                    if (reqToken) endPopupFetch('biasSeries', reqToken);
                });
        }
        function requestFullSeriesWithRetry(firstTag, secondTag) {
            requestSeries(reqUrlFull, cacheKeyFull, BIAS_SERIES_FULL_TIMEOUT_MS).then(function (fullRes) {
                if (!fullRes || !isSameModalOpen()) return;
                if (fullRes.__error) {
                    if (loadingEl) loadingEl.style.display = 'none';
                    showBiasSeriesMsg(fullRes.__error);
                    return;
                }
                if (!fullRes.__abort) {
                    if (loadingEl) loadingEl.style.display = 'none';
                    drawBiasSeriesChart(fullRes);
                    if (firstTag) logBiasModalFirstPaint(firstTag);
                    return;
                }
                // 全量第一次超时/取消：自动再试一次，减少偶发 timeout 直接暴露给用户
                logError((kind === 'mcd' ? 'MCD' : 'BIAS') + ' 全量序列首次超时/取消，自动重试', fullRes.__errObj);
                requestSeries(reqUrlFull, cacheKeyFull, BIAS_SERIES_FULL_TIMEOUT_MS).then(function (fullRetryRes) {
                    if (!fullRetryRes || !isSameModalOpen()) return;
                    if (fullRetryRes.__error) {
                        if (loadingEl) loadingEl.style.display = 'none';
                        showBiasSeriesMsg(fullRetryRes.__error);
                        return;
                    }
                    if (fullRetryRes.__abort) {
                        if (loadingEl) loadingEl.style.display = 'none';
                        showBiasSeriesMsg('请求超时或已取消，请重试');
                        logError((kind === 'mcd' ? 'MCD' : 'BIAS') + ' 全量序列重试仍超时/取消', fullRetryRes.__errObj);
                        return;
                    }
                    if (loadingEl) loadingEl.style.display = 'none';
                    drawBiasSeriesChart(fullRetryRes);
                    if (secondTag) logBiasModalFirstPaint(secondTag);
                });
            });
        }
        var cachedFull = getPopupPayloadCache(cacheKeyFull);
        if (cachedFull) {
            if (loadingEl) loadingEl.style.display = 'none';
            drawBiasSeriesChart(cachedFull);
            logBiasModalFirstPaint('cache-full');
            return;
        }
        var cachedQuick = getPopupPayloadCache(cacheKeyQuick);
        if (cachedQuick) {
            if (loadingEl) loadingEl.style.display = 'none';
            drawBiasSeriesChart(cachedQuick);
            logBiasModalFirstPaint('cache-quick');
            if (reqUrlQuick !== reqUrlFull) {
                requestSeries(reqUrlFull, cacheKeyFull, POPUP_FETCH_TIMEOUT_MS).then(function (fullRes) {
                    if (!fullRes || !isSameModalOpen()) return;
                    if (!fullRes.__error && !fullRes.__abort) drawBiasSeriesChart(fullRes);
                });
            }
            return;
        }
        requestSeries(reqUrlQuick, cacheKeyQuick, BIAS_SERIES_QUICK_TIMEOUT_MS).then(function (quickRes) {
            if (!isSameModalOpen()) return;
            if (!quickRes) {
                // 竞态场景下 quick 结果可能被安全忽略；兜底尝试全量，避免界面长期停留“加载中...”
                requestFullSeriesWithRetry('net-full-after-quick-null', 'net-full-retry-after-quick-null');
                return;
            }
            if (quickRes.__error) {
                if (loadingEl) loadingEl.style.display = 'none';
                showBiasSeriesMsg(quickRes.__error);
                return;
            }
            if (quickRes.__abort) {
                // 快速窗口超时时，不直接判失败；继续尝试全窗口，成功则可用
                logError((kind === 'mcd' ? 'MCD' : 'BIAS') + ' 快速序列请求超时/取消，回退全量请求', quickRes.__errObj);
                requestFullSeriesWithRetry('net-full-after-quick-abort', 'net-full-retry-after-quick-abort');
                return;
            }
            if (loadingEl) loadingEl.style.display = 'none';
            drawBiasSeriesChart(quickRes);
            logBiasModalFirstPaint('net-quick');
            if (reqUrlQuick === reqUrlFull) return;
            requestSeries(reqUrlFull, cacheKeyFull, BIAS_SERIES_FULL_TIMEOUT_MS).then(function (fullRes) {
                if (!fullRes || !isSameModalOpen()) return;
                if (fullRes.__error) {
                    logError((kind === 'mcd' ? 'MCD' : 'BIAS') + ' 全量序列请求失败', fullRes.__error);
                    return;
                }
                if (fullRes.__abort) {
                    logError((kind === 'mcd' ? 'MCD' : 'BIAS') + ' 全量序列请求超时/取消', fullRes.__errObj);
                    return;
                }
                drawBiasSeriesChart(fullRes);
            });
        });
    }

    function handlePerCellClick(e) {
        var cell = e.target && e.target.closest && e.target.closest('.bias-cell-clickable');
        if (!cell) return;
        e.preventDefault();
        openBiasMcdSeriesFromCell(cell);
    }

    function handlePerCellKeydown(e) {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        var cell = e.target && e.target.closest && e.target.closest('.bias-cell-clickable');
        if (!cell) return;
        if (e.key === ' ') e.preventDefault();
        openBiasMcdSeriesFromCell(cell);
    }

    var tbody = document.getElementById('bias-table-body');
    if (tbody) {
        tbody.addEventListener('click', handlePerCellClick);
        tbody.addEventListener('keydown', handlePerCellKeydown);
    }
    var maTbody = document.getElementById('ma-signal-table-body');
    if (maTbody) {
        maTbody.addEventListener('click', handlePerCellClick);
        maTbody.addEventListener('keydown', handlePerCellKeydown);
    }
}

/** 拉取涨跌中位数、成交额、上涨比例等表数据，并渲染中位数/成交额/上涨比例/涨跌幅% 四张表 */
function fetchRtMedian() {
    if (!beginPollFetch('rtMedian')) return;
    fetchJsonNoGuard(rtMonitorApiUrl('/api/rt_median'), 10000)
        .then(function (o) {
            var data = o && o.data ? o.data : {};
            if (!o.ok || data.error) {
                logError('涨跌中位数接口返回失败', data && data.error ? data.error : ('HTTP ' + (o ? o.status : '--')));
                if (rtMedianLastOk) {
                    renderRtMedian(Object.assign({}, rtMedianLastOk, { __stale: true }));
                } else {
                    renderRtMedian({ error: '网络请求失败，请稍后重试' });
                }
                return;
            }
            if (!Array.isArray(data.table) || data.table.length < MEDIAN_QUAD_ROWS) {
                if (rtMedianLastOk) {
                    renderRtMedian(Object.assign({}, rtMedianLastOk, { __stale: true }));
                } else {
                    renderRtMedian(data);
                }
                return;
            }
            var merged = mergeRtMedianStableFields(data, rtMedianLastOk);
            rtMedianLastOk = merged;
            renderRtMedian(merged);
        })
        .catch(function (err) {
            logError('涨跌中位数请求失败', err);
            if (rtMedianLastOk) {
                renderRtMedian(Object.assign({}, rtMedianLastOk, { __stale: true }));
            } else {
                renderRtMedian({ error: '网络请求失败，请稍后重试' });
            }
        })
        .finally(function () {
            endPollFetch('rtMedian');
        });
}

/** 拉取国证A指分钟差值及当日/昨日累计序列，驱动分钟图与当日/昨日累计图 */
function fetchIdxMinuteDiff() {
    if (!window.Chart) return;
    fetch(rtMonitorApiUrl('/api/idx_minute_diff?ts_code=399317.SZ'))
        .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, status: res.status, data: data }; }); })
        .then(function (o) {
            if (!o.ok) {
                logError('分钟差值请求失败 HTTP ' + o.status, o.data && o.data.error);
                return;
            }
            if (o.data && o.data.error) {
                logError('分钟差值接口错误', o.data.error);
                return;
            }
            var data = o.data || {};
            renderIdxMinuteDiffChart(data);
            renderIdxCumsumChart(data);
        })
        .catch(function (err) {
            logError('分钟差值请求异常', err);
        });
}

/** 拉取当日/前五日均值分钟数据，驱动前五日均值分钟图与累计图 */
function fetchIdxMinuteDiffPrev5() {
    if (!window.Chart) return;
    fetch(rtMonitorApiUrl('/api/idx_minute_diff_prev5?ts_code=399317.SZ'))
        .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, status: res.status, data: data }; }); })
        .then(function (o) {
            var datesEl = document.getElementById('idx-prev5-dates');
            if (!o.ok || (o.data && o.data.error)) {
                if (datesEl) datesEl.textContent = '';
                return;
            }
            var data = o.data || {};
            // 前端不展示“基准日/前五日”的具体日期列表（仅保留图表本身）
            if (datesEl) datesEl.textContent = '';
            renderPrev5MinuteDiffChart(data);
            renderPrev5CumsumChart(data);
        })
        .catch(function (err) {
            logError('前五日均值分钟数据请求异常', err);
            var el = document.getElementById('idx-prev5-dates');
            if (el) el.textContent = '';
        });
}

/** 前五均值图表头：Ma(20) 与（预测/Ma(20)），与分钟差值图预测口径一致（Wind 优先） */
function idxPrev5Ma20HeaderSuffix(payload) {
    var predictTodayAmount = (typeof payload.predict_today_amount_wind === 'number') ? payload.predict_today_amount_wind : ((typeof payload.predict_today_amount === 'number') ? payload.predict_today_amount : null);
    var ma20Yi = (typeof payload.index_daily_ma20_amount_yi === 'number') ? payload.index_daily_ma20_amount_yi : null;
    if (ma20Yi == null || isNaN(ma20Yi) || ma20Yi <= 0) {
        return '';
    }
    var s = '；Ma(20) <span class="idx-predict-value">' + Math.round(ma20Yi) + ' 亿</span>';
    if (predictTodayAmount != null && !isNaN(predictTodayAmount) && predictTodayAmount > 0) {
        var ratioMaPct = (predictTodayAmount / ma20Yi) * 100;
        var ratioMaStr = ratioMaPct.toFixed(2) + '%';
        var ratioMaCls = ratioMaPct >= 100 ? 'rise' : 'fall';
        s += '（预测/Ma(20) <span class="' + ratioMaCls + '">' + ratioMaStr + '</span>）';
    }
    return s;
}

function renderPrev5MinuteDiffChart(payload) {
    var container = document.getElementById('idx-prev5-minute-diff-chart');
    var predictEl = document.getElementById('idx-prev5-predict');
    if (!container || !window.Chart) return;
    var times = Array.isArray(payload.times) ? payload.times : [];
    var diff = Array.isArray(payload.diff_amount) ? payload.diff_amount : [];
    var close = Array.isArray(payload.close) ? payload.close : [];
    var todayCumsum = Array.isArray(payload.today_cumsum) ? payload.today_cumsum : [];
    var prev5Cumsum = Array.isArray(payload.prev5_avg_cumsum) ? payload.prev5_avg_cumsum : [];
    var n = Math.min(times.length, diff.length, close.length);
    if (n === 0) {
        container.innerHTML = '<div class="rt-monitor-chart-empty">暂无前五日均值分钟数据</div>';
        if (predictEl) {
            var ma20Only = idxPrev5Ma20HeaderSuffix(payload);
            predictEl.innerHTML = ma20Only ? ma20Only.replace(/^；/, '') : '--';
        }
        if (idxPrev5MinuteDiffChartInstance) {
            idxPrev5MinuteDiffChartInstance.destroy();
            idxPrev5MinuteDiffChartInstance = null;
        }
        return;
    }
    times = times.slice(0, n);
    diff = diff.slice(0, n);
    close = close.slice(0, n);
    todayCumsum = todayCumsum.slice(0, n);
    prev5Cumsum = prev5Cumsum.slice(0, n);
    // 右上角文案：同分钟累计成交额相对前五日均值的偏离（与“今日预测”样式保持一致）
    if (predictEl) {
        var lastIdx = -1;
        for (var i = n - 1; i >= 0; i--) {
            var tv = todayCumsum[i];
            var pv = prev5Cumsum[i];
            if (tv != null && pv != null && !isNaN(parseFloat(tv)) && !isNaN(parseFloat(pv)) && parseFloat(pv) > 0) {
                lastIdx = i;
                break;
            }
        }
        if (lastIdx >= 0) {
            var todayVal = parseFloat(todayCumsum[lastIdx]);
            var prev5Val = parseFloat(prev5Cumsum[lastIdx]);
            var changeAmt = todayVal - prev5Val;
            var changePct = (changeAmt / prev5Val) * 100;
            var pctStr = (changePct >= 0 ? '+' : '') + changePct.toFixed(2) + '%';
            var amountStr = Math.abs(Math.round(changeAmt)) + ' 亿';
            var dirLabel = changeAmt > 0 ? '放量' : (changeAmt < 0 ? '缩量' : '持平');
            var cls = changeAmt > 0 ? 'rise' : (changeAmt < 0 ? 'fall' : '');
            var baseHtml;
            if (cls) {
                baseHtml = '（<span class="' + cls + '">' + pctStr + '，' + dirLabel + ' ' + amountStr + '</span>）';
            } else {
                baseHtml = '（' + pctStr + '，' + dirLabel + ' ' + amountStr + '）';
            }
            predictEl.innerHTML = baseHtml + idxPrev5Ma20HeaderSuffix(payload);
        } else {
            var ma20Fallback = idxPrev5Ma20HeaderSuffix(payload);
            predictEl.innerHTML = ma20Fallback ? ma20Fallback.replace(/^；/, '') : '--';
        }
    }
    var labels = times.map(function (t, idx) { return formatMinuteLabel(t, idx); });
    var validClose = close.map(function (v) { return (typeof v === 'number') ? v : parseFloat(v); }).filter(function (v) { return !isNaN(v); });
    var closeMin = null, closeMax = null;
    if (validClose.length > 0) {
        closeMin = Math.min.apply(null, validClose);
        closeMax = Math.max.apply(null, validClose);
        if (closeMin === closeMax) { var pad = closeMin * 0.01 || 1; closeMin -= pad; closeMax += pad; }
        else { var range = closeMax - closeMin; closeMin -= range * 0.05; closeMax += range * 0.05; }
    }
    if (!idxPrev5MinuteDiffChartInstance) {
        var canvas = document.createElement('canvas');
        container.innerHTML = '';
        container.appendChild(canvas);
        idxPrev5MinuteDiffChartInstance = new Chart(canvas, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    { type: 'bar', label: '成交额差值（亿）', data: diff, yAxisID: 'yDiff',
                        backgroundColor: idxDiffBarColor,
                        borderWidth: 0,
                    },
                    { type: 'line', label: '收盘价', data: close, yAxisID: 'yClose',
                        borderColor: 'rgba(155, 89, 182, 1)', backgroundColor: 'rgba(155, 89, 182, 0.12)',
                        tension: 0.35, fill: false, pointRadius: 0, pointHoverRadius: 3,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                animation: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        displayColors: false,
                        callbacks: {
                            title: function (items) { return items && items[0] ? '时间：' + items[0].label : ''; },
                            label: function (ctx) {
                                var v = ctx.parsed.y;
                                if (v === null || v === undefined || isNaN(v)) return '';
                                return ctx.datasetIndex === 0 ? '成交额差值：' + v.toFixed(2) + ' 亿' : '收盘价：' + v.toFixed(2);
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        display: true,
                        grid: { lineWidth: idxXGridLineWidth, color: idxXGridColor },
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: (typeof PAGE_FONT !== 'undefined' ? PAGE_FONT : 'sans-serif') },
                            autoSkip: false,
                            maxTicksLimit: 15,
                            callback: function (value) {
                                var label = (this && this.getLabelForValue) ? this.getLabelForValue(value) : value;
                                return idxXTickFilter(label);
                            },
                        },
                    },
                    yDiff: {
                        type: 'linear', position: 'left', title: { display: false },
                        ticks: { color: '#555', font: { size: RT_MONITOR_CHART_FONT_SIZE, family: (typeof PAGE_FONT !== 'undefined' ? PAGE_FONT : 'sans-serif') }, callback: function (val) { return formatAxisInt(val); } },
                        grid: { color: function (ctx) { return (ctx.tick && ctx.tick.value === 0) ? '#b0bec5' : '#eceff1'; }, lineWidth: function (ctx) { return (ctx.tick && ctx.tick.value === 0) ? 1.2 : 0.5; } },
                    },
                    yClose: {
                        type: 'linear', position: 'right', grid: { drawOnChartArea: false }, title: { display: false },
                        min: closeMin, max: closeMax,
                        ticks: { color: '#555', font: { size: RT_MONITOR_CHART_FONT_SIZE, family: (typeof PAGE_FONT !== 'undefined' ? PAGE_FONT : 'sans-serif') }, callback: function (val) { return formatAxisInt(val); } },
                    },
                },
            },
        });
    } else {
        var chart = idxPrev5MinuteDiffChartInstance;
        chart.options.animation = false;
        chart.data.labels = labels;
        if (chart.data.datasets && chart.data.datasets.length >= 2) {
            chart.data.datasets[0].data = diff;
            chart.data.datasets[1].data = close;
        }
        if (closeMin != null && closeMax != null && chart.options.scales && chart.options.scales.yClose) {
            chart.options.scales.yClose.min = closeMin;
            chart.options.scales.yClose.max = closeMax;
        }
        chart.update('none');
    }
}

/** 当日/前五日均值：成交额差额累计值图表与最新值展示（含相对前五日均值的变化比例） */
function renderPrev5CumsumChart(payload) {
    var container = document.getElementById('idx-prev5-cumsum-chart');
    var latestEl = document.getElementById('idx-prev5-cumsum-latest');
    if (!container || !window.Chart) return;
    var times = Array.isArray(payload.times) ? payload.times : [];
    var diffCumsum = Array.isArray(payload.diff_cumsum) ? payload.diff_cumsum : [];
    var prev5Cumsum = Array.isArray(payload.prev5_avg_cumsum) ? payload.prev5_avg_cumsum : [];
    var todayCumsum = Array.isArray(payload.today_cumsum) ? payload.today_cumsum : [];
    var n = Math.min(times.length, diffCumsum.length, prev5Cumsum.length, todayCumsum.length);
    if (n === 0) {
        setCumsumLatestText(latestEl, NaN);
        container.innerHTML = '<div class="rt-monitor-chart-empty">暂无累计值数据</div>';
        if (idxPrev5CumsumChartInstance) {
            idxPrev5CumsumChartInstance.destroy();
            idxPrev5CumsumChartInstance = null;
        }
        return;
    }
    times = times.slice(0, n);
    diffCumsum = diffCumsum.slice(0, n);
    prev5Cumsum = prev5Cumsum.slice(0, n);
    todayCumsum = todayCumsum.slice(0, n);
    // 最新差额：取差额累计序列最后一个非空值；变化比例 = 差额 / 前五日均值同索引累计（分母>0 时计算）
    var lastDiffPrev5 = null, lastIdxPrev5 = -1;
    for (var j = diffCumsum.length - 1; j >= 0; j--) {
        if (diffCumsum[j] != null && !isNaN(parseFloat(diffCumsum[j]))) {
            lastDiffPrev5 = diffCumsum[j];
            lastIdxPrev5 = j;
            break;
        }
    }
    var pctPrev5 = null;
    if (lastDiffPrev5 != null && lastIdxPrev5 >= 0 && prev5Cumsum[lastIdxPrev5] != null) {
        var ref5 = parseFloat(prev5Cumsum[lastIdxPrev5]);
        if (ref5 > 0) pctPrev5 = (lastDiffPrev5 / ref5) * 100;
    }
    setCumsumLatestText(latestEl, lastDiffPrev5, pctPrev5);
    var labels = times.map(function (t, idx) { return formatMinuteLabel(t, idx); });
    if (!idxPrev5CumsumChartInstance) {
        var canvas = document.createElement('canvas');
        container.innerHTML = '';
        container.appendChild(canvas);
        idxPrev5CumsumChartInstance = new Chart(canvas, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    {
                        type: 'bar',
                        label: '累计差值',
                        data: diffCumsum,
                        yAxisID: 'yCumDiff',
                        backgroundColor: idxDiffBarColor,
                        borderWidth: 0,
                    },
                    { type: 'line', label: '前五日均值', data: prev5Cumsum, yAxisID: 'yCumAmount', borderColor: 'rgba(255, 193, 7, 1)', backgroundColor: 'rgba(255, 193, 7, 0.1)', tension: 0.35, fill: false, pointRadius: 0, pointHoverRadius: 3 },
                    { type: 'line', label: '当日成交额', data: todayCumsum, yAxisID: 'yCumAmount', borderColor: 'rgba(183, 28, 28, 1)', backgroundColor: 'rgba(183, 28, 28, 0.1)', tension: 0.35, fill: false, pointRadius: 0, pointHoverRadius: 3 },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                animation: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        displayColors: false,
                        callbacks: {
                            title: function (items) {
                                if (!items || !items.length) return '';
                                return '时间：' + (items[0].label || '');
                            },
                            label: function (ctx) {
                                var v = ctx.parsed.y;
                                if (v === null || v === undefined || isNaN(v)) return ctx.dataset.label + ': --';
                                if (ctx.datasetIndex === 0) return '累计差值：' + v.toFixed(2) + ' 亿';
                                return ctx.dataset.label + '：' + v.toFixed(2) + ' 亿';
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        display: true,
                        grid: { lineWidth: idxXGridLineWidth, color: idxXGridColor },
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: (typeof PAGE_FONT !== 'undefined' ? PAGE_FONT : 'sans-serif') },
                            autoSkip: false,
                            maxTicksLimit: 15,
                            callback: function (value) {
                                var label = (this && this.getLabelForValue) ? this.getLabelForValue(value) : value;
                                return idxXTickFilter(label);
                            },
                        },
                    },
                    yCumDiff: {
                        type: 'linear',
                        position: 'left',
                        title: { display: false },
                        ticks: { color: '#555', font: { size: RT_MONITOR_CHART_FONT_SIZE, family: (typeof PAGE_FONT !== 'undefined' ? PAGE_FONT : 'sans-serif') }, callback: function (val) { return formatAxisInt(val); } },
                        grid: { color: function (ctx) { return (ctx.tick && ctx.tick.value === 0) ? '#b0bec5' : '#eceff1'; }, lineWidth: function (ctx) { return (ctx.tick && ctx.tick.value === 0) ? 1.2 : 0.5; } },
                    },
                    yCumAmount: {
                        type: 'linear',
                        position: 'right',
                        title: { display: false },
                        grid: { drawOnChartArea: false },
                        ticks: { color: '#555', font: { size: RT_MONITOR_CHART_FONT_SIZE, family: (typeof PAGE_FONT !== 'undefined' ? PAGE_FONT : 'sans-serif') }, callback: function (val) { return formatAxisInt(val); } },
                    },
                },
            },
        });
    } else {
        var chart = idxPrev5CumsumChartInstance;
        chart.options.animation = false;
        chart.data.labels = labels;
        if (chart.data.datasets && chart.data.datasets.length >= 3) {
            chart.data.datasets[0].data = diffCumsum;
            chart.data.datasets[1].data = prev5Cumsum;
            chart.data.datasets[2].data = todayCumsum;
        }
        chart.update('none');
    }
}

/** 当日/昨日：成交额差额累计值图表与最新值展示（含相对昨日同时刻的变化比例） */
function renderIdxCumsumChart(payload) {
    var container = document.getElementById('idx-cumsum-chart');
    var latestEl = document.getElementById('idx-cumsum-latest');
    if (!container || !window.Chart) return;
    var times = Array.isArray(payload.times) ? payload.times : [];
    var diffCumsum = Array.isArray(payload.diff_cumsum) ? payload.diff_cumsum : [];
    var yesterdayCumsum = Array.isArray(payload.yesterday_cumsum) ? payload.yesterday_cumsum : [];
    var todayCumsum = Array.isArray(payload.today_cumsum) ? payload.today_cumsum : [];
    var n = Math.min(times.length, diffCumsum.length, yesterdayCumsum.length, todayCumsum.length);
    if (n === 0) {
        setCumsumLatestText(latestEl, NaN);
        container.innerHTML = '<div class="rt-monitor-chart-empty">暂无累计值数据</div>';
        if (idxCumsumChartInstance) {
            idxCumsumChartInstance.destroy();
            idxCumsumChartInstance = null;
        }
        return;
    }
    times = times.slice(0, n);
    diffCumsum = diffCumsum.slice(0, n);
    yesterdayCumsum = yesterdayCumsum.slice(0, n);
    todayCumsum = todayCumsum.slice(0, n);
    // 最新差额：取差额累计序列最后一个非空值；变化比例 = 差额 / 昨日同索引累计（分母>0 时计算）
    var lastDiff = null, lastIdx = -1;
    for (var i = diffCumsum.length - 1; i >= 0; i--) {
        if (diffCumsum[i] != null && !isNaN(parseFloat(diffCumsum[i]))) {
            lastDiff = diffCumsum[i];
            lastIdx = i;
            break;
        }
    }
    var pctYday = null;
    if (lastDiff != null && lastIdx >= 0 && yesterdayCumsum[lastIdx] != null) {
        var refY = parseFloat(yesterdayCumsum[lastIdx]);
        if (refY > 0) pctYday = (lastDiff / refY) * 100;
    }
    setCumsumLatestText(latestEl, lastDiff, pctYday);
    var labels = times.map(function (t, idx) { return formatMinuteLabel(t, idx); });
    var canvas;
    if (!idxCumsumChartInstance) {
        canvas = document.createElement('canvas');
        container.innerHTML = '';
        container.appendChild(canvas);
        idxCumsumChartInstance = new Chart(canvas, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    {
                        type: 'bar',
                        label: '累计差值',
                        data: diffCumsum,
                        yAxisID: 'yCumDiff',
                        backgroundColor: idxDiffBarColor,
                        borderWidth: 0,
                    },
                    {
                        type: 'line',
                        label: '昨日成交额',
                        data: yesterdayCumsum,
                        yAxisID: 'yCumAmount',
                        borderColor: 'rgba(255, 193, 7, 1)',
                        backgroundColor: 'rgba(255, 193, 7, 0.1)',
                        tension: 0.35,
                        fill: false,
                        pointRadius: 0,
                        pointHoverRadius: 3,
                    },
                    {
                        type: 'line',
                        label: '实时成交额',
                        data: todayCumsum,
                        yAxisID: 'yCumAmount',
                        borderColor: 'rgba(183, 28, 28, 1)',
                        backgroundColor: 'rgba(183, 28, 28, 0.1)',
                        tension: 0.35,
                        fill: false,
                        pointRadius: 0,
                        pointHoverRadius: 3,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                animation: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        displayColors: false,
                        callbacks: {
                            title: function (items) {
                                if (!items || !items.length) return '';
                                return '时间：' + (items[0].label || '');
                            },
                            label: function (ctx) {
                                var v = ctx.parsed.y;
                                if (v === null || v === undefined || isNaN(v)) return ctx.dataset.label + ': --';
                                if (ctx.datasetIndex === 0) return '累计差值：' + v.toFixed(2) + ' 亿';
                                return ctx.dataset.label + '：' + v.toFixed(2) + ' 亿';
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        display: true,
                        grid: { lineWidth: idxXGridLineWidth, color: idxXGridColor },
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: (typeof PAGE_FONT !== 'undefined' ? PAGE_FONT : 'sans-serif') },
                            autoSkip: false,
                            maxTicksLimit: 15,
                            callback: function (value) {
                                var label = (this && this.getLabelForValue) ? this.getLabelForValue(value) : value;
                                return idxXTickFilter(label);
                            },
                        },
                    },
                    yCumDiff: {
                        type: 'linear',
                        position: 'left',
                        title: { display: false },
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: (typeof PAGE_FONT !== 'undefined' ? PAGE_FONT : 'sans-serif') },
                            callback: function (val) { return formatAxisInt(val); },
                        },
                        grid: {
                            color: function (ctx) {
                                return ctx.tick && ctx.tick.value === 0 ? '#b0bec5' : '#eceff1';
                            },
                            lineWidth: function (ctx) {
                                var v = ctx.tick && ctx.tick.value;
                                return v === 0 ? 1.2 : 0.5;
                            },
                        },
                    },
                    yCumAmount: {
                        type: 'linear',
                        position: 'right',
                        title: { display: false },
                        grid: { drawOnChartArea: false },
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: (typeof PAGE_FONT !== 'undefined' ? PAGE_FONT : 'sans-serif') },
                            callback: function (val) { return formatAxisInt(val); },
                        },
                    },
                },
            },
        });
    } else {
        var chart = idxCumsumChartInstance;
        chart.options.animation = false;
        chart.data.labels = labels;
        if (chart.data.datasets && chart.data.datasets.length >= 3) {
            chart.data.datasets[0].data = diffCumsum;
            chart.data.datasets[1].data = yesterdayCumsum;
            chart.data.datasets[2].data = todayCumsum;
        }
        chart.update('none');
    }
}

/** 国证A指分钟成交额差值图 + 今日预测（Wind 优先/线性兜底）及较昨日变化比例/金额 */
function renderIdxMinuteDiffChart(payload) {
    var container = document.getElementById('idx-minute-diff-chart');
    var predictEl = document.getElementById('idx-minute-predict');
    if (!container) return;
    if (!window.Chart) {
        logError('Chart.js 未加载，无法渲染分钟差值图');
        return;
    }
    var times = Array.isArray(payload.times) ? payload.times : [];
    var diff = Array.isArray(payload.diff_amount) ? payload.diff_amount : [];
    var close = Array.isArray(payload.close) ? payload.close : [];
    // 优先使用 Wind 算法预测（当日/前9日同结构比值），无则用通达信线性外推
    var predictTodayAmount = (typeof payload.predict_today_amount_wind === 'number') ? payload.predict_today_amount_wind : ((typeof payload.predict_today_amount === 'number') ? payload.predict_today_amount : null);
    if (times.length === 0 || diff.length === 0 || close.length === 0) {
        container.innerHTML = '<div class="rt-monitor-chart-empty">暂无国证A指分钟数据</div>';
        if (predictEl) predictEl.innerHTML = '今日预测 <span class="idx-predict-value">-- 亿</span>';
        if (idxMinuteDiffChartInstance) {
            idxMinuteDiffChartInstance.destroy();
            idxMinuteDiffChartInstance = null;
        }
        return;
    }
    // 统一长度，防御异常
    var n = Math.min(times.length, diff.length, close.length);
    times = times.slice(0, n);
    diff = diff.slice(0, n);
    close = close.slice(0, n);

    // 今日预测（亿）深黄色；与上一交易日比较：正红放量/负绿缩量
    var yesterdayFullYi = (typeof payload.yesterday_full_amount_yi === 'number') ? payload.yesterday_full_amount_yi : null;
    var max60Amount = (typeof payload.marketinfo_max60_amount === 'number') ? payload.marketinfo_max60_amount : null;
    var max60Date = payload.marketinfo_max60_trade_date ? String(payload.marketinfo_max60_trade_date) : null;
    if (predictEl) {
        if (predictTodayAmount != null && !isNaN(predictTodayAmount)) {
            var v = Math.round(predictTodayAmount);
            var html = '今日预测 <span class="idx-predict-value">' + v + ' 亿</span>';
            if (yesterdayFullYi != null && !isNaN(yesterdayFullYi) && yesterdayFullYi > 0) {
                var changePct = (predictTodayAmount - yesterdayFullYi) / yesterdayFullYi * 100;
                var changeAmt = predictTodayAmount - yesterdayFullYi;
                var isUp = changeAmt > 0;
                var pctStr = (changePct >= 0 ? '+' : '') + changePct.toFixed(2) + '%';
                var labelStr = isUp ? ('放量 ' + Math.abs(Math.round(changeAmt)) + ' 亿') : ('缩量 ' + Math.abs(Math.round(changeAmt)) + ' 亿');
                html += ' （<span class="' + (isUp ? 'rise' : 'fall') + '">' + pctStr + '，' + labelStr + '</span>）';
            }
            if (max60Amount != null && !isNaN(max60Amount) && max60Amount > 0) {
                var ratioPct = (predictTodayAmount / max60Amount) * 100;
                var ratioStr = ratioPct.toFixed(2) + '%';
                var maxAmtRounded = Math.round(max60Amount);
                var dateStr = max60Date || '--';
                var ratioCls = ratioPct >= 100 ? 'rise' : 'fall';
                html += '；Max(60) ' + dateStr + ' <span class="idx-predict-value">' + maxAmtRounded + ' 亿</span>（预测/峰值 <span class="' + ratioCls + '">' + ratioStr + '</span>）';
            }
            predictEl.innerHTML = html;
        } else {
            predictEl.innerHTML = '今日预测 <span class="idx-predict-value">-- 亿</span>';
        }
    }

    var labels = times.map(function (t, idx) { return formatMinuteLabel(t, idx); });

    // 收盘价 Y 轴范围：忽略 null/NaN，留 5% 边距
    var validClose = close
        .map(function (v) { return (typeof v === 'number') ? v : parseFloat(v); })
        .filter(function (v) { return !isNaN(v); });
    var closeMin = null, closeMax = null;
    if (validClose.length > 0) {
        closeMin = Math.min.apply(null, validClose);
        closeMax = Math.max.apply(null, validClose);
        if (closeMin === closeMax) {
            // 单值时给一点上下浮动
            var pad = closeMin * 0.01 || 1;
            closeMin -= pad;
            closeMax += pad;
        } else {
            var range = closeMax - closeMin;
            var pad2 = range * 0.05;
            closeMin -= pad2;
            closeMax += pad2;
        }
    }

    var canvas;
    if (!idxMinuteDiffChartInstance) {
        canvas = document.createElement('canvas');
        container.innerHTML = '';
        container.appendChild(canvas);
        idxMinuteDiffChartInstance = new Chart(canvas, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    {
                        type: 'bar',
                        label: '成交额差值（亿）',
                        data: diff,
                        yAxisID: 'yDiff',
                        backgroundColor: idxDiffBarColor,
                        borderWidth: 0,
                    },
                    {
                        type: 'line',
                        label: '收盘价',
                        data: close,
                        yAxisID: 'yClose',
                        borderColor: 'rgba(155, 89, 182, 1)',      // 紫色主线
                        backgroundColor: 'rgba(155, 89, 182, 0.12)', // 淡紫色填充
                        tension: 0.35,
                        fill: false,
                        pointRadius: 0,
                        pointHoverRadius: 3,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                animation: false, // 关闭动画，减少闪烁
                plugins: {
                    // 不显示图例，保持画面简洁
                    legend: { display: false },
                    tooltip: {
                        displayColors: false,
                        callbacks: {
                            title: function (items) {
                                if (!items || !items.length) return '';
                                var t = items[0].label || '';
                                return '时间：' + t;
                            },
                            label: function (ctx) {
                                var v = ctx.parsed.y;
                                if (v === null || v === undefined || isNaN(v)) return '';
                                if (ctx.datasetIndex === 0) {
                                    return '成交额差值：' + v.toFixed(2) + ' 亿';
                                }
                                return '收盘价：' + v.toFixed(2);
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        display: true,
                        grid: { lineWidth: idxXGridLineWidth, color: idxXGridColor },
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                            autoSkip: false,
                            maxTicksLimit: 15,
                            callback: function (value) {
                                var label = (this && this.getLabelForValue) ? this.getLabelForValue(value) : value;
                                return idxXTickFilter(label);
                            },
                        },
                    },
                    yDiff: {
                        type: 'linear',
                        position: 'left',
                        title: { display: false },
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                            callback: function (val) { return formatAxisInt(val); },
                        },
                        grid: {
                            color: function (ctx) {
                                var v = ctx.tick && ctx.tick.value;
                                if (v === 0) return '#b0bec5'; // 0 轴稍深
                                return '#eceff1';
                            },
                            lineWidth: function (ctx) {
                                var v = ctx.tick && ctx.tick.value;
                                return v === 0 ? 1.2 : 0.5;
                            },
                        },
                    },
                    yClose: {
                        type: 'linear',
                        position: 'right',
                        grid: { drawOnChartArea: false },
                        title: { display: false },
                        min: closeMin,
                        max: closeMax,
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                            callback: function (val) { return formatAxisInt(val); },
                        },
                    },
                },
            },
        });
    } else {
        // 仅更新数据与标签，关闭动画避免闪烁
        var chart = idxMinuteDiffChartInstance;
        chart.options.animation = false;
        chart.data.labels = labels;
        if (chart.data.datasets && chart.data.datasets.length >= 2) {
            chart.data.datasets[0].data = diff;
            chart.data.datasets[1].data = close;
        }
        if (closeMin !== null && closeMax !== null && chart.options.scales && chart.options.scales.yClose) {
            chart.options.scales.yClose.min = closeMin;
            chart.options.scales.yClose.max = closeMax;
        }
        chart.update('none');
    }
}

/** 拉取 BIAS 百分位表并渲染；弹窗内序列图由单元格点击触发单独请求 */
function fetchBiasTable() {
    if (!beginPollFetch('biasTable')) return;
    fetchJsonNoGuard(rtMonitorApiUrl('/api/etf_bias_table'), 10000)
        .then(function (o) {
            var data = o && o.data ? o.data : {};
            if (!o.ok || data.error) {
                logError('BIAS 表接口返回错误', data && data.error ? data.error : ('HTTP ' + (o ? o.status : '--')));
                if (biasTableLastOk) {
                    renderBiasTable(Object.assign({}, biasTableLastOk, { __stale: true }));
                }
                return;
            }
            if (Array.isArray(data.rows) && data.rows.length > 0) {
                biasTableLastOk = data;
            }
            renderBiasTable(data);
        })
        .catch(function (err) {
            logError('BIAS 表请求失败', err);
            if (biasTableLastOk) {
                renderBiasTable(Object.assign({}, biasTableLastOk, { __stale: true }));
            }
        })
        .finally(function () {
            endPollFetch('biasTable');
        });
}

/** 与后端 ETF_MA_SIGNAL_ORDER 一致，用于下拉与默认选中 */
var MA_SIGNAL_ETF_ORDER = [
    '510300.SH', '510500.SH', '588000.SH', '159915.SZ', '588080.SH',
    '510050.SH', '159919.SZ', '159922.SZ', '159901.SZ',
];

var maSignalChartInstance = null;
var maSignalSelectedTs = MA_SIGNAL_ETF_ORDER[0] || '510300.SH';
var maSignalSelectedFreq = '60MIN';
/** 最近一次 MA 表接口数据，用于保存支撑阻力后仅重绘不重复请求 */
var maSignalTableLastPayload = null;
var MA_SR_STORAGE_KEY = 'rt_monitor_ma_sr_levels_v1';
/** 与服务器合并后的有效缓存；null 表示尚未完成首次 /api/ma_sr_levels 同步 */
var maSrLevelsCache = null;
/** 是否已成功把「仅 localStorage 有数据、服务器为空」迁移 POST 过一次（本页生命周期内） */
var maSrDidPushLocalToServer = false;

function formatEtfPrice3(value) {
    if (value === null || value === undefined || (typeof value === 'number' && isNaN(value))) {
        return '--';
    }
    var n = parseFloat(value);
    return isNaN(n) ? '--' : n.toFixed(3);
}

function loadMaSrLevelsFromStorageOnly() {
    try {
        var raw = localStorage.getItem(MA_SR_STORAGE_KEY);
        if (!raw) return {};
        var o = JSON.parse(raw);
        return o && typeof o === 'object' ? o : {};
    } catch (e) {
        return {};
    }
}

/** 读当前生效的支撑阻力：优先内存缓存（已与服务器合并），否则仅本地（同步前） */
function loadMaSrLevels() {
    if (maSrLevelsCache !== null) return maSrLevelsCache;
    return loadMaSrLevelsFromStorageOnly();
}

function maSrObjHasAnyValues(obj) {
    if (!obj || typeof obj !== 'object') return false;
    var keys = Object.keys(obj);
    for (var i = 0; i < keys.length; i++) {
        var L = obj[keys[i]];
        if (!L || typeof L !== 'object') continue;
        var ks = ['s1', 'r1', 's2', 'r2'];
        for (var j = 0; j < ks.length; j++) {
            var v = L[ks[j]];
            if (v === null || v === undefined || v === '') continue;
            var n = parseFloat(v);
            if (!isNaN(n)) return true;
        }
    }
    return false;
}

/** 合并：同字段服务器非空优先，否则本地（解决 localhost 与 IP 不同源 localStorage 隔离） */
function mergeMaSrServerLocal(server, local) {
    var codes = {};
    MA_SIGNAL_ETF_ORDER.forEach(function (c) { codes[c] = true; });
    if (server && typeof server === 'object') {
        Object.keys(server).forEach(function (c) { codes[c] = true; });
    }
    if (local && typeof local === 'object') {
        Object.keys(local).forEach(function (c) { codes[c] = true; });
    }
    var out = {};
    Object.keys(codes).forEach(function (code) {
        var S = (server && server[code]) || {};
        var L = (local && local[code]) || {};
        var row = { s1: null, r1: null, s2: null, r2: null };
        ['s1', 'r1', 's2', 'r2'].forEach(function (k) {
            var sv = S[k];
            var lv = L[k];
            var sn = (sv !== null && sv !== undefined && sv !== '') ? parseFloat(sv) : NaN;
            var ln = (lv !== null && lv !== undefined && lv !== '') ? parseFloat(lv) : NaN;
            if (!isNaN(sn)) row[k] = sn;
            else if (!isNaN(ln)) row[k] = ln;
            else row[k] = null;
        });
        out[code] = row;
    });
    return out;
}

function setMaSrLevelsCache(obj) {
    maSrLevelsCache = obj && typeof obj === 'object' ? obj : {};
    try {
        localStorage.setItem(MA_SR_STORAGE_KEY, JSON.stringify(maSrLevelsCache));
    } catch (e) { /* ignore quota */ }
}

/** 拉取服务器价位并与本地合并；若服务器全空而本地有数据则 POST 迁移一次；最后回调 */
function syncMaSrLevelsFromServer(done) {
    fetch(rtMonitorApiUrl('/api/ma_sr_levels'))
        .then(function (res) {
            if (!res.ok) return {};
            return res.json();
        })
        .then(function (srv) {
            if (!srv || typeof srv !== 'object' || srv.error) srv = {};
            var loc = loadMaSrLevelsFromStorageOnly();
            var needPush = !maSrDidPushLocalToServer && !maSrObjHasAnyValues(srv) && maSrObjHasAnyValues(loc);
            if (needPush) {
                return fetch(rtMonitorApiUrl('/api/ma_sr_levels'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(loc),
                }).then(function (r) {
                    if (r.ok) maSrDidPushLocalToServer = true;
                    return srv;
                }).catch(function () { return srv; });
            }
            return Promise.resolve(srv);
        })
        .then(function (srv) {
            if (!srv || typeof srv !== 'object') srv = {};
            var loc = loadMaSrLevelsFromStorageOnly();
            var eff = mergeMaSrServerLocal(srv, loc);
            setMaSrLevelsCache(eff);
            var cfgPanel = document.getElementById('config-panel');
            if (cfgPanel && cfgPanel.classList.contains('config-panel-visible') && typeof renderMaSrConfigTable === 'function') {
                renderMaSrConfigTable(true);
            }
            if (typeof done === 'function') done();
        })
        .catch(function () {
            setMaSrLevelsCache(loadMaSrLevelsFromStorageOnly());
            if (typeof done === 'function') done();
        });
}

function saveMaSrLevels(obj) {
    var payload = obj && typeof obj === 'object' ? obj : {};
    setMaSrLevelsCache(payload);
    fetch(rtMonitorApiUrl('/api/ma_sr_levels'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    }).catch(function () { /* 离线时仅本地 */ });
}

function parseMaSrInput(val) {
    if (val === '' || val === null || val === undefined) return null;
    var n = parseFloat(String(val).replace(/,/g, ''));
    return isNaN(n) ? null : n;
}

/** 配置面板内：按 ETF 生成支撑阻力输入表（仅首次或强制刷新时填充） */
function renderMaSrConfigTable(force) {
    var wrap = document.getElementById('config-ma-sr-table-wrap');
    if (!wrap || (wrap.getAttribute('data-filled') === '1' && !force)) return;
    var levels = loadMaSrLevels();
    var rows = MA_SIGNAL_ETF_ORDER.map(function (code) {
        var L = levels[code] || {};
        var esc = String(code).replace(/"/g, '&quot;');
        return '<tr data-ts-code="' + esc + '">' +
            '<td class="config-ma-sr-code">' + code + '</td>' +
            '<td><input type="number" class="config-ma-sr-inp" data-k="s1" step="0.001" placeholder="—" value="' +
            (L.s1 != null ? String(L.s1) : '') + '"></td>' +
            '<td><input type="number" class="config-ma-sr-inp" data-k="r1" step="0.001" placeholder="—" value="' +
            (L.r1 != null ? String(L.r1) : '') + '"></td>' +
            '<td><input type="number" class="config-ma-sr-inp" data-k="s2" step="0.001" placeholder="—" value="' +
            (L.s2 != null ? String(L.s2) : '') + '"></td>' +
            '<td><input type="number" class="config-ma-sr-inp" data-k="r2" step="0.001" placeholder="—" value="' +
            (L.r2 != null ? String(L.r2) : '') + '"></td>' +
            '</tr>';
    }).join('');
    wrap.innerHTML = '<table class="config-ma-sr-table" aria-label="MA表支撑阻力"><thead><tr>' +
        '<th>代码</th><th>支撑一</th><th>阻力一</th><th>支撑二</th><th>阻力二</th></tr></thead><tbody>' +
        rows + '</tbody></table>';
    wrap.setAttribute('data-filled', '1');
}

function collectMaSrLevelsFromForm() {
    var out = {};
    var wrap = document.getElementById('config-ma-sr-table-wrap');
    if (!wrap) return out;
    [].forEach.call(wrap.querySelectorAll('tr[data-ts-code]'), function (tr) {
        var code = tr.getAttribute('data-ts-code');
        if (!code) return;
        var o = { s1: null, r1: null, s2: null, r2: null };
        [].forEach.call(tr.querySelectorAll('.config-ma-sr-inp'), function (inp) {
            var k = inp.getAttribute('data-k');
            if (!k) return;
            o[k] = parseMaSrInput(inp.value);
        });
        out[code] = o;
    });
    return out;
}

/** 支撑/阻力列中文名（用于悬停提示） */
function maSrKindLabel(kind) {
    var map = { s1: '支撑一', r1: '阻力一', s2: '支撑二', r2: '阻力二' };
    return map[kind] || String(kind);
}

/** 提示中展示的设置价位（三位小数，与 ETF 行情列 formatEtfPrice3 一致） */
function formatMaSrLevelTooltip(lv) {
    var n = parseFloat(lv);
    if (isNaN(n)) return '--';
    return n.toFixed(3);
}

/** HTML 属性 title 内转义，避免引号截断 */
function escapeHtmlAttr(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;');
}

/** 支撑/阻力浮层内百分比：与全站红涨绿跌一致（正 .rise / 负 .fall / 零 .ma-sr-pct-neutral） */
function maSrTooltipPctClass(pct) {
    if (pct > 0) return 'rise';
    if (pct < 0) return 'fall';
    return 'ma-sr-pct-neutral';
}

/** 浮层 HTML 存 data-ma-sr-tip-json（JSON），aria-label 为纯文本供读屏 */
function maSrTooltipDataAttr(htmlInner, ariaPlain) {
    var payload = JSON.stringify({ html: htmlInner });
    return ' data-ma-sr-tip-json="' + escapeHtmlAttr(payload) + '" aria-label="' + escapeHtmlAttr(ariaPlain) + '"';
}

function maSrCellHtml(kind, level, close, high, low) {
    if (level === null || level === undefined || level === '') {
        return '<td class="median-table-num ma-sr-cell">--</td>';
    }
    var lv = parseFloat(level);
    if (isNaN(lv)) {
        return '<td class="median-table-num ma-sr-cell">--</td>';
    }
    var c = close != null ? parseFloat(close) : NaN;
    var hi = high != null ? parseFloat(high) : NaN;
    var lo = low != null ? parseFloat(low) : NaN;
    if (isNaN(c) || c === 0) {
        return '<td class="median-table-num ma-sr-cell">--</td>';
    }
    var pct = (lv - c) / c * 100;
    var isR = kind.indexOf('r') === 0;
    var isS = kind.indexOf('s') === 0;
    var name = maSrKindLabel(kind);
    var priceStr = formatMaSrLevelTooltip(lv);
    var tipPct = formatMedian(pct);
    var pctCls = maSrTooltipPctClass(pct);
    if (isR && !isNaN(hi) && hi >= lv) {
        var tipB = name + ' 设置价位：' + priceStr + '；已向上突破（相对幅度约 ' + tipPct + '%）';
        var htmlB = name + ' 设置价位：' + priceStr + '；已向上突破（相对幅度约 <span class="' + pctCls + '">' + tipPct + '%</span>）';
        return '<td class="median-table-num ma-sr-cell ma-sr-breakout"' + maSrTooltipDataAttr(htmlB, tipB) + '><span class="ma-sr-label">突破</span></td>';
    }
    if (isS && !isNaN(lo) && lo <= lv) {
        var tipD = name + ' 设置价位：' + priceStr + '；已向下跌破（相对幅度约 ' + tipPct + '%）';
        var htmlD = name + ' 设置价位：' + priceStr + '；已向下跌破（相对幅度约 <span class="' + pctCls + '">' + tipPct + '%</span>）';
        return '<td class="median-table-num ma-sr-cell ma-sr-breakdown"' + maSrTooltipDataAttr(htmlD, tipD) + '><span class="ma-sr-label">破位</span></td>';
    }
    var sign = pct > 0 ? '+' : '';
    var tipN = name + ' 设置价位：' + priceStr + '；相对当前价约 ' + sign + tipPct + '%';
    var htmlN = name + ' 设置价位：' + priceStr + '；相对当前价约 <span class="' + pctCls + '">' + sign + tipPct + '%</span>';
    return '<td class="median-table-num ma-sr-cell"' + maSrTooltipDataAttr(htmlN, tipN) + '><span class="median-cell-value ma-sr-pct-cell">' + tipPct + '</span></td>';
}

function maSignalCellHtml(token) {
    if (token === 'up') {
        // 用“空 span + CSS 三角形”绘制，避免 Unicode 字形差导致的对齐漂移
        return '<span class="ma-signal-arrow ma-signal-up" title="多头：(C-MA)&gt;0 且 (C-EMA)&gt;0 且 (C-REF)&gt;0"></span>';
    }
    if (token === 'down') {
        return '<span class="ma-signal-arrow ma-signal-down" title="空头：三式均 &lt;0"></span>';
    }
    return '<span class="ma-signal-none" title="无共振">—</span>';
}

function formatPerMcdCell(value) {
    if (value === null || value === undefined || (typeof value === 'number' && isNaN(value))) {
        return '--';
    }
    var n = parseFloat(value);
    return isNaN(n) ? '--' : n.toFixed(2);
}

function mcdPerCellHtml(tsCode, freq, value) {
    var sigmaClass = getBiasSigmaClass(value);
    var attrs = ' data-ts-code="' + String(tsCode || '').replace(/"/g, '&quot;') + '"' +
        ' data-freq="' + String(freq || '').replace(/"/g, '&quot;') + '"' +
        ' data-col="MCD" data-series-kind="mcd"';
    return '<td class="median-table-num bias-cell' + sigmaClass + '"><span class="bias-cell-value bias-cell-clickable mcd-cell-clickable" tabindex="0" role="button"' + attrs + '>' + formatPerMcdCell(value) + '</span></td>';
}

/** 仅更新单元格内容/结构，供定时刷新时避免整表 innerHTML 闪动 */
function patchMaSignalTableRow(tr, row, levelsMap, rowIndex) {
    tr.className = 'median-table-row ma-signal-row' + ((rowIndex % 2 === 1) ? ' median-table-zebra' : '');
    var ts = row.ts_code || '--';
    var L = levelsMap[ts] || {};
    var last = row.etf_last;
    var hi = row.etf_high;
    var lo = row.etf_low;
    tr.setAttribute('data-ts-code', ts);
    tr.cells[0].textContent = ts;
    var amtSpan = tr.querySelector('.ma-signal-amt-value');
    if (amtSpan) amtSpan.textContent = formatAmountYi2(row.etf_amount_yi);
    tr.cells[2].textContent = formatEtfPrice3(last);
    var valsQ = [row.etf_pct, row.etf_intra, row.etf_gap];
    ['pct', 'intra', 'gap'].forEach(function (qk, j) {
        var span = tr.querySelector('.ma-signal-idx-cell[data-q="' + qk + '"]');
        if (span) span.textContent = formatMedian(valsQ[j]);
    });
    var ampSp = tr.querySelector('.ma-signal-idx-cell[data-q="amp"]');
    if (ampSp) ampSp.textContent = formatMedian(row.etf_amp);
    var rkMapM = { t1: 'range_pct_t1', t5: 'range_pct_t5', t20: 'range_pct_t20', t60: 'range_pct_t60', t120: 'range_pct_t120' };
    ['t1', 't5', 't20', 't60', 't120'].forEach(function (rk) {
        var sp = tr.querySelector('.ma-signal-range-cell[data-rk="' + rk + '"]');
        if (sp) sp.textContent = formatMedian(row[rkMapM[rk]]);
    });
    var dirCells = tr.querySelectorAll('.ma-signal-dir-cell');
    if (dirCells.length >= 4) {
        dirCells[0].innerHTML = maSignalCellHtml(row['60MIN_20']);
        dirCells[1].innerHTML = maSignalCellHtml(row['60MIN_60']);
        dirCells[2].innerHTML = maSignalCellHtml(row['D_20']);
        dirCells[3].innerHTML = maSignalCellHtml(row['D_60']);
    }
    tr.cells[16].outerHTML = mcdPerCellHtml(ts, '15MIN', row['PMCD_15']);
    tr.cells[17].outerHTML = mcdPerCellHtml(ts, '60MIN', row['PMCD_60']);
    tr.cells[18].outerHTML = mcdPerCellHtml(ts, 'D', row['PMCD_D']);
    tr.cells[19].outerHTML = maSrCellHtml('s1', L.s1, last, hi, lo);
    tr.cells[20].outerHTML = maSrCellHtml('r1', L.r1, last, hi, lo);
    tr.cells[21].outerHTML = maSrCellHtml('s2', L.s2, last, hi, lo);
    tr.cells[22].outerHTML = maSrCellHtml('r2', L.r2, last, hi, lo);
}

function finalizeMaSignalTableRow(tr, row) {
    var vals = [row.etf_pct, row.etf_intra, row.etf_gap];
    ['pct', 'intra', 'gap'].forEach(function (qk, j) {
        var span = tr.querySelector('.ma-signal-idx-cell[data-q="' + qk + '"]');
        if (span) applyMedianClass(span, vals[j]);
    });
    var ampSpan = tr.querySelector('.ma-signal-idx-cell[data-q="amp"]');
    if (ampSpan) ampSpan.classList.remove('rise', 'fall');
    paintMaSignalMetricTail(tr, row);
    ['t1', 't5', 't20', 't60', 't120'].forEach(function (rk) {
        var span = tr.querySelector('.ma-signal-range-cell[data-rk="' + rk + '"]');
        var keyMap = { t1: 'range_pct_t1', t5: 'range_pct_t5', t20: 'range_pct_t20', t60: 'range_pct_t60', t120: 'range_pct_t120' };
        applyMedianClass(span, row[keyMap[rk]]);
    });
    [].forEach.call(tr.querySelectorAll('.ma-sr-pct-cell'), function (span) {
        var v = parseFloat(span.textContent);
        applyMedianClass(span, isNaN(v) ? null : v);
    });
}

function renderMaSignalTable(data) {
    var tbody = document.getElementById('ma-signal-table-body');
    if (!tbody) return;
    var rows = Array.isArray(data.rows) ? data.rows : [];
    if (rows.length > 0) {
        maSignalTableLastPayload = data;
    }
    if (rows.length === 0) {
        if (data && (data.__loading || data.warming)) {
            if (tbody.rows.length === 0) {
                tbody.innerHTML = '<tr class="median-table-row"><td class="median-table-cat" colspan="' + MA_SIGNAL_COLS + '">MA/EMA 数据加载中...</td></tr>';
            }
            return;
        }
        return;
    }
    var levelsMap = loadMaSrLevels();
    if (rows.length > 0 && tbody.rows.length === rows.length) {
        var canPatchMs = true;
        for (var mi = 0; mi < rows.length; mi++) {
            var trc = tbody.rows[mi];
            if (!trc.classList.contains('ma-signal-row') || trc.cells.length !== MA_SIGNAL_COLS || !trc.querySelector('.idx-metric-per[data-per="amp"]')) {
                canPatchMs = false;
                break;
            }
        }
        if (canPatchMs) {
            for (var mj = 0; mj < rows.length; mj++) {
                patchMaSignalTableRow(tbody.rows[mj], rows[mj], levelsMap, mj);
                finalizeMaSignalTableRow(tbody.rows[mj], rows[mj]);
            }
            return;
        }
    }
    tbody.innerHTML = rows.map(function (row, i) {
        var trClass = (i % 2 === 1) ? ' median-table-zebra' : '';
        var ts = row.ts_code || '--';
        var L = levelsMap[ts] || {};
        var amountYi = row.etf_amount_yi;
        var last = row.etf_last;
        var hi = row.etf_high;
        var lo = row.etf_low;
        var pct = row.etf_pct;
        var gap = row.etf_gap;
        var intra = row.etf_intra;
        var amp = row.etf_amp;
        return '<tr class="median-table-row ma-signal-row' + trClass + '" data-ts-code="' + String(ts).replace(/"/g, '&quot;') + '">' +
            '<td class="median-table-cat">' + ts + '</td>' +
            '<td class="median-table-num ma-signal-amt-cell" title="AMT：rt_k.amount 当日累计成交额，元÷1e8。旁注为 etf_daily 全历史分位"><span class="ma-signal-amt-value" data-q="amt">' + formatAmountYi2(amountYi) + '</span><span class="idx-metric-per" data-per="amt" title="全历史分位"></span></td>' +
            '<td class="median-table-num ma-signal-quote-cell">' + formatEtfPrice3(last) + '</td>' +
            '<td class="median-table-num"><span class="median-cell-value ma-signal-idx-cell" data-q="pct">' + formatMedian(pct) + '</span><span class="idx-metric-per" data-per="pct" title="全历史分位"></span></td>' +
            '<td class="median-table-num"><span class="median-cell-value ma-signal-idx-cell" data-q="intra">' + formatMedian(intra) + '</span><span class="idx-metric-per" data-per="intra" title="全历史分位"></span></td>' +
            '<td class="median-table-num"><span class="median-cell-value ma-signal-idx-cell" data-q="gap">' + formatMedian(gap) + '</span><span class="idx-metric-per" data-per="gap" title="全历史分位"></span></td>' +
            '<td class="median-table-num"><span class="median-cell-value ma-signal-idx-cell" data-q="amp">' + formatMedian(amp) + '</span><span class="idx-metric-per" data-per="amp" title="全历史分位；对照完整交易日，盘中常偏低"></span></td>' +
            '<td class="median-table-num"><span class="median-cell-value ma-signal-range-cell" data-rk="t1">' + formatMedian(row.range_pct_t1) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value ma-signal-range-cell" data-rk="t5">' + formatMedian(row.range_pct_t5) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value ma-signal-range-cell" data-rk="t20">' + formatMedian(row.range_pct_t20) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value ma-signal-range-cell" data-rk="t60">' + formatMedian(row.range_pct_t60) + '</span></td>' +
            '<td class="median-table-num"><span class="median-cell-value ma-signal-range-cell" data-rk="t120">' + formatMedian(row.range_pct_t120) + '</span></td>' +
            '<td class="median-table-num ma-signal-dir-cell">' + maSignalCellHtml(row['60MIN_20']) + '</td>' +
            '<td class="median-table-num ma-signal-dir-cell">' + maSignalCellHtml(row['60MIN_60']) + '</td>' +
            '<td class="median-table-num ma-signal-dir-cell">' + maSignalCellHtml(row['D_20']) + '</td>' +
            '<td class="median-table-num ma-signal-dir-cell">' + maSignalCellHtml(row['D_60']) + '</td>' +
            mcdPerCellHtml(ts, '15MIN', row['PMCD_15']) +
            mcdPerCellHtml(ts, '60MIN', row['PMCD_60']) +
            mcdPerCellHtml(ts, 'D', row['PMCD_D']) +
            maSrCellHtml('s1', L.s1, last, hi, lo) +
            maSrCellHtml('r1', L.r1, last, hi, lo) +
            maSrCellHtml('s2', L.s2, last, hi, lo) +
            maSrCellHtml('r2', L.r2, last, hi, lo) +
            '</tr>';
    }).join('');
    rows.forEach(function (row, i) {
        var tr = tbody.children[i];
        if (!tr) return;
        finalizeMaSignalTableRow(tr, row);
    });
}

function populateMaSignalTsSelect() {
    var sel = document.getElementById('ma-signal-ts-select');
    if (!sel) return;
    sel.innerHTML = MA_SIGNAL_ETF_ORDER.map(function (c) {
        return '<option value="' + c.replace(/"/g, '&quot;') + '">' + c + '</option>';
    }).join('');
    sel.value = maSignalSelectedTs;
}

function highlightMaSignalRow(tsCode) {
    var tbody = document.getElementById('ma-signal-table-body');
    if (!tbody) return;
    [].forEach.call(tbody.querySelectorAll('.ma-signal-row'), function (tr) {
        var c = tr.getAttribute('data-ts-code');
        if (c === tsCode) tr.classList.add('ma-signal-row-selected');
        else tr.classList.remove('ma-signal-row-selected');
    });
}

/** 与 BIAS 弹窗类似的 X 轴稀疏刻度（日线 / 分钟跨日 9:30） */
function maSignalBuildTickIndices(times, isDaily) {
    var labels = times.map(function (t, i) {
        try {
            var d = new Date(t);
            if (isNaN(d.getTime())) return '' + i;
            if (isDaily) return (d.getMonth() + 1) + '/' + d.getDate();
            return (d.getMonth() + 1) + '/' + d.getDate() + ' ' + (d.getHours() < 10 ? '0' : '') + d.getHours() + ':' + (d.getMinutes() < 10 ? '0' : '') + d.getMinutes();
        } catch (e) { return '' + i; }
    });
    var xTickIndicesByDate = [];
    if (isDaily) {
        for (var i = 0; i < labels.length; i += Math.max(1, Math.floor(labels.length / 20))) {
            xTickIndicesByDate.push(i);
        }
        if (labels.length > 0 && xTickIndicesByDate.indexOf(labels.length - 1) === -1) {
            xTickIndicesByDate.push(labels.length - 1);
        }
    } else {
        var lastDateKey = '';
        var tickIdxForDay = -1;
        for (var idx = 0; idx < times.length; idx++) {
            try {
                var dt = new Date(times[idx]);
                if (isNaN(dt.getTime())) continue;
                var dateKey = dt.getFullYear() + '-' + (dt.getMonth() + 1) + '-' + dt.getDate();
                var is930 = (dt.getHours() === 9 && dt.getMinutes() === 30);
                if (dateKey !== lastDateKey) {
                    if (lastDateKey !== '' && tickIdxForDay >= 0) xTickIndicesByDate.push(tickIdxForDay);
                    tickIdxForDay = idx;
                    if (is930) tickIdxForDay = idx;
                    lastDateKey = dateKey;
                } else if (is930) {
                    tickIdxForDay = idx;
                }
            } catch (e) { /* skip */ }
        }
        if (lastDateKey !== '' && tickIdxForDay >= 0) xTickIndicesByDate.push(tickIdxForDay);
    }
    return { labels: labels, xTickIndicesByDate: xTickIndicesByDate };
}

function drawMaSignalChart(payload) {
    var canvas = document.getElementById('ma-signal-chart');
    if (!canvas || !window.Chart) return;
    if (maSignalChartInstance) {
        maSignalChartInstance.destroy();
        maSignalChartInstance = null;
    }
    var times = payload.times || [];
    var close = payload.close || [];
    var s20 = payload.signal_20 || [];
    var s60 = payload.signal_60 || [];
    if (times.length === 0) {
        return;
    }
    var isDaily = payload.freq === 'D';
    var tickInfo = maSignalBuildTickIndices(times, isDaily);
    var labels = tickInfo.labels;
    var xTickIndicesByDate = tickInfo.xTickIndicesByDate;
    function seriesAtSignal(sigArr, want, closeArr) {
        return sigArr.map(function (s, i) {
            if (s === want && closeArr[i] != null && !isNaN(Number(closeArr[i]))) return Number(closeArr[i]);
            return null;
        });
    }
    var up20 = seriesAtSignal(s20, 1, close);
    var down20 = seriesAtSignal(s20, -1, close);
    var up60 = seriesAtSignal(s60, 1, close);
    var down60 = seriesAtSignal(s60, -1, close);
    var dsClose = {
        type: 'line',
        label: 'close',
        data: close,
        borderColor: 'rgba(75, 192, 192, 1)',
        borderWidth: 2,
        tension: 0.25,
        fill: false,
        pointRadius: 0,
        pointHoverRadius: 3,
        order: 10,
    };
    var mkScatter = function (label, data, color, style, pointR) {
        return {
            type: 'scatter',
            label: label,
            data: data,
            borderColor: color,
            backgroundColor: color,
            pointRadius: pointR != null ? pointR : (style === 'triangle' ? 7 : 6),
            pointHoverRadius: 9,
            pointStyle: style,
            showLine: false,
            order: 0,
        };
    };
    var datasets = [
        dsClose,
        mkScatter('20↑', up20, 'rgba(231, 76, 60, 1)', 'triangle', 7),
        mkScatter('20↓', down20, 'rgba(46, 204, 113, 1)', 'triangle', 6),
        mkScatter('60↑', up60, 'rgba(192, 57, 43, 1)', 'circle', 7),
        mkScatter('60↓', down60, 'rgba(39, 174, 96, 1)', 'circle', 6),
    ];
    try {
        maSignalChartInstance = new Chart(canvas, {
            type: 'line',
            data: { labels: labels, datasets: datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                animation: false,
                elements: {
                    point: {
                        rotation: function (ctx) {
                            var lab = ctx.dataset && ctx.dataset.label;
                            if (lab === '20↓') return 180;
                            return 0;
                        },
                    },
                },
                plugins: {
                    /* myuiux：数据图默认不显示图例；系列区分靠 Tooltip */
                    legend: { display: false },
                    tooltip: {
                        displayColors: false,
                        callbacks: {
                            label: function (ctx) {
                                var v = ctx.raw;
                                if (v == null || (typeof v === 'number' && isNaN(v))) return ctx.dataset.label + ': (无)';
                                return ctx.dataset.label + ': ' + (typeof v === 'number' ? v.toFixed(3) : v);
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        ticks: {
                            color: '#555',
                            font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT },
                            maxRotation: 45,
                            callback: function (val, index) {
                                return xTickIndicesByDate.indexOf(index) >= 0 ? labels[index] : undefined;
                            },
                        },
                    },
                    y: {
                        ticks: { color: '#555', font: { size: RT_MONITOR_CHART_FONT_SIZE, family: PAGE_FONT } },
                    },
                },
            },
        });
    } catch (err) {
        logError('MA 信号图渲染失败', err);
    }
}

function fetchMaSignalChartData() {
    var canvas = document.getElementById('ma-signal-chart');
    if (!canvas || !maSignalSelectedTs) return;
    var qs = '?ts_code=' + encodeURIComponent(maSignalSelectedTs) + '&freq=' + encodeURIComponent(maSignalSelectedFreq);
    var reqUrl = rtMonitorApiUrl('/api/etf_ma_signal_series' + qs);
    var cacheKey = 'maSignalSeries|' + reqUrl;
    var cachedPayload = getPopupPayloadCache(cacheKey);
    if (cachedPayload) {
        drawMaSignalChart(cachedPayload);
        return;
    }
    var reqToken = null;
    fetchPopupJson(reqUrl, 'maSignalSeries')
        .then(function (o) {
            reqToken = o.token;
            if (!isPopupFetchCurrent('maSignalSeries', o.token)) return;
            if (!o.ok) {
                logError('MA 信号序列请求失败 HTTP ' + o.status, o.data && o.data.error);
                return;
            }
            if (o.data && o.data.error) {
                logError('MA 信号序列接口错误', o.data.error);
                return;
            }
            setPopupPayloadCache(cacheKey, o.data, POPUP_PAYLOAD_CACHE_TTL_MS);
            drawMaSignalChart(o.data);
        })
        .catch(function (err) {
            reqToken = err && err.__popupToken ? err.__popupToken : reqToken;
            if (!isPopupFetchCurrent('maSignalSeries', reqToken)) return;
            logError('MA 信号序列请求失败', err);
        })
        .finally(function () {
            if (reqToken) endPopupFetch('maSignalSeries', reqToken);
        });
}

/** 涨跌家数上次成功数据：请求失败时不刷成 --，避免与邻表同 tick 刷新时整行闪一下 */
var limitStatsLastOk = null;

/** 可弹出明细的 data-k（与后端 read_limit_stats_detail 一致） */
var LIMIT_STAT_DETAIL_KINDS = {
    cnt_h5: true,
    cnt_h10: true,
    cnt_h20: true,
    cnt_h60: true,
    cnt_h120: true,
    cnt_h200: true,
    cnt_h250: true,
    cnt_h850: true,
    cnt_h_high: true,
    cnt_l5: true,
    cnt_l10: true,
    cnt_l20: true,
    cnt_l60: true,
    cnt_l120: true,
    cnt_l200: true,
    cnt_l250: true,
    cnt_l850: true,
    cnt_l_low: true,
    cnt_high_limitone: true,
    cnt_high_limit: true,
    cnt_high_limit_lifting: true,
    cnt_low_limitone: true,
    cnt_low_limit: true,
    cnt_low_limit_lifting: true,
    cnt_lb_zt: true,
    cnt_lb_dt: true,
    zt_t1: true,
    dt_t1: true,
};

/** 弹窗标题 */
var LIMIT_STAT_DETAIL_TITLES = {
    cnt_h5: '5日新高明细',
    cnt_h10: '10日新高明细',
    cnt_h20: '20日新高明细',
    cnt_h60: '60日新高明细',
    cnt_h120: '120日新高明细',
    cnt_h200: '200日新高明细',
    cnt_h250: '250日新高明细',
    cnt_h850: '850日新高明细',
    cnt_h_high: '历史新高明细',
    cnt_l5: '5日新低明细',
    cnt_l10: '10日新低明细',
    cnt_l20: '20日新低明细',
    cnt_l60: '60日新低明细',
    cnt_l120: '120日新低明细',
    cnt_l200: '200日新低明细',
    cnt_l250: '250日新低明细',
    cnt_l850: '850日新低明细',
    cnt_l_low: '历史新低明细',
    cnt_high_limitone: '一字涨明细',
    cnt_high_limit: '涨停明细',
    cnt_high_limit_lifting: '炸板明细',
    cnt_low_limitone: '一字跌明细',
    cnt_low_limit: '跌停明细',
    cnt_low_limit_lifting: '开板明细',
    cnt_lb_zt: '连板涨停明细',
    cnt_lb_dt: '连板跌停明细',
    zt_t1: '涨停T-1明细',
    dt_t1: '跌停T-1明细',
};
var RT_MONITOR_FINA_IMPORT_SESSION_KEY = 'rt_monitor_fina_import_v1';
var limitStatsDetailCurrentKind = '';
var limitStatsDetailCurrentRows = [];
var LIMIT_STATS_FINA_IMPORT_KIND_MAP = {
    zt_t1: 'zt_t1',
    cnt_high_limit: 'cnt_high_limit',
    cnt_h850: 'cnt_h850',
    cnt_h_high: 'cnt_h_high',
};

/**
 * 旧版模板：T-1/T-5 曾放在「新高/新低」行，且 T-5 位可能是灰色「—」无 data-k，接口里的 zt_t5/dt_t5 无法写入。
 * 当前模板：T-1/T-5 已在「连板涨停/连板跌停」右侧，本函数对新高/新低行无 zt_t1 时会 no-op。
 * 仍保留：渲染前把旧 DOM 中 T-1 右侧第一个占位格升级为 zt_t5/dt_t5 数值格，兼容强缓存的旧 HTML。
 */
function patchLimitStatsT5FromLegacyMuted() {
    var rowHigh = document.getElementById('limit-stats-row-high');
    var rowLow = document.getElementById('limit-stats-row-low');
    if (!rowHigh || !rowLow) return;

    function patchLabelRow(dataRow, t1Needle, t5Text) {
        var labelTr = dataRow.previousElementSibling;
        if (!labelTr || !labelTr.classList || !labelTr.classList.contains('limit-stats-label-row')) return;
        /* 标签行现为 th（与 BIAS/MA 表头一致）；兼容旧缓存 td */
        var labelCells = labelTr.querySelectorAll('th, td');
        var i;
        for (i = 0; i < labelCells.length; i++) {
            var raw = (labelCells[i].textContent || '').replace(/\s/g, '');
            if (raw.indexOf(t1Needle) === -1) continue;
            var next = labelCells[i].nextElementSibling;
            if (next && next.classList.contains('limit-stats-cell-muted')) {
                next.classList.remove('limit-stats-cell-muted');
                next.textContent = t5Text;
            }
            break;
        }
    }

    function patchValueRow(row, t1Key, t5Key, riseFallClass) {
        if (row.querySelector('.js-limit-stat[data-k="' + t5Key + '"]')) return;
        var t1el = row.querySelector('.js-limit-stat[data-k="' + t1Key + '"]');
        if (!t1el) return;
        var next = t1el.nextElementSibling;
        if (!next || !next.classList.contains('limit-stats-cell-muted')) return;
        next.className = 'limit-stats-num ' + riseFallClass + ' js-limit-stat';
        next.setAttribute('data-k', t5Key);
        next.removeAttribute('title');
        next.textContent = '--';
    }

    patchLabelRow(rowHigh, '涨停T-1', '涨停T-5');
    patchLabelRow(rowLow, '跌停T-1', '跌停T-5');
    patchValueRow(rowHigh, 'zt_t1', 'zt_t5', 'rise');
    patchValueRow(rowLow, 'dt_t1', 'dt_t5', 'fall');
}

/** 为可点明细格补全 limit-stats-clickable（缓存旧 HTML 可能缺该类，导致仅 cnt_lb_zt 有下划线/可点）；并补键盘可达 tabindex/role */
function patchLimitStatsDetailCells() {
    var table = document.getElementById('limit-stats-table');
    if (!table) return;
    var cells = table.querySelectorAll('.js-limit-stat[data-k]');
    for (var i = 0; i < cells.length; i++) {
        var td = cells[i];
        var k = td.getAttribute('data-k');
        if (k && LIMIT_STAT_DETAIL_KINDS[k]) {
            td.classList.add('limit-stats-clickable');
            td.setAttribute('tabindex', '0');
            td.setAttribute('role', 'button');
        }
    }
    var upCell = document.getElementById('js-limit-stat-up-num-ma5-rt');
    if (upCell && upCell.classList.contains('bias-cell-clickable')) {
        upCell.setAttribute('tabindex', '0');
        upCell.setAttribute('role', 'button');
    }
}

/** 涨跌家数（不含 ST）面板（stockstat + limit_list_ths） */
function fetchLimitStatsPanel() {
    if (!beginPollFetch('limitStats')) return;
    fetchJsonNoGuard(rtMonitorApiUrl('/api/limit_stats?_=' + Date.now()), 10000)
        .then(function (o) {
            renderLimitStatsPanel((o && o.data) ? o.data : {});
        })
        .catch(function (err) {
            logError('涨跌家数请求失败', err);
            renderLimitStatsPanel({ ok: false });
        })
        .finally(function () {
            endPollFetch('limitStats');
        });
}

/** 超跌 / 超跌Per：默认黑色字；底色与 BIAS(Per) 表一致（2σ 黄、3σ 红）。超跌：数值&lt;25 标 2σ，&lt;20 标 3σ；超跌Per：同 getBiasSigmaClass（2.5/97.5 与 0.15/99.85） */
function paintLimitStatsUpNumMa5SigmaStyles(data) {
    var cellRt = document.getElementById('js-limit-stat-up-num-ma5-rt');
    var cellPer = document.getElementById('js-limit-stat-up-num-ma5-rt-per');
    function clearSigma(el) {
        if (!el) return;
        el.classList.remove('rise', 'fall', 'bias-sigma-2', 'bias-sigma-3');
    }
    clearSigma(cellRt);
    clearSigma(cellPer);
    if (!data) return;
    var vrt = data.up_num_ma5_rt;
    if (vrt !== null && vrt !== undefined) {
        var srt = String(vrt).trim();
        if (srt !== '' && srt !== '--') {
            var n = parseFloat(srt.replace(/,/g, ''));
            if (!isNaN(n)) {
                if (n < 20) cellRt.classList.add('bias-sigma-3');
                else if (n < 25) cellRt.classList.add('bias-sigma-2');
            }
        }
    }
    var vper = data.up_num_ma5_rt_per;
    var sigmaExtra = getBiasSigmaClass(vper);
    if (sigmaExtra.indexOf('bias-sigma-3') >= 0) cellPer.classList.add('bias-sigma-3');
    else if (sigmaExtra.indexOf('bias-sigma-2') >= 0) cellPer.classList.add('bias-sigma-2');
}

/** 涨跌家数表头：涨红、跌绿、平默认字色 */
function paintLimitStatsHeaderBreadth(data) {
    var specs = [
        { id: 'limit-stats-header-up', key: 'cnt_up', prefix: '涨：', colorClass: 'rise' },
        { id: 'limit-stats-header-flat', key: 'cnt_flat', prefix: '平：', colorClass: null },
        { id: 'limit-stats-header-down', key: 'cnt_down', prefix: '跌：', colorClass: 'fall' },
    ];
    for (var i = 0; i < specs.length; i++) {
        var spec = specs[i];
        var el = document.getElementById(spec.id);
        if (!el) continue;
        el.classList.remove('rise', 'fall');
        if (spec.colorClass) el.classList.add(spec.colorClass);
        if (!data) {
            el.textContent = spec.prefix + '--';
            continue;
        }
        var v = data[spec.key];
        var txt = (v === null || v === undefined || (typeof v === 'number' && isNaN(v))) ? '--' : String(v);
        el.textContent = spec.prefix + txt;
    }
}

/** 昨涨Pct / 昨跌Pct：正负红绿（与 HL_C 一致） */
function paintLimitStatsPrevZtDtPctRiseFall(data) {
    var table = document.getElementById('limit-stats-table');
    if (!table) return;
    var keys = ['prev_zt_pct', 'prev_dt_pct'];
    for (var p = 0; p < keys.length; p++) {
        var kk = keys[p];
        var cell = table.querySelector('.js-limit-stat[data-k="' + kk + '"]');
        if (!cell) continue;
        cell.classList.remove('rise', 'fall');
        if (!data) continue;
        var v = data[kk];
        if (v === null || v === undefined) continue;
        var n = parseFloat(v);
        if (isNaN(n)) continue;
        if (n > 0) cell.classList.add('rise');
        else if (n < 0) cell.classList.add('fall');
    }
}

/** HL_C 百分点：正值红（rise）、负值绿（fall），与涨跌家数配色一致；0 或 -- 不着色 */
function paintLimitStatsHlcRiseFall(data) {
    var keys = [
        'hlc_cccc_5', 'hlc_cccc_10', 'hlc_cccc_20', 'hlc_cccc_60', 'hlc_cccc_120',
        'hlc_cccc_200', 'hlc_cccc_250', 'hlc_cccc_850', 'hlc_cccc_h',
    ];
    var table = document.getElementById('limit-stats-table');
    if (!table) return;
    for (var i = 0; i < keys.length; i++) {
        var kk = keys[i];
        var cell = table.querySelector('.js-limit-stat[data-k="' + kk + '"]');
        if (!cell) continue;
        cell.classList.remove('rise', 'fall');
        if (!data) continue;
        var v = data[kk];
        if (v === null || v === undefined) continue;
        var s = String(v).trim();
        if (s === '' || s === '--') continue;
        var n = parseFloat(s.replace(/,/g, ''));
        if (isNaN(n)) continue;
        if (n > 0) cell.classList.add('rise');
        else if (n < 0) cell.classList.add('fall');
    }
}

function mergeLimitStatsFieldData(prev, next) {
    if (!next || next.ok === false || next.error) {
        return prev || next;
    }
    if (!prev || prev.ok === false) {
        return next;
    }
    var merged = {};
    var key;
    for (key in next) {
        if (Object.prototype.hasOwnProperty.call(next, key)) {
            merged[key] = next[key];
        }
    }
    var stickyKeys = [
        'hlc_cccc_5', 'hlc_cccc_10', 'hlc_cccc_20', 'hlc_cccc_60', 'hlc_cccc_120',
        'hlc_cccc_200', 'hlc_cccc_250', 'hlc_cccc_850', 'hlc_cccc_h',
        'zt_t1', 'zt_t5', 'dt_t1', 'dt_t5', 'cnt_lb_zt', 'cnt_lb_dt',
        'prev_zt_pct', 'prev_dt_pct', 'up_num_ma5_rt', 'up_num_ma5_rt_per',
    ];
    for (var i = 0; i < stickyKeys.length; i++) {
        key = stickyKeys[i];
        if ((merged[key] === null || merged[key] === undefined)
            && prev[key] !== null && prev[key] !== undefined) {
            merged[key] = prev[key];
        }
    }
    merged.ok = next.ok !== false;
    return merged;
}

function renderLimitStatsPanel(data) {
    patchLimitStatsT5FromLegacyMuted();
    var table = document.getElementById('limit-stats-table');
    var cells = table ? table.querySelectorAll('.js-limit-stat') : document.querySelectorAll('#limit-stats-table .js-limit-stat');
    if (!data || data.ok === false || data.error) {
        if (limitStatsLastOk) {
            data = limitStatsLastOk;
        } else {
            for (var i = 0; i < cells.length; i++) {
                cells[i].textContent = '--';
            }
            paintLimitStatsUpNumMa5SigmaStyles(null);
            paintLimitStatsPrevZtDtPctRiseFall(null);
            paintLimitStatsHlcRiseFall(null);
            paintLimitStatsHeaderBreadth(null);
            return;
        }
    } else {
        if (limitStatsLastOk) {
            data = mergeLimitStatsFieldData(limitStatsLastOk, data);
        }
        limitStatsLastOk = data;
    }
    patchLimitStatsDetailCells();
    for (var j = 0; j < cells.length; j++) {
        var el = cells[j];
        var k = el.getAttribute('data-k');
        if (!k) continue;
        var v = data[k];
        var next;
        if (k === 'prev_zt_pct' || k === 'prev_dt_pct') {
            next = formatLimitStatsPrevPoolPctNoSign(v);
        } else {
            next = (v === null || v === undefined || (typeof v === 'number' && isNaN(v))) ? '--' : String(v);
        }
        if (el.textContent !== next) {
            el.textContent = next;
        }
    }
    paintLimitStatsPrevZtDtPctRiseFall(data);
    /* T-5均：固定 id 写入，避免 querySelector 选错格或旧 DOM 无 data-k */
    function paintT5Cell(id, kk) {
        var cell = document.getElementById(id);
        if (!cell || !data) return;
        var vv = data[kk];
        var txt = (vv === null || vv === undefined || (typeof vv === 'number' && isNaN(vv))) ? '--' : String(vv);
        cell.textContent = txt;
    }
    paintT5Cell('js-limit-stat-zt-t5', 'zt_t5');
    paintT5Cell('js-limit-stat-dt-t5', 'dt_t5');
    paintLimitStatsUpNumMa5SigmaStyles(data);
    paintLimitStatsHlcRiseFall(data);
    paintLimitStatsHeaderBreadth(data);
    prefetchLimitStatsHisDetailCaches(data);
}

/** 面板刷新后后台预热 His 高/低明细（依赖服务端 stockstat 快路径 + 进程缓存，点开即可命中浏览器缓存） */
function warmLimitStatsDetailCache(kind) {
    if (!LIMIT_STAT_DETAIL_KINDS[kind]) return;
    if (!window.__rtMonitorLimitStatsDetailCache) window.__rtMonitorLimitStatsDetailCache = {};
    var cacheKey = kind + '|500';
    var rec = window.__rtMonitorLimitStatsDetailCache[cacheKey];
    if (rec && rec.expireAt > Date.now()) return;
    var url = rtMonitorApiUrl(
        '/api/limit_stats_detail?kind=' + encodeURIComponent(kind) + '&limit=500&_=' + Date.now()
    );
    fetchJsonNoGuard(url, 12000)
        .then(function (o) {
            var d = o && o.data;
            if (!d || !Array.isArray(d.rows)) return;
            window.__rtMonitorLimitStatsDetailCache[cacheKey] = {
                expireAt: Date.now() + 6000,
                data: d,
            };
        })
        .catch(function () {});
}

function prefetchLimitStatsHisDetailCaches(data) {
    if (!data || data.ok === false) return;
    var kinds = ['cnt_h_high', 'cnt_l_low'];
    for (var i = 0; i < kinds.length; i++) {
        var k = kinds[i];
        var v = data[k];
        var n = typeof v === 'number' ? v : parseInt(String(v || '').replace(/,/g, ''), 10);
        if (!isNaN(n) && n > 0) warmLimitStatsDetailCache(k);
    }
}

function formatLbztAmountYi(value) {
    if (value === null || value === undefined || (typeof value === 'number' && isNaN(value))) return '--';
    var n = parseFloat(value);
    return isNaN(n) ? '--' : n.toFixed(2);
}

/** 涨跌家数表：昨涨Pct/昨跌Pct — 正值不显示+（红），负值显示负号（绿），颜色由 paintLimitStatsPrevZtDtPctRiseFall 的 .rise/.fall */
function formatLimitStatsPrevPoolPctNoSign(value) {
    if (value === null || value === undefined || (typeof value === 'number' && isNaN(value))) return '--';
    var n = parseFloat(value);
    if (isNaN(n)) return '--';
    if (n < 0) return '-' + Math.abs(n).toFixed(2);
    return n.toFixed(2);
}

function formatLbztPct(value) {
    if (value === null || value === undefined || (typeof value === 'number' && isNaN(value))) return '--';
    var n = parseFloat(value);
    if (isNaN(n)) return '--';
    return (n >= 0 ? '+' : '') + n.toFixed(2);
}

function loadLbztDetailModalLayout() {
    try {
        if (typeof localStorage === 'undefined') return null;
        var raw = localStorage.getItem(LBZT_DETAIL_MODAL_LAYOUT_STORAGE_KEY);
        if (!raw) return null;
        var o = JSON.parse(raw);
        if (typeof o.w !== 'number' || typeof o.h !== 'number') return null;
        if (o.w < 320 || o.h < 320) return null;
        return {
            w: o.w,
            h: o.h,
            l: typeof o.l === 'number' ? o.l : null,
            t: typeof o.t === 'number' ? o.t : null,
        };
    } catch (e) {
        return null;
    }
}

function saveLbztDetailModalLayout(modalEl) {
    try {
        if (!modalEl || typeof localStorage === 'undefined') return;
        var rect = modalEl.getBoundingClientRect();
        var w = Math.round(rect.width);
        var h = Math.round(rect.height);
        var l = Math.round(rect.left);
        var t = Math.round(rect.top);
        if (w < 320 || h < 320) return;
        localStorage.setItem(LBZT_DETAIL_MODAL_LAYOUT_STORAGE_KEY, JSON.stringify({ w: w, h: h, l: l, t: t }));
    } catch (e) {
        /* ignore */
    }
}

function applyLbztDetailModalSavedLayout() {
    var overlay = document.getElementById('lbzt-detail-modal-overlay');
    var modal = overlay && overlay.querySelector('.lbzt-detail-modal-shell');
    if (!modal) return;
    var lay = loadLbztDetailModalLayout();
    if (!lay) return;
    var vw = window.innerWidth || document.documentElement.clientWidth;
    var vh = window.innerHeight || document.documentElement.clientHeight;
    var minW = 320;
    var minH = 320;
    var w = Math.min(Math.max(lay.w, minW), Math.max(minW, vw - 4));
    var h = Math.min(Math.max(lay.h, minH), Math.max(minH, vh - 4));
    modal.style.width = w + 'px';
    modal.style.height = h + 'px';
    modal.style.transform = 'none';
    var l = lay.l != null ? lay.l : (vw - w) / 2;
    var t = lay.t != null ? lay.t : (vh - h) / 2;
    l = Math.max(0, Math.min(l, vw - w));
    t = Math.max(0, Math.min(t, vh - h));
    modal.style.left = l + 'px';
    modal.style.top = t + 'px';
}

function scheduleSaveLbztDetailModalLayout(modalEl) {
    if (!modalEl) return;
    clearTimeout(lbztDetailModalSaveTimer);
    lbztDetailModalSaveTimer = setTimeout(function () {
        if (Date.now() - lbztDetailModalLayoutOpenTs < 700) return;
        saveLbztDetailModalLayout(modalEl);
    }, 400);
}

function openLbztDetailModal() {
    var overlay = document.getElementById('lbzt-detail-modal-overlay');
    if (!overlay) return;
    lbztDetailModalLayoutOpenTs = Date.now();
    overlay.classList.add('is-open');
    _modalOpened();
    applyLbztDetailModalSavedLayout();
}

function closeLbztDetailModal() {
    var overlay = document.getElementById('lbzt-detail-modal-overlay');
    var modal = overlay && overlay.querySelector('.lbzt-detail-modal-shell');
    var wasOpen = overlay && overlay.classList.contains('is-open');
    if (wasOpen && modal) saveLbztDetailModalLayout(modal);
    if (overlay) overlay.classList.remove('is-open');
    abortPopupFetch('limitStatsDetail');
    if (wasOpen) _modalClosed();
}

function showLbztDetailMsg(text) {
    var el = document.getElementById('lbzt-detail-msg');
    if (!el) return;
    el.textContent = text || '';
    el.style.display = text ? 'flex' : 'none';
}

function renderLbztDetailTable(rows) {
    var theadRow = document.querySelector('#lbzt-detail-table thead tr');
    if (theadRow && theadRow.children) {
        if (theadRow.children.length < 6) {
            var thReason = document.createElement('th');
            thReason.textContent = '涨停原因';
            theadRow.appendChild(thReason);
        }
        if (theadRow.children.length < 7) {
            var thTag = document.createElement('th');
            thTag.textContent = '涨停标签';
            theadRow.appendChild(thTag);
        }
    }
    var tbody = document.getElementById('lbzt-detail-table-body');
    if (!tbody) return;
    var list = Array.isArray(rows) ? rows : [];
    limitStatsDetailCurrentRows = list;
    var colCount = (theadRow && theadRow.children && theadRow.children.length) ? theadRow.children.length : 7;
    if (list.length === 0) {
        tbody.innerHTML = '<tr><td colspan="' + colCount + '">暂无数据</td></tr>';
        return;
    }
    tbody.innerHTML = list.map(function (row, i) {
        var trClass = (i % 2 === 1) ? ' median-table-zebra' : '';
        var pct = row && row.pctchange != null ? parseFloat(row.pctchange) : null;
        var pctClass = (pct == null || isNaN(pct)) ? '' : (pct >= 0 ? ' rise' : ' fall');
        return '<tr class="median-table-row' + trClass + '">' +
            '<td class="median-table-cat">' + (row.windcodes || '--') + '</td>' +
            '<td class="median-table-cat">' + (row.sec_name || '--') + '</td>' +
            '<td class="median-table-num">' + formatLbztAmountYi(row && row.amount_yi) + '</td>' +
            '<td class="median-table-num' + pctClass + '">' + formatLbztPct(row && row.pctchange) + '</td>' +
            '<td class="median-table-cat">' + (row.sw_name || '--') + '</td>' +
            '<td class="median-table-cat">' + (row.lu_desc || '--') + '</td>' +
            '<td class="median-table-cat">' + (row.tag || '--') + '</td>' +
            '</tr>';
    }).join('');
}

function extractWindcodesFromLimitStatsRows(rows) {
    var list = Array.isArray(rows) ? rows : [];
    var out = [];
    var seen = {};
    for (var i = 0; i < list.length; i++) {
        var row = list[i] || {};
        var raw = String(row.windcodes || row.ts_code || '').trim().toUpperCase();
        if (!/^\d{6}\.(SZ|SH|BJ)$/.test(raw)) continue;
        if (seen[raw]) continue;
        seen[raw] = true;
        out.push(raw);
    }
    return out;
}

function jumpToFinaWithDetailRows(rows, kind) {
    var codes = extractWindcodesFromLimitStatsRows(rows);
    if (!codes.length) {
        showLbztDetailMsg('当前明细无有效股票代码，无法跳转到财报页。');
        return;
    }
    var source = LIMIT_STATS_FINA_IMPORT_KIND_MAP[kind];
    if (!source) {
        showLbztDetailMsg('当前明细暂不支持一键带入财报页。');
        return;
    }
    var payload = {
        source: source,
        tsCodes: codes,
        tsCodeExpr: codes.join(' or '),
        createdAt: Date.now(),
    };
    try {
        if (typeof sessionStorage !== 'undefined') {
            sessionStorage.setItem(RT_MONITOR_FINA_IMPORT_SESSION_KEY, JSON.stringify(payload));
        }
    } catch (e) {
        logError('写入财报页导入上下文失败', e);
    }
    var targetUrl = rtMonitorApiUrl(
        '/rt-monitor/fina?import_source=' + encodeURIComponent(source) + '&import_key=' + encodeURIComponent(RT_MONITOR_FINA_IMPORT_SESSION_KEY)
    );
    var newTab = window.open(targetUrl, '_blank');
    if (!newTab) {
        showLbztDetailMsg('浏览器拦截了新标签页，请允许弹窗后重试。');
    }
}

/** 拉取一次明细 URL，若非 JSON 则 reject（用于多 URL 回退） */
function fetchLimitStatsDetailOnce(url, signal, token) {
    var opts = { cache: 'no-store' };
    if (signal) opts.signal = signal;
    return fetch(url, opts).then(function (res) {
        var ct = (res.headers.get('content-type') || '').toLowerCase();
        if (ct.indexOf('application/json') === -1) {
            return Promise.reject({ _detailRetry: true, status: res.status });
        }
        return res.json().then(function (data) {
            return { res: res, data: data, token: token };
        });
    }).catch(function (err) {
        if (err && typeof err === 'object') err.__popupToken = token;
        throw err;
    });
}

function fetchLimitStatsDetail(kind) {
    var loadingEl = document.getElementById('lbzt-detail-loading');
    var titleEl = document.getElementById('lbzt-detail-modal-title');
    var openFinaBtn = document.getElementById('lbzt-detail-open-fina-btn');
    if (titleEl) titleEl.textContent = LIMIT_STAT_DETAIL_TITLES[kind] || '明细';
    if (openFinaBtn) {
        var canJumpFina = !!LIMIT_STATS_FINA_IMPORT_KIND_MAP[kind];
        openFinaBtn.disabled = !canJumpFina;
        openFinaBtn.title = canJumpFina
            ? '将当前明细个股带入财报页（重置筛选并使用最新报告期）'
            : '仅涨停明细、涨停T-1、850日新高、历史新高明细支持一键带入财报页';
    }
    if (loadingEl) loadingEl.style.display = 'flex';
    showLbztDetailMsg('');
    var lim = 'limit=500';
    var kq = encodeURIComponent(kind);
    var bust = '&_=' + Date.now();
    var detailFetchRec = beginPopupFetch('limitStatsDetail');
    var detailFetchToken = detailFetchRec.token;
    var detailFetchSignal = detailFetchRec.controller ? detailFetchRec.controller.signal : null;
    function finishDetailFetch() {
        endPopupFetch('limitStatsDetail', detailFetchToken);
    }
    if (!window.__rtMonitorLimitStatsDetailCache) window.__rtMonitorLimitStatsDetailCache = {};
    limitStatsDetailCurrentKind = kind;
    var cacheKey = kind + '|500';
    var cacheRec = window.__rtMonitorLimitStatsDetailCache[cacheKey];
    var nowTs = Date.now();
    if (cacheRec && cacheRec.expireAt > nowTs && cacheRec.data && Array.isArray(cacheRec.data.rows)) {
        if (loadingEl) loadingEl.style.display = 'none';
        showLbztDetailMsg('');
        renderLbztDetailTable(cacheRec.data.rows);
        finishDetailFetch();
        return;
    }
    /*
     * URL 顺序（根因：部分环境按「路径」缓存 GET，面板 /api/limit_stats 与 ?detail=1 同路径会误返回面板 JSON，无 rows）。
     * 连板涨停第二备用 /detail/lb_zt 路径不同故能成功；其它 kind 必须把「非 limit_stats 路径」放最前。
     */
    var urls = [];
    if (kind === 'cnt_lb_zt') {
        urls.push(rtMonitorApiUrl('/api/limit_stats/detail/lb_zt?' + lim + bust));
    }
    urls.push(
        rtMonitorApiUrl('/api/limit_stats_detail?kind=' + kq + '&' + lim + bust),
        rtMonitorApiUrl('/api/limit_stats/detail/' + kq + '?' + lim + bust),
        rtMonitorApiUrl('/api/limit_stats/detail?kind=' + kq + '&' + lim + bust),
        rtMonitorApiUrl('/api/limit_stats?detail=1&kind=' + kq + '&' + lim + bust)
    );
    function tryIndex(i) {
        if (i >= urls.length) {
            if (loadingEl) loadingEl.style.display = 'none';
            showLbztDetailMsg('明细接口不可用（多次尝试均为 404 或非 JSON）。请结束旧进程后重新运行 rt_monitor_app.py，并在浏览器 Ctrl+F5 强刷。');
            renderLbztDetailTable([]);
            finishDetailFetch();
            return;
        }
        var url = urls[i];
        fetchLimitStatsDetailOnce(url, detailFetchSignal, detailFetchToken)
            .then(function (o) {
                if (!isPopupFetchCurrent('limitStatsDetail', detailFetchToken)) return;
                if (o.res.status === 404) {
                    tryIndex(i + 1);
                    return;
                }
                /* 误命中面板缓存时 JSON 含 pct_gt5、cnt_lb_zt 等但无 rows 数组，勿当成空明细 */
                if (!o.data || !Array.isArray(o.data.rows)) {
                    tryIndex(i + 1);
                    return;
                }
                if (loadingEl) loadingEl.style.display = 'none';
                if (!o.res.ok || o.data.ok === false) {
                    showLbztDetailMsg((o.data && o.data.error) ? o.data.error : ('请求失败: HTTP ' + o.res.status));
                    renderLbztDetailTable([]);
                    finishDetailFetch();
                    return;
                }
                showLbztDetailMsg('');
                window.__rtMonitorLimitStatsDetailCache[cacheKey] = {
                    expireAt: Date.now() + 6000,
                    data: o.data,
                };
                renderLbztDetailTable(o.data.rows);
                finishDetailFetch();
            })
            .catch(function (err) {
                if (!isPopupFetchCurrent('limitStatsDetail', detailFetchToken)) return;
                if (err && err._detailRetry) {
                    tryIndex(i + 1);
                    return;
                }
                logError('涨跌家数明细请求失败 ' + kind + ' ' + url, err);
                tryIndex(i + 1);
            });
    }
    tryIndex(0);
}

function initLimitStatsDetailModal() {
    patchLimitStatsT5FromLegacyMuted();
    patchLimitStatsDetailCells();
    var overlay = document.getElementById('lbzt-detail-modal-overlay');
    var closeBtn = document.getElementById('lbzt-detail-modal-close');
    var openFinaBtn = document.getElementById('lbzt-detail-open-fina-btn');
    if (closeBtn) closeBtn.addEventListener('click', closeLbztDetailModal);
    if (openFinaBtn && !openFinaBtn.dataset.boundOpenFina) {
        openFinaBtn.dataset.boundOpenFina = '1';
        openFinaBtn.addEventListener('click', function () {
            if (!LIMIT_STATS_FINA_IMPORT_KIND_MAP[limitStatsDetailCurrentKind]) {
                showLbztDetailMsg('仅「涨停明细 / 涨停T-1 / 850日新高 / 历史新高」明细支持一键带入财报页。');
                return;
            }
            jumpToFinaWithDetailRows(limitStatsDetailCurrentRows, limitStatsDetailCurrentKind);
        });
    }
    if (overlay) {
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) closeLbztDetailModal();
        });
    }
    var table = document.getElementById('limit-stats-table');
    if (!table) return;
    function onLimitStatsTableActivate(e) {
        var cell = e.target && e.target.closest && e.target.closest('.js-limit-stat');
        if (!cell) return;
        if (cell.id === 'js-limit-stat-up-num-ma5-rt' && cell.classList.contains('bias-cell-clickable')) {
            if (e.type === 'keydown' && e.key === ' ') e.preventDefault();
            if (e.type === 'click') e.preventDefault();
            openUpNumMa5ExpandModal();
            return;
        }
        var k = cell.getAttribute('data-k');
        if (!k || !LIMIT_STAT_DETAIL_KINDS[k]) return;
        if (e.type === 'keydown' && e.key === ' ') e.preventDefault();
        openLbztDetailModal();
        fetchLimitStatsDetail(k);
    }
    table.addEventListener('click', onLimitStatsTableActivate);
    table.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        onLimitStatsTableActivate(e);
    });
    var lbztOv = document.getElementById('lbzt-detail-modal-overlay');
    var lbztModal = lbztOv && lbztOv.querySelector('.lbzt-detail-modal-shell');
    var lbztHeader = lbztOv && lbztOv.querySelector('.lbzt-detail-modal-header');
    if (lbztOv && lbztModal && lbztHeader && lbztOv.dataset.lbztModalLayoutBound !== '1') {
        lbztOv.dataset.lbztModalLayoutBound = '1';
        lbztModal.style.position = 'fixed';
        lbztModal.style.left = '50%';
        lbztModal.style.top = '50%';
        lbztModal.style.transform = 'translate(-50%, -50%)';
        var lbztDragging = false;
        var lbztStartX = 0;
        var lbztStartY = 0;
        var lbztStartLeft = 0;
        var lbztStartTop = 0;
        lbztHeader.style.cursor = 'move';
        lbztHeader.addEventListener('mousedown', function (e) {
            if (e.button !== 0) return;
            lbztDragging = true;
            lbztStartX = e.clientX;
            lbztStartY = e.clientY;
            var rect = lbztModal.getBoundingClientRect();
            lbztStartLeft = rect.left;
            lbztStartTop = rect.top;
            lbztModal.style.transform = '';
            e.preventDefault();
        });
        document.addEventListener('mousemove', function (e) {
            if (!lbztDragging) return;
            var dx = e.clientX - lbztStartX;
            var dy = e.clientY - lbztStartY;
            var newLeft = lbztStartLeft + dx;
            var newTop = lbztStartTop + dy;
            var vw = window.innerWidth || document.documentElement.clientWidth;
            var vh = window.innerHeight || document.documentElement.clientHeight;
            var rect2 = lbztModal.getBoundingClientRect();
            var maxLeft = vw - rect2.width;
            var maxTop = vh - rect2.height;
            if (newLeft < 0) newLeft = 0;
            if (newTop < 0) newTop = 0;
            if (newLeft > maxLeft) newLeft = maxLeft;
            if (newTop > maxTop) newTop = maxTop;
            lbztModal.style.left = newLeft + 'px';
            lbztModal.style.top = newTop + 'px';
        });
        document.addEventListener('mouseup', function () {
            if (lbztDragging) saveLbztDetailModalLayout(lbztModal);
            lbztDragging = false;
        });
        if (typeof ResizeObserver !== 'undefined') {
            var roLbztShell = new ResizeObserver(function () {
                scheduleSaveLbztDetailModalLayout(lbztModal);
            });
            roLbztShell.observe(lbztModal);
        }
    }
    if (document.body.dataset.lbztModalEscBound !== '1') {
        document.body.dataset.lbztModalEscBound = '1';
        document.addEventListener('keydown', function (e) {
            if (e.key !== 'Escape') return;
            var ov = document.getElementById('lbzt-detail-modal-overlay');
            if (!ov || !ov.classList.contains('is-open')) return;
            closeLbztDetailModal();
        });
    }
}

function fetchMaSignalTable() {
    var section = document.getElementById('ma-signal-table-section');
    function ensureMaSrSync() {
        if (maSrSyncStarted) return;
        maSrSyncStarted = true;
        syncMaSrLevelsFromServer(function () {
            if (maSignalTableLastPayload) {
                renderMaSignalTable(maSignalTableLastPayload);
            }
        });
    }
    function doFetch() {
        if (!beginPollFetch('maSignal')) return;
        fetchJsonNoGuard(rtMonitorApiUrl('/api/etf_ma_signal_table'), 12000)
            .then(function (o) {
                var data = o && o.data ? o.data : {};
                var rows = Array.isArray(data.rows) ? data.rows : [];
                if (!o.ok || data.error) {
                    if (section) section.setAttribute('data-fetch-state', 'stale');
                    logError('MA 信号表接口返回错误', data && data.error ? data.error : ('HTTP ' + (o ? o.status : '--')));
                    if (maSignalTableLastOk) {
                        renderMaSignalTable(Object.assign({}, maSignalTableLastOk, { __stale: true }));
                    }
                    return;
                }
                if (rows.length > 0) {
                    maSignalTableLastOk = data;
                    if (section) section.setAttribute('data-fetch-state', 'ok');
                    renderMaSignalTable(data);
                    return;
                }
                if (section) section.setAttribute('data-fetch-state', (data.warming ? 'loading' : 'stale'));
                if (maSignalTableLastOk) {
                    renderMaSignalTable(Object.assign({}, maSignalTableLastOk, { __stale: true }));
                } else {
                    renderMaSignalTable({ rows: [], __loading: true, warming: !!data.warming });
                }
            })
            .catch(function (err) {
                if (section) section.setAttribute('data-fetch-state', 'stale');
                logError('MA 信号表请求失败', err);
                if (maSignalTableLastOk) {
                    renderMaSignalTable(Object.assign({}, maSignalTableLastOk, { __stale: true }));
                } else {
                    renderMaSignalTable({ rows: [], __loading: true });
                }
            })
            .finally(function () {
                endPollFetch('maSignal');
            });
    }
    // 首屏优先拉表；价位配置只在页面生命周期内同步一次，避免轮询额外并发。
    doFetch();
    ensureMaSrSync();
}

/** MA 支撑/阻力格：自定义浮层（约 60ms 出现，避免原生 title 约 1s 延迟） */
var MA_SR_TOOLTIP_DELAY_MS = 60;
var maSrTooltipTimer = null;
var maSrTooltipEl = null;

function ensureMaSrTooltipEl() {
    if (maSrTooltipEl) return maSrTooltipEl;
    maSrTooltipEl = document.getElementById('ma-sr-tooltip');
    if (!maSrTooltipEl) {
        maSrTooltipEl = document.createElement('div');
        maSrTooltipEl.id = 'ma-sr-tooltip';
        maSrTooltipEl.className = 'ma-sr-tooltip-popover';
        maSrTooltipEl.setAttribute('role', 'tooltip');
        document.body.appendChild(maSrTooltipEl);
    }
    return maSrTooltipEl;
}

function positionMaSrTooltip(clientX, clientY) {
    var el = ensureMaSrTooltipEl();
    var pad = 10;
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var w = el.offsetWidth;
    var h = el.offsetHeight;
    var left = clientX + pad;
    var top = clientY + pad;
    if (left + w > vw - 8) left = vw - w - 8;
    if (top + h > vh - 8) top = clientY - h - pad;
    if (left < 8) left = 8;
    if (top < 8) top = 8;
    el.style.left = left + 'px';
    el.style.top = top + 'px';
}

function showMaSrTooltipFromCell(cell, clientX, clientY) {
    var el = ensureMaSrTooltipEl();
    var raw = cell.getAttribute('data-ma-sr-tip-json');
    if (raw) {
        try {
            var o = JSON.parse(raw);
            el.innerHTML = o.html || '';
        } catch (err) {
            el.textContent = cell.getAttribute('aria-label') || '';
        }
    } else {
        el.textContent = cell.getAttribute('aria-label') || '';
    }
    el.removeAttribute('hidden');
    el.setAttribute('aria-hidden', 'false');
    el.classList.add('is-visible');
    requestAnimationFrame(function () {
        positionMaSrTooltip(clientX, clientY);
    });
}

function hideMaSrTooltip() {
    if (maSrTooltipTimer) {
        clearTimeout(maSrTooltipTimer);
        maSrTooltipTimer = null;
    }
    var el = document.getElementById('ma-sr-tooltip');
    if (!el) return;
    el.setAttribute('hidden', '');
    el.setAttribute('aria-hidden', 'true');
    el.classList.remove('is-visible');
    el.innerHTML = '';
    el.textContent = '';
}

function initMaSrTooltipDelegation() {
    var tb = document.getElementById('ma-signal-table-body');
    if (!tb || tb.getAttribute('data-ma-sr-tip-bound') === '1') return;
    tb.setAttribute('data-ma-sr-tip-bound', '1');
    tb.addEventListener('pointerover', function (e) {
        var cell = e.target.closest && e.target.closest('td.ma-sr-cell[data-ma-sr-tip-json]');
        if (!cell) return;
        if (maSrTooltipTimer) clearTimeout(maSrTooltipTimer);
        var cx = e.clientX;
        var cy = e.clientY;
        var tipCell = cell;
        maSrTooltipTimer = setTimeout(function () {
            showMaSrTooltipFromCell(tipCell, cx, cy);
        }, MA_SR_TOOLTIP_DELAY_MS);
    });
    tb.addEventListener('pointerout', function (e) {
        var cell = e.target.closest && e.target.closest('td.ma-sr-cell[data-ma-sr-tip-json]');
        if (!cell) return;
        var rt = e.relatedTarget;
        if (rt && cell.contains(rt)) return;
        if (rt && tb.contains(rt) && rt.closest && rt.closest('td.ma-sr-cell[data-ma-sr-tip-json]')) return;
        hideMaSrTooltip();
    });
    tb.addEventListener('pointermove', function (e) {
        var cell = e.target.closest && e.target.closest('td.ma-sr-cell[data-ma-sr-tip-json]');
        if (!cell) return;
        var el = document.getElementById('ma-sr-tooltip');
        if (!el || el.hasAttribute('hidden')) return;
        positionMaSrTooltip(e.clientX, e.clientY);
    });
}

function initMaSignalControls() {
    populateMaSignalTsSelect();
    var sel = document.getElementById('ma-signal-ts-select');
    if (sel) {
        sel.addEventListener('change', function () {
            maSignalSelectedTs = sel.value || maSignalSelectedTs;
        });
    }
    var tbody = document.getElementById('ma-signal-table-body');
    if (tbody) {
        tbody.addEventListener('click', function (e) {
            var tr = e.target && e.target.closest && e.target.closest('tr.ma-signal-row');
            if (!tr) return;
            var code = tr.getAttribute('data-ts-code');
            if (!code) return;
            maSignalSelectedTs = code;
            if (sel) sel.value = code;
        });
    }
    [].forEach.call(document.querySelectorAll('.ma-signal-freq-btn'), function (btn) {
        btn.addEventListener('click', function () {
            var f = btn.getAttribute('data-freq');
            if (!f) return;
            maSignalSelectedFreq = f;
            [].forEach.call(document.querySelectorAll('.ma-signal-freq-btn'), function (b) {
                b.classList.toggle('is-active', b.getAttribute('data-freq') === f);
            });
        });
    });
}

// 默认刷新间隔（秒）；与后端 RT_MONITOR_POLL_TTL_SEC 一致；若输入为空或无效则用此值
var DEFAULT_REFRESH_INTERVAL_SEC = 1;

function getIntervalSeconds() {
    var el = document.getElementById('refresh-interval');
    if (!el) return DEFAULT_REFRESH_INTERVAL_SEC;
    var sec = parseInt(el.value, 10);
    if (isNaN(sec) || sec < 0) return DEFAULT_REFRESH_INTERVAL_SEC;
    return sec;
}

/** 浏览器可能把旧默认 2 用 autofill 填回；启动时强制与当前默认一致 */
function applyDefaultRefreshInterval() {
    var el = document.getElementById('refresh-interval');
    if (!el) return;
    el.value = String(DEFAULT_REFRESH_INTERVAL_SEC);
}

function pollHomeRealtime() {
    if (document.hidden) {
        return;
    }
    requestAnimationFrame(function () {
        fetchRtMedian();
        fetchBiasTable();
        fetchMaSignalTable();
        fetchLimitStatsPanel();
        fetchIdxMinuteDiff();
        fetchIdxMinuteDiffPrev5();
        fetchTopMoves();
    });
}

function startAutoRefresh() {
    if (refreshTimerId) {
        clearInterval(refreshTimerId);
        refreshTimerId = null;
    }
    var autoEl = document.getElementById('auto-refresh');
    if (!autoEl || !autoEl.checked) return;
    var intervalSec = getIntervalSeconds();
    if (intervalSec <= 0) return;
    /* 日频融资融券双图不参与间隔轮询，见 refreshDailyMarginCharts / 顶栏「刷新日频」 */
    /* MA 信号表：区间涨跌幅为 Wind 口径并拼 rt_k 期末价，与本间隔同步刷新 */
    refreshTimerId = setInterval(pollHomeRealtime, intervalSec * 1000);
}

function stopAutoRefresh() {
    if (refreshTimerId) {
        clearInterval(refreshTimerId);
        refreshTimerId = null;
    }
}

/** 绑定刷新与配置相关控件；DOM 缺失时不绑定，避免脚本报错。应在 load 后调用 */
function bindRefreshControls() {
    var autoRefreshEl = document.getElementById('auto-refresh');
    var intervalEl = document.getElementById('refresh-interval');
    var btnRefresh = document.getElementById('btn-refresh');
    if (autoRefreshEl) {
        autoRefreshEl.addEventListener('change', function () {
            if (this.checked) startAutoRefresh(); else stopAutoRefresh();
        });
    }
    if (intervalEl) {
        intervalEl.addEventListener('input', function () {
            if (autoRefreshEl && autoRefreshEl.checked) startAutoRefresh();
        });
        intervalEl.addEventListener('blur', function () {
            if (autoRefreshEl && autoRefreshEl.checked) startAutoRefresh();
        });
    }
    if (btnRefresh) {
        btnRefresh.addEventListener('click', function () {
            requestAnimationFrame(function () {
                fetchRtMedian();
                fetchBiasTable();
                fetchMaSignalTable();
                fetchLimitStatsPanel();
                fetchIdxMinuteDiff();
                fetchIdxMinuteDiffPrev5();
                fetchTopMoves();
                refreshDailyMarginCharts();
            });
        });
    }
    var btnDaily = document.getElementById('btn-refresh-daily');
    if (btnDaily) btnDaily.addEventListener('click', function () {
        refreshDailyMarginChartsWithToast();
    });
    var btnMaSr = document.getElementById('btn-ma-sr-save');
    if (btnMaSr) {
        btnMaSr.addEventListener('click', function () {
            saveMaSrLevels(collectMaSrLevelsFromForm());
            if (maSignalTableLastPayload) {
                renderMaSignalTable(maSignalTableLastPayload);
            }
        });
    }
}

(function () {
    var trigger = document.getElementById('config-trigger');
    var panel = document.getElementById('config-panel');
    var closeBtn = document.getElementById('config-close');
    var topBar = document.getElementById('config-top-bar');
    if (!trigger || !panel || !closeBtn) return;
    function openPanel() {
        panel.classList.add('config-panel-visible');
        panel.setAttribute('aria-hidden', 'false');
        if (typeof renderMaSrConfigTable === 'function') {
            renderMaSrConfigTable(true);
        }
    }
    function closePanel() {
        panel.classList.remove('config-panel-visible');
        panel.setAttribute('aria-hidden', 'true');
    }
    trigger.addEventListener('click', function (e) {
        e.stopPropagation();
        if (panel.classList.contains('config-panel-visible')) closePanel();
        else openPanel();
    });
    closeBtn.addEventListener('click', closePanel);
    document.addEventListener('click', function (e) {
        if (!panel.classList.contains('config-panel-visible')) return;
        if (panel.contains(e.target)) return;
        if (topBar && topBar.contains(e.target)) return;
        closePanel();
    });
})();

/** 首块：纵两行；上行三张横排；下行成交额|涨跌家数；与 #rt-layout-force 一致 */
function applyMedianQuadRowLayout() {
    var row = document.getElementById('median-quad-row');
    if (!row) return;
    var gapPx = '3px';
    try {
        var root = window.getComputedStyle(document.documentElement);
        var q = root.getPropertyValue('--rt-monitor-quad-gap').trim();
        if (q) gapPx = q;
        else {
            var v = root.getPropertyValue('--rt-monitor-inline-table-gap').trim();
            if (v) gapPx = v;
        }
    } catch (e) { /* ignore */ }
    function flexRow(el) {
        if (!el) return;
        el.style.setProperty('display', 'flex', 'important');
        el.style.setProperty('flex-direction', 'row', 'important');
        el.style.setProperty('flex-wrap', 'nowrap', 'important');
        el.style.setProperty('align-items', 'flex-start', 'important');
        el.style.setProperty('justify-content', 'flex-start', 'important');
        el.style.setProperty('gap', gapPx, 'important');
        el.style.setProperty('margin', '0', 'important');
    }
    row.style.setProperty('display', 'flex', 'important');
    row.style.setProperty('flex-direction', 'column', 'important');
    row.style.setProperty('flex-wrap', 'nowrap', 'important');
    row.style.setProperty('align-items', 'flex-start', 'important');
    row.style.setProperty('justify-content', 'flex-start', 'important');
    row.style.setProperty('gap', gapPx, 'important');
    row.style.setProperty('width', 'max-content', 'important');
    row.style.setProperty('min-width', 'min-content', 'important');
    row.style.setProperty('max-width', 'none', 'important');
    row.style.setProperty('overflow-x', 'visible', 'important');
    flexRow(document.getElementById('median-quad-top-row'));
    flexRow(document.getElementById('median-quad-bottom-row'));
    ['idx-pct-table-section', 'median-table-section', 'uppct-table-section', 'amount-table-section', 'limit-stats-section'].forEach(function (id) {
        var sec = document.getElementById(id);
        if (!sec) return;
        sec.style.setProperty('margin', '0', 'important');
        sec.style.removeProperty('grid-column');
        sec.style.removeProperty('grid-row');
    });
    var container = document.querySelector('.rt-monitor .container');
    if (container) container.style.setProperty('overflow-x', 'auto', 'important');
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
        if (!window.__RT_MONITOR_MARGIN_ONLY__) applyMedianQuadRowLayout();
    });
} else {
    if (!window.__RT_MONITOR_MARGIN_ONLY__) applyMedianQuadRowLayout();
}

window.addEventListener('load', function () {
    if (!window.__RT_MONITOR_MARGIN_ONLY__) applyMedianQuadRowLayout();
    /* 财报筛选独立页已使用 static/js/rt_monitor_fina.js，不再在本脚本中初始化 */
    /* 两融独立页：仅融资融券双图 + 弹窗（见 rt_monitor_margin.html） */
    if (window.__RT_MONITOR_MARGIN_ONLY__) {
        initMarginDailyModal();
        initMarginRzmreIdxModal();
        if (hideTrendChartRowsWhenChartUnavailable()) {
            return;
        }
        showTrendChartRowsWhenChartReady();
        fetchMarginDailyRecent();
        fetchMarginRzmreIdxSeries();
        return;
    }
    /* 旧版 HTML 曾含底部 #limit-stats-meta（同花顺池日期）；缓存页面可能仍存在，加载时移除 */
    (function () {
        var legacyMeta = document.getElementById('limit-stats-meta');
        if (legacyMeta && legacyMeta.parentNode) legacyMeta.parentNode.removeChild(legacyMeta);
    })();
    initBiasSigmaToggle();
    initBiasSeriesModal();
    initMarginDailyModal();
    initMarginRzmreIdxModal();
    initUpNumMa5Modal();
    initAmountTop5PctModal();
    initLimitStatsDetailModal();
    initMaSrTooltipDelegation();
    applyDefaultRefreshInterval();
    if (hideTrendChartRowsWhenChartUnavailable()) {
        fetchRtMedian();
        fetchBiasTable();
        fetchMaSignalTable();
        fetchLimitStatsPanel();
        fetchTopMoves();
        startAutoRefresh();
        bindRefreshControls();
        return;
    }
    showTrendChartRowsWhenChartReady();
    fetchRtMedian();
    fetchBiasTable();
    fetchMaSignalTable();
    fetchLimitStatsPanel();
    fetchIdxMinuteDiff();
    fetchIdxMinuteDiffPrev5();
    fetchTopMoves();
    if (hasRtMonitorMarginMainCharts()) {
        fetchMarginDailyRecent();
        fetchMarginRzmreIdxSeries();
    }
    startAutoRefresh();
    bindRefreshControls();
});

document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    if (window.__RT_MONITOR_MARGIN_ONLY__) return;
    var autoEl = document.getElementById('auto-refresh');
    if (!autoEl || !autoEl.checked) return;
    pollHomeRealtime();
});

window.addEventListener('beforeunload', function () {
    stopAutoRefresh();
});
